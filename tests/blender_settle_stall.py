"""Measure what a settle does to Blender's MAIN THREAD.

The artist's report: painting is smooth, and a second after they let go
everything freezes for ~3 s.  The settle already sends the compile and the
push to worker threads -- so the question this harness answers is whether a
worker THREAD actually frees the main thread at all.

The instrument is a heartbeat: a main-thread loop that does the cheapest
possible Python work and counts how many iterations it gets through.  That is
what Blender's main loop does -- it enters Python on every iteration
(`bpy.app.timers`) and on every panel `draw()`, of which its own UI has
hundreds.  A worker holding the GIL does not stall the main thread once; it
taxes EVERY Python entry, and the throughput ratio is the number that says how
unusable the UI is.

Run:  python3 tests/blender_settle_stall.py [blender-binary]
"""
import json
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
ADDON_DIR = PKG / "addons" / "exmateria_map"
TMP = Path(__file__).resolve().parent / ".blender_settle_stall"
REPORT = TMP / "report.json"


def stage_real_map(number=22, arrangement=0):
    sys.path.insert(0, str(PKG))
    from exmateria_map import corpus
    from exmateria_map.dump import write_bundle
    map_dir = corpus.map_dir()
    if map_dir is None:
        raise SystemExit("no map dir; populate project-assets (SETUP.md)")
    return write_bundle(map_dir, number, arrangement, TMP)


SCRIPT = r'''
import json, sys, threading, time
import bpy
sys.path.insert(0, r"@ADDONPKG@")
import exmateria_map
exmateria_map.register()

from exmateria_map import live_link as _L
_L.DEFAULT_PORT = 9                       # never reach a real emulator

out = {"python": sys.version.split()[0],
       "switchinterval": sys.getswitchinterval(), "steps": []}


def heartbeat(seconds=None, while_alive=None):
    """Main-thread Python throughput, the way Blender's main loop feels it."""
    t0 = time.monotonic()
    last, worst, n = t0, 0.0, 0
    while True:
        now = time.monotonic()
        gap = (now - last) * 1000.0
        if gap > worst:
            worst = gap
        last, n = now, n + 1
        if while_alive is not None:
            out.setdefault("_si", sys.getswitchinterval())   # FIRST sample
            if not while_alive.is_alive():
                break
        elif now - t0 >= seconds:
            break
        time.sleep(0)
    dt = time.monotonic() - t0
    return {"seconds": dt, "iterations": n, "per_sec": n / dt if dt else 0,
            "max_gap_ms": worst}


base = heartbeat(seconds=1.0)
out["baseline"] = base

# ---- the artist's session, step by step --------------------------------
bpy.ops.import_map.document(filepath=r"@JSON@")
from exmateria_map.import_document import marker_in_scene
ob = marker_in_scene(bpy.context)

t = time.monotonic()
r = bpy.ops.exmateria_map.convert_manifold()      # scale defaults to 4
out["convert"] = {"result": list(r), "seconds": time.monotonic() - t}

from exmateria_map.compile_op import (_subject_of, animated_rows_of,
                                      compile_off_thread, land_compile,
                                      read_for_compile)
found = _subject_of(ob)
if isinstance(found, str):
    out["error"] = found
else:
    ob_, state, sheet, painting, idx, rows = found
    out["painting"] = {"name": painting.name, "size": list(painting.size),
                       "channels": painting.channels}

    # A stroke: a handful of texels, the way one brush dab lands.
    px = list(painting.pixels[:4096])
    for i in range(0, 4096, 4):
        px[i] = 1.0
    painting.pixels[:4096] = px

    # ---- what the settle then does -- the REAL path, never a copy of it -
    # `_launch` is what the timer calls: it does the `bpy` read on this thread
    # and hands the rest to `worker.spawn`.  Driving it rather than
    # re-implementing it is the whole point -- a harness that spawns its own
    # thread grades a thread this addon does not use.
    from exmateria_map import settle_op, worker
    # The bound floor 2 is expressed in: the settle timer's OWN period, read
    # off the module rather than restated, so the two cannot drift.
    out["tick_seconds"] = settle_op.TICK
    t = time.monotonic()
    settle_op._launch(ob_, state, sheet, painting, idx, rows, "deadbeef")
    out["launch_main_thread_ms"] = (time.monotonic() - t) * 1000.0

    class _Alive:
        def is_alive(self):
            return not settle_op._RESULT

    t = time.monotonic()
    hb = heartbeat(while_alive=_Alive())
    out["compile_thread"] = {
        "wall_seconds": time.monotonic() - t,
        "switchinterval_while_running": out.pop("_si", None),
        "main_thread": hb,
        "throughput_ratio": (hb["per_sec"] / base["per_sec"]) if base["per_sec"] else 0,
    }

    (_n, _st, _sh, _pn, _ix, polygons, off, _dg) = settle_op._RESULT.pop(0)
    t = time.monotonic()
    land_compile(ob_, state, sheet, painting, idx, off.master, polygons,
                 off.chosen, off.compiled, off.key)
    out["land_compile_ms"] = (time.monotonic() - t) * 1000.0

    # ---- and what the PUSH's main-thread half costs ---------------------
    from exmateria_map import live_link_ui
    _L.LuaClient.check = lambda self: ""          # pretend an emulator is up
    say = live_link_ui._Say()
    t = time.monotonic()
    kw = live_link_ui.push_gather(bpy.context, ob_, say)
    out["push_gather_ms"] = (time.monotonic() - t) * 1000.0
    out["push_gather_refusals"] = say.lines[:4]

    import cProfile, pstats, io as _io
    from exmateria_map.export_document import forget_masters
    forget_masters()
    say2 = live_link_ui._Say()
    pr = cProfile.Profile(); pr.enable()
    live_link_ui.push_gather(bpy.context, ob_, say2)
    pr.disable()
    buf = _io.StringIO()
    pstats.Stats(pr, stream=buf).sort_stats("tottime").print_stats(14)
    out["push_gather_profile"] = buf.getvalue()

    # And the SAME question for the compile worker.
    from exmateria_map.compile_op import read_for_compile as _rfc
    _polys, _floats = _rfc(ob_, painting)
    pr = cProfile.Profile(); pr.enable()
    compile_off_thread(_polys, _floats, rows, animated_rows_of(ob_, state))
    pr.disable()
    buf = _io.StringIO()
    pstats.Stats(pr, stream=buf).sort_stats("tottime").print_stats(14)
    out["compile_profile"] = buf.getvalue()

json.dump(out, open(r"@OUT@", "w"), indent=1)
print("WROTE", r"@OUT@")
'''


