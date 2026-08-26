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

# Install + enable the addon in THIS process (--factory-startup uses a scratch
# user dir, so nothing survives across headless invocations).
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

l0 = [r for r in (doc.get("terrain") or []) if r.get("level", 0) == 0]
for r in l0:
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
        if k not in tob:
            check(f"tile_declared_{r['x']}_{r['z']}", False, f"missing declared field {k}")
            ok = False
            break
        if tob[k] != want or tob[f"{k}_shadow"] != want:
            ok = False
    # absent field is NOT zero: an undeclared payload field has no property
    for k in mod.TILE_PAYLOAD_FIELDS:
        if k in r:
            continue
        if k in tob:
            check(f"tile_absent_{r['x']}_{r['z']}", False, f"undeclared field {k} has a property")
            ok = False
            break
    check(f"tile_{r['x']}_{r['z']}_props", ok)
    # Z = height * 12, locked
    h = r.get("height", 0) * mod.HEIGHT_STEP
    check(f"tile_{r['x']}_{r['z']}_z", all(v.co.z == h for v in tob.data.vertices))
lvl1 = [r for r in (doc.get("terrain") or []) if r.get("level", 0) != 0]
for r in lvl1:
    check(f"tile_lvl{r['level']}_absent_{r['x']}_{r['z']}",
          bpy.data.objects.get(f"tile_{r['x']}_{r['z']}_L{r['level']}") is None)

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

_icons = []
_props = []
_ops = []
_menus = []
_labels = []
_fl = _FakeLayout(_icons)
try:
    class _Self:
        layout = _fl
    mod.MAP_PT_preview.draw(_Self(), type("_Ctx", (), {"object": ob2})())
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
check("panel_light_debug_props",
      _props == ["exmateria_map_light_debug", "exmateria_map_light_boost"],
      str(_props))
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
               type("_Ctx", (), {"object": ob2})())
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
# The question that sent this back: "I change map preview and hit push and
# nothing happens -- shouldn't it update the texture?" It cannot, twice over:
# the previewed state is VIEW state that never enters the document, and the
# texture sheet and CLUT have no live sink in this module at all. The panel has
# to say so where the button is, not in a report the artist reads afterwards.
_push_text = " ".join(_push_labels).lower()
check("push_panel_says_it_carries_no_texture",
      "texture" in _push_text, str(_push_labels))
check("push_panel_says_the_preview_state_is_not_pushed",
      "preview" in _push_text or "state" in _push_text, str(_push_labels))

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
check("export_panel_survives_selecting_a_lamp",
      bool(bpy.types.MAP_PT_export.poll(bpy.context)))
check("push_panel_survives_selecting_a_lamp",
      bool(bpy.types.MAP_PT_live_push.poll(bpy.context)))
# ...and both still DRAW, or surviving the poll buys nothing.
for _tag, _cls in (("export", bpy.types.MAP_PT_export),
                   ("push", bpy.types.MAP_PT_live_push)):
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
check("override_absent_initially",
      len(ob2.exmateria_map_rig_overrides) == 0
      and mod.find_override(ob2, _i0) is None)

bpy.context.view_layer.objects.active = ob2
_res = bpy.ops.exmateria_map.mint_rig_override()
check("override_mint_ran", _res == {"FINISHED"}, f"res={_res}")
_ov = mod.find_override(ob2, _i0)
check("override_minted", _ov is not None and len(ob2.exmateria_map_rig_overrides) == 1)

# Minting must NOT move the picture: it converts "the ROM's rig" into "the same
# rig, editable".  Ambient and the gains are an integer scaled by a constant and
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
_check_bake("override_minted_same_picture", ob2, _src_rig)

# The document is untouched — the Override is stored apart, by construction.
check("override_document_untouched",
      ob2["exmateria_map/map_states"] == _doc_before,
      "minting an Override rewrote the document")

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
    mod.MAP_PT_preview.draw(_Self(), type("_Ctx", (), {"object": ob2})())
    check("panel_draw_edited", True)
except Exception as e:
    check("panel_draw_edited", False, repr(e))
check("panel_rig_props",
      set(_props) >= {"ambient", "gain_1", "gain_2", "gain_3",
                      "dir_1", "dir_2", "dir_3", "gradient"},
      str(_props))
check("panel_icons_valid_edited",
      all(i in _valid for i in _icons if i is not None),
      str([i for i in _icons if i is not None and i not in _valid]))

# Revert returns the ROM's picture exactly.
_res = bpy.ops.exmateria_map.clear_rig_override(all_states=True)
check("override_cleared_ran", _res == {"FINISHED"}, f"res={_res}")
check("override_cleared",
      len(ob2.exmateria_map_rig_overrides) == 0
      and mod.find_override(ob2, _i0) is None)
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
# `terrain` is null on a dump document and a LIST here: the fixture declares
# one level-0 record, so the tile-object leg is exercised, not vacuous.
check("export_terrain_from_tile_objects",
      _doc["terrain"] == doc["terrain"] and isinstance(_doc["terrain"], list)
      and len(_doc["terrain"]) == 1, str(_doc["terrain"]))
check("export_grid_from_grid_object",
      _doc["base"]["terrain_grid"] == {"size_x": 10, "size_z": 13},
      str(_doc["base"]["terrain_grid"]))
