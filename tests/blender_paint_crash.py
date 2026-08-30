"""The reproduction loop for the Blender SIGSEGV in texture paint.

**What this grades is the SIGNAL, not a check count.** The bug is a segfault:
the subject either dies (returncode -11 / 139) or it does not. There is no
assertion inside Blender that can fire, because the process is gone before it
could write one -- so the report is the progress log, flushed and `fsync`ed
after every step, which names the step that was running when it died.

**Why it is HEADFUL and timer-driven.** The crash is inside Blender's
projective texture paint (`ProjPaintState`), and that state only exists on the
3D-viewport paint path, which is reached through the draw/depsgraph loop. A
script that runs straight through at `--python` time never lets the event loop
turn, so it never builds the state that dies. One step per timer tick gives
Blender its redraws back. `--background` would remove the whole code path this
is about, which is why the repo convention against it is load-bearing here
rather than incidental.

**The sharpness guarantee.** A stroke that deposits no pixels cannot reach the
code that crashes, so a quiet run would be a measurement of the harness rather
than of the addon. The driver fingerprints the paint canvas at mode entry and
again after the strokes, and a launch where the canvas did not move is graded
**FAILED HARNESS**, never green. That is the arm that keeps "0 crashes" honest.

WHAT IT REPRODUCES, and the one flag that unlocked it. `--modal` drives the
stroke with `Window.event_simulate` instead of an `exec` stroke list, which is
the difference between a state that lives inside one call and one that lives
across timer ticks. With `--modal --no-guard` the crash is **6 of 7 launches**,
first try, with the artist's own backtrace signature; with `--modal` alone
(the shipped guard) it is **0 of 7** over three times the strokes (60 against
21), and those launches still report
`PAINTED_EVER: True` and `MODAL_EVER: True`, so arm B is not green by being
quiet. That pair is the direction test for the fix in `settle_op._mid_gesture`.

`Window.event_simulate` IS available in this build -- an earlier note here said
it was not. The method is on `bpy.types.Window.bl_rna.functions` either way and
raises "Not running with '--enable-event-simulate' enabled" until the launcher
passes that flag, which `--modal` now does. `hasattr` is not the check: it
answers False on the TYPE for an RNA function and True on an instance of a
build that refuses every call.

The exec-stroke arms (the default, and every arm before 2026-08-29) do not
reproduce and structurally cannot: `bpy.ops.paint.image_paint(stroke=[...])`
builds `ProjPaintState` and destroys it inside the one call, so no timer, push
or depsgraph re-evaluation can free mesh state while it is still pointing into
it. They are kept because they grade the same operator sequence cheaply, but a
green run of them is not evidence about this bug.

The other thing that kept the loop quiet was cadence, not just modality: the
exec arms fire a stroke every 0.02s while `settle_op.QUIET_DEFAULT` is 1.5s, so
the canvas is never quiet and the settle is starved of the event under
investigation. `ms_hold` is the fix -- it stands still for 2.4s with the stroke
OPEN, which is the artist's pause mid-gesture and the whole hypothesis in one
step.

Run:  python3 tests/blender_paint_crash.py [--live-push] [--perturb] ...
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
ROOT = PKG.parent
ADDON_PKG = PKG / "addons"
MAPDIR = ROOT / "project-assets" / "fft-extract" / "MAP"
TMP = Path(__file__).resolve().parent / ".blender_paint_crash"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from blender_env import isolated_env                            # noqa: E402


DRIVER = r'''
"""In-Blender driver. One step per timer tick; every step flushed to the log."""
import json
import os
import sys
import traceback

import bpy

ARGS = json.loads(os.environ["PAINT_CRASH_ARGS"])
LOG = open(ARGS["log"], "a", buffering=1)


def say(*parts):
    LOG.write(" ".join(str(p) for p in parts) + "\n")
    LOG.flush()
    os.fsync(LOG.fileno())          # a segfault must not lose the last line


sys.path.insert(0, ARGS["addon_pkg"])
import exmateria_map                                            # noqa: E402

exmateria_map.register()

# Nothing here reaches a real emulator unless the arm asked for it: the box
# this runs on has a PCSX-Redux on 8080, possibly mid-battle.
if not ARGS.get("live_push"):
    from exmateria_map import live_link as _L
    _L.DEFAULT_PORT = 9                                    # the discard port

say("BOOT", bpy.app.version_string)

if ARGS.get("no_guard"):
    # Reproduce PRE-FIX behaviour on purpose. The direction test for the fix
    # is two arms of this harness: with the guard, the settle is held out of
    # an open stroke; without it, the settle lands mid-stroke exactly as it
    # did for the artist. An arm that only ever runs the fixed code can show
    # "no crash" and never show that the guard is why.
    from exmateria_map import settle_op as _S
    _S._mid_gesture = lambda: False
    say("GUARD DISABLED (pre-fix behaviour)")


def view3d():
    """(window, area, region) of the first 3D viewport, or None."""
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type != "VIEW_3D":
                continue
            for region in area.regions:
                if region.type == "WINDOW":
                    return win, area, region
    return None


def image_editors():
    """Every Image Editor SPACE, the way `paint.image_editor_spaces` counts.

    Over `bpy.data.screens`, not the active window's screen: the addon builds
    its own workspace on import, so a walk of the active screen alone reported
    ZERO and would have made an absent arm read as a present one. The crash
    trails say "shown in 4 Image Editor(s)" -- this is how that is observed.
    """
    out = []
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == "IMAGE_EDITOR":
                for space in area.spaces:
                    if space.type == "IMAGE_EDITOR":
                        out.append(space)
    return out


def canvas():
    ip = getattr(bpy.context.tool_settings, "image_paint", None)
    return getattr(ip, "canvas", None) if ip else None


def canvas_fingerprint():
    """A cheap hash of the paint canvas; None when there is no canvas."""
    img = canvas()
    if img is None or not img.has_data:
        return None
    n = len(img.pixels)
    buf = [0.0] * n
    img.pixels.foreach_get(buf)
    step = max(1, n // 40000)
    return hash(tuple(buf[::step]))


def stroke_points(n, region, jitter):
    """A dab list across the middle of the viewport."""
    w, h = region.width, region.height
    pts = []
    for i in range(n):
        t = (i + 1) / (n + 1)
        pts.append({
            "name": "d",
            "is_start": i == 0,
            "location": (0.0, 0.0, 0.0),
            "mouse": (region.x + int(w * (0.25 + 0.5 * t)),
                      region.y + int(h * (0.5 + 0.18 * jitter))),
            "mouse_event": (region.x + int(w * (0.25 + 0.5 * t)),
                            region.y + int(h * (0.5 + 0.18 * jitter))),
            "pressure": 1.0,
            "size": 40.0,
            "time": float(i),
            "x_tilt": 0.0,
            "y_tilt": 0.0,
        })
    return pts


STATE = {"cycle": 0, "stroke": 0, "step": 0, "ob": None, "pushes": 0,
         "fp0": None, "painted": False, "ms_x": 0, "ms_y": 0,
         "modal_ever": False}


def wipe():
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    for me in list(bpy.data.meshes):
        bpy.data.meshes.remove(me)


def do_import():
    say("import", ARGS["gns"], "a" + str(ARGS["arrangement"]))
    say("  ->", bpy.ops.import_map.gns(filepath=ARGS["gns"],
                                       arrangement=str(ARGS["arrangement"])))
    from exmateria_map.import_document import marker_in_scene
    ob = marker_in_scene(bpy.context)
    STATE["ob"] = ob
    say("  object:", getattr(ob, "name", None),
        "polys:", len(ob.data.polygons) if ob else -1)


def do_paint_sheet():
    say("paint_sheet")
    say("  ->", bpy.ops.exmateria_map.paint_sheet())
    spaces = image_editors()
    say("  image editors:", len(spaces),
        "holding an image:", sum(1 for s in spaces if s.image is not None),
        "canvas:", getattr(canvas(), "name", None),
        "mode:", bpy.context.tool_settings.image_paint.mode)


def do_convert():
    say("convert_manifold scale=1")
    say("  ->", bpy.ops.exmateria_map.convert_manifold(scale="1"))


def do_push(replace=False):
    """A push at the artist's rhythm: paint a bit, push, paint a bit.

    `replace_loaded_map` only on the FIRST push of a cycle -- that is step 4 of
    the recipe; every push after it is the incremental one whose report named
    the texture packets in crash 4's trail."""
    say("live_push", "replace" if replace else "incremental",
        "#%d" % STATE["pushes"])
    try:
        r = bpy.ops.map.live_push(replace_loaded_map=replace)
    except Exception as e:                                      # noqa: BLE001
        say("  push raised:", type(e).__name__, e)
        return
    STATE["pushes"] += 1
    say("  ->", r)


