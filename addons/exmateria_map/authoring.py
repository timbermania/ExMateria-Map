"""The terrain authoring surfaces: growth (block 4) and the drift checker
(block 3) of `docs/interchange-export-v1.md`.

Both produce the tile objects the export leg reads (`export_document` §2), and
neither writes the document: the extent IS the growth (§7.3), and a drift
handle is where a fix is *declared*, not where it is applied.

**Growth (§7).**  `size_x` / `size_z` on the grid object are document data in
the ROM's own shape, so they stay ID properties — the thing export reads.  The
artist edits registered Object properties beside them, whose `update` callback
is decision 10's clamp and whose message names WHICH ceiling stopped the field
(`SizeX * SizeZ <= 256` and `max(SizeX, SizeZ) <= 18`, or the import-time
extent, since shrink is refused).  Both ceilings bind, on disjoint populations:
17x16 = 272 is refused by area with both axes legal, and 19 by axis with the
area still under 256.  Typing the field grows the document's extent; the
"Apply growth" button only creates the authoring handles, and creates them
UNDECLARED — decision 20: growth writes nothing, the bytes past the old edge
become live as they stand.

**Drift (§6).**  The checker owns the quads for the lifetime of the scene: an
untouched document has no drift by construction (decision 22), so import
creates none.  Every run is a TOTAL sync — recompute the drifted set, create a
handle where one is missing, delete one that is no longer drifted, and leave a
surviving handle's declared fields alone.

The drifted set is decision 15's population *exactly*: the tiles
`base.floor_steps` names.  For each, the base step is the row's, and the
current step is `round(bottom / 12)` recomputed from the LIVE mesh by `build`'s
own coverage rule — the floor-like polygons (|n_up| >= 0.5) whose centroid tile
or whose bounding-box tile centre falls inside them — never from the declared
binding (decision 15).  Integer equality, so a tile is drifted or it is not.

That recomputation is the same rule `dump` ran to produce `floor_steps` in the
first place, which is what makes the checker's own correctness checkable: on an
untouched import the drifted set must be EMPTY, on every arrangement in the
corpus.  A coverage rule that disagreed with dump's would light the whole grid
up on import.

The handler is `depsgraph_update_post`, guarded three ways because a handler
that creates and removes objects can re-enter itself: a busy flag, a suspend
flag the import/export operators hold, and a cheap geometry signature that
early-outs when nothing that could move the drifted set has moved.
"""
import json

import bpy
from bpy.app.handlers import persistent
from bpy.props import IntProperty
from bpy.types import Operator, Panel

from .export_document import (GROWTH_AREA_MAX, GROWTH_AXIS_MAX, TILE_DRIFT,
                              TILE_GROWTH, flagged, marker_collection,
                              markers, section)
from .import_document import (HEIGHT_STEP, TILE_PAYLOAD_FIELDS, TILE_UNITS,
                              _new_material, _plain_quad_mesh, UNLIT_GREY)

FLOOR_COS = 0.5          # #438's |n_up| threshold for "floor-like"
DRIFT_FIELDS = ("height", "slope_height", "slope_type")
DRIFT_MATERIAL = "exmateria_map_drift"
# §7.5: the shipped snapshot of #447's external pins.  Absent by design so far
# -- the panel says so and the number reads n/a rather than 0, which would be a
# claim.
PIN_TABLE = "external_pins.json"

_BUSY = False            # re-entry guard: the handler's own edits
_SUSPEND = 0             # held by the import/export operators (§6.1)
_SIGNATURE = {}          # object name -> last geometry signature


# ---------------------------------------------------------------------------
# Declared fields.
# ---------------------------------------------------------------------------

def is_declared(ob, field):
    return bool(ob.get(field + "_declared"))


def declare(ob, field, value=None):
    ob[field + "_declared"] = True
    if value is not None:
        ob[field] = int(value)


def undeclare(ob, field):
    ob[field + "_declared"] = False


def seed(ob, field, value):
    """A value SHOWN but not declared — §7.2's growth seed, §6.3's base value."""
    ob[field] = int(value)
    ob[field + "_declared"] = bool(ob.get(field + "_declared"))


