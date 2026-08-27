"""The quantiser's `bpy`-free core (ADR-0007 decision 4).

`addons/exmateria_map/quantise.py` imports `bpy` never, for the reason
`live_link.py` does not: a quantiser iterated through Blender restarts is a
quantiser nobody tunes. What is testable under plain `pytest` lives here; the
panel, the operator and the stress run against a real paint image do not.

Every expectation in this file comes from the FORMAT -- the BGR555 word and
its `c8 = c5 * 255 // 31` expansion (`CONTEXT.md`, "Unrepresentable vs
unreferenced") -- or from `exmateria_map/document.py`, which is a second,
independently written implementation of the same arithmetic. Neither side of
an assertion is computed by the code under test.
"""

import json
import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
ADDON = PKG / "addons" / "exmateria_map"
sys.path.insert(0, str(ADDON))
sys.path.insert(0, str(PKG))

import quantise as Q                                        # noqa: E402
from exmateria_map import document as doc                    # noqa: E402


#: The 32 byte values a BGR555 channel can expand to. Written out rather than
#: derived, so a change to the expansion has to be argued for here too.
#: 224 of the 256 byte values do NOT survive 8 -> 5 -> 8.
SPEC_LATTICE = (0, 8, 16, 24, 32, 41, 49, 57, 65, 74, 82, 90, 98, 106, 115,
                123, 131, 139, 148, 156, 164, 172, 180, 189, 197, 205, 213,
                222, 230, 238, 246, 255)


def test_lattice_is_the_32_reachable_byte_values():
    assert tuple(Q.LATTICE) == SPEC_LATTICE


def test_lattice_agrees_with_the_packages_own_expansion():
    """`document.bgr555_to_hex` is the disc-side implementation of the same
    expansion. Two implementations, one lattice, or one of them is wrong."""
    from_document = [int(doc.bgr555_to_hex(c)[1:3], 16) for c in range(32)]
    assert list(Q.LATTICE) == from_document


# ---------------------------------------------------------------------------
# Unrepresentable: off the lattice, and no palette decision can help.
# ---------------------------------------------------------------------------

def test_snap_agrees_with_the_discs_own_round_trip():
    """`document.hex_to_bgr555` then `bgr555_to_hex` is the disc-side 8->5->8
    round trip. `snap` must land on exactly the same byte for all 256."""
    for c8 in range(256):
        word = doc.hex_to_bgr555("#%02X%02X%02X" % (c8, c8, c8))
        expect = int(doc.bgr555_to_hex(word)[1:3], 16)
        assert Q.snap((c8, c8, c8)) == (expect, expect, expect), c8


def test_snap_is_idempotent_on_the_lattice():
    for v in SPEC_LATTICE:
        assert Q.snap((v, v, v)) == (v, v, v)


def test_snap_moves_every_byte_value_the_format_cannot_hold():
    moved = [c for c in range(256) if Q.snap((c, c, c))[0] != c]
    assert len(moved) == 224, len(moved)
    assert set(range(256)) - set(moved) == set(SPEC_LATTICE)


def test_on_lattice_is_snap_being_a_no_op():
    assert Q.on_lattice((0, 0, 0))
    assert Q.on_lattice((255, 255, 255))
    assert Q.on_lattice((8, 41, 255))
    # One byte off in ONE channel is enough to be unrepresentable.
    assert not Q.on_lattice((8, 41, 254))
    assert not Q.on_lattice((7, 41, 255))


def test_snap_picks_the_nearer_lattice_point():
    """Not merely *a* lattice point. 90 and 98 are neighbours; 93 is nearer
    to 90 and 95 is nearer to 98, and a truncating snap gets 95 wrong."""
    assert Q.snap((93, 93, 93)) == (90, 90, 90)
    assert Q.snap((95, 95, 95)) == (98, 98, 98)


def test_channels_snap_independently():
    assert Q.snap((93, 95, 1)) == (90, 98, 0)


# ---------------------------------------------------------------------------
# ADR-0007 decision 5: a refusal names WHICH failure it is.
# ---------------------------------------------------------------------------

