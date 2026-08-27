"""The VRAM leg's core: aim a texture sheet and its CLUT rows at real VRAM.

`live_vram.py` is `live_link.py`'s sibling — same job, different memory. The
RAM leg pokes `PCSX.getMemPtr()`; this one POSTs rectangles to the fork's
`/api/v1/gpu/vram/raw`, which **does** write (measured A/B/A on a live Gariland
battle, 2026-08-26; the docstrings that said it 400s were wrong).

What is asserted here is arithmetic and refusal, never the transport: VRAM is a
byte buffer, so a synthetic one exercises every branch of locate, plan and
verify. The emulator-gated proof is the screenshot A/B/A, and it lives
elsewhere.

The addresses below are **literals on purpose**. They were measured live, not
derived the way the module derives them, so a test that recomputed them could
not disagree with the code:

    sheet   page p at (768 + p*64, 0), 64 x 256   [byte offset 1536, and
                                                   live TPAGE - doc page = 12]
    CLUT    row id at (id*16, 480)                [live CLUT - doc id = 0x7800]
"""

import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "addons" / "exmateria_map")):
    if p not in sys.path:
        sys.path.insert(0, p)

import live_vram as VR                      # noqa: E402

#: The addresses measured on the live Gariland battle, as `derive_addresses`
#: reports them. Passing these explicitly is the point: the plan takes the
#: address it was GIVEN, and these tests are what pin that address to what the
#: emulator was actually seen doing.
MEASURED = VR.Derived(sheet_x=768, sheet_y=0, clut_x=0, clut_y=480, witnesses=385)


def _bytes(seed: str, n: int) -> bytes:
    """Deterministic high-entropy filler; `_distinctive` skips flat rows."""
    out = bytearray()
    block = hashlib.sha256(seed.encode()).digest()
    while len(out) < n:
        out += block
        block = hashlib.sha256(block).digest()
    return bytes(out[:n])


# --- the sheet's four rectangles --------------------------------------------

def test_the_sheet_is_four_page_rectangles_at_the_measured_vram_address():
    """A texture page is 256x256 at 4bpp = 64 VRAM words wide, and the four
    sit side by side from x=768. Measured live: the sheet's page 0 is at byte
    offset 1536, which is (768, 0), and `live_tpage_low4 - doc_texture_page`
    is 12 on 385 of 385 polygons -- 12*64 = 768."""
    sheet = _bytes("sheet", VR.SHEET_BYTES)
    rects = VR.plan_sheet(sheet, MEASURED)
    assert [(r.x, r.y, r.width, r.height) for r in rects] == [
        (768, 0, 64, 256), (832, 0, 64, 256),
        (896, 0, 64, 256), (960, 0, 64, 256)]


def _blank_vram() -> bytearray:
    """1 MB, the size of the fork's GET. VRAM is a byte buffer and nothing
    more, which is why every branch here is exercisable without an emulator."""
    return bytearray(512 * VR.PITCH)


def _paint(vram: bytearray, rects) -> None:
    """Apply rectangles the way the endpoint does: row-major inside the rect,
    each row `width` words at `(x, y + r)`."""
    for rc in rects:
        for r in range(rc.height):
            o = (rc.y + r) * VR.PITCH + rc.x * 2
            vram[o:o + rc.width * 2] = rc.data[r * rc.width * 2:
                                               (r + 1) * rc.width * 2]


class FakeVram:
    """VRAM as a variable, and a record of what was POSTed to it.

    The endpoint's own guards (400 on a bad rectangle or a body of the wrong
    length) were measured live and are the fork's, not this module's -- what is
    asserted here is that the module does not ASK for a write it does not need,
    which is decision 6's "already live" and is invisible to a byte count.
    """

    def __init__(self, vram=None):
        self.vram = vram if vram is not None else bytearray(512 * VR.PITCH)
        self.posted = []

    def read(self):
        return bytes(self.vram)

    def write_rect(self, rc):
        self.posted.append((rc.x, rc.y, rc.width, rc.height))
        _paint(self.vram, [rc])


def test_the_planned_rectangles_land_where_the_sheet_is_located():
    """The plan and the locate are two independent descriptions of one layout
    -- the plan walks four PAGES as rectangles, `_rects` walks 1,024 ROWS at a
    2,048-byte pitch -- so agreeing is evidence rather than arithmetic restated.
    This is the assertion that a page written in the wrong order fails."""
    sheet = _bytes("sheet", VR.SHEET_BYTES)
    vram = _blank_vram()
    _paint(vram, VR.plan_sheet(sheet, MEASURED))
    assert VR.locate(vram, sheet) == 1536          # measured live: (768, 0)
    assert VR.diff(vram, 1536, sheet) == 0