# ---------------------------------------------------------------------------
# The live coverage rule — `build`'s, recomputed from the mesh.
# ---------------------------------------------------------------------------

def _newell_up(ring):
    """The z component of the Newell normal, normalised.  Blender's +Z is the
    map's up: decision 14's frame is (x, z, -y), a rotation, so the FFT
    normal's y IS this, up to the sign |.| removes."""
    nx = ny = nz = 0.0
    n = len(ring)
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        nx += (a[1] - b[1]) * (a[2] + b[2])
        ny += (a[2] - b[2]) * (a[0] + b[0])
        nz += (a[0] - b[0]) * (a[1] + b[1])
    m = (nx * nx + ny * ny + nz * nz) ** 0.5
    return abs(nz / m) if m else 0.0


def _inside_xy(px, py, ring):
    """Even-odd point-in-polygon on the ground plane (the map's x/z)."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        yi, yj = ring[i][1], ring[j][1]
        if (yi > py) != (yj > py):
            xi, xj = ring[i][0], ring[j][0]
            if px < (xj - xi) * (py - yi) / (yj - yi) + xi:
                inside = not inside
        j = i
    return inside


def floor_bottoms(me, sx, sz):
    """{(x, z): bottom} over the live mesh — the tiles floor-like polygons
    cover, and the LOWEST world Z of the polygons covering each.

    `bottom` is dump's `-max(fft_y)`: the axis map sends fft y to -z, so the
    largest fft y is the smallest Blender z, and dump's negation is exactly
    this minimum.  Same rule, same number, read off the mesh instead of the
    resource."""
    out = {}
    for f in me.polygons:
        ring = [tuple(me.vertices[me.loops[li].vertex_index].co)
                for li in range(f.loop_start, f.loop_start + f.loop_total)]
        if _newell_up(ring) < FLOOR_COS:
            continue
        cx = sum(v[0] for v in ring) / len(ring)
        cy = sum(v[1] for v in ring) / len(ring)
        tx, tz = int(cx // TILE_UNITS), int(cy // TILE_UNITS)
        bx0 = int(min(v[0] for v in ring) // TILE_UNITS)
        bx1 = int(max(v[0] for v in ring) // TILE_UNITS)
        bz0 = int(min(v[1] for v in ring) // TILE_UNITS)
        bz1 = int(max(v[1] for v in ring) // TILE_UNITS)
        low = min(v[2] for v in ring)
        for gx in range(max(bx0, 0), min(bx1, sx - 1) + 1):
            for gz in range(max(bz0, 0), min(bz1, sz - 1) + 1):
                if (gx, gz) == (tx, tz) or _inside_xy(
                        gx * TILE_UNITS + TILE_UNITS / 2,
                        gz * TILE_UNITS + TILE_UNITS / 2, ring):
                    key = (gx, gz)
                    if key not in out or low < out[key]:
                        out[key] = low
    return out


def base_floor_steps(ob):
    """{(x, z): (step, slope_height, slope_type)} from the marker's `base`."""
    base = section(ob, "base") or {}
    return {(int(r[0]), int(r[1])): (int(r[2]), int(r[3]), int(r[4]))
            for r in (base.get("floor_steps") or []) if len(r) >= 5}


def grid_extent(ob):
    g = flagged(ob, "grid")
    if not g:
        return None
    sx, sz = g[0].get("size_x"), g[0].get("size_z")
    if not isinstance(sx, int) or not isinstance(sz, int) or sx < 1 or sz < 1:
        return None
    return g[0], sx, sz


def drifted(ob):
    """{(x, z): (step_now, base_step, bottom)} — decision 15's population, the
    tiles whose live floor no longer sits at the base's step."""
    ext = grid_extent(ob)
    if ext is None:
        return {}                       # §6.5: no grid, no overlay
    _g, sx, sz = ext
    base = base_floor_steps(ob)
    if not base:
        return {}
    now = floor_bottoms(ob.data, sx, sz)
    out = {}
    for key, (step, _sh, _st) in base.items():
        bottom = now.get(key)
        if bottom is None:              # no floor covers it now: nothing to
            continue                    # compare, so nothing to warn about
        step_now = int(round(bottom / HEIGHT_STEP))
        if step_now != step:
            out[key] = (step_now, step, bottom)
    return out


