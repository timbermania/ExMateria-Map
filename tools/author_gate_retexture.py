"""Author the map-to-disc gate's retexture and build its bundle.

`docs/map-to-disc-gate.md` needs an **authored** MAP022 a0, not a vanilla one:
a size-preserving edit is the only thing that separates "the patcher delivered
the bytes" from "the patcher did nothing", because a byte-identical bundle
renders exactly like the unpatched disc either way.

What it authors, in one sentence -- the gate's *named difference*:

    the default-state sheet's FLOOR is repainted as an 8x8 checkerboard of
    palette indices 9 and 15, and nothing else on the sheet is touched.

Three things make that the right edit rather than a random smear:

* **It lands only on floor quads.** The footprint is rasterised from the
  document's own UVs, restricted to the polygons whose Newell normal says they
  are floor-like (`geometry`, decision 15's rule). Walls and props keep their
  art, so a human can see at a glance that the edit went where it was aimed --
  which is check 5's binding clause ("the retextured sheet lands on the right
  quads") turned into something you can just look at.
* **A regular pattern makes mis-binding obvious.** If the patcher wrote to the
  wrong LBA, or GaneshaDx re-binds the sector ranks, a checkerboard shows it.
  A flat recolour would not.
* **The index pair is measured, not chosen by eye.** A floor polygon in MAP022
  a0 wears one of seven CLUTs (palette ids 1, 2, 3, 4, 5, 12, 13), and the
  sheet stores indices, not colours -- so a pair that is high-contrast in one
  CLUT can be invisible in another. 9/15 is the pair whose *worst* per-CLUT
  luminance gap is largest (32.9 of 255; the runner-up 3/10 scores 29.7).
  Index 0 is excluded: it is the transparent slot.

Run (needs EXMATERIA_ASSETS_DIR, like every harness here):

    python3 tools/author_gate_retexture.py <output-dir>

It writes `<out>/document/` (the authored document + sidecars) and
`<out>/bundle/` (what `fft-iso-patcher` ingests), and prints the run record's
hash gate. Nothing it writes belongs in git: the sheet is ROM-derived, which is
why this recipe is the committed artifact and its output is not.
"""

import hashlib
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exmateria_map import build as build_leg          # noqa: E402
from exmateria_map import corpus, document as schema  # noqa: E402
from exmateria_map import dump as dump_leg            # noqa: E402
from exmateria_map.geometry import newell_normal, ring   # noqa: E402
from exmateria_map.png_indexed import (               # noqa: E402
    pack_4bpp,
    read_indexed_png,
    write_indexed_png,
)

MAP, ARRANGEMENT = 22, 0
INDEX_A, INDEX_B = 9, 15
CHECK = 8                       # checker cell, in texels

NAMED_DIFFERENCE = (
    f"the default-state sheet's floor is repainted as a {CHECK}x{CHECK} "
    f"checkerboard of palette indices {INDEX_A} and {INDEX_B}; walls, props "
    f"and every other page are untouched"
)


def uv_ring(poly):
    """The UV corners in ring order. Raw quads are triangle-STRIP order, so the
    ring is (0, 1, 3, 2) -- the same reordering the positions need (#426)."""
    uv = poly["uv"]
    return [uv[0], uv[1], uv[3], uv[2]] if len(uv) == 4 else list(uv)


def inside(px, py, poly):
    hit = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        yi, yj = poly[i][1], poly[j][1]
        if (yi > py) != (yj > py):
            xi, xj = poly[i][0], poly[j][0]
            if px < (xj - xi) * (py - yi) / (yj - yi) + xi:
                hit = not hit
        j = i
    return hit


def floor_polygons(document):
    return [p for p in document["polygons"]
            if p["kind"].startswith("textured")
            and abs(newell_normal(ring(p["positions"]))[1]) >= schema.FLOOR_COS]


