"""The spatial axis of the Painting: shrink to native, replicate to the master.

ADR-0186 **Amendment 10** adds a scale N to the Painting.  The master is the
N-times picture; the 256x1024 buffer the compile is handed is *derived* from
it by a box average that runs in FRONT of the compile (decision 37), so
`compile_sheet` and everything under it never learn N existed.

`bpy`-free, like `charts.py`, `islands.py` and `quantise.py` and for the same
reason (ADR-0007 decision 4).  Amendment 10's grading criteria 2 and 4 are
exact byte claims about an artist's work surviving, and they are checkable
here under plain `pytest` with no window open.

It imports `numpy` BARE (ADR-0186 **Amendment 13** decision 52): this module is
addon-only, numpy is a hard dependency of Blender itself, and a retained
pure-Python fallback would be a second implementation of a byte-exact transform
that nothing would ever run.  `png_indexed.py` is the one module here that does
NOT do that, for a reason particular to it (decision 53).

The buffer everything here speaks is the one `png_indexed.read_rgb_png`
returns and `convert()` blits: three bytes a pixel, row-major from the TOP
scanline.  Not Blender's float layout, whose row 0 is the BOTTOM -- the flip
belongs to the two functions that touch `Image.pixels`, and putting it here
too would apply it twice.
"""

import numpy

#: The legal scales (Amendment 10 decision 36).  Powers of two only, because
#: decision 38's box average is exact only at an integer factor and decision
#: 43 derives N from `256k x 1024k` dimensions with nothing stored to drift.
SCALES = (1, 2, 4, 8)

#: The Sheet the game reads, and so the Painting at k = 1 (`compile_map`
#: holds the same pair; restated rather than imported, because that module is
#: the compile and this one runs in FRONT of it).
SHEET_W, SHEET_H = 256, 1024

__all__ = ["SCALES", "SHEET_W", "SHEET_H", "scale_of", "scale_of_buffer",
           "snap_scale", "shrink", "expand", "write_through"]


def snap_scale(value):
    """The legal scale a typed number means.  Rounds UP, and never refuses.

    The ladder has four rungs and the artist has a number field, so most of
    the integers they can type are not scales.  Refusing them was rejected:
    a number field that snaps back to its old value on 3 says nothing about
    why, and the artist's next guess is the same guess.

    **Up, not nearest.**  Raising replicates and is lossless; lowering box-
    averages and cannot be undone, and decision 36 rules that a down-conversion
    is a deliberate, warned act.  A number that means neither rung is not
    deliberate, so it must not be the one that destroys detail: at 8, a typed
    5 under a nearest rule would silently halve the picture.  Rounding up
    makes every ambiguous input a no-op or a lossless growth, and the way DOWN
    stays available by typing a rung exactly.

    Out of range clamps rather than wrapping -- the property is already bounded
    at 1 and 8, so this only answers for the arithmetic reaching it by any
    other route.
    """
    for n in SCALES:
        if n >= value:
            return n
    return SCALES[-1]


def scale_of(width, height):
    """The Painting's scale, read off its dimensions.  `None` if illegal.

    Amendment 10 decision 43: N is DERIVED and never stored, because a PNG
    already carries its own width and height and a `scale` key would be the
    redundant, driftable copy §3 refuses for the polygon counts.  The rule is
    `256k x 1024k` for k in `SCALES`, and both axes must agree -- a picture
    that is 256 x 2048 is not a scaled sheet at all.

    Returns a verdict rather than raising, because the two callers take
    OPPOSITE postures on it (schema §7.3b): `export_source_art` refuses an
    illegal painting by name, since decision 4 makes the Painting the
    irreplaceable half of an authored map; `_build_source_art` warns, skips,
    and lets that state preview through the CLUT, because "an import that lost
    a file must still open; it is the export that refuses".  Keeping the rule
    in one place and the posture in two is what stops the two from drifting.

    A document's paintings must also AGREE on k -- N belongs to the map, not
    to a state.  That is a claim about a set of paintings and belongs to the
    caller holding the set; this answers for one picture.
    """
    for n in SCALES:
        if width == SHEET_W * n and height == SHEET_H * n:
            return n
    return None


def scale_of_buffer(nbytes):
    """The scale N of an RGB Painting buffer `nbytes` long, or None.

    `scale_of`'s rule asked by a caller that holds bytes and no dimensions --
    `convert_op._write_art`, which is *deciding* the image size rather than
    reading it.  It lives here, beside `scale_of`, because decision 43 puts
    the `256k x 1024k` rule in one place: the copy it replaces was a hardcoded
    `w, h = 256, 1024` that took a 4x buffer, kept the top strip, and said
    nothing.
    """
    for n in SCALES:
        if nbytes == 3 * (SHEET_W * n) * (SHEET_H * n):
            return n
    return None


