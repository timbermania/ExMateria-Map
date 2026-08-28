"""ADR-0004 decision 27, end to end: an authored light rig reaches the DISC.

The chain this grades is the whole point of the decision and no other harness
covers any of it:

    dump -> the addon's real import operator -> edit the state's exposed rig
    -> the addon's real export operator -> `build` -> the resource's 45 bytes

Five checks, and each one names a way the pipe can be built and still be wrong:

1. **DECLARED**   export writes `map_states[].authored_light_rig` for the state
   that has an Override, writes it for NO other state, and stamps `version: 2`.
   The presence of the field is the declaration (schema §7.1), so a leg that
   wrote it everywhere would be as wrong as one that wrote it nowhere.
2. **GRADIENT**   the 6 gradient bytes are the STATE's own, verbatim. The
   Override carries an editable-looking copy, and if a borrowed rig seeded it
   they are the LENDER's -- decision 25 shows those bytes read-only and the
   solve owns 39 bytes and carries 6.
3. **ON DISC**    `build`'s bytes at pointer `0x64` are exactly
   `pack_light_rig` of what the artist typed -- ambient and the gains
   integer-exact -- and **every other byte of every resource is the base's**.
   A rig that lands is worth nothing if it moved something else.
4. **PICTURE**    a direction does NOT round-trip byte-exactly and that is not
   a defect: the disc's magnitudes run 4094.4-4096.7 and the Override re-emits
   at exactly 4096, so an i16 can move a couple of LSB. The bar is the picture
   -- the angle between the written direction and the one the artist aimed,
   under 0.05 degrees.
5. **NOT EXPORTED**  an AUTHORED rig on a state that can hold none (a texture
   row, or a mesh row whose `0x64` is zero) is WARNED about and left
   preview-only. The rig is exposed on every state, borrowing ones included, so
   refusing would turn an ordinary preview action into a failed export.

The seed arm is check 1's "no other state": all 21 states are exposed and the
run edits exactly one, so a leg that declared on exposure rather than on an edit
fails here rather than reading green on a document nobody compared.

Run:  python3 tests/blender_authored_rig.py [blender-binary]
      (needs EXMATERIA_ASSETS_DIR, like the corpus harness)
"""
import json
import math
import subprocess
import sys
import zipfile
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
ADDON_DIR = PKG / "addons" / "exmateria_map"
TMP = Path(__file__).resolve().parent / ".blender_authored_rig"
BLENDER = next((a for a in sys.argv[1:] if a.endswith("blender") or a == "blender"),
               "blender")

MAP_NUMBER = 1
ARRANGEMENT = 0
#: MAP001.9, kind 46 -- a mesh row that carries a 0x64 chunk.
RIG_STATE = 1
#: MAP001.8, kind 23 -- a texture row. No rig, by kind (#576).
TEXTURE_STATE = 0
#: MAP001.11, kind 47 -- a MESH row whose 0x64 is zero. One of decision 27's
#: 13 chunkless rows; `build` may not manufacture the bytes (decision 19).
CHUNKLESS_STATE = 2

#: What the artist "types". Deliberately not the ROM's values -- an export that
#: promoted the state's own rig unchanged would pass a bytes check and prove
#: nothing about the editor.
AMBIENT = (0.2, 0.4, 0.6)                       # -> u8 51 / 102 / 153
GAINS = ((1.5, 0.25, 0.125), (0.0, 2.0, 0.5), (3.0, 3.0, 3.0))
DIRS = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.3, -0.4, 0.5))


