"""Read an FFT PSX map from the extracted disc tree.

The bytes side of the interchange seam. ``dump`` decodes a base map through
this module and ``build`` re-reads the same base through it, so the two legs
cannot disagree about where a chunk starts or what counts as a valid one --
that agreement is the whole reason it is one module and not two readers.

Everything here is *raw*: on-disc integers, no names, no enums. Interpretation
belongs to ``document.py`` (the schema) and ``build.py`` (the writer).

Correctness notes the corpus earned (they are cheap to re-break):

* GNS byte 2 is the **arrangement byte**, values 0..5 -- not a boolean. Folding
  2..5 into one bucket corrupts 15 maps.
* GNS sector and length are u32 at record offsets 8 and 12.
* resource -> file binding is positional: sectors ascending map to the ``.N``
  ordinals ascending. ``bind`` refuses a map where the two counts disagree
  rather than binding something plausible.
* 627 of 796 mesh-class resources have ``primary_ptr == 0``. ``read_mesh``
  returning ``None`` is the normal case, not an error.
* Property byte 3 is ``0x78`` and byte 7 is ``0x00`` on 73,888/73,888 textured
  polygons -- encoder constants, so they are not read back as fields.
"""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path
from typing import NamedTuple

# --- section pointer slots (byte offsets into the 196-byte header) ----------
PRIMARY_PTR = 0x40
PALETTE_PTR = 0x44
LIGHT_PTR = 0x64
TERRAIN_PTR = 0x68
PALETTE_ANIM_PTR = 0x70
GRAYSCALE_PTR = 0x7C
VISIBLE_ANGLES_PTR = 0xB0
#: AnimatedMesh1-8 (schema §3 / sections.SLOT_NAMES), 0x90..0xAC step 4.
ANIMATED_MESH_PTRS = tuple(0x90 + i * 4 for i in range(8))

HEADER_BYTES = 196

TEXTURE_BYTES = 131072
PALETTE_CHUNK_BYTES = 512
TERRAIN_CHUNK_BYTES = 4098          # 2 size bytes + two 2,048-byte levels
#: Each level occupies a fixed 2,048 B, NOT ``size_x * size_z * 8`` packed:
#: GaneshaDx's reader steps ``2048 - width*length*8`` past the end of each one
#: (``MeshResourceData.ProcessTerrain``).  Measured over the 191 arrangements
#: carrying a valid 0x68: the padded read finds 21,763 of 23,037 level-1 slots
#: at the format default, the packed read 3,842.
TERRAIN_LEVEL_BYTES = 2048
TERRAIN_RECORD_BYTES = 8
TERRAIN_LEVELS = 2
VISIBLE_ANGLES_BYTES = 4096
VISIBLE_ANGLES_HEADER_BYTES = 896
LIGHT_RIG_BYTES = 45

GNS_RECORD_BYTES = 20
GNS_TYPE_TEXTURE = 23
GNS_TYPE_PAD = 49
GNS_MESH_TYPES = (46, 47, 48)       # Initial / Override / Alternate


def u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def i16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<h", data, offset)[0]


def u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def pointer(data: bytes, slot: int) -> int:
    """The section pointer at ``slot``, or 0 when the header is not there."""
    if len(data) < slot + 4:
        return 0
    return u32(data, slot)


def section_pointers(data: bytes) -> dict[int, int]:
    """``{slot: pointer}`` for every non-zero slot of the 49-slot header."""
    if len(data) < HEADER_BYTES:
        return {}
    return {slot: u32(data, slot)
            for slot in range(0, HEADER_BYTES, 4)
            if u32(data, slot) != 0}


# ---------------------------------------------------------------------------
# GNS
# ---------------------------------------------------------------------------

