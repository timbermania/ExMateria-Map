"""Does a saved `.blend` still show the textures when it is reopened?

`bpy.data.images.new()` images live only in RAM.  A `.blend` that references
one regenerates it BLANK on reload, and blank means index 0 — which is
`#000000` on every FFT CLUT — so every textured face reopens PURE BLACK while
the untextured ones still shade, and the fault reads like a broken material.
The addon packs its generated images to stop that; this proves the packing
works by RENDERING IN A SECOND PROCESS, which is the only place the failure
can appear.

Two Blender runs, same camera, same frame:
  A  import the fixture document, render, save the .blend
  B  open that .blend in a FRESH process and render again
The two PNGs must be byte-identical.

Run:  python3 tests/blender_reload_persistence.py [blender-binary]
"""
import struct
import subprocess
import sys
import zlib
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
ADDONS = PKG / "addons"
FIXTURES = PKG / "tests" / "fixtures"
FIXTURE = FIXTURES / "MAP001.a0.stub.json"
TMP = Path(__file__).resolve().parent / ".blender_reload"

COMMON = r'''
import math
import bpy
import mathutils

def setup(sc):
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.samples = 1
    sc.cycles.use_denoising = False
    sc.render.resolution_x = sc.render.resolution_y = 128
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGB"
    sc.render.image_settings.color_depth = "8"
    sc.render.film_transparent = False
    sc.view_settings.view_transform = "Standard"
    sc.view_settings.look = "None"

def frame(sc, ob):
    bb = [ob.matrix_world @ mathutils.Vector(c) for c in ob.bound_box]
    ctr = sum(bb, mathutils.Vector()) / 8.0
    rad = max(max((v - ctr).length for v in bb), 1e-3)
    d = mathutils.Vector((0.0, -0.6, 0.8)).normalized()
    cd = bpy.data.cameras.new("cam")
    cd.type = "ORTHO"
    cd.ortho_scale = rad * 2.05
    cd.clip_start, cd.clip_end = 0.01, rad * 20
    cam = bpy.data.objects.new("cam", cd)
    cam.location = ctr + d * (rad * 4)
    cam.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
    sc.collection.objects.link(cam)
    sc.camera = cam
'''

SCRIPT_A = COMMON + r'''
import json
import sys
sys.path.insert(0, "@ADDONS@")
from exmateria_map import import_document as mod

# This harness imports the module rather than installing the zip, so nothing
# has run `register()` — and the Override lives on a registered
# CollectionProperty, which does not exist until it does.
mod.register()

sc = bpy.context.scene
for o in list(bpy.data.objects):
    bpy.data.objects.remove(o, do_unlink=True)
setup(sc)
ob = mod.build(json.loads(open("@DOC@").read()), bpy.context, "@DOC@")
frame(sc, ob)
# An edited rig is an artist's WORK, so it has to survive the file the same
# way the generated images do — and neither property is introspectable, which
# is why both are asserted by reopening and rendering. The clean round asserts
# the opposite half: every state is EXPOSED either way, so a round that edits
# nothing must still render the ROM's picture, which is what keeps the parity
# instrument measuring what it claims to.
if @OVERRIDE@:
    bpy.context.view_layer.objects.active = ob
    _ov = mod.find_override(ob, int(ob["exmateria_map/preview_state"]))
    _ov.ambient = (0.5, 0.125, 0.75)
    _ov.gain_1 = (3.5, 0.25, 1.0)
    print("EXMATERIA-MAP override seeded:", mod.override_rig(_ov)["ambient"],
          mod.override_rig(_ov)["colors"][0])
else:
    print("EXMATERIA-MAP overrides:",
          len(mod.dirty_overrides(ob)), "edited of",
          len(ob.exmateria_map_rig_overrides), "exposed (clean round)")
sc.render.filepath = "@OUTA@"
bpy.ops.render.render(write_still=True)
packed = [i.name for i in bpy.data.images if i.name.startswith("exmateria_map/")
          and i.packed_file is not None]
print("PACKED", len(packed), "of",
      len([i for i in bpy.data.images if i.name.startswith("exmateria_map/")]))
bpy.ops.wm.save_as_mainfile(filepath="@BLEND@")
'''

SCRIPT_B = COMMON + r'''
import sys
sys.path.insert(0, "@ADDONS@")
from exmateria_map import import_document as mod

# Registered BEFORE the load, so the RNA the Override lives on exists while the
# file is read — the same order a real session has (addon enabled, then open).
mod.register()
bpy.ops.wm.open_mainfile(filepath="@BLEND@")
sc = bpy.context.scene
sc.render.filepath = "@OUTB@"
bpy.ops.render.render(write_still=True)
_obs = [o for o in bpy.data.objects if "exmateria_map/preview_state" in o]
_n = sum(len(o.exmateria_map_rig_overrides) for o in _obs)
print("EXMATERIA-MAP reopened overrides:", _n, "expected", @OVERRIDE@)
if _n != (1 if @OVERRIDE@ else 0):
    raise SystemExit("override count did not survive the reload")
for _o in _obs:
    for _ov in _o.exmateria_map_rig_overrides:
        print("EXMATERIA-MAP reopened rig:", mod.override_rig(_ov)["ambient"],
              mod.override_rig(_ov)["colors"][0])
print("REOPENED images:",
      [(i.name, i.packed_file is not None)
       for i in bpy.data.images if i.name.startswith("exmateria_map/")])
'''


