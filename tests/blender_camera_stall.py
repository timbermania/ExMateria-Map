"""Measure what the CONTINUOUS CAMERA SYNC does to Blender's MAIN THREAD.

The artist's report: *"blender is laggy when panning and moving the camera and
auto sync is on for the camera (which is default - I think) can we do a similar
kind of async-thing for camera movement - or is that different?"*

**It is different, and that is the whole finding.**  The compile's freeze was
CPU-bound Python holding the GIL, where a worker thread made things WORSE
(`worker.py`'s table: 586 fps -> 8.7) and only numpy fixed it.  This one is
blocking socket I/O, which RELEASES the GIL -- so here a worker is exactly the
treatment, and the two answers are opposite for a reason.

**What actually costs, measured against the running emulator on 2026-08-29.**
Not the 2 MB.  Every request to pcsx-redux costs a fixed **~32 ms service
wait**, and it is request-shaped rather than payload-shaped:

| request | median time to FIRST byte |
|---|---|
| `GET /api/v1/cpu/ram/raw` (2 MB) | 31.9 ms |
| `GET /api/v1/gpu/vram/raw` (1 MB) | 32.3 ms |
| `GET /api/v1/nonexistent` -- a 404 that does NO work | 36.1 ms |
| `POST /api/v1/nonexistent` -- writes nothing | 32.3 ms |

The 2 MB body then streams in **0.5 ms**.  So the cost of a tick is the number
of ROUND TRIPS, and a changed-pose tick makes **four**: the whole-RAM GET for
the before-image, then three POSTs (`work_rotation` -- with the vertical datum
42 bytes on, inside `COALESCE_GAP` -- `sprite_scale`, and `work_position`).
~128 ms of blocking I/O every 50 ms, on Blender's own thread.

**This is why `COALESCE_GAP` is not the lever, and why dropping the GET is
not either.**  Without a before-image the four runs cannot be merged at all --
stock has no partial GET to fill the 42-byte gap from -- so `0 GET + 4 POSTs`
is the same four requests as `1 GET + 3 POSTs`.  Measured both: ratio 0.263 and
0.265.  Keep-alive is no lever either; the 404 above pays the wait on a
connection that did nothing.

The instrument is `blender_settle_stall.py`'s: a main-thread heartbeat counting
Python iterations, because that is what Blender's main loop does -- it enters
Python on every `bpy.app.timers` callback and every panel `draw()`.  The stub
is **latency-matched** to the table above rather than being a fast loopback
server, which is the only thing that makes the number honest; a plain
`http.server` answers in 1.1 ms and reports ratio 0.96, i.e. no bug at all.

It is a stub and never the artist's emulator.  `_camera_sync_timer`'s
`bpy.app.background` guard is left alone -- this drives the tick function the
timer calls, the way `blender_settle_stall.py` drives `settle_op._launch`.

Run:  python3 tests/blender_camera_stall.py [blender-binary]
      python3 tests/blender_camera_stall.py [blender-binary] --seed
      python3 tests/blender_camera_stall.py --verdict [report.json]
"""
import json
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
ADDON_DIR = PKG / "addons" / "exmateria_map"
TMP = Path(__file__).resolve().parent / ".blender_camera_stall"
REPORT = TMP / "report.json"

#: The measured per-request service wait, in seconds.  See the table above.
#: NOT a tuning knob -- it is the emulator's number, and lowering it would make
#: every floor here pass against a machine nobody runs.
SERVICE_WAIT = 0.032

#: "The UI thread is on top", as a number, and the SAME bar
#: `blender_settle_stall.py` holds the settle to.  The two loops make the same
#: promise to the artist and there is no reason for them to make it differently.
MIN_THROUGHPUT_RATIO = 0.5

#: The sync must still make the rate it schedules itself at.  A tick that
#: outlasts its own period cannot, and the emulator then gets the viewport at
#: whatever rate the transport allows instead of at 20 Hz.
MIN_RATE_FRACTION = 0.8


