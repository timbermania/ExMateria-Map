"""``dump`` -- a base map + arrangement -> the schema-v1 interchange document.

One of the two legs the addon speaks (``docs/interchange-schema-v1.md``).
``dump`` reads; ``build`` writes; the pair is the round-trip instrument's
oracle, and an untouched document must come back byte-identical over all
1,575 corpus files.

Discipline (decision 6): raw on-disc integers, no enums, no names for terrain
fields, no booleans except where the on-disc value is a bit.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import document as doc_schema
from . import mapfile
from .document import BUCKETS, SLOT_CAPACITY, clut_to_json
from .geometry import floor_steps, ring
from .mapfile import (
    TERRAIN_CHUNK_BYTES,
    TEXTURE_BYTES,
    VISIBLE_ANGLES_HEADER_BYTES,
)
from .png_indexed import unpack_4bpp, write_indexed_png

CARRY_NOTE = ("light rig, grayscale set, texture/palette animations, "
              "mesh animations, GNS")


class DumpError(RuntimeError):
    """This (map, arrangement) is not a dumpable document."""


# ---------------------------------------------------------------------------
# the pieces
# ---------------------------------------------------------------------------

def polygons(mesh: mapfile.Mesh, slots: list[int] | None) -> list[dict]:
    """The flat polygon list, on-disk order (schema §3, §5).

    ``visible_angles`` is ``null`` when the base resource carries no 0xB0
    chunk at all (10 of 169 geometry-carrying resources -- schema §6.2 /
    ADR-0004 decision 26). The field is always present; only its value can be
    absent -- and a value the artist puts back into it is one of the two
    triggers that has `build` manufacture the missing chunk.
    """
    tt, tq, ut, uq = mesh.counts
    bucket_start, offset = {}, 0
    for kind, capacity in SLOT_CAPACITY:
        bucket_start[kind] = offset
        offset += capacity

    def angle(kind: str, row: int) -> int | None:
        if slots is None:
            return None
        return slots[bucket_start[kind] + row]

    out: list[dict] = []
    for kind, key, count in ((BUCKETS[0], "tt", tt), (BUCKETS[1], "tq", tq)):
        for row in range(count):
            index = row if key == "tt" else tt + row
            tex = mesh.texture[index]
            x, z, level = mesh.bindings[index]
            out.append({
                "kind": kind,
                "positions": [list(v) for v in mesh.positions[key][row]],
                "normals": [list(v) for v in mesh.normals[key][row]],
                "uv": [list(v) for v in tex["uv"]],
                "palette_id": tex["palette_id"],
                "palette_byte_high_nibble": tex["palette_byte_high_nibble"],
                "texture_page": tex["texture_page"],
                "unknown_texture_value_6a": tex["unknown_texture_value_6a"],
                "texture_byte6_high_nibble": tex["texture_byte6_high_nibble"],
                "terrain": {"x": x, "z": z, "level": level},
                "visible_angles": angle(kind, row),
            })
    for kind, key, count, base in ((BUCKETS[2], "ut", ut, 0),
                                   (BUCKETS[3], "uq", uq, ut)):
        for row in range(count):
            out.append({
                "kind": kind,
                "positions": [list(v) for v in mesh.positions[key][row]],
                "unknown_untextured": list(mesh.untextured[base + row]),
                "visible_angles": angle(kind, row),
            })
    return out


def visible_angle_slots(data: bytes) -> list[int] | None:
    """The whole 1,600-slot table of a resource's 0xB0 chunk, or ``None``."""
    p = mapfile.visible_angles_offset(data)
    if p is None:
        return None
    o = p + VISIBLE_ANGLES_HEADER_BYTES
    return [mapfile.u16(data, o + i * 2) for i in range(doc_schema.SLOT_TOTAL)]


def terrain_source(files: mapfile.MapFiles, rows) -> tuple[str, int, int, int, bytes] | None:
    """Schema §7.3's pick: the arrangement's one resource with a *valid* 0x68.

    Sector order, so the pick is deterministic; ``MAP053`` a0 is the one
    arrangement where the grid lives outside the geometry source and the naive
    "it is always the mesh" rule fails.
    """
    for row in sorted(rows, key=lambda r: r.sector):
        path = files.by_sector[row.sector]
        data = path.read_bytes()
        p = mapfile.terrain_offset(data)
        if p is None:
            continue
        return path.name, data[p], data[p + 1], p, data[p:p + TERRAIN_CHUNK_BYTES]
    return None


