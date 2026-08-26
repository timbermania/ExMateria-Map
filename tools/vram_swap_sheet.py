"""Swap a map's texture sheet inside a pcsx-redux savestate — mid-battle.

The disc path (`build` -> `fft-iso-patcher` -> ISO -> boot -> reach the map)
is the shipping path, and it is slow to iterate: every look at a repaint costs
a patch, a boot and a walk back into the battle. This is the fast path. A
savestate embeds the whole VRAM image, and a map's texture sheet is uploaded
to VRAM **once at map load** and not re-uploaded per frame -- so replacing
those bytes and reloading the state swaps the map's art *in a running battle*,
with no ISO involved at all.

    python3 tools/vram_swap_sheet.py <in.sstate> <from.sheet> <to.sheet> <out.sstate>

`from.sheet` is the 131,072-byte blob that is *currently* in VRAM (the disc's
`MAP0nn.x`, or the authored blob if the state came from a patched ISO); it is
what locates the rectangle. `to.sheet` is what you want to see instead.

**How the sheet sits in VRAM.** A page is 256x256 at 4bpp = 128 bytes per row.
VRAM is 1024 px of 16 bits, so its row pitch is 2,048 bytes, and the four pages
sit side by side 128 bytes apart. So the sheet is not contiguous: it is four
256-row rectangles, each row `PITCH` apart. Measured on a real Gariland battle
state, 40 of 40 probe rows agreed on the origin for every page.

**The origin is derived, never assumed.** Where VRAM lands inside a savestate
depends on the serialisation, and where the sheet lands inside VRAM depends on
the game. Both are found by searching for the `from` sheet's own rows, and the
result is *self-checked*: writing `from` back must change **zero** bytes. If it
changes any, the origin is wrong and the tool refuses rather than corrupting a
megabyte of VRAM.

What this is NOT: it does not touch the disc, so it proves nothing about the
delivery path (`docs/map-to-disc-gate.md` is what does that). It is an
authoring loupe -- see the repaint now, ship it later.
"""

import sys
import zlib
from pathlib import Path

SHEET_BYTES = 131072
ROW = 128                 # 256 px at 4bpp
PITCH = 2048              # VRAM row: 1024 px x 16 bit
PAGE_STRIDE = 128         # the four pages sit side by side
PAGES, ROWS = 4, 256
MIN_AGREE = 8             # probe rows that must agree on a page's origin


class SwapError(RuntimeError):
    """Refuse rather than write a megabyte of VRAM at a guessed offset."""


def _distinctive(sheet, page, limit=40):
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


def locate(dec: bytes, sheet: bytes, hint: int | None = None,
           window: int = 1 << 16) -> int:
    """The byte offset of page 0, row 0, derived from the sheet's own rows.

    `hint` is a previous origin. **It is a search window, never an answer.**
    The offset MOVES between saves of the same session -- measured 17378311 vs
    17378314 on two states seconds apart -- because the protobuf fields ahead
    of VRAM are variable-length. Caching the number and trusting it writes 128
    bytes per row at a 3-byte skew, which is a corrupt texture, not an error.
    So a hint only narrows the scan (a 19 MB search is ~4 s; a 64 KB one is
    free), and the origin is re-derived and re-checked every time.
    """
    if hint is not None:
        lo = max(0, hint - window)
        found = _scan(dec[lo:lo + 2 * window + SHEET_BYTES], sheet)
        if found is not None:
            return found + lo
    found = _scan(dec, sheet)
    if found is None:
        raise SwapError(
            "could not locate the sheet in this savestate -- is `from.sheet` "
            "really what is in VRAM? A state captured on a PATCHED ISO holds "
            "the authored blob, not the disc's."
        )
    return found


