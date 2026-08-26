"""Standalone 8-bit indexed PNG reader (ADR-0004 decision 7).

The addon speaks only the interchange, never the ROM: texture sheets reach it
as sidecar PNGs written by the dump leg.  This is a minimal stdlib-only
decoder for the subset the sidecars use -- 8-bit indexed (colour type 3),
non-interlaced, all five scanline filters.  Anything else is refused with a
ValueError rather than guessed at.
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


def read_indexed_png(data):
    """Decode an 8-bit indexed PNG.

    Returns (width, height, indices -- bytes, one 0..255 value per pixel,
    row-major starting at the TOP scanline --, palette [(r, g, b), ...],
    tRNS alpha [0..255 per palette entry] or None).

    Raises ValueError on any input outside the supported subset.
    """
    if not data.startswith(_SIG):
        raise ValueError("not a PNG (bad signature)")
    off, w, h = 8, None, None
    palette, alpha, idat = [], None, bytearray()
    while off + 8 <= len(data):
        (length,) = struct.unpack_from(">I", data, off)
        tag = data[off + 4:off + 8]
        body = data[off + 8:off + 8 + length]
        if len(body) < length:
            raise ValueError(f"truncated {tag} chunk")
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
        elif tag == b"IEND":
            break
        off += 12 + length
    if w is None or h is None or not palette:
        raise ValueError("missing IHDR or PLTE")
    raw = zlib.decompress(bytes(idat))
    stride = w
    if len(raw) != h * (stride + 1):
        raise ValueError(f"IDAT decodes to {len(raw)} bytes, expected {h * (stride + 1)}")
    prev = bytearray(stride)
    indices = bytearray(w * h)
    for y in range(h):
        row = bytearray(raw[y * (stride + 1) + 1: y * (stride + 1) + 1 + stride])
        f = raw[y * (stride + 1)]
        if f > 4:
            raise ValueError(f"scanline {y}: filter byte {f}")
        if f == 0:
            # Filter None is a straight copy.  Skipping the per-byte loop is
            # not a micro-optimisation here: the sidecars are written with
            # filter 0 throughout, so this is the whole decode, and the loop
            # costs 262,144 no-op iterations per sheet.
            indices[y * w: y * w + w] = row
            prev = row
            continue
        for x in range(stride):
            left = row[x - 1] if x >= 1 else 0
            up = prev[x]
            upleft = prev[x - 1] if x >= 1 else 0
            if f == 1:
                row[x] = (row[x] + left) & 0xFF
            elif f == 2:
                row[x] = (row[x] + up) & 0xFF
            elif f == 3:
                row[x] = (row[x] + ((left + up) >> 1)) & 0xFF
            elif f == 4:
                row[x] = (row[x] + _paeth(left, up, upleft)) & 0xFF
        indices[y * w: y * w + w] = row
        prev = row
    return w, h, bytes(indices), palette, alpha


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

    def chunk(tag, body):
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

    out = bytearray(_SIG)
    out += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 3, 0, 0, 0))
    out += chunk(b"PLTE", bytes(plte))
    if alpha is not None:
        out += chunk(b"tRNS", bytes(bytearray(a & 0xFF for a in alpha)))
    out += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    out += chunk(b"IEND", b"")
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