class GnsRow(NamedTuple):
    index: int
    kind: int                # raw type code: 23 / 46 / 47 / 48 / 49
    arrangement: int         # record byte 2
    night: int               # record byte 3, bit 7
    weather: int             # record byte 3, bits 4-6
    sector: int
    length: int

    @property
    def is_mesh(self) -> bool:
        return self.kind in GNS_MESH_TYPES

    @property
    def is_pad(self) -> bool:
        return self.kind == GNS_TYPE_PAD

    # PRE-DECISION-29: `arrangement` above is byte 2 for every row, but a
    # type-49 row is a TERMINATOR and byte 2's 0x01 is part of its constant
    # filler -- so all 1,533 of them report arrangement 1, manufacturing a
    # phantom arrangement 1 in the 84 single-arrangement maps. Harmless today:
    # `dump` and `build` both filter on is_pad first. ADR-0004 decision 29 says
    # a terminator has no arrangement; saying so here is execution, off #517.


def parse_gns(raw: bytes) -> list[GnsRow]:
    """The 20-byte records of a GNS, in file order.

    The filter is the one the corpus validates: byte 0 in (34, 48, 112),
    byte 4 == 1, and a known type code. Everything else is the opaque tail.
    """
    out: list[GnsRow] = []
    for index, base in enumerate(range(0, len(raw) - (GNS_RECORD_BYTES - 1),
                                       GNS_RECORD_BYTES)):
        e = raw[base:base + GNS_RECORD_BYTES]
        if e[0] not in (34, 48, 112) or e[4] != 1:
            continue
        if e[5] not in GNS_MESH_TYPES and e[5] not in (GNS_TYPE_TEXTURE, GNS_TYPE_PAD):
            continue
        out.append(GnsRow(index=index, kind=e[5], arrangement=e[2],
                          night=(e[3] >> 7) & 1, weather=(e[3] >> 4) & 7,
                          sector=u32(e, 8), length=u32(e, 12)))
    return out


class BindError(RuntimeError):
    """The GNS's sectors and the map's resource files do not correspond."""


class MapFiles(NamedTuple):
    number: int
    name: str                        # "MAP001"
    gns_path: Path
    rows: list[GnsRow]               # sector-ascending
    by_sector: dict[int, Path]

    def path(self, resource_name: str) -> Path:
        return self.gns_path.parent / resource_name

    def arrangement_rows(self, arrangement: int) -> list[GnsRow]:
        return [r for r in self.rows if r.arrangement == arrangement]


def bind(map_dir: Path, number: int) -> MapFiles:
    """Bind ``MAP<number>.GNS``'s sectors to its resource files, positionally.

    Sectors ascending correspond to the ``.N`` ordinals ascending. A count
    mismatch raises rather than binding something plausible: a wrong binding
    reads as a wrong map, not as an error.
    """
    name = f"MAP{number:03d}"
    gns_path = Path(map_dir) / f"{name}.GNS"
    rows = sorted(parse_gns(gns_path.read_bytes()), key=lambda r: r.sector)
    sectors = list(dict.fromkeys(r.sector for r in rows))
    files = sorted((f for f in Path(map_dir).glob(f"{name}.*")
                    if f.suffix.upper() != ".GNS"),
                   key=lambda f: int(f.suffix[1:]))
    if len(sectors) != len(files):
        raise BindError(
            f"{name}: GNS names {len(sectors)} distinct sectors but the disc "
            f"tree holds {len(files)} resource files; the positional binding "
            f"is unsafe"
        )
    return MapFiles(number=number, name=name, gns_path=gns_path, rows=rows,
                    by_sector={s: files[i] for i, s in enumerate(sectors)})


def map_numbers(map_dir: Path) -> list[int]:
    return sorted(int(p.name[3:6]) for p in Path(map_dir).glob("MAP*.GNS"))


class AddressError(RuntimeError):
    """The picked path does not address a map. Never swallow into a default."""


class MapAddress(NamedTuple):
    map_dir: Path                                # the extracted disc tree
    number: int                                  # `MAP###`'s ###