# ---------------------------------------------------------------------------
# Tile and handle objects.
# ---------------------------------------------------------------------------

def _tile_objects(ob, kind=None):
    out = {}
    for t in flagged(ob, "tile"):
        if kind is not None and t.get("exmateria_map/tile") != kind:
            continue
        x, z, lv = t.get("x"), t.get("z"), t.get("level")
        if isinstance(x, int) and isinstance(z, int) and lv == 0:
            out[(x, z)] = t
    return out


def _quad(name, x, z, world_z, collection, material):
    cx, cz = x * TILE_UNITS, z * TILE_UNITS
    me = _plain_quad_mesh(name, [(cx, cz, world_z),
                                 (cx + TILE_UNITS, cz, world_z),
                                 (cx + TILE_UNITS, cz + TILE_UNITS, world_z),
                                 (cx, cz + TILE_UNITS, world_z)])
    o = bpy.data.objects.new(name, me)
    collection.objects.link(o)
    o.data.materials.append(material)
    o.data.polygons[0].material_index = 0
    return o


def _drift_material():
    mat = bpy.data.materials.get(DRIFT_MATERIAL)
    if mat is not None:
        return mat
    mat = _new_material(DRIFT_MATERIAL, grey=0.5)
    nt = mat.node_tree
    emit = next(n for n in nt.nodes if n.type == "EMISSION")
    emit.inputs["Color"].default_value = (1.0, 0.35, 0.1, 1.0)
    mat.diffuse_color = (1.0, 0.35, 0.1, 0.35)     # the solid-mode colour
    try:                                # 4.x has it, 5.x moved it; neither is
        mat.blend_method = "BLEND"      # worth failing a drift sync over
    except (AttributeError, TypeError):
        pass
    return mat


# ---------------------------------------------------------------------------
# §6 — the drift checker.
# ---------------------------------------------------------------------------

def sync_drift(ob):
    """§6.2's TOTAL sync.  Returns (n_drifted, n_with_a_declared_fix)."""
    col = marker_collection(ob)
    if col is None:
        return 0, 0
    live = drifted(ob)
    # A tile the DOCUMENT already declares carries its own record object, and
    # two objects at one (x, z, level) would export two records for one tile --
    # which schema §7.2 refuses at build time.  Decision 23's drift fix exists
    # for tiles that have no record, and decision 22 makes that every tile of
    # an untouched document, so this only ever bites a hand-authored one.
    declared_here = set(_tile_objects(ob)) - set(_tile_objects(ob, TILE_DRIFT))
    shadowed = {k for k in live if k in declared_here}
    for k in shadowed:
        del live[k]
    have = _tile_objects(ob, TILE_DRIFT)
    mat = _drift_material() if live else None
    for key, o in list(have.items()):
        if key not in live:
            bpy.data.objects.remove(o, do_unlink=True)   # drift cleared
            del have[key]
    base = base_floor_steps(ob)
    for (x, z), (step_now, step, bottom) in live.items():
        o = have.get((x, z))
        if o is None:
            o = _quad(f"drift_{x}_{z}_L0", x, z, bottom, col, mat)
            o["exmateria_map/tile"] = TILE_DRIFT
            o["x"], o["z"], o["level"] = x, z, 0
            o.display_type = "TEXTURED"
            for f in TILE_PAYLOAD_FIELDS:
                o[f + "_declared"] = False
            have[(x, z)] = o
        # The base values ride the handle so decision 17's panel can show them
        # beside each field; they are shown, never declared (§6.3).
        b_step, b_sh, b_st = base[(x, z)]
        o["height_base"], o["slope_height_base"], o["slope_type_base"] = \
            b_step, b_sh, b_st
        o["drift_step_now"] = step_now
        for f in DRIFT_FIELDS:
            if f not in o.keys():
                o[f] = {"height": step_now, "slope_height": b_sh,
                        "slope_type": b_st}[f]
    fixed = sum(1 for o in have.values()
                if any(is_declared(o, f) for f in DRIFT_FIELDS))
    ob["exmateria_map/drift_count"] = len(live)
    ob["exmateria_map/drift_fixed"] = fixed
    ob["exmateria_map/drift_shadowed"] = len(shadowed)
    return len(live), fixed


