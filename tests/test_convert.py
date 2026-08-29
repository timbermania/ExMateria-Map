"""Conversion is visually lossless, and the first compile is a no-op.

ADR-0186 decision 7: converting a map unwraps it, packs the islands and bakes
the disc's current sheet into source art.  Its Consequences make a claim that
is exact rather than approximate -- every island is a copy of the texels it
already read, under the row it already named -- and this is where that claim is
checked.

The oracle is the disc's own sheet.  For every textured polygon, the block of
indices it reads BEFORE conversion is compared to the block it reads AFTER,
through the rewritten UVs.  Neither side is computed by `convert`: one is read
through the shipped UVs, the other through the new ones.
"""

import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
ADDON = PKG / "addons" / "exmateria_map"
sys.path.insert(0, str(ADDON))
sys.path.insert(0, str(PKG))

import convert as V                                         # noqa: E402
from exmateria_map import corpus                             # noqa: E402
from exmateria_map.dump import dump, dumpable_arrangements   # noqa: E402
from exmateria_map.png_indexed import unpack_4bpp            # noqa: E402

MAP_DIR = corpus.map_dir()
needs_corpus = pytest.mark.skipif(
    MAP_DIR is None,
    reason="corpus absent; set EXMATERIA_ASSETS_DIR to the extracted disc tree",
)

#: `workspace/chartisland.py`: every textured resource packs under the island
#: rule, so a conversion never refuses on the shipped corpus.
CORPUS_RESOURCES = 147

#: ...but only 135 of those 147 ship a sheet to convert.  Twelve arrangements
#: carry textured geometry whose single map state names NO `texture_sheet` at
#: all, so there is nothing to bake into source art and nothing to blit.  They
#: are named rather than skipped, because "12 resources were silently passed
#: over" and "12 resources converted" look identical in a pass count.
CORPUS_CONVERTIBLE = 135
CORPUS_CONVERTIBLE_POLYGONS = 58_123
SHEETLESS = [
    "MAP011.a2", "MAP011.a3", "MAP011.a5",
    "MAP034.a1", "MAP034.a2", "MAP034.a3",
    "MAP041.a1", "MAP041.a2",
    "MAP053.a1", "MAP053.a2",
    "MAP064.a1", "MAP083.a1",
]

SHEET_W, SHEET_H = 256, 1024


def block(indices, uv, page, row=None):
    """The rectangle a polygon reads.

    With `row`, the 0..15 indices are resolved through that CLUT row, so the
    answer is the COLOURS the artist sees -- which is what "visually
    lossless" is a claim about.  Without it, the raw indices.
    """
    us = [c[0] for c in uv]
    vs = [c[1] for c in uv]
    return [[(indices[(page * 256 + y) * SHEET_W + x] if row is None
              else row[indices[(page * 256 + y) * SHEET_W + x] & 0xF])
             for x in range(min(us), max(us) + 1)]
            for y in range(min(vs), max(vs) + 1)]


def art_block(art, uv, page):
    """The same rectangle read out of the true-colour SOURCE ART."""
    us = [c[0] for c in uv]
    vs = [c[1] for c in uv]
    out = []
    for y in range(min(vs), max(vs) + 1):
        line = []
        for x in range(min(us), max(us) + 1):
            at = 3 * ((page * 256 + y) * SHEET_W + x)
            line.append(tuple(art[at:at + 3]))
        out.append(line)
    return out


#: Sixteen rows of sixteen, all distinct, so a bake through the WRONG row
#: cannot land on the right colour by accident.
FLAT_PALETTES = [{"colors": [f"#{r * 16 + c:02X}{r:02X}{c:02X}"
                             for c in range(16)], "stp": 0}
                 for r in range(16)]


def a_sheet():
    """A sheet no two texels of which agree by accident: an index that
    depends on both coordinates, so a blit off by one row or one column
    cannot read the same value."""
    return bytes(((x * 7 + y * 13) % 16)
                 for y in range(SHEET_H) for x in range(SHEET_W))


def quad(uv, positions, palette_id=0, texture_page=0):
    return {"kind": "textured_quad", "uv": [list(c) for c in uv],
            "positions": [list(p) for p in positions],
            "palette_id": palette_id, "texture_page": texture_page}