def test_identify_names_which_sheet_is_in_vram_and_none_when_it_is_a_stranger():
    """Decision 5's second half, for the CORPUS-BACKED callers.

    Deriving an address and then assuming it holds the sheet you derived it
    from is how a rig writes a neighbouring state's art at a confident, wrong
    offset -- locating and identifying are two questions, and the answers
    differ the moment anything has been pushed.

    **The addon is not one of those callers, and cannot be.** It cannot read
    the disc (ADR-0004 §7), so it has no candidate set: on a first press VRAM
    holds the disc's blob, which the addon has never seen, and a refusal on
    "unrecognised" would refuse every first press -- the exact stuck state
    decision 5 exists to remove. So this is a probe and CLI instrument, and the
    button's guard against a wrong map is the descriptor/count gate that runs
    before any of this.
    """
    mine = _bytes("mine", VR.SHEET_BYTES)
    other = _bytes("other", VR.SHEET_BYTES)
    vram = _blank_vram()
    _paint(vram, VR.plan_sheet(mine, MEASURED))
    origin = VR.locate(vram, mine)
    assert VR.identify(vram, origin, {"other": other, "mine": mine}) == "mine"
    assert VR.identify(vram, origin, {"other": other}) is None


def test_matches_and_diff_agree_about_a_sheet_that_is_there():
    """`matches` short-circuits and `diff` counts; a push asks the cheap one
    thousands of times, so they must not be able to disagree."""
    sheet = _bytes("sheet", VR.SHEET_BYTES)
    vram = _blank_vram()
    _paint(vram, VR.plan_sheet(sheet, MEASURED))
    assert VR.matches(vram, 1536, sheet)
    assert VR.diff(vram, 1536, sheet) == 0
    assert not VR.matches(vram, 1536, bytes(b ^ 0xFF for b in sheet))


# --- the CLUT rows are READ, not written ------------------------------------

def _clut(n=16):
    """16 CLUTs x 16 BGR555 words, the shape `mapfile.read_palettes` returns."""
    return [[(row * 16 + col) | 0x8000 for col in range(16)] for row in range(n)]


def test_the_clut_block_is_read_from_the_derived_address():
    """The palettes are pushed to main RAM (`live_link.CLUT_BLOCK`), because
    the engine re-uploads this block every frame and a VRAM write here is gone
    in 50 ms. What VRAM still provides is the WITNESS: these are the rows the
    GPU is really showing, and comparing them to the RAM block is the only way
    to tell the live block from the inert second copy of it."""
    vram = _blank_vram()
    for row in range(16):
        o = 480 * VR.PITCH + row * 16 * 2
        vram[o:o + 32] = bytes([row]) * 32
    got = VR.clut_block(vram, MEASURED)
    assert len(got) == 512
    assert got[:32] == b"\x00" * 32 and got[13 * 32:14 * 32] == b"\x0d" * 32


def test_this_module_offers_no_way_to_write_a_palette():
    """A guard on the correction itself. `plan_palettes` used to live here, and
    a caller that still finds it would push into a sink that reverts before the
    next frame -- which reads as "the palettes leg does not work" rather than
    as "it is aimed at the wrong memory"."""
    assert not hasattr(VR, "plan_palettes")


# --- the readback (decisions 3 and 8) ---------------------------------------

class RewritingVram(FakeVram):
    """VRAM with something else writing to it -- the game reloading the map
    over a push, which uploads the disc's bytes back across the sheet."""

    def __init__(self, clobber_page=2):
        super().__init__()
        self.clobber_page = clobber_page

    def read(self):
        o = (MEASURED.sheet_x + self.clobber_page * VR.PAGE_WIDTH) * 2
        self.vram[o] = (self.vram[o] + 1) & 0xFF
        return bytes(self.vram)


def test_a_page_that_did_not_hold_is_named_by_the_readback():
    """Decision 8's shipped in-band readback. A VRAM POST answers 200 for a
    rectangle it accepted, which is not the same claim as "the pixels are still
    there when the next frame draws" -- a map reload uploads the disc's bytes
    back over all of it, and the artist has to be told which page went."""
    client = RewritingVram(clobber_page=2)
    rects = VR.plan_sheet(_bytes("sheet", VR.SHEET_BYTES), MEASURED)
    VR.apply(client, rects)
    assert [r.label for r, _n in VR.verify(client, rects)] == ["texture page 2"]


