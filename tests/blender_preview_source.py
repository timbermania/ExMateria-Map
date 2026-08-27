"""Grade the preview source swap — RENDERED, because a link check is blind.

The Raw/Compiled preview (`import_document.set_preview_source`) rewires which
node feeds the multiply's **albedo** input: the CLUT lookup (what the disc will
hold) or the artist's paint image (what they just painted, in any colour at
all). That is what makes painting live — Blender refreshes a viewport when a
material samples the image being painted, so there is no timer and no poll.

**Why rendered.** `blender_light_debug.py`'s reason applies here unchanged: a
check that asserts a LINK exists passes on a node whose image is None, on a
node sampling the wrong image, and on a swap that silently fell back. The only
thing that cannot be faked is the pixel. So both modes are rendered and the
defect each check exists to catch is seeded and re-rendered.

CYCLES/CPU at one sample: the chain is Emission-only so one sample is exact,
and an EEVEE headless render core-dumps in this box's NVIDIA driver.

Run:  python3 tests/blender_preview_source.py [blender-binary]
"""
import json
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
ADDONS = PKG / "addons"
TMP = Path(__file__).resolve().parent / ".blender_preview_source"
REPORT = TMP / "report.json"

#: The committed colour (a CLUT entry) and the raw painted colour. They must be
#: far apart on every channel, or a check could not tell the two modes apart --
#: asserted as a precondition rather than assumed.
COMMITTED = (200, 40, 40)
RAW = (33, 180, 90)
PID, ENTRY, U, V, PAGE = 3, 7, 11, 29, 1
TOLERANCE = 2

SCRIPT = r'''
import json, os, sys
import bpy
sys.path.insert(0, "@ADDONS@")
from exmateria_map import import_document as mod

CFG = @CFG@
OUT = "@OUT@"
OUTDIR = os.path.dirname(OUT)

sc = bpy.context.scene
sc.render.engine = "CYCLES"
sc.cycles.device = "CPU"
sc.cycles.samples = 1
sc.cycles.use_denoising = False
sc.render.resolution_x = sc.render.resolution_y = 64
sc.render.image_settings.file_format = "PNG"
sc.render.image_settings.color_mode = "RGB"
sc.render.image_settings.color_depth = "8"
sc.render.film_transparent = False
sc.view_settings.view_transform = "Standard"
sc.display_settings.display_device = "sRGB"
sc.view_settings.look = "None"
sc.view_settings.exposure = 0.0
sc.view_settings.gamma = 1.0
for o in list(bpy.data.objects):
    bpy.data.objects.remove(o, do_unlink=True)

cam_d = bpy.data.cameras.new("cam")
cam_d.type = "ORTHO"; cam_d.ortho_scale = 1.0
cam = bpy.data.objects.new("cam", cam_d)
cam.location = (0, 0, 5)
sc.collection.objects.link(cam); sc.camera = cam

indices = [0] * (256 * 1024)
# `_uv_enc(u, v, page)` addresses sheet row `page*256 + v`, so the page has to
# be in the index too. Leaving it out put the texel on page 0 while the UV read
# page 1, and the CLUT looked up index 0 -- which renders BLACK and looks
# exactly like a preview swap that failed.
indices[((CFG["page"] * 256) + CFG["v"]) * 256 + CFG["u"]] = CFG["entry"]
idx_img = mod._index_image("ps_index", indices, 256, 1024)
rows = [[(0, 0, 0)] * 16 for _ in range(16)]
rows[CFG["pid"]][CFG["entry"]] = tuple(CFG["committed"])
clut_img = mod._clut_image("ps_clut", rows)

# The paint image, uniform in the RAW colour. Uniform on purpose: it removes
# every question about row order from the proof, and the swap is still the only
# thing that can put this colour on screen.
paint = bpy.data.images.new("ps_paint", 256, 1024, alpha=False, float_buffer=True)
paint.colorspace_settings.name = "Non-Color"
px = [0.0] * (256 * 1024 * 4)
r, g, b = (c / 255.0 for c in CFG["raw"])
for i in range(256 * 1024):
    px[i*4], px[i*4+1], px[i*4+2], px[i*4+3] = r, g, b, 1.0
paint.pixels.foreach_set(px)
paint.update()


def build(tag):
    me = bpy.data.meshes.new(tag)
    me.from_pydata([(-1,-1,0), (1,-1,0), (1,1,0), (-1,1,0)], [], [(0,1,2,3)])
    me.update()
    uvl = me.uv_layers.new(name="UVMap")
    for d in uvl.data:
        d.uv = mod._uv_enc(CFG["u"], CFG["v"], CFG["page"])
    me.attributes.new("palette_id", "INT", "FACE").data[0].value = CFG["pid"]
    for a in ("normals", "normals_shadow"):
        for d in me.attributes.new(a, "FLOAT_VECTOR", "CORNER").data:
            d.vector = (0.0, 0.0, 1.0)
    me.attributes.new("diffuse", "FLOAT_COLOR", "CORNER")
    for d in me.attributes["diffuse"].data:
        d.color = (0.0, 0.0, 0.0, 1.0)
    mat = mod._preview_material(f"ps_{tag}", idx_img, clut_img)
    # Light is the identity here: ambient 1.0, diffuse 0, so the multiply passes
    # the albedo through unchanged and the render IS the albedo. That is what
    # makes the two modes comparable byte for byte.
    mat.node_tree.nodes["exmateria_map.ambient"].outputs[0].default_value = \
        (1.0, 1.0, 1.0, 1.0)
    me.materials.append(mat)
    ob = bpy.data.objects.new(tag, me)
    sc.collection.objects.link(ob)
    return ob, mat


def render(tag):
    path = os.path.join(OUTDIR, f"{tag}.png")
    sc.render.filepath = path
    bpy.ops.render.render(write_still=True)
    img = bpy.data.images.load(path)
    p = img.pixels[:]
    j = (32 * 64 + 32) * 4
    out = [round(p[j + k] * 255) for k in range(3)]
    flat = True
    for sy in (20, 32, 44):
        for sx in (20, 32, 44):
            q = (sy * 64 + sx) * 4
            if any(abs(round(p[q + k] * 255) - out[k]) > 1 for k in range(3)):
                flat = False
    bpy.data.images.remove(img)
    return out, flat


def shown(tag, mode, image, seed=None, debug_mode=0):
    ob, mat = build(tag)
    mod.set_preview_source(mat, mode, image)
    if seed == "ignore_mode":
        # The defect: the swap runs but the CLUT stays wired. Seeded on THIS
        # material only -- a mutation in shared code moves both arms together
        # and the guard passes on unfixed code.
        nt = mat.node_tree
        mix = nt.nodes["exmateria_map.multiply"]
        for lk in list(nt.links):
            if lk.to_node.name == mix.name and lk.to_socket.name == "Color1":
                nt.links.remove(lk)
        nt.links.new(nt.nodes["exmateria_map.clut"].outputs["Color"],
                     mix.inputs["Color1"])
    mod.set_light_debug(mat, debug_mode, 1.0)
    out, flat = render(tag)
    bpy.data.objects.remove(ob, do_unlink=True)
    return out, flat


rep = {}
rep["quantised"], rep["quantised_flat"] = shown("quantised", "QUANTISED", None)
rep["raw"], rep["raw_flat"] = shown("raw", "RAW", paint)
rep["raw_seeded"] = shown("raw_seed", "RAW", paint, seed="ignore_mode")[0]
# RAW with no paint image must fall back to the CLUT, not render black: the
# paint image is created by *Paint sheet*, so a mode that went black until then
# would read as a broken preview rather than as a missing step.
rep["raw_no_image"] = shown("raw_none", "RAW", None)[0]
# Albedo-only (light-debug mode 5) must follow the preview source too. It named
# the CLUT node outright, so RAW + albedo-only showed the committed colour --
# the one place the two switches could disagree.
rep["raw_albedo_only"] = shown("raw_albedo", "RAW", paint, debug_mode=5)[0]
rep["quantised_albedo_only"] = shown("q_albedo", "QUANTISED", None, debug_mode=5)[0]
rep["view_transform"] = sc.view_settings.view_transform
json.dump(rep, open(OUT, "w"), indent=1)
'''


