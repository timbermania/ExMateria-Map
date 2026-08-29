"""File > Import/Export: the interchange export.

The inverse of `import_document`, per `docs/interchange-export-v1.md` — the
five blocks (round trip and carry, the palette gate, the drift surface,
growth, new-face defaults), the refuse/warn/informational lists, and the
all-or-nothing operator shape.  The acceptance identity is
`export(import(doc)) == doc`.

Export reads the SCENE, never a source document.  Its five sources (§1):

- **the marker** — the mesh object carrying one JSON custom property per
  top-level document section (`exmateria_map/base`, `.../polygons`,
  `.../terrain`, `.../map_states`, `.../carry`).  Four are import-time
  snapshots handed back verbatim; the level-1 half of `terrain` is the one
  carried section the artist edits;
- **the mesh** — positions, the corner `normals` attribute, `UVMap`, the
  schema-v1 face attributes, and three addon-internal face attributes that
  never enter the document (`imported`, `walkable`, `fft_ring_flipped`);
- **the grid object** — `size_x` / `size_z`, the writable target extent;
- **tile objects** — one flagged object per level-0 record;
- **the index images** — one per distinct sheet, the 4-bit index buffer the
  sidecar is repacked and re-hashed from (§4.5);
- **the CLUT images** — one 16x16 per map state, pixel (col, row) = CLUT
  `row`'s entry `col`, which `map_states[].palettes` is re-emitted from (§6.4).
  A state the document gives no palettes keeps none: its image is fabricated
  from the sidecar's display-only PLTE and is not that state's data.

Objects are found by their `exmateria_map/*` flags inside the marker's own
collection; names are never parsed (§1's rule).

**`authored` is stored inverted, as `imported`.**  Export-v1 §8 asks for a
face BOOL defaulting to True that import sets False on loaded faces.  Blender
5.2's Python API has no per-attribute default — `mesh.attributes.new()`
zero-fills and `bpy.types.Attribute` carries no `default_value` — so the only
default the addon can hand a from-scratch face is False.  Storing the negation
(`imported`: False on a new face, True on a loaded one) delivers §8's semantics
exactly and costs nothing: measured, a face built from scratch zero-fills every
attribute, and an extruded child inherits its parent's values, which is §8's
inheritance clause for free.  The attribute is addon-internal and never enters
the document (§8.4), so the polarity is invisible to the identity.

`visible_angles` is the one §8 default zero-fill gets wrong (it wants 0x8000,
and 0 is a legal value, so the attribute cannot tell "new" from "set to zero"
on its own).  `stamp_new_faces` supplies it, keyed on `imported` and made
idempotent by a second zero-filled flag — run at the head of every export, so
the document is right whether or not a live handler ever runs.
"""
import array
import contextlib
import hashlib
import json
import os
from collections import Counter
from pathlib import Path

import bpy
import numpy
from bpy.props import BoolProperty, StringProperty
from bpy.types import Operator

from . import png_indexed
from .import_document import (AUTHORED_RIG, AUTHORED_RIG_VERSION, FACE_INTS,
                              FORMAT, TILE_PAYLOAD_FIELDS, VERSION,
                              _blender_to_fft, _prefs, find_override,
                              import_order, override_rig, remember_dir,
                              rig_is_dirty)

SHEET_W, SHEET_H = 256, 1024
NEW_FACE_VISIBLE_ANGLES = 32768        # 0x8000, export-v1 §8
GROWTH_AREA_MAX = 256                  # decision 10's two ceilings
GROWTH_AXIS_MAX = 18
# §8.1: `walkable` False means the FF FF sentinel -- NOT schema §5.2's worked
# example {255, 127, 0}, which is FF FE, a shipped OUT-OF-GRID binding.
SENTINEL_BINDING = {"x": 255, "z": 127, "level": 1}

# §5.1.3 / decision 23: only these three may be declared on a DRIFT record.
DRIFT_FIELDS = ("height", "slope_height", "slope_type")

# §5.1.2's allowed ranges, by document field.
I16 = (-32768, 32767)
RANGES = {
    "visible_angles": (0, 65535),
    "palette_id": (0, 15),
    "palette_byte_high_nibble": (0, 15),
    "texture_byte6_high_nibble": (0, 15),
    "texture_page": (0, 3),
    "unknown_texture_value_6a": (0, 3),
    "terrain_x": (0, 255),
    "terrain_z": (0, 127),
    "terrain_level": (0, 1),
}
DRIFT_RANGES = {"height": (0, 255), "slope_height": (0, 31),
                "slope_type": (0, 255)}
SHADOWED = FACE_INTS                   # the carried fields §5.3 compares


# ---------------------------------------------------------------------------
# §1 — finding the scene's sources.
# ---------------------------------------------------------------------------

def is_marker(ob):
    """A marker is an object carrying the document's `base` section."""
    return ob is not None and ob.type == "MESH" and "exmateria_map/base" in ob


def markers(scene):
    return [ob for ob in scene.objects if is_marker(ob)]


def find_marker(context):
    """§9.1: one marker stands alone; with several, the active object's marker
    wins and an active object that is not a marker refuses.

    Returns (marker, problem)."""
    found = markers(context.scene)
    active = getattr(context, "object", None)
    if not found:
        return None, "not an interchange scene: no marker object in the scene"
    if len(found) == 1:
        return found[0], None
    if is_marker(active):
        return active, None
    return None, (f"{len(found)} interchange markers in the scene "
                  f"({', '.join(o.name for o in found)}); "
                  f"select the one to export")


def marker_collection(ob):
    """The collection import linked the document into — found by MEMBERSHIP,
    never by name (§1)."""
    for col in ob.users_collection:
        return col
    return None


def flagged(ob, flag):
    """Objects UNDER the marker's collection carrying `exmateria_map/<flag>`.

    `all_objects`, not `objects` (ADR-0187 decision 13): the direct-members
    read lost a tile the artist dragged into a sub-collection, silently and
    with no message.  That was latent while the scene held a handful of drift
    handles; the grid arrives as hundreds of tile objects at once, which the
    artist will organise, so the loss goes from latent to routine.  The nested
    `terrain` collection the importer now makes is itself a sub-collection, so
    this read is what keeps the grid in the document at all.
    """
    col = marker_collection(ob)
    if col is None:
        return []
    key = f"exmateria_map/{flag}"
    return [o for o in col.all_objects if key in o]


def section(ob, name, default=None):
    """One of the marker's JSON sections, parsed."""
    raw = ob.get(f"exmateria_map/{name}")
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# The inverse geometry (import-v1 §7's inverse reads).
# ---------------------------------------------------------------------------

def export_order(n, flipped=False):
    """doc corner index -> the Blender loop SLOT holding it.

    The inverse permutation of `import_order`, which is what makes the ring
    reversal and decision 14's per-face flip undoable exactly."""
    order = import_order(n, flipped)
    inv = [0] * n
    for slot, corner in enumerate(order):
        inv[corner] = slot
    return inv


def uv_dec(bu, bv):
    """The inverse of import's `_uv_enc`: a Blender UV -> (u, v_global).

    `v_global` spans the whole 1,024-row sheet; the caller subtracts the face's
    `texture_page` band to get the document's u8 `v`.  Both halves are exact in
    float32 — every encoded value is a dyadic rational over a power of two — so
    `round` RECOVERS the integer, it does not approximate it."""
    return (int(round(bu * SHEET_W - 0.5)),
            int(round((1.0 - bv) * SHEET_H - 0.5)))


# ---------------------------------------------------------------------------
# §8 — new-face defaults.
# ---------------------------------------------------------------------------

def ensure_attr(me, name, kind, domain):
    a = me.attributes.get(name)
    if a is None:
        a = me.attributes.new(name, kind, domain)
    return a


