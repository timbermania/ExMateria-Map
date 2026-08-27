"""``build`` -- a schema-v1 interchange document -> the map's resource bundle.

The one leg between an authored map and the disc. Its whole model is one
sentence (schema §11):

    new bytes = base bytes, with the named chunks replaced.

Everything the document does not describe -- the light rig, the grayscale set,
the texture and palette animations, the mesh animations, the unnamed slack --
is carried *at its offset, by construction*. There is no list of things to
carry and no digest promising they survived, because nothing ever reads them.
That is the difference between this writer and a rebuilder: GaneshaDx rebuilds
the resource from its own model and is byte-exact on 0 of 795 mesh resources.

``build`` never sees a disc. It emits the bundle the patcher's
``resolve_map()`` ingests -- the arrangement's GNS with the original LBAs
verbatim, plus one blob per non-pad resource -- and the patcher owns placement
and the ``(lba, length)`` fixup (the #372 contract).

Refusals are named, in schema §10's order. A refusal is never a warning that
got louder: it means ``build`` cannot write bytes it can defend.
"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from typing import NamedTuple

from . import document as schema
from . import mapfile
from .document import (
    AUTHORED_RIG,
    BUCKETS,
    CLUT_WORD_HIGH_BYTE,
    CORPUS_MAX,
    DEFAULT_VISIBLE_ANGLES,
    DRIFT_FIELDS,
    ENGINE_CAPACITY,
    PROPERTY_BYTE_7,
    SLOT_CAPACITY,
    SLOT_TOTAL,
    TEXTURED_BUCKETS,
    clut_from_json,
    encode_record,
)
from .geometry import drifted, ring
from .mapfile import (
    HEADER_BYTES,
    LIGHT_RIG_BYTES,
    PALETTE_CHUNK_BYTES,
    TERRAIN_CHUNK_BYTES,
    TERRAIN_RECORD_BYTES,
    TEXTURE_BYTES,
    VISIBLE_ANGLES_BYTES,
    VISIBLE_ANGLES_HEADER_BYTES,
)
from .png_indexed import pack_4bpp, read_indexed_png

#: Decision 10's axis ceiling. A binding naming a coordinate at or past it
#: names a tile no legal grid can ever hold, so it is not a binding at all --
#: it is the idle value a polygon carries when it is off the grid. The export
#: leg gates its warning on exactly this (``export_document.names_a_tile``);
#: the two legs must agree or one of them is lying about the same document.
GROWTH_AXIS_MAX = 18
#: `walkable` False. NOT schema §5.2's worked example {255, 127, 0}, which is
#: FF FE -- different bytes, and both carry verbatim.
SENTINEL_BINDING = {"x": 255, "z": 127, "level": 1}

#: #357's nine unexplained maps: the named suppression list for the
#: floor(centroid/28) drift check (schema §10).
DRIFT_CHECK_SUPPRESSED = frozenset({
    "MAP000", "MAP034", "MAP039", "MAP083", "MAP093",
    "MAP097", "MAP098", "MAP104", "MAP118",
})


class BuildRefusal(RuntimeError):
    """``build`` will not write bytes it cannot defend. Never caught internally."""


def _refuse(message: str) -> None:
    raise BuildRefusal(message)


# ---------------------------------------------------------------------------
# chunk writers (schema §6)
# ---------------------------------------------------------------------------

def bucketed(polygons: list[dict]) -> dict[str, list[dict]]:
    """The document's flat list split into its four buckets, order preserved."""
    out: dict[str, list[dict]] = {k: [] for k in BUCKETS}
    for index, poly in enumerate(polygons):
        kind = poly.get("kind")
        if kind not in out:
            _refuse(f"polygon {index}: unknown kind {kind!r}")
        out[kind].append(poly)
    return out


def pack_primary_mesh(polygons: list[dict]) -> bytes:
    """The 0x40 section (schema §6.1).

    The four u16 counts are *derived* from the list, never read off the
    document: decision 19 forbids storing a number the list already carries,
    and a stored count is a number that can drift from the thing it counts.
    """
    by = bucketed(polygons)
    out = bytearray(struct.pack("<HHHH", *(len(by[k]) for k in BUCKETS)))

    for kind in BUCKETS:
        nverts = schema.VERTS[kind]
        for poly in by[kind]:
            pos = poly["positions"]
            if len(pos) != nverts:
                _refuse(f"a {kind} carries {len(pos)} vertices, not {nverts}")
            for v in pos:
                out += struct.pack("<hhh", *v)

    for kind in TEXTURED_BUCKETS:
        for poly in by[kind]:
            for v in poly["normals"]:
                out += struct.pack("<hhh", *v)

    for kind in TEXTURED_BUCKETS:
        for poly in by[kind]:
            uv = poly["uv"]
            b2 = (poly["palette_id"] & 0x0F) | ((poly["palette_byte_high_nibble"] & 0x0F) << 4)
            b6 = ((poly["texture_page"] & 0x03)
                  | ((poly["unknown_texture_value_6a"] & 0x03) << 2)
                  | ((poly["texture_byte6_high_nibble"] & 0x0F) << 4))
            out += bytes([uv[0][0], uv[0][1], b2, CLUT_WORD_HIGH_BYTE,
                          uv[1][0], uv[1][1], b6, PROPERTY_BYTE_7,
                          uv[2][0], uv[2][1]])
            if kind == BUCKETS[1]:
                out += bytes([uv[3][0], uv[3][1]])

    for kind in (BUCKETS[2], BUCKETS[3]):
        for poly in by[kind]:
            raw = poly["unknown_untextured"]
            if len(raw) != 4:
                _refuse(f"a {kind} carries {len(raw)} unknown bytes, not 4")
            out += bytes(raw)

    for kind in TEXTURED_BUCKETS:
        for poly in by[kind]:
            t = poly["terrain"]
            out += bytes([((t["z"] & 0x7F) << 1) | (t["level"] & 1), t["x"] & 0xFF])

    return bytes(out)