def terrain_tiles(payload: bytes, size_x: int, size_z: int) -> list[list[int]]:
    """Every slot of the base's 0x68 chunk, raw: ``[x, z, level, b0 ... b7]``.

    Derived, information-bearing, ``build`` ignores it (schema §4) -- the same
    standing as ``floor_steps``.  It is what the addon draws the grid from, and
    it declares nothing: decision 22's ``"terrain": None`` is untouched.
    """
    out = []
    for level in range(mapfile.TERRAIN_LEVELS):
        for z in range(size_z):
            for x in range(size_x):
                o = (2 + level * mapfile.TERRAIN_LEVEL_BYTES
                     + (z * size_x + x) * mapfile.TERRAIN_RECORD_BYTES)
                out.append([x, z, level]
                           + list(payload[o:o + mapfile.TERRAIN_RECORD_BYTES]))
    return out


# ---------------------------------------------------------------------------
# dump
# ---------------------------------------------------------------------------

def dump(map_dir: Path, number: int, arrangement: int) -> tuple[dict, dict]:
    """``(document, sheets)`` for one (map, arrangement).

    ``sheets`` is ``{sidecar file name: raw 131,072-byte sheet}`` -- the
    sidecars deduplicated by the sheet's own digest (schema §1).
    """
    files = mapfile.bind(map_dir, number)
    rows = files.arrangement_rows(arrangement)
    if not rows:
        raise DumpError(f"{files.name} has no arrangement {arrangement}")
    real = [r for r in rows if not r.is_pad]
    if not real:
        raise DumpError(f"{files.name} a{arrangement} holds only pad rows")

    # --- geometry source: the first mesh row of the arrangement that carries
    #     a 0x40 chunk. 627 of 796 mesh resources carry none.
    geometry_source = geometry_data = mesh = None
    for row in sorted((r for r in real if r.is_mesh), key=lambda r: r.sector):
        data = files.by_sector[row.sector].read_bytes()
        candidate = mapfile.read_mesh(data)
        if candidate:
            geometry_source = files.by_sector[row.sector].name
            geometry_data, mesh = data, candidate
            break
    if mesh is None:
        raise DumpError(f"{files.name} a{arrangement} carries no primary mesh")

    slots = visible_angle_slots(geometry_data)
    vis_offset = mapfile.visible_angles_offset(geometry_data)

    # --- terrain source, grid extent, digest, floor steps
    picked = terrain_source(files, real)
    if picked is None:
        terrain_name = terrain_digest = terrain_grid = None
        steps: list[list[int]] = []
        tiles: list[list[int]] = []
    else:
        terrain_name, size_x, size_z, _offset, payload = picked
        terrain_digest = hashlib.sha256(payload).hexdigest()
        terrain_grid = {"size_x": size_x, "size_z": size_z}
        tiles = terrain_tiles(payload, size_x, size_z)
        rings = [ring(p) for key in ("tt", "tq", "ut", "uq")
                 for p in mesh.positions[key]]
        by_tile = floor_steps(rings, size_x, size_z)
        steps = []
        for (x, z), step in sorted(by_tile.items()):
            index = 2 + (z * size_x + x) * mapfile.TERRAIN_RECORD_BYTES
            record = doc_schema.decode_record(payload[index:index + 8])
            steps.append([x, z, step, record["slope_height"], record["slope_type"]])

    # --- map states, one per non-pad row (schema §7.1)
    states, sheets = [], {}
    for row in real:
        path = files.by_sector[row.sector]
        data = path.read_bytes()
        state = {"resource": path.name, "kind": row.kind,
                 "night": row.night, "weather": row.weather,
                 "palettes": None, "texture_sheet": None,
                 "light_rig": mapfile.read_light_rig(data, row.is_mesh)}
        if len(data) == TEXTURE_BYTES:
            digest = hashlib.sha256(data).hexdigest()[:8]
            name = f"{files.name}.a{arrangement}.sheet-{digest}.png"
            state["texture_sheet"] = name
            sheets[name] = data
        else:
            clut = mapfile.read_palettes(data)
            if clut is not None:
                state["palettes"] = [clut_to_json(c) for c in clut]
        states.append(state)

    resources = []
    seen = set()
    for row in real:
        path = files.by_sector[row.sector]
        if path.name in seen:
            continue
        seen.add(path.name)
        resources.append({"name": path.name,
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})

    return {
        "format": doc_schema.FORMAT,
        "version": doc_schema.VERSION,
        "base": {
            "map": files.name,
            "arrangement": arrangement,
            "resources": resources,
            "geometry_source": geometry_source,
            "geometry_digest": mapfile.mesh_digest(geometry_data, mesh),
            "terrain_source": terrain_name,
            "terrain_digest": terrain_digest,
            "terrain_grid": terrain_grid,
            "terrain_tiles": tiles,
            "floor_steps": steps,
        },
        "polygons": polygons(mesh, slots),
        # An untouched document declares no terrain at all (decision 22).
        "terrain": None,
        "map_states": states,
        "carry": {
            "note": CARRY_NOTE,
            "visible_angles_unknown_896": (
                None if vis_offset is None
                else geometry_data[vis_offset:vis_offset + VISIBLE_ANGLES_HEADER_BYTES].hex()),
            "visible_angles_slots": (
                None if slots is None
                else b"".join(w.to_bytes(2, "little") for w in slots).hex()),
        },
    }, sheets