SCRIPT = r'''
import http.server, json, sys, threading, time
import bpy
sys.path.insert(0, r"@ADDONPKG@")
import exmateria_map
exmateria_map.register()

from exmateria_map import live_link as _L, live_link_ui as _UI, worker
import mathutils

SEED = @SEED@
WAIT = @WAIT@
RAM = bytes(_L.RAM_BYTES)
COUNT = {"get": 0, "post": 0}


class Handler(http.server.BaseHTTPRequestHandler):
    """The emulator's SERVICE WAIT, not a fast loopback server."""
    protocol_version = "HTTP/1.0"
    def log_message(self, *a): pass
    def do_GET(self):
        time.sleep(WAIT); COUNT["get"] += 1
        self.send_response(200)
        self.send_header("Content-Length", str(len(RAM))); self.end_headers()
        self.wfile.write(RAM)
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        time.sleep(WAIT); COUNT["post"] += 1
        self.send_response(200)
        self.send_header("Content-Length", "0"); self.end_headers()


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

out = {"python": sys.version.split()[0], "seeded": SEED,
       "service_wait_ms": WAIT * 1000.0,
       "tick_interval_s": _L.CAMERA_SYNC_INTERVAL}


class OrbitingView:
    """A `RegionView3D` the artist is dragging: the pose changes EVERY tick,
    which is the sync's worst case and is exactly what was reported.  Blender's
    own `mathutils` so the quaternion-to-matrix step is really exercised."""
    view_perspective = "ORTHO"
    def __init__(self):
        self.n = 0
        self.view_distance = 336.0
        self.view_rotation = mathutils.Quaternion((1, 0, 0, 0))
        self.view_location = mathutils.Vector((182.0, 154.0, -4.75))
    def orbit(self):
        self.n += 1
        self.view_location = mathutils.Vector((182.0 + self.n, 154.0, -4.75))


def heartbeat(seconds, tick=None, interval=_L.CAMERA_SYNC_INTERVAL):
    """Main-thread Python throughput, the way Blender's main loop feels it."""
    t0 = time.monotonic(); n = ticks = 0; worst = 0.0
    last = t0; nxt = t0
    while True:
        now = time.monotonic()
        gap = (now - last) * 1000.0
        if gap > worst: worst = gap
        last = now
        if now - t0 >= seconds: break
        n += 1
        if tick is not None and now >= nxt:
            tick(); ticks += 1
            nxt = time.monotonic() + interval
        time.sleep(0)
    dt = time.monotonic() - t0
    return {"seconds": dt, "iterations": n, "per_sec": n / dt if dt else 0,
            "ticks": ticks, "ticks_per_sec": ticks / dt if dt else 0,
            "max_gap_ms": worst}


out["baseline"] = heartbeat(1.0)

view = OrbitingView()
ticker = _L.CameraSyncTicker()
make_client = lambda: _L.RamClient(host="127.0.0.1", port=PORT)


def tick():
    view.orbit()
    if SEED:
        # THE DEFECT, on purpose: the transport on the calling thread, which
        # is what shipped before the amendment.  It is what proves the floors
        # below can go red at all.
        _UI.sync_camera(make_client(), view, ticker=ticker)
    else:
        _UI.sync_camera_background(make_client, view, ticker=ticker)


COUNT["get"] = COUNT["post"] = 0
run = heartbeat(3.0, tick=tick)
out["syncing"] = run
out["requests"] = dict(COUNT)
out["requests_per_tick"] = (COUNT["get"] + COUNT["post"]) / max(run["ticks"], 1)
out["throughput_ratio"] = (run["per_sec"] / out["baseline"]["per_sec"]
                           if out["baseline"]["per_sec"] else 0)
out["achieved_hz"] = run["ticks_per_sec"]
out["wanted_hz"] = 1.0 / _L.CAMERA_SYNC_INTERVAL

# The pose really did reach the stub -- a harness that measures a sync which
# quietly stopped syncing measures nothing.  Wait the flight out first.
deadline = time.monotonic() + 10.0
while worker.live() and time.monotonic() < deadline:
    time.sleep(0.01)
out["worker_drained"] = worker.live() == 0
out["landed"] = COUNT["post"] > 0

json.dump(out, open(r"@OUT@", "w"), indent=1)
print("WROTE", r"@OUT@")
srv.shutdown()
'''


def verdict(r):
    ratio = r["throughput_ratio"]
    hz, want = r["achieved_hz"], r["wanted_hz"]
    checks = [
        ("the UI thread is on top: the camera sync leaves the main thread most "
         "of its Python throughput",
         ratio > MIN_THROUGHPUT_RATIO, f"{ratio:.3f} > {MIN_THROUGHPUT_RATIO}"),
        (f"no tick outlasts the sync's own period "
         f"({r['tick_interval_s'] * 1000:.0f} ms), so it makes the rate it "
         f"schedules itself at",
         hz >= want * MIN_RATE_FRACTION,
         f"{hz:.1f} Hz of {want:.0f} Hz"),
        ("the main thread is never blocked for longer than one tick",
         r["syncing"]["max_gap_ms"] < r["tick_interval_s"] * 1000.0,
         f"worst gap {r['syncing']['max_gap_ms']:.0f} ms"),
        ("...and the pose still reaches the emulator",
         r["landed"] and r["worker_drained"],
         f"{r['requests']} posted, workers drained: {r['worker_drained']}"),
    ]
    print()
    for name, ok, detail in checks:
        print(("  ok   " if ok else "  FAIL ") + name + f": {detail}")
    bad = [n for n, ok, _ in checks if not ok]
    print(f"\nSUMMARY: {len(checks) - len(bad)}/{len(checks)} checks passed")
    print("PASS" if not bad else "FAIL")
    return not bad


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--verdict":
        path = Path(sys.argv[2]) if len(sys.argv) > 2 else REPORT
        r = json.loads(path.read_text())
        if not verdict(r):
            raise SystemExit(1)
        return r
    argv = [a for a in sys.argv[1:] if a != "--seed"]
    seed = "--seed" in sys.argv
    blender = argv[0] if argv else "blender"
    TMP.mkdir(parents=True, exist_ok=True)
    script = (SCRIPT.replace("@ADDONPKG@", str(ADDON_DIR.parent))
                    .replace("@OUT@", str(REPORT))
                    .replace("@SEED@", "True" if seed else "False")
                    .replace("@WAIT@", repr(SERVICE_WAIT)))
    path = TMP / "run.py"
    path.write_text(script)
    if REPORT.exists():
        REPORT.unlink()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from blender_env import isolated_env
    p = subprocess.run([blender, "--background", "--factory-startup",
                        "--python", str(path)],
                       capture_output=True, text=True, timeout=600,
                       env=isolated_env())
    if not REPORT.exists():
        print(p.stdout[-6000:], file=sys.stderr)
        print(p.stderr[-4000:], file=sys.stderr)
        raise SystemExit("no report")
    r = json.loads(REPORT.read_text())
    ok = verdict(r)
    print(json.dumps(r, indent=1))
    if seed:
        # The seeded arm is EXPECTED to fail. It passing is the bad news.
        print("\n-- seeded arm: the shipped-before-the-amendment blocking tick")
        if ok:
            raise SystemExit("the seed did not bite: the floors cannot go red")
        print("the floors went red on the defect, as they must")
        return r
    if not ok:
        raise SystemExit(1)
    return r


if __name__ == "__main__":
    main()
