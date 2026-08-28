"""ADR-0004 decision 31: a GNS goes in and a bundle comes out, without a CLI.

The tree-identity guard proves the vendored copy MATCHES; it says nothing about
whether an artist can now do the thing decision 31 exists for. This harness
drives the real operators in a real Blender against a real disc tree.

The map is **MAP011**, chosen deliberately: it names six arrangements and dumps
five, so the dropdown is exercised. 101 of 121 maps offer exactly one, and on
every one of those a dropdown hardcoded to zero would pass.

Run:  python3 tests/blender_gns_bundle.py [blender-binary]
"""
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
ADDON_DIR = PKG / "addons" / "exmateria_map"
TMP = Path(__file__).resolve().parent / ".blender_gns"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPORT = TMP / "report.json"

sys.path.insert(0, str(PKG))
from exmateria_map import build, corpus, dump, mapfile   # noqa: E402

MAP = 11
PICKED = 3          # geometry differs from a0
SAME_GEOMETRY = 1   # a1: a0's geometry exactly, a different state count

SCRIPT_TEMPLATE = r'''
import json
import os
import sys
import traceback

import bpy

ZIP = "@ZIP@"
OUT = "@OUT@"
FACTS = json.loads(r"""@FACTS@""")

try:
    bpy.ops.preferences.addon_install(filepath=ZIP)
except Exception as e:
    print(f"INSTALL: {e}")
bpy.ops.preferences.addon_enable(module='exmateria_map')

checks = {}


def check(n, cond, detail=""):
    checks[n] = bool(cond)
    print(("OK   " if cond else "FAIL ") + n + ("" if cond else f": {detail}"))


def finish():
    json.dump({"checks": checks}, open(OUT, "w"), indent=1)
    print(f"CHECKS: {sum(checks.values())}/{len(checks)} passed")
    if not all(checks.values()):
        raise SystemExit(1)
    raise SystemExit(0)


def clear():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)


def marker():
    for o in bpy.data.objects:
        if "exmateria_map/base" in o.keys():
            return o
    return None


def import_gns(path, arrangement=None):
    """Returns (result, error-string)."""
    kw = {"filepath": str(path)}
    if arrangement is not None:
        kw["arrangement"] = str(arrangement)
    try:
        return bpy.ops.import_map.gns(**kw), ""
    except RuntimeError as e:            # self.report({"ERROR"}) -- a refusal
        return {"CANCELLED"}, str(e)
    except TypeError as e:               # the enum never offered that value
        return {"REJECTED"}, str(e)


# --- 1. the operator exists at all --------------------------------------
# `bpy.ops` resolves attributes lazily, so `hasattr` is True for every name
# that was ever spelled.  The registered class is the only honest probe.
have = hasattr(bpy.types, "IMPORT_MAP_OT_gns")
check("the_gns_import_operator_is_registered", have,
      "File > Import cannot take a GNS; decision 31 part 2 is not built")
if not have:
    finish()

# --- 2. nothing asks for a disc tree or a map number ---------------------
props = set(bpy.ops.import_map.gns.get_rna_type().properties.keys())
check("the_operator_asks_for_neither_the_disc_tree_nor_the_number",
      not (props & {"map_dir", "directory_map", "number", "map_number"}),
      f"the picked path IS the address; operator still asks: {sorted(props)}")

# --- 3. a GNS import builds a marker -------------------------------------
clear()
res, err = import_gns(FACTS["gns"], FACTS["picked"])
check("a_gns_import_finishes", res == {"FINISHED"}, err)
ob = marker()
check("a_gns_import_builds_a_marker", ob is not None,
      "no object carries `exmateria_map/base`")
if ob is None:
    finish()

# --- 4. the picked arrangement is the one that got imported --------------
picked = FACTS["arrangements"][str(FACTS["picked"])]
zero = FACTS["arrangements"][str(FACTS["zero"])]
check("the_picked_arrangement_is_the_one_imported",
      len(ob.data.polygons) == picked["polygons"],
      f"scene has {len(ob.data.polygons)} faces, a{FACTS['picked']} has "
      f"{picked['polygons']}, a{FACTS['zero']} has {zero['polygons']}")
check("the_dropdown_is_not_hardcoded_to_zero",
      len(ob.data.polygons) != zero["polygons"],
      "the scene carries arrangement 0's geometry")
check("the_marker_records_the_picked_arrangement",
      json.loads(ob["exmateria_map/base"])["arrangement"] == FACTS["picked"])

# --- 5. the GNS is remembered, because export needs it -------------------
check("the_marker_remembers_the_gns_path",
      ob.get("exmateria_map/gns_path") == FACTS["gns"],
      f"got {ob.get('exmateria_map/gns_path')!r}")

# --- 6/7. the control offers the DUMPABLE arrangements, and ONLY those ----
#     Driven rather than read: a dynamic enum's items depend on the picked
#     path, so the RNA type's `enum_items` outside a pick reports the fallback,
#     not what the artist would see.  Every NAMED arrangement is offered to the
#     operator; the ones that carry no primary mesh must not get through.
accepted, leaked, blocked = [], [], []
for a in FACTS["named"]:
    clear()
    res, err = import_gns(FACTS["gns"], a)
    built = marker() is not None
    if res == {"FINISHED"} and built:
        accepted.append(a)
        if a not in FACTS["dumpable"]:
            leaked.append(a)
    elif a in FACTS["dumpable"]:
        blocked.append((a, err[:120]))
check("every_dumpable_arrangement_can_be_imported", not blocked, str(blocked))
check("no_arrangement_without_geometry_can_be_imported", not leaked,
      f"{leaked} carry no primary mesh and imported anyway")
check("only_the_dumpable_arrangements_import",
      accepted == FACTS["dumpable"],
      f"accepted {accepted}, dumpable {FACTS['dumpable']}, "
      f"named {FACTS['named']}")

# --- 7b. what the CONTROL offers, not what the operator survives ----------
#     The three checks above are satisfied whether the dropdown filters or
#     `dump` refuses downstream -- seeded by wiring the items callback to
#     `arrangements()` AND deleting the operator's guard, and all three stayed
#     green.  Decision 31 part 3 is about the CONTROL: an entry the artist can
#     pick and that then refuses is the defect, and only reading the items the
#     enum draws can see it.
addon = sys.modules.get("exmateria_map")
gns_bundle = getattr(addon, "gns_bundle", None)
if gns_bundle is None:
    for k, m in list(sys.modules.items()):
        if k.endswith("gns_bundle"):
            gns_bundle = m
            break


class _Picked:
    filepath = FACTS["gns"]


try:
    drawn = sorted(int(i[0]) for i in
                   gns_bundle.arrangement_items(_Picked(), bpy.context))
except Exception as e:
    drawn = f"could not read the enum items: {e}"
check("the_dropdown_draws_exactly_the_dumpable_arrangements",
      drawn == FACTS["dumpable"],
      f"the sidebar would offer {drawn}; dumpable is {FACTS['dumpable']} and "
      f"named is {FACTS['named']}")

# --- 8. two arrangements can share geometry and differ in STATES ---------
#     a{same} is a{zero}'s geometry byte for byte, with a different state
#     count.  An operator that ignored the pick between them would pass every
#     geometry check above.
clear()
res, err = import_gns(FACTS["gns"], FACTS["same_geometry"])
ob = marker()
same = FACTS["arrangements"][str(FACTS["same_geometry"])]
check("an_arrangement_is_more_than_its_geometry",
      ob is not None
      and len(json.loads(ob["exmateria_map/map_states"])) == same["states"],
      f"expected {same['states']} states for a{FACTS['same_geometry']}, got "
      f"{None if ob is None else len(json.loads(ob['exmateria_map/map_states']))}")

# --- 9. a bad pick is a named refusal ------------------------------------
clear()
res, err = import_gns(FACTS["resource"])
check("picking_a_resource_file_instead_of_the_gns_is_refused",
      res == {"CANCELLED"} and marker() is None,
      f"{FACTS['resource']} is not a GNS; got {res}")


# =========================================================================
# decision 31 part 4: File > Export writes a BUNDLE
# =========================================================================

def sha(path):
    import hashlib
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def export_bundle(directory, **kw):
    try:
        return bpy.ops.export_map.bundle(directory=str(directory), **kw), ""
    except RuntimeError as e:
        return {"CANCELLED"}, str(e)
    except TypeError as e:
        return {"REJECTED"}, str(e)


have = hasattr(bpy.types, "EXPORT_MAP_OT_bundle")
check("the_bundle_export_operator_is_registered", have,
      "File > Export cannot write a bundle; decision 31 part 4 is not built")
if not have:
    finish()

props = set(bpy.types.EXPORT_MAP_OT_bundle.bl_rna.properties.keys())
check("the_export_asks_for_neither_the_disc_tree_nor_the_number",
      not (props & {"map_dir", "number", "map_number", "arrangement"}),
      f"the remembered GNS is the address; operator asks: {sorted(props)}")

# --- 10. an untouched import exports a bundle, with no CLI anywhere -------
clear()
res, err = import_gns(FACTS["gns"], FACTS["picked"])
if res != {"FINISHED"}:
    check("import_before_export_worked", False, err)
    finish()
out = os.path.join(FACTS["out"], "bundle")
res, err = export_bundle(out)
check("an_untouched_gns_import_exports_a_bundle", res == {"FINISHED"}, err)

written = sorted(os.listdir(out)) if os.path.isdir(out) else []
expected = FACTS["cli_bundles"][str(FACTS["picked"])]
check("the_bundle_holds_the_gns_and_one_blob_per_non_pad_resource",
      written == sorted(expected),
      f"wrote {written}, the CLI writes {sorted(expected)}")

# --- 11. the GNS is carried VERBATIM -------------------------------------
gns_name = os.path.basename(FACTS["gns"])
copied = os.path.join(out, gns_name)
check("the_bundle_carries_the_gns_verbatim",
      os.path.isfile(copied) and sha(copied) == FACTS["gns_sha"],
      "the GNS in the bundle is not the disc's GNS byte for byte")

# --- 12. the bundle IS what the CLI builds -------------------------------
#     The acceptance gate.  A tree-identity test proves the vendored copy
#     matches; only this proves an artist can do the job the copy exists for,
#     and get the same bytes `exmateria-map-dump | exmateria-map-build` would
#     have produced from the same untouched map.
differing = [n for n, digest in sorted(expected.items())
             if not os.path.isfile(os.path.join(out, n))
             or sha(os.path.join(out, n)) != digest]
check("the_bundle_is_byte_for_byte_what_the_cli_builds", not differing,
      f"{len(differing)} of {len(expected)} resource(s) differ: {differing}")

# --- 12b. the same gate over an EIGHT-resource arrangement ----------------
#     a{picked} carries one resource; a bundle that dropped or corrupted a
#     blob in a multi-resource map would not be visible above.
clear()
res, err = import_gns(FACTS["gns"], FACTS["zero"])
out0 = os.path.join(FACTS["out"], "bundle_a0")
res0, err0 = (export_bundle(out0) if res == {"FINISHED"}
              else ({"CANCELLED"}, err))
check("a_multi_resource_arrangement_exports_a_bundle",
      res0 == {"FINISHED"}, err0)
expected0 = FACTS["cli_bundles"][str(FACTS["zero"])]
written0 = sorted(os.listdir(out0)) if os.path.isdir(out0) else []
check("every_resource_of_a_multi_resource_arrangement_is_written",
      written0 == sorted(expected0),
      f"wrote {written0}, the CLI writes {sorted(expected0)}")
differing0 = [n for n, digest in sorted(expected0.items())
              if not os.path.isfile(os.path.join(out0, n))
              or sha(os.path.join(out0, n)) != digest]
check("all_eight_resources_are_byte_for_byte_what_the_cli_builds",
      not differing0,
      f"{len(differing0)} of {len(expected0)} differ: {differing0}")

# --- 12c. the browsers reopen where the artist was ------------------------
#     `remember_dir` stores the PARENT of what it is handed, so both calls
#     depend on passing a path INSIDE the directory meant. Only `execute`
#     reaches this; `invoke` is GUI-only.
prefs = bpy.context.preferences.addons["exmateria_map"].preferences
check("an_import_remembers_the_disc_tree",
      bpy.path.abspath(getattr(prefs, "last_dir", "")).rstrip("/")
      == FACTS["map_dir"].rstrip("/"),
      f"last_dir is {getattr(prefs, 'last_dir', None)!r}, "
      f"the tree is {FACTS['map_dir']}")
check("an_export_remembers_the_folder_the_bundle_landed_in",
      bpy.path.abspath(getattr(prefs, "last_export_dir", "")).rstrip("/")
      == out0.rstrip("/"),
      f"last_export_dir is {getattr(prefs, 'last_export_dir', None)!r}, "
      f"the bundle went to {out0}")

# --- 13. a scene with no remembered GNS asks, it does not guess -----------
#     §31: "a scene imported from a document instead has no remembered GNS,
#     and export asks for one".  It must not fall back to a plausible tree.
clear()
try:
    bpy.ops.import_map.document(filepath=FACTS["document"])
except RuntimeError as e:
    print(f"document import: {e}")
ob = marker()
check("the_document_import_still_works", ob is not None)
check("a_document_import_carries_no_remembered_gns",
      ob is not None and ob.get("exmateria_map/gns_path") is None)
out2 = os.path.join(FACTS["out"], "no_base")
res, err = export_bundle(out2)
check("a_scene_with_no_remembered_gns_refuses_to_export",
      res != {"FINISHED"}, "a bundle was built against a guessed base map")
check("the_refusal_writes_nothing",
      not os.path.isdir(out2) or not os.listdir(out2),
      f"{out2} is not empty after a refusal")

# --- 14. ...and a GNS handed to it is accepted and remembered -------------
res, err = export_bundle(out2, gns_path=FACTS["document_gns"])
check("a_gns_supplied_at_export_time_is_enough", res == {"FINISHED"}, err)
check("the_supplied_gns_is_remembered_for_next_time",
      marker() is not None
      and marker().get("exmateria_map/gns_path") == FACTS["document_gns"],
      "the artist would be asked again on the next export")

# The document route and the GNS route must land on the SAME bytes. They read
# different things -- one a JSON on disk, one the disc -- and decision 31 only
# changes packaging, not the format, so a difference here would mean the two
# entry points had become two formats.
differing2 = [n for n, digest in sorted(expected.items())
              if not os.path.isfile(os.path.join(out2, n))
              or sha(os.path.join(out2, n)) != digest]
check("a_document_scene_exports_the_same_bundle_as_a_gns_scene",
      not differing2,
      f"{len(differing2)} of {len(expected)} differ: {differing2}")

finish()
'''