def _signature(ob):
    me = ob.data
    ext = grid_extent(ob)
    # Coverage moves with x/y as well as with height, so all three sums ride
    # the signature: a Z-only digest is blind to a floor slid sideways onto a
    # different tile.
    sx = sy = sz = 0.0
    for v in me.vertices:
        sx += v.co[0]
        sy += v.co[1]
        sz += v.co[2]
    return (len(me.polygons), len(me.vertices), ext[1:] if ext else None,
            round(sx, 3), round(sy, 3), round(sz, 3))


_ACTIVE_FACE = {}


def _paint_trigger(scene):
    """§3.3's face-select trigger: select a face and the paint image re-colours
    under THAT face's CLUT row, so the artist edits in the palette the face
    actually reads.

    Before the depsgraph's change list, because a selection change need not
    report as geometry.  The whole body is behind the no-paint-image early-out,
    so a scene that is not painting pays one dictionary lookup per marker."""
    from .paint import (active_face_index, on_trigger, paint_image_name,
                        sheet_of_state)
    for ob in markers(scene):
        try:
            sheet = sheet_of_state(
                ob, int(ob.get("exmateria_map/preview_state") or 0))
            if not sheet or bpy.data.images.get(paint_image_name(sheet)) is None:
                continue
            # NOT `ob.data.polygons.active`: it FREEZES in Edit Mode, which is
            # the only mode that can select a face, so this trigger could never
            # fire where it was meant to and the paint image never re-coloured
            # to the face the artist had just clicked.
            active = active_face_index(ob)
        except (AttributeError, ReferenceError):
            continue
        if _ACTIVE_FACE.get(ob.name) == active:
            continue
        _ACTIVE_FACE[ob.name] = active
        on_trigger(ob)


def _touched(depsgraph):
    """The datablock names this update actually moved.

    Walking every marker on every scene update is not affordable: a scene can
    hold many arrangements at once (the corpus harness holds 148), and
    `_signature` walks every vertex.  The depsgraph already knows what changed,
    so ask it, and let an unrelated update cost nothing."""
    names = set()
    for upd in getattr(depsgraph, "updates", ()) or ():
        if not (getattr(upd, "is_updated_geometry", False)
                or getattr(upd, "is_updated_transform", False)):
            continue
        name = getattr(getattr(upd, "id", None), "name", None)
        if name:
            names.add(name)
    return names


@persistent
def _depsgraph_handler(scene, depsgraph=None):
    """§6.1 — a scene-update handler, not a timer.  Guarded four ways: the
    busy flag (this handler creates and removes objects, so it can re-enter
    itself), the operators' suspend, the depsgraph's own change list, and a
    geometry signature."""
    global _BUSY
    if _BUSY or _SUSPEND or depsgraph is None:
        return
    _paint_trigger(scene)
    names = _touched(depsgraph)
    if not names:
        return
    try:
        _BUSY = True
        for ob in markers(scene):
            try:
                if ob.name not in names and ob.data.name not in names:
                    continue
                sig = _signature(ob)
            except (AttributeError, ReferenceError):
                continue
            if _SIGNATURE.get(ob.name) == sig:
                continue
            _SIGNATURE[ob.name] = sig
            sync_drift(ob)
    finally:
        _BUSY = False


class suspended:
    """Held by the import/export operators so their own scene mutations do not
    race the checker (§6.1)."""

    def __enter__(self):
        global _SUSPEND
        _SUSPEND += 1
        return self

    def __exit__(self, *exc):
        global _SUSPEND
        _SUSPEND = max(0, _SUSPEND - 1)
        return False