def test_every_polygon_reads_the_same_texels_after_a_convert():
    polygons = [
        quad([(40, 40), (48, 40), (40, 48), (48, 48)],
             [(0, 0, 0), (1, 0, 0), (0, 0, 1), (1, 0, 1)]),
        quad([(200, 90), (208, 90), (200, 98), (208, 98)],
             [(5, 0, 0), (6, 0, 0), (5, 0, 1), (6, 0, 1)],
             texture_page=2, palette_id=3),
    ]
    sheet = a_sheet()
    rows = V.clut_rows(FLAT_PALETTES)
    before = [block(sheet, p["uv"], p["texture_page"], rows[p["palette_id"]])
              for p in polygons]

    converted, art, _ = V.convert(polygons, sheet, FLAT_PALETTES)

    after = [art_block(art, p["uv"], p["texture_page"]) for p in converted]
    assert after == before, "a polygon's COLOURS moved"
    assert [p["palette_id"] for p in converted] == [0, 3], \
        "conversion keeps the row the polygon already named"


def test_all_of_a_maps_state_sheets_are_blitted_by_one_unwrap():
    """A map has one mesh and several state sheets -- MAP022 a0 has 20 states
    over 5 sheets.  The UVs are rewritten ONCE, so every state's sheet must
    be blitted by that same unwrap or the states stop agreeing about which
    texel a polygon reads."""
    polygons = [
        quad([(40, 40), (48, 40), (40, 48), (48, 48)],
             [(0, 0, 0), (1, 0, 0), (0, 0, 1), (1, 0, 1)]),
    ]
    day = a_sheet()
    night = bytes((v + 5) % 16 for v in day)     # a different picture
    assert day != night

    converted, art, _ = V.convert(polygons, [day, night],
                                  [FLAT_PALETTES, FLAT_PALETTES])
    assert len(art) == 2

    row = V.clut_rows(FLAT_PALETTES)[polygons[0]["palette_id"]]
    for original, baked in zip((day, night), art):
        assert (art_block(baked, converted[0]["uv"],
                          converted[0]["texture_page"])
                == block(original, polygons[0]["uv"],
                         polygons[0]["texture_page"], row))


def a_state_with_both(doc, sheets):
    """One state's (index plane, palettes) -- and they are NOT the same row.

    `map_states` interleaves two resource kinds.  A **texture** row (kind 23)
    names the `texture_sheet` and carries `palettes: null`; a **mesh** row
    (kind 46/48) carries the palettes and names no sheet.  They pair by
    `(night, weather)`, which is import's own rule.  Looking for one row with
    both finds NOTHING on all 147 resources -- a null result that looks like a
    corpus fact and is a pairing bug.

    133 of 147 have an exact `(night, weather)` partner and 2 fall back to any
    palette-bearing state; the remaining 12 name no sheet at all.
    """
    by_name = dict(sheets)
    keyed = {(st.get("night"), st.get("weather")): st["palettes"]
             for st in doc.get("map_states") or [] if st.get("palettes")}
    spare = next((st["palettes"] for st in doc.get("map_states") or []
                  if st.get("palettes")), None)
    for state in doc.get("map_states") or []:
        name = state.get("texture_sheet")
        if name in by_name:
            palettes = keyed.get((state.get("night"),
                                  state.get("weather"))) or spare
            if palettes:
                return unpack_4bpp(by_name[name]), palettes
    return None, None


def rows_read(indices, polygon, row):
    """The polygon's rectangle as COLOURS, one flat `bytes` per line."""
    us = [c[0] for c in polygon["uv"]]
    vs = [c[1] for c in polygon["uv"]]
    base = polygon["texture_page"] * 256
    return [bytes(b for x in range(min(us), max(us) + 1)
                  for b in row[indices[(base + y) * SHEET_W + x] & 0xF])
            for y in range(min(vs), max(vs) + 1)]


def rows_painted(art, polygon):
    """The same rectangle read out of the true-colour source art."""
    us = [c[0] for c in polygon["uv"]]
    vs = [c[1] for c in polygon["uv"]]
    base = polygon["texture_page"] * 256
    return [bytes(art[3 * ((base + y) * SHEET_W + x):
                      3 * ((base + y) * SHEET_W + x) + 3][i]
                  for x in range(min(us), max(us) + 1) for i in range(3))
            for y in range(min(vs), max(vs) + 1)]