def main():
    if all(abs(a - b) <= TOLERANCE for a, b in zip(COMMITTED, RAW)):
        print(f"PRECONDITION FAILED: committed {COMMITTED} and raw {RAW} are "
              f"indistinguishable; no render could tell the modes apart")
        return 1
    TMP.mkdir(exist_ok=True)
    if REPORT.exists():
        REPORT.unlink()
    cfg = {"committed": list(COMMITTED), "raw": list(RAW), "pid": PID,
           "entry": ENTRY, "u": U, "v": V, "page": PAGE}
    script = TMP / "run_preview.py"
    script.write_text(SCRIPT.replace("@ADDONS@", str(ADDONS))
                            .replace("@OUT@", str(REPORT))
                            .replace("@CFG@", json.dumps(cfg)))
    proc = subprocess.run([sys.argv[1] if len(sys.argv) > 1 else "blender",
                           "--background", "--factory-startup",
                           "--python", str(script)],
                          capture_output=True, text=True)
    if not REPORT.exists():
        sys.stdout.write(proc.stdout[-3000:])
        sys.stdout.write("\n[stderr]\n" + proc.stderr[-4000:])
        print("\nFAIL: no report written")
        return 1
    rep = json.loads(REPORT.read_text())

    near = lambda a, b: all(abs(x - y) <= TOLERANCE for x, y in zip(a, b))
    checks = [
        ("view transform is Standard", rep["view_transform"] == "Standard",
         rep["view_transform"]),
        ("QUANTISED renders the committed CLUT colour",
         near(rep["quantised"], COMMITTED), f"{rep['quantised']} vs {list(COMMITTED)}"),
        ("RAW renders the painted colour",
         near(rep["raw"], RAW), f"{rep['raw']} vs {list(RAW)}"),
        ("the two modes actually differ on screen",
         not near(rep["quantised"], rep["raw"]),
         f"{rep['quantised']} vs {rep['raw']}"),
        ("both modes render flat", rep["quantised_flat"] and rep["raw_flat"],
         f"{rep['quantised_flat']} / {rep['raw_flat']}"),
        ("the seeded defect MOVES the render (the check can go red)",
         not near(rep["raw_seeded"], rep["raw"]),
         f"seeded {rep['raw_seeded']} vs clean {rep['raw']}"),
        ("RAW with no paint image falls back to the CLUT, not black",
         near(rep["raw_no_image"], COMMITTED), str(rep["raw_no_image"])),
        ("albedo-only follows the preview source in RAW",
         near(rep["raw_albedo_only"], RAW),
         f"{rep['raw_albedo_only']} vs {list(RAW)}"),
        ("albedo-only still shows the CLUT in QUANTISED",
         near(rep["quantised_albedo_only"], COMMITTED),
         f"{rep['quantised_albedo_only']} vs {list(COMMITTED)}"),
    ]
    bad = 0
    print(f"committed {COMMITTED}   raw {RAW}\n")
    for name, ok, detail in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {name}: {detail}")
        bad += 0 if ok else 1
    print(f"\nSUMMARY: {len(checks) - bad}/{len(checks)} checks passed")
    print("PASS" if not bad else "FAIL")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
