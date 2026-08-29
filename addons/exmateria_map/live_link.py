"""The live link's `bpy`-free core: push an authored map into a running battle.

The artist edits a map in Blender and sees it in PCSX-Redux a moment later,
without an ISO, a reboot, or a walk back to the map. Design and the six
decisions behind it: `../../docs/live-link-v1.md`. Why this module lives in
the addon and why there is exactly **one** copy of it:
`../../../docs/adr/0005-the-blender-addon-is-the-shippable-authoring-tool.md`.

This module imports `bpy` **never** -- only the stdlib. That is ADR-0005
decision 2, and it is what keeps the core testable under plain `pytest` (the
emulator-gated audits cannot be) and the pusher drivable from a document with
Blender closed. The panel and the button are the only parts that need `bpy`,
and they live elsewhere.

## What the engine holds while a battle is running

Main RAM holds an unpacked **render view**, not the map file. The `0x40`
section has been exploded into four contiguous per-bucket arrays -- positions
-- plus two parallel **normal** arrays for the two textured buckets, plus the
GPU primitive packets. There is no terrain chunk, no palette chunk and no
texture-sheet source, which is why the pull direction is an *identification*
and `dump` reads the disc (decision 1).

## The descriptor block is what is authoritative

`live-link-v1.md` §2 named eight globals -- four per-bucket counts and four
array pointers -- that the renderers genuinely read. They are all **recomputed
from scratch every frame**, immediately before the four renderer calls, so a
poke into any of them lasts less than one frame. What survives is the
**descriptor block** they are recomputed *from*:

    0x800FBE00  primary mesh
    0x800FBE98 .. 0x800FC2C0   the 8 animated-mesh instances (stride 0x98)

Each frame, per bucket, the dispatch at `0x800E840C` does:

    count    = descriptor[+0x90 + 2*bucket]
    positions= POSITION_BASE[bucket] + descriptor[+0x88 + 2*bucket] * stride
    normals  = NORMAL_BASE[bucket]   + descriptor[+0x88 + 2*bucket] * stride

so **one start index governs positions, normals and packets together**, each
with its own stride -- which is a strong self-check on the reading, and it
holds for all four buckets.

Two consequences, and the second is a trap:

1. The four arrays are **shared and sliced**. Polygon `i` of the primary mesh
   is at `base + (start + i) * stride`, not at `base + i * stride`. On a map
   with no animated meshes every start index is 0 and the two happen to agree
   -- Gariland is such a map, so a rig that ignores `start` looks correct
   there and silently stops being correct elsewhere.
2. Changing polygon counts means writing `+0x90..+0x96` *and* re-deriving
   every following instance's start index. This module does not: it refuses a
   document whose per-bucket counts differ from what is loaded. Growing a mesh
   is `build`'s job.

## Provenance

Confirmed **[LIVE]** in a Gariland battle on 2026-08-26: descriptor 0 reads
`24 / 361 / 18 / 51`, exactly MAP022 a0's polygon counts, and all four pointer
globals resolve to the bases below. See `../../docs/live-link-v1.md` §2.
"""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path
from typing import NamedTuple

# The document's palette entries are hex colours plus an STP mask, and turning
# those into BGR555 words is `build`'s arithmetic. It is imported rather than
# repeated: a CLUT that reached RAM through different packing than the one that
# reaches the disc would make this loupe lie in the one way it exists to stop.
# `mapfile` comes along for the animation chunks (decision 11): the `0x6c`
# instruction table this module erases and re-installs is decoded ONCE, by the
# reader `dump` and `build` already use, so the live link cannot come to a
# different reading of a record than the disc writer does.
# Both spellings are needed -- the addon imports this module as a package
# member, the tests and the CLI tools import it as a top-level module.
try:                                     # pragma: no cover - import shape only
    from ._vendor.exmateria_map.document import clut_from_json
    from ._vendor.exmateria_map import mapfile as _mapfile
except ImportError:                      # pragma: no cover
    from _vendor.exmateria_map.document import clut_from_json
    from _vendor.exmateria_map import mapfile as _mapfile

# --- the document's four polygon buckets, in disc order (schema §3) ---------
BUCKETS = ("textured_triangle", "textured_quad",
           "untextured_triangle", "untextured_quad")
VERTS = {"textured_triangle": 3, "textured_quad": 4,
         "untextured_triangle": 3, "untextured_quad": 4}

#: Bytes per vertex in RAM. Six are coordinates; bytes 6-7 are the polygon's
#: own metadata (the terrain binding word on vertex 0, VISIBLE_ANGLES on
#: vertex 1) and are **not** ours to write. `live_geometry.py`'s docstring has
#: the measurement: garbage there shatters the map, zero happens to be benign.
VERTEX_STRIDE = 8
COORD_BYTES = 6

RAM_BASE = 0x80000000
RAM_BYTES = 2 * 1024 * 1024

# --- the descriptor block --------------------------------------------------
DESCRIPTOR_BASE = 0x800FBE00
DESCRIPTOR_STRIDE = 0x98
DESCRIPTOR_COUNT = 9          # primary mesh + 8 animated-mesh instances

#: Offsets **within** a descriptor. Four `ushort` start indices then four
#: `ushort` counts, both in `BUCKETS` order.
DESCRIPTOR_STARTS = 0x88
DESCRIPTOR_COUNTS = 0x90


class Descriptor(NamedTuple):
    """One mesh instance's slice of the four shared arrays."""

    index: int
    starts: tuple[int, int, int, int]
    counts: tuple[int, int, int, int]

    @property
    def address(self) -> int:
        return DESCRIPTOR_BASE + self.index * DESCRIPTOR_STRIDE

    def is_empty(self) -> bool:
        """No polygons in any bucket -- an unused animated-mesh slot."""
        return not any(self.counts)


def parse_descriptor(block: bytes, index: int) -> Descriptor:
    """Descriptor `index` out of a verbatim copy of the block.

    `block` is the bytes starting at `DESCRIPTOR_BASE`; it need only be long
    enough to contain the descriptor asked for.
    """
    if not 0 <= index < DESCRIPTOR_COUNT:
        raise ValueError(
            f"descriptor {index} is outside the block's {DESCRIPTOR_COUNT} "
            f"entries")
    at = index * DESCRIPTOR_STRIDE
    if len(block) < at + DESCRIPTOR_COUNTS + 8:
        raise ValueError(
            f"need {at + DESCRIPTOR_COUNTS + 8} bytes to read descriptor "
            f"{index}, got {len(block)}")
    starts = struct.unpack_from("<4H", block, at + DESCRIPTOR_STARTS)
    counts = struct.unpack_from("<4H", block, at + DESCRIPTOR_COUNTS)
    return Descriptor(index=index, starts=starts, counts=counts)


#: The engine's four polygon arrays in main RAM -- ADR-0004 decision 28. These
#: are the array **capacities**, not the `0xB0` slot table's (512/768/64/256),
#: which is a different and larger thing:
#:
#:   0x800F2A68  slti s0,0x168   stride 0x18   textured_triangle    360
#:   0x800F2BE4  slti s0,0x2C6   stride 0x20   textured_quad        710
#:   0x800F2C2C  slti s0,0x40    stride 0x18   untextured_triangle   64
#:   0x800F2C50  slti v1,0x2000  stride 0x20   untextured_quad      256
#:
#: The last two are written as a byte cursor rather than a count -- 0x2000/0x20
#: is exactly 256 records -- which is why a count-shaped search for them finds
#: only the first two. A copy, deliberately: the addon does not import
#: `exmateria_map` (ADR-0004 §7), and `document.ENGINE_CAPACITY` is the
#: package's own statement of the same fact for `build`. ADR-0004 decision 31
#: vendors the package, which makes this copy collapsible once that is built.
ENGINE_CAPACITY = {"textured_triangle": 360, "textured_quad": 710,
                   "untextured_triangle": 64, "untextured_quad": 256}

#: Bytes per polygon in RAM, per bucket.
POLYGON_STRIDE = {b: VERTS[b] * VERTEX_STRIDE for b in BUCKETS}


class LiveLinkError(RuntimeError):
    """Refuse rather than write polygons' worth of bytes at a guessed address."""


def read_descriptors(block: bytes) -> list[Descriptor]:
    """All nine descriptors out of a verbatim copy of the block."""
    return [parse_descriptor(block, i) for i in range(DESCRIPTOR_COUNT)]


def check_descriptors(block: bytes) -> list[Descriptor]:
    """The push direction's guard. Returns the descriptors, or raises.

    `live_geometry.py` locates by **verifying** -- polygon 0's first vertex is
    a needle, and only a unique whole-bucket hit is accepted -- because the
    pull direction has to prove the declared map is the loaded one. Pushing a
    whole map needs no such proof: the artist has the document open in
    Blender, they edited it, and there is nothing to identify.

    What is still worth catching is that a map is loaded at all and that this
    really is the descriptor block, and the engine's own array bounds say both
    in four comparisons. Note this is *not* a claim that the loaded map is the
    document's -- see `live-link-v1.md` decision 2, as amended.
    """
    descriptors = read_descriptors(block)
    primary = descriptors[0]
    if primary.is_empty():
        raise LiveLinkError(
            "the primary mesh descriptor at "
            f"0x{primary.address:08X} carries no polygons in any bucket -- no "
            "map is loaded, or it has not finished loading. Refusing to write.")
    for d in descriptors:
        for bucket, start, count in zip(BUCKETS, d.starts, d.counts):
            cap = ENGINE_CAPACITY[bucket]
            if start + count > cap:
                raise LiveLinkError(
                    f"descriptor {d.index} at 0x{d.address:08X} claims "
                    f"{bucket} [{start}, {start + count}) but the engine's "
                    f"array holds {cap} -- this is not a descriptor block, or "
                    "the map is mid-load. Refusing to write.")
    return descriptors


# --- where each bucket's arrays live ---------------------------------------

class Sink(NamedTuple):
    """A bucket's three parallel arrays, all indexed by the same start index."""

    positions: int
    normals: int | None          # None on the two unlit buckets
    packet: int                  # offset from the packet base, not an address
    packet_stride: int


#: All four position bases and both normal bases are **[LIVE]** -- the position
#: four matched the disc byte-for-byte in `live_geometry.py` (0 mismatches of
#: 10,644 coordinate bytes) and the normal two were confirmed the same way on
#: 2026-08-26 (0 of 9,096). The dispatch recomputes its pointer globals from
#: exactly these bases every frame, and reading those globals back live gives
#: the same six numbers.
SINKS = {
    "textured_triangle":   Sink(0x8011A2D8, 0x801251D4, 0x0000,  0x28),  # GT3
    "textured_quad":       Sink(0x8011C498, 0x80127394, 0x3840,  0x34),  # GT4
    "untextured_triangle": Sink(0x80122004, None,       0xC878,  0x14),  # G3
    "untextured_quad":     Sink(0x80122604, None,       0xCD78,  0x18),  # G4
}

#: `FUN_8012cc54`, the textured-triangle renderer. Not a sink -- it is here
#: because the textured-quad normal array runs up to its first byte, which is
#: the arithmetic check that a wrong stride or a wrong bound would fail.
TEXTURED_TRIANGLE_RENDERER = 0x8012CC54

#: Where the CURRENT primitive buffer's base is kept. UV, CLUT and TPAGE live
#: in the packets, are written once at load by `FUN_800f5578`, and are never
#: rewritten -- the per-frame renderer touches only the three screen XYs and the
#: vertex colours NCT produces. So they are pokeable *and* persistent, which is
#: what makes a texture-page or palette edit hold. Measured: a write here is
#: byte-identical a second later.
#:
#: **This is a pointer that ALTERNATES** -- see `PACKET_BASES`. Writing the base
#: it happens to read is half the job. The comment here used to credit
#: `FUN_800f4dd4` with building the packets; that function loads the four
#: POSITION arrays and the descriptor block's `+0x88..+0x96` and touches no
#: packet field.
PACKET_BASE_POINTER = 0x8011A2D4


# --- the write plan --------------------------------------------------------

#: The two per-vertex coordinate fields a document can push. `positions` is the
#: geometry; `normals` is what the GTE re-lights from every frame.
FIELDS = ("positions", "normals")


def plan(descriptor: Descriptor, bucket: str, field: str,
         polygons: list) -> list[tuple[int, bytes]]:
    """One `(address, 6 bytes)` write per vertex, in bucket order.

    Six of every eight bytes: bytes 6-7 of each vertex are the polygon's own
    metadata -- the terrain binding word on vertex 0, VISIBLE_ANGLES on vertex
    1 -- and they are not ours. `live_geometry.py`'s docstring has the A/B/A:
    scribbling them culls quads away into holes.

    The address comes off the descriptor, not off the polygon index, because
    the arrays are shared and sliced. On a map with no animated meshes every
    start index is 0 and the distinction is invisible; that is exactly why it
    is spelled out here rather than left to the caller.
    """
    if field not in FIELDS:
        raise ValueError(f"{field!r} is not a per-vertex field; {FIELDS} are")
    sink = SINKS[bucket]
    base = getattr(sink, field)
    if base is None:
        raise LiveLinkError(
            f"{bucket} has no normal array -- it is unlit by construction, "
            "taking one flat colour from DAT_800f5b58. Nothing to push.")

    # The document's OWN length, not the descriptor's count. Adding and
    # removing geometry is what #598 built; what bounds it is `check_capacity`
    # and `check_followers`, which the caller runs before it gets here, not an
    # equality that also forbade every legal shrink.
    i = BUCKETS.index(bucket)
    start = descriptor.starts[i]
    return plan_at(base + start * POLYGON_STRIDE[bucket], bucket, polygons)


def plan_at(origin: int, bucket: str, polygons: list) -> list[tuple[int, bytes]]:
    """`plan`, from an absolute origin rather than a descriptor slice.

    The descriptor is how the *push* direction finds a bucket. The pull
    direction finds it by searching -- `tools/live_geometry.py` takes polygon
    0's first vertex as a needle and accepts only a unique whole-bucket hit --
    and what that returns is an address, not a slice. Both want the same
    per-vertex arithmetic, so it lives here once.

    It is also the flat form of a bucket's coordinates: the needle itself is
    `b"".join(d for _, d in plan_at(0, bucket, polys))`, so nothing else has to
    say "three little-endian shorts per vertex, six of every eight bytes".
    """
    nverts, stride = VERTS[bucket], POLYGON_STRIDE[bucket]
    writes = []
    for p, poly in enumerate(polygons):
        if len(poly) != nverts:
            raise LiveLinkError(
                f"{bucket}[{p}] has {len(poly)} vertices, not {nverts}")
        for k, (x, y, z) in enumerate(poly):
            writes.append((origin + p * stride + k * VERTEX_STRIDE,
                           struct.pack("<hhh", x, y, z)))
    return writes


# --- bytes 6-7: the binding word and VISIBLE_ANGLES -------------------------
#
# The two trailing bytes of each vertex are NOT padding. `live_geometry.py`
# identified them on MAP022 a0 across all four buckets, 454 of 454 polygons,
# and `tests/test_live_link.py` re-measures the whole rule against the
# checked-in Gariland savestate on every run:
#
#     vertex 0's 4th short = the terrain BINDING word, verbatim, on the two
#                            textured buckets; 0x0000 on the untextured two,
#                            which carry no binding at all
#     vertex 1's 4th short = VISIBLE_ANGLES from the 0xB0 chunk, with bit 0
#                            SET on textured polygons and clear on untextured
#     vertex 2 (and 3)     = 0x0000
#
# They are persistent, not per-frame scratch: two RAM dumps two seconds apart
# differ in 2,517 bytes elsewhere and in zero bytes of any polygon array.
#
# **Why the push writes them, when it used to leave them alone.** A mid-mesh
# deletion re-slots every surviving polygon. Positions, normals and the packet
# all follow a polygon to its new slot; these two shorts would not, so the
# survivor would arrive wearing the previous occupant's VISIBLE_ANGLES -- and a
# wrong value here does not mis-colour a quad, it culls it away into a hole
# (measured A/B/A). Shrink without this write ships a feature that punches
# holes on the ordinary edit. A new slot at growth is worse still: it holds
# whatever was there.
#
# `unknown_untextured` -- the untextured record's four raw property bytes --
# is a DIFFERENT thing from these two shorts and is still unlocated. It stays
# in `UNPUSHED`, and is not zero-filled (#496).

#: The document's `visible_angles` is `null` on the 10 of 169 resources with no
#: 0xB0 chunk. They write what `stamp_new_faces` already gives a new face, so
#: RAM never holds a value the document cannot name.
VISIBLE_ANGLES_DEFAULT = 0x8000

#: Bit 0 of the VISIBLE_ANGLES word is the engine's own mark for a textured
#: polygon. It is a real transformation, not a coincidence: no `visible_angles`
#: value on MAP022 a0's disc carries it, and RAM sets it on all 385 textured
#: polygons and on none of the 69 untextured ones.
TEXTURED_FLAG = 0x0001

#: Which vertex of a polygon carries which of the two metadata shorts.
BINDING_VERTEX = 0
VISIBLE_ANGLES_VERTEX = 1


def binding_word(poly: dict) -> int:
    """One polygon's `{x, z, level}` as the halfword RAM holds.

    `mapfile.read_mesh` reads the disc's pair back as
    `(data[o + 1], data[o] >> 1, data[o] & 1)`, so the word is
    `x << 8 | z << 1 | level`. `FF FF` is the sentinel `{255, 127, 1}` --
    *this face is not on the grid* -- and `FF FE` is `{255, 127, 0}`, an
    ordinary binding that happens to point outside it. They differ by one bit
    and mean opposite things.

    An untextured polygon carries no binding (the disc's binding array is
    `tt + tq` long, not `tt + tq + ut + uq`), and RAM holds zero for all 69 of
    MAP022 a0's. Zero is what the loader put there, not a fill this module
    chose.
    """
    t = poly.get("terrain")
    if not t:
        return 0
    return ((t["x"] & 0xFF) << 8) | ((t["z"] & 0x7F) << 1) | (t["level"] & 1)


def visible_angles_word(poly: dict, textured: bool) -> int:
    """One polygon's VISIBLE_ANGLES as the halfword RAM holds."""
    va = poly.get("visible_angles")
    if va is None:
        va = VISIBLE_ANGLES_DEFAULT
    return (va | TEXTURED_FLAG) if textured else (va & 0xFFFF)


def plan_metadata(descriptor: Descriptor, bucket: str,
                  polygons: list) -> list[tuple[int, bytes]]:
    """Two `(address, 2 bytes)` writes per polygon: bytes 6-7 of vertices 0-1.

    Addressed off the descriptor's start index, like every other plan here,
    and into the POSITION array -- these shorts share a vertex with the
    coordinates rather than living in a parallel array of their own.
    """
    if bucket not in BUCKETS:
        raise ValueError(f"{bucket!r} is not a bucket; {BUCKETS} are")
    textured = SINKS[bucket].normals is not None
    i = BUCKETS.index(bucket)
    stride = POLYGON_STRIDE[bucket]
    origin = SINKS[bucket].positions + descriptor.starts[i] * stride

    writes = []
    for p, poly in enumerate(polygons):
        at = origin + p * stride
        writes.append((at + BINDING_VERTEX * VERTEX_STRIDE + COORD_BYTES,
                       struct.pack("<H", binding_word(poly))))
        writes.append((at + VISIBLE_ANGLES_VERTEX * VERTEX_STRIDE + COORD_BYTES,
                       struct.pack("<H", visible_angles_word(poly, textured))))
    return writes


# --- the two growth gates ---------------------------------------------------
#
# The loader does not bound-check the four polygon arrays (ADR-0004 decision
# 28), and a following slice's start index is not re-derived by anything here.
# So a count is not free to move in either direction, and these two are what
# stand between "the artist added a face" and "the emulator is writing through
# main RAM". They are built and seeded red BEFORE the count refusal was lifted.
#
# **Neither is gradable on the only map this repo holds a savestate for.**
# MAP022 a0 has no animated mesh at all and its `24 / 361 / 18 / 51` sit far
# under `360 / 710 / 64 / 256`, so no document an artist could author reaches
# either refusal there. They are graded by `tests/test_live_link.py` and by
# `tests/blender_live_push.py`'s fake RAM, and that has to be said out loud
# wherever they are claimed green.

#: The largest the shipped disc goes, per bucket, SUMMED over a resource's
#: primary mesh and its AnimatedMesh 1-8. Above this and at or below
#: `ENGINE_CAPACITY` is ground no shipped map has tested, so it warns rather
#: than refusing -- the same two-tier arithmetic `build.py` §10.4 runs, on the
#: same constants. A copy for the same reason `ENGINE_CAPACITY` is one, and
#: `test_live_link.py` asserts the two agree so they cannot drift apart.
CORPUS_MAX = {"textured_triangle": 350, "textured_quad": 683,
              "untextured_triangle": 58, "untextured_quad": 241}


