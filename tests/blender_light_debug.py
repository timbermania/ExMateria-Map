"""Grade the six `map_light_debug` modes BY RENDERING them, and prove the
ambient/diffuse bake split is pixel-identical to the summed bake it replaced.

Introspection cannot answer either question.  A graph-structure assertion —
"node X feeds the decode in mode N" — grades the WIRING, and a correctly wired
graph still paints the wrong picture: mode 1's whole risk is the axis swizzle,
which a link check sails straight past.  So every mode here is rendered and
compared against a value computed on the HOST, from the rig, by code that
shares nothing with the addon's bake.

Each check ships with the defect it exists to catch, seeded and re-rendered.  A
check that has never been observed to fail is a reporter nobody has tested.

The six modes mirror `indexed_color.gdshader:52`.  Godot overrides `final` in
the debug branch and then runs the SAME `pow(final, 2.2)` conversion on it, so
every mode routes through the one sRGB decode group and only its INPUT changes:

    0 normal        albedo x clamp(ambient + diffuse)
    1 normals       0.5 + 0.5*n, in the RAW FFT triple
    2 lighting      clamp((ambient + diffuse) * boost)
    3 ambient       clamp(ambient * boost)
    4 diffuse       clamp(diffuse * boost)
    5 albedo        the CLUT colour

Renders on CYCLES/CPU: the chain is Emission-only so one sample is exact, and
an EEVEE headless render core-dumps in the NVIDIA driver whenever this box's
VRAM is not free (a local VLLM routinely holds 28 of 32 GB).

Run:  python3 tests/blender_light_debug.py [blender-binary]
"""
import json
import math
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
ADDONS = PKG / "addons"
TMP = Path(__file__).resolve().parent / ".blender_debug"
REPORT = TMP / "light_debug.json"

# Blender's 8-bit PNG write floors (#427), so a value landing on x.5 reads back
# one low.  The tolerance is that floor, not a fudge factor.
TOLERANCE = 1

# --- the scene under test, chosen so every seed can actually bite ------------
# The CLUT entry is saturated, not grey: #427 measured that a grey certifies
# nothing, because a neutral value renders identically under AgX and Standard.
CLUT_ENTRY = (224, 160, 244)
PID, ENTRY, U, V, PAGE = 0, 3, 8, 8, 0

# A normal with three DISTINCT components, so a swizzle that permutes green and
# blue cannot pass by coincidence.  In FFT space, as the document carries it.
NORMAL_FFT = (-2000.0, 1750.0, 1500.0)

# ambient + diffuse must exceed 1.0 (else clamping the sum node is invisible)
# and albedo x sum must exceed 1.0 on some channel (else unclamping the
# multiply is invisible).  Both are asserted below before anything renders.
RIG = {
    "colors": [[6000, 5760, 4800], [400, 400, 1600], [0, 0, 0]],
    "directions": [[-3750, -1237, -1087], [3592, -251, 1949], [0, -4096, 0]],
    "ambient": [60, 60, 52],
    "gradient": [0, 0, 0, 0, 0, 0],
}
# Below 1.0 on purpose.  `map_light_boost` scales BEFORE the clamp, so at boost
# 1.0 every channel of `ambient + diffuse` that exceeds 1.0 saturates and the
# `sum_clamped` seed becomes invisible in mode 2 — the check would pass on a
# graph that clamps the sum.  At 0.5 the over-1.0 head-room survives into the
# visible range and the seed moves the render by 13 bytes.
BOOST = 0.5


def _fft_to_blender(v):
    return (v[0], v[2], -v[1])


def _blender_to_fft(v):
    return (v[0], -v[2], v[1])


def _unit(v):
    m = math.sqrt(sum(c * c for c in v))
    return tuple(c / m for c in v) if m > 1e-9 else (0.0, 0.0, 0.0)