def facts():
    """Everything the in-Blender script is graded against, computed OUT here by
    the package -- the independent oracle. A check that recomputed the expected
    face count the way the addon does could not disagree with it."""
    map_dir = corpus.map_dir()
    if map_dir is None:
        print("FAIL: no extracted disc tree; set EXMATERIA_ASSETS_DIR")
        sys.exit(1)
    gns = map_dir / f"MAP{MAP:03d}.GNS"
    dumpable = dump.dumpable_arrangements(map_dir, MAP)
    named = dump.arrangements(map_dir, MAP)
    chunkless = sorted(set(named) - set(dumpable))
    if not chunkless or len(dumpable) < 2:
        print(f"FAIL: MAP{MAP:03d} no longer exercises the dropdown "
              f"(named {named}, dumpable {dumpable})")
        sys.exit(1)
    per = {}
    for a in dumpable:
        doc, _sheets = dump.dump(map_dir, MAP, a)
        blob = json.dumps([p["positions"] for p in doc["polygons"]],
                          separators=(",", ":")).encode()
        per[str(a)] = {"polygons": len(doc["polygons"]),
                       "states": len(doc["map_states"]),
                       "geometry": hashlib.sha256(blob).hexdigest()}
    files = mapfile.bind(map_dir, MAP)

    # The oracle for the export leg: what `exmateria-map-dump |
    # exmateria-map-build` produces for the same map and arrangement,
    # untouched. Computed by the PACKAGE, out here, so a check cannot agree
    # with the addon by recomputing the addon's own answer.
    cli_bundles, doc_path = {}, None
    for a in (dumpable[0], PICKED):
        out = TMP / f"cli_bundle_a{a}"
        scratch = TMP / f"cli_dump_a{a}"
        shutil.rmtree(out, ignore_errors=True)
        shutil.rmtree(scratch, ignore_errors=True)
        written = dump.write_bundle(map_dir, MAP, a, scratch)
        build.build_bundle(written, map_dir, out)
        cli_bundles[str(a)] = {f.name: hashlib.sha256(f.read_bytes()).hexdigest()
                               for f in sorted(out.iterdir())}
        if a == PICKED:
            doc_path = written
    # The picked arrangement is the one the dropdown checks need, but it holds
    # ONE resource -- a byte-identity gate over a single blob is thin, and a
    # bundle that dropped a resource would be the same size as a correct one.
    # a0 carries eight, so the gate runs on both.
    if len(cli_bundles[str(dumpable[0])]) < 5:
        print(f"FAIL: a{dumpable[0]} no longer carries enough resources to "
              f"make the byte-identity gate meaningful")
        sys.exit(1)

    # The document-imported scene, for the "no remembered GNS" leg, is the
    # document `dump` just wrote -- a REAL one, not the hand-cut stub fixture,
    # which names 2 of MAP001 a0's 21 resources and `build` refuses on sight.
    # It is the same (map, arrangement) as the GNS leg on purpose: any fallback
    # that guessed a base map at all would find the right one and succeed, so
    # the refusal check cannot pass by the fallback happening to be wrong.

    return {
        "out": str(TMP / "out"),
        "cli_bundles": cli_bundles,
        "gns_sha": hashlib.sha256(gns.read_bytes()).hexdigest(),
        "document": str(doc_path),
        "document_gns": str(gns),
        "gns": str(gns),
        "map_dir": str(map_dir),
        "resource": str(sorted(files.by_sector.values())[0]),
        "dumpable": dumpable,
        "named": named,
        "chunkless": chunkless[0],
        "zero": dumpable[0],
        "picked": PICKED,
        "same_geometry": SAME_GEOMETRY,
        "arrangements": per,
    }


