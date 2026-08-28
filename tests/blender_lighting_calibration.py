"""Calibrate the addon's SHIPPED preview graph BY RENDERING it.

Introspection cannot answer this.  #427 measured that Blender reports only the
current item of `view_transform`, so `Standard` looks absent while AgX quietly
regrades every pixel; and a neutral grey renders identically under both, so a
grey calibration value certifies nothing.  The only honest check is to render
known values and read the file back.

What is being pinned is WHICH SPACE the light multiply happens in.  The PSX
multiplies the 8-bit CLUT value, so the whole chain multiplies in byte space
and converts once at the end; for CLUT byte 128 at light 0.5 the two
candidate chains are nowhere near each other:

    byte space (correct) -> 128 * 0.5                       =  64
    linear space (wrong) -> srgb_encode(linear(128/255)*0.5) = ~94

A saturated triple is used, not a grey, for the AgX reason above.

Renders on CYCLES/CPU: the chain is Emission-only so one sample is exact, the
colour-management path is identical to EEVEE's, and this box's VRAM is not
reliably free (an EEVEE headless render core-dumps in the NVIDIA driver when
it is not).

Run:  python3 tests/blender_lighting_calibration.py [blender-binary]
"""
import json
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
ADDONS = PKG / "addons"
TMP = Path(__file__).resolve().parent / ".blender_calib"
REPORT = TMP / "calibration.json"

# CLUT entry -> expected rendered byte, per light value.  `191 * 0.5 = 95.5`
# and `191 * 0.25 = 47.75` land on 95 and 47: Blender's 8-bit PNG write floors
# (#427).  The tolerance is 1 byte, which is the floor, not a fudge factor.
TEST_ENTRY = (128, 64, 191)
EXPECTED = {
    "1.0": (128, 64, 191),
    "0.5": (64, 32, 95),
    "0.25": (32, 16, 47),
}
TOLERANCE = 1

SCRIPT = r'''
import json
import os
import sys

import bpy

ADDON_PARENT = "@ADDONS@"
OUT = "@OUT@"
sys.path.insert(0, ADDON_PARENT)
from exmateria_map import import_document as mod

TEST = @TEST@
LIGHTS = @LIGHTS@
U, V, PAGE, ENTRY, PID = 8, 8, 0, 3, 0

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

indices = [0] * (256 * 1024)
indices[V * 256 + U] = ENTRY
idx_img = mod._index_image("calib_index", indices, 256, 1024)
rows = [[(0, 0, 0)] * 16 for _ in range(16)]
rows[PID][ENTRY] = tuple(TEST)
clut_img = mod._clut_image("calib_clut", rows)

rendered = {}
for light in LIGHTS:
    me = bpy.data.meshes.new("q")
    me.from_pydata([(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)], [], [(0, 1, 2, 3)])
    me.update()
    uvl = me.uv_layers.new(name="UVMap")
    uvxy = mod._uv_enc(U, V, PAGE)
    for d in uvl.data:
        d.uv = uvxy
    me.attributes.new("palette_id", "INT", "FACE").data[0].value = PID
    la = me.attributes.new("diffuse", "FLOAT_COLOR", "CORNER")
    for d in la.data:
        d.color = (light, light, light, 1.0)
    mat = mod._preview_material(f"calib_{light}", idx_img, clut_img)
    # ambient is a GRAPH constant since the bake split; zero it so the sum
    # reaching the multiply is exactly the light value under test
    mat.node_tree.nodes["exmateria_map.ambient"].outputs[0].default_value = \
        (0.0, 0.0, 0.0, 1.0)
    me.materials.append(mat)
    ob = bpy.data.objects.new("q", me)
    sc.collection.objects.link(ob)
    cam_d = bpy.data.cameras.new("cam")
    cam_d.type = "ORTHO"
    cam_d.ortho_scale = 1.0
    cam = bpy.data.objects.new("cam", cam_d)
    cam.location = (0, 0, 5)
    sc.collection.objects.link(cam)
    sc.camera = cam

    path = os.path.join(os.path.dirname(OUT), f"calib_{light}.png")
    sc.render.filepath = path
    bpy.ops.render.render(write_still=True)
    img = bpy.data.images.load(path)
    px = img.pixels[:]
    j = (32 * 64 + 32) * 4
    rendered[str(light)] = [round(px[j + k] * 255) for k in range(3)]
    bpy.data.images.remove(img)
    bpy.data.objects.remove(ob, do_unlink=True)
    bpy.data.objects.remove(cam, do_unlink=True)

json.dump({"test_entry": list(TEST), "rendered": rendered,
           "view_transform": sc.view_settings.view_transform,
           "clut_colorspace": clut_img.colorspace_settings.name,
           "index_colorspace": idx_img.colorspace_settings.name},
          open(OUT, "w"), indent=1)
print("CALIB", json.dumps(rendered))
'''


def main():
    TMP.mkdir(exist_ok=True)
    if REPORT.exists():
        REPORT.unlink()          # never grade on a stale report
    script = TMP / "run_calib.py"
    script.write_text(SCRIPT
                      .replace("@ADDONS@", str(ADDONS))
                      .replace("@OUT@", str(REPORT))
                      .replace("@TEST@", json.dumps(list(TEST_ENTRY)))
                      .replace("@LIGHTS@", json.dumps([float(k) for k in EXPECTED])))
    # Isolate this Blender from the artist's OWN install. Without it the
    # `addon_install` in the script above overwrites the addon they are
    # clicking, and `addon_enable` then grades that copy rather than this
    # tree. `--factory-startup` does NOT do this -- see `blender_env`.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from blender_env import isolated_env
    proc = subprocess.run([sys.argv[1] if len(sys.argv) > 1 else "blender",
                           "--background", "--factory-startup",
                           "--python", str(script)],
                          capture_output=True, text=True,
                          env=isolated_env())
    sys.stdout.write(proc.stdout)
    if not REPORT.exists():
        sys.stdout.write("\n[stderr]\n" + proc.stderr[-4000:])
        print("\nFAIL: no report written")
        sys.exit(1)
    rep = json.loads(REPORT.read_text())
    failed = []
    if rep["clut_colorspace"] != "Non-Color" or rep["index_colorspace"] != "Non-Color":
        failed.append(f"images are colour-managed: clut={rep['clut_colorspace']} "
                      f"index={rep['index_colorspace']}")
    for k, want in EXPECTED.items():
        got = rep["rendered"].get(k)
        print(f"  light {k:<5} CLUT {TEST_ENTRY} -> rendered {got}, expected {list(want)}")
        if got is None or any(abs(g - w) > TOLERANCE for g, w in zip(got, want)):
            failed.append(f"light {k}: {got} != {list(want)}")
    if failed:
        print("\nFAILED:")
        for f in failed:
            print("  " + f)
        sys.exit(1)
    print("\nPASS: the preview chain multiplies in PSX byte space and renders "
          "the CLUT byte back unchanged at light 1.0")


if __name__ == "__main__":
    main()