def address(gns_path) -> MapAddress:
    """The ``(map_dir, number)`` a ``MAP###.GNS`` path addresses.

    ADR-0004 decision 31: the picked path is the *entire* address, so a caller
    that has one asks the artist for neither the disc tree nor the number. The
    parsing is `map_numbers`' -- `name[3:6]` -- spelled once more here because
    a path is what a file browser hands back and a directory is what `bind`
    takes.

    An addition, not a replacement: `bind`/`dump`/`build` keep their
    ``(map_dir, number)`` signatures, which 28 call sites and both CLIs depend
    on. Spend this at those signatures.

    Raises `AddressError` rather than returning something plausible. A wrong
    address opens a different map, which reads as a corrupt map rather than as
    a bad pick -- and the near miss is easy: `MAP022.8` sits beside
    `MAP022.GNS` in the same browser.
    """
    path = Path(gns_path)
    name = path.name
    if path.suffix.upper() != ".GNS":
        raise AddressError(
            f"{name}: not a GNS; File > Import takes the MAP###.GNS beside "
            f"the numbered resource files, not a resource file")
    stem = path.stem
    if not (len(stem) == 6 and stem[:3].upper() == "MAP" and stem[3:6].isdigit()):
        raise AddressError(
            f"{name}: a map GNS is named MAP###.GNS; this one carries no "
            f"map number")
    if not path.is_file():
        raise AddressError(f"{name}: no such file at {path.parent}")
    return MapAddress(path.parent, int(stem[3:6]))


# ---------------------------------------------------------------------------
# primary mesh (0x40)
# ---------------------------------------------------------------------------

class Mesh(NamedTuple):
    start: int                                   # the 0x40 pointer
    end: int                                     # exclusive; end of the bindings
    counts: tuple[int, int, int, int]            # tt, tq, ut, uq
    positions: dict[str, list[list[tuple[int, int, int]]]]
    normals: dict[str, list[list[tuple[int, int, int]]]]
    texture: list[dict]                          # tt then tq, disc order
    untextured: list[list[int]]                  # ut then uq, 4 raw bytes each
    bindings: list[tuple[int, int, int]]         # (x, z, level), tt then tq


def read_mesh(data: bytes) -> Mesh | None:
    """The primary-mesh section, or ``None`` when the resource carries none."""
    p = pointer(data, PRIMARY_PTR)
    if p == 0 or p + 8 > len(data):
        return None
    tt, tq, ut, uq = (u16(data, p), u16(data, p + 2),
                      u16(data, p + 4), u16(data, p + 6))
    o = p + 8

    def triples(count: int, nverts: int) -> list[list[tuple[int, int, int]]]:
        nonlocal o
        out = []
        for _ in range(count):
            out.append([(i16(data, o + k * 6), i16(data, o + k * 6 + 2),
                         i16(data, o + k * 6 + 4)) for k in range(nverts)])
            o += nverts * 6
        return out

    positions = {"tt": triples(tt, 3), "tq": triples(tq, 4),
                 "ut": triples(ut, 3), "uq": triples(uq, 4)}
    normals = {"tt": triples(tt, 3), "tq": triples(tq, 4)}

    texture = []
    for count, nverts in ((tt, 3), (tq, 4)):
        for _ in range(count):
            uv = [(data[o], data[o + 1]), (data[o + 4], data[o + 5]),
                  (data[o + 8], data[o + 9])]
            rec = {"uv": uv,
                   "palette_id": data[o + 2] & 0x0F,
                   "palette_byte_high_nibble": data[o + 2] >> 4,
                   "texture_page": data[o + 6] & 3,
                   "unknown_texture_value_6a": (data[o + 6] >> 2) & 3,
                   "texture_byte6_high_nibble": data[o + 6] >> 4}
            o += 10
            if nverts == 4:
                uv.append((data[o], data[o + 1]))
                o += 2
            texture.append(rec)

    untextured = [list(data[o + i * 4:o + i * 4 + 4]) for i in range(ut + uq)]
    o += (ut + uq) * 4

    bindings = [(data[o + i * 2 + 1], data[o + i * 2] >> 1, data[o + i * 2] & 1)
                for i in range(tt + tq)]
    o += (tt + tq) * 2

    return Mesh(start=p, end=o, counts=(tt, tq, ut, uq), positions=positions,
                normals=normals, texture=texture, untextured=untextured,
                bindings=bindings)