#: Sixteen on-lattice colours standing in for a CLUT row. Every entry is a
#: colour the format can hold, because a row read off the disc always is.
ROW = ((0, 0, 0), (255, 255, 255), (8, 8, 8), (16, 16, 16), (24, 24, 24),
       (32, 32, 32), (41, 41, 41), (49, 49, 49), (57, 57, 57), (65, 65, 65),
       (74, 74, 74), (82, 82, 82), (90, 90, 90), (98, 98, 98),
       (255, 0, 0), (0, 255, 0))


def test_a_colour_the_row_holds_is_not_a_refusal():
    assert Q.refusal_kind((41, 41, 41), ROW) is None
    assert Q.refusal_kind((255, 0, 0), ROW) is None


def test_on_the_lattice_and_absent_from_the_row_is_unreferenced():
    """Another row may already hold it, a duplicate entry has a slot for it,
    an unreferenced row could be given it -- the remedy is a palette
    decision, so this is the half a quantiser can actually spend fidelity on."""
    assert (0, 0, 255) not in ROW and Q.on_lattice((0, 0, 255))
    assert Q.refusal_kind((0, 0, 255), ROW) == "unreferenced"


def test_off_the_lattice_is_unrepresentable():
    assert Q.refusal_kind((1, 2, 3), ROW) == "unrepresentable"


def test_unrepresentable_wins_even_when_the_snap_lands_in_the_row():
    """(42, 42, 42) snaps to (41, 41, 41), which the row DOES hold. It is
    still unrepresentable: the remedy is the snap, not a palette decision,
    and filing it under `unreferenced` is the conflation that reports the
    format's bit depth as palette scarcity."""
    assert Q.snap((42, 42, 42)) == (41, 41, 41) and (41, 41, 41) in ROW
    assert Q.refusal_kind((42, 42, 42), ROW) == "unrepresentable"


def test_the_two_kinds_partition_a_bag_of_refusals():
    bag = [(0, 0, 255), (8, 0, 255), (1, 2, 3), (42, 42, 42), (41, 41, 41)]
    counts = Q.partition(bag, ROW)
    assert counts == {"resolved": 1, "unreferenced": 2, "unrepresentable": 2}


# ---------------------------------------------------------------------------
# The colour chart. Its whole value is COST (the handoff's §4): the refusal count is
# arithmetically predetermined, so what the stress run measures is what the
# resolve and the gate charge for a sheet full of colours they cannot hold.
# ---------------------------------------------------------------------------

SHEET_TEXELS = 256 * 1024                       # 262,144


def test_the_gamut_is_32768_colours_not_16_7_million():
    """A CLUT entry is a BGR555 word, so the whole reachable space is 32,768
    -- 12.5% of the sheet at one texel each. A 24-bit chart would refuse
    99.8% of its pixels to an integer division and measure bit depth."""
    assert Q.FULL_GAMUT == 32 ** 3 == 32768
    assert Q.FULL_GAMUT * 8 == SHEET_TEXELS


def test_the_full_gamut_chart_is_every_colour_the_format_can_hold_once():
    seq = Q.colour_chart(colours=Q.FULL_GAMUT)
    assert len(seq) == Q.FULL_GAMUT
    assert len(set(seq)) == Q.FULL_GAMUT
    assert set(seq) == {(r, g, b) for r in SPEC_LATTICE
                        for g in SPEC_LATTICE for b in SPEC_LATTICE}


@pytest.mark.parametrize("n", [1, 8, 16, 1000, 4000, 16000, 32768])
def test_a_scaled_chart_has_exactly_the_colours_asked_for(n):
    seq = Q.colour_chart(colours=n)
    assert len(seq) == n and len(set(seq)) == n
    assert all(Q.on_lattice(c) for c in seq)


def test_more_colours_than_the_format_has_is_refused():
    with pytest.raises(ValueError):
        Q.colour_chart(colours=Q.FULL_GAMUT + 1)