SCRIPT = r'''
import json
import os
import shutil
import sys

import bpy

INDIR = "@INDIR@"
PKG = "@ADDONPKG@"
ZIP = "@ZIP@"
DOC = "@DOC@"
OUTDIR = "@OUTDIR@"
OUTDIR2 = "@OUTDIR2@"
REPORT = "@REPORT@"
RIG_STATE = @RIG_STATE@
TEXTURE_STATE = @TEXTURE_STATE@
CHUNKLESS_STATE = @CHUNKLESS_STATE@
AMBIENT = tuple(json.loads('@AMBIENT@'))
GAINS = [tuple(g) for g in json.loads('@GAINS@')]
DIRS = [tuple(d) for d in json.loads('@DIRS@')]

bpy.ops.preferences.addon_install(filepath=ZIP)
bpy.ops.preferences.addon_enable(module='exmateria_map')

sys.path.insert(0, PKG)
from exmateria_map import import_document as mod

report = {"errors": []}


def fail(text):
    report["errors"].append(text)


res = bpy.ops.import_map.document(filepath=DOC)
if "FINISHED" not in res:
    fail("import did not finish: %r" % (res,))
ob = next((o for o in bpy.data.objects if "exmateria_map/map_states" in o), None)
if ob is None:
    fail("no marker object after import")
    Path = None
else:
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)

    # Nothing to switch off any more.  Lamp authority defaults OFF and import
    # lands OFF (decision 30), so the lamps are not a writer on this export
    # unless the artist hands them the map -- which this harness never does.
    # The old `live_bake = False` here was guarding against a default of True.

    def expose(i):
        """Select state `i` and hand back the rig it is ALREADY exposed with.

        There is no gesture in between any more: `ensure_rig_exposure` seeded
        every state at import."""
        ob["exmateria_map/preview_state"] = i
        return mod.find_override(ob, i)

    # 1. the state that CAN receive bytes
    ov = expose(RIG_STATE)
    if ov is None:
        fail("state %d was not exposed by the import" % RIG_STATE)
    else:
        ov.ambient = AMBIENT
        for k, name in enumerate(mod.MAP_PG_rig_override.GAINS):
            setattr(ov, name, GAINS[k])
        for k, name in enumerate(mod.MAP_PG_rig_override.DIRS):
            setattr(ov, name, DIRS[k])
        # Push the Override's gradient OFF the state's own. Seeded from this
        # state it is identical, and then "the export echoed the state's
        # gradient" is satisfied by an export that echoed the OVERRIDE's -- an
        # inert check that reads exactly like a live one. This is the shape a
        # BORROWING state's exposed rig arrives in: it is seeded from the
        # lender, gradient included.
        ov.gradient = tuple((g + 7) % 256 for g in ov.gradient)
        report["typed"] = mod.override_rig(ov)

    # 2 and 3. states that CANNOT -- the artist may author on both, and export
    # must warn rather than refuse.
    #
    # These are EDITED, not merely exposed.  Every state carries an Override
    # now, so existence warns about nothing; what the artist MOVED is the
    # signal, and moving something is what has to draw the warning.  Without
    # the edit this arm grades an export that never warns at all.
    for i in (TEXTURE_STATE, CHUNKLESS_STATE):
        ov = expose(i)
        if ov is None:
            fail("state %d has no exposed rig to author on" % i)
        else:
            ov.ambient = tuple(min(1.0, c + 0.25) for c in ov.ambient)
            if not mod.rig_is_dirty(ov):
                fail("editing state %d's ambient left it reading clean" % i)

    report["overrides"] = sorted(o.state_index
                                 for o in ob.exmateria_map_rig_overrides)

    # Blender raises on `self.report({"ERROR"}, ...)`, and a REFUSAL is exactly
    # that -- so an export that refuses would kill this script and the audit
    # would score "the harness broke" instead of "the check found it". Catching
    # it makes the refusal a RESULT, which is what `nothing_refused` grades.
    try:
        res = bpy.ops.export_map.document(filepath=OUTDIR)
    except RuntimeError as e:
        res = {"CANCELLED", "error: %s" % e}
    report["export_result"] = sorted(res)
    report["export_lines"] = json.loads(ob.get("exmateria_map/last_export", "[]"))

    # --- the v2 document must survive being REOPENED ----------------------
    # Nothing else covers this. The authored rig lives in the marker's
    # `map_states` snapshot, so an import that dropped the field, or one that
    # re-seeded an Override from it (a direction does NOT survive the unit
    # vector), would lose or move the artist's lighting the next time they
    # opened the file -- silently, because the picture would still look lit.
    exported = None
    for f in sorted(os.listdir(OUTDIR)):
        if f.endswith(".json"):
            exported = os.path.join(OUTDIR, f)
    # NOT a `fail()`: an export that wrote nothing is a FINDING the outer leg
    # grades by name (`export_finished`, `export_wrote_a_document`,
    # `nothing_refused`). Calling it a scene-leg error made the outer leg bail
    # before any of them ran, so the `export_refuses_an_unwritable_override`
    # seed was scored as a broken harness rather than as the check that caught
    # it. The scene leg only `fail()`s when it could not run at all.
    if exported is not None:
        # The export rewrites only repainted sheets; this run repaints none, so
        # the sidecars it names are still the ones `dump` wrote.
        for f in os.listdir(INDIR):
            if f.endswith(".png") and not os.path.exists(os.path.join(OUTDIR, f)):
                shutil.copyfile(os.path.join(INDIR, f), os.path.join(OUTDIR, f))
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o, do_unlink=True)
        try:
            res2 = bpy.ops.import_map.document(filepath=exported)
        except RuntimeError as e:
            res2 = {"CANCELLED", "error: %s" % e}
        report["reimport_result"] = sorted(res2)
        ob2 = next((o for o in bpy.data.objects
                    if "exmateria_map/map_states" in o), None)
        if ob2 is None:
            fail("re-importing the v2 document built no object")
        else:
            bpy.context.view_layer.objects.active = ob2
            ob2.select_set(True)
            report["reimport_overrides"] = sorted(
                o.state_index for o in ob2.exmateria_map_rig_overrides)
            report["reimport_dirty"] = sorted(
                o.state_index for o in mod.dirty_overrides(ob2))
            try:
                res3 = bpy.ops.export_map.document(filepath=OUTDIR2)
            except RuntimeError as e:
                res3 = {"CANCELLED", "error: %s" % e}
            report["reexport_result"] = sorted(res3)

with open(REPORT, "w") as fh:
    json.dump(report, fh)
'''


