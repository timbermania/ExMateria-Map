"""S1 corpus axis harness: the REAL addon operator over the whole 148-arrangement corpus.

Mirrors `workspace/roundtrip426.py`'s three-axis verdict, but on the real import
leg (the operator, not the in-process mirror) and on the SHIPPED `dump`
(`exmateria_map.dump`, the same one `build` round-trips against), so every
attribute it asserts is a schema-v1 name and the addon and the writer cannot
be reading two different documents:

- axis 1: `export(import(doc)) == doc` per arrangement, through the addon's
  REAL export operator (#557) and over the WHOLE document -- positions/UV/
  normals through decision 14's axis map + ring reversal + per-face flip,
  every carried field, the visible_angles -1 sentinel, and the `base` /
  `terrain` / `map_states` / `carry` sections each export reads from a
  different scene source;
- axis 2: the wellformed() buckets against the PINNED
  `exmateria-map/blender_axis_baseline.json` (a fixed expectation, no ratchet),
  plus the pinned-constant read-back (axis_map);
- axis 0: field coverage — no document field may be constant corpus-wide;
- the drift checker's live coverage rule against `dump`'s `base.floor_steps`
  (#557 / export-v1 §6): decision 22 makes the drifted set EMPTY on every
  untouched import, so any disagreement between the two paths shows up here.

Run:  python3 tests/blender_corpus.py [blender-binary] [--limit N]
"""
import json
import subprocess
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
ADDON_DIR = PKG / "addons" / "exmateria_map"
TMP = Path(__file__).resolve().parent / ".blender_corpus"
DOCS = TMP / "docs"
REPORT = TMP / "report.json"
BASELINE = PKG / "blender_axis_baseline.json"

LIMIT = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None
BLENDER = next((a for a in sys.argv if a.endswith("blender") or a == "blender"), "blender")