def test_a_prefix_is_spread_over_the_cube_not_clustered_in_one_corner():
    """A scaled run whose colours all sit in one corner measures something
    other than a run that samples the space. The first eight are the eight
    coarse octant corners."""
    assert set(Q.colour_chart(colours=8)) == {(r, g, b) for r in (0, 131)
                                       for g in (0, 131) for b in (0, 131)}


def test_a_power_of_two_prefix_is_an_exact_uniform_sub_grid():
    """The strong form of "spread": at 1,024 the chart is precisely every
    other red level against every fourth green and blue -- a 16x8x8 grid
    over the whole cube, not 1,024 neighbours of black."""
    assert set(Q.colour_chart(colours=1024)) == {
        (SPEC_LATTICE[r], SPEC_LATTICE[g], SPEC_LATTICE[b])
        for r in range(0, 32, 2)
        for g in range(0, 32, 4) for b in range(0, 32, 4)}


def test_every_octant_is_populated_at_every_scale():
    for n in (8, 64, 1000, 4096):
        octants = {}
        for c in Q.colour_chart(colours=n):
            key = tuple(ch >= 128 for ch in c)
            octants[key] = octants.get(key, 0) + 1
        assert len(octants) == 8, (n, octants)
        assert max(octants.values()) - min(octants.values()) <= 1, (n, octants)


def test_the_chart_is_deterministic():
    assert Q.colour_chart(colours=500, off_gamut=7) == Q.colour_chart(colours=500,
                                                        off_gamut=7)


def test_the_off_gamut_band_is_between_the_lattice_points():
    """Its job is to prove the stress test can tell the two failures APART.
    Without it, an implementation that lumps unrepresentable and unreferenced
    into one bucket passes."""
    seq = Q.colour_chart(colours=100, off_gamut=40)
    assert len(seq) == 140 and len(set(seq)) == 140
    band = seq[100:]
    assert len(set(band)) == 40
    assert not any(Q.on_lattice(c) for c in band)
    assert all(Q.on_lattice(c) for c in seq[:100])
    assert not set(band) & set(seq[:100])


def test_the_band_is_unrepresentable_and_the_gamut_is_not():
    seq = Q.colour_chart(colours=100, off_gamut=40)
    counts = Q.partition(seq, ROW)
    assert counts["unrepresentable"] == 40
    assert counts["resolved"] + counts["unreferenced"] == 100


def test_the_sequence_tiles_to_fill_a_sheet():
    """32,768 colours over 262,144 texels is eight copies; the distinct
    colour count and the painted pixel count are separate dials, which is
    what lets a scaled run move one without the other."""
    seq = Q.colour_chart(colours=16, texels=64)
    assert len(seq) == 64 and len(set(seq)) == 16
    assert seq[:16] == seq[16:32] == seq[32:48] == seq[48:]
    full = Q.colour_chart(colours=Q.FULL_GAMUT, texels=SHEET_TEXELS)
    assert len(full) == SHEET_TEXELS
    assert len(set(full)) == Q.FULL_GAMUT


def test_a_partial_tile_truncates_rather_than_dropping_a_colour():
    seq = Q.colour_chart(colours=10, texels=25)
    assert len(seq) == 25
    assert seq[20:] == Q.colour_chart(colours=10)[:5]


def test_texels_below_the_colour_count_is_refused():
    """Asking for 1,000 colours in 500 texels cannot be honoured, and
    silently returning 500 makes a scaled run report the wrong dial."""
    with pytest.raises(ValueError):
        Q.colour_chart(colours=1000, texels=500)


# ---------------------------------------------------------------------------
# Phase (b): the quantiser. Its bar is a baseline anyone can compute without
# looking at the image -- a uniform subdivision of the RGB cube, centroid
# each. NOT an absolute error figure: that would encode today's algorithm as
# the specification.
# ---------------------------------------------------------------------------