#: The THREE structural floors (ADR-0186 Amendment 13 decision 56).  They
#: replace two that did not express the bar: `MIN_THROUGHPUT_RATIO = 0.05` was
#: beaten five times over by the very code that provoked the complaint, and
#: "push_gather < the compile" compares two quantities that shrink TOGETHER, so
#: it stayed green while both regressed.  Neither could see a hard-blocked main
#: thread at all, which is the thing that actually stops an artist painting.
#:
#: The bar is decision 15's, in the artist's own words: **the UI thread on top;
#: the emulator may lag; the number one goal is to not prevent the artist from
#: painting.**  Each floor is one clause of it:
#:
#:  1. `MIN_THROUGHPUT_RATIO` 0.05 -> **0.5** -- "UI thread on top" as a
#:     number.  The pure-Python worker measured 0.250; numpy releases the GIL
#:     and measured **0.920**.
#:  2. **No single main-thread leg may exceed `settle_op.TICK`.**  Not a
#:     wall-clock budget smuggled in: a *cadence* claim, whose bound is the
#:     settle timer's own period, read off the module.  A leg longer than TICK
#:     means the loop cannot make the rate it schedules itself at.
#:     `push_gather` at 235 ms was inside 250 ms by 15 ms -- the near miss this
#:     exists to catch.
#:  3. **The main-thread legs sum to less than the off-thread compile** -- the
#:     shape claim that a settle is mostly off-thread.  Unlike the floor it
#:     replaces it is a sum against a single leg, so it does not silently
#:     survive both halves regressing together.
#:
#: Still no millisecond budget, for the reason this file already gave: the box
#: is shared, and a threshold on a contended machine is a flake that teaches
#: nothing.
MIN_THROUGHPUT_RATIO = 0.5