def animated_mesh_counts(data: bytes) -> tuple[int, int, int, int]:
    """The ``AnimatedMesh1``-``8`` sections' polygon counts, summed per bucket.

    ADR-0004 decision 28: the engine's four destination cursors
    (``DAT_800f5b64/68/6c/70``) are zeroed once by case ``0x10`` and then
    appended at by ``FUN_800f4dd4`` for the primary mesh **and** every present
    animated section, with no bound check -- so the quantity that overflows the
    arrays is the sum over all nine, not the primary mesh alone.

    Each section opens with the same 4x u16 count header the primary mesh
    carries. That reading is self-checked against the length each header
    pointer implies: **31 of 43** corpus sections span exactly the implied
    length and the other **12** run two bytes longer, none short. An absent or
    out-of-range pointer contributes nothing.

    Corpus: 15 of 169 geometry-carrying resources carry any (43 sections). The
    largest are ``MAP103.10`` at 140 textured triangles and ``MAP053.19`` at
    179 textured quads; the untextured buckets peak at 0 and 1.
    """
    total = [0, 0, 0, 0]
    for slot in ANIMATED_MESH_PTRS:
        start = pointer(data, slot)
        if start <= 0 or start + 8 > len(data):
            continue
        for i in range(4):
            total[i] += u16(data, start + i * 2)
    return (total[0], total[1], total[2], total[3])


def mesh_digest(data: bytes, mesh: Mesh) -> str:
    """Schema §4 ``geometry_digest``: sha256 over the whole 0x40 section."""
    return hashlib.sha256(data[mesh.start:mesh.end]).hexdigest()


# ---------------------------------------------------------------------------
# palettes (0x44), terrain (0x68), visible angles (0xB0), light rig (0x64)
# ---------------------------------------------------------------------------

def palette_offset(data: bytes) -> int | None:
    """The 0x44 chunk's offset when the resource carries a valid one.

    Schema §7.1's validity test: ``0 < p`` and ``p + 512 <= len``.
    """
    p = pointer(data, PALETTE_PTR)
    if p <= 0 or p + PALETTE_CHUNK_BYTES > len(data):
        return None
    return p


def read_palettes(data: bytes, offset: int | None = None) -> list[list[int]] | None:
    """16 CLUTs x 16 BGR555 words, raw."""
    p = palette_offset(data) if offset is None else offset
    if p is None:
        return None
    return [[u16(data, p + i * 32 + j * 2) for j in range(16)] for i in range(16)]


# ---------------------------------------------------------------------------
# The animation chunks: ``0x6c`` instructions and ``0x70`` palette frames.
#
# ``0x70`` was declared here for months and never read (#624). Decoding it needs
# ``0x6c`` too: the frames say what the colours become, and the instruction
# table says **which** CLUT rows they drive and how fast.
#
# Rooted in the corpus and validated on a live Gariland battle, 2026-08-27:
# ``MAP022.9``'s first three instructions read ``(208, 480, 16, 1)`` /
# ``(224, …)`` / ``(240, …)``, which is CLUT rows 13, 14, 15 -- exactly the rows
# measured animating, and no others. Their frames cycle 0 -> 1 -> 2 -> 3 at 12
# ticks each (~0.213 s measured), and frame 3 is byte-identical to frame 1, so a
# plain forward loop *reads* as a yo-yo. The engine side is one function
# (``ra = 0x80092794``) writing each entry into both palette blocks.
# ---------------------------------------------------------------------------

ANIM_INSTRUCTION_PTR = 0x6C
ANIM_INSTRUCTION_BYTES = 640
ANIM_INSTRUCTION_STRIDE = 20
ANIM_INSTRUCTION_COUNT = ANIM_INSTRUCTION_BYTES // ANIM_INSTRUCTION_STRIDE   # 32

