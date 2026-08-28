"""Phase (a) of the quantiser stress test: what does a sheet full of colours
the row cannot hold COST, and can the run tell the two failures apart?

ADR-0007 and `exmateria-map/CONTEXT.md`. The refusal *count* here is
arithmetically predetermined -- at most sixteen of the colour chart's colours can be
in the active CLUT row, so essentially every painted pixel is refused. Do not
mistake the red number for information. **The entire value of phase (a) is
cost**, and the second thing it proves is that the report separates
**unrepresentable** (off the BGR555 lattice; the remedy is a snap) from
**unreferenced** (on the lattice, absent from this row; the remedy is a
palette decision). A measurement that conflates them reports the format's bit
depth as though it were palette scarcity.

**Timing is RECORDED, never asserted.** A hardcoded wall-clock bound goes red
on someone else's machine and proves nothing about the code. What is asserted
is conservation -- `resolved + refused == painted`, no painted pixel silently
lost -- and the partition.

Two dials move separately so their curves can be told apart:

- `colours` at one texel each -- what `paint._gate` is a function of.
- `texels` at a FIXED colour count -- what the diff loop and the sticky
  re-check pass are a function of.

Run:  python3 tests/blender_texture_stress.py [blender-binary]
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
TMP = Path(__file__).resolve().parent / ".blender_stress"
REPORT = TMP / "report.json"

#: `(colours, off_gamut, texels)`. The first three scale the colour dial with
#: one texel each; the fourth holds the colour dial at the first run's value
#: and multiplies texels by sixteen, which is the control that separates
#: `_gate`'s per-entry cost from the resolve's per-pixel cost.
RUNS = [
    (1000, 24, 1024),
    (4000, 96, 4096),
    (16000, 384, 16384),
    (1000, 24, 16384),
    (32768, 768, 262144),
]

#: The full-sheet run also probes EXPORT, which is a separate module with the
#: same root cause: `export_document.py` emits one refusal LINE per sticky
#: entry. Surfaced, not capped -- a cap would hide how many there are.
EXPORT_PROBE_TEXELS = 262144

SCRIPT_TEMPLATE = r'''
import json
import os
import sys
import time
import traceback

import bpy

PKG = "@ADDONPKG@"
ZIP = "@ZIP@"
JSON = "@JSON@"
OUT = "@OUT@"
RUNS = @RUNS@
EXPORT_PROBE_TEXELS = @PROBE@

try:
    bpy.ops.preferences.addon_install(filepath=ZIP)
except Exception as e:
    print(f"INSTALL: {e}")
bpy.ops.preferences.addon_enable(module='exmateria_map')

sys.path.insert(0, PKG)
from exmateria_map import export_document as exp
from exmateria_map import paint as pnt
from exmateria_map import quantise as Q

doc = json.loads(open(JSON).read())
name = f"{doc['base']['map']}.a{doc['base']['arrangement']}"
checks = {}
runs = []


def check(n, cond, detail=""):
    checks[n] = bool(cond)
    if not cond:
        print(f"CHECK FAIL {n}: {detail}")


def clear_scene():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    for c in list(bpy.data.collections):
        if c.name != "Collection":
            bpy.data.collections.remove(c)
    # An IMAGE is a datablock: removing the object leaves it behind, and
    # `ensure_paint_image` hands the next run the LAST run's pixels back.
    # `paint._CACHE` is keyed on the image name and is per-process, so `was`
    # is the previous chart too -- and `painted` then counts the DIFFERENCE
    # between two charts rather than the chart.  Measured before this line
    # existed: run 2 read 3,096 painted of 4,096, exactly the 1,000 texels it
    # shared with run 1.
    #
    # The sticky list lives on the OBJECT and does clear, which is why a
    # freshness check pointed at it passes while the canvas is dirty.
    for im in list(bpy.data.images):
        if im.type == "IMAGE":
            bpy.data.images.remove(im)
    pnt._CACHE.clear()


# `_gate` is an INSTRUMENT here, not a subject: every assertion below reads
# the public summary.  Timing it separately is the only way to see which of
# the two curves a run is actually on.
_gate_seconds = [0.0]
_real_gate = pnt._gate


def _timed_gate(*a, **k):
    t = time.perf_counter()
    try:
        return _real_gate(*a, **k)
    finally:
        _gate_seconds[0] += time.perf_counter() - t


pnt._gate = _timed_gate


def one_run(colours, off_gamut, texels):
    """A FRESH import per run: a sticky list carried over from the last one
    makes every number after the first a measurement of the run before it."""
    clear_scene()
    bpy.ops.import_map.document(filepath=JSON)
    ob = bpy.data.objects.get(name)
    bpy.context.view_layer.objects.active = ob
    sheet = pnt.sheet_of_state(ob, int(ob["exmateria_map/preview_state"]))
    img = pnt.ensure_paint_image(ob, sheet)
    state, pal = pnt.active_palette(ob)
    entries = pnt.clut_entries(ob, state, pal)
    check(f"c{colours}t{texels}_sticky_starts_empty", not pnt.sticky(ob))
    # The canvas starts as the IMPORTED sheet under this row -- the check the
    # sticky one cannot make, and the one that catches a run reading the
    # previous run's colour chart as its baseline.
    check(f"c{colours}t{texels}_canvas_starts_from_the_import",
          list(pnt._floats(img))
          == list(pnt.expand(pnt.read_buffer(pnt.index_image(ob, sheet)),
                             entries)),
          "the paint image is not the imported sheet expanded under this row")

    seq = Q.colour_chart(colours=colours, off_gamut=off_gamut, texels=texels)
    px = pnt._floats(img)
    # What the canvas already holds under each chart texel. A chart colour
    # the row holds AND that already sits at that texel is not painted at all
    # (§3.4: an unchanged pixel is never re-resolved), so `painted == texels`
    # is the wrong bar -- it goes red on a correctness property. Only a row
    # entry can coincide, so this is bounded by the sixteen.
    already = sum(1 for i, c in enumerate(seq)
                  if tuple(int(round(px[i * 4 + k] * 255.0))
                           for k in range(3)) == c)
    for i, (r, g, b) in enumerate(seq):
        px[i * 4] = r / 255.0
        px[i * 4 + 1] = g / 255.0
        px[i * 4 + 2] = b / 255.0
    img.pixels.foreach_set(px)

    _gate_seconds[0] = 0.0
    t0 = time.perf_counter()
    s = pnt.resolve(ob)
    seconds = time.perf_counter() - t0

    # What the CHART says it laid down, classified by a module the resolve
    # never touches.  Both sides of the partition assertion are independent
    # of `paint.py`.
    want = Q.partition(seq, entries)

    got = {"unrepresentable": 0, "unreferenced": 0}
    for e in pnt.sticky(ob):
        hexc = e.get("color") or "#000000"
        rgb = tuple(int(hexc.lstrip("#")[k:k + 2], 16) for k in (0, 2, 4))
        got[Q.refusal_kind(rgb, entries)] += e.get("count", 0)

    blob = ob.get("exmateria_map/off_palette") or ""
    rec = {
        "colours": colours, "off_gamut": off_gamut, "texels": texels,
        "painted": s["painted"], "resolved": s["resolved"],
        "refused": s["refused"], "off_palette": s["off_palette"],
        "cleared": s["cleared"], "already": already,
        "seconds": round(seconds, 3),
        "gate_seconds": round(_gate_seconds[0], 3),
        "sticky_entries": len(pnt.sticky(ob)),
        "sticky_bytes": len(blob),
        "unrepresentable": got["unrepresentable"],
        "unreferenced": got["unreferenced"],
        "want_unrepresentable": want["unrepresentable"],
        "want_unreferenced": want["unreferenced"],
        "want_resolved": want["resolved"],
    }
    runs.append(rec)
    print(f"RUN {rec}")

    tag = f"c{colours}t{texels}"
    # --- the one invariant worth having ---------------------------------
    check(f"{tag}_conserves", s["resolved"] + s["refused"] == s["painted"],
          str(rec))
    check(f"{tag}_painted_everything_that_changed",
          s["painted"] == texels - already, str(rec))
    check(f"{tag}_only_a_row_entry_can_already_be_there",
          already <= want["resolved"], str(rec))
    # --- and the one thing the band exists to prove ----------------------
    check(f"{tag}_unrepresentable_matches_the_band",
          got["unrepresentable"] == want["unrepresentable"] > 0, str(rec))
    check(f"{tag}_unreferenced_matches_the_gamut",
          got["unreferenced"] == want["unreferenced"] > 0, str(rec))
    check(f"{tag}_the_two_kinds_are_the_whole_refusal",
          got["unrepresentable"] + got["unreferenced"] == s["off_palette"],
          str(rec))
    check(f"{tag}_the_two_kinds_are_different_numbers",
          got["unrepresentable"] != got["unreferenced"],
          f"{rec}; equal counts cannot show a bucket that lumps them")
    # A chart colour the row DOES hold resolves rather than refusing, and the
    # chart knows exactly how many of its texels that is -- minus the ones
    # already sitting there, which are never repainted. An EXACT number, not
    # a bound: `seq` is the tiled sequence, so a bound written over its
    # length reads 1 copy where there are 8 and goes red on correct code.
    check(f"{tag}_resolved_is_exactly_what_the_row_holds",
          s["resolved"] == want["resolved"] - already, str(rec))
    check(f"{tag}_nothing_was_cleared", s["cleared"] == 0, str(rec))
    check(f"{tag}_sticky_cap_never_fired",
          all(len(e.get("pixels") or []) == e.get("count")
              for e in pnt.sticky(ob)),
          f"STICKY_PIXEL_CAP caps pixels PER ENTRY; this chart is the "
          f"inverse case -- many colours, few pixels each -- so it must not "
          f"fire")

    if texels >= EXPORT_PROBE_TEXELS:
        t0 = time.perf_counter()
        _pd, _pf, _prep = exp.assemble(ob)
        rec["export_seconds"] = round(time.perf_counter() - t0, 3)
        rec["export_refusal_lines"] = len(_prep.refusals)
        rec["export_refusal_bytes"] = sum(len(str(r)) for r in _prep.refusals)
        print(f"EXPORT {rec}")
        # The gate held: a sheet full of colours the row cannot hold does not
        # reach the disc.
        check(f"{tag}_export_refuses", bool(_prep.refusals), str(rec))
        phase_b(ob, sheet, img, seq, state, pal, entries, rec, tag)
        # How many lines it takes to say so is RECORDED, not bounded. A
        # check that cannot fail is not coverage, and a cap here would hide
        # the number that makes the defect triageable.


def phase_b(ob, sheet, img, seq, state, pal, before, rec, tag):
    """ADR-0007 decision 1: the quantiser is a layer ABOVE `resolve()`.

    It reads what the artist painted, decides sixteen colours AND the index
    every painted texel takes, writes the row, and hands `resolve()` a sheet
    it can gate exactly.  `resolve()` is not relaxed -- its match is still
    exact, and the whole job here is to arrange that nothing needs refusing.

    The full-sheet chart is the only run this is asked of, because every one
    of its 262,144 texels was painted.  Decision 3 -- the quantiser may only
    decide pixels the artist painted -- has nothing to bite on there.  On a
    partially painted sheet, authoring the row also changes what the
    UNTOUCHED texels' indices look like, and what to do about that is not
    settled by ADR-0007.
    """
    bag = {}
    for c in seq:
        bag[c] = bag.get(c, 0) + 1
    t0 = time.perf_counter()
    row = Q.quantise(bag, 16)
    rec["quantise_seconds"] = round(time.perf_counter() - t0, 3)
    rec["quantised_error"] = round(Q.error(bag, row), 1)
    rec["naive_error"] = round(min(Q.error(bag, Q.naive_palette(sp))
                                   for sp in ((4, 2, 2), (2, 4, 2), (2, 2, 4))),
                               1)

    check(f"{tag}_row_is_legal",
          len(row) == 16 and all(Q.on_lattice(c) for c in row), str(row))
    # The bar is a baseline anyone can compute, never an absolute error.
    check(f"{tag}_beats_the_naive_baseline",
          rec["quantised_error"] <= rec["naive_error"],
          f"{rec['quantised_error']} vs {rec['naive_error']}")

    clut = bpy.data.images[json.loads(ob["exmateria_map/state_cluts"])[state]]
    cpx = pnt._floats(clut)
    for i, c in enumerate(row):
        for k in range(3):
            cpx[(pal * 16 + i) * 4 + k] = c[k] / 255.0
    clut.pixels.foreach_set(cpx)
    check(f"{tag}_row_reached_the_clut",
          pnt.clut_entries(ob, state, pal) == [tuple(c) for c in row],
          str(pnt.clut_entries(ob, state, pal)[:4]))

    # Committing the decision means the CANVAS becomes the quantised image.
    # `resolve()` matches exactly, so a texel whose colour is merely NEAR an
    # entry is still refused -- deciding the index and not writing it would
    # leave the sheet exactly as refused as before.
    want_index = []
    px = pnt._floats(img)
    for i, c in enumerate(seq):
        j = Q._nearest(c, row)[0]
        want_index.append(j)
        e = row[j]
        px[i * 4] = e[0] / 255.0
        px[i * 4 + 1] = e[1] / 255.0
        px[i * 4 + 2] = e[2] / 255.0
    img.pixels.foreach_set(px)

    t0 = time.perf_counter()
    s2 = pnt.resolve(ob)
    rec["commit_seconds"] = round(time.perf_counter() - t0, 3)
    rec["commit"] = dict(s2)
    print(f"PHASE-B {rec}")

    check(f"{tag}_commit_conserves",
          s2["resolved"] + s2["refused"] == s2["painted"], str(s2))
    check(f"{tag}_nothing_is_refused_any_more",
          s2["off_palette"] == 0 and s2["refused"] == 0, str(s2))
    check(f"{tag}_every_earlier_refusal_was_accounted_for",
          s2["cleared"] == rec["off_palette"], str(s2))
    # Every texel carries the index the quantiser chose -- the claim that the
    # sheet on the disc IS the quantised image, not merely a legal one.
    buf = pnt.read_buffer(pnt.index_image(ob, sheet))
    check(f"{tag}_the_buffer_is_the_quantisers_decision",
          list(buf[:len(want_index)]) == want_index,
          f"first disagreement at texel "
          f"{next((i for i in range(len(want_index)) if buf[i] != want_index[i]), None)}")
    _d, _f, _r2 = exp.assemble(ob)
    check(f"{tag}_the_sheet_reaches_the_disc", not _r2.refusals,
          str(_r2.refusals[:3]))
    # ...and `before` is the row it started with, so the export moved.
    check(f"{tag}_the_row_actually_changed", list(before) != list(row),
          "the quantiser returned the row it was given; an inert commit")


try:
    for _c, _o, _t in RUNS:
        one_run(_c, _o, _t)
except Exception:
    traceback.print_exc()
    checks["harness_completed"] = False

json.dump({"checks": checks, "runs": runs}, open(OUT, "w"), indent=1)
print(f"CHECKS: {sum(checks.values())}/{len(checks)} passed")
'''


def ensure_addon():
    zf_path = TMP / "exmateria_map.zip"
    if zf_path.exists():
        zf_path.unlink()
    with zipfile.ZipFile(zf_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(ADDON_DIR.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(ADDON_DIR.parent))
    return zf_path


def main():
    TMP.mkdir(exist_ok=True)
    if REPORT.exists():
        REPORT.unlink()                 # never grade on a stale report
    zf_path = ensure_addon()
    staged = TMP / "MAP001.a0.stub.json"
    staged.write_text(FIXTURE.read_text())
    fx = json.loads(FIXTURE.read_text())
    for st in fx["map_states"]:
        s = st.get("texture_sheet")
        if s:
            (TMP / s).write_bytes((FIXTURES / s).read_bytes())

    script = TMP / "run_stress.py"
    script.write_text(SCRIPT_TEMPLATE
                      .replace("@ADDONPKG@", str(ADDON_DIR.parent))
                      .replace("@ZIP@", str(zf_path))
                      .replace("@JSON@", str(staged))
                      .replace("@OUT@", str(REPORT))
                      .replace("@RUNS@", json.dumps(RUNS))
                      .replace("@PROBE@", str(EXPORT_PROBE_TEXELS)))
    # Isolate this Blender from the artist's OWN install. Without it the
    # `addon_install` in the script above overwrites the addon they are
    # clicking, and `addon_enable` then grades that copy rather than this
    # tree. `--factory-startup` does NOT do this -- see `blender_env`.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from blender_env import isolated_env
    proc = subprocess.run([sys.argv[1] if len(sys.argv) > 1 else "blender",
                           "--background", "--factory-startup",
                           "--python", str(script)],
                          capture_output=True, text=True,
                          env=isolated_env())
    sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stdout.write("\n[stderr]\n" + proc.stderr[-4000:])
    if not REPORT.exists():
        print("\nFAIL: no report written")
        sys.exit(1)
    report = json.loads(REPORT.read_text())

    print("\n  colours  band   texels    painted  refused   unrep   unref"
          "   resolve_s   gate_s   sticky_kB")
    for r in report["runs"]:
        print(f"  {r['colours']:>7}  {r['off_gamut']:>4}  {r['texels']:>7}  "
              f"{r['painted']:>9}  {r['refused']:>7}  {r['unrepresentable']:>6}"
              f"  {r['unreferenced']:>6}  {r['seconds']:>9.3f}  "
              f"{r['gate_seconds']:>7.3f}  {r['sticky_bytes'] / 1024:>9.1f}")
    for r in report["runs"]:
        if "quantised_error" in r:
            c = r["commit"]
            print(f"\n  phase (b) at {r['texels']} texels: quantised in "
                  f"{r['quantise_seconds']:.3f} s, committed in "
                  f"{r['commit_seconds']:.3f} s\n"
                  f"    error {r['quantised_error']} vs naive baseline "
                  f"{r['naive_error']}\n"
                  f"    after the commit: painted {c['painted']}, resolved "
                  f"{c['resolved']}, refused {c['refused']}, cleared "
                  f"{c['cleared']}, recovered {c.get('recovered')}")
    for r in report["runs"]:
        if "export_refusal_lines" in r:
            print(f"\n  export probe at {r['texels']} texels: "
                  f"{r['export_refusal_lines']} refusal lines "
                  f"({r['export_refusal_bytes'] / 1024:.0f} kB of text) in "
                  f"{r['export_seconds']:.3f} s, from "
                  f"{r['sticky_entries']} sticky entries")

    checks = report["checks"]
    failed = [n for n, ok in checks.items() if not ok]
    print(f"\nSUMMARY: {len(checks) - len(failed)}/{len(checks)} checks passed")
    if failed:
        print("FAILED:", ", ".join(failed))
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