@needs_corpus
def test_the_whole_corpus_converts_without_moving_a_single_texel():
    """The acceptance test ADR-0186 says is exact rather than approximate.

    Every textured polygon of every textured resource must read, through its
    rewritten UVs, exactly the COLOURS it read through the shipped ones --
    its indices resolved through the CLUT row it already named.  Colours
    rather than indices is the whole point: the source art carries no palette,
    so "the same index" would not be a claim about anything the artist sees.
    """
    resources = polygons = 0
    sheetless = []
    for num in range(1, 130):
        try:
            arrangements = dumpable_arrangements(MAP_DIR, num)
        except Exception:
            continue
        for a in arrangements:
            try:
                doc, sheets = dump(MAP_DIR, num, a)
            except Exception:
                continue
            polys = doc.get("polygons") or []
            if not any("uv" in p for p in polys):
                continue
            plane, palettes = a_state_with_both(doc, sheets)
            if plane is None:
                sheetless.append(f"MAP{num:03d}.a{a}")
                continue
            resources += 1

            rows = V.clut_rows(palettes)
            before = [rows_read(plane, p, rows[p["palette_id"]])
                      for p in polys if "uv" in p]

            converted, art, _ = V.convert(polys, plane, palettes)
            after = [rows_painted(art, p) for p in converted if "uv" in p]

            assert len(after) == len(before)
            for i, (was, now) in enumerate(zip(before, after)):
                assert now == was, (
                    f"MAP{num:03d}.a{a} polygon {i} reads different COLOURS "
                    f"after conversion")
            polygons += len(before)

    assert sheetless == SHEETLESS
    assert resources == CORPUS_CONVERTIBLE
    assert resources + len(sheetless) == CORPUS_RESOURCES
    assert polygons == CORPUS_CONVERTIBLE_POLYGONS


@needs_corpus
def test_a_conversion_carries_every_state_sheet_of_a_real_map():
    """MAP022 a0 ships 5 distinct sheets across 20 states."""
    doc, sheets = dump(MAP_DIR, 22, 0)
    polys = doc["polygons"]
    originals = [unpack_4bpp(raw) for _, raw in sorted(sheets.items())]
    assert len(originals) == 5

    _, palettes = a_state_with_both(doc, sheets)
    rows = V.clut_rows(palettes)
    before = [[rows_read(plane, p, rows[p["palette_id"]])
               for p in polys if "uv" in p] for plane in originals]
    converted, art, _ = V.convert(polys, originals,
                                  [palettes] * len(originals))
    after = [[rows_painted(a, p) for p in converted if "uv" in p]
             for a in art]
    assert after == before


@needs_corpus
def test_repacking_a_converted_map_carries_the_painting_unresampled():
    """Decision 10, and it is NOT a second `convert`.

    The compile has no inverse, so there is no going back to indices: what a
    re-pack moves is the artist's own picture.  Every island is re-placed and
    none resized, so the painting arrives whole.
    """
    doc, sheets = dump(MAP_DIR, 22, 0)
    plane, palettes = a_state_with_both(doc, sheets)
    once, art, sheet = V.convert(doc["polygons"], plane, palettes)
    before = [rows_painted(art, p) for p in once if "uv" in p]

    twice, moved, _ = V.repack(once, art, sheet)

    after = [rows_painted(moved, p) for p in twice if "uv" in p]
    assert after == before


@needs_corpus
def test_adding_geometry_repacks_without_disturbing_what_was_painted():
    """Decision 10: adding geometry triggers a blit-only re-pack.  Every
    island is re-placed, so the whole UV block churns -- but every polygon
    that existed before must still read the texels it read before."""
    doc, sheets = dump(MAP_DIR, 22, 0)
    plane, palettes = a_state_with_both(doc, sheets)
    converted, art, sheet = V.convert(doc["polygons"], plane, palettes)
    before = [rows_painted(art, p) for p in converted if "uv" in p]

    #: New geometry, welded to nothing that exists: its own chart.
    grown = converted + [quad(
        [(0, 0), (16, 0), (0, 16), (16, 16)],
        [(900, 0, 900), (916, 0, 900), (900, 0, 916), (916, 0, 916)])]
    regrown, moved, _ = V.repack(grown, art, sheet)

    after = [rows_painted(moved, p) for p in regrown[:len(converted)]
             if "uv" in p]
    assert after == before