def ensure_addon():
    TMP.mkdir(exist_ok=True)
    zf_path = TMP / "exmateria_map.zip"
    if zf_path.exists():
        zf_path.unlink()
    with zipfile.ZipFile(zf_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(ADDON_DIR.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                zf.write(f, f.relative_to(ADDON_DIR.parent))
    return zf_path


def main():
    TMP.mkdir(exist_ok=True)
    if REPORT.exists():
        REPORT.unlink()                      # never grade on a stale report
    shutil.rmtree(TMP / "out", ignore_errors=True)
    f = facts()
    assert f["picked"] in f["dumpable"], f
    assert f["same_geometry"] in f["dumpable"], f
    assert (f["arrangements"][str(f["picked"])]["geometry"]
            != f["arrangements"][str(f["zero"])]["geometry"]), \
        "the picked arrangement no longer differs from a0; the dropdown check is blind"
    assert (f["arrangements"][str(f["same_geometry"])]["geometry"]
            == f["arrangements"][str(f["zero"])]["geometry"]), \
        "a{} no longer shares a0's geometry".format(f["same_geometry"])

    script = TMP / "run_check.py"
    script.write_text(SCRIPT_TEMPLATE
                      .replace("@ZIP@", str(ensure_addon()))
                      .replace("@OUT@", str(REPORT))
                      .replace("@FACTS@", json.dumps(f)))
    # Isolate this Blender from the artist's OWN install. Without it the
    # `addon_install` in the script above overwrites the addon they are
    # clicking, and `addon_enable` then grades that copy rather than this
    # tree. `--factory-startup` does NOT do this -- see `blender_env`.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from blender_env import isolated_env
    proc = subprocess.run(
        [sys.argv[1] if len(sys.argv) > 1 else "blender",
         "--background", "--factory-startup", "--python", str(script)],
        capture_output=True, text=True,
                          env=isolated_env())
    sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stdout.write("\n[stderr]\n" + proc.stderr[-4000:])
    if not REPORT.exists():
        print("\nFAIL: no report written")
        sys.exit(1)
    checks = json.loads(REPORT.read_text())["checks"]
    failed = [n for n, ok in checks.items() if not ok]
    print(f"\nSUMMARY: {len(checks) - len(failed)}/{len(checks)} checks passed")
    if failed:
        print("FAILED:", ", ".join(failed))
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