def animated_counts(descriptors: list) -> tuple[int, int, int, int]:
    """How many polygons the eight AnimatedMesh instances hold, per bucket.

    The live answer to the question `build` asks the disc
    (`mapfile.animated_mesh_counts`). No disc read, no corpus, and none of
    ADR-0004 §7's problem: `check_descriptors` already reads all nine.
    """
    return tuple(sum(d.counts[k] for d in descriptors[1:])
                 for k in range(len(BUCKETS)))


def check_capacity(descriptors: list,
                   counts: tuple[int, int, int, int]) -> list[str]:
    """Refuse above the engine's array, warn above the corpus's maximum.

    Returns the warnings; raises `LiveLinkError` on the refusal. The bound is
    on the SUM of the primary mesh and its animated instances, because the
    loader's destination cursors are shared across all nine.
    """
    animated = animated_counts(descriptors)
    warnings = []
    for k, bucket in enumerate(BUCKETS):
        total = int(counts[k]) + animated[k]
        cap = ENGINE_CAPACITY[bucket]
        if total > cap:
            raise LiveLinkError(
                f"the document has {counts[k]} {bucket} and the loaded map's "
                f"animated meshes hold {animated[k]} more, which is {total}; "
                f"the engine's array holds {cap} and the loader does NOT "
                "bound-check it (ADR-0004 decision 28), so writing this would "
                "not be a wrong picture, it would be memory corruption. "
                "Refusing to write.")
        end = descriptors[0].starts[k] + int(counts[k])
        if end > cap:
            raise LiveLinkError(
                f"the primary mesh's {bucket} slice starts at "
                f"{descriptors[0].starts[k]} and {counts[k]} polygons would "
                f"run it to {end}, past the {cap} the engine's array holds. "
                "Refusing to write.")
        if total > CORPUS_MAX[bucket]:
            warnings.append(
                f"{bucket} {total} is above the corpus maximum "
                f"({CORPUS_MAX[bucket]}) and at or below the engine's array "
                f"({cap}) -- no shipped map has gone here (decision 28)")
    return warnings


def check_followers(descriptors: list,
                    counts: tuple[int, int, int, int]) -> None:
    """Refuse growth into a bucket some animated mesh follows in.

    Growing the primary mesh shoves every *following* slice: its data must move
    **and** its start at `+0x88` must be rewritten. That is not built, and it is
    refused rather than guessed at -- no savestate in this repo reaches any of
    the twelve maps that have a follower, so a shove could only ever be checked
    against fake RAM, and the asymmetry decides it: a refusal costs an artist on
    twelve maps a walk to `build`, which does this correctly, while a wrong
    shove is unbounded memory corruption on a map no instrument here can watch.

    Per bucket, because the four arrays are independent. Measured over the 169
    geometry-carrying resources, the number with no follower at all is 160
    textured triangles, 154 textured quads, 169 untextured triangles -- no
    shipped resource animates that bucket -- and 168 untextured quads.

    Shrinking is not refused: nothing moves, so every follower's `+0x88` stays
    valid.
    """
    primary = descriptors[0]
    for k, bucket in enumerate(BUCKETS):
        if int(counts[k]) <= primary.counts[k]:
            continue
        followers = [d for d in descriptors[1:] if d.counts[k]]
        if not followers:
            continue
        who = ", ".join(f"descriptor {d.index} at 0x{d.address:08X} "
                        f"({d.counts[k]})" for d in followers)
        raise LiveLinkError(
            f"the document has {counts[k]} {bucket} and the loaded map's "
            f"primary mesh carries {primary.counts[k]}; growing that bucket "
            f"shoves the animated mesh that follows it -- {who} -- and both "
            "its data and its start index would have to move. That is `build`'s "
            "job, not a poke's. Shrinking this bucket is fine; growing it is "
            "not. Refusing to write.")


# --- the count write --------------------------------------------------------
#
# **The count is the switch.** The dispatch at `0x800E840C` recomputes
# `count = descriptor[+0x90 + 2*bucket]` immediately before each renderer call,
# every frame. Lower it and the slots past the end simply stop being drawn on
# the next frame; raise it and the renderer draws more. No reload, no
# reallocation -- which is what makes a count change a poke at all, and is also
# the danger: the loader does not bound-check these arrays (ADR-0004 decision
# 28), so a count above capacity is not a refusal, it is memory corruption.
# `check_capacity` and `check_followers` are what stand between the two.


def bucket_counts(polygons: list) -> tuple[int, int, int, int]:
    """How many polygons a document carries in each bucket, in `BUCKETS` order."""
    return tuple(sum(1 for p in polygons if p.get("kind") == b)
                 for b in BUCKETS)


def plan_counts(descriptor: Descriptor,
                counts: tuple[int, int, int, int]) -> list[tuple[int, bytes]]:
    """One `(address, 2 bytes)` write per bucket, at `+0x90 + 2*bucket`.

    Driven by the **four buckets**, never by a plan dict: `plan_document` skips
    a bucket the document has no polygons in, so a count write that followed it
    would leave an emptied bucket's old count standing and the engine would go
    on drawing slots the document no longer has. **Zero is a legal count to
    write**, and it is the whole of what "the artist deleted that bucket" looks
    like.
    """
    at = descriptor.address + DESCRIPTOR_COUNTS
    return [(at + 2 * k, struct.pack("<H", int(counts[k])))
            for k in range(len(BUCKETS))]


# --- the primitive packets: uv, palette_id, texture_page --------------------
#
# Rooted in `FUN_800f5578`, which builds the packets at load. (`FUN_800f4dd4`,
# which this module's PACKET_BASE_POINTER comment used to credit, is the
# POSITION loader -- it fills the four coordinate arrays and the descriptor
# block's `+0x88..+0x96`, and touches no packet field.)
#
# Per textured triangle, at stride 0x28 from the packet base:
#
#     *(char *)(base + i*0x28 + 0x0c) = u0     +0x0d = v0
#     *(char *)(base + i*0x28 + 0x18) = u1     +0x19 = v1
#     *(char *)(base + i*0x28 + 0x24) = u2     +0x25 = v2
#     *(ushort *)(base + i*0x28 + 0x0e) = src & 0x3f | 0x7800      <- CLUT
#     *(ushort *)(base + i*0x28 + 0x1a) = src                      <- TPAGE
#
# and per textured quad, at stride 0x34 from base + 0x3840, the same offsets
# plus a fourth pair at +0x30/31. That is POLY_GT3 / POLY_GT4 exactly, and the
# command bytes confirm it: 24 packets carrying 0x34 and 361 carrying 0x3C, the
# loaded map's own bucket counts.
#
# **[LIVE]** in a Gariland battle (MAP022 a0), 2026-08-25: 1,516 of 1,516 UV
# corners equal the disc's, `palette_id` maps 1:1 onto `0x7800|id` over 10
# distinct values, `texture_page` 1:1 onto the TPAGE word's low two bits over 3.
#
# The two untextured buckets are absent on purpose. Their packets are G3/G4 --
# no UV, no CLUT, no TPAGE -- and `FUN_800f5578` writes them a flag byte and
# nothing else.


class PacketLayout(NamedTuple):
    """Where a bucket's packet keeps the three fields a document can author."""

    uv: tuple                    # one byte-pair offset per corner, in order
    clut: int                    # halfword; the document owns its low 4 bits
    tpage: int                   # halfword; the document owns its low 2 bits


PACKET_LAYOUT = {
    "textured_triangle": PacketLayout(uv=(0x0C, 0x18, 0x24), clut=0x0E, tpage=0x1A),
    "textured_quad":     PacketLayout(uv=(0x0C, 0x18, 0x24, 0x30), clut=0x0E, tpage=0x1A),
}

#: How many bits of each halfword the DOCUMENT owns, and therefore how much of
#: it a push may replace. `mapfile` reads `palette_id` as `data[o+2] & 0x0F` and
#: `texture_page` as `data[o+6] & 3`, so those are the widths -- and writing the
#: rest would clobber the CLUT's `0x7800` row and the TPAGE's VRAM column.
#:
#: Masking rather than reconstructing is what makes `texture_page` 3 safe. The
#: Gariland battle only ever shows pages 0-2 (7,207 polygons in the corpus use
#: page 3, none of them there), so `0x0C + page` is a relation measured on ONE
#: map's source data -- `FUN_800f5578` copies TPAGE through verbatim and does no
#: arithmetic, so the base is not the engine's to promise. Under the mask the
#: base is never assumed: whatever column the map loaded into is preserved.
PACKET_MASK = {"palette_id": 0x0F, "texture_page": 0x03}

#: Packet fields a document can push, and which layout slot each one writes.
PACKET_FIELDS = ("uv", "palette_id", "texture_page")

#: **The packets are DOUBLE BUFFERED, and both copies must be written.**
#:
#: `FUN_800ee104` sets it up:
#:
#:     DAT_8011a2d4 = &DAT_800fc55c;      <- the base starts at buffer A
#:     do { ...; puVar4 = puVar4 + 0xee28; iVar2 = iVar2 + 1; } while (iVar2 < 2);
#:
#: -- exactly two buffers, `0xEE28` apart. Sampling `PACKET_BASE_POINTER` live in
#: a Gariland battle returns `0x800FC55C` and `0x8010B384` and nothing else,
#: which is that pair. [LIVE] 2026-08-25.
#:
#: This is the one structural difference from `positions` and `normals`: those
#: arrays are static and shared, so one write serves every frame. A packet write
#: into whichever buffer the pointer happens to name lands in half the frames.
#: Measured: a palette push into one buffer changed 385 bytes and the screen did
#: not move at all.
PACKET_BUFFER_STRIDE = 0xEE28
PACKET_BASES = (0x800FC55C, 0x800FC55C + PACKET_BUFFER_STRIDE)


def check_packet_base(live: int) -> None:
    """Refuse a `PACKET_BASE_POINTER` that is not one of the two buffers.

    Writing the wrong base is SILENT: every address involved is inside main
    RAM, so `apply` reports a plausible changed-byte count and the only symptom
    is a picture that does not move -- which reads as "the push does not work"
    rather than as "the push went somewhere else". Decision 2's locate-by-verify
    applied to the one address here that is not static.
    """
    if live not in PACKET_BASES:
        raise LiveLinkError(
            f"the packet base pointer reads 0x{live:08X}, which is neither "
            f"buffer ({' / '.join(f'0x{b:08X}' for b in PACKET_BASES)}). "
            "This is not the map this module was built against -- refusing "
            "rather than writing into whatever is there")


def plan_packets(descriptor: Descriptor, packet_base: int, bucket: str,
                 field: str, polygons: list,
                 current: bytes) -> list[tuple[int, bytes]]:
    """Writes for one packet `field` of one bucket.

    `packet_base` is read from `PACKET_BASE_POINTER` at push time -- unlike the
    position and normal arrays, whose bases are static, the packets live behind
    a pointer.

    `current` is the bucket's packet bytes as they stand in RAM,
    `stride * count` of them from the same origin these writes are addressed
    against. `uv` does not read it; `palette_id` and `texture_page` are
    read-modify-write, because the document owns only part of each halfword.
    Passing it empty for one of those is refused rather than treated as zero --
    zeroing the CLUT's row bits would point every face at VRAM row 0.
    """
    if field not in PACKET_FIELDS:
        raise ValueError(f"{field!r} is not a packet field; {PACKET_FIELDS} are")
    lay = PACKET_LAYOUT.get(bucket)
    if lay is None:
        raise LiveLinkError(
            f"{bucket} has no textured packet -- it is a G3/G4 primitive with "
            "no UV, CLUT or TPAGE. Nothing to push.")

    i = BUCKETS.index(bucket)
    start, count = descriptor.starts[i], descriptor.counts[i]
    sink = SINKS[bucket]
    stride = sink.packet_stride
    origin = packet_base + sink.packet + start * stride

    if field == "uv":
        writes = []
        for p, poly in enumerate(polygons):
            uv = poly["uv"]
            if len(uv) != len(lay.uv):
                raise LiveLinkError(
                    f"{bucket}[{p}] has {len(uv)} uv pair(s), not {len(lay.uv)}")
            for k, off in enumerate(lay.uv):
                u, v = uv[k]
                writes.append((origin + p * stride + off, bytes((u & 0xFF,
                                                                 v & 0xFF))))
        return writes

    # The bound is the DOCUMENT's length, not the descriptor's count. A slot
    # past the loaded count is not a slot with nothing in it -- the engine's
    # array is fixed-capacity and still holds whatever the last load or the
    # last push left, and the count only says how many are DRAWN. Reading only
    # `count` slots and treating the rest as zero cleared the CLUT's 0x7800
    # row bits on every grown slot and pointed those faces at VRAM row 0.
    # Measured on a live Gariland battle: the map came back geometrically
    # whole and half of it blue, while the byte count truthfully reported the
    # next press as 0 changed bytes. The plan was wrong, not the count.
    need = stride * len(polygons)
    if len(current) < need:
        raise LiveLinkError(
            f"{field} is a read-modify-write: it needs the {need} byte(s) of "
            f"{bucket} packets currently in RAM for the {len(polygons)} "
            f"polygon(s) planned, and got {len(current)}. Writing the whole "
            "halfword would clobber the bits the engine owns")
    off = lay.clut if field == "palette_id" else lay.tpage
    mask = PACKET_MASK[field]
    writes = []
    for p, poly in enumerate(polygons):
        held, = struct.unpack_from("<H", current, p * stride + off)
        writes.append((origin + p * stride + off,
                       struct.pack("<H", (held & ~mask) | (poly[field] & mask))))
    return writes


def plan_packets_document(client, descriptor: Descriptor,
                         document: dict) -> dict[tuple, list]:
    """Every packet field of every textured bucket, for BOTH buffers.

    Takes a client because two of its three inputs are only knowable live: the
    base pointer (checked against the two the engine alternates) and the packet
    bytes currently held, which `palette_id` and `texture_page` modify rather
    than replace.

    Keyed `(bucket, field, base)` so a caller reporting per-plan counts says
    which buffer, and so the two buffers' writes can never collapse into one
    another in a dict.
    """
    live, = struct.unpack("<I", client.read(PACKET_BASE_POINTER, 4))
    check_packet_base(live)

    by_bucket: dict[str, list[dict]] = {b: [] for b in BUCKETS}
    for poly in document.get("polygons", ()):
        by_bucket[poly["kind"]].append(poly)

    plans: dict[tuple, list] = {}
    for base in PACKET_BASES:
        for bucket in PACKET_LAYOUT:
            polys = by_bucket[bucket]
            if not polys:
                continue
            sink = SINKS[bucket]
            i = BUCKETS.index(bucket)
            stride, start = sink.packet_stride, descriptor.starts[i]
            # `max`, not the descriptor's count: on a growth the plan is
            # longer than the loaded map and every slot it reaches still holds
            # bits the engine owns. `check_capacity` has already refused a
            # count that would run this read past the array.
            current = client.read(
                base + sink.packet + start * stride,
                stride * max(descriptor.counts[i], len(polys)))
            for field in PACKET_FIELDS:
                if any(field not in p for p in polys):
                    continue
                plans[(bucket, field, base)] = plan_packets(
                    descriptor, base, bucket, field, polys, current)
    return plans


# --- the transport ---------------------------------------------------------
# A small Lua-over-HTTP client and nothing more. `pcsx-agent` is the reference
# for the shape and is deliberately NOT a dependency (ADR-0005 decision 5): it
# is generic transport with no FFT knowledge, it must be edited in its own
# worktree, and an addon an artist installs cannot pip-install anything.

import contextlib        # noqa: E402  -- kept beside the transport it serves
import urllib.error      # noqa: E402
import urllib.request    # noqa: E402

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8080


class TransportError(LiveLinkError):
    """The emulator did not answer, or answered with a Lua error."""


class NoHandlerError(TransportError):
    """The emulator answered, and has no such Lua handler -- a `404`.

    Its own class because it is the failure with the most useful diagnosis and
    the least obvious one: the emulator is up, every upstream endpoint works,
    and the artist simply left `-dofile pcsx_handlers.lua` off the launch
    line. Folded into a generic transport failure it reads as "no emulator",
    which sends them to look at the wrong thing entirely.
    """


import json                                              # noqa: E402
import os                                                # noqa: E402
import os.path                                           # noqa: E402

#: The Lua handlers this module needs, as installed. Shipped inside the addon
#: package, so a zip install carries it and this path resolves for the artist
#: exactly as it does here.
HANDLERS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "pcsx_handlers.lua")


def launch_command(port: int = DEFAULT_PORT, handlers: str = "",
                   binary: str = "pcsx-redux") -> str:
    """The command that brings up an emulator this module can talk to."""
    return (f"{binary} -webserver -webserver-port {port} "
            f"-dofile {handlers or HANDLERS}")


#: What the emulator is called, in the order worth trying.
#:
#: Both routes need the SAME folder and neither needs a second answer from the
#: artist -- so the binary is found in it by name rather than asked for. That
#: works because when Blender launches the emulator it also SETS the working
#: directory, so "where pcsx-redux lives" and "where pcsx-redux runs" are made
#: to be one folder instead of being two questions.
BINARY_NAMES = ("pcsx-redux", "pcsx-redux.exe", "PCSX-Redux.exe",
                "pcsx-redux.AppImage",
                os.path.join("PCSX-Redux.app", "Contents", "MacOS",
                             "PCSX-Redux"))


def find_binary(directory: str) -> str:
    """The emulator inside `directory`, or `""`.

    Executable-checked, not just present: a `pcsx-redux` that is a README or a
    half-finished download is not the thing to hand to `Popen`, and the failure
    it produces there names a permission error rather than the folder.
    """
    if not directory:
        return ""
    for name in BINARY_NAMES:
        path = os.path.join(directory, name)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return ""


def launch_argv(directory: str, port: int = DEFAULT_PORT,
                handlers: str = "") -> list[str]:
    """`launch_command` as an argv, for spawning it rather than printing it.

    Takes the FOLDER, not the binary: one answer from the artist serves both
    routes, because the caller runs this with `cwd=directory` and thereby makes
    that folder the emulator's working directory -- the same folder the
    `pcsx.lua` shim goes in.

    A list, not a string through a shell: both paths in it are real filesystem
    paths and routinely contain spaces. Quoting them for a shell is a bug
    waiting for the first artist with `Program Files` in the way; not having a
    shell at all is not.
    """
    if not directory:
        raise LiveLinkError(
            "set the PCSX-Redux folder first -- the folder the emulator lives "
            "and runs in")
    binary = find_binary(directory)
    if not binary:
        raise LiveLinkError(
            f"no PCSX-Redux executable in {directory} (looked for "
            + ", ".join(BINARY_NAMES[:3]) + "). Point the preference at the "
            "folder the emulator is in")
    return [binary, "-webserver", "-webserver-port", str(port),
            "-dofile", handlers or HANDLERS]


# --- getting the handlers loaded without a terminal --------------------------
# `-dofile` on the launch line is the reliable route and the one `launch_argv`
# takes. It is not the only one, and the other costs the artist nothing per
# session, which is why it is here.
#
# The emulator's GUI **Lua editor** reads a file called `pcsx.lua` from its
# working directory when the GUI is constructed, and runs it on the pane's first
# draw (`Auto run` defaults on). Measured 2026-08-27 on a plain double-click
# launch with no flags at all: `lua/ping` answered `pong` and `lua/gte` wrote
# two registers.
#
# **The pane has to be visible.** `draw()` is what runs the buffer, and it is
# called only when *Show Lua editor* is ticked -- a setting that persists in the
# emulator's `pcsx.json`. Measured with it off, same file, same directory: the
# emulator was up (`cpu/ram` answered 200) and `lua/ping` was `404 URL Not
# found`. So the setup here writes both halves, and half of it silently
# accomplishes nothing.

#: The name the Lua editor looks for. Not ours to choose.
SHIM_NAME = "pcsx.lua"

#: How a shim we wrote is told from a file the artist owns.
#:
#: Load-bearing, because the Lua editor's `Auto save` also defaults ON: that
#: file is a document the emulator writes back, so it may be the artist's own
#: work. Overwriting it would destroy something they cannot get back, and the
#: only thing standing between us and that is recognising our own handwriting.
SHIM_MARKER = "-- exmateria-map live link"