def ensure_addon():
    TMP.mkdir(parents=True, exist_ok=True)
    zf_path = TMP / "exmateria_map.zip"
    if zf_path.exists():
        zf_path.unlink()
    with zipfile.ZipFile(zf_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(ADDON_DIR.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(ADDON_DIR.parent))
    return zf_path


RESULTS = []


def check(name, ok, detail=""):
    """`detail` is the FAILURE's evidence, so it prints only when it is one --
    a green line reading `x != x` is how a passing check gets read as a red."""
    RESULTS.append((name, bool(ok), detail))
    print(("  ok   " + name) if ok
          else ("  FAIL " + name + (f"  {detail}" if detail else "")))


def verdict():
    """The one place a run ends. Prints SUMMARY, then FAILED or PASS."""
    bad = [n for n, ok, _ in RESULTS if not ok]
    print(f"\nSUMMARY: {len(RESULTS) - len(bad)}/{len(RESULTS)} checks passed")
    if bad:
        print("FAILED: " + ", ".join(bad))
        return 1
    print("PASS")
    return 0


def angle_between(a, b):
    na = math.sqrt(sum(c * c for c in a))
    nb = math.sqrt(sum(c * c for c in b))
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b)) / (na * nb)
    return math.degrees(math.acos(max(-1.0, min(1.0, dot))))


