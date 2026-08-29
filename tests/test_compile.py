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

import random
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


def bag(colours, texels=1):
    """A histogram in `histogram()`'s shape -- `{(r, g, b): texels}`."""
    return {Q.snap(c): texels for c in colours}


#: Sixteen lattice colours, which is exactly what one CLUT row holds.
SIXTEEN = [Q.snap((8 * i, 200 - 8 * i, 4 * i)) for i in range(16)]

#: Twenty, which is four more than a row can hold, so a row pooling these
#: cannot be exact and its error is above zero for a reason from the format.
TWENTY = [Q.snap((6 * i, 240 - 6 * i, 3 * i)) for i in range(20)]


def test_the_scorer_hands_back_the_palettes_it_quantised():
    """Decision 31's free half, and the shape that makes it possible.

    `select_binding` quantised all sixteen rows inside its scorer and threw
    the palettes away, then re-quantised the same rows from the same members
    so each chart could pick its next one.  The fused call returns both.

    The contract is graded against the FORMAT, not against the code it
    replaces: a CLUT row holds sixteen entries, so a row whose pooled bag
    carries no more than sixteen lattice colours must come back holding
    exactly them, at zero error.  That is the same oracle
    `test_the_first_compile_of_a_real_map_changes_no_colour` runs on the disc.
    """
    bags = [bag(SIXTEEN[:8]), bag(SIXTEEN[8:])]

    err, palettes = C.score_and_palettes(bags, [3, 3])

    assert set(palettes) == {3}, \
        "a row nothing is bound to reads nothing, so it has no palette here"
    assert sorted(palettes[3]) == sorted(SIXTEEN), (
        "sixteen lattice colours pooled into one row are exactly what that "
        "row can hold, so they must come back unchanged")
    assert err == 0.0


def test_the_score_is_weighted_by_TEXELS_and_not_by_ROWS():
    """The objective is the whole map's count-weighted mean error, which is
    what makes a big badly-fitted surface matter more than a small one.

    Two rows, one exactly representable and one that cannot be: which of them
    carries the map's texels is the whole difference.  Asserted as a
    comparison rather than a number, so nothing here recomputes the mean the
    way the scorer does.
    """
    clean, dirty = bag(SIXTEEN), bag(TWENTY)

    heavy_clean, _ = C.score_and_palettes(
        [bag(SIXTEEN, 100), bag(TWENTY, 1)], [0, 1])
    heavy_dirty, _ = C.score_and_palettes(
        [bag(SIXTEEN, 1), bag(TWENTY, 100)], [0, 1])

    alone, _ = C.score_and_palettes([dirty], [1])
    assert alone > 0.0, (
        "twenty colours do not fit in a sixteen-entry row; if this is zero "
        "the fixture proves nothing about weighting")
    assert C.score_and_palettes([clean], [0])[0] == 0.0
    assert heavy_clean < heavy_dirty, (
        "the same two rows, weighted the other way, must not score the same")


#: A fixture with more colour FAMILIES than there are CLUT rows, so rows must
#: pool, no binding can be exact, and the search does not settle on its first
#: pass.  `random.Random(11)` is not decoration: seeds 0-10 all reached a fixed
#: point immediately (`scored == 2`), which would have pinned a single pass.
PINNED_SEED, PINNED_CHARTS, PINNED_COLOURS = 11, 24, 12


def pinned_fixture():
    rnd = random.Random(PINNED_SEED)
    quads = chart_quads(PINNED_CHARTS)
    for q in quads:                     # everything on one row: a bad binding
        q["palette_id"] = 0
    families = [family((rnd.randrange(160), rnd.randrange(160),
                        rnd.randrange(160)), PINNED_COLOURS)
                for _ in range(PINNED_CHARTS)]
    return quads, painted(quads, families)


def test_fusing_the_scorer_and_the_palette_build_moves_no_binding_and_no_bit():
    """Amendment 7 decision 31 calls the fuse *"provably a refactor -- same
    binding, same error to the last bit, same pass count"*.  This is the proof.

    The four literals below were captured from the search as it stood BEFORE
    `score_and_palettes` existed, when it scored a binding with one set of
    sixteen `quantise` calls and then re-quantised the same sixteen rows from
    the same members to move the charts.  They are a pin, and that is the
    point: a fuse that changed any of them would not be a refactor, and
    nothing cheaper can tell the difference between the two.

    It keeps its value after the fuse, because the objective is what every
    other decision in this ADR is argued against: decision 8's "never lose to
    the disc", decision 9's per-chart report and decision 31's own
    equal-quality claim are all statements about THIS number.  Moving it
    should cost a deliberate re-pin.
    """
    quads, art = pinned_fixture()

    chosen = C.select_binding(quads, art)

    assert list(chosen.binding) == [12, 12, 5, 15, 11, 4, 13, 14, 1, 2, 6, 10,
                                    15, 8, 0, 8, 3, 7, 2, 4, 9, 0, 6, 10]
    assert chosen.error == 24.951171875
    assert chosen.incumbent_error == 880.55712890625
    assert chosen.scored == 3, (
        "the pin is worth less if the search settles on its first pass: "
        "three candidates is one seed plus two passes of the clusterer")
    assert not chosen.is_incumbent


