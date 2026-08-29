"""S1+S2 harness: drive the REAL addon operator headless and read the scene back.

Unlike the old stub test, this asserts what import-v1 §1–§7 actually build:
the mesh geometry (decision 14's axis map + ring reversal + per-face flip),
every schema-v1 named face/corner attribute and its `<field>_shadow` twin,
the marker JSON properties, the grid/tile objects, the two material slots,
the §4 texture leg (index images from the tracked sidecar PNG, 16x16 CLUT
images per state, the preview node graph, the corner light attribute, the
state selector), and the refusal rule.  The axis frame is re-asserted
against the pinned constants (axis 2 of the #520 plan), so a silent
re-derivation of `(x, z, -y)` cannot pass unnoticed.

Run:  python3 tests/blender_roundtrip.py [blender-binary]
"""
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
ADDON_DIR = PKG / "addons" / "exmateria_map"
FIXTURES = PKG / "tests" / "fixtures"
FIXTURE = FIXTURES / "MAP001.a0.stub.json"
TMP = Path(__file__).resolve().parent / ".blender_rt"
REPORT = TMP / "report.json"

SCRIPT_TEMPLATE = r'''
import importlib.util
import json
import math
import os
import sys
import traceback

import bpy
import bpy.utils

PKG = "@ADDONPKG@"
ZIP = "@ZIP@"
JSON = "@JSON@"
OUT = "@OUT@"

# Install + enable the addon in THIS process.
#
# **`--factory-startup` does NOT give this a scratch user dir.** That claim
# stood here and was false: the flag resets PREFERENCES, not the scripts
# directory, so with no `BLENDER_USER_RESOURCES` this `addon_install` writes
# into the artist's real `~/.config/blender/<ver>/scripts/addons/` -- and the
# `addon_enable` below then grades whatever is there. The launcher sets that
# variable for every run now (`tests/blender_env.py`); this comment is what
# sent a session looking in the wrong place, so it says the opposite loudly.
try:
    bpy.ops.preferences.addon_install(filepath=ZIP)
except Exception as e:
    print(f"INSTALL: {e}")
bpy.ops.preferences.addon_enable(module='exmateria_map')

sys.path.insert(0, PKG)
from exmateria_map import import_document as mod

doc = json.loads(open(JSON).read())
name = f"{doc['base']['map']}.a{doc['base']['arrangement']}"
checks = {}


def check(n, cond, detail=""):
    checks[n] = bool(cond)
    if not cond:
        print(f"CHECK FAIL {n}: {detail}")


def run_import(path):
    try:
        return bpy.ops.import_map.document(filepath=str(path))
    except RuntimeError as e:
        # Blender raises on self.report({"ERROR"}, ...) — that IS the refusal
        return {"result": "CANCELLED", "error": str(e)}


def importable_objects():
    return [o for o in bpy.data.objects if o.name == name or o.name.startswith(name + "_") or o.name.startswith("tile_")]


def clear_scene():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    for c in list(bpy.data.collections):
        if c.name != "Collection":
            bpy.data.collections.remove(c)


# ---------------------------------------------------------------- refusal ---
staged = JSON.replace(".json", ".staged.json")
open(staged, "w").write(open(JSON).read())

# ADR-0004 decision 27: `version` is the OLDEST leg that can handle the
# document, so 2 is now ACCEPTED and 3 is the next unknown. Both arms, because
# "refuses 3" alone is satisfied by an addon that refuses everything above 1 --
# which is the code this decision replaces.
bad = dict(doc)
bad["version"] = 3
p = JSON.replace(".json", ".bad_version.json")
json.dump(bad, open(p, "w"))
res = run_import(p)
check("refuse_version3", "FINISHED" not in res, f"res={res}")
check("refuse_version3_no_objects", len(importable_objects()) == 0)

ok2 = dict(doc)
ok2["version"] = 2
p = JSON.replace(".json", ".version2.json")
json.dump(ok2, open(p, "w"))
res = run_import(p)
check("accept_version2", "FINISHED" in res, f"res={res}")
check("accept_version2_built", len(importable_objects()) > 0)
clear_scene()

bad2 = dict(doc)
bad2["format"] = "some-other/interchange"
p2 = JSON.replace(".json", ".bad_format.json")
json.dump(bad2, open(p2, "w"))
res = run_import(p2)
check("refuse_bad_format", "FINISHED" not in res, f"res={res}")

p3 = JSON.replace(".json", ".notjson.json")
open(p3, "w").write("this is { not json")
res = run_import(p3)
check("refuse_not_json", "FINISHED" not in res, f"res={res}")

# ----------------------------------------------------------------- import ---
res = run_import(staged)
check("import_finished", res == {"FINISHED"}, f"res={res}")

# --- the import browser must not re-navigate on every launch ----------------
# `invoke` assigning `filepath` OVERRIDES Blender's own last-directory memory,
# so the addon has to keep that memory itself or the artist starts at the
# filesystem root every session (fresh `scene.render.filepath` is `/tmp/`, and
# its parent is `/`).  Preferences, not scene properties: the point is
# surviving a restart.
_want_dir = os.path.dirname(staged)
_prefs = mod._prefs(bpy.context)
check("prefs_registered", _prefs is not None, "addon preferences not registered")
check("import_remembers_dir",
      _prefs is not None and os.path.normpath(_prefs.last_dir) == os.path.normpath(_want_dir),
      f"{None if _prefs is None else _prefs.last_dir!r} != {_want_dir!r}")
check("browser_opens_at_remembered",
      os.path.normpath(mod.start_filepath(bpy.context)) == os.path.normpath(_want_dir),
      mod.start_filepath(bpy.context))
if _prefs is not None:                      # the no-memory-yet arm must not throw
    _keep = _prefs.last_dir
    _prefs.last_dir = ""
    try:
        _fallback = mod.start_filepath(bpy.context)
        check("browser_fallback_without_memory", isinstance(_fallback, str) and _fallback != "")
    except Exception as e:
        check("browser_fallback_without_memory", False, repr(e))
    _prefs.last_dir = _keep

polys = doc["polygons"]
flipped = [mod._wound_against(p) for p in polys]
orders = [mod.import_order(len(p["positions"]), f) for p, f in zip(polys, flipped)]

col = bpy.data.collections.get(name)
check("collection_exists", col is not None, "missing collection")
ob = bpy.data.objects.get(name)
check("mesh_object_exists", ob is not None, "missing mesh object")
check("mesh_in_collection", col is not None and ob is not None and col.objects.get(ob.name) is ob)
if ob is None:
    json.dump({"checks": checks}, open(OUT, "w"))
    raise SystemExit(1)
me = ob.data

n_corners = sum(len(p["positions"]) for p in polys)
n_distinct = len({mod._fft_to_blender(tuple(c)) for p in polys for c in p["positions"]})
check("n_faces", len(me.polygons) == len(polys), f"{len(me.polygons)} != {len(polys)}")
check("n_verts_welded", len(me.vertices) == n_distinct,
      f"{len(me.vertices)} != {n_distinct} distinct positions (import-v1 §1 weld)")
check("quads_stay_quads",
      all(len(me.polygons[i].loop_indices) == len(polys[i]["positions"]) for i in range(len(polys))))

# positions / normals / UV, loop-by-loop, through the pinned axis map.  The
# CORNER domain is loop-indexed, so the corner attributes are read directly;
# `positions_shadow` carries each loop's position.
ok_pos = True
detail = ""
for i, p in enumerate(polys):
    for slot, li in enumerate(me.polygons[i].loop_indices):
        corner = orders[i][slot]
        exp_pos = mod._fft_to_blender(tuple(p["positions"][corner]))
        got_pos = tuple(me.attributes["positions_shadow"].data[li].vector)
        if got_pos != exp_pos:
            ok_pos = False
            detail = f"face {i} loop {slot}: pos {got_pos} != {exp_pos}"
            break
    if not ok_pos:
        break
check("positions_axis14", ok_pos, detail)
detail = ""
ok_nrm = True
for i, p in enumerate(polys):
    for slot, li in enumerate(me.polygons[i].loop_indices):
        corner = orders[i][slot]
        exp_n = mod._fft_to_blender(tuple(p["normals"][corner])) if p["kind"] in mod.TEXTURED_KINDS else (0.0, 0.0, 0.0)
        got_n = tuple(me.attributes["normals"].data[li].vector)
        if got_n != exp_n:
            ok_nrm = False
            detail = f"face {i} loop {slot}: normal {got_n} != {exp_n}"
            break
    if not ok_nrm:
        break
check("normals_axis14", ok_nrm, detail)
detail = ""
ok_uv = True
uv = me.uv_layers.get("UVMap")
check("uv_layer", uv is not None, "no UVMap layer")
if uv:
    for i, p in enumerate(polys):
        for slot, li in enumerate(me.polygons[i].loop_indices):
            corner = orders[i][slot]
            if p["kind"] in mod.TEXTURED_KINDS:
                exp_uv = mod._uv_enc(p["uv"][corner][0], p["uv"][corner][1], p["texture_page"])
                got_uv = tuple(uv.data[li].uv)
                if abs(got_uv[0] - exp_uv[0]) > 1e-9 or abs(got_uv[1] - exp_uv[1]) > 1e-9:
                    ok_uv = False
                    detail = f"face {i} loop {slot}: uv {got_uv} != {exp_uv}"
                    break
        if not ok_uv:
            break
check("uv_encoding", ok_uv, detail)

# face attributes: values AND shadow twins (import-v1 §3, §7)
detail = ""
ok_fa = True
for i, p in enumerate(polys):
    is_t = p["kind"] in mod.TEXTURED_KINDS
    expected = {
        "visible_angles": -1 if p.get("visible_angles") is None else p["visible_angles"],
        "palette_id": p.get("palette_id", 0),
        "palette_byte_high_nibble": p.get("palette_byte_high_nibble", 0),
        "texture_page": p.get("texture_page", 0),
        "unknown_texture_value_6a": p.get("unknown_texture_value_6a", 0),
        "texture_byte6_high_nibble": p.get("texture_byte6_high_nibble", 0),
        "terrain_x": p.get("terrain", {}).get("x", 0),
        "terrain_z": p.get("terrain", {}).get("z", 0),
        "terrain_level": p.get("terrain", {}).get("level", 0),
        "unknown_untextured_0": (0 if is_t else p["unknown_untextured"][0]),
        "unknown_untextured_1": (0 if is_t else p["unknown_untextured"][1]),
        "unknown_untextured_2": (0 if is_t else p["unknown_untextured"][2]),
        "unknown_untextured_3": (0 if is_t else p["unknown_untextured"][3]),
    }
    for k, want in expected.items():
        got = me.attributes[k].data[i].value
        if got != want:
            ok_fa = False
            detail = f"face {i}: {k} = {got} != {want}"
            break
        if me.attributes[k + "_shadow"].data[i].value != want:
            ok_fa = False
            detail = f"face {i}: {k}_shadow drifted"
            break
    if me.attributes["textured"].data[i].value != is_t:
        ok_fa, detail = False, f"face {i}: textured flag"
    if me.attributes["fft_ring_flipped"].data[i].value != flipped[i]:
        ok_fa, detail = False, f"face {i}: fft_ring_flipped"
    if not ok_fa:
        break
check("face_int_attrs+shadows", ok_fa, detail)

detail = ""
ok_cs = True
for i, p in enumerate(polys):
    for slot, li in enumerate(me.polygons[i].loop_indices):
        corner = orders[i][slot]
        exp_p = mod._fft_to_blender(tuple(p["positions"][corner]))
        if tuple(me.attributes["positions_shadow"].data[li].vector) != exp_p:
            ok_cs = False
            detail = f"face {i} loop {slot}: positions_shadow"
            break
        exp_n = mod._fft_to_blender(tuple(p["normals"][corner])) if p["kind"] in mod.TEXTURED_KINDS else (0.0, 0.0, 0.0)
        if tuple(me.attributes["normals_shadow"].data[li].vector) != exp_n:
            ok_cs = False
            detail = f"face {i} loop {slot}: normals_shadow"
            break
    if not ok_cs:
        break
check("corner_attrs+shadows", ok_cs, detail)

# axis-2 hard bar, post-import: every textured face with a non-degenerate
# average corner normal is wound WITH its normal, never against it.
detail = ""
ok_wind = True
for i, p in enumerate(polys):
    if p["kind"] not in mod.TEXTURED_KINDS:
        continue
    pts = [tuple(me.vertices[li].co) for li in me.polygons[i].loop_indices]
    ns = [tuple(me.attributes["normals"].data[li].vector) for li in me.polygons[i].loop_indices]
    acc = [sum(v[k] for v in ns) / len(ns) for k in range(3)]
    g = mod._newell(pts)
    gm, am = mod._mag(g), mod._mag(acc)
    if gm > 1e-3 and am > 1e-6:
        if mod._dot(g, acc) / (gm * am) < -mod.WIND:
            ok_wind = False
            detail = f"face {i} wound against its normal"
            break
check("winding_aligned_post_import", ok_wind, detail)

# marker JSON properties (import-v1 §6)
detail = ""
ok_marker = True
for section in ("base", "polygons", "terrain", "map_states", "carry"):
    if f"exmateria_map/{section}" not in ob:
        ok_marker = False
        detail = f"missing property exmateria_map/{section}"
        break
    if json.loads(ob[f"exmateria_map/{section}"]) != doc[section]:
        ok_marker = False
        detail = f"exmateria_map/{section} does not round-trip"
        break
check("marker_json_props", ok_marker, detail)

# grid + tiles
tg = doc["base"].get("terrain_grid")
grid = bpy.data.objects.get(f"{name}_grid")
if tg:
    check("grid_object", grid is not None)
    if grid:
        check("grid_props", grid["size_x"] == tg["size_x"] and grid["size_z"] == tg["size_z"]
              and grid["size_x_shadow"] == tg["size_x"] and grid["size_z_shadow"] == tg["size_z"])
        check("grid_extent", grid.data.vertices[0].co.z == 0.0 and len(grid.data.polygons) == 1)
else:
    check("no_grid_when_absent", grid is None)

# ============================== ADR-0187 §1: the tile's geometry (dec. 6, 7) ===
# `build_tile` places the quad at `(height + depth) * 12` and lifts whichever
# corners the slope type names by `slope_height * 12`.  GaneshaDx's `+1` display
# nudge rides `delta_location.z`, NOT `co`, so `export_terrain`, the drift tests
# and the lighting bake's BVH all still read the record's own number.
#
# The expectations below are transcribed from
# `vendor/GaneshaDx/Resources/ContentDataTypes/Terrains/TerrainTile.cs:100-151`
# and `Common/CommonLists.cs:92` in GaneshaDx's OWN vertex order, and are keyed
# by WORLD CORNER rather than by our mesh's corner index.  That is deliberate:
# GaneshaDx's V0..V3 walk (x, z), (x, z+1), (x+1, z+1), (x+1, z) while
# `_plain_quad_mesh` walks (x, z), (x+1, z), (x+1, z+1), (x, z+1), so a lift
# table transcribed straight across without swapping 1 and 3 MIRRORS every
# incline -- and would still satisfy any check that only asked "did some corner
# move".  Keying on world position is what makes that failure visible.
GDX_LIFT = {
    0:   (),            # Flat
    133: (1, 2),        # InclineNorth
    82:  (2, 3),        # InclineEast
    37:  (0, 3),        # InclineSouth
    88:  (0, 1),        # InclineWest
    65:  (2,),          # ConvexNortheast
    17:  (3,),          # ConvexSoutheast
    20:  (0,),          # ConvexSouthwest
    68:  (1,),          # ConvexNorthwest
    150: (1, 2, 3),     # ConcaveNortheast
    102: (0, 2, 3),     # ConcaveSoutheast
    105: (0, 1, 3),     # ConcaveSouthwest
    153: (0, 1, 2),     # ConcaveNorthwest
}
#: GaneshaDx vertex index -> (dx, dz) in tile units.
GDX_CORNER = {0: (0, 0), 1: (0, 1), 2: (1, 1), 3: (1, 0)}


def raw_fields(b):
    """The four fields the geometry needs, straight off the 8 bytes.

    Transcribed from `MeshResourceData.ProcessTerrain` -- byte 3's high three
    bits are `depth`, its low five `slope_height` -- so this does not borrow
    the addon's own decode."""
    return {"height": b[2], "depth": b[3] >> 5,
            "slope_height": b[3] & 0x1F, "slope_type": b[4]}


def corner_heights(ob):
    """{(dx, dz): world Z} for one tile object, read off the mesh."""
    x, z = ob["x"], ob["z"]
    out = {}
    for v in ob.data.vertices:
        dx = round((v.co[0] - x * mod.TILE_UNITS) / mod.TILE_UNITS)
        dz = round((v.co[1] - z * mod.TILE_UNITS) / mod.TILE_UNITS)
        out[(dx, dz)] = v.co[2]
    return out


def expected_heights(x, z, b):
    f = raw_fields(b)
    floor = (f["height"] + f["depth"]) * mod.HEIGHT_STEP
    lift = f["slope_height"] * mod.HEIGHT_STEP
    named = GDX_LIFT.get(f["slope_type"], ())      # an unlisted byte is Flat
    return {GDX_CORNER[i]: floor + (lift if i in named else 0.0)
            for i in range(4)}


_geo_col = bpy.data.collections.new("adr0187_geometry")
bpy.context.scene.collection.children.link(_geo_col)
# One synthetic tile per slope byte, plus the flat/depth cases.  The corpus
# only ships 8 of the 13 slope types on MAP001 a0, and a table is wrong in the
# rows nobody looked at.
_cases = [("flat", [0, 0, 5, 0, 0, 0, 0, 0]),
          ("depth", [0, 0, 5, (3 << 5) | 0, 0, 0, 0, 0]),
          ("depth_and_slope", [0, 0, 5, (2 << 5) | 4, 133, 0, 0, 0])]
_cases += [(f"slope_{_b}", [0, 0, 7, (0 << 5) | 3, _b, 0, 0, 0])
           for _b in sorted(GDX_LIFT)]
# A slope byte no map uses: GaneshaDx falls back to Flat rather than throwing.
_cases.append(("slope_unknown", [0, 0, 7, (0 << 5) | 3, 199, 0, 0, 0]))
# Far outside any legal 18x18 grid, so these synthetic tiles cannot be mistaken
# for the fixture's own by the checks that follow.
for _i, (_label, _bytes) in enumerate(_cases):
    _e = [200 + _i, 200, 0] + _bytes
    _t = mod.build_tile(_e, _geo_col)
    _want = expected_heights(200 + _i, 200, _bytes)
    _got = corner_heights(_t)
    check(f"adr0187_tile_geometry_{_label}",
          all(abs(_got.get(k, 1e9) - v) < 1e-4 for k, v in _want.items()),
          f"want {_want}, got {_got}")
    # Decision 7: the +1 is a DISPLAY fact and lives nowhere near `co`.
    check(f"adr0187_tile_nudge_{_label}",
          abs(_t.delta_location[2] - 1.0) < 1e-6,
          f"delta_location.z = {_t.delta_location[2]}")

# The seed that proves the corner map is actually under test.  Copying
# GaneshaDx's vertex indices onto OUR corner order without swapping 1 and 3
# lifts a DIFFERENT pair of world corners -- the same COUNT of corners, at the
# same height, mirrored about Z.  Assert the two readings disagree, or the
# checks above would pass against either table.
OUR_CORNER = {0: (0, 0), 1: (1, 0), 2: (1, 1), 3: (0, 1)}
_mirrored = [b for b, lifts in GDX_LIFT.items()
             if {GDX_CORNER[i] for i in lifts} != {OUR_CORNER[i] for i in lifts}]
check("adr0187_geometry_seed_catches_a_mirrored_table",
      len(_mirrored) == 8 and 133 in _mirrored and 82 in _mirrored,
      f"only {len(_mirrored)} of the 13 slope bytes can tell the swapped and "
      f"unswapped corner maps apart: {_mirrored}")
for _o in list(_geo_col.objects):
    bpy.data.objects.remove(_o, do_unlink=True)
bpy.data.collections.remove(_geo_col)

# ==================== ADR-0187 §1: one object per carried slot (dec. 3, 4, 5) ==
# The tiles come from `base.terrain_tiles` now, not from `doc["terrain"]`.  An
# untouched document declares NO record (decision 22) and still shows the whole
# grid, which is the entire point of the ADR.
LEVEL1_DEFAULT = [0, 0, 0, 0, 0, 0, 1, 0]
_carried = doc["base"].get("terrain_tiles") or []
check("adr0187_fixture_carries_the_grid",
      len(_carried) == 2 * tg["size_x"] * tg["size_z"] and len(_carried) > 100,
      f"{len(_carried)} carried rows; the checks below would be vacuous")

_want_l0 = [e for e in _carried if e[2] == 0]
_missing = [e[:3] for e in _want_l0
            if bpy.data.objects.get(f"tile_{e[0]}_{e[1]}_L0") is None]
check("adr0187_every_level_0_slot_has_an_object", not _missing,
      f"{len(_missing)} level-0 slots have no tile object: {_missing[:5]}")

# Decision 5: a level-1 object wherever the slot is not the format's default.
# NOT `selectable`, which would hide the 977 corpus slots that are unselectable
# and carry a real height -- a byte that is not the default is a byte someone
# put there.
_l1_real = [e for e in _carried if e[2] == 1 and e[3:] != LEVEL1_DEFAULT]
_l1_default = [e for e in _carried if e[2] == 1 and e[3:] == LEVEL1_DEFAULT]
check("adr0187_level_1_seed_has_both_populations",
      len(_l1_real) > 0 and len(_l1_default) > 0,
      f"{len(_l1_real)} non-default and {len(_l1_default)} default level-1 "
      f"slots; the predicate below cannot be told from `always` or `never`")
check("adr0187_level_1_object_where_the_slot_is_not_default",
      all(bpy.data.objects.get(f"tile_{e[0]}_{e[1]}_L1") is not None
          for e in _l1_real),
      f"a non-default level-1 slot got no object: "
      f"{[e[:3] for e in _l1_real if bpy.data.objects.get(f'tile_{e[0]}_{e[1]}_L1') is None][:5]}")
check("adr0187_no_level_1_object_on_a_default_slot",
      all(bpy.data.objects.get(f"tile_{e[0]}_{e[1]}_L1") is None
          for e in _l1_default),
      f"a default level-1 slot got an object: "
      f"{[e[:3] for e in _l1_default if bpy.data.objects.get(f'tile_{e[0]}_{e[1]}_L1') is not None][:5]}")

# The carried tile's geometry is the BASE's, and it declares nothing.
_carried_ok = True
_carried_detail = ""
_declared_keys = {(r["x"], r["z"], r.get("level", 0))
                  for r in (doc.get("terrain") or [])}
for _e in _want_l0:
    if (_e[0], _e[1], 0) in _declared_keys:
        continue                    # a tile the document declares is not carried
    _t = bpy.data.objects.get(f"tile_{_e[0]}_{_e[1]}_L0")
    if _t is None:
        continue
    _w = expected_heights(_e[0], _e[1], _e[3:])
    _g = corner_heights(_t)
    if any(abs(_g.get(k, 1e9) - v) > 1e-4 for k, v in _w.items()):
        _carried_ok, _carried_detail = False, f"tile {_e[:3]}: want {_w}, got {_g}"
        break
    if any(bool(_t.get(f + "_declared")) for f in mod.TILE_PAYLOAD_FIELDS):
        _carried_ok = False
        _carried_detail = (f"tile {_e[:3]} declares a field; a carried tile is "
                           f"the base's and decision 22 makes that every tile")
        break
check("adr0187_carried_tiles_draw_the_base_and_declare_nothing",
      _carried_ok, _carried_detail)

# A carried tile SHOWS all twenty payload values -- read-only (decision 11),
# but visible, which is what the sidebar draws as text.
_t00 = bpy.data.objects.get("tile_0_0_L0")
_e00 = next(e for e in _want_l0 if (e[0], e[1]) == (0, 0))
check("adr0187_carried_tile_shows_every_payload_field",
      _t00 is not None
      and all(f in _t00.keys() for f in mod.TILE_PAYLOAD_FIELDS)
      and _t00["surface_type"] == _e00[3] & 0x3F
      and _t00["height"] == _e00[5],
      "a carried tile hides the values the panel has to show")

# ---- ADR-0187 decision 10: GaneshaDx's GetVertexColor, ported --------------
# Transcribed from `TerrainTile.cs:164-201`, NOT read back off the addon: a
# grey ramp by the VERTEX's own height (so a slope ramps across the tile), a
# chequer on `(IndexX + IndexZ) % 2`, and `R += 32` on impassable or
# unselectable.  The tab / hover / selection branches are Blender's job and
# are deliberately not ported.
def gdx_vertex_color(ix, iz, vertex_z, impassable, unselectable):
    base = int(vertex_z / 12) * 7 + 16 + (0 if (ix + iz) % 2 == 0 else 8)
    r, g, b = base + 16, base + 16, base
    if impassable or unselectable:
        r += 32
    return (min(255, max(0, r)), min(255, max(0, g)), min(255, max(0, b)))


def tile_colors(ob):
    """The tile's colour attribute, read back as bytes, keyed by vertex."""
    attr = ob.data.color_attributes.get(mod.TILE_COLOR_ATTR) \
        if hasattr(mod, "TILE_COLOR_ATTR") else None
    if attr is None:
        return None
    out = []
    for i, v in enumerate(ob.data.vertices):
        c = attr.data[i].color_srgb if attr.data_type == "BYTE_COLOR" \
            else attr.data[i].color
        out.append((round(c[0] * 255), round(c[1] * 255), round(c[2] * 255),
                    round(v.co[2])))
    return out


_col_tiles = [o for o in bpy.data.objects
              if o.name.startswith("tile_") and "exmateria_map/tile" in o]
_col_bad = []
_col_seen = set()
_col_bumped = 0
for _ct in _col_tiles:
    _got = tile_colors(_ct)
    if _got is None:
        _col_bad.append((_ct.name, "no colour attribute"))
        continue
    if _ct.get("impassable") or _ct.get("unselectable"):
        _col_bumped += 1
    for _r, _g, _b, _vz in _got:
        _want = gdx_vertex_color(_ct["x"], _ct["z"], _vz,
                                 _ct.get("impassable"), _ct.get("unselectable"))
        _col_seen.add((_r, _g, _b))
        if (_r, _g, _b) != _want and len(_col_bad) < 6:
            _col_bad.append((_ct.name, _vz, (_r, _g, _b), _want))
check("adr0187_every_tile_carries_a_colour_attribute",
      _col_tiles and all(tile_colors(_o) is not None for _o in _col_tiles),
      f"{sum(1 for _o in _col_tiles if tile_colors(_o) is None)} of "
      f"{len(_col_tiles)} tiles have no colour attribute")
check("adr0187_tile_colour_is_ganeshadx_per_vertex", not _col_bad,
      str(_col_bad[:4]))
check("adr0187_tile_colour_arm_is_not_vacuous",
      len(_col_seen) > 4 and _col_bumped > 0,
      f"{len(_col_seen)} distinct colours over {len(_col_tiles)} tiles, "
      f"{_col_bumped} of them impassable/unselectable -- a constant would pass")
# One material, shared, and it READS the attribute: a per-tile copy would put
# hundreds of materials in the file and a hardcoded colour would pass the
# attribute checks above while showing the artist flat grey.
_col_mats = {_o.data.materials[0] for _o in _col_tiles if _o.data.materials}
check("adr0187_one_shared_tile_material", len(_col_mats) == 1,
      f"{len(_col_mats)} distinct materials over {len(_col_tiles)} tiles")
_col_mat = next(iter(_col_mats), None)
_col_attr_nodes = [] if _col_mat is None else \
    [n for n in _col_mat.node_tree.nodes if n.bl_idname == "ShaderNodeVertexColor"]
check("adr0187_tile_material_reads_the_attribute",
      len(_col_attr_nodes) == 1
      and _col_attr_nodes[0].layer_name == mod.TILE_COLOR_ATTR,
      str([(n.bl_idname, getattr(n, "layer_name", None))
           for n in (_col_mat.node_tree.nodes if _col_mat else [])]))
check("adr0187_tile_material_is_unlit",
      _col_mat is not None
      and any(n.bl_idname == "ShaderNodeEmission" for n in _col_mat.node_tree.nodes)
      and not any(n.bl_idname == "ShaderNodeBsdfPrincipled"
                  for n in _col_mat.node_tree.nodes),
      "the grid is a display overlay; Blender's lighting must not touch it")

# ---- decision 4: a DECLARED record lands on the object that is already there
# The fixture declares one level-0 record.  Import must mark those fields
# declared on the tile the carried grid already built, or an authored fix would
# not survive a save-and-reopen -- and `export_terrain` reads declarations off
# the objects and nowhere else.
for r in [x for x in (doc.get("terrain") or []) if x.get("level", 0) == 0]:
    tob = bpy.data.objects.get(f"tile_{r['x']}_{r['z']}_L{r['level']}")
    if tob is None:
        check(f"tile_{r['x']}_{r['z']}", False, "missing tile object")
        continue
    ok = True
    for k in ("x", "z", "level"):
        if tob[k] != r[k] or tob[f"{k}_shadow"] != r[k]:
            ok = False
    for k, want in r.items():
        if k in ("x", "z", "level"):
            continue
        if not bool(tob.get(k + "_declared")):
            check(f"tile_declared_{r['x']}_{r['z']}", False,
                  f"declared field {k} did not reach the object")
            ok = False
            break
        if tob[k] != want or tob[f"{k}_shadow"] != want:
            ok = False
    # A field the record does not name stays UNDECLARED -- "an absent field is
    # not zero".  It still carries the base's value; what it must not carry is
    # the declaration.
    for k in mod.TILE_PAYLOAD_FIELDS:
        if k in r:
            continue
        if bool(tob.get(k + "_declared")):
            check(f"tile_absent_{r['x']}_{r['z']}", False,
                  f"undeclared field {k} came back declared")
            ok = False
            break
    check(f"tile_{r['x']}_{r['z']}_props", ok)

# materials (import-v1 §4)
check("two_slots", len(me.materials) == 2, f"slots={len(me.materials)}")
if len(me.materials) == 2:
    check("slot0_unlit_grey", me.materials[0].name == mod.UNLIT_GREY, me.materials[0].name)
    check("slot1_preview", me.materials[1].name == f"{name}_preview", me.materials[1].name)
detail = ""
ok_mi = True
for i, p in enumerate(polys):
    want = 1 if p["kind"] in mod.TEXTURED_KINDS else 0
    if me.polygons[i].material_index != want:
        ok_mi = False
        detail = f"face {i}: material_index {me.polygons[i].material_index} != {want}"
        break
check("face_material_index", ok_mi, detail)

# ============================================================ S2 textures ===
# §4: index images (256x1024 float, the sidecar's raw indices), one 16x16
# CLUT image per state (document palettes, PLTE fallback when null), the
# preview graph, the corner light attribute, the default state, and the
# state selector rewiring.
SAMPLES = {p[0]: json.loads(open(p[1]).read()) for p in json.loads('@SAMPLES@')}
states = doc["map_states"]
sheet_names = []
for st in states:
    s = st.get("texture_sheet")
    if s and s not in sheet_names:
        sheet_names.append(s)
check("sidecar_sheets", len(sheet_names) >= 1, "fixture has no texture_sheet")
for s in sheet_names:
    img = bpy.data.images.get(f"exmateria_map/{s}_index")
    check(f"index_image_{s[13:21]}",
          img is not None and tuple(img.size) == (256, 1024) and img.is_float,
          f"{s}: img={img}")
    if img is None or s not in SAMPLES:
        continue
    ok_px, detail = True, ""
    for k, want in list(SAMPLES[s]["samples"].items()):
        u, page, v = map(int, k.split(","))
        j = ((1023 - (page * 256 + v)) * 256 + u) * 4
        if round(img.pixels[j]) != want:
            ok_px, detail = False, f"({u},p{page},{v}): {img.pixels[j]} != {want}"
            break
    check(f"index_values_{s[13:21]}", ok_px, detail)

n_states = len(states)
cluts = [bpy.data.images.get(f"exmateria_map/{name}_clut_{i}") for i in range(n_states)]
for i, img in enumerate(cluts):
    check(f"clut_{i}_size", img is not None and tuple(img.size) == (16, 16),
          f"state {i} clut: {img}")
ok_c0, detail = True, ""
for row in range(16):
    for col in range(16):
        h = states[0]["palettes"][row]["colors"][col].lstrip("#")
        want = (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0)
        j = (row * 16 + col) * 4
        if cluts[0] is not None and any(abs(g - w) > 1e-6 for g, w in zip(cluts[0].pixels[j:j + 3], want)):
            ok_c0, detail = False, f"entry ({col},{row})"
            break
    if not ok_c0:
        break
check("clut0_doc_palettes", ok_c0, detail)
ok_c1, detail = True, ""
plte = [tuple(int(h[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
        for h in SAMPLES[sheet_names[0]]["plte"]]
for row in range(16):
    for col in range(16):
        j = (row * 16 + col) * 4
        if cluts[1] is not None and any(abs(g - w) > 1e-6 for g, w in zip(cluts[1].pixels[j:j + 3], plte[col])):
            ok_c1, detail = False, f"({col},{row}) != PLTE[{col}]"
            break
    if not ok_c1:
        break
check("clut1_plte_fallback", ok_c1, detail)

# the state whose resource IS the geometry source — the default preview state
_geo_state_pre = next(i for i, st in enumerate(states)
                      if st.get("resource") == doc["base"]["geometry_source"])

mat = me.materials[1]
nt = mat.node_tree
idx_node = nt.nodes.get("exmateria_map.index")
clut_node = nt.nodes.get("exmateria_map.clut")
pal_node = nt.nodes.get("exmateria_map.palette")
light_node = nt.nodes.get("exmateria_map.diffuse")
amb_node = nt.nodes.get("exmateria_map.ambient")
sum_node = nt.nodes.get("exmateria_map.light_sum")
mul_node = nt.nodes.get("exmateria_map.multiply")
boost_node = nt.nodes.get("exmateria_map.boost")
nenc_node = nt.nodes.get("exmateria_map.normal_encode")
emit = [n for n in nt.nodes if n.bl_idname == "ShaderNodeEmission"]
outp = [n for n in nt.nodes if n.bl_idname == "ShaderNodeOutputMaterial"]
# three MixRGB now: the clamped multiply, the UNCLAMPED ambient+diffuse sum,
# and the debug boost.  Addressed by NAME — a positional index would silently
# grade the wrong node.
mix = [n for n in nt.nodes if n.bl_idname == "ShaderNodeMixRGB"]
check("node_graph_nodes",
      all(x is not None for x in (idx_node, clut_node, pal_node, light_node))
      and all(x is not None for x in (amb_node, sum_node, mul_node,
                                      boost_node, nenc_node))
      and len(emit) == 1 and len(outp) == 1 and len(mix) == 3)
check("node_index_image", idx_node is not None and idx_node.image is not None
      and idx_node.image.name == f"exmateria_map/{sheet_names[0]}_index")
check("node_closest",
      idx_node is not None and idx_node.interpolation == "Closest"
      and clut_node is not None and clut_node.interpolation == "Closest")
check("node_mix_multiply",
      mul_node is not None and mul_node.blend_type == "MULTIPLY")
check("node_sum_add", sum_node is not None and sum_node.blend_type == "ADD")
# the sum MUST stay unclamped: ambient + diffuse routinely exceeds 1.0
# (#358 max gain 13.55x) and the PSX saturates only the FINAL pixel
check("node_sum_unclamped", sum_node is not None and not sum_node.use_clamp)
check("node_ambient_is_state",
      amb_node is not None
      and all(abs(a - b) < 1e-6 for a, b in zip(
          tuple(amb_node.outputs[0].default_value)[:3],
          mod.rig_ambient(states[_geo_state_pre]["light_rig"]))),
      str(tuple(amb_node.outputs[0].default_value)[:3]) if amb_node else "no node")
check("node_attr_names", pal_node is not None and pal_node.attribute_name == "palette_id"
      and light_node is not None and light_node.attribute_name == "diffuse")
check("node_link_emit_out",
      any(l.from_node.bl_idname == "ShaderNodeEmission"
          and l.to_node.bl_idname == "ShaderNodeOutputMaterial" for l in nt.links))
dec_node = nt.nodes.get("exmateria_map.srgb_decode")
check("node_srgb_decode", dec_node is not None
      and dec_node.node_tree is not None
      and dec_node.node_tree.name == "exmateria_map_srgb_to_linear")
# The chain multiplies in PSX BYTE space and decodes ONCE at the end (#427):
# mix -> decode -> emit, and both sampled images are Non-Color so nothing
# transforms the bytes on the way in.
# RNA hands out a fresh wrapper per access, so nodes are compared by NAME.
check("node_link_mix_decode",
      dec_node is not None
      and any(l.from_node.bl_idname == "ShaderNodeMixRGB"
              and l.to_node.name == dec_node.name for l in nt.links))
check("node_link_decode_emit",
      dec_node is not None
      and any(l.from_node.name == dec_node.name
              and l.to_node.bl_idname == "ShaderNodeEmission" for l in nt.links))
check("node_mix_clamped", mul_node is not None and mul_node.use_clamp)
check("images_non_color",
      idx_node is not None and clut_node is not None
      and idx_node.image.colorspace_settings.name == "Non-Color"
      and clut_node.image.colorspace_settings.name == "Non-Color",
      f"{idx_node.image.colorspace_settings.name} / "
      f"{clut_node.image.colorspace_settings.name}")

la = me.attributes.get("diffuse")
check("light_attr", la is not None and la.data_type == "FLOAT_COLOR"
      and la.domain == "CORNER")


def _lv(d):
    try:
        return tuple(d.vector)
    except AttributeError:
        return tuple(d.color)


# --- the corner bake, computed a SECOND way ---------------------------------
# The addon dots the corner normal against the light direction in BLENDER
# space (both through the same proper rotation).  This recomputes the term
# from the document's RAW i16 triples with no transform at all — #427 measured
# the two identical on 273,128 of 273,128 corners, so a disagreement here is a
# real defect, not a rounding gap.  A mutation must be seeded on ONE path only
# to mean anything (the ab-revert trap).
def _expect_light(rig, poly, corner):
    # DIFFUSE only since the bake split — ambient is a graph constant, and
    # `node_ambient_is_state` grades it separately.
    if poly["kind"] not in mod.TEXTURED_KINDS:
        return (0.0, 0.0, 0.0)                  # untextured: normal is (0,0,0)
    n = [float(c) for c in poly["normals"][corner]]
    m = math.sqrt(sum(c * c for c in n))
    if m < 1e-9:
        return (0.0, 0.0, 0.0)
    n = [c / m for c in n]
    acc = [0.0, 0.0, 0.0]
    for i in range(3):
        d = [float(c) for c in rig["directions"][i]]
        dm = math.sqrt(sum(c * c for c in d)) or 1.0
        k = sum(n[j] * d[j] / dm for j in range(3))
        if k > 0.0:
            for c in range(3):
                acc[c] += rig["colors"][i][c] / 8.0 / 255.0 * k
    return tuple(acc)


def _check_bake(tag, obj, rig):
    m2 = obj.data
    at = m2.attributes.get("diffuse")
    if at is None or rig is None:
        check(f"light_baked_{tag}", False, "no attribute or no rig")
        return
    ok, detail = True, ""
    for i, poly in enumerate(polys):
        order = mod.import_order(len(poly["positions"]), mod._wound_against(poly))
        for slot, corner in enumerate(order):
            li = m2.polygons[i].loop_indices[slot]
            want = _expect_light(rig, poly, corner)
            got = _lv(at.data[li])[:3]
            if any(abs(g - w) > 1e-4 for g, w in zip(got, want)):
                ok, detail = False, f"face {i} slot {slot}: {got} != {want}"
                break
        if not ok:
            break
    check(f"light_baked_{tag}", ok, detail)


_geo_state = next(i for i, st in enumerate(states)
                  if st.get("resource") == doc["base"]["geometry_source"])
_check_bake("default_state", ob, states[_geo_state]["light_rig"])
check("light_not_flat",
      la is not None and not all(_lv(d)[:3] == (0.0, 0.0, 0.0) for d in la.data),
      "every corner baked diffuse 0")
check("light_source_prop",
      ob.get("exmateria_map/light_source") == states[_geo_state]["resource"],
      str(ob.get("exmateria_map/light_source")))

geo_state = next(i for i, st in enumerate(states)
                 if st.get("resource") == doc["base"]["geometry_source"])
check("default_preview_state", ob["exmateria_map/preview_state"] == geo_state,
      f"{ob['exmateria_map/preview_state']} != {geo_state}")

bpy.context.view_layer.objects.active = ob
res = bpy.ops.exmateria_map.set_preview_state(state_index=1)
check("state_rewire_op", res == {"FINISHED"}, f"res={res}")
check("state_rewire_prop", ob["exmateria_map/preview_state"] == 1)
check("state_rewire_clut", clut_node is not None
      and clut_node.image.name == f"exmateria_map/{name}_clut_1")
# state 1 is the TEXTURE row: it carries no rig of its own, so decision 7's
# borrow applies and the bake must still be the geometry source's rig.
check("state1_light_borrowed",
      ob.get("exmateria_map/light_source") == states[_geo_state]["resource"],
      str(ob.get("exmateria_map/light_source")))
_check_bake("borrowed", ob, states[_geo_state]["light_rig"])

# state 2 is a second MESH row with its OWN (darker) rig — the bake is part of
# the state, so selecting it must MOVE every corner.  Asserted as a real
# difference, not just "the operator returned FINISHED".
_before = [_lv(d)[:3] for d in me.attributes["diffuse"].data]
res = bpy.ops.exmateria_map.set_preview_state(state_index=2)
check("state2_rewire_op", res == {"FINISHED"}, f"res={res}")
check("state2_light_source",
      ob.get("exmateria_map/light_source") == states[2]["resource"],
      str(ob.get("exmateria_map/light_source")))
_check_bake("state2", ob, states[2]["light_rig"])
_after = [_lv(d)[:3] for d in me.attributes["diffuse"].data]
check("state2_light_moved",
      any(any(abs(a - b) > 1e-4 for a, b in zip(x, y))
          for x, y in zip(_before, _after)),
      "selecting a state with a different rig left every corner unchanged")

res = bpy.ops.exmateria_map.set_preview_state(state_index=0)
check("state_rewire_back",
      ob["exmateria_map/preview_state"] == 0
      and clut_node.image.name == f"exmateria_map/{name}_clut_0")
_check_bake("back_to_default", ob, states[_geo_state]["light_rig"])

# --- decision 25: the borrow source is a (night, weather) KEY MATCH ----------
# Decision 7 said a rig-less state borrows from "a same-arrangement sibling"
# and never said which; the code took the FIRST bearer in the document, which
# is a different rig for 76.8% of borrowing states corpus-wide and a different
# AMBIENT — a flat shift across the whole mesh — for 66.4%.
#
# `state_rig` is a pure function of the document, so the rule is fully
# determined by these synthetic lists; the operator arm below then proves the
# rig it picks actually reaches the graph.  Every case is paired with the OLD
# rule, and the seed block asserts the OLD rule really does fail them — a
# table of cases that both rules satisfy would grade nothing.
def _st(res, night, weather, amb=None):
    return {"resource": res, "night": night, "weather": weather,
            "light_rig": None if amb is None else
            {"colors": [[0, 0, 0]] * 3, "directions": [[0, 0, 4096]] * 3,
             "ambient": amb, "gradient": [0] * 6}}


def _old_rule(sts, i):
    """What `state_rig` did before decision 25: first bearer in the document."""
    if 0 <= i < len(sts) and sts[i].get("light_rig"):
        return sts[i]["light_rig"], sts[i].get("resource")
    for st in sts:
        if st.get("light_rig"):
            return st["light_rig"], st.get("resource")
    return None, None


_A = _st("A", 0, "none", [10, 10, 10])
_B = _st("B", 1, "none", [20, 20, 20])
_C = _st("C", 1, "none", [30, 30, 30])
_W = _st("W", 0, "heavy", [40, 40, 40])
_RULE_CASES = [
    # (name, states, index, expected source resource)
    ("own_rig_beats_everything",  [_A, _B],                            1, "B"),
    ("keyed_beats_first",         [_A, _st("t", 1, "none"), _B],       1, "B"),
    ("keyed_beats_nearest",       [_A, _B, _st("t", 0, "none")],       2, "A"),
    ("tie_prefers_the_later",     [_B, _st("t", 1, "none"), _C],       1, "C"),
    ("weather_is_in_the_key",     [_A, _st("t", 0, "heavy"), _W],      1, "W"),
    ("no_partner_falls_to_near",  [_A, _st("t", 1, "light")],          1, "A"),
    ("no_bearer_is_none",         [_st("t", 0, "none")],               0, None),
    ("index_out_of_range",        [_A],                                7, None),
]
_rule_bad = [n for n, sts, i, want in _RULE_CASES if mod.state_rig(sts, i)[1] != want]
check("borrow_rule", not _rule_bad, f"wrong source for {_rule_bad}")

# The seed: the OLD rule must FAIL this table, or the table is not grading the
# change.  Named individually so a case that silently stops discriminating is
# visible rather than absorbed into a count.
# Named, not counted.  A count let `index_out_of_range` — which discriminates
# the RANGE GUARD, not the borrow rule — stand in for a borrow case and keep
# the bar green while the cases that matter stopped discriminating.
_discriminating = {n for n, sts, i, want in _RULE_CASES
                   if _old_rule(sts, i)[1] != want}
_must_discriminate = {"keyed_beats_first", "tie_prefers_the_later",
                      "weather_is_in_the_key"}
check("borrow_rule_seeded", _must_discriminate <= _discriminating,
      f"the old rule already satisfies {sorted(_must_discriminate - _discriminating)}, "
      f"so those cases grade nothing")

# --- the same rule, through the real operator --------------------------------
# The shipped fixture cannot see this change: its state 1 is (night=0, w=3) and
# so is state 0, which is ALSO the first bearer, so both rules agree.  Flipping
# state 1's night makes its keyed partner state 2 (MAP001.47, a DIFFERENT
# ambient), which is exactly the corpus shape the fix is for.
_night = json.loads(ob["exmateria_map/map_states"])
check("fixture_undiscriminating",
      _night[1]["night"] == _night[0]["night"],
      "fixture state 1 already differs from state 0 — the arm below is moot")
_night[1]["night"] = 1 - int(bool(_night[0]["night"]))
ob["exmateria_map/map_states"] = json.dumps(_night)
res = bpy.ops.exmateria_map.set_preview_state(state_index=1)
check("borrow_op_ran", res == {"FINISHED"}, f"res={res}")
check("borrow_op_source",
      ob.get("exmateria_map/light_source") == states[2]["resource"],
      f"{ob.get('exmateria_map/light_source')} != {states[2]['resource']}")
_amb_want = mod.rig_ambient(states[2]["light_rig"])
_amb_got = tuple(amb_node.outputs[0].default_value)[:3]
check("borrow_op_ambient",
      all(abs(a - b) < 1e-6 for a, b in zip(_amb_got, _amb_want)),
      f"{_amb_got} != {_amb_want}")
_check_bake("borrow_keyed", ob, states[2]["light_rig"])
# The seed: the OLD source must render a DIFFERENT ambient, or this arm would
# pass on the unfixed code.  states[0] is the first bearer.
_amb_old = mod.rig_ambient(states[0]["light_rig"])
check("borrow_op_seeded",
      any(abs(a - b) > 1e-6 for a, b in zip(_amb_old, _amb_want)),
      f"old and new source share ambient {_amb_old} — the arm cannot report it")
ob["exmateria_map/map_states"] = json.dumps(states)
bpy.ops.exmateria_map.set_preview_state(state_index=0)

check("grey_shared",
      me.materials[0] is bpy.data.materials.get(mod.UNLIT_GREY))

# re-import: deletes and rebuilds, no duplicates
n_before = len([o for o in bpy.data.objects if o.name.startswith(name) or o.name.startswith("tile_")])
res = run_import(staged)
check("reimport_finished", res == {"FINISHED"}, f"res={res}")
n_after = len([o for o in bpy.data.objects if o.name.startswith(name) or o.name.startswith("tile_")])
check("reimport_no_duplicates", n_before == n_after, f"{n_before} -> {n_after}")
ob2 = bpy.data.objects.get(name)
check("reimport_object_rebuilt", ob2 is not None and len(ob2.data.polygons) == len(polys))

# S2: the Properties panel must survive a real draw pass.  Headless runs never
# lay out UI, so an invalid icon enum (or any draw-time error) only shows up
# here, against a recorder layout that mirrors bpy.types.UILayout's surface.
class _FakeLayout:
    enabled = True

    def __init__(self, sink):
        self._sink = sink

    def box(self, **kw):
        return self

    def label(self, text="", icon=None, **kw):
        self._sink.append(icon)
        _labels.append(text)
        return self

    def column(self, align=False, **kw):
        return self

    def separator(self, **kw):
        return self

    def grid_flow(self, **kw):
        return self

    def template_palette(self, data, prop, color=False, **kw):
        _labels.append(f"<palette {getattr(getattr(data, prop, None), 'name', None)}>")
        return self

    def operator(self, bl_idname, text="", icon=None, **kw):
        class _Op:
            pass
        self._sink.append(icon)
        _ops.append(bl_idname)
        return _Op()

    def operator_menu_enum(self, bl_idname, prop, text="", icon=None, **kw):
        _menus.append(bl_idname)
        return self

    def menu(self, menu_id, text="", icon=None, **kw):
        self._sink.append(icon)
        _menus.append(menu_id)
        _labels.append(text)
        return self

    def row(self, align=False, **kw):
        return self

    def prop(self, data, prop_name, **kw):
        _props.append(prop_name)
        return self

def _panel_shim(cls, layout):
    """The `self` Blender hands to `draw` -- a Panel INSTANCE, so it carries
    every class attribute the panel declared.

    A shim holding only `layout` is a WEAKER `self` than the real one, and a
    panel registered in two editors reads its own class attributes to know
    which copy it is drawing.  Copied wholesale rather than by name, so the
    harness does not have to be edited every time a panel gains one.
    """
    attrs = {}
    for k in dir(cls):
        if k.startswith("__"):
            continue
        try:
            attrs[k] = getattr(cls, k)
        except Exception:
            pass
    attrs["layout"] = layout
    return type("_S", (), attrs)()


_icons = []
_props = []
_ops = []
_menus = []
_labels = []
_fl = _FakeLayout(_icons)
try:
    class _Self:
        layout = _fl
    _ctx_pv = type("_Ctx", (), {"object": ob2, "scene": bpy.context.scene})()
    mod.MAP_PT_preview.draw(_Self(), _ctx_pv)
    # ...and the LIGHT panel, onto the same tape. The rig, its provenance line
    # and the light-debug pair moved out of Preview and into Lighting Bake
    # (2026-08-27, *"the light stuff in there should just go in the light
    # panel"*), and the arms below are about the rig's SHAPE -- that it is
    # drawn without a gesture, that the provenance line is said once, that the
    # three lights are side by side. Those claims are about the TAB, not about
    # which of its panels holds them, so both panels feed one tape and the
    # arms are unchanged. WHICH panel now owns the rig is asserted separately,
    # further down, in both directions -- a move that leaves a copy behind
    # would satisfy this tape and fail that one.
    from exmateria_map import lighting_bake as _lb_early
    _lb_early.MAP_PT_lighting_bake.draw(_Self(), _ctx_pv)
    check("panel_draw", True)
except Exception as e:
    check("panel_draw", False, repr(e))
# Against Blender's OWN icon enum, not a hand-kept allow-list: a list that has
# to be edited whenever the panel gains an icon is a guard on the list, not on
# the panel.
_valid = set(bpy.types.UILayout.bl_rna.functions["label"]
             .parameters["icon"].enum_items.keys())
# the debug mode + boost are VIEW state on the Object, so the panel must reach
# them through `prop` on the registered properties, not through the
# `exmateria_map/...` custom properties that carry the document.
# The rig props come FIRST and unconditionally -- nothing authorable is
# hidden, so a plain import with no gesture already draws the 21 controls.
# The debug mode and boost are VIEW state on the Object, so the panel must
# still reach those two through `prop` on the registered properties rather
# than through the `exmateria_map/...` custom properties that carry the
# document; that is what the tail of this list is asserting.
check("panel_light_debug_props",
      _props[-2:] == ["exmateria_map_light_debug", "exmateria_map_light_boost"],
      str(_props))
# `gradient` is NOT in this set: decision 6 collapses it to one line, because
# it was a third of the box being un-editable, which reads as broken rather
# than as deliberate.  The six values stay in the Override -- the rig is still
# the whole 45 bytes -- and that half is asserted separately, on the Override.
check("panel_draws_the_rig_without_a_gesture",
      set(_props) >= {"ambient", "gain_1", "gain_2", "gain_3",
                      "dir_1", "dir_2", "dir_3"},
      f"a plain import does not draw the rig: {_props}")
# The LIGHT PROVENANCE line, which nothing asserted before -- the panel's icons
# and prop names were graded, its words never were.  Exposing the rig on every
# state made that gap load-bearing: the line keyed on an Override EXISTING, so
# it now fires on every state and tells the artist their untouched map is not
# the ROM's.  That is the exact lie decision 25's provenance line exists to
# prevent, and it also makes the four honest branches (albedo / AUTHORED /
# 45-byte rig / BORROWED) unreachable.
_light_lines = [t for t in _labels if isinstance(t, str) and t.startswith("light:")]
check("panel_says_the_light_provenance_once",
      len(_light_lines) == 1, str(_light_lines))
check("panel_clean_rig_is_not_called_edited",
      bool(_light_lines) and "EDITED" not in _light_lines[0],
      f"a freshly imported, untouched state reports {_light_lines[:1]} -- "
      f"exposure is not authorship")
check("panel_clean_rig_names_where_it_came_from",
      bool(_light_lines) and any(w in _light_lines[0] for w in
                                 ("rig from", "BORROWED", "albedo", "AUTHORED")),
      f"the provenance line says nothing about the source: {_light_lines[:1]}")
check("panel_icons_valid",
      bool(_valid) and all(i in _valid for i in _icons if i is not None),
      str([i for i in _icons if i is not None and i not in _valid]))

# --- the panel's SHAPE (reported from use: "the menus are a mess") -----------
# The preview panel drew one operator button PER MAP STATE -- ten of them on
# 32.17% of geometry-bearing arrangements -- and then carried the export leg and
# the PCSX push underneath. Three unrelated jobs in one column, most of it a
# list. The states are a CHOICE OF ONE, which is a menu; the push is its own
# job, which is its own panel.
check("preview_panel_uses_a_state_menu",
      len(_menus) == 1, f"menus drawn: {_menus}")
check("preview_panel_does_not_list_every_state",
      _ops.count("exmateria_map.set_preview_state") == 0,
      f"{_ops.count('exmateria_map.set_preview_state')} per-state buttons still "
      f"drawn in the preview panel")
check("preview_panel_does_not_carry_the_push",
      "map.live_push" not in _ops, str(_ops))
check("preview_panel_does_not_carry_the_export",
      "export_map.document" not in _ops, str(_ops))

# The state menu itself must offer every state, and mark the one in view.
_menu_ops = []
_menu_labels = []


class _MenuLayout(_FakeLayout):
    def operator(self, bl_idname, text="", icon=None, **kw):
        class _Op:
            pass
        _menu_ops.append((bl_idname, text, icon))
        return _Op()


try:
    _mcls = getattr(bpy.types, "MAP_MT_preview_state")
    _mcls.draw(type("_S", (), {"layout": _MenuLayout([])})(),
               type("_Ctx", (), {"object": ob2,
                                 "scene": bpy.context.scene})())
    check("state_menu_registered", True)
except Exception as e:
    check("state_menu_registered", False, repr(e))
_panel_states = json.loads(ob2["exmateria_map/map_states"])
check("state_menu_offers_every_state",
      len(_menu_ops) == len(_panel_states) and len(_panel_states) > 1,
      f"{len(_menu_ops)} entries for {len(_panel_states)} state(s)")
check("state_menu_marks_the_one_in_view",
      sum(1 for _i, _t, _ic in _menu_ops if _ic == "CHECKMARK") == 1,
      str([(t, ic) for _i, t, ic in _menu_ops]))

# --- the PCSX push is its own panel ------------------------------------------
_push_ops, _push_labels, _push_props = [], [], []


class _PushLayout(_FakeLayout):
    def operator(self, bl_idname, text="", icon=None, **kw):
        class _Op:
            pass
        _push_ops.append(bl_idname)
        self._sink.append(icon)
        return _Op()

    def label(self, text="", icon=None, **kw):
        self._sink.append(icon)
        _push_labels.append(text)
        return self

    def prop(self, data, prop_name, **kw):
        _push_props.append(prop_name)
        return self


_push_icons = []
try:
    _pcls = getattr(bpy.types, "MAP_PT_live_push")
    _pcls.draw(type("_S", (), {"layout": _PushLayout(_push_icons)})(),
               type("_Ctx", (), {"object": ob2, "scene": bpy.context.scene})())
    check("push_panel_registered", True)
except Exception as e:
    check("push_panel_registered", False, repr(e))
check("push_panel_draws_the_button", "map.live_push" in _push_ops, str(_push_ops))
check("push_panel_icons_valid",
      all(i in _valid for i in _push_icons if i is not None),
      str([i for i in _push_icons if i is not None and i not in _valid]))
# The question that sent the sub-panel here: "I change map preview and hit push
# and nothing happens -- shouldn't it update the texture?" It cannot, twice
# over, and neither reason was on screen. The answer was a DEFAULT_CLOSED
# `What a push carries` child panel; the artist has since deleted it -- *"I
# don't care about the 'what a push carries' section. delete it. that belongs
# in a console or something ... you are putting console stuff in the ui area."*
#
# The LIMIT is not deleted with the panel, and that is what these two arms
# grade. The panel is controls only, and the statement it used to hold is on
# every push instead of on screen forever.
check("the_push_panel_is_controls_only",
      not _push_labels,
      f"the push panel drew {len(_push_labels)} label row(s) — it holds things "
      f"you PRESS; what a run had to say goes to the console and the Log: "
      f"{_push_labels!r}")
# Where it went. `unpushed_lines` is still called by the operator, so the
# limit is stated once per push, next to the push it describes -- which the
# static panel could never do, since it restated a table regardless of what
# any given push actually covered.
from exmateria_map import live_link_ui as _ui
_unp = _ui.unpushed_lines(set())
check("the_unpushed_limit_still_has_lines_to_say",
      bool(_unp) and all("not pushed:" in ln for ln in _unp), str(_unp[:3]))
# The two source-level halves of this -- that the operator still CALLS
# `unpushed_lines`, and that `finish` PRINTS what it stores -- need
# `_tree_func`, which is defined further down; they run in the AST section.

# Aiming a lamp means SELECTING it, which makes it the active object. A panel
# polling on `context.object` therefore disappears at exactly the moment the
# artist reached for it -- the defect `target_map`'s docstring already records
# for the bake panel. Both new panels ask the SCENE, as export's `find_marker`
# does, so both survive it.
_prev_active = bpy.context.view_layer.objects.active
_probe_lamp = bpy.data.objects.new("panel_poll_lamp",
                                   bpy.data.lights.new("ppl", "POINT"))
bpy.context.scene.collection.objects.link(_probe_lamp)
bpy.context.view_layer.objects.active = _probe_lamp
# Export's copy of this arm went with the panel; Push's is the one that
# matters, and it matters more now that Push is FIRST in the tab -- the top
# panel disappearing when a lamp is selected is the most visible form of the
# defect this arm exists for.
check("push_panel_survives_selecting_a_lamp",
      bool(bpy.types.MAP_PT_live_push.poll(bpy.context)))
# ...and it still DRAWS, or surviving the poll buys nothing.
for _tag, _cls in (("push", bpy.types.MAP_PT_live_push),):
    try:
        _cls.draw(type("_S", (), {"layout": _FakeLayout([])})(), bpy.context)
        check(f"{_tag}_panel_draws_with_a_lamp_active", True)
    except Exception as e:
        check(f"{_tag}_panel_draws_with_a_lamp_active", False, repr(e))
bpy.data.objects.remove(_probe_lamp, do_unlink=True)
bpy.context.view_layer.objects.active = _prev_active

# --- decision 25: the rig Override ------------------------------------------
# Resolution order is override -> own -> keyed partner, and NOTHING about an
# edit may reach the document.  That last one is the whole reason the Override
# is a separate store, so it is asserted on the bytes of the property that
# carries the document, not inferred from the code shape.
_doc_before = ob2["exmateria_map/map_states"]
_ov_states = json.loads(_doc_before)
_i0 = int(ob2["exmateria_map/preview_state"])
_src_rig, _src_name = mod.state_rig(_ov_states, _i0)
# Nothing AUTHORABLE is hidden.  The rig is exposed on every state from the
# moment of import, with no gesture in between -- so EXPOSURE can no longer be
# the declaration, and a second signal has to carry that.  That signal is
# DIRTY: an Override whose editing-unit values still equal what it was seeded
# with declares nothing, warns about nothing, and lights nothing differently.
#
# Compared in the FLOATS, never in the packed bytes.  `override_rig` re-emits a
# direction at exactly 4096 while the disc's magnitudes run 4094.4-4096.7, so a
# byte comparison calls every untouched rig dirty and lands straight back on a
# rig that is exposed and therefore declared -- which is the bug this replaces.
_dirty = getattr(mod, "rig_is_dirty", None)
check("rig_dirty_predicate_exists", callable(_dirty),
      "import_document exposes no `rig_is_dirty`; exposure cannot stop meaning "
      "declaration without it")
check("rig_exposed_on_every_state",
      sorted(o.state_index for o in ob2.exmateria_map_rig_overrides)
      == list(range(len(_ov_states))),
      f"exposed on {sorted(o.state_index for o in ob2.exmateria_map_rig_overrides)}, "
      f"expected every state of {len(_ov_states)}")
check("rig_exposure_is_clean",
      callable(_dirty)
      and not any(_dirty(o) for o in ob2.exmateria_map_rig_overrides),
      "a freshly imported document already reads as edited")
# Name the trap, so `rig_exposure_is_clean` cannot pass for the wrong reason.
# If this fixture's rig round-trips byte-exactly then the check above is green
# whichever way dirty is measured, and this harness stops covering the choice.
_seed_rig, _seed_src = mod.exposure_rig(ob2, _ov_states, _i0)
_packed_now = mod.override_rig(mod.find_override(ob2, _i0))
check("packed_bytes_would_call_this_rig_dirty",
      _packed_now["directions"] != [list(d) for d in _seed_rig["directions"]],
      f"this fixture's directions survive the unit vector byte-exactly "
      f"({_packed_now['directions']}), so the editing-units comparison is not "
      f"under test here -- blender_corpus.py's 148 maps still cover it")

bpy.context.view_layer.objects.active = ob2
_ov = mod.find_override(ob2, _i0)
check("rig_exposed_without_a_gesture", _ov is not None,
      "the previewed state has no Override after a plain import")

# Exposure must NOT move the picture: it makes the ROM's rig editable, it does
# not replace it.  Ambient and the gains are an integer scaled by a constant and
# back, so those are byte-exact; a direction is re-emitted at exactly 4096 while
# the disc runs 4094.4-4096.7, so its bytes may move by a couple of LSB and the
# bar there is the PICTURE (measured 0.0099 deg worst case, a 0.000/255 delta).
_rt = mod.override_rig(_ov)
check("override_ambient_exact", _rt["ambient"] == list(_src_rig["ambient"]),
      f"{_rt['ambient']} != {_src_rig['ambient']}")
check("override_gains_exact",
      _rt["colors"] == [list(c) for c in _src_rig["colors"]],
      f"{_rt['colors']} != {_src_rig['colors']}")
check("override_gradient_carried",
      _rt["gradient"] == list(_src_rig["gradient"]),
      f"{_rt['gradient']} != {_src_rig['gradient']}")
_ang = []
for _k in range(3):
    _a = mod._unit(tuple(float(c) for c in _src_rig["directions"][_k]))
    _b = mod._unit(tuple(float(c) for c in _rt["directions"][_k]))
    _ang.append(sum(x * y for x, y in zip(_a, _b)))
check("override_dirs_picture_exact", all(d > 1 - 1e-6 for d in _ang),
      f"dot products {_ang}")
_check_bake("override_exposed_same_picture", ob2, _src_rig)

# The document is untouched — the Override is stored apart, by construction.
check("override_document_untouched",
      ob2["exmateria_map/map_states"] == _doc_before,
      "exposing the rig rewrote the document")

# An edit must reach the bake AND the graph constant, through the same one path
# a state switch uses.
_amb_node = ob2.data.materials[1].node_tree.nodes.get("exmateria_map.ambient")
_before_bake = [_lv(d)[:3] for d in ob2.data.attributes["diffuse"].data]
_ov.ambient = (0.125, 0.25, 0.5)
_ov.gain_1 = (2.0, 0.5, 0.25)
# The graph gets the ROM-QUANTIZED value, not the raw slider float: an
# Override is ROM-shaped, so `override_rig` rounds ambient to u8 on the way to
# the bake and 0.125 renders as round(0.125*255)/255 = 32/255.  Asserted rather
# than tolerated — the artist is shown exactly what the 45 bytes can hold, and a
# preview that rendered a precision the format cannot store would be the FEDS
# picker's defect inverted.
_amb_q = tuple(round(c * 255.0) / 255.0 for c in (0.125, 0.25, 0.5))
check("override_edit_ambient_reached_graph",
      _amb_node is not None
      and all(abs(a - b) < 1e-6 for a, b in
              zip(tuple(_amb_node.outputs[0].default_value)[:3], _amb_q)),
      str(tuple(_amb_node.outputs[0].default_value)[:3]) if _amb_node else "none")
check("override_ambient_quantizes_to_u8",
      mod.override_rig(_ov)["ambient"] == [32, 64, 128],
      str(mod.override_rig(_ov)["ambient"]))
_after_bake = [_lv(d)[:3] for d in ob2.data.attributes["diffuse"].data]
check("override_edit_moved_the_bake",
      any(any(abs(a - b) > 1e-6 for a, b in zip(x, y))
          for x, y in zip(_before_bake, _after_bake)),
      "editing gain_1 left every corner unchanged")
check("override_edit_document_untouched",
      ob2["exmateria_map/map_states"] == _doc_before,
      "editing an Override rewrote the document")
_check_bake("override_edited", ob2, mod.override_rig(_ov))

# An Override beats the state's OWN rig — that is what "resolved ahead of the
# document" means, and the seed is the source rig it displaced.
_r, _lbl, _edited = mod.resolved_rig(ob2, _ov_states, _i0)
check("override_wins_resolution",
      _edited and _r == mod.override_rig(_ov) and _r != _src_rig,
      f"edited={_edited} label={_lbl}")

# The badge is the screenshot-visible half of the signal, and it must be
# ABSENT until an Override exists — an unedited preview has to stay pixel
# identical to what it was before this feature.
check("badge_reports_edited",
      ob2 in mod.edited_objects(type("_C", (), {"visible_objects": [ob2]})()))

# The panel's editable path: with an Override live, the rig props must be
# reachable through `prop` on the PropertyGroup.
_props.clear()
_icons.clear()
try:
    _ctx_ed = type("_Ctx", (), {"object": ob2, "scene": bpy.context.scene})()
    mod.MAP_PT_preview.draw(_Self(), _ctx_ed)
    # Both panels onto one tape again -- the rig lives in Lighting Bake now.
    _lb_early.MAP_PT_lighting_bake.draw(_Self(), _ctx_ed)
    check("panel_draw_edited", True)
except Exception as e:
    check("panel_draw_edited", False, repr(e))
check("panel_rig_props",
      set(_props) >= {"ambient", "gain_1", "gain_2", "gain_3",
                      "dir_1", "dir_2", "dir_3"},
      str(_props))
check("panel_icons_valid_edited",
      all(i in _valid for i in _icons if i is not None),
      str([i for i in _icons if i is not None and i not in _valid]))

# Revert returns the ROM's picture exactly.
_res = bpy.ops.exmateria_map.clear_rig_override(all_states=True)
check("override_cleared_ran", _res == {"FINISHED"}, f"res={_res}")
# Reset RE-SEEDS; it does not remove.  Removing would take the sliders off
# screen, which is the one thing exposure exists to stop.
check("override_reset_stays_exposed",
      len(ob2.exmateria_map_rig_overrides) == len(_ov_states)
      and mod.find_override(ob2, _i0) is not None,
      f"reset left {len(ob2.exmateria_map_rig_overrides)} of "
      f"{len(_ov_states)} states exposed")
check("override_reset_is_clean",
      not mod.dirty_overrides(ob2),
      "reset left the state reading as edited, so `build` would still write "
      "its 45 bytes")
_check_bake("override_reverted", ob2, _src_rig)
check("badge_silent_when_clean",
      not mod.edited_objects(type("_C", (), {"visible_objects": [ob2]})()))

# ================================================================= export ===
# The #557 export leg.  Every check below ships with the defect it catches:
# the clean arm asserts the export is silent, then ONE scene value is seeded
# and the same call must speak.  Seeds go on the SCENE, never on the export
# code -- a mutation to shared code moves both sides together and passes on
# unfixed code.
import bmesh
import shutil

from exmateria_map import export_document as exp
from exmateria_map import png_indexed as pngmod

clear_scene()
_res = run_import(staged)
check("export_scene_reimported", _res == {"FINISHED"}, f"res={_res}")
obx = bpy.data.objects.get(name)
EXPORT_DIR = os.path.join(os.path.dirname(JSON), "export_out")
os.makedirs(EXPORT_DIR, exist_ok=True)
for _f in os.listdir(EXPORT_DIR):
    os.unlink(os.path.join(EXPORT_DIR, _f))


def docdiff(a, b, path="doc"):
    """Every field where two parsed documents disagree."""
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in set(a) | set(b):
            if k not in a or k not in b:
                out.append(f"{path}.{k}: only on one side")
            else:
                out += docdiff(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: len {len(a)} != {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            out += docdiff(x, y, f"{path}[{i}]")
    elif a != b:
        out.append(f"{path}: {a!r} != {b!r}")
    return out


def refusals_mentioning(rep, *words):
    return [r for r in rep.refusals if all(w in r for w in words)]


# ---- the acceptance identity: export(import(doc)) == doc -------------------
_doc, _files, _rep = exp.assemble(obx)
_d = docdiff(doc, _doc)
check("export_identity", not _d, "; ".join(_d[:6]))
check("export_clean_no_refusals", not _rep.refusals, str(_rep.refusals[:4]))
check("export_clean_no_divergence",
      not _rep.divergence and _rep.new_faces == 0,
      f"{dict(_rep.divergence)} new={_rep.new_faces}")
# ADR-0187 decision 22: an untouched document declares NO terrain, and since
# decision 3 it shows the whole grid anyway.  The tile-object export leg is not
# vacuous for that -- it is exercised where a declaration is LEGAL, by
# `drift_fix_reaches_the_document` (a drift-named tile) and
# `growth_declared_field_reaches_the_document` (a growth-created one).  Those
# are the only two classes `build` accepts a record for, so testing the leg on
# a carried tile only ever tested a document `build` rejects.
check("export_terrain_from_tile_objects",
      _doc["terrain"] is None and doc["terrain"] is None, str(_doc["terrain"]))
check("export_grid_from_grid_object",
      _doc["base"]["terrain_grid"] == {"size_x": 10, "size_z": 13},
      str(_doc["base"]["terrain_grid"]))
check("export_carry_verbatim", _doc["carry"] == doc["carry"])
# The live bug this leg fixes: an Override that existed and was NEVER moved
# 2 bytes of MAP011.8 through `build`, because the Override's existence alone
# promoted it to `authored_light_rig` and the direction was re-emitted at 4096.
# Now every state is exposed, so that would fire on all 1,371 of them.
check("export_exposure_declares_nothing",
      not any(mod.AUTHORED_RIG in st for st in _doc["map_states"]),
      f"exposure alone declared an authored rig on states "
      f"{[i for i, st in enumerate(_doc['map_states']) if mod.AUTHORED_RIG in st]}")
check("export_exposure_does_not_bump_version",
      _doc["version"] == mod.VERSION,
      f"version={_doc['version']}, expected {mod.VERSION} -- an untouched "
      f"document must stay readable by the oldest `build` that can take it")

# The identity check's own seed: move ONE vertex and it must speak, naming the
# field.  Without this arm, `export_identity` passing proves only that
# docdiff() returns an empty list.
_v0 = tuple(obx.data.vertices[0].co)
obx.data.vertices[0].co = (_v0[0] + 8.0, _v0[1], _v0[2])
_seed_doc, _, _seed_rep = exp.assemble(obx)
_sd = docdiff(doc, _seed_doc)
check("export_identity_seed_speaks",
      any("positions" in x for x in _sd),
      f"moving a vertex left the identity check silent: {_sd[:4]}")
check("export_divergence_seed_speaks",
      _seed_rep.divergence.get("positions") == 1,
      f"divergence did not see the moved corner: {dict(_seed_rep.divergence)}")
obx.data.vertices[0].co = _v0
check("export_identity_restored", not docdiff(doc, exp.assemble(obx)[0]))

# ---- §6.4 the CLUT image is the palette EDIT surface -----------------------
# Import builds a 16x16 image per state (`_clut_image`: pixel (col, row) = CLUT
# `row`'s entry `col`), the preview samples it, and `paint.clut_entries` gates
# against it -- so it is already what the artist sees and paints under.  What it
# was not, until this leg, is what the DOCUMENT is written from: `_assemble`
# copied `map_states` through and never re-emitted `palettes`, so a recoloured
# entry previewed correctly and exported the imported colour.
_pal_names = json.loads(obx["exmateria_map/state_cluts"])
_pal_img = bpy.data.images[_pal_names[0]]
_pal_orig = list(_pal_img.pixels)
_PAL_ROW, _PAL_COL = 3, 5


def pal_hex(d, state, row, col):
    return d["map_states"][state]["palettes"][row]["colors"][col].upper()


def pal_set(img, row, col, hexcolor):
    """Paint one CLUT entry, the way the artist's colour picker does."""
    buf = list(img.pixels)
    j = (row * 16 + col) * 4
    for k in range(3):
        buf[j + k] = int(hexcolor[1 + 2 * k:3 + 2 * k], 16) / 255.0
    img.pixels[:] = buf


_pal_was = pal_hex(doc, 0, _PAL_ROW, _PAL_COL)
_PAL_NEW = "#FF00FF" if _pal_was != "#FF00FF" else "#00FF00"
check("export_palette_seed_is_not_inert", _pal_was != _PAL_NEW,
      f"CLUT entry ({_PAL_ROW},{_PAL_COL}) already holds {_PAL_NEW}; "
      f"recolouring it to that could not change anything")
pal_set(_pal_img, _PAL_ROW, _PAL_COL, _PAL_NEW)
_pal_doc = exp.assemble(obx)[0]
check("export_palette_reemitted_from_clut_image",
      pal_hex(_pal_doc, 0, _PAL_ROW, _PAL_COL) == _PAL_NEW,
      f"recolouring CLUT entry ({_PAL_ROW},{_PAL_COL}) to {_PAL_NEW} left the "
      f"document at {pal_hex(_pal_doc, 0, _PAL_ROW, _PAL_COL)}")
# EXACTLY one line, not "no line that isn't it": an export that re-emits
# nothing has an empty diff and would satisfy the weaker form vacuously.
_pal_diff = docdiff(doc, _pal_doc)
check("export_palette_edit_touches_one_entry",
      len(_pal_diff) == 1 and _pal_diff[0].startswith(
          f"doc.map_states[0].palettes[{_PAL_ROW}].colors[{_PAL_COL}]"),
      f"one recoloured entry should move itself and nothing else, but the "
      f"document moved at: {_pal_diff[:6]}")

# The control, and the bar this whole leg has to clear: reading the palettes
# back OUT of a float image must reproduce what `dump` wrote, byte for byte,
# or every untouched CLUT in the corpus reads as an edit.
_pal_img.pixels[:] = _pal_orig
check("export_palette_untouched_is_byte_exact",
      not docdiff(doc, exp.assemble(obx)[0]),
      "an untouched CLUT did not survive the image round trip")

# A `palettes: null` state's CLUT image is FABRICATED -- import fills it from
# the sidecar's display-only PLTE so the state still previews (§4).  Those
# pixels are not the state's data, so writing them back would invent a `0x44`
# chunk for a resource that has none.  The fixture's state 1 (MAP001.8) is
# exactly that row, so this arm is not hypothetical.
_PAL_NULL = next(i for i, st in enumerate(doc["map_states"])
                 if st.get("palettes") is None)
check("export_palette_null_state_exists_in_fixture", _PAL_NULL == 1,
      f"the fixture no longer has a `palettes: null` state at 1: {_PAL_NULL}")
_pal_null_img = bpy.data.images[_pal_names[_PAL_NULL]]
_pal_null_orig = list(_pal_null_img.pixels)
pal_set(_pal_null_img, 0, 0, "#FF00FF")
_pal_null_doc = exp.assemble(obx)[0]
check("export_palette_null_state_stays_null",
      _pal_null_doc["map_states"][_PAL_NULL]["palettes"] is None,
      f"recolouring a fabricated CLUT invented palettes for a state that has "
      f"none: {str(_pal_null_doc['map_states'][_PAL_NULL]['palettes'])[:120]}")
_pal_null_img.pixels[:] = _pal_null_orig

# `stp` is per-CLUT live data (1,178 bits set across 651 palette-carrying
# resources) and the CLUT image has nowhere to put it -- an entry's colour and
# its STP bit are independent.  Seed a mask AND recolour that same row, so the
# row is definitely rewritten and the bit has to ride through the writer.
_PAL_STP_ROW, _PAL_STP = 2, 0xBEEF
_pal_ms = json.loads(obx["exmateria_map/map_states"])
_pal_ms_orig = obx["exmateria_map/map_states"]
check("export_palette_stp_seed_is_not_inert",
      _pal_ms[0]["palettes"][_PAL_STP_ROW]["stp"] != _PAL_STP,
      "the fixture already carries the seeded STP mask")
_pal_ms[0]["palettes"][_PAL_STP_ROW]["stp"] = _PAL_STP
obx["exmateria_map/map_states"] = json.dumps(_pal_ms)
pal_set(_pal_img, _PAL_STP_ROW, 0, _PAL_NEW)
_pal_stp_doc = exp.assemble(obx)[0]
check("export_palette_stp_rides_through_a_recolour",
      _pal_stp_doc["map_states"][0]["palettes"][_PAL_STP_ROW]["stp"] == _PAL_STP,
      f"recolouring an entry dropped its CLUT's STP mask: "
      f"{_pal_stp_doc['map_states'][0]['palettes'][_PAL_STP_ROW].get('stp')!r} "
      f"!= {_PAL_STP}")
check("export_palette_stp_row_was_actually_rewritten",
      pal_hex(_pal_stp_doc, 0, _PAL_STP_ROW, 0) == _PAL_NEW,
      "the STP arm never exercised the writer: the row's colour did not move")
obx["exmateria_map/map_states"] = _pal_ms_orig
_pal_img.pixels[:] = _pal_orig
check("export_palette_arms_restored", not docdiff(doc, exp.assemble(obx)[0]),
      "the palette arms did not restore the scene")

# ---- §5.2 the out-of-grid WARNING -----------------------------------------
# MAP001.a0's live polygon carries {255, 127, 0}.  It is NOT a warning: no
# legal grid reaches x=255 (decision 10 caps an axis at 18), so the value names
# no tile at all.  Measured, that distinction IS the warning: of the 40,745
# bindings a plain extent test flags corpus-wide, 40,542 name an unreachable
# tile and 203 do not.  Before this, the first real GUI export printed "234
# terrain binding(s) outside the 10x15 grid" with all 234 reading (255,127,L0).
check("export_unreachable_binding_does_not_warn",
      not _rep.warnings,
      f"{{255,127,0}} warned as out-of-grid: {_rep.warnings}")
check("export_out_of_grid_never_refuses",
      not refusals_mentioning(_rep, "grid"), str(_rep.refusals))
_tx = obx.data.attributes["terrain_x"].data[0].value
_tz = obx.data.attributes["terrain_z"].data[0].value
# The other arm, or the check above is satisfied by an export that never warns:
# a binding a legal grid COULD hold, outside THIS 10x13 one, must speak.
obx.data.attributes["terrain_x"].data[0].value = 12
obx.data.attributes["terrain_z"].data[0].value = 2
_w = exp.assemble(obx)[2]
check("export_warns_on_a_reachable_tile_outside_the_grid",
      any("outside the 10x13 grid" in x and "(12, 2, L0)" in x
          for x in _w.warnings),
      f"a binding at (12, 2) — inside decision 10's ceiling, outside this "
      f"grid — did not warn: {_w.warnings}")
check("export_reachable_warning_never_refuses", not _w.refusals,
      str(_w.refusals[:3]))
check("export_boundary_is_the_grid_not_the_ceiling",
      not any("(9, 2" in x for x in _w.warnings), str(_w.warnings))
obx.data.attributes["terrain_x"].data[0].value = 1
obx.data.attributes["terrain_z"].data[0].value = 1
check("export_no_warning_when_in_grid",
      not exp.assemble(obx)[2].warnings,
      "an in-grid binding still warned")
obx.data.attributes["terrain_x"].data[0].value = _tx
obx.data.attributes["terrain_z"].data[0].value = _tz
check("export_warning_arms_restored", not docdiff(doc, exp.assemble(obx)[0]))

# ---- §5.1.2 the range refusals, one seeded arm each -----------------------
_seeds = [
    ("position_i16", lambda: setattr(obx.data.vertices[0], "co",
                                     (40000.0, _v0[1], _v0[2])),
     lambda: setattr(obx.data.vertices[0], "co", _v0), ("positions", "-32768")),
    ("palette_id", lambda: _setattrv("palette_id", 99),
     lambda: _setattrv("palette_id", 0), ("palette_id", "0..15")),
    ("texture_page", lambda: _setattrv("texture_page", 7),
     lambda: _setattrv("texture_page", 1), ("texture_page", "0..3")),
    ("unknown_texture_value_6a", lambda: _setattrv("unknown_texture_value_6a", 9),
     lambda: _setattrv("unknown_texture_value_6a", 3),
     ("unknown_texture_value_6a", "0..3")),
    ("visible_angles", lambda: _setattrv("visible_angles", 70000),
     lambda: _setattrv("visible_angles", 32768), ("visible_angles", "0..65535")),
    ("terrain_z", lambda: _setattrv("terrain_z", 200),
     lambda: _setattrv("terrain_z", _tz), ("terrain.z", "0..127")),
]


def _setattrv(attr, value, face=0):
    obx.data.attributes[attr].data[face].value = value


for _label, _break, _fix, _words in _seeds:
    _break()
    _r = exp.assemble(obx)[2]
    check(f"export_refuses_{_label}",
          bool(refusals_mentioning(_r, *_words)),
          f"no refusal naming {_words}; got {_r.refusals[:3]}")
    _fix()
    check(f"export_refusal_clears_{_label}",
          not exp.assemble(obx)[2].refusals,
          "the refusal survived the repair")

# A UV dragged out of its own `texture_page` band: the band is 256 rows tall,
# so leaving it puts the document's `v` outside u8.  This is the refusal that
# catches an edit no attribute range can see.
_uv0 = tuple(obx.data.uv_layers["UVMap"].data[0].uv)
obx.data.uv_layers["UVMap"].data[0].uv = (_uv0[0], _uv0[1] - 0.25)
_r = exp.assemble(obx)[2]
check("export_refuses_uv_out_of_page_band",
      bool(refusals_mentioning(_r, "uv[", "0..255")),
      f"a UV dragged a whole band left the export silent: {_r.refusals[:3]}")
obx.data.uv_layers["UVMap"].data[0].uv = _uv0
check("export_uv_refusal_clears", not exp.assemble(obx)[2].refusals)

# ---- §5.1.2 the grid ceilings and shrink (decision 10) --------------------
_grid = exp.flagged(obx, "grid")[0]
check("export_grid_object_is_flagged", _grid is not None)
# The two ceilings bind on DISJOINT populations (decision 16): 18x18 = 324 is
# refused by area with both axes legal, and 19 is refused by axis with the area
# still under 256.  A seed that trips both at once cannot tell them apart.
for _label, _set, _words in (
        ("shrink", {"size_z": 3}, ("shrinks the grid",)),
        ("axis_ceiling", {"size_z": 19}, ("axis ceiling",)),
        ("area_ceiling", {"size_x": 18, "size_z": 18}, ("area ceiling",)),
        ("non_positive", {"size_x": 0}, ("allowed 1..18",))):
    _was = {k: _grid[k] for k in _set}
    for _k, _v in _set.items():
        _grid[_k] = _v
    _r = exp.assemble(obx)[2]
    check(f"export_refuses_grid_{_label}",
          bool(refusals_mentioning(_r, *_words)),
          f"{_set} left the export silent: {_r.refusals[:3]}")
    if _label == "area_ceiling":
        check("export_area_ceiling_is_not_the_axis_one",
              not refusals_mentioning(_r, "axis ceiling"),
              "18x18 tripped the AXIS ceiling; 18 is legal on both axes")
    for _k, _v in _was.items():
        _grid[_k] = _v
check("export_grid_refusals_clear", not exp.assemble(obx)[2].refusals)
# Growth IS expressible: 10x13 -> 12x13 is inside both ceilings and must pass,
# and it must reach the document.  Without this arm every ceiling check above
# is satisfied by an export that refuses everything.
_grid["size_x"] = 12
_gr_doc, _, _gr = exp.assemble(obx)
check("export_growth_allowed", not _gr.refusals, str(_gr.refusals[:3]))
check("export_growth_reaches_document",
      _gr_doc["base"]["terrain_grid"] == {"size_x": 12, "size_z": 13},
      str(_gr_doc["base"]["terrain_grid"]))
_grid["size_x"] = 10

# ---- ADR-0187 decision 12: export mirrors `build`'s classification --------
# `tile_record` enforced decision 23's three-field limit only when a STORED
# kind flag read "drift", and nothing mirrored `_classify_terrain`'s pre-growth
# refusal at all -- so export and `build` disagreed about what was legal, and
# the addon would happily write a document `build` rejects one record at a
# time.  Decision 3 deletes the stored kind, so the class is DERIVED here the
# way `build` derives it: inside the base map's own extent and not named by the
# drift checker is a CARRIED tile, and a carried tile declares nothing.
#
# The drift and growth arms of the same rule are exercised where those classes
# actually exist -- `drift_pin_byte_still_refuses` and
# `growth_declared_field_reaches_the_document` -- because a class cannot be
# faked here by writing a property: it is recomputed from the mesh every time.
_tile = exp.flagged(obx, "tile")[0]
check("export_tile_object_is_flagged",
      "exmateria_map/tile" in _tile.keys(),
      f"{_tile.name} carries no tile flag")
check("adr0187_export_clean_before_the_carried_seed",
      not exp.assemble(obx)[2].refusals,
      f"the seed below could not be told from a standing refusal: "
      f"{exp.assemble(obx)[2].refusals[:3]}")
_tile["height_declared"] = True
_r = exp.assemble(obx)[2]
check("adr0187_export_refuses_a_declaration_on_a_carried_tile",
      bool(refusals_mentioning(_r, "still the base's")),
      f"a carried tile declared a field and export passed it on to a `build` "
      f"that refuses it: {_r.refusals[:3]}")
check("adr0187_carried_refusal_names_the_tile",
      any(f"({_tile['x']}, {_tile['z']}, L{_tile['level']})" in _x
          for _x in _r.refusals),
      str(_r.refusals[:3]))
_tile["height_declared"] = False
check("adr0187_carried_refusal_clears", not exp.assemble(obx)[2].refusals,
      str(exp.assemble(obx)[2].refusals[:3]))

# ---- ADR-0187 decision 13: the tiles are a NESTED collection --------------
# Two claims, and the second is a bug that predates the grid: `flagged()` read
# `col.objects`, which is the collection's DIRECT members only, so a tile the
# artist dragged into a sub-collection left the document with no message.  With
# 260 tile objects arriving at once the artist will organise them, so the
# silent loss goes from latent to routine.  The toggle decision 13 asks for is
# the nested collection's own visibility -- Blender's, not ours.
# The expected population is recomputed from the carried rows, not read back
# off `flagged()` -- which is the thing under test.
_want_tiles = len([e for e in (doc["base"].get("terrain_tiles") or [])
                   if e[2] == 0 or list(e[3:11]) != LEVEL1_DEFAULT])
_terr = next((c for c in exp.marker_collection(obx).children
              if c.get("exmateria_map/terrain") is not None), None)
check("adr0187_tiles_live_in_a_nested_collection", _terr is not None,
      "the marker collection has no addon-owned child collection: "
      + str([c.name for c in exp.marker_collection(obx).children]))
check("adr0187_the_nested_collection_holds_the_tiles",
      _terr is not None
      and len([o for o in _terr.objects if "exmateria_map/tile" in o]) == _want_tiles,
      f"{0 if _terr is None else len(_terr.objects)} objects in the child "
      f"collection, {_want_tiles} carried slots want an object")
check("adr0187_the_marker_stays_a_direct_member",
      exp.marker_collection(obx).objects.get(obx.name) is obx,
      "the marker moved out of its own collection, so `marker_collection` "
      "would resolve somewhere else")

# ...and it arrives CLOSED.  Reported from use: 260 tiles land on top of the
# map, and the thing the artist opened a map to look at is under them.  The
# grid is still "shown" in the sense ADR-0187 means -- it exists, it is one
# toggle away, and the toggle is Blender's own.  The EYE, not the checkbox:
# `exclude` takes the collection out of the view layer's depsgraph, which is a
# different claim than "I am not looking at this right now".
_terr_lc = mod._layer_collection(bpy.context.view_layer, _terr) if _terr else None
check("adr0187_the_grid_has_a_layer_collection",
      _terr_lc is not None,
      "the nested collection has no handle in the view layer, so nothing "
      "below can be read as hidden-or-not")
check("adr0187_the_grid_is_hidden_after_import",
      _terr_lc is not None and _terr_lc.hide_viewport,
      "the terrain collection's eye is open on arrival")
check("adr0187_the_grid_is_hidden_not_excluded",
      _terr_lc is not None and not _terr_lc.exclude,
      "the grid was EXCLUDED from the view layer rather than hidden")
check("adr0187_a_hidden_grid_still_reaches_the_document",
      len(exp.flagged(obx, "tile")) == _want_tiles,
      f"{len(exp.flagged(obx, 'tile'))} tiles reach the export with the "
      f"collection hidden, {_want_tiles} expected -- hiding must be a display "
      f"fact and nothing else")

# The recursion, proven two levels deep: a one-level special case would pass a
# `children` loop and still lose this tile.
_deep = bpy.data.collections.new("adr0187_deep")
(_terr or exp.marker_collection(obx)).children.link(_deep)
_moved = exp.flagged(obx, "tile")[0]
for _c in list(_moved.users_collection):
    _c.objects.unlink(_moved)
_deep.objects.link(_moved)
check("adr0187_moved_tile_is_not_a_direct_member",
      exp.marker_collection(obx).objects.get(_moved.name) is None,
      "the seed did not actually move the tile, so the two checks below "
      "cannot fail")
check("adr0187_flagged_finds_a_tile_two_collections_down",
      _moved in exp.flagged(obx, "tile"),
      f"{_moved.name} dropped out of the document when it was dragged into a "
      f"sub-collection")
# ...and the consequence at the document seam: a refusal that must fire cannot
# fire on a tile export cannot see.
_moved["height_declared"] = True
check("adr0187_a_tile_in_a_sub_collection_still_reaches_the_document",
      bool(refusals_mentioning(exp.assemble(obx)[2], "still the base's")),
      "a declaring carried tile in a sub-collection was exported in silence")
_moved["height_declared"] = False
for _c in list(_moved.users_collection):
    _c.objects.unlink(_moved)
(_terr or exp.marker_collection(obx)).objects.link(_moved)
bpy.data.collections.remove(_deep)
check("adr0187_nesting_arms_restored", not exp.assemble(obx)[2].refusals,
      str(exp.assemble(obx)[2].refusals[:3]))

# ---- §3.6 / §4.4 the sticky off-palette list ------------------------------
obx["exmateria_map/off_palette"] = json.dumps(
    [{"color": "#FF00FF", "count": 4, "bbox": [1, 2, 3, 4]}])
_r = exp.assemble(obx)[2]
check("export_refuses_off_palette",
      bool(refusals_mentioning(_r, "off-palette", "#FF00FF")),
      f"a sticky off-palette entry did not refuse: {_r.refusals[:3]}")
del obx["exmateria_map/off_palette"]
check("export_off_palette_clears", not exp.assemble(obx)[2].refusals)

# ---- §4.5 the sidecar: repack, re-hash, rename ----------------------------
_sheet = [st["texture_sheet"] for st in doc["map_states"] if st.get("texture_sheet")][0]
check("export_sidecar_name_reproduces",
      list(_files) == [_sheet],
      f"{list(_files)} != [{_sheet!r}] -- an UNCHANGED buffer must re-hash to "
      f"its own imported name")
_w, _h, _back, _plte, _al = pngmod.read_indexed_png(_files[_sheet])
_src_png = pngmod.read_indexed_png(
    open(os.path.join(os.path.dirname(JSON), _sheet), "rb").read())
check("export_sidecar_indices_are_the_buffer",
      (_w, _h) == (256, 1024) and _back == _src_png[2],
      "the written sidecar's indices are not the imported ones")
check("export_sidecar_plte_is_16", len(_plte) == 16, str(len(_plte)))
# The seed: ONE painted pixel must move the name, or the hash is not over the
# buffer at all.
_img = bpy.data.images[json.loads(obx["exmateria_map/sheet_images"])[_sheet]]
_px0 = _img.pixels[0]
_img.pixels[0] = float((int(round(_px0)) + 1) % 16)
_f2 = exp.assemble(obx)[1]
check("export_sidecar_name_moves_on_edit",
      list(_f2) != [_sheet] and list(_f2)[0].startswith("MAP001.a0.sheet-"),
      f"one changed index left the sidecar name at {list(_f2)}")
_img.pixels[0] = _px0
check("export_sidecar_name_restored", list(exp.assemble(obx)[1]) == [_sheet])

# ---- §5.3 the divergence list, both arms ----------------------------------
_pal0 = obx.data.attributes["palette_id"].data[0].value
obx.data.attributes["palette_id"].data[0].value = 5
_r = exp.assemble(obx)[2]
check("export_divergence_sees_an_edit",
      _r.divergence.get("palette_id") == 1, str(dict(_r.divergence)))
check("export_divergence_never_refuses", not _r.refusals, str(_r.refusals))
obx.data.attributes["palette_id"].data[0].value = _pal0
check("export_divergence_clears", not exp.assemble(obx)[2].divergence)

# ---- §9 the operator: all-or-nothing, and the bundle ----------------------
obx["exmateria_map/off_palette"] = json.dumps(
    [{"color": "#FF00FF", "count": 4, "bbox": [1, 2, 3, 4]}])
try:
    _res = bpy.ops.export_map.document(filepath=EXPORT_DIR)
except RuntimeError as e:
    _res = {"CANCELLED": str(e)}
check("export_operator_cancels_on_refusal", "FINISHED" not in _res, str(_res))
check("export_writes_nothing_on_refusal",
      os.listdir(EXPORT_DIR) == [],
      f"a refused export left {os.listdir(EXPORT_DIR)} behind")
del obx["exmateria_map/off_palette"]

# §9.2's target is a DIRECTORY, and the browser still shows a filename field.
# Typing `test` into it produced `<...>/Documents/test`, which does not exist,
# which fell back to its PARENT: the files landed one directory up from where
# the artist asked, and the report did not say where, so it read as nothing
# having been written.
_probe = os.path.join(EXPORT_DIR, "probe")
os.makedirs(_probe, exist_ok=True)
_probe_json = os.path.join(_probe, "already.json")
open(_probe_json, "w").write("{}")
check("outdir_uses_an_existing_directory",
      exp.output_directory(_probe) == _probe, exp.output_directory(_probe))
check("outdir_takes_the_parent_of_an_existing_file",
      exp.output_directory(_probe_json) == _probe,
      exp.output_directory(_probe_json))
check("outdir_takes_the_parent_of_a_typed_filename",
      exp.output_directory(os.path.join(_probe, "nope.json")) == _probe,
      exp.output_directory(os.path.join(_probe, "nope.json")))
check("outdir_treats_a_typed_bare_name_as_a_folder",
      exp.output_directory(os.path.join(_probe, "test"))
      == os.path.join(_probe, "test"),
      f"a typed folder name resolved to "
      f"{exp.output_directory(os.path.join(_probe, 'test'))!r}, which is where "
      f"MAP022's export went missing")
# The `filepath` here must live in a DIFFERENT directory than `directory`, or
# both branches return the same answer and the check cannot see which one ran.
_elsewhere = os.path.join(EXPORT_DIR, "elsewhere.json")
check("outdir_browser_directory_field_wins",
      exp.output_directory(_elsewhere, _probe) == _probe,
      f"{exp.output_directory(_elsewhere, _probe)!r}; the browser's own "
      f"directory field must beat the filename field")
# End to end: a folder that does not exist yet is created and written into.
_named = os.path.join(EXPORT_DIR, "typed_folder")
_res = bpy.ops.export_map.document(filepath=_named)
check("export_creates_the_named_folder",
      _res == {"FINISHED"} and os.path.isdir(_named)
      and "MAP001.a0.json" in os.listdir(_named),
      f"res={_res} contents={os.listdir(_named) if os.path.isdir(_named) else 'MISSING'}")
check("export_did_not_write_to_the_parent",
      "MAP001.a0.json" not in os.listdir(EXPORT_DIR),
      f"the bundle landed in the PARENT: {os.listdir(EXPORT_DIR)}")
shutil.rmtree(_named)
shutil.rmtree(_probe)

bpy.context.view_layer.objects.active = obx
_res = bpy.ops.export_map.document(filepath=EXPORT_DIR)
check("export_operator_finished", _res == {"FINISHED"}, str(_res))
_written = sorted(os.listdir(EXPORT_DIR))
check("export_operator_wrote_bundle",
      _written == sorted(["MAP001.a0.json", _sheet]), str(_written))
_out = json.loads(open(os.path.join(EXPORT_DIR, "MAP001.a0.json")).read())
check("export_operator_document_is_the_identity", not docdiff(doc, _out),
      "; ".join(docdiff(doc, _out)[:6]))
# §9.5 idempotence: a second export of an untouched scene is byte-identical.
_first = {f: open(os.path.join(EXPORT_DIR, f), "rb").read() for f in _written}
bpy.ops.export_map.document(filepath=EXPORT_DIR)
check("export_idempotent",
      all(open(os.path.join(EXPORT_DIR, f), "rb").read() == b
          for f, b in _first.items()),
      "a second export of an untouched scene changed a byte")
# The exported bundle re-imports, and re-exports to the same document: the
# document is not just equal to its source, it is a fixed point of the pair.
clear_scene()
_res = run_import(os.path.join(EXPORT_DIR, "MAP001.a0.json"))
check("export_output_reimports", _res == {"FINISHED"}, str(_res))
_ob3 = bpy.data.objects.get(name)
check("export_output_is_a_fixed_point",
      _ob3 is not None and not docdiff(doc, exp.assemble(_ob3)[0]),
      "re-importing the export and re-exporting did not reproduce it")

# §5.1.4 / §9.1: a scene with no marker is not an interchange scene.
clear_scene()
_m, _why = exp.find_marker(bpy.context)
check("export_refuses_without_marker",
      _m is None and "no marker" in (_why or ""), str(_why))
check("export_operator_poll_false_without_marker",
      not exp.EXPORT_OT_interchange_document.poll(bpy.context))

# ---- §8.1 the FF FF sentinel, on a LOADED face -----------------------------
# MAP001.a0 carries no sentinel binding -- its live polygon ships {255,127,0},
# which is FF FE -- so `walkable`'s import rule is untestable on the fixture as
# shipped.  The corpus carries 166 FF FF bindings across 6 arrangements, but
# they are invisible to the identity there: for a LOADED face both walkable
# arms write the same three numbers.  The rule is only observable on the
# import side, so assert it there.
clear_scene()
_sent = json.loads(open(JSON).read())
_sent["polygons"][0]["terrain"] = {"x": 255, "z": 127, "level": 1}
_sentp = JSON.replace(".json", ".sentinel.json")
json.dump(_sent, open(_sentp, "w"))
_res = run_import(_sentp)
check("sentinel_document_imports", _res == {"FINISHED"}, str(_res))
_obs = bpy.data.objects.get(name)
check("sentinel_face_is_not_walkable",
      not _obs.data.attributes["walkable"].data[0].value,
      "a face bound to FF FF imported as WALKABLE; the sentinel is not a tile")
check("sentinel_neighbour_is_walkable",
      _obs.data.attributes["walkable"].data[1].value is False,
      "the untextured face must never be walkable")
_sdoc, _, _srep = exp.assemble(_obs)
check("sentinel_round_trips", not docdiff(_sent, _sdoc),
      "; ".join(docdiff(_sent, _sdoc)[:4]))
# A sentinel is not a binding, so it has nothing to be outside of: the same
# document warned before, on {255, 127, 0}.
check("sentinel_does_not_warn", not _srep.warnings, str(_srep.warnings))

# ---- §8 new-face defaults -------------------------------------------------
# Two arms, in two scenes, because they assert OPPOSITE things about the same
# attribute: a from-scratch face takes the addon's default, an extruded child
# takes its parent's value.  Run together, a face count cannot tell them apart.

# arm 1 -- a face built from scratch: no parent, so the defaults are all it has
clear_scene()
run_import(staged)
obn = bpy.data.objects.get(name)
men = obn.data
_n_before = len(men.polygons)
bm = bmesh.new()
bm.from_mesh(men)
bm.faces.new([bm.verts.new(v) for v in ((900, 900, 0), (928, 900, 0),
                                        (928, 928, 0), (900, 928, 0))])
bm.to_mesh(men)
bm.free()
men.update()
check("newface_scratch_added_one", len(men.polygons) == _n_before + 1,
      f"{len(men.polygons)} faces, expected {_n_before + 1}")
_imported = [d.value for d in men.attributes["imported"].data]
_scratch_i = [i for i, v in enumerate(_imported) if not v]
check("newface_scratch_is_not_imported", _scratch_i == [_n_before],
      f"{_scratch_i}; a from-scratch face must read the ZERO-FILLED default")
# The normal is the verbatim corner attribute -- §8.2, no fallback to a
# computed one -- and zero-fill is what "no authored normal" means.
_f = men.polygons[_scratch_i[0]]
check("newface_normal_is_zero",
      all(tuple(men.attributes["normals"].data[li].vector) == (0.0, 0.0, 0.0)
          for li in range(_f.loop_start, _f.loop_start + _f.loop_total)),
      "a new face arrived with a computed normal")
check("newface_uv_is_zero",
      all(tuple(men.uv_layers["UVMap"].data[li].uv) == (0.0, 0.0)
          for li in range(_f.loop_start, _f.loop_start + _f.loop_total)))
check("newface_not_walkable",
      not men.attributes["walkable"].data[_scratch_i[0]].value)
check("newface_not_flipped",
      not men.attributes["fft_ring_flipped"].data[_scratch_i[0]].value)
check("newface_visible_angles_starts_zero",
      men.attributes["visible_angles"].data[_scratch_i[0]].value == 0,
      "Blender zero-fill is the premise `stamp_new_faces` exists to fix; if "
      "this ever reads 0x8000 on its own the stamp is untested")

_nd, _, _nrep = exp.assemble(obn)
check("newface_counted_as_added", _nrep.new_faces == 1, str(_nrep.new_faces))
check("newface_stamped_visible_angles",
      men.attributes["visible_angles"].data[_scratch_i[0]].value == 32768,
      f"export left a from-scratch face at "
      f"{men.attributes['visible_angles'].data[_scratch_i[0]].value}, "
      f"not §8's 0x8000")
_uq = [q for q in _nd["polygons"] if q["kind"] == "untextured_quad"]
check("newface_exports_untextured_quad", len(_uq) == 2, str(len(_uq)))
# Name the new polygon by its POSITIONS, not by a value count: the fixture's
# own untextured quad already carries 32768, so counting them cannot tell the
# stamped face from the loaded one.
_mine = [q for q in _uq if [900, 0, 900] in q["positions"]]
check("newface_reached_the_document", len(_mine) == 1,
      str([q["positions"] for q in _uq]))
check("newface_default_reached_the_document",
      len(_mine) == 1 and _mine[0]["visible_angles"] == 32768,
      str(_mine[0]["visible_angles"]) if _mine else "no new polygon")
check("newface_unknown_untextured_zero",
      len(_mine) == 1 and _mine[0]["unknown_untextured"] == [0, 0, 0, 0],
      str(_mine[0]["unknown_untextured"]) if _mine else "no new polygon")
# §9.5's idempotence reaches the stamp: a second export must not re-stamp, and
# an artist who deliberately sets 0 must keep it.
men.attributes["visible_angles"].data[_scratch_i[0]].value = 0
check("newface_stamp_is_idempotent",
      exp.stamp_new_faces(men) == 0
      and men.attributes["visible_angles"].data[_scratch_i[0]].value == 0,
      "the stamp overwrote a deliberate 0 on a second run")

# §8.1: `walkable` False exports the FF FF sentinel -- {255, 127, 1} -- which
# is NOT schema §5.2's worked example {255, 127, 0} (that is FF FE, a shipped
# out-of-grid binding, and the fixture's live polygon carries it).
_tq = [q for q in _nd["polygons"] if q["kind"] == "textured_quad"]
check("newface_walkable_keeps_the_shipped_binding",
      [q["terrain"] for q in _tq] == [{"x": 255, "z": 127, "level": 0}],
      str([q["terrain"] for q in _tq]))
men.attributes["walkable"].data[0].value = False
_sd = exp.assemble(obn)[0]
check("newface_sentinel_for_unwalkable",
      [q["terrain"] for q in _sd["polygons"]
       if q["kind"] == "textured_quad"] == [{"x": 255, "z": 127, "level": 1}],
      "walkable False did not export the FF FF sentinel: "
      + str([q.get("terrain") for q in _sd["polygons"]]))
men.attributes["walkable"].data[0].value = True

# arm 2 -- an extruded child: §8's inheritance clause.  The parent is given a
# distinctive value FIRST, because the fixture's own 32768 is exactly the
# stamp value and would make "inherited" and "stamped" indistinguishable.
clear_scene()
run_import(staged)
obe = bpy.data.objects.get(name)
mee = obe.data
mee.attributes["visible_angles"].data[0].value = 4321
_n_before = len(mee.polygons)
bm = bmesh.new()
bm.from_mesh(mee)
bm.faces.ensure_lookup_table()
_ext = bmesh.ops.extrude_face_region(bm, geom=[bm.faces[0]])
bmesh.ops.translate(bm, vec=(0, 0, 24),
                    verts=[e for e in _ext["geom"]
                           if isinstance(e, bmesh.types.BMVert)])
bm.to_mesh(mee)
bm.free()
mee.update()
_children = list(range(_n_before, len(mee.polygons)))
check("extrude_made_children", len(_children) > 0, str(_children))
check("extrude_children_are_imported",
      all(mee.attributes["imported"].data[i].value for i in _children),
      "an extruded child read as a from-scratch face")
_ed, _, _erep = exp.assemble(obe)
check("extrude_no_faces_added_since_import", _erep.new_faces == 0,
      str(_erep.new_faces))
check("extrude_children_inherit_visible_angles",
      all(mee.attributes["visible_angles"].data[i].value == 4321
          for i in _children),
      "an extruded child was STAMPED (0x8000) instead of inheriting 4321: "
      + str([mee.attributes["visible_angles"].data[i].value
             for i in _children]))
check("extrude_children_inherit_the_binding",
      all(q["terrain"] == {"x": 255, "z": 127, "level": 0}
          for q in _ed["polygons"] if q["kind"] == "textured_quad"),
      str([q["terrain"] for q in _ed["polygons"]
           if q["kind"] == "textured_quad"]))
check("extrude_children_export_without_refusal", not _erep.refusals,
      str(_erep.refusals[:3]))

# ---- schema §3's bucket order ---------------------------------------------
# A document whose polygons arrive OUT of bucket order must come back IN it.
# Without this, `export_identity` is satisfied by an export that simply keeps
# the mesh's face order, which is only accidentally right on a dump document.
clear_scene()
_rev = json.loads(open(JSON).read())
_rev["polygons"] = list(reversed(_rev["polygons"]))     # uq before tq
_revp = JSON.replace(".json", ".reordered.json")
json.dump(_rev, open(_revp, "w"))
_res = run_import(_revp)
check("bucket_reordered_imports", _res == {"FINISHED"}, str(_res))
_obr = bpy.data.objects.get(name)
_rd = exp.assemble(_obr)[0]
check("export_emits_bucket_order",
      [q["kind"] for q in _rd["polygons"]] == ["textured_quad", "untextured_quad"],
      str([q["kind"] for q in _rd["polygons"]]))
check("export_bucket_order_keeps_the_faces",
      sorted(json.dumps(q, sort_keys=True) for q in _rd["polygons"])
      == sorted(json.dumps(q, sort_keys=True) for q in doc["polygons"]),
      "re-bucketing changed a polygon's contents")

# =============================================== growth + drift (blocks 4, 3) ===
from exmateria_map import authoring as au

# ---- §7.1 the clamp, as a pure function of the target extent ---------------
# Decision 16: every refusal on this surface is knowable BEFORE anything
# happens, so the clamp is testable without a scene at all.  Both ceilings bind
# on DISJOINT populations, so each arm names the one that stopped it.
_c = au.clamp_extent(19, 13, 10, 13)
check("clamp_axis_ceiling", _c[0] == 18 and "axis ceiling" in _c[2], str(_c))
check("clamp_axis_is_not_the_area_one", "area ceiling" not in _c[2], str(_c))
_c = au.clamp_extent(18, 18, 10, 13)
check("clamp_area_ceiling", _c[0] * _c[1] <= 256 and "area ceiling" in _c[2],
      str(_c))
check("clamp_area_is_not_the_axis_one", "axis ceiling" not in _c[2], str(_c))
_c = au.clamp_extent(8, 13, 10, 13)
check("clamp_refuses_shrink", _c[0] == 10 and "shrink" in _c[2], str(_c))
_c = au.clamp_extent(12, 13, 10, 13)
check("clamp_allows_growth", _c[:2] == (12, 13) and _c[2] == "", str(_c))
check("clamp_is_idempotent", au.clamp_extent(*_c[:2], 10, 13)[:2] == (12, 13))

# ---- §7 growth on the real scene ------------------------------------------
clear_scene()
run_import(staged)
obg = bpy.data.objects.get(name)
_g = exp.flagged(obg, "grid")[0]
# ADR-0187 decision 3: the pre-growth extent already carries an object per
# tile, so growth's baseline is the whole grid rather than the fixture's single
# declared record.  What growth adds is still only what is OUTSIDE that extent.
_g_before = len(exp.flagged(obg, "tile"))
check("adr0187_growth_baseline_is_the_whole_grid", _g_before >= 130,
      f"{_g_before} tile objects before growth")
check("growth_widget_seeded_from_import",
      (_g.exmateria_map_size_x, _g.exmateria_map_size_z) == (10, 13),
      f"{_g.exmateria_map_size_x}x{_g.exmateria_map_size_z}")
check("growth_preview_zero_on_untouched",
      au.growth_preview(obg)["created"] == 0,
      f"an untouched import reads {au.growth_preview(obg)['created']} pending "
      f"-- the pre-growth extent is the `_shadow` twin, not zero")
_res = bpy.ops.exmateria_map.apply_growth()
check("growth_apply_on_untouched_creates_nothing",
      len(exp.flagged(obg, "tile")) == _g_before,
      str(len(exp.flagged(obg, "tile"))))

# Typing the field grows the DOCUMENT's extent; the button only makes handles.
_g.exmateria_map_size_x = 12
check("growth_field_reaches_the_id_property", _g["size_x"] == 12,
      str(_g.get("size_x")))
check("growth_field_reaches_the_document",
      exp.assemble(obg)[0]["base"]["terrain_grid"] == {"size_x": 12,
                                                       "size_z": 13})
check("growth_field_resized_the_footprint",
      abs(max(v.co[0] for v in _g.data.vertices) - 12 * 28) < 1e-3,
      str(max(v.co[0] for v in _g.data.vertices)))
check("growth_field_created_no_objects",
      len(exp.flagged(obg, "tile")) == _g_before,
      "typing the extent created tile objects; §7.1 says it must not")
_pv = au.growth_preview(obg)
check("growth_preview_counts_the_new_column", _pv["created"] == 2 * 13,
      str(_pv))
check("growth_preview_changes_nothing_existing", _pv["changed"] == 0)
check("growth_preview_pin_number_is_na", _pv["externally_pinned"] is None,
      "§7.5: with no shipped pin table the number is n/a, never 0")

_res = bpy.ops.exmateria_map.apply_growth()
check("growth_apply_finished", _res == {"FINISHED"}, str(_res))
check("growth_apply_created_the_new_column",
      len(exp.flagged(obg, "tile")) == _g_before + 2 * 13,
      str(len(exp.flagged(obg, "tile"))))
check("growth_apply_is_idempotent",
      bpy.ops.exmateria_map.apply_growth() == {"FINISHED"}
      and len(exp.flagged(obg, "tile")) == _g_before + 2 * 13,
      "a second apply created more handles")
# ADR-0187 site 9: the growth SEED read the same unfiltered set `sync_drift`
# did.  §7.2 seeds a new handle from "an existing record's values when there is
# one"; the code took `next(iter(_tile_objects(ob)))`, which was that only
# while a handful of declaring objects existed.  With the grid shown it is an
# arbitrary CARRIED tile -- here (0, 0), whose bytes are the base map's -- so
# every new handle arrives wearing another tile's height and surface type.
_seeded = [t for t in exp.flagged(obg, "tile")
           if t["x"] >= 10 or t["z"] >= 13]
_zero = bpy.data.objects["tile_0_0_L0"]
check("adr0187_growth_seed_arm_is_not_vacuous",
      (_zero.get("height"), _zero.get("surface_type")) != (0, 3)
      and _zero.get("height") != 0,
      f"tile (0,0) already looks like the default seed "
      f"(height={_zero.get('height')}), so the check below cannot fail")
check("adr0187_growth_seeds_the_default_not_a_carried_neighbour",
      all(t.get("height") == 0 and t.get("impassable") == 1
          and all(t.get(_f) == 0 for _f in au.TILE_PAYLOAD_FIELDS
                  if _f not in ("height", "impassable"))
          for t in _seeded),
      f"{len(_seeded)} new handles; the first reads "
      + str({_f: _seeded[0].get(_f) for _f in au.TILE_PAYLOAD_FIELDS}
            if _seeded else {}))

# A growth handle is a tile of the same grid, so it wears decision 10's colour
# and decision 10's one shared material.  Left grey it would read as a class of
# its own, which is a distinction the ADR does not make.
_gm = {_o.data.materials[0] for _o in _seeded if _o.data.materials}
check("adr0187_growth_handles_share_the_tile_material",
      _gm == {bpy.data.materials.get(mod.TILE_MATERIAL)},
      str([_m.name for _m in _gm]))
_g_col_bad = [(_o.name, _c[:3],
               gdx_vertex_color(_o["x"], _o["z"], _c[3],
                                _o.get("impassable"), _o.get("unselectable")))
              for _o in _seeded for _c in (tile_colors(_o) or [])
              if _c[:3] != gdx_vertex_color(_o["x"], _o["z"], _c[3],
                                            _o.get("impassable"),
                                            _o.get("unselectable"))]
check("adr0187_growth_handles_are_coloured_the_same_way",
      _seeded and all(tile_colors(_o) is not None for _o in _seeded)
      and not _g_col_bad,
      str(_g_col_bad[:3]) or "a growth handle carries no colour attribute")

# Decision 20: growth writes NOTHING.  26 handles, none declared, so the
# document's `terrain` is exactly the record it arrived with.
_gd, _, _grep = exp.assemble(obg)
check("growth_handles_export_no_record", _gd["terrain"] == doc["terrain"],
      str(_gd["terrain"]))
check("growth_export_has_no_refusals", not _grep.refusals, str(_grep.refusals[:3]))
# ... and a DECLARED field on one of them does reach the document, or the
# handles are decoration.
# The growth arm of ADR-0187 decision 12's rule: a tile OUTSIDE the base map's
# own extent is growth-created, and growth may declare the lot -- decision 23's
# three-field limit is the drift class's, not everyone's.
_pre = exp.pre_growth_extent(obg)
check("adr0187_pre_growth_extent_is_the_base_maps",
      _pre == (10, 13),
      f"the pre-growth boundary reads {_pre}; it must be the BASE map's own "
      f"extent, carried in `base.terrain_tiles`, not the grown target extent")
_new = [t for t in exp.flagged(obg, "tile")
        if t["x"] >= _pre[0] or t["z"] >= _pre[1]][0]
au.declare(_new, "height", 7)
_gnr = exp.assemble(obg)[2]
check("adr0187_growth_tile_may_declare_anything",
      not _gnr.refusals, str(_gnr.refusals[:3]))
au.declare(_new, "surface_type", 5)
check("adr0187_growth_tile_may_declare_a_pin_byte",
      not exp.assemble(obg)[2].refusals,
      str(exp.assemble(obg)[2].refusals[:3]))
au.undeclare(_new, "surface_type")
_gd2 = exp.assemble(obg)[0]
_rec = [r for r in _gd2["terrain"]
        if (r["x"], r["z"]) == (_new["x"], _new["z"])]
check("growth_declared_field_reaches_the_document",
      len(_rec) == 1 and _rec[0].get("height") == 7
      and set(_rec[0]) == {"x", "z", "level", "height"},
      str(_rec))
au.undeclare(_new, "height")
check("growth_undeclare_drops_the_record",
      exp.assemble(obg)[0]["terrain"] == doc["terrain"])

# ADR-0187 decision 13, on the GROWTH path: a handle the button creates is a
# tile of the same grid, so it belongs in the same nested `terrain` collection
# the import built.  Linked into the marker collection beside the marker
# instead, the collection visibility that IS decision 13's toggle cannot hide
# it -- and decision 14's "one toggle covers both levels" quietly becomes one
# toggle covering most tiles, which is the failure nobody reports because the
# grid still mostly disappears.
_gterr = next((c for c in exp.marker_collection(obg).children
               if c.get("exmateria_map/terrain") is not None), None)
check("adr0187_growth_arm_is_not_vacuous",
      _gterr is not None and len(_seeded) == 2 * 13,
      f"{len(_seeded)} growth handles, nested collection "
      f"{None if _gterr is None else _gterr.name}")
_g_outside = [_o.name for _o in _seeded
              if _gterr is None or _gterr.objects.get(_o.name) is not _o]
check("adr0187_growth_handles_join_the_nested_collection",
      not _g_outside,
      f"{len(_g_outside)} growth handles are outside the `terrain` "
      f"collection, so the toggle cannot hide them: {_g_outside[:3]}")
check("adr0187_growth_handles_are_not_direct_members_of_the_marker",
      not [_o for _o in _seeded
           if exp.marker_collection(obg).objects.get(_o.name) is _o],
      "a growth handle is a direct member of the marker collection")

# §7.2's seed is "an existing RECORD's values", and a record is what a tile
# DECLARES.  `src` is the first tile carrying any declared field -- typically a
# drift tile, whose other nineteen values are still the base map's bytes -- and
# copying every key of `src` dresses each new handle in the base map after all,
# by a different route.  Seed it here from a CARRIED tile, which is the same
# shape and the one the fixture can build: only `height` is a record.
au.declare(_zero, "height", 7)
_g.exmateria_map_size_x = 13
_res = bpy.ops.exmateria_map.apply_growth()
_seeded2 = [t for t in exp.flagged(obg, "tile") if t["x"] == 12]
check("adr0187_growth_seed_source_arm_is_not_vacuous",
      _res == {"FINISHED"} and len(_seeded2) == 13
      and _zero.get("surface_type") not in (0, None),
      f"{len(_seeded2)} handles in the new column; the source tile reads "
      f"surface_type={_zero.get('surface_type')}, so an undeclared copy "
      f"would be invisible here")
check("adr0187_growth_seed_takes_the_sources_declared_field",
      all(t.get("height") == 7 for t in _seeded2),
      str([t.get("height") for t in _seeded2[:4]]))
_undecl = [(t.name, _f, t.get(_f)) for t in _seeded2
           for _f in au.TILE_PAYLOAD_FIELDS
           if _f != "height"
           and t.get(_f) != (1 if _f == "impassable" else 0)]
check("adr0187_growth_seed_defaults_what_the_source_did_not_declare",
      not _undecl,
      f"{len(_undecl)} field(s) copied off an undeclared value -- the base "
      f"map's own bytes, reaching the new handle through the source tile: "
      + str(_undecl[:3]))
au.undeclare(_zero, "height")
check("adr0187_growth_seed_arms_restored", not exp.assemble(obg)[2].refusals,
      str(exp.assemble(obg)[2].refusals[:3]))

# ---- ADR-0187 decision 11: the sidebar is READ-ONLY on a carried tile ------
# `_tile` drew a declare-checkbox per payload field for anything carrying the
# tile flag.  That was 20 checkboxes on objects that barely existed; with the
# grid shown it is 20 checkboxes on every one of 260 tiles, and each of them
# leads straight to `build` refusing with *"that tile is still the base's"*.
# The values still SHOW -- read-only is not blank.
#
# Graded by what `draw` EMITS, onto a recording layout: a structural read of
# the method would pass a panel that emits the checkboxes anyway.
def _terrain_panel(tile_ob):
    _sink, _drawn_ops, _drawn_labels = [], [], []

    class _TL(_FakeLayout):
        def label(self, text="", icon=None, **kw):
            _drawn_labels.append(text)
            _sink.append(icon)
            return self

        def operator(self, bl_idname, text="", icon=None, **kw):
            class _Op:
                pass
            _drawn_ops.append(bl_idname)
            _sink.append(icon)
            return _Op()

    _tl = _TL(_sink)
    _cls = bpy.types.MAP_PT_terrain
    _cls.draw(_panel_shim(_cls, _tl),
              type("_Ctx", (), {"object": tile_ob, "scene": bpy.context.scene})())
    return _drawn_ops, _drawn_labels, _sink


_declare_id = au.MAP_OT_declare_field.bl_idname
_carried_tile = next(t for t in exp.flagged(obg, "tile")
                     if t["x"] < _pre[0] and t["z"] < _pre[1]
                     and not any(au.is_declared(t, _f)
                                 for _f in au.TILE_PAYLOAD_FIELDS))
_c_ops, _c_labels, _c_icons = _terrain_panel(_carried_tile)
check("adr0187_sidebar_offers_no_checkbox_on_a_carried_tile",
      _c_ops.count(_declare_id) == 0,
      f"{_c_ops.count(_declare_id)} declare-field checkboxes on a carried "
      f"tile; every one of them leads to a `build` refusal")
check("adr0187_sidebar_still_shows_every_payload_value",
      all(any(_f + " = " in _t for _t in _c_labels)
          for _f in au.TILE_PAYLOAD_FIELDS),
      f"read-only became blank: {_c_labels[:4]}")
check("adr0187_sidebar_says_the_tile_is_the_base_maps",
      any("carried" in _t for _t in _c_labels),
      f"the panel never names the class, so the missing checkboxes read as a "
      f"bug: {_c_labels[:3]}")
check("adr0187_sidebar_icons_valid_on_a_carried_tile",
      all(_i is None or _i in bpy.types.UILayout.bl_rna.functions["prop"]
          .parameters["icon"].enum_items for _i in _c_icons),
      str(_c_icons))
# ...and the one box that survives: a field the carried tile ALREADY declares.
# That state is reachable -- a drift fix stays declared when the drift clears
# (decision 4) -- and with no box the artist's only exit from decision 12's
# refusal is a re-import that throws the rest of their work away.
au.declare(_carried_tile, "height", 4)
_w_ops, _w_labels, _ = _terrain_panel(_carried_tile)
check("adr0187_a_carried_tile_can_still_WITHDRAW_a_declaration",
      _w_ops.count(_declare_id) == 1,
      f"{_w_ops.count(_declare_id)} checkboxes on a carried tile declaring one "
      f"field; the artist must be able to take it back, and only that")
au.undeclare(_carried_tile, "height")
check("adr0187_withdrawing_restores_the_read_only_panel",
      _terrain_panel(_carried_tile)[0].count(_declare_id) == 0,
      "the box outlived the declaration it was there to withdraw")

# The contrast arm: without it, a panel that emits nothing at all passes above.
_g_ops, _g_labels, _ = _terrain_panel(_new)
check("adr0187_sidebar_is_writable_on_a_growth_tile",
      _g_ops.count(_declare_id) == len(au.TILE_PAYLOAD_FIELDS),
      f"{_g_ops.count(_declare_id)} checkboxes on a growth tile, want "
      f"{len(au.TILE_PAYLOAD_FIELDS)}")

# ---- the class has to SURVIVE a save and a reopen --------------------------
# Decision 3 deletes the stored kind as `build`'s authority, but the addon
# still stamps one, and `build_terrain` stamped `imported` on EVERY object it
# made -- including a tile rebuilt from a declared record OUTSIDE the base
# map's extent, which is growth-created.  Reopen a grown document and the
# sidebar would call that tile carried and read-only while `tile_record`
# classified the same tile as growth and accepted all twenty fields.  Panel and
# export disagreeing about one tile is the defect; agreeing is the check.
au.declare(_new, "height", 7)
_reopen_key = (_new["x"], _new["z"], _new["level"])
_rdoc, _rfiles, _rrep = exp.assemble(obg)
_RDIR = os.path.join(os.path.dirname(JSON), "reopen_out")
os.makedirs(_RDIR, exist_ok=True)
for _f in os.listdir(_RDIR):
    os.unlink(os.path.join(_RDIR, _f))
exp.write_bundle(_rdoc, _rfiles, _RDIR)
check("adr0187_grown_document_reexports_the_growth_record",
      not _rrep.refusals
      and any((r["x"], r["z"]) == _reopen_key[:2] for r in _rdoc["terrain"]),
      f"the seed never reached the document: {_rrep.refusals[:3]} "
      f"{_rdoc['terrain']}")
_rres = run_import(os.path.join(_RDIR, f"{name}.json"))
check("adr0187_grown_document_reimports", _rres == {"FINISHED"}, str(_rres))
_rob = bpy.data.objects.get(
    f"tile_{_reopen_key[0]}_{_reopen_key[1]}_L{_reopen_key[2]}")
check("adr0187_reopened_growth_tile_exists", _rob is not None,
      "the declared record beyond the base extent rebuilt no object")
check("adr0187_reopened_growth_tile_is_stamped_growth",
      _rob is not None and _rob.get("exmateria_map/tile") == au.TILE_GROWTH,
      f"stamped {None if _rob is None else _rob.get('exmateria_map/tile')!r}; "
      f"the sidebar reads this and would call it carried and read-only")
check("adr0187_reopened_growth_tile_is_writable_in_the_sidebar",
      _rob is not None
      and _terrain_panel(_rob)[0].count(_declare_id)
      == len(au.TILE_PAYLOAD_FIELDS),
      "the panel and `tile_record` disagree about this tile's class")
# ...and the general form of the same rule, over every tile in the scene: the
# flag the panel reads must say what `tile_record` derives.
_rmark = bpy.data.objects.get(name)
_rpre = exp.pre_growth_extent(_rmark)
_rdrift = frozenset(au.drifted(_rmark))
_class_bad = []
for _t in exp.flagged(_rmark, "tile"):
    _pre_growth = _t["x"] < _rpre[0] and _t["z"] < _rpre[1]
    _stored_growth = _t.get("exmateria_map/tile") == au.TILE_GROWTH
    if _stored_growth == _pre_growth and len(_class_bad) < 5:
        _class_bad.append((_t.name, _t.get("exmateria_map/tile"), _rpre))
check("adr0187_the_stamped_class_agrees_with_the_derived_one",
      not _class_bad, str(_class_bad[:4]))

# ---- §6 the drift checker --------------------------------------------------
# MAP001.a0's two shipped polygons are both VERTICAL, so nothing covers a tile
# and the checker is vacuous on the fixture as it stands.  This variant adds a
# floor over tile (0,0) at the base's own step 2 (world Z 24 = 2 x 12) and
# drops the terrain record, so the tile is the checker's to warn about.
clear_scene()
_dv = json.loads(open(JSON).read())
_floor = json.loads(json.dumps(_dv["polygons"][0]))
# doc ring is (0, 1, 3, 2), so the perimeter is doc[0], doc[1], doc[3], doc[2];
# FFT (x, y, z) <- Blender (x, -z, y), and Blender +Z is up, so a floor at
# world Z 24 is FFT y -24 with the normal at FFT -y.
_floor["positions"] = [[0, -24, 0], [28, -24, 0], [0, -24, 28], [28, -24, 28]]
_floor["normals"] = [[0, -4096, 0]] * 4
_dv["polygons"] = [_dv["polygons"][0], _floor, _dv["polygons"][1]]
_dv["terrain"] = None
_dvp = JSON.replace(".json", ".drift.json")
json.dump(_dv, open(_dvp, "w"))
_res = run_import(_dvp)
check("drift_variant_imports", _res == {"FINISHED"}, str(_res))
obd = bpy.data.objects.get(name)
check("drift_variant_has_a_floor_step",
      au.base_floor_steps(obd) == {(0, 0): (2, 0, 0)},
      str(au.base_floor_steps(obd)))
check("drift_variant_covers_the_tile",
      au.floor_bottoms(obd.data, 10, 13) == {(0, 0): 24.0},
      f"the live coverage rule found {au.floor_bottoms(obd.data, 10, 13)}; it "
      f"must agree with dump's, or every tile reads as drifted on import")
# Decision 22: an untouched document has no drift BY CONSTRUCTION.
check("drift_zero_on_import", not au.drifted(obd), str(au.drifted(obd)))
# ADR-0187 decision 3: the drift checker no longer CREATES anything.  Every
# tile of the extent already has an object, so what must be zero on import is
# the number of tiles MARKED as drifted -- not the number of tile objects,
# which is now the whole grid.
_all_tiles_before = len(exp.flagged(obd, "tile"))
check("adr0187_drift_variant_has_the_whole_grid",
      _all_tiles_before >= 130,
      f"only {_all_tiles_before} tile objects; the shadowing trap below cannot "
      f"bite unless the grid is actually present, so this check is what stops "
      f"the trap test passing vacuously")
check("drift_no_handles_on_import",
      [t for t in exp.flagged(obd, "tile") if bool(t.get("exmateria_map/drift"))] == [])
check("drift_count_reported", obd.get("exmateria_map/drift_count") == 0,
      str(obd.get("exmateria_map/drift_count")))

# The seed: raise the floor one step.  The check exists to see exactly this.
_floor_face = [i for i, f in enumerate(obd.data.polygons)
               if abs(min(obd.data.vertices[obd.data.loops[li].vertex_index].co[2]
                          for li in range(f.loop_start,
                                          f.loop_start + f.loop_total)) - 24) < 1e-3][0]
_fp = obd.data.polygons[_floor_face]
_fverts = {obd.data.loops[li].vertex_index
           for li in range(_fp.loop_start, _fp.loop_start + _fp.loop_total)}
for _vi in _fverts:
    obd.data.vertices[_vi].co[2] = 36.0
_n, _fixed = au.sync_drift(obd)
# THE trap ADR-0187's Consequences names.  `sync_drift` used to subtract "every
# tile object" from the drifted set under a comment saying it meant "every tile
# that declares something".  Those were the same set only because ~0 tile
# objects existed; with decision 3 they are the whole grid, and the checker
# would report zero drift FOREVER while printing that the document already
# declares the tiles -- green, silent and false.
check("drift_seen_after_the_floor_moves", _n == 1,
      f"a floor moved a whole step and the checker saw {_n} drifted tiles "
      f"with {_all_tiles_before} tile objects present")
_handles = [t for t in exp.flagged(obd, "tile") if bool(t.get("exmateria_map/drift"))]
check("drift_marks_the_tile_already_there", len(_handles) == 1, str(len(_handles)))
check("drift_creates_no_object",
      len(exp.flagged(obd, "tile")) == _all_tiles_before,
      f"drift changed the object count from {_all_tiles_before} to "
      f"{len(exp.flagged(obd, 'tile'))}; decision 3 says it marks, never creates")
_h = _handles[0] if _handles else bpy.data.objects["tile_0_0_L0"]
check("drift_handle_names_the_tile",
      (_h["x"], _h["z"], _h["level"]) == (0, 0, 0), str(dict(_h)))
check("drift_handle_shows_the_base_value",
      _h.get("height_base") == 2 and _h.get("drift_step_now") == 3,
      f"base={_h.get('height_base')} now={_h.get('drift_step_now')}")
check("drift_handle_declares_nothing_yet",
      not any(au.is_declared(_h, f) for f in au.DRIFT_FIELDS)
      and exp.assemble(obd)[0]["terrain"] is None,
      "a fresh handle already declared a field; §7.4 says `terrain` stays null")
# Decision 8: the tile shows the RECORD, the mesh shows itself, and the gap
# between them IS the drift.  Pinning the tile to the live floor would hide the
# record's number and show the artist their own mesh twice.
# Decision 11's third class: a drifted tile is writable, but only over
# decision 23's three fields.
_d_ops, _d_labels, _ = _terrain_panel(_h)
check("adr0187_sidebar_is_the_three_fields_on_a_drifted_tile",
      _d_ops.count(_declare_id) == len(au.DRIFT_FIELDS),
      f"{_d_ops.count(_declare_id)} checkboxes on a drifted tile, want "
      f"{len(au.DRIFT_FIELDS)}")
check("drift_tile_stays_at_the_record_height",
      abs(_h.data.vertices[0].co[2] - 24.0) < 1e-3,
      f"the tile moved to {_h.data.vertices[0].co[2]}; it must stay at the "
      f"record's 24 while the floor sits at 36")
check("drift_sync_is_idempotent",
      au.sync_drift(obd) == (1, 0)
      and len([t for t in exp.flagged(obd, "tile")
               if bool(t.get("exmateria_map/drift"))]) == 1
      and len(exp.flagged(obd, "tile")) == _all_tiles_before,
      "a second sync duplicated the mark or an object")

# Decision 23: only the three.  The declared fix reaches the document.
au.declare(_h, "height", 3)
check("drift_fix_counted", au.sync_drift(obd)[1] == 1,
      "the panel's `M with a declared fix` did not move")
_dd, _, _drep = exp.assemble(obd)
check("drift_fix_reaches_the_document",
      _dd["terrain"] == [{"x": 0, "z": 0, "level": 0, "height": 3}],
      str(_dd["terrain"]))
check("drift_fix_does_not_refuse", not _drep.refusals, str(_drep.refusals[:3]))
check("drift_declared_survives_resync",
      au.sync_drift(obd) and au.is_declared(_h, "height"),
      "a re-sync withdrew a declared fix; §6.2 says declared fields survive")
au.declare(_h, "surface_type", 3)
check("drift_pin_byte_still_refuses",
      bool(refusals_mentioning(exp.assemble(obd)[2], "drift record",
                               "surface_type")),
      "a drift handle declaring a pin byte passed")
# And the drifted tile is still the ONE object, so the record it exports is one
# record -- which is what makes decision 3's collapse safe. The double-record
# hazard the old `shadowed` set guarded against cannot arise any more.
check("adr0187_a_drifted_tile_exports_one_record",
      len([r for r in (exp.assemble(obd)[0]["terrain"] or [])
           if (r["x"], r["z"], r["level"]) == (0, 0, 0)]) == 1,
      str(exp.assemble(obd)[0]["terrain"]))
au.undeclare(_h, "surface_type")

# Drift CLEARING deletes the quad, and the record drops from the next export.
for _vi in _fverts:
    obd.data.vertices[_vi].co[2] = 24.0
_n, _fixed = au.sync_drift(obd)
check("drift_cleared_when_the_floor_returns", _n == 0, str(_n))
# Decision 4: the object is NEVER deleted -- an authored fix lives on it, so
# deleting one destroys the artist's work.  Clearing drift unmarks it.
check("drift_unmarked_when_it_clears",
      [t for t in exp.flagged(obd, "tile") if bool(t.get("exmateria_map/drift"))] == [])
check("drift_clearing_deletes_no_object",
      len(exp.flagged(obd, "tile")) == _all_tiles_before,
      f"clearing drift left {len(exp.flagged(obd, 'tile'))} of "
      f"{_all_tiles_before} tile objects")
# Decision 4 is why this is not "the record drops": the object survives the
# drift clearing and so does the fix declared on it, because deleting either
# would throw away the artist's work without saying so.  What the artist gets
# instead is decision 12's refusal -- the tile is carried again, its
# declaration is no longer legal, and export says so rather than writing a
# document `build` would reject one record at a time.
_ddd, _, _ddrep = exp.assemble(obd)
check("drift_fix_survives_the_drift_clearing",
      au.is_declared(_h, "height")
      and _ddd["terrain"] == [{"x": 0, "z": 0, "level": 0, "height": 3}],
      f"the declaration was silently withdrawn: {_ddd['terrain']}")
check("drift_fix_is_refused_once_the_tile_is_carried_again",
      bool(refusals_mentioning(_ddrep, "still the base's", "(0, 0, L0)")),
      f"a fix on a tile that is no longer drifted exported clean: "
      f"{_ddrep.refusals[:3]}")
# ... and withdrawing it is what makes the document legal again.
au.undeclare(_h, "height")
check("drift_record_drops_when_the_fix_is_withdrawn",
      exp.assemble(obd)[0]["terrain"] is None
      and not exp.assemble(obd)[2].refusals,
      str(exp.assemble(obd)[0]["terrain"]))

# §6.5: no grid, no overlay -- and the checker must not throw on the way past.
_gd_obj = exp.flagged(obd, "grid")[0]
bpy.data.objects.remove(_gd_obj, do_unlink=True)
for _vi in _fverts:
    obd.data.vertices[_vi].co[2] = 36.0
check("drift_silent_without_a_grid", au.sync_drift(obd) == (0, 0),
      "the checker warned in an arrangement with no terrain chunk")
check("grid_absent_exports_null_terrain_grid",
      exp.assemble(obd)[0]["base"]["terrain_grid"] is None)

# ==================================================== the palette gate (block 2) ===
# Painting headless IS painting: a brush writes pixels into the paint image,
# and that is exactly what these checks do.  The resolve pass reads the same
# floats either way, so the gate is testable without a paint context.
from exmateria_map import paint as pnt

clear_scene()
run_import(staged)
obp = bpy.data.objects.get(name)
bpy.context.view_layer.objects.active = obp     # the state operator polls on it
_state = int(obp["exmateria_map/preview_state"])
_sheet = pnt.sheet_of_state(obp, _state)
check("paint_sheet_found", _sheet == list(_files)[0], str(_sheet))
_pal_state, _pal = pnt.active_palette(obp)
_entries = pnt.clut_entries(obp, _pal_state, _pal)
check("paint_active_palette_has_16", len(_entries) == 16, str(len(_entries)))
check("paint_entries_are_bytes",
      all(all(0 <= c <= 255 for c in e) for e in _entries), str(_entries[:3]))

_pimg = pnt.ensure_paint_image(obp, _sheet)
check("paint_image_created",
      _pimg is not None and tuple(_pimg.size) == (256, 1024), str(_pimg))
check("paint_image_is_non_color",
      _pimg.colorspace_settings.name == "Non-Color",
      f"{_pimg.colorspace_settings.name}; the gate is EXACT match, so a "
      f"colour-managed round trip would refuse every painted pixel")
_idx = pnt.index_image(obp, _sheet)
_buf0 = pnt.read_buffer(_idx)
check("paint_image_is_the_buffer_expanded",
      list(pnt._floats(_pimg)) == list(pnt.expand(_buf0, _entries)),
      "the paint image is not the buffer under the active palette")

# Nothing painted: the resolve must be a no-op, and the buffer must not move.
_r = pnt.resolve(obp)
check("paint_resolve_is_a_noop_when_clean",
      (_r["painted"], _r["resolved"], _r["off_palette"]) == (0, 0, 0), str(_r))
check("paint_clean_resolve_left_the_buffer",
      pnt.read_buffer(_idx) == _buf0, "a clean resolve moved the index buffer")
check("paint_clean_export_identity", not docdiff(doc, exp.assemble(obp)[0]),
      "a clean resolve moved the exported document")

# --- paint ONE pixel to a palette colour: it must become that INDEX ---------
# Pick an entry whose colour is not already at pixel 0, so the seed is not
# inert -- painting a pixel the colour it already is changes nothing to find.
_target = next((i for i, e in enumerate(_entries)
                if e != _entries[_buf0[0]]), None)
check("paint_seed_is_not_inert", _target is not None,
      "every entry of this CLUT is the colour pixel 0 already holds; the "
      "paint seed could not change anything")
_px = pnt._floats(_pimg)
for _k in range(3):
    _px[_k] = _entries[_target][_k] / 255.0
_pimg.pixels.foreach_set(_px)
_r = pnt.resolve(obp)
check("paint_resolved_one_pixel",
      (_r["painted"], _r["resolved"], _r["off_palette"]) == (1, 1, 0), str(_r))
check("paint_reached_the_index_buffer",
      pnt.read_buffer(_idx)[0] == _target,
      f"index {pnt.read_buffer(_idx)[0]}, expected {_target}")
check("paint_left_every_other_pixel",
      pnt.read_buffer(_idx)[1:] == _buf0[1:],
      "resolving one painted pixel moved another; §3.4 says an unchanged "
      "pixel keeps its import-time index")
# ... and it reaches the disc: a changed buffer re-hashes to a new sidecar.
_pdoc, _pfiles, _prep = exp.assemble(obp)
check("paint_moves_the_sidecar_name",
      list(_pfiles) and list(_pfiles)[0] != _sheet, str(list(_pfiles)))
check("paint_moves_the_document",
      _pdoc["map_states"] != doc["map_states"],
      "a repainted sheet did not reach `map_states[].texture_sheet`")
check("paint_export_still_clean", not _prep.refusals, str(_prep.refusals[:3]))

# --- §3.5 the lowest index wins on a duplicate ------------------------------
# Duplicate entries within one 16-set are legal, so the match rule has to be
# TOTAL.  Forge a duplicate in the CLUT image and check which index it picks.
_clut = bpy.data.images[json.loads(obp["exmateria_map/state_cluts"])[_pal_state]]
_cpx = pnt._floats(_clut)
_lo, _hi = min(_target, 3), max(_target, 3)
if _lo == _hi:
    _lo, _hi = 0, max(1, _target)
for _k in range(3):                       # entry _hi := entry _lo, exactly
    _cpx[(_pal * 16 + _hi) * 4 + _k] = _cpx[(_pal * 16 + _lo) * 4 + _k]
_clut.pixels.foreach_set(_cpx)
_dupe = pnt.clut_entries(obp, _pal_state, _pal)
check("paint_duplicate_entry_forged", _dupe[_lo] == _dupe[_hi] and _lo < _hi,
      f"{_dupe[_lo]} vs {_dupe[_hi]}")
_px = pnt._floats(_pimg)
for _k in range(3):
    _px[4 + _k] = _dupe[_hi][_k] / 255.0    # pixel 1, the duplicated colour
_pimg.pixels.foreach_set(_px)
_r = pnt.resolve(obp)
check("paint_lowest_index_wins",
      pnt.read_buffer(_idx)[1] == _lo,
      f"a duplicated colour resolved to {pnt.read_buffer(_idx)[1]}, not the "
      f"lowest matching index {_lo}")

# --- §3.6 off-palette is a REFUSAL, and it is sticky ------------------------
_off = (7, 11, 13)
check("paint_off_colour_is_really_off", tuple(_off) not in set(_dupe),
      "the 'off-palette' colour is in the palette; an inert seed")
_px = pnt._floats(_pimg)
for _k in range(3):
    _px[8 + _k] = _off[_k] / 255.0          # pixel 2
_pimg.pixels.foreach_set(_px)
_r = pnt.resolve(obp)
check("paint_off_palette_listed", _r["off_palette"] == 1, str(_r))
_entry = pnt.sticky(obp)[0]
check("paint_off_palette_entry_shape",
      _entry["color"] == "#070B0D" and _entry["count"] == 1
      and _entry["bbox"] == [2, 0, 2, 0],
      str(_entry))
check("paint_off_palette_not_written_to_the_buffer",
      pnt.read_buffer(_idx)[2] == _buf0[2],
      "an off-palette pixel changed the index buffer; it is a refusal, not a "
      "best-effort match")
_pd, _pf, _pr = exp.assemble(obp)
check("paint_off_palette_refuses_the_export",
      bool(refusals_mentioning(_pr, "off-palette", "#070B0D")),
      str(_pr.refusals[:3]))
# Sticky: a resolve that does not touch the pixel must NOT clear it (§4.4).
_r = pnt.resolve(obp)
check("paint_refusal_is_sticky", _r["off_palette"] == 1, str(_r))
check("paint_refusal_still_refuses",
      bool(refusals_mentioning(exp.assemble(obp)[2], "off-palette")))
# ... and repainting the pixel to a colour the palette accepts DOES clear it.
_px = pnt._floats(_pimg)
for _k in range(3):
    _px[8 + _k] = _dupe[_lo][_k] / 255.0
_pimg.pixels.foreach_set(_px)
_r = pnt.resolve(obp)
check("paint_repaint_clears_the_refusal",
      _r["off_palette"] == 0 and _r["cleared"] == 1, str(_r))
check("paint_export_unblocked", not exp.assemble(obp)[2].refusals,
      str(exp.assemble(obp)[2].refusals[:3]))

# --- the summary is auditable: no painted pixel is silently lost -----------
# ADR-0007's last consequence.  `off_palette` is the STANDING sticky total
# over every pass, `painted` counts this pass alone, so the one invariant
# worth having -- every painted pixel either resolved or was refused -- is
# not expressible from the two of them.  It needs a per-pass refusal count.
#
# Seed a pass that already has refusals STANDING from an earlier one, or the
# per-pass count and the standing total are equal by accident and the check
# proves nothing.
_bad = [(17, 19, 23), (29, 31, 37), (41, 43, 47), (53, 59, 61)]
check("audit_seed_colours_are_really_off",
      all(c not in set(_dupe) for c in _bad), str(_dupe))
_px = pnt._floats(_pimg)
for _n, _p in enumerate((5, 6)):
    for _k in range(3):
        _px[_p * 4 + _k] = _bad[_n][_k] / 255.0
_pimg.pixels.foreach_set(_px)
_r = pnt.resolve(obp)
check("audit_prior_pass_refused_two",
      (_r["painted"], _r["resolved"], _r["off_palette"]) == (2, 0, 2), str(_r))

# This pass paints TWO pixels: one colour the row holds, one it does not.
# The worked example, from the seed rather than from the code: painted 2,
# resolved 1, refused 1 -- while three refusals stand on the sticky list.
_good = next((e for e in _dupe if e != _dupe[_buf0[7]]), None)
check("audit_resolvable_seed_is_not_inert", _good is not None,
      "every entry of this CLUT is the colour pixel 7 already holds")
_px = pnt._floats(_pimg)
for _k in range(3):
    _px[7 * 4 + _k] = _good[_k] / 255.0        # pixel 7, the row holds it
    _px[8 * 4 + _k] = _bad[2][_k] / 255.0      # pixel 8, it does not
_pimg.pixels.foreach_set(_px)
_r = pnt.resolve(obp)
check("audit_this_pass_painted_two", _r["painted"] == 2, str(_r))
check("audit_this_pass_resolved_one", _r["resolved"] == 1, str(_r))
check("audit_refusal_count_is_per_pass", _r.get("refused") == 1,
      f"refused={_r.get('refused')!r}; expected the ONE colour THIS pass "
      f"refused, not the {_r['off_palette']} standing on the sticky list")
check("audit_off_palette_is_still_the_standing_total",
      _r["off_palette"] == 3,
      f"{_r}; `off_palette` is the cross-pass total and must stay that way -- "
      f"export reads the same section")
_ref = _r.get("refused")
check("audit_no_painted_pixel_is_lost",
      _ref is not None and _r["resolved"] + _ref == _r["painted"], str(_r))
check("audit_nothing_was_cleared", _r["cleared"] == 0, str(_r))
check("audit_nothing_was_recovered", _r.get("recovered") == 0,
      f"{_r}; a pass that only PAINTED must not report a palette recovery")

# A refusal counts PIXELS, not distinct colours.  Three pixels of ONE colour
# the row cannot hold are three painted pixels that did not resolve; folding
# them into one is the same silent loss in a different disguise, and a count
# of colours passes every check above it.
_px = pnt._floats(_pimg)
for _p in (9, 10, 11):
    for _k in range(3):
        _px[_p * 4 + _k] = _bad[3][_k] / 255.0
_pimg.pixels.foreach_set(_px)
_r = pnt.resolve(obp)
_ref = _r.get("refused")
check("audit_refusal_counts_pixels_not_colours", _ref == 3,
      f"refused={_ref!r} for three pixels of ONE off-palette colour; "
      f"expected 3")
check("audit_one_colour_pass_conserves",
      _ref is not None and _r["resolved"] + _ref == _r["painted"] == 3, str(_r))
check("audit_one_colour_pass_is_one_sticky_entry",
      _r["off_palette"] == 6
      and len([e for e in pnt.sticky(obp)
               if e.get("color") == "#353B3D"]) == 1,
      f"{_r}; {pnt.sticky(obp)}")

# A pass that paints NOTHING refuses nothing, however many refusals stand.
_r = pnt.resolve(obp)
check("audit_a_clean_pass_refuses_nothing",
      (_r["painted"], _r["resolved"], _r.get("refused")) == (0, 0, 0)
      and _r["off_palette"] == 6, str(_r))

# Painting MORE pixels in a colour already on the list merges into that
# entry rather than making a second one -- and the merged entry moves to the
# END of the list, which is the order export prints its refusal lines in.
# Two merges in one pass, from different positions, or a rewrite that only
# ever merges the head passes.
_px = pnt._floats(_pimg)
for _p, _n in ((12, 0), (13, 3)):
    for _k in range(3):
        _px[_p * 4 + _k] = _bad[_n][_k] / 255.0
_pimg.pixels.foreach_set(_px)
_r = pnt.resolve(obp)
check("audit_merge_pass_conserves",
      _r.get("refused") == _r["painted"] == 2 and _r["resolved"] == 0, str(_r))
check("audit_merge_did_not_make_a_second_entry",
      len(pnt.sticky(obp)) == 4 and _r["off_palette"] == 8,
      f"{_r}; {[e['color'] for e in pnt.sticky(obp)]}")
_merged = next(e for e in pnt.sticky(obp) if e["color"] == "#111317")
check("audit_merge_unions_the_pixels_and_the_bbox",
      (_merged["count"], _merged["pixels"], _merged["bbox"])
      == (2, [5, 12], [5, 0, 12, 0]), str(_merged))
check("audit_a_merged_entry_moves_to_the_end",
      [e["color"] for e in pnt.sticky(obp)]
      == ["#1D1F25", "#292B2F", "#111317", "#353B3D"],
      str([e["color"] for e in pnt.sticky(obp)]))

# Clearing is not a refusal either: repaint all six back onto the row.
_px = pnt._floats(_pimg)
for _p in (5, 6, 8, 9, 10, 11, 12, 13):
    for _k in range(3):
        _px[_p * 4 + _k] = _dupe[_buf0[_p]][_k] / 255.0
_pimg.pixels.foreach_set(_px)
_r = pnt.resolve(obp)
_ref = _r.get("refused")
check("audit_clearing_pass_conserves",
      _ref is not None and _r["resolved"] + _ref == _r["painted"] == 8,
      str(_r))
check("audit_clearing_pass_emptied_the_list",
      (_r["off_palette"], _r["cleared"], _r.get("refused")) == (0, 8, 0),
      str(_r))
check("audit_repainting_is_not_a_recovery",
      _r.get("recovered") == 0,
      f"{_r}; the diff loop already moved these indices -- counting them "
      f"again as recoveries would double-report the same event")
check("audit_export_unblocked_again", not exp.assemble(obp)[2].refusals,
      str(exp.assemble(obp)[2].refusals[:3]))

# --- authoring an entry TO a refused colour must RESOLVE the pixel --------
# The path a quantiser needs (ADR-0007 decision 1): it decides sixteen
# colours, writes them into the row, and the pixels that were refused for
# want of them have to land on the new indices.
#
# `_gate` drops a stored pixel whose colour the palette now accepts, but the
# INDEX is written in `resolve`'s diff loop, which is skipped when nothing was
# painted -- and authoring a CLUT entry paints nothing. So the refusal can
# clear while the index never moves: the sticky list goes quiet, export stops
# refusing, and the sheet ships the pixel's import-time colour.
_off2 = (65, 74, 82)
check("author_seed_is_really_off", tuple(_off2) not in set(_dupe), str(_dupe))
_px = pnt._floats(_pimg)
for _k in range(3):
    _px[14 * 4 + _k] = _off2[_k] / 255.0
_pimg.pixels.foreach_set(_px)
_r = pnt.resolve(obp)
check("author_pixel_starts_refused",
      (_r.get("refused"), _r["off_palette"]) == (1, 1), str(_r))
# The slot must NOT be the index pixel 14 already carries, or the check
# below is satisfied by an index that never moved -- which is exactly the
# failure it exists to catch.
_slot = next(i for i in range(16)
             if i not in (_lo, _hi, _target, pnt.read_buffer(_idx)[14]))
check("author_slot_is_not_inert", pnt.read_buffer(_idx)[14] != _slot,
      f"pixel 14 already carries index {_slot}")
_cpx = pnt._floats(_clut)
for _k in range(3):
    _cpx[(_pal * 16 + _slot) * 4 + _k] = _off2[_k] / 255.0
_clut.pixels.foreach_set(_cpx)
check("author_entry_took",
      pnt.clut_entries(obp, _pal_state, _pal)[_slot] == _off2,
      str(pnt.clut_entries(obp, _pal_state, _pal)[_slot]))
_r = pnt.resolve(obp)
check("author_clears_the_refusal",
      (_r["off_palette"], _r["cleared"]) == (0, 1), str(_r))
check("author_resolves_the_pixel_it_cleared",
      pnt.read_buffer(_idx)[14] == _slot,
      f"index {pnt.read_buffer(_idx)[14]}, expected {_slot}: the refusal "
      f"cleared but the index never moved, so the pixel ships its "
      f"import-time colour and the artist's paint is silently lost")
check("author_conserves",
      _r["resolved"] + _r.get("refused", -1) == _r["painted"] == 0, str(_r))
check("author_reports_the_recovery",
      _r.get("recovered") == 1,
      f"{_r}; the index moved on a pass that painted nothing, so it has to "
      f"be counted somewhere other than `resolved`")
check("author_export_is_clean", not exp.assemble(obp)[2].refusals,
      str(exp.assemble(obp)[2].refusals[:3]))
_dupe = pnt.clut_entries(obp, _pal_state, _pal)

# --- §4.1 a state change re-colours, and resolves against the OUTGOING set --
_before = list(pnt._floats(_pimg))
_other = next(k for k in range(len(json.loads(obp["exmateria_map/map_states"])))
              if k != _state)
_res = bpy.ops.exmateria_map.set_preview_state(state_index=_other)
check("paint_state_change_ran", _res == {"FINISHED"}, str(_res))
check("paint_state_change_recoloured",
      list(pnt._floats(_pimg)) != _before
      or pnt.clut_entries(obp, _other, _pal) == _dupe,
      "the paint image was not re-coloured under the incoming palette")
check("paint_state_change_remembers_the_palette",
      json.loads(obp["exmateria_map/paint_palette"])[_sheet][0] == _other,
      str(obp.get("exmateria_map/paint_palette")))
_buf_before = pnt.read_buffer(_idx)
_res = bpy.ops.exmateria_map.set_preview_state(state_index=_state)
check("paint_state_change_is_lossless",
      pnt.read_buffer(_idx) == _buf_before,
      "switching states and back moved the index buffer")

# --- §3.2 the override picks the palette, and it is a trigger --------------
obp.exmateria_map_palette_override = 4
check("paint_override_picks_the_palette",
      pnt.active_palette(obp) == (_state, 4), str(pnt.active_palette(obp)))
obp.exmateria_map_palette_override = -1
check("paint_override_released",
      pnt.active_palette(obp)[1] == _pal, str(pnt.active_palette(obp)))

# --- §3.3 export is itself a trigger ---------------------------------------
# Paint off-palette and go STRAIGHT to `assemble`, with no resolve in between.
# Without export resolving first, the gate passes on a sheet the artist has
# already broken — the sticky list is only ever as current as its last resolve.
_px = pnt._floats(_pimg)
for _k in range(3):
    _px[12 + _k] = (3, 5, 9)[_k] / 255.0      # pixel 3
_pimg.pixels.foreach_set(_px)
check("paint_export_resolves_before_it_gates",
      bool(refusals_mentioning(exp.assemble(obp)[2], "off-palette", "#030509")),
      "a pixel painted since the last resolve reached export ungated")
_px = pnt._floats(_pimg)
for _k in range(3):
    _px[12 + _k] = _dupe[_lo][_k] / 255.0
_pimg.pixels.foreach_set(_px)
check("paint_export_trigger_clears_too",
      not exp.assemble(obp)[2].refusals,
      str(exp.assemble(obp)[2].refusals[:3]))

# --- the operators ---------------------------------------------------------
_res = bpy.ops.exmateria_map.apply_paint()
check("apply_paint_finished", _res == {"FINISHED"}, str(_res))
try:
    _res = bpy.ops.exmateria_map.paint_sheet()
except RuntimeError as e:
    _res = {"CANCELLED": str(e)}
check("paint_sheet_finished", _res == {"FINISHED"}, str(_res))

# --- the active palette follows the SELECTED FACE, in BOTH modes ------------
# How an artist learns which of the 16 CLUT rows a wall uses: select it and read
# the panel.  In Edit Mode -- the only mode that can select a face --
# `me.attributes["palette_id"].data` is EMPTY, so the old read raised IndexError
# inside a panel `draw`, which means the panel does not render at all.  Same
# species as `02b99279a`, in the surface used to CHOOSE the face.
import bmesh as _bmesh

_pal_attr = obp.data.attributes["palette_id"].data
_want = [(0, 11), (1, 4)][:len(obp.data.polygons)]
for _i, _v in _want:
    _pal_attr[_i].value = _v
bpy.context.view_layer.objects.active = obp
obp.select_set(True)

_seen = []
for _i, _v in _want:
    bpy.ops.object.mode_set(mode="EDIT")
    _bm = _bmesh.from_edit_mesh(obp.data)
    _bm.faces.ensure_lookup_table()
    for _f in _bm.faces:
        _f.select = False
    _bm.faces[_i].select = True
    _bm.faces.active = _bm.faces[_i]
    _bmesh.update_edit_mesh(obp.data)
    check(f"paint_attr_data_is_empty_in_edit_mode_{_i}",
          len(obp.data.attributes["palette_id"].data) == 0,
          "if this goes green-by-accident the defect below is unreachable and "
          "the check no longer states anything")
    try:
        _edit = pnt.active_palette(obp)
    except Exception as _e:
        _edit = f"{type(_e).__name__}: {_e}"
    bpy.ops.object.mode_set(mode="OBJECT")
    _obj = pnt.active_palette(obp)
    _seen.append((_i, _v, _edit, _obj))
    check(f"paint_active_palette_in_edit_mode_face_{_i}",
          isinstance(_edit, tuple) and _edit[1] == _v,
          f"selected face {_i} carries palette_id {_v}; Edit Mode said {_edit}")
    check(f"paint_active_palette_agrees_across_modes_face_{_i}",
          _edit == _obj,
          f"Edit {_edit} vs Object {_obj} -- the panel must not change its "
          f"answer when the artist tabs")

# --- the 16 legal colours reach Blender's own colour shelf ------------------
# The gate is an exact byte match and nothing ever showed the artist WHAT the
# legal colours are, so every colour was chosen by eye against a gate that
# accepts only a perfect hit.  #423 is right that a Palette cannot LOCK the
# artist to the row -- the gate does that -- but the shelf says what yes looks
# like, and it was never built.
_shelf = bpy.data.palettes.get(pnt.PALETTE_SHELF)
_ip_t = bpy.context.scene.tool_settings.image_paint
_st_s, _pal_s = pnt.active_palette(obp)
_entries_s = pnt.clut_entries(obp, _st_s, _pal_s)
check("paint_shelf_exists_after_paint_sheet",
      _shelf is not None and len(_shelf.colors) == len(_entries_s),
      f"{_shelf and len(_shelf.colors)} swatches vs {len(_entries_s)} entries")
check("paint_shelf_is_bound_to_the_brush",
      _ip_t.palette is _shelf, str(_ip_t.palette))
# EXACT, not near: a swatch that misses by one unit is refused by the gate, so
# "close" is the same as broken here.
check("paint_shelf_colours_are_byte_exact",
      [tuple(round(c * 255.0) for c in sl.color) for sl in _shelf.colors]
      == _entries_s,
      "a PaletteColor that drifts by one unit hands the brush a colour the "
      "gate refuses, which is indistinguishable from having no shelf")
# ...and it must follow the face, or it names the wrong row's colours -- worse
# than nothing, because it looks authoritative.
_other = next(((i, v) for i, v in _want if v != _pal_s), None)
if _other is not None:
    _oi, _ov = _other
    bpy.ops.object.mode_set(mode="EDIT")
    _bm = _bmesh.from_edit_mesh(obp.data)
    _bm.faces.ensure_lookup_table()
    for _f in _bm.faces:
        _f.select = False
    _bm.faces[_oi].select = True
    _bm.faces.active = _bm.faces[_oi]
    _bmesh.update_edit_mesh(obp.data)
    bpy.context.view_layer.update()
    check("paint_shelf_follows_the_clicked_face",
          [tuple(round(c * 255.0) for c in sl.color)
           for sl in bpy.data.palettes[pnt.PALETTE_SHELF].colors]
          == pnt.clut_entries(obp, _st_s, _ov),
          f"clicked a face on row {_ov}; the shelf still holds row {_pal_s}")
    bpy.ops.object.mode_set(mode="OBJECT")
else:
    check("paint_shelf_follows_the_clicked_face", False,
          "no second row in the fixture -- this arm cannot run and must not "
          "read as a pass")

# --- and the PAINT IMAGE re-colours to the face you clicked ----------------
# §3.3's face-select trigger is the point of all of the above: click a wall and
# edit in the palette that wall actually reads.  It watched
# `me.polygons.active`, which FREEZES in Edit Mode, so it could never fire in
# the only mode that can select a face.
_sheet_t = pnt.sheet_of_state(obp, int(obp.get("exmateria_map/preview_state") or 0))
_pimg_t = pnt.ensure_paint_image(obp, _sheet_t)
_idx_t = pnt.index_image(obp, _sheet_t)
_frozen = []
for _i, _v in _want:
    bpy.ops.object.mode_set(mode="EDIT")
    _bm = _bmesh.from_edit_mesh(obp.data)
    _bm.faces.ensure_lookup_table()
    for _f in _bm.faces:
        _f.select = False
    _bm.faces[_i].select = True
    _bm.faces.active = _bm.faces[_i]
    _bmesh.update_edit_mesh(obp.data)
    _frozen.append(obp.data.polygons.active)
    check(f"paint_active_face_index_tracks_in_edit_mode_{_i}",
          pnt.active_face_index(obp) == _i,
          f"clicked face {_i}, active_face_index said "
          f"{pnt.active_face_index(obp)}")
    bpy.context.view_layer.update()          # runs the scene-update handlers
    _st_t, _pal_t = pnt.active_palette(obp)
    _want_px = list(pnt.expand(pnt.read_buffer(_idx_t),
                               pnt.clut_entries(obp, _st_t, _pal_t)))
    check(f"paint_image_recolours_to_the_clicked_face_{_i}",
          list(pnt._floats(_pimg_t)) == _want_px,
          f"face {_i} reads CLUT row {_v}; the paint image is not that row's "
          f"expansion, so the artist edits in the WRONG palette's colours")
    bpy.ops.object.mode_set(mode="OBJECT")

# The seed's own precondition: `me.polygons.active` does NOT name the face the
# artist just clicked.  Stated as disagreement rather than as a specific wrong
# value, because the wrong value has two observed shapes -- FROZEN at 0 across
# 454 faces on MAP022 a0, and STALE BY ONE on this 2-face fixture ([1, 0] for
# clicks on [0, 1]).  A guard written to either shape would go green on the
# other while the defect stood.  If Blender ever starts syncing it, this check
# says so rather than the arms above passing for a reason that no longer holds.
check("paint_polygons_active_disagrees_in_edit_mode",
      _frozen != [i for i, _ in _want],
      f"me.polygons.active read {_frozen} for clicks on "
      f"{[i for i, _ in _want]} -- if it now tracks, `active_face_index`'s "
      f"Edit branch is no longer load-bearing")

# The two faces must actually DIFFER, or one constant would satisfy both arms.
check("paint_active_palette_seed_faces_differ",
      len({v for _, v, _, _ in _seen}) == len(_seen),
      str([(i, v) for i, v, _, _ in _seen]))

# A panel whose draw raises does not render; drive the real draw in Edit Mode.
bpy.ops.object.mode_set(mode="EDIT")
_bm = _bmesh.from_edit_mesh(obp.data)
_bm.faces.ensure_lookup_table()
for _f in _bm.faces:
    _f.select = False
_bmesh.update_edit_mesh(obp.data)
try:
    # A real panel context always carries `scene`; the shim must too, or the
    # check fails for a reason the artist can never hit.
    _props_before = len(_props)
    _drew = pnt.MAP_PT_paint.draw(
        _panel_shim(pnt.MAP_PT_paint, _FakeLayout([])),
        type("C", (), {"object": obp, "scene": bpy.context.scene})()) or True
except Exception as _e:
    _drew = f"{type(_e).__name__}: {_e}"
bpy.ops.object.mode_set(mode="OBJECT")
check("paint_panel_draws_in_edit_mode_with_nothing_selected",
      _drew is True, str(_drew))
# `draw` surviving is not the claim -- the 16 swatches reaching the panel is.
# Counted over THIS draw only: `_props` is shared with every other panel the
# harness drives, so a global count would be satisfied by somebody else's.
_swatches = _props[_props_before:].count("color")
check("paint_panel_draws_all_16_swatches",
      _swatches == len(_entries_s),
      f"{_swatches} colour swatches drawn, expected {len(_entries_s)} -- a "
      f"panel that survives its draw while showing nothing is, to the artist, "
      f"the same as one that crashed")

# --- the button reaches Image Editors in OTHER workspaces -------------------
# The defect: the walk read `context.screen` and stopped at the first hit, so
# an artist on Layout -- which has no Image Editor -- pressed the button, saw
# nothing happen, and was told the image was "open".  Blender's own
# `CLIP_spaces_walk` walks `bpy.data.screens`; this asserts we take that arm.
_here = sum(1 for _a in bpy.context.window.screen.areas
            if _a.type == "IMAGE_EDITOR")
_all = pnt.image_editor_spaces()
check("paint_walk_reaches_other_workspaces",
      _here == 0 and len(_all) >= 4,
      f"the pressed-from screen '{bpy.context.window.screen.name}' has {_here} "
      f"Image Editor(s); the walk reaches {len(_all)}. If _here is nonzero the "
      f"startup changed and this check no longer states the defect")
for _sp, _ in _all:
    _sp.image = None
_shown, _skipped = pnt.show_in_image_editors(_pimg)
check("paint_sheet_fills_every_free_editor",
      (_shown, _skipped) == (len(_all), 0)
      and all(_sp.image is _pimg for _sp, _ in pnt.image_editor_spaces()),
      f"shown={_shown} skipped={_skipped} of {len(_all)}")

# A render result is not the artist's file and must not be displaced.  Real
# RENDER_RESULT images cannot be minted from Python, so the guard is seeded on
# its own predicate -- and restored, with an arm that proves the restore, so a
# guard stuck permanently on cannot read as a pass (an INERT seed).
_saved = pnt.PROTECTED_IMAGE_TYPES
pnt.PROTECTED_IMAGE_TYPES = {"IMAGE"}
_s2, _k2 = pnt.show_in_image_editors(_pimg)
check("paint_sheet_skips_protected_editors",
      (_s2, _k2) == (0, len(_all)), f"shown={_s2} skipped={_k2}")
pnt.PROTECTED_IMAGE_TYPES = _saved
_s3, _ = pnt.show_in_image_editors(_pimg)
check("paint_sheet_protected_seed_was_not_inert", _s3 == len(_all), str(_s3))

# With nowhere to show it: WARNING, no claim that anything opened, and the hint
# that says how to open one.
_types = [(_sc, _a, _a.type) for _sc in bpy.data.screens for _a in _sc.areas
          if _a.type == "IMAGE_EDITOR"]
for _sc, _a, _t in _types:
    _a.type = "VIEW_3D"
check("paint_no_editors_left", not pnt.image_editor_spaces(),
      str(len(pnt.image_editor_spaces())))


class _ReportShim:                  # execute() only ever uses self.report
    def __init__(self):
        self.reports = []

    def report(self, level, msg):
        self.reports.append((level, msg))


_shim = _ReportShim()
_res = pnt.MAP_OT_paint_sheet.execute(_shim, bpy.context)
_lvl, _msg = _shim.reports[-1]
check("paint_sheet_finished_with_no_editor", _res == {"FINISHED"}, str(_res))
check("paint_sheet_warns_when_no_editor", _lvl == {"WARNING"}, str(_lvl))
check("paint_sheet_does_not_claim_it_opened",
      "NO Image Editor is open" in _msg,
      f"the report must not advertise an editor this button never opens: {_msg}")
check("paint_sheet_hint_says_how_to_open_one",
      all(_w in _msg for _w in ("UV Editing", "Texture Paint", "Image Editor")),
      _msg)
for _sc, _a, _t in _types:          # put the artist's screens back
    _a.type = _t
check("paint_editors_restored", len(pnt.image_editor_spaces()) == len(_all),
      str(len(pnt.image_editor_spaces())))

# --- recolouring a CLUT entry the sheet USES must not refuse the export -----
# This is the ordering trap of the palette-authoring leg, and it only fires on
# the SECOND session with a file.
#
# `resolve` decides what was painted by diffing the paint image against
# `_CACHE` -- what this module last wrote.  The cache is per-process, so a
# reopened `.blend` has none and `resolve` rebuilds the baseline as
# `expand(buffer, entries)` under the entries in force NOW.  Recolour an entry
# and that rebuilt baseline is the NEW colour while the paint image still shows
# the OLD one, so every pixel holding that index reads as freshly painted, in a
# colour the CLUT no longer contains: the export refuses a sheet the artist
# never touched, and refuses it for the one gesture this whole leg exists for.
#
# The buffer arm is the sharper half.  If the old colour happens to still match
# some OTHER entry, the pixels do not refuse -- they silently re-index to it,
# which is data loss rather than a failed export.
_out_state, _out_pal = pnt.outgoing_palette(obp, _sheet)
check("clut_edit_state_carries_palettes",
      json.loads(obp["exmateria_map/map_states"])[_out_state]["palettes"],
      f"map state {_out_state} carries no palettes, so recolouring its CLUT "
      f"image tests nothing about the document")
_cur = pnt.clut_entries(obp, _out_state, _out_pal)
_buf_now = pnt.read_buffer(_idx)
_USED = _buf_now[0]
_NEWC = (0xFF, 0x00, 0xFF)
check("clut_edit_seed_is_not_inert",
      tuple(_NEWC) not in set(_cur) and _buf_now.count(_USED) > 0,
      f"{_NEWC} is already among this row's sixteen, or no pixel uses entry "
      f"{_USED}: nothing would change")
_clut_img = bpy.data.images[
    json.loads(obp["exmateria_map/state_cluts"])[_out_state]]
_cpx2 = pnt._floats(_clut_img)
for _k in range(3):
    _cpx2[(_out_pal * 16 + _USED) * 4 + _k] = _NEWC[_k] / 255.0
_clut_img.pixels.foreach_set(_cpx2)
pnt._CACHE.clear()          # exactly what reopening the .blend leaves behind
_cd, _cf, _crep = exp.assemble(obp)
check("clut_edit_does_not_refuse_the_export",
      not _crep.refusals,
      f"recolouring CLUT entry {_USED}, which {_buf_now.count(_USED)} pixels "
      f"use, refused the export: {_crep.refusals[:3]}")
check("clut_edit_does_not_move_the_index_buffer",
      pnt.read_buffer(_idx) == _buf_now,
      "recolouring a CLUT entry re-indexed the sheet: a colour edit moves the "
      "palette, never which entry a pixel points at")
check("clut_edit_recolours_the_paint_image",
      tuple(int(round(pnt._floats(_pimg)[_k] * 255.0)) for _k in range(3))
      == _NEWC,
      f"the paint image still shows the colour entry {_USED} used to hold, so "
      f"the artist's canvas disagrees with the palette they just authored")
check("clut_edit_reaches_the_document",
      _cd["map_states"][_out_state]["palettes"][_out_pal]["colors"][_USED]
      == "#FF00FF",
      str(_cd["map_states"][_out_state]["palettes"][_out_pal]["colors"][_USED]))

from exmateria_map import lighting_bake as mod_bake

# --- Lamp authority (decision 30), IN A LIT SCENE ---------------------------
# This block exists because the harness could not see the defect it was written
# for. `clear_scene()` deletes the startup Light, and `bake_normals` used to
# return early with no lamps -- so a solve fired by an unprimed import, or by a
# state change, wrote nothing and every check stayed green while the shipped
# addon ate an artist's ROM normals on their very first import. A real session
# HAS a lamp: the default scene ships one, and the whole feature is about adding
# more.
#
# Every "must not fire" leg below is paired with a leg that MAKES it fire, or a
# handler that never runs at all would satisfy the lot.
clear_scene()
_sun = bpy.data.objects.new("live_probe_sun", bpy.data.lights.new("live_probe", "SUN"))
bpy.context.scene.collection.objects.link(_sun)
_sun.rotation_euler = (0.6, 0.2, 1.1)
bpy.context.view_layer.update()
_res = run_import(staged)
_lit = next((o for o in bpy.data.objects if "exmateria_map/map_states" in o), None)
check("live_probe_imported", _lit is not None and "FINISHED" in _res, str(_res))
if _lit is not None:
    bpy.context.view_layer.objects.active = _lit
    _me = _lit.data

    def _live_normals():
        return [tuple(d.vector) for d in _me.attributes["normals"].data]

    _doc_normals = []
    _orders = [mod.import_order(len(p["positions"]), mod._wound_against(p))
               for p in polys]
    for _i, _p in enumerate(polys):
        for _slot, _li in enumerate(_me.polygons[_i].loop_indices):
            _c = _orders[_i][_slot]
            _doc_normals.append(
                mod._fft_to_blender(tuple(_p["normals"][_c]))
                if _p["kind"] in mod.TEXTURED_KINDS else (0.0, 0.0, 0.0))
    bpy.context.view_layer.update()

    # IMPORT LANDS OFF.  Not "the solve happened to write nothing" -- the switch
    # itself, read off the object, because that is the rule decision 30 states
    # and the one `prime_live` used to be needed for.
    check("import_lands_authority_off",
          _lit.exmateria_map_lamp_authority is False,
          f"authority={_lit.exmateria_map_lamp_authority!r} on a fresh import")
    check("import_does_not_solve",
          _live_normals() == _doc_normals,
          "importing into a lit scene re-solved the ROM normals")

    # ...and the lamp is IN SCOPE, so "nothing moved" is the switch's doing and
    # not the scope rule's.  Moving it while authority is off must still do
    # nothing.
    bpy.context.scene.collection.objects.unlink(_sun)
    _lit.users_collection[0].objects.link(_sun)
    _sun.rotation_euler = (1.3, 0.9, 0.2)
    bpy.context.view_layer.update()
    check("authority_off_ignores_an_in_scope_lamp",
          _live_normals() == _doc_normals,
          "a lamp inside the map's collection re-solved with authority OFF")

    # The switch is what makes it fire.  Without this leg every check above is
    # satisfied by a handler that is simply not registered.
    _lit.exmateria_map_lamp_authority = True
    check("authority_on_solves",
          _live_normals() != _doc_normals,
          "authority went ON with an in-scope lamp and nothing re-solved")
    _solved = _live_normals()

    # A VIEW change must not re-solve.  One normal set serves every state's
    # picture, so a re-solve re-shades all of them -- exactly the silent move
    # decision 27's "every state a bake touched is NAMED" exists to forbid.
    # Only gradeable with authority ON, which is why it lives after that leg.
    #
    # Counted as WORK, not as an output diff.  A solve is a pure function of the
    # lamps, so a handler that DOES re-solve on a state change writes exactly the
    # normals that were already there and an output check reads green -- which is
    # why `live_bake_fires_on_state_change` was a BLIND seed in
    # `export_mutation_audit.py` for as long as it existed.  The counter is read
    # out of the REGISTERED handler's globals: `addon_install` + `import` yield
    # two module objects, so `mod_bake._LIVE_RUNS` is a counter nobody increments.
    def _runs():
        for h in bpy.app.handlers.depsgraph_update_post:
            if getattr(h, "__name__", "") == "_live_handler":
                return h.__globals__.get("_LIVE_RUNS")
        return None

    _r0 = _runs()
    check("solve_counter_is_readable", _r0 is not None,
          "no registered _live_handler to read a run count from")
    bpy.ops.exmateria_map.set_preview_state(state_index=2)
    bpy.context.view_layer.update()
    _r1 = _runs()
    check("a_state_change_does_not_solve",
          _r0 is not None and _r1 == _r0,
          f"looking at another state re-solved the normals ({_r0} -> {_r1})")
    check("a_state_change_moved_no_normals",
          _live_normals() == _solved,
          "looking at another state changed the normals")
    # ...and the counter CAN move here, or "it did not move" is a claim about a
    # counter that is simply stuck.
    _sun.rotation_euler = (0.95, 0.15, 1.25)
    bpy.context.view_layer.update()
    check("the_solve_counter_can_move",
          _runs() is not None and _r1 is not None and _runs() > _r1,
          f"a lamp moved and the run count stayed at {_r1}")
    _solved = _live_normals()

    # OFF COMMITS.  The flip writes nothing, and the lamp it leaves behind
    # writes nothing either.
    _lit.exmateria_map_lamp_authority = False
    check("authority_off_commits",
          _live_normals() == _solved,
          "switching authority off reverted the normals instead of committing")
    _sun.rotation_euler = (0.4, 1.5, 0.8)
    bpy.context.view_layer.update()
    check("authority_off_is_silent",
          _live_normals() == _solved,
          "a lamp moved with authority OFF and the normals moved with it")

    # THE BADGE, both arms, on the normals axis.  It keys on DIVERGENCE and
    # never on the switch -- a badge tied to authority would go silent exactly
    # where edited normals now live, which is here: off, with a solve committed.
    check("badge_reports_edited_normals",
          _lit in mod.edited_objects(type("_C", (), {"visible_objects": [_lit]})()),
          "a committed solve with authority OFF carries no badge")
    _badge = mod.badge_text([_lit])
    check("badge_names_the_normals_axis", "normals" in _badge, _badge)
    check("badge_does_not_name_a_rig_it_has_no_override_for",
          "light rig" not in _badge, _badge)

    # RESTORE, over IMPORTED faces only.  A face the artist CREATED has a blank
    # `normals_shadow`, so restoring one would ZERO it rather than reset it --
    # the case that killed "off reverts to the ROM" as a design.
    import bmesh as _bm
    _bmesh = _bm.new()
    _bmesh.from_mesh(_me)
    _bmesh.verts.ensure_lookup_table()
    _bm.ops.create_grid(_bmesh, x_segments=1, y_segments=1, size=4.0)
    _bmesh.to_mesh(_me)
    _bmesh.free()
    _me.update()
    _new_face = len(_me.polygons) - 1
    _new_span = list(_me.polygons[_new_face].loop_indices)
    _nrm_attr = _me.attributes["normals"].data
    for _li in _new_span:
        _nrm_attr[_li].vector = (0.0, 0.0, 4096.0)
    _made = [tuple(_nrm_attr[_li].vector) for _li in _new_span]
    check("created_face_is_not_imported",
          not _me.attributes["imported"].data[_new_face].value)

    bpy.context.view_layer.objects.active = _lit
    _res = bpy.ops.map.restore_imported_normals()
    check("restore_ran", _res == {"FINISHED"}, f"res={_res}")
    check("restore_returns_the_document_normals",
          [tuple(d.vector) for d in _me.attributes["normals"].data][:len(_doc_normals)]
          == _doc_normals,
          "restore did not put the document's own normals back")
    check("restore_spares_a_created_face",
          [tuple(_me.attributes["normals"].data[_li].vector) for _li in _new_span]
          == _made,
          "restore ZEROED the normals on a face the artist created")
    check("restore_clears_the_badge",
          not mod.edited_objects(type("_C", (), {"visible_objects": [_lit]})()),
          "the badge survived a restore")

    # RE-IMPORT SPARES WHAT THE ARTIST MADE.  Scoping lamps into the map's
    # collection would otherwise mean re-importing destroys the lighting work:
    # `_remove_collection` removed EVERY object in it.  The rule is now the same
    # one export uses -- an `exmateria_map/*` FLAG is what the addon owns.
    # Both arms, one setup: two lamps in the same collection differing ONLY in
    # whether they carry the flag.  Arm 1 alone would be satisfied by a
    # `_remove_collection` that stopped removing anything at all.
    _torch = bpy.data.objects.new("artist_torch", bpy.data.lights.new("torch", "POINT"))
    _lit.users_collection[0].objects.link(_torch)
    _owned_lamp = bpy.data.objects.new("addon_owned_lamp",
                                       bpy.data.lights.new("owned", "POINT"))
    _owned_lamp[mod_bake.LAMP_TAG] = 0
    _lit.users_collection[0].objects.link(_owned_lamp)
    _res = run_import(staged)
    check("reimport_ran", "FINISHED" in _res, str(_res))
    _fresh = next((o for o in bpy.data.objects if "exmateria_map/map_states" in o), None)
    _survivor = bpy.data.objects.get("artist_torch")
    check("reimport_spares_the_artists_lamp",
          _survivor is not None,
          "re-import deleted a lamp the artist added to the map's collection")
    # ...and it must land back INSIDE the rebuilt collection.  Left at the scene
    # root it survives the re-import and is out of scope for every solve after
    # it -- the same work lost, one step later.
    check("the_spared_lamp_is_still_in_scope",
          _survivor is not None and _fresh is not None
          and _survivor.name in [o.name for o in _fresh.users_collection[0].objects],
          "the spared lamp is no longer in the map's collection")
    check("reimport_destroys_what_the_addon_made",
          bpy.data.objects.get("addon_owned_lamp") is None,
          "re-import spared an object carrying an `exmateria_map/*` flag")

# --- the Map workspace (ADR-0185 decision 1, as amended) --------------------
# WHAT THIS CAN AND CANNOT SEE.  In `-b` the screen never lays out and the
# timers never tick, so the PANES are not gradeable here at all and this file
# deliberately does not try: `area.type` is readable in background mode and is
# the one field that LIES -- assigning it on a screen no window is showing
# records the type and never swaps `spaces.active`, so an area that draws as a
# 3D viewport reports `TEXT_EDITOR` forever.  A check over it would be green on
# exactly the defect that shipped first.  The layout is graded headful, by
# `workspace/workspace_probe.py` phase `build`.  What IS gradeable here is the
# offer: the operator exists, runs, makes one workspace, and does not make a
# second one on a second press.
from exmateria_map import workspace as mod_ws

# `bpy.ops` resolves lazily, so `hasattr(bpy.ops.exmateria_map, "...")` is True
# for any name ever spelled -- probe the RNA type instead (addon CLAUDE.md).
check("workspace_operator_registered",
      getattr(bpy.types, "EXMATERIA_MAP_OT_add_workspace", None) is not None,
      "MAP_OT_add_workspace is not registered")
# The harness has already imported a document by now, and under Amendment 2
# that import OFFERS the workspace -- so its presence here is the hook's own
# receipt, not a leftover. (It used to be checked absent; that premise died
# with the decision, and the check is inverted rather than deleted.)
_ws_before = {w.name for w in bpy.data.workspaces}
check("import_left_the_workspace_behind",
      mod_ws.WORKSPACE_NAME in _ws_before, str(sorted(_ws_before)))
# Guarded: an unregistered operator raises, and an abort here would take the
# whole report with it -- a hard "no report written" reads as a broken harness
# rather than as the one check that is actually red.
try:
    _res = bpy.ops.exmateria_map.add_workspace()
except Exception as e:
    _res = {"raised": repr(e)}
check("workspace_button_ran", "FINISHED" in _res, str(_res))
check("workspace_named_after_the_tab",
      mod_ws.WORKSPACE_NAME in bpy.data.workspaces,
      str(sorted(w.name for w in bpy.data.workspaces)))
# Pressing it when the workspace is already there must switch, never build --
# rebuilding would re-split a screen the artist has since arranged.
check("workspace_button_on_an_existing_one_adds_nothing",
      {w.name for w in bpy.data.workspaces} == _ws_before,
      f"{sorted(_ws_before)} -> "
      f"{sorted(w.name for w in bpy.data.workspaces)}")
# ...and `build` itself, under a name no import can have taken. It is a
# DUPLICATE, not an add: `workspace.add` is PASS_THROUGH in every mode, which
# is what the ADR's rejected alternative was measured on.
_before_build = {w.name for w in bpy.data.workspaces}
_made = mod_ws.build(mod_ws._main_window(), name="_check_map_workspace")
check("build_returns_the_workspace_it_made",
      _made is not None and _made.name == "_check_map_workspace",
      repr(getattr(_made, "name", _made)))
check("build_adds_exactly_one_workspace",
      len(bpy.data.workspaces) == len(_before_build) + 1,
      f"{len(_before_build)} -> {len(bpy.data.workspaces)}")
# Pressing it twice is switching, not building: an artist who already has the
# workspace must not end up with `Map.001` and a re-split screen.
try:
    _res2 = bpy.ops.exmateria_map.add_workspace()
except Exception as e:
    _res2 = {"raised": repr(e)}
check("workspace_second_press_ran", "FINISHED" in _res2, str(_res2))
check("workspace_second_press_makes_no_duplicate",
      f"{mod_ws.WORKSPACE_NAME}.001" not in bpy.data.workspaces,
      str(sorted(w.name for w in bpy.data.workspaces)))
# `focus_tab` runs on every click, including today, when no panel anywhere
# carries `bl_category = "Map"` in a sidebar.  Refusing must be silent: the
# property is an enum over the categories that exist, so it RAISES rather than
# returning a falsy value, and an unguarded assignment would break the button
# for everyone until decision 3 lands.
_ws = bpy.data.workspaces[mod_ws.WORKSPACE_NAME]
try:
    _focused = mod_ws.focus_tab(_ws.screens[0])
    check("focus_tab_survives_having_no_Map_tab", True)
except Exception as e:
    check("focus_tab_survives_having_no_Map_tab", False, repr(e))
    _focused = None
check("focus_tab_claims_nothing_it_did_not_set", _focused == [], str(_focused))
# The offer has to be somewhere the artist can reach with no map open, which
# is what rules out every panel in the addon: all six poll on a marker.
_prefs_ops, _prefs_props = [], []


class _PrefsLayout:
    """Records what the preferences `draw` EMITS.

    The `__getattr__` fallback is not optional, and this arm has already been
    red for the want of it: `a8d9e7668` gave the preferences a `layout.box()`
    of PCSX launch instructions, and a recorder that raises on an unknown
    widget turned an unrelated feature into a red arm about the prefs panel.
    `UILayout` is wide and a panel grows widgets.  `blender_convert.py`'s
    `FakeLayout` carries the same fallback for the same reason.

    A raising `draw` renders everything before it and nothing after, so an
    incomplete recorder reads exactly like the panel having stopped drawing --
    which is the failure this fallback exists to keep distinguishable.
    """

    def operator(self, idname, **kw):
        _prefs_ops.append(idname)
        return self

    def prop(self, _data=None, _name=None, *a, **kw):
        _prefs_props.append(_name)
        return self

    def row(self, **kw):
        return self

    def __getattr__(self, name):
        def sub(*a, **kw):
            return self
        return sub


class _PrefsSelf:
    """`self` for the preferences `draw`: a recording layout over the REAL
    preferences.

    It used to be a bare object carrying nothing but `layout`, on the
    reasoning that `draw` reads `self.layout`.  That stopped being true at
    `a8d9e7668`, where `draw` grew a PCSX launch line built from
    `self.live_port` -- and the arm went red naming the prefs panel for a
    change that was about the live link.  Delegating is what keeps the arm
    pointed at whether `draw` RUNS: the panel reads its own settings, so it
    is handed its own settings, and only the layout is faked.
    """

    def __init__(self, prefs, layout):
        self.layout = layout
        self._prefs = prefs

    def __getattr__(self, name):
        return getattr(self._prefs, name)


try:
    _pf = bpy.context.preferences.addons["exmateria_map"].preferences
    type(_pf).draw(_PrefsSelf(_pf, _PrefsLayout()), None)
    check("prefs_draw_ran", True)
except Exception as e:
    check("prefs_draw_ran", False, repr(e))
check("prefs_offers_the_workspace_button",
      "exmateria_map.add_workspace" in _prefs_ops, str(_prefs_ops))
# ...and in File > Import, which is the door an artist actually finds.  The
# preferences copy is behind a disclosure triangle in a window that is not even
# the one the layout lands in.  Reported from use: "do I have to do this
# ceremony every time?"  Same operator, so two doors and one behaviour.
_menu_ops = []


class _MenuLayout:
    def operator(self, idname, **kw):
        _menu_ops.append(idname)
        return self

    def separator(self, **kw):
        return self


mod_ws.menu_func(type("_S", (), {"layout": _MenuLayout()})(), bpy.context)
check("file_import_menu_offers_the_workspace",
      "exmateria_map.add_workspace" in _menu_ops, str(_menu_ops))
# `_draw_funcs` hangs off the DISPATCHER, not the class: `append()` replaces
# `cls.draw` with `draw_ls` and the list lives on that (addon CLAUDE.md,
# "Menu wiring").  `hasattr(cls, "_draw_funcs")` is False and reads as "the
# menu entry is missing" when it is there.
check("workspace_menu_func_is_registered_on_file_import",
      mod_ws.menu_func in getattr(bpy.types.TOPBAR_MT_file_import.draw,
                                  "_draw_funcs", []),
      str([getattr(f, "__name__", "?") for f in
           getattr(bpy.types.TOPBAR_MT_file_import.draw, "_draw_funcs", [])]))
# The button is clicked from the addon preferences, which Blender opens as a
# SEPARATE TEMPORARY WINDOW holding one PREFERENCES area -- `context.window`
# there has no viewport, and the first release laid the workspace out on it,
# which is to say not at all.  A harness cannot open a second window, so the
# window CHOICE is graded on the pure helper instead of on Blender's state.
class _FakeScreen:
    def __init__(self, temp, types):
        self.is_temporary = temp
        self.areas = [type("_A", (), {"type": t})() for t in types]


class _FakeWindow:
    def __init__(self, parent, screen):
        self.parent = parent
        self.screen = screen


_real = _FakeWindow(None, _FakeScreen(False, ["VIEW_3D", "OUTLINER"]))
_prefs_win = _FakeWindow(_real, _FakeScreen(True, ["PREFERENCES"]))
check("main_window_skips_the_preferences_window",
      mod_ws._main_window([_prefs_win, _real]) is _real,
      "picked the temporary Preferences window")
check("main_window_survives_being_the_only_window",
      mod_ws._main_window([_real]) is _real, "lost the only window there is")
check("preferences_screen_has_no_viewport_to_build_from",
      not mod_ws._has_viewport(_prefs_win.screen),
      "a PREFERENCES-only screen was accepted as a layout target")
check("a_real_screen_does_have_a_viewport",
      mod_ws._has_viewport(_real.screen), "the positive arm is broken")
# ...and the choice itself, structurally.  Seeding `window = context.window`
# back into `execute` is BLIND to every runtime check above -- headless there is
# exactly one window, so `context.window` and `_main_window()` are the same
# object, and the defect only appears in a second window a harness cannot open
# (`temp_override` refuses a temporary screen outright).  A source GREP would be
# worse than nothing: this module's own docstring says "context.window" four
# times explaining why not to use it, so the string is present either way.  Read
# the AST of `execute` instead, where a comment cannot satisfy the assertion.
import ast as _ast
import inspect as _inspect
import textwrap as _textwrap

try:
    _ex_tree = _ast.parse(_textwrap.dedent(
        _inspect.getsource(mod_ws.MAP_OT_add_workspace.execute)))
    _bad = [n for n in _ast.walk(_ex_tree)
            if isinstance(n, _ast.Attribute) and n.attr == "window"
            and isinstance(n.value, _ast.Name) and n.value.id == "context"]
    check("execute_reads_no_context_window", not _bad,
          f"execute still reads `context.window` at line(s) "
          f"{[n.lineno for n in _bad]} — clicked from the addon preferences "
          f"that is the temporary Preferences window")
except Exception as e:
    check("execute_reads_no_context_window", False, repr(e))
# The SECOND half of the same defect, and the second traceback the artist saw:
# `temp_override` is refused outright while a temporary screen is ACTIVE --
# `TypeError: Overriding context with an active temporary screen isn't
# supported` -- whatever you override it TO.  A click made from Preferences
# therefore cannot use one at all, even to reach the artist's own window.
# Blind for the same reason as above (no second window headless), so it is read
# off the AST of the two functions the click runs through.  The layout's own
# overrides are fine and are NOT covered here: measured, a timer callback sees
# the main window even with Preferences open.
try:
    _click_path = "".join(_textwrap.dedent(_inspect.getsource(f))
                          for f in (mod_ws.MAP_OT_add_workspace.execute,
                                    mod_ws.build))
    _over = [n for n in _ast.walk(_ast.parse(_click_path))
             if isinstance(n, _ast.Attribute) and n.attr == "temp_override"]
    check("click_path_uses_no_temp_override", not _over,
          f"`temp_override` at line(s) {[n.lineno for n in _over]} of the "
          f"click path — it raises when the click came from Preferences")
except Exception as e:
    check("click_path_uses_no_temp_override", False, repr(e))
# ...and the layout retries rather than raising, if a context ever does refuse.
check("split_swallows_a_refused_context",
      "TypeError" in _inspect.getsource(mod_ws._split),
      "a refused override in _split would reach the artist as a traceback")
# --- the workspace on import (ADR-0185 decision 4, Amendment 2) -------------
# The decision said NOT hooked to import, on the reasoning that an import which
# rearranges the artist's screen is a failure one level up.  Reported from use,
# the artist wants it on a GNS or interchange import -- so the switch is theirs,
# and BOTH arms are graded: on, it offers; off, it does not.
_pf = bpy.context.preferences.addons["exmateria_map"].preferences
check("workspace_on_import_preference_exists",
      "workspace_on_import" in type(_pf).bl_rna.properties,
      str([p for p in type(_pf).bl_rna.properties.keys()]))
check("workspace_on_import_defaults_on",
      type(_pf).bl_rna.properties["workspace_on_import"].default is True,
      "the artist asked for it; off by default would be a surprise the other way")
check("prefs_offers_the_import_switch",
      "workspace_on_import" in _prefs_props, str(_prefs_props))
# OFF must mean off.  This is the arm that protects the decision's objection.
_pf.workspace_on_import = False
check("import_hook_respects_off", mod_ws.ensure_on_import(bpy.context) == "off",
      "the preference is drawn but not read")
_pf.workspace_on_import = True
check("import_hook_acts_when_on",
      mod_ws.ensure_on_import(bpy.context) in ("built", "switched"),
      "the hook did nothing with the preference on")
# Both importers have to call it, or the switch is true of one format only --
# and "on GNS OR interchange" was the request.  Read the AST: a grep would
# match the `from .workspace import ensure_on_import` line in either file even
# if nothing called it.
for _name, _fn in (("interchange", mod.IMPORT_OT_interchange_document.execute),
                   ("gns", __import__("exmateria_map.gns_bundle",
                                      fromlist=["x"]).IMPORT_OT_gns.execute)):
    _calls = [n for n in _ast.walk(_ast.parse(
                  _textwrap.dedent(_inspect.getsource(_fn))))
              if isinstance(n, _ast.Call)
              and getattr(n.func, "id", None) == "ensure_on_import"]
    check(f"{_name}_import_calls_the_workspace_hook", bool(_calls),
          f"{_name} importer never calls ensure_on_import")
# An import that succeeded must not fail because a screen could not be arranged.
check("import_hook_never_raises",
      "try:" in _inspect.getsource(mod_ws.ensure_on_import),
      "ensure_on_import has no guard around the preferences lookup")

# --- the Log (ADR-0185 decision 5) ------------------------------------------
# What is gradeable here is the RECORD. Whether it is on screen is a claim
# about a Text editor, and `-b` has none -- that half is graded headful by
# `workspace/workspace_probe.py` phase `log`.
from exmateria_map import report_log as mod_log

check("log_reuses_the_existing_text_datablock_name",
      mod_log.LOG_NAME == mod.REPORT_TEXT_NAME,
      f"{mod_log.LOG_NAME!r} vs {mod.REPORT_TEXT_NAME!r} — a .blend saved "
      f"before the Log would be orphaned beside it")
if mod_log.LOG_NAME in bpy.data.texts:
    bpy.data.texts[mod_log.LOG_NAME].clear()
_b = mod_log.append("Push to PCSX-Redux", "MAP001.a0", ["wrote 1816 bytes"])
check("log_writes_an_entry",
      _b is not None and "wrote 1816 bytes" in _b.as_string(),
      repr(getattr(_b, "as_string", lambda: None)()))
check("log_stamps_and_names_the_subject",
      "Push to PCSX-Redux" in _b.as_string() and "MAP001.a0" in _b.as_string(),
      _b.as_string())
# A LOG, not a rewrite. This is the whole difference from what `copy_report`
# used to do, which was `clear()` then write -- every press destroying the
# history it was meant to preserve.
_n1 = len(_b.lines)
mod_log.append("Export", "MAP001.a0", ["changed since import: nothing"])
check("log_appends_rather_than_replacing",
      len(_b.lines) > _n1 and "wrote 1816 bytes" in _b.as_string(),
      f"{_n1} -> {len(_b.lines)}")
check("log_keeps_the_entries_in_order",
      _b.as_string().index("wrote 1816 bytes")
      < _b.as_string().index("changed since import"),
      "the newest entry is not last — sequence is the Log's whole job")
# Copy must not make the artist's own history claim it happened twice.
_n2 = len(_b.lines)
mod_log.append("Export", "MAP001.a0", ["changed since import: nothing"],
               unless_duplicate=True)
check("log_refuses_a_duplicate_of_its_last_entry", len(_b.lines) == _n2,
      f"{_n2} -> {len(_b.lines)}")
# ...and the guard must compare BODIES, not rendered entries: every entry
# carries a clock stamp, so comparing rendered text never matches and the
# check above would pass for the wrong reason.
check("the_duplicate_guard_ignores_the_stamp",
      mod_log._last_body(_b.as_string())
      == mod_log._body(["changed since import: nothing"]),
      repr(mod_log._last_body(_b.as_string())))
# ...and prove it END TO END across a stamp that DIFFERS. Two appends a
# millisecond apart render identical text, so a guard that compared rendered
# entries would pass this test by accident -- which is exactly what a seed
# doing that turned out to do. Age the last entry's clock first.
_txt = _b.as_string()
_stamp = _txt.split("[")[-1].split("]")[0]
_head, _sep, _tail = _txt.rpartition(_stamp)   # the LAST one: every entry in
_b.from_string(_head + "00:00:01" + _tail)     # this block shares a second
_n3 = len(_b.lines)
mod_log.append("Export", "MAP001.a0", ["changed since import: nothing"],
               unless_duplicate=True)
check("the_duplicate_guard_holds_across_a_different_stamp",
      len(_b.lines) == _n3, f"{_n3} -> {len(_b.lines)} — the guard is "
                            f"comparing stamps, not bodies")
# A different Outcome is not a duplicate.
mod_log.append("Export", "MAP001.a0", ["1 face(s) moved"], unless_duplicate=True)
check("log_still_appends_a_different_outcome",
      "1 face(s) moved" in _b.as_string(), _b.as_string()[-200:])
# One datablock per session, and bounded.
check("log_uses_exactly_one_datablock",
      sum(1 for t in bpy.data.texts
          if t.name.startswith(mod_log.LOG_NAME)) == 1,
      str([t.name for t in bpy.data.texts]))
mod_log.append("Flood", "x", [f"line {i}" for i in range(mod_log.MAX_LINES * 2)])
check("log_is_bounded", len(_b.lines) <= mod_log.MAX_LINES + 1,
      f"{len(_b.lines)} lines, cap {mod_log.MAX_LINES}")
check("log_keeps_the_NEWEST_when_it_trims",
      f"line {mod_log.MAX_LINES * 2 - 1}" in _b.as_string(),
      "the trim dropped the newest lines instead of the oldest")
check("show_survives_having_no_text_editor",
      isinstance(mod_log.show(), int), "show() did not return a count")
# A real export, logged. "Does `execute` call `record` anywhere" is too weak:
# export logs on three paths (refused, could-not-write, wrote), and deleting
# the SUCCESS one leaves the other two matching. Only running it can tell.
bpy.data.texts[mod_log.LOG_NAME].clear()
_res = bpy.ops.export_map.document(filepath=EXPORT_DIR)
_logged = bpy.data.texts[mod_log.LOG_NAME].as_string()
check("a_real_export_lands_in_the_log", "FINISHED" in _res and "Export" in _logged,
      f"{_res} / {_logged!r}")
check("the_export_entry_names_what_it_wrote", "wrote into" in _logged
      and ".json" in _logged, _logged)
check("the_export_entry_carries_the_divergence_stats",
      "changed since import" in _logged,
      f"the stats went only to a toast: {_logged!r}")
# Every Outcome-producing operator must actually call it, or the Log is true of
# one gesture only.  AST again: a `from .report_log import record` line would
# match a grep in a file where nothing calls it.
#
# READ THE TREE, NOT THE LOADED MODULE.  `inspect.getsource` reads the file the
# module was imported from, which is the INSTALLED addon -- and
# `bpy.ops.preferences.addon_install` does NOT reliably overwrite an existing
# install on 5.2, so a mutated tree can be graded through a stale copy and read
# green.  Measured: a seed that deleted the push's `record` call left
# `_ui.__file__` pointing at `~/.config/blender/.../live_link_ui.py`, whose
# source still had it.  Parsing the file under `PKG` grades what the harness
# was actually pointed at.
import os.path as _osp


def _tree_path(relpath):
    return _osp.join(PKG, "exmateria_map", relpath)


def _tree_func(relpath, name, cls=None):
    """The AST of one function, read from the source tree under test."""
    with open(_tree_path(relpath)) as _fh:
        tree = _ast.parse(_fh.read())
    scope = tree
    if cls is not None:
        scope = next(n for n in tree.body
                     if isinstance(n, _ast.ClassDef) and n.name == cls)
    return next(n for n in scope.body
                if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                and n.name == name)


def _calls_named(node, fname):
    return [n for n in _ast.walk(node)
            if isinstance(n, _ast.Call) and getattr(n.func, "id", None) == fname]


from exmateria_map import live_link_ui as ui

#: Every panel we register, and where its `draw` is read from.  The two Paint
#: classes share `_PaintPanel`'s body, so both map to the mixin.  Kept beside
#: `_tree_func` because the arms that walk it are AST arms; the arms that walk
#: the REGISTERED panels use `_HOMES`, and the roster check ties the two
#: together by demanding `_HOMES` name exactly what `bpy.types` holds.
_PANEL_SOURCES = {
    "MAP_PT_paint":             ("paint.py", "_PaintPanel"),
    "MAP_PT_paint_view":        ("paint.py", "_PaintPanel"),
    "MAP_PT_preview":           ("import_document.py", "MAP_PT_preview"),
    "MAP_PT_terrain":           ("authoring.py", "MAP_PT_terrain"),
    "MAP_PT_lighting_bake":     ("lighting_bake.py", "MAP_PT_lighting_bake"),
    "MAP_PT_live_push":         ("live_link_ui.py", "MAP_PT_live_push"),
    "MAP_PT_live_camera":       ("live_link_ui.py", "MAP_PT_live_camera"),
    "MAP_PT_live_isolate":      ("live_link_ui.py", "MAP_PT_live_isolate"),
}


check("the_ast_checks_read_the_tree_not_the_installed_copy",
      _osp.exists(_tree_path("live_link_ui.py"))
      and PKG not in str(bpy.utils.user_resource("SCRIPTS")),
      f"PKG {PKG} is the installed addon directory — the seeds would be blind")
for _tag, _rel, _cls, _fn in (
        ("export", "export_document.py", "EXPORT_OT_interchange_document",
         "execute"),
        ("bundle", "gns_bundle.py", "EXPORT_OT_bundle", "execute"),
        # `push_report`, not `MAP_OT_live_push.execute`: the push's report is
        # landed by a plain function now, because a BACKGROUND push (the
        # settle's) lands its report long after the operator returned. The
        # chain from the operator to it is asserted below.
        ("push", "live_link_ui.py", None, "push_report"),
        ("lamp_authority", "lighting_bake.py", None, "_authority_update")):
    try:
        check(f"{_tag}_records_an_outcome_in_the_log",
              bool(_calls_named(_tree_func(_rel, _fn, _cls), "record")),
              f"{_tag} never calls report_log.record")
    except Exception as e:
        check(f"{_tag}_records_an_outcome_in_the_log", False, repr(e))
# The live handler must NOT: it bakes on every lamp change, and an entry per
# depsgraph update would bury every export and push in the same pane.
try:
    check("the_live_bake_handler_does_not_flood_the_log",
          not _calls_named(_tree_func("lighting_bake.py", "_live_handler"),
                           "record"),
          "the depsgraph handler records an Outcome on every lamp change")
except Exception as e:
    check("the_live_bake_handler_does_not_flood_the_log", False, repr(e))

# The push panel's deleted rows, from the source side. The artist's rule is
# that a run's output is CONSOLE output; the panel is controls only (graded
# above, on the rendered rows), so both halves of the replacement have to be
# real or the rows were simply lost.
#
# The three anchors moved when the push was split so the settle could run its
# transport off the main thread: the limit is stated by `_transport`, the
# report is landed by `push_report`, and `MAP_OT_live_push.execute` is a
# four-line call into `push_now`. So the LINK is asserted too -- an `execute`
# that stopped going through `push_now` would satisfy the other two arms while
# reaching none of that code.
try:
    def _called_in(fn, cls=None):
        return {getattr(n.func, "id", None) or getattr(n.func, "attr", None)
                for n in _ast.walk(_tree_func("live_link_ui.py", fn, cls))
                if isinstance(n, _ast.Call)}

    check("the_push_operator_still_states_the_limit",
          "unpushed_lines" in _called_in("_transport"),
          "the push no longer calls unpushed_lines -- deleting `What a "
          "push carries` would then have deleted the LIMIT as well as the panel")
    check("the_push_operator_reaches_the_push",
          "push_now" in _called_in("execute", "MAP_OT_live_push")
          and {"push_gather", "push_transport", "push_report"}
          <= _called_in("push_now"),
          "MAP_OT_live_push.execute no longer routes through push_now, or "
          "push_now no longer runs all three halves")
    check("the_push_prints_what_it_stores",
          "print" in _called_in("push_report"),
          "push_report records to the Log but prints nothing -- the panel rows "
          "were deleted TO the console, so the console has to receive them")
except Exception as e:
    check("the_push_operator_still_states_the_limit", False, repr(e))

# ===========================================================================
# ADR-0185 Amendment 4 -- landing 1.
#
# SEEDED, because most of what follows states what must not be LOST, and an arm
# that has never been red is decoration. Eight defects, one at a time, into a
# scratch copy of the package (never this worktree -- another agent commits
# here), each re-run through the whole file. 8/8 caught, none blind, none
# SEED-BROKEN:
#
#   both copies shout NO_EDITOR_HINT ....... the_image_editor_copy_does_not_say_it
#   neither copy says it .................... the_viewport_copy_still_says_where_the_sheet_went
#   an UNTAGGED workspace claimed as ours ... an_untagged_map_workspace_is_the_artists_and_is_left_alone
#   the report loses its copy button ........ the_report_still_offers_the_copy_button
#
# `the_export_panel_still_shows_the_last_export` was the eighth and is RETIRED
# with its subject: the salience pass deleted `MAP_PT_export`, and the claim it
# seeded -- that the Outcome survives somewhere the artist can read it -- is now
# `the_export_report_still_reaches_the_Log`, which is not a seeded arm because
# it names a module rather than a rendered row.  The `_stored_report` behaviour
# the deleted arms graded is graded on `MAP_PT_live_push` instead, which is a
# live caller of the same helper.
#   `marker_in_scene` back to the selection . no_panel_polls_itself_away_with_the_map_deselected
#   the rig table drops light 3 ............. the_table_still_draws_every_authorable_rig_control
#   `_timeline` takes a DOPE SHEET .......... the_band_finder_does_not_take_a_dope_sheet_for_a_timeline
#
# The two arms of the placement RATCHET are seeds by construction: they run the
# predicate against a synthetic yesterday-shaped panel and a compliant one.
# ===========================================================================

# --- ADR-0185 Amendment 4: the layout is the home, Properties is vacated ----
# The rule, which replaces decision 3: "Every map panel lives in the `Map` tab
# of the 3D viewport sidebar.  One leaves it, and only when a different editor
# is RENDERING ITS ACTUAL SUBJECT: Paint, because the sheet's pixels are in the
# Image Editor."  Reported from use, twice: "I don't want it mixed with the
# regular properties menu."
#
# Graded as a RATCHET WITH BOTH ARMS, because "no panel of ours is in
# PROPERTIES" is exactly the claim that goes quietly vacuous.  A check walking a
# hand-kept tuple of class names passes forever once a class leaves the tuple; a
# check walking `bpy.types` for `MAP_PT_*` passes if the addon never registered.
# So the roster is DISCOVERED from the registered types, the table has to name
# every member of it, and the predicate is then run against a synthetic
# offender to prove it can still say no.
# Revision 2 (2026-08-27, the salience pass).  `Push to PCSX-Redux` is FIRST
# -- it is the only panel holding two of the controls the artist named most
# important, `Launch PCSX-Redux` and `Push to PCSX` -- and Transform and Export
# are GONE from the tab, withdrawn by the same artist who asked for them in
# Amendment 4.  The roster arm below demands this table name every registered
# panel and no more, so a panel that comes back fails it and so does one that
# quietly leaves.
_HOMES = {
    # editor,       bl_order within that editor
    "MAP_PT_paint":             ("IMAGE_EDITOR", 0),
    "MAP_PT_live_push":         ("VIEW_3D", 0),
    # Camera sits with Push and not at the end: both are the live link, and the
    # artist presses them in one breath -- Push sends the map, Match camera
    # aims at it.  Everything below shifted by one to keep the permutation
    # arm's 0..N-1, which is the arm that refuses a second panel at order 1.
    "MAP_PT_live_camera":       ("VIEW_3D", 1),
    # Isolate joins them for the same reason, one panel further down: aim the
    # camera, hide the units, look at the map -- one breath (decision 13).
    # Everything below shifted by one again, which the permutation arm's
    # 0..N-1 is what forces to be done rather than left to drift.
    "MAP_PT_live_isolate":      ("VIEW_3D", 2),
    "MAP_PT_preview":           ("VIEW_3D", 3),
    "MAP_PT_paint_view":        ("VIEW_3D", 4),
    "MAP_PT_terrain":           ("VIEW_3D", 5),
    "MAP_PT_lighting_bake":     ("VIEW_3D", 6),
}
# The deletion, as its own arm.  A table entry going missing is otherwise
# indistinguishable from a table entry never written, and these two leaving is
# the DECISION -- so it is asserted against `bpy.types`, not against `_HOMES`.
check("the_withdrawn_panels_are_gone_from_the_tab",
      not [n for n in ("MAP_PT_map_transform", "MAP_PT_export",
                       "MAP_PT_live_push_carries")
           if getattr(bpy.types, n, None) is not None],
      "a withdrawn panel is registered again. Transform -> Blender's own "
      "`Item` tab; Export's report -> the Log; `What a push carries` -> the "
      "console and the Log, once per push, via `unpushed_lines`")
# ...and each has to have LEFT SOMETHING BEHIND, or a deletion is a removal.
#
# Export's report: `report_log` is the surface it deferred to.  This is the
# reason the panel was safe to delete, so it is checked, not assumed.
check("the_export_report_still_reaches_the_Log",
      callable(getattr(mod_log, "record", None))
      and callable(getattr(mod_log, "show", None)),
      "MAP_PT_export was deleted BECAUSE report_log carries the Outcome; "
      "without it an export refusal has no surface once the toast expires")
# Transform's WARNING: the panel is gone, the consequence is not.  Moving the
# map relative to its lamps still changes the baked normals and those still
# reach the disc, so the sentence moved to the panel that owns the bake.
_lb_doc = (bpy.types.MAP_PT_lighting_bake.__doc__ or "").lower()
check("the_bake_consequence_outlived_the_transform_panel",
      "transform" in _lb_doc and "normal" in _lb_doc and "disc" in _lb_doc,
      "MAP_PT_map_transform carried the only statement that the map's "
      "transform feeds the bake and therefore the disc; deleting the panel "
      f"must not delete the sentence: {_lb_doc[:200]!r}")
_roster = sorted(n for n in dir(bpy.types) if n.startswith("MAP_PT_"))
# The AST sweep's table has to cover the same roster, or a panel added without
# a `_PANEL_SOURCES` entry is simply skipped by every arm that walks sources --
# which reads as "no panel repeats the File menu entry" rather than as a hole.
check("the_ast_panel_table_covers_the_whole_roster",
      set(_PANEL_SOURCES) == set(_roster),
      f"_PANEL_SOURCES names {sorted(_PANEL_SOURCES)}, registered {_roster}")
check("the_panel_roster_is_the_one_the_rule_was_written_for",
      set(_roster) == set(_HOMES),
      f"registered {_roster}, the Amendment-4 table names "
      f"{sorted(_HOMES)} -- a panel with no home in the table is a seventh "
      f"judgement call, which is what the one rule exists to prevent")

# --- Amendment 4: NO panel is selection-scoped ------------------------------
# "There is only really one map in the scene -- and having to have that map
# SELECTED to see the properties is annoying."  Decision 3 would have codified
# the split as intentional; measured, only three of six panels obeyed the rule
# their own docstrings state, and Preview's blank-on-deselect sat 38 lines
# above `marker_in_scene` in the same file without using it.
#
# Aiming a lamp means SELECTING the lamp, which makes it the ACTIVE object.  So
# the deselect is staged with a lamp and not with `None`: `None` is reachable
# by clicking empty space, but a lamp is the gesture the artist makes on the
# way to the panel they are about to lose.
#
# "Survives" is not "does not raise".  A panel that draws nothing is, to the
# artist, the same as one that crashed -- so every arm counts what reached the
# layout as well.
class _SinkLayout(_FakeLayout):
    """A `_FakeLayout` with its own tape, so one panel's draw cannot be
    satisfied by another panel's output."""

    def __init__(self):
        _FakeLayout.__init__(self, [])
        self.drawn = []

    def label(self, text="", icon=None, **kw):
        self.drawn.append(("label", text))
        return self

    def operator(self, bl_idname, text="", icon=None, **kw):
        self.drawn.append(("operator", bl_idname))

        class _Op:
            pass
        return _Op()

    def operator_menu_enum(self, bl_idname, prop, text="", icon=None, **kw):
        self.drawn.append(("operator", bl_idname))
        return self

    def menu(self, menu_id, text="", icon=None, **kw):
        self.drawn.append(("menu", menu_id))
        return self

    def prop(self, data, prop_name, **kw):
        # The DATA, not its name: what makes the transform block right is
        # WHOSE transform reached the layout, and a name read back later is a
        # dead StructRNA by the end of this file.
        self.drawn.append(("prop", data, prop_name))
        return self

    def template_palette(self, data, prop, *a, **kw):
        # the DATA as well: what makes the palette the legal set is whose it
        # is, and `template_palette` takes TWO arguments on 5.2 -- `color=` is
        # a TypeError, so `*a` catches a caller that puts it back.
        self.drawn.append(("palette", data, prop, a, sorted(kw)))
        return self


def _draw_deselected(cls):
    """Drive one panel's real `draw` with a lamp as the active object."""
    sink = _SinkLayout()
    try:
        cls.draw(_panel_shim(cls, sink), bpy.context)
    except Exception as e:
        return sink, f"{type(e).__name__}: {e}"
    return sink, None


_prev_active2 = bpy.context.view_layer.objects.active
_lamp2 = bpy.data.objects.new("deselect_probe_lamp",
                              bpy.data.lights.new("dpl", "POINT"))
bpy.context.scene.collection.objects.link(_lamp2)
bpy.context.view_layer.objects.active = _lamp2
# The precondition, or every arm below passes for the wrong reason.
check("the_deselect_arm_really_deselects_the_map",
      bpy.context.object is _lamp2
      and "exmateria_map/preview_state" not in bpy.context.object,
      f"active object is {getattr(bpy.context.object, 'name', None)!r}")
_deselected = {}
for _n in _roster:
    _deselected[_n] = _draw_deselected(getattr(bpy.types, _n))
_raised = {n: err for n, (_s, err) in _deselected.items() if err}
check("no_panel_raises_with_the_map_deselected", not _raised, str(_raised))
_blank = sorted(n for n, (s, err) in _deselected.items() if not err and not s.drawn)
check("every_panel_still_draws_with_the_map_deselected",
      not _blank,
      f"{_blank} went BLANK with a lamp active -- the panel disappears at "
      f"exactly the moment the artist reached for it")
# ...and the `poll`s, which are the other half of disappearing.
#
# The two Paint copies are excluded BY NAME and that is the one exclusion here:
# their poll answers a different question -- "is the other copy visible" -- and
# is graded on its own arms below, with a fake screen.  Everything else's poll
# is about the MAP, which is what this arm is about.
_polled_away = sorted(n for n in _roster
                      if getattr(bpy.types, n, None) is not None
                      and hasattr(getattr(bpy.types, n), "poll")
                      and n not in ("MAP_PT_paint", "MAP_PT_paint_view")
                      and not getattr(bpy.types, n).poll(bpy.context))
check("no_panel_polls_itself_away_with_the_map_deselected",
      not _polled_away, str(_polled_away))



# --- the light rig moved out of Preview -------------------------------------
# *"The light stuff in there should just go in the light panel -- those can be
# authored now, right?  they shouldn't be in a preview panel."*  Right: a rig
# Override is EDITABLE and `rig_is_dirty` makes `build` write 45 bytes to the
# disc (decision 27), so those sliders author and the panel said Preview.
#
# Graded as a MOVE with both ends, because "Preview no longer draws the rig"
# passes just as well if the rig was deleted, and "the light panel draws it"
# passes just as well if Preview draws it too.
_prev_sink, _prev_err = _SinkLayout(), None
try:
    mod.MAP_PT_preview.draw(_panel_shim(mod.MAP_PT_preview, _prev_sink),
                            bpy.context)
except Exception as e:
    _prev_err = f"{type(e).__name__}: {e}"
_bake_sink, _bake_err = _SinkLayout(), None
try:
    mod_bake.MAP_PT_lighting_bake.draw(
        _panel_shim(mod_bake.MAP_PT_lighting_bake, _bake_sink), bpy.context)
except Exception as e:
    _bake_err = f"{type(e).__name__}: {e}"
check("both_panels_drew_after_the_rig_move", _prev_err is None and _bake_err is None,
      f"preview: {_prev_err}, lighting bake: {_bake_err}")
_LIGHT_PROPS = {"exmateria_map_light_debug", "exmateria_map_light_boost"}
_prev_props = {r[2] for r in _prev_sink.drawn if r[0] == "prop"}
_bake_props = {r[2] for r in _bake_sink.drawn if r[0] == "prop"}
check("the_preview_panel_draws_no_light_control",
      not (_prev_props & _LIGHT_PROPS),
      f"Preview still draws {sorted(_prev_props & _LIGHT_PROPS)} -- the light "
      f"controls moved to the panel that owns light")
check("the_light_panel_draws_the_light_controls",
      _LIGHT_PROPS <= _bake_props,
      f"Lighting Bake draws {sorted(_bake_props)}; the move must ARRIVE, not "
      f"just depart -- missing {sorted(_LIGHT_PROPS - _bake_props)}")
# The rig TABLE itself, which is the half that authors bytes. `_rig_box` draws
# the Override's own properties, so its arrival is visible as a prop on a
# `MAP_PG_rig_override` rather than on the Object.
_bake_owners = {type(r[1]).__name__ for r in _bake_sink.drawn if r[0] == "prop"}
_prev_owners = {type(r[1]).__name__ for r in _prev_sink.drawn if r[0] == "prop"}
check("the_rig_table_arrived_in_the_light_panel",
      "MAP_PG_rig_override" in _bake_owners,
      f"no rig Override property is drawn in Lighting Bake ({sorted(_bake_owners)}) "
      f"-- the sliders that write 45 bytes have to be SOMEWHERE")
check("the_rig_table_left_the_preview_panel",
      "MAP_PG_rig_override" not in _prev_owners,
      f"Preview still draws rig Override properties ({sorted(_prev_owners)}) -- "
      f"a move that leaves a copy behind is a duplication")
# What Preview KEPT, or the move took the panel with it.
check("the_preview_panel_still_chooses_the_state_and_the_source",
      "exmateria_map_preview_source" in _prev_props
      and any(r[0] == "menu" for r in _prev_sink.drawn),
      f"Preview drew {sorted(_prev_props)} and "
      f"{[r[0] for r in _prev_sink.drawn]} -- the state menu and the "
      f"painting/compiled switch are what makes it a Preview panel")

# --- `Pin Standard view transform`: RETIRED, and the effect asserted --------
# *"I don't understand this pin standard view thing -- do we really need it?"*
# The effect yes, the button no. #427: Blender reports only the CURRENT item of
# `view_transform`, so `Standard` looks absent while AgX regrades every pixel --
# under which the sixteen swatches are not the sixteen colours on the disc.
# `bpy.types` ONLY. `bpy.ops.<module>.<anything>` resolves lazily and answers
# `hasattr` with a stub for operators that do not exist, so an arm asking
# `bpy.ops` that question is always red and says nothing.
check("the_pin_view_transform_button_is_retired",
      "MAP_OT_pin_view_transform" not in dir(bpy.types),
      "exmateria_map.pin_view_transform is still a registered operator")
# The button set ONE of the four things every harness in this package pins.
# That is the reason it was not merely redundant, and it is the reason this
# arm names the whole set rather than the transform alone.
check("view_parity_names_all_four_settings",
      mod.VIEW_PARITY == {"view_transform": "Standard", "look": "None",
                          "exposure": 0.0, "gamma": 1.0},
      f"{mod.VIEW_PARITY} -- the harnesses pin four; the deleted button pinned "
      f"one, which is why pressing it was not enough")
# It WORKS. Measured while writing these arms, and both facts shape them:
#
#   - `look`'s enum is DYNAMIC. `bl_rna.properties["look"].enum_items` reports
#     `['NONE']` while `'AgX - Punchy'` assigns fine -- the same trap as #427,
#     one layer along: introspection under-reports and a hardcoded guess is a
#     TypeError. The AgX looks exist only while `view_transform` is AgX.
#   - Blender RESETS `look` when `view_transform` changes. So `look` cannot be
#     staged wrong on its own once the transform is right, and `pin_view_parity`
#     correctly reports only what IT changed -- the reset is Blender's.
#
# Hence: the transform and its look are staged together, exposure and gamma on
# their own, and every arm ends by asserting the SETTING, never only the report.
_vs = bpy.context.scene.view_settings
mod.pin_view_parity(bpy.context.scene)
try:
    _vs.view_transform = "AgX"
    _vs.look = "AgX - Punchy"
    _staged = (_vs.view_transform, _vs.look)
except (TypeError, ValueError) as e:
    _staged = None
check("a_wrong_transform_and_look_could_be_staged",
      _staged == ("AgX", "AgX - Punchy"), str(_staged))
_moved = mod.pin_view_parity(bpy.context.scene)
check("pinning_fixes_a_wrong_transform_and_look",
      _vs.view_transform == "Standard" and _vs.look == "None"
      and any(m.startswith("view_transform:") for m in _moved),
      f"transform={_vs.view_transform!r} look={_vs.look!r} reported={_moved} — "
      f"under AgX the sixteen swatches are not the sixteen colours on the disc")
for _k, _bad in (("exposure", 1.5), ("gamma", 2.2)):
    mod.pin_view_parity(bpy.context.scene)
    setattr(_vs, _k, _bad)
    _moved = mod.pin_view_parity(bpy.context.scene)
    check(f"pinning_fixes_a_wrong_{_k}",
          getattr(_vs, _k) == mod.VIEW_PARITY[_k]
          and any(m.startswith(f"{_k}:") for m in _moved),
          f"{_k} left at {getattr(_vs, _k)!r}, reported {_moved} -- it must be "
          f"put back AND named, since the import announces only what it moved")
# ORDER is load-bearing and invisible: `look` is only assignable to `None` once
# the transform is already Standard, so `VIEW_PARITY` has to name the transform
# FIRST and `pin_view_parity` has to walk it in order. A dict literal reordered
# by a tidy-up would break colour parity with every arm above still green,
# because they stage the transform first themselves.
check("view_parity_pins_the_transform_before_the_look",
      list(mod.VIEW_PARITY).index("view_transform")
      < list(mod.VIEW_PARITY).index("look"),
      f"{list(mod.VIEW_PARITY)} -- `look`'s valid values depend on the "
      f"transform, so the transform is set first or the look assignment is a "
      f"TypeError that `pin_view_parity` swallows")
# ...and it is IDEMPOTENT: a second call moves nothing, which is what lets the
# import report real changes instead of claiming to have altered a scene that
# was already right.
mod.pin_view_parity(bpy.context.scene)
check("pinning_an_already_pinned_scene_reports_nothing",
      mod.pin_view_parity(bpy.context.scene) == [],
      "a no-op pin still reports changes -- the import would then announce it "
      "moved settings it did not touch")
check("the_scene_is_left_under_view_parity",
      all(getattr(_vs, k) == v for k, v in mod.VIEW_PARITY.items()),
      f"{ {k: getattr(_vs, k) for k in mod.VIEW_PARITY} }")

# The import is what asserts it, since nothing else does and the button is gone.
try:
    _iexec = _tree_func("import_document.py", "execute",
                        "IMPORT_OT_interchange_document")
    check("the_import_pins_the_view_parity",
          bool(_calls_named(_iexec, "pin_view_parity")),
          "the import does not call pin_view_parity -- with the button retired "
          "there is then NO route to correct colour, and #427's failure is "
          "that AgX looks fine while regrading every pixel")
except Exception as e:
    check("the_import_pins_the_view_parity", False, repr(e))


def _misplaced(cls, want_space):
    """Every way a panel can fail to live where the rule says it does."""
    bad = []
    if getattr(cls, "bl_space_type", None) != want_space:
        bad.append(f"bl_space_type={getattr(cls, 'bl_space_type', None)!r}")
    if getattr(cls, "bl_region_type", None) != "UI":
        bad.append(f"bl_region_type={getattr(cls, 'bl_region_type', None)!r}")
    if getattr(cls, "bl_category", None) != mod_ws.TAB:
        bad.append(f"bl_category={getattr(cls, 'bl_category', None)!r}")
    # `bl_context` is the Properties editor's own field.  Left behind on a
    # sidebar panel it is inert, and inert is how the next reader learns the
    # wrong rule.
    if getattr(cls, "bl_context", "") != "":
        bad.append(f"bl_context={getattr(cls, 'bl_context', None)!r}")
    return bad


_wrong = {n: _misplaced(getattr(bpy.types, n), _HOMES[n][0])
          for n in _roster if n in _HOMES}
_wrong = {n: v for n, v in _wrong.items() if v}
check("every_panel_lives_where_the_rule_says", not _wrong, str(_wrong))
# Stated separately from the table, because THIS is the sentence the artist
# said and the one a future reader will look for.
check("no_panel_of_ours_is_registered_for_properties",
      not [n for n in _roster
           if getattr(bpy.types, n).bl_space_type == "PROPERTIES"],
      str([n for n in _roster
           if getattr(bpy.types, n).bl_space_type == "PROPERTIES"]))
# The ratchet's second arm: the predicate itself, against a panel shaped
# exactly like the six this landing moved.  Without it, `_misplaced` returning
# `[]` unconditionally would read as nine panels in the right place.
class _WasInProperties:
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"
    bl_category = "Map"


class _IsInTheSidebar:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Map"


check("the_placement_rule_still_catches_a_properties_panel",
      len(_misplaced(_WasInProperties, "VIEW_3D")) == 3,
      f"the yesterday-shaped panel scored {_misplaced(_WasInProperties, 'VIEW_3D')} "
      f"-- it must be caught on space, region AND the leftover bl_context")
check("the_placement_rule_passes_a_panel_that_obeys_it",
      _misplaced(_IsInTheSidebar, "VIEW_3D") == [],
      str(_misplaced(_IsInTheSidebar, "VIEW_3D")))
# Order is per EDITOR now, so the old global 0-5 cannot survive: two panels in
# different editors may share a number and two in the same one may not.
# Asserted as a PERMUTATION rather than against the literals above, which would
# only restate the table.
for _sp in sorted({s for s, _ in _HOMES.values()}):
    _there = {n: o for n, (s, o) in _HOMES.items() if s == _sp}
    _live = {n: getattr(bpy.types, n, None) for n in _there}
    check(f"bl_order_is_a_permutation_in_{_sp.lower()}",
          all(c is not None and getattr(c, "bl_order", None) == _there[n]
              for n, c in _live.items())
          and sorted(_there.values()) == list(range(len(_there))),
          f"{ {n: getattr(c, 'bl_order', None) for n, c in _live.items()} } "
          f"vs {_there}")
# NOTHING is DEFAULT_CLOSED now. The only closed panel was `What a push
# carries`, and closing it was the compromise this pass replaced with a
# deletion: reference material does not get a collapsed corner of the column,
# it goes to the console. The artist's words were "move and UNCOLLAPSE the
# controls", and the tab is now controls all the way down.
_closed = [n for n in _roster
           if "DEFAULT_CLOSED" in getattr(getattr(bpy.types, n),
                                          "bl_options", set())]
check("no_panel_opens_closed",
      _closed == [],
      f"{_closed} opens DEFAULT_CLOSED -- every panel in the tab is controls "
      f"now, and a collapsed one hides a control rather than tidying prose")
# Decision 8 -- "prefix where there is no tab, bare name where there is" --
# under the new rule resolves to BARE EVERYWHERE, because every panel now sits
# under a tab that already says `Map`.
_prefixed = [(n, getattr(bpy.types, n).bl_label) for n in _roster
             if getattr(bpy.types, n).bl_label.startswith("ExMateria Map")]
check("no_panel_repeats_the_addon_name_under_the_Map_tab",
      not _prefixed, str(_prefixed))

# --- the map marker's own Transform: WITHDRAWN ------------------------------
# Amendment 4 put Location / Rotation / Scale at the top of the tab, reading
# them off `marker_in_scene` so they survived a lamp being selected.  The
# artist has since withdrawn it -- *"we honestly don't really need it.  We can
# just use the regular transform section where you need to select the object
# to move it.  that's fine."* -- so the four arms that drove the block are
# gone with it, and the two claims that OUTLIVE it are asserted where the
# deletion is, beside `_HOMES`: the panel is not registered, and the bake
# consequence it used to state now lives in `MAP_PT_lighting_bake`'s docstring.
bpy.data.objects.remove(_lamp2, do_unlink=True)
bpy.context.view_layer.objects.active = _prev_active2

# --- Amendment 5: Paint draws in BOTH panes -------------------------------
# Amendment 4 registered the viewport copy as a FALLBACK -- its reason to exist
# was `says_where_the_sheet_went`, "you have no Image Editor, here is where the
# sheet went" -- and against that reason it polled itself away wherever
# `context.screen` already held an Image Editor.  Two arms here used to assert
# that poll in both directions.
#
# ADR-0186 Amendment 6 inverts the premise: after a conversion the sheet is not
# an unwrap at all, and the MODEL is the paint surface.  The Map workspace has
# an Image Editor by construction, so the poll put the brush controls in the
# other pane from the thing being painted.  ADR-0185 Amendment 5 decision 9
# deletes it, and the two arms are replaced rather than dropped.
#
# What made deleting it SAFE is that the poll conflated two questions, and the
# second -- "should this copy explain where the sheet went" -- already has its
# own guard inside `draw`: `if self.says_where_the_sheet_went and not free`.
# So the control below asserts the guard's INPUT (there are free Image Editor
# spaces for it to see); `tests/blender_convert.py` drives the guard itself and
# asserts the hint really does stay silent.
def _screen_ctx(screen):
    return type("_C", (), {"screen": screen})()


check("the_hint_guard_has_free_image_editor_spaces_to_see",
      any(not protected for _, protected in pnt.image_editor_spaces()),
      f"{len(pnt.image_editor_spaces())} Image Editor space(s) reachable "
      f"through bpy.data.screens and none of them free -- with the poll gone "
      f"the `not free` guard is the only thing withholding NO_EDITOR_HINT, "
      f"and an arm that cannot see a free space cannot grade it")
_scr_hit = _FakeScreen(False, ["VIEW_3D", "IMAGE_EDITOR", "OUTLINER"])
_scr_miss = _FakeScreen(False, ["VIEW_3D", "OUTLINER"])
check("the_viewport_paint_copy_no_longer_polls_itself_away",
      "poll" not in vars(pnt.MAP_PT_paint_view),
      f"MAP_PT_paint_view defines {sorted(k for k in vars(pnt.MAP_PT_paint_view) if not k.startswith('__'))!r} "
      f"-- a poll here suppresses the panel in the very pane the model is "
      f"painted in (Amendment 5 decision 9)")
check("and_nothing_it_inherits_suppresses_it_either",
      "poll" not in vars(pnt._PaintPanel),
      "a poll on the shared body would take BOTH copies down with it")
# The Image Editor copy has nothing to stand down FOR.
check("the_image_editor_paint_copy_never_stands_itself_down",
      not hasattr(pnt.MAP_PT_paint, "poll")
      or bool(pnt.MAP_PT_paint.poll(_screen_ctx(_scr_hit))),
      "the copy that lives beside the pixels polls itself away")
# `NO_EDITOR_HINT` is NOT deleted, contrary to decision 3: it survives in the
# viewport copy, where it is true.  Told "no Image Editor open" INSIDE one, the
# artist would be reading a lie.
check("only_the_viewport_copy_says_where_the_sheet_went",
      pnt.MAP_PT_paint_view.says_where_the_sheet_went is True
      and pnt.MAP_PT_paint.says_where_the_sheet_went is False,
      f"{pnt.MAP_PT_paint.says_where_the_sheet_went!r} / "
      f"{pnt.MAP_PT_paint_view.says_where_the_sheet_went!r}")
check("the_hint_itself_is_not_deleted",
      isinstance(pnt.NO_EDITOR_HINT, str) and pnt.NO_EDITOR_HINT,
      "decision 3 would have deleted it; Amendment 4 keeps it")

# --- the interchange export is offered ONCE ---------------------------------
# "We already have the interchange format in the export drop down under file --
# we don't need it twice."  `MAP_PT_export` used to hold this arm, drawing no
# button and keeping the report; the artist has since deleted the panel too, so
# the claim is now graded against EVERY panel we register rather than that one.
# That is strictly stronger: the old arm passed the moment its subject was
# deleted, and this one cannot.
_ex_dupes = {}
for _pn, (_pfile, _ptxt) in _PANEL_SOURCES.items():
    try:
        _pd = _tree_func(_pfile, "draw", _ptxt)
    except Exception:
        continue
    _hits = [n.lineno for n in _ast.walk(_pd)
             if isinstance(n, _ast.Call)
             and getattr(n.func, "attr", None) == "operator"
             and n.args and isinstance(n.args[0], _ast.Constant)
             and n.args[0].value == "export_map.document"]
    if _hits:
        _ex_dupes[_pn] = _hits
check("no_panel_repeats_the_File_menu_export_entry", not _ex_dupes,
      f"`export_map.document` is drawn in {_ex_dupes} -- the File menu already "
      f"is that door, and a signpost is not a second one")
# ...and the door everything defers to has to actually be there.  This was
# already the only export button; with `MAP_PT_export` gone it is also the only
# mention of export anywhere in the tab, so the arm carries more than it did.
# `_draw_funcs` hangs off the DISPATCHER, not the class (addon CLAUDE.md,
# "Menu wiring").
check("the_File_Export_entry_everything_defers_to_exists",
      exp.menu_func in getattr(bpy.types.TOPBAR_MT_file_export.draw,
                               "_draw_funcs", []),
      str([getattr(f, "__name__", "?") for f in
           getattr(bpy.types.TOPBAR_MT_file_export.draw, "_draw_funcs", [])]))

# --- Amendment 4: the workspace is write-once, and this is revision 2 -------
# `ensure_on_import` looked a workspace up BY NAME and, finding one, switched
# to it and never rebuilt -- so an artist holding a workspace from the previous
# layout would have imported a map, been switched into the OLD panes, and
# reported that nothing changed, for the third time.  `build`'s `_free_name()`
# would meanwhile have handed a second workspace the name `Map.001`.
#
# A `Map` workspace with a STALE tag is ours and is rebuilt.  A `Map` workspace
# with NO tag was not built by us and is left alone -- which is what makes the
# removal safe rather than presumptuous.
_wsn = mod_ws.WORKSPACE_NAME
# Sentinel, never a bare attribute read: a check that ABORTS takes the whole
# report with it, and "no report written" reads as a broken harness rather
# than as the one arm that is actually red.
_LV = getattr(mod_ws, "LAYOUT_VERSION", "<no LAYOUT_VERSION>")
_live_ws = bpy.data.workspaces.get(_wsn)
check("a_workspace_we_built_says_which_layout_it_is",
      _live_ws is not None
      and _live_ws.get("exmateria_map/layout_version") == _LV,
      f"tag={None if _live_ws is None else _live_ws.get('exmateria_map/layout_version')!r} "
      f"vs LAYOUT_VERSION={_LV!r}")
if _live_ws is not None:
    _live_ws["exmateria_map/layout_version"] = -1        # a previous revision
    try:
        _act = mod_ws.ensure_on_import(bpy.context)
    except Exception as e:
        _act = repr(e)
    _rebuilt = bpy.data.workspaces.get(_wsn)
    check("a_stale_map_workspace_is_REBUILT_not_switched_to", _act == "rebuilt",
          f"ensure_on_import said {_act!r} -- switching lands the artist in "
          f"the old panes and they report that nothing changed")
    check("the_rebuilt_workspace_carries_the_current_layout_version",
          _rebuilt is not None
          and _rebuilt.get("exmateria_map/layout_version") == _LV,
          f"tag={None if _rebuilt is None else _rebuilt.get('exmateria_map/layout_version')!r}")
    check("the_rebuild_keeps_the_name_and_leaves_no_Map_001",
          f"{_wsn}.001" not in bpy.data.workspaces,
          str(sorted(w.name for w in bpy.data.workspaces)))
    # ...and the arm that makes the removal safe: no tag means not ours.
    _mine = bpy.data.workspaces.get(_wsn)
    if _mine is not None and "exmateria_map/layout_version" in _mine.keys():
        del _mine["exmateria_map/layout_version"]
    try:
        _act2 = mod_ws.ensure_on_import(bpy.context)
    except Exception as e:
        _act2 = repr(e)
    _after = bpy.data.workspaces.get(_wsn)
    check("an_untagged_map_workspace_is_the_artists_and_is_left_alone",
          _act2 == "switched" and _after is not None
          and "exmateria_map/layout_version" not in _after.keys(),
          f"ensure_on_import said {_act2!r} and the workspace now tags "
          f"{None if _after is None else _after.get('exmateria_map/layout_version')!r} "
          f"-- a workspace we did not build is not ours to remove OR to claim")
else:
    check("a_stale_map_workspace_is_REBUILT_not_switched_to", False,
          "no Map workspace to stage the arm on")

# --- decision 5's in-panel half: the panel is a STATUS ROW ------------------
# Reports render into the LOG pane now -- a running Text datablock, selectable,
# in sequence, with a header per entry, because "I pushed, then exported, and
# the export refused" is a sequence the three-key model cannot express.  So the
# in-panel block stops being a second, worse copy of it and goes back to being
# one status row plus the copy button: the `[:12]` cut, the `[:3]` refusal cut,
# the 88-column wrap and `exmateria_map_report_expanded` all lose their reason
# to exist.  Amendment 3 deferred this waiting for exactly the column space
# Amendment 4 recovers.
#
# **Refusals stay in-panel in full** -- that rule is untouched, and "in full"
# means every refusal LINE, none dropped and none cut short.  A refusal is the
# whole reason the report exists, so it is never behind a disclosure triangle.
# Driven through the LIGHTING BAKE panel. `_stored_report` is shared and has
# lost two of its three callers to the salience pass -- `MAP_PT_export` was
# deleted outright, and `MAP_PT_live_push` dropped its call when the artist
# ruled that a run's output is console output, not panel content. `_bake_report`
# is the last caller, so the contract is graded there rather than retired with
# the subjects that happened to be named first. Every arm below is about
# `_stored_report`'s behaviour, which is unchanged.
#
# NOTE for whoever removes the bake's report next: this whole block, plus
# `_stored_report` and `MAP_OT_copy_report`, goes with it. The claim would then
# be the Log's alone, and needs an arm saying refusals reach it in full.
_mk = exp.markers(bpy.context.scene)[0]
_REP_KEY = "exmateria_map/last_bake"
_saved_rep = _mk.get(_REP_KEY)
_long_refusal = "REFUSE: " + "the reason lives at the end of the line, " * 5
_planted = ([f"informational line {i}" for i in range(20)]
            + [f"REFUSE: reason {i}" for i in range(6)]
            + [_long_refusal])
_mk[_REP_KEY] = json.dumps(_planted)
_rep_sink = _SinkLayout()
try:
    mod_bake.MAP_PT_lighting_bake.draw(
        _panel_shim(mod_bake.MAP_PT_lighting_bake, _rep_sink), bpy.context)
    _rep_err = None
except Exception as e:
    _rep_err = f"{type(e).__name__}: {e}"
check("the_panel_drew_its_stored_report", _rep_err is None, str(_rep_err))
_rep_labels = [r[1] for r in _rep_sink.drawn if r[0] == "label"]
_want_ref = [ln for ln in _planted if ln.startswith("REFUSE")]
check("every_refusal_is_drawn_in_the_panel_whole_and_in_order",
      [t for t in _rep_labels if t.startswith("REFUSE")] == _want_ref,
      f"drew {[t for t in _rep_labels if t.startswith('REFUSE')]!r} for "
      f"{_want_ref!r} -- a cut at three, or an 88-column wrap that splits the "
      f"seventh, and the reason stops being readable where it lives")
check("the_panel_does_not_reprint_the_whole_report",
      not [t for t in _rep_labels if t.startswith("informational line")],
      f"{len([t for t in _rep_labels if t.startswith('informational line')])} "
      f"of 20 informational lines redrawn -- that is the Log's job now")
check("the_panel_never_says_it_cut_something",
      not [t for t in _rep_labels if "more" in t and "..." in t],
      str([t for t in _rep_labels if "more" in t]))
check("the_status_row_counts_what_it_is_not_showing",
      any(f"{len(_planted)} line(s)" in t and f"{len(_want_ref)} refusal(s)" in t
          for t in _rep_labels),
      f"no row states the totals: {_rep_labels[:4]!r}")
check("the_report_still_offers_the_copy_button",
      "map.copy_report" in [r[1] for r in _rep_sink.drawn
                            if r[0] == "operator"],
      str(_rep_sink.drawn))
# The disclosure triangle is RETIRED, not merely defaulted open: the property
# is what made the refusals hideable in the first place.
check("the_report_disclosure_triangle_is_retired",
      not hasattr(bpy.types.Object, "exmateria_map_report_expanded"),
      "exmateria_map_report_expanded is still registered on Object")
check("the_panel_draws_no_disclosure_prop",
      not [r for r in _rep_sink.drawn
           if r[0] == "prop" and r[2] == "exmateria_map_report_expanded"],
      str([r for r in _rep_sink.drawn if r[0] == "prop"]))
if _saved_rep is None:
    del _mk[_REP_KEY]
else:
    _mk[_REP_KEY] = _saved_rep

# --- decision 6: the rig draws as a TABLE, not a list -----------------------
# `Light 1 | Light 2 | Light 3` side by side -- ~24 rows to ~9, measured at the
# sidebar's 280 px as ~610 px of column against ~175 px
# (`workspace/README.md`, gate 3; `gate3_rig_table_vs_list.png`).  The data is
# three instances of ONE shape and a list makes them incomparable: you cannot
# see that light 2 is twice light 1 without scrolling between them.  A `UIList`
# was rejected -- it is Blender's stock answer for N-of-a-kind and it shows one
# and hides two, which is the opposite of the goal.
#
# `_FakeLayout` cannot grade this: it returns ITSELF from every container, so a
# stacked list and a three-column table record identically -- and side-by-side
# is the whole of the decision.  This one remembers its shape.
class _TreeLayout:
    def __init__(self, kind="root"):
        self.kind = kind
        self.children = []
        self.items = []
        self.enabled = True

    def _child(self, kind):
        c = _TreeLayout(kind)
        self.children.append(c)
        return c

    def box(self, **kw):
        return self._child("box")

    def row(self, **kw):
        return self._child("row")

    def column(self, align=False, **kw):
        return self._child("column")

    def grid_flow(self, **kw):
        return self._child("grid_flow")

    def separator(self, **kw):
        return self

    def label(self, text="", icon=None, **kw):
        self.items.append(("label", text))
        return self

    def prop(self, data, prop_name, **kw):
        self.items.append(("prop", prop_name))
        return self

    def operator(self, idname, text="", icon=None, **kw):
        self.items.append(("operator", idname))

        class _Op:
            pass
        return _Op()

    def operator_menu_enum(self, idname, prop, **kw):
        self.items.append(("operator", idname))
        return self

    def menu(self, mid, text="", icon=None, **kw):
        self.items.append(("menu", mid))
        return self

    def template_palette(self, *a, **kw):
        return self

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def all_items(self):
        return [it for n in self.walk() for it in n.items]


# The LIGHTING BAKE panel, which owns the rig since 2026-08-27. These arms are
# about decision 6's three-column table and are unchanged by the move; only the
# panel driven changed, which is what a move should cost a test.
_rig_tree = _TreeLayout()
try:
    mod_bake.MAP_PT_lighting_bake.draw(
        _panel_shim(mod_bake.MAP_PT_lighting_bake, _rig_tree), bpy.context)
    _rig_err = None
except Exception as e:
    _rig_err = f"{type(e).__name__}: {e}"
check("the_light_panel_drew_for_the_rig_shape_arms", _rig_err is None,
      str(_rig_err))
# A row whose children are three columns, one light's controls in each.
_tables = [[[p for k, p in c.items if k == "prop"]
            for c in n.children if c.kind == "column"]
           for n in _rig_tree.walk()
           if n.kind == "row"
           and len([c for c in n.children if c.kind == "column"]) == 3]
check("the_rig_draws_the_three_lights_SIDE_BY_SIDE",
      _tables == [[["gain_1", "dir_1"], ["gain_2", "dir_2"],
                   ["gain_3", "dir_3"]]],
      f"{_tables} -- one row of three columns, one light in each, is what "
      f"makes them comparable; stacked, you cannot see that light 2 is twice "
      f"light 1 without scrolling between them")
_rig_items = _rig_tree.all_items()
_rig_props = [p for k, p in _rig_items if k == "prop"]
# Nothing AUTHORABLE is lost by re-laying it out.
check("the_table_still_draws_every_authorable_rig_control",
      set(_rig_props) >= {"ambient", "gain_1", "gain_2", "gain_3",
                          "dir_1", "dir_2", "dir_3"}
      and all(_rig_props.count(n) == 1 for n in
              ("gain_1", "gain_2", "gain_3", "dir_1", "dir_2", "dir_3")),
      str(_rig_props))
# The gradient collapses to ONE LINE.  Its six values stay in the Override, so
# the rig is still the whole 45 bytes; they stop occupying a third of the box
# to be un-editable, which reads as broken rather than as deliberate.  This is
# NOT the sky-gradient work, which is parked.
check("the_gradient_collapses_to_one_line",
      "gradient" not in _rig_props
      and len([t for k, t in _rig_items
               if k == "label" and "gradient" in t.lower()]) == 1,
      f"gradient drawn as a prop: {'gradient' in _rig_props}; gradient "
      f"labels: {[t for k, t in _rig_items if k == 'label' and 'gradient' in t.lower()]}")
_ov_now = mod.find_override(_mk, int(_mk["exmateria_map/preview_state"]))
check("the_gradient_values_are_still_CARRIED",
      _ov_now is not None and len(list(_ov_now.gradient)) == 6,
      f"the Override must still hold all six bytes or the rig stops being 45: "
      f"{None if _ov_now is None else list(_ov_now.gradient)}")

# --- Amendment 4: the console runs along the BOTTOM -------------------------
# "Can you move the CONSOLE looking panel so it runs across the bottom like in
# most sane programs with a console. And then on the right (where the console
# is now) can you move and uncollapse the controls."
#
# The band is SOURCED by converting the inherited Timeline, and splitting only
# as a fallback.  The workspace is a duplicate, so it inherits the factory
# Outliner + Properties column -- the Log was never the rightmost thing on
# screen, and "where the console is now" named the middle-right slot.  And
# Blender's own Timeline already stops at that column, so a bottom band
# spanning ~2107 of 2560 px is the engine's idiom rather than a truncation.
# Converting it also costs no split and takes no height from the viewport.
#
# `-b` can grade the SOURCE and nothing about the panes: the screen never lays
# out, the timers never tick, and `area.type` is the field that LIES.  The
# geometry belongs to `workspace/workspace_probe.py` phase `build`, and that
# restraint is deliberate -- see this section's header.
_factory = bpy.data.workspaces.get("Layout")
_factory_screen = _factory.screens[0] if _factory else None
_strip = (mod_ws._timeline(_factory_screen)
          if _factory_screen is not None and hasattr(mod_ws, "_timeline")
          else None)
check("the_factory_layout_still_ships_the_timeline_the_band_comes_from",
      _factory_screen is not None
      and any(a.type == "DOPESHEET_EDITOR" and a.ui_type == "TIMELINE"
              for a in _factory_screen.areas),
      f"no Timeline in the factory Layout: "
      f"{[] if _factory_screen is None else sorted((a.type, a.ui_type) for a in _factory_screen.areas)} "
      f"-- the fallback split is then the only path and this arm is blind")
check("the_log_band_is_sourced_from_the_inherited_timeline",
      _strip is not None and _strip.ui_type == "TIMELINE",
      f"_timeline() found {getattr(_strip, 'ui_type', None)!r} -- the band has "
      f"to come from the strip the duplicate already has, or the viewport pays "
      f"for it in height")
# ...and it must not answer YES on a screen that has none, or the fallback
# split is unreachable and every workspace without a Timeline gets no Log.
class _StripArea:
    def __init__(self, t, ui):
        self.type, self.ui_type = t, ui


class _StripScreen:
    def __init__(self, pairs):
        self.areas = [_StripArea(t, u) for t, u in pairs]


check("the_band_finder_says_no_when_there_is_no_timeline",
      hasattr(mod_ws, "_timeline")
      and mod_ws._timeline(_StripScreen(
          [("VIEW_3D", "VIEW_3D"), ("OUTLINER", "OUTLINER")])) is None,
      "a screen with no Timeline was handed one anyway")
# A DOPESHEET area is not automatically the Timeline: `ui_type` is what tells
# them apart, and the Animation workspace ships both.
check("the_band_finder_does_not_take_a_dope_sheet_for_a_timeline",
      hasattr(mod_ws, "_timeline")
      and mod_ws._timeline(_StripScreen(
          [("DOPESHEET_EDITOR", "DOPESHEET")])) is None,
      "the artist's dope sheet would be converted into a Log")
# The Log pane is DROPPED, not moved: the viewport widens to fill it and its
# own `Map` sidebar comes to rest against Blender's Properties column.  A third
# pane would buy a second 280 px column, which is width the panels cannot use
# -- they are too TALL, not too narrow.
check("there_is_no_second_vertical_split_factor_left",
      not hasattr(mod_ws, "LOG_SPLIT"),
      "LOG_SPLIT still exists -- the layout still cuts a third pane out of the "
      "viewport")

# --- NO_EDITOR_HINT is MOVING, not going away -------------------------------
# Decision 3 would have DELETED this block: drawn inside an Image Editor the
# "no Image Editor open / every editor holds a render" box and its wrapped
# instructions are unreachable.  Amendment 4 keeps Paint in the viewport as
# well, where the block is true -- so it survives, in that copy only.  The
# operator's own report (`paint_sheet_warns_when_no_editor` and the three arms
# around it) is unchanged and stays where it is; what moved is the PANEL half,
# and "not deleted" is a claim about a constant until something renders it.
_ie = [(_sc, _a, _a.type) for _sc in bpy.data.screens for _a in _sc.areas
       if _a.type == "IMAGE_EDITOR"]
for _sc, _a, _t in _ie:
    _a.type = "VIEW_3D"
check("the_hint_arm_really_leaves_no_image_editor",
      bool(_ie) and not pnt.image_editor_spaces(),
      f"{len(_ie)} editor(s) taken away, {len(pnt.image_editor_spaces())} "
      f"still reachable -- without this the arm below is blind")
_hint_v, _hint_i = _SinkLayout(), _SinkLayout()
try:
    pnt.MAP_PT_paint_view.draw(
        _panel_shim(pnt.MAP_PT_paint_view, _hint_v), bpy.context)
    pnt.MAP_PT_paint.draw(_panel_shim(pnt.MAP_PT_paint, _hint_i), bpy.context)
    _hint_err = None
except Exception as e:
    _hint_err = f"{type(e).__name__}: {e}"
check("both_paint_copies_draw_with_no_image_editor_anywhere",
      _hint_err is None, str(_hint_err))
_hv_txt = " ".join(r[1] for r in _hint_v.drawn if r[0] == "label")
_hi_txt = " ".join(r[1] for r in _hint_i.drawn if r[0] == "label")
check("the_viewport_copy_still_says_where_the_sheet_went",
      "no Image Editor open" in _hv_txt
      and all(w in _hv_txt for w in ("UV Editing", "Texture Paint")),
      f"the hint is not rendered by the copy that is allowed to say it: "
      f"{_hv_txt!r}")
check("the_image_editor_copy_does_not_say_it",
      "no Image Editor open" not in _hi_txt and "UV Editing" not in _hi_txt,
      f"told 'no Image Editor open' INSIDE one, the artist is reading a lie: "
      f"{_hi_txt!r}")
for _sc, _a, _t in _ie:                    # put the artist's screens back
    _a.type = _t
check("the_hint_arm_put_the_editors_back",
      len(pnt.image_editor_spaces()) == len(_ie),
      f"{len(pnt.image_editor_spaces())} of {len(_ie)} restored")

# --- the picker and the palette in ONE tab ----------------------------------
# SEEDED, 5/5 caught, each by exactly ONE arm:
#   `color=True` put back ............. the_palette_template_is_called_with_the_arity_5_2_accepts
#   any palette blessed as legal ...... a_palette_we_did_not_build_is_never_shown_as_the_legal_set
#   grid drawn as well as the palette . the_reference_grid_stands_down_when_the_palette_is_live
#   the picker left in the Tool tab ... the_paint_panel_reaches_for_the_BRUSH_colour
#   the template pointed at the shelf . the_paint_panel_hands_the_sixteen_to_the_brush_in_one_click
# The first is the historical defect itself, which is why it is seeded rather
# than merely described.
# Reported from use: "map has the palette I need to eye drop from, tool has the
# color picker -- and I can't see them at the same time."  A sidebar region
# shows exactly ONE tab, always, so this cannot be arranged around -- it is the
# same wall Amendment 4 hit with Blender's `Item` tab, and it takes the same
# answer: bring what is needed into OUR tab rather than chase theirs.
#
# Measured headful (probe phase `swatches`), because two of the three facts are
# claims about Blender that introspection cannot settle:
#   - `TOOLS` is a real 56 px region in an Image Editor, and a panel registered
#     to it does NOT draw.  There is no second sidebar to have.
#   - `template_palette` RENDERS -- in Texture Paint AND in Edit Mode, with its
#     add/remove buttons greyed in the latter.  The shipped comment saying it
#     "draws NOTHING without an active paint brush" was measuring
#     `TypeError: UILayout.template_palette(): takes at most 2 arguments, got
#     3` -- the `color=True` kwarg, which does not exist on 5.2.  A `draw` that
#     raises renders everything emitted BEFORE it and nothing after, which is
#     indistinguishable from a template that drew nothing.  That is how the
#     wrong reason got recorded.
# The template's swatches are natively click-to-arm, so the eyedrop round trip
# the artist was making disappears rather than being made easier.
_ip = bpy.context.scene.tool_settings.image_paint
_shelf_now = bpy.data.palettes.get(pnt.PALETTE_SHELF)
check("the_swatch_arms_have_a_shelf_to_grade",
      _shelf_now is not None and len(_shelf_now.colors) == 16,
      f"shelf={None if _shelf_now is None else len(_shelf_now.colors)} -- "
      f"without it the arms below cannot tell the branches apart")
# THE PICKER HALF IS NOT GRADEABLE HERE, and this is the control that says so
# rather than an arm that quietly never runs. `ImagePaint.brush` is READ-ONLY
# on 5.2 -- a brush is resolved from the asset system on entering a paint mode,
# `bpy.data.brushes` is empty under `-b --factory-startup`, and assigning one
# raises `bpy_struct: attribute "brush" from "ImagePaint" is read-only`. So the
# `if brush is not None` branch is unreachable in background mode whatever the
# panel does. Written to go RED if a future Blender makes it assignable, which
# is the signal to replace the AST arm below with a real one.
try:
    _ip.brush = (bpy.data.brushes.get("_check_brush")
                 or bpy.data.brushes.new("_check_brush", mode="TEXTURE_PAINT"))
    _brush_assignable = True
except Exception as e:                                             # noqa: BLE001
    _brush_assignable = f"{type(e).__name__}: {e}"
check("the_brush_is_unreachable_headless_so_the_PROBE_grades_the_picker",
      getattr(_ip, "brush", None) is None and _brush_assignable is not True,
      f"brush={getattr(getattr(_ip, 'brush', None), 'name', None)!r}, "
      f"assignment={_brush_assignable!r} -- a brush can be had headless now, "
      f"so grade the picker here instead of on the AST")
_ip.palette = _shelf_now


def _draw_paint_panel():
    sink = _SinkLayout()
    try:
        pnt.MAP_PT_paint.draw(_panel_shim(pnt.MAP_PT_paint, sink), bpy.context)
        return sink, None
    except Exception as e:
        return sink, f"{type(e).__name__}: {e}"


_ps, _ps_err = _draw_paint_panel()
check("the_paint_panel_drew_with_the_shelf_bound", _ps_err is None, str(_ps_err))
_pal_calls = [r for r in _ps.drawn if r[0] == "palette"]
# `==`, NOT `is`: bpy hands back a FRESH Python wrapper for a nested struct on
# every access, so `context.tool_settings.image_paint` is a different object
# each time and an identity test fails on the correct call. (ID datablocks ARE
# wrapper-cached, which is why the palette comparisons either side of this hold
# under `is` -- the two cases do not behave the same, so compare by `==`.)
check("the_paint_panel_hands_the_sixteen_to_the_brush_in_one_click",
      len(_pal_calls) == 1 and _pal_calls[0][1] == _ip
      and _pal_calls[0][2] == "palette",
      f"{_pal_calls} -- `template_palette` on the paint settings is what makes "
      f"a swatch ARM the brush; a read-only `prop(slot, 'color')` grid only "
      f"opens a picker on the shelf slot, which edits the SHELF and not the "
      f"CLUT, and is overwritten on the next sync")
# TWO arguments.  `color=True` raises `TypeError: takes at most 2 arguments,
# got 3` on 5.2 -- and a `draw` that raises renders everything emitted BEFORE
# it and nothing after, which is why the shipped comment recorded the symptom
# ("the label rendered and the swatches did not") against the wrong cause.
check("the_palette_template_is_called_with_the_arity_5_2_accepts",
      len(_pal_calls) == 1 and not _pal_calls[0][3] and not _pal_calls[0][4],
      f"extra args={_pal_calls[0][3] if _pal_calls else None}, "
      f"kwargs={_pal_calls[0][4] if _pal_calls else None}")
# ...and it must NOT also draw the read-only grid, which would be sixteen more
# rows of the same colours in a column two decisions just spent shortening.
# `paint_owner`, not `_ip.brush`: the colour row now draws whoever actually
# owns the colour, and with `use_unified_color` at its default that is
# `unified_paint_settings` -- so an exclusion written against the brush would
# count the painting colour as a stray reference swatch.
# `==`, NOT `is`, on the owner -- for the third time in this file. bpy hands
# back a FRESH Python wrapper for a nested struct on every access, so
# `paint_owner`'s `UnifiedPaintSettings` is a different object each time it is
# read and an identity test misses the correct row. (`_ip.brush` beside it is
# compared with `is` because it is None here, which is a different question.)
_colour_owner = pnt.paint_owner(_ip, "use_unified_color")
_slot_props = [r for r in _ps.drawn if r[0] == "prop" and r[2] == "color"
               and r[1] is not getattr(_ip, "brush", None)
               and not (r[1] == _colour_owner)]
check("the_reference_grid_stands_down_when_the_palette_is_live",
      not _slot_props,
      f"{len(_slot_props)} read-only swatch(es) drawn as well as the palette")

# The safety arm, and the reason the branch is on OUR shelf rather than on
# `ip.palette` merely existing: the artist can point the palette anywhere from
# the `Tool` tab, and a palette we did not build is not the legal set.
_theirs = (bpy.data.palettes.get("_check_not_ours")
           or bpy.data.palettes.new("_check_not_ours"))
_ip.palette = _theirs
_ps2, _ps2_err = _draw_paint_panel()
check("the_paint_panel_drew_with_a_foreign_palette", _ps2_err is None,
      str(_ps2_err))
_pal2 = [r for r in _ps2.drawn if r[0] == "palette"]
_slot2 = [r for r in _ps2.drawn if r[0] == "prop" and r[2] == "color"
          and r[1] is not getattr(_ip, "brush", None)
          and not (r[1] == _colour_owner)]
check("a_palette_we_did_not_build_is_never_shown_as_the_legal_set",
      not _pal2 and len(_slot2) == 16,
      f"template calls={_pal2}, reference swatches={len(_slot2)} -- pointing "
      f"the palette elsewhere in the `Tool` tab must fall back to the "
      f"read-only sixteen, not present someone else's colours as legal")
_ip.palette = _shelf_now
# The picker itself. This used to be graded on the AST -- "the branch cannot be
# reached in this mode" -- and that stopped being true with the fix it is now
# grading: the row draws whoever OWNS the colour, and at the default
# `use_unified_color = True` that owner is `unified_paint_settings`, which
# exists headless. Only the `use_unified_color = False` leg still needs a brush,
# and the control above is what says so.
#
# SEEDED, 2/2 caught here; 14/14 across this leg, listed per block. Run each
# seed with its own `BLENDER_USER_RESOURCES` -- see the note below the seed
# block for why a shared one grades the wrong code.
# SEEDED, 2/2 caught:
#   `prop(_ip.brush, "color")` put back .. the_paint_panel_draws_the_colour_that_PAINTS
#     (headless the brush is None, so the row vanishes entirely and the arm
#      reads 0 -- which is the same defect the artist saw as a BLACK swatch)
#   the unified toggle dropped ........... the_panel_says_WHICH_colour_is_the_live_one
#
# Measured headful, probe phases `brush` and `swatchclick`: one stroke armed
# with `brush.color` red and `unified.color` blue lands BLUE at the default and
# RED with the flag off, and a swatch click -- clicked for real through
# `Window.event_simulate` -- writes the same owner and leaves the other one
# untouched. So `brush.color` is neither what paints nor what the click arms,
# and drawing it is why `painting with:` photographed BLACK.
_paint_colour = [r for r in _ps.drawn if r[0] == "prop" and r[2] == "color"
                 and r[1] == _colour_owner]
check("the_paint_panel_draws_the_colour_that_PAINTS",
      _colour_owner is not None and len(_paint_colour) == 1,
      f"owner={_colour_owner!r}, rows={len(_paint_colour)} -- with "
      f"use_unified_color={_ip.unified_paint_settings.use_unified_color} the "
      f"colour that lands is on {type(_colour_owner).__name__}, and a row "
      f"drawn against the brush shows a value that neither paints nor "
      f"receives the swatch click")
_unified_toggles = [r for r in _ps.drawn if r[0] == "prop"
                    and r[2] in ("use_unified_size", "use_unified_strength")]
check("the_panel_says_WHICH_colour_is_the_live_one",
      len(_unified_toggles) >= 1,
      f"{_unified_toggles} -- the unified flags decide whether the number "
      f"above them is the one that paints, they default to ON, and that is "
      f"exactly why `brush.size = 1` went nowhere; a row without its flag is "
      f"a number the artist cannot trust")

# --- the brush controls, and the SEED ---------------------------------------
# "I like how you put the tool options at the bottom now so I can have both --
# but can we also expose other things down there, especially brush size. And
# also default the brush to pixel and size to 1."
#
# The second half is a SEED, not the force ADR-0004 asked for: *"I don't know,
# I want to force it -- just when I open the workspace, that should be my brush
# as a default."*  So it needs BOTH arms -- it lands on a fresh scene, and it
# does NOT come back after the artist has moved the value.  The second is the
# one that separates a default from a force, and it is the one a later session
# would drop.
#
# SEEDED, 5/5 caught -- three by exactly one arm, two by their own arm AND a
# second that legitimately depends on the same property:
#   the mark never written ............... the_seed_does_not_come_back_after_the_artist_moves_it
#   the mark written with nothing set .... a_seed_that_set_nothing_does_not_mark_itself_done
#                                          (+ the real-scene control, which is
#                                           the same claim about a real Scene)
#   size written to the brush always ..... the_seed_writes_the_size_to_whoever_OWNS_it
#                                          (+ the_button_is_the_way_BACK, which
#                                           reads the size off the same owner)
#   the falloff left alone ............... the_seed_sets_the_falloff_the_gate_needs
#   `force` ignored ...................... the_button_is_the_way_BACK_to_the_default
#
# The seeded audit itself needed one fix before it could say anything: the
# harness's RUNTIME checks grade the INSTALLED addon, not the tree under `PKG`
# (`addon_enable` imports `exmateria_map` from `bpy.utils.user_resource`, so the
# later `sys.path.insert` hands back an already-cached module), and four seeds
# run in parallel installed over each other -- three of them were graded against
# a fourth's code and the audit read 4/13. Give each run its own
# `BLENDER_USER_RESOURCES` or run them one at a time. Only the AST arms were
# honest under the race, because those do read the tree.
_size_rows = [r for r in _ps.drawn if r[0] == "prop" and r[2] == "size"]
check("the_panel_exposes_the_brush_size_in_the_Map_tab",
      len(_size_rows) == 1
      and _size_rows[0][1] == pnt.paint_owner(_ip, "use_unified_size"),
      f"{_size_rows} -- a sidebar shows ONE tab, so the size the artist asked "
      f"for has to be drawn here, on whoever owns it")
check("the_panel_offers_the_way_back_to_the_pixel_default",
      any(r[0] == "operator" and r[1] == pnt.MAP_OT_seed_brush.bl_idname
          for r in _ps.drawn),
      f"{[r for r in _ps.drawn if r[0] == 'operator']}")


class _FakeUPS:
    def __init__(self, size=100, colour=True, unified_size=True):
        self.size = size
        self.color = (0.0, 0.0, 0.0)
        self.strength = 1.0
        self.use_unified_color = colour
        self.use_unified_size = unified_size
        self.use_unified_strength = False


class _FakeBrush:
    def __init__(self):
        self.size = 70
        self.color = (0.0, 0.0, 0.0)
        self.strength = 1.0
        self.curve_distance_falloff_preset = "CUSTOM"


class _FakeIP:
    def __init__(self, brush=True, **kw):
        self.brush = _FakeBrush() if brush else None
        self.unified_paint_settings = _FakeUPS(**kw)


class _FakeScene(dict):
    """A scene is a dict-like ID with `tool_settings` -- which is all
    `seed_brush` touches, and the whole reason it takes a scene rather than a
    context: the seam is testable in a mode that cannot hold a brush."""

    def __init__(self, ip):
        dict.__init__(self)
        self.tool_settings = type("_TS", (), {"image_paint": ip})()


_sc = _FakeScene(_FakeIP())
check("the_seed_sets_the_falloff_the_gate_needs",
      pnt.seed_brush(_sc) == "seeded"
      and _sc.tool_settings.image_paint.brush.curve_distance_falloff_preset
      == "CONSTANT",
      f"falloff="
      f"{_sc.tool_settings.image_paint.brush.curve_distance_falloff_preset!r} "
      f"-- measured 18.0% of a stroke off-palette at the `CUSTOM` default and "
      f"0.0% at `CONSTANT` (probe phase `brush`), and the export gate is an "
      f"exact byte match, so a feathered edge is a refusal and not a soft one")
check("the_seed_writes_the_size_to_whoever_OWNS_it",
      _sc.tool_settings.image_paint.unified_paint_settings.size == 1
      and _sc.tool_settings.image_paint.brush.size == 70,
      f"unified={_sc.tool_settings.image_paint.unified_paint_settings.size}, "
      f"brush={_sc.tool_settings.image_paint.brush.size} -- "
      f"`use_unified_size` defaults to True and REPLACES the brush's value, "
      f"so a size written to the brush is a size the artist never paints with")
_sc_off = _FakeScene(_FakeIP(unified_size=False))
pnt.seed_brush(_sc_off)
check("the_seed_follows_the_flag_when_the_artist_turns_it_off",
      _sc_off.tool_settings.image_paint.brush.size == 1
      and _sc_off.tool_settings.image_paint.unified_paint_settings.size == 100,
      f"brush={_sc_off.tool_settings.image_paint.brush.size}, "
      f"unified={_sc_off.tool_settings.image_paint.unified_paint_settings.size}")
# THE ARM THAT MAKES IT A DEFAULT. The artist sets the size to 4; it stays 4,
# this session and across a save, because `tool_settings` lives in the `.blend`
# and nothing re-asserts it.
_sc.tool_settings.image_paint.unified_paint_settings.size = 4
_sc.tool_settings.image_paint.brush.curve_distance_falloff_preset = "SMOOTH"
_again = pnt.seed_brush(_sc)
check("the_seed_does_not_come_back_after_the_artist_moves_it",
      _again == "already"
      and _sc.tool_settings.image_paint.unified_paint_settings.size == 4
      and _sc.tool_settings.image_paint.brush.curve_distance_falloff_preset
      == "SMOOTH",
      f"returned {_again!r}, size="
      f"{_sc.tool_settings.image_paint.unified_paint_settings.size}, falloff="
      f"{_sc.tool_settings.image_paint.brush.curve_distance_falloff_preset!r} "
      f"-- ADR-0004 says FORCE and the artist ruled that out; a seed that "
      f"re-asserts is the force under another name")
check("the_button_is_the_way_BACK_to_the_default",
      pnt.seed_brush(_sc, force=True) == "seeded"
      and _sc.tool_settings.image_paint.unified_paint_settings.size == 1,
      f"size={_sc.tool_settings.image_paint.unified_paint_settings.size} -- "
      f"`force` is what `MAP_OT_seed_brush` presses, and it is the only thing "
      f"that may write over the artist")
_sc_none = _FakeScene(_FakeIP(brush=False))
_none_got = pnt.seed_brush(_sc_none)
check("a_seed_that_set_nothing_does_not_mark_itself_done",
      _none_got == "no brush" and not _sc_none.get(pnt.BRUSH_SEED_MARK)
      and _sc_none.tool_settings.image_paint.unified_paint_settings.size == 100,
      f"returned {_none_got!r}, mark={_sc_none.get(pnt.BRUSH_SEED_MARK)!r} -- "
      f"`ImagePaint.brush` is resolved from the asset system on entering a "
      f"paint mode and is None before it, so a seed that marked itself done "
      f"having set nothing is a default that never happened")
# ...which is exactly what the REAL scene does in this mode. The control, again:
# every positive arm above runs on a fake, and this is what says the fake is
# standing in for something `-b` genuinely cannot reach.
_real_scene = bpy.context.scene
_real_got = pnt.seed_brush(_real_scene)
check("the_real_scene_cannot_be_seeded_headless_which_is_why_the_fakes_exist",
      _real_got == "no brush" and not _real_scene.get(pnt.BRUSH_SEED_MARK),
      f"returned {_real_got!r} -- a brush can be had headless now, so replace "
      f"the fakes above with the real thing")
# The brush box on BOTH authoring paths. Decision 17 returns early on a
# converted map -- rightly, because the palette block above is a statement about
# the indexed path -- and the brush is not part of that statement: size and
# falloff are the same question whichever path the artist is on. Read off the
# tree, because reaching the Painting branch needs a converted map.
try:
    _boxes = [n for n in _ast.walk(_tree_func("paint.py", "draw", "_PaintPanel"))
              if isinstance(n, _ast.Call)
              and getattr(n.func, "id", None) == "_brush_box"]
    check("the_brush_box_is_drawn_on_BOTH_authoring_paths",
          len(_boxes) >= 2,
          f"{len(_boxes)} call(s) to `_brush_box` in the panel -- the "
          f"converted-map branch returns early, so a single call at the bottom "
          f"gives the artist brush size on one path and not the other")
except Exception as e:
    check("the_brush_box_is_drawn_on_BOTH_authoring_paths", False, repr(e))
# Both moments the artist named reach it. `build` cannot run here (it needs a
# window to duplicate a workspace into) and `execute` needs a map, so the CALL
# is read off the tree -- a grep would match the docstrings explaining it.
for _rel, _fn, _cls in (("workspace.py", "build", None),
                        ("paint.py", "execute", "MAP_OT_paint_sheet")):
    try:
        _seeds = [n for n in _ast.walk(_tree_func(_rel, _fn, _cls))
                  if isinstance(n, _ast.Call)
                  and getattr(n.func, "attr", None) == "seed_brush"
                  or isinstance(n, _ast.Call)
                  and getattr(n.func, "id", None) == "seed_brush"]
        check(f"the_seed_is_reached_from_{_cls or _rel.split('.')[0]}_{_fn}",
              bool(_seeds),
              f"nothing in {_rel}:{_fn} calls `seed_brush` -- the workspace "
              f"being built is the moment the artist named, and `Paint sheet` "
              f"is the one that still has a brush when it was not")
    except Exception as e:
        check(f"the_seed_is_reached_from_{_cls or _rel.split('.')[0]}_{_fn}",
              False, repr(e))
# The rule itself, both ways round, on a fake that can hold a brush.
# `isinstance`, and no attribute read on the result: an arm that RAISES takes
# the whole run down and reports nothing, which is the same failure shape as a
# panel `draw` that raises. Seeded with the rule inverted, the first version of
# this arm reached `.use_unified_color` on a `_FakeBrush` and killed the file.
_own_on = pnt.paint_owner(_FakeIP(), "use_unified_color")
_own_off = pnt.paint_owner(_FakeIP(colour=False), "use_unified_color")
check("paint_owner_hands_back_the_unified_settings_while_the_flag_is_on",
      isinstance(_own_on, _FakeUPS) and isinstance(_own_off, _FakeBrush),
      f"flag on -> {type(_own_on).__name__}, flag off -> "
      f"{type(_own_off).__name__} -- the flag decides the owner, and this is "
      f"Blender's own rule from `bl_ui/properties_paint_common.py`")
# The attribute genuinely ABSENT, not merely None: `getattr(<None>, flag,
# False)` already copes with None, so a `None` stub grades nothing.
# CAUGHT, because the claim IS "must not raise": seeded with a bare attribute
# read, the uncaught version took the whole run down and reported no checks at
# all -- a fatal that reads as an infrastructure problem rather than as this
# arm going red.
try:
    _own_bare = pnt.paint_owner(type("_NoUPS", (), {"brush": _FakeBrush()})(),
                                "use_unified_size")
except Exception as _e:                                            # noqa: BLE001
    _own_bare = f"{type(_e).__name__}: {_e}"
check("paint_owner_falls_back_to_the_brush_with_no_unified_settings_at_all",
      isinstance(_own_bare, _FakeBrush),
      f"{type(_own_bare).__name__} -- a missing `unified_paint_settings` must "
      f"not raise inside a panel `draw`: a draw that raises renders everything "
      f"emitted before it and nothing after")

json.dump({"checks": checks, "counts": {"faces": len(polys), "verts": n_distinct}},
          open(OUT, "w"), indent=1)
print(f"CHECKS: {sum(checks.values())}/{len(checks)} passed")
if not all(checks.values()):
    raise SystemExit(1)
'''


