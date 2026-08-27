"""The compile: a Painting and a row binding become a Sheet (ADR-0186).

Decision 15 separates the compile into a **search** and a **fit**.  *Recalculate
palettes* recompiles with the binding held; *Re-select clusters* moves the
binding first and then recompiles.  There are not two compiles -- there is one
compile and one thing that moves its input -- so this file grades
`compile_sheet` first and `select_binding` as a thing that hands it a
different `palette_id`.

The oracle for the fit is the ADR's own claim that **the first compile is a
no-op**: conversion bakes the disc's sheet into a true-colour picture using
nothing but the CLUT rows the polygons already named, so compiling that
picture straight back must land on the same colour at every texel.  It is
exact rather than approximate, and it needs no reference implementation --
the disc is the reference.
"""

import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
ADDON = PKG / "addons" / "exmateria_map"
sys.path.insert(0, str(ADDON))
sys.path.insert(0, str(PKG))

import compile_map as C                                      # noqa: E402
import convert as V                                          # noqa: E402
import quantise as Q                                         # noqa: E402
from exmateria_map import corpus                             # noqa: E402
from exmateria_map.dump import dump                          # noqa: E402
from exmateria_map.png_indexed import unpack_4bpp            # noqa: E402

MAP_DIR = corpus.map_dir()
needs_corpus = pytest.mark.skipif(
    MAP_DIR is None,
    reason="corpus absent; set EXMATERIA_ASSETS_DIR to the extracted disc tree",
)

SHEET_W, SHEET_H = 256, 1024


def quad(uv, positions, palette_id=0, texture_page=0):
    return {"kind": "textured_quad", "uv": [list(c) for c in uv],
            "positions": [list(p) for p in positions],
            "palette_id": palette_id, "texture_page": texture_page}


#: Sixteen rows of sixteen, all distinct and all ON THE LATTICE, so a compile
#: that reproduced them is reproducing the row rather than rounding into it.
FLAT_PALETTES = [{"colors": ["#%02X%02X%02X" % tuple(
                        Q.snap((r * 16 + c, r * 9, c * 17)))
                            for c in range(16)], "stp": 0}
                 for r in range(16)]


def a_sheet():
    return bytes(((x * 7 + y * 13) % 16)
                 for y in range(SHEET_H) for x in range(SHEET_W))


def shows(compiled, polygons, art):
    """Every texel every polygon reads, as `(painted, compiled)` colour pairs.

    The compiled side is resolved the way the GAME resolves it -- the row the
    polygon names, indexed by the sheet -- so this is a claim about what the
    artist sees and not about the numbers on either side of the compile.
    """
    out = []
    for p in polygons:
        if "uv" not in p:
            continue
        row = compiled.palettes[p["palette_id"]]
        for t in C.texel_addresses(p):
            out.append((tuple(art[3 * t:3 * t + 3]),
                        tuple(row[compiled.indices[t]])))
    return out


def test_the_first_compile_of_a_converted_map_changes_no_colour():
    polygons = [
        quad([(40, 40), (48, 40), (40, 48), (48, 48)],
             [(0, 0, 0), (1, 0, 0), (0, 0, 1), (1, 0, 1)]),
        quad([(200, 90), (208, 90), (200, 98), (208, 98)],
             [(5, 0, 0), (6, 0, 0), (5, 0, 1), (6, 0, 1)],
             texture_page=2, palette_id=3),
    ]
    converted, art, _ = V.convert(polygons, a_sheet(), FLAT_PALETTES)

    compiled = C.compile_sheet(converted, art)

    pairs = shows(compiled, converted, art)
    assert pairs, "no texel was compiled: the assertion below is vacuous"
    assert all(painted == got for painted, got in pairs), (
        f"{sum(1 for a, b in pairs if a != b)} of {len(pairs)} texels "
        f"changed colour on the first compile")
    assert compiled.error == 0.0


