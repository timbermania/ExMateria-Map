"""The spatial axis: box-average shrink and integer replicate (ADR-0186 Amdt 10).

`addons/exmateria_map/resample.py` imports `bpy` never, for the reason
`quantise.py` does not (ADR-0007 decision 4): the two grading criteria that
say whether an artist's work survives -- Amendment 10's 2 and 4 -- are exact
byte claims, and a claim that needs a window to check is a claim nobody
checks.

Every expectation here is worked by hand from decision 38's rule (one output
pixel is the arithmetic mean of its N x N block's BYTES) or is a structural
identity stated in the amendment.  Nothing on the left of an assertion is
computed the way the code under test computes it.
"""

import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
ADDON = PKG / "addons" / "exmateria_map"
sys.path.insert(0, str(ADDON))
sys.path.insert(0, str(PKG))

import resample as R                                        # noqa: E402


def test_shrink_averages_one_block_to_one_pixel():
    """A 2x2 image at n=2 is one block, so the whole picture becomes the mean.

    Three channels chosen to pin the rounding in both directions and on the
    tie: R is 10,20,30,40 -> 25 exactly; G is 0,0,0,1 -> 0.25, which must go
    DOWN; B is 255,255,255,254 -> 254.75, which must go UP.
    """
    rgb = bytes([10, 0, 255,
                 20, 0, 255,
                 30, 0, 255,
                 40, 1, 254])
    assert R.shrink(rgb, 2, 2, 2) == bytes([25, 0, 255])


def test_shrink_supports_each_output_pixel_on_its_OWN_block():
    """Four blocks, four means, in top-scanline-first order.

    A 4x4 ramp whose pixel value is `4*row + col`.  Every block's mean lands
    on a .5 tie and on a value no member holds, so an implementation that
    smeared a block into its neighbour, or transposed x against y, cannot
    reproduce the answer by accident:

        0  1 | 2  3        (0+1+4+5)/4 = 2.5 -> 3    (2+3+6+7)/4 = 4.5 -> 5
        4  5 | 6  7
        -----+-----   ->
        8  9 |10 11       (8+9+12+13)/4 = 10.5 -> 11  (...)/4 = 12.5 -> 13
       12 13 |14 15
    """
    rgb = bytes(v for v in range(16) for _ in range(3))
    out = R.shrink(rgb, 4, 4, 2)
    assert out == bytes(v for v in (3, 5, 11, 13) for _ in range(3))


def test_expand_replicates_a_pixel_into_a_FLAT_block():
    """Conversion replicates; it never smooths.

    That is not a stylistic preference -- it is what makes criterion 2 an
    exact byte claim rather than an approximate one, because the box average
    of N^2 identical bytes is that byte.  A 2x1 pair at n=2 becomes two 2x2
    blocks, and neither block holds a value interpolated towards the other's.
    """
    a, b = (1, 2, 3), (4, 5, 6)
    out = R.expand(bytes(a + b), 2, 1, 2)
    assert out == bytes(a + a + b + b) * 2


@pytest.fixture(scope="module")
def disc_painting():
    """A real 256x1024 Painting, built from disc bytes the way `convert` does.

    `MAP001.a0.sheet-b57ddf71.png` is the shipped index plane and its
    `.samples.json` sibling is the shipped CLUT, so mapping one through the
    other is the true-colour picture `convert_op` bakes -- not a buffer this
    file invented to be convenient.
    """
    import json

    import png_indexed as P

    fixtures = Path(__file__).resolve().parent / "fixtures"
    w, h, idx, _plte, _a = P.read_indexed_png(
        (fixtures / "MAP001.a0.sheet-b57ddf71.png").read_bytes())
    plte = json.loads(
        (fixtures / "MAP001.a0.sheet-b57ddf71.samples.json").read_text())["plte"]
    pal = [bytes.fromhex(c[1:]) for c in plte]
    return w, h, b"".join(pal[v] for v in idx)


@pytest.mark.parametrize("n", R.SCALES)
def test_criterion_2_converting_at_N_ships_the_bytes_1x_ships(disc_painting, n):
    """Amendment 10 grading criterion 2, on a real disc sheet.

    Conversion replicates a disc texel into an N x N block of identical
    pixels, and the shrink in front of the compile averages that block back
    to the byte it was made of.  So the compile at N = 4 is handed the SAME
    256x1024 buffer as the compile at N = 1, byte for byte, and decision 7's
    "conversion is visually lossless" holds on the spatial axis as an exact
    claim rather than an approximate one -- which is what keeps the first
    compile a no-op at every scale.

    **What this criterion cannot see**, measured by seeding each defect into
    `resample.py` and re-running: it is a check on the block GRID -- it goes
    red on a misaligned grid, on an inverted scanline order, and on a
    replicate that is off by more than about half a level per block.  It stays
    GREEN on truncation instead of round-to-nearest, and on a replicate that
    dims one pixel of each block by 1, because a nearly-flat block averages
    back to the byte it came from.  Criterion 2 is an identity over UNIFORM
    blocks, so it can say nothing about the averaging rule; that is what
    `test_shrink_averages_one_block_to_one_pixel` and
    `test_expand_replicates_a_pixel_into_a_FLAT_block` are for.  Do not read
    this one as covering them.
    """
    w, h, rgb = disc_painting
    assert R.shrink(R.expand(rgb, w, h, n), w * n, h * n, n) == rgb