def main():
    sys.path.insert(0, str(PKG))
    from exmateria_map import corpus, mapfile
    from exmateria_map import document as schema
    from exmateria_map.build import BuildRefusal, build

    map_dir = corpus.map_dir()
    if map_dir is None:
        print("FAIL: no corpus; set EXMATERIA_ASSETS_DIR")
        return 1

    TMP.mkdir(parents=True, exist_ok=True)
    doc_dir = TMP / "in"
    out_dir = TMP / "out"
    out_dir2 = TMP / "out2"
    for d in (doc_dir, out_dir, out_dir2):
        d.mkdir(exist_ok=True)
        for f in d.glob("*"):
            f.unlink()

    from exmateria_map.dump import write_bundle as dump_bundle
    name = f"MAP{MAP_NUMBER:03d}.a{ARRANGEMENT}"
    dump_bundle(map_dir, MAP_NUMBER, ARRANGEMENT, doc_dir)
    base_doc = json.loads((doc_dir / f"{name}.json").read_text())

    report_path = TMP / "report.json"
    if report_path.exists():
        report_path.unlink()
    script = TMP / "run.py"
    script.write_text(
        SCRIPT.replace("@ADDONPKG@", str(ADDON_DIR.parent))
              .replace("@ZIP@", str(ensure_addon()))
              .replace("@DOC@", str(doc_dir / f"{name}.json"))
              .replace("@OUTDIR@", str(out_dir))
              .replace("@OUTDIR2@", str(out_dir2))
              .replace("@INDIR@", str(doc_dir))
              .replace("@REPORT@", str(report_path))
              .replace("@RIG_STATE@", str(RIG_STATE))
              .replace("@TEXTURE_STATE@", str(TEXTURE_STATE))
              .replace("@CHUNKLESS_STATE@", str(CHUNKLESS_STATE))
              .replace("@AMBIENT@", json.dumps(list(AMBIENT)))
              .replace("@GAINS@", json.dumps([list(g) for g in GAINS]))
              .replace("@DIRS@", json.dumps([list(d) for d in DIRS])))

    # Isolate this Blender from the artist's OWN install. Without it the

    # `addon_install` in the script above overwrites the addon they are

    # clicking, and `addon_enable` then grades that copy rather than this

    # tree. `--factory-startup` does NOT do this -- see `blender_env`.

    sys.path.insert(0, str(Path(__file__).resolve().parent))

    from blender_env import isolated_env

    proc = subprocess.run([BLENDER, "--background", "--factory-startup",
                           "--python", str(script)],
                          capture_output=True, text=True,
                          env=isolated_env())
    if not report_path.exists():
        sys.stdout.write(proc.stdout[-4000:])
        sys.stdout.write("\n[stderr]\n" + proc.stderr[-3000:])
        check("blender_wrote_a_report", False, "the scene leg died")
        return verdict()
    r = json.loads(report_path.read_text())
    if r["errors"]:
        for e in r["errors"]:
            print("  blender: " + e)
        check("the_scene_leg_completed", False, "; ".join(r["errors"]))
        return verdict()

    print(f"exposed on states {r['overrides']}")
    for line in r.get("export_lines", []):
        print(f"  export: {line}")

    out = out_dir / f"{name}.json"
    check("export_finished", r["export_result"] == ["FINISHED"],
          str(r["export_result"]))

    # --- 5. NOT EXPORTED (warned, not refused) ----------------------------
    # Graded here, off the report lines alone, and BEFORE anything that needs
    # the written document: a seed that turns this warning into a refusal stops
    # the export writing at all, and these two are the checks that name why.
    warned = [l for l in r.get("export_lines", []) if "preview-only" in l
              or "NOT exported" in l]
    check("unwritable_overrides_warned", len(warned) == 2,
          f"{len(warned)} warning(s) for 2 unwritable Overrides")
    check("nothing_refused",
          not any(l.startswith("REFUSE") for l in r.get("export_lines", [])),
          "an Override on a borrowing state must not fail the export")
    check("export_wrote_a_document", out.exists(), str(out))
    if not out.exists():
        return verdict()
    doc = json.loads(out.read_text())
    states = doc["map_states"]
    typed = r["typed"]

    # --- 1. DECLARED ------------------------------------------------------
    declared = [i for i, st in enumerate(states) if schema.AUTHORED_RIG in st]
    check("declared_on_the_writable_state_only", declared == [RIG_STATE],
          f"declared on {declared}, expected [{RIG_STATE}]")
    check("stamps_version_2", doc["version"] == schema.AUTHORED_RIG_VERSION,
          f"version={doc['version']}")

    if declared != [RIG_STATE]:
        # Everything below reads the authored rig, so there is nothing left to
        # grade -- but the SUMMARY still prints. A harness that walks off the
        # end quietly is indistinguishable from one that never started, and a
        # grader keyed on "did it report" then scores the silence as a catch.
        return verdict()
    authored = states[RIG_STATE][schema.AUTHORED_RIG]

    # --- 2. GRADIENT ------------------------------------------------------
    base_state = base_doc["map_states"][RIG_STATE]
    own_gradient = base_state["light_rig"]["gradient"]
    check("the_overrides_gradient_is_not_the_states",
          typed["gradient"] != own_gradient,
          "the seed arm is inert: the Override carries the state's own gradient")
    check("gradient_echoes_the_states_own",
          authored["gradient"] == own_gradient,
          f"{authored['gradient']} != {own_gradient}")
    check("the_rig_is_not_just_the_roms",
          authored["ambient"] != base_state["light_rig"]["ambient"],
          "export promoted the ROM's rig unchanged -- the edit did not survive")

    # --- 3. ON DISC -------------------------------------------------------
    # The export rewrites only the sheets it repainted, and this run repaints
    # none -- so the sidecars a `build` needs are still the ones `dump` wrote.
    for sidecar in doc_dir.glob("*.png"):
        target = out_dir / sidecar.name
        if not target.exists():
            target.write_bytes(sidecar.read_bytes())
    # A `build` refusal is a FINDING, not a crash: several of this harness's own
    # seeds produce one, and letting it propagate would end the run before its
    # verdict -- which the audit then scores as a broken harness rather than as
    # the check that found the defect.
    try:
        bundle = build(doc, map_dir, sidecar_dir=out_dir)
    except BuildRefusal as e:
        check("build_accepts_the_exported_document", False, str(e))
        return verdict()
    check("build_accepts_the_exported_document", True)
    resource = states[RIG_STATE]["resource"]
    data = bundle.resources[resource]
    offset = mapfile.light_rig_offset(data, True)
    check("the_45_bytes_are_the_authored_rig",
          data[offset:offset + mapfile.LIGHT_RIG_BYTES]
          == mapfile.pack_light_rig(authored))
    written = mapfile.read_light_rig(data, True)
    check("ambient_is_byte_exact", written["ambient"] == authored["ambient"],
          f"{written['ambient']} != {authored['ambient']}")
    check("ambient_is_what_the_artist_typed",
          written["ambient"] == [int(round(c * 255.0)) for c in AMBIENT],
          f"{written['ambient']} vs {[int(round(c * 255.0)) for c in AMBIENT]}")
    check("gains_are_byte_exact", written["colors"] == authored["colors"],
          f"{written['colors']} != {authored['colors']}")

    moved = {n for n, blob in bundle.resources.items()
             if blob != (map_dir / n).read_bytes()}
    check("only_that_resource_moved", moved == {resource}, str(sorted(moved)))
    base_bytes = (map_dir / resource).read_bytes()
    differing = {i for i, (a, b) in enumerate(zip(base_bytes, data)) if a != b}
    span = set(range(offset, offset + mapfile.LIGHT_RIG_BYTES))
    check("only_those_45_bytes_moved", differing and differing <= span,
          f"{len(differing)} byte(s) differ, {len(differing - span)} outside the rig")

    # --- 4. PICTURE -------------------------------------------------------
    worst = 0.0
    for k in range(3):
        worst = max(worst, angle_between(written["directions"][k],
                                         typed["directions"][k]))
    check("directions_hold_the_picture", worst < 0.05,
          f"worst {worst:.4f} deg between the written direction and the aim")

    # --- 6. REOPENED ------------------------------------------------------
    check("reimport_accepts_the_v2_document",
          r.get("reimport_result") == ["FINISHED"], str(r.get("reimport_result")))
    # Reopening exposes the rig on every state, as any import does.  What must
    # NOT come back is a DIRTY one: an Override that read as edited would be
    # re-emitted through `override_rig`, whose directions do not survive the
    # unit vector, and the artist's lighting would drift a little every time
    # they opened the file.  Clean means the authored rig is CARRIED out of the
    # document's own `map_states` snapshot instead, byte for byte -- which is
    # what `the_v2_document_is_a_fixed_point` below then proves end to end.
    check("reimport_exposes_every_state",
          len(r.get("reimport_overrides") or []) == len(states),
          f"{r.get('reimport_overrides')} for {len(states)} states")
    check("reimport_seeds_no_dirty_override", r.get("reimport_dirty") == [],
          f"{r.get('reimport_dirty')} came back reading as edited -- an "
          f"Override re-seeded from an authored rig re-emits its directions "
          f"and breaks the identity")
    again = out_dir2 / f"{name}.json"
    check("the_v2_document_is_a_fixed_point",
          again.exists() and json.loads(again.read_text()) == doc,
          "export(import(v2 doc)) != the v2 doc -- reopening loses the rig")

    return verdict()


if __name__ == "__main__":
    sys.exit(main())