def shim_text(handlers: str = "") -> str:
    """A `pcsx.lua` that loads the addon's real handler file.

    A two-line shim rather than a copy of the handlers, so the file the
    emulator runs is the file the addon ships: reinstall the addon and the
    handlers change under this without anyone having to remember to re-copy
    them. It also keeps the emulator's `Auto save` away from anything but two
    lines we can rewrite at will.
    """
    return (f"{SHIM_MARKER} -- rewritten by the addon; edit the addon's\n"
            "-- pcsx_handlers.lua instead, which is what this loads.\n"
            f'Support.extra.dofile("{handlers or HANDLERS}")\n')


def install_shim(directory: str, handlers: str = "") -> str:
    """Write the shim into `directory`; return the path. Refuses to clobber.

    A `pcsx.lua` without our marker is the artist's own Lua, kept there by the
    editor's `Auto save` -- and it is the one file in this whole flow that
    holds something the addon cannot regenerate.
    """
    if not directory:
        raise LiveLinkError(
            "set the PCSX-Redux folder first -- it is the folder the emulator "
            "runs from, which is where its Lua editor looks for `pcsx.lua`")
    path = os.path.join(directory, SHIM_NAME)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            existing = f.read()
        if SHIM_MARKER not in existing:
            raise LiveLinkError(
                f"{path} already exists and is not ours. PCSX-Redux's Lua "
                "editor saves that file, so it is probably your own script -- "
                "move it aside, or add this line to it yourself:\n"
                f'    Support.extra.dofile("{handlers or HANDLERS}")')
    with open(path, "w", encoding="utf-8") as f:
        f.write(shim_text(handlers))
    return path


def settings_path() -> str:
    """The emulator's `pcsx.json`, where *Show Lua editor* persists."""
    if os.name == "nt":
        base = os.environ.get("APPDATA", "")
        return os.path.join(base, "pcsx-redux", "pcsx.json") if base else ""
    home = os.path.expanduser("~")
    return os.path.join(home, ".config", "pcsx-redux", "pcsx.json")


def enable_lua_editor(settings: str = "") -> bool:
    """Tick *Show Lua editor* in the emulator's settings. `True` if it changed.

    Editing another application's config file, which is worth being uneasy
    about -- but the shim without this accomplishes exactly nothing, and an
    artist who has to be told "now find this checkbox" has not been spared the
    thing we were sparing them.

    It rewrites one key and leaves the file otherwise as found. The emulator
    saves `pcsx.json` on exit, so a running emulator would discard this; the
    operator that calls it checks for one first.
    """
    path = settings or settings_path()
    if not path or not os.path.exists(path):
        raise LiveLinkError(
            f"no PCSX-Redux settings at {path or '(unknown)'} -- start it "
            "once so it writes them, then press this again")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    gui = data.setdefault("gui", {})
    if gui.get("ShowLuaEditor") is True:
        return False
    gui["ShowLuaEditor"] = True
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return True


#: The longest URL a **stock** pcsx-redux web server will route, in bytes.
#:
#: `BUFFER_SIZE = 256` in `src/core/web-server.cc`, and `onUrl` parses each read
#: chunk as a whole URI instead of accumulating -- so when the request line runs
#: past the first read, the server resolves a truncated path and answers
#: `404 URL Not found`. **The failure mode is a silent 404, not an error**,
#: which is why this is a named refusal here rather than something to discover.
#:
#: Bisected on a live emulator 2026-08-27: a 251-byte URL runs the handler and a
#: 252-byte one 404s. The bound is on the whole request line, not the URL --
#: `POST` (one byte longer than `GET`) moves the cliff to 250, measured -- so the
#: rule is `len(method) + 1 + len(url) <= 255`. 251 is therefore the ceiling for
#: the GETs this module makes, and only for those.
URL_LIMIT = 251


