"""Grade the lighting AUTO-PUSH -- ADR-0186 Amendment 14, decisions 58-63.

The brief: *"right now it will auto push for texture painting -- can we do the
same for changing the lighting? I really just want to keep pcsx redux updated
with whatever the current config is."*

The amendment's finding is that **every lighting byte an artist can move
already pushes** -- 39 bytes of rig plus the mesh `normals` -- and that the
whole of the missing work is a **trigger**.  `settle_op._step()` watches one
thing, `canvas_digest(painting)`, and a lamp move, a rig **Override** move and
a state switch each change nothing it looks at.

So this harness asks one question, four times: **did a lighting change reach a
push?**  That is a different instrument from `blender_settle_witness.py`, which
grades `canvas_digest` as a *function* -- that it is stable, that it moves on
one texel, that it is cheap against blake2b.  Those are property arms over a
pure function and they are left alone; these are end to end, and the end is
`settle_op.push_after_compile`.

**Where the arms observe, and why there.**  At `push_after_compile`, wrapped so
the real one still runs -- decision 58's seam, the one a lighting change is
routed through rather than given a transport of its own.  The wrapper asserts
the CALL.  A check on a witness's return value instead would be the `#421`
shape: a witness wired to a clock that never fires returns a perfectly good
digest forever, and every check that only looks at what the witness *said*
reads green while nothing reaches the emulator.

**Arm 4 is the point.**  Arms 1-3 fail as no-ops -- a missing wire and they are
red.  Arm 4 is the one that fails silently in the *other* direction: witness A
armed with **Lamp authority** off would push on every lamp nudge that changes
nothing, which decision 60 rules out, and no output check would notice.  Arm 4
is only meaningful once arms 1-3 are green, so the summary says so.

**No emulator is touched.**  `live_link.DEFAULT_PORT` is redirected to 9
(`discard`), so the push refuses at the emulator check on this thread and never
reaches a worker or the artist's PCSX-Redux.  What is graded is the trigger;
that the transport works is `blender_live_push.py`'s job and the amendment's
whole point is that it already does.

**The quiet period is the shipped default.**  The addon is imported and
registered rather than installed, the way `blender_settle_stall.py` does it, so
`import_document._prefs` returns `None` and every preference reads its default
-- which is what the artist has.  `QUIET_DEFAULT` is read off `settle_clock`
rather than restated, so the two cannot drift.

Run:  python3 tests/blender_lighting_push.py [blender-binary]
"""
import json
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
ADDON_DIR = PKG / "addons" / "exmateria_map"
FIXTURES = PKG / "tests" / "fixtures"
FIXTURE = FIXTURES / "MAP001.a0.stub.json"
TMP = Path(__file__).resolve().parent / ".blender_lighting_push"
REPORT = TMP / "report.json"

#: A run that stops early has caught nothing -- `live_normals_audit.py` printed
#: PASS directly under "the audit itself broke", and this is the guard against
#: repeating it.  Every arm below is counted, positive and negative alike.
EXPECTED_CHECKS = 10


def stage_stub():
    """The three-state stub, and its states are exactly the case under test.

    State **1** owns the only texture sheet; states 0 and 2 have none, so
    `paint.sheet_of_state` hands them state 1's.  Switching between them is
    therefore a state switch that **cannot** move `canvas_digest` -- the
    amendment's borrowing case, without having to construct one.
    """
    TMP.mkdir(parents=True, exist_ok=True)
    staged = TMP / FIXTURE.name
    staged.write_text(FIXTURE.read_text())
    for st in json.loads(FIXTURE.read_text())["map_states"]:
        name = st.get("texture_sheet")
        if name:
            (TMP / name).write_bytes((FIXTURES / name).read_bytes())
    return staged