def raster(path):
    """The decompressed IDAT of a PNG — the picture, with no file metadata."""
    d = path.read_bytes()
    if d[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    i, idat = 8, b""
    while i + 8 <= len(d):
        n = struct.unpack(">I", d[i:i + 4])[0]
        if d[i + 4:i + 8] == b"IDAT":
            idat += d[i + 8:i + 8 + n]
        i += 12 + n
    return zlib.decompress(idat) if idat else None


def run(blender, name, body, subs):
    for k, v in subs.items():
        body = body.replace(k, v)
    path = TMP / name
    path.write_text(body)
    p = subprocess.run([blender, "--background", "--factory-startup",
                        "--python", str(path)], capture_output=True, text=True)
    for line in p.stdout.splitlines():
        if line.startswith(("PACKED", "REOPENED", "EXMATERIA-MAP")):
            print("  " + line)
    return p


def main():
    blender = sys.argv[1] if len(sys.argv) > 1 else "blender"
    TMP.mkdir(exist_ok=True)
    # Two rounds.  CLEAN proves an unedited preview reopens as the ROM's
    # picture — the parity instrument's own premise.  EDITED proves an
    # Override is not silently lost, which would cost the artist their work.
    shots = {}
    for override in (False, True):
        shots["EDITED" if override else "CLEAN"] = _round(blender, override)
    # Without this the EDITED round is VACUOUS: an Override that reached
    # nothing would reopen "unchanged" and both rounds would pass while
    # asserting nothing about an edit at all.
    ca, ea = shots["CLEAN"], shots["EDITED"]
    moved = sum(1 for x, y in zip(ca, ea) if x != y) if ca and ea else 0
    print(f"\n  CLEAN vs EDITED render: {moved} of {len(ca)} raster bytes differ")
    if not moved:
        print("\nFAIL: the Override did not change the picture, so the EDITED "
              "round proves nothing about persisting an edit")
        sys.exit(1)
    print("\nPASS: the saved .blend reopens rendering the same picture, with "
          "and without a live Override, and the Override does move the picture")


def _round(blender, override):
    tag = "EDITED" if override else "CLEAN"
    print(f"\n=== round: {tag} ===")
    out_a, out_b = TMP / f"a_{tag}.png", TMP / f"b_{tag}.png"
    blend = TMP / f"reload_{tag}.blend"
    for f in (out_a, out_b, blend):
        if f.exists():
            f.unlink()                      # never grade on a stale artifact

    # the addon reads sidecars next to the document
    doc = TMP / FIXTURE.name
    doc.write_text(FIXTURE.read_text())
    for png in FIXTURES.glob("*.png"):
        (TMP / png.name).write_bytes(png.read_bytes())

    subs = {"@ADDONS@": str(ADDONS), "@DOC@": str(doc), "@OUTA@": str(out_a),
            "@OUTB@": str(out_b), "@BLEND@": str(blend),
            "@OVERRIDE@": str(bool(override))}
    print("run A — import, render, save")
    pa = run(blender, f"run_a_{tag}.py", SCRIPT_A, subs)
    if not out_a.exists() or not blend.exists():
        sys.stdout.write(pa.stderr[-3000:])
        print("FAIL: run A produced no render or no .blend")
        sys.exit(1)
    print("run B — reopen in a fresh process, render")
    pb = run(blender, f"run_b_{tag}.py", SCRIPT_B, subs)
    if not out_b.exists():
        sys.stdout.write(pb.stderr[-3000:])
        print("FAIL: run B produced no render")
        sys.exit(1)

    # Compare the DECODED raster, not the file: Blender's PNG writer varies
    # its chunking between runs, so a file digest reports a difference that is
    # not in the picture.
    ra, rb = raster(out_a), raster(out_b)
    if ra is None or rb is None or len(ra) != len(rb):
        print("\nFAIL: renders are not comparable "
              f"({None if ra is None else len(ra)} vs "
              f"{None if rb is None else len(rb)} bytes)")
        sys.exit(1)
    diff = [i for i, (x, y) in enumerate(zip(ra, rb)) if x != y]
    print(f"  {len(ra)} raster bytes, {len(diff)} differ")
    if diff:
        print("\nFAIL: the reopened .blend does not render what it rendered "
              "before saving — the generated preview images did not survive")
        sys.exit(1)
    return ra


if __name__ == "__main__":
    main()
