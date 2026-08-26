"""The lighting bake's three checks (`docs/lighting-bake-v1.md` §9), on real bytes.

Every check asserts on THE PICTURE, never on the normals: §4 measured a median
16.77 degrees between two normals that render identically, so a test demanding
the original normals back would fail a solver that was entirely correct.  The
one exception is the fixed point, where the normals ARE the picture's identity —
the target is the current picture, so the answer must be the current normals.

  1. FIXED POINT   over the WHOLE corpus.  Import, seed the lamps from the
                   state's own rig, bake, and every corner normal must come back
                   byte-identical.  One fixture cannot grade this: it holds one
                   rig shape, while the corpus holds maps with three live lights
                   (16.15%), two (77.02%) and none (6.83%), states that borrow a
                   rig rather than own one, five arrangements with no rig at all,
                   and 1,383 zero-length normals.
  2. RECOVERY      scramble every normal, re-solve against the map's own render,
                   and the render must return within 1/255 on the worst channel
                   (above §4's 0.3228 quantisation ceiling, below anything
                   visible).  The forward model the assertion uses is written
                   HERE, independently: grading with the module's own
                   `forward_luma` would move both arms under one mutation.
  3. HONEST        a target with per-corner hue variation wider than the gamut
     RESIDUAL      must be reported as CHROMA error, with brightness still near
                   zero -- not as one small combined RMS.  §7: chroma is not
                   actionable and collapsing the two sends the artist chasing
                   the half that cannot move.
  5. LAMP TYPES    point, spot and area lamps reach the solve at all.  Only SUN
                   was ever exercised -- check 3 recolours the seeded suns -- so
                   the other three code paths in `_lamp_irradiance` shipped
                   untested, including the cone and the single-sided emitter.
  4. OPERATORS     the two buttons, driven through the REAL registered operator
                   path on an INSTALLED addon.  The three checks above call
                   `bake_normals` directly, so a broken operator class, a panel
                   that cannot draw, or a report that never reaches the object
                   would pass every one of them.

Run:  EXMATERIA_ASSETS_DIR=... python3 tests/blender_bake.py [blender] [--limit N]
"""
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
ADDON_DIR = PKG / "addons" / "exmateria_map"
TMP = Path(__file__).resolve().parent / ".blender_bake"
DOCS = TMP / "docs"
REPORT = TMP / "report.json"

LIMIT = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None
BLENDER = next((a for a in sys.argv if a.endswith("blender") or a == "blender"), "blender")