def do_push_replace():
    do_push(replace=True)


def do_isolate():
    """`map.live_isolate` -- in the 2026-08-29 08:57 trail and in no arm of this
    harness before it.  It hides units, which is a depsgraph-visible change to
    objects that share the scene with the one being painted."""
    say("live_isolate")
    try:
        say("  ->", bpy.ops.map.live_isolate())
    except Exception as exc:                                  # noqa: BLE001
        say("  !! live_isolate refused:", repr(exc))


def do_ortho():
    """The LAST thing in the crash trail before the backtrace.

    `bpy.data.screens["Layout.001"]. = 'ORTHO'` -- a view change, which is what
    "move the camera around" reduces to, and it re-evaluates the depsgraph for
    the draw that follows.
    """
    v = view3d()
    if v is None:
        return
    win, area, region = v
    rv3d = getattr(area.spaces.active, "region_3d", None)
    if rv3d is None:
        say("  !! no region_3d")
        return
    rv3d.view_perspective = "ORTHO"
    say("view ORTHO")


def do_enter_paint():
    ob = STATE["ob"]
    say("enter texture paint; mode now", ob.mode)
    v = view3d()
    if v is None:
        say("  !! no VIEW_3D area")
        return
    win, area, region = v
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    with bpy.context.temp_override(window=win, area=area, region=region):
        bpy.ops.view3d.view_all()
        if ob.mode != "TEXTURE_PAINT":
            bpy.ops.paint.texture_paint_toggle()
    STATE["fp0"] = canvas_fingerprint()
    say("  mode:", ob.mode, "canvas:", getattr(canvas(), "name", None),
        "fp:", STATE["fp0"])