def expected():
    """The six modes, computed from the rig with no addon code in the path.

    Deliberately works in FFT space throughout — the addon dots in BLENDER
    space — so agreement is two independent routes to the same number rather
    than one route run twice.
    """
    n = _unit(NORMAL_FFT)
    amb = tuple(c / 255.0 for c in RIG["ambient"])
    dif = [0.0, 0.0, 0.0]
    for i in range(3):
        d = _unit(tuple(float(c) for c in RIG["directions"][i]))
        k = sum(n[j] * d[j] for j in range(3))
        if k > 0.0:
            for c in range(3):
                dif[c] += RIG["colors"][i][c] / 8.0 / 255.0 * k
    total = tuple(amb[c] + dif[c] for c in range(3))
    albedo = tuple(c / 255.0 for c in CLUT_ENTRY)
    enc = tuple(0.5 + 0.5 * c for c in n)

    def clamp(t):
        return tuple(min(1.0, max(0.0, c)) for c in t)

    def byte(t):
        return [int(round(c * 255)) for c in clamp(t)]

    return {
        # Godot: `clamp(final_color.rgb * (ambient_term + diffuse_term))` —
        # the PRODUCT is clamped, not the sum, and the graph's multiply node
        # clamps its OUTPUT for the same reason.
        "0": byte(tuple(albedo[c] * total[c] for c in range(3))),
        "1": byte(enc),
        "2": byte(tuple(c * BOOST for c in total)),
        "3": byte(tuple(c * BOOST for c in amb)),
        "4": byte(tuple(c * BOOST for c in dif)),
        "5": byte(albedo),
    }, total, albedo


SCRIPT = r'''
import json
import os
import sys

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
cam_d.type = "ORTHO"
cam_d.ortho_scale = 1.0
cam = bpy.data.objects.new("cam", cam_d)
cam.location = (0, 0, 5)
sc.collection.objects.link(cam)
sc.camera = cam

indices = [0] * (256 * 1024)
indices[CFG["v"] * 256 + CFG["u"]] = CFG["entry"]
idx_img = mod._index_image("dbg_index", indices, 256, 1024)
rows = [[(0, 0, 0)] * 16 for _ in range(16)]
rows[CFG["pid"]][CFG["entry"]] = tuple(CFG["clut"])
clut_img = mod._clut_image("dbg_clut", rows)

RIG = CFG["rig"]
NRM_B = tuple(CFG["normal_blender"])


def build(tag, summed=False):
    """One quad, flat-shaded by construction: every corner carries the SAME
    normal, so the Gouraud interpolation is constant and the raster is flat —
    one sampled pixel is then the whole picture, not a point estimate."""
    me = bpy.data.meshes.new(tag)
    me.from_pydata([(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)], [], [(0, 1, 2, 3)])
    me.update()
    uvl = me.uv_layers.new(name="UVMap")
    uvxy = mod._uv_enc(CFG["u"], CFG["v"], CFG["page"])
    for d in uvl.data:
        d.uv = uvxy
    me.attributes.new("palette_id", "INT", "FACE").data[0].value = CFG["pid"]
    # BOTH twins, as `build` writes them — the bake and mode 1 read the live
    # `normals`, `export_document.divergence` reads the `_shadow`.
    for a in ("normals", "normals_shadow"):
        for d in me.attributes.new(a, "FLOAT_VECTOR", "CORNER").data:
            d.vector = NRM_B
    mod.bake_light(me, RIG)
    mat = mod._preview_material(f"dbg_{tag}", idx_img, clut_img)
    mod.set_ambient(mat, RIG)
    if summed:
        # the PRE-SPLIT shape: the corner attribute carries ambient + diffuse
        # and the graph constant is zero.  Same pixels iff the split is sound.
        amb = mod.rig_ambient(RIG)
        for d in me.attributes["diffuse"].data:
            c = d.color
            d.color = (c[0] + amb[0], c[1] + amb[1], c[2] + amb[2], 1.0)
        mat.node_tree.nodes["exmateria_map.ambient"].outputs[0].default_value = \
            (0.0, 0.0, 0.0, 1.0)
    me.materials.append(mat)
    ob = bpy.data.objects.new(tag, me)
    sc.collection.objects.link(ob)
    return ob, mat


def render(tag):
    path = os.path.join(OUTDIR, f"{tag}.png")
    sc.render.filepath = path
    bpy.ops.render.render(write_still=True)
    img = bpy.data.images.load(path)
    px = img.pixels[:]
    j = (32 * 64 + 32) * 4
    out = [round(px[j + k] * 255) for k in range(3)]
    # flatness: the whole quad must agree with the sampled pixel, else a single
    # sample would be reporting a gradient it cannot see
    flat = True
    for sy in (20, 32, 44):
        for sx in (20, 32, 44):
            q = (sy * 64 + sx) * 4
            if any(abs(round(px[q + k] * 255) - out[k]) > 1 for k in range(3)):
                flat = False
    bpy.data.images.remove(img)
    return out, flat


def shown(mode, boost, seed=None):
    """Render one mode, optionally with a defect seeded on THIS material only.

    Seeding one arm matters: a mutation applied to shared code moves both sides
    of a comparison together and the guard passes on unfixed code.
    """
    ob, mat = build(f"m{mode}_{seed or 'clean'}")
    nt = mat.node_tree
    if seed == "no_swizzle":
        comb = nt.nodes["exmateria_map.normal_fft"]
        sep = nt.nodes["exmateria_map.normal_split"]
        for lk in list(nt.links):
            if lk.to_node.name == comb.name:
                nt.links.remove(lk)
        for ax in ("X", "Y", "Z"):
            nt.links.new(sep.outputs[ax], comb.inputs[ax])
    elif seed == "ambient_x2":
        a = nt.nodes["exmateria_map.ambient"].outputs[0].default_value
        nt.nodes["exmateria_map.ambient"].outputs[0].default_value = \
            (a[0] * 2, a[1] * 2, a[2] * 2, 1.0)
    elif seed == "diffuse_zero":
        for d in ob.data.attributes["diffuse"].data:
            d.color = (0.0, 0.0, 0.0, 1.0)
    elif seed == "clut_shift":
        c = CFG["clut"]
        rows[CFG["pid"]][CFG["entry"]] = (c[0], (c[1] + 40) % 256, c[2])
        nt.nodes["exmateria_map.clut"].image = mod._clut_image("dbg_clut_seed", rows)
        rows[CFG["pid"]][CFG["entry"]] = tuple(c)
    elif seed == "sum_clamped":
        nt.nodes["exmateria_map.light_sum"].use_clamp = True
    elif seed == "ambient_zero":
        nt.nodes["exmateria_map.ambient"].outputs[0].default_value = \
            (0.0, 0.0, 0.0, 1.0)
    mod.set_light_debug(mat, mode, boost)
    out, flat = render(f"m{mode}_{seed or 'clean'}")
    bpy.data.objects.remove(ob, do_unlink=True)
    return out, flat


rep = {"clean": {}, "flat": {}, "seeded": {}}
for mode in range(6):
    out, flat = shown(mode, CFG["boost"])
    rep["clean"][str(mode)] = out
    rep["flat"][str(mode)] = flat
for mode, seed in CFG["seeds"].items():
    rep["seeded"][mode] = shown(int(mode), CFG["boost"], seed)[0]

# --- the split identity: pre-split shape vs post-split shape, same pixels ----
ob_a, mat_a = build("split_new")
mod.set_light_debug(mat_a, 0, 1.0)
rep["split_new"] = render("split_new")[0]
bpy.data.objects.remove(ob_a, do_unlink=True)
ob_b, mat_b = build("split_old", summed=True)
mod.set_light_debug(mat_b, 0, 1.0)
rep["split_old"] = render("split_old")[0]
bpy.data.objects.remove(ob_b, do_unlink=True)
# ...and the same identity with the sum node clamped on ONE arm only
ob_c, mat_c = build("split_seed")
mat_c.node_tree.nodes["exmateria_map.light_sum"].use_clamp = True
mod.set_light_debug(mat_c, 0, 1.0)
rep["split_seeded"] = render("split_seed")[0]
bpy.data.objects.remove(ob_c, do_unlink=True)

rep["view_transform"] = sc.view_settings.view_transform
json.dump(rep, open(OUT, "w"), indent=1)
print("DEBUGMODES", json.dumps(rep["clean"]))
'''

