"""What region of the sheet a POLYGON owns (ADR-0186 Amendment 6 dec. 22).

An island is a polygon.  Every textured polygon gets its own rectangle of the
sheet, so after a conversion no texel is read by two polygons.

That supersedes Amendment 1, which made an island a chart's UV-connected
piece and kept a piece whole only while its hull cost no more than its
members did apart.  Amendment 1 was reaching for the same property one level
up -- a chart's shipped bounding box puts 76 of 147 resources over the sheet
on area alone -- and it stopped at the chart because Amendment 2 believed a
manifold chart needed a resampling unwrap.  A copy is not an unwrap:
`workspace/island_split_cost.py` measures full one-to-one at +1.5pp of the
sheet at the median and 0 refusals of 147.

Every island stays a verbatim copy of texels the disc already ships, so a
conversion is an integer blit and is exactly lossless.  A chart owns many
islands; they carry `chart` and move together, so decision 3's "a chart is
never split between CLUT rows" is untouched.
"""

import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
ADDON = PKG / "addons" / "exmateria_map"
sys.path.insert(0, str(ADDON))
sys.path.insert(0, str(PKG))

import islands as I                                         # noqa: E402
import pack as P                                            # noqa: E402
from exmateria_map import corpus                             # noqa: E402
from exmateria_map.dump import dump, dumpable_arrangements   # noqa: E402

MAP_DIR = corpus.map_dir()
needs_corpus = pytest.mark.skipif(
    MAP_DIR is None,
    reason="corpus absent; set EXMATERIA_ASSETS_DIR to the extracted disc tree",
)

#: Measured by `workspace/island_split_cost.py` arm C over the 147 textured
#: resources.  One island per textured polygon: 62,838 of them, and the whole
#: corpus still packs -- median 50.8% of the sheet, p90 65.3%, max 89.2%,
#: which is +1.5pp at the median over Amendment 1's 48,361 chart pieces.
CORPUS_RESOURCES = 147
CORPUS_ISLANDS = 62_838


def quad(uv, positions, palette_id=0, texture_page=0):
    return {"kind": "textured_quad", "uv": [list(c) for c in uv],
            "positions": [list(p) for p in positions],
            "palette_id": palette_id, "texture_page": texture_page}


#: Two welded quads whose UV boxes ABUT on the disc: 0..8 and 8..16 in u.
CONTIGUOUS = [
    quad([(0, 0), (8, 0), (0, 8), (8, 8)],
         [(0, 0, 0), (1, 0, 0), (0, 0, 1), (1, 0, 1)]),
    quad([(8, 0), (16, 0), (8, 8), (16, 8)],
         [(1, 0, 0), (2, 0, 0), (1, 0, 1), (2, 0, 1)]),
]


def test_a_chart_the_disc_laid_out_contiguously_is_one_island_PER_POLYGON():
    """The median chart on the disc, and what decision 22 changed about it.

    Its two members abut in UV space, so Amendment 1 kept them as one 17x9
    blit: the hull wasted nothing.  Under decision 22 they are two islands
    anyway.

    What that costs is the shared column at u=8, which both quads read and
    which is now copied twice -- the seam Amendment 2 is right to call
    *correct*.  Preserving it buys 0.2pp of the sheet (arm B against arm C)
    and costs the whole distinction: two rules instead of one, mesh adjacency
    consulted during packing, and a class of texel the artist cannot reason
    about locally.  At that price the simpler property wins.
    """
    found = I.islands(CONTIGUOUS)
    assert len(found) == 2
    assert [i["members"] for i in found] == [[0], [1]]
    assert [i["source"] for i in found] == [(0, 0), (8, 0)]
    assert all(i["size"] == (9, 9) for i in found)
    assert {i["chart"] for i in found} == {0}, "still ONE chart"


def test_two_polygons_of_one_chart_reading_ONE_rectangle_get_TWO_islands():
    """The fold, and the whole of decision 22.

    Two welded faces reading the identical UV rectangle: on the disc they are
    one surface painted twice, and 8.5% of the corpus's charts do it
    (Amendment 2).  Under Amendment 1's rule they were a single UV-connected
    piece whose hull wasted nothing, so they stayed ONE island -- and a stroke
    on one face repainted the other, mirrored, somewhere else in the map.

    Two copies of one rectangle is still an integer blit of the same source
    texels, so the conversion stays lossless; the copies diverge only when
    something paints one of them.  Amendment 2 called that irreconcilable
    with "the first compile is a no-op".  It is not: an unwrap resamples, a
    copy does not.
    """
    fold = [
        quad([(0, 0), (8, 0), (0, 8), (8, 8)],
             [(0, 0, 0), (1, 0, 0), (0, 0, 1), (1, 0, 1)]),
        quad([(0, 0), (8, 0), (0, 8), (8, 8)],
             [(1, 0, 0), (2, 0, 0), (1, 0, 1), (2, 0, 1)]),
    ]
    assert I.charts(fold) == [[0, 1]], "the fixture must be ONE chart"

    found = I.islands(fold)
    assert len(found) == 2, "the fold shares one rectangle between two faces"
    assert [i["members"] for i in found] == [[0], [1]]
    assert all(i["source"] == (0, 0) and i["size"] == (9, 9) for i in found)
    assert {i["chart"] for i in found} == {0}, "still ONE chart"