def relocate(dec, sheet: bytes, last: int, window: int = 128) -> int:
    """Re-find a sheet that has NO high-entropy rows, near where it last was.

    `locate` needs distinctive rows, and an authored sheet may have none at all
    -- a flat checkerboard is two byte values, so every row of it matches
    everywhere and the content scan finds nothing. But the origin only drifts a
    few bytes between saves (measured 311 / 312 / 314 / 318 / 325), so the
    answer is nearby, and a full 131,072-byte match across four 256-row
    rectangles at a 2,048-byte stride is a much stronger key than any single
    row.

    Ambiguity is refused, not resolved: a periodic sheet CAN match at more than
    one offset in the window, and picking one would be a coin flip that
    corrupts a texture when it loses.
    """
    hits = [o for o in range(max(0, last - window), last + window + 1)
            if matches(dec, o, sheet)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise SwapError(f"the sheet is not within {window} bytes of {last}")
    raise SwapError(f"the sheet matches at {len(hits)} offsets near {last} "
                    f"({hits[:4]}) -- too periodic to place; refusing")


def _scan(dec: bytes, sheet: bytes) -> int | None:
    origins = {}
    for page in range(PAGES):
        votes = {}
        for r, row in _distinctive(sheet, page):
            j = dec.find(row)
            if j >= 0 and dec.find(row, j + 1) < 0:      # unique hit only
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
        raise SwapError(f"the pages disagree on the origin: {origins}")
    return next(iter(origins.values()))


def _rects(origin: int):
    """The (savestate offset, sheet offset) pairs of every row, all 4 pages."""
    for page in range(PAGES):
        base = origin + page * PAGE_STRIDE
        for r in range(ROWS):
            yield base + r * PITCH, (page * ROWS + r) * ROW


def diff(dec, origin: int, sheet: bytes) -> int:
    """Bytes that differ between VRAM and `sheet`. **Non-destructive.**

    This exists because `write` was used as a probe once ("does this sheet
    round-trip to zero?") and `write` mutates: the first candidate overwrote
    the buffer before the second was tested, so *every* candidate came back
    non-zero and a sheet plainly on screen read as "not in VRAM". Ask this,
    never `write`, when the question is which sheet is live.
    """
    return sum(1 for dst, src in _rects(origin)
               for a, b in zip(dec[dst:dst + ROW], sheet[src:src + ROW]) if a != b)


def matches(dec, origin: int, sheet: bytes) -> bool:
    """`diff(...) == 0`, but stopping at the first differing row.

    `relocate` asks this a thousand times per push and almost every answer is
    "no" on the first row, so counting all 131,072 bytes before saying so cost
    ~2.5 s a push. Same question, asked cheaply."""
    if origin < 0 or origin + PAGES * PAGE_STRIDE + ROWS * PITCH > len(dec):
        return False
    for dst, src in _rects(origin):
        if dec[dst:dst + ROW] != sheet[src:src + ROW]:
            return False
    return True


def identify(dec, origin: int, candidates: dict) -> str | None:
    """Which of `{name: sheet}` is the one in VRAM, or None."""
    return next((n for n, blob in candidates.items() if diff(dec, origin, blob) == 0),
                None)


def write(dec: bytearray, origin: int, sheet: bytes) -> int:
    changed = diff(dec, origin, sheet)
    for dst, src in _rects(origin):
        dec[dst:dst + ROW] = sheet[src:src + ROW]
    return changed


def swap(state_in: Path, from_sheet: Path, to_sheet: Path, state_out: Path) -> int:
    src = Path(from_sheet).read_bytes()
    dst = Path(to_sheet).read_bytes()
    for name, blob in (("from", src), ("to", dst)):
        if len(blob) != SHEET_BYTES:
            raise SwapError(f"{name}.sheet is {len(blob)} bytes, not {SHEET_BYTES}")

    dec = bytearray(zlib.decompressobj(47).decompress(Path(state_in).read_bytes()))
    origin = locate(dec, src)

    # The self-check: writing what is already there must be a no-op. This is
    # what turns "I found a plausible offset" into "I found the offset", and it
    # covers every page and every row, not the handful used to locate it.
    noop = diff(dec, origin, src)
    if noop:
        raise SwapError(
            f"origin {origin} rewrote {noop} byte(s) with the sheet already "
            f"there -- the address is wrong; refusing to write"
        )

    changed = write(dec, origin, dst)
    c = zlib.compressobj(6, zlib.DEFLATED, 31)      # 31 = gzip; 47 is READ-only
    Path(state_out).write_bytes(c.compress(bytes(dec)) + c.flush())
    print(f"sheet at savestate offset {origin} (page stride {PAGE_STRIDE}, "
          f"row pitch {PITCH})")
    print(f"self-check: rewriting the existing sheet changed 0 bytes")
    print(f"{changed:,} VRAM byte(s) changed -> {state_out}")
    if changed == 0:
        print("NOTE: the two sheets are identical; nothing will look different")
    return changed


if __name__ == "__main__":
    if len(sys.argv) != 5:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print("usage: vram_swap_sheet.py <in.sstate> <from.sheet> <to.sheet> "
              "<out.sstate>", file=sys.stderr)
        raise SystemExit(2)
    try:
        swap(*(Path(a) for a in sys.argv[1:]))
    except SwapError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        raise SystemExit(1)