@needs_corpus
def test_repeated_repacks_do_not_ratchet_the_sheet_upward():
    """Decision 10 re-packs on EVERY geometry edit, so the drift a re-pack
    leaves behind compounds over a map's editing life.  A map that creeps
    upward eventually meets decision 11's refusal for no reason except
    having been edited.

    Under Amendment 1 there WAS drift, and this arm bounded it at 5%: a
    chart's islands landed adjacent after packing and merged on the next
    pass, and a merged hull is only free while it is exactly tiled --
    measured MAP053 0.0%, MAP022 +1.4%, MAP001 +3.0% over 8 re-packs.

    Decision 22 removes the mechanism rather than bounding it.  An island is
    one polygon, so `islands()` re-derives the SAME rectangles from the
    re-packed UVs however they landed, and there is nothing left to merge.
    Measured 0.0% on all three, so the assertion is exact -- a bound would
    now pass on drift that cannot happen.
    """
    for num in (1, 22, 53):
        doc, sheets = dump(MAP_DIR, num, 0)
        plane, palettes = a_state_with_both(doc, sheets)
        polys, art, sheet = V.convert(doc["polygons"], plane, palettes)
        first = sum(w * h for w, h in
                    (i["size"] for i in V.islands(polys)))
        for _ in range(7):
            polys, art, sheet = V.repack(polys, art, sheet)
        last = sum(w * h for w, h in (i["size"] for i in V.islands(polys)))

        assert last == first, (
            f"MAP{num:03d}.a0 grew {100 * (last / first - 1):.1f}% over 8 "
            f"re-packs: {first:,} -> {last:,} texels")


def polygons_reading(ps):
    """(page, x, y) -> the set of polygon indices that read that texel."""
    owners = {}
    for i, p in enumerate(ps):
        if "uv" not in p:
            continue
        us = [c[0] for c in p["uv"]]
        vs = [c[1] for c in p["uv"]]
        for x in range(min(us), max(us) + 1):
            for y in range(min(vs), max(vs) + 1):
                owners.setdefault((p["texture_page"], x, y), set()).add(i)
    return owners


def polygons_reading(ps):
    """(page, x, y) -> the set of polygon indices that read that texel."""
    owners = {}
    for i, p in enumerate(ps):
        if "uv" not in p:
            continue
        us = [c[0] for c in p["uv"]]
        vs = [c[1] for c in p["uv"]]
        for x in range(min(us), max(us) + 1):
            for y in range(min(vs), max(vs) + 1):
                owners.setdefault((p["texture_page"], x, y), set()).add(i)
    return owners


@needs_corpus
def test_conversion_removes_every_shared_texel_between_charts():
    """The authoring problem ADR-0186 exists to solve, checked corpus-wide.

    The Context's complaint is that 16.14% of claimed texels have more than
    one polygon reader, so a brush stroke on one surface repaints others.
    Conversion gives every chart its own copy of the texels it reads, so
    afterwards NO texel is read by two charts -- 7.07% before, 0.00% after.

    Chart identity is fixed BEFORE the conversion and carried through.  It
    has to be: conversion rewrites `texture_page`, and `charts()` cuts at a
    page change, so re-deriving charts from the converted polygons gives a
    FINER partition and reports sharing that is not there.  That mistake
    reads as a 0.25% residue.
    """
    for num in (1, 4, 22, 53):
        for a in dumpable_arrangements(MAP_DIR, num):
            doc, sheets = dump(MAP_DIR, num, a)
            polys = doc.get("polygons") or []
            if not any("uv" in p for p in polys) or not sheets:
                continue

            chart_of = {}
            for c, members in enumerate(V.charts(polys)):
                for m in members:
                    chart_of[m] = c

            def charts_reading(ps):
                owners = {}
                for i, p in enumerate(ps):
                    if "uv" not in p:
                        continue
                    us = [c[0] for c in p["uv"]]
                    vs = [c[1] for c in p["uv"]]
                    for x in range(min(us), max(us) + 1):
                        for y in range(min(vs), max(vs) + 1):
                            owners.setdefault(
                                (p["texture_page"], x, y), set()).add(chart_of[i])
                return owners

            plane, palettes = a_state_with_both(doc, sheets)
            if plane is None:
                continue
            before = sum(1 for s in charts_reading(polys).values() if len(s) > 1)
            converted, _, _ = V.convert(polys, plane, palettes)
            after = sum(1 for s in charts_reading(converted).values()
                        if len(s) > 1)

            assert before > 0, f"MAP{num:03d}.a{a} shares nothing: no control"
            assert after == 0, (
                f"MAP{num:03d}.a{a}: {after:,} texels still read by two "
                f"charts after conversion")


