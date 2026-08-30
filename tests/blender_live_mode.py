"""Grade the ALL-ON / ALL-OFF live mode — ADR-0186 Amendment 16.

The brief: *"i know there might be individual toggles but i want an all on or
all off next to where the auto push button is."*

`live_mode` is an enum on `MAP_AddonPreferences` that **stores nothing**. It
reads `settle_on`, `auto_push` and `live_camera_sync` and it writes all three,
which is why it is not the third switch ADR-0186 Amendment 14 decision 62
refuses: there is no state it can hold that disagrees with them.

**Both arms, on every claim that has two.** A mode wired to nothing passes every
negative check ever written — "no push happened" is true when nothing in the
process can push at all — so each `Manual` arm below is paired with the
`Automatic` control that would go red if the rig, the fixture or the settle were
broken rather than the mode. That pairing is the whole design of this file.

**Real preferences, not the defaults.** Unlike `blender_lighting_push.py`, which
imports and registers the package so `_prefs` returns `None`, this harness
INSTALLS and ENABLES the addon: `live_mode` lives on `AddonPreferences` and
there is nothing to grade without one. `live_port` is then a real preference
too, so it is pinned to **9** (`discard`) by hand — redirecting
`live_link.DEFAULT_PORT` alone would not do it here, because with real
preferences the code reads the preference and never the module default. The box
may have a real emulator mid-battle on it.

**Headful, never `--background`.** `_camera_sync_timer`'s first act is to
unregister itself under `bpy.app.background` — measured, and deliberately so: a
background Blender still has a `VIEW_3D` region and would POST a pose nobody
orbited. In `--background` both camera arms below would therefore pass on a
mode that is wired to nothing, which is exactly the vacuous negative this file
exists to avoid.

The compile witness is `crumbs.py`'s trail rather than an assertion about a
mesh: `land.begin` is the line the settle drops immediately before
`land_compile` runs `_write_binding` and `me.update()`, and a mesh write inside
an open stroke is the SIGSEGV in `docs/paint-crash-diagnosis.md`. So "manual
mode is quiet" is graded as a trail with no `land.begin` in it, next to an
automatic trail that has one — which is what makes it a measurement instead of
a claim.

Run:  python3 tests/blender_live_mode.py [blender-binary]
"""
import json
import subprocess
import sys
import zipfile
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
ADDON_DIR = PKG / "addons" / "exmateria_map"
FIXTURES = PKG / "tests" / "fixtures"
FIXTURE = FIXTURES / "MAP001.a0.stub.json"
TMP = Path(__file__).resolve().parent / ".blender_live_mode"
REPORT = TMP / "report.json"

#: A run that stops early has caught nothing -- `live_normals_audit.py` printed
#: PASS directly under "the audit itself broke". Every arm is counted.
EXPECTED_CHECKS = 16

#: The three controls whose CONTROL arm must be green for their negative twin
#: to say anything at all. Named here so `main` can say so in the report rather
#: than leaving a reader to notice.
CONTROLS = (
    "CONTROL: Automatic -- a paint stroke reaches a push",
    "CONTROL: Automatic -- and the trail carries a land.begin",
    "CONTROL: Automatic -- the camera ticker DOES sync",
)


def stage_stub():
    """The fixture, plus the sheets its states name, in this suite's own dir."""
    TMP.mkdir(parents=True, exist_ok=True)
    staged = TMP / FIXTURE.name
    staged.write_text(FIXTURE.read_text())
    for st in json.loads(FIXTURE.read_text())["map_states"]:
        name = st.get("texture_sheet")
        if name:
            (TMP / name).write_bytes((FIXTURES / name).read_bytes())
    return staged