def pack_visible_angles(polygons: list[dict], header: bytes,
                        slots: list[int]) -> bytes:
    """The fixed 4,096-byte 0xB0 chunk (schema §6.2).

    ``slots`` is the WHOLE base table, 1,600 u16. The document's live values
    are overlaid onto it and every slot the document does not describe keeps
    the base's value -- no truncation, no manufactured slot (decision 19).
    That is what makes adding and removing a polygon symmetric: the table's
    shape never depends on how many polygons there happen to be.
    """
    if len(header) != VISIBLE_ANGLES_HEADER_BYTES:
        _refuse(f"the 0xB0 header is {len(header)} bytes, not "
                f"{VISIBLE_ANGLES_HEADER_BYTES}")
    if len(slots) != SLOT_TOTAL:
        _refuse(f"the 0xB0 slot table holds {len(slots)} slots, not {SLOT_TOTAL}")

    table = list(slots)
    by = bucketed(polygons)
    base = 0
    for kind, capacity in SLOT_CAPACITY:
        for row, poly in enumerate(by[kind]):
            value = poly.get("visible_angles")
            if value is None:
                continue          # no 0xB0 chunk at dump time: nothing to overlay
            table[base + row] = value & 0xFFFF
        base += capacity

    out = bytearray(header)
    for word in table:
        out += struct.pack("<H", word)
    assert len(out) == VISIBLE_ANGLES_BYTES
    return bytes(out)


def pack_palette_chunk(palettes: list[dict]) -> bytes:
    """The 512-byte 0x44 chunk: 16 CLUTs x 16 BGR555 words (schema §6.4)."""
    if len(palettes) != 16:
        _refuse(f"a palette chunk holds 16 CLUTs, not {len(palettes)}")
    out = bytearray()
    for clut in palettes:
        for word in clut_from_json(clut):
            out += struct.pack("<H", word)
    assert len(out) == PALETTE_CHUNK_BYTES
    return bytes(out)


def pack_terrain_chunk(base_payload: bytes, size_x: int, size_z: int,
                       records: list[tuple[int, dict]]) -> bytes:
    """The 4,098-byte 0x68 payload (schema §6.3).

    ``records`` is ``[(slot index, declared fields), ...]``, already classified.
    The payload starts as the base's, so the pad past ``SizeX*SizeZ`` and every
    undeclared field reproduce verbatim (decisions 19, 20, 21) -- ``build``
    stamps a default only into a slot that already holds it, which is a no-op.
    """
    if len(base_payload) != TERRAIN_CHUNK_BYTES:
        _refuse(f"the base 0x68 payload is {len(base_payload)} bytes, not "
                f"{TERRAIN_CHUNK_BYTES}")
    out = bytearray(base_payload)
    out[0], out[1] = size_x & 0xFF, size_z & 0xFF
    for index, declared in records:
        start = 2 + index * TERRAIN_RECORD_BYTES
        out[start:start + TERRAIN_RECORD_BYTES] = encode_record(
            bytes(out[start:start + TERRAIN_RECORD_BYTES]), declared)
    return bytes(out)


# ---------------------------------------------------------------------------
# the splice
# ---------------------------------------------------------------------------