def stamp_new_faces(me):
    """Give every face the artist created the one §8 default it cannot inherit.

    Zero-fill already IS the default for every attribute in §8's table except
    `visible_angles` (0x8000).  A from-scratch face reads `imported` False; an
    extruded or subdivided child inherits its parent's `imported` True and with
    it the parent's whole carried row, which is §8's inheritance clause.
    `visible_angles_stamped` — zero-filled like the rest — makes this
    idempotent, so an artist who deliberately sets 0 keeps it.

    Returns the number of faces stamped."""
    n = len(me.polygons)
    if not n:
        return 0
    imported = ensure_attr(me, "imported", "BOOLEAN", "FACE").data
    stamped = ensure_attr(me, "visible_angles_stamped", "BOOLEAN", "FACE").data
    va = ensure_attr(me, "visible_angles", "INT", "FACE").data
    # `readable_mesh` is what keeps this from firing. It is here anyway because
    # the alternative was an `IndexError` from deep in a loop, which names
    # neither the invariant nor the gesture that broke it.
    if not (len(imported) == len(stamped) == len(va) == n):
        raise RuntimeError(
            f"{me.name} has {n} face(s) but its attributes hold "
            f"{len(imported)} / {len(stamped)} / {len(va)} -- the mesh and its "
            "attributes are out of sync. This is what Edit Mode looks like "
            "from here; leave it and press again")
    hit = 0
    for i in range(n):
        if imported[i].value or stamped[i].value:
            continue
        va[i].value = NEW_FACE_VISIBLE_ANGLES
        stamped[i].value = True
        hit += 1
    return hit


# ---------------------------------------------------------------------------
# The report: refuse (§5.1), warn (§5.2), informational (§5.3).
# ---------------------------------------------------------------------------

class Report:
    def __init__(self):
        self.refusals = []
        self.warnings = []
        self.divergence = Counter()
        self.new_faces = 0
        self.stamped = 0
        self.paint = None
        #: {sidecar name: the 131,072-byte 4bpp blob}, for the live push.
        #: Export writes PNGs; the push wants the disc's own layout, and this
        #: is where it already exists (§4.5). Empty when no sheet had a buffer.
        self.sheets = {}

    def refuse(self, text):
        self.refusals.append(text)

    def warn(self, text):
        self.warnings.append(text)

    def ranged(self, where, field, value, lo, hi):
        """§5.1.2's shape: the face (or tile / grid), the field, the value, and
        the allowed range."""
        if not isinstance(value, int) or isinstance(value, bool) \
                or not (lo <= value <= hi):
            self.refuse(f"{where}: {field} = {value!r}, allowed {lo}..{hi}")
            return False
        return True

    def lines(self):
        return ([f"REFUSE: {r}" for r in self.refusals]
                + [f"warning: {w}" for w in self.warnings])


# ---------------------------------------------------------------------------
# §2 — `polygons`.
# ---------------------------------------------------------------------------

BUCKETS = ("textured_triangle", "textured_quad",
           "untextured_triangle", "untextured_quad")


def face_kind(textured, n):
    return ("textured_" if textured else "untextured_") + (
        "quad" if n == 4 else "triangle")


def export_polygons(me, rep):
    """The scene graph -> the document's flat `polygons` list.

    Emitted in schema §3's bucket order tt->tq->ut->uq; within a bucket the
    mesh's own face order, so a new face keeps its position (§2).  The sort is
    stable, so an untouched import — already in bucket order — is unmoved."""
    tex = me.attributes["textured"].data
    flip = me.attributes["fft_ring_flipped"].data
    walk = ensure_attr(me, "walkable", "BOOLEAN", "FACE").data
    ints = {a: me.attributes[a].data for a in FACE_INTS}
    nrm = me.attributes["normals"].data
    uvl = me.uv_layers["UVMap"].data
    verts, loops = me.vertices, me.loops

    out = []
    for i, f in enumerate(me.polygons):
        n = f.loop_total
        if n not in (3, 4):
            rep.refuse(f"face {i}: {n} corners; the format holds triangles "
                       f"and quads only")
            continue
        where = f"face {i}"
        inv = export_order(n, flip[i].value)
        textured = bool(tex[i].value)
        q = {"kind": face_kind(textured, n)}

        pos = []
        for k in range(n):
            co = verts[loops[f.loop_start + inv[k]].vertex_index].co
            v = _blender_to_fft([int(round(c)) for c in co])
            for axis, c in zip("xyz", v):
                rep.ranged(where, f"positions[{k}].{axis}", c, *I16)
            pos.append(list(v))
        q["positions"] = pos

        va = ints["visible_angles"][i].value
        # -1 is import's in-band spelling of the document's `null` -- a dead
        # slot the base owns.  Every other value is the u16 itself.
        if va != -1:
            rep.ranged(where, "visible_angles", va, *RANGES["visible_angles"])
        q["visible_angles"] = None if va == -1 else va

        if textured:
            page = ints["texture_page"][i].value
            rep.ranged(where, "texture_page", page, *RANGES["texture_page"])
            nrms, uvs = [], []
            for k in range(n):
                li = f.loop_start + inv[k]
                nv = _blender_to_fft([int(round(c)) for c in nrm[li].vector])
                for axis, c in zip("xyz", nv):
                    rep.ranged(where, f"normals[{k}].{axis}", c, *I16)
                nrms.append(list(nv))
                u, g = uv_dec(*uvl[li].uv)
                v = g - page * SHEET_W if 0 <= page <= 3 else g
                rep.ranged(where, f"uv[{k}].u", u, 0, 255)
                # A UV dragged out of its own `texture_page` band lands here:
                # the band is 256 rows tall, so leaving it puts `v` outside u8.
                rep.ranged(where, f"uv[{k}].v", v, 0, 255)
                uvs.append([u, v])
            q["normals"] = nrms
            q["uv"] = uvs
            q["texture_page"] = page
            for a in ("palette_id", "palette_byte_high_nibble",
                      "unknown_texture_value_6a", "texture_byte6_high_nibble"):
                val = ints[a][i].value
                rep.ranged(where, a, val, *RANGES[a])
                q[a] = val
            if walk[i].value:
                t = {"x": ints["terrain_x"][i].value,
                     "z": ints["terrain_z"][i].value,
                     "level": ints["terrain_level"][i].value}
                for key, a in (("x", "terrain_x"), ("z", "terrain_z"),
                               ("level", "terrain_level")):
                    rep.ranged(where, f"terrain.{key}", t[key], *RANGES[a])
                q["terrain"] = t
            else:
                q["terrain"] = dict(SENTINEL_BINDING)     # §8.1, verbatim
        else:
            q["unknown_untextured"] = [
                ints[f"unknown_untextured_{j}"][i].value for j in range(4)]
            for j, val in enumerate(q["unknown_untextured"]):
                rep.ranged(where, f"unknown_untextured[{j}]", val, 0, 255)
        out.append((BUCKETS.index(q["kind"]), q))
    out.sort(key=lambda r: r[0])                  # stable: bucket order only
    return [q for _, q in out]


def divergence(me, rep):
    """§5.3 — the informational per-face comparison against the `_shadow`
    twins.  Never blocks; it answers "what has the artist changed since
    import", and drives the "faces added since import" count."""
    n = len(me.polygons)
    imported = ensure_attr(me, "imported", "BOOLEAN", "FACE").data
    live = [i for i in range(n) if imported[i].value]
    rep.new_faces = n - len(live)
    for a in SHADOWED:
        cur, shadow = me.attributes.get(a), me.attributes.get(a + "_shadow")
        if cur is None or shadow is None:
            continue
        moved = sum(1 for i in live
                    if cur.data[i].value != shadow.data[i].value)
        if moved:
            rep.divergence[a] = moved
    corner = {"normals": lambda li: tuple(me.attributes["normals"].data[li].vector),
              "positions": lambda li: tuple(
                  me.vertices[me.loops[li].vertex_index].co)}
    for a, read in corner.items():
        shadow = me.attributes.get(a + "_shadow")
        if shadow is None:
            continue
        faces = 0
        for i in live:
            f = me.polygons[i]
            span = range(f.loop_start, f.loop_start + f.loop_total)
            if any(any(abs(x - y) > 1e-4 for x, y in
                       zip(read(li), tuple(shadow.data[li].vector)))
                   for li in span):
                faces += 1
        if faces:
            rep.divergence[a] = faces