def off_lattice_twin(art, seed=7, spread=3):
    """The same picture to the FORMAT, different everywhere else.

    Every texel is offered a small move and the move is kept only where `snap`
    is unmoved -- so no CLUT row could tell the two paintings apart, and a
    search that ranks on the lattice must not either.
    """
    rnd = random.Random(seed)
    out = bytearray(art)
    moved = 0
    for i in range(0, len(out), 3):
        was = (out[i], out[i + 1], out[i + 2])
        if was == (0, 0, 0):
            continue
        cand = tuple(max(0, min(255, c + rnd.randint(-spread, spread)))
                     for c in was)
        if cand != was and Q.snap(cand) == Q.snap(was):
            out[i:i + 3] = bytes(cand)
            moved += 1
    return bytes(out), moved


def test_a_colour_no_CLUT_ROW_could_hold_moves_no_chart_and_no_score():
    """Decision 31: the search's bags are lattice-snapped, and the ADR argues
    that as the better-POSED question rather than as a shortcut -- a CLUT row
    cannot hold an off-lattice colour, so a difference no row could ever
    express must not change which row a chart is bound to.

    Measured before it was built: the exact fixture below moved 6,124 texels
    without moving a single colour the format can hold, and the search read
    them as 6,124 more distinct colours and re-bound seven of twenty-four
    charts.  It also caps the bag at the lattice's 32,768 colours instead of a
    painted canvas's 136,133, which is where the 10.6x comes from.

    The score is asserted alongside the binding because that is the other half
    of the same claim: `Selection.error` is a number about the lattice, and it
    is the FIT (`compile_sheet`, still on the true painting) that measures what
    the artist will actually see.
    """
    quads, art = pinned_fixture()
    twin, moved = off_lattice_twin(art)
    assert moved > 1000, (
        f"only {moved} texels differ off-lattice; the fixture proves nothing")

    one, two = C.select_binding(quads, art), C.select_binding(quads, twin)

    assert list(one.binding) == list(two.binding)
    assert one.error == two.error
    assert one.incumbent_error == two.incumbent_error


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


# ---------------------------------------------------------------------------
# Decision 49 -- an animated CLUT row is not a colour the search may spend.
# ---------------------------------------------------------------------------
#
# Reported from use, on MAP022: *"if I choose a color, and then paint, it will,
# sometimes, do an issue we resolved before which is a palette would inherit
# the water animation -- and then a bunch of polygons would turn blue and
# shimmer like water."*  Measured: MAP022's `0x6c` animates rows 13, 14 and 15;
# the disc puts 52 of 385 polygons on row 13 and NOTHING on 14 or 15, so those
# two look free to a scorer that ranks on colour error alone.  Over a painted
# canvas the unbounded search put up to 152 of 385 polygons onto animated rows.
#
# Note what does NOT have to be pressed for the artist to see this: the settle
# runs the whole compile, search included, and pushes (Amendment 9, decision
# 28).  Painting alone re-binds.