@pytest.mark.parametrize("n", R.SCALES)
def test_criterion_2_survives_every_byte_value(n):
    """The same identity over all 256 values a channel can hold.

    The disc fixture's CLUT is sixteen colours, so it cannot say whether the
    rounding survives at the top of the range.  A ramp covering 0..255 can:
    `(255 * N^2 + N^2/2) // N^2` must be 255 and not 256.
    """
    rgb = bytes(v for v in range(256) for _ in range(3))
    assert R.shrink(R.expand(rgb, 16, 16, n), 16 * n, 16 * n, n) == rgb


#: An 8x8 master at n = 4: four blocks of 4x4, every pixel a different grey,
#: so no block is uniform and a block that comes back flat was WRITTEN flat.
DETAIL = bytes(v for v in range(64) for _ in range(3))


def test_criterion_4_a_native_stroke_does_not_erase_detail_it_did_not_touch():
    """Amendment 10 grading criterion 4 -- decision 35, made testable.

    Detail exists at N = 4.  The artist paints ONE pixel on the native canvas.
    Only that pixel's block may go flat; every other block must come back
    bit-identical, because an untouched native pixel is bit-identical to what
    was derived into it and is never written back.

    This is `interchange-export-v1.md` §3.4's invariant -- an unchanged pixel
    is never re-resolved -- moved from the colour axis onto the spatial one.
    It is the claim that would destroy an artist's work if it were wrong: a
    write-through that stamped every block would turn a whole 4x painting into
    a 1x one the moment the artist touched a single native texel.
    """
    was = R.shrink(DETAIL, 8, 8, 4)
    now = bytearray(was)
    now[3:6] = b"\xaa\xbb\xcc"                 # native pixel (1, 0), top right
    master, changed = R.write_through(DETAIL, 8, 8, 4, bytes(now), was)

    assert changed == 1
    for y in range(8):
        for x in range(8):
            at = 3 * (y * 8 + x)
            got, before = master[at:at + 3], DETAIL[at:at + 3]
            if y < 4 and x >= 4:               # the block under that pixel
                assert got == b"\xaa\xbb\xcc", f"({x},{y}) not stamped"
            else:
                assert got == before, f"({x},{y}) lost detail it never met"


def test_criterion_3_a_switch_with_no_painting_leaves_the_master_alone():
    """Amendment 10 grading criterion 3, at the seam it is decided in.

    High -> Native -> High with no stroke in between.  The canvas the artist
    was handed is exactly `shrink(master)`, nothing moved in it, so the diff
    is empty and the master comes back bit-identical.  "Seamless" is this, and
    it is why there is one canvas rather than two things to reconcile: there
    is nothing to commit because nothing was ever divergent.

    (The Blender half of criterion 3 -- that the switch rewires the paint
    target and re-derives -- is not gradable here.  This is its kernel.)
    """
    was = R.shrink(DETAIL, 8, 8, 4)
    master, changed = R.write_through(DETAIL, 8, 8, 4, was, was)
    assert changed == 0
    assert master == DETAIL


def test_a_stroke_of_ONE_LEVEL_is_still_a_stroke():
    """The diff is exact, not tolerant -- the negative arm of criterion 4.

    Criterion 4 only ever says what must NOT be written.  A write-through that
    called anything within a few levels "untouched" would pass it and every
    other test in this file, while silently discarding the quietest strokes an
    artist makes: a one-level nudge on a gradient is deliberate work, and the
    canvas can carry it exactly because it holds `byte / 255` in a `Non-Color`
    image.  Seeded as `abs(a - b) <= 4` and confirmed to slip past the rest.
    """
    was = R.shrink(DETAIL, 8, 8, 4)
    now = bytearray(was)
    now[3] = (now[3] + 1) & 0xFF               # one level, on one channel
    master, changed = R.write_through(DETAIL, 8, 8, 4, bytes(now), was)

    assert changed == 1
    block = {master[3 * (y * 8 + x):3 * (y * 8 + x) + 3]
             for y in range(4) for x in range(4, 8)}
    assert block == {bytes(now[3:6])}