SCRIPT_TEMPLATE = r'''
import json
import sys
import time
from collections import Counter, defaultdict

import bpy

PKG = "@ADDONPKG@"
ZIP = "@ZIP@"
DOCS = "@DOCS@"
OUT = "@OUT@"
TARGETS = json.loads('@TARGETS@')
AXIS_MAP = json.loads('@AXIS_MAP@')
EXPECT = json.loads('@EXPECT@')
GROUPS = json.loads('@GROUPS@')
DOC_FIELDS = tuple(json.loads('@DOC_FIELDS@'))
ARRANGEMENTS = @ARRANGEMENTS@

bpy.ops.preferences.addon_install(filepath=ZIP)
bpy.ops.preferences.addon_enable(module='exmateria_map')

sys.path.insert(0, PKG)
from exmateria_map import authoring as aumod
from exmateria_map import export_document as expmod
from exmateria_map import import_document as mod

FWD = mod._fft_to_blender
INV = mod._blender_to_fft
WIND = mod.WIND
FLOOR = 0.85
TEXTURED = mod.TEXTURED_KINDS


def ring(n):
    return (0, 1, 3, 2) if n == 4 else tuple(range(n))


def import_order(n, flipped=False):
    o = ring(n)
    if mod.REVERSE_RING:
        o = o[::-1]
    if flipped:
        o = o[::-1]
    return o


def newell(p):
    g = [0.0, 0.0, 0.0]
    n = len(p)
    for i in range(n):
        a, b = p[i], p[(i + 1) % n]
        g[0] += (a[1] - b[1]) * (a[2] + b[2])
        g[1] += (a[2] - b[2]) * (a[0] + b[0])
        g[2] += (a[0] - b[0]) * (a[1] + b[1])
    return g


def mag(v):
    return (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) ** 0.5


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def uv_dec(u, v):
    t = int(round((1.0 - v) * 1024.0 - 0.5))
    return int(round(u * 256.0 - 0.5)), t % 256, t // 256


def run_import(path):
    try:
        return bpy.ops.import_map.document(filepath=path)
    except RuntimeError as e:
        return {"result": "CANCELLED", "error": str(e)}


# ---------------------------------------------------------------- axis 1 ---
# The addon's REAL export leg (decision 8 / #519: the harness drives the
# shipped operators).  This block used to hold an in-script MIRROR of export;
# a mirror can only ever agree with itself, and #557 made the real leg exist.
def readback(ob):
    doc, files, rep = expmod.assemble(ob)
    return doc, rep


def diff(a, b):
    """Whole-document mismatch counts -- `export(import(doc)) == doc`, not the
    polygons alone.  `base`, `terrain`, `map_states` and `carry` each ride a
    different export source (the grid object, the tile objects, the sheet
    buffers, the marker JSON), so a polygon-only diff cannot see three of the
    four."""
    bad = Counter()
    for k in ("format", "version", "base", "terrain", "map_states", "carry"):
        if a.get(k) != b.get(k):
            bad["doc." + k] += 1
    if len(a["polygons"]) != len(b["polygons"]):
        bad["POLYGON_COUNT"] = abs(len(a["polygons"]) - len(b["polygons"]))
    for pa, pb in zip(a["polygons"], b["polygons"]):
        for k in set(pa) | set(pb):
            if pa.get(k, "<missing>") != pb.get(k, "<missing>"):
                bad[k] += 1
    return bad


# ---------------------------------------------------------------- axis 0 ---
def field_values(doc, acc):
    for p in doc["polygons"]:
        for k in DOC_FIELDS:
            if k not in p:
                continue
            v = p[k]
            if k in ("positions", "normals", "uv"):
                for row in v:
                    acc[k].update(row)
            elif k == "unknown_untextured":
                acc[k].add(tuple(v))
            elif k == "terrain":
                acc[k].add(tuple(v.items()))
            else:
                acc[k].add(v)


# ---------------------------------------------------------------- axis 2 ---
def wellformed(ob):
    me = ob.data
    tex = me.attributes["textured"].data
    flipa = me.attributes["fft_ring_flipped"].data
    nrm = me.attributes["normals"].data
    c = Counter()
    distinct = len({tuple(int(round(x)) for x in v.co) for v in me.vertices})
    c["verts"] = len(me.vertices)
    c["corners"] = len(me.loops)
    c["distinct_positions"] = distinct
    c["unwelded_verts"] = len(me.vertices) - distinct
    for i, f in enumerate(me.polygons):
        if not tex[i].value:
            continue
        quad = f.loop_total == 4
        flipped = flipa[i].value
        if flipped:
            c["flipped"] += 1
            c["flipped_quad" if quad else "flipped_tri"] += 1
        rp = [me.vertices[me.loops[li].vertex_index].co
              for li in range(f.loop_start, f.loop_start + f.loop_total)]
        g = newell([tuple(v) for v in rp])
        gm = mag(g)
        acc = [0.0, 0.0, 0.0]
        for li in range(f.loop_start, f.loop_start + f.loop_total):
            v = nrm[li].vector
            for k in range(3):
                acc[k] += v[k] / f.loop_total
        am = mag(acc)
        if am:
            up = acc[2] / am
            if abs(up) >= FLOOR:
                c["floor"] += 1
                if up > 0:
                    c["floor_up"] += 1
        if not quad:
            continue
        c["textured_quads"] += 1
        if mag(tuple(f.normal)) < 1e-6:
            c["blender_normal_degenerate"] += 1
        if gm < 1e-3 or am < 1e-6:
            c["degenerate"] += 1
            c["pre_degenerate"] += 1
            continue
        d = dot(g, acc) / (gm * am)
        c["aligned" if d > WIND else "anti" if d < -WIND else "ambiguous"] += 1
        pre = -d if flipped else d
        c["pre_" + ("aligned" if pre > WIND else "anti" if pre < -WIND else "ambiguous")] += 1
    return c


t0 = time.time()
total = Counter()
wf = Counter()
fv = defaultdict(set)
n = ok = dropped = refused = warned = 0
drift_tiles = drift_wrong = 0
fails = []
for name in TARGETS:
    doc = json.loads(open(f"{DOCS}/{name}").read())
    res = run_import(f"{DOCS}/{name}")
    if "FINISHED" not in res:
        fails.append((name, "import refused", str(res)))
        continue
    bname = f"{doc['base']['map']}.a{doc['base']['arrangement']}"
    ob = bpy.data.objects.get(bname)
    if ob is None:
        fails.append((name, "no mesh object", ""))
        continue
    n += 1
    built = len(ob.data.polygons)
    dropped += len(doc["polygons"]) - built
    field_values(doc, fv)
    wf.update(wellformed(ob))
    # Decision 22: an untouched document has no drift BY CONSTRUCTION, so the
    # drifted set must be EMPTY on every arrangement.  This is the only place
    # the checker's live coverage rule is graded against `dump`'s: `dump`
    # computed `base.floor_steps` from the ROM resource, the checker recomputes
    # the same number from the Blender mesh, and a rule that disagreed would
    # light the whole grid up on import.  Two independent paths to one number.
    _dr = aumod.drifted(ob)
    drift_tiles += len(aumod.base_floor_steps(ob))
    if _dr:
        drift_wrong += len(_dr)
        fails.append((name, "drift on an untouched import",
                      list(_dr.items())[:3]))
    got, rep = readback(ob)
    # An untouched corpus document must export with ZERO refusals: every
    # refusal is a rule about a value the artist can only reach by editing.
    if rep.refusals:
        refused += len(rep.refusals)
        fails.append((name, "export refused", rep.refusals[:4]))
    warned += len(rep.warnings)
    bad = diff(doc, got)
    if not bad and not rep.refusals:
        ok += 1
    total.update(bad)
    if bad:
        fails.append((name, "mismatch", dict(bad)))

dt = time.time() - t0

const_ok = (list(mod.AXIS_NAME) == list(AXIS_MAP["blender_from_fft"])
            and mod.REVERSE_RING == AXIS_MAP["reverse_ring"])
moved, rows = [], []
for grp, keys in GROUPS:
    badg = False
    for k in keys:
        got, exp = wf.get(k, 0), EXPECT.get(k)
        rows.append([grp, k, got, exp, got == exp])
        if got != exp:
            badg = True
    if badg:
        moved.append(grp)

dead = [k for k in DOC_FIELDS if len(fv.get(k, ())) <= 1]

report = {
    "n": n, "ok": ok, "dropped": dropped, "dt": round(dt, 1),
    "export_refused": refused, "export_warned": warned,
    "drift_tiles": drift_tiles, "drift_wrong": drift_wrong,
    "mismatch_total": dict(total), "fails": fails,
    "wf": dict(wf),
    "axis_constant": {"axis_map": list(mod.AXIS_NAME), "reverse_ring": mod.REVERSE_RING,
                      "matches": const_ok},
    "axis2_moved": moved, "axis2_rows": rows,
    "axis0_dead": dead,
    "arrangements_expected": ARRANGEMENTS,
}
json.dump(report, open(OUT, "w"), indent=1)
print(f"CORPUS {ok}/{n} EXACT in {dt:.1f}s; dropped={dropped}; "
      f"export_refused={refused}; export_warned={warned}; "
      f"drift_wrong={drift_wrong}/{drift_tiles}; fails={len(fails)}")
if total:
    for k, v in total.most_common():
        print(f"MISMATCH {k:32} {v}")
for name, why, detail in fails[:10]:
    print(f"FAIL {name}: {why} {detail}")
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
    if LIMIT:
        targets = targets[:LIMIT]

    print(f"corpus: {len(targets)} arrangement candidates" + (f" (limited to {LIMIT})" if LIMIT else ""))
    # roundtrip426's enumeration is `try: dump / except AssertionError: skip` —
    # an arrangement whose geometry source carries no 0x40 chunk is not a
    # dumpable corpus member (it never entered the 148).
    built = []
    skipped = []
    for num, a in targets:
        try:
            doc, _sheets = pkg_dump.dump(map_dir, num, a)
        except pkg_dump.DumpError as e:
            skipped.append((f"MAP{num:03d}.a{a}", str(e)))
            continue
        (DOCS / f"MAP{num:03d}.a{a}.json").write_text(json.dumps(doc))
        built.append((num, a))
    targets = built
    if skipped:
        print(f"skipped (no primary mesh): {len(skipped)}")

    baseline = json.loads(BASELINE.read_text())
    arrangements = baseline["arrangements"] if isinstance(baseline.get("arrangements"), int) \
        else len(targets)
    script = TMP / "run_corpus.py"
    script.write_text(SCRIPT_TEMPLATE
                      .replace("@ADDONPKG@", str(ADDON_DIR.parent))
                      .replace("@ZIP@", str(ensure_addon()))
                      .replace("@DOCS@", str(DOCS))
                      .replace("@OUT@", str(REPORT))
                      .replace("@TARGETS@", json.dumps([f"MAP{num:03d}.a{a}.json" for num, a in targets]))
                      .replace("@AXIS_MAP@", json.dumps(baseline["axis_map"]))
                      .replace("@EXPECT@", json.dumps(baseline["expect"]))
                      .replace("@GROUPS@", json.dumps([
                          ["weld", ["verts", "corners", "distinct_positions", "unwelded_verts"]],
                          ["winding", ["textured_quads", "aligned", "anti", "ambiguous", "degenerate"]],
                          ["ring", ["pre_aligned", "pre_anti", "pre_ambiguous", "pre_degenerate",
                                    "flipped", "flipped_tri", "flipped_quad"]],
                          ["up", ["floor", "floor_up"]]]))
                      .replace("@DOC_FIELDS@", json.dumps([
                          "kind", "positions", "normals", "uv", "texture_page", "palette_id",
                          "palette_byte_high_nibble", "texture_byte6_high_nibble",
                          "unknown_texture_value_6a", "terrain", "unknown_untextured",
                          "visible_angles"]))
                      .replace("@ARRANGEMENTS@", str(arrangements)))

    # Isolate this Blender from the artist's OWN install. Without it the

    # `addon_install` in the script above overwrites the addon they are

    # clicking, and `addon_enable` then grades that copy rather than this

    # tree. `--factory-startup` does NOT do this -- see `blender_env`.

    sys.path.insert(0, str(Path(__file__).resolve().parent))

    from blender_env import isolated_env

    proc = subprocess.run([BLENDER, "--background", "--factory-startup", "--python", str(script)],
                          capture_output=True, text=True,
                          env=isolated_env())
    sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stdout.write("\n[stderr]\n" + proc.stderr[-3000:])
    if not REPORT.exists():
        print("\nFAIL: no report written")
        sys.exit(1)

    r = json.loads(REPORT.read_text())
    full = r["n"] == r["arrangements_expected"]
    print(f"\nSUMMARY: {r['ok']}/{r['n']} EXACT, dropped={r['dropped']}, "
          f"export_refused={r['export_refused']}, "
          f"export_warned={r['export_warned']}, "
          f"drift_wrong={r['drift_wrong']}/{r['drift_tiles']}, "
          f"axis2_moved={r['axis2_moved'] if full else '(partial run — no verdict)'}, "
          f"axis0_dead={r['axis0_dead'] if full else '(partial run — no verdict)'}, "
          f"axis_constant={r['axis_constant']['matches']}, n={r['n']}/{r['arrangements_expected']}")
    bad = bool(r["mismatch_total"]) or r["dropped"] or r["fails"] \
        or r["export_refused"] or r["drift_wrong"] \
        or not r["axis_constant"]["matches"] \
        or (full and (r["axis2_moved"] or r["axis0_dead"]))
    for name, why, detail in r["fails"]:
        print(f"  FAIL {name}: {why} {detail}")
    for grp, k, got, exp, good in r["axis2_rows"]:
        if not good:
            print(f"  MOVED {grp}.{k} got {got} expected {exp}")
    print("PASS" if not bad else "FAIL")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
