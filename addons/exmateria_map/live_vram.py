"""The live link's VRAM leg: push a map's texture sheet into a running battle.

`live_link.py`'s sibling. Same act -- put what the artist authored into the
running battle -- over different memory: geometry and packets live in main RAM
and are poked through `PCSX.getMemPtr()`, while a sheet's pixels and a state's
palettes live in the GPU's 1 MB of VRAM and are not reachable that way at all.

This module imports `bpy` **never** (ADR-0005 decision 2), for the same reason
its sibling does not: the arithmetic here is testable under plain `pytest` and
the emulator-gated proof is not.

## VRAM is writable, and the rig used to believe it was not

`tools/live_push.py` and `tools/live_geometry.py` both stated that this fork
"exposes no VRAM write -- `POST /api/v1/gpu/vram/raw` is a 400", and built a
savestate round trip around it: save the moment, patch the sheet's bytes inside
the 19 MB state, load it back. The premise was **false**, and the reason it
looked true is that a *bare* POST really is a 400 -- the endpoint requires the
rectangle in the query string:

    POST /api/v1/gpu/vram/raw?x=768&y=0&width=64&height=256

Measured [LIVE] on a Gariland battle, 2026-08-26, by A/B/A: eight bytes seeded
at (1020, 511), read back exact through the GET, restored exact. Its guards
were measured too, and they are all 400s: no query string, a body one byte
short, a body one byte long, `x + width > 1024`, `y + height > 512`.

So the round trip is gone, and with it everything that existed only to serve
it: the origin that drifted between saves, the search window, the cache of what
was last pushed, the size-settling poll. What survives is the geometry below --
it was always about VRAM, and the savestate was only ever the container it was
read through.

## Where the two things sit, and how both addresses were derived

A texture page is 256x256 at 4bpp = 128 bytes per row. VRAM is 1024 pixels of
16 bits, so a page is **64 VRAM words wide**, and a map's four pages sit side
by side starting at x=768:

    page p  ->  (768 + p*64, 0), 64 x 256

Two independent derivations agree on that. The sheet's own bytes locate at byte
offset 1536 in a VRAM GET, and 1536 / 2 = 768 with y = 0. And the live packets
say the same thing without reading a pixel: `live_tpage_low4 -
doc_texture_page` is **12 on 385 of 385** textured polygons, and a TPAGE's
low nibble is the x base in 64-pixel units -- 12 * 64 = 768, with the y bit
clear.

The CLUT rows sit in the bottom row of VRAM, one 16-entry row per palette id:

    CLUT id ->  (id*16, 480), 16 x 1

Derived the same second way: `live_clut_halfword - doc_palette_id` is
**0x7800 on 385 of 385**, and a CLUT attribute packs `(y << 6) | (x >> 4)`, so
0x7800 is y = 480, x = 0, and the document's id is the x offset in 16-pixel
units.

**And this module does not write them.** That address is right and it is not a
sink: the engine re-uploads the whole CLUT block from main RAM every frame, so
a write here is reverted within 50 ms -- long enough to read back as a success,
far too short for the artist to see. Measured [LIVE] at four delays, against a
sheet write made in the same session that was still intact a full second later.
The sheet is uploaded once at map load and left alone; the palettes are not.
`live_link.plan_palettes` pushes the RAM block that feeds these rows, and what
this module contributes to that leg is the READ -- VRAM's CLUT rows are the
independent witness that the RAM block is the one on screen.

Decision 5: **locate = derive + verify.** The address comes from the packets,
where 385 witnesses agree and any disagreement is a refusal; the *identity* of
what is there comes from content (`identify`). That pair is what removes the
stuck state the old rig had, where a flat authored sheet could not be found
again because it had no distinctive row to scan for.
"""

from __future__ import annotations

from typing import NamedTuple