def arrangements(map_dir: Path, number: int) -> list[int]:
    """The arrangements of a map that NAME a mesh row.

    A superset of what `dump` can dump: a mesh row is not a 0x40 chunk, and 627
    of 796 mesh resources carry none. Over the disc this enumerates **197**
    arrangements, of which **148** dump -- see `dumpable_arrangements`.
    """
    files = mapfile.bind(map_dir, number)
    return sorted({r.arrangement for r in files.rows if r.is_mesh})


def dumpable_arrangements(map_dir: Path, number: int) -> list[int]:
    """The arrangements `dump` will actually return a document for.

    ADR-0004 decision 31 part 3: this is what the import browser's arrangement
    dropdown offers. `arrangements` is the wrong population for a control --
    it would list entries that refuse when picked, on 17 maps.

    The test is `dump`'s own: an arrangement is dumpable when some non-pad mesh
    row of it carries a primary-mesh chunk. Probing for that reads at most one
    resource per mesh row and stops at the first hit, so a dropdown built per
    map is cheap; a full `dump` per candidate would not be.

    Over the disc: **148** of the 197 named, leaving **101** maps offering
    exactly one arrangement (the control is invisible) and **20** offering
    more, up to five.
    """
    files = mapfile.bind(map_dir, number)
    found = []
    for arrangement in sorted({r.arrangement for r in files.rows if r.is_mesh}):
        rows = [r for r in files.arrangement_rows(arrangement)
                if r.is_mesh and not r.is_pad]
        for row in sorted(rows, key=lambda r: r.sector):
            if mapfile.read_mesh(files.by_sector[row.sector].read_bytes()):
                found.append(arrangement)
                break
    return found


def sidecar_palette(document: dict) -> list[tuple[int, int, int]]:
    """The display PLTE for the sidecars: the first state's first CLUT.

    Display only -- the indices are authoritative and ``build`` ignores the
    PLTE (decision 6). A grey ramp stands in when the arrangement carries no
    palette at all, so the file is still a legal indexed PNG.
    """
    for state in document["map_states"]:
        if state.get("palettes"):
            return [(int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16))
                    for c in state["palettes"][0]["colors"]]
    return [(v, v, v) for v in range(0, 256, 16)]


def write_bundle(map_dir: Path, number: int, arrangement: int,
                 directory: Path) -> Path:
    """Schema §1's layout: the document and its sidecars in one directory."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    document, sheets = dump(map_dir, number, arrangement)
    name = f"{document['base']['map']}.a{arrangement}"
    (directory / f"{name}.json").write_text(json.dumps(document, separators=(",", ":")))
    palette = sidecar_palette(document)
    for sidecar, raw in sheets.items():
        (directory / sidecar).write_bytes(
            write_indexed_png(unpack_4bpp(raw), palette))
    return directory / f"{name}.json"