def test_the_naive_baseline_is_a_uniform_subdivision_snapped_to_the_lattice():
    """4x2x2 is sixteen cells. The centroids are the cell midpoints in byte
    space, and every one of them must be a colour a CLUT entry can hold --
    a baseline that cheats the format is not a baseline."""
    pal = Q.naive_palette((4, 2, 2))
    assert len(pal) == 16 and len(set(pal)) == 16
    assert all(Q.on_lattice(c) for c in pal)
    assert {c[0] for c in pal} == {Q.snap((v, 0, 0))[0]
                                   for v in (32, 96, 160, 224)}
    assert {c[1] for c in pal} == {Q.snap((0, v, 0))[1] for v in (64, 192)}
    assert {c[2] for c in pal} == {Q.snap((0, 0, v))[2] for v in (64, 192)}


def test_the_baseline_takes_the_split_so_no_axis_is_privileged_by_accident():
    for split in ((4, 2, 2), (2, 4, 2), (2, 2, 4)):
        pal = Q.naive_palette(split)
        assert len(set(pal)) == 16, split
        for ch, n in enumerate(split):
            assert len({c[ch] for c in pal}) == n, (split, ch)


# ---------------------------------------------------------------------------
# The referee. Tested against hand-worked numbers before it is trusted to
# score anything.
# ---------------------------------------------------------------------------

def test_error_is_zero_when_every_colour_is_an_entry():
    assert Q.error({(0, 0, 0): 5, (255, 255, 255): 3}, ROW) == 0.0


def test_error_is_the_count_weighted_mean_of_the_nearest_squared_distance():
    """(0,0,8) is 8 from (0,0,0) -- 64 -- and (0,8,0) likewise. Weight them
    3 and 1: (3*64 + 1*64) / 4 = 64. The nearest entry, not the first."""
    assert Q.error({(0, 0, 8): 3, (0, 8, 0): 1}, [(0, 0, 0)]) == 64.0
    assert Q.error({(0, 0, 8): 3, (0, 8, 0): 1},
                   [(0, 0, 0), (0, 0, 8)]) == 16.0


def test_error_uses_the_nearest_entry_not_the_first():
    assert Q.error({(200, 200, 200): 1},
                   [(0, 0, 0), (197, 197, 197)]) == 27.0


def test_a_bigger_palette_is_never_worse():
    bag = {(r, g, b): 1 for r in (0, 90, 180, 255)
           for g in (0, 90, 180, 255) for b in (0, 90, 180, 255)}
    assert Q.error(bag, ROW[:8]) >= Q.error(bag, ROW)


def _cluster(centre, into):
    """A tight ball of on-lattice colours around `centre`. Real art is
    CLUSTERED; the colour chart is not, and the two are different tests."""
    for dr in (-8, 0, 8):
        for dg in (-8, 0, 8):
            for db in (-8, 0, 8):
                c = Q.snap((centre[0] + dr, centre[1] + dg, centre[2] + db))
                into[c] = into.get(c, 0) + 1
    return into


#: Two tight balls -- stone and foliage -- in one dark corner of the cube.
CLUSTERED = _cluster((41, 32, 24), _cluster((16, 74, 32), {}))


def _best_naive(bag):
    """The baseline at its STRONGEST: whichever axis the extra bits help
    most. Beating the weakest of the three would prove less."""
    return min(Q.error(bag, Q.naive_palette(s))
               for s in ((4, 2, 2), (2, 4, 2), (2, 2, 4)))


def test_the_quantiser_returns_a_legal_row():
    pal = Q.quantise(CLUSTERED)
    assert len(pal) == 16
    assert all(Q.on_lattice(c) for c in pal)


def test_the_quantiser_beats_the_baseline_on_clustered_art():
    """Where the win is. A uniform subdivision spends fourteen of its
    sixteen entries on regions of the cube the image never visits."""
    assert Q.error(CLUSTERED, Q.quantise(CLUSTERED)) < _best_naive(CLUSTERED)


def test_the_quantiser_is_not_worse_than_the_baseline_on_a_uniform_colour_chart():
    """The colour chart's density is uniform over colour space, which is the case
    the naive subdivision is built for -- so this arm is `<=`, and its job
    is to catch a quantiser that only ever wins by overfitting a cluster."""
    bag = {c: 1 for c in Q.colour_chart(colours=4096)}
    assert Q.error(bag, Q.quantise(bag)) <= _best_naive(bag)