class LuaClient:
    """The emulator's Lua VM over HTTP: `/api/v1/lua/<handler>`.

    Upstream pcsx-redux, not our fork -- `LuaExecutor` and the `/api/v1/lua/`
    prefix are both stock. What the handlers themselves do is ours, and they
    ship with the addon as `pcsx_handlers.lua`:

        pcsx-redux -webserver -webserver-port <N> -dofile pcsx_handlers.lua

    **A handler can only be reached through the URL.** On stock a POST body is
    not exposed to Lua at all, an urlencoded POST arrives with `req.form` empty,
    and a multipart POST hands over the part *headers* with the values
    concatenated. So `call` is a GET and `URL_LIMIT` is the whole payload
    budget; `exec` below, which POSTs a body, works only on the fork.
    """

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.host, self.port = host, port
        self.base = f"http://{host}:{port}/api/v1/lua"

    def call(self, handler: str, query: str = "",
             timeout: float = 30.0) -> str:
        """`GET /api/v1/lua/<handler>?<query>`, the body as text.

        The length check is the point: over `URL_LIMIT` the server does not
        fail, it routes somewhere else and 404s, and a caller that built the
        query from a plan would read that as "the handler is missing".
        """
        url = f"{self.base}/{handler}" + (f"?{query}" if query else "")
        path = url[len(f"http://{self.host}:{self.port}"):]
        if len(path) > URL_LIMIT:
            raise LiveLinkError(
                f"a {len(path)}-byte URL for lua/{handler} is past the "
                f"{URL_LIMIT}-byte ceiling a stock pcsx-redux routes; it would "
                "come back as a silent 404. Split the request")
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body = (e.read() or b"")[:400].decode("utf-8", "replace")
            if e.code == 404:
                raise NoHandlerError(
                    f"pcsx-redux is running on {self.host}:{self.port} but has "
                    f"no `{handler}` Lua handler -- relaunch it with\n    "
                    + launch_command(self.port)) from e
            raise TransportError(f"lua/{handler} {e.code}: {body}") from e
        except (urllib.error.URLError, OSError) as e:
            raise TransportError(
                f"no emulator answering on {self.host}:{self.port} ({e}). "
                "Launch pcsx-redux with -webserver and load a battle.") from e

    def exec(self, code: str, timeout: float = 180.0) -> str:
        """Run arbitrary Lua. **Fork-only** -- stock does not expose the body.

        Kept because `tools/live_*.py` push multi-KB Lua programs that could
        never fit in a URL, and those tools run against the fork on purpose.
        Nothing on the addon's own path calls this: `apply` prefers
        `client.write` and `apply_gte` goes through `call`.
        """
        req = urllib.request.Request(
            self.base + "/exec", data=code.encode("utf-8"), method="POST",
            headers={"Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body = (e.read() or b"")[:400].decode("utf-8", "replace")
            raise TransportError(f"lua/exec {e.code}: {body}") from e
        except (urllib.error.URLError, OSError) as e:
            raise TransportError(
                f"no emulator answering on {self.host}:{self.port} ({e}). "
                "Launch pcsx-redux with -webserver and load a battle.") from e

    def check(self) -> str:
        """`""` when a push can start, otherwise **why not**, for an artist.

        Three states, not two, and the middle one is the point. `-dofile` is
        the step artists forget, and an emulator running without it answers
        every upstream endpoint perfectly and 404s ours -- so a gate that only
        knew "reachable / not reachable" would report "no emulator answering"
        about an emulator that is plainly on their screen, and send them to
        check a port that was never the problem.

        Answered by `ping` rather than by the connection alone for the same
        reason: proving the web server is listening does not prove the light
        rig has a route, and the difference would otherwise surface halfway
        through a push.
        """
        try:
            if "pong" in self.call("ping", timeout=2.0):
                return ""
            return (f"{self.host}:{self.port} answered the live link's ping "
                    "with something else -- is that a pcsx-redux?")
        except NoHandlerError as e:
            return str(e)
        except LiveLinkError:
            return (f"no emulator answering on {self.host}:{self.port} -- "
                    "launch it and load a battle:\n    "
                    + launch_command(self.port))

    def ping(self) -> bool:
        """`check()` as a bool, for callers that only gate on it."""
        return not self.check()

    def read(self, address: int, length: int) -> bytes:
        """`length` bytes of main RAM from `address`. **Fork-only** (`exec`).

        `RamClient.read` is the stock answer and the addon's default; this one
        survives for `tools/live_*.py`.
        """
        o = address - RAM_BASE
        if o < 0 or o + length > RAM_BYTES:
            raise LiveLinkError(
                f"0x{address:08X}+{length} is outside main RAM")
        hexed = self.exec(f'''
local mem = PCSX.getMemPtr()
local t = {{}}
for i = {o}, {o + length - 1} do t[#t+1] = string.format("%02x", mem[i]) end
return table.concat(t)''').strip()
        if len(hexed) != length * 2:
            raise TransportError(
                f"asked for {length} bytes and got {len(hexed) // 2}")
        return bytes.fromhex(hexed)


class RamClient:
    """Main RAM over HTTP -- `GET`/`POST /api/v1/cpu/ram/raw` (#606 part 1).

    The same job as `LuaClient` and none of the encoding. The Lua path hex-codes
    every byte into a string and walks it in the interpreter; this hands raw
    bytes to a bounds-checked `memcpy` and gets the whole 2 MB back in one GET.

    **Both endpoints are upstream pcsx-redux**, not our fork's -- which is the
    point of the port, and why this is the addon's default transport. The light
    rig's GTE half (`apply_gte`, control registers `cnt13-15` / `cnt16-20`) is
    still a `LuaClient`, because those are not `m_wram` and no HTTP endpoint
    reaches them -- but since #606 part 2 it is a GET against a handler that
    ships with the addon, so a session running this for RAM and a `LuaClient`
    for the rig is a session on a **stock** emulator.

    Measured [LIVE] 2026-08-27 on a Gariland battle: A/B/A through this client
    round-trips exactly, and the Lua window sees the same bytes.

    **The trap this class is shaped around.** `POST` takes ONE contiguous run,
    and a geometry plan is thousands of six-byte runs -- a request each would be
    *slower* than the Lua walk. `cluster_writes` collapses a whole plan into a
    handful of requests, which is where the win actually is.
    """

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.host, self.port = host, port
        self.base = f"http://{host}:{port}/api/v1/cpu/ram/raw"
        self._held = None            # the image a `hold()` is answering from
        self._holding = 0            # `hold()` depth

    @contextlib.contextmanager
    def hold(self):
        """Fetch main RAM once for the length of a push, and answer from it.

        The endpoint cannot help: stock's GET always returns the whole 2 MB and
        the `offset`/`size` parameters exist on the POST only
        (`web-server.cc:118-122`), so a push moved roughly 40 MB to write tens
        of kilobytes -- the descriptor read, the packet fields, the per-bucket
        before-images and the self-check, each one a fresh 2 MB
        (ADR-0186 Amendment 7, decision 32).

        A **scope**, not a cache with a lifetime, for two reasons and both are
        correctness rather than taste:

        * The console is RUNNING. An image held past the push it was fetched
          for would answer the next push's descriptor read with the last
          push's RAM.
        * A write DROPS it rather than updating it. Decision 32 says the
          self-check is not traded away for speed, and a write-through image
          would make it compare the plan against the plan -- passing against an
          engine that took none of it. `selfcheck` runs BEFORE the writes, so
          it costs nothing to keep honest: all of its reads share the one
          image, and the first `_post` ends that image's life.
        """
        self._holding += 1
        try:
            yield self
        finally:
            self._holding -= 1
            if not self._holding:
                self._held = None

    def _ram(self) -> bytes:
        """Main RAM -- from the held image when a `hold()` is open."""
        if self._held is not None:
            return self._held
        got = self._get()
        if self._holding:
            self._held = got
        return got

    # -- transport, isolated so the tests can drive a byte array
    def _get(self) -> bytes:
        try:
            with urllib.request.urlopen(self.base, timeout=60.0) as r:
                got = r.read()
        except urllib.error.HTTPError as e:
            raise TransportError(f"ram GET {e.code}") from e
        except (urllib.error.URLError, OSError) as e:
            raise TransportError(
                f"no emulator answering on {self.host}:{self.port} ({e})") from e
        if len(got) != RAM_BYTES:
            raise TransportError(
                f"the RAM read returned {len(got):,} bytes, not {RAM_BYTES:,}")
        return got

    def _post(self, offset: int, data: bytes) -> None:
        req = urllib.request.Request(
            f"{self.base}?offset={offset}&size={len(data)}", data=data,
            method="POST", headers={"Content-Type": "application/octet-stream"})
        try:
            urllib.request.urlopen(req, timeout=60.0).read()
        except urllib.error.HTTPError as e:
            raise TransportError(
                f"ram POST {e.code} for {len(data)} byte(s) at "
                f"0x{RAM_BASE + offset:08X}") from e
        except (urllib.error.URLError, OSError) as e:
            raise TransportError(
                f"no emulator answering on {self.host}:{self.port} ({e})") from e

    def ping(self) -> bool:
        try:
            return len(self._get()) == RAM_BYTES
        except LiveLinkError:
            return False

    def read(self, address: int, length: int) -> bytes:
        """`length` bytes of main RAM from `address`.

        Refused by name outside main RAM rather than left to the endpoint's
        400: the Lua client names the address and the field, and a transport
        swap must not quietly downgrade that to a status code.
        """
        o = address - RAM_BASE
        if o < 0 or o + length > RAM_BYTES:
            raise LiveLinkError(f"0x{address:08X}+{length} is outside main RAM")
        return self._ram()[o:o + length]

    def read_live(self, address: int, length: int) -> bytes:
        """`length` bytes fetched from the CONSOLE, ignoring any held image.

        `read` above answers from `hold()`'s image, which is what makes a push
        one GET instead of twenty. A **readback** cannot use it: sampling one
        image five times is five copies of one instant, so every row reads
        still and a healthy animation install is reported as *not added*
        (`readback_animation`). This is the one read in the module that has to
        cost a fetch, and it neither reads nor populates the held image.
        """
        o = address - RAM_BASE
        if o < 0 or o + length > RAM_BYTES:
            raise LiveLinkError(f"0x{address:08X}+{length} is outside main RAM")
        return self._get()[o:o + length]

    def write(self, writes: list[tuple[int, bytes]]) -> int:
        """Do the writes; return how many bytes actually **changed**.

        The count is the same contract `apply` has always had -- `selfcheck`
        leans on a zero meaning "the engine already holds this" -- and here it
        is free: the GET has already provided the before-image the clustering
        needed anyway.
        """
        if not writes:
            return 0
        image = self._ram()
        changed = 0
        for address, data in writes:
            o = address - RAM_BASE
            changed += sum(1 for a, b in zip(image[o:o + len(data)], data)
                           if a != b)
        for address, data in cluster_writes(writes, image):
            self._post(address - RAM_BASE, data)
        # The console has moved on from the image these writes were planned
        # against, and the next read must be able to say so -- see `hold()`.
        self._held = None
        return changed


#: A packed record is `[8 hex offset][4 hex length][data]`. The length field's
#: width is load-bearing: a length too wide for it silently spills into the
#: data, Lua then reads data bytes as the NEXT record's address, and the write
#: lands wherever those bytes happen to point. Encoded in two hex digits, an
#: 11,552-byte restore formatted as `2d20`, and the walk wrote through main RAM
#: until it ran off the FFI pointer and took the emulator with it. `pack_writes`
#: refuses rather than truncates, and the Lua bounds-checks every record.
RECORD_LENGTH_HEX = 4
RECORD_MAX = (1 << (4 * RECORD_LENGTH_HEX)) - 1        # 65,535
RECORD_HEADER = 8 + RECORD_LENGTH_HEX


def pack_writes(writes: list[tuple[int, bytes]]) -> str:
    """The wire form of a write list, or a refusal."""
    out = []
    for address, data in writes:
        o = address - RAM_BASE
        if o < 0 or o + len(data) > RAM_BYTES:
            raise LiveLinkError(
                f"0x{address:08X}+{len(data)} is outside main RAM")
        if len(data) > RECORD_MAX:
            raise LiveLinkError(
                f"a {len(data)}-byte write at 0x{address:08X} is too long to "
                f"encode in a {RECORD_LENGTH_HEX}-hex length field "
                f"(max {RECORD_MAX}); split it")
        out.append(f"{o:08x}{len(data):0{RECORD_LENGTH_HEX}x}{data.hex()}")
    return "".join(out)


#: How far apart two runs may be and still travel in one request.
#:
#: `POST /api/v1/cpu/ram/raw` takes ONE contiguous run, and a geometry plan is
#: thousands of six-byte runs -- a request each would be slower than the Lua
#: path it replaces, so coalescing is the entire point of this transport. The
#: bytes in a filled gap are read from the before-image and written straight
#: back, so the request is a no-op over them.
#:
#: It is a **collateral bound**, not a tuning knob. Every byte inside a filled
#: gap is read-modify-written, so if the engine changes one between the GET and
#: the POST, the POST puts the stale copy back. That is the one way this
#: transport can be wrong where the hex-encoded Lua walk -- which touched only
#: the bytes it was given -- could not be. 64 keeps the collateral to a few
#: bytes per cluster while still collapsing a whole bucket's plan into a
#: handful of requests.
COALESCE_GAP = 64


def cluster_writes(writes: list[tuple[int, bytes]], image: bytes,
                   gap: int = COALESCE_GAP) -> list[tuple[int, bytes]]:
    """Merge a plan into the fewest contiguous runs worth one request each.

    `image` is a whole-RAM before-image (the endpoint's GET), used to fill the
    gaps between runs with the bytes already there.

    The plan is **sorted first**. It arrives grouped per bucket and per field
    and is therefore unordered; the Lua walk did not care, but a cluster does --
    an unsorted plan produces a negative-length gap and silently truncates.
    """
    if not writes:
        return []
    for address, data in writes:
        o = address - RAM_BASE
        if o < 0 or o + len(data) > RAM_BYTES:
            raise LiveLinkError(
                f"0x{address:08X}+{len(data)} is outside main RAM")

    ordered = sorted(writes, key=lambda w: w[0])
    out: list[tuple[int, bytearray]] = []
    for address, data in ordered:
        if out:
            base, buf = out[-1]
            end = base + len(buf)
            if address - end <= gap and address >= base:
                if address > end:
                    o = end - RAM_BASE
                    buf += image[o:o + (address - end)]
                head = address - base
                buf[head:head + len(data)] = data
                continue
        out.append((address, bytearray(data)))
    return [(a, bytes(b)) for a, b in out]


def apply(client, writes: list[tuple[int, bytes]]) -> int:
    """Do the writes; return how many bytes actually **changed**.

    Transport-agnostic since #606 part 1: it delegates to `client.write` when
    the client has one (`RamClient`), and otherwise runs the packed-Lua walk
    below. Every caller -- geometry, metadata, packets, counts, palettes -- is
    untouched by the swap. `apply_gte` is the one leg that cannot move onto
    `/api/v1/cpu/ram/raw`: the GTE control registers are not `m_wram` and no
    HTTP endpoint reaches them, so it goes through a Lua handler instead.

    The packed-Lua walk below is the **fork** path -- it POSTs its program as a
    request body, which stock does not expose to Lua. It is kept, not
    deprecated: `tools/live_geometry.py`, `tools/live_map.py` and the audits in
    `tests/` construct a `LuaClient` on purpose, and it is the faster of the
    two. Nothing the addon's button does reaches it -- the operator builds a
    `RamClient` unconditionally -- so there is no longer a preference that can
    send an artist down a path their emulator cannot serve.

    Zero is the answer when the caller pushes bytes that are already there,
    and that is the whole point: `selfcheck()` pushes the engine's own bytes
    back over themselves and demands zero. It costs nothing, it runs before
    every real push, and it is the check that catches an off-by-one in a
    stride, a vertex offset or a field mask -- in this module's own
    arithmetic, which is the only place those bugs live.
    """
    if not writes:
        return 0
    writer = getattr(client, "write", None)
    if writer is not None:
        return writer(writes)
    packed = pack_writes(writes)
    return int(client.exec(f'''
local mem = PCSX.getMemPtr()
local p = "{packed}"
local i, changed = 1, 0
while i <= #p do
  local o = tonumber(p:sub(i, i + 7), 16)
  local n = tonumber(p:sub(i + 8, i + 11), 16)
  i = i + {RECORD_HEADER}
  if o < 0 or o + n > {RAM_BYTES} then
    error(string.format("record at %d writes 0x%x+%d, outside main RAM", i, o, n))
  end
  for k = 0, n - 1 do
    local c = tonumber(p:sub(i + k*2, i + k*2 + 1), 16)
    if mem[o + k] ~= c then mem[o + k] = c changed = changed + 1 end
  end
  i = i + n * 2
end
return tostring(changed)''').strip())


def verify(client, writes: list[tuple[int, bytes]]) -> tuple[int, int]:
    """`(bytes that differ, bytes compared)` between RAM and a plan.

    Non-destructive on purpose: the first version of the self-check *wrote*
    to do this, and a check that writes is a check that can damage the thing
    it was inspecting.
    """
    if not writes:
        return 0, 0
    # One read spanning the whole plan, not one per write: a bucket's plan is
    # thousands of six-byte runs, and a round trip each is minutes.
    lo = min(a for a, _ in writes)
    hi = max(a + len(d) for a, d in writes)
    ram = client.read(lo, hi - lo)
    differ = total = 0
    for address, data in writes:
        got = ram[address - lo:address - lo + len(data)]
        differ += sum(1 for a, b in zip(got, data) if a != b)
        total += len(data)
    return differ, total


def selfcheck(client, writes: list[tuple[int, bytes]]) -> None:
    """Demand that RAM already holds what the plan expects.

    Call it with a plan built from the **base map's own** bytes, before
    pushing the document's. It proves the plan's addresses hold the content
    the plan believes they hold, which is what catches an off-by-one in a
    stride, a vertex offset or a field mask.

    What it cannot do alone is say *why* a mismatch happened, and the first
    version pretended otherwise -- it said "the address arithmetic is wrong",
    full stop. But a live push edits exactly these bytes, so after one
    succeeds RAM legitimately differs from the disc and the check fires on a
    perfectly healthy rig. It blamed the rig for having worked. All three
    causes are named now, and the caller decides -- the third being that the
    base is the wrong map's, which is a live possibility precisely because
    decision 2 stopped claiming the loaded map is the declared one.
    """
    differ, total = verify(client, writes)
    if differ:
        raise LiveLinkError(
            f"write-path self-check FAILED: {differ:,} of {total:,} byte(s) at "
            f"the planned addresses do not hold the base map's own bytes. "
            "Either this module's address arithmetic is wrong, or the loaded "
            "map is not the base's map, or the map has **already been pushed "
            "to** -- a live push edits these very bytes and does not survive a "
            "map reload, so reload the savestate to get back to the disc's. "
            "Nothing was written.")


#: Fields whose match at a COMPUTED address proves this module's arithmetic.
#: `positions` and `metadata` share the position array's base; a plan that
#: reproduces the document's own bytes there, to the byte, cannot have been
#: built from a wrong stride, a wrong vertex offset or a wrong map.
PROVING_FIELDS = ("positions", "metadata")


def diagnose_selfcheck(results: dict) -> tuple[bool, list[str]]:
    """Read the WHOLE self-check before saying what it means.

    `results` is `{(bucket, field): (matched, differ, total)}`, where `matched`
    names the candidate RAM was found to hold, or is `None`.

    This used to raise on the first plan whose candidates both mismatched, and
    it iterated in sorted order -- `metadata`, `normals`, `positions`. So an
    emulator holding a previous session's baked normals failed at `normals` and
    reported *"the loaded map is not this document's map"* while holding, three
    keys later and unexamined, the proof that it is. Measured on a live
    battle: positions **0** of 8,664 differ, metadata **0** of 1,444, normals
    **7,589** of 8,664. Three fields, three different stories, and the
    pessimistic one won on alphabetical order.

    So: every plan is evaluated, and the PATTERN decides.

    * everything matched -> pass, naming what was found.
    * only `normals` differ, with every `positions` and `metadata` plan exact
      -> **pass with a warning**. The arithmetic is proven by the fields that
      matched, and lighting differing on top of it means somebody pushed a
      bake here. That is the one cause of a mismatch that is harmless, and
      refusing it walls the artist out completely: `_LAST_PUSH` is recorded
      only on a SUCCESSFUL push, so a refusal can never establish the memory
      that would let the next press through. The only way out was reloading a
      savestate that was never the problem.
    * anything else -> refuse, reporting every plan rather than the first.
    """
    unmatched = {k: v for k, v in results.items() if v[0] is None}
    if not unmatched:
        found = sorted({v[0] for v in results.values()})
        return True, [f"the planned addresses hold {' and '.join(found)}"]

    proven = all(v[0] is not None for k, v in results.items()
                 if k[1] in PROVING_FIELDS)
    if proven and all(k[1] == "normals" for k in unmatched):
        differ = sum(v[1] for v in unmatched.values())
        total = sum(v[2] for v in unmatched.values())
        return True, [
            f"this emulator was ALREADY PUSHED TO -- by another session, not "
            f"this one. {differ:,} of {total:,} `normals` byte(s) differ, while "
            f"every position and binding byte at the same computed addresses "
            f"is exact, so the map is this document's and the rig's addresses "
            f"are right. Something pushed lighting here (a bake, a previous "
            f"Blender). Pushing will overwrite it, which is what you asked "
            f"for; reload the savestate first if you wanted the disc's."]

    rows = "; ".join(
        f"{b} {f} {v[1]:,}/{v[2]:,}" + ("" if v[0] is None else " (ok)")
        for (b, f), v in sorted(results.items()))
    return False, [
        "write-path self-check FAILED. Bytes at the planned addresses that "
        "hold neither the imported document's own geometry nor anything this "
        f"session pushed, per plan: {rows}. Positions differing is the case "
        "this check exists for: either the loaded map is not this document's "
        "map, or something else pushed to this emulator (reload the "
        "savestate), or this rig's address arithmetic is wrong. Nothing was "
        "written."]


#: Which of a bucket's arrays each planned field is written into. `metadata`
#: is not a third array -- it is bytes 6-7 of the POSITION array's vertices --
#: so it shares `positions`' extent, and a table that gave it one of its own
#: would be inventing an array the engine does not have.
PLAN_ARRAY = {"positions": "positions", "metadata": "positions",
              "normals": "normals"}


def array_extent(bucket: str, field: str) -> tuple[int, int]:
    """`[base, end)` in main RAM of the array `(bucket, field)` is written to.

    `end` is the array's own end, from ADR-0004 decision 28's capacities --
    not the loaded map's slice. A slice is what a descriptor declares and it
    moves per map; the array is the engine's, fixed, and is the thing a write
    must not leave.
    """
    base = getattr(SINKS[bucket], PLAN_ARRAY[field])
    if base is None:
        raise LiveLinkError(
            f"{bucket} has no {field} array -- it is unlit by construction.")
    return base, base + ENGINE_CAPACITY[bucket] * POLYGON_STRIDE[bucket]


def check_plan_bounds(plans: dict[tuple[str, str], list]) -> list[str]:
    """Prove every planned address lands inside the array it names.

    The self-check for the mode where RAM does NOT hold the document's bytes.
    `selfcheck` proves the addresses by their CONTENT, which is the strongest
    thing this build can say and is unavailable the moment the point is to
    replace what is loaded. This is what is left: the addresses are still
    checked, against the one fact about them that does not depend on which map
    is loaded -- the engine's arrays are fixed-capacity and engine-global
    (ADR-0004 decision 28), so a write outside one is corruption whatever is
    in RAM.

    Say plainly what it does not do. A wrong stride, a wrong vertex offset or
    a wrong field mask that happens to stay inside the array passes this and
    would have failed `selfcheck`. It is a bound, not a proof of correctness.
    """
    proved = []
    for (bucket, field), writes in sorted(plans.items()):
        if not writes:
            continue
        base, end = array_extent(bucket, field)
        lo = min(a for a, _ in writes)
        hi = max(a + len(d) for a, d in writes)
        proved.append((bucket, field, lo, hi,
                       sum(len(d) for _, d in writes)))
        if lo < base or hi > end:
            raise LiveLinkError(
                f"bounds proof FAILED: the {bucket} {field} plan writes "
                f"[0x{lo:08X}, 0x{hi:08X}) and that array is "
                f"[0x{base:08X}, 0x{end:08X}) -- "
                f"{ENGINE_CAPACITY[bucket]} slots of "
                f"{POLYGON_STRIDE[bucket]} bytes. Writing outside it is not a "
                "wrong picture, it is memory corruption. Nothing was written.")
    if not proved:
        return ["bounds proof: NOTHING was planned, so nothing was bounded"]
    return ["bounds proof: "
            + "; ".join(f"{b} {f} {n:,} byte(s) into "
                        f"[0x{lo:08X}, 0x{hi:08X})"
                        for b, f, lo, hi, n in proved),
            "  this is WEAKER than the content self-check it replaced, and "
            "the difference is not cosmetic: it proves only that the writes "
            "land inside the engine's arrays. A wrong stride, a wrong vertex "
            "offset or a wrong field mask that stays inside one passes here "
            "and would have been caught there. Nothing checked WHICH map is "
            "loaded -- replacing it is what you asked for"]


def read_descriptor_block(client: LuaClient) -> bytes:
    """The whole block, verbatim, for `check_descriptors`."""
    return client.read(DESCRIPTOR_BASE, DESCRIPTOR_STRIDE * DESCRIPTOR_COUNT)


def plan_document(descriptor: Descriptor,
                  document: dict) -> dict[tuple[str, str], list]:
    """Every `(bucket, field)` of the document that has a live sink.

    Decision 4: push what has a sink and **name** what was skipped, never
    refuse. `untextured_*` normals are skipped because the buckets are unlit
    by construction, and `terrain` is skipped because its sink is not located
    yet -- neither yields a *wrong* picture, only a stale one, and a refusal
    would block the artist on a mismatch they cannot see.

    Nothing is written here. The whole plan is built, and every count checked
    against the loaded map, before a single byte moves.
    """
    by_bucket: dict[str, list[dict]] = {b: [] for b in BUCKETS}
    for poly in document.get("polygons", ()):
        by_bucket[poly["kind"]].append(poly)

    plans: dict[tuple[str, str], list] = {}
    for bucket in BUCKETS:
        polys = by_bucket[bucket]
        if not polys:
            continue
        for field in FIELDS:
            if SINKS[bucket].normals is None and field == "normals":
                continue
            if any(field not in p for p in polys):
                continue
            plans[(bucket, field)] = plan(
                descriptor, bucket, field, [p[field] for p in polys])
        # Bytes 6-7 are not a per-vertex field and are not optional: they are
        # DERIVED from the document on every polygon of every bucket, every
        # push. `plan_metadata` has why -- a re-slot moves positions, normals
        # and the packet and would leave these two shorts behind, and the
        # survivor would wear the previous occupant's VISIBLE_ANGLES.
        plans[(bucket, "metadata")] = plan_metadata(descriptor, bucket, polys)
    return plans


# --- the aim (decision 9) ---------------------------------------------------

class Aim(NamedTuple):
    """The map state an act is pointed at, and the rows that hold its picture.

    Keyed `(night, weather, kind)` -- never an index. `(night, weather)` names
    a GROUP of 1-4 rows rather than a row (634 of the corpus's 774 groups are
    one TEXTURE row plus one mesh row), and the group is where the picture
    splits: the sheet's pixels are the TEXTURE row's, the palettes and the rig
    are the mesh row's. So one aim resolves to TWO rows.
    """

    night: int
    weather: int
    kind: int
    rig_row: dict | None
    sheet_row: dict | None
    palette_row: dict | None


def aim(states: list[dict], index: int) -> Aim:
    """Resolve the previewed row to the rows an act reads from."""
    row = states[index]
    group = [s for s in states
             if s["night"] == row["night"] and s["weather"] == row["weather"]]
    return Aim(night=row["night"], weather=row["weather"], kind=row["kind"],
               rig_row=next((s for s in group if s.get("light_rig")), None),
               sheet_row=next((s for s in group if s.get("texture_sheet")),
                              None),
               palette_row=next((s for s in group if s.get("palettes")), None))


def also_moved(states: list[dict], at: Aim) -> dict[str, list[tuple[int, int]]]:
    """The other `(night, weather)` groups an aimed push moves with it.

    ADR-0004 decision 27's rule -- *every state the bake touched is NAMED in
    its report* -- carried to the push, which needs it more, because what it
    moves is shared four different ways: the sheet by every state naming the
    same sidecar, the palettes by every state naming the same resource (`build`
    refuses two rows naming one resource with differing palettes), the rig by
    the state alone, and `normals` by every state in the arrangement.

    Reported per datum rather than as one set: they are shared on different
    keys, so a single list would be right about none of them.
    """
    out: dict[str, list[tuple[int, int]]] = {}
    for field, row in (("texture_sheet", at.sheet_row),
                       ("palettes", at.palette_row)):
        if row is None:
            continue
        key = "texture_sheet" if field == "texture_sheet" else "resource"
        out[field] = sorted({(s["night"], s["weather"]) for s in states
                             if s.get(key) == row[key]
                             and (s["night"], s["weather"])
                             != (at.night, at.weather)})
    return out


# --- the light rig (decision 9's atom, docs/live-link-v1.md §2.2) -----------

#: The three RAM homes of a map state's light rig, and where each one goes.
#: All three were predicted from the disc before they were read, and all three
#: were then A/B/A'd on a live Gariland battle (§2.2).
RIG_GAINS = 0x800F5AF4          # 18 B, PLANAR -> GTE LCM (cnt16-20)
RIG_DIRECTIONS = 0x800F5B14     # 18 B, interleaved -> GTE LLM (cnt8-12)
RIG_AMBIENT = 0x800F5B40        # 3 x int32 -> GTE BK (cnt13-15), each x16

#: GTE control registers. `SetColorMatrix` (0x8001D108) packs `m[3][3]` two
#: shorts to a word across `cnt16-20`; `SetBackColor` (0x8001D168) is
#: `sll aN, aN, 4` then `ctc2` into `cnt13-15` -- which is the x16.
GTE_COLOR_MATRIX = 16
GTE_BACK_COLOR = 13
BACK_COLOR_SHIFT = 4


def _rig_triples(rig: dict, key: str, lo: int, hi: int) -> list[list[int]]:
    rows = rig.get(key)
    if not isinstance(rows, list) or len(rows) != 3:
        raise LiveLinkError(f"light_rig.{key} holds 3 triples, not {rows!r}")
    for row in rows:
        if not isinstance(row, list) or len(row) != 3:
            raise LiveLinkError(f"light_rig.{key} entry is not 3 values: {row!r}")
        for value in row:
            if not isinstance(value, int) or not lo <= value <= hi:
                raise LiveLinkError(
                    f"light_rig.{key} value {value!r} is outside {lo}..{hi}")
    return rows


def _rig_bytes(rig: dict) -> tuple[bytes, bytes, list[int]]:
    """The rig as the FILE holds it: planar gains, interleaved directions.

    This is `mapfile.pack_light_rig`'s first 39 bytes, re-derived because the
    addon ships without the `exmateria_map` package (ADR-0005 decision **2**
    keeps this module `bpy`-free; there is no decision 6). `tests/
    test_live_link.py` asserts the two agree byte for byte, so the second copy
    cannot drift silently. ADR-0004 decision 31 vendors the package and so
    retires the reason for this copy -- collapsible once that is built.
    """
    colors = _rig_triples(rig, "colors", -0x8000, 0x7FFF)
    directions = _rig_triples(rig, "directions", -0x8000, 0x7FFF)
    ambient = rig.get("ambient")
    if (not isinstance(ambient, list) or len(ambient) != 3
            or any(not isinstance(v, int) or not 0 <= v <= 0xFF
                   for v in ambient)):
        raise LiveLinkError(f"light_rig.ambient is not 3 bytes: {ambient!r}")
    # PLANAR: light i's channel c at c*6 + i*2 -- all three reds, then greens,
    # then blues. That is already the GTE colour matrix's order, which is why
    # the gains need no transposition and the directions do not either.
    gains = struct.pack("<9h", *(colors[i][c] for c in range(3)
                                 for i in range(3)))
    dirs = struct.pack("<9h", *(v for row in directions for v in row))
    return gains, dirs, ambient


def plan_rig(rig: dict) -> list[tuple[int, bytes]]:
    """The RAM half of the rig atom: 48 bytes at three addresses.

    39 bytes on disc, 48 in RAM -- the ambient widens from `[u8 x 3]` to three
    32-bit words. The 6 gradient bytes stay out: ADR-0004 decision 27 has them
    read-only and echoed verbatim, so the target is the 39 the solve owns.

    **This is half a rig on its own.** Only the directions reach the picture
    from RAM; the gains and the ambient are loaded into GTE control registers
    at map load and were not seen to re-load -- measured across seven seconds
    and a dialogue transition in one battle, which is NOT the same as "nothing
    reloads them" (§2.2's scope note). `plan_rig_gte` is the other
    half, and pushing one without the other produces this state's angles over
    the last-loaded state's brightness -- a rig belonging to no real state.
    """
    gains, dirs, ambient = _rig_bytes(rig)
    return [(RIG_GAINS, gains),
            (RIG_DIRECTIONS, dirs),
            (RIG_AMBIENT, struct.pack("<3i", *ambient))]


def plan_rig_gte(rig: dict) -> list[tuple[int, int]]:
    """The GTE half: `(control register, 32-bit value)`, eight of them.

    The light DIRECTION matrix is deliberately absent. The render dispatch
    recomposes it with the camera rotation into `0x800F7E34` and re-loads
    `cnt8-12` every frame, so the RAM write in `plan_rig` reaches it on its
    own -- writing the register too would be overwritten on the next frame and
    would hide that fact from anyone reading this list.
    """
    gains, _dirs, ambient = _rig_bytes(rig)
    m = struct.unpack("<9h", gains)
    words = [(m[0], m[1]), (m[2], m[3]), (m[4], m[5]), (m[6], m[7]), (m[8], 0)]
    out = [(GTE_COLOR_MATRIX + i, (lo & 0xFFFF) | ((hi & 0xFFFF) << 16))
           for i, (lo, hi) in enumerate(words)]
    out += [(GTE_BACK_COLOR + i, v << BACK_COLOR_SHIFT)
            for i, v in enumerate(ambient)]
    return out


def gte_queries(writes: list[tuple[int, int]],
                budget: int = URL_LIMIT - len("/api/v1/lua/gte?")
                ) -> list[str]:
    """A write list as the fewest `index=value` query strings that fit.

    Split by **measured length**, not by a pair count: a pair is 3 to 13 bytes
    wide depending on the value, so any fixed count is either wasteful or --
    the direction that matters -- occasionally over the ceiling, where the
    answer is a silent 404. Today's rig is eight registers in one request; this
    is what keeps a rig that grows to fifty a slower push rather than a mystery.
    """
    out: list[str] = []
    for index, value in writes:
        pair = f"{index}={value}"
        if out and len(out[-1]) + 1 + len(pair) <= budget:
            out[-1] += "&" + pair
        else:
            out.append(pair)
    return out


def apply_gte(client: LuaClient, writes: list[tuple[int, int]]) -> int:
    """Write GTE control registers. A different transport from `apply`.

    `apply` walks main RAM; these are cop2 control registers, which live in
    `PCSX.getRegisters().CP2C`. Keeping them apart is the honest shape: a RAM
    write survives in the map's data and a register write does not, so the two
    have different lifetimes and a caller should know which it made.

    `GET /api/v1/lua/gte?<index>=<u32>&...`, against the `gte` handler in the
    addon's own `pcsx_handlers.lua`. It used to POST Lua source, which is the
    one thing a stock pcsx-redux cannot receive; the guards below are older
    than the transport and are what make the query string safe to build.

    The handler's reply is the count it wrote, and it is checked. A value it
    cannot parse -- a negative, anything not `%d+` -- is skipped there in
    silence, so an unchecked count is the difference between a rig that failed
    and a rig that half-applied and looked fine.
    """
    for index, value in writes:
        if not 0 <= index <= 31:
            raise LiveLinkError(f"cop2 control register {index} does not exist")
        if not 0 <= value <= 0xFFFFFFFF:
            raise LiveLinkError(f"0x{value:X} is not a 32-bit value")
    if not writes:
        return 0
    written = 0
    for query in gte_queries(writes):
        reply = client.call("gte", query).strip()
        try:
            written += int(reply)
        except ValueError:
            raise TransportError(
                f"lua/gte answered {reply[:80]!r}, not a count") from None
    if written != len(writes):
        raise TransportError(
            f"lua/gte wrote {written} of {len(writes)} register(s)")
    return written


def packet_witnesses(client, descriptor: Descriptor,
                     document: dict) -> list[tuple[int, int, int, int]]:
    """What `live_vram.derive_addresses` needs, read from the live packets.

    One `(live_clut, live_tpage, doc_palette_id, doc_texture_page)` per
    textured polygon. The engine's halfwords carry the VRAM addresses it is
    rendering FROM; the document says which palette and page each polygon
    means. Subtracting the second from the first is what locates the sheet and
    the CLUT block without reading a single pixel -- and it is 385 independent
    witnesses on MAP022 a0, which is what makes the answer knowledge.

    Both textured buckets, always. 361 of those 385 are quads, so a witness set
    drawn from triangles alone would happily agree about a layout that the bulk
    of the map disagreed with.
    """
    live, = struct.unpack("<I", client.read(PACKET_BASE_POINTER, 4))
    check_packet_base(live)

    by_bucket: dict[str, list[dict]] = {b: [] for b in BUCKETS}
    for poly in document.get("polygons", ()):
        by_bucket[poly["kind"]].append(poly)

    out = []
    for bucket in PACKET_LAYOUT:
        polys = by_bucket[bucket]
        if not polys:
            continue
        sink, lay = SINKS[bucket], PACKET_LAYOUT[bucket]
        i = BUCKETS.index(bucket)
        stride, start = sink.packet_stride, descriptor.starts[i]
        current = client.read(live + sink.packet + start * stride,
                              stride * max(descriptor.counts[i], len(polys)))
        for p, poly in enumerate(polys):
            if "palette_id" not in poly or "texture_page" not in poly:
                continue
            clut, = struct.unpack_from("<H", current, p * stride + lay.clut)
            tpage, = struct.unpack_from("<H", current, p * stride + lay.tpage)
            out.append((clut, tpage, poly["palette_id"], poly["texture_page"]))
    return out


# --- the palettes (decision 2's other half of the sheet atom) ---------------

#: The 16x16 CLUT block's home in **main RAM**, and the reason this leg is here
#: rather than in `live_vram.py`.
#:
#: The obvious sink is wrong. A map state's palettes ARE VRAM's CLUT rows at
#: y=480 -- `live_clut_halfword - doc_palette_id` is 0x7800 on 385 of 385
#: polygons, and 0x7800 decodes to exactly that row. But VRAM is not where they
#: LIVE. Measured [LIVE] on a Gariland battle, 2026-08-26, by writing a row and
#: reading it back at four delays:
#:
#:     VRAM  (x=80, y=480)   written, verified 0/32 differ, and back to its
#:                           ORIGINAL bytes 50 ms later -- and at 0.2 s, and 1 s
#:     RAM   0x800E4EA4+160  written, still 0/32 differ a full second later,
#:                           and VRAM's row 5 moved to match within 0.3 s
#:
#: The same experiment on the texture SHEET holds in VRAM indefinitely, which
#: is what makes this a property of the palettes rather than of the endpoint:
#: the engine re-uploads this block every frame and does not re-upload the
#: sheet. So a push aimed at VRAM's CLUT rows is a push that works for one
#: frame -- long enough to verify, short enough that the artist never sees it.
#:
#: This block matched the live VRAM CLUT block **0 of 512 bytes different**,
#: and differs from `MAP022.9`'s own 0x44 chunk by 35 bytes over rows 0, 7, 8,
#: 10, 13 and 14 -- which is why no disc resource matches all 16 live rows.
#:
#: **Corrected 2026-08-27 (decision 11).** Only rows 13, 14 and 15 of those six
#: are the animation. `MAP022.9`'s 0x6c table carries exactly three palette
#: records and they name 13/14/15 and nothing else, and a 2.5 s readback finds
#: those three moving and the other thirteen still. Whatever moved rows 0, 7, 8
#: and 10 off the disc's bytes did it ONCE and is unidentified. `0x70` HAS a
#: reader now (#624): `mapfile.read_palette_animation` for the frames and
#: `read_animation_instructions` for the table saying which rows they drive.
#:
#: **This is block 0 of FOURTEEN**, not a 512-byte block. The engine's loader
#: addresses it as `CLUT_BLOCK + block*512` and `clut_view_strip_init`
#: (`0x80093048`) initialises 14 of them; `flush_clut_view_strip`
#: (`0x80092F98`) uploads all 7,168 bytes as one 256x14 rectangle at VRAM
#: (0, 494), gated on the dirty flag `DAT_800995EC`. Measured [LIVE]: blocks
#: 1-13 against VRAM rows 495-507 are 0 of 512 bytes different each, and VRAM
#: (0, 480) -- the line the polygons sample -- is 0 of 512 against block 0.
#: Nothing here writes past block 0, and `clut_rows` refuses a row >= 16, but a
#: future caller that takes `CLUT_BLOCK + n` for a row offset would land in
#: another block rather than off the end of anything.
CLUT_BLOCK = 0x800E4EA4
CLUT_ROWS = 16
CLUT_ENTRIES = 16
CLUT_ROW_BYTES = CLUT_ENTRIES * 2
CLUT_BLOCK_BYTES = CLUT_ROWS * CLUT_ROW_BYTES

#: The map's **0x44 palette chunk, as the loader left it in RAM** -- the other
#: block a content scan for these 512 bytes finds. Pushing here does not reach
#: the screen (measured: writing row 5 moved 0 of 32 VRAM bytes), so `CLUT_BLOCK`
#: above is the sink and this is not.
#:
#: It was first recorded here as an "inert twin", and that was wrong about WHAT
#: it is while right about what it does. It is not a stale duplicate: each
#: animated entry is written into **both** blocks, entry by entry -- this one at
#: `+0x10`, `CLUT_BLOCK` at `+0x00` of the same loop body. Confirmed by
#: watchpoint, 20 and 60 hits, single writer each.
#:
#: **Corrected 2026-08-27 (decision 11): that loop body is NOT the animation.**
#: It is `clut_strip_load_base` (`0x80092620`), a shared CLUT loader taking
#: (source, block, row), and it has exactly two callers -- both inside
#: `color_field_dispatch` (`0x800926D8`), the `{33}` Color Field opcode
#: handler, which CONTEXT.md classifies as a MODULATOR. `ra = 0x80092794` is
#: the instruction after that function's single-row `jal`, so the watchpoint
#: found the HELPER, one frame below whoever called it. The animation is one of
#: `color_field_dispatch`'s 24 call sites and has not been identified. Every
#: byte measurement above stands; the name attached to them did not.
#:
#: Why a push here is still ineffective: nothing re-uploads a STATIC row from
#: either block after map load. `CLUT_BLOCK` reaches VRAM continuously, so a
#: write there shows up within ~0.3 s; this block is only ever read as the
#: animation's own source. Anything that later wants to change an ANIMATED row
#: for more than one frame has to deal with both, which is #624's problem.
CLUT_BLOCK_BASE_COPY = 0x80099D76

#: The old name, kept briefly because it appears in a shipped docstring and in
#: #606's comment thread. It described the address correctly and the thing
#: incorrectly.
CLUT_BLOCK_INERT_TWIN = CLUT_BLOCK_BASE_COPY

#: Rows the engine repaints on its own, measured by writing all 16 and reading
#: back. Used ONLY to keep the pre-write check from refusing on them (they
#: legitimately differ between a RAM read and a VRAM read taken moments apart).
#:
#: Independently confirmed since (#624): `MAP022.9`'s 0x6c palette records name
#: 13/14/15 and no others, so the set is now DERIVABLE from the map. It is left
#: as a measured constant on purpose -- decision 3 wants the push to report what
#: did not hold, never to predict it -- but the old reason for that rule ("the
#: period is unknown") is retired: the period is byte 17 of the record, and the
#: slowest palette record in the corpus is 30 ticks, so a 0.6 s dwell cannot
#: miss one. See decision 11.
#: Decision 3: the push does not PREDICT which rows will not hold -- it writes,
#: reads back, and names whatever did not. This list is what the check
#: tolerates, never what the report is built from.
CLUT_ANIMATED_MEASURED = (13, 14, 15)


def clut_rows(palettes) -> list[tuple[int, bytes]]:
    """`map_states[].palettes` as `(row index, BGR555 bytes)`, one per declared
    row -- the packing, with no address in it.

    Decision 10: **push only what the document declares.** `None` plans
    nothing -- 38.5% of corpus states carry no palettes of their own and render
    with a keyed partner's, so a null is a normal document, and refusing the
    whole press for one would strand a state whose SHEET is perfectly pushable.
    A short row yields only the entries it has, for the same reason: what it
    does not declare is not ours to zero (#496 -- zero is the worst fill).

    A row at a time rather than one 512-byte blob because a row is the unit
    everything else here happens to: a refusal names one, the readback reports
    one, and the engine's animation overwrites one.

    **This is separate from `plan_palettes` because the palettes have two
    sinks, not one.** `plan_palettes` aims these rows at `CLUT_BLOCK` in main
    RAM and `live_vram.plan_clut` aims the SAME rows at VRAM's CLUT column;
    both are needed and which one reaches the screen depends on the map (see
    `plan_palettes`). Packing them twice would be two chances to write
    different colours for one document field, and the divergence would surface
    as *"the palettes are wrong on some maps"* -- the hardest possible symptom
    to trace back to a planner.
    """
    if not palettes:
        return []
    out = []
    for i, row in enumerate(palettes):
        if not row:
            continue
        row = _clut_words(row, i)
        if i >= CLUT_ROWS:
            raise LiveLinkError(
                f"this document declares CLUT row {i}, and a map has "
                f"{CLUT_ROWS}. Writing past the block would land in whatever "
                "follows it in RAM")
        if len(row) > CLUT_ENTRIES:
            raise LiveLinkError(
                f"CLUT row {i} declares {len(row)} entries and a row holds "
                f"{CLUT_ENTRIES}")
        out.append((i, b"".join(int(w).to_bytes(2, "little") for w in row)))
    return out


def plan_palettes(palettes) -> list[tuple[int, bytes]]:
    """The RAM half of the palette push: `clut_rows` aimed at `CLUT_BLOCK`.

    **This sink is correct on 42 resources of 169 and inert on the other 127**,
    and that is the whole shape of this leg. `CLUT_BLOCK` reaches VRAM because
    a map that animates runs the path that sets the dirty flag `DAT_800995EC`,
    and `flush_clut_view_strip` uploads the RAM block whenever that flag is set
    -- so on a map whose `0x70` chunk carries an animation a write here is
    durable and wins over anything written to VRAM directly. (The entry-by-entry
    writes are `clut_strip_load_base`, into main RAM; the RAM-to-VRAM upload is
    the flush. Stating it as "the animation re-uploads it" folded two functions
    into one and is corrected under decision 11 -- the net 42/127 behaviour is
    unchanged, but the split is a property of the FLAG being set, which is why
    the 127 have a candidate remedy and not just a limitation.) On a map without one, nothing
    re-uploads the block after map load and a write here is byte-perfect and
    invisible. Measured [LIVE] 2026-08-27 on Orbonne (`MAP062.8`, no
    animation): this block matched the document 0 of 512 bytes off while all
    16 VRAM CLUT rows still held Orbonne's, and nothing ever moved them.

    So the caller pushes **both** sinks (`live_vram.plan_clut` is the other),
    and neither is a fallback for the other: on the 42 this one wins the next
    frame, on the 127 the VRAM one is the only thing the artist can see. See
    `docs/live-link-v1.md` §2.3.
    """
    return [(CLUT_BLOCK + i * CLUT_ROW_BYTES, data)
            for i, data in clut_rows(palettes)]


def _clut_words(row, index: int) -> list[int]:
    """One CLUT row as BGR555 words, from either shape it can arrive in.

    `map_states[].palettes` is the DOCUMENT's `{"colors": ["#RRGGBB" x 16],
    "stp": u16}` (schema §6.4); `mapfile.read_palettes` hands back the disc's
    raw words. The push is driven from a document, and the corpus tools and the
    live probes hold the other, so both are taken here rather than pushing the
    conversion out to whichever caller is least tested.

    The hex form is converted by the **vendored writer `build` itself uses**,
    not by a second implementation of the packing. A CLUT that reached RAM
    through different arithmetic than the one that reaches the disc would make
    the loupe lie in exactly the way it exists to prevent.
    """
    if isinstance(row, dict):
        try:
            return clut_from_json(row)
        except (KeyError, ValueError, TypeError) as e:
            raise LiveLinkError(
                f"CLUT row {index} is a document palette entry but not a valid "
                f"one ({e}). The schema is "
                '{"colors": ["#RRGGBB" x 16], "stp": 0..65535}') from e
    try:
        return [int(w) for w in row]
    except (TypeError, ValueError) as e:
        raise LiveLinkError(
            f"CLUT row {index} is neither a document palette entry "
            '({"colors": [...], "stp": N}) nor a list of BGR555 words: '
            f"{row!r:.80}") from e


def check_clut_block(ram: bytes, vram_clut: bytes) -> None:
    """Refuse unless `CLUT_BLOCK` is really feeding the CLUT rows on screen.

    Decision 5's locate-by-verify, at the one address on this leg that a
    content scan cannot settle: the same 512 bytes appear TWICE in main RAM,
    and the other one (`CLUT_BLOCK_BASE_COPY`, the map's 0x44 chunk as the
    loader left it) does not reach the screen -- a push into it reports a
    healthy changed-byte count and moves nothing at all. So the address is not
    trusted for being written down; the block it names is checked against what
    the GPU is actually showing.

    The engine-animated rows are excluded, and that is not a loophole. They
    differ between a RAM read and a VRAM read taken microseconds apart because
    the engine repaints them in between -- comparing them would make this check
    fail at random on a perfectly healthy rig, which is the one way to make a
    guard worth ignoring.
    """
    if len(ram) < CLUT_BLOCK_BYTES or len(vram_clut) < CLUT_BLOCK_BYTES:
        raise LiveLinkError(
            f"the CLUT check needs {CLUT_BLOCK_BYTES} bytes from each side and "
            f"got {len(ram)} / {len(vram_clut)}")
    differ = []
    for row in range(CLUT_ROWS):
        if row in CLUT_ANIMATED_MEASURED:
            continue
        o = row * CLUT_ROW_BYTES
        n = sum(1 for a, b in zip(ram[o:o + CLUT_ROW_BYTES],
                                  vram_clut[o:o + CLUT_ROW_BYTES]) if a != b)
        if n:
            differ.append(f"row {row} ({n}/{CLUT_ROW_BYTES} B)")
    if differ:
        raise LiveLinkError(
            f"0x{CLUT_BLOCK:08X} does not hold the palettes the GPU is showing: "
            + ", ".join(differ) +
            ". This is not the map this module was built against, or the block "
            f"has moved -- the map's 0x44 chunk as loaded sits at "
            f"0x{CLUT_BLOCK_BASE_COPY:08X} and a push into THAT one does not "
            "reach the screen. Refusing rather than writing there")


# ---------------------------------------------------------------------------
# The animation table (decision 11)
# ---------------------------------------------------------------------------
# A swap writes a new map's geometry, packets, sheet and palettes into slots
# the host map was using. What it does NOT displace is the host's `0x6c`
# instruction table, which the engine keeps walking every frame -- so a correct
# push gets repainted 4.49 times a second by a map that was supposed to be
# gone. That is the reported "one chunk got the blue water palette and it's
# animated", and the unit of the fix is the TABLE, not the palettes: 60 of the
# corpus's 110 tables drive CLUT rows and 94 drive TEXTURE regions, and
# Gariland's own eight texture records point at `x = 839..923`, inside the four
# VRAM pages a swap has just uploaded a sheet to.


#: The **live** `0x6c` instruction table -- 32 records of 20 bytes, in disc
#: layout, at a fixed engine address. Confirmed two ways:
#:
#: - offline, against `reference-assets/thief_whats_this.sstate`: these 640
#:   bytes are `MAP022.9`'s `0x6c` chunk, differing only at the four runtime
#:   bytes of its three running palette records;
#: - live [LIVE] 2026-08-27, by a one-byte reversible poke with the record's
#:   own siblings as the control -- zeroing record 0 took row 13 from 4.49
#:   steps a second to 0.00 while rows 14 and 15 stayed at 4.5.
#:
#: The poke is what earns the address. A SECOND structure at `0x800F6DC4` (24
#: byte stride) repeats each record's leading 8 bytes, and inspection cannot
#: tell the two apart -- three sessions have landed on the wrong side of that.
#: Hence `check_animation_table` below: the address is never trusted for being
#: written down.
ANIM_TABLE = 0x80121D7C
ANIM_TABLE_BYTES = _mapfile.ANIM_INSTRUCTION_BYTES          # 640

#: The loaded `0x70` frames, 512 B, byte-identical to the disc's chunk in a
#: running battle (verified against the savestate). The animation reads its
#: colours from here, so installing a pushed map's animation is these bytes
#: plus the records above.
ANIM_FRAMES = 0x800F687C
ANIM_FRAMES_BYTES = _mapfile.PALETTE_ANIM_BYTES             # 512

#: The bytes of a RUNNING palette record that the engine owns: a frame cursor
#: and a tick counter, not the map's data. `MAP022.9`'s records read
#: `04 .. 00 .. 00 00` on the disc and `81 .. 02 .. 09 01` in a battle, at
#: exactly these four offsets and nowhere else in all 640 bytes.
ANIM_RUNTIME_BYTES = (14, 16, 18, 19)

#: Byte 19 is the one the ENGINE needs set for a record to run, and the disc
#: ships it CLEAR on 127 of the corpus's 128 palette records. The loader arms
#: the records at map load; a map does not author this.
#:
#: Measured [LIVE] 2026-08-28 on a running Gariland battle, with the record's
#: own siblings as the control. The table held `MAP022.9`'s three palette
#: records byte-for-byte off the disc -- the state a verbatim install leaves --
#: and rows 13, 14 and 15 were **still**. Writing byte 19 = 1 into record 0
#: alone started row 13 and left 14 and 15 at zero; putting the record back
#: stopped it again. Byte 14 (`0x81`) and byte 16 (`0x02`) alone did nothing,
#: so the engine initialises the rest from the record it is handed.
#:
#: This is the case decision 11's behavioural readback exists for, and it is
#: the case a byte readback would have passed: the chunk really was at the
#: address, byte-perfect, and nothing read it.
ANIM_RUN_FLAG_BYTE = 19
ANIM_RUN_FLAG = 1


def mask_animation_runtime(table: bytes) -> bytes:
    """`table` with every palette record's runtime bytes zeroed.

    What makes a live table comparable with a disc chunk. Applied to the
    PALETTE records only, and that is not a judgement call: `is_palette` is
    decided by bytes 0-7, which this never touches, so the two sides either
    agree on which records are palette records or they have already differed
    somewhere the mask cannot hide.
    """
    if len(table) < ANIM_TABLE_BYTES:
        raise LiveLinkError(
            f"an animation table is {ANIM_TABLE_BYTES} bytes and this is "
            f"{len(table)}")
    out = bytearray(table[:ANIM_TABLE_BYTES])
    for r in _mapfile.read_animation_instructions(_anim_resource(out)) or ():
        if not r.is_palette:
            continue
        for byte in ANIM_RUNTIME_BYTES:
            out[r.index * _mapfile.ANIM_INSTRUCTION_STRIDE + byte] = 0
    return bytes(out)


def _anim_resource(table: bytes) -> bytes:
    """`table` wrapped in the smallest resource `mapfile` will read it out of.

    The decode is `mapfile`'s and stays `mapfile`'s: a second reader of these
    records in the live link is a second chance to disagree with the disc
    writer about what a record IS, which is the whole class of bug the
    vendored package exists to prevent.
    """
    head = bytearray(_mapfile.HEADER_BYTES)
    head[_mapfile.ANIM_INSTRUCTION_PTR:_mapfile.ANIM_INSTRUCTION_PTR + 4] = \
        _mapfile.HEADER_BYTES.to_bytes(4, "little")
    return bytes(head) + bytes(table)


def read_animation_table(table: bytes):
    """The live 640 bytes as `mapfile.AnimInstruction` records."""
    return _mapfile.read_animation_instructions(_anim_resource(table))


def animation_tables(map_dir) -> dict[str, bytes]:
    """Every `0x6c` chunk in the extracted disc tree, by resource name.

    The candidate set for the content guard. 110 of the corpus's 1,575
    resources carry one; the scan reads a 196-byte header and 640 bytes per
    file and costs ~20 ms over the whole tree, which is why the guard can run
    on every press rather than being something the artist opts into.
    """
    out = {}
    if map_dir is None:
        return out
    for path in sorted(map_dir.glob("MAP*.*")):
        if path.suffix == ".GNS":
            continue
        try:
            size = path.stat().st_size
            with path.open("rb") as f:
                head = f.read(_mapfile.HEADER_BYTES)
                if len(head) < _mapfile.HEADER_BYTES:
                    continue
                at = struct.unpack_from(
                    "<I", head, _mapfile.ANIM_INSTRUCTION_PTR)[0]
                if at <= 0 or at + ANIM_TABLE_BYTES > size:
                    continue
                f.seek(at)
                chunk = f.read(ANIM_TABLE_BYTES)
        except OSError:                  # a tree we cannot read is no tree
            continue
        if len(chunk) == ANIM_TABLE_BYTES:
            out[path.name] = chunk
    return out


def check_animation_table(live: bytes, candidates: dict) -> list[str]:
    """Refuse unless the live table is some map's, and name which.

    Decision 5's locate-by-verify, at the address decision 11 writes 640 bytes
    to. The address was confirmed on ONE battle; writing there on any other is
    a bet, and `0x800F6DC4` is standing right next to it wearing the same
    leading eight bytes per record.

    It also closes the case that cannot be ruled out: if anything other than a
    map ever holds records there, it matches nothing, and the push stops
    instead of erasing it.

    **An empty candidate set is a refusal, not a pass.** Decision 11's
    degradation rule says a missing disc tree costs the install and not the
    removal; that cannot be honoured without contradicting part 2, because a
    candidate set of nothing verifies nothing and the erase's only proof that
    it is writing to a map's table is this match. Amended in
    `docs/live-link-v1.md` -- the animation leg degrades as a unit, and the
    rest of the push is unaffected either way.
    """
    if not candidates:
        raise LiveLinkError(
            "the animation table is guarded by CONTENT, and there is nothing "
            "to compare it against: no extracted disc tree was found, so the "
            f"640 bytes at 0x{ANIM_TABLE:08X} cannot be confirmed to be a "
            "map's. Refusing to erase them")
    want = mask_animation_runtime(live)
    stride = _mapfile.ANIM_INSTRUCTION_STRIDE
    slots = range(0, ANIM_TABLE_BYTES, stride)
    live_slots = [want[o:o + stride] for o in slots]
    empty = bytes(stride)
    if all(s == empty for s in live_slots):
        raise LiveLinkError(
            f"every slot of the table at 0x{ANIM_TABLE:08X} is empty, so the "
            "loaded map has no animation to remove and there is nothing to "
            "confirm it against -- an empty table is compatible with every "
            f"one of the {len(candidates)} on the disc, which is not a match. "
            "Nothing erased (#659)")
    matched = []
    for name, table in sorted(candidates.items()):
        masked = mask_animation_runtime(table)
        if all(mine == empty or mine == masked[o:o + stride]
               for mine, o in zip(live_slots, slots)):
            matched.append(name)
    if not matched:
        raise LiveLinkError(
            f"the 640 bytes at 0x{ANIM_TABLE:08X} match none of the "
            f"{len(candidates)} animation tables in the extracted disc tree, "
            "so they are not a loaded map's instruction records. Refusing to "
            "erase them -- this is either a different build of the game, or "
            "the table has moved, or something that is not a map writes here")
    return matched


def animation_rows(records) -> list[int]:
    """The CLUT rows a `0x6c` table animates, in table order.

    This is the **expected** side of the behavioural readback: the set of rows
    that move after a push has to equal the set the pushed map's own table
    names. It is read from the map rather than predicted, which is decision 3's
    rule, and it is what makes the empty case correct rather than special --
    `MAP002.9` carries no `0x6c` at all, so it names nothing, so *nothing
    moves* is the whole assertion for that push.

    `None` (the resource carries no table) yields `[]` rather than raising: a
    map without an animation is the common map, not a malformed one.
    """
    return [r.clut_row for r in records or ()
            if r.is_palette and r.frame_count and r.clut_row is not None]


def moved_clut_rows(*samples: bytes) -> list[int]:
    """Which CLUT rows are not constant across two or more samples of
    `CLUT_BLOCK`.

    The **measured** side of the readback. A byte readback is not sufficient on
    its own here and the reason is on the record: `check_clut_block` passed on
    Orbonne with both sides holding Orbonne's palettes while the write went
    nowhere. Bytes prove the values are at an address, never that anything
    reads it -- so what the animation leg is graded by is a row MOVING, which
    only a second sample can see.

    **Variadic on purpose, and two is the minimum rather than the number.** A
    cycle may repeat a frame -- `MAP022.9`'s frame 3 is byte-identical to its
    frame 1 -- so a pair of samples that straddles two steps reads a running
    row as still, and the readback would call a healthy install *not added*.
    The dwell is wall-clock over HTTP against an emulator whose speed is not
    ours, so which frames a pair lands on is not this code's to choose:
    sampling across the dwell rather than only at its ends is what removes the
    coincidence. One sample is refused, because a single sample reports
    *nothing moved* for every map -- a clean bill nothing measured.
    """
    if len(samples) < 2:
        raise LiveLinkError(
            f"the readback compares samples of `CLUT_BLOCK` over a dwell and "
            f"got {len(samples)}; one sample can only ever report that nothing "
            "moved")
    for i, s in enumerate(samples):
        if len(s) < CLUT_BLOCK_BYTES:
            raise LiveLinkError(
                f"the readback needs {CLUT_BLOCK_BYTES} bytes per sample and "
                f"sample {i} is {len(s)}")
    out = []
    for row in range(CLUT_ROWS):
        o = row * CLUT_ROW_BYTES
        seen = {s[o:o + CLUT_ROW_BYTES] for s in samples}
        if len(seen) > 1:
            out.append(row)
    return out


#: How many times the readback samples `CLUT_BLOCK` across the dwell. Two is
#: the minimum and not the number: a cycle may repeat a frame (`MAP022.9`'s
#: frame 3 is its frame 1), so a pair at the ends of the dwell can land on two
#: identical frames of a row that never stopped moving. Five is four intervals,
#: which cannot all coincide with a repeat.
ANIM_READBACK_SAMPLES = 5


def readback_animation(client, expected, dwell: float,
                       samples: int = ANIM_READBACK_SAMPLES,
                       sleep=None) -> tuple[bool, list[str]]:
    """Watch `CLUT_BLOCK` across the dwell and grade it against `expected`.

    The push's own goal, measured rather than asserted: **the set of rows that
    move equals the set the pushed map's table names**. `check_clut_block`
    passed on Orbonne with both sides holding Orbonne's palettes while the
    write went nowhere -- bytes prove the values are at an address, never that
    anything reads them -- so this is the check the animation leg is graded by.

    Every sample is a fresh fetch (`read_live`). A push holds one image of main
    RAM for its whole length, and answering a readback from it would compare an
    instant with itself.
    """
    if sleep is None:                    # imported here: the core is stdlib-
        import time                      # only and nothing else needs a clock
        sleep = time.sleep
    if not hasattr(client, "read_live"):
        raise LiveLinkError(
            "the animation readback needs a client that can re-read the "
            "console; this one can only answer from the push's held image, "
            "and an instant compared with itself never moves")
    got = []
    for i in range(samples):
        if i:
            sleep(dwell / (samples - 1))
        got.append(client.read_live(CLUT_BLOCK, CLUT_BLOCK_BYTES))
    return check_animation_readback(moved_clut_rows(*got), expected)


def check_animation_readback(moved, expected) -> tuple[bool, list[str]]:
    """Grade one animation push: **the rows that move must be the rows the
    pushed map names.**

    Phrased as the goal rather than as two separate checks, because the goal is
    the artist's own sentence -- *"the total removal of the old map, and adding
    the new map"* (decision 10, amended) -- and each direction of a mismatch is
    one half of it failing:

    - a row still moving that the pushed map does not name is **the old map not
      removed**: the erase missed it, or something re-installed it;
    - a row the pushed map names that did not move is **the new map not
      added**: the install did not land, or the frames it points at are absent.

    Both are reported when both hold. A swap between two animating maps fails
    in both directions in one press, and a report that stopped at the first
    would send the next reader looking for one defect where there are two.

    Returns `(ok, lines)` -- `diagnose_selfcheck`'s shape -- because the write
    has already happened by the time this runs. Decision 3: the push writes,
    reads back, and NAMES what did not hold; it does not predict.
    """
    moved, expected = sorted(set(moved)), sorted(set(expected))
    stale = [r for r in moved if r not in expected]
    absent = [r for r in expected if r not in moved]
    if not stale and not absent:
        if not expected:
            return True, ["animation: nothing moves, and the pushed map names "
                          "no animated rows -- removal confirmed by the "
                          "picture, not by the write"]
        return True, [f"animation: CLUT row(s) {_rows(expected)} move and no "
                      f"others do, which is exactly what this map's own table "
                      f"names"]
    lines = []
    if stale:
        lines.append(
            f"animation NOT fully removed: CLUT row(s) {_rows(stale)} are "
            f"still moving and this map does not animate them -- that is the "
            f"replaced map's table still running, which is what repaints a "
            f"correct push 4.49 times a second")
    if absent:
        lines.append(
            f"animation NOT added: this map animates CLUT row(s) "
            f"{_rows(absent)} and they did not move over the dwell -- the "
            f"instruction records or the frames did not reach the engine")
    return False, lines


def _rows(rows) -> str:
    return ", ".join(str(r) for r in rows)


#: Fields per second. The `0x6c` records count in fields, not frames -- byte 17
#: reading 12 is the ~0.213 s per step measured live, which is 12/60.
ANIM_TICKS_PER_SECOND = 60

#: The dwell a `duration = 0` palette record gets instead of none. #654: the
#: field is undecoded -- it may mean *every tick* or *inert* -- and a dwell
#: sized from it computes to zero, which is a readback that watches for no time
#: and reports *not added* about every row.
#:
#: The corpus does not reach this: both of its `duration = 0` palette records
#: (`MAP053.8`, `MAP053.22`) carry `frame_count = 0` too, so they animate
#: nothing and name no row. So this stands behind an authored or foreign table,
#: and it is **reported as an assumption** rather than hidden. 30 ticks is the
#: corpus's own slowest palette step, so an undecoded duration is watched for
#: at least as long as the slowest animation anyone has measured.
ANIM_DWELL_FLOOR_TICKS = 30


def base_animation(map_dir, document: dict, resource: str):
    """The pushed map's `0x6c` records and `0x70` frames, off the disc tree.

    Returns `(records, frames, source)`. `records` and `frames` are `None`
    when the resource carries no animation, which is the common map.

    **Read from the base rather than the document, on purpose.** Schema §8 puts
    both chunks on the *carried from base* side, so `dump` never writes them
    and `build` copies them verbatim. Putting them in the document would make
    them look authorable when nothing in the preview can show an animation, and
    would put `build` in the business of writing bytes it currently copies --
    on the one leg whose entire value is byte-exactness over 1,575 files. The
    shape does not foreclose it: if animation ever becomes authorable, this
    reads the document instead and nothing else changes.

    **The document's own pin is what makes the read verifiable.** `CONTEXT.md`,
    *Base map*: a document "is a diff against it, never a replacement... and
    pins the one it expects by a sha256 per resource." A tree that is not this
    document's own is a tree whose records mean something else.

    The frames may live on a **sibling resource of the same map** --
    `MAP053.19` and `MAP061.10` each declare a palette animation with a null
    `0x70` pointer and keep their frames on `.8`. That is the same sharing
    `palettes` and `light_rig` already do across a state group, and a reader
    that assumed the two chunks travel together would refuse two perfectly
    ordinary maps. The sibling is usually **not** in `base.resources` (MAP053
    a1 pins one resource), so it is not itself pinned, and `source` says where
    the frames came from rather than reporting them in the same words.
    """
    if map_dir is None or not Path(str(map_dir)).is_dir():
        raise LiveLinkError(
            "the animation lives in the map's base resource and there is no "
            "extracted disc tree to read it from. The push is not refused over "
            "it -- the erase is a separate act with a separate guard")
    map_dir = Path(str(map_dir))
    pins = {e.get("name"): e.get("sha256")
            for e in (document.get("base") or {}).get("resources") or ()}
    if resource not in pins:
        raise LiveLinkError(
            f"this document does not pin a resource named {resource}, so "
            f"there is nothing to verify a read of it against. It pins "
            f"{len(pins)}: {', '.join(sorted(pins)[:6])}"
            + (" ..." if len(pins) > 6 else ""))
    data = _read_pinned(map_dir, resource, pins[resource])

    records = _mapfile.read_animation_instructions(data)
    frames = _mapfile.read_palette_animation(data)
    if records is None:
        return None, None, resource
    if frames is not None:
        return records, frames, resource

    wants = any(r.is_palette and r.frame_count for r in records)
    if not wants:
        return records, None, resource
    stem = resource.split(".")[0]
    for sibling in sorted(map_dir.glob(f"{stem}.*")):
        if sibling.suffix == ".GNS" or sibling.name == resource:
            continue
        blob = sibling.read_bytes()
        found = _mapfile.read_palette_animation(blob)
        if found is None:
            continue
        if sibling.name in pins:
            _read_pinned(map_dir, sibling.name, pins[sibling.name])
            return records, found, f"{resource} (frames from {sibling.name})"
        return records, found, (
            f"{resource} (frames from {sibling.name}, which this document "
            f"does not pin)")
    raise LiveLinkError(
        f"{resource} declares a palette animation and carries no `0x70` frame "
        f"chunk, and no other resource of {stem} carries one either")


def _read_pinned(map_dir, resource: str, want: str) -> bytes:
    path = map_dir / resource
    if not path.is_file():
        raise LiveLinkError(
            f"the extracted disc tree at {map_dir} holds no {resource}, and "
            "the animation is read from the base resource")
    data = path.read_bytes()
    got = hashlib.sha256(data).hexdigest()
    if got != want:
        raise LiveLinkError(
            f"{resource} in {map_dir} is not the resource this document was "
            f"dumped from: it pins sha256 {want[:16]}... and the file is "
            f"{got[:16]}.... Its animation records would not be this map's")
    return data


def confirm_animation_erased(client) -> tuple[bool, list[str]]:
    """Read the table back and confirm no TEXTURE record survived the erase.

    The texture half of decision 11, and it is graded by **bytes**, in
    different words from the palette half on purpose. Decision 10's rule: *a
    weaker check reported in the same words as the strong one is worse than no
    check* -- the artist reads "confirmed" and believes the thing that was not
    proved. What was proved here is that the records are gone from RAM, not
    that the screen stopped moving: the corpus's slowest record is 240 ticks,
    4.00 s, and that is not time to spend inside a button press.

    Palette records are not counted. Nothing installs a texture record, so
    every surviving one is the replaced map's; the palette records in the table
    at this point are the pushed map's own, just written.
    """
    table = client.read_live(ANIM_TABLE, ANIM_TABLE_BYTES)
    left = [r for r in read_animation_table(table) or ()
            if any(r.raw) and not r.is_palette]
    if not left:
        return True, ["animation: no texture record left in the table -- "
                      f"{ANIM_TABLE_BYTES} bytes read back at "
                      f"0x{ANIM_TABLE:08X}. This is a BYTE confirmation, not a "
                      "picture: the slowest texture record in the corpus is "
                      "4.00s per step and that is not time to spend inside a "
                      "press, so what is proved is that the records are gone, "
                      "not that the sheet stopped being shuffled"]
    where = ", ".join(f"({r.x}, {r.y})" for r in left[:4])
    return False, [
        f"animation: {len(left)} texture record(s) survived the erase, at "
        f"{where}{' ...' if len(left) > 4 else ''} -- byte-read back from "
        f"0x{ANIM_TABLE:08X}. Those are the replaced map's, and they scroll "
        "rectangles inside the sheet this push just uploaded"]


def animation_report(records, source: str) -> list[str]:
    """Part 5: what the **edit** path says about the map's own animation.

    The rule is one line -- *neutralise foreign animation; never neutralise a
    map's own.* On `Push to PCSX` the animation belongs to the document's own
    map: `build` will carry `0x6c` and `0x70` to the disc verbatim, and
    freezing it would show the artist a picture the shipped map can never
    produce, which is the loupe lying in exactly the way the shared palette
    packing exists to prevent.

    So the edit path explains rather than acts. This reporting half is needed
    on the *Replace* path anyway (decision 4), so it is cheaper than saying
    nothing, not dearer.
    """
    rows = animation_rows(records)
    if not rows:
        return [f"animation: {source} declares no animated CLUT row, so every "
                "palette this push writes is the one the battle shows"]
    step = animation_dwell(records)
    return [
        f"animation: {source} animates CLUT row(s) {_rows(rows)} and the "
        f"battle repaints them every {step:.2f}s from the map's own `0x70` "
        "frames. Those colours are in this document and on the disc; the push "
        "writes them and the engine cycles them, which is what the shipped map "
        "does. They are NOT frozen -- only a Replace erases an animation, and "
        "only because it is the map being replaced's"]


def plan_erase_animation() -> list[tuple[int, bytes]]:
    """Part 1: zero the host map's whole instruction table.

    640 bytes at the address `check_animation_table` has just confirmed. An
    all-zero record is **the corpus's own encoding for no animation** -- 21 of
    `MAP022.9`'s 32 slots ship that way and the engine already walks all 32
    every frame -- so this writes what the disc writes rather than disabling a
    feature it has to know how to switch off. Measured [LIVE] 2026-08-27:
    zeroing record 0 took CLUT row 13 from 4.49 steps a second to 0.00 while
    its two siblings stayed at 4.5, which is a control the poke gets for free.

    **The whole table, not the palette records.** 60 of the corpus's tables
    drive CLUT rows and 94 drive TEXTURE regions, and Gariland's own eight
    texture records point at `x = 839..928`, inside the four VRAM pages a swap
    has just uploaded a foreign sheet to. A fix scoped to palettes leaves them
    shuffling rectangles inside the new sheet, to be reported later in words
    that sound unrelated to this bug.
    """
    return [(ANIM_TABLE, bytes(ANIM_TABLE_BYTES))]


#: The console's frame buffer, in 16-bit pixels. A `0x6c` record names an
#: absolute rectangle in it; ~84 records in the corpus name one that does not
#: fit, at `x` 3,840 / 61,440 / 61,680 and `y` 3,840 / 4,080 / 61,440 / 65,520.
#: Those are *absent* records rather than corrupt files -- schema §10.3's
#: terrain rule applied here -- and they are refused rather than written.
VRAM_WIDTH = 1024
VRAM_HEIGHT = 512


def _fits_in_vram(r) -> bool:
    return (0 <= r.x and r.x + r.width <= VRAM_WIDTH
            and 0 <= r.y and r.y + r.height <= VRAM_HEIGHT)


def plan_install_animation(records, frames) -> tuple[list, list[str]]:
    """Parts 3 and 4: install the pushed map's PALETTE animation, and name
    everything left behind.

    `records` and `frames` are the pushed map's own `0x6c` and `0x70` chunks,
    read from its BASE resource on the extracted disc tree -- the interchange
    document carries neither (schema §8 puts both on the *carried from base*
    side), and putting them in the document would make them look authorable
    when nothing in the preview can show an animation.

    Three things are deliberately not written:

    - **texture records** (#653). A palette record needs no translation: the
      CLUT line is `y = 480` on every map, forced by the packet encoding that
      gave `0x7800` on 385 of 385 polygons. A texture record is absolute VRAM
      against its own map's sheet base, and that base is assigned by the
      loader -- it is in neither the document nor the base resource, and it is
      not a constant (479 of 577 sit at `x >= 768`, 80 at `x = 0`, 18
      elsewhere). Rebasing by the dominant value would be right for most and
      silently wrong for ~98 with nothing to say which.
    - **records that do not fit in VRAM**, which `is_palette` does not screen
      for: it asks where a record points, not whether the place exists.
    - **empty slots**, which the erase has already left as zeros -- the shape
      the disc itself ships for "no animation".

    Returns `(writes, notes)`. The notes are decision 4's rule: name what was
    skipped, never drop it silently.
    """
    records = list(records or ())
    live = [r for r in records if any(r.raw)]
    palette = [r for r in live if r.is_palette and _fits_in_vram(r)]
    notes = []

    if not live:
        return [], ["animation: this map carries no animation table, so there "
                    "is no animation to install -- the readback expects "
                    "nothing to move"]
    if palette and not frames:
        raise LiveLinkError(
            f"this map's animation names {len(palette)} CLUT row(s) and its "
            "`0x70` frame chunk was not found. Installing the records without "
            "the frames would point the engine at the REPLACED map's colours, "
            "cycling on this map's rows -- which is the bug with an extra step")

    stride = _mapfile.ANIM_INSTRUCTION_STRIDE
    writes = [(ANIM_TABLE + r.index * stride, _armed(r.raw)) for r in palette]
    if writes:
        writes.append((ANIM_FRAMES, _pack_frames(frames)))

    texture = [r for r in live if not r.is_palette]
    if texture:
        notes.append(
            f"animation: {len(texture)} texture record(s) erased and NOT "
            "installed -- a texture record is absolute VRAM against its own "
            "map's sheet base, and that base is the loader's, in neither the "
            "document nor the base resource (#653). The sheet itself IS "
            "pushed; what is not is the animation that scrolls it")
    for r in live:
        if r.is_palette and not _fits_in_vram(r):
            notes.append(
                f"animation: record {r.index} names ({r.x}, {r.y}) "
                f"{r.width}x{r.height}, which is outside the console's "
                f"{VRAM_WIDTH}x{VRAM_HEIGHT} frame buffer -- an absent record, "
                "refused rather than written")
    if palette:
        notes.append(
            f"animation: installed {len(palette)} palette record(s) driving "
            f"CLUT row(s) {_rows(animation_rows(palette))}, and the 16 frames "
            "they read")
    return writes, notes


def _armed(raw: bytes) -> bytes:
    """A disc record with the engine's run flag set -- the one byte the LOADER
    owns. Everything the map declares is carried verbatim; see
    `ANIM_RUN_FLAG_BYTE` for the measurement that put it here."""
    out = bytearray(raw)
    out[ANIM_RUN_FLAG_BYTE] = ANIM_RUN_FLAG
    return bytes(out)


def _pack_frames(frames) -> bytes:
    """The `0x70` chunk as bytes, from `mapfile.read_palette_animation`'s
    16 x 16 BGR555 words -- blank frames included, because a frame's index is
    what a record refers to."""
    packed = b"".join(int(w).to_bytes(2, "little")
                      for frame in frames for w in frame)
    if len(packed) != ANIM_FRAMES_BYTES:
        raise LiveLinkError(
            f"the `0x70` frame chunk is {ANIM_FRAMES_BYTES} bytes -- "
            f"{_mapfile.PALETTE_ANIM_FRAMES} frames of {CLUT_ENTRIES} words -- "
            f"and this packs to {len(packed)}")
    return packed


def animation_dwell(records) -> float:
    """Seconds the readback must watch for, for the rows THIS map animates.

    One step of the slowest palette record -- `max(duration)/60` -- which is
    <= 0.5 s on every map in the corpus and 0.2 s on Gariland.

    **Palette records only.** The corpus's slowest record is 240 ticks, or
    4.00 s, and it is a TEXTURE record; that is not time to spend inside a
    button press, which is why decision 11's texture half is byte-confirmed
    and reported in different words. A dwell that took the whole table's
    maximum would make every press with a texture animation in it feel hung.

    `0.0` when the map animates no CLUT row -- there is nothing to wait for,
    and the readback still runs: two samples, expecting no movement.
    """
    durations = [r.duration or ANIM_DWELL_FLOOR_TICKS
                 for r in records or ()
                 if r.is_palette and r.frame_count and r.clut_row is not None]
    return max(durations) / ANIM_TICKS_PER_SECOND if durations else 0.0


# --- the sinks a pose is written to ----------------------------------------
# Every one of these was read out of `reference-assets/thief_whats_this.sstate`
# and is asserted against it in `tests/test_live_link.py`, rather than being
# carried in prose. Two of them are corrections to labels that were about to be
# built on: the scratch struct's angle offsets are mislabelled (its yaw is at
# `+0x7C`, the slot called roll), and `camera_current_w` (`0x801B8B04`) is NOT
# the zoom -- it reads 0 in a running battle, because the whole
# `saved`/`start`/`current` block is an idle effect save/restore slot.

#: The camera's optical centre -- the world point it is aimed at. Three s32,
#: 20.12 fixed point, so `raw / 4096` is FFT world units.
WORK_POSITION = 0x800E4E74

#: `work_rotation` -- pitch, yaw, roll as three `short`s, 4096 = 360 degrees.
WORK_ROTATION = 0x800A7784

#: The live zoom, three s32 at 4096 = 1.0x. Mirrored at scratch `+0x80`.
SPRITE_SCALE = 0x800C7CA0

#: The engine's composed view rotation, nine `short`s at 4096 = 1.0 -- read by
#: BOTH the map affine transform and `project_all_unit_sprites`. A push does
#: not write here; the engine rebuilds it every frame from `work_rotation`,
#: which is what makes reading it back a BEHAVIOURAL check on a pose write.
CAMERA_VIEW_MATRIX = 0x80098A24

#: `camera_tracked_target`, three s32: the GTE translation's other half, where
#: `TR = camera_tracked_target - R*work_position`. It reads `{256, 160, 640}`.
CAMERA_TRACKED_TARGET = 0x800A77B0

#: The VERTICAL DATUM: the `160` of that triple, the reason `work_position`
#: projects to screen y=160 on a 240-line frame instead of the midpoint 120.
#: FFT frames the action two thirds down, leaving headroom -- so a pose sync
#: that is right in every other respect still leaves the two views 40 world
#: units, 1.43 tiles, apart vertically, which is the artist's reported symptom.
#: It is the engine's own named word, not a constant anyone fitted.
CAMERA_VERTICAL_DATUM = CAMERA_TRACKED_TARGET + 4

#: Where a sync puts it: the middle of the frame, so the optical centre and the
#: screen centre coincide. The correction lives in the ENGINE rather than in an
#: offset applied here, so it scales with zoom for free and there is no
#: hand-tuned 40 to keep right. Costs: the emulator's framing is then not
#: authentic, and `smooth_track_camera_target` (`FUN_8008B6E4`) maintains this
#: word per frame, so it may not stick.
SCREEN_CENTRE_DATUM = 120


# --- the camera scratch struct, the OTHER candidate sink --------------------
# The per-vsync ticker `camera_per_vsync_ticker` (`FUN_801439C0`) reads this
# struct through a pointer cell and latches it into the GTE block. Decision 12
# names it the LEADING candidate for a live write, on the strength of its
# position being byte-identical to `work_position` in the battle savestate.
#
# Two things read since say the ranking is the other way round, and both come
# out of the RE record decision 12 itself cites:
#
# * The copy runs `work_position` -> scratch -> GTE, not the reverse. F14 has
#   it statically at `0x80143AC8/0x80143B24` and validated it live: *"a
#   `work_position` poke sticks and re-projects; the handoff had it
#   backwards"*. Byte-identity is what a copy in EITHER direction looks like,
#   so the savestate cannot rank them and the disassembly can.
# * F14's own rig note is blunter: *"Camera-scratch pokes do NOT stick"* -- an
#   interpolator re-drives `+0x68` every frame back to the keyframe target.
#   That was measured on a cinematic, where an interpolator is running; a
#   battle idle may differ, which is exactly what the live A/B is for.
#
# So both sinks are planned and named, and `work_position` is the default.
#
# The angle offsets are NOT mislabelled. Decision 12 reads `[302, 0, 4608]`
# there and concludes the yaw sits in the slot labelled roll; that reading is
# at a TWO-byte stride. The fields are four bytes apart -- `renames_high.tsv`
# gives the aliases itself, `camera_scratch_pitch` at `0x80057790` and `_yaw`
# at `0x80057794` -- and at four bytes the struct reads `[302, 4608, 0]`,
# agreeing with `work_rotation` word for word.

#: The pointer cell the ticker dereferences once per vsync.
SCRATCH_STRUCT_PTR = 0x80165F9C

#: What it holds. Confirmed by content against the savestate, not by the label.
SCRATCH_STRUCT = 0x8005771C

SCRATCH_POSITION = SCRATCH_STRUCT + 0x68        # 3 x s32, mirrors work_position
SCRATCH_ANGLES = SCRATCH_STRUCT + 0x74          # 3 x s32 slots, pitch/yaw/roll
SCRATCH_ZOOM = SCRATCH_STRUCT + 0x80            # s32, mirrors sprite_scale


# --- the axis frame, spelled a second time ---------------------------------
# ADR-0004 decision 14, `AXIS_NAME = ("x", "z", "-y")` in `import_document.py`
# and ratified by `blender_axis_baseline.json`. It cannot be imported from
# there -- that module needs `bpy`, and imports THIS one -- so it is spelled
# again, and `tests/test_live_link.py` reads the same ratified baseline to keep
# the two from drifting. det = +1: it is a rotation, not a mirror.

#: How FFT world axes are named in Blender. Documentation for the pair below.
BLENDER_FROM_FFT = ("x", "z", "-y")

#: The scale is 1:1. `TILE_UNITS = 28` -- the addon imports geometry at FFT
#: WORLD scale, so one Blender unit is one FFT world unit and there is no
#: factor to invent. `godot-learning`'s `GODOT_CAMERA_SIZE = 12.6` is in a
#: space where 1 unit is 1 TILE, so it is off by 28x used here.
POSITION_FRACTION = 4096            # s32 20.12


def blender_from_fft(v) -> tuple:
    return (v[0], v[2], -v[1])


def fft_from_blender(v) -> tuple:
    return (v[0], -v[2], v[1])


def camera_position(pivot) -> tuple:
    """A Blender view pivot as the three raw words `work_position` holds.

    The pivot is the OPTICAL CENTRE -- the world point the camera is aimed at,
    not the camera's own location. FFT has no separate eye position: the
    projection is orthographic, so a pose is a centre, an orientation and a
    scale, and there is nothing to place an eye at.
    """
    return tuple(round(c * POSITION_FRACTION) for c in fft_from_blender(pivot))


# --- decision 12: the camera ------------------------------------------------
# The engine's camera is orthographic and its rotation is
# `R = Rx(pitch)*Ry(yaw)*Rz(roll)` -- right-handed elementary rotations with
# POSITIVE signs, on PSX world axes (X lateral, Y DOWN, Z depth). That was
# fitted to 65 live samples off a cinematic (F4 in
# `camera_framing_pivot_decode.md`) and confirmed again against a battle
# savestate; `tests/test_live_link.py` asserts both the winner and the rivals
# against that savestate on every commit, because a fit written into prose
# grades nothing.
#
# Angles are the engine's: signed, 4096 = 360 degrees, and stored UNWRAPPED --
# a battle holds yaw 4608 = 4096 + 512 = 405 degrees, and consumers mask
# `& 0xfff` themselves. Nothing here normalises on the artist's behalf.

import math                                                # noqa: E402

#: One full turn in the engine's angle units.
ANGLE_UNITS = 4096


def _radians(units: int | float) -> float:
    return units * 2.0 * math.pi / ANGLE_UNITS


Mat3 = tuple


def rotation_x(units: int | float) -> Mat3:
    """Right-handed rotation about X (the lateral axis) -- the camera's pitch."""
    c, s = math.cos(_radians(units)), math.sin(_radians(units))
    return ((1.0, 0.0, 0.0), (0.0, c, -s), (0.0, s, c))


def rotation_y(units: int | float) -> Mat3:
    """Right-handed rotation about Y (down) -- the camera's yaw."""
    c, s = math.cos(_radians(units)), math.sin(_radians(units))
    return ((c, 0.0, s), (0.0, 1.0, 0.0), (-s, 0.0, c))


def rotation_z(units: int | float) -> Mat3:
    """Right-handed rotation about Z (depth) -- the camera's roll.

    FFT has this axis and has never used it: fixed 0, no control, and roll was
    0 in all 65 of F4's samples and in the battle savestate. `Rz`'s PLACEMENT
    in the composition is therefore assumed, never confirmed -- which is why
    decision 12 clamps a pushed roll to zero rather than driving it.
    """
    c, s = math.cos(_radians(units)), math.sin(_radians(units))
    return ((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0))


def mat3_multiply(a: Mat3, b: Mat3) -> Mat3:
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3))
                       for j in range(3)) for i in range(3))