SCRIPT = r'''
import json, sys, time, traceback
import bpy

sys.path.insert(0, r"@ADDONPKG@")
import exmateria_map
exmateria_map.register()

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
    from exmateria_map import (lighting_bake as LB, live_link as L,
                               settle_op as S)
    from exmateria_map.compile_op import _subject_of
    from exmateria_map.import_document import (find_override, marker_in_scene,
                                               object_states)
    from exmateria_map.settle_clock import QUIET_DEFAULT

    # Port 9 is `discard`: the push refuses at the emulator check, on this
    # thread, and the artist's PCSX-Redux is never reached.
    L.DEFAULT_PORT = 9

    # ---- the instrument, at decision 58's seam -------------------------
    # It CALLS THROUGH.  The real `push_after_compile` still runs, so the
    # `auto_push` gate and the back-off are exercised rather than mocked away,
    # and what is recorded is the decision to push -- which is the whole of
    # what this amendment adds.
    PUSHES = []
    _real_push = S.push_after_compile

    def _record(ob, why):
        PUSHES.append(why)
        return _real_push(ob, why)

    S.push_after_compile = _record

    bpy.ops.import_map.document(filepath=DOC)
    ob = marker_in_scene(bpy.context)
    notes.append(f"states: {len(object_states(ob))}")

    QUIET = QUIET_DEFAULT
    #: How long a NEGATIVE arm watches for a push that must not come.  Four
    #: quiet periods: a witness wired to the same clock would have fired three
    #: times over, so this is not a race the arm can win by being lucky.
    SILENCE = QUIET * 4.0
    #: How long a POSITIVE arm waits.  It returns the moment a push lands, so
    #: this is only the give-up point.
    PATIENCE = QUIET * 8.0

    def ticks(seconds, step=0.05):
        """Run the REAL `_step()` at a tick cadence -- never a copy of it."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            S._step()
            time.sleep(step)

    def ticks_until(pred, timeout, step=0.05):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            S._step()
            if pred():
                return True
            time.sleep(step)
        return bool(pred())

    def arm(settle_first=QUIET * 3.0):
        """Let whatever has already moved settle, then forget that it did."""
        ticks(settle_first)
        PUSHES.clear()
        S._PUSH["quiet_until"] = 0.0
        S._PUSH["said"] = None

    # ---- ARM 0: the first sight of a map is not a change ----------------
    # A fresh `SettleClock` treats its FIRST observation as a change, so a
    # lighting clock that is not primed from what the scene already holds
    # fires one quiet period after the map comes into view -- a push nobody
    # asked for, on every import and every file reload.  Nothing has been
    # touched here, so nothing may be sent.
    ticks(SILENCE)
    check("0. a map that has just been imported pushes NOTHING on its own",
          not PUSHES, f"pushes={PUSHES}")
    PUSHES.clear()

    # A painting, so the CANVAS path has something to settle on.  Without one
    # `_subject_of` refuses and the control arm below could not tell a wired
    # lighting witness from a dead recorder.
    notes.append(f"convert: {list(bpy.ops.exmateria_map.convert_manifold(scale='1'))}")
    found = _subject_of(ob)
    if isinstance(found, str):
        raise SystemExit(f"the fixture has no subject to paint: {found}")
    _ob, _state, _sheet, painting, _idx, _rows = found

    def paint_a_stroke():
        px = list(painting.pixels[:4096])
        for i in range(0, 4096, 4):
            px[i] = 1.0 - px[i]
        painting.pixels[:4096] = px

    # ---- CONTROL: the recorder can see a push at all -------------------
    # Without this, arm 4 passing means nothing: a broken wrapper, a disabled
    # settle or a fixture with no subject all make "no push" true.
    arm()
    paint_a_stroke()
    _saw = ticks_until(lambda: PUSHES, PATIENCE + 30.0)
    check("CONTROL: a paint settle still reaches a push (the recorder works)",
          _saw and PUSHES[:1] == ["settle"], f"pushes={PUSHES}")

    # ---- lamps, and the authority that arms witness A ------------------
    notes.append(f"seed_rig_lamps: {list(bpy.ops.map.seed_rig_lamps())}")
    lamps = sorted(LB.scene_lamps(bpy.context.scene, ob), key=lambda o: o.name)
    notes.append(f"lamps: {[l.name for l in lamps]}")
    if not lamps:
        raise SystemExit("no lamps were seeded; witness A has nothing to watch")
    lamp = lamps[0]

    def nudge_the_lamp():
        lamp.rotation_euler[0] += 0.4
        bpy.context.view_layer.update()

    # ---- ARM 1 ---------------------------------------------------------
    ob.exmateria_map_lamp_authority = True
    bpy.context.view_layer.update()
    arm()
    _sig_before = LB.lamp_signature(bpy.context.scene, ob)
    nudge_the_lamp()
    notes.append("arm1 lamp_signature moved: "
                 f"{LB.lamp_signature(bpy.context.scene, ob) != _sig_before}")
    _saw = ticks_until(lambda: PUSHES, PATIENCE)
    check("1. a lamp move under Lamp authority reaches a push",
          _saw and PUSHES == ["lighting"], f"pushes={PUSHES}")

    # ---- ARM 4 (before arm 2 -- it needs authority OFF, and turning it off
    #      is itself a change that has to be let settle) -----------------
    ob.exmateria_map_lamp_authority = False
    bpy.context.view_layer.update()
    arm()
    nudge_the_lamp()
    ticks(SILENCE)
    check("4. a lamp move with Lamp authority OFF reaches NO push "
          "(decision 60)", not PUSHES, f"pushes={PUSHES}")

    # ---- ARM 2 -- still with authority OFF, which is decision 60's scope
    _state_i = int(ob.get("exmateria_map/preview_state", 0))
    ov = find_override(ob, _state_i)
    if ov is None:
        raise SystemExit(f"no Override on state {_state_i}; witness B has "
                         f"nothing to watch")
    arm()
    ov.ambient = (0.9, 0.1, 0.15)
    _saw = ticks_until(lambda: PUSHES, PATIENCE)
    check("2. a rig Override move reaches a push, with Lamp authority OFF",
          _saw and PUSHES == ["lighting"], f"pushes={PUSHES}")

    # ---- ARM 3 ---------------------------------------------------------
    _states = object_states(ob)
    _other = next(i for i in range(len(_states)) if i != _state_i)
    arm()
    _canvas_before = S.canvas_digest(painting)
    # `set_preview_state.poll` reads `context.object`, and seeding the lamps
    # left one of them active.
    bpy.context.view_layer.objects.active = ob
    notes.append(f"arm3: preview {_state_i} -> {_other}, "
                 f"{list(bpy.ops.exmateria_map.set_preview_state(state_index=_other))}")
    _borrowed = S.canvas_digest(painting) == _canvas_before
    check("3a. the state switch is a BORROWING one: the painting did not move",
          _borrowed, f"digest {_canvas_before} -> {S.canvas_digest(painting)}")
    _saw = ticks_until(lambda: PUSHES, PATIENCE)
    check("3. previewing a borrowing state reaches a push",
          _saw and bool(PUSHES), f"pushes={PUSHES}")
    check("3b. ...and it pushes rather than COMPILES (decision 61)",
          PUSHES == ["lighting"], f"pushes={PUSHES}")

    # ---- ARM 5: one gesture, ONE push -----------------------------------
    # Painting and moving a light in one gesture must not push twice: the
    # compile's own push already carries the rig.  Not in decision 63's four,
    # and it is the case the ADR's three-way branch is easiest to get wrong in
    # -- silently, and in the direction of doing MORE work per stroke.
    ob.exmateria_map_lamp_authority = True
    bpy.context.view_layer.update()
    arm()
    paint_a_stroke()
    nudge_the_lamp()
    _saw = ticks_until(lambda: PUSHES, PATIENCE + 30.0)
    ticks(SILENCE)
    check("5. painting and moving a light in ONE gesture produce ONE push",
          _saw and len(PUSHES) == 1, f"pushes={PUSHES}")

    # ---- ARM 6: what the new witness costs, every tick ------------------
    # `lighting_digest` is read on EVERY `_step()`, on the main thread, beside
    # `canvas_digest` -- and Amendment 10 decision 42 exists because exactly
    # that kind of per-tick witness grew to 34% of one core without anyone
    # noticing.  Adding a second one unmeasured would be repeating it.
    #
    # Asserted RELATIVE to the witness already paid on the same tick, the way
    # `blender_settle_witness.py` asserts against blake2b rather than against
    # a millisecond figure: the box is shared, and a threshold on a contended
    # machine is a flake that teaches nothing.  The claim is the modest one
    # that matters -- the amendment did not DOUBLE the tick.
    def best(fn, repeats=20):
        t = None
        for _ in range(repeats):
            s0 = time.perf_counter()
            fn()
            e = time.perf_counter() - s0
            t = e if t is None else min(t, e)
        return t * 1000.0

    _ms_light = best(lambda: S.lighting_digest(bpy.context.scene, ob))
    _ms_canvas = best(lambda: S.canvas_digest(painting))
    notes.append(f"per tick: lighting_digest {_ms_light:.3f} ms, "
                 f"canvas_digest {_ms_canvas:.3f} ms "
                 f"({len(object_states(ob))} states, {len(lamps)} lamps)")
    check("6. the lighting witness costs less per tick than the canvas one "
          "it rides beside",
          _ms_light < _ms_canvas,
          f"{_ms_light:.3f} ms vs {_ms_canvas:.3f} ms")

    write_report()
except SystemExit as e:
    traceback.print_exc()
    write_report(fatal=str(e))
except Exception as e:                                        # noqa: BLE001
    traceback.print_exc()
    write_report(fatal=f"{type(e).__name__}: {e}")
print("WROTE", OUT)
'''