def do_stroke():
    """One projective-paint stroke in the 3D viewport."""
    ob = STATE["ob"]
    v = view3d()
    if v is None or ob is None or ob.mode != "TEXTURE_PAINT":
        say("  stroke skipped (mode=%s)" % getattr(ob, "mode", None))
        return
    win, area, region = v
    i = STATE["stroke"]
    jitter = ((i * 7919) % 200 - 100) / 100.0
    with bpy.context.temp_override(window=win, area=area, region=region):
        # The recipe is "move the camera around AND paint": a re-projection is
        # what rebuilds `ProjPaintState` against the mesh.
        bpy.ops.view3d.view_orbit(angle=0.15, type="ORBITLEFT")
        bpy.ops.paint.image_paint(
            stroke=stroke_points(ARGS["dabs"], region, jitter))
    STATE["stroke"] += 1
    if STATE["stroke"] % 10 == 0:
        say("  strokes", STATE["stroke"])


def _win_xy(region, fx, fy):
    """Window-space pixel for a fraction of the 3D region.

    `region.x`/`region.y` are already offsets within the window, and Blender's
    window origin is bottom-left like the region's, so this is an add and not
    a flip. `event_simulate` routes on these coordinates -- get them wrong by
    a screen height and the event lands in some other area's keymap, the
    stroke never starts, and the arm reads as "modal paint does not work".
    """
    return (region.x + int(region.width * fx),
            region.y + int(region.height * fy))


def _guard_open():
    """Is a modal operator open, as the addon's own guard sees it?"""
    from exmateria_map import settle_op
    return settle_op._mid_gesture()


def _modal_names():
    out = []
    for win in bpy.context.window_manager.windows:
        for op in getattr(win, "modal_operators", []) or []:
            out.append(op.bl_idname if hasattr(op, "bl_idname")
                       else type(op).__name__)
    return out


def ms_press():
    """Open a REAL modal stroke: MOUSEMOVE, then LEFTMOUSE PRESS.

    This is the thing `exec` strokes cannot do. `bpy.ops.paint.image_paint`
    called with a `stroke` list builds `ProjPaintState` and destroys it inside
    the one call, so nothing a timer does in between can dangle it. A modal
    stroke holds that state open across ticks, which is the artist's case.
    """
    v = view3d()
    if v is None:
        say("  !! no VIEW_3D for the modal stroke")
        return
    win, area, region = v
    i = STATE["stroke"]
    jitter = ((i * 7919) % 200 - 100) / 200.0
    x, y = _win_xy(region, 0.28, 0.5 + 0.16 * jitter)
    STATE["ms_y"] = y
    win.event_simulate(type="MOUSEMOVE", value="NOTHING", x=x, y=y)
    win.event_simulate(type="LEFTMOUSE", value="PRESS", x=x, y=y)
    STATE["ms_x"] = x
    say("modal press at", (x, y))


