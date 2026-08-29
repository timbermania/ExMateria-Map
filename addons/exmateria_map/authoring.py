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

from .export_document import (GROWTH_AREA_MAX, GROWTH_AXIS_MAX, flagged,
                              marker_collection, markers, section)
from .import_document import (HEIGHT_STEP, TILE_DRIFT, TILE_GROWTH,
                              TILE_PAYLOAD_FIELDS, TILE_UNITS, _new_material,
                              _paint_tile, _plain_quad_mesh, _terrain_collection,
                              _tile_material)

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


def _set_tile_material(ob, material):
    """Swap a tile's one material slot.  Colour is a display fact (ADR-0187
    decision 10); nothing about the record moves with it."""
    if material is None:
        return
    ob.data.materials.clear()
    ob.data.materials.append(material)
    ob.data.polygons[0].material_index = 0


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
    """§6.2's TOTAL sync.  Returns (n_drifted, n_with_a_declared_fix).

    Since ADR-0187 decision 3 this **creates and deletes nothing**.  Every tile
    of the extent already carries an object built from `base.terrain_tiles`, so
    a drift is MARKED on the tile that is already there and unmarked when it
    clears.  Decision 4 is why: an authored fix lives on the object, so
    deleting one destroys the artist's work.

    **The `shadowed` set is gone, and that is the point of this rewrite.**  It
    used to compute `set(_tile_objects(ob)) - set(_tile_objects(ob, TILE_DRIFT))`
    -- under a comment saying it meant *tiles that declare something*, while it
    actually tested *an object exists*.  The two agreed only because ~0 tile
    objects existed.  With one object per tile that set is the whole grid, and
    the checker would report zero drift forever while printing *"N more
    drifted, but the document already declares those tiles"* -- green, silent
    and false.  It is not repaired here, it is **removed**: it existed because
    a drift handle and a document-declared tile were two objects at one
    `(x, z, level)`, which schema §7.2 refuses at build time.  Decision 3 makes
    that one object, so the hazard it guarded cannot arise.
    """
    col = marker_collection(ob)
    if col is None:
        return 0, 0
    live = drifted(ob)
    have = _tile_objects(ob)
    base = base_floor_steps(ob)
    mat = _drift_material() if live else None
    for key, o in have.items():
        if key in live or not o.get("exmateria_map/drift"):
            continue
        o["exmateria_map/drift"] = False       # drift cleared: unmark, keep
        # Back to decision 10's shared colour material, NOT flat grey: the
        # tile still has to read as terrain once it stops reading as drift.
        _set_tile_material(o, _tile_material())
    for (x, z), (step_now, step, _bottom) in live.items():
        o = have.get((x, z))
        if o is None:                          # outside the carried grid
            continue
        o["exmateria_map/drift"] = True
        _set_tile_material(o, mat)
        o.display_type = "TEXTURED"
        # The base values ride the tile so decision 17's panel can show them
        # beside each field; they are shown, never declared (§6.3).
        b_step, b_sh, b_st = base[(x, z)]
        o["height_base"], o["slope_height_base"], o["slope_type_base"] = \
            b_step, b_sh, b_st
        o["drift_step_now"] = step_now
        for f in DRIFT_FIELDS:
            if f not in o.keys():
                o[f] = {"height": step_now, "slope_height": b_sh,
                        "slope_type": b_st}[f]
    fixed = sum(1 for k, o in have.items()
                if k in live and any(is_declared(o, f) for f in DRIFT_FIELDS))
    ob["exmateria_map/drift_count"] = len(live)
    ob["exmateria_map/drift_fixed"] = fixed
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
        # The SHADOWED row is gone with ADR-0187 decision 3: one object per
        # tile means one record per tile, so there is no longer a class of
        # tile that drifted but whose fix the checker has to stand aside from.
        said = f"{n} drifted, {fixed} with a declared fix"
        print(f"EXMATERIA-MAP terrain: {said}")
        self.report({"INFO"}, said)
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


