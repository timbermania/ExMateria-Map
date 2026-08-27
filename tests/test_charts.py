"""The chart — the compile's island and its re-grouping atom (ADR-0186 dec. 2, 3).

`addons/exmateria_map/charts.py` imports `bpy` never, for the reason
`quantise.py` does not: the partition that decides what an artist can paint
without repainting something else is not a thing you iterate through Blender
restarts.

Every expectation here comes from the FORMAT or from a hand-built mesh whose
chart count is countable by eye -- never from the code under test. The corpus
figures come from `workspace/charts.py`, a separately written implementation
that produced ADR-0186's numbers.
"""

import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
ADDON = PKG / "addons" / "exmateria_map"
sys.path.insert(0, str(ADDON))
sys.path.insert(0, str(PKG))

import charts as C                                          # noqa: E402
from exmateria_map import corpus                             # noqa: E402
from exmateria_map.dump import dump, dumpable_arrangements   # noqa: E402

MAP_DIR = corpus.map_dir()
needs_corpus = pytest.mark.skipif(
    MAP_DIR is None,
    reason="corpus absent; set EXMATERIA_ASSETS_DIR to the extracted disc tree",
)

#: ADR-0186's Context, measured by `workspace/charts.py` -- a separately
#: written implementation of the same rule.  Neither number is computed here.
CORPUS_RESOURCES = 147
CORPUS_TEXTURED_POLYGONS = 62_838
CORPUS_CHARTS = 11_177

#: What the same corpus reads if the corners are walked in DOCUMENT order
#: instead of the PSX ring -- two of every four "edges" become diagonals, so
#: polygons that do not touch weld.  Named so this test can never be
#: satisfied by that implementation.
CORPUS_CHARTS_IF_RING_IGNORED = 32_652


def quad(corners, palette_id=0, texture_page=0):
    """A textured quad from four corners in DOCUMENT order.

    The document's corner order is NOT the perimeter: FFT rings a quad
    (0,1,3,2), so `corners[1]`-`corners[2]` is a DIAGONAL, not an edge.
    Callers below rely on that.
    """
    return {"kind": "textured_quad",
            "positions": [list(c) for c in corners],
            "uv": [[0, 0], [8, 0], [0, 8], [8, 8]],
            "palette_id": palette_id,
            "texture_page": texture_page}


#: Two unit squares in the y=0 plane, side by side, sharing the edge
#: x=1 between z=0 and z=1.  Document order c0,c1,c2,c3 rings to the
#: perimeter c0,c1,c3,c2 -- so each of these IS a square.
LEFT = quad([(0, 0, 0), (1, 0, 0), (0, 0, 1), (1, 0, 1)])
RIGHT = quad([(1, 0, 0), (2, 0, 0), (1, 0, 1), (2, 0, 1)])


def test_two_quads_welded_across_a_shared_edge_are_one_chart():
    assert C.charts([LEFT, RIGHT]) == [[0, 1]]


def test_a_palette_id_change_cuts_the_chart_even_across_a_welded_edge():
    """A texel on a chart seam carries ONE index, so it resolves through one
    CLUT row.  Two surfaces bound to different rows cannot share a seam
    texel, so the weld does not survive the row change (ADR-0186 dec. 2)."""
    other_row = quad([(1, 0, 0), (2, 0, 0), (1, 0, 1), (2, 0, 1)], palette_id=7)
    assert C.charts([LEFT, other_row]) == [[0], [1]]


def test_a_texture_page_change_cuts_the_chart_even_across_a_welded_edge():
    """An island lives inside one of the sheet's four 256x256 pages, so a
    chart that spanned two pages could not be given one island."""
    other_page = quad([(1, 0, 0), (2, 0, 0), (1, 0, 1), (2, 0, 1)],
                      texture_page=3)
    assert C.charts([LEFT, other_page]) == [[0], [1]]


def test_three_polygons_on_one_edge_weld_the_pair_that_shares_a_row():
    """A fold puts three polygons on one mesh edge.  Two of them share a CLUT
    row and must weld THROUGH the third, which does not -- so the welding
    rule is over every PAIR on the edge, not over consecutive ones."""
    fold = quad([(1, 0, 0), (1, -1, 0), (1, 0, 1), (1, -1, 1)], palette_id=7)
    #                 0=LEFT  1=fold(row 7)  2=RIGHT
    # LEFT and RIGHT are both row 0 and both touch the edge x=1; the fold
    # sits between them in document order and is on a different row.
    assert C.charts([LEFT, fold, RIGHT]) == [[0, 2], [1]]


def test_an_untextured_polygon_is_in_no_chart():
    """51 of MAP022 a0's 454 polygons carry no `uv` at all.  A chart is what
    gets an ISLAND, so a polygon that reads no texels is not in one -- and
    the indices returned still count from the whole polygon list, because
    that is what the document and every caller index by."""
    bare = {"kind": "untextured_quad",
            "positions": [[1, 0, 0], [2, 0, 0], [1, 0, 1], [2, 0, 1]],
            "unknown_untextured": 0}
    assert C.charts([LEFT, bare, RIGHT]) == [[0, 2]]


@needs_corpus
def test_the_corpus_partitions_into_the_number_of_charts_the_adr_measured():
    resources = polygons = found = 0
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
            textured = [p for p in polys if "uv" in p]
            if not textured:
                continue
            resources += 1
            polygons += len(textured)
            found += len(C.charts(polys))

    assert (resources, polygons) == (CORPUS_RESOURCES,
                                     CORPUS_TEXTURED_POLYGONS)
    assert found != CORPUS_CHARTS_IF_RING_IGNORED, (
        "the corners were walked in document order: two of every four edges "
        "formed were diagonals, so polygons that do not touch welded")
    assert found == CORPUS_CHARTS