def test_a_readback_with_nothing_moving_names_nothing():
    """The other arm. Without it, a verify that named every page would pass the
    test above just as well."""
    client = FakeVram()
    rects = VR.plan_sheet(_bytes("sheet", VR.SHEET_BYTES), MEASURED)
    VR.apply(client, rects)
    assert VR.verify(client, rects) == []


def test_the_sheet_pages_are_named_by_page():
    rects = VR.plan_sheet(_bytes("sheet", VR.SHEET_BYTES), MEASURED)
    assert [r.label for r in rects] == [f"texture page {p}" for p in range(4)]


# --- the transport ----------------------------------------------------------

def test_a_rectangle_already_holding_its_bytes_is_not_written_again():
    """Decision 6: skip the write when the bytes already match, and report it
    as "already live". A push that re-POSTs an identical megabyte costs the
    artist a wait and tells them nothing, and the byte count cannot tell the
    two apart -- both report zero changed."""
    sheet = _bytes("sheet", VR.SHEET_BYTES)
    client = FakeVram()
    # VRAM starts blank, so the bytes that CHANGE are exactly the non-zero
    # ones -- an oracle the module's own arithmetic plays no part in. (538 of
    # this fixture's bytes are zero, which is why "all of them" is wrong.)
    assert VR.apply(client, VR.plan_sheet(sheet, MEASURED)) == sum(1 for b in sheet if b)
    assert len(client.posted) == 4

    client.posted.clear()
    assert VR.apply(client, VR.plan_sheet(sheet, MEASURED)) == 0
    assert client.posted == []


def test_only_the_pages_that_differ_are_posted():
    """Four pages, one repainted: the other three are already live and the
    push should say so by not sending them."""
    sheet = bytearray(_bytes("sheet", VR.SHEET_BYTES))
    client = FakeVram()
    VR.apply(client, VR.plan_sheet(bytes(sheet), MEASURED))
    sheet[2 * 32768] ^= 0xFF                      # one byte, in page 2
    client.posted.clear()
    changed = VR.apply(client, VR.plan_sheet(bytes(sheet), MEASURED))
    assert changed == 1
    assert [p[0] for p in client.posted] == [896]     # page 2 only


# --- decision 5: the address is DERIVED from the live packets ---------------

def _witnesses(n=385, clut_base=0x7800, tpage_base=12):
    """`n` polygons' worth of (live CLUT, live TPAGE, doc id, doc page).

    Shaped as the real cross-check reads them: the engine's packet halfwords
    beside the document's declared fields. Measured live on MAP022 a0,
    `live_clut - palette_id` was 0x7800 and `live_tpage_low4 - texture_page`
    was 12, on 385 of 385.
    """
    out = []
    for i in range(n):
        pid, page = i % 16, i % 4
        out.append((clut_base + pid, tpage_base + page, pid, page))
    return out


def test_the_addresses_are_derived_from_the_live_packets():
    """Decision 5's first half. The sheet's column and the CLUT block's row are
    not constants this module gets to assume -- they are whatever the map
    loaded into, and the engine's own packets say so 385 times over."""
    at = VR.derive_addresses(_witnesses())
    assert (at.sheet_x, at.sheet_y) == (768, 0)
    assert (at.clut_x, at.clut_y) == (0, 480)


def test_a_clut_block_that_is_not_at_x_zero_is_derived_as_such():
    """A witness set whose CLUT base is x=0 makes the x arithmetic
    unobservable -- `(pid - pid) * anything` is zero, so a wrong unit derives
    the right answer. Measured on MAP022 a0 the base IS zero, which is exactly
    why a test built only on that map cannot see the multiplier at all."""
    at = VR.derive_addresses(_witnesses(clut_base=0x7802))
    assert (at.clut_x, at.clut_y) == (32, 480)          # 2 * 16 px
    at = VR.derive_addresses(_witnesses(clut_base=0x7805))
    assert at.clut_x == 80                              # 5 * 16 px