def splice(data: bytes, replacements: list[tuple[int, int, bytes]],
           manufactured: tuple[int, bytes] | None = None) -> bytes:
    """``data`` with each ``[start, end)`` replaced, and the header fixed up.

    Replacements must not overlap. When a replacement changes length every
    later section moves, so the 49-slot pointer table is rewritten by the
    accumulated delta -- a pointer *at* a replaced range's start does not move
    (it still points at the same chunk), a pointer at or past its end does.

    When no length changes -- the identity path, and every edit that does not
    add or remove a polygon -- the header is untouched and the output is the
    input with some bytes swapped. That is the property the round-trip
    instrument measures.

    ``manufactured`` is ``(header slot, chunk)``: a section that does not exist
    in ``data`` at all, appended past the last byte, whose **zero** pointer slot
    becomes the offset it landed at (ADR-0004 decision 26). It is not a
    replacement at ``len(data)`` because the offset it must be told is known
    only *after* every earlier delta is applied, and because a replacement
    would model it as a span of the base that it is not. Turning a zero pointer
    into a real one is the one thing this function does that shifting cannot:
    a zero slot is skipped by the fixup loop below, by construction -- an
    absent section has no offset to move.
    """
    ordered = sorted(replacements)
    out = bytearray()
    position = 0
    delta = 0
    shifts: list[tuple[int, int]] = []
    for start, end, new in ordered:
        if start < position:
            _refuse(f"overlapping chunk replacements at offset {start}")
        if not 0 <= start <= end <= len(data):
            _refuse(f"replacement [{start}, {end}) is outside a {len(data)}-byte resource")
        out += data[position:start]
        out += new
        delta += len(new) - (end - start)
        shifts.append((end, delta))
        position = end
    out += data[position:]

    moves = delta != 0 or any(d != 0 for _, d in shifts)
    if len(data) < HEADER_BYTES or not (moves or manufactured):
        return bytes(out)
    if moves:
        for slot in range(0, HEADER_BYTES, 4):
            pointer = struct.unpack_from("<I", data, slot)[0]
            if pointer == 0:
                continue
            moved = 0
            for start, end, _new in ordered:
                if start < pointer < end:
                    _refuse(
                        f"section pointer at slot 0x{slot:02X} is {pointer}, "
                        f"inside the rewritten chunk [{start}, {end}) whose length "
                        f"changed; `build` cannot say where it should land"
                    )
            for boundary, cumulative in shifts:
                if pointer >= boundary:
                    moved = cumulative
            struct.pack_into("<I", out, slot, pointer + moved)

    if manufactured is not None:
        slot, chunk = manufactured
        standing = struct.unpack_from("<I", data, slot)[0]
        if standing != 0:
            _refuse(f"cannot manufacture a section into slot 0x{slot:02X}: it "
                    f"already points at {standing}. A manufactured section is "
                    f"only ever an ABSENT one (decision 26)")
        struct.pack_into("<I", out, slot, len(out))
        out += chunk
    return bytes(out)


# ---------------------------------------------------------------------------
# the bundle
# ---------------------------------------------------------------------------

class Bundle(NamedTuple):
    name: str                          # "MAP022.a0"
    gns_name: str                      # "MAP022.GNS"
    gns: bytes
    resources: dict[str, bytes]        # base.resources order
    warnings: list[str]
    #: resource name -> the ``[start, end)`` spans of the BASE file that were
    #: written from the document rather than carried. The round-trip
    #: instrument's carry ratchet is measured off this, so a chunk that quietly
    #: stops being written shows up as carry *rising*, which fails the harness.
    modelled: dict[str, list[tuple[int, int]]]

    def write(self, directory: Path) -> Path:
        """Write the patcher's bundle directory (map-leg-v1 §1.2)."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / self.gns_name).write_bytes(self.gns)
        for name, data in self.resources.items():
            (directory / name).write_bytes(data)
        return directory


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_version(document: dict) -> None:
    """Schema §10.1 / §2. Refused before anything else is read."""
    fmt = document.get("format")
    if fmt != schema.FORMAT:
        _refuse(f"format is {fmt!r}, not {schema.FORMAT!r}")
    version = document.get("version")
    if version not in schema.ACCEPTED_VERSIONS:
        accepted = ", ".join(str(v) for v in schema.ACCEPTED_VERSIONS)
        _refuse(f"version is {version!r}; this `build` accepts {accepted}. "
                f"`version` is the OLDEST `build` that can handle the document "
                f"(decision 27), so a higher one is refused, never guessed at")


def _rings(polygons: list[dict]) -> list[list]:
    """Ring-ordered position lists, bucket order -- the drift check's input."""
    by = bucketed(polygons)
    return [ring(p["positions"]) for kind in BUCKETS for p in by[kind]]


def _out_of_grid_warning(polygons: list[dict], grid: dict | None) -> str | None:
    """Decision 9: both ends warn, neither refuses, nothing is rewritten.

    A sentinel binding is not a binding, and a value no legal grid can hold is
    not a binding either (99.5% of what a plain extent test flags). The
    suppressed count rides the message so nothing is dropped silently.
    """
    if not grid:
        return None
    size_x, size_z = grid.get("size_x"), grid.get("size_z")
    if not isinstance(size_x, int) or not isinstance(size_z, int):
        return None
    bad, unreachable = [], 0
    for index, poly in enumerate(polygons):
        t = poly.get("terrain")
        if not t or t == SENTINEL_BINDING:
            continue
        if 0 <= t["x"] < size_x and 0 <= t["z"] < size_z:
            continue
        if not (t["x"] < GROWTH_AXIS_MAX and t["z"] < GROWTH_AXIS_MAX):
            unreachable += 1
            continue
        bad.append((index, t))
    if not bad:
        return None
    shown = ", ".join(f"#{i} -> ({t['x']}, {t['z']}, L{t['level']})"
                      for i, t in bad[:8])
    more = f" (+{len(bad) - 8} more)" if len(bad) > 8 else ""
    tail = (f"; {unreachable} further binding(s) name a tile no legal grid "
            f"can hold and are not counted") if unreachable else ""
    return (f"{len(bad)} terrain binding(s) outside the {size_x}x{size_z} "
            f"grid: {shown}{more}{tail}")