def _print_growth_preview(grid):
    """The four growth numbers §7.5 states, to stdout.

    They were four label rows in `MAP_PT_terrain` and are the bulk of the text
    the artist asked to have out of the column.  Printed rather than dropped:
    *"tiles created that a file outside the map names"* is a REFUSAL waiting to
    happen, and `externally_pinned` distinguishes `0` from `n/a -- pin table
    missing`, which is exactly the distinction a silent panel loses.

    Takes the GRID (`_extent_update`'s `self`) and finds its marker, because
    `growth_preview` is written against the marker.  Never raises: this is a
    print on a property update, and an exception here would abort the typed
    edit that triggered it.
    """
    try:
        marker = next((m for m in markers(bpy.context.scene)
                       if grid_extent(m) is not None
                       and grid_extent(m)[0] == grid), None)
        if marker is None:
            return
        pv = growth_preview(marker)
        if not pv:
            return
        pinned = ("n/a — pin table missing" if pv["externally_pinned"] is None
                  else pv["externally_pinned"])
        print(f"EXMATERIA-MAP terrain: {pv['created']} tile handle(s) to "
              f"create, {pv['changed']} existing tile(s) changed, "
              f"0 already carrying a record (not in the document), "
              f"{pinned} named by a file outside the map")
    except Exception as exc:                       # never break a typed edit
        print(f"EXMATERIA-MAP terrain: growth preview unavailable ({exc})")


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
    # The panel is controls-only now, so the clamp and the growth preview say
    # themselves HERE -- on the typed change, which is when they are true and
    # is once per edit rather than once per redraw.  `extent_message` is still
    # stored: it is what a refused SizeX is explained by, and a field that
    # snaps back with nothing said anywhere is the defect this print prevents.
    if msg:
        print(f"EXMATERIA-MAP terrain: {msg}")
    _print_growth_preview(self)


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
        # ADR-0187 decision 13: a handle is a tile of the same grid, so it
        # goes in the same nested `terrain` collection.  Linked beside the
        # marker instead, the collection visibility that IS the toggle cannot
        # hide it, and decision 14's "one toggle covers both levels" becomes
        # one toggle covering most tiles.
        col = _terrain_collection(marker_collection(ob))
        have = _tile_objects(ob)
        mat = _tile_material()
        was_x = _g.get("size_x_shadow", sx)
        was_z = _g.get("size_z_shadow", sz)
        made = 0
        # §7.2's seed is "an existing RECORD's values when there is one", and
        # `have` is not that set.  It was, while ~0 tile objects existed; since
        # ADR-0187 decision 3 it is the whole grid, so this used to pick an
        # arbitrary CARRIED tile -- tile (0, 0) -- and dress every new handle
        # in the base map's bytes.  A record is a tile that DECLARES something.
        src = next((t for t in have.values()
                    if any(is_declared(t, f) for f in TILE_PAYLOAD_FIELDS)),
                   None)
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
                    # ...and only the fields `src` DECLARES.  Every tile
                    # carries all twenty values since decision 3, and on a
                    # drift tile the other seventeen are still the base map's
                    # bytes -- so copying `src`'s whole key set dresses the new
                    # handle in the base map after all, by a longer route.
                    if src is not None and is_declared(src, f):
                        o[f] = int(src[f])
                    else:
                        o[f] = 1 if f == "impassable" else 0
                # Decision 10 again: a growth handle is a tile of the same
                # grid, so it is coloured the same way.  Painted after the
                # seed above, because the impassable bit decides `R += 32`.
                _paint_tile(o, x, z, o.get("impassable"), o.get("unselectable"))
                have[(x, z)] = o
                made += 1
        said = (f"{made} tile handle(s) created; the extent itself grew when "
                f"the field was set")
        print(f"EXMATERIA-MAP terrain: {said}")
        self.report({"INFO"}, said)
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
    # Renumbered again when `Isolate` joined Push and Camera at the top
    # (decision 13); the relative order below them is unchanged.
    bl_order = 5

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
        g, _sx, _sz = ext
        # CONTROLS ONLY.  Reported from use: *"terrain is ok... it just has a
        # bunch of text taking up too much space for no reason.  That text
        # should just be sent to the terminal -- or something.  just the 3
        # buttons size x, size z, and apply growth."*
        #
        # Eight label rows went: the `Grid sx x sz` header (the two fields
        # below it already say it), the clamp message, the four growth-preview
        # numbers and the two drift counts.  None of them are DELETED -- every
        # one is printed, and the two that answer a question the artist asked
        # by pressing something are on that operator's report as well.
        #
        # They print from the places that FIRE ON A CHANGE -- `_extent_update`
        # when a field is typed, the two operators when they are pressed --
        # and never from here.  `draw` runs on every redraw of the region, so
        # a print in this method is a print per mouse-move; the terminal it was
        # sent to would be unreadable, and `growth_preview` walks the tiles.
        col = layout.column(align=True)
        col.prop(g, "exmateria_map_size_x", text="SizeX")
        col.prop(g, "exmateria_map_size_z", text="SizeZ")
        col.operator(MAP_OT_apply_growth.bl_idname, icon="ADD")
        # Kept, against the literal "just the 3 buttons": Check drift is a
        # BUTTON and the report was the complaint.  Its counts print and land
        # on the operator report; there is no other door to a drift resync, so
        # deleting it would remove a feature rather than a paragraph.
        layout.operator(MAP_OT_check_drift.bl_idname, icon="FILE_REFRESH")

    def _tile(self, layout, ob):
        """The three tile classes, and only two of them are writable.

        ADR-0187 decision 11: on a **carried** tile every one of the twenty
        checkboxes led to `build` refusing with *"that tile is still the
        base's"*, so the panel offered the artist twenty ways to break their
        own document.  Read-only is not blank -- the values still show, which
        is the whole point of drawing the grid.

        The class is DERIVED here, the way `export_document.tile_record`
        derives it, because decision 3 deleted the stored kind: the flag now
        says "imported" on a tile that has since drifted.

        One checkbox does survive on a carried tile: a field it ALREADY
        declares.  That state is reachable -- a drift fix stays declared when
        the drift clears (decision 4) -- and without the box the artist's only
        exit from the refusal is a re-import that throws the rest away.
        """
        drift = bool(ob.get("exmateria_map/drift"))
        growth = ob.get("exmateria_map/tile") == TILE_GROWTH
        kind = TILE_DRIFT if drift else (TILE_GROWTH if growth else "carried")
        layout.label(text=f"tile ({ob.get('x')}, {ob.get('z')}) "
                          f"L{ob.get('level')} — {kind}",
                     icon="MESH_PLANE")
        if drift:
            layout.label(text=f"floor now at step {ob.get('drift_step_now')}, "
                              f"base says {ob.get('height_base')}",
                         icon="INFO")
        elif not growth:
            layout.label(text="the base map's own bytes — read-only",
                         icon="LOCKED")
        writable = DRIFT_FIELDS if drift else \
            (TILE_PAYLOAD_FIELDS if growth else ())
        for f in TILE_PAYLOAD_FIELDS:
            row = layout.row(align=True)
            on = is_declared(ob, f)
            if f in writable or on:
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
