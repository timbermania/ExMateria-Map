"""The round-trip instrument: dump -> build -> cmp over the whole corpus.

This converts "exmateria_map writes the exact same format GaneshaDx does" from a
claim into a measurement. It answers on **two axes**, and both are pass/fail:

**Coverage** -- how many files came back byte-identical, reported per class.
Coverage is achievable by construction (a builder may always carry bytes through
opaquely), so a coverage miss is unambiguously a bug. There is no exception list.

**Carry** -- how many bytes the builder reproduced *opaquely* rather than
reconstructing from its model. This is the axis that actually measures progress,
and it is a **ratchet**: it may never rise. The per-region breakdown is
diagnostic; the pass/fail is on the per-class total, so moving bytes between
regions during a refactor doesn't trip the guard.

Reporting contract on failure -- three distinct classes:

* ``length``  -- sizes differ. Reported separately because "first differing
  offset" is undefined when one file is a prefix of the other.
* ``bytes``   -- first differing offset, its owning section, expected vs actual,
  plus the total differing-byte count and the set of sections touched. First
  offset alone cannot tell "one wrong byte in Terrain" from "4,000 wrong bytes
  spanning Terrain and PolygonRenderProperties"; those are different bugs.
* ``carry``   -- cmp is green but carry rose. A ratchet that doesn't fail isn't one.

Attribution always runs against the **original**, never the rebuilt file: the
original is the oracle, and attributing against the writer's own output would let
a wrong pointer table relabel its own damage. When the 196-byte pointer table
itself differs, every section boundary in that file has moved, so the report says
so first and marks the rest of that file's attribution suspect.

There is no CI. What makes this binding is ``roundtrip_baseline.json``, checked
into git: a regression is a diff in a tracked file that someone has to explain.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Mapping, NamedTuple

from . import corpus
from .corpus import CorpusError, Resource
from .sections import (
    GNS_RECORD_BYTES,
    TEXTURE_ROW_BYTES,
    header_differs,
    mesh_sections,
    owning_section,
)

CHUNK = 4096
MAX_NAMES = 8

DEFAULT_BASELINE = Path(__file__).resolve().parent.parent / "roundtrip_baseline.json"


# --------------------------------------------------------------------------
# builder protocol
# --------------------------------------------------------------------------

class BuildResult(NamedTuple):
    """What a builder produces for one resource.

    ``carried`` maps a region name to the number of bytes reproduced opaquely --
    copied from the original rather than reconstructed from the model. A builder
    that models nothing declares its whole output as carried; that is the honest
    opening position, and the ratchet drives it down from there.
    """
    data: bytes
    carried: Mapping[str, int]


Builder = Callable[[Resource, bytes], BuildResult]


def identity_builder(resource: Resource, original: bytes) -> BuildResult:
    """Reproduce the file by copying it, declaring every byte as carried.

    Not a stub: it is the true zero point of the carry ratchet, and it is what
    the mutation seeds mutate. Replace it with the real writer as sections of the
    format become reconstructible; each one that moves out of ``carried`` is the
    ratchet advancing.
    """
    n = len(original)
    if resource.kind == "texture":
        carried = {"Texture": n}
    elif resource.kind == "gns":
        carried = {"GNS records": n}
    else:
        carried = {sec.name: sec.length for sec in mesh_sections(original)} or {"whole file": n}
    return BuildResult(original, carried)


class DocumentBuilder:
    """The real writer: ``dump`` the arrangement, ``build`` it back.

    This is what makes the instrument an oracle rather than a tautology. A
    resource is rebuilt through the *document* -- decoded to schema v1 and
    re-encoded from it -- so a field the schema does not carry, or carries
    wrongly, comes back as changed bytes with a section name attached.

    Not every corpus file is reachable that way, and the ones that are not are
    named rather than dropped: an arrangement with no ``0x40`` chunk has no
    ``geometry_source``, so it has no document at all (``skipped``), and the
    GNS is carried verbatim by the #372 patcher contract. Those fall back to
    the identity builder and declare their bytes as carried, which is exactly
    what they are.

    A resource shared by several arrangements is built by each of them and the
    results must agree; a disagreement is raised, not averaged. That is schema
    §10.6's fan-out correspondence measured rather than asserted.
    """

    def __init__(self, map_dir: Path | None = None, with_sheets: bool = True,
                 sidecar_dir: Path | None = None):
        from . import build as build_leg
        from . import dump as dump_leg
        self._build, self._dump = build_leg, dump_leg
        self.map_dir = map_dir or corpus.map_dir()
        self.with_sheets = with_sheets
        self._sidecar_dir = sidecar_dir
        self._built: dict[str, bytes] = {}
        self._modelled: dict[str, list[tuple[int, int]]] = {}
        self.skipped: list[tuple[str, str]] = []
        self.refused: list[tuple[str, str]] = []
        self.warnings: list[tuple[str, str]] = []
        self.arrangements = 0
        self._run()

    def _run(self) -> None:
        from .mapfile import BindError, map_numbers
        import tempfile
        sidecars = self._sidecar_dir
        tmp = None
        if self.with_sheets and sidecars is None:
            tmp = tempfile.TemporaryDirectory(prefix="exmateria-sheets-")
            sidecars = Path(tmp.name)
        try:
            for number in map_numbers(self.map_dir):
                try:
                    arrangements = self._dump.arrangements(self.map_dir, number)
                except BindError as exc:
                    self.skipped.append((f"MAP{number:03d}", str(exc)))
                    continue
                for a in arrangements:
                    label = f"MAP{number:03d}.a{a}"
                    try:
                        document, sheets = self._dump.dump(self.map_dir, number, a)
                    except self._dump.DumpError as exc:
                        self.skipped.append((label, str(exc)))
                        continue
                    if sidecars is not None:
                        self._write_sidecars(document, sheets, sidecars)
                    try:
                        bundle = self._build.build(document, self.map_dir,
                                                   sidecar_dir=sidecars)
                    except self._build.BuildRefusal as exc:
                        self.refused.append((label, str(exc)))
                        continue
                    self.arrangements += 1
                    for warning in bundle.warnings:
                        self.warnings.append((label, warning))
                    self._absorb(label, bundle)
        finally:
            if tmp is not None:
                tmp.cleanup()

    def _write_sidecars(self, document, sheets, directory: Path) -> None:
        from .png_indexed import unpack_4bpp, write_indexed_png
        palette = self._dump.sidecar_palette(document)
        for name, raw in sheets.items():
            path = Path(directory) / name
            if path.exists():
                continue                  # deduplicated by the sheet's digest
            path.write_bytes(write_indexed_png(unpack_4bpp(raw), palette))

    def _absorb(self, label: str, bundle) -> None:
        for name, data in bundle.resources.items():
            seen = self._built.get(name)
            if seen is not None and seen != data:
                raise RuntimeError(
                    f"{label}: {name} builds differently here than in an "
                    f"earlier arrangement -- the fan-out targets disagree"
                )
            self._built[name] = data
            self._modelled[name] = bundle.modelled.get(name, [])
        self._built.setdefault(bundle.gns_name, bundle.gns)
        self._modelled.setdefault(bundle.gns_name, [])

    def __call__(self, resource: Resource, original: bytes) -> BuildResult:
        data = self._built.get(resource.name)
        if data is None:
            return identity_builder(resource, original)
        spans = self._modelled.get(resource.name) or []
        if resource.kind == "texture":
            written = sum(end - start for start, end in spans)
            return BuildResult(data, {"Texture": len(original) - written})
        if resource.kind == "gns":
            return BuildResult(data, {"GNS records": len(original)})
        sections = mesh_sections(original)
        if not sections:
            return BuildResult(data, {"whole file": len(original)})
        carried = {sec.name: sec.length for sec in sections}
        for start, end in spans:
            for sec in sections:
                overlap = min(end, sec.end) - max(start, sec.start)
                if overlap > 0:
                    carried[sec.name] -= overlap
        return BuildResult(data, carried)


# --------------------------------------------------------------------------
# compare
# --------------------------------------------------------------------------

class Comparison(NamedTuple):
    name: str
    kind: str
    status: str                     # "match" | "length" | "bytes"
    original_len: int
    rebuilt_len: int
    first_offset: int | None
    owner: str | None
    expected_byte: int | None
    actual_byte: int | None
    differing_bytes: int
    regions_touched: tuple[str, ...]
    header_moved: bool

    @property
    def ok(self) -> bool:
        return self.status == "match"


def _first_difference(a: bytes, b: bytes) -> int | None:
    n = min(len(a), len(b))
    for base in range(0, n, CHUNK):
        end = min(base + CHUNK, n)
        if a[base:end] != b[base:end]:
            for i in range(base, end):
                if a[i] != b[i]:
                    return i
    return None


def _regions_touched(original: bytes, rebuilt: bytes, kind: str) -> tuple[str, ...]:
    """Names of the regions containing at least one differing byte.

    Uses slice comparison per region rather than per-byte attribution, so a file
    where everything differs costs one compare per section, not one per byte.
    """
    n = min(len(original), len(rebuilt))
    if kind == "mesh":
        spans = [(s.name, s.start, s.end) for s in mesh_sections(original)]
    elif kind == "gns":
        spans = [
            (f"record[{i}]", i * GNS_RECORD_BYTES, (i + 1) * GNS_RECORD_BYTES)
            for i in range((n + GNS_RECORD_BYTES - 1) // GNS_RECORD_BYTES)
        ]
    else:
        spans = [
            (f"row {i}", i * TEXTURE_ROW_BYTES, (i + 1) * TEXTURE_ROW_BYTES)
            for i in range((n + TEXTURE_ROW_BYTES - 1) // TEXTURE_ROW_BYTES)
        ]
    hit = [
        name
        for name, start, end in spans
        if start < n and original[start:min(end, n)] != rebuilt[start:min(end, n)]
    ]
    if len(hit) > MAX_NAMES:
        return (*hit[:MAX_NAMES], f"...(+{len(hit) - MAX_NAMES} more)")
    return tuple(hit)


def compare(resource: Resource, original: bytes, rebuilt: bytes) -> Comparison:
    kind = resource.kind
    base = dict(
        name=resource.name,
        kind=kind,
        original_len=len(original),
        rebuilt_len=len(rebuilt),
        header_moved=len(rebuilt) >= 196 and header_differs(original, rebuilt, kind),
    )
    if original == rebuilt:
        return Comparison(
            status="match", first_offset=None, owner=None, expected_byte=None,
            actual_byte=None, differing_bytes=0, regions_touched=(), **base,
        )

    offset = _first_difference(original, rebuilt)
    shared = min(len(original), len(rebuilt))
    differing = sum(1 for i in range(shared) if original[i] != rebuilt[i])
    differing += abs(len(original) - len(rebuilt))

    if len(original) != len(rebuilt):
        status = "length"
    else:
        status = "bytes"

    return Comparison(
        status=status,
        first_offset=offset,
        owner=owning_section(original, offset, kind) if offset is not None else None,
        expected_byte=original[offset] if offset is not None else None,
        actual_byte=rebuilt[offset] if offset is not None else None,
        differing_bytes=differing,
        regions_touched=_regions_touched(original, rebuilt, kind),
        **base,
    )


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

class Report(NamedTuple):
    comparisons: list[Comparison]
    matched: dict[str, int]
    total: dict[str, int]
    total_bytes: dict[str, int]
    carry: dict[str, dict[str, int]]        # kind -> region -> bytes

    @property
    def failures(self) -> list[Comparison]:
        return [c for c in self.comparisons if not c.ok]

    @property
    def carry_total(self) -> dict[str, int]:
        return {k: sum(v.values()) for k, v in self.carry.items()}

    @property
    def coverage_ok(self) -> bool:
        return self.matched == self.total

    def as_baseline(self) -> dict:
        return {
            "coverage": self.matched,
            "total": self.total,
            "total_bytes": self.total_bytes,
            "carry_total": self.carry_total,
            "carry_by_region": {k: dict(sorted(v.items())) for k, v in self.carry.items()},
        }


def run(resources: list[Resource], builder: Builder = identity_builder) -> Report:
    comparisons: list[Comparison] = []
    matched: Counter[str] = Counter()
    total: Counter[str] = Counter()
    total_bytes: Counter[str] = Counter()
    carry: dict[str, Counter[str]] = defaultdict(Counter)

    for resource in resources:
        original = resource.path.read_bytes()
        built = builder(resource, original)
        result = compare(resource, original, built.data)
        comparisons.append(result)
        total[resource.kind] += 1
        total_bytes[resource.kind] += len(original)
        matched[resource.kind] += 1 if result.ok else 0
        for region, count in built.carried.items():
            carry[resource.kind][region] += count

    for kind in corpus.CLASSES:
        total.setdefault(kind, 0)
        matched.setdefault(kind, 0)
        total_bytes.setdefault(kind, 0)
        carry.setdefault(kind, Counter())

    return Report(
        comparisons=comparisons,
        matched=dict(matched),
        total=dict(total),
        total_bytes=dict(total_bytes),
        carry={k: dict(v) for k, v in carry.items()},
    )


# --------------------------------------------------------------------------
# ratchet
# --------------------------------------------------------------------------

class Regression(NamedTuple):
    kind: str
    baseline: int
    current: int


def carry_regressions(report: Report, baseline: dict) -> list[Regression]:
    """Classes whose opaque-carry total rose. Rising carry fails the harness."""
    recorded = baseline.get("carry_total", {})
    out = []
    for kind, current in sorted(report.carry_total.items()):
        before = recorded.get(kind)
        if before is not None and current > before:
            out.append(Regression(kind, before, current))
    return out


def load_baseline(path: Path = DEFAULT_BASELINE) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def format_report(report: Report, regressions: list[Regression]) -> str:
    lines: list[str] = []
    lines.append(f"{'class':<10}{'matched':>16}{'bytes':>14}{'carried':>14}{'carry %':>9}")
    lines.append("-" * 63)
    for kind in corpus.CLASSES:
        total = report.total.get(kind, 0)
        matched = report.matched.get(kind, 0)
        nbytes = report.total_bytes.get(kind, 0)
        carried = report.carry_total.get(kind, 0)
        pct = (100 * carried / nbytes) if nbytes else 0.0
        lines.append(
            f"{kind:<10}{f'{matched}/{total}':>16}{nbytes:>14,}{carried:>14,}{pct:>8.2f}%"
        )
    lines.append("-" * 63)
    grand_bytes = sum(report.total_bytes.values())
    grand_carry = sum(report.carry_total.values())
    lines.append(
        f"{'ALL':<10}{f'{sum(report.matched.values())}/{sum(report.total.values())}':>16}"
        f"{grand_bytes:>14,}{grand_carry:>14,}"
        f"{(100 * grand_carry / grand_bytes if grand_bytes else 0):>8.2f}%"
    )

    failures = report.failures
    if failures:
        lines.append("")
        lines.append(f"FAILURES ({len(failures)}):")
        for c in failures:
            if c.header_moved:
                lines.append(
                    f"  !! {c.name}: HEADER POINTER TABLE DIFFERS -- every section "
                    f"boundary moved; attribution below is suspect"
                )
            if c.status == "length":
                lines.append(
                    f"  {c.name} [{c.kind}] LENGTH: original {c.original_len:,} B, "
                    f"rebuilt {c.rebuilt_len:,} B (delta {c.rebuilt_len - c.original_len:+,})"
                )
                continue
            lines.append(
                f"  {c.name} [{c.kind}] first diff at {c.first_offset} "
                f"-> {c.owner}; expected 0x{c.expected_byte:02X}, got 0x{c.actual_byte:02X}"
            )
            lines.append(
                f"      {c.differing_bytes:,} differing byte(s) across: "
                f"{', '.join(c.regions_touched) or '(none)'}"
            )

    if regressions:
        lines.append("")
        lines.append(f"CARRY REGRESSIONS ({len(regressions)}):")
        for r in regressions:
            lines.append(
                f"  {r.kind}: carry rose {r.baseline:,} -> {r.current:,} "
                f"({r.current - r.baseline:+,} bytes)"
            )
    return "\n".join(lines)


def format_document_report(builder: "DocumentBuilder") -> str:
    """What the document leg reached, refused and warned about.

    A file the document leg cannot reach is not a pass and not a failure --
    it is a population this instrument does not speak for, and it is named so
    nobody reads 148/148 as 1,575/1,575.
    """
    lines = ["", f"DOCUMENT LEG: {builder.arrangements} arrangement(s) built"]
    if builder.skipped:
        lines.append(f"  not dumpable ({len(builder.skipped)}): "
                     f"{', '.join(name for name, _ in builder.skipped[:8])}"
                     + (" ..." if len(builder.skipped) > 8 else ""))
        lines.append("    -- these carry; their bytes are in the ratchet as carry")
    if builder.warnings:
        lines.append(f"  warnings ({len(builder.warnings)}) -- never refusals:")
        for label, warning in builder.warnings[:8]:
            lines.append(f"    {label}: {warning[:140]}")
        if len(builder.warnings) > 8:
            lines.append(f"    ...(+{len(builder.warnings) - 8} more)")
    if builder.refused:
        lines.append(f"  REFUSED ({len(builder.refused)}):")
        for label, reason in builder.refused:
            lines.append(f"    {label}: {reason}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="exmateria-map-roundtrip",
        description="dump -> build -> cmp over the FFT map corpus.",
    )
    parser.add_argument("--corpus", type=Path, default=None,
                        help="MAP directory (default: discover, honouring EXMATERIA_ASSETS_DIR)")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--write-baseline", action="store_true",
                        help="overwrite the baseline with this run (only when it passes)")
    parser.add_argument("--builder", choices=("document", "identity"),
                        default="document",
                        help="document: dump -> build, the real writer (default). "
                             "identity: copy every byte, the carry ratchet's zero point")
    parser.add_argument("--no-sheets", action="store_true",
                        help="skip the texture-sheet leg (PNG sidecar -> 4bpp); "
                             "the sheets then carry, which the ratchet will see")
    args = parser.parse_args(argv)

    try:
        resources = corpus.load(args.corpus)
    except CorpusError as exc:
        print(f"SKIPPED: {exc}", file=sys.stderr)
        return 77                      # distinct from 0 and 1: neither pass nor fail

    builder: Builder = identity_builder
    documents = None
    if args.builder == "document":
        documents = DocumentBuilder(args.corpus or corpus.map_dir(),
                                    with_sheets=not args.no_sheets)
        builder = documents

    report = run(resources, builder)
    baseline = load_baseline(args.baseline)
    regressions = carry_regressions(report, baseline) if baseline else []
    print(format_report(report, regressions))
    if documents is not None:
        print(format_document_report(documents))

    passed = report.coverage_ok and not regressions
    if documents is not None and documents.refused:
        passed = False
    if args.write_baseline:
        if not passed:
            print("\nrefusing to write baseline from a failing run", file=sys.stderr)
            return 1
        args.baseline.write_text(json.dumps(report.as_baseline(), indent=1) + "\n")
        print(f"\nbaseline written to {args.baseline}")
    print("\nPASS" if passed else "\nFAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