# ---------------------------------------------------------------------------
# §2 / §6 / §7 — `terrain` and the grid extent.
# ---------------------------------------------------------------------------

def pre_growth_extent(ob):
    """The BASE map's own extent -- the boundary `build._classify_terrain`
    classifies a record against (schema §7.2), or `None`.

    Read off `base.terrain_tiles`, which ADR-0187 decision 1 carries straight
    from the base map's `0x68` chunk.  **Not** from `terrain_grid`: that field
    is the *target* extent and grows the moment the artist types (decision 16),
    so a grown document that was saved and reopened would report its grown edge
    as the base's and then refuse every record its own growth made legal.
    Decision 3 names `size_x_shadow`/`size_z_shadow`, and that is the fallback
    here -- it is the same number for an ungrown document and the only one
    available to a scene with no carried grid.
    """
    base = section(ob, "base") or {}
    rows = base.get("terrain_tiles") or []
    if rows:
        return (max(int(r[0]) for r in rows) + 1,
                max(int(r[1]) for r in rows) + 1)
    g = flagged(ob, "grid")
    if not g:
        return None
    sx, sz = g[0].get("size_x_shadow"), g[0].get("size_z_shadow")
    if not isinstance(sx, int) or not isinstance(sz, int):
        return None
    return sx, sz


def tile_record(ob, rep, pre_extent=None, drift=frozenset()):
    """One flagged tile object -> its record, or None when it declares nothing.

    A declared field is one whose `<field>_declared` twin is set (§1, §6.3) --
    NOT one whose value is merely present.  Since ADR-0187 decision 3 every
    tile carries the base's twenty values, all of them undeclared, so "the
    value is there" stopped being able to mean anything at all.

    The tile's CLASS is derived, never read off a stored flag (decision 3): a
    tile inside `pre_extent` that `drift` does not name is a **carried tile**
    and may declare nothing, one that `drift` names may declare
    `DRIFT_FIELDS`, and one outside `pre_extent` is growth-created and may
    declare the lot.  That is `build._classify_terrain`'s rule, and mirroring
    it here is decision 12 -- before it, export wrote documents `build` then
    refused one record at a time."""
    where = f"tile object {ob.name!r}"
    rec = {}
    for key, lo, hi in (("x", 0, 255), ("z", 0, 127), ("level", 0, 1)):
        val = ob.get(key)
        if not isinstance(val, int):
            rep.refuse(f"{where}: missing or non-integer {key!r}")
            return None
        rep.ranged(where, key, val, lo, hi)
        rec[key] = val
    x, z, level = rec["x"], rec["z"], rec["level"]
    where = f"{where} ({x}, {z}, L{level})"
    declared = [k for k in TILE_PAYLOAD_FIELDS
                if bool(ob.get(k + "_declared")) and k in ob.keys()]
    pre_growth = (pre_extent is not None
                  and x < pre_extent[0] and z < pre_extent[1])
    is_drift = pre_growth and (x, z) in drift
    if declared and pre_growth and not is_drift:
        rep.refuse(f"{where}: that tile is still the base's -- it is inside "
                   f"the pre-growth {pre_extent[0]}x{pre_extent[1]} extent and "
                   f"the drift checker does not name it (decisions 3, 11, 12); "
                   f"it declares {', '.join(sorted(declared))}")
    if is_drift:
        # §5.1.3 / decision 23 -- on a tile the drift checker named, the pin
        # bytes stay unreachable.  A growth-created tile may declare the lot.
        for k in declared:
            if k not in DRIFT_FIELDS:
                rep.refuse(f"{where}: a drift record may declare only "
                           f"{', '.join(DRIFT_FIELDS)}; it declares {k!r}")
    for k in declared:
        val = ob[k]
        if isinstance(val, (bool, float)):
            val = int(val)
        if is_drift and k in DRIFT_RANGES:
            rep.ranged(where, k, val, *DRIFT_RANGES[k])
        rec[k] = val
    # §7.4: {x, z, level} plus nothing declared is not a record at all.
    return rec if declared else None


def export_terrain(ob, rep):
    """Level-0 records from the flagged tile objects + level-1 records from the
    marker's carried section.  `null` when nothing is declared — never `[]`
    (§2)."""
    # The classification is computed ONCE and handed down, exactly as
    # `build._classify_terrain` computes it once per document (decision 12).
    # `authoring` imports this module, so the reverse import is local.
    from .authoring import drifted
    pre_extent = pre_growth_extent(ob)
    drift = frozenset(drifted(ob))
    recs = [r for r in (tile_record(t, rep, pre_extent, drift)
                        for t in flagged(ob, "tile")) if r]
    carried = section(ob, "terrain") or []
    level1 = [r for r in carried if r.get("level", 0) == 1]
    recs.sort(key=lambda r: (r["z"], r["x"]))
    level1.sort(key=lambda r: (r["z"], r["x"]))
    out = recs + level1
    if not out:
        return None

    # Identity: when the exported SET is the carried set, hand back the CARRIED
    # ORDER.  A sparse list's order is dump's, not a rule this leg gets to
    # re-derive; only a new or dropped record may reorder it.
    def key(r):
        return (r.get("level", 0), r["z"], r["x"])

    if {key(r) for r in out} == {key(r) for r in carried}:
        by = {key(r): r for r in out}
        return [by[key(r)] for r in carried]
    return out


def export_grid(ob, rep):
    """`base.terrain_grid` <- the grid object's props; `null` with no grid
    object (import §7's inverse read: no grid object <=> dump wrote `null`)."""
    grids = flagged(ob, "grid")
    if not grids:
        return None
    g = grids[0]
    where = f"grid object {g.name!r}"
    out = {}
    for key in ("size_x", "size_z"):
        val = g.get(key)
        if not isinstance(val, int):
            rep.refuse(f"{where}: missing or non-integer {key!r}")
            return None
        # Decision 10's ceilings, and shrink -- non-positive included (§5.1.2).
        if val < 1:
            rep.refuse(f"{where}: {key} = {val}, allowed 1..{GROWTH_AXIS_MAX}")
        elif val > GROWTH_AXIS_MAX:
            rep.refuse(f"{where}: {key} = {val} exceeds the axis ceiling "
                       f"max(SizeX, SizeZ) <= {GROWTH_AXIS_MAX}")
        was = g.get(key + "_shadow")
        if isinstance(was, int) and val < was:
            rep.refuse(f"{where}: {key} = {val} shrinks the grid from {was}; "
                       f"shrink is refused (decision 10)")
        out[key] = val
    if all(out[k] >= 1 for k in out):
        area = out["size_x"] * out["size_z"]
        if area > GROWTH_AREA_MAX:
            rep.refuse(f"{where}: size_x * size_z = {area} exceeds the area "
                       f"ceiling {GROWTH_AREA_MAX}")
    return out