# --- the sheet's shape in VRAM ---------------------------------------------
SHEET_BYTES = 131072
ROW = 128                 # 256 px at 4bpp
PITCH = 2048              # VRAM row: 1024 px x 16 bit
PAGE_STRIDE = 128         # the four pages sit side by side
PAGES, ROWS = 4, 256

#: The sheet's x origin in VRAM pixels, and a page's width in them. Both are
#: measurements (`live_tpage_low4 - doc_texture_page == 12`, 385/385), not
#: choices: whatever column the map loaded into is where the bytes must go.
SHEET_X = 768
PAGE_WIDTH = 64           # 256 px at 4bpp, in 16-bit VRAM words
SHEET_Y = 0


class Rect(NamedTuple):
    """One `POST /api/v1/gpu/vram/raw` — a rectangle and the bytes for it.

    The VRAM mirror of `live_link.plan`'s `(address, bytes)`: the smallest
    thing that can be planned, verified and written, and the unit a refusal
    can name.
    """

    x: int
    y: int
    width: int
    height: int
    data: bytes
    #: What to call this rectangle in a report. Decision 3 needs the readback
    #: to NAME what did not hold, and "the rectangle at (208, 480)" is not a
    #: thing an artist can act on -- "CLUT row 13" is.
    label: str = ""


def plan_sheet(sheet: bytes, at: "Derived") -> list[Rect]:
    """One rectangle per texture page, in page order, at the DERIVED address.

    `at` is required rather than defaulted to the measured constants, and that
    is decision 5: a caller who can skip the derivation will, and a plan built
    on `SHEET_X` is a plan that writes 768 at a map that loaded somewhere else.
    The constants are what the derivation is CHECKED against, not what the
    push runs on.

    Each body is a **contiguous slice** of the packed blob and needs no
    reshaping. That is not a convenience, it is the layout: the sheet is
    page-major (`(page*256 + row) * 128`), and a 64 x 256 rectangle's body is
    256 rows of 64 words = 128 bytes each, in the same order. A single
    256-wide rectangle covering all four pages would NOT be contiguous -- it
    would interleave the four pages a row at a time -- so four POSTs are the
    cheap shape and one is the expensive one.
    """
    if len(sheet) != SHEET_BYTES:
        raise VramError(
            f"a texture sheet is {SHEET_BYTES} bytes; this one is {len(sheet)}")
    per = SHEET_BYTES // PAGES
    return [Rect(at.sheet_x + p * PAGE_WIDTH, at.sheet_y, PAGE_WIDTH, ROWS,
                 sheet[p * per:(p + 1) * per], f"texture page {p}")
            for p in range(PAGES)]


# --- locating a sheet by its own content ------------------------------------
# Ported from `tools/vram_swap_sheet.py`, which is deleted. Its subject was
# always "a byte buffer holding a sheet at an origin"; a savestate was one such
# buffer and a VRAM GET is a better one -- the same code, one indirection less.
#
# What did NOT survive the move, and why: `relocate`, the `hint` search window
# and the drift re-derivation. All three existed because the sheet's offset
# INSIDE a savestate moved between saves (measured 17378311 / 17378314 / 17378318
# / 17378325) as the variable-length protobuf fields ahead of VRAM changed
# length. VRAM has no such fields. The origin is 1536, every time, and it is
# checked by content rather than remembered.

MIN_AGREE = 8             # probe rows that must agree on a page's origin


class VramError(RuntimeError):
    """Refuse rather than write a megabyte of VRAM at a guessed offset."""


def _distinctive(sheet: bytes, page: int, limit: int = 40):
    """Rows with enough entropy to be a unique signature. A flat row (all one
    index -- the sheet has many) matches in a hundred places and would derive
    a confident, wrong origin."""
    out = []
    for r in range(ROWS):
        row = sheet[(page * ROWS + r) * ROW:(page * ROWS + r + 1) * ROW]
        if len(set(row)) >= 8:
            out.append((r, row))
        if len(out) >= limit:
            break
    return out


