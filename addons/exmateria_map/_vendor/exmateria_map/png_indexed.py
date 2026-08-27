"""Standalone 8-bit PNG codecs (ADR-0004 decision 7, ADR-0186 decision 5).

The addon speaks only the interchange, never the ROM: texture sheets reach it
as sidecar PNGs written by the dump leg.  This is a minimal stdlib-only codec
for the subset the sidecars use -- non-interlaced 8 bit, all five scanline
filters.  Anything else is refused with a ValueError rather than guessed at.

TWO colour types, because a map carries two pictures:

* **the Sheet**, 8-bit INDEXED (colour type 3) -- the 4bpp resource the game
  reads, expanded one index per byte;
* **the Painting**, 8-bit TRUECOLOUR (colour type 2) -- the artist's own
  picture on the converted authoring path, which has no palette at all.

They share a directory and a `.png` suffix, so each reader refuses the other's
colour type outright.  Read one as the other and every texel is wrong in a way
nothing downstream could name.
"""
import struct
import zlib

_SIG = b"\x89PNG\r\n\x1a\n"


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _unfilter(raw, stride, height, bpp):
    """The five PNG scanline filters, undone, for any 8-bit colour type.

    `bpp` is the distance in BYTES to the `left` neighbour -- 1 for indexed,
    3 for truecolour.  Getting it wrong does not raise: it decodes to a
    plausible picture with the channels smeared, which is why it is a
    parameter rather than a constant either reader could quietly inherit.
    """
    if len(raw) != height * (stride + 1):
        raise ValueError(f"IDAT decodes to {len(raw)} bytes, "
                         f"expected {height * (stride + 1)}")
    prev = bytearray(stride)
    out = bytearray(stride * height)
    for y in range(height):
        at = y * (stride + 1)
        f = raw[at]
        row = bytearray(raw[at + 1:at + 1 + stride])
        if f > 4:
            raise ValueError(f"scanline {y}: filter byte {f}")
        if f:
            for x in range(stride):
                left = row[x - bpp] if x >= bpp else 0
                up = prev[x]
                upleft = prev[x - bpp] if x >= bpp else 0
                if f == 1:
                    row[x] = (row[x] + left) & 0xFF
                elif f == 2:
                    row[x] = (row[x] + up) & 0xFF
                elif f == 3:
                    row[x] = (row[x] + ((left + up) >> 1)) & 0xFF
                else:
                    row[x] = (row[x] + _paeth(left, up, upleft)) & 0xFF
        # Filter None is a straight copy, and skipping the per-byte loop is
        # not a micro-optimisation: the sheet sidecars are written with filter
        # 0 throughout, so this IS the whole decode of one, and the loop costs
        # 262,144 no-op iterations per sheet.
        out[y * stride:(y + 1) * stride] = row
        prev = row
    return bytes(out)


def _chunks(data):
    """Walk a PNG's chunks, yielding `(tag, body)` up to and including IEND."""
    if not data.startswith(_SIG):
        raise ValueError("not a PNG (bad signature)")
    off = 8
    while off + 8 <= len(data):
        (length,) = struct.unpack_from(">I", data, off)
        tag = data[off + 4:off + 8]
        body = data[off + 8:off + 8 + length]
        if len(body) < length:
            raise ValueError(f"truncated {tag} chunk")
        yield tag, body
        if tag == b"IEND":
            return
        off += 12 + length


def _chunk(tag, body):
    return (struct.pack(">I", len(body)) + tag + body
            + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))


def read_rgb_png(data):
    """Decode an 8-bit TRUECOLOUR (colour type 2) PNG.

    Returns `(width, height, rgb)` -- three bytes per pixel, row-major from
    the TOP scanline, which is the layout `convert()` speaks and the sidecar
    sheets already use.

    This is the **Painting**'s codec (ADR-0186 decision 5).  A painting has no
    palette -- that is the whole point of the other authoring path -- so
    `read_indexed_png`'s subset cannot carry one.  The two sidecar kinds share
    a directory and a suffix, so an indexed PNG is REFUSED here rather than
    read as something it is not: nothing downstream could name that mistake.
    """
    w = h = None
    idat = bytearray()
    for tag, body in _chunks(data):
        if tag == b"IHDR":
            w, h, depth, ctype, _c, _f, interlace = struct.unpack(">IIBBBBB",
                                                                  body)
            if depth != 8 or ctype != 2 or interlace != 0:
                raise ValueError(f"unsupported IHDR: depth={depth} "
                                 f"colour_type={ctype} interlace={interlace}; "
                                 f"expected 8-bit truecolour (2)")
        elif tag == b"IDAT":
            idat += body
    if w is None or h is None:
        raise ValueError("missing IHDR")
    return w, h, _unfilter(zlib.decompress(bytes(idat)), 3 * w, h, 3)