def _binding_drift_warning(map_name: str, polygons: list[dict]) -> str | None:
    """#357's floor(centroid/28) check, with the nine unexplained maps named.

    Three classes are excluded because #357 measured that the rule does not
    speak for them: the FF FF sentinel, (0, 0), and the byte0 == byte1 filler.
    """
    if map_name in DRIFT_CHECK_SUPPRESSED:
        return None
    wrong = []
    for index, poly in enumerate(polygons):
        t = poly.get("terrain")
        if not t:
            continue
        x, z, level = t["x"], t["z"], t["level"]
        if (x == 255 and z == 127) or (x == 0 and z == 0) or ((z << 1) | level) == x:
            continue
        pos = poly["positions"]
        n = len(pos)
        derived = (int(sum(v[0] for v in pos) / n // schema.TILE_UNITS),
                   int(sum(v[2] for v in pos) / n // schema.TILE_UNITS))
        if derived != (x, z):
            wrong.append((index, (x, z), derived))
    if not wrong:
        return None
    shown = ", ".join(f"#{i} binds ({bx}, {bz}) but its centroid is at {d}"
                      for i, (bx, bz), d in wrong[:8])
    more = f" (+{len(wrong) - 8} more)" if len(wrong) > 8 else ""
    return f"{len(wrong)} polygon(s) drifted off their terrain binding: {shown}{more}"


def _classify_terrain(document: dict, base_extent: tuple[int, int],
                      base_rings: list, doc_rings: list) -> tuple[int, int, list]:
    """Schema §7.2 / §10.5: the target extent and the records to write.

    ``base.floor_steps`` is out of reach here (schema §4: ``dump`` computes it,
    ``build`` ignores it), so the drift set is recomputed from the two things
    ``build`` does trust -- the document's polygons and the base map's own
    geometry. It is the same rule ``authoring.drifted`` runs, which is what
    keeps "a tile the addon flagged" and "a tile ``build`` accepts a fix for"
    the same set.
    """
    records = document.get("terrain") or []
    grid = document["base"].get("terrain_grid") or {}
    size_x = int(grid.get("size_x", base_extent[0]))
    size_z = int(grid.get("size_z", base_extent[1]))
    if size_x < 1 or size_z < 1:
        _refuse(f"base.terrain_grid is {size_x}x{size_z}; an extent has at "
                f"least one tile on each axis")
    if 2 + 2 * size_x * size_z > TERRAIN_CHUNK_BYTES:
        _refuse(f"base.terrain_grid {size_x}x{size_z} needs "
                f"{2 + 2 * size_x * size_z} bytes; the 0x68 chunk holds "
                f"{TERRAIN_CHUNK_BYTES}")
    if size_x < base_extent[0] or size_z < base_extent[1]:
        _refuse(f"base.terrain_grid {size_x}x{size_z} shrinks the base's "
                f"{base_extent[0]}x{base_extent[1]} grid; shrink is refused "
                f"(decision 10)")

    drift = drifted(base_rings, doc_rings, base_extent[0], base_extent[1]) if records else {}

    out = []
    seen = set()
    for record in records:
        try:
            x, z, level = int(record["x"]), int(record["z"]), int(record["level"])
        except (KeyError, TypeError, ValueError):
            _refuse(f"terrain record {record!r} is missing x/z/level")
        where = f"terrain record ({x}, {z}, L{level})"
        if level not in (0, 1):
            _refuse(f"{where}: level is 0 or 1")
        if (x, z, level) in seen:
            _refuse(f"{where}: declared twice; one tile has one record")
        seen.add((x, z, level))
        if not (0 <= x < size_x and 0 <= z < size_z):
            _refuse(f"{where}: outside the document's {size_x}x{size_z} "
                    f"extent -- there is no byte to write it to")
        try:
            declared = {k: int(v) for k, v in record.items()
                        if k not in schema.RECORD_KEYS}
        except (TypeError, ValueError):
            _refuse(f"{where}: a payload field is not an integer -- "
                    f"{ {k: v for k, v in record.items() if k not in schema.RECORD_KEYS} }")
        unknown = [k for k in declared if k not in schema.RECORD_FIELDS]
        if unknown:
            _refuse(f"{where}: unknown payload field(s) {', '.join(sorted(unknown))}")
        if not declared:
            continue                     # {x, z, level} and nothing else is not a record

        pre_growth = x < base_extent[0] and z < base_extent[1]
        if pre_growth:
            if (x, z) not in drift:
                _refuse(f"{where}: that tile is still the base's -- it is "
                        f"inside the pre-growth {base_extent[0]}x"
                        f"{base_extent[1]} extent and the drift checker does "
                        f"not name it (decisions 3, 11, 12)")
            illegal = [k for k in declared if k not in DRIFT_FIELDS]
            if illegal:
                _refuse(f"{where}: a drift record may declare only "
                        f"{', '.join(DRIFT_FIELDS)}; it declares "
                        f"{', '.join(sorted(illegal))} (decision 23)")
        try:
            encode_record(bytes(TERRAIN_RECORD_BYTES), declared)   # range check
        except ValueError as exc:
            _refuse(f"{where}: {exc}")
        out.append((z * size_x + x, declared))
    return size_x, size_z, out


def build(document: dict, map_dir: Path, sidecar_dir: Path | None = None) -> Bundle:
    """The document's resource bundle, verified in schema §10's order."""
    # --- §10.1 format and version -----------------------------------------
    _check_version(document)

    base = document.get("base") or {}
    map_name = base.get("map")
    arrangement = base.get("arrangement")
    if not isinstance(map_name, str) or not map_name.startswith("MAP"):
        _refuse(f"base.map is {map_name!r}")
    if not isinstance(arrangement, int):
        _refuse(f"base.arrangement is {arrangement!r}")

    files = mapfile.bind(Path(map_dir), int(map_name[3:6]))
    rows = [r for r in files.arrangement_rows(arrangement) if not r.is_pad]
    if not rows:
        _refuse(f"{map_name} has no non-pad rows in arrangement {arrangement}")

    # --- §10.2 base identity ----------------------------------------------
    declared_resources = base.get("resources") or []
    on_disc, seen = [], set()
    for row in rows:
        name = files.by_sector[row.sector].name
        if name not in seen:
            seen.add(name)
            on_disc.append(name)
    if [r.get("name") for r in declared_resources] != on_disc:
        _refuse(f"{map_name} a{arrangement}: base.resources names "
                f"{[r.get('name') for r in declared_resources]}, the disc's "
                f"arrangement holds {on_disc}")

    raw: dict[str, bytes] = {}
    for entry in declared_resources:
        name = entry["name"]
        data = files.path(name).read_bytes()
        digest = _digest(data)
        declared = entry.get("sha256")
        if digest != declared:
            # A short digest that still PREFIXES the real one is not a
            # different base map -- it is a document from before schema v1
            # pinned `sha256` as the whole digest. Saying "this is not the
            # base map" there sends the artist hunting a disc that is fine.
            if (isinstance(declared, str) and len(declared) < 64
                    and declared and digest.startswith(declared)):
                _refuse(f"{name}: base.sha256 is {len(declared)} hex chars, "
                        f"not 64 -- this document predates schema v1 "
                        f"(`workspace/schemav1.py` truncated the digest). The "
                        f"base map matches; re-dump with `exmateria-map-dump` "
                        f"and re-author, or the four fields in schema §13 are "
                        f"all wrong in it")
            _refuse(f"{name}: base.sha256 is {declared}, the file on disk "
                    f"digests to {digest} -- this is not the base map `dump` "
                    f"came from (decision 5)")
        raw[name] = data

    geometry_source = base.get("geometry_source")
    if geometry_source not in raw:
        _refuse(f"base.geometry_source {geometry_source!r} is not one of the "
                f"arrangement's resources")
    geometry_data = raw[geometry_source]
    base_mesh = mapfile.read_mesh(geometry_data)
    if base_mesh is None:
        _refuse(f"{geometry_source} carries no 0x40 chunk")
    got = mapfile.mesh_digest(geometry_data, base_mesh)
    if got != base.get("geometry_digest"):
        _refuse(f"{geometry_source}: base.geometry_digest is "
                f"{base.get('geometry_digest')}, its 0x40 section digests to "
                f"{got} -- this is not the base map `dump` came from "
                f"(decision 5)")

    terrain_source = base.get("terrain_source")
    base_payload = base_extent = None
    if terrain_source is not None:
        if terrain_source not in raw:
            _refuse(f"base.terrain_source {terrain_source!r} is not one of the "
                    f"arrangement's resources")
        offset = mapfile.terrain_offset(raw[terrain_source])
        if offset is None:
            _refuse(f"{terrain_source} carries no valid 0x68 chunk")
        base_payload = mapfile.terrain_payload(raw[terrain_source], offset)
        base_extent = (base_payload[0], base_payload[1])
        got = _digest(base_payload)
        if got != base.get("terrain_digest"):
            _refuse(f"{terrain_source}: base.terrain_digest is "
                    f"{base.get('terrain_digest')}, its 4,098-byte payload "
                    f"digests to {got}")
    elif document.get("terrain"):
        _refuse(f"{map_name} a{arrangement} declares {len(document['terrain'])} "
                f"terrain record(s) but base.terrain_source is null -- there is "
                f"no chunk to write them to")

    # --- §10.3 pointers ----------------------------------------------------
    for name, data in raw.items():
        if len(data) == TEXTURE_BYTES:
            continue                      # a sheet is a raw blob, not a header
        for slot, pointer in mapfile.section_pointers(data).items():
            if pointer >= len(data):
                _refuse(f"{name}: section pointer at slot 0x{slot:02X} is "
                        f"{pointer}, at or past the {len(data)}-byte EOF "
                        f"(decision 22)")

    # --- §10.4 polygon capacity (decision 28) ------------------------------
    # NOT the 0xB0 slot table. The engine's four arrays are smaller than it on
    # the textured buckets, and the loader appends to them at shared, unchecked
    # cursors -- shared with the base's AnimatedMesh1-8, which is why the bound
    # is on the SUM. A slot with no array behind it is still read and discarded,
    # so SLOT_CAPACITY stays exactly where it is: describing the table.
    polygons = document.get("polygons") or []
    by = bucketed(polygons)
    base_anim = dict(zip(BUCKETS, mapfile.animated_mesh_counts(geometry_data)))
    corpus_max = dict(CORPUS_MAX)
    untested = []
    for kind, capacity in ENGINE_CAPACITY:
        declared = len(by[kind])
        total = declared + base_anim[kind]
        if total > capacity:
            _refuse(f"{geometry_source}: {declared} {kind}s in the document "
                    f"plus {base_anim[kind]} in the base's AnimatedMesh "
                    f"sections is {total}; the engine's array holds "
                    f"{capacity} and the loader does not bound-check it "
                    f"(decision 28). The 0xB0 slot table's "
                    f"{dict(SLOT_CAPACITY)[kind]} is not the bound")
        if total > corpus_max[kind]:
            untested.append(f"{kind} {total} (corpus max {corpus_max[kind]}, "
                            f"engine {capacity})")
    capacity_warning = (
        f"{geometry_source}: above the corpus maximum and at or below the "
        f"engine's array -- no shipped map has gone here (decision 28): "
        + "; ".join(untested)) if untested else None

    # --- §6.2 the one manufacture (decision 26) ----------------------------
    # 10 of 169 geometry-carrying resources have no 0xB0 chunk, and this is the
    # ONE section the writer may bring into existence -- decision 19 stands
    # unamended everywhere else. The triggers are narrow because manufacturing
    # is not merely accommodation: FUN_800f4dd4 writes every polygon field
    # EXCEPT +0x0e, so with no chunk the mask is left stale from the previous
    # map, and a manufactured table makes the resource's *existing* polygons
    # deterministic for the first time. That is a behavioural change to a
    # shipped map, accepted deliberately -- adding a quad to a chunkless base
    # is not something an artist does by accident.
    base_vis = mapfile.visible_angles_offset(geometry_data)
    manufacture = None
    if base_vis is None:
        base_counts = dict(zip(BUCKETS, base_mesh.counts))
        added = [k for k in BUCKETS if len(by[k]) > base_counts[k]]
        authored = sum(1 for p in polygons if p.get("visible_angles") is not None)
        reasons = ([f"the document adds {', '.join(added)} polygons"] if added
                   else [])
        if authored:
            reasons.append(f"{authored} polygon(s) carry a visible_angles mask")
        if reasons:
            # A warning, never a refusal, and it cannot be one: the cost is a
            # relocation the patcher owns and `build` never sees the recipe.
            # Measured -- 0 of 10 of these resources have room for 4,096 B in
            # place, and of 1,453 inter-resource gaps in the map tree exactly
            # 2 are that large, so the relocation is unconditional.
            manufacture = (
                f"{geometry_source} carries no 0xB0 chunk and "
                f"{' and '.join(reasons)}; `build` manufactures a whole "
                f"{VISIBLE_ANGLES_BYTES}-byte chunk (ADR-0004 decision 26). "
                f"The resource grows by {VISIBLE_ANGLES_BYTES} B and no "
                f"chunkless resource has room for that in place -- the patcher "
                f"needs allow_relocate = true and [free_space].ranges (#522)")

    # --- §10.6 fan-out correspondence -------------------------------------
    mesh_targets, vis_targets, palette_targets, terrain_targets = [], [], [], []
    for name, data in raw.items():
        if len(data) == TEXTURE_BYTES:
            continue
        mesh = mapfile.read_mesh(data)
        if mesh is not None:
            got = mapfile.mesh_digest(data, mesh)
            if got != base["geometry_digest"]:
                _refuse(f"{name}: its 0x40 section digests to {got}, the "
                        f"document's base.geometry_digest is "
                        f"{base['geometry_digest']} -- the arrangement's mesh "
                        f"resources are byte-identical on the disc and `build` "
                        f"never silently rewrites one into the other "
                        f"(decision 2)")
            mesh_targets.append((name, mesh))
        vis = mapfile.visible_angles_offset(data)
        if vis is not None:
            chunk = data[vis:vis + VISIBLE_ANGLES_BYTES]
            if base_vis is not None:
                reference = geometry_data[base_vis:base_vis + VISIBLE_ANGLES_BYTES]
                if chunk != reference:
                    _refuse(f"{name}: its 0xB0 chunk differs from "
                            f"{geometry_source}'s; all 16 multi-row "
                            f"arrangements carry byte-identical chunks and "
                            f"`build` re-checks it rather than picking one "
                            f"(schema §8)")
            vis_targets.append((name, vis))
        if mapfile.palette_offset(data) is not None:
            palette_targets.append(name)
        offset = mapfile.terrain_offset(data)
        if offset is not None:
            payload = mapfile.terrain_payload(data, offset)
            if base_payload is None or payload != base_payload:
                _refuse(f"{name}: carries a valid 0x68 chunk that is not "
                        f"{terrain_source}'s; the corpus holds one distinct "
                        f"valid chunk per arrangement (schema §7.3)")
            terrain_targets.append((name, offset))

    # --- the new chunks ----------------------------------------------------
    new_mesh = pack_primary_mesh(polygons)

    carry = document.get("carry") or {}
    new_vis = None
    if base_vis is not None:
        header_hex = carry.get("visible_angles_unknown_896")
        header = (bytes.fromhex(header_hex) if header_hex else
                  geometry_data[base_vis:base_vis + VISIBLE_ANGLES_HEADER_BYTES])
        slots_hex = carry.get("visible_angles_slots")
        if slots_hex:
            blob = bytes.fromhex(slots_hex)
            if len(blob) != SLOT_TOTAL * 2:
                _refuse(f"carry.visible_angles_slots holds {len(blob)} bytes, "
                        f"not {SLOT_TOTAL * 2}")
            slots = [int.from_bytes(blob[i * 2:i * 2 + 2], "little")
                     for i in range(SLOT_TOTAL)]
        else:
            # The addon's export leaves `carry` null; `build` refills it from
            # the base's own chunk (schema §11).
            o = base_vis + VISIBLE_ANGLES_HEADER_BYTES
            slots = [mapfile.u16(geometry_data, o + i * 2) for i in range(SLOT_TOTAL)]
        new_vis = pack_visible_angles(polygons, header, slots)
    elif manufacture is not None:
        # Decision 26's fixed blob: the corpus's 896-B header, then 1,600 slots,
        # every one the disc's own dead fill except the ones the document
        # authors. There is nothing to carry and nothing to overlay from -- both
        # `carry` keys dump `null` when the base has no chunk (schema §8) -- so
        # the header is the only byte here that comes from neither side.
        new_vis = pack_visible_angles(polygons, mapfile.visible_angles_header(),
                                      [DEFAULT_VISIBLE_ANGLES] * SLOT_TOTAL)

    states = document.get("map_states") or []
    if len(states) != len(rows):
        _refuse(f"{map_name} a{arrangement}: map_states holds {len(states)} "
                f"entries, the arrangement has {len(rows)} non-pad GNS rows -- "
                f"type-49 pad rows are out of the document and carried whole "
                f"(schema §7.1, #525)")

    states_by_resource: dict[str, dict] = {}
    for state in states:
        name = state.get("resource")
        if name not in raw:
            _refuse(f"map_states names {name!r}, which is not one of the "
                    f"arrangement's resources")
        previous = states_by_resource.get(name)
        if previous is not None:
            for key in ("palettes", "texture_sheet", AUTHORED_RIG):
                if previous.get(key) != state.get(key):
                    _refuse(f"{name}: two map_states rows name the same "
                            f"resource with different {key}; one file cannot "
                            f"hold both")
        states_by_resource[name] = state
    missing = [n for n in raw if n not in states_by_resource]
    if missing:
        _refuse(f"map_states has no entry for {', '.join(sorted(missing))}")

    new_palettes: dict[str, bytes] = {}
    for name in palette_targets:
        entry = states_by_resource[name].get("palettes")
        if not entry:
            _refuse(f"{name}: carries a valid 0x44 chunk but its map_states "
                    f"entry has no palettes")
        new_palettes[name] = pack_palette_chunk(entry)
    for name, state in states_by_resource.items():
        if state.get("palettes") and name not in new_palettes:
            _refuse(f"{name}: map_states declares palettes but the resource "
                    f"has no valid 0x44 chunk to write them to")

    # --- the authored light rig (decision 27) ------------------------------
    # The one field the addon WRITES BACK. `light_rig` beside it keeps §7.1's
    # standing exactly -- derived, information-bearing, ignored here -- and it
    # is the PRESENCE of this second field that declares an authored rig, so an
    # untouched document reaches none of this and the identity trip is
    # untouched by construction.
    declared_rigs = [(index, state) for index, state in enumerate(states)
                     if state.get(AUTHORED_RIG) is not None]
    if declared_rigs and document.get("version", 0) < schema.AUTHORED_RIG_VERSION:
        _refuse(f"{len(declared_rigs)} map_states entry(s) declare "
                f"{AUTHORED_RIG} but the document stamps version "
                f"{document.get('version')!r}; an authored rig needs "
                f"{schema.AUTHORED_RIG_VERSION}, because `version` is the "
                f"oldest `build` that can honour the document and a v1 `build` "
                f"would IGNORE the field and drop the artist's lighting "
                f"(schema §2, decision 27)")

    new_rigs: dict[str, tuple[int, bytes]] = {}
    for index, state in declared_rigs:
        name = state["resource"]
        # Kind first, and from the document's own GNS type code -- a rig lives
        # at 0x64 of a MESH resource and a texture row has none BY KIND. The
        # reader's pointer-shaped test is not a kind test: four shipped texture
        # states (MAP062 a0) hold `f0 0f 00 00` there, which is 4,080 sheet
        # PIXELS read as a plausible pointer (#576). That reached no disc only
        # while `build` ignored the field, which is what this block removes.
        if state.get("kind") not in mapfile.GNS_MESH_TYPES:
            _refuse(f"map_states[{index}] ({name}) declares {AUTHORED_RIG} but "
                    f"its GNS type is {state.get('kind')!r}, not a mesh type "
                    f"{mapfile.GNS_MESH_TYPES}; a rig lives at 0x64 of a MESH "
                    f"resource and a texture row has none, by kind "
                    f"(decision 27's correction, #576)")
        offset = mapfile.light_rig_offset(raw[name], True)
        if offset is None:
            _refuse(f"map_states[{index}] ({name}) declares {AUTHORED_RIG} but "
                    f"the resource's 0x64 pointer is zero -- there are no 45 "
                    f"bytes to overwrite and creating them is the byte decision "
                    f"19 forbids (decision 26's exception is the 0xB0 chunk "
                    f"ALONE). 13 mesh rows corpus-wide are in this position and "
                    f"the bake skips and reports them rather than declaring one")
        rig = state[AUTHORED_RIG]
        base_rig = mapfile.read_light_rig(raw[name], True)
        if list(rig.get("gradient") or []) != base_rig["gradient"]:
            _refuse(f"map_states[{index}] ({name}): {AUTHORED_RIG}.gradient is "
                    f"{list(rig.get('gradient') or [])}, the base's is "
                    f"{base_rig['gradient']}. An authored rig ECHOES the "
                    f"state's own gradient bytes verbatim -- the solve owns 39 "
                    f"bytes and carries 6 -- which is what keeps decision 25's "
                    f"parity boundary where it was (decision 27)")
        try:
            new_rigs[name] = (offset, mapfile.pack_light_rig(rig))
        except ValueError as e:
            _refuse(f"map_states[{index}] ({name}): {AUTHORED_RIG} is not a "
                    f"45-byte rig -- {e}")

    new_terrain = None
    if base_payload is not None:
        base_rings = [ring(p) for key in ("tt", "tq", "ut", "uq")
                      for p in base_mesh.positions[key]]
        size_x, size_z, records = _classify_terrain(
            document, base_extent, base_rings, _rings(polygons))
        new_terrain = pack_terrain_chunk(base_payload, size_x, size_z, records)

    # --- the splice --------------------------------------------------------
    resources: dict[str, bytes] = {}
    replacements: dict[str, list[tuple[int, int, bytes]]] = {n: [] for n in raw}
    for name, mesh in mesh_targets:
        replacements[name].append((mesh.start, mesh.end, new_mesh))
    if new_vis is not None and base_vis is not None:
        for name, offset in vis_targets:
            replacements[name].append((offset, offset + VISIBLE_ANGLES_BYTES, new_vis))
    for name, chunk in new_palettes.items():
        offset = mapfile.palette_offset(raw[name])
        replacements[name].append((offset, offset + PALETTE_CHUNK_BYTES, chunk))
    for name, (offset, blob) in new_rigs.items():
        replacements[name].append((offset, offset + LIGHT_RIG_BYTES, blob))
    if new_terrain is not None:
        for name, offset in terrain_targets:
            replacements[name].append((offset, offset + TERRAIN_CHUNK_BYTES, new_terrain))

    modelled: dict[str, list[tuple[int, int]]] = {}
    for name, data in raw.items():
        if len(data) == TEXTURE_BYTES:
            resources[name] = _sheet_bytes(states_by_resource[name], name,
                                           data, sidecar_dir)
            modelled[name] = [] if sidecar_dir is None else [(0, len(data))]
            continue
        # The manufacture reaches the geometry source and nothing else. It is
        # the resource whose absent chunk decisions 5-6 need, and §10 rule 6
        # can never trip on it: each of the nine affected arrangements has
        # exactly one geometry-carrying mesh row and it is the chunkless one.
        # MAP083 a0 is the case that names the scope -- its source MAP083.9
        # HAS a chunk, so its chunkless sibling MAP083.10 is never written to.
        appended = ((mapfile.VISIBLE_ANGLES_PTR, new_vis)
                    if manufacture is not None and name == geometry_source
                    else None)
        resources[name] = splice(data, replacements[name], manufactured=appended)
        # A manufactured chunk covers no span of the base, so it contributes
        # nothing here by definition -- `modelled` is what was written *instead
        # of* carried, and there was nothing at that offset to carry.
        modelled[name] = [(start, end) for start, end, _ in
                          sorted(replacements[name])]

    warnings = [w for w in (
        capacity_warning,
        manufacture,
        _out_of_grid_warning(polygons, base.get("terrain_grid")),
        _binding_drift_warning(map_name, polygons),
    ) if w]

    return Bundle(name=f"{map_name}.a{arrangement}",
                  gns_name=files.gns_path.name,
                  gns=files.gns_path.read_bytes(),
                  resources={n["name"]: resources[n["name"]]
                             for n in declared_resources},
                  warnings=warnings,
                  modelled=modelled)


def _sheet_bytes(state: dict, name: str, base_data: bytes,
                 sidecar_dir: Path | None) -> bytes:
    """A texture resource's new bytes: its sidecar repacked (schema §6.5).

    No sidecar directory means no repaint to apply, so the base sheet carries
    -- the same "carried by construction" rule every other unnamed byte gets.
    """
    sidecar = state.get("texture_sheet")
    if sidecar is None:
        _refuse(f"{name} is a {TEXTURE_BYTES}-byte texture resource but its "
                f"map_states entry names no texture_sheet")
    if sidecar_dir is None:
        return base_data
    path = Path(sidecar_dir) / sidecar
    if not path.is_file():
        _refuse(f"{name}: the document names sidecar {sidecar!r}, which is "
                f"not in {sidecar_dir}")
    width, height, indices, _palette, _alpha = read_indexed_png(path.read_bytes())
    if (width, height) != (schema.SHEET_WIDTH, schema.SHEET_HEIGHT):
        _refuse(f"{sidecar}: {width}x{height}; a sheet is "
                f"{schema.SHEET_WIDTH}x{schema.SHEET_HEIGHT}")
    if any(v > 15 for v in indices):
        _refuse(f"{sidecar}: holds an index above 15; the sheet is 4bpp")
    packed = pack_4bpp(indices)
    if len(packed) != TEXTURE_BYTES:
        _refuse(f"{sidecar}: repacks to {len(packed)} bytes, not {TEXTURE_BYTES}")
    return packed


def build_bundle(document_path: Path, map_dir: Path, out_dir: Path) -> Bundle:
    """Read a document (and the sidecars beside it), build, and write."""
    document_path = Path(document_path)
    document = json.loads(document_path.read_text())
    bundle = build(document, map_dir, sidecar_dir=document_path.parent)
    bundle.write(out_dir)
    return bundle