# --- the zoom, which is a dial ---------------------------------------------
# The emulator's frame is a fixed 256x240; a Blender viewport is whatever shape
# the artist dragged it to. The two can therefore agree on at most one axis,
# and rather than pick one, the push derives a zoom from the view distance and
# multiplies it by a factor in the panel: *"just make the center axis align and
# we can dial in a zoom in the UI"*.
#
# That is not a convenience. It removes the one CONTESTED number in the camera
# model from the design: the horizontal store-to-pixel factor is unsettled in
# the RE record -- F15 measured `screen_x ~= view_x` at 1:1, F20's
# decomposition implies a factor of two, and F19's whole finding was godot's
# horizontal coming out compressed 0.82x. Under a dial nothing here depends on
# which of them is right. Pixel aspect is likewise not corrected anywhere:
# *"if we want a PAR-less comparison we can watch the VRAM viewer"*.

#: The engine's own 1.0x. `sprite_scale` is three s32 at 4096 = 1.0.
ZOOM_ONE = 4096

#: The Blender view distance at which the dial, at rest, emits 1.0x.
#:
#: A STARTING POINT, not a measurement. It is about twelve tiles' worth of
#: world units, which is roughly what a 240-line frame holds at F15's 20 px per
#: tile -- but the exact relation between a Blender view distance and an
#: on-screen extent depends on the viewport's own lens, and the FFT side of it
#: is the contested factor above. Calibrating this belongs to the dial, which
#: is why this number is allowed to be approximate and the dial is not.
ZOOM_REFERENCE_DISTANCE = 336.0

