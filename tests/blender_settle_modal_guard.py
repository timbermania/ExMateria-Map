"""The settle must not mutate the mesh while a GESTURE is still open.

This is the regression test for the SIGSEGV in
`docs/paint-crash-diagnosis.md`.  `settle_op._tick` runs at 4 Hz and reaches
`land_compile`, which does `_write_binding` plus `me.update()` on the original
mesh; that tags the depsgraph and frees the EVALUATED mesh, whose corner and UV
arrays Blender's projective texture paint caches in `ProjPaintState` for the
whole of a modal stroke.  Fire it inside an open stroke and the next dab reads
freed memory.

**Why the subject is a DUMMY modal operator and not a paint stroke.**  A paint
stroke driven from Python (`bpy.ops.paint.image_paint(stroke=[...])`) is one
`exec` call: it runs to completion inside the call and is never modal, so it
populates `window.modal_operators` with nothing and could not exercise this
guard at all.  What the guard actually asks is *"is a modal operator running"*,
and any modal operator answers that -- so the test registers one it can start
and stop on demand, which makes the loop deterministic and needs no synthetic
mouse input.  The artist's real stroke is `PAINT_OT_image_paint` in the same
list.

**Both arms, because one arm cannot fail.**  Arm A (a gesture is open -> no
land) would pass just as well against a settle that never lands for any reason
-- a canvas that was not dirty, a clock that never armed, an addon that failed
to register.  Arm B (the same session, the gesture closed -> it DOES land) is
what proves the settle was loaded and would have fired, so arm A's silence is
the guard and not an accident.

Run:  python3 tests/blender_settle_modal_guard.py
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
ROOT = PKG.parent
MAPDIR = ROOT / "project-assets" / "fft-extract" / "MAP"
TMP = Path(__file__).resolve().parent / ".blender_settle_modal_guard"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from blender_env import isolated_env                            # noqa: E402


DRIVER = r'''
import json
import os
import sys
import traceback

import bpy

A = json.loads(os.environ["MODAL_GUARD_ARGS"])
LOG = open(A["log"], "a", buffering=1)


def say(*p):
    LOG.write(" ".join(str(x) for x in p) + "\n")
    LOG.flush()
    os.fsync(LOG.fileno())


sys.path.insert(0, A["addon_pkg"])
import exmateria_map                                            # noqa: E402

exmateria_map.register()
from exmateria_map import live_link as _L                       # noqa: E402
_L.DEFAULT_PORT = 9                    # never a real emulator from a test
from exmateria_map import settle_op, compile_op                 # noqa: E402

say("BOOT", bpy.app.version_string)

HITS = {"a": 0, "b": 0, "modal_seen_a": 0, "ticks_a": 0, "ticks_b": 0}
PHASE = {"now": "setup"}
_real_land = settle_op.land_compile


def land_spy(ob, *a, **k):
    HITS[PHASE["now"]] = HITS.get(PHASE["now"], 0) + 1
    say(f"  LAND during phase {PHASE['now']} mode={getattr(ob, 'mode', '?')}")
    return _real_land(ob, *a, **k)


settle_op.land_compile = land_spy
compile_op.land_compile = land_spy


class GUARD_OT_dummy_gesture(bpy.types.Operator):
    """A modal operator that holds the gesture open until told to stop.

    It adds its own timer so it keeps receiving events with no mouse moving --
    a modal operator that is never sent an event is never asked to finish.
    """
    bl_idname = "guard.dummy_gesture"
    bl_label = "Dummy gesture"
    _timer = None
    stop = False

    def modal(self, context, event):
        if GUARD_OT_dummy_gesture.stop:
            context.window_manager.event_timer_remove(self._timer)
            say("  dummy gesture ENDED")
            return {"FINISHED"}
        return {"PASS_THROUGH"}

    def invoke(self, context, event):
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.05, window=context.window)
        wm.modal_handler_add(self)
        say("  dummy gesture STARTED")
        return {"RUNNING_MODAL"}


bpy.utils.register_class(GUARD_OT_dummy_gesture)

ST = {"i": 0, "ob": None}


def view3d():
    for w in bpy.context.window_manager.windows:
        for ar in w.screen.areas:
            if ar.type == "VIEW_3D":
                for rg in ar.regions:
                    if rg.type == "WINDOW":
                        return w, ar, rg
    return None


def modal_now():
    out = []
    for w in bpy.context.window_manager.windows:
        out += [o.bl_idname for o in getattr(w, "modal_operators", [])]
    return out


def dabs(n, region, j):
    w, h = region.width, region.height
    out = []
    for i in range(n):
        t = (i + 1) / (n + 1)
        x = region.x + int(w * (0.25 + 0.5 * t))
        y = region.y + int(h * (0.5 + 0.18 * j))
        out.append({"name": "d", "is_start": i == 0, "location": (0, 0, 0),
                    "mouse": (x, y), "mouse_event": (x, y), "pressure": 1.0,
                    "size": 40.0, "time": float(i),
                    "x_tilt": 0.0, "y_tilt": 0.0})
    return out


def do_import():
    bpy.ops.import_map.gns(filepath=A["gns"], arrangement="0")
    from exmateria_map.import_document import marker_in_scene
    ST["ob"] = marker_in_scene(bpy.context)
    say("import ok", getattr(ST["ob"], "name", None))


def do_sheet():
    say("paint_sheet ->", bpy.ops.exmateria_map.paint_sheet())


def do_convert():
    say("convert ->", bpy.ops.exmateria_map.convert_manifold(scale="1"))


def do_enter_paint():
    v = view3d()
    ob = ST["ob"]
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    with bpy.context.temp_override(window=v[0], area=v[1], region=v[2]):
        bpy.ops.object.mode_set(mode="TEXTURE_PAINT")
    say("mode now", ob.mode)


def paint(j):
    def go():
        v = view3d()
        with bpy.context.temp_override(window=v[0], area=v[1], region=v[2]):
            bpy.ops.paint.image_paint(stroke=dabs(A["dabs"], v[2], j))
        canvas = bpy.context.tool_settings.image_paint.canvas
        say(f"stroke {j} dirty={canvas.is_dirty if canvas else None}")
    return go


def start_gesture():
    v = view3d()
    GUARD_OT_dummy_gesture.stop = False
    with bpy.context.temp_override(window=v[0], area=v[1], region=v[2]):
        bpy.ops.guard.dummy_gesture("INVOKE_DEFAULT")
    say("  modal_operators now:", modal_now())
    PHASE["now"] = "a"


def watch_a():
    HITS["ticks_a"] += 1
    if modal_now():
        HITS["modal_seen_a"] += 1


def end_gesture():
    GUARD_OT_dummy_gesture.stop = True


def enter_b():
    say("  modal_operators now:", modal_now())
    PHASE["now"] = "b"


def watch_b():
    HITS["ticks_b"] += 1


def finish():
    say("HITS", json.dumps(HITS))
    say("DONE")
    bpy.ops.wm.quit_blender()


# Arm A: paint, open a gesture, and hold it open for well over the settle's
# quiet period.  Arm B: close it and wait the same again.
PLAN = [do_import, do_sheet, do_convert, do_sheet, do_enter_paint]
PLAN += [paint(0)]
PLAN += [start_gesture]
PLAN += [watch_a] * A["watch"]
PLAN += [end_gesture]
PLAN += [enter_b]
PLAN += [watch_b] * A["watch"]
PLAN += [finish]


def tick():
    i = ST["i"]
    if i >= len(PLAN):
        return None
    ST["i"] += 1
    try:
        PLAN[i]()
    except Exception:
        say("EXC in step", i, getattr(PLAN[i], "__name__", PLAN[i]))
        traceback.print_exc(file=LOG)
        LOG.flush()
    return A["tick"]


bpy.app.timers.register(tick, first_interval=1.0)
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--blender", default="blender")
    ap.add_argument("--map", type=int, default=22)
    ap.add_argument("--dabs", type=int, default=12)
    ap.add_argument("--watch", type=int, default=24)     # 6 s at tick 0.25
    ap.add_argument("--tick", type=float, default=0.25)
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    gns = MAPDIR / f"MAP{args.map:03d}.GNS"
    if not gns.exists():
        print(f"SKIP: {gns} is absent (project-assets is gitignored)")
        return 0

    TMP.mkdir(parents=True, exist_ok=True)
    driver = TMP / "driver.py"
    driver.write_text(DRIVER)
    log = TMP / "guard.log"
    log.unlink(missing_ok=True)

    env = isolated_env(tag="settle_modal_guard")
    env["MODAL_GUARD_ARGS"] = json.dumps({
        "log": str(log), "addon_pkg": str(PKG / "addons"), "gns": str(gns),
        "dabs": args.dabs, "watch": args.watch, "tick": args.tick,
    })
    # HEADFUL: the settle's subject is the paint path, and `--background`
    # deletes it.  See `blender_paint_crash.py` for the same reason at length.
    proc = subprocess.run(
        [args.blender, "--factory-startup", "--python", str(driver)],
        capture_output=True, text=True, env=env, timeout=args.timeout)

    text = log.read_text() if log.exists() else ""
    print(text or "(no log)")

    if "DONE" not in text:
        print(f"INCONCLUSIVE: the driver did not finish (rc {proc.returncode}) "
              f"-- a headful Blender whose window is closed exits 0, so this "
              f"graded nothing either way")
        return 1

    hits = json.loads(text.rsplit("HITS ", 1)[1].splitlines()[0])
    ok = True
    # The arm that keeps arm A honest: it must have SEEN the modal operator, or
    # "no land while a gesture is open" is a claim about a session that had no
    # gesture in it.
    if not hits["modal_seen_a"]:
        print("FAILED HARNESS: `window.modal_operators` was empty for every "
              "tick of arm A -- the dummy gesture never registered, so arm A "
              "measured nothing")
        ok = False
    if hits["a"]:
        print(f"FAIL arm A: the settle landed {hits['a']} compile(s) while a "
              f"modal gesture was open -- that is the mesh write that frees "
              f"the evaluated mesh under a live ProjPaintState")
        ok = False
    if not hits["b"]:
        print("FAIL arm B: the settle landed nothing after the gesture closed "
              "-- so arm A's silence proves nothing about the guard")
        ok = False
    print("PASS: deferred while the gesture was open, landed once it closed"
          if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