class MAP_OT_check_drift(Operator):
    """Re-run the drift check now (the handler runs it on every scene update)."""
    bl_idname = "exmateria_map.check_drift"
    bl_label = "Check terrain drift"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(markers(context.scene))

    def execute(self, context):
        ob = context.object if context.object in markers(context.scene) \
            else markers(context.scene)[0]
        _SIGNATURE.pop(ob.name, None)
        n, fixed = sync_drift(ob)
        self.report({"INFO"}, f"{n} drifted, {fixed} with a declared fix")
        return {"FINISHED"}


class MAP_OT_declare_field(Operator):
    """Declare (or withdraw) one payload field on the selected tile handle."""
    bl_idname = "exmateria_map.declare_field"
    bl_label = "Declare terrain field"
    bl_options = {"REGISTER", "UNDO"}

    field: bpy.props.StringProperty()
    on: bpy.props.BoolProperty(default=True)

    def execute(self, context):
        ob = context.object
        if ob is None or "exmateria_map/tile" not in ob:
            self.report({"ERROR"}, "select a tile or drift handle")
            return {"CANCELLED"}
        if self.on:
            declare(ob, self.field)
        else:
            undeclare(ob, self.field)
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# §7 — growth.
# ---------------------------------------------------------------------------

def clamp_extent(want_x, want_z, was_x, was_z):
    """Decision 10's two ceilings and the shrink refusal, as a pure function of
    the target extent (decision 16: every refusal here is knowable before
    anything happens).  Returns (x, z, message)."""
    msg = []
    x, z = int(want_x), int(want_z)
    if x < was_x:
        x = was_x
        msg.append(f"SizeX cannot shrink below the imported {was_x}")
    if z < was_z:
        z = was_z
        msg.append(f"SizeZ cannot shrink below the imported {was_z}")
    if x > GROWTH_AXIS_MAX:
        x = GROWTH_AXIS_MAX
        msg.append(f"axis ceiling: max(SizeX, SizeZ) <= {GROWTH_AXIS_MAX}")
    if z > GROWTH_AXIS_MAX:
        z = GROWTH_AXIS_MAX
        msg.append(f"axis ceiling: max(SizeX, SizeZ) <= {GROWTH_AXIS_MAX}")
    if x * z > GROWTH_AREA_MAX:
        # Give back whichever axis the artist just pushed; the caller passes
        # the pair, so shrink the one that is furthest above its import value.
        while x * z > GROWTH_AREA_MAX and (x > was_x or z > was_z):
            if x - was_x >= z - was_z and x > was_x:
                x -= 1
            elif z > was_z:
                z -= 1
            else:
                break
        msg.append(f"area ceiling: SizeX * SizeZ <= {GROWTH_AREA_MAX}")
    return x, z, "; ".join(msg)


def _footprint(g, sx, sz):
    """Redraw the grid quad at the new extent (decision 13's footprint)."""
    me = g.data
    if len(me.vertices) != 4:
        return
    for v, co in zip(me.vertices, [(0, 0, 0), (sx * TILE_UNITS, 0, 0),
                                   (sx * TILE_UNITS, sz * TILE_UNITS, 0),
                                   (0, sz * TILE_UNITS, 0)]):
        v.co = co
    me.update()
    me.name = f"grid {sx}x{sz}"


def _extent_update(self, context):
    """The clamp, as the field is typed (§7.1).  `self` is the grid object."""
    if self.get("_extent_busy"):
        return
    was_x = self.get("size_x_shadow", 1)
    was_z = self.get("size_z_shadow", 1)
    x, z, msg = clamp_extent(self.exmateria_map_size_x,
                             self.exmateria_map_size_z, was_x, was_z)
    self["_extent_busy"] = True
    try:
        self.exmateria_map_size_x = x
        self.exmateria_map_size_z = z
    finally:
        del self["_extent_busy"]
    # The document data stays the ID property -- the shape export reads.
    self["size_x"], self["size_z"] = x, z
    self["extent_message"] = msg
    _footprint(self, x, z)