SCRIPT_TEMPLATE = r'''
import json
import math
import random
import sys

import bpy
from mathutils import Vector

PKG = "@ADDONPKG@"
DOCS = "@DOCS@"
OUT = "@OUT@"
TARGETS = json.loads('@TARGETS@')
TOL = @TOL@
ZIP = "@ZIP@"

# Install and enable, so check 4 can drive the REAL operators.  The direct
# imports below still come from PKG, which is the same source the zip was built
# from -- `blender_roundtrip.py` uses this same pairing.
try:
    bpy.ops.preferences.addon_install(filepath=ZIP)
    bpy.ops.preferences.addon_enable(module="exmateria_map")
except Exception as e:
    print(f"ADDON-INSTALL: {e}")

sys.path.insert(0, PKG)
from exmateria_map import import_document as imp
from exmateria_map import lighting_bake as lb

LUMA = (0.299, 0.587, 0.114)


def clear_lights():
    for o in [x for x in bpy.data.objects if x.type == "LIGHT"]:
        bpy.data.objects.remove(o, do_unlink=True)


def own_forward(n, dirs, gains, live):
    """The rig's diffuse at a normal, written HERE so a mutation to the
    module's own forward model cannot move the grader with the solver."""
    m = math.sqrt(sum(c * c for c in n)) or 1.0
    u = [c / m for c in n]
    out = [0.0, 0.0, 0.0]
    for i in live:
        k = sum(u[c] * dirs[i][c] for c in range(3))
        if k > 0.0:
            for c in range(3):
                out[c] += gains[i][c] * k
    return out


def own_luma(c):
    return LUMA[0] * c[0] + LUMA[1] * c[1] + LUMA[2] * c[2]


def live_runs():
    """The live handler's OWN run counter, read out of the registered function's
    globals rather than through an import.

    `addon_install` + `import` in one process yields TWO module objects -- the
    addon CLAUDE.md's standing warning -- so `lb._LIVE_RUNS` is a counter nobody
    increments and a check reading it passes no matter what. That is how
    `live_signature_dropped` read BLIND. Returns None when the handler is not
    registered at all, which the verdict treats as a failure rather than a zero.
    """
    for h in bpy.app.handlers.depsgraph_update_post:
        if getattr(h, "__name__", "") == "_live_handler":
            return h.__globals__.get("_LIVE_RUNS")
    return None


report = {"fixed": [], "recovery": None, "honest": None,
          "operators": None, "types": None, "errors": []}


# ---- check 1: fixed point over the corpus ---------------------------------
for name in TARGETS:
    path = f"{DOCS}/{name}"
    try:
        clear_lights()
        doc = json.loads(open(path).read())
        ob = imp.build(doc, doc_path=path)
        me = ob.data
        before = [tuple(int(round(c)) for c in d.vector)
                  for d in me.attributes["normals"].data]
        states = imp.object_states(ob)
        i = int(ob.get("exmateria_map/preview_state", 0))
        rig, _src, _e = imp.resolved_rig(ob, states, i) if states else (None, None, False)
        seeded = bool(rig)
        if seeded:
            lb.seed_lamps(ob, rig)
        rep = lb.bake_normals(ob, bpy.context)
        after = [tuple(int(round(c)) for c in d.vector)
                 for d in me.attributes["normals"].data]
        moved = sum(1 for a, b in zip(before, after) if a != b)
        report["fixed"].append({"map": name, "corners": len(before), "moved": moved,
                                "seeded": seeded, "solved": rep.corners,
                                "reached": rep.reached})
        bpy.data.objects.remove(ob, do_unlink=True)
    except Exception as e:
        import traceback
        report["errors"].append(f"{name}: {e}\n{traceback.format_exc()[-600:]}")

# ---- check 2: recovery -----------------------------------------------------
try:
    clear_lights()
    path = f"{DOCS}/{TARGETS[0]}"
    doc = json.loads(open(path).read())
    ob = imp.build(doc, doc_path=path)
    me = ob.data
    states = imp.object_states(ob)
    i = int(ob.get("exmateria_map/preview_state", 0))
    rig, _s, _e = imp.resolved_rig(ob, states, i)
    dirs, gains, live = lb.rig_frames(rig)
    glum = [own_luma(g) for g in gains]
    vs = lb.luma_vectors(dirs, glum, live)
    shadow = me.attributes["normals_shadow"].data
    textured = me.attributes["textured"].data
    rnd = random.Random(20260825)
    worst, n_ok, n_miss, n_tot = 0.0, 0, 0, 0
    for poly in me.polygons:
        if not textured[poly.index].value:
            continue
        for li in range(poly.loop_start, poly.loop_start + poly.loop_total):
            orig = tuple(shadow[li].vector)
            if math.sqrt(sum(c * c for c in orig)) < 1e-9:
                continue
            # the map's own render at this corner -- the target to recover
            target = own_luma(own_forward(orig, dirs, gains, live))
            while True:
                s = (rnd.gauss(0, 1), rnd.gauss(0, 1), rnd.gauss(0, 1))
                m = math.sqrt(sum(c * c for c in s))
                if m > 1e-6:
                    break
            start = [c / m for c in s]
            n, reached = lb.solve_corner(target, start, dirs, gains, glum, live, vs)
            got = own_luma(own_forward(n, dirs, gains, live))
            n_tot += 1
            if reached:
                n_ok += 1
                worst = max(worst, abs(got - target) * 255.0)
            else:
                n_miss += 1
    report["recovery"] = {"map": TARGETS[0], "corners": n_tot, "reached": n_ok,
                          "unreachable": n_miss, "worst_255": worst, "tol": TOL}
    bpy.data.objects.remove(ob, do_unlink=True)
except Exception as e:
    import traceback
    report["errors"].append(f"recovery: {e}\n{traceback.format_exc()[-600:]}")

# ---- check 3: honest residual ---------------------------------------------
# A warm lamp on one side and a cool lamp on the other asks for a hue gradient
# across the mesh.  §2: chroma reachable by choosing the normal is a median 8.15
# degrees, so the format cannot hold it and the report must SAY so as chroma,
# with brightness still near zero.
try:
    clear_lights()
    path = f"{DOCS}/{TARGETS[0]}"
    doc = json.loads(open(path).read())
    ob = imp.build(doc, doc_path=path)
    states = imp.object_states(ob)
    i = int(ob.get("exmateria_map/preview_state", 0))
    rig, _s, _e = imp.resolved_rig(ob, states, i)
    lb.seed_lamps(ob, rig)
    # Recolour the seeded suns warm and cool while PRESERVING each one's luma
    # (`energy = luma(gain) / luma(colour)`).  That asks for a hue that swings
    # across the mesh with which light dominates, while every corner's target
    # brightness stays exactly what the rig already produces -- so brightness is
    # reachable everywhere and the ONLY thing the format cannot hold is the hue.
    # A pair of bright point lamps would confound the two: their inverse-square
    # falloff drove targets far past any reachable brightness (measured max
    # 8136/255), and the check would then pass on the wrong quantity.
    grad = ((1.0, 0.35, 0.05), (0.05, 0.35, 1.0), (1.0, 1.0, 1.0))
    for k, lamp in enumerate(o for o in bpy.context.scene.objects
                             if o.type == "LIGHT" and lb.LAMP_TAG in o):
        want = grad[lamp[lb.LAMP_TAG]]
        keep = own_luma(lamp.data.color) * lamp.data.energy
        lamp.data.color = want
        lamp.data.energy = keep / own_luma(want)
    rep = lb.bake_normals(ob, bpy.context)
    bmed, bmax = rep._stat(rep.bright)
    cmed, cmax = rep._stat(rep.chroma)
    report["honest"] = {"map": TARGETS[0], "corners": rep.corners,
                        "bright_med": bmed, "bright_max": bmax,
                        "chroma_med": cmed, "chroma_max": cmax,
                        "lines": rep.lines}
except Exception as e:
    import traceback
    report["errors"].append(f"honest: {e}\n{traceback.format_exc()[-600:]}")

# ---- check 4: the authority switch, through the registered path ------------
# Decision 30: ONE switch, not three.  The Bake button and the Live toggle are
# both gone, so every solve here is triggered the way the artist triggers one --
# by flipping authority, or by moving a lamp while it is on.
try:
    clear_lights()
    path = f"{DOCS}/{TARGETS[0]}"
    doc = json.loads(open(path).read())
    ob = imp.build(doc, doc_path=path)
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    # `dir()`, never `hasattr`: `bpy.ops.map.anything_at_all` builds an operator
    # proxy on demand, so `hasattr` is True for names that do not exist and the
    # registration check it was written as could never fail.
    _ops = set(dir(bpy.ops.map))
    got = {"ops_registered": {"seed_rig_lamps", "restore_imported_normals"} <= _ops,
           "panel_registered": hasattr(bpy.types, "MAP_PT_lighting_bake")}
    # "One switch, not three" asserted as ABSENCE.  Leaving the old two in place
    # would satisfy every behavioural check below while shipping three ways to
    # write the same attribute.
    got["bake_button_gone"] = "bake_lighting" not in _ops
    got["live_toggle_gone"] = not hasattr(bpy.types.Object, "exmateria_map_live_bake")
    # Import must land OFF.  With it on, an import into a lamp-less scene solves
    # against no light at all and flattens the map -- the failure `prime_live`
    # was written for (205 of 243 normals moved on MAP000 a0).
    got["authority_default_off"] = ob.exmateria_map_lamp_authority is False

    def _normals():
        return [tuple(round(c, 3) for c in d.vector)
                for d in ob.data.attributes["normals"].data]

    def _force_solve():
        """The artist's own re-solve: off, then on.  `_authority_update` bakes
        unconditionally on the ON edge, so this is a solve through the REGISTERED
        path rather than a direct call to `bake_normals`."""
        ob.exmateria_map_lamp_authority = False
        ob.exmateria_map_lamp_authority = True

    _rom = _normals()
    got["seed_poll"] = bool(bpy.ops.map.seed_rig_lamps.poll())
    got["seed"] = sorted(bpy.ops.map.seed_rig_lamps())
    got["lamps"] = sorted(o.name for o in bpy.context.scene.objects
                          if o.type == "LIGHT")
    # Seeding alone must NOT solve: authority is still off, and "lamps arrive
    # only when asked" is not "lamps take over when they arrive".
    bpy.context.view_layer.update()
    got["seed_alone_is_silent"] = sum(1 for x, y in zip(_rom, _normals()) if x != y)
    ob.exmateria_map_lamp_authority = True
    got["authority_on_solves"] = sum(1 for x, y in zip(_rom, _normals()) if x != y)
    got["report_lines"] = len(json.loads(ob.get("exmateria_map/last_bake") or "[]"))
    got["panel_poll"] = bool(bpy.types.MAP_PT_lighting_bake.poll(bpy.context))

    # The panel's lamp readout must agree with what the SOLVE actually reads.
    # It counted `not hide_render` while `scene_lamps` also requires
    # `visible_get()`, so hiding every lamp with the Outliner's EYE left the
    # panel insisting three lamps were live while the bake saw none.  Graded as
    # AGREEMENT rather than against a literal, and read out of the panel's OWN
    # module globals -- `addon_install` + `import` yield two module objects, so
    # comparing the installed panel against the directly-imported `lb` would be
    # comparing two different copies of the rule.
    class _FakeLayout:
        def __init__(self, sink):
            self._sink = sink

        def label(self, text="", icon=None, **kw):
            self._sink.append(text)
            return self

        def row(self, align=False, **kw):
            return self

        def column(self, align=False, **kw):
            return self

        def box(self, **kw):
            return self

        def separator(self, **kw):
            return self

        def operator(self, bl_idname, text="", icon=None, **kw):
            class _Op:
                pass
            return _Op()

        def prop(self, data, prop_name, **kw):
            return self

    _panel_g = bpy.types.MAP_PT_lighting_bake.draw.__globals__

    def _panel_lamp_n():
        """The integer the panel PUTS ON SCREEN, parsed out of its own label."""
        sink = []
        bpy.types.MAP_PT_lighting_bake.draw(
            type("_S", (), {"layout": _FakeLayout(sink)})(), bpy.context)
        hits = [t for t in sink if "lamp(s)" in t]
        return int(hits[0].split()[0]) if len(hits) == 1 else -1

    def _solve_lamp_n():
        return len(_panel_g["scene_lamps"](bpy.context.scene, ob))

    got["panel_n_lit"], got["solve_n_lit"] = _panel_lamp_n(), _solve_lamp_n()
    for _l in [o for o in bpy.context.scene.objects if o.type == "LIGHT"]:
        _l.hide_set(True)                        # the Outliner's EYE
    bpy.context.view_layer.update()
    got["panel_n_hidden"], got["solve_n_hidden"] = _panel_lamp_n(), _solve_lamp_n()
    for _l in [o for o in bpy.context.scene.objects if o.type == "LIGHT"]:
        _l.hide_set(False)
    bpy.context.view_layer.update()

    # SCOPE (decision 30).  The lamps that author this map are the ones in ITS
    # collection; a light elsewhere in the scene is not this map's.  TWO arms,
    # because arm 1 alone is satisfied by a lamp that contributes nothing at all
    # -- measured, the unscoped reader gave a stray sun 1,019 corners.  The same
    # object is used for both, so the only thing that differs is its membership.
    # Each arm FORCES a solve rather than relying on the handler firing: a
    # signature that was scoped while the lamp pool was not would otherwise pass
    # arm 1 by never running at all.
    _force_solve()
    _scope_base = _normals()
    _stray = bpy.data.objects.new("stray_sun", bpy.data.lights.new("stray", "SUN"))
    bpy.context.scene.collection.objects.link(_stray)   # the scene ROOT
    _stray.rotation_euler = (1.1, 0.4, 2.3)
    _stray.data.energy = 5.0
    _force_solve()
    got["stray_lamp_corners"] = sum(1 for x, y in zip(_scope_base, _normals()) if x != y)
    bpy.context.scene.collection.objects.unlink(_stray)
    ob.users_collection[0].objects.link(_stray)         # the MAP's collection
    _force_solve()
    got["scoped_lamp_corners"] = sum(1 for x, y in zip(_scope_base, _normals()) if x != y)
    bpy.data.objects.remove(_stray, do_unlink=True)
    _force_solve()

    # Aiming a lamp means selecting it.  The authoring surface must survive that
    # -- polling on the ACTIVE object hid the panel and disabled the buttons at
    # exactly the moment the artist reached for them.
    bpy.ops.object.select_all(action="DESELECT")
    lamp = next(o for o in bpy.context.scene.objects if o.type == "LIGHT")
    lamp.select_set(True)
    bpy.context.view_layer.objects.active = lamp
    got["panel_with_lamp"] = bool(bpy.types.MAP_PT_lighting_bake.poll(bpy.context))
    got["seed_polls_with_lamp"] = bool(bpy.ops.map.seed_rig_lamps.poll())
    got["restore_polls_with_lamp"] = bool(bpy.ops.map.restore_imported_normals.poll())
    # and the lamps must not be stacked on the origin, where they cannot be clicked
    locs = [tuple(round(c, 2) for c in o.location)
            for o in bpy.context.scene.objects if o.type == "LIGHT"]
    got["lamp_locations_distinct"] = len(set(locs)) == len(locs)

    # Aiming a lamp through the ordinary Rotation X/Y/Z fields must MOVE the
    # solve.  Seeded lamps were once QUATERNION mode, where `rotation_euler` is
    # silently ignored -- the artist rotates and nothing happens at all.
    _force_solve()
    before_rot = _normals()
    l1 = next(o for o in bpy.context.scene.objects if o.type == "LIGHT")
    got["lamp_rotation_mode"] = l1.rotation_mode
    l1.rotation_euler = (1.2, 0.3, 2.0)
    bpy.context.view_layer.update()
    got["euler_moves_solve"] = sum(1 for x, y in zip(before_rot, _normals()) if x != y)

    # Under authority a lamp move re-solves with NO button press, an unchanged
    # scene must cost nothing (or the handler chases its own mesh writes round
    # forever), and the switch must actually stop it.
    live_base = _normals()
    l1.rotation_euler = (0.7, 1.1, 0.4)
    bpy.context.view_layer.update()
    got["authority_fires"] = sum(1 for x, y in zip(live_base, _normals()) if x != y)
    # Count WORK, not output.  A re-solve with unchanged lamps yields identical
    # normals -- the solve is a pure function -- so diffing them cannot tell a
    # quiet handler from one spinning on its own writes.
    runs = live_runs()
    bpy.context.view_layer.update()
    bpy.context.view_layer.update()
    now = live_runs()
    got["live_counter_found"] = runs is not None and now is not None
    got["live_idle_reruns"] = (now - runs) if got["live_counter_found"] else -1
    # and the counter must be able to MOVE, or "0 reruns" means nothing
    l1.rotation_euler = (0.9, 0.2, 1.5)
    bpy.context.view_layer.update()
    got["live_counter_moves"] = (live_runs() or 0) - (now or 0)

    # The signature guard, graded as a RULE rather than by its effect.  Its job
    # is to stop the handler re-entering on its own mesh writes -- and that loop
    # cannot be reproduced in background mode, where a mesh write does not
    # re-tag the depsgraph, so the handler never re-fires with OR without the
    # guard.  Calling it directly is what makes the rule observable: after one
    # real change, repeated calls on an UNCHANGED scene must solve exactly once.
    # (`walkable`'s import-side assertion is the same move.)
    handler = next((h for h in bpy.app.handlers.depsgraph_update_post
                    if getattr(h, "__name__", "") == "_live_handler"), None)
    if handler is None:
        got["handler_dedupes"] = -1
    else:
        l1.rotation_euler = (1.4, 0.8, 0.2)
        dg = bpy.context.evaluated_depsgraph_get()
        base_runs = live_runs()
        for _ in range(3):
            handler(bpy.context.scene, dg)
        # 0 or 1: the depsgraph may already have solved this pose, in which case
        # the guard correctly suppresses all three.  3 means no guard at all.
        got["handler_dedupes"] = live_runs() - base_runs

    # OFF COMMITS, it does not revert (decision 30).  Two arms: the flip itself
    # must not touch a byte, and a lamp moved afterwards must not either.  The
    # losing candidate -- "off reverts to the ROM" -- would fail the first.
    off_base = _normals()
    ob.exmateria_map_lamp_authority = False
    got["authority_off_commits"] = sum(1 for x, y in zip(off_base, _normals()) if x != y)
    l1.rotation_euler = (0.2, 0.5, 1.9)
    bpy.context.view_layer.update()
    got["authority_off_ignores_lamps"] = sum(
        1 for x, y in zip(off_base, _normals()) if x != y)
    # ...and what it committed must not be the ROM's, or "commits" is
    # indistinguishable from "reverts" on a map that was never solved.
    got["committed_differs_from_rom"] = sum(
        1 for x, y in zip(_rom, _normals()) if x != y)

    # FLIP-FLOP IS A FIXED POINT.  On -> off -> on with the same lamps must land
    # byte-identical, because the solve's receiver stays `normals_shadow`.  This
    # is what proves §11's no-chaining rule survived the switch: had the commit
    # written the shadow, the next solve would start from the last one.
    l1.rotation_euler = (0.2, 0.5, 1.9)          # the pose off ignored
    ob.exmateria_map_lamp_authority = True
    _flip_a = _normals()
    ob.exmateria_map_lamp_authority = False
    ob.exmateria_map_lamp_authority = True
    _flip_b = _normals()
    got["flip_flop_moves"] = sum(1 for x, y in zip(_flip_a, _flip_b) if x != y)
    # ...and the flip really did re-solve, or "identical" is satisfied by a
    # switch that does nothing at all.  Counted as WORK: exactly ONE solve
    # across off-then-on, which is also the assertion that the OFF edge writes
    # nothing.  (`_LIVE_RUNS` counts every re-solve, handler or switch.)
    bpy.context.view_layer.update()          # drain anything still pending
    _runs_before = live_runs()
    ob.exmateria_map_lamp_authority = False
    _runs_mid = live_runs()
    ob.exmateria_map_lamp_authority = True
    _runs_after = live_runs()
    got["off_edge_solves"] = (_runs_mid - _runs_before) if _runs_before is not None else -1
    got["on_edge_solves"] = (_runs_after - _runs_mid) if _runs_mid is not None else -1

    # The report reaches the object.  With the Bake button gone the HANDLER and
    # the authority flip are the only writers, so this is where the write is
    # observable at all.
    ob["exmateria_map/last_bake"] = json.dumps(["cleared by the harness"])
    _force_solve()
    after = json.loads(ob.get("exmateria_map/last_bake") or "[]")
    got["solve_writes_report"] = len(after) >= 3 and after != ["cleared by the harness"]
    report["operators"] = got
except Exception as e:
    import traceback
    report["errors"].append(f"operators: {e}\n{traceback.format_exc()[-600:]}")

# ---- check 6: ZERO LAMPS under authority is DARKNESS, not a no-op ----------
# Decision 30's headline, and the defect it names.  `bake_normals` early-returned
# on `if not lamps:` with "no lamps in the scene -- seed them from the rig
# first", so `normals` was never written at target 0.  Measured on MAP001 a0:
# aim one lamp for 1,498 corners off the ROM, then HIDE every lamp -- 0 change;
# DELETE every lamp -- 0 change.  The map stayed lit by lamps that no longer
# existed.
#
# Graded on THE PICTURE with the harness's own forward model, not on the corner
# count: "some normals moved" is satisfied by a solver that moved them anywhere.
# A check that only asserted `reached == corners` would pass on the OLD code
# too, because the old code touched nothing and reported nothing.
try:
    clear_lights()
    path = f"{DOCS}/{TARGETS[0]}"
    doc = json.loads(open(path).read())
    ob = imp.build(doc, doc_path=path)
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    me = ob.data
    states = imp.object_states(ob)
    i0 = int(ob.get("exmateria_map/preview_state", 0))
    rig, _s, _e = imp.resolved_rig(ob, states, i0)
    dirs, gains, live = lb.rig_frames(rig)
    shadow = me.attributes["normals_shadow"].data
    textured = me.attributes["textured"].data

    def _corners():
        return [tuple(d.vector) for d in me.attributes["normals"].data]

    bpy.ops.map.seed_rig_lamps()
    ob.exmateria_map_lamp_authority = True
    l1 = next(o for o in ob.users_collection[0].objects if o.type == "LIGHT")
    l1.rotation_euler = (1.2, 0.3, 2.0)        # get the map away from the ROM
    bpy.context.view_layer.update()
    lit = _corners()

    for o in [x for x in list(ob.users_collection[0].objects) if x.type == "LIGHT"]:
        bpy.data.objects.remove(o, do_unlink=True)
    ob.exmateria_map_lamp_authority = False    # off, then on: force the solve
    ob.exmateria_map_lamp_authority = True
    dark = _corners()
    lines = json.loads(ob.get("exmateria_map/last_bake") or "[]")

    z = {"lamps_left": len([x for x in ob.users_collection[0].objects
                            if x.type == "LIGHT"]),
         "moved_from_lit": sum(1 for a, b in zip(lit, dark)
                               if any(abs(x - y) > 1e-4 for x, y in zip(a, b))),
         "says_ambient": any("ambient" in l for l in lines),
         "reached_all": any("reached exactly" in l for l in lines),
         "out_of_reach": any("out of reach" in l for l in lines)}
    # THE PICTURE: at target 0 every textured corner must render black, judged
    # by the forward model written in this harness rather than the module's.
    worst, n, cap = 0.0, 0, 0
    for poly in me.polygons:
        if not textured[poly.index].value:
            continue
        for li in range(poly.loop_start, poly.loop_start + poly.loop_total):
            orig = tuple(shadow[li].vector)
            if math.sqrt(sum(c * c for c in orig)) < 1e-9:
                continue
            n += 1
            worst = max(worst, own_luma(own_forward(dark[li], dirs, gains, live)))
            # §2's dark cap: a corner whose ROM normal ALREADY sits behind every
            # terminator is returned unchanged, so it must come back
            # byte-identical rather than being re-aimed somewhere else that is
            # also black.
            if all(abs(x - y) <= 1e-4 for x, y in zip(dark[li], orig)):
                cap += 1
    z["textured_corners"] = n
    z["worst_luma_255"] = worst * 255.0
    z["dark_cap"] = cap
    z["dark_cap_pct"] = round(100.0 * cap / n, 2) if n else 0.0
    report["zero_lamps"] = z
    bpy.data.objects.remove(ob, do_unlink=True)
except Exception as e:
    import traceback
    report["errors"].append(f"zero_lamps: {e}\n{traceback.format_exc()[-600:]}")

# ---- check 5: point / spot / area actually reach the solve -----------------
try:
    clear_lights()
    path = f"{DOCS}/{TARGETS[0]}"
    doc = json.loads(open(path).read())
    ob = imp.build(doc, doc_path=path)
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    bpy.ops.map.seed_rig_lamps()

    def resolve():
        """Force a solve through the REGISTERED path: with the Bake button gone
        (decision 30) the authority switch's ON edge is what re-solves on
        demand, and it does so unconditionally rather than through the lamp
        signature -- which is what makes a clean A/B possible at all."""
        ob.exmateria_map_lamp_authority = False
        ob.exmateria_map_lamp_authority = True

    resolve()

    def _n():
        return [tuple(round(c, 3) for c in d.vector)
                for d in ob.data.attributes["normals"].data]
    base = _n()
    co = [v.co for v in ob.data.vertices]
    lo = Vector((min(c.x for c in co), min(c.y for c in co), min(c.z for c in co)))
    hi = Vector((max(c.x for c in co), max(c.y for c in co), max(c.z for c in co)))
    mid, top = (lo + hi) * 0.5, hi.z - lo.z

    def probe(kind, power, away=False, dist=1.0, **kw):
        """Returns (corners moved, this lamp's peak contribution).

        The peak is what makes the check discriminating.  "Does this lamp type
        move anything" is satisfied by a lamp that is WRONG in every respect —
        it is how `falloff_dropped` and `area_double_sided` first read blind.
        """
        d = bpy.data.lights.new("probe", kind)
        d.energy, d.use_shadow = power, False
        for k, v in kw.items():
            setattr(d, k, v)
        o = bpy.data.objects.new("probe", d)
        o.location = mid + Vector((0.0, 0.0, top * dist))
        o.rotation_euler = (math.pi if away else 0.0, 0.0, 0.0)
        # THE MAP'S collection, not the scene root: decision 30 scopes the solve
        # to it, so a probe in the root would be out of scope and every lamp
        # type would read 0 -- a blind check that looks like a broken solver.
        ob.users_collection[0].objects.link(o)
        resolve()
        moved = sum(1 for x, y in zip(base, _n()) if x != y)
        peak = 0.0
        for line in json.loads(ob.get("exmateria_map/last_bake") or "[]"):
            if line.startswith("lamp probe ") and "peak " in line:
                peak = float(line.rsplit("peak ", 1)[1])
        bpy.data.objects.remove(o, do_unlink=True)
        bpy.data.lights.remove(d)
        resolve()
        return moved, peak

    t = {}
    t["point"], near = probe("POINT", 1.0e5, dist=1.0)
    # Inverse-square: the SAME lamp twice as far away must land far dimmer.
    # Without falloff the two are identical and `point > 0` still passes.
    _far_moved, far = probe("POINT", 1.0e5, dist=3.0)
    t["falloff_ratio"] = round(near / far, 3) if far > 1e-12 else 999.0
    # Hiding a lamp must exclude it — by ANY of the three Outliner switches, not
    # just the camera icon.  The eye is how anyone A/Bs a light.
    for tag, on, off in (("hidden_eye", lambda o: o.hide_set(True), lambda o: o.hide_set(False)),
                         ("hidden_monitor", lambda o: setattr(o, "hide_viewport", True),
                          lambda o: setattr(o, "hide_viewport", False)),
                         ("hidden_render", lambda o: setattr(o, "hide_render", True),
                          lambda o: setattr(o, "hide_render", False))):
        d = bpy.data.lights.new("hidden", "POINT")
        d.energy, d.use_shadow = 1.0e5, False
        o = bpy.data.objects.new("hidden", d)
        o.location = mid + Vector((0.0, 0.0, top))
        # THE MAP'S collection.  In the scene root decision 30 puts it out of
        # scope, so "hidden contributes 0" would pass on a lamp that was never
        # in the bake to begin with -- a blind check wearing a green tick.
        ob.users_collection[0].objects.link(o)
        resolve()
        # The control arm: VISIBLE, the same lamp must move something, or the
        # hidden arm below asserts nothing at all.
        t[tag + "_visible"] = sum(1 for x, y in zip(base, _n()) if x != y)
        on(o)
        resolve()
        t[tag] = sum(1 for x, y in zip(base, _n()) if x != y)
        off(o)
        bpy.data.objects.remove(o, do_unlink=True)
        bpy.data.lights.remove(d)
        resolve()
    t["spot_narrow"], _ = probe("SPOT", 5.0e6, spot_size=math.radians(20), spot_blend=0.1)
    t["spot_wide"], _ = probe("SPOT", 5.0e6, spot_size=math.radians(150), spot_blend=0.1)
    t["spot_narrow"], _ = t["spot_narrow"], None
    t["area"], area_front = probe("AREA", 5.0e6, size=80.0)
    # Single-sided: an emitter turned away must go nearly dark, not merely dimmer.
    _away_moved, area_back = probe("AREA", 5.0e6, size=80.0, away=True)
    t["area_facing_ratio"] = round(area_front / area_back, 3) if area_back > 1e-12 else 999.0
    report["types"] = t
except Exception as e:
    import traceback
    report["errors"].append(f"types: {e}\n{traceback.format_exc()[-600:]}")

open(OUT, "w").write(json.dumps(report))
print("BAKE-HARNESS-DONE")
'''