@needs_corpus
def test_conversion_removes_every_shared_texel_between_polygons():
    """ADR-0186 Amendment 6 decision 22, corpus-wide.

    The strictly stronger form of the chart oracle above: after a conversion
    **no texel is read by two POLYGONS**, not merely by two charts.  A chart
    that folds reads one rectangle from several of its own faces, and the
    chart oracle cannot see that -- 8.5% of charts do it, and a stroke on one
    of those faces repaints a distant one, mirrored.

    Polygon identity needs no carrying, unlike chart identity: a polygon is
    its own index before and after, so the partition cannot go finer under
    the reader's feet.
    """
    for num in (1, 4, 22, 53):
        for a in dumpable_arrangements(MAP_DIR, num):
            doc, sheets = dump(MAP_DIR, num, a)
            polys = doc.get("polygons") or []
            if not any("uv" in p for p in polys) or not sheets:
                continue

            plane, palettes = a_state_with_both(doc, sheets)
            if plane is None:
                continue
            before = sum(1 for s in polygons_reading(polys).values()
                         if len(s) > 1)
            converted, _, _ = V.convert(polys, plane, palettes)
            after = sum(1 for s in polygons_reading(converted).values()
                        if len(s) > 1)

            assert before > 0, f"MAP{num:03d}.a{a} shares nothing: no control"
            assert after == 0, (
                f"MAP{num:03d}.a{a}: {after:,} texels still read by two "
                f"polygons after conversion")


# ---------------------------------------------------------------------------
# ADR-0186 Amendment 3 decision 14 — the Sheet is carried through the blit.
#
# The Painting is not the only picture a conversion moves.  The compiled
# **Sheet** -- the 4bpp index plane the game actually reads -- pictures the UV
# layout it was compiled under, so a conversion that rewrites every UV and
# leaves the Sheet where it was does not produce a stale map, it produces a
# map whose mesh and sheet disagree.  Decision 14 forbids that by CARRYING
# rather than by gating: the same walk that blits the Painting blits the
# indices.
#
# The claim is stronger than the colours one above.  A bake resolves indices
# through a CLUT row, so two different indices naming the same colour would
# pass "zero colours moved"; nothing hides an index that moved.
# ---------------------------------------------------------------------------

def test_every_polygon_reads_the_same_indices_after_a_convert():
    polygons = [
        quad([(40, 40), (48, 40), (40, 48), (48, 48)],
             [(0, 0, 0), (1, 0, 0), (0, 0, 1), (1, 0, 1)]),
        quad([(200, 90), (208, 90), (200, 98), (208, 98)],
             [(5, 0, 0), (6, 0, 0), (5, 0, 1), (6, 0, 1)],
             texture_page=2, palette_id=3),
    ]
    sheet = a_sheet()
    before = [block(sheet, p["uv"], p["texture_page"]) for p in polygons]

    converted, art, moved = V.convert(polygons, sheet, FLAT_PALETTES)

    after = [block(moved, p["uv"], p["texture_page"]) for p in converted]
    assert after == before, "a polygon's INDICES moved"


def indices_read(indices, polygon):
    """The polygon's rectangle as raw 0..15 INDICES, one flat `bytes` per line.

    The sibling of `rows_read`, one step earlier: nothing is resolved through
    a CLUT row, so nothing can be hidden by two indices naming one colour."""
    us = [c[0] for c in polygon["uv"]]
    vs = [c[1] for c in polygon["uv"]]
    base = polygon["texture_page"] * 256
    return [bytes(indices[(base + y) * SHEET_W + x] & 0xF
                  for x in range(min(us), max(us) + 1))
            for y in range(min(vs), max(vs) + 1)]


