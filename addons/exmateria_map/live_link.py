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

import struct
from typing import NamedTuple

# The document's palette entries are hex colours plus an STP mask, and turning
# those into BGR555 words is `build`'s arithmetic. It is imported rather than
# repeated: a CLUT that reached RAM through different packing than the one that
# reaches the disc would make this loupe lie in the one way it exists to stop.
# Both spellings are needed -- the addon imports this module as a package
# member, the tests and the CLI tools import it as a top-level module.
try:                                     # pragma: no cover - import shape only
    from ._vendor.exmateria_map.document import clut_from_json
except ImportError:                      # pragma: no cover
    from _vendor.exmateria_map.document import clut_from_json

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

import urllib.error      # noqa: E402  -- kept beside the transport it serves
import urllib.request    # noqa: E402

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8080


class TransportError(LiveLinkError):
    """The emulator did not answer, or answered with a Lua error."""


class LuaClient:
    """`PCSX.getMemPtr()` is a writable pointer into main RAM; this reaches it.

    The fork exposes its Lua VM at `/api/v1/lua/exec` when launched with
    `-webserver -webserver-port <N> -dofile <handlers.lua>`.
    """

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.host, self.port = host, port
        self.base = f"http://{host}:{port}/api/v1/lua"

    def exec(self, code: str, timeout: float = 180.0) -> str:
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

    def ping(self) -> bool:
        try:
            with urllib.request.urlopen(self.base + "/ping", timeout=2.0) as r:
                return "pong" in r.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError):
            return False

    def read(self, address: int, length: int) -> bytes:
        """`length` bytes of main RAM from `address`."""
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
    point of the port: only the light rig's GTE half (`apply_gte`, control
    registers `cnt13-15` / `cnt16-20`) genuinely needs the fork, because those
    are not `m_wram` and no HTTP endpoint reaches them. So a session runs this
    for RAM and keeps a `LuaClient` for the rig.

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
        image = self._get()
        changed = 0
        for address, data in writes:
            o = address - RAM_BASE
            changed += sum(1 for a, b in zip(image[o:o + len(data)], data)
                           if a != b)
        for address, data in cluster_writes(writes, image):
            self._post(address - RAM_BASE, data)
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
    untouched by the swap. `apply_gte` is the one leg that cannot move: the GTE
    control registers are not `m_wram` and no HTTP endpoint reaches them.

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


def apply_gte(client: LuaClient, writes: list[tuple[int, int]]) -> int:
    """Write GTE control registers. A different transport from `apply`.

    `apply` walks `PCSX.getMemPtr()`; these are cop2 control registers, which
    live in `PCSX.getRegisters().CP2C`. Keeping them apart is the honest shape:
    a RAM write survives in the map's data and a register write does not, so
    the two have different lifetimes and a caller should know which it made.
    """
    for index, value in writes:
        if not 0 <= index <= 31:
            raise LiveLinkError(f"cop2 control register {index} does not exist")
        if not 0 <= value <= 0xFFFFFFFF:
            raise LiveLinkError(f"0x{value:X} is not a 32-bit value")
    body = " ".join(f"r.CP2C.r[{i}] = {v}" for i, v in writes)
    client.exec(f"local r = PCSX.getRegisters() {body} return \"{len(writes)}\"")
    return len(writes)


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
#: Those six are the palette ANIMATION's work; the chunk that drives it
#: (`mapfile.PALETTE_ANIM_PTR`, 0x70) still has no reader.
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
#: it is while right about what it does. It is not a stale duplicate: the
#: palette-animation routine at `0x8009269C` / `0x800926AC` (one function,
#: `ra = 0x80092794`) writes each animated frame into **both** blocks, entry by
#: entry -- this one at `+0x10`, `CLUT_BLOCK` at `+0x00` of the same loop body.
#: Confirmed by watchpoint, 20 and 60 hits, single writer each.
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
#: Decision 3: the push does not PREDICT which rows will not hold -- it writes,
#: reads back, and names whatever did not. This list is what the check
#: tolerates, never what the report is built from.
CLUT_ANIMATED_MEASURED = (13, 14, 15)


def plan_palettes(palettes) -> list[tuple[int, bytes]]:
    """Writes for `map_states[].palettes`, one per declared CLUT row.

    Decision 10: **push only what the document declares.** `None` plans
    nothing -- 38.5% of corpus states carry no palettes of their own and render
    with a keyed partner's, so a null is a normal document, and refusing the
    whole press for one would strand a state whose SHEET is perfectly pushable.
    A short row writes only the entries it has, for the same reason: what it
    does not declare is not ours to zero (#496 -- zero is the worst fill).

    A row at a time rather than one 512-byte write because a row is the unit
    everything else here happens to: a refusal names one, the readback reports
    one, and the engine's animation overwrites one.
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
        out.append((CLUT_BLOCK + i * CLUT_ROW_BYTES,
                    b"".join(int(w).to_bytes(2, "little") for w in row)))
    return out


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


#: Document fields with no live sink, and why. Decision 4 wants these NAMED on
#: every push rather than silently dropped.
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
