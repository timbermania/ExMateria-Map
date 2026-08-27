"""Schema-v1 constants and the codecs both legs share.

``docs/interchange-schema-v1.md`` is the contract; this module is the part of
it that is executable. Nothing here reads a file or takes a policy decision --
``dump`` decodes through it, ``build`` encodes through it, and a field that
round-trips wrong here is wrong in both legs at once, which is the point.
"""

from __future__ import annotations

FORMAT = "exmateria-map/interchange"

#: What ``dump`` stamps, always. An untouched document needs nothing newer.
VERSION = 1
#: Every ``version`` this ``build`` accepts (schema §2 / §10 rule 1).
#:
#: ``version`` is the **oldest `build` that can handle this document**, not a
#: format serial -- ADR-0004 decision 27. So a newer ``build`` accepts every
#: value at or below its own, and refusing is reserved for a document that
#: needs something this writer does not have. That is the rule the authored
#: light rig needs: §7.1 has ``build`` IGNORE the derived ``light_rig``, so a v1
#: ``build`` handed an authored one would emit a map that silently dropped the
#: artist's lighting -- exactly the failure §2's refusal rule was written to
#: catch.
ACCEPTED_VERSIONS = (1, 2)
#: The version a document that declares an authored light rig must stamp, and
#: the only thing that makes an export stamp anything but ``VERSION``.
AUTHORED_RIG_VERSION = 2
#: The ``map_states`` field that carries an authored rig (schema §7.1). Its
#: PRESENCE is the declaration -- decision 22's ``terrain: null`` shape -- so an
#: untouched document carries no key at all and is byte-identical to a v1 one.
AUTHORED_RIG = "authored_light_rig"

TEXTURED_TRIANGLE = "textured_triangle"
TEXTURED_QUAD = "textured_quad"
UNTEXTURED_TRIANGLE = "untextured_triangle"
UNTEXTURED_QUAD = "untextured_quad"

#: On-disk bucket order (schema §3). The polygon list is this order, flat.
BUCKETS = (TEXTURED_TRIANGLE, TEXTURED_QUAD, UNTEXTURED_TRIANGLE, UNTEXTURED_QUAD)
TEXTURED_BUCKETS = (TEXTURED_TRIANGLE, TEXTURED_QUAD)
VERTS = {TEXTURED_TRIANGLE: 3, TEXTURED_QUAD: 4,
         UNTEXTURED_TRIANGLE: 3, UNTEXTURED_QUAD: 4}

#: The 0xB0 slot table's fixed capacities (schema §6.2). These are the SLOT
#: counts and they are right: case 0x2c walks all 1,600. They are NOT the
#: polygon ceiling -- see ENGINE_CAPACITY below.
SLOT_CAPACITY = ((TEXTURED_TRIANGLE, 512), (TEXTURED_QUAD, 768),
                 (UNTEXTURED_TRIANGLE, 64), (UNTEXTURED_QUAD, 256))
SLOT_TOTAL = sum(n for _, n in SLOT_CAPACITY)          # 1600

#: The engine's four polygon arrays in main RAM -- ADR-0004 decision 28, and
#: the bound `build` accepts against (schema §10 rule 4). Smaller than the slot
#: table on the two textured buckets: case 0x2c reads this many records and
#: then advances the source over the rest, storing nothing.
#:
#:   0x800F2A68  slti s0,0x168   stride 0x18   textured_triangle    360
#:   0x800F2BE4  slti s0,0x2C6   stride 0x20   textured_quad        710
#:   0x800F2C2C  slti s0,0x40    stride 0x18   untextured_triangle   64
#:   0x800F2C50  slti v1,0x2000  stride 0x20   untextured_quad      256
#:
#: The last two are written as a BYTE cursor rather than a count -- 0x2000/0x20
#: is exactly 256 records. Live RAM confirms 360 and 64 from the array spacing
#: (0x8011C498-0x8011A2D8 = 360x24 B; 0x80122604-0x80122004 = 64x24 B) and
#: brackets textured_quad at <=731, so 710 is the conservative reading.
ENGINE_CAPACITY = ((TEXTURED_TRIANGLE, 360), (TEXTURED_QUAD, 710),
                   (UNTEXTURED_TRIANGLE, 64), (UNTEXTURED_QUAD, 256))

#: The largest the shipped disc goes, per bucket, SUMMED over a resource's
#: primary mesh and its AnimatedMesh1-8 (decision 28: the loader's destination
#: cursors are shared across all nine and never bound-checked). Above this and
#: at or below ENGINE_CAPACITY, `build` warns -- ground no shipped map tested.
#: `test_corpus_maxima_still_hold` recomputes these from the disc, so the pair
#: cannot drift apart silently.
CORPUS_MAX = ((TEXTURED_TRIANGLE, 350), (TEXTURED_QUAD, 683),
              (UNTEXTURED_TRIANGLE, 58), (UNTEXTURED_QUAD, 241))

#: Encoder constants -- corpus-wide on all 73,888 textured polygons, so
#: decision 19 keeps them out of the document (schema §5.2).
CLUT_WORD_HIGH_BYTE = 0x78
PROPERTY_BYTE_7 = 0x00

#: The disc's own fill in 171,626 of 172,488 unused slots (schema §5.1).
DEFAULT_VISIBLE_ANGLES = 0x8000

SHEET_WIDTH = 256
SHEET_HEIGHT = 1024