def test_a_bag_that_already_fits_is_lossless():
    """Sixteen on-lattice colours need no fidelity spent at all. A
    quantiser that loses any here is broken, however well it clusters."""
    bag = {c: 1 for c in Q.colour_chart(colours=16)}
    assert Q.error(bag, Q.quantise(bag)) == 0.0


def test_a_bag_that_fits_after_snapping_reaches_the_snap_optimum():
    """Sixteen colours OFF the lattice cannot be held exactly, but nothing
    beats snapping each one -- that is the unrepresentable floor."""
    raw = [tuple(v + 3 for v in c) for c in Q.colour_chart(colours=16)]
    bag = {c: 1 for c in raw}
    floor = Q.error(bag, [Q.snap(c) for c in raw])
    assert Q.error(bag, Q.quantise(bag)) == floor > 0


def test_the_quantiser_is_deterministic():
    assert Q.quantise(CLUSTERED) == Q.quantise(CLUSTERED)


def test_the_quantiser_does_not_depend_on_the_bags_insertion_order():
    """A caller that walks the sheet in a different order must get the same
    row, or two runs of the same measurement disagree for no reason."""
    reversed_bag = {c: n for c, n in reversed(list(CLUSTERED.items()))}
    assert Q.quantise(reversed_bag) == Q.quantise(CLUSTERED)


def test_k_is_respected():
    assert len(Q.quantise(CLUSTERED, k=4)) == 4
    assert Q.error(CLUSTERED, Q.quantise(CLUSTERED, k=16)) \
        <= Q.error(CLUSTERED, Q.quantise(CLUSTERED, k=4))


def test_an_empty_bag_is_not_a_crash():
    assert Q.quantise({}) == []


# ---------------------------------------------------------------------------
# The refinement pass has to earn its place: median cut ALONE already clears
# every bar above, so without a test aimed at it the whole loop is code
# nothing can tell from its absence. (Checked by seeding `passes=0`: 47 of 47
# still passed.)
# ---------------------------------------------------------------------------

def _regressing_bag():
    """A bag whose FIRST refinement pass is WORSE than the median cut it
    starts from -- 61 of 3,000 randomly weighted bags behave this way. The
    fixture is written out so the property is pinned rather than assumed."""
    return {c: 1 + (1469 + 7 * i) % 97
            for i, c in enumerate(Q.colour_chart(colours=26, off_gamut=13))}


def test_one_refinement_pass_can_make_it_worse():
    """The premise of `quantise` keeping the best palette it SAW. If this
    goes green-by-accident the test below stops testing anything, so assert
    the regression itself and not just the outcome."""
    bag = _regressing_bag()
    start = Q.quantise(bag, k=3, passes=0)
    assert Q.error(bag, Q.refine(start, bag)) > Q.error(bag, start)


def test_quantise_keeps_the_best_palette_seen_not_the_last():
    bag = _regressing_bag()
    start = Q.quantise(bag, k=3, passes=0)
    assert Q.quantise(bag, k=3, passes=1) == start
    assert Q.error(bag, Q.quantise(bag, k=3)) <= Q.error(bag, start)


def test_refinement_is_never_worse_than_the_median_cut_alone():
    for bag in (CLUSTERED, {c: 1 for c in Q.colour_chart(colours=4096)},
                _regressing_bag()):
        assert Q.error(bag, Q.quantise(bag)) \
            <= Q.error(bag, Q.quantise(bag, passes=0))


def test_refinement_is_strictly_better_somewhere_or_it_is_dead_code():
    """Measured: 51.4 -> 50.2 on the clustered bag, 3,134.2 -> 3,086.6 over
    the full gamut. Small, and real."""
    assert Q.error(CLUSTERED, Q.quantise(CLUSTERED)) \
        < Q.error(CLUSTERED, Q.quantise(CLUSTERED, passes=0))