def _scan(vram: bytes, sheet: bytes) -> int | None:
    origins = {}
    for page in range(PAGES):
        votes = {}
        for r, row in _distinctive(sheet, page):
            j = vram.find(row)
            if j >= 0 and vram.find(row, j + 1) < 0:      # unique hit only
                votes[j - r * PITCH] = votes.get(j - r * PITCH, 0) + 1
        if not votes:
            continue                    # a page can be entirely flat; skip it
        origin, agree = max(votes.items(), key=lambda kv: kv[1])
        if agree < MIN_AGREE:
            continue
        origins[page] = origin - page * PAGE_STRIDE
    if not origins:
        return None
    if len(set(origins.values())) != 1:
        raise VramError(f"the pages disagree on the origin: {origins}")
    return next(iter(origins.values()))


def locate(vram: bytes, sheet: bytes) -> int:
    """The byte offset of page 0, row 0, derived from the sheet's own rows."""
    found = _scan(vram, sheet)
    if found is None:
        raise VramError(
            "could not locate this sheet in VRAM -- the battle is not in this "
            "map, or the game has reloaded it over a push"
        )
    return found


def _rects(origin: int):
    """The (VRAM offset, sheet offset) pairs of every row, all 4 pages."""
    for page in range(PAGES):
        base = origin + page * PAGE_STRIDE
        for r in range(ROWS):
            yield base + r * PITCH, (page * ROWS + r) * ROW


def diff(vram, origin: int, sheet: bytes) -> int:
    """Bytes that differ between VRAM and `sheet`. **Non-destructive.**

    This exists because a write was once used as a probe ("does this sheet
    round-trip to zero?"), and the first candidate overwrote the buffer before
    the second was tested, so every candidate came back non-zero and a sheet
    plainly on screen read as "not in VRAM". Ask this, never a write, when the
    question is which sheet is live.
    """
    return sum(1 for dst, src in _rects(origin)
               for a, b in zip(vram[dst:dst + ROW], sheet[src:src + ROW]) if a != b)


def matches(vram, origin: int, sheet: bytes) -> bool:
    """`diff(...) == 0`, but stopping at the first differing row."""
    if origin < 0 or origin + (PAGES - 1) * PAGE_STRIDE + ROWS * PITCH > len(vram):
        return False
    for dst, src in _rects(origin):
        if vram[dst:dst + ROW] != sheet[src:src + ROW]:
            return False
    return True


def identify(vram, origin: int, candidates: dict) -> str | None:
    """Which of `{name: sheet}` is the one in VRAM, or None.

    Decision 5's second half. Deriving an address and then ASSUMING it holds
    the sheet you derived it from is how a rig writes a neighbouring state's
    art at a confident, wrong offset -- locating and identifying are two
    questions and the answers differ the moment anything has been pushed.

    **This is for corpus-backed callers -- the probes and the CLI -- and NOT
    for the button.** The addon cannot read the disc (ADR-0004 §7), so it has
    no candidate set worth the name: on a first press VRAM holds the disc's
    blob, which the addon has never seen, and refusing an unrecognised sheet
    would refuse every first press. That is the exact stuck state decision 5
    exists to remove, so the button does not call this. Its guard against
    pushing into the wrong map is the descriptor/count gate, which runs before
    anything is planned.
    """
    return next((n for n, blob in candidates.items()
                 if diff(vram, origin, blob) == 0), None)


# --- the CLUT rows, which this module READS and does not write --------------
#: The CLUT block's y in VRAM, and a row's width in entries. Measured:
#: `live_clut_halfword - doc_palette_id == 0x7800` on 385 of 385 polygons, and
#: a CLUT attribute packs `(y << 6) | (x >> 4)`, so 0x7800 is y=480, x=0.
CLUT_Y = 480
CLUT_ENTRIES = 16
CLUT_ROWS = 16
CLUT_BLOCK_BYTES = CLUT_ROWS * CLUT_ENTRIES * 2