def pin_table(ob):
    """§7.5's fourth preview number.  The shipped snapshot of #447's external
    pins; absent -> the number is n/a, never 0."""
    try:
        import os
        from pathlib import Path
        here = Path(__file__).resolve().parent / PIN_TABLE
        if not here.exists():
            return None
        return json.loads(here.read_text())
    except Exception:
        return None


def growth_preview(ob):
    """Decision 16's four numbers, read out BEFORE any commit."""
    ext = grid_extent(ob)
    if ext is None:
        return None
    g, sx, sz = ext
    was_x = g.get("size_x_shadow", sx)
    was_z = g.get("size_z_shadow", sz)
    have = _tile_objects(ob)
    # §7.2: "every level-0 tile NEWLY IN THE EXTENT that lacks a tile object".
    # The pre-growth extent is the `_shadow` twin, so an untouched import has
    # nothing pending -- counting the whole grid would read as 130 tiles of
    # growth on a document that grew by none.
    pending = new_tiles(sx, sz, was_x, was_z, have)
    table = pin_table(ob)
    base = section(ob, "base") or {}
    if table is None:
        pinned = None
    else:
        key = f"{base.get('map')}.a{base.get('arrangement')}"
        pinned = sum(1 for x, z in pending
                     if [x, z] in table.get(key, []))
    return {
        "created": len(pending),
        "changed": 0,                       # decision 20: growth writes nothing
        "already_carry_a_record": None,     # not in the document (see §0)
        "externally_pinned": pinned,
        "from": (was_x, was_z), "to": (sx, sz),
    }


def new_tiles(sx, sz, was_x, was_z, have):
    """The level-0 tiles the growth created that carry no handle yet."""
    return [(x, z) for z in range(sz) for x in range(sx)
            if (x >= was_x or z >= was_z) and (x, z) not in have]


class MAP_OT_apply_growth(Operator):
    """Create an authoring handle for every level-0 tile now in the extent that
    has none.  Idempotent: pending = in-extent minus has-object (§7.2)."""
    bl_idname = "exmateria_map.apply_growth"
    bl_label = "Apply growth"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(markers(context.scene))

    def execute(self, context):
        scene_markers = markers(context.scene)
        ob = context.object if context.object in scene_markers \
            else scene_markers[0]
        ext = grid_extent(ob)
        if ext is None:
            self.report({"ERROR"}, "no terrain grid in this arrangement")
            return {"CANCELLED"}
        _g, sx, sz = ext
        col = marker_collection(ob)
        have = _tile_objects(ob)
        mat = _new_material(UNLIT_GREY)
        was_x = _g.get("size_x_shadow", sx)
        was_z = _g.get("size_z_shadow", sz)
        made = 0
        src = next(iter(have.values()), None)
        with suspended():
            for x, z in new_tiles(sx, sz, was_x, was_z, have):
                o = _quad(f"tile_{x}_{z}_L0", x, z, 0.0, col, mat)
                o["exmateria_map/tile"] = TILE_GROWTH
                o["x"], o["z"], o["level"] = x, z, 0
                # §7.2's seed: an existing record's values when there is one,
                # else decision 11's level-0 default (height 0, impassable).
                # SHOWN, never declared -- decision 20 has growth write
                # NOTHING, so an untouched handle exports no record at all.
                for f in TILE_PAYLOAD_FIELDS:
                    o[f + "_declared"] = False
                    if src is not None and f in src.keys():
                        o[f] = int(src[f])
                    else:
                        o[f] = 1 if f == "impassable" else 0
                have[(x, z)] = o
                made += 1
        self.report({"INFO"}, f"{made} tile handle(s) created; the extent "
                              f"itself grew when the field was set")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# The N-panel.
# ---------------------------------------------------------------------------