def write_rgb_png(rgb, width=256, height=1024):
    """Encode an 8-bit truecolour PNG -- the inverse of `read_rgb_png`.

    Every scanline is written with filter 1 (Sub).  Unlike an index plane, a
    painting is continuous tone, where filter 0 is the wrong default: it is
    what the *disc's* sheets want and what would make a painting's sidecar
    several times the size it needs to be.  The reader still honours all five,
    because a painting may arrive from any tool.
    """
    if len(rgb) != 3 * width * height:
        raise ValueError(f"{len(rgb)} bytes, expected {3 * width * height}")
    raw = bytearray()
    for y in range(height):
        row = rgb[3 * y * width:3 * (y + 1) * width]
        raw.append(1)                                    # filter 1 (Sub)
        raw += bytes((row[x] - (row[x - 3] if x >= 3 else 0)) & 0xFF
                     for x in range(len(row)))
    out = bytearray(_SIG)
    out += _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height,
                                       8, 2, 0, 0, 0))
    out += _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    out += _chunk(b"IEND", b"")
    return bytes(out)


def read_indexed_png(data):
    """Decode an 8-bit indexed PNG.

    Returns (width, height, indices -- bytes, one 0..255 value per pixel,
    row-major starting at the TOP scanline --, palette [(r, g, b), ...],
    tRNS alpha [0..255 per palette entry] or None).

    Raises ValueError on any input outside the supported subset.
    """
    w = h = None
    palette, alpha, idat = [], None, bytearray()
    for tag, body in _chunks(data):
        if tag == b"IHDR":
            w, h, depth, ctype, _comp, _flt, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8 or ctype != 3 or interlace != 0:
                raise ValueError(
                    f"unsupported IHDR: depth={depth} colour_type={ctype} interlace={interlace}")
        elif tag == b"PLTE":
            if len(body) % 3 or not body:
                raise ValueError("bad PLTE chunk")
            palette = [tuple(body[i:i + 3]) for i in range(0, len(body), 3)]
        elif tag == b"tRNS":
            alpha = list(body)
        elif tag == b"IDAT":
            idat += body
    if w is None or h is None or not palette:
        raise ValueError("missing IHDR or PLTE")
    indices = _unfilter(zlib.decompress(bytes(idat)), w, h, 1)
    return w, h, indices, palette, alpha


def write_indexed_png(indices, palette, width=256, height=1024, alpha=None):
    """Encode an 8-bit indexed PNG — the inverse of `read_indexed_png`.

    `indices` is one 0..255 byte per pixel, row-major from the TOP scanline
    (the same shape the reader hands back).  `palette` is a list of (r, g, b)
    triples; `alpha` an optional per-entry 0..255 list written as tRNS.
    Every scanline is written with filter 0 (None): the data is a palette
    index image, so the filters that help continuous tone actively hurt here,
    and filter 0 keeps the encoder trivially inverse to the decoder above.

    Returns the PNG bytes.
    """
    if len(indices) != width * height:
        raise ValueError(f"{len(indices)} indices, expected {width * height}")
    if not palette or len(palette) > 256:
        raise ValueError(f"palette of {len(palette)} entries")
    plte = bytearray()
    for r, g, b in palette:
        plte += bytes((r & 0xFF, g & 0xFF, b & 0xFF))
    raw = bytearray()
    for y in range(height):
        raw.append(0)                                  # filter 0 (None)
        raw += indices[y * width:(y + 1) * width]

    out = bytearray(_SIG)
    out += _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 3, 0, 0, 0))
    out += _chunk(b"PLTE", bytes(plte))
    if alpha is not None:
        out += _chunk(b"tRNS", bytes(bytearray(a & 0xFF for a in alpha)))
    out += _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    out += _chunk(b"IEND", b"")
    return bytes(out)


def pack_4bpp(indices):
    """256x1024 one-byte-per-pixel indices -> the disc's packed 131,072-byte
    4bpp sheet.  Low nibble first (schema v1 §1/§6.5:
    `byte(v*256+u pair) = even | odd<<4`)."""
    if len(indices) % 2:
        raise ValueError("odd pixel count")
    return bytes((indices[i] & 0xF) | ((indices[i + 1] & 0xF) << 4)
                 for i in range(0, len(indices), 2))


def unpack_4bpp(packed):
    """The inverse of `pack_4bpp`: packed 4bpp -> one index byte per pixel."""
    out = bytearray(len(packed) * 2)
    for i, b in enumerate(packed):
        out[2 * i] = b & 0xF
        out[2 * i + 1] = b >> 4
    return bytes(out)