# Which defect each mode's check must be observed to catch.
SEEDS = {
    # ambient + diffuse must survive PAST 1.0 all the way to the multiply — the
    # PSX saturates only the final pixel, so clamping the sum darkens every
    # overbright texel.  This is the invariant the bake split could have broken.
    "0": "sum_clamped",
    "1": "no_swizzle",        # the axis convention — mode 1's whole risk
    "2": "ambient_zero",      # mode 2 is ambient + diffuse, not diffuse alone
    "3": "ambient_x2",        # the graph constant is the state's own bytes
    "4": "diffuse_zero",      # the corner bake actually reaches the pixel
    "5": "clut_shift",        # albedo-only really is the CLUT
}
# NOT seeded, because it cannot be: unclamping the multiply node is invisible in
# a render.  A value above 1.0 clips at the display exactly as the node's clamp
# would, so both arms write the same byte.  The clamp is kept because it is what
# Godot's shader says and because a downstream consumer would see the
# difference — but it can only ever be a STRUCTURAL check
# (`node_mix_clamped` in blender_roundtrip.py), never a rendered one, and
# claiming otherwise would be a check that cannot report its own defect.
MODE_NAMES = {"0": "normal", "1": "normals", "2": "lighting-only",
              "3": "ambient-only", "4": "diffuse-only", "5": "albedo-only"}