def names_a_tile(t):
    """Could ANY legal grid hold the tile this binding names?

    Decision 10 caps a grid at `max(SizeX, SizeZ) <= 18`, so a pair with a
    coordinate at or past 18 names a tile that no growth, however legal, can
    ever cover.  Such a value is not a binding to a tile — it is the idle value
    a polygon carries when it is not on the grid at all.

    Measured over the corpus, this is not a corner case, it is the population:
    of the 40,745 bindings a plain "outside the extent" test flags, **40,542
    (99.5%) name an unreachable tile** — 38,975 of them the single value
    `{255, 127, 0}`, which is 54% of every binding on the disc.  A warning that
    fires on 136 of 148 arrangements and is right on 8 of them is not a
    warning, and that is what it did until the first real GUI export printed
    "234 terrain binding(s) outside the 10x15 grid" with every one of the 234
    reading `(255, 127, L0)`.

    NOTE — this contradicts export-v1 §8.1's premise, which calls
    `{255, 127, 0}` "a shipped out-of-grid binding, a different thing" from the
    FF FF sentinel.  They are different BYTES, and the export leg still treats
    them as different bytes: `walkable` stays keyed to FF FF alone and a FF FE
    binding round-trips verbatim, so nothing about the document changes here
    (decision 9: "nothing is rewritten").  What changes is only who gets
    WARNED about.  Whether FF FE should also read as unbound on the export side
    is a decision, and it belongs on map #517, not in this predicate."""
    return t["x"] < GROWTH_AXIS_MAX and t["z"] < GROWTH_AXIS_MAX


def out_of_grid_warnings(polys, grid, rep):
    """§5.2 / decision 9, verbatim: "Both ends warn; neither refuses, and
    nothing is rewritten."

    The test is on the SIGNED pair the document carries, which is what the
    attributes hold; the disc's own encoding wraps (`byte0 = (z & 0x7F) << 1 |
    level`, `byte1 = x & 0xFF`), so a post-encode range test is blind.  A
    sentinel binding is not a binding, a value no legal grid can hold is not a
    binding (`names_a_tile`), and an arrangement with no grid has nothing to be
    outside of — all three are vacuous, not warnings."""
    if not grid:
        return
    sx, sz = grid.get("size_x"), grid.get("size_z")
    if not isinstance(sx, int) or not isinstance(sz, int):
        return
    bad, unbound = [], 0
    for i, p in enumerate(polys):
        t = p.get("terrain")
        if not t or t == SENTINEL_BINDING:
            continue
        if 0 <= t["x"] < sx and 0 <= t["z"] < sz:
            continue
        if not names_a_tile(t):
            unbound += 1
            continue
        bad.append((i, t))
    if bad:
        shown = ", ".join(f"#{i} -> ({t['x']}, {t['z']}, L{t['level']})"
                          for i, t in bad[:8])
        # Never a silent cap: the suppressed count rides the message, so
        # "8 outside" cannot be mistaken for "8 unusual values on this map".
        rep.warn(f"{len(bad)} terrain binding(s) outside the {sx}x{sz} grid: "
                 f"{shown}" + (" ..." if len(bad) > 8 else "")
                 + (f" ({unbound} more sit outside it at a coordinate no legal "
                    f"grid can hold — not counted)" if unbound else ""))


# ---------------------------------------------------------------------------
# §3 / §4 — the sheets: index recovery, the palette gate, sidecar writing.
# ---------------------------------------------------------------------------

def image_indices(img):
    """The 4-bit index buffer behind one index image.

    R holds the exact 0..15 index per pixel (import writes it that way, and
    0..15 is exact in float32).  PNG row 0 is the TOP scanline and Blender's
    pixel row 0 is the BOTTOM, so rows flip on the way out — the inverse of
    import's `_index_image`.

    MAIN THREAD, and one of the three hard blocks ADR-0186 Amendment 13
    decision 55 ranks above the worker: `push_gather` calls it once per sheet
    and five of them cost 102 ms of a 235 ms block.  The per-texel walk it
    replaces was 20.4 ms a sheet; this is 0.35 ms.  Byte-identical because
    `round()` and `numpy.rint` are the same rounding (both round half to
    even) and the mask is the same mask — and because R holds 0..15, which is
    exact in float32, the rounding never actually has a tie to break."""
    w, h = img.size
    buf = numpy.empty(w * h * 4, dtype=numpy.float32)
    img.pixels.foreach_get(buf)
    return (numpy.rint(buf.reshape(h, w, 4)[:, :, 0]).astype(numpy.uint8)
            & 0xF)[::-1].tobytes()


def set_image_indices(img, indices):
    """Write a 4-bit index plane back into one index image.

    The inverse of `image_indices`, and it lives beside it so the two row
    flips cannot drift apart: `indices` is TOP-scanline-first (the sidecar
    PNG's order, which is exactly what `image_indices` hands out) and
    Blender's pixel row 0 is the BOTTOM.  R holds the exact 0..15 index and
    alpha is 1, the shape `import_document._index_image` writes at import.

    ADR-0186 Amendment 3 decision 14 is what needs it: a conversion or a
    re-pack moves every island, and the compiled **Sheet** is carried through
    that same blit rather than left picturing a layout the mesh no longer
    uses.

    MAIN THREAD, and the other half of Amendment 13 decision 55: it is ~25 ms
    of `land_compile`'s 74 ms block, and 1.0 ms after.  The row flip is a
    `[::-1]` view rather than a loop, which is what keeps it the visible
    inverse of `image_indices` above.  `reshape` also gives the size mismatch
    a name: the loop it replaces would quietly write a short plane and leave
    the rest of the image holding the last compile's indices.
    """
    w, h = img.size
    plane = (numpy.frombuffer(indices, dtype=numpy.uint8) & 0xF
             ).reshape(h, w).astype(numpy.float32)[::-1]
    buf = numpy.zeros((h, w, 4), dtype=numpy.float32)
    buf[:, :, 0] = plane
    buf[:, :, 3] = 1.0
    img.pixels.foreach_set(buf.ravel())
    img.update()
    try:
        img.pack()          # or it reloads BLANK, which is index 0 everywhere
    except RuntimeError:
        pass


#: Scanlines per band in `rgb_from_floats`.  64 rows of a 4x Painting is
#: ~3 MB of float64, which stays in cache; the whole picture at once is
#: 302 MB and measures six times slower for the same arithmetic.
_BAND = 64


def image_floats(img):
    """MAIN THREAD -- one image's raw float RGBA, and the size it came at.

    The half of `image_rgb` that touches `bpy`, and the only half that has to
    be on the main thread.  `foreach_get` is a C copy; splitting it out is
    what lets the per-pixel walk below run in a worker, which at a 4x Painting
    is the difference between a compile and a 2.2 s freeze (ADR-0186
    Amendment 10, measured: 0.08 s at 1x, **2.20 s at 4x**).
    """
    w, h = img.size
    buf = array.array("f", bytes(4 * w * h * 4))
    img.pixels.foreach_get(buf)
    return buf, w, h


def rgb_from_floats(buf, w, h):
    """Float RGBA -> three bytes per texel, top scanline first.  No `bpy`.

    The hottest thing in the addon -- 12.6 M texels at a 4x Painting, and
    ~65 % of the compile worker before this (ADR-0186 Amendment 13).  Measured
    in Blender on MAP022 a0 at the shipped 4x: **611 ms -> 17 ms**, and
    byte-identical to the strided-slice walk it replaces.

    The inverse of `convert_op._write_art`: that buffer is TOP-scanline-first
    like the sidecar PNG and Blender's pixel row 0 is the BOTTOM.

    **The scale is done in float64, and that is not a detail.**  The loop this
    replaces computed `round(v * 255.0)` in Python floats, which is float64,
    and a float32 times 255 needs at most 32 mantissa bits -- so it rounded
    the EXACT product.  `a * 255.0` on a float32 array stays in float32 and
    rounds the product first.  Amendment 13 verified the float32 form
    byte-identical, and it is -- on the buffers it was measured against, which
    hold `byte / 255`.  The Painting is a `float_buffer=True` image and a
    brush can leave any float32 in it: over 67 M uniform-random float32
    values the two disagree on **379**, and the adversarial case is
    reproducible (`v = 0.0019607844` scales to 0.5000000295, which rounds to
    1 exactly and to 0 in float32).  Amendment 15 records it as the third
    exactness trap.

    An exact tie is impossible, which is why only the product's precision
    matters: `v * 255 = k + 0.5` needs `v` to have 510 in its denominator, and
    510 is not a power of two.

    In BANDS of 64 scanlines, which is not a memory dodge -- it is faster.  A
    whole-picture float64 temporary at 4x is 302 MB and misses cache on every
    pass; 64 rows stay in L2 and the same arithmetic measures 15.7 ms against
    101.5 ms.  (The float32 whole-picture form, which is also wrong, measures
    92.1 ms.)

    `astype(uint8)` reproduces the old `& 0xFF` on every finite input,
    negative and out-of-range included -- checked against `round(v * 255) &
    0xFF` for values from -3921.6 to +3921.6.  A NaN or an infinity is the one
    place they part: the old loop raised `ValueError` / `OverflowError` and
    this returns 0, which is the better of the two but is a change.
    """
    a = numpy.frombuffer(buf, dtype=numpy.float32).reshape(h, w, 4)[:, :, :3]
    out = numpy.empty((h, w, 3), dtype=numpy.uint8)
    for y0 in range(0, h, _BAND):
        y1 = min(y0 + _BAND, h)
        scaled = numpy.multiply(a[y0:y1], 255.0, dtype=numpy.float64)
        numpy.rint(scaled, out=scaled)
        out[y0:y1] = scaled.astype(numpy.uint8)
    # Blender's row 0 is the BOTTOM one.
    return out[::-1].tobytes()