#: **The palettes are NOT pushed here.** They are VRAM's CLUT rows and they are
#: not VRAM's to keep: the engine re-uploads the whole block from main RAM every
#: frame, so a write here is reverted within 50 ms (measured [LIVE], four
#: delays, against a sheet write at the same moment that held for a full second).
#: `live_link.plan_palettes` writes the RAM block that feeds this one.
#:
#: What this module still does with these rows is READ them: they are the
#: independent witness that `live_link.CLUT_BLOCK` is the block actually on
#: screen, which is the one thing a RAM-only check cannot establish about
#: itself -- a second, inert copy of the same 512 bytes sits elsewhere in RAM.


def clut_block(vram, at: "Derived") -> bytes:
    """VRAM's 16 CLUT rows, as one 512-byte block in row order.

    The oracle for `live_link.check_clut_block`. Read from the DERIVED address
    rather than `CLUT_Y`, for the same reason the sheet is written to one.
    """
    out = bytearray()
    for row in range(CLUT_ROWS):
        o = at.clut_y * PITCH + (at.clut_x + row * CLUT_ENTRIES) * 2
        out += vram[o:o + CLUT_ENTRIES * 2]
    return bytes(out)


# --- reading and writing a rectangle ---------------------------------------

def rect_bytes(vram, rc: Rect) -> bytes:
    """What VRAM currently holds inside `rc` -- the rectangle's own bytes, in
    the order the endpoint's body wants them: row-major, `width` words a row."""
    out = bytearray()
    for r in range(rc.height):
        o = (rc.y + r) * PITCH + rc.x * 2
        out += vram[o:o + rc.width * 2]
    return bytes(out)


def rect_diff(vram, rc: Rect) -> int:
    """Bytes of `rc` that VRAM does not already hold. **Non-destructive.**"""
    return sum(1 for a, b in zip(rect_bytes(vram, rc), rc.data) if a != b)


def apply(client, rects: list[Rect]) -> int:
    """POST the rectangles that need it; return how many bytes **changed**.

    Mirrors `live_link.apply` deliberately, down to the return value: the two
    legs of one press report in the same currency, and the operator adds them.

    A rectangle already holding its bytes is **not posted**. That is decision
    6's "already live", and it is not only a saving: the four sheet pages are
    32 KB of body each, and re-sending an unchanged megabyte on every press is
    the difference between a loupe and a wait. The count cannot express it --
    zero changed bytes is what both a skipped write and a landed no-op report
    -- so the skip is observable through what was ASKED for, and that is what
    the caller reports.
    """
    if not rects:
        return 0
    vram = client.read()
    changed = 0
    for rc in rects:
        n = rect_diff(vram, rc)
        if not n:
            continue
        client.write_rect(rc)
        changed += n
    return changed


def verify(client, rects: list[Rect]) -> list[tuple[Rect, int]]:
    """Read VRAM back and return the rectangles that do **not** hold their
    bytes, each with how many differ. Empty means every one of them held.

    Decision 8 ships this in band rather than leaving it to a probe: a VRAM
    write has no acknowledgement worth the name -- the endpoint answers 200
    for a rectangle it accepted, which is not the same claim as "the pixels
    are still there when the next frame draws".

    And decision 3 is why it reports rather than refuses. Some CLUT rows are
    **engine-animated**: on MAP022 a0, rows 13-15 move within two seconds of
    play. A row the engine is repainting will not hold, that is not a fault in
    the push, and the artist still has to be told WHICH row -- otherwise one
    reverting swatch reads as a rig that does not work. What this must never do
    is predict the set in advance; see `ANIMATED_ROWS_MEASURED_ON_MAP022`.
    """
    if not rects:
        return []
    vram = client.read()
    return [(rc, n) for rc in rects if (n := rect_diff(vram, rc))]


# --- decision 5: derive the address, then verify the identity ---------------

