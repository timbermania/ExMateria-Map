"""What region of the sheet a chart owns (ADR-0186 dec. 2, Amendment 1).

A chart's island cannot be its shipped UV bounding box: 76 of 147 resources
exceed the sheet on area alone that way, because a scattered chart's hull is
almost entirely air (`workspace/chartisland.py`).  It is instead the chart's
UV-CONNECTED pieces -- and a piece is kept whole only while its hull costs no
more than its members do apart.

Every island stays a verbatim copy of texels the disc already ships, so a
conversion is an integer blit and is exactly lossless.  A chart may own
several islands; they move together, so decision 3's "a chart is never split
between CLUT rows" is untouched.
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

#: Measured by `workspace/chartisland.py` over the 147 textured resources.
#: The whole corpus packs, with more headroom than the per-polygon bound the
#: ADR kept as its conservative case (median 49.3% against 50.8%, max 83.4%
#: against 89.2%) and on 48,361 islands rather than 62,838.
CORPUS_RESOURCES = 147
CORPUS_ISLANDS = 48_361


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


def test_a_chart_the_disc_laid_out_contiguously_is_one_island():
    """The median chart on the disc: its members touch in UV space, so the
    hull wastes nothing and the whole chart is one blit."""
    found = I.islands(CONTIGUOUS)
    assert len(found) == 1
    assert found[0]["members"] == [0, 1]
    assert found[0]["source"] == (0, 0)
    assert found[0]["size"] == (17, 9)


def test_pieces_of_a_chart_that_do_not_touch_in_uv_are_separate_islands():
    """Two welded quads at opposite corners of the page are one chart, but
    their hull is 249x249 for 162 texels of surface.  Copying that hull is
    what puts 76 of 147 resources over the sheet, so the piece is split."""
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
def test_every_textured_resource_packs_with_chart_islands():
    """Amendment 1's resolution, corpus-wide.  This is the claim the ADR's
    'charts can only do better' was reaching for, and the shipped bounding
    box could not deliver: 38/147 that way, 147/147 this way."""
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


def test_a_touching_piece_whose_hull_is_mostly_air_is_split_to_its_members():
    """The rule the corpus figure rests on, in isolation.

    Four quads chained corner to corner: every one abuts the next, so they
    are a single UV-connected piece -- but the hull of a diagonal staircase
    is mostly air.  Keeping it whole costs 33x33 = 1,089 texels for 4x81 =
    324 of surface, so the piece is split back to its members.

    Without this the corpus reads 143/147, not 147/147.
    """
    staircase = [
        quad([(8 * k, 8 * k), (8 * k + 8, 8 * k),
              (8 * k, 8 * k + 8), (8 * k + 8, 8 * k + 8)],
             [(k, 0, 0), (k + 1, 0, 0), (k, 0, 1), (k + 1, 0, 1)])
        for k in range(4)
    ]
    assert len(I.islands(staircase[:1])) == 1, "one quad is one island"

    found = I.islands(staircase)
    assert {i["chart"] for i in found} == {0}, "the staircase is ONE chart"
    hull_area = 33 * 33
    apart = 4 * 9 * 9
    assert hull_area > apart, "the fixture must have a wasteful hull"
    assert len(found) == 4, (
        "the hull was kept whole: 1,089 texels claimed for 324 of surface")
    assert all(i["size"] == (9, 9) for i in found)


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
    """The split rule has to compare the hull against the members' UNION, not
    their sum.  When two members OVERLAP -- and 16.14% of the disc's read
    texels have more than one reader -- the sum double-counts the overlap,
    so a hull with air in it passes a test it should fail.

    Here: boxes 0..9 x 0..9 and 5..14 x 0..19, welded, one chart.  Hull 15x20
    = 300.  Sum of boxes = 100 + 200 = 300, so a sum test merges them.  Their
    union is only 250 -- the 5x10 block at the bottom left is read by
    neither.  Those 50 texels are another chart's paint on the disc, and
    carrying them into this island puts a second surface inside it.
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
