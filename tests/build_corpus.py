"""``dump -> build -> cmp`` over the corpus, in one line of output.

The same claim ``exmateria-map-roundtrip`` makes, in the shape a mutation
audit can grade: a summary line naming the exact count, the refusals and the
warnings, so a seeded defect shows up as a *named* change rather than as a
different-looking report.

It also takes ``--limit``, which the full instrument deliberately does not: a
partial run is not a verdict, but it is enough to move a byte, and 20 seeds x
24 s of full corpus is not a loop anyone runs.

Run:  python3 tests/build_corpus.py [--limit N]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exmateria_map import build as build_leg          # noqa: E402
from exmateria_map import corpus, dump as dump_leg    # noqa: E402
from exmateria_map.mapfile import BindError, map_numbers   # noqa: E402

LIMIT = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None


def main() -> int:
    map_dir = corpus.map_dir()
    if map_dir is None:
        print("SKIPPED: no corpus; set EXMATERIA_ASSETS_DIR")
        return 77

    targets = []
    for number in map_numbers(map_dir):
        try:
            targets.extend((number, a) for a in dump_leg.arrangements(map_dir, number))
        except BindError:
            continue
    if LIMIT:
        targets = targets[:LIMIT]

    n = exact = refused = warned = 0
    mismatches, refusals = [], []
    for number, a in targets:
        label = f"MAP{number:03d}.a{a}"
        try:
            document, _sheets = dump_leg.dump(map_dir, number, a)
        except dump_leg.DumpError:
            continue
        n += 1
        try:
            bundle = build_leg.build(document, map_dir)
        except build_leg.BuildRefusal as exc:
            refused += 1
            refusals.append(label)
            print(f"REFUSED {label}: {str(exc)[:160]}")
            continue
        differing = [name for name, data in bundle.resources.items()
                     if (map_dir / name).read_bytes() != data]
        if bundle.gns != (map_dir / bundle.gns_name).read_bytes():
            differing.append(bundle.gns_name)
        if differing:
            mismatches.append(label)
            print(f"MISMATCH {label}: {', '.join(differing[:6])}")
        else:
            exact += 1
        warned += bool(bundle.warnings)

    print(f"\nBUILD {exact}/{n} EXACT, refused={refused}, warned={warned}")
    ok = n > 0 and exact == n and refused == 0
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