class Derived(NamedTuple):
    """Where this map's sheet and CLUTs actually sit, per the engine."""

    sheet_x: int
    sheet_y: int
    clut_x: int
    clut_y: int
    witnesses: int


#: How a PSX CLUT attribute halfword packs its VRAM address, and how a TPAGE
#: packs the page base. These are hardware, not FFT: `clut = (y << 6) | (x >> 4)`
#: gives 16-pixel x granularity and 1-pixel y; a TPAGE's low nibble is x in
#: 64-pixel units and bit 4 is y in 256-pixel ones.
CLUT_X_UNIT = 16
TPAGE_X_UNIT = 64
TPAGE_Y_UNIT = 256


def derive_addresses(witnesses) -> Derived:
    """Where the sheet and the CLUT block are, per the engine's own packets.

    `witnesses` is one tuple per textured polygon:
    `(live_clut, live_tpage, doc_palette_id, doc_texture_page)` -- the halfwords
    the engine is rendering from, beside what the document declares the polygon
    uses. Subtracting one from the other gives the base each field is measured
    from, and **every polygon must give the same answer**.

    This is decision 5's first half, and the reason it is a derivation rather
    than a constant is that neither base is FFT's to promise. `FUN_800f5578`
    copies TPAGE through verbatim and does no arithmetic, so the column a map
    loads into is the loader's business; hard-coding 768 would be right on
    every map it was measured on and silently wrong on the first one it was
    not.

    **Disagreement is a refusal, not a vote.** 385 witnesses agreeing is what
    turns "a plausible address" into "the address"; if one dissents, the
    packets are not describing the layout this module believes in, and writing
    131,072 bytes on the strength of the other 384 is exactly how a rig
    corrupts VRAM with confidence.
    """
    if not witnesses:
        raise VramError(
            "no textured polygon to derive the VRAM addresses from. The sheet's "
            "column and the CLUT block's row come from the engine's packets, "
            "and a map with nothing textured carries no witness to either -- "
            "defaulting to the addresses another map was measured at would be "
            "this module asserting the measurement it was asked to make")

    sheet, clut = None, None
    for i, (live_clut, live_tpage, pid, page) in enumerate(witnesses):
        cx = ((live_clut & 0x3F) - pid) * CLUT_X_UNIT
        cy = live_clut >> 6
        sx = ((live_tpage & 0x0F) - page) * TPAGE_X_UNIT
        sy = ((live_tpage >> 4) & 1) * TPAGE_Y_UNIT
        if sheet is None:
            sheet, clut = (sx, sy), (cx, cy)
            continue
        if (sx, sy) != sheet:
            raise VramError(
                f"the live packets disagree about where the texture sheet is: "
                f"polygon 0 says {sheet} and polygon {i} says {(sx, sy)}. "
                "Refusing to write 131,072 bytes at an address the engine "
                "does not agree on")
        if (cx, cy) != clut:
            raise VramError(
                f"the live packets disagree about where the CLUT rows are: "
                f"polygon 0 says {clut} and polygon {i} says {(cx, cy)}. "
                "Refusing to write palettes at an address the engine does not "
                "agree on")
    return Derived(sheet[0], sheet[1], clut[0], clut[1], len(witnesses))


# --- the transport ----------------------------------------------------------
# The fork's GPU endpoint, and nothing more. Same shape as `live_link.LuaClient`
# and for the same reasons (ADR-0005 decision 5): stdlib only, no dependency on
# `pcsx-agent`, because an addon an artist installs cannot pip-install anything.

import urllib.error      # noqa: E402  -- kept beside the transport it serves
import urllib.request    # noqa: E402

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8080

VRAM_WIDTH = 1024         # in 16-bit words
VRAM_HEIGHT = 512
VRAM_BYTES = VRAM_WIDTH * VRAM_HEIGHT * 2