def ensure_addon():
    """Zip the addon (the check script installs it inside its own process —
    --factory-startup gives each headless run a scratch user dir, so nothing
    survives across invocations)."""
    zf_path = TMP / "exmateria_map.zip"
    if zf_path.exists():
        zf_path.unlink()
    with zipfile.ZipFile(zf_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(ADDON_DIR.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(ADDON_DIR.parent))
    return zf_path


def main():
    # PREFLIGHT, before anything is built: no suite here may start a Blender
    # without isolating it. This is the guard for the defect that had a test
    # run overwrite the artist's installed addon -- see `blender_env`. It runs
    # first because a green suite that installed over their Blender is worse
    # than a red one.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from blender_env import audit_launchers
    _offenders = audit_launchers()
    if _offenders:
        print("FAIL: a suite starts Blender without isolating it --")
        for _o in _offenders:
            print("   ", _o)
        print("Add `env=isolated_env()` to the subprocess.run call "
              "(tests/blender_env.py).")
        sys.exit(1)
    TMP.mkdir(exist_ok=True)
    if REPORT.exists():
        REPORT.unlink()  # never grade on a stale report
    zf_path = ensure_addon()
    staged = TMP / "MAP001.a0.stub.json"
    staged.write_text(FIXTURE.read_text())

    # The addon discovers sheet sidecars next to the document first (§4);
    # the staged copy lives in TMP, so the tracked sidecars move with it.
    fx = json.loads(FIXTURE.read_text())
    sidecars = [st.get("texture_sheet") for st in fx["map_states"]
                if st.get("texture_sheet")]
    samples = []
    for s in sidecars:
        src = FIXTURES / s
        (TMP / s).write_bytes(src.read_bytes())
        stem = s[:-4] if s.endswith(".png") else s
        spath = FIXTURES / (stem + ".samples.json")
        if spath.exists():
            samples.append([s, str(spath)])

    script = TMP / "run_check.py"
    script.write_text(SCRIPT_TEMPLATE
                      .replace("@ADDONPKG@", str(ADDON_DIR.parent))
                      .replace("@ZIP@", str(zf_path))
                      .replace("@JSON@", str(staged))
                      .replace("@OUT@", str(REPORT))
                      .replace("@SAMPLES@", json.dumps(samples)))
    # Isolate this Blender from the artist's OWN install. Without it the
    # `addon_install` in the script above overwrites the addon they are
    # clicking, and `addon_enable` then grades that copy rather than this
    # tree. `--factory-startup` does NOT do this -- see `blender_env`.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from blender_env import isolated_env
    proc = subprocess.run([sys.argv[1] if len(sys.argv) > 1 else "blender",
                           "--background", "--factory-startup", "--python", str(script)],
                          capture_output=True, text=True,
                          env=isolated_env())
    sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stdout.write("\n[stderr]\n" + proc.stderr[-4000:])
    if not REPORT.exists():
        print("\nFAIL: no report written")
        sys.exit(1)
    report = json.loads(REPORT.read_text())
    checks = report["checks"]
    failed = [n for n, ok in checks.items() if not ok]
    print(f"\nSUMMARY: {len(checks) - len(failed)}/{len(checks)} checks passed "
          f"({report.get('counts')})")
    if failed:
        print("FAILED:", ", ".join(failed))
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
