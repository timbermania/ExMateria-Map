"""Attribute a byte offset to the thing that owns it.

The round-trip instrument's whole value on failure is turning "byte 4336 differs"
into "GrayscalePalettes +40". That mapping is knowledge about the FFT map format,
not test scaffolding, so it lives in the package.

Each of the three resource classes gets its own attribution scheme:

* ``mesh``    -- 196-byte header of 49 little-endian section pointers, then the
                 sections themselves. An offset belongs to the section whose
                 pointer is the greatest one <= it.
* ``texture`` -- a raw 256x1024 4bpp blob, always exactly 131,072 bytes.
                 Offsets attribute to a pixel row/column.
* ``gns``     -- a table of 20-byte records. Offsets attribute to a record index.
"""

from __future__ import annotations

import struct
from typing import NamedTuple

HEADER_BYTES = 196
HEADER_SLOTS = HEADER_BYTES // 4

GNS_RECORD_BYTES = 20

TEXTURE_BYTES = 131072
TEXTURE_WIDTH = 256          # pixels
TEXTURE_HEIGHT = 1024        # rows
TEXTURE_ROW_BYTES = TEXTURE_WIDTH // 2   # 4bpp -> two pixels per byte

# Header slot -> section name. Taken from GaneshaDx's reader; slots absent here
# are real sections we have no name for, and are reported as ``slot0xNN`` rather
# than swept into a neighbour.
SLOT_NAMES: dict[int, str] = {
    64: "PrimaryMesh",
    68: "TexturePalettes",
    100: "Lighting",
    104: "Terrain",
    108: "TextureAnimations",
    112: "PaletteAnimations",
    124: "GrayscalePalettes",
    140: "AnimatedMeshInstructions",
    176: "PolygonRenderProperties",
    **{144 + i * 4: f"AnimatedMesh{i + 1}" for i in range(8)},
}


def slot_name(slot: int) -> str:
    return SLOT_NAMES.get(slot, f"slot0x{slot:02X}")


class Section(NamedTuple):
    name: str
    start: int
    end: int          # exclusive

    @property
    def length(self) -> int:
        return self.end - self.start


def _read_i32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<i", data, offset)[0]


def mesh_sections(data: bytes) -> list[Section]:
    """Named ``[start, end)`` spans for a mesh resource, header included.

    Sections are delimited by the *next* pointer, which is how GaneshaDx itself
    reads them -- the format stores no section lengths. Several slots may alias
    the same offset; those share one span and their names are joined.
    """
    n = len(data)
    if n < HEADER_BYTES:
        return []

    by_offset: dict[int, list[str]] = {}
    for slot in range(0, HEADER_BYTES, 4):
        ptr = _read_i32(data, slot)
        if 0 < ptr < n:
            by_offset.setdefault(ptr, []).append(slot_name(slot))

    starts = sorted(by_offset)
    out = [Section("Header:pointer-table", 0, HEADER_BYTES)]

    # Anything between the header and the first section is unclaimed slack.
    if starts and starts[0] > HEADER_BYTES:
        out.append(Section("gap:header-to-first-section", HEADER_BYTES, starts[0]))
    if not starts:
        out.append(Section("trailing/unsectioned", HEADER_BYTES, n))
        return out

    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else n
        out.append(Section("+".join(by_offset[start]), start, end))
    return out


def _attribute(sections: list[Section], offset: int) -> str:
    for sec in sections:
        if sec.start <= offset < sec.end:
            return f"{sec.name} (+{offset - sec.start})"
    return f"unattributed (+{offset})"


def owning_section(data: bytes, offset: int, kind: str) -> str:
    """Human-readable owner of ``offset`` within ``data``.

    ``data`` must be the *original* resource, never the rebuilt one -- see the
    reporting contract in ``roundtrip.py``.
    """
    if kind == "texture":
        row, within = divmod(offset, TEXTURE_ROW_BYTES)
        return f"Texture row {row}, px {within * 2}-{within * 2 + 1}"
    if kind == "gns":
        record, within = divmod(offset, GNS_RECORD_BYTES)
        return f"GNS record[{record}] (+{within})"
    if offset < HEADER_BYTES:
        return f"Header:pointer-table[slot 0x{(offset // 4) * 4:02X}] (+{offset % 4})"
    return _attribute(mesh_sections(data), offset)


def header_differs(original: bytes, rebuilt: bytes, kind: str) -> bool:
    """True when the mesh pointer table itself changed.

    When it does, every section boundary has moved and every downstream
    attribution in that file is suspect -- the report must say so first.
    """
    if kind != "mesh":
        return False
    return original[:HEADER_BYTES] != rebuilt[:HEADER_BYTES]