@needs_corpus
def test_the_whole_corpus_converts_without_moving_a_single_index():
    """Decision 14's oracle, corpus-wide, and the exact claim.

    `test_the_whole_corpus_converts_without_moving_a_single_texel` asks the
    same question of the **Painting**; this one asks it of the **Sheet**.  A
    conversion rewrites every UV, so a Sheet left on the disc's layout
    pictures a layout the mesh no longer uses -- and that is not a stale map,
    it is not a map.  Every textured polygon must read, through its rewritten
    UVs, exactly the indices it read through the shipped ones.
    """
    resources = polygons = 0
    for num in range(1, 130):
        try:
            arrangements = dumpable_arrangements(MAP_DIR, num)
        except Exception:
            continue
        for a in arrangements:
            try:
                doc, sheets = dump(MAP_DIR, num, a)
            except Exception:
                continue
            polys = doc.get("polygons") or []
            if not any("uv" in p for p in polys):
                continue
            plane, palettes = a_state_with_both(doc, sheets)
            if plane is None:
                continue
            resources += 1

            before = [indices_read(plane, p) for p in polys if "uv" in p]

            converted, _, moved = V.convert(polys, plane, palettes)
            after = [indices_read(moved, p) for p in converted if "uv" in p]

            assert len(after) == len(before)
            for i, (was, now) in enumerate(zip(before, after)):
                assert now == was, (
                    f"MAP{num:03d}.a{a} polygon {i} reads different INDICES "
                    f"after conversion")
            polygons += len(before)

    assert resources == CORPUS_CONVERTIBLE
    assert polygons == CORPUS_CONVERTIBLE_POLYGONS


@needs_corpus
def test_a_repack_carries_the_sheet_beside_the_painting():
    """Decision 10's re-pack has the same duty as decision 7's conversion.

    Both walk every island and blit its texels, and both leave the Sheet
    picturing the OLD layout unless it rides along.  This is the arm that
    would go red if only `convert` learned to carry."""
    doc, sheets = dump(MAP_DIR, 22, 0)
    plane, palettes = a_state_with_both(doc, sheets)
    once, art, sheet = V.convert(doc["polygons"], plane, palettes)
    before = [indices_read(sheet, p) for p in once if "uv" in p]

    twice, _, moved = V.repack(once, art, sheet)

    after = [indices_read(moved, p) for p in twice if "uv" in p]
    assert after == before
    assert moved != sheet, "no island moved: the re-pack proves nothing here"


@needs_corpus
def test_a_state_sheet_and_its_painting_are_carried_by_one_unwrap():
    """The pair moves together or it does not move at all.

    MAP022 a0 ships 5 sheets across 20 states.  Every one of them is blitted
    by the single unwrap the mesh gets, and the Painting baked out of it lands
    on exactly the same texels -- so after a conversion the Sheet is still
    `f(Painting, binding)` everywhere the mesh reads."""
    doc, sheets = dump(MAP_DIR, 22, 0)
    originals = [unpack_4bpp(raw) for _, raw in sorted(sheets.items())]
    _, palettes = a_state_with_both(doc, sheets)
    polys = doc["polygons"]

    converted, art, moved = V.convert(polys, originals,
                                      [palettes] * len(originals))
    assert len(moved) == len(originals) == 5

    rows = V.clut_rows(palettes)
    for plane, carried, painting in zip(originals, moved, art):
        for p_before, p_after in zip((p for p in polys if "uv" in p),
                                     (p for p in converted if "uv" in p)):
            assert indices_read(carried, p_after) == indices_read(plane,
                                                                  p_before)
            # ...and the Painting agrees with the Sheet it was baked from,
            # texel for texel, through the row the polygon names.
            row = rows[p_after["palette_id"]]
            assert (rows_painted(painting, p_after)
                    == [bytes(b for i in line for b in row[i])
                        for line in indices_read(carried, p_after)])


# ---------------------------------------------------------------------------
# ADR-0186 Amendment 10 -- the Painting gains a SCALE.
#
# Decision 34 adds a spatial axis to this path: the Painting is N pixels per
# texel, the Sheet stays one.  Decision 37 puts the shrink in FRONT of the
# compile, so nothing under `compile_sheet` learns N existed -- which makes
# these two claims the whole contract this module owes it.
# ---------------------------------------------------------------------------

import resample as R                                        # noqa: E402


