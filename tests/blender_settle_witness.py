"""Grade the settle's change WITNESS — ADR-0186 Amendment 10 decision 42.

`settle_op.canvas_digest` is polled at 4 Hz while the artist paints, and it is
the only thing that decides whether a settle fires.  Amendment 9 sized it at
5.4 ms on a 256x1024 float sheet -- "about 2 % of one core".  Amendment 10
re-sized it at N = 4, where the same buffer is 67 MB and the same poll is
85.8 ms: **34 % of one core, permanently, while the artist paints.**  That is
the whole reason for decision 42, so the check is on the COST as well as on
the answer.

Three properties, and the third is the one that moved:

1. **It is stable.** Polling an untouched canvas twice must give one answer,
   or the settle fires forever and the compile never converges.
2. **It moves on a single texel** -- Amendment 9's stated requirement.  A
   one-channel nudge is a change inside ONE 32-bit float, which is a burst of
   at most 32 bits, and CRC-32 detects every burst that short by construction
   where blake2b makes it only overwhelmingly likely.
3. **It is cheap.** Asserted RELATIVE to blake2b on the same buffer rather
   than against a millisecond figure, so it means the same thing on a machine
   that is not this one.  Amendment 10 measured 85.8 ms against 14.2 ms, a
   factor of six; this asks for three, which no reasonable machine turns into
   a coin flip and which blake2b cannot reach against itself.

Not a `pytest` file: `settle_op` imports `bpy` at module scope, and the buffer
under test is a real `Image.pixels`, which is the thing the 85.8 ms is about.

Run:  python3 tests/blender_settle_witness.py [blender-binary]
"""
import json
import os
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
ADDONS = PKG / "addons"
TMP = Path(__file__).resolve().parent / ".blender_settle_witness"
REPORT = TMP / "report.json"

#: The scale the cost is asserted at.  Decision 36 makes 4 the default at
#: conversion, so this is what the artist actually pays, not a worst case.
#: `WITNESS_SCALE=1` re-measures the sheet Amendment 9 sized, which is how the
#: rig is checked against a figure that was arrived at independently: it reads
#: blake2b at 5.4 ms there, the number Amendment 9 records.
SCALE = int(os.environ.get("WITNESS_SCALE", 4))
#: How much faster than `blake2b` the shipped witness must be on the same
#: buffer.  Amendment 10 measured 6.0x; three leaves room for a slower zlib
#: while still being unreachable for blake2b, which would score 1.0.
SPEEDUP = 3.0
#: Repeats per timing, taking the MINIMUM.  A mean on a shared machine reports
#: the neighbours; the minimum reports the code.
REPEATS = 5

SCRIPT = r'''
import array, hashlib, json, os, sys, time, zlib
import bpy
sys.path.insert(0, "@ADDONS@")
from exmateria_map import settle_op as S

CFG = json.loads(r"""@CFG@""")
OUT = "@OUT@"
os.makedirs(os.path.dirname(OUT), exist_ok=True)
n = CFG["scale"]
W, H = 256 * n, 1024 * n

img = bpy.data.images.new("witness", W, H, alpha=True, float_buffer=False)
img.colorspace_settings.name = "Non-Color"
# A real picture, not a flat one: a flat canvas makes several wrong digests
# look right, since almost any function of a constant buffer is constant.
px = array.array("f", [0.0]) * (W * H * 4)
for i in range(W * H):
    px[4 * i] = (i % 251) / 255.0
    px[4 * i + 1] = ((i // 7) % 251) / 255.0
    px[4 * i + 2] = ((i // 13) % 251) / 255.0
    px[4 * i + 3] = 1.0
img.pixels.foreach_set(px)

rep = {"scale": n, "w": W, "h": H}
rep["a"] = S.canvas_digest(img)
rep["b"] = S.canvas_digest(img)

# One texel, one channel, one level -- the smallest thing an artist can do to
# a `Non-Color` canvas that holds `byte / 255`.
TEXEL = (W * H) // 3
px[4 * TEXEL] = (px[4 * TEXEL] * 255.0 + 1.0) / 255.0
img.pixels.foreach_set(px)
rep["after_one_texel"] = S.canvas_digest(img)

# Cost.  Both hashes over the SAME bytes, so the `foreach_get` they share is
# out of the comparison and what is left is decision 42's actual change.
buf = array.array("f", [0.0]) * (W * H * 4)
img.pixels.foreach_get(buf)


def best(fn):
    t = None
    for _ in range(CFG["repeats"]):
        s = time.perf_counter()
        fn(buf)
        e = time.perf_counter() - s
        t = e if t is None else min(t, e)
    return t * 1000.0


rep["ms_blake2b"] = best(lambda b: hashlib.blake2b(b, digest_size=16).hexdigest())
rep["ms_crc32"] = best(lambda b: format(zlib.crc32(b), "08x"))
# The shipped poll end to end, `foreach_get` included.  Timed at the PUBLIC
# seam on purpose: the artist pays the read as well as the hash, and a check
# reaching past `canvas_digest` for the hash alone would be measuring a thing
# nobody calls.  It costs the check nothing -- the read is ~3 ms of the 86.
s = time.perf_counter()
for _ in range(CFG["repeats"]):
    S.canvas_digest(img)
rep["ms_canvas_digest"] = (time.perf_counter() - s) * 1000.0 / CFG["repeats"]

with open(OUT, "w") as f:
    json.dump(rep, f, indent=1)
print("REPORT", OUT)
'''