def repaint(indices, document):
    """Paint the checkerboard over the floor's UV footprint. Returns the count
    of texels changed -- a repaint that moved nothing is not an edit."""
    out = bytearray(indices)
    painted, changed = set(), 0
    for poly in floor_polygons(document):
        corners = uv_ring(poly)
        page = poly["texture_page"]
        u0 = min(u for u, _ in corners)
        u1 = max(u for u, _ in corners)
        v0 = min(v for _, v in corners)
        v1 = max(v for _, v in corners)
        for u in range(u0, u1 + 1):
            for v in range(v0, v1 + 1):
                if not inside(u + 0.5, v + 0.5, corners):
                    continue
                row = page * schema.SHEET_WIDTH + v      # page is a 256-row band
                if row >= schema.SHEET_HEIGHT:
                    continue
                painted.add((u, row))
        # A degenerate or hairline UV footprint can contain no texel centre at
        # all; the corners still name the quad, so seed them.
        for u, v in corners:
            row = page * schema.SHEET_WIDTH + v
            if row < schema.SHEET_HEIGHT:
                painted.add((u, row))
    for u, row in painted:
        value = INDEX_A if ((u // CHECK) + (row // CHECK)) % 2 == 0 else INDEX_B
        offset = row * schema.SHEET_WIDTH + u
        changed += out[offset] != value
        out[offset] = value
    return bytes(out), len(painted), changed


def main() -> int:
    out_root = Path(sys.argv[1] if len(sys.argv) > 1 else "./gate-run").resolve()
    map_dir = corpus.map_dir()
    if map_dir is None:
        print("no corpus; set EXMATERIA_ASSETS_DIR", file=sys.stderr)
        return 1

    doc_dir = out_root / "document"
    shutil.rmtree(doc_dir, ignore_errors=True)
    document_path = dump_leg.write_bundle(map_dir, MAP, ARRANGEMENT, doc_dir)
    document = json.loads(document_path.read_text())

    # The default state: day, weather 0 -- what the Gariland Fight scenarios
    # render (gate doc, "the acceptance trip").
    state = next(s for s in document["map_states"]
                 if s["texture_sheet"] and not s["night"] and s["weather"] == 0)
    sidecar = doc_dir / state["texture_sheet"]
    sharers = [s["resource"] for s in document["map_states"]
               if s["texture_sheet"] == state["texture_sheet"]]

    original_blob = (map_dir / state["resource"]).read_bytes()
    width, height, indices, palette, alpha = read_indexed_png(sidecar.read_bytes())
    painted, texels, changed = repaint(indices, document)
    sidecar.write_bytes(write_indexed_png(painted, palette, width, height, alpha))

    bundle_dir = out_root / "bundle"
    shutil.rmtree(bundle_dir, ignore_errors=True)
    bundle = build_leg.build_bundle(document_path, map_dir, bundle_dir)

    authored_blob = bundle.resources[state["resource"]]
    before = hashlib.sha256(original_blob).hexdigest()
    after = hashlib.sha256(authored_blob).hexdigest()
    untouched = [n for n, v in bundle.resources.items()
                 if v == (map_dir / n).read_bytes()]

    print(f"named difference : {NAMED_DIFFERENCE}")
    print(f"sheet            : {state['resource']}  ({state['texture_sheet']})")
    print(f"                   shared by {len(sharers)} state row(s): "
          f"{', '.join(sharers)}")
    print(f"floor polygons   : {len(floor_polygons(document))} of "
          f"{len(document['polygons'])}")
    print(f"texels repainted : {texels:,} in footprint, {changed:,} actually changed")
    print(f"blob size        : {len(original_blob):,} -> {len(authored_blob):,} B")
    print(f"sha256 disc      : {before}")
    print(f"sha256 authored  : {after}")
    print(f"HASH GATE        : {'PASS' if before != after else 'FAIL (identical)'}")
    print(f"untouched blobs  : {len(untouched)} of {len(bundle.resources)} "
          f"+ {bundle.gns_name}")
    print(f"build warnings   : {bundle.warnings or 'none'}")
    print(f"\ndocument : {doc_dir}\nbundle   : {bundle_dir}")

    if before == after or changed == 0:
        print("\nFAIL: the authored sheet is the disc's -- there is nothing to "
              "prove a delivery with")
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