@pytest.mark.parametrize("n", [1, 2, 4])
def test_criterion_2_a_conversion_at_N_bakes_what_1x_bakes(n):
    """Amendment 10 grading criterion 2, at the seam that decides it.

    Conversion REPLICATES a disc texel into an N x N block of identical
    pixels, so the box average in front of the compile gives that byte back
    and `compile_sheet` is handed the same 256x1024 buffer at every scale.
    That is what keeps decision 7's "the first compile is a no-op" true on the
    spatial axis, as an exact byte claim rather than an approximate one.

    Three things move together or the claim is empty: the Painting scales, the
    **Sheet does not** (it is the 4bpp resource the game reads), and the UVs
    do not either -- they address the Sheet's texel space, and Blender samples
    the Painting through normalised coordinates, so one unwrap serves both.
    """
    polygons = [
        quad([(40, 40), (48, 40), (40, 48), (48, 48)],
             [(0, 0, 0), (1, 0, 0), (0, 0, 1), (1, 0, 1)]),
        quad([(200, 90), (208, 90), (200, 98), (208, 98)],
             [(5, 0, 0), (6, 0, 0), (5, 0, 1), (6, 0, 1)],
             texture_page=2, palette_id=3),
    ]
    sheet = a_sheet()
    at_1x, art_1x, moved_1x = V.convert(polygons, sheet, FLAT_PALETTES)
    at_n, art_n, moved_n = V.convert(polygons, sheet, FLAT_PALETTES, scale=n)

    assert len(art_n) == 3 * (SHEET_W * n) * (SHEET_H * n)
    assert R.shrink(art_n, SHEET_W * n, SHEET_H * n, n) == art_1x
    assert moved_n == moved_1x, "the Sheet is 1x at every scale"
    assert ([p["uv"] for p in at_n] == [p["uv"] for p in at_1x]
            and [p["texture_page"] for p in at_n]
            == [p["texture_page"] for p in at_1x]), \
        "one unwrap serves both resolutions"


def rows_painted_at(art, polygon, n):
    """`rows_painted`, on an N-times Painting.

    The UVs still address the Sheet's texel space, so the rectangle is scaled
    here rather than in the polygon -- which is the claim as much as the
    check: one unwrap, read at two resolutions.
    """
    us = [c[0] for c in polygon["uv"]]
    vs = [c[1] for c in polygon["uv"]]
    base = polygon["texture_page"] * 256 * n
    w = SHEET_W * n
    x0, x1 = min(us) * n, (max(us) + 1) * n
    return [bytes(art[3 * ((base + y) * w + x0):3 * ((base + y) * w + x1)])
            for y in range(min(vs) * n, (max(vs) + 1) * n)]


@needs_corpus
@pytest.mark.parametrize("n", [2, 4])
def test_a_repack_carries_an_N_times_painting_unresampled(n):
    """Decision 10's re-pack, on the spatial axis.

    A re-pack moves the artist's own picture and never resamples it, so at N
    the block that moves is N times as wide and N times as tall and every byte
    of it survives.  The scale is DERIVED from the buffers rather than passed
    (decision 43's shape): the Painting is N^2 times the Sheet's area, so the
    two already say what N is and there is no stored copy to drift.

    On the corpus rather than a synthetic pair, for the reason the 1x arm of
    this is: a re-pack of two quads lands them where they already were, so the
    control -- that an island MOVED -- cannot pass and the test would be
    green over a `repack` that did nothing at all.
    """
    doc, sheets = dump(MAP_DIR, 22, 0)
    plane, palettes = a_state_with_both(doc, sheets)
    once, art, sheet = V.convert(doc["polygons"], plane, palettes, scale=n)
    before = [rows_painted_at(art, p, n) for p in once if "uv" in p]

    twice, moved_art, moved_sheet = V.repack(once, art, sheet)

    assert len(moved_art) == len(art)
    after = [rows_painted_at(moved_art, p, n) for p in twice if "uv" in p]
    assert after == before
    assert moved_sheet != sheet, "no island moved: the re-pack proves nothing"


@needs_corpus
def test_repack_refuses_a_painting_that_is_not_N_squared_the_sheet():
    """The derivation is a REFUSAL, not a guess.

    A Painting and a Sheet that do not stand in an `N^2` area relation are not
    a pair, and blitting one against the other would carry the artist's
    picture to addresses computed from the wrong stride -- silently, and in a
    shape that reads as a corrupted painting rather than as a mismatched call.
    """
    doc, sheets = dump(MAP_DIR, 22, 0)
    plane, palettes = a_state_with_both(doc, sheets)
    once, art, sheet = V.convert(doc["polygons"], plane, palettes, scale=2)
    with pytest.raises(ValueError, match="N"):
        V.repack(once, art + b"\x00" * 3, sheet)