def test_pieces_of_a_chart_that_do_not_touch_in_uv_are_separate_islands():
    """Two welded quads at opposite corners of the page are one chart, but
    their hull is 249x249 for 162 texels of surface.  Copying that hull is
    what puts 76 of 147 resources over the sheet.  Under decision 22 the
    question never arises -- a hull is never a candidate, because the unit of
    placement is the polygon."""
    scattered = [
        quad([(0, 0), (8, 0), (0, 8), (8, 8)],
             [(0, 0, 0), (1, 0, 0), (0, 0, 1), (1, 0, 1)]),
        quad([(240, 240), (248, 240), (240, 248), (248, 248)],
             [(1, 0, 0), (2, 0, 0), (1, 0, 1), (2, 0, 1)]),
    ]
    found = I.islands(scattered)
    assert {i["chart"] for i in found} == {0}, "still ONE chart"
    assert len(found) == 2
    assert sorted(i["size"] for i in found) == [(9, 9), (9, 9)]
    assert sorted(i["source"] for i in found) == [(0, 0), (240, 240)]


@needs_corpus
def test_every_textured_resource_packs_with_polygon_islands():
    """Decision 22's price, corpus-wide, and the reason it was affordable.

    The bijection is the *most* islands any rule in this ADR has produced --
    62,838 against Amendment 1's 48,361 -- and it still refuses on nothing.
    The claim the ADR's 'charts can only do better' was reaching for, which
    the shipped bounding box could not deliver: 38/147 that way, 147/147
    this way, and the tightest map lands at 89.2% of the sheet."""
    fitted = refused = total_islands = 0
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
            if not any("uv" in p for p in polys):
                continue
            found = I.islands(polys)
            total_islands += len(found)
            try:
                P.pack([i["size"] for i in found])
                fitted += 1
            except P.PackRefusal:
                refused += 1

    assert (fitted, refused) == (CORPUS_RESOURCES, 0)
    assert total_islands == CORPUS_ISLANDS


@needs_corpus
def test_every_textured_polygon_belongs_to_exactly_one_island():
    """An island is a region ONE run of surface owns -- decision 2's whole
    point is that a stroke cannot repaint another surface.  A polygon in two
    islands, or in none, breaks that before any packing happens."""
    for num in (1, 22, 53):
        for a in dumpable_arrangements(MAP_DIR, num):
            doc, _ = dump(MAP_DIR, num, a)
            polys = doc.get("polygons") or []
            textured = {i for i, p in enumerate(polys) if "uv" in p}
            if not textured:
                continue
            owned = [m for i in I.islands(polys) for m in i["members"]]
            assert sorted(owned) == sorted(textured)
            assert len(owned) == len(set(owned)), "a polygon in two islands"


def air_in(polygons, island):
    """Texels inside the island that NO member reads."""
    w, h = island["size"]
    sx, sy = island["source"]
    read = set()
    for m in island["members"]:
        us = [c[0] for c in polygons[m]["uv"]]
        vs = [c[1] for c in polygons[m]["uv"]]
        for x in range(min(us), max(us) + 1):
            for y in range(min(vs), max(vs) + 1):
                read.add((x, y))
    return sum(1 for x in range(sx, sx + w) for y in range(sy, sy + h)
               if (x, y) not in read)


def test_an_island_never_carries_a_texel_none_of_its_members_reads():
    """A texel no member reads is another surface's paint carried inside this
    island, which puts a second surface in it and defeats decision 2.

    Amendment 1 had to work for this: its hull had to be compared against the
    members' UNION and not their sum, because 16.14% of the disc's read texels
    have more than one reader, so a sum double-counts the overlap and admits
    a hull with air in it.  This fixture is that trap -- boxes 0..9 x 0..9 and
    5..14 x 0..19, welded, one chart; hull 15x20 = 300, sum of boxes also 300,
    union only 250, and the 5x10 block at the bottom left read by neither.

    Under decision 22 an island is one polygon's rectangle, so air is
    structurally unreachable rather than ruled out.  The arm is kept pointed
    at the property and not at the rule: decision 23 consolidates islands
    again, and anything that groups rectangles can put air back.
    """
    overlapping = [
        quad([(0, 0), (9, 0), (0, 9), (9, 9)],
             [(0, 0, 0), (1, 0, 0), (0, 0, 1), (1, 0, 1)]),
        quad([(5, 0), (14, 0), (5, 19), (14, 19)],
             [(1, 0, 0), (2, 0, 0), (1, 0, 1), (2, 0, 1)]),
    ]
    assert I.charts(overlapping) == [[0, 1]], "the fixture must be ONE chart"

    found = I.islands(overlapping)
    assert sum(air_in(overlapping, i) for i in found) == 0


@needs_corpus
def test_no_island_in_the_corpus_carries_a_texel_none_of_its_members_reads():
    carrying = 0
    for num in (1, 22, 53):
        for a in dumpable_arrangements(MAP_DIR, num):
            doc, _ = dump(MAP_DIR, num, a)
            polys = doc.get("polygons") or []
            if not any("uv" in p for p in polys):
                continue
            carrying += sum(1 for i in I.islands(polys)
                            if air_in(polys, i))
    assert carrying == 0