def main():
    blender = sys.argv[1] if len(sys.argv) > 1 else "blender"
    doc = stage_stub()
    script = TMP / "run.py"
    script.write_text(SCRIPT.replace("@ADDONPKG@", str(ADDON_DIR.parent))
                            .replace("@JSON@", str(doc))
                            .replace("@OUT@", str(REPORT)))
    if REPORT.exists():
        REPORT.unlink()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from blender_env import isolated_env
    p = subprocess.run([blender, "--background", "--factory-startup",
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
    bad = [k for k, v in rep["checks"].items() if not v]
    print()
    for k, v in rep["checks"].items():
        print(("  ok   " if v else "  FAIL ") + k)
    if rep.get("fatal"):
        print(f"\nFATAL: {rep['fatal']}")
    short = len(rep["checks"]) < EXPECTED_CHECKS
    if short:
        print(f"\nFAIL: {len(rep['checks'])} checks ran, "
              f"{EXPECTED_CHECKS} expected -- the run stopped early")
    if not rep["checks"].get("CONTROL: a paint settle still reaches a push "
                             "(the recorder works)", False):
        print("\n  NOTE: the CONTROL is red, so arm 4 says nothing -- "
              "\"no push\" is true when nothing can push at all.")
    print(f"\nSUMMARY: {len(rep['checks']) - len(bad)}/{len(rep['checks'])} "
          f"checks passed")
    ok = not bad and not short and not rep.get("fatal")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