#: Bounds on the emitted scale. These are NOT the game's envelope -- decision 1
#: pushes a pose faithfully, and the pad's 0xC00-0x1000 is the very thing that
#: makes a map uninspectable. They exist because Blender hands over a view
#: distance of 0 when an orbit is driven all the way in, and a scale of 0
#: collapses the map to a point, which an artist reads as a broken sync.
ZOOM_RAW_MIN = 1
ZOOM_RAW_MAX = 1 << 20              # 256x, far outside anything reachable


def camera_zoom(view_distance: float, dial: float = 1.0) -> int:
    """A Blender view distance as the raw word `sprite_scale` holds.

    Inverse, because that is what the artist feels: half the distance is twice
    the picture. `dial` multiplies the result rather than replacing it, so
    zooming in Blender still moves the emulator after the dial is turned.
    """
    if view_distance <= 0.0:
        return ZOOM_RAW_MAX
    raw = round(ZOOM_ONE * dial * ZOOM_REFERENCE_DISTANCE / view_distance)
    return max(ZOOM_RAW_MIN, min(ZOOM_RAW_MAX, raw))


#: Blender view space to FFT screen space. Blender's view is +X right, +Y up,
#: +Z toward the viewer; FFT's screen is X right, Y DOWN, Z INTO the screen. So
#: two of the three axes are negated and nothing else changes.
SCREEN_FROM_VIEW = ((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, -1.0))