def test_one_witness_disagreeing_about_the_SHEET_is_a_refusal():
    """The sheet's arm of the same guard. The CLUT arm below fires on a CLUT
    dissenter and would pass just as well with the sheet's comparison deleted
    -- two fields, two refusals, and one test cannot stand for both."""
    w = _witnesses()
    w[100] = (w[100][0], (w[100][1] + 1) & 0xFFFF, w[100][2], w[100][3])
    with pytest.raises(VR.VramError) as exc:
        VR.derive_addresses(w)
    assert "texture sheet" in str(exc.value) and "polygon 100" in str(exc.value)


def test_one_disagreeing_witness_is_a_refusal_that_names_it():
    """385 witnesses agreeing is what makes the address knowledge rather than a
    guess, so a single dissenter is not a majority to be outvoted -- it means
    the packets are not describing the layout this module believes in, and
    writing a megabyte on the strength of the other 384 is how a rig corrupts
    VRAM confidently."""
    w = _witnesses()
    w[200] = (0x7800 + w[200][2] + 0x40, w[200][1], w[200][2], w[200][3])
    with pytest.raises(VR.VramError) as exc:
        VR.derive_addresses(w)
    assert "polygon 200" in str(exc.value)


def test_an_empty_witness_list_is_a_refusal_not_a_default():
    """A map with no textured polygons derives nothing, and falling back to
    768/480 would be this module asserting the measurement it was asked to
    make."""
    with pytest.raises(VR.VramError):
        VR.derive_addresses([])


def test_the_plan_follows_the_derived_address_not_the_measured_default():
    """The derivation is decorative unless the plan USES it. A map that loaded
    into a different column is exactly the case the constants cannot cover, and
    it is the case where writing at 768 corrupts a megabyte of someone else's
    VRAM."""
    # Leftward, not rightward: a TPAGE's x base is a nibble in 64-pixel units
    # and the four pages must all fit, so `base + 3 <= 15` -- x=768 is the
    # RIGHTMOST a four-page sheet can sit, and 12 is the largest legal base.
    at = VR.derive_addresses(_witnesses(tpage_base=8, clut_base=0x7C00))
    assert (at.sheet_x, at.clut_y) == (512, 496)
    sheet = _bytes("sheet", VR.SHEET_BYTES)
    assert [r.x for r in VR.plan_sheet(sheet, at)] == [512, 576, 640, 704]
    assert VR.clut_block(_blank_vram(), at) is not None    # reads at y=496 too


# --- the HTTP transport -----------------------------------------------------

def test_a_rectangle_whose_body_is_the_wrong_length_is_refused_before_the_post():
    """The endpoint answers 400 for a body that is not `width*height*2` bytes
    -- measured, one byte short and one byte long both. Catching it here is not
    redundancy: a 400 from the fork says nothing about WHICH rectangle or by
    how much, and this is the leg where a mis-sliced page is the likely fault."""
    bad = VR.Rect(768, 0, 64, 256, b"\x00" * 100, "texture page 0")
    with pytest.raises(VR.VramError) as exc:
        VR.check_rect(bad)
    assert "texture page 0" in str(exc.value) and "32,768" in str(exc.value)


def test_a_rectangle_off_the_edge_of_vram_is_refused():
    """`x + width > 1024` and `y + height > 512` are both 400s on the fork.
    VRAM is 1024x512 of 16-bit words and there is nothing past it."""
    for rc in (VR.Rect(1000, 0, 64, 1, b"\x00" * 128, "past the right edge"),
               VR.Rect(0, 500, 16, 32, b"\x00" * 1024, "past the bottom")):
        with pytest.raises(VR.VramError) as exc:
            VR.check_rect(rc)
        assert rc.label in str(exc.value)


def test_every_planned_rectangle_passes_its_own_guard():
    """The plans this module emits must never be the thing that trips the
    endpoint -- if one does, the arithmetic above it is wrong."""
    for rc in VR.plan_sheet(_bytes("sheet", VR.SHEET_BYTES), MEASURED):
        VR.check_rect(rc)


def test_the_post_url_carries_the_rectangle_in_the_query_string():
    """The whole false premise in one assertion. A bare POST to this endpoint
    IS a 400, which is what the old docstrings saw and generalised into "this
    fork cannot write VRAM"; with the rectangle in the query string it is a
    200 and the bytes land."""
    client = VR.VramClient(host="example", port=1234)
    assert client.rect_url(VR.Rect(768, 0, 64, 256, b"", "")) == (
        "http://example:1234/api/v1/gpu/vram/raw?x=768&y=0&width=64&height=256")