def repainted(art):
    """A converted map's Painting, as an artist leaves it.

    Each 16x16 block of the sheet is flooded with one colour taken from the
    map's OWN distinct set, on a coarse two-prime pattern.  Three properties
    it is chosen for, in the order they matter:

    * It really is a different picture, so the incumbent stops being exactly
      representable and the search starts choosing rows.  `off_lattice_twin`
      -- the other repaint in this file -- deliberately does not: it moves
      only where `snap` is unmoved, so no row can tell the two apart and the
      search correctly does not move either.
    * A *permutation* of the colours does not work either, and it is the
      obvious thing to reach for: it maps each row's sixteen colours to
      sixteen others, so the incumbent still represents the picture exactly
      and ties.  Colours have to cross chart boundaries, which is what a
      block pattern does and a per-colour remap cannot.
    * The distinct-colour count is unchanged, and that is the whole cost:
      `cProfile` puts the search almost entirely inside `quantise._nearest`,
      over the bag.  A per-texel jitter asserts the same thing and takes ten
      seconds a call instead of a tenth of one.
    """
    seen = sorted({bytes(art[i:i + 3]) for i in range(0, len(art), 3)})
    out = bytearray(art)
    for t in range(len(art) // 3):
        u, v = t % C.SHEET_W, t // C.SHEET_W
        out[3 * t:3 * t + 3] = seen[((u // 16) * 7 + (v // 16) * 13) % len(seen)]
    return bytes(out)


def test_the_partition_is_by_the_incumbents_animatedness():
    """`held_and_free` is the whole rule, and it is one rule in two directions.

    A chart on an animated row may not leave it -- the animated rows are not
    interchangeable, each record carrying its own frames -- and no other chart
    may arrive on one.
    """
    incumbent = [13, 5, 14, 5, 0]

    movable, free_rows = C.held_and_free(incumbent, [13, 14, 15])

    assert movable == [1, 3, 4], "the charts on 13 and 14 are held"
    assert free_rows == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
    assert 15 not in free_rows, (
        "row 15 holds no chart and is still animated -- an EMPTY animated row "
        "is exactly the one the scorer finds cheapest, which is the defect")


def test_no_animation_is_the_search_this_module_already_had():
    """The common map, and the arm that says this decision costs it nothing.

    1,465 of the corpus's 1,575 resources carry no `0x6c` at all, so the
    default has to be today's search and not a near miss of it.  Asserted as
    an identical BINDING rather than an identical error, because two searches
    can score the same and bind differently.
    """
    quads = chart_quads(24)
    art = painted(quads, [EIGHT_FAMILIES[i % 8] for i in range(24)])

    plain = C.select_binding(quads, art)
    absent = C.select_binding(quads, art, animated=None)
    empty = C.select_binding(quads, art, animated=())

    assert absent.binding == plain.binding
    assert empty.binding == plain.binding
    assert absent.error == plain.error == empty.error
    assert absent.scored == plain.scored == empty.scored


def test_a_held_chart_keeps_its_OWN_animated_row_not_merely_an_animated_one():
    """Row 13's frames are not row 14's, so "still animated" is not enough.

    The `0x70` chunk is one cycle per record, so moving water from 13 to 14
    swaps one animation for another -- a different defect wearing the same
    shape.  The assertion is on the row, not on the set.
    """
    quads = chart_quads(24)
    for i, q in enumerate(quads):
        q["palette_id"] = 13 if i < 4 else 14 if i < 6 else 0
    art = painted(quads, [EIGHT_FAMILIES[i % 8] for i in range(24)])

    chosen = C.select_binding(quads, art, animated=[13, 14, 15])

    assert [chosen.binding[i] for i in range(4)] == [13, 13, 13, 13]
    assert [chosen.binding[i] for i in range(4, 6)] == [14, 14]
    assert all(chosen.binding[i] not in (13, 14, 15) for i in range(6, 24)), (
        "a chart the disc did not animate must not become animated")


def test_a_map_whose_every_row_is_animated_is_a_no_op_RESULT():
    """Nothing to move is decision 8's tie, not a refusal.

    No corpus map does this, and the guard is here because the seed and the
    reassign step both divide by the number of rows the search may use: an
    empty candidate set is a `ZeroDivisionError` one map away, and it must be
    a result instead.
    """
    quads = chart_quads(8)
    for i, q in enumerate(quads):
        q["palette_id"] = i % 4
    art = painted(quads, EIGHT_FAMILIES)

    chosen = C.select_binding(quads, art, animated=range(16))

    assert chosen.is_incumbent
    assert chosen.binding == [q["palette_id"] for q in quads]
    assert chosen.error == chosen.incumbent_error


@needs_corpus
def test_MAP022s_search_moves_no_chart_onto_its_water_rows():
    """The reported defect, on the map it was reported on.

    The Painting is jittered off the disc's own sheet, which is what an artist
    painting does to it: a freshly converted map is exactly representable
    under its own binding, so the incumbent ties and the search never moves at
    all -- an arm on the untouched conversion is BLIND to this and would pass
    with the bound deleted.  The control below is what makes it not blind: the
    same painting, unbounded, moves charts onto rows 14 and 15.
    """
    doc, sheets = dump(MAP_DIR, 22, 0)
    plane, palettes = a_state_with_both(doc, sheets)
    converted, art, _ = V.convert(doc["polygons"], plane, palettes)
    art = repainted(art)

    animated = [13, 14, 15]
    was = [q["palette_id"] for q in converted if "uv" in q]

    loose = C.select_binding(converted, art)
    bound = C.select_binding(converted, art, animated=animated)

    def on_animated(binding):
        return sum(1 for i, q in enumerate(converted)
                   if "uv" in q and binding[i] in animated)

    assert not loose.is_incumbent, "the control has to have MOVED something"
    assert on_animated(loose.binding) != was.count(13), (
        "the control: unbounded, this painting moves polygons on or off the "
        "animated rows -- without that this test cannot fail")

    now = [bound.binding[i] for i, q in enumerate(converted) if "uv" in q]
    assert [r for r in now if r in animated] == [r for r in was if r in animated]
    assert all((a in animated) == (b in animated) for a, b in zip(was, now)), (
        "the search changed whether a chart is animated")
    assert now.count(13) == was.count(13) == 52
    assert now.count(14) == now.count(15) == 0, (
        "rows 14 and 15 are animated and EMPTY on the disc, which is what "
        "makes them look free to the scorer")


@needs_corpus
def test_the_bound_search_still_never_loses_to_the_disc():
    """Decision 8 survives the bound, and that is not automatic.

    The incumbent is feasible under decision 49 by construction -- every chart
    is already on a row of its own class -- so it stays in the candidate set
    and the minimum over that set can still only tie or improve.  A bound that
    excluded the incumbent would have traded one defect for a worse one.
    """
    doc, sheets = dump(MAP_DIR, 22, 0)
    plane, palettes = a_state_with_both(doc, sheets)
    converted, art, _ = V.convert(doc["polygons"], plane, palettes)
    art = repainted(art)

    bound = C.select_binding(converted, art, animated=[13, 14, 15])

    assert bound.error <= bound.incumbent_error