def shrink(rgb, width, height, n):
    """The N-times master, box-averaged down to one pixel per texel.

    Amendment 10 decision 38: one output pixel is the arithmetic mean of its
    N x N block's BYTES.  At exactly N:1 each output pixel's support IS that
    block, so there is nothing to reconstruct and nothing to choose -- the
    filter dropdown a non-integer factor would need is unreachable here, and
    is refused anyway because the compile is one thing and a setting would
    change what every stored measurement means.

    **Byte space, not linear light**, which is decision 38's named departure:
    the whole chain multiplies in PSX byte space (#427) and the reference is
    the disc's own 147 textures, judged on a CRT in gamma space.  Recorded as
    a departure rather than an oversight; the A/B is to be rendered and looked
    at before it is called settled.

    The mean is rounded to NEAREST, ties away from zero -- `(sum + N^2/2) //
    N^2`.  Truncating instead would bias every block half a level dark and
    compound the departure above, which is a second unargued change riding
    inside the first one.
    """
    if n not in SCALES:
        raise ValueError(f"scale {n!r} is not one of {SCALES}")
    if len(rgb) != 3 * width * height:
        raise ValueError(f"{len(rgb)} bytes, expected {3 * width * height}")
    if width % n or height % n:
        raise ValueError(f"{width}x{height} does not divide by {n}")
    if n == 1:
        return bytes(rgb)

    # The SAME two folds the Python loop did, in the same order, done by
    # numpy.  ADR-0186 Amendment 13: this was the compile worker's
    # second-largest leg, ~18 % of it.  Measured on a 256N x 1024N master --
    # the real shape at each scale, not one buffer relabelled -- and byte for
    # byte the same answer at each:
    #
    #     N = 2   114 ms -> 10.0 ms      N = 4   174 ms -> 14.2 ms
    #     N = 8   395 ms -> 29.4 ms
    #
    # THE ROUNDING IS INTEGER, and it has to be.  Decision 38's mean is
    # `(sum + N^2/2) // N^2` -- round-half-UP -- and `numpy.rint` implements
    # round-half-to-EVEN.  The first numpy shrink written for this amendment
    # used `rint` and came back non-identical for exactly that reason: on a
    # 64x64 random buffer at N = 2 the two disagree on 1,511 of 3,072 bytes.
    # `uint32` is wide enough by construction -- the largest block sum is
    # 255 * 8^2 = 16,320 -- and the accumulator dtype is given to `sum` rather
    # than reached by widening the whole buffer first, which is three times
    # dearer for the same arithmetic.
    n2 = n * n
    src = numpy.frombuffer(rgb, dtype=numpy.uint8)
    # Fold 1, vertical: the N source scanlines under each output row summed
    # elementwise, leaving one row of per-column channel sums.
    col = src.reshape(height // n, n, 3 * width).sum(axis=1,
                                                     dtype=numpy.uint32)
    # Fold 2, horizontal: the N columns of each block, summed.
    total = col.reshape(height // n, width // n, n, 3).sum(axis=2)
    return ((total + n2 // 2) // n2).astype(numpy.uint8).tobytes()


def expand(rgb, width, height, n):
    """One pixel per texel, replicated into flat N x N blocks.

    The inverse `shrink` is exact against, and the reason Amendment 10's
    criterion 2 is a BYTE claim: a conversion at N = 4 must ship the map a
    conversion at N = 1 ships, and it does because the box average of N^2
    identical bytes is that byte.  Interpolating here would make decision 7's
    "conversion is visually lossless" merely approximate on the spatial axis,
    and the first compile would stop being a no-op.

    Also decision 36's up-conversion, which is lossless for the same reason.
    """
    if n not in SCALES:
        raise ValueError(f"scale {n!r} is not one of {SCALES}")
    if len(rgb) != 3 * width * height:
        raise ValueError(f"{len(rgb)} bytes, expected {3 * width * height}")
    if n == 1:
        return bytes(rgb)

    out = bytearray(3 * width * height * n * n)
    stride = 3 * width * n
    for y in range(height):
        src = 3 * y * width
        row = bytearray(stride)
        for x in range(width):
            i = src + 3 * x
            row[3 * x * n:3 * (x + 1) * n] = rgb[i:i + 3] * n
        at = y * n * stride
        out[at:at + n * stride] = row * n
    return bytes(out)


def write_through(master, width, height, n, now, was):
    """Stamp the native canvas's CHANGED pixels back into the master.

    Amendment 10 decision 35.  `now` is the native canvas as it stands, `was`
    is what was last derived into it; both are `(width // n) x (height // n)`.
    Every pixel that differs -- by EXACT byte match, never a tolerance --
    becomes a flat N x N block in the master, and every pixel that does not is
    not written at all.  Returns `(master, changed)`.

    That asymmetry is the whole point, and is `interchange-export-v1.md`
    §3.4's "an unchanged pixel is never re-resolved" moved from the colour
    axis onto the spatial one.  Stamping every block instead would be correct
    on the canvas and catastrophic underneath it: one native stroke would
    flatten the entire N-times painting, which is Amendment 10's criterion 4
    and the reason it is graded.

    The comparison is exact because it can be.  The canvas holds `byte / 255`
    in a `Non-Color` image, so a read back is the byte again -- the same
    exactness `paint.py`'s gate already relies on -- and a tolerance would
    make a legitimately-painted near-identical colour indistinguishable from
    an untouched one, silently discarding the stroke.

    `was` is a parameter rather than `shrink(master)` recomputed here: this
    module must not assume WHEN the canvas was last derived.  `paint.py`'s
    `resolve()` records what a cold cache costs when the baseline is rebuilt
    from the wrong state -- every pixel reads as freshly painted -- and the
    caller is the only thing that knows which state that is.
    """
    if n not in SCALES:
        raise ValueError(f"scale {n!r} is not one of {SCALES}")
    if len(master) != 3 * width * height:
        raise ValueError(f"{len(master)} bytes, expected {3 * width * height}")
    nw, nh = width // n, height // n
    for name, buf in (("now", now), ("was", was)):
        if len(buf) != 3 * nw * nh:
            raise ValueError(
                f"{name}: {len(buf)} bytes, expected {3 * nw * nh}")
    if now == was:
        return bytes(master), 0

    out = bytearray(master)
    changed = 0
    for y in range(nh):
        src = 3 * y * nw
        for x in range(nw):
            i = src + 3 * x
            px = now[i:i + 3]
            if px == was[i:i + 3]:
                continue                       # §3.4: never re-resolved
            changed += 1
            block = px * n
            for by in range(n):
                at = 3 * ((y * n + by) * width + x * n)
                out[at:at + 3 * n] = block
    return bytes(out), changed