def test_refinement_has_converged_well_inside_the_bound():
    """`LLOYD_PASSES` is 8. If the answer still moves at 8 the bound is the
    thing choosing the palette, not the algorithm."""
    for bag in (CLUSTERED, _regressing_bag()):
        assert Q.quantise(bag, passes=Q.LLOYD_PASSES) \
            == Q.quantise(bag, passes=Q.LLOYD_PASSES * 4)


# ---------------------------------------------------------------------------
# The identity that matters most, and the one a user asked for by name:
# re-quantising art that is ALREADY legal must be a NO-OP.
#
# A sheet off the disc is a 4bpp image read under sixteen-colour **CLUT rows**.
# Every row is therefore a bag the quantiser is being asked to fit into a
# budget it already fits in, and the only correct answer is the colours it was
# given. Anything else is fidelity spent on a picture that needed none.
#
# The oracle is retail data — MAP001 a0's own sheet and its sixteen rows — so
# neither the colours nor the texel weights are invented by this file.
# ---------------------------------------------------------------------------

import png_indexed                                             # noqa: E402

FIXTURES = PKG / "tests" / "fixtures"
STUB = json.loads((FIXTURES / "MAP001.a0.stub.json").read_text()) \
    if (FIXTURES / "MAP001.a0.stub.json").exists() else None


def _disc_rows_and_indices():
    sheets = [st["texture_sheet"] for st in STUB["map_states"]
              if st.get("texture_sheet")]
    rows = [st["palettes"] for st in STUB["map_states"] if st.get("palettes")]
    _w, _h, idx, _p, _a = png_indexed.read_indexed_png(
        (FIXTURES / sheets[0]).read_bytes())
    def rgb(t):
        s = t.lstrip("#")
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    return [[rgb(c) for c in r["colors"]] for r in rows[0]], idx


DISC_ROWS, DISC_INDICES = _disc_rows_and_indices()


def _bag_under(row):
    """What the sheet's texels look like read under one CLUT row, weighted by
    how many texels carry each index."""
    bag = {}
    for i in DISC_INDICES:
        c = row[i & 0x0F]
        bag[c] = bag.get(c, 0) + 1
    return bag


def test_the_fixture_really_is_sixteen_rows_of_retail_data():
    assert len(DISC_ROWS) == 16 and all(len(r) == 16 for r in DISC_ROWS)
    assert len(DISC_INDICES) == 256 * 1024


def test_every_disc_row_is_on_the_lattice():
    """The precondition of the identity below. A CLUT entry comes off the disc
    as a BGR555 word, so it cannot be anywhere else -- and if one ever were,
    the no-op would fail for a reason that is not the quantiser's fault."""
    for i, row in enumerate(DISC_ROWS):
        assert all(Q.on_lattice(c) for c in row), i


@pytest.mark.parametrize("row_id", range(16))
def test_requantising_vanilla_art_is_a_no_op(row_id):
    bag = _bag_under(DISC_ROWS[row_id])
    out = Q.quantise(bag, 16)
    assert Q.error(bag, out) == 0.0
    # Zero error is not quite the whole claim: every colour the art used has
    # to still BE there, or a colour with few texels could vanish cheaply.
    assert set(bag) <= set(tuple(c) for c in out)


def test_the_no_op_is_not_vacuous():
    """If the rows were all one colour, or the sheet used one index, the test
    above would pass on a quantiser that returns anything at all."""
    used = {i & 0x0F for i in DISC_INDICES}
    assert len(used) >= 8, used
    assert max(len(set(r)) for r in DISC_ROWS) >= 8


def test_a_row_that_does_NOT_already_fit_is_not_a_no_op():
    """The other arm. Sixteen rows' worth of colour forced through one row is
    ADR-0007's rung 5, and it must cost something -- otherwise "no-op" is
    just what this quantiser always does."""
    bag = {}
    for row in DISC_ROWS:
        for c, n in _bag_under(row).items():
            bag[c] = bag.get(c, 0) + n
    assert len(bag) > 16
    assert Q.error(bag, Q.quantise(bag, 16)) > 0.0