PALETTE_ANIM_BYTES = 512
PALETTE_ANIM_FRAMES = 16
CLUT_ENTRIES = 16

#: Where the CLUT block sits in VRAM, and how wide one row is there. A palette
#: instruction is recognised by pointing at exactly one row of it. Both numbers
#: are measurements, not conventions: ``live_clut_halfword - palette_id`` is
#: ``0x7800`` on 385 of 385 polygons, and ``0x7800`` decodes to ``y = 480``.
CLUT_VRAM_Y = 480


class AnimInstruction(NamedTuple):
    """One ``0x6c`` record -- a VRAM rectangle and how to animate it.

    The table is shared: most records point into the texture pages and scroll
    or swap a region of the sheet, and a minority point at the CLUT line. They
    are told apart by **where they point**, which is why ``is_palette`` is a
    property of the rectangle rather than a type byte this decode has not found.
    """

    index: int
    x: int
    y: int
    width: int
    height: int
    x2: int
    y2: int
    frame_count: int          # byte 14
    mode: int                 # byte 15 -- NOT decoded; see the module note
    duration: int             # byte 17, in ticks
    raw: bytes

    @property
    def is_palette(self) -> bool:
        """Does this record drive a CLUT row rather than a texture region?"""
        return (self.y == CLUT_VRAM_Y and self.width == CLUT_ENTRIES
                and self.height == 1)

    @property
    def is_row_aligned(self) -> bool:
        """Does this palette record start exactly on a CLUT row boundary?

        127 of the corpus's 128 palette records do. **One does not**:
        ``MAP056.48``'s record 0 reads ``x = 85`` where every other map reads a
        multiple of 16, and 85 is ``0x55`` against ``0x50`` -- row 5, which is
        the single most common value in the table. It looks like a retail
        one-nibble typo, and it is left as it is found rather than rounded: a
        reader that silently snapped it to row 5 would make the corpus agree
        with the reader instead of the other way round, and ``build`` carries
        this chunk verbatim anyway.
        """
        return self.is_palette and self.x % CLUT_ENTRIES == 0

    @property
    def clut_row(self) -> int | None:
        """Which of the 16 CLUT rows, or ``None``.

        ``None`` for a texture record **and** for the one misaligned palette
        record -- it does not name a row, and inventing one for it would be
        this decode asserting a fact about the disc it does not have.
        """
        return self.x // CLUT_ENTRIES if self.is_row_aligned else None


def animation_instruction_offset(data: bytes) -> int | None:
    """The ``0x6c`` chunk's offset when the resource carries a valid one."""
    p = pointer(data, ANIM_INSTRUCTION_PTR)
    if p <= 0 or p + ANIM_INSTRUCTION_BYTES > len(data):
        return None
    return p


def read_animation_instructions(data: bytes) -> list[AnimInstruction] | None:
    """The 32 animation instructions, verbatim, including the empty slots.

    Empty records are **kept**. The table is indexed, an instruction's slot is
    part of its identity, and dropping the blanks would renumber everything
    after the first gap -- which is exactly the kind of quiet reindexing a
    byte-exact writer cannot survive.
    """
    p = animation_instruction_offset(data)
    if p is None:
        return None
    out = []
    for i in range(ANIM_INSTRUCTION_COUNT):
        r = data[p + i * ANIM_INSTRUCTION_STRIDE:
                 p + (i + 1) * ANIM_INSTRUCTION_STRIDE]
        out.append(AnimInstruction(
            index=i, x=u16(r, 0), y=u16(r, 2), width=u16(r, 4), height=u16(r, 6),
            x2=u16(r, 8), y2=u16(r, 10),
            frame_count=r[14], mode=r[15], duration=r[17], raw=r))
    return out


def palette_animation_offset(data: bytes) -> int | None:
    """The ``0x70`` chunk's offset when the resource carries a valid one."""
    p = pointer(data, PALETTE_ANIM_PTR)
    if p <= 0 or p + PALETTE_ANIM_BYTES > len(data):
        return None
    return p