def chart_quads(n, colours_per=6):
    """`n` charts that share no vertex and no texel.

    Disjoint positions is what makes them separate charts -- `charts()` welds
    across a shared mesh edge -- and disjoint UV boxes is what a CONVERTED map
    already guarantees (0.00% of texels have two chart readers).  So this is
    the shape the compile always meets, built without needing a disc.
    """
    quads = []
    for i in range(n):
        u, v = (i % 8) * 32, (i // 8) * 32
        z = 100 * (i + 1)
        quads.append(quad([(u, v), (u + 15, v), (u, v + 15), (u + 15, v + 15)],
                          [(z, 0, z), (z + 1, 0, z), (z, 0, z + 1),
                           (z + 1, 0, z + 1)]))
    return quads


def painted(quads, families):
    """A Painting with one colour family per chart, all on the lattice."""
    art = bytearray(3 * SHEET_W * SHEET_H)
    for q, family in zip(quads, families):
        for k, t in enumerate(C.texel_addresses(q)):
            art[3 * t:3 * t + 3] = bytes(family[k % len(family)])
    return bytes(art)


def family(base, n=6):
    """`n` lattice colours clustered around one hue, so a row that pools two
    families fits neither well and a row that holds one fits it exactly."""
    return [Q.snap((base[0] + 8 * i, base[1] + 8 * i, base[2])) for i in range(n)]


EIGHT_FAMILIES = [family((16 * i, 200 - 16 * i, 8 * i)) for i in range(8)]


def test_a_search_that_cannot_improve_says_so_rather_than_looking_idle():
    """Decision 8 makes the incumbent a candidate, so it can legitimately WIN
    -- and the ADR's Consequences require that be said, not left looking like
    a button that did nothing."""
    quads = chart_quads(4)
    for i, q in enumerate(quads):
        q["palette_id"] = i
    art = painted(quads, EIGHT_FAMILIES[:4])

    compiled = C.compile_sheet(quads, art)
    assert compiled.error == 0.0, "the fixture must be exactly representable"

    chosen = C.select_binding(quads, art)
    assert chosen.is_incumbent
    assert chosen.error == chosen.incumbent_error == 0.0


def test_the_search_never_loses_to_the_binding_the_disc_ships():
    """Decision 8's whole purpose, and it holds by construction: the winner is
    a minimum over a candidate set the incumbent is IN."""
    quads = chart_quads(8)
    for q in quads:                     # everything on one row: a bad binding
        q["palette_id"] = 0
    art = painted(quads, EIGHT_FAMILIES)

    chosen = C.select_binding(quads, art)
    assert chosen.error <= chosen.incumbent_error
    assert chosen.error < chosen.incumbent_error, (
        "eight colour families crammed into one CLUT row is not something a "
        "search should fail to improve; the fixture proves nothing otherwise")
    assert not chosen.is_incumbent


def test_the_search_is_not_seeded_from_the_incumbent():
    """Decision 8: *"by selection, not by seeding, which was measured and is
    worse"* -- 44.41 seeded against 34.68 unseeded on MAP022 a0.

    Two incumbents that are equally bad in different places.  A seeded search
    would start in two different corners and land in two different minima; an
    unseeded one starts from the Painting alone and lands in the same place
    both times.  The atoms are handed in so the two runs score the SAME
    partition -- `charts()` cuts at a `palette_id` change, so a differing
    incumbent would otherwise hand the search a differing set of charts and
    the comparison would be about that instead.
    """
    quads = chart_quads(8)
    art = painted(quads, EIGHT_FAMILIES)
    atoms = [[i] for i in range(8)]

    runs = []
    for row in (0, 7):
        binding = [dict(q, palette_id=row) for q in quads]
        runs.append(C.select_binding(binding, art, atoms=atoms))

    assert runs[0].incumbent_error == runs[1].incumbent_error, \
        "the two incumbents must be equally bad, or this proves nothing"
    assert runs[0].error == runs[1].error
    assert all(r.error < r.incumbent_error for r in runs)


def test_regressions_are_reported_per_chart_and_ranked_by_how_much():
    """Decision 9: rule per map, report per chart.  A global mean can improve
    while one corner of the mesh gets visibly worse."""
    atoms = [[0], [1], [2]]
    rose = C.regressions([1.0, 5.0, 2.0], [3.0, 5.0, 9.0], atoms)
    assert [i for i, _, _, _ in rose] == [2, 0], \
        "worst regression first, and a chart that did not regress is absent"
    assert rose[0][1:3] == (2.0, 9.0)
    assert C.regressions([1.0], [1.0], [[0]]) == []


def a_state_with_both(doc, sheets):
    """One state's (index plane, palettes).  `tests/test_convert.py`'s rule,
    restated: `map_states` interleaves a TEXTURE row that names the sheet and
    carries `palettes: null` with a MESH row that carries the palettes and
    names no sheet, and they pair by `(night, weather)`."""
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


@needs_corpus
@pytest.mark.parametrize("num", [1, 4, 22, 53, 96])
def test_the_first_compile_of_a_real_map_changes_no_colour(num):
    """ADR-0186's *"the first compile is a no-op"*, against the disc.

    Conversion bakes the disc's sheet into a true-colour picture using nothing
    but the CLUT rows the polygons already named, so every row's histogram
    holds at most the sixteen colours that row already carries -- all of them
    on the lattice, because they came off a BGR555 word.  Compiling that
    picture straight back therefore has to land on the same colour at every
    texel, exactly.

    It needs no reference implementation: the disc is the reference, and the
    two sides are computed by different code (one bakes indices through a
    CLUT, the other quantises a bag and nearest-indexes it).
    """
    doc, sheets = dump(MAP_DIR, num, 0)
    plane, palettes = a_state_with_both(doc, sheets)
    assert plane is not None, f"MAP{num:03d}.a0 ships no sheet to convert"

    converted, art, _ = V.convert(doc["polygons"], plane, palettes)
    compiled = C.compile_sheet(converted, art,
                               incumbent=V.clut_rows(palettes))

    pairs = shows(compiled, converted, art)
    assert len(pairs) > 1000, len(pairs)
    wrong = sum(1 for was, now in pairs if was != now)
    assert wrong == 0, (f"MAP{num:03d}.a0: {wrong} of {len(pairs)} texels "
                        f"changed colour on the first compile")
    assert compiled.error == 0.0


@needs_corpus
def test_a_real_maps_search_never_loses_to_the_disc(): 
    """Decision 8 on real data -- and the number the ADR measured.

    MAP022 a0 is the map `workspace/chartcluster.py` measured 44.41 (seeded)
    against 34.68 (unseeded) on.  What is asserted here is only the rule, not
    either number: the winner is a minimum over a candidate set the incumbent
    is in, so it can tie and can never lose.
    """
    doc, sheets = dump(MAP_DIR, 22, 0)
    plane, palettes = a_state_with_both(doc, sheets)
    converted, art, _ = V.convert(doc["polygons"], plane, palettes)

    chosen = C.select_binding(converted, art)

    assert chosen.error <= chosen.incumbent_error
    assert chosen.incumbent_error == 0.0, (
        "a freshly converted map is exactly representable under its own "
        "binding, so this is the tie case and `is_incumbent` must say so")
    assert chosen.is_incumbent
