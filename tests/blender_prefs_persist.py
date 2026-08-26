"""Does the browser's directory memory survive a RESTART?

`blender_roundtrip.py` cannot answer that and never could: it runs
`--factory-startup`, which hands every invocation a scratch user directory, so
its `import_remembers_dir` / `browser_opens_at_remembered` checks prove the
SETTER works and stop there.  A preference test that never crosses a process
boundary tests the setter, not the memory — and the whole reason this lives on
Preferences rather than the Scene is surviving a restart.

It shipped broken because of exactly that gap.  Measured here:

    assign the property                      -> reads back '' in a fresh Blender
    assign it + `preferences.is_dirty = True` -> reads back ''
    assign it + explicit `wm.save_userpref()` -> survives

So this harness runs REAL, SEPARATE Blender processes over a scratch
`BLENDER_USER_RESOURCES` tree — which isolates config AND scripts, so it never
touches the artist's own preferences or their installed addon — and grades what
the second process can still see.

Four properties, each with the arm that would otherwise let it pass hollow:

- the import memory survives a process;
- the guard is honoured: with `Auto-Save Preferences` OFF the memory does NOT
  persist, because the artist has said not to write preferences behind them;
- import and export keep SEPARATE memories, so exporting into a build directory
  does not move where the next import opens (which presents as "it forgot");
- `start_filepath` actually opens at the remembered directory, rather than at
  the filesystem root — the symptom that started this.

Run:  python3 tests/blender_prefs_persist.py [blender-binary]
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
ADDON_DIR = PKG / "addons" / "exmateria_map"
FIXTURES = PKG / "tests" / "fixtures"
FIXTURE = FIXTURES / "MAP001.a0.stub.json"
BLENDER = sys.argv[1] if len(sys.argv) > 1 else "blender"

SETUP = r'''
import addon_utils, bpy
bpy.ops.preferences.addon_install(filepath=r"@ZIP@", overwrite=True)
addon_utils.enable("exmateria_map", default_set=True, persistent=True)
bpy.context.preferences.use_preferences_save = True
bpy.ops.wm.save_userpref()
print("SETUP_OK")
'''

IMPORT = r'''
import addon_utils, bpy
addon_utils.enable("exmateria_map", default_set=True, persistent=True)
bpy.context.preferences.use_preferences_save = @AUTOSAVE@
res = bpy.ops.import_map.document(filepath=r"@DOC@")
print("IMPORT_RESULT", res)
'''

EXPORT = r'''
import addon_utils, bpy
addon_utils.enable("exmateria_map", default_set=True, persistent=True)
bpy.context.preferences.use_preferences_save = True
bpy.ops.import_map.document(filepath=r"@DOC@")
ob = bpy.data.objects.get("MAP001.a0")
bpy.context.view_layer.objects.active = ob
res = bpy.ops.export_map.document(filepath=r"@OUT@")
print("EXPORT_RESULT", res)
'''

READ = r'''
import addon_utils, bpy, json
addon_utils.enable("exmateria_map", default_set=True, persistent=True)
p = bpy.context.preferences.addons["exmateria_map"].preferences
from exmateria_map import import_document as imp
from exmateria_map import export_document as exp
json.dump({"last_dir": p.last_dir, "last_export_dir": p.last_export_dir,
           "start_filepath": imp.start_filepath(bpy.context),
           "start_directory": exp.start_directory(bpy.context)},
          open(r"@OUT@", "w"))
print("READ_OK")
'''


def zip_addon(tmp):
    z = Path(tmp) / "exmateria_map.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(ADDON_DIR.rglob("*")):
            if f.is_file() and "__pycache__" not in str(f):
                zf.write(f, f.relative_to(ADDON_DIR.parent))
    return z


def run(resources, script, tmp, name):
    path = Path(tmp) / f"{name}.py"
    path.write_text(script)
    env = dict(os.environ, BLENDER_USER_RESOURCES=str(resources))
    p = subprocess.run([BLENDER, "--background", "--python", str(path)],
                       capture_output=True, text=True, env=env, timeout=600)
    return p.stdout


def stage(directory):
    """A document plus its sidecars, so the import really succeeds."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    (d / FIXTURE.name).write_text(FIXTURE.read_text())
    for st in json.loads(FIXTURE.read_text())["map_states"]:
        s = st.get("texture_sheet")
        if s and (FIXTURES / s).exists():
            (d / s).write_bytes((FIXTURES / s).read_bytes())
    return d / FIXTURE.name