#: The last masters a compile derived, by the buffer they were derived FROM.
#: Two entries: a push assembles one painting and an export may hold the
#: previous one still.
_MASTERS = {}

#: A cap, because one entry is 12.6 MB at N = 4 and this is a session-long
#: dict.  Small on purpose: a miss costs the walk, which is what used to
#: happen every time.
_MASTERS_KEEP = 2


def master_key(buf):
    """What names a master: a sha256 of the FLOAT buffer it came from.

    Not `zlib.crc32`, which is cheaper (14.2 ms against 24.6 ms on a 4x
    Painting) and is what `settle_op.canvas_digest` uses.  That one is a change
    DETECTOR, where a collision costs one skipped settle; this one keys a value
    that goes on to name a sidecar file through `sha256(rgb)`, so a collision
    would write one painting's bytes under another's name.  The 10 ms buys the
    key the same strength as the identity it feeds -- `canvas_digest`'s own
    docstring draws exactly this line.
    """
    return hashlib.sha256(memoryview(buf)).digest()


def remember_master(buf_key, w, h, rgb):
    """Deposit a master a WORKER already derived, for the export that follows.

    `land_compile` calls this with the compile's own `master`.  Without it the
    settle derives the same 12.6 M texels twice a second apart -- once on the
    compile's thread and once inside `assemble`, on the main thread, where it
    measured **1.2 s** of frozen Blender per settle at N = 4.
    """
    if len(_MASTERS) >= _MASTERS_KEEP:
        _MASTERS.clear()
    _MASTERS[(buf_key, w, h)] = rgb


def forget_masters():
    """For the harnesses, and for anything that wants the walk proved."""
    _MASTERS.clear()


def image_rgb(img):
    """The true-colour picture behind one image, three bytes per texel.

    Both halves, on the caller's thread -- which is every caller but the
    compile's, whose read and whose walk happen on different ones.

    **Served from `_MASTERS` when the compile already derived it.**  The key is
    the float buffer's own sha256, so this can only answer for the exact pixels
    it was deposited against: a reload, an undo, a stroke, another tool's write
    all move the key and take the walk.  The read (`foreach_get`, 28 ms) and
    the key (24.6 ms) are paid either way; what a hit skips is the 1.2 s walk.
    """
    buf, w, h = image_floats(img)
    key = (master_key(buf), w, h)
    hit = _MASTERS.get(key)
    if hit is not None:
        return hit
    rgb = rgb_from_floats(buf, w, h)
    remember_master(key[0], w, h, rgb)
    return rgb


def export_source_art(ob, states, base, rep=None, sidecars=True):
    """The **Painting**, written beside the document (ADR-0186 dec. 4, 5, 6).

    Decision 4: the compile has no inverse, so the painting cannot be
    recovered from a compiled map -- and the irreplaceable half of an authored
    map does not live in the `.blend`.  Decision 5: it sits in its OWN
    top-level section under its own name, never in `map_states[].texture_sheet`
    -- `build` reads only what that field names, so it stays blind to source
    art by construction rather than by a rule someone has to keep.
    Decision 6: one entry per map state, deduplicated by the painting's own
    content hash, exactly as the disc's own sheets already collapse.

    The hash is over the RGB bytes and not over the PNG, so two identical
    paintings share one file whatever the encoder was feeling -- the same rule
    `export_sheets` uses, where the name comes from the packed 4bpp.

    **The SCALE is read off the picture** (Amendment 10 decision 43), never
    stored: a painting is `256k x 1024k` for k in `resample.SCALES`, and the
    name carries `@Nx` for k > 1 only.  Tagging `@1x` would change every
    existing key in this section and break the whole-document
    `export(import(doc)) == doc` identity asserted over all 148 arrangements,
    so bare means 1x and the suffix lands on decision 7's shape a third time:
    *absence is the declaration*.  All of a document's paintings must AGREE on
    k -- N belongs to the map, not to a state -- and that agreement is the
    check, which is why no field is needed to make one.

    **It REFUSES rather than dropping.**  This read
    `!= (SHEET_W, SHEET_H): continue`, so a Painting that was not exactly
    256x1024 left the document with nothing said.  Decision 4 makes the
    Painting the irreplaceable half of an authored map and decision 11 exists
    because "an artist who painted detail and got back a blur, with nothing
    saying so, has no way to find out why".  Widening the size check was never
    the fix.  Import takes the OPPOSITE posture on the same fact and warns --
    "an import that lost a file must still open; it is the export that
    refuses" (schema 7.3b).

    **The hash is over the RGB, so `sidecars=False` costs the caller nothing
    but the file.**  The section, its keys and the digests are identical
    either way; only `files` is left empty.  A 4x Painting encodes in **0.9 s**
    (a per-pixel Sub filter plus `zlib` level 9) and the live push throws the
    result away, which is the whole reason the flag exists.

    Returns `(files, section, digests)`.  All are EMPTY on an unconverted map:
    the section is emitted only when there is something in it, so a document
    that never met the compile round-trips byte for byte as it always has.
    """
    from . import resample
    from .convert_op import source_art_name
    stem = f"{base['map']}.a{base['arrangement']}"
    files, out, by_hash, digests = {}, {}, {}, {}
    scales = {}
    for i, st in enumerate(states):
        sheet = st.get("texture_sheet")
        if not sheet:
            continue
        img = bpy.data.images.get(source_art_name(sheet))
        if img is None:
            continue
        w, h = tuple(img.size)
        n = resample.scale_of(w, h)
        if n is None:
            if rep is not None:
                rep.refuse(
                    f"painting {img.name} is {w}x{h}, which is not a legal "
                    f"Painting: it must be 256k x 1024k for k in "
                    f"{list(resample.SCALES)} (ADR-0186 Amendment 10 dec. 43). "
                    "Resize it or delete it -- it will not be written")
            continue
        scales[img.name] = n
        rgb = image_rgb(img)
        full = hashlib.sha256(rgb).hexdigest()
        digests[sheet] = full
        digest = full[:8]
        name = by_hash.get(digest)
        if name is None:
            tag = "" if n == 1 else f"@{n}x"
            name = by_hash[digest] = f"{stem}.source-{digest}{tag}.png"
            if sidecars:
                files[name] = png_indexed.write_rgb_png(rgb, w, h)
            out[name] = {"states": []}
        out[name]["states"].append(i)
    # N belongs to the MAP.  A document holding a 4x painting for one state and
    # a 1x painting for another is incoherent, and the shrink in front of the
    # compile would be handed two different answers to one question.
    if rep is not None and len(set(scales.values())) > 1:
        rep.refuse(
            "this map's paintings disagree on scale: "
            + ", ".join(f"{k} is {v}x" for k, v in sorted(scales.items()))
            + ". N belongs to the map, not to a state (ADR-0186 Amendment 10 "
              "dec. 43)")
    return files, out, digests