def ensure_addon():
    """Zip the tree so `addon_install` installs THIS addon, not the artist's."""
    TMP.mkdir(parents=True, exist_ok=True)
    zf_path = TMP / "exmateria_map.zip"
    with zipfile.ZipFile(zf_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(ADDON_DIR.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                zf.write(f, f.relative_to(ADDON_DIR.parent))
    return zf_path


SCRIPT = r'''
import json, os, sys, time, traceback, types
import bpy

ZIP = r"@ZIP@"
PKG = r"@ADDONPKG@"
OUT = r"@OUT@"
DOC = r"@JSON@"

checks, notes = {}, []


def check(n, cond, detail=""):
    checks[n] = bool(cond)
    print(("  ok   " if cond else "  FAIL ") + n + (f": {detail}" if detail else ""))


def write_report(fatal=None):
    with open(OUT, "w") as f:
        json.dump({"checks": checks, "notes": notes, "fatal": fatal}, f, indent=1)


try:
    try:
        bpy.ops.preferences.addon_install(filepath=ZIP)
    except Exception as e:
        print(f"INSTALL: {e}")
    bpy.ops.preferences.addon_enable(module='exmateria_map')
    # AFTER the enable, never before: `addon_install` + a plain `import` in one
    # process yields TWO module objects, and the arms would then patch one copy
    # while the operators call the other. `sys.modules` already holds the
    # enabled one, so this only makes the name importable.
    sys.path.insert(0, PKG)

    from exmateria_map import (crumbs as CR, live_link as L, live_link_ui as UI,
                               settle_op as S)
    from exmateria_map.compile_op import _subject_of
    from exmateria_map.import_document import (LIVE_MODE_SWITCHES,
                                               marker_in_scene)
    from exmateria_map.settle_clock import QUIET_DEFAULT

    prefs = bpy.context.preferences.addons['exmateria_map'].preferences
    if prefs is None:
        raise SystemExit("the addon enabled with no preferences to grade")
    # The box may have a real emulator mid-battle on it. With REAL preferences
    # the transport reads `prefs.live_port`, so redirecting `L.DEFAULT_PORT` --
    # which is what the `_prefs is None` harnesses do -- would not be enough.
    L.DEFAULT_PORT = 9
    prefs.live_port = 9
    notes.append(f"host/port under test: {prefs.live_host}:{prefs.live_port}")

    def switches():
        return {n: bool(getattr(prefs, n)) for n in LIVE_MODE_SWITCHES}

    # ---- 1-5: the mode against the three it sets -------------------------
    # Pure property arms, run FIRST: everything below reads the mode to arm
    # itself, so a mode that does not set what it says it sets would make every
    # later arm a statement about the wrong scene.
    check("1. every switch the mode names is a real preference",
          all(prefs.bl_rna.properties.get(n) is not None
              for n in LIVE_MODE_SWITCHES),
          str(LIVE_MODE_SWITCHES))

    prefs.live_mode = "AUTOMATIC"
    check("2. Automatic sets ALL THREE on, and reads back Automatic",
          all(switches().values()) and prefs.live_mode == "AUTOMATIC",
          f"{switches()} -> {prefs.live_mode}")

    prefs.live_mode = "MANUAL"
    check("3. Manual sets ALL THREE off, and reads back Manual",
          not any(switches().values()) and prefs.live_mode == "MANUAL",
          f"{switches()} -> {prefs.live_mode}")

    # The individual toggles still exist and are still independent -- the mode
    # is one control OVER them, not a replacement for them. A half-on state is
    # therefore reachable, and the mode has to say so rather than read it as
    # one of its two ends.
    prefs.auto_push = True
    check("4. one switch back on reads MIXED, not one of the two ends",
          prefs.live_mode == "MIXED", f"{switches()} -> {prefs.live_mode}")

    before = switches()
    # `MIXED` is only in the enum's items WHILE the reading is mixed, so
    # assigning it can also fail with a `TypeError` -- which is the same claim
    # reached by a shorter road, and is caught rather than fatal so a red arm
    # 4 above cannot take the rest of the run down with it.
    try:
        prefs.live_mode = "MIXED"
        how = "assigned"
    except TypeError:
        how = "not even offered as an item"
    check("5. MIXED is a reading and not a destination -- setting it moves "
          "nothing",
          switches() == before, f"{how}: {before} -> {switches()}")

    # ---- the scene, and the recorder at decision 58's seam ----------------
    prefs.live_mode = "AUTOMATIC"
    bpy.ops.import_map.document(filepath=DOC)
    ob = marker_in_scene(bpy.context)
    notes.append(f"convert: {list(bpy.ops.exmateria_map.convert_manifold(scale='1'))}")
    found = _subject_of(ob)
    if isinstance(found, str):
        raise SystemExit(f"the fixture has no subject to paint: {found}")
    _ob, _state, _sheet, painting, _idx, _rows = found

    PUSHES = []
    _real_push = S.push_after_compile

    def _record(o, why):
        PUSHES.append(why)
        return _real_push(o, why)

    S.push_after_compile = _record

    QUIET = QUIET_DEFAULT
    #: How long a NEGATIVE arm watches for something that must not happen.
    #: Four quiet periods: a settle wired to the same clock would have fired
    #: three times over, so this is not a race the arm wins by being lucky.
    SILENCE = QUIET * 4.0
    PATIENCE = QUIET * 8.0 + 30.0

    def ticks(seconds, step=0.05):
        """Run the REAL timer callback -- `_tick`, not a copy of it, and not
        `_step`: `_tick` is what `bpy.app.timers` calls, it is where the
        gesture guard and `_drain_push` live, and it is what drops crumbs."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            S._tick()
            time.sleep(step)

    def ticks_until(pred, timeout, step=0.05):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            S._tick()
            if pred():
                return True
            time.sleep(step)
        return bool(pred())

    def arm(mode, settle_first=None):
        """Let whatever has already moved settle, then forget that it did."""
        prefs.live_mode = "AUTOMATIC"      # so the settle-out actually settles
        ticks(QUIET * 3.0 if settle_first is None else settle_first)
        prefs.live_mode = mode
        PUSHES.clear()
        S._PUSH["quiet_until"] = 0.0
        S._PUSH["said"] = None
        return crumb_mark()

    def crumb_mark():
        p = CR.path()
        return os.path.getsize(p) if p and os.path.exists(p) else 0

    def crumb_tail(mark):
        p = CR.path()
        if not p or not os.path.exists(p):
            return ""
        with open(p) as f:
            f.seek(mark)
            return f.read()

    def paint_a_stroke():
        px = list(painting.pixels[:4096])
        for i in range(0, 4096, 4):
            px[i] = 1.0 - px[i]
        painting.pixels[:4096] = px

    # ---- CONTROL then ARM: the settle ------------------------------------
    mark = arm("AUTOMATIC")
    paint_a_stroke()
    saw = ticks_until(lambda: PUSHES, PATIENCE)
    trail = crumb_tail(mark)
    check("CONTROL: Automatic -- a paint stroke reaches a push",
          saw and PUSHES[:1] == ["settle"], f"pushes={PUSHES}")
    check("CONTROL: Automatic -- and the trail carries a land.begin",
          "land.begin" in trail,
          f"{trail.count('land.begin')} land.begin in {len(trail)} bytes")
    notes.append(f"automatic trail: {trail.count(chr(10))} crumbs, "
                 f"{trail.count('land.begin')} land.begin, "
                 f"{trail.count('mesh.update')} mesh.update")

    mark = arm("MANUAL")
    paint_a_stroke()
    ticks(SILENCE)
    trail = crumb_tail(mark)
    # Named for the SEAM the recorder sits on: it wraps `push_after_compile`
    # and records the DECISION to push, before `auto_push` and the back-off
    # get their say. So this arm is about the settle having done nothing at
    # all -- which is the stronger claim, and the one arm 7 then completes.
    check("6. Manual -- the SAME stroke never reaches push_after_compile",
          not PUSHES, f"pushes={PUSHES}")
    # The half a transport gate would not buy. `settle_on` off stops `_step`
    # before `land_compile`, so there is no `me.update()` on the artist's mesh
    # -- which is the whole crash surface of `docs/paint-crash-diagnosis.md`.
    check("7. Manual -- and no COMPILE: the trail has no land.begin",
          "land.begin" not in trail and "mesh.update" not in trail,
          f"{trail.count('land.begin')} land.begin, "
          f"{trail.count('mesh.update')} mesh.update in {len(trail)} bytes")
    notes.append(f"manual trail: {trail.count(chr(10))} crumbs "
                 f"({trail.count('tick')} ticks, so the timer DID run)")

    # ---- the camera ticker, both ways ------------------------------------
    SYNCS = []
    UI.sync_camera_background = lambda *a, **k: SYNCS.append(1) or []

    prefs.live_mode = "MANUAL"
    UI._camera_sync_timer()
    UI._camera_sync_timer()
    check("8. Manual -- the camera ticker syncs nothing", not SYNCS,
          f"syncs={len(SYNCS)}")

    SYNCS.clear()
    prefs.live_mode = "AUTOMATIC"
    UI._camera_sync_timer()
    check("CONTROL: Automatic -- the camera ticker DOES sync", len(SYNCS) == 1,
          f"syncs={len(SYNCS)}")

    # ---- the button, in Manual -------------------------------------------
    # The point of the mode: Manual stops the TIMERS, never the buttons. If a
    # later change gates `MAP_OT_live_push` on `auto_push` -- which reads like
    # a tidy-up -- Manual becomes a mode in which nothing can be pushed at all,
    # and this is the arm that says so.
    PRESSES = []
    _real_now = UI.push_now

    def _record_now(*a, **k):
        PRESSES.append(1)
        return _real_now(*a, **k)

    UI.push_now = _record_now
    prefs.live_mode = "MANUAL"
    # `bpy.ops` RAISES whatever an operator reports as an ERROR, so a push with
    # no emulator to reach comes back as a `RuntimeError` rather than a status.
    # Caught here and read, not let past: a harness that died on the discard
    # port would report the MODE as broken, and the message is itself the
    # evidence for the arm below.
    try:
        status, refusal = list(bpy.ops.map.live_push()), ""
    except RuntimeError as e:
        status, refusal = ["CANCELLED"], str(e)
    check("9. Manual -- Push to PCSX still delivers (the mode gates timers, "
          "not buttons)",
          len(PRESSES) == 1, f"presses={len(PRESSES)}, operator={status}")
    # ...and it got as far as the TRANSPORT rather than being turned back
    # early: the only thing that stopped this push is that nothing is
    # listening on the discard port, which is the emulator's answer and not
    # the mode's. Without this arm, "the button was called" would still be
    # green if the call returned at its first line.
    check("10. Manual -- and the push's refusal is the EMULATOR's, not the "
          "mode's",
          "no emulator answering" in refusal,
          refusal.splitlines()[0] if refusal else "no refusal at all")

    # ---- it is actually DRAWN, and where ---------------------------------
    # Everything above grades the mode as a PROPERTY. A property nothing draws
    # is #421's shape -- a control the artist cannot reach passes every arm
    # about what it does when set. `draw` is a plain Python function, so it is
    # called with a `self` of our own and a layout that RECORDS rather than
    # renders; no viewport, no sidebar tab, no open preferences window needed.
    class FakeLayout:
        """Records what a `draw` emits.

        `prop` validates the name against RNA the way Blender's own does, so a
        typo is a red arm here instead of a panel that breaks the first time an
        artist opens the tab -- which is the one defect a recording layout
        would otherwise launder into a pass.
        """

        def __init__(self, sink):
            self.sink = sink

        def prop(self, owner, name, **kw):
            if owner.bl_rna.properties.get(name) is None:
                raise AttributeError(f"no property {name!r} to draw")
            self.sink.append(("prop", name))

        def operator(self, idname, **kw):
            self.sink.append(("operator", idname))
            return types.SimpleNamespace()

        def label(self, **kw):
            self.sink.append(("label", kw.get("text", "")))

        def box(self):
            return self

        def row(self, **kw):
            return self

        def column(self, **kw):
            return self

        def __getattr__(self, k):
            # A `draw` may reach for anything a real `UILayout` offers. An
            # unfamiliar call must not be the reason an arm goes red -- only a
            # real defect may do that.
            return lambda *a, **kw: None

    class DrawShim:
        """A `self` for a `draw`, carrying a recording layout.

        Everything but `layout` falls through to the real object, so a
        `self.prop(self, ...)` in `AddonPreferences.draw` is still validated
        against real RNA rather than against a stub that would accept anything.
        """

        def __init__(self, real, sink):
            object.__setattr__(self, "_real", real)
            object.__setattr__(self, "layout", FakeLayout(sink))

        def __getattr__(self, k):
            return getattr(object.__getattribute__(self, "_real"), k)

    from exmateria_map.import_document import MAP_AddonPreferences
    from exmateria_map.live_link_ui import MAP_PT_live_push

    panel = []
    MAP_PT_live_push.draw(DrawShim(prefs, panel), bpy.context)
    notes.append(f"push panel emits: {panel}")
    _pushed = [i for i, e in enumerate(panel) if e == ("operator", "map.live_push")]
    _mode = [i for i, e in enumerate(panel) if e == ("prop", "live_mode")]
    check("11. the push panel DRAWS the mode, next to the push button",
          len(_mode) == 1 and _pushed and _mode[0] > _pushed[0],
          f"push at {_pushed}, mode at {_mode}")
    # ...and ONLY the mode. Drawing the three here as well is decision 62's
    # complaint arriving by the other road -- four controls over one question.
    check("12. ...and the panel does NOT also draw the three it sets",
          not [e for e in panel if e[0] == "prop" and e[1] in LIVE_MODE_SWITCHES],
          str([e for e in panel if e[0] == "prop"]))

    pref_ui = []
    MAP_AddonPreferences.draw(DrawShim(prefs, pref_ui), bpy.context)
    drawn = {n for k, n in pref_ui if k == "prop"}
    notes.append(f"preferences emit {len(pref_ui)} items, "
                 f"{len(drawn)} distinct props")
    _want = {"live_mode", "settle_quiet", *LIVE_MODE_SWITCHES}
    check("13. the preferences draw the mode AND every switch it sets",
          drawn.issuperset(_want), f"missing={sorted(_want - drawn)}")

    write_report()
except SystemExit as e:
    traceback.print_exc()
    write_report(fatal=str(e))
except Exception as e:                                        # noqa: BLE001
    traceback.print_exc()
    write_report(fatal=f"{type(e).__name__}: {e}")
print("WROTE", OUT)
sys.stdout.flush()
sys.stderr.flush()
# Headful, so nothing quits on its own -- and `bpy.ops.wm.quit_blender()` is
# not enough. MEASURED: on a run that aborted early it left the window standing
# with the addon's own timers still ticking at 4 Hz for ten minutes, until the
# launcher's timeout, and the run read as a hang rather than as the four checks
# it had already written. `os._exit` cannot be queued, prompted or joined
# behind a worker thread. The report is already on disk and both streams are
# flushed above, so there is nothing left for a graceful shutdown to save.
os._exit(0)
'''


def main():
    blender = sys.argv[1] if len(sys.argv) > 1 else "blender"
    doc = stage_stub()
    zf = ensure_addon()
    script = TMP / "run.py"
    script.write_text(SCRIPT.replace("@ADDONPKG@", str(ADDON_DIR.parent))
                            .replace("@ZIP@", str(zf))
                            .replace("@JSON@", str(doc))
                            .replace("@OUT@", str(REPORT)))
    if REPORT.exists():
        REPORT.unlink()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from blender_env import isolated_env
    # NEVER `--background`: `_camera_sync_timer` unregisters itself there, so
    # both camera arms would pass on a mode wired to nothing. And the addon is
    # SYMLINKED into the artist's Blender, so `isolated_env` is what keeps
    # `addon_install` off the copy they are clicking.
    p = subprocess.run([blender, "--factory-startup",
                        "--disable-crash-handler", "-noaudio",
                        "--python", str(script)],
                       capture_output=True, text=True, timeout=1800,
                       env=isolated_env())
    if not REPORT.exists():
        sys.stdout.write(p.stdout[-6000:])
        sys.stdout.write("\n[stderr]\n" + p.stderr[-4000:])
        print("\nFAIL: no report written")
        return 1
    rep = json.loads(REPORT.read_text())
    print(p.stdout[-4000:])
    for n in rep["notes"]:
        print(f"  note: {n}")
    print()
    for k, v in rep["checks"].items():
        print(("  ok   " if v else "  FAIL ") + k)
    bad = [k for k, v in rep["checks"].items() if not v]
    if rep.get("fatal"):
        print(f"\nFATAL: {rep['fatal']}")
    short = len(rep["checks"]) < EXPECTED_CHECKS
    if short:
        print(f"\nFAIL: {len(rep['checks'])} checks ran, "
              f"{EXPECTED_CHECKS} expected -- the run stopped early")
    dead = [c for c in CONTROLS if not rep["checks"].get(c, False)]
    if dead:
        print("\n  NOTE: a CONTROL is red, so its Manual twin says nothing --"
              "\n  \"it did not happen\" is true when it cannot happen at all:")
        for c in dead:
            print(f"    - {c}")
    print(f"\nSUMMARY: {len(rep['checks']) - len(bad)}/{len(rep['checks'])} "
          f"checks passed")
    ok = not bad and not short and not rep.get("fatal")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