def ms_move():
    v = view3d()
    if v is None:
        return
    win, area, region = v
    STATE["ms_x"] += max(6, region.width // 40)
    win.event_simulate(type="MOUSEMOVE", value="NOTHING",
                       x=STATE["ms_x"], y=STATE["ms_y"])


def ms_hold():
    """Stand still with the stroke OPEN, long enough for the settle to land.

    This is the whole hypothesis in one step. `QUIET_DEFAULT` is 1.5s and the
    tick is 0.25s, so a 2.4s pause is the shortest hold that reliably lets
    `_tick` see a quiet canvas and reach `land_compile` -- which writes the
    mesh and calls `me.update()` while `ProjPaintState` is still holding
    pointers into the evaluated mesh it frees.
    """
    say("modal HOLD; open:", _modal_names(), "guard sees gesture:",
        _guard_open())
    return 2.4


def ms_check():
    fp = canvas_fingerprint()
    moved = fp is not None and fp != STATE["fp0"]
    STATE["painted"] = STATE["painted"] or moved
    STATE["modal_ever"] = STATE["modal_ever"] or bool(_modal_names())
    say("modal check: open", _modal_names(), "canvas moved:", moved)


def ms_release():
    v = view3d()
    if v is None:
        return
    win, area, region = v
    win.event_simulate(type="LEFTMOUSE", value="RELEASE",
                       x=STATE["ms_x"], y=STATE["ms_y"])
    STATE["stroke"] += 1
    say("modal release; strokes", STATE["stroke"])


def do_measure_paint():
    """Did the strokes land? A loop that painted nothing is not red-capable."""
    fp = canvas_fingerprint()
    painted = (fp is not None and STATE["fp0"] is not None
               and fp != STATE["fp0"])
    STATE["painted"] = STATE["painted"] or painted
    say("PAINTED:", painted, "(fp", STATE["fp0"], "->", fp, ")",
        "strokes:", STATE["stroke"])


def do_exit_paint():
    ob, v = STATE["ob"], view3d()
    if v is None or ob is None:
        return
    win, area, region = v
    with bpy.context.temp_override(window=win, area=area, region=region):
        if ob.mode == "TEXTURE_PAINT":
            bpy.ops.paint.texture_paint_toggle()
    say("exit texture paint; mode:", ob.mode)


def plan():
    if ARGS.get("replay_crash"):
        # The operator trail of the 2026-08-29 08:57:51 crash, in ITS order --
        # which is not the order this harness had been running.  The push comes
        # BEFORE the first convert, `live_isolate` appears at all, and
        # `convert_manifold` runs TWICE, the second time reporting "0
        # material(s) now show the painting".  Eleven launches of the other
        # order never reproduced, so the order is a variable, not a detail.
        steps = [wipe, do_import, do_push_replace, do_convert, do_isolate,
                 do_paint_sheet, do_convert, do_paint_sheet,
                 do_enter_paint, do_ortho]
        every = max(1, ARGS.get("push_every", 10))
        for i in range(ARGS["strokes"]):
            steps.append(do_stroke)
            if (i + 1) % 3 == 0:
                steps.append(do_ortho)         # "move the camera around"
            if (i + 1) % every == 0 and ARGS.get("live_push"):
                steps.append(do_push)
        steps += [do_measure_paint, do_exit_paint]
        return steps
    steps = [wipe, do_import, do_paint_sheet, do_convert, do_paint_sheet]
    if ARGS.get("live_push"):
        steps.append(do_push_replace)          # recipe step 4, once per cycle
    steps.append(do_enter_paint)
    every = max(1, ARGS.get("push_every", 10))
    for i in range(ARGS["strokes"]):
        if ARGS.get("modal"):
            # press -> paint -> STAND STILL while the settle lands -> keep
            # painting on the SAME open stroke -> release. The dabs after the
            # hold are the ones that read what `me.update()` freed.
            steps += [ms_press, ms_move, ms_move, ms_move, ms_check,
                      ms_hold,
                      ms_move, ms_move, ms_move, ms_move, ms_check,
                      ms_release]
        else:
            steps.append(do_stroke)
        if (i + 1) % every == 0:
            if ARGS.get("live_push"):
                steps.append(do_push)          # the TEXTURE push, mid-paint
            if ARGS.get("sheet_in_paint_mode"):
                # `Paint sheet` REMOVES the stale paint image and re-assigns
                # `space.image` in every Image Editor plus the brush canvas,
                # done here while the object is in TEXTURE_PAINT. That is the
                # ordering the image-lifetime leads name, and the OBJECT-mode
                # arm cannot reach it.
                steps.append(do_paint_sheet)
    steps += [do_measure_paint, do_exit_paint]
    return steps


STEPS = plan()


def tick():
    i = STATE["step"]
    if i >= len(STEPS):
        STATE["cycle"] += 1
        say("=== CYCLE", STATE["cycle"], "SURVIVED ===")
        if STATE["cycle"] >= ARGS["cycles"]:
            say("PAINTED_EVER:", STATE["painted"])
            say("MODAL_EVER:", STATE["modal_ever"])
            say("ALL CYCLES SURVIVED")
            LOG.close()
            bpy.ops.wm.quit_blender()
            return None
        STATE["step"] = STATE["stroke"] = STATE["pushes"] = 0
        return 0.05
    fn = STEPS[i]
    STATE["step"] = i + 1
    try:
        wait = fn()
        if isinstance(wait, (int, float)):
            return wait     # `ms_hold` asks for the settle's quiet period
    except Exception:                                           # noqa: BLE001
        say("EXCEPTION in", fn.__name__)
        say(traceback.format_exc())
        say("PAINTED_EVER:", STATE["painted"])
        say("ABORT")
        LOG.close()
        bpy.ops.wm.quit_blender()
        return None
    return 0.02


bpy.app.timers.register(tick, first_interval=1.0)
say("TIMER ARMED", len(STEPS), "steps x", ARGS["cycles"], "cycles")
'''


def one(args, n, driver):
    log = TMP / f"progress-{n}.log"
    if log.exists():
        log.unlink()
    # Isolate this Blender from the artist's OWN install. The addon is
    # SYMLINKED into `~/.config/blender/5.2/scripts/addons/`, so a launch
    # without this grades -- or overwrites -- the copy they are clicking.
    env = isolated_env(tag="paint_crash")
    env["PAINT_CRASH_ARGS"] = json.dumps({
        "log": str(log),
        "addon_pkg": str(ADDON_PKG),
        "gns": str(MAPDIR / f"MAP{args.map:03d}.GNS"),
        "arrangement": args.arrangement,
        "cycles": args.cycles,
        "strokes": args.strokes,
        "dabs": args.dabs,
        "live_push": args.live_push,
        "push_every": args.push_every,
        "sheet_in_paint_mode": args.sheet_in_paint_mode,
        "replay_crash": args.replay_crash,
        "modal": args.modal,
        "no_guard": args.no_guard,
    })
    if args.perturb:
        # Poison freed memory. A use-after-free that silently read plausible
        # bytes now reads 0xA5..., and an index built from those bytes is a
        # wild pointer that faults ON THE SPOT. A rate amplifier, not a change
        # of bug: it cannot invent a read of freed memory, only stop one from
        # looking harmless.
        env["MALLOC_PERTURB_"] = "165"
    cmd = [args.blender, "--factory-startup"]
    if args.modal:
        # Without this the RNA call raises "Not running with
        # '--enable-event-simulate' enabled". The method is present either
        # way, which is why `hasattr` is not the check -- it answers True on
        # a build that refuses every call.
        cmd.append("--enable-event-simulate")
    if args.debug_memory:
        cmd.append("--debug-memory")
    # NEVER `--background`: it removes the 3D-viewport paint path, which is
    # the whole subject. See the module docstring.
    cmd += ["--python", str(driver)]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                          timeout=args.timeout)
    text = log.read_text() if log.exists() else ""
    return proc.returncode, text, proc.stdout[-3000:], proc.stderr[-3000:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--blender", default="blender")
    ap.add_argument("--map", type=int, default=22)
    ap.add_argument("--arrangement", type=int, default=0)
    ap.add_argument("--launches", type=int, default=1)
    ap.add_argument("--cycles", type=int, default=2)
    ap.add_argument("--strokes", type=int, default=60)
    ap.add_argument("--dabs", type=int, default=12)
    ap.add_argument("--push-every", type=int, default=10)
    ap.add_argument("--timeout", type=int, default=1500)
    ap.add_argument("--live-push", action="store_true",
                    help="push to a REAL PCSX-Redux on the default port")
    ap.add_argument("--sheet-in-paint-mode", action="store_true")
    ap.add_argument("--replay-crash", action="store_true",
                    help="replay the 2026-08-29 08:57 operator trail exactly")
    ap.add_argument("--modal", action="store_true",
                    help="drive REAL modal strokes with Window.event_simulate")
    ap.add_argument("--no-guard", action="store_true",
                    help="disable the mid-gesture guard (pre-fix behaviour)")
    ap.add_argument("--expect-crash", action="store_true",
                    help="grade the SEED arm: crashing is the pass")
    ap.add_argument("--perturb", action="store_true")
    ap.add_argument("--debug-memory", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    gns = MAPDIR / f"MAP{args.map:03d}.GNS"
    if not gns.exists():
        # `project-assets/` is local-only and gitignored. Saying so beats a
        # traceback out of the importer that reads like an addon fault.
        print(f"SKIP: {gns} is absent — populate project-assets (SETUP.md)")
        return
    TMP.mkdir(exist_ok=True)
    driver = TMP / "paint_driver.py"
    driver.write_text(DRIVER)

    crashes = blind = early = 0
    for n in range(args.launches):
        rc, text, out, err = one(args, n, driver)
        tail = text.splitlines()[-5:] or ["(no log)"]
        crashed = rc in (-11, 139)
        # Three different non-survivals, kept apart on purpose. A launch that
        # stopped before its own terminal marker with rc 0 was QUIT -- headful
        # Blender exits 0 when its window is closed -- and that is a different
        # problem from strokes that deposited nothing. Measured 2026-08-29:
        # one launch ended mid-cycle after a push with rc 0, no coredump and
        # `/tmp/blender.crash.txt` untouched. Reported as one thing, it read as
        # a paint failure and nearly cost an hour on the wrong question.
        ended = ("ALL CYCLES SURVIVED" in text) or ("ABORT" in text)
        quit_early = (not crashed) and (not ended)
        painted = "PAINTED_EVER: True" in text
        crashes += crashed
        early += quit_early
        blind += (not crashed) and ended and (not painted)
        note = ("" if crashed else
                "  [ENDED EARLY — window closed?]" if quit_early else
                "" if painted else "  [PAINTED NOTHING]")
        print(f"launch {n}: "
              + ("SEGV" if crashed else "ok" if rc == 0 else f"rc={rc}")
              + note)
        for line in tail:
            print("   |", line)
        if args.verbose or (crashed and n == 0):
            print("--- stdout tail ---\n" + out)
            print("--- stderr tail ---\n" + err)

    print(f"\nCRASHES {crashes}/{args.launches}")
    if early:
        print(f"INCONCLUSIVE: {early} launch(es) exited cleanly before "
              f"finishing — headful Blender returns 0 when its window is "
              f"closed, so those launches graded nothing either way")
    if blind:
        print(f"FAIL: {blind} launch(es) deposited no paint — the loop was "
              f"not red-capable on those, so their survival says nothing")
        sys.exit(1)
    if early:
        sys.exit(1)
    # `--expect-crash` inverts the verdict so the seed arm can be GRADED
    # rather than merely observed. Without it, "the crash reproduced" and "the
    # guard is missing" both exit 1, and a pair of arms run back to back reads
    # as two failures instead of one direction test.
    if args.expect_crash:
        if crashes:
            print(f"SEEDED AS EXPECTED: {crashes}/{args.launches} crashed — "
                  "the arm this fix defends against is live")
            return
        print("NOT SEEDED: the crash arm survived, so a green guard arm "
              "proves nothing — the loop, not the fix, is what moved")
        sys.exit(1)
    if crashes:
        print("REPRODUCED: copy /tmp/blender.crash.txt now — the next crash "
              "overwrites it — and `coredumpctl info` the pid")
        sys.exit(1)
    if args.modal:
        print(f"HELD: {args.launches}/{args.launches} modal launches survived "
              "with the mid-gesture guard on")
        return
    print("survived — an exec-stroke arm, which cannot reach this bug; "
          "see the docstring")


if __name__ == "__main__":
    main()
