"""Packing islands into the sheet's four 256x256 pages (ADR-0186 dec. 2, 11).

`addons/exmateria_map/pack.py` is `bpy`-free.  The sheet is 131,072 bytes of
4bpp indices -- four 256x256 texture pages -- and that is the entire budget an
unwrap has.

**Gutter 0 is not a taste.**  The PSX GPU point-samples with no filtering and
no mipmaps, and the shipped data relies on it: all 147 textured resources abut
polygon UV boxes at zero gutter and 141 of them abut ACROSS CLUT ROWS
(`workspace/gutter.py`).  A packer that inserts a one-texel gutter is not being
careful; it is failing to reproduce the format.  The exact-tiling test below is
what says so, and it cannot pass under any positive gutter.
"""

import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
ADDON = PKG / "addons" / "exmateria_map"
sys.path.insert(0, str(ADDON))
sys.path.insert(0, str(PKG))

import pack as P                                            # noqa: E402
import charts as C                                          # noqa: E402
from exmateria_map import corpus                             # noqa: E402
from exmateria_map.dump import dump, dumpable_arrangements   # noqa: E402

MAP_DIR = corpus.map_dir()
needs_corpus = pytest.mark.skipif(
    MAP_DIR is None,
    reason="corpus absent; set EXMATERIA_ASSETS_DIR to the extracted disc tree",
)

#: ADR-0186's Context, measured by `workspace/pack.py`: a shelf/FFD packer
#: over four 256x256 bins places every one of the 147 textured resources,
#: using PER-POLYGON islands -- the conservative case, since that is the
#: finest atom and so the most per-island rounding waste.  Charts can only do
#: better.  A packer that fits fewer than this is worse than the reference.
CORPUS_RESOURCES = 147


def placed_boxes(islands, placements):
    """(page, x0, y0, x1, y1) per island -- half-open, gutter-free."""
    return [(pg, x, y, x + w, y + h)
            for (w, h), (pg, x, y) in zip(islands, placements)]


def overlaps(a, b):
    if a[0] != b[0]:
        return False
    return a[1] < b[3] and b[1] < a[3] and a[2] < b[4] and b[2] < a[4]


def assert_legal(islands, placements, pages=4, size=256):
    assert len(placements) == len(islands)
    boxes = placed_boxes(islands, placements)
    for pg, x0, y0, x1, y1 in boxes:
        assert 0 <= pg < pages, f"page {pg} outside the sheet's {pages}"
        assert 0 <= x0 and x1 <= size and 0 <= y0 and y1 <= size, \
            f"island ({x0},{y0})-({x1},{y1}) leaves the {size}x{size} page"
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            assert not overlaps(a, b), f"{a} overlaps {b}: a texel with two owners"


def test_islands_are_placed_inside_the_pages_and_never_overlap():
    islands = [(64, 32), (128, 96), (16, 16), (200, 40)]
    assert_legal(islands, P.pack(islands))


def test_islands_abut_with_no_gutter_so_the_pages_tile_exactly():
    """Eight 256x128 islands are exactly four 256x256 pages, to the texel.
    Under any positive gutter this cannot be placed -- which is the point:
    the disc abuts at zero gutter on 147/147 resources, across CLUT rows on
    141/147, so a gutter is capacity thrown away for a bleed that the PSX's
    point-sampling cannot produce."""
    islands = [(256, 128)] * 8
    placements = P.pack(islands)
    assert_legal(islands, placements)
    covered = sum(w * h for w, h in islands)
    assert covered == 4 * 256 * 256, "the fixture is not an exact tiling"
    assert None not in placements, "an exact tiling did not fit: gutter > 0"


def test_over_capacity_the_pack_refuses_and_names_the_overflow_in_texels():
    """Decision 11: refuse, and say by how much.  It never scales islands
    down to fit -- an artist who painted detail and got back a blur, with
    nothing saying so, has no way to find out why.  This is `build`'s posture
    (`BuildRefusal`), applied to the sheet."""
    #: One page more than the sheet holds: five 256x256 islands into four
    #: pages.  Over by exactly one page.
    islands = [(256, 256)] * 5
    with pytest.raises(P.PackRefusal) as refusal:
        P.pack(islands)
    said = str(refusal.value)
    assert "65536" in said or "65,536" in said, (
        f"the refusal must name the overflow in texels (65,536); said: {said}")
    assert "256x256" in said or "256×256" in said, (
        f"the refusal must name the largest islands; said: {said}")