def main():
    want, total, albedo = expected()
    # the scene must be able to expose the seeds at all
    # Every seed below is only observable if the scene can express its defect.
    # Asserting that up front is what stops a green run from meaning "the seed
    # was applied and nothing could have shown it".
    preconditions = []
    if not any(c > 1.0 for c in total):
        preconditions.append(f"ambient+diffuse {total} never exceeds 1.0 — "
                             f"clamping the sum node would be invisible")
    if not any(albedo[c] * total[c] < 1.0 for c in range(3)):
        preconditions.append(f"albedo x sum saturates on every channel, so mode "
                             f"0 could not show the sum_clamped seed")
    if min(RIG["ambient"]) <= 0:
        preconditions.append("ambient is zero, so the ambient_zero seed would "
                             "be a no-op")
    m0 = [min(1.0, albedo[c] * total[c]) for c in range(3)]
    if all(abs(m0[c] - albedo[c]) * 255 <= TOLERANCE for c in range(3)):
        preconditions.append(f"mode 0 {m0} is indistinguishable from mode 5 "
                             f"{list(albedo)} — its value could not tell them "
                             f"apart")
    if preconditions:
        for p in preconditions:
            print("PRECONDITION FAILED: " + p)
        sys.exit(1)

    TMP.mkdir(exist_ok=True)
    if REPORT.exists():
        REPORT.unlink()               # never grade on a stale report
    cfg = {"clut": list(CLUT_ENTRY), "pid": PID, "entry": ENTRY, "u": U, "v": V,
           "page": PAGE, "rig": RIG, "boost": BOOST, "seeds": SEEDS,
           "normal_blender": list(_fft_to_blender(NORMAL_FFT))}
    script = TMP / "run_debug.py"
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
        sys.exit(1)
    rep = json.loads(REPORT.read_text())
    failed = []
    if rep.get("view_transform") != "Standard":
        failed.append(f"view transform is {rep.get('view_transform')}, not Standard")

    print(f"boost {BOOST}, CLUT {CLUT_ENTRY}, FFT normal {NORMAL_FFT}")
    print(f"  ambient+diffuse = {tuple(round(c, 4) for c in total)}\n")
    for m in ("0", "1", "2", "3", "4", "5"):
        got, w = rep["clean"][m], want[m]
        ok = all(abs(g - x) <= TOLERANCE for g, x in zip(got, w))
        seeded = rep["seeded"][m]
        moved = any(abs(s - g) > TOLERANCE for s, g in zip(seeded, got))
        print(f"  mode {m} {MODE_NAMES[m]:<14} {got} vs {w}   "
              f"{'ok ' if ok else 'BAD'}  flat={rep['flat'][m]}  "
              f"seed[{SEEDS[m]}]->{seeded} {'RED' if moved else 'BLIND'}")
        if not ok:
            failed.append(f"mode {m} ({MODE_NAMES[m]}): {got} != {w}")
        if not rep["flat"][m]:
            failed.append(f"mode {m}: raster is not flat — the sampled pixel "
                          f"does not represent the quad")
        if not moved:
            failed.append(f"mode {m}: seeding `{SEEDS[m]}` did NOT move the "
                          f"render, so the check cannot report that defect")

    a, b, c = rep["split_new"], rep["split_old"], rep["split_seeded"]
    same = all(abs(x - y) <= TOLERANCE for x, y in zip(a, b))
    seed_moved = any(abs(x - y) > TOLERANCE for x, y in zip(c, a))
    print(f"\n  split identity  new {a} vs pre-split {b}  "
          f"{'ok ' if same else 'BAD'}   seed[sum_clamped]->{c} "
          f"{'RED' if seed_moved else 'BLIND'}")
    if not same:
        failed.append(f"the bake split changed the picture: {a} != {b}")
    if not seed_moved:
        failed.append("clamping the sum node did NOT move the render, so the "
                      "split-identity check is blind")

    if failed:
        print("\nFAILED:")
        for f in failed:
            print("  " + f)
        sys.exit(1)
    print("\nPASS: six debug modes render their computed values, each proven by "
          "the defect it catches; the bake split is pixel-identical")


if __name__ == "__main__":
    main()