def main():
    TMP.mkdir(parents=True, exist_ok=True)
    if REPORT.exists():
        REPORT.unlink()
    script = TMP / "run_witness.py"
    script.write_text(SCRIPT.replace("@ADDONS@", str(ADDONS))
                            .replace("@OUT@", str(REPORT))
                            .replace("@CFG@", json.dumps(
                                {"scale": SCALE, "repeats": REPEATS})))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from blender_env import isolated_env
    proc = subprocess.run([sys.argv[1] if len(sys.argv) > 1 else "blender",
                           "--background", "--factory-startup",
                           "--disable-crash-handler", "-noaudio",
                           "--python", str(script)],
                          capture_output=True, text=True, env=isolated_env())
    if not REPORT.exists():
        sys.stdout.write(proc.stdout[-3000:])
        sys.stdout.write("\n[stderr]\n" + proc.stderr[-4000:])
        print("\nFAIL: no report written")
        return 1
    rep = json.loads(REPORT.read_text())

    # Deliberately an OVER-estimate of the change decision 42 makes: the
    # shipped side pays `foreach_get` and the blake2b side does not.  A margin
    # that survives being handicapped is a margin worth quoting.
    shipped = rep["ms_canvas_digest"]
    ratio = rep["ms_blake2b"] / shipped if shipped else 0.0

    checks = [
        ("the witness is stable on an untouched canvas",
         rep["a"] == rep["b"], rep["a"]),
        ("the witness MOVES on a single texel (Amendment 9's requirement)",
         rep["after_one_texel"] != rep["a"],
         f"{rep['a']} -> {rep['after_one_texel']}"),
        (f"the witness is at least {SPEEDUP}x cheaper than blake2b",
         ratio >= SPEEDUP,
         f"{shipped:.1f} ms (the whole poll) vs blake2b "
         f"{rep['ms_blake2b']:.1f} ms (the hash alone) = {ratio:.1f}x"),
        ("zlib.crc32 is the cheap one Amendment 10 measured",
         rep["ms_blake2b"] / rep["ms_crc32"] >= SPEEDUP,
         f"crc32 {rep['ms_crc32']:.1f} ms vs blake2b {rep['ms_blake2b']:.1f} ms"),
    ]
    bad = 0
    print(f"N = {rep['scale']}  ({rep['w']}x{rep['h']}, "
          f"{rep['w'] * rep['h'] * 4 * 4 / 1e6:.1f} MB of float)\n")
    for name, ok, detail in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {name}: {detail}")
        bad += 0 if ok else 1
    print(f"\n  poll as the artist pays it: {rep['ms_canvas_digest']:.1f} ms "
          f"= {rep['ms_canvas_digest'] / 250.0 * 100:.0f}% of one core at 4 Hz")
    print(f"\nSUMMARY: {len(checks) - bad}/{len(checks)} checks passed")
    print("PASS" if not bad else "FAIL")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