check("export_carry_verbatim", _doc["carry"] == doc["carry"])

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

# ---- §5.1.3 the drift record's pin bytes (decision 23) --------------------
_tile = exp.flagged(obx, "tile")[0]
check("export_tile_object_is_flagged",
      _tile.get("exmateria_map/tile") == "imported",
      str(_tile.get("exmateria_map/tile")))
check("export_imported_tile_may_declare_anything",
      not refusals_mentioning(exp.assemble(obx)[2], "drift record"),
      "an IMPORTED tile was held to the drift rule")
_tile["exmateria_map/tile"] = "drift"
_r = exp.assemble(obx)[2]
check("export_refuses_drift_pin_byte",
      bool(refusals_mentioning(_r, "drift record", "surface_type")),
      f"a drift record declaring surface_type passed: {_r.refusals[:3]}")
_tile["exmateria_map/tile"] = "imported"
check("export_drift_refusal_clears", not exp.assemble(obx)[2].refusals)

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
check("growth_widget_seeded_from_import",
      (_g.exmateria_map_size_x, _g.exmateria_map_size_z) == (10, 13),
      f"{_g.exmateria_map_size_x}x{_g.exmateria_map_size_z}")
check("growth_preview_zero_on_untouched",
      au.growth_preview(obg)["created"] == 0,
      f"an untouched import reads {au.growth_preview(obg)['created']} pending "
      f"-- the pre-growth extent is the `_shadow` twin, not zero")
_res = bpy.ops.exmateria_map.apply_growth()
check("growth_apply_on_untouched_creates_nothing",
      len(exp.flagged(obg, "tile")) == 1, str(len(exp.flagged(obg, "tile"))))

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
check("growth_field_created_no_objects", len(exp.flagged(obg, "tile")) == 1,
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
      len(exp.flagged(obg, "tile")) == 1 + 2 * 13,
      str(len(exp.flagged(obg, "tile"))))
check("growth_apply_is_idempotent",
      bpy.ops.exmateria_map.apply_growth() == {"FINISHED"}
      and len(exp.flagged(obg, "tile")) == 1 + 2 * 13,
      "a second apply created more handles")
# Decision 20: growth writes NOTHING.  26 handles, none declared, so the
# document's `terrain` is exactly the record it arrived with.
_gd, _, _grep = exp.assemble(obg)
check("growth_handles_export_no_record", _gd["terrain"] == doc["terrain"],
      str(_gd["terrain"]))
check("growth_export_has_no_refusals", not _grep.refusals, str(_grep.refusals[:3]))
# ... and a DECLARED field on one of them does reach the document, or the
# handles are decoration.
_new = [t for t in exp.flagged(obg, "tile")
        if t.get("exmateria_map/tile") == "growth"][0]
au.declare(_new, "height", 7)
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
check("drift_no_handles_on_import",
      [t for t in exp.flagged(obd, "tile")
       if t.get("exmateria_map/tile") == "drift"] == [])
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
check("drift_seen_after_the_floor_moves", _n == 1,
      f"a floor moved a whole step and the checker saw {_n} drifted tiles")
_handles = [t for t in exp.flagged(obd, "tile")
            if t.get("exmateria_map/tile") == "drift"]
check("drift_handle_created", len(_handles) == 1, str(len(_handles)))
_h = _handles[0]
check("drift_handle_names_the_tile",
      (_h["x"], _h["z"], _h["level"]) == (0, 0, 0), str(dict(_h)))
check("drift_handle_shows_the_base_value",
      _h.get("height_base") == 2 and _h.get("drift_step_now") == 3,
      f"base={_h.get('height_base')} now={_h.get('drift_step_now')}")
check("drift_handle_declares_nothing_yet",
      not any(au.is_declared(_h, f) for f in au.DRIFT_FIELDS)
      and exp.assemble(obd)[0]["terrain"] is None,
      "a fresh handle already declared a field; §7.4 says `terrain` stays null")
check("drift_handle_sits_at_the_floor",
      abs(_h.data.vertices[0].co[2] - 36.0) < 1e-3,
      str(_h.data.vertices[0].co[2]))
check("drift_sync_is_idempotent",
      au.sync_drift(obd) == (1, 0)
      and len([t for t in exp.flagged(obd, "tile")
               if t.get("exmateria_map/tile") == "drift"]) == 1,
      "a second sync duplicated the handle")

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
au.undeclare(_h, "surface_type")

# Drift CLEARING deletes the quad, and the record drops from the next export.
for _vi in _fverts:
    obd.data.vertices[_vi].co[2] = 24.0
_n, _fixed = au.sync_drift(obd)
check("drift_cleared_when_the_floor_returns", _n == 0, str(_n))
check("drift_handle_deleted",
      [t for t in exp.flagged(obd, "tile")
       if t.get("exmateria_map/tile") == "drift"] == [])
check("drift_record_drops_from_the_export",
      exp.assemble(obd)[0]["terrain"] is None,
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
    proc = subprocess.run([sys.argv[1] if len(sys.argv) > 1 else "blender",
                           "--background", "--factory-startup", "--python", str(script)],
                          capture_output=True, text=True)
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