@pytest.mark.parametrize("n", R.SCALES)
def test_scale_of_derives_N_from_the_dimensions(n):
    """Decision 43: the scale is DERIVED, never stored.

    A PNG carries its own width and height, so a `scale` key would be exactly
    the redundant, driftable copy §3 already refuses for the polygon counts.
    `256k x 1024k` is the whole rule, and it needs no field to check.
    """
    assert R.scale_of(256 * n, 1024 * n) == n


@pytest.mark.parametrize("w,h", [
    (256, 2048),        # the two axes disagree on k -- not one scale at all
    (512, 1024),        # ditto, the other way round
    (768, 3072),        # k = 3: an integer factor, but not a power of two
    (128, 512),         # k < 1: a painting smaller than the sheet it feeds
    (1024, 4097),       # off by one, the shape a truncating writer produces
    (4096, 16384),      # k = 16: past the top of decision 36's set
    (0, 0),
])
def test_scale_of_refuses_a_size_that_is_not_a_legal_painting(w, h):
    """`None` rather than a raise, because the two callers need OPPOSITE
    postures on the same fact.

    §7.3b: export refuses an illegal painting by name, since decision 4 makes
    the Painting the irreplaceable half of an authored map; import warns,
    skips, and previews that state through the CLUT, because "an import that
    lost a file must still open".  A verdict both can read keeps the rule in
    one place and leaves the posture to the caller.
    """
    assert R.scale_of(w, h) is None


# ---------------------------------------------------------------------------
# `scale_of_buffer` -- the same rule asked of a LENGTH.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n", R.SCALES)
def test_scale_of_buffer_derives_N_from_the_byte_count(n):
    """Decision 43's rule, asked by a caller that holds bytes and no size.

    `convert_op._write_art` is handed an art buffer and has to build the image
    to put it in, so it is deciding the dimensions rather than reading them.
    That is the same rule as `scale_of` and it lives in the same place: a
    second copy of `256k x 1024k` is a second place for it to go wrong, and
    the one it protects is a hardcoded `w, h = 256, 1024` that wrote a 4x
    buffer into a 1x image and kept the top strip in SILENCE.
    """
    assert R.scale_of_buffer(3 * (256 * n) * (1024 * n)) == n


@pytest.mark.parametrize("nbytes", [
    0,
    3 * 256 * 1024 - 3,          # one texel short of 1x
    3 * 256 * 1024 + 3,          # one texel over
    3 * 300 * 1024,              # the width the export refusal arm uses
    256 * 1024,                  # a count of TEXELS, not of bytes
    4 * 256 * 1024,              # RGBA, not RGB
    3 * (256 * 3) * (1024 * 3),  # k = 3: a square number of texels, illegal N
])
def test_scale_of_buffer_refuses_a_length_that_is_no_painting(nbytes):
    """A verdict, not a raise -- `scale_of`'s posture, for the same reason.

    Note `4 * 256 * 1024`: RGBA at 1x is 3 * 256 * 1024 * 4/3, which is not
    a legal RGB painting at any N.  A length check that only divided by 3
    would take it and build an image a quarter the picture.
    """
    assert R.scale_of_buffer(nbytes) is None


# --------------------------------------------------------------------------
# `snap_scale` -- what a typed number means.
#
# Exhaustive rather than sampled, because the whole ladder is four rungs and
# the interesting inputs are the ones BETWEEN them.  The expected values are
# written out by hand from decision 36's rule, not derived from `SCALES`.

@pytest.mark.parametrize("typed,meant", [
    (1, 1),
    (2, 2), (3, 4), (4, 4),          # 3 is the mis-type the UI can produce
    (5, 8), (6, 8), (7, 8), (8, 8),  # every number above 4 means 8, not 4
])
def test_snap_scale_rounds_UP_to_the_next_legal_rung(typed, meant):
    assert R.snap_scale(typed) == meant


def test_snap_scale_never_picks_the_DESTRUCTIVE_neighbour():
    """The property this rule exists for, stated without naming a rung.

    Nearest-with-ties-up would send 5 to 4, and an artist at 8 who typed it
    would lose three quarters of their pixels to a number that meant neither
    scale.  Decision 36 makes a down-conversion deliberate; an ambiguous input
    is by definition not deliberate, so no input that is not itself a rung may
    ever land below the rung above it.
    """
    for typed in range(1, 9):
        if typed in R.SCALES:
            continue
        assert R.snap_scale(typed) > typed


def test_snap_scale_clamps_rather_than_wrapping():
    """Out of range on both ends. The property is bounded, so this is the
    arithmetic answering for any other caller."""
    assert R.snap_scale(0) == 1
    assert R.snap_scale(-4) == 1
    assert R.snap_scale(9) == 8
    assert R.snap_scale(1000) == 8


def test_snap_scale_is_the_identity_on_every_legal_scale():
    """A rung must never move -- otherwise setting the value it already has
    would rescale the Painting, and `painting_scale_set`'s no-op guard is
    written against this."""
    for n in R.SCALES:
        assert R.snap_scale(n) == n