def read_palette_animation(data: bytes) -> list[list[int]] | None:
    """The 16 animation FRAMES, each 16 BGR555 words.

    Same shape and size as the ``0x44`` palette chunk, which is what makes "a
    second palette bank" the natural first guess -- and it is wrong. Rows here
    are frames of one animation, not palettes of one state: three of MAP022.9's
    are all zero, and the live rows 13, 14 and 15 all cycle the SAME four.

    Returned raw and complete, blank frames included, for the same reason the
    instruction table keeps its empty slots: a frame's index is what an
    instruction refers to.
    """
    p = palette_animation_offset(data)
    if p is None:
        return None
    return [[u16(data, p + f * 32 + e * 2) for e in range(CLUT_ENTRIES)]
            for f in range(PALETTE_ANIM_FRAMES)]


def terrain_offset(data: bytes) -> int | None:
    """The 0x68 chunk's offset when the resource carries a **valid** one.

    Schema §7.3: pointer in range, 4,098 B present, ``SizeX, SizeZ >= 1``, and
    ``2 + 2*SizeX*SizeZ <= 4098``. Texture resources carry garbage chunks that
    fail on the size pair -- 227 of them corpus-wide -- and a garbage chunk is
    an absent chunk, never a corrupt file (§10.3).
    """
    p = pointer(data, TERRAIN_PTR)
    if p <= 0 or p + TERRAIN_CHUNK_BYTES > len(data):
        return None
    size_x, size_z = data[p], data[p + 1]
    if size_x < 1 or size_z < 1:
        return None
    if 2 + 2 * size_x * size_z > TERRAIN_CHUNK_BYTES:
        return None
    return p


def terrain_payload(data: bytes, offset: int) -> bytes:
    return data[offset:offset + TERRAIN_CHUNK_BYTES]


def visible_angles_offset(data: bytes) -> int | None:
    p = pointer(data, VISIBLE_ANGLES_PTR)
    if p <= 0 or p + VISIBLE_ANGLES_BYTES > len(data):
        return None
    return p


#: The measured identity of the 896-B `0xB0` header: **159 of 159**
#: chunk-carrying resources agree byte for byte (ADR-0004 decision 26, schema
#: §6.2). A corpus constant -- the one blob in the `build` leg that comes from
#: neither the document nor the base.
VISIBLE_ANGLES_HEADER_SHA256 = (
    "45ca29ccdb1fd2c38469be5bb07c2021e596f43cbf79228a97251f572865ec56")

#: What that header *is*, rather than 1,792 characters of hex: a 4-byte tag,
#: thirteen u32 `1`s, and 879 zero bytes. Written as its shape so the source
#: says what it knows -- decision 26 asks for the header to be derived and the
#: identity asserted, not for a blob to be pasted in. Both halves are measured
#: rather than trusted: `test_the_manufactured_header_is_the_corpus_constant`
#: re-derives the sha256 above from these bytes AND re-reads all 159 chunks off
#: the disc, so a wrong digit here fails against the corpus, not against itself.
VISIBLE_ANGLES_HEADER_TAG = bytes((0x12, 0x12, 0x34, 0x34))
VISIBLE_ANGLES_HEADER_ONES = (4, 8, 12, 16, 20, 40, 44, 48, 52, 56, 60, 64, 68)


def visible_angles_header() -> bytes:
    """The 896 bytes every shipped `0xB0` chunk opens with (decision 26)."""
    out = bytearray(VISIBLE_ANGLES_HEADER_BYTES)
    out[:len(VISIBLE_ANGLES_HEADER_TAG)] = VISIBLE_ANGLES_HEADER_TAG
    for offset in VISIBLE_ANGLES_HEADER_ONES:
        struct.pack_into("<I", out, offset, 1)
    return bytes(out)