#: World units per terrain tile, and world Y per point of `height`.
TILE_UNITS = 28
HEIGHT_STEP = 12
#: |ny| threshold for "floor-like" -- decision 15's population.
FLOOR_COS = 0.5

#: Decision 23: on a tile the drift checker named, only these three may be
#: declared; every other payload byte stays a pin the artist cannot reach.
DRIFT_FIELDS = ("height", "slope_height", "slope_type")

# ---------------------------------------------------------------------------
# Terrain records (schema §6.3)
# ---------------------------------------------------------------------------

#: field -> (byte index, shift, mask). GaneshaDx ``TerrainTile.cs``, verified
#: against the corpus: ``b4`` is ``slope_type`` and ``b3 & 0x1F`` is
#: ``slope_height`` -- ``gdxterrain370.py``'s comment had the two swapped.
RECORD_FIELDS: dict[str, tuple[int, int, int]] = {
    "unknown_0a":        (0, 7, 0x01),
    "unknown_0b":        (0, 6, 0x01),
    "surface_type":      (0, 0, 0x3F),
    "unknown_1":         (1, 0, 0xFF),
    "height":            (2, 0, 0xFF),
    "depth":             (3, 5, 0x07),
    "slope_height":      (3, 0, 0x1F),
    "slope_type":        (4, 0, 0xFF),
    "unknown_5a":        (5, 7, 0x01),
    "unknown_5b":        (5, 6, 0x01),
    "unknown_5c":        (5, 5, 0x01),
    "thickness":         (5, 0, 0x1F),
    "pass_through_only": (6, 7, 0x01),
    "unknown_6b":        (6, 6, 0x01),
    "unknown_6c":        (6, 5, 0x01),
    "unknown_6d":        (6, 4, 0x01),
    "shading":           (6, 2, 0x03),
    "impassable":        (6, 1, 0x01),
    "unselectable":      (6, 0, 0x01),
    "rotation":          (7, 0, 0xFF),
}

#: The record's locator keys, which are not payload.
RECORD_KEYS = ("x", "z", "level")

PAYLOAD_FIELDS = tuple(RECORD_FIELDS)


def decode_record(raw: bytes) -> dict[str, int]:
    """The 8 on-disc bytes -> every payload field, raw integers."""
    return {name: (raw[b] >> shift) & mask
            for name, (b, shift, mask) in RECORD_FIELDS.items()}


def encode_record(base: bytes, declared: dict[str, int]) -> bytes:
    """``base``'s 8 bytes with the *declared* fields overwritten.

    An absent field is not zero (schema §7.2): it keeps whatever the base slot
    holds. That is what makes "carry from the base" and "default into a slot
    that already holds it" the same operation, and it is why ``build`` never
    has to invent a byte.
    """
    out = bytearray(base)
    for name, value in declared.items():
        b, shift, mask = RECORD_FIELDS[name]
        if not 0 <= value <= mask:
            raise ValueError(f"{name} = {value} does not fit in {mask:#x}")
        out[b] = (out[b] & ~(mask << shift) & 0xFF) | ((value & mask) << shift)
    return bytes(out)


def sparse_record(raw: bytes, base: bytes) -> dict[str, int]:
    """The fields of ``raw`` that differ from ``base`` -- the sparse form."""
    a, b = decode_record(raw), decode_record(base)
    return {k: v for k, v in a.items() if b[k] != v}


# ---------------------------------------------------------------------------
# Colour (schema §6.4)
# ---------------------------------------------------------------------------

def bgr555_to_hex(word: int) -> str:
    """A BGR555 word -> ``#RRGGBB``. Bit 15 is the STP bit and never enters
    the colour; it rides the per-CLUT ``stp`` mask."""
    r, g, b = word & 0x1F, (word >> 5) & 0x1F, (word >> 10) & 0x1F
    return f"#{r * 255 // 31:02X}{g * 255 // 31:02X}{b * 255 // 31:02X}"


def hex_to_bgr555(text: str, stp: int = 0) -> int:
    """``#RRGGBB`` -> a BGR555 word, quantising with ``(c8*31 + 127)//255``.

    The round trip is exact for every value ``dump`` produces (both directions
    are the nearest-5-bit-of-8-bit mapping). A colour the addon invents off
    that lattice is quantised here; catching *that* is the decision-7
    exact-match gate's job, not this function's.
    """
    s = text.lstrip("#")
    if len(s) not in (6, 8):
        raise ValueError(f"colour {text!r} is not #RRGGBB")
    r8, g8, b8 = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    r5 = (r8 * 31 + 127) // 255
    g5 = (g8 * 31 + 127) // 255
    b5 = (b8 * 31 + 127) // 255
    return (r5 & 0x1F) | ((g5 & 0x1F) << 5) | ((b5 & 0x1F) << 10) | ((stp & 1) << 15)


def clut_to_json(clut: list[int]) -> dict:
    """One 16-entry CLUT -> ``{"colors": [...16], "stp": u16}``."""
    return {"colors": [bgr555_to_hex(w) for w in clut],
            "stp": sum((w >> 15) << i for i, w in enumerate(clut))}


def clut_from_json(entry: dict) -> list[int]:
    colors = entry["colors"]
    if len(colors) != 16:
        raise ValueError(f"a CLUT holds 16 entries, not {len(colors)}")
    stp = int(entry.get("stp", 0))
    return [hex_to_bgr555(c, (stp >> i) & 1) for i, c in enumerate(colors)]