#: FFT world to Blender world, `BLENDER_FROM_FFT` written as a matrix.
BLENDER_FROM_FFT_MATRIX = ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, -1.0, 0.0))


def mat3_transpose(m: Mat3) -> Mat3:
    return tuple(tuple(m[j][i] for j in range(3)) for i in range(3))


def _units(radians: float) -> int:
    """A angle in the engine's units, wrapped into one turn."""
    return round(radians * ANGLE_UNITS / (2.0 * math.pi)) % ANGLE_UNITS


def view_rotation_to_fft(view_rotation) -> Mat3:
    """The engine's `R` for a Blender `view_rotation`, before decomposition.

    `view_rotation` is Blender's own: the rotation taking VIEW space to WORLD
    space, three rows, whose columns are therefore the world directions of the
    viewport's right, up and toward-the-viewer. Composed the way the spaces
    chain -- world to view, world frame to world frame, view to screen -- this
    is `S * view_rotation^T * B`.
    """
    world_to_view = mat3_transpose(tuple(tuple(float(c) for c in row)
                                         for row in view_rotation))
    return mat3_multiply(mat3_multiply(SCREEN_FROM_VIEW, world_to_view),
                         BLENDER_FROM_FFT_MATRIX)


def camera_angles(view_rotation) -> tuple:
    """A Blender `view_rotation` as the engine's `(pitch, yaw, roll)`.

    Roll comes back 0 always -- decision 12's one clamp, and the reason is that
    `Rz`'s placement in the composition is the single part of the camera model
    never confirmed against hardware: FFT has the axis and has never used it,
    so roll was 0 in all 65 of F4's samples and in the battle savestate too.
    A rolled Blender view is answered with the nearest unrolled pose.

    With roll pinned at 0 the composition `Rx(p)*Ry(y)` is

        [[ cy,     0,   sy  ],
         [ sp*sy,  cp, -sp*cy],
         [-cp*sy,  sp,  cp*cy]]

    so both angles read straight off, each from a full `atan2` rather than from
    an `asin` -- which matters, because a camera behind the map is yaw 180 and
    an `asin` cannot say so. `R[0][1]` is the roll: it is zero exactly when the
    viewport's right is horizontal, and it is what gets dropped.
    """
    r = view_rotation_to_fft(view_rotation)
    return (_units(math.atan2(r[2][1], r[1][1])),
            _units(math.atan2(r[0][2], r[0][0])),
            0)


def camera_rotation(pitch: int | float, yaw: int | float,
                    roll: int | float = 0) -> Mat3:
    """The engine's view rotation for a pose, at unit scale (not 4096-scaled).

    The order is the whole of the finding: `Rx*Ry*Rz`, and the two arguments
    are in the order `work_rotation` stores them. The scratch struct's labels
    disagree -- `renames_high.tsv` calls its `+0x74/78/7C` pitch/yaw/roll while
    the live struct holds `[302, 0, 4608]` against a camera yaw of 4608, so the
    yaw is at `+0x7C`. Read a pose out of the scratch struct in the labelled
    order and this returns a matrix that misses the engine's by 0.948.
    """
    return mat3_multiply(mat3_multiply(rotation_x(pitch), rotation_y(yaw)),
                         rotation_z(roll))


#: How far the engine's own composed matrix may sit from the one a pose
#: implies. The floor is the 4096-quantization -- each entry is a product of
#: two 12-bit fixed-point sines and the measured fit is about seven LSB -- and
#: this is eight. A rival composition lands two orders of magnitude out, so the
#: bar does not need to be tight to separate "landed" from "did not".
CAMERA_MATRIX_FLOOR = 8 / 4096


def camera_readback(stored: bytes, angles) -> tuple:
    """Does the engine's own view matrix agree with the pose that was pushed?

    `stored` is the 18 bytes at `CAMERA_VIEW_MATRIX`, read back a frame after
    the write. This is BEHAVIOURAL and that is the whole point: the engine
    rebuilds that matrix every frame from `work_rotation`, so agreement means
    the write reached something the engine composes from. A byte readback of
    the write itself would agree with a poke that nothing downstream ever read
    -- which is exactly the failure decision 11 was reported as.

    It is a REPORT, not a refusal. The fallback for a sink that does not stick
    is to pause the emulator, and a paused emulator runs no frame in which to
    rebuild anything.
    """
    want = struct.unpack("<9h", stored)
    got = camera_rotation(*angles)
    error = max(abs(got[i][j] - want[i * 3 + j] / 4096)
                for i in range(3) for j in range(3))
    return error < CAMERA_MATRIX_FLOOR, error


def check_view_syncable(view_perspective: str) -> None:
    """Refuse the one viewport whose pose is not what is on screen.

    `RegionView3D.view_perspective` is `'ORTHO'`, `'PERSP'` or `'CAMERA'`.
    Perspective is not refused: FFT is orthographic, so a perspective viewport
    cannot be made to match by any arithmetic, but the panel's ortho toggle is
    its own indicator and the addon does not reach in and change a view the
    artist set.

    Looking through a scene camera is different in kind. `view_location` and
    `view_rotation` then describe the last FREE view rather than what is on
    screen, so a push would sync the emulator to a viewport nobody is looking
    at -- which presents as exactly the bug this feature exists to fix.
    """
    if view_perspective == "CAMERA":
        raise LiveLinkError(
            "the viewport is looking through a scene camera, and a scene "
            "camera's pose is not the view's -- Blender still reports the "
            "last free view, so a sync would push a pose you are not looking "
            "at. Leave the camera view first.")


# --- the pose, and the plan that writes it ---------------------------------

class CameraPose(NamedTuple):
    """One Blender viewport, in the engine's own raw words.

    A value, not a write: the continuous leg compares the pose it just derived
    against the one it last pushed and does nothing when they are equal, which
    is what keeps an idle viewport off the wire.
    """

    position: tuple           #: 3 x s32, 20.12, -> work_position
    angles: tuple             #: pitch, yaw, roll, 4096 = 360 -> work_rotation
    zoom: int                 #: s32, 4096 = 1.0x -> sprite_scale


def camera_pose(pivot, view_rotation, view_distance: float,
                dial: float = 1.0) -> CameraPose:
    """A Blender viewport as a pose. `bpy`-free: the caller unpacks the
    `RegionView3D` and hands over a pivot, a rotation matrix and a distance."""
    return CameraPose(camera_position(pivot),
                      camera_angles(view_rotation),
                      camera_zoom(view_distance, dial))