class MAP_PT_terrain(Panel):
    """`Map` sidebar, 3D viewport: the grid extent, the growth preview, and
    the drift count."""
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Map"
    bl_label = "Terrain"
    bl_order = 3

    def draw(self, context):
        ob = context.object
        layout = self.layout
        if ob is not None and "exmateria_map/tile" in ob:
            self._tile(layout, ob)
            return
        found = [o for o in markers(context.scene)] if context.scene else []
        marker = ob if ob in found else (found[0] if found else None)
        if marker is None:
            return
        ext = grid_extent(marker)
        if ext is None:
            layout.label(text="no terrain grid in this arrangement",
                         icon="INFO")
            return
        g, sx, sz = ext
        box = layout.box()
        box.label(text=f"Grid {sx} x {sz}", icon="GRID")
        box.prop(g, "exmateria_map_size_x", text="SizeX")
        box.prop(g, "exmateria_map_size_z", text="SizeZ")
        msg = g.get("extent_message") or ""
        if msg:
            box.label(text=msg, icon="ERROR")
        pv = growth_preview(marker)
        if pv:
            box.label(text=f"tile handles to create: {pv['created']}")
            box.label(text=f"existing tiles changed: {pv['changed']}")
            box.label(text="tiles created that already carry a record: n/a "
                           "(not in the document)")
            box.label(text=("tiles created that a file outside the map names: "
                            + ("n/a — pin table missing"
                               if pv["externally_pinned"] is None
                               else str(pv["externally_pinned"]))))
        box.operator(MAP_OT_apply_growth.bl_idname, icon="ADD")
        d = layout.box()
        n = marker.get("exmateria_map/drift_count")
        fixed = marker.get("exmateria_map/drift_fixed")
        shadowed = marker.get("exmateria_map/drift_shadowed") or 0
        d.label(text=(f"{n} drifted, {fixed} with a declared fix"
                      if n is not None else "drift not checked yet"),
                icon="ERROR" if n else "CHECKMARK")
        if shadowed:
            d.label(text=f"{shadowed} more drifted, but the document already "
                         f"declares those tiles", icon="INFO")
        d.operator(MAP_OT_check_drift.bl_idname, icon="FILE_REFRESH")

    def _tile(self, layout, ob):
        kind = ob.get("exmateria_map/tile")
        layout.label(text=f"tile ({ob.get('x')}, {ob.get('z')}) "
                          f"L{ob.get('level')} — {kind}",
                     icon="MESH_PLANE")
        fields = DRIFT_FIELDS if kind == TILE_DRIFT else TILE_PAYLOAD_FIELDS
        if kind == TILE_DRIFT:
            layout.label(text=f"floor now at step {ob.get('drift_step_now')}, "
                              f"base says {ob.get('height_base')}",
                         icon="INFO")
        for f in fields:
            row = layout.row(align=True)
            on = is_declared(ob, f)
            op = row.operator(MAP_OT_declare_field.bl_idname,
                              text="", icon="CHECKBOX_HLT" if on
                              else "CHECKBOX_DEHLT")
            op.field, op.on = f, not on
            base = ob.get(f + "_base")
            row.label(text=f"{f} = {ob.get(f)}"
                           + (f"  (base {base})" if base is not None else "")
                           + ("" if on else "  — not declared"))


CLASSES = (MAP_OT_check_drift, MAP_OT_declare_field, MAP_OT_apply_growth,
           MAP_PT_terrain)


def register():
    bpy.types.Object.exmateria_map_size_x = IntProperty(
        name="SizeX", description="The grid's target extent in X (decision 16's "
        "typed field); both ceilings clamp it as it is typed",
        default=1, min=1, max=GROWTH_AXIS_MAX, update=_extent_update)
    bpy.types.Object.exmateria_map_size_z = IntProperty(
        name="SizeZ", description="The grid's target extent in Z (decision 16's "
        "typed field); both ceilings clamp it as it is typed",
        default=1, min=1, max=GROWTH_AXIS_MAX, update=_extent_update)
    for c in CLASSES:
        bpy.utils.register_class(c)
    if _depsgraph_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_depsgraph_handler)


def unregister():
    if _depsgraph_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_depsgraph_handler)
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
    del bpy.types.Object.exmateria_map_size_z
    del bpy.types.Object.exmateria_map_size_x