def main():
    checks, detail = {}, {}

    def check(name, cond, why=""):
        checks[name] = bool(cond)
        if not cond:
            print(f"CHECK FAIL {name}: {why}")

    with tempfile.TemporaryDirectory(prefix="exmateria-prefs-") as tmp:
        res = Path(tmp) / "resources"
        res.mkdir()
        zip_path = zip_addon(tmp)
        out = Path(tmp) / "read.json"

        def read():
            run(res, READ.replace("@OUT@", str(out)), tmp, "read")
            return json.loads(out.read_text()) if out.exists() else {}

        print(run(res, SETUP.replace("@ZIP@", str(zip_path)), tmp,
                  "setup").count("SETUP_OK") and "setup ok" or "SETUP FAILED")

        # --- the memory survives a process -----------------------------------
        first = stage(Path(tmp) / "docs_one")
        run(res, IMPORT.replace("@DOC@", str(first)).replace("@AUTOSAVE@", "True"),
            tmp, "import_one")
        got = read()
        check("import_memory_survives_a_process",
              got.get("last_dir", "").rstrip("/") == str(first.parent),
              f"{got.get('last_dir')!r} != {str(first.parent)!r}")
        # The symptom that started this: a lost memory opens the browser at the
        # filesystem ROOT, because `scene.render.filepath` is /tmp/ and its
        # parent is /.
        check("browser_opens_at_the_remembered_directory",
              got.get("start_filepath", "").rstrip("/") == str(first.parent),
              f"{got.get('start_filepath')!r}")
        check("browser_does_not_open_at_root",
              got.get("start_filepath") not in ("/", "/interchange.json"),
              f"{got.get('start_filepath')!r}")

        # --- the guard: auto-save OFF must NOT persist ------------------------
        second = stage(Path(tmp) / "docs_two")
        run(res, IMPORT.replace("@DOC@", str(second)).replace("@AUTOSAVE@", "False"),
            tmp, "import_two")
        got = read()
        check("autosave_off_does_not_persist",
              got.get("last_dir", "").rstrip("/") == str(first.parent),
              f"an import wrote preferences with Auto-Save Preferences OFF: "
              f"{got.get('last_dir')!r}")

        # --- import and export keep separate memories -------------------------
        outdir = Path(tmp) / "built"
        outdir.mkdir()
        run(res, EXPORT.replace("@DOC@", str(first)).replace("@OUT@", str(outdir)),
            tmp, "export")
        got = read()
        check("export_memory_survives_a_process",
              got.get("last_export_dir", "").rstrip("/") == str(outdir),
              f"{got.get('last_export_dir')!r} != {str(outdir)!r}")
        check("export_did_not_move_the_import_memory",
              got.get("last_dir", "").rstrip("/") == str(first.parent),
              f"exporting moved where the next IMPORT opens, to "
              f"{got.get('last_dir')!r}")
        check("export_browser_opens_at_the_export_directory",
              got.get("start_directory", "").rstrip("/") == str(outdir),
              f"{got.get('start_directory')!r}")
        detail["read"] = got

    failed = [n for n, ok in checks.items() if not ok]
    print(f"\nSUMMARY: {len(checks) - len(failed)}/{len(checks)} checks passed")
    if failed:
        print("FAILED:", ", ".join(failed))
        print(json.dumps(detail, indent=1))
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