def light_rig_offset(data: bytes, is_mesh: bool) -> int | None:
    """Where the 45-byte rig starts, or ``None`` when the resource has none.

    ``is_mesh`` is not optional and not a convenience: a rig lives at 0x64 of a
    MESH resource, a texture row has none **by kind**, and a pointer-shaped
    test is not a kind test (ADR-0004 decision 27's correction, #576). On a
    131,072-byte sheet those four bytes are PIXELS, and on four shipped states
    -- MAP062.7/.11/.15/.19 -- they read as the plausible pointer 4080 and
    invent an ambient of ``[204, 204, 199]`` out of the picture.
    """
    if not is_mesh:
        return None
    p = pointer(data, LIGHT_PTR)
    if p <= 0 or p + LIGHT_RIG_BYTES > len(data):
        return None
    return p


def read_light_rig(data: bytes, is_mesh: bool) -> dict | None:
    """The 45-byte rig at 0x64, raw (schema §7.1).

    ``colors`` is stored PLANAR on disc -- all three reds, then greens, then
    blues -- and is a GTE gain (``/8``), routinely over 255. ``directions`` is
    interleaved, unnormalised (``/4096``), in the mesh normals' object space.
    """
    p = light_rig_offset(data, is_mesh)
    if p is None:
        return None
    return {
        "colors": [[i16(data, p + c * 6 + i * 2) for c in range(3)]
                   for i in range(3)],
        "directions": [[i16(data, p + 18 + i * 6 + k * 2) for k in range(3)]
                       for i in range(3)],
        "ambient": [data[p + 36], data[p + 37], data[p + 38]],
        "gradient": list(data[p + 39:p + 45]),
    }


def pack_light_rig(rig: dict) -> bytes:
    """A rig dict -> its 45 on-disc bytes. The exact inverse of the reader.

    The layout is asymmetric and that asymmetry is the whole difficulty:
    ``colors`` is PLANAR (light ``i``'s channel ``c`` at ``c*6 + i*2`` -- all
    three reds, then greens, then blues) while ``directions`` is INTERLEAVED
    (light ``i``'s component ``k`` at ``18 + i*6 + k*2``). Both are i16 and
    both are routinely out of any 0..255 range: a colour is a GTE gain (``/8``,
    max 3,456 in the corpus) and a direction is unnormalised (``/4096``).

    ``ambient`` is ``[u8 x 3]`` at +36 and ``gradient`` ``[u8 x 6]`` at +39 --
    the gradient rides along verbatim (ADR-0004 decision 27: the solve owns 39
    bytes and carries 6).
    """
    def _triples(key, count=3):
        rows = rig.get(key)
        if not isinstance(rows, list) or len(rows) != 3:
            raise ValueError(f"light_rig.{key} holds 3 triples, not {rows!r}")
        for row in rows:
            if not isinstance(row, list) or len(row) != count:
                raise ValueError(f"light_rig.{key} entry is not {count} "
                                 f"values: {row!r}")
        return rows

    out = bytearray(LIGHT_RIG_BYTES)
    for i, row in enumerate(_triples("colors")):
        for c, value in enumerate(row):
            if not -32768 <= int(value) <= 32767:
                raise ValueError(f"light_rig.colors[{i}][{c}] = {value} is "
                                 f"not an i16")
            struct.pack_into("<h", out, c * 6 + i * 2, int(value))
    for i, row in enumerate(_triples("directions")):
        for k, value in enumerate(row):
            if not -32768 <= int(value) <= 32767:
                raise ValueError(f"light_rig.directions[{i}][{k}] = {value} "
                                 f"is not an i16")
            struct.pack_into("<h", out, 18 + i * 6 + k * 2, int(value))
    for name, start, count in (("ambient", 36, 3), ("gradient", 39, 6)):
        values = rig.get(name)
        if not isinstance(values, list) or len(values) != count:
            raise ValueError(f"light_rig.{name} holds {count} bytes, not "
                             f"{values!r}")
        for j, value in enumerate(values):
            if not 0 <= int(value) <= 255:
                raise ValueError(f"light_rig.{name}[{j}] = {value} is not a u8")
            out[start + j] = int(value)
    return bytes(out)