def check_rect(rc: Rect) -> None:
    """Refuse a rectangle the endpoint would 400, while it can still be named.

    Every guard here was measured on the live fork, and each one is a plain
    400 with no body worth reading. That is the reason for checking twice: a
    bare `400` tells the artist nothing about which of five rectangles was
    malformed or by how much, and on this leg the likely fault -- a mis-sliced
    page -- is exactly the one a length check identifies precisely.
    """
    need = rc.width * rc.height * 2
    if len(rc.data) != need:
        raise VramError(
            f"{rc.label or 'rectangle'}: {rc.width}x{rc.height} needs "
            f"{need:,} byte(s) and this one carries {len(rc.data):,}")
    if rc.width <= 0 or rc.height <= 0:
        raise VramError(f"{rc.label or 'rectangle'}: {rc.width}x{rc.height} is empty")
    if rc.x < 0 or rc.x + rc.width > VRAM_WIDTH:
        raise VramError(
            f"{rc.label or 'rectangle'}: x={rc.x}+{rc.width} runs past VRAM's "
            f"{VRAM_WIDTH} words")
    if rc.y < 0 or rc.y + rc.height > VRAM_HEIGHT:
        raise VramError(
            f"{rc.label or 'rectangle'}: y={rc.y}+{rc.height} runs past VRAM's "
            f"{VRAM_HEIGHT} rows")


class VramClient:
    """`GET`/`POST /api/v1/gpu/vram/raw` -- the fork's window onto GPU memory.

    The GET returns the whole 1 MB image. The POST takes a rectangle **in the
    query string** and that rectangle's pixels as the body; without the query
    string it is a 400, which is the entire reason two docstrings in this repo
    claimed the fork could not write VRAM at all.
    """

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.host, self.port = host, port
        self.base = f"http://{host}:{port}/api/v1/gpu/vram/raw"

    def rect_url(self, rc: Rect) -> str:
        return (f"{self.base}?x={rc.x}&y={rc.y}"
                f"&width={rc.width}&height={rc.height}")

    def read(self) -> bytes:
        """The whole VRAM image, 1,048,576 bytes.

        One GET for everything rather than a rectangle at a time: the image is
        a megabyte, the push needs to compare against five rectangles of it,
        and five round trips cost more than the one that answers all of them.
        """
        try:
            with urllib.request.urlopen(self.base, timeout=60.0) as r:
                got = r.read()
        except urllib.error.HTTPError as e:
            body = (e.read() or b"")[:400].decode("utf-8", "replace")
            raise VramError(f"vram GET {e.code}: {body}") from e
        except (urllib.error.URLError, OSError) as e:
            raise VramError(
                f"no emulator answering on {self.host}:{self.port} ({e}). "
                "Launch pcsx-redux with -webserver and load a battle.") from e
        if len(got) != VRAM_BYTES:
            raise VramError(
                f"the VRAM read returned {len(got):,} bytes, not {VRAM_BYTES:,} "
                "-- this is not the endpoint this module was built against")
        return got

    def write_rect(self, rc: Rect) -> None:
        check_rect(rc)
        req = urllib.request.Request(
            self.rect_url(rc), data=rc.data, method="POST",
            headers={"Content-Type": "application/octet-stream"})
        try:
            urllib.request.urlopen(req, timeout=60.0).read()
        except urllib.error.HTTPError as e:
            body = (e.read() or b"")[:400].decode("utf-8", "replace")
            raise VramError(
                f"vram POST {e.code} for {rc.label or 'a rectangle'} at "
                f"({rc.x}, {rc.y}) {rc.width}x{rc.height}: {body}") from e
        except (urllib.error.URLError, OSError) as e:
            raise VramError(
                f"no emulator answering on {self.host}:{self.port} ({e})") from e

    def ping(self) -> bool:
        try:
            with urllib.request.urlopen(self.base, timeout=2.0) as r:
                return len(r.read()) == VRAM_BYTES
        except (urllib.error.URLError, OSError):
            return False