def off_palette_list(ob):
    """§3.6 / §4.4 — the sticky off-palette list.

    The resolve pass writes it onto the marker; export refuses while it is
    non-empty and never re-derives it (a pixel the artist did not paint keeps
    its import-time index and is never re-resolved, §3.4)."""
    return section(ob, "off_palette", []) or []


def clut_rows_for_plte(states):
    """The CLUT rows the display-only PLTE is expanded from: the first state
    that carries palettes.  `build` ignores PLTE (decision 6); it exists so a
    `palettes: null` state still previews."""
    for st in states:
        if st.get("palettes"):
            rows = []
            for r in range(16):
                ent = st["palettes"][r] if r < len(st["palettes"]) else None
                cols = ([tuple(int(c.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
                         for c in ent["colors"]] if ent else [])
                rows.append(cols + [(0, 0, 0)] * (16 - len(cols)))
            return rows
    ramp = [(c, c, c) for c in range(0, 256, 16)]
    return [list(ramp) for _ in range(16)]


def majority_plte(indices, claims, clut_rows):
    """#366's texel-majority expansion: per INDEX, the colour the most texels
    holding it actually see (§4.5).

    `claims` is one (palette_id, texture_page, uvs) per textured face; a face
    claims its UV bounding box in its own page band.  Display-only, so an index
    no polygon claims falls back to CLUT row 0."""
    own = bytearray(SHEET_W * SHEET_H)          # 0 = unclaimed, else pal + 1
    for pal, page, uvs in claims:
        if not (0 <= pal <= 15 and 0 <= page <= 3):
            continue
        us = [c[0] for c in uvs]
        vs = [c[1] for c in uvs]
        u0, u1 = max(0, min(us)), min(SHEET_W - 1, max(us))
        v0, v1 = max(0, min(vs)), min(255, max(vs))
        if u1 < u0 or v1 < v0:
            continue
        fill = bytes([pal + 1]) * (u1 - u0 + 1)
        for v in range(v0, v1 + 1):
            row = (page * 256 + v) * SHEET_W
            own[row + u0:row + u1 + 1] = fill
    votes = Counter(zip(indices, own))
    plte = []
    for idx in range(16):
        best, seen = None, 0
        for (i, o), c in votes.items():
            if i == idx and o and c > seen:
                best, seen = o - 1, c
        plte.append(tuple(clut_rows[best if best is not None else 0][idx]))
    return plte


def export_sheets(ob, states, base, sidecars=True):
    """§4.5 — repack, re-hash, rename.

    Returns ({new sidecar name: PNG bytes}, {old name: new name},
    {new sidecar name: the 4bpp blob}).  With `sidecars=False` the first is
    EMPTY and the other two are unchanged -- the name is `sha256(packed)` and
    never the PNG's, so a caller that wants the blob and the rename (the live
    push wants exactly those) pays no encode.  A sheet whose sidecar never decoded
    has no buffer: its states keep the imported name and export writes no file
    for it, rather than inventing one.  Export writes its own files only and
    never deletes a stale sidecar.

    The third return is the disc's own 131,072-byte layout, and it is handed
    back rather than recomputed because the live push needs exactly it. It was
    already being built here and thrown away after hashing -- and that hash is
    the sidecar's NAME, so the bytes the push sends and the bytes `build` puts
    on a disc are the same bytes by construction rather than by agreement. The
    alternative, PNG-encoding here and decoding again in the pusher, is two
    more chances to differ and no more truth.
    """
    sheet_images = section(ob, "sheet_images", {}) or {}
    state_sheets = section(ob, "state_sheets", []) or []
    stem = f"{base['map']}.a{base['arrangement']}"
    clut_rows = clut_rows_for_plte(states)

    claims = {}
    me = ob.data
    if "textured" in me.attributes and "UVMap" in me.uv_layers:
        tex = me.attributes["textured"].data
        pal = me.attributes["palette_id"].data
        pge = me.attributes["texture_page"].data
        flip = me.attributes["fft_ring_flipped"].data
        uvl = me.uv_layers["UVMap"].data
        # v1 previews ONE sheet at a time (import §4's default index image), so
        # the claims land on the first sheet a state names.
        sheet = next((s for s in state_sheets if s), None)
        for i, f in enumerate(me.polygons):
            if not tex[i].value:
                continue
            page = pge[i].value
            inv = export_order(f.loop_total, flip[i].value)
            uvs = []
            for k in range(f.loop_total):
                u, g = uv_dec(*uvl[f.loop_start + inv[k]].uv)
                uvs.append((u, g - page * SHEET_W))
            claims.setdefault(sheet, []).append((pal[i].value, page, uvs))

    files, rename, blobs = {}, {}, {}
    for old, img_name in sheet_images.items():
        img = bpy.data.images.get(img_name) if img_name else None
        if img is None or tuple(img.size) != (SHEET_W, SHEET_H):
            continue                                   # no buffer: keep `old`
        indices = image_indices(img)
        packed = png_indexed.pack_4bpp(indices)
        new = f"{stem}.sheet-{hashlib.sha256(packed).hexdigest()[:8]}.png"
        rename[old] = new
        blobs[new] = packed
        if sidecars:
            files[new] = png_indexed.write_indexed_png(
                indices, majority_plte(indices, claims.get(old, []), clut_rows),
                SHEET_W, SHEET_H)
    return files, rename, blobs


# ---------------------------------------------------------------------------
# §9 — the operator's document assembly.
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def readable_mesh(ob):
    """Read the mesh OUT of Edit Mode, and put the artist back where they were.

    In Edit Mode the live geometry lives in the BMesh, and the Mesh datablock's
    **attribute arrays read as size 0** while `me.polygons` still reports a
    count -- so the two disagree and the first attribute lookup is a bare
    `IndexError`. That is what an artist got for deleting a face and pressing
    the button, which is the exact gesture the whole shrink leg exists for.

    `Object.update_from_editmode()` is NOT the fix. Measured: after deleting
    one face of 454 it syncs `me.polygons` to 453 and leaves the `imported`
    attribute at **0**, so it converts a stale read into a mismatched one.
    Only leaving Edit Mode restores the attributes (measured: 453 and 453).

    The round trip is safe to do for the artist rather than refuse at them:
    measured, face selection survives EDIT -> OBJECT -> EDIT unchanged. If the
    object is not the active one there is nothing to toggle *to*, so that is
    the one case this refuses rather than guesses at.
    """
    if getattr(ob, "mode", "OBJECT") != "EDIT":
        yield
        return
    if bpy.context.view_layer.objects.active is not ob:
        raise RuntimeError(
            f"{ob.name} is in Edit Mode but is not the active object, so its "
            "mesh cannot be read: Blender keeps edit-mode geometry in a BMesh "
            "and the attributes read empty. Leave Edit Mode and press again")
    bpy.ops.object.mode_set(mode="OBJECT")
    try:
        yield
    finally:
        bpy.ops.object.mode_set(mode="EDIT")


def assemble(ob, sidecars=True):
    """`_assemble`, with the mesh made readable first. See `readable_mesh`.

    **`sidecars=False` is for a caller that wants the DOCUMENT and not the
    files** -- the live push, which sends `rep.sheets` and `doc` and discards
    the PNGs. It is not an optimisation flag to sprinkle: anything that writes
    a bundle must leave it True, because `files` is what gets written and an
    empty one writes a document whose sidecars are missing. Measured on MAP022
    a0 at N = 4, it is 0.9 s of the push's main-thread half.
    """
    with readable_mesh(ob):
        return _assemble(ob, sidecars)


def _assemble(ob, sidecars=True):
    """The §2 table, end to end.  Returns (doc, sidecars, report).

    Refusals are collected, never raised: §9.4 evaluates them ALL first and
    writes nothing while any stands, so the artist sees every reason at once."""
    rep = Report()
    base = section(ob, "base")
    if base is None:
        rep.refuse("not an interchange scene: the marker carries no `base`")
        return None, {}, rep

    me = ob.data
    rep.stamped = stamp_new_faces(me)
    # §3.3: export is a resolve trigger.  A pixel painted since the last one
    # has to become an index (or a refusal) BEFORE the sticky list is read --
    # otherwise the gate passes on a sheet the artist has already broken.
    from .paint import on_trigger
    rep.paint = on_trigger(ob)
    states = section(ob, "map_states", []) or []

    polys = export_polygons(me, rep)
    divergence(me, rep)
    base = dict(base)
    base["terrain_grid"] = export_grid(ob, rep)
    terrain = export_terrain(ob, rep)
    out_of_grid_warnings(polys, base["terrain_grid"], rep)

    for entry in off_palette_list(ob):
        rep.refuse(f"off-palette colour {entry.get('color')}: "
                   f"{entry.get('count')} pixel(s), bbox {entry.get('bbox')}")
    files, rename, rep.sheets = export_sheets(ob, states, base, sidecars)
    # BEFORE the rename: `source_art` keys its entries by STATE INDEX, and the
    # paintings are named from the sheet the marker knows, not from the hashed
    # name this export is about to give it.
    art_files, source_art, art_digests = export_source_art(
        ob, states, base, rep, sidecars)
    files.update(art_files)
    # ADR-0186 Amendment 5: the Sheet is a cache and a stale one still ships
    # (decision 13), so this WARNS and never refuses.  It is free here -- the
    # digest is the one `export_source_art` already computed to name the
    # sidecar -- which is why the check lives on this path rather than in the
    # push, and why both the export report and the push report carry it.
    from .compile_op import compare_stamp
    for note in compare_stamp(ob, art_digests):
        rep.warn(note)
    states = [dict(st) for st in states]
    for st in states:
        if st.get("texture_sheet") in rename:
            st["texture_sheet"] = rename[st["texture_sheet"]]

    export_palettes(ob, states, rep)
    export_rigs(ob, states, rep)

    # §2 / decision 27: `version` is the oldest `build` that can handle this
    # document, so it moves only when something in it needs a newer one. The
    # test is on the FIELD, not on whether this export promoted anything -- a
    # document that arrived carrying an authored rig still carries it back.
    version = (AUTHORED_RIG_VERSION
               if any(st.get(AUTHORED_RIG) for st in states) else VERSION)
    doc = {"format": FORMAT, "version": version, "base": base,
           "polygons": polys, "terrain": terrain, "map_states": states,
           "carry": section(ob, "carry")}
    # Only when there IS one.  Decision 7's shape a third time: the presence
    # of the section is the declaration, and a map that never met the compile
    # must round-trip exactly as it did before this leg existed -- which is
    # what `export(import(doc)) == doc` asserts over all 148 arrangements.
    if source_art:
        doc["source_art"] = source_art
    return doc, files, rep


def export_palettes(ob, states, rep):
    """§6.4 — re-emit `map_states[].palettes` from the 16x16 CLUT image.

    The CLUT image is already the addon's palette surface: import builds one
    per state (`_clut_image`, pixel (col, row) = CLUT `row`'s entry `col`), the
    preview graph samples it, and `paint.clut_entries` gates painted pixels
    against it deliberately -- "the image is what the preview shows, so the gate
    accepts exactly the colours the artist can see".  It was the one surface the
    document was NOT written from, so a recoloured entry previewed, gated, and
    then exported the colour it had replaced.

    Three rules, each a decision rather than a convenience:

    - **A `palettes: null` state stays null.**  Import fabricates that state's
      CLUT image out of the sidecar's display-only PLTE (§4's untrusted-colour
      preview), so its pixels are not the state's data and never were.  Writing
      them back would invent a `0x44` chunk for a resource that has none --
      decision 3's ownership, not a colour edit.
    - **`stp` is carried, never re-derived.**  The mask is per-CLUT live data
      (1,178 bits set across 651 palette-carrying resources) and the image has
      nowhere to put it: an entry's colour and its STP bit are independent, so
      the bit rides through from the imported row untouched.
    - **Only the entries the document declared.**  A row is re-emitted at its
      own length, so a short CLUT is not silently padded to 16 out of image
      pixels import zero-filled.

    The bar is the identity trip: §6.4's expansion (`c8 = c5 * 255 // 31`) and
    this read-back both land on the same 8-bit lattice, so an untouched CLUT
    must come back byte-identical to what `dump` wrote.  That is asserted, not
    assumed -- `export_palette_untouched_is_byte_exact`.
    """
    names = section(ob, "state_cluts", []) or []
    written = 0
    for i, state in enumerate(states):
        rows_in = state.get("palettes")
        if not rows_in:
            continue                      # `palettes: null` stays null
        img = bpy.data.images.get(names[i]) if i < len(names) else None
        if img is None or tuple(img.size) != (16, 16):
            continue                      # no image: hand back what arrived
        px = array.array("f", bytes(4 * 16 * 16 * 4))
        img.pixels.foreach_get(px)
        rows_out = []
        for r, ent in enumerate(rows_in):
            if not ent or r >= 16:
                rows_out.append(ent)
                continue
            colors = []
            for c in range(len(ent.get("colors", []))):
                j = (r * 16 + min(15, c)) * 4
                colors.append("#%02X%02X%02X" % tuple(
                    max(0, min(255, int(round(px[j + k] * 255.0))))
                    for k in range(3)))
            rows_out.append(dict(ent, colors=colors))
        state["palettes"] = rows_out
        written += 1
    return written


def export_rigs(ob, states, rep):
    """Promote each live rig Override into `map_states[].authored_light_rig`.

    ADR-0004 decision 27: decision 25's Scope line stops being true here -- an
    Override is no longer preview-only; on export it BECOMES the state's
    authored rig, and `build` writes its 45 bytes at pointer `0x64`.

    Only an Override the artist MOVED something on.  The rig is exposed on
    every state from import, so existence declares nothing -- see
    `import_document.rig_is_dirty`.

    Three rules, and each one is a decision rather than a convenience:

    - **A state that can hold no rig is warned about, never refused.**  The rig
      is exposed on borrowing states too -- that is what makes a borrowed
      picture editable -- and 640 of the corpus's rig-less rows are texture
      rows.  Refusing here would turn an ordinary preview action into a failed
      export.  The Override stays what it was: the
      screen.  `light_rig is None` is exactly the right test now that the reader
      takes the resource's KIND (#576): a texture row reads None by kind and a
      chunkless mesh row reads None for having no chunk, which are the only two
      populations `build` cannot write to.
    - **The 6 gradient bytes are the STATE's, not the Override's.**  An Override
      seeded from a borrowed rig carries the LENDER's gradient, and writing that
      would edit bytes decision 25 shows read-only.  The solve owns 39 bytes and
      carries 6, so they are re-read from this state's own rig here -- which is
      also what makes `build`'s verbatim-gradient refusal something an honest
      export can never trip.
    - **The presence of the field is the declaration.**  A state whose rig the
      artist never moved has no key written to it, so an untouched document is
      byte-for-byte what `dump` produced and the identity trip is untouched.
    """
    if not getattr(ob, "exmateria_map_rig_overrides", ()):
        return 0
    written = 0
    for i, state in enumerate(states):
        ov = find_override(ob, i)
        # EXISTENCE is no longer the declaration: the rig is exposed on every
        # state from import, so what the artist MOVED is the only honest
        # signal.  Before this, an Override that existed and was never moved
        # still shifted 2 bytes of MAP011.8 through `build` -- the direction
        # re-emitted at
        # exactly 4096 against a disc magnitude of 4094.4-4096.7.  Exposed on
        # every state, that defect would fire on all 1,371 of them.
        if ov is None or not rig_is_dirty(ov):
            continue
        base_rig = state.get("light_rig")
        if not base_rig:
            rep.warn(f"map state {i} ({state.get('resource')}) carries a rig "
                     f"Override, but its resource has no 45-byte rig to "
                     f"overwrite — a texture row has none by kind, and a mesh "
                     f"row whose 0x64 is zero cannot be given one (decision "
                     f"19). The Override stays preview-only and is NOT exported")
            continue
        rig = override_rig(ov)
        rig["gradient"] = list(base_rig.get("gradient") or [0] * 6)
        state[AUTHORED_RIG] = rig
        written += 1
    if written:
        rep.warn(f"{written} map state(s) export an AUTHORED light rig: "
                 f"`build` writes those 45 bytes to the disc (decision 27), "
                 f"and the document stamps version {AUTHORED_RIG_VERSION}")
    return written


def write_bundle(doc, files, directory):
    """§9.2 / §9.4: the document and every sidecar in ONE directory, written
    only once nothing refuses.  Returns the document's path."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{doc['base']['map']}.a{doc['base']['arrangement']}.json"
    for name, blob in files.items():
        (d / name).write_bytes(blob)
    path.write_text(json.dumps(doc, separators=(",", ":")))
    return path


def describe_divergence(rep):
    """§5.3's one-line read-out — informational, never a block."""
    parts = [f"{v} face(s) {k}" for k, v in sorted(rep.divergence.items())]
    if rep.new_faces:
        parts.insert(0, f"{rep.new_faces} face(s) added since import")
    return "changed since import: " + (", ".join(parts) if parts else "nothing")


# ---------------------------------------------------------------------------
# The operator.
# ---------------------------------------------------------------------------

def start_directory(context):
    """Where the export browser opens: the last directory exported TO.

    Its own field, not the import memory: sharing one would move where the next
    import opens every time the artist exported somewhere else."""
    prefs = _prefs(context)
    last = bpy.path.abspath(getattr(prefs, "last_export_dir", "") or "")
    if last and os.path.isdir(last):
        return os.path.join(last, "")
    return str(Path(context.scene.render.filepath).parent / "interchange.json")


def output_directory(filepath, directory=""):
    """Resolve the browser's answer to the ONE directory §9.2 writes into.

    The operator's target is a DIRECTORY — the document's name is derived from
    `base.map` / `base.arrangement`, and every sidecar is a bare name beside it
    (schema §1) — so a typed name can only ever mean a folder.  Blender's file
    browser still presents a filename field, and typing `test` into it produced
    `/home/.../Documents/test`, which does not exist, which fell back to its
    PARENT.  The files landed one directory up from where the artist asked, and
    the report did not say where, so it read as nothing having been written.

    - the browser's own `directory` field wins when the fork fills it;
    - an existing directory is used;
    - an existing file names its parent (the artist clicked a document);
    - a path that does not exist and carries a suffix names its parent (the
      artist typed `map.json`);
    - anything else is the folder the artist meant, and is created.
    """
    if directory:
        return str(Path(bpy.path.abspath(directory)))
    path = Path(bpy.path.abspath(filepath or "."))
    if path.is_dir():
        return str(path)
    if path.exists() or path.suffix:
        return str(path.parent)
    return str(path)


class EXPORT_OT_interchange_document(Operator):
    """Export the scene's interchange document (JSON) and its sheet sidecars."""
    bl_idname = "export_map.document"
    bl_label = "ExMateria Map Interchange"
    bl_description = ("Export an exmateria-map interchange document (JSON) "
                      "and its texture-sheet sidecars")
    bl_options = {"REGISTER"}

    filepath: StringProperty(subtype="DIR_PATH")
    # Declaring `directory` puts the browser in folder-select mode where the
    # build supports it; `output_directory` resolves either answer, so the
    # operator does not depend on which one the fork fills.
    directory: StringProperty(subtype="DIR_PATH")
    filter_folder: BoolProperty(default=True, options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        return bool(markers(context.scene))

    def invoke(self, context, event):
        self.filepath = start_directory(context)
        self.directory = self.filepath
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, context):
        col = self.layout.column(align=True)
        col.label(text="Pick a FOLDER — the document and every sidecar")
        col.label(text="land in it, named from the map and arrangement.")
        col.label(text="A name typed here is created as a folder.")

    def execute(self, context):
        from .authoring import suspended
        ob, problem = find_marker(context)
        if problem:
            self.report({"ERROR"}, problem)
            return {"CANCELLED"}
        with suspended():               # §6.1, as on the import side
            # Decision 25 -- before `assemble` reads the mesh, and never
            # inside it.  Imported here rather than at module scope because
            # `compile_op` imports this module.
            from .compile_op import ensure_compiled
            compiled_notes = ensure_compiled(ob)
            doc, files, rep = assemble(ob)
        for note in compiled_notes:
            self.report({"INFO"}, note)
        ob["exmateria_map/last_export"] = json.dumps(rep.lines())
        # The Log carries what the artist would have READ, in order.
        # `rep.lines()` is refusals + warnings only, so on a clean export it is
        # empty -- the stats are in `describe_divergence`, which until now went
        # only to a toast that is gone by the time anyone looks up.
        from .report_log import record
        summary = [describe_divergence(rep)] + list(rep.lines())
        for w in rep.warnings:
            self.report({"WARNING"}, w)
        self.report({"INFO"}, describe_divergence(rep))
        if rep.refusals:
            # §9.4 -- nothing is written, and every reason is reported.
            self.report({"ERROR"},
                        f"{len(rep.refusals)} refusal(s), nothing written: "
                        + "; ".join(rep.refusals[:12])
                        + (" ..." if len(rep.refusals) > 12 else ""))
            record("Export REFUSED", ob.name,
                   summary + [f"{len(rep.refusals)} refusal(s), nothing written"])
            return {"CANCELLED"}
        directory = output_directory(self.filepath, self.directory)
        try:
            path = write_bundle(doc, files, directory)
        except Exception as e:
            self.report({"ERROR"}, f"could not write into {directory}: {e}")
            record("Export FAILED", ob.name,
                   summary + [f"could not write into {directory}: {e}"])
            return {"CANCELLED"}
        record("Export", ob.name, summary + [f"wrote into {path}"])
        remember_dir(context, str(path), field="last_export_dir")
        # Name the DIRECTORY, always.  "wrote MAP022.a0.json + 5 sidecar(s)" is
        # indistinguishable from having written nothing if the artist is
        # looking in the wrong place.
        self.report({"INFO"},
                    f"wrote {path.name} + {len(files)} sidecar(s) to {directory}")
        print(f"EXMATERIA-MAP: exported {path} "
              f"({len(doc['polygons'])} polygons, "
              f"{len(doc['terrain'] or [])} terrain records, "
              f"{len(files)} sidecar(s), {len(rep.warnings)} warning(s))")
        return {"FINISHED"}


def menu_func(self, context):
    self.layout.operator(EXPORT_OT_interchange_document.bl_idname,
                         text="ExMateria Map Interchange (.json)")


def register():
    bpy.utils.register_class(EXPORT_OT_interchange_document)
    try:
        bpy.types.TOPBAR_MT_file_export.append(menu_func)
    except AttributeError:
        from bpy.utils import menu_registry
        menu_registry.append(bpy.types.TOPBAR_MT_file_export, menu_func)


def unregister():
    try:
        bpy.types.TOPBAR_MT_file_export.remove(menu_func)
    except AttributeError:
        from bpy.utils import menu_registry
        menu_registry.remove(bpy.types.TOPBAR_MT_file_export, menu_func)
    bpy.utils.unregister_class(EXPORT_OT_interchange_document)