#: The two candidate sinks. Which one survives a write during a running battle
#: is the single thing decision 12 leaves open, and it is answered by an A/B --
#: poke one, read the engine's own derived matrix at `CAMERA_VIEW_MATRIX` one
#: frame later, poke the other, compare, with a framebuffer dump as the witness
#: because only a render settles a rendering question.
CAMERA_SINK_WORK = "work"
CAMERA_SINK_SCRATCH = "scratch"

#: F14 measured a `work_position` poke sticking and re-projecting, and
#: camera-scratch pokes NOT sticking. That was on a cinematic, where an
#: interpolator is re-driving the scratch every frame; the artist's loop is a
#: battle idle, which is why the other plan exists rather than being deleted.
CAMERA_SINK_DEFAULT = CAMERA_SINK_WORK


def plan_camera(pose: CameraPose,
                sink: str = CAMERA_SINK_DEFAULT) -> list:
    """`(address, bytes)` for one pose, plus the framing datum.

    The widths are the engine's and they differ between the sinks:
    `work_rotation` holds three SHORTS, while the scratch struct's angles are
    word slots four bytes apart. Getting that wrong writes a yaw eight bytes
    past anything that reads it -- the map is still there, turned wrong -- and
    it would answer the A/B above with a false negative.

    The datum poke is in both plans because the 40-unit vertical gap is a
    property of how FFT frames a shot, not of which sink carried the pose.
    """
    datum = [(CAMERA_VERTICAL_DATUM, struct.pack("<i", SCREEN_CENTRE_DATUM))]
    if sink == CAMERA_SINK_WORK:
        return [(WORK_POSITION, struct.pack("<3i", *pose.position)),
                (WORK_ROTATION, struct.pack("<3h", *pose.angles)),
                (SPRITE_SCALE, struct.pack("<3i", *([pose.zoom] * 3)))] + datum
    if sink == CAMERA_SINK_SCRATCH:
        return [(SCRATCH_POSITION, struct.pack("<3i", *pose.position)),
                (SCRATCH_ANGLES, struct.pack("<3i", *pose.angles)),
                (SCRATCH_ZOOM, struct.pack("<i", pose.zoom))] + datum
    raise LiveLinkError(
        f"unknown camera sink {sink!r}; it is {CAMERA_SINK_WORK!r} or "
        f"{CAMERA_SINK_SCRATCH!r}")


#: Document fields with no live sink, and why. Decision 4 wants these NAMED on
#: every push rather than silently dropped.
#: How often the continuous sync looks at the viewport, in seconds. 20 Hz is
#: the cadence decision 12 names, and it is a LOOK, not a write -- the write
#: happens only when the pose actually changed, which is what makes an idle
#: Blender cost nothing.
CAMERA_SYNC_INTERVAL = 0.05
#: How often it looks after a write FAILED. Nothing here is worth 20 attempted
#: connections a second to an emulator that is not running, and the artist gets
#: the pose within two seconds of starting one.
CAMERA_SYNC_BACKOFF = 2.0


class CameraSyncTicker:
    """What a continuous-sync tick DECIDES, with no `bpy` and no socket in it.

    Decision 12 part 2 builds the timer on top of the button, and the timer's
    risk is not the arithmetic -- that is the button's, and it is proven. The
    timer's risk is cadence: pushing a viewport nobody moved, retrying a write
    that already landed, or saying the same sentence twenty times a second.
    Those are decisions rather than plumbing, so they live here where a plain
    `pytest` can grade them.

    Three rules, and each one is a defect it would otherwise ship:

    * **Only a CHANGED pose is written.** A still viewport makes no traffic at
      all, which is what makes decision 2's "ON by default costs nothing when
      no emulator is running" true rather than aspirational.
    * **A failed write is not a push.** The pose is remembered on success only,
      so an emulator started after Blender receives the view the moment it
      answers, with no nudge from the artist.
    * **Only state CHANGES are reported.** The sync is meant to be invisible
      while it works; a line per tick would bury the push reports that share
      the console and the Log.

    The readback is deliberately NOT here. It is the button's instrument -- a
    second round trip per tick, and a line per tick in the Log, to re-answer a
    question the artist already answered by pressing the button once.
    """

    def __init__(self, interval=CAMERA_SYNC_INTERVAL,
                 backoff=CAMERA_SYNC_BACKOFF):
        self._interval = interval
        self._backoff = backoff
        self._pushed = None      # the last pose the emulator ACKNOWLEDGED
        self._said = None        # the state already reported, so it is not again
        self._failing = False

    def wants(self, pose) -> bool:
        """Is this pose worth a write? Only if it is not the one that landed."""
        return pose != self._pushed

    def interval(self) -> float:
        """Seconds until the next look. Backed off only by a FAILED write --
        an idle reason is not the emulator's fault and must not slow it."""
        return self._backoff if self._failing else self._interval

    def succeeded(self, pose) -> list:
        """The write landed. Returns the lines worth saying, usually none."""
        self._pushed = pose
        lines = []
        if self._failing or self._said is not None:
            lines.append("the camera sync reached the emulator again")
        self._failing = False
        self._said = None
        return lines

    def failed(self, error) -> list:
        """The write did not land. The pose is NOT remembered, so the next
        tick retries it."""
        self._failing = True
        return self._announce(f"the camera sync cannot reach the emulator: "
                              f"{error}")

    def idle(self, reason) -> list:
        """Nothing to push, and not because of the emulator -- a viewport
        looking through a scene camera, say. Reported once and at full rate."""
        return self._announce(f"the camera sync is idle: {reason}")

    def _announce(self, line) -> list:
        if self._said == line:
            return []
        self._said = line
        return [line]

    def reset(self) -> None:
        """Forget everything. The toggle going off and on again must RESEND,
        because the battle's own camera moved while the sync was off."""
        self.__init__(self._interval, self._backoff)


UNPUSHED = {
    "the terrain grid": "no located sink -- the tile records themselves: "
                        "their heights, slopes and walkability. This is NOT "
                        "the per-polygon BINDING, which every push now writes "
                        "on all four buckets; the two are not two views of "
                        "one thing and do not share a fate (CONTEXT.md, "
                        "*Binding vs the terrain grid*). So the map looks "
                        "right and COLLIDES wrong until `build` puts it on a "
                        "disc",
    "polygons[].unknown_untextured": "the untextured record's four raw "
                                     "property bytes -- a DIFFERENT thing "
                                     "from bytes 6-7, which this push does "
                                     "write. Where they land in RAM is not "
                                     "located, and they are not zero-filled: "
                                     "#496 settled that zero is the worst "
                                     "fill. A reordered untextured polygon "
                                     "leaves them behind, on 69 of MAP022 "
                                     "a0's 454 polygons",
}

#: Fields that USED to be in `UNPUSHED` and are not any more, kept as a note
#: because the addon's own `CLAUDE.md` records a false premise here that
#: "outlived the code by four months".
#:
#: `map_states[].texture_sheet` and `map_states[].palettes` are both pushed by
#: the button now (`live_vram.plan_sheet` and `plan_palettes` above), so they
#: are out of `UNPUSHED` altogether -- the same exit the light rig made when it
#: got a sink. The entry for the sheet said it was built "by
#: `tools/live_push.py`'s savestate round trip"; that tool is gone, and so is
#: the premise it rested on.


# --- decision 13: isolating the map ----------------------------------------
# Everything above pushes a DOCUMENT FIELD at a live sink. Nothing below does.
# These are *isolation writes* (`CONTEXT.md`): engine state with no document
# behind it, written to take something off the screen and restored from a value
# saved before the write. They are deliberately outside the push's reporting.

#: Head of the unit sprite display-object list. Eighteen readers, three
#: writers, and every one of the writers is list surgery -- which is why the
#: lever is the per-unit flags below and NOT this pointer. Nulling the head
#: would hide the units and also blind `unit_sprite_object_find` (0x8007A6E4),
#: which gameplay resolves ids through.
UNIT_LIST_HEAD = 0x80098A54

#: The three fields the walk reads, at the offsets the engine's OWN getter uses.
#: `unit_sprite_object_find` does `lw node+0x0` for the next pointer and
#: **`lbu` node+0x4** for the id -- a byte, not a word. Read as a word the
#: Gariland list's first id is 0x0061000A; the low byte, 0x0A, is the number
#: `{44}`/`{46}` and the `{47}` ghost gate pass around.
UNIT_NEXT = 0x0
UNIT_ID = 0x4

#: The two halfwords `unit_sprite_object_hide` (0x8008D18C) zeroes together --
#: `sh zero,0xa(v1)` and `sh zero,0x1d8(v1)`. `+0x1d8` is the one that matters
#: to the picture: `unit_sprite_render_dispatch` reads it at 0x80086768 and
#: branches to its epilogue when zero, BEFORE the `+0x298` shadow test at
#: 0x80086ACC -- so the ground shadow follows from the same branch and
#: `unit_shadow_disable` (0x8008C2A4) is not needed. `+0xa` is written for
#: company, because the engine writes the pair.
UNIT_SHOW = 0xA
UNIT_DISPATCH = 0x1D8

#: A backstop, not the mechanism -- cycle detection is by visited address,
#: which is exact. `entd_to_roster_loader_16` loads 16 ENTD slots and `{47}`
#: adds up to three ghosts, so a chain past 32 is not a roster.
UNIT_WALK_CAP = 32


class UnitNode(NamedTuple):
    """One display object, and the two halfwords an isolate would overwrite.

    `show` and `dispatch` are SAVED VALUES. Restore writes them back rather
    than the constant `1` that `unit_sprite_object_show` writes: a unit the
    game had legitimately hidden -- not yet revealed, erased by a `{46}`,
    off-roster -- would be wrongly revealed by an un-isolate that copied the
    engine's own show path.
    """

    address: int
    id: int
    show: int
    dispatch: int


class UnitWalk(NamedTuple):
    """How far the walk got, and what it may write to.

    `units` holds only nodes that were **validated** -- in main RAM, aligned,
    and not already visited. The walk never derives a write address from a link
    it refused, so "not in a battle" degrades to *found nothing, wrote nothing*
    rather than to a write into garbage.
    """

    units: list
    ended: str                 #: why the walk stopped, in the artist's words
    complete: bool             #: reached the list's own null terminator

    @property
    def found(self) -> int:
        return len(self.units)


def walk_units(client) -> UnitWalk:
    """Follow the unit list from its head, saving each node's two flags.

    **One round trip.** Every `read` here is answered from a single `hold()`
    fetch, so a battle's whole roster costs one GET -- not one per node.
    """
    with client.hold():
        (head,) = struct.unpack("<I", client.read(UNIT_LIST_HEAD, 4))
        units, seen, node = [], set(), head
        while True:
            if node == 0:
                return UnitWalk(units, "the list ended", True)
            if not (RAM_BASE <= node <= RAM_BASE + RAM_BYTES - UNIT_DISPATCH - 2):
                return UnitWalk(units,
                                f"the chain left main RAM at 0x{node:08X}",
                                False)
            if node % 4:
                return UnitWalk(units,
                                f"the chain hit a misaligned node at "
                                f"0x{node:08X}", False)
            if node in seen:
                return UnitWalk(units,
                                f"the chain loops back to 0x{node:08X}", False)
            if len(units) >= UNIT_WALK_CAP:
                return UnitWalk(units,
                                f"the chain is longer than {UNIT_WALK_CAP} "
                                f"nodes, which is not a roster", False)
            seen.add(node)
            units.append(UnitNode(
                address=node,
                id=client.read(node + UNIT_ID, 1)[0],
                show=struct.unpack("<H", client.read(node + UNIT_SHOW, 2))[0],
                dispatch=struct.unpack(
                    "<H", client.read(node + UNIT_DISPATCH, 2))[0]))
            (node,) = struct.unpack("<I", client.read(node + UNIT_NEXT, 4))


def plan_hide_units(units: list) -> list[tuple[int, bytes]]:
    """Zero both flags on every unit the walk validated.

    The engine's own hide, node by node. `+0x1d8` is what takes the sprite and
    its ground shadow off the screen; `+0xa` goes with it because
    `unit_sprite_object_hide` writes the pair, and leaving half of it set would
    put the two out of step with what `{44}`/`{46}` maintain.
    """
    return [(u.address + off, b"\x00\x00")
            for u in units for off in (UNIT_SHOW, UNIT_DISPATCH)]


def plan_restore_units(units: list) -> list[tuple[int, bytes]]:
    """Put both flags back to the values the walk SAVED.

    Not the constant `1` that `unit_sprite_object_show` writes. A unit the
    battle had legitimately hidden reads 0 here, and an un-isolate that wrote
    `1` would reveal it -- a unit not yet cued to appear, one a `{46}` erased,
    one off the roster. The saved value is the only correct restore, and it is
    why `UnitNode` carries the flags rather than just the address.
    """
    return [(u.address + off, struct.pack("<H", value))
            for u in units
            for off, value in ((UNIT_SHOW, u.show),
                               (UNIT_DISPATCH, u.dispatch))]


#: The HUD and the cursor have no flag, so they take a code poke -- the first
#: write this addon makes to the INSTRUCTION STREAM rather than to data. It is
#: named as its own gate kind rather than smuggled in beside the flags.
#:
#: `jr ra` then `nop`, over a function's first two instructions. Safe at an
#: ENTRY and nowhere else: it returns before the prologue builds a frame, so
#: `sp` is never touched. The technique is not new to the package --
#: `workspace/probe496.py` already pokes `0x03e00008` and nops a guard branch.
RETURN_STUB = struct.pack("<II", 0x03E00008, 0x00000000)

#: The bottom-left HP/MP/CT readout. A confirmed function head (`addiu
#: sp,sp,-0x248`) with NO direct `jal` caller -- it is dispatched through a
#: pointer, which is what makes the entry poke the only practical gate rather
#: than merely the easiest.
HUD_RENDERER = 0x801363DC

#: The on-grid knife, and **the uncertain one**. `FUN_8008924C` calls
#: `tile_cursor_bob_render` at 0x80089294, so nulling the caller should take
#: the whole cursor. It is a named constant because the decision record ships
#: the uncertainty instead of asserting the address: the artist's first press
#: resolves it, and *knife gone* / *knife there but not bobbing* / *something
#: else vanished too* are three different answers.
CURSOR_RENDERER = 0x8008924C

#: The second candidate, and probably wrong -- `tile_cursor_bob_render` itself
#: subtracts the table offset from the cursor sprite Y *before* `rotate_vector`,
#: so nulling it likely leaves the knife drawn and unbobbed. One line to swap.
CURSOR_RENDERER_FALLBACK = 0x8007E304

#: There is no data switch to find. The nearest thing is `g_cursor_anim_pause`
#: (0x800960F0) and its own label says it skips the phase/accumulator advance:
#: it FREEZES THE BOB, it does not hide the cursor. Recorded so the next
#: session does not re-find it and mistake it for the gate.
CURSOR_ANIM_PAUSE = 0x800960F0

#: THE CAMERA LEASH, and the reason decision 12's camera push does not stick in
#: a battle. `FUN_8006FE58` runs every frame and integrates a signed velocity
#: into `work_position` -- `DAT_800A1C48` into X, `DAT_800A1C4C` into Y --
#: clamping against the map extent (`DAT_800961B4 * 28 + 14`). So the engine
#: walks the camera back toward whatever it wants to look at, one step per
#: frame, and a pushed pose is overwritten before the artist sees it.
#:
#: Measured [LIVE] 2026-08-28 against `battle_wizard_melee_777`: push
#: `work_position` to (100, 0, 100) with the pad idle and the engine eases it
#: back to (234.9, -18.1, 43.8) over about a second -- 191 units of drift --
#: then holds. With this gate stubbed the same push holds at exactly
#: (100.000, 0.000, 100.000), three pushes running, and restoring the eight
#: bytes puts the drift straight back. That both-arms A/B is what earns the
#: address.
#:
#: Unlike the other two gates this is a LEAF: 141 instructions to its first
#: `jr ra`, with no `jal`, no `sp` adjustment and no `ra` save anywhere in
#: between. There is no frame to half-build, so the entry poke is safer here
#: than at a prologue -- see the code-gate entry test, which checks the two
#: shapes separately rather than demanding `addiu sp,sp,-N` of both.
#:
#: NOT the leash, each cut on its own and measured with the drift unchanged:
#: `FUN_8008B440` (0x8008B440, the countdown-gated glide -- its counter
#: `DAT_8009616A` reads 0 for the whole pull-back), `FUN_800700BC`,
#: `FUN_8006EF00`, `FUN_8008B30C`, `FUN_8008B2C4`. Recorded so the next session
#: does not re-derive the writer set; it is six functions, and five of them are
#: innocent.
CAMERA_LEASH = 0x8006FE58

#: `event_portrait_render_ft4` -- the per-frame builder of the boxed-dialogue
#: POLY_FT4s (frame, text and speaker portrait alike). Stubbing it takes the
#: WHOLE box off the screen, portrait included, and the cutscene keeps running:
#: measured A/B/A against `scenario6_delita_tough_dialogue_pc334`, and three
#: CROSS presses still advanced the scene with the gate cut.
#:
#: Decision 13 shipped boxed dialogue as *the one leg of the ask with no located
#: gate*, and the three functions it named (`event_display_message_handler`
#: 0x801308C0, `event_dialogue_tick` 0x8012F6D4, `event_text_glyph_reader`
#: 0x8014CE80) really are the text pipeline rather than the draw. This is the
#: draw. `dialog_box_compositor` (0x8014C18C) is NOT: it composites once at box
#: open, so cutting it mid-dialogue changes nothing on screen -- measured, same
#: scene, byte-identical picture.
#:
#: **Scoped to boxed dialogue, not to UI in general.** Cut against a battle with
#: the action menu, the unit panel and a damage number on screen, the picture is
#: unchanged. That is why it is a separate gate from the vitals HUD.
#:
#: It supersedes `research/hide_dialogue_box.py`, which hides the box by
#: clearing CLUT 0x7C3C in a savestate: same picture, but offline and it leaves
#: the speaker PORTRAIT drawing, because the portrait samples the unit's own SPR
#: strip at VRAM x832 and not the box palette.
DIALOGUE_BOX_RENDERER = 0x8012E65C


class CodeGate(NamedTuple):
    """A renderer to stub out, and the eight bytes that were there first.

    `saved` is the only way back: unlike the unit flags there is no constant
    that could stand in for a function's real prologue.
    """

    name: str
    address: int
    saved: bytes


CODE_GATES = (("the vitals HUD", HUD_RENDERER),
              ("the tile cursor", CURSOR_RENDERER),
              ("the camera leash", CAMERA_LEASH),
              ("boxed dialogue", DIALOGUE_BOX_RENDERER))


def save_code_gates(client) -> list:
    """Read each renderer's first eight bytes before anything overwrites them."""
    with client.hold():
        return [CodeGate(name, address, client.read(address, 8))
                for name, address in CODE_GATES]


def plan_hide_code(gates: list) -> list[tuple[int, bytes]]:
    """Stub each renderer to an immediate return."""
    return [(g.address, RETURN_STUB) for g in gates]


def plan_restore_code(gates: list) -> list[tuple[int, bytes]]:
    """Put each renderer's own prologue back."""
    return [(g.address, g.saved) for g in gates]


def merge_saved(saved: list, found: list) -> list:
    """The session memory's update rule, for a press that is not the first.

    Keyed on node address. A node already in the memory KEEPS its first saved
    value -- the second walk read back what the first press wrote, and taking
    it would save `show = 0` for the whole roster and turn Restore into a
    no-op that leaves the battle empty. A node not in the memory is new, its
    flags are genuinely the battle's, and it joins.

    This is what makes Isolate re-pressable against a unit spawning mid-battle
    without a ticker and without a readback.
    """
    out = {u.address: u for u in found}
    out.update({u.address: u for u in saved})
    return [out[a] for a in sorted(out)]


def isolate_report(walk: UnitWalk, hidden: int, changed: int) -> str:
    """One sentence, in UNITS. Decision 13's reporting rule.

    Bytes changed is the mechanism's self-check and it stays available, but it
    cannot be the report: `0 changed` already means *already isolated*, so a
    null head reporting the same number would make one sentence mean two
    opposite things. Units found is the second number that keeps them apart,
    and it is what a refusal was going to protect -- recovered without one.
    """
    if not walk.found:
        return ("found no units -- a null list head, which is what not being "
                "in a battle looks like. Nothing was written")
    if walk.complete:
        line = f"hid {hidden} of {walk.found} units"
    else:
        line = (f"hid {hidden} units, then {walk.ended} -- so there may be "
                f"more on screen than this reached")
    if not changed:
        line += " (already isolated; nothing needed writing)"
    return line