def ensure_addon():
    zf_path = TMP / "exmateria_map.zip"
    if zf_path.exists():
        zf_path.unlink()
    with zipfile.ZipFile(zf_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(ADDON_DIR.rglob("*")):
            if f.is_file() and "__pycache__" not in str(f):
                zf.write(f, f.relative_to(ADDON_DIR.parent))
    return zf_path


def main():
    TMP.mkdir(exist_ok=True)
    DOCS.mkdir(exist_ok=True)
    sys.path.insert(0, str(PKG))
    from exmateria_map import corpus as pkg_corpus, dump as pkg_dump, mapfile
    map_dir = pkg_corpus.map_dir()
    if map_dir is None:
        print("SKIPPED: no corpus; set EXMATERIA_ASSETS_DIR")
        sys.exit(77)

    targets = []
    for num in mapfile.map_numbers(map_dir):
        try:
            arrangements = pkg_dump.arrangements(map_dir, num)
        except mapfile.BindError:
            continue
        targets.extend((num, a) for a in arrangements)
    built = []
    for num, a in targets:
        try:
            doc, _sheets = pkg_dump.dump(map_dir, num, a)
        except pkg_dump.DumpError:
            continue
        (DOCS / f"MAP{num:03d}.a{a}.json").write_text(json.dumps(doc))
        built.append(f"MAP{num:03d}.a{a}.json")
    if LIMIT:
        built = built[:LIMIT]
    print(f"corpus: {len(built)} arrangement(s)")

    ensure_addon()
    script = TMP / "run_bake.py"
    script.write_text(SCRIPT_TEMPLATE
                      .replace("@ADDONPKG@", str(ADDON_DIR.parent))
                      .replace("@DOCS@", str(DOCS))
                      .replace("@OUT@", str(REPORT))
                      .replace("@TARGETS@", json.dumps(built))
                      .replace("@ZIP@", str(TMP / "exmateria_map.zip"))
                      .replace("@TOL@", "1.0"))
    if REPORT.exists():
        REPORT.unlink()
    # `--factory-startup` skips the artist's preferences but does NOT redirect
    # where `addon_install` WRITES: without this the harness installs into
    # ~/.config/blender/<ver>/scripts/addons, i.e. over the artist's own copy.
    # That is how a MUTANT from `bake_mutation_audit.py` came to be installed in
    # a live Blender, silently disabling the report for a real session.
    # `blender_prefs_persist.py` already isolates this way.
    userres = TMP / "userres"
    userres.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, BLENDER_USER_RESOURCES=str(userres))
    proc = subprocess.run([BLENDER, "-b", "--factory-startup", "--python", str(script)],
                          capture_output=True, text=True, env=env)
    if "BAKE-HARNESS-DONE" not in proc.stdout:
        sys.stdout.write(proc.stdout[-4000:])
        sys.stdout.write("\n[stderr]\n" + proc.stderr[-3000:])
    if not REPORT.exists():
        print("FAIL: no report written")
        sys.exit(1)
    r = json.loads(REPORT.read_text())

    fails = []
    for e in r["errors"]:
        fails.append(f"harness error: {e.splitlines()[0]}")

    # ---- check 1
    fx = r["fixed"]
    exact = [f for f in fx if f["moved"] == 0]
    movers = [f for f in fx if f["moved"]]
    print(f"\n1. FIXED POINT   {len(exact)}/{len(fx)} arrangement(s) byte-identical")
    for f in movers[:8]:
        print(f"     MOVED {f['map']}: {f['moved']}/{f['corners']} corner(s)")
    if movers:
        fails.append(f"fixed point: {len(movers)} arrangement(s) moved")
    if not fx:
        fails.append("fixed point: nothing ran")

    # ---- check 2
    rc = r["recovery"]
    if rc:
        ok = rc["worst_255"] <= rc["tol"] and rc["unreachable"] == 0
        print(f"2. RECOVERY      {rc['reached']}/{rc['corners']} recovered, "
              f"worst {rc['worst_255']:.6f}/255 (tol {rc['tol']}), "
              f"unreachable {rc['unreachable']}  [{rc['map']}]")
        if not ok:
            fails.append(f"recovery: worst {rc['worst_255']:.4f}/255, "
                         f"{rc['unreachable']} unreachable")
    else:
        fails.append("recovery: did not run")

    # ---- check 3
    hn = r["honest"]
    if hn:
        # the gradient must land in CHROMA, not in brightness, and the report
        # must carry them as two separate lines
        two = sum(1 for l in hn["lines"] if "residual:" in l)
        ok = hn["chroma_max"] > 2.0 and hn["bright_max"] <= 1.0 and two == 2
        print(f"3. HONEST RESID. chroma med {hn['chroma_med']:.2f} deg / max "
              f"{hn['chroma_max']:.2f} deg, brightness med {hn['bright_med']:.3f} "
              f"/ max {hn['bright_max']:.3f} /255, {two} residual line(s)")
        if not ok:
            fails.append(f"honest residual: chroma_max={hn['chroma_max']:.2f}, "
                         f"bright_max={hn['bright_max']:.3f}, lines={two}")
    else:
        fails.append("honest residual: did not run")

    # ---- check 4
    op = r.get("operators")
    if op:
        want = (op["ops_registered"] and op["panel_registered"]
                and op["bake_button_gone"] and op["live_toggle_gone"]
                and op["authority_default_off"]
                and op["seed_poll"] and op["seed"] == ["FINISHED"]
                and len(op["lamps"]) == 3
                and op["seed_alone_is_silent"] == 0
                and op["authority_on_solves"] > 0
                and op["report_lines"] >= 3
                and op["panel_poll"] and op["panel_with_lamp"]
                and op["seed_polls_with_lamp"] and op["restore_polls_with_lamp"]
                and op["lamp_locations_distinct"]
                and op["panel_n_lit"] == op["solve_n_lit"] == 3
                and op["panel_n_hidden"] == op["solve_n_hidden"] == 0
                and op["stray_lamp_corners"] == 0
                and op["scoped_lamp_corners"] > 0
                and op["lamp_rotation_mode"] == "XYZ" and op["euler_moves_solve"] > 0
                and op["authority_fires"] > 0 and op["live_counter_found"]
                and op["live_idle_reruns"] == 0 and op["live_counter_moves"] > 0
                and 0 <= op["handler_dedupes"] <= 1
                and op["authority_off_commits"] == 0
                and op["authority_off_ignores_lamps"] == 0
                and op["committed_differs_from_rom"] > 0
                and op["flip_flop_moves"] == 0
                and op["off_edge_solves"] == 0 and op["on_edge_solves"] == 1
                and op["solve_writes_report"])
        print(f"4. AUTHORITY     seed={op['seed']}, {len(op['lamps'])} lamp(s), "
              f"{op['report_lines']} report line(s), "
              f"bake-button-gone={op['bake_button_gone']} "
              f"live-toggle-gone={op['live_toggle_gone']} "
              f"default-off={op['authority_default_off']}\n"
              f"                 panel={op['panel_registered']}/{op['panel_poll']}, "
              f"survives lamp selection={op['panel_with_lamp']}/"
              f"{op['seed_polls_with_lamp']}/{op['restore_polls_with_lamp']}, "
              f"lamps apart={op['lamp_locations_distinct']}, "
              f"rot mode={op['lamp_rotation_mode']} moves "
              f"{op['euler_moves_solve']} normals\n"
              f"                 lamp readout: panel/solve lit="
              f"{op['panel_n_lit']}/{op['solve_n_lit']} "
              f"eye-hidden={op['panel_n_hidden']}/{op['solve_n_hidden']}\n"
              f"                 scope: stray lamp moves "
              f"{op['stray_lamp_corners']} corner(s), the same lamp inside the "
              f"map's collection moves {op['scoped_lamp_corners']}\n"
              f"                 seed-alone={op['seed_alone_is_silent']} "
              f"authority-on solves {op['authority_on_solves']}, "
              f"lamp move fires={op['authority_fires']} "
              f"idle-reruns={op['live_idle_reruns']} (counter moves "
              f"{op['live_counter_moves']}) 3-calls-solve="
              f"{op['handler_dedupes']}x\n"
              f"                 OFF COMMITS: flip moves "
              f"{op['authority_off_commits']}, later lamp move moves "
              f"{op['authority_off_ignores_lamps']}, committed-vs-ROM="
              f"{op['committed_differs_from_rom']}\n"
              f"                 flip-flop: moves {op['flip_flop_moves']}, "
              f"off-edge solves {op['off_edge_solves']}, on-edge solves "
              f"{op['on_edge_solves']}, report written="
              f"{op['solve_writes_report']}")
        if not want:
            fails.append(f"operators: {op}")
    else:
        fails.append("operators: did not run")

    ty = r.get("types")
    if ty:
        ok = (ty["point"] > 0 and ty["area"] > 0 and ty["spot_wide"] > 0
              and ty["spot_narrow"] > 0 and ty["spot_wide"] > ty["spot_narrow"]
              and ty["hidden_eye"] == 0 and ty["hidden_monitor"] == 0
              and ty["hidden_render"] == 0
              and ty["hidden_eye_visible"] > 0 and ty["hidden_monitor_visible"] > 0
              and ty["hidden_render_visible"] > 0
              and ty["falloff_ratio"] > 2.0 and ty["area_facing_ratio"] > 2.0)
        print(f"5. LAMP TYPES    point={ty['point']} area={ty['area']} "
              f"spot 20deg={ty['spot_narrow']} spot 150deg={ty['spot_wide']} "
              f"(cone must widen the reach)\n"
              f"                 falloff near/far={ty['falloff_ratio']}x, "
              f"area front/back={ty['area_facing_ratio']}x, "
              f"hidden eye/monitor/camera="
              f"{ty['hidden_eye']}/{ty['hidden_monitor']}/{ty['hidden_render']} "
              f"(the same lamps VISIBLE move {ty['hidden_eye_visible']}/"
              f"{ty['hidden_monitor_visible']}/{ty['hidden_render_visible']})")
        if not ok:
            fails.append(f"lamp types: {ty}")
    else:
        fails.append("lamp types: did not run")

    zl = r.get("zero_lamps")
    if zl:
        ok = (zl["lamps_left"] == 0 and zl["moved_from_lit"] > 0
              and zl["says_ambient"] and zl["reached_all"]
              and not zl["out_of_reach"]
              and zl["worst_luma_255"] <= 0.5
              and zl["dark_cap"] > 0 and zl["dark_cap"] < zl["textured_corners"])
        print(f"6. ZERO LAMPS    {zl['moved_from_lit']} corner(s) moved when the "
              f"last lamp was deleted, worst residual luma "
              f"{zl['worst_luma_255']:.6f}/255 over {zl['textured_corners']} "
              f"textured corner(s)\n"
              f"                 all reached exactly={zl['reached_all']} "
              f"(none out of reach={not zl['out_of_reach']}), "
              f"report says ambient={zl['says_ambient']}, "
              f"dark cap returns byte-identical: {zl['dark_cap']} "
              f"({zl['dark_cap_pct']}%)")
        if not ok:
            fails.append(f"zero lamps: {zl}")
    else:
        fails.append("zero lamps: did not run")

    print()
    if fails:
        for f in fails:
            print(f"FAIL {f}")
        sys.exit(1)
    print("PASS all six checks")


if __name__ == "__main__":
    main()