def main_thread_legs(r):
    """The three legs a settle runs ON the main thread, in the order it runs
    them.  Named here rather than inline because floors 2 and 3 ask two
    different questions of the same list."""
    return [("_launch", r["launch_main_thread_ms"]),
            ("land_compile", r["land_compile_ms"]),
            ("push_gather", r["push_gather_ms"])]


def verdict(r):
    ratio = r["compile_thread"]["throughput_ratio"]
    legs = main_thread_legs(r)
    tick_ms = r["tick_seconds"] * 1000.0
    over = [f"{n} {ms:.0f} ms" for n, ms in legs if ms > tick_ms]
    total = sum(ms for _, ms in legs)
    compile_ms = r["compile_thread"]["wall_seconds"] * 1000.0
    checks = [
        ("the UI thread is on top: the compile's worker leaves the main thread "
         "most of its Python throughput",
         ratio > MIN_THROUGHPUT_RATIO, f"{ratio:.3f} > {MIN_THROUGHPUT_RATIO}"),
        (f"no single main-thread leg outlasts the settle's own tick "
         f"({tick_ms:.0f} ms)",
         not over,
         ", ".join(over) if over
         else " / ".join(f"{n} {ms:.0f} ms" for n, ms in legs)),
        ("a settle is mostly OFF the main thread: its main-thread legs sum to "
         "less than the compile",
         total < compile_ms, f"{total:.0f} ms vs {compile_ms:.0f} ms"),
    ]
    print()
    for name, ok, detail in checks:
        print(("  ok   " if ok else "  FAIL ") + name + f": {detail}")
    bad = [n for n, ok, _ in checks if not ok]
    print(f"\nSUMMARY: {len(checks) - len(bad)}/{len(checks)} checks passed")
    print("PASS" if not bad else "FAIL")
    return not bad


def main():
    # `--verdict [report.json]` re-grades a report this harness already wrote,
    # without a four-minute Blender run.  It exists for decision 56's seed
    # loop: a floor is only shown to WORK by watching it go red, and each of
    # the three is seeded by perturbing one number in a copy of the report and
    # re-grading it.  It grades a measurement; it cannot take one.
    if len(sys.argv) > 1 and sys.argv[1] == "--verdict":
        path = Path(sys.argv[2]) if len(sys.argv) > 2 else REPORT
        r = json.loads(path.read_text())
        if not verdict(r):
            raise SystemExit(1)
        return r
    blender = sys.argv[1] if len(sys.argv) > 1 else "blender"
    scale = sys.argv[2] if len(sys.argv) > 2 else "4"
    TMP.mkdir(parents=True, exist_ok=True)
    doc = stage_real_map()
    script = (SCRIPT.replace("@ADDONPKG@", str(ADDON_DIR.parent))
                    .replace("@JSON@", str(doc))
                    .replace("@OUT@", str(REPORT))
                    .replace("@SCALE@", scale))
    path = TMP / "run.py"
    path.write_text(script)
    if REPORT.exists():
        REPORT.unlink()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from blender_env import isolated_env
    p = subprocess.run([blender, "--background", "--factory-startup",
                        "--python", str(path)],
                       capture_output=True, text=True, timeout=900,
                       env=isolated_env())
    if not REPORT.exists():
        print(p.stdout[-6000:], file=sys.stderr)
        print(p.stderr[-4000:], file=sys.stderr)
        raise SystemExit("no report")
    r = json.loads(REPORT.read_text())
    ok = verdict(r)
    # The profiles print even on a FAIL.  A red floor is the state this file
    # is most useful in -- it is what the seed loop and the fix are both run
    # in -- and raising before the profile is what makes it useless there.
    for k in ("push_gather_profile", "compile_profile"):
        if k in r:
            print("=" * 20, k); print(r.pop(k))
    print(json.dumps(r, indent=1))
    if not ok:
        raise SystemExit(1)
    return r


if __name__ == "__main__":
    main()