def uv_box(polygons, members):
    """The texel box a set of polygons reads -- the island a CONVERSION gives
    them, since conversion copies the texels they already read."""
    us = [c[0] for i in members for c in polygons[i]["uv"]]
    vs = [c[1] for i in members for c in polygons[i]["uv"]]
    return max(us) - min(us) + 1, max(vs) - min(vs) + 1


def corpus_resources():
    for num in range(1, 130):
        try:
            arrangements = dumpable_arrangements(MAP_DIR, num)
        except Exception:
            continue
        for a in arrangements:
            try:
                doc, _ = dump(MAP_DIR, num, a)
            except Exception:
                continue
            polys = doc.get("polygons") or []
            if any("uv" in p for p in polys):
                yield f"MAP{num:03d}.a{a}", polys


@needs_corpus
def test_every_textured_resource_packs_with_per_polygon_islands():
    fitted = refused = 0
    for name, polys in corpus_resources():
        islands = [uv_box(polys, [i])
                   for i, p in enumerate(polys) if "uv" in p]
        try:
            assert_legal(islands, P.pack(islands))
            fitted += 1
        except P.PackRefusal:
            refused += 1
    assert (fitted, refused) == (CORPUS_RESOURCES, 0)


def test_a_scattered_charts_shipped_uv_box_is_mostly_air():
    """Why a chart's island may NOT be its shipped UV bounding box.

    ADR-0186 reasons that since per-polygon islands fit 147/147, "charts can
    only do better" -- fewer, larger islands pack more efficiently.  That
    holds only if a chart's box is about the sum of its members' boxes.  On
    the disc it often is not: the median chart is laid out contiguously
    (box == members, coverage 100%), but the tail is scattered -- p90 3.11x,
    max 624x, worst chart reading 0.1% of its own box
    (`workspace/chartisland.py`).

    Two welded quads at opposite corners of a page: one chart, 8 texels of
    surface, a box of nearly the whole page.
    """
    near = [[0, 0], [8, 0], [0, 8], [8, 8]]
    far = [[240, 240], [248, 240], [240, 248], [248, 248]]
    a = {"kind": "textured_quad", "uv": near, "palette_id": 0,
         "texture_page": 0,
         "positions": [[0, 0, 0], [1, 0, 0], [0, 0, 1], [1, 0, 1]]}
    b = {"kind": "textured_quad", "uv": far, "palette_id": 0,
         "texture_page": 0,
         "positions": [[1, 0, 0], [2, 0, 0], [1, 0, 1], [2, 0, 1]]}
    polys = [a, b]
    assert C.charts(polys) == [[0, 1]], "the fixture must be ONE chart"

    members = sum(uv_box(polys, [i])[0] * uv_box(polys, [i])[1]
                  for i in (0, 1))
    w, h = uv_box(polys, [0, 1])
    assert members == 2 * 9 * 9
    assert w * h == 249 * 249
    assert w * h > 100 * members, (
        "the chart's shipped box is not its members' area -- so a bbox-copy "
        "island is not bounded by the per-polygon fit")


def test_a_set_that_tiles_a_page_exactly_is_placed_in_that_page():
    """One 128x256 column beside two stacked 128x128 squares IS a 256x256
    page, to the texel.  A shelf packer cannot see it: sorted by height it
    opens a 256-tall shelf, fills the rest of that shelf with the first
    square, and has nowhere left for the second -- it has thrown away the
    128 texels of height above the square it just placed.

    ADR-0186 leaves the algorithm open and says so ("MaxRects or guillotine
    will beat it"); the shelf/FFD reference is the measured floor.  This is
    the smallest case that separates them.
    """
    islands = [(128, 256), (128, 128), (128, 128)]
    assert sum(w * h for w, h in islands) == 256 * 256
    placements = P.pack(islands, pages=1)
    assert_legal(islands, placements, pages=1)
