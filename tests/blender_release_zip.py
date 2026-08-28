"""Does the RELEASE ZIP install, and is the thing that registers the zip?

`tools/make_addon_zip.py` builds the artifact a GitHub release carries. Every
other Blender suite here zips the addon its own way and installs that, so none
of them grades the released file — a zip that drops `_vendor/` still installs,
still enables, still registers all 25 idnames, and only fails later when the
artist picks a `MAP###.GNS` and the import raises. So this suite grades the
shipped artifact specifically, and the checks are chosen for what a bad zip
would still let pass:

- the zip's ROOT is one `exmateria_map/` directory holding `__init__.py`.
  A zip of loose modules installs a directory named after the FILE and the
  package's relative imports break;
- no `__pycache__`, no `*.pyc`, no `CLAUDE.md` rides along;
- every `.py` in the source tree is present — an over-broad exclude is the
  failure the shape checks above cannot see;
- two builds of one tree are byte-identical (the builder is deterministic, so
  a re-upload is recognisable as one);
- Blender installs it, enables it, and the module it imports resolves UNDER
  THE SCRATCH RESOURCES DIR — not the repo. Without that arm the whole suite
  can pass against a symlinked dev install (`tools/dev_install.sh`) and say
  nothing about the zip;
- the four menu entry points and the vendored `build` module are reachable
  from the installed copy.

Isolated per `tests/blender_env.py`: this suite INSTALLS, so with no isolation
it would overwrite the artist's own addon.

Run:  python3 tests/blender_release_zip.py [blender-binary]
"""
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
#: `EXMATERIA_ADDON_DIR` re-points the SUBJECT, so a seeded audit can hand
#: this suite a deliberately broken copy of the tree and read what fails.
ADDON_DIR = Path(os.environ.get("EXMATERIA_ADDON_DIR",
                                PKG / "addons" / "exmateria_map")).resolve()
BLENDER = sys.argv[1] if len(sys.argv) > 1 else "blender"

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(PKG / "tools"))
from blender_env import isolated_env          # noqa: E402
import make_addon_zip                         # noqa: E402

# What the artist clicks, and the vendored leg that makes GNS work at all.
ENTRY_POINTS = ["import_map.gns", "export_map.bundle",
                "import_map.document", "export_map.document"]

PROBE = r'''
import json, sys, traceback
import addon_utils, bpy

# Every step is guarded and the JSON is written no matter what. An enable that
# RAISES -- a broken __init__, a missing _vendor -- would otherwise take the
# whole probe down with it, and the suite could then only report "Blender ran
# the probe", which names the harness instead of the defect.
out = {"errors": []}


def step(key, fn):
    try:
        out[key] = fn()
    except Exception:
        out[key] = None
        out["errors"].append("%s: %s" % (key, traceback.format_exc(limit=4)))


# `addon_install` returns {'CANCELLED'} in -b while succeeding, so its return
# is recorded and never graded.
step("install_result", lambda: str(bpy.ops.preferences.addon_install(
    filepath=r"@ZIP@", overwrite=True)))
step("enabled", lambda: bool(addon_utils.enable(
    "exmateria_map", default_set=True, persistent=True))
    and "exmateria_map" in bpy.context.preferences.addons)
step("module_file", lambda: getattr(sys.modules.get("exmateria_map"), "__file__", None))

ops = {}
for idname in @ENTRIES@:
    group, _, name = idname.partition(".")
    ops[idname] = hasattr(getattr(bpy.ops, group, None), name)
out["ops"] = ops


def _vendor_build_file():
    from exmateria_map._vendor.exmateria_map import build as _b
    return _b.__file__


step("vendor_build_file", _vendor_build_file)

# An AddonPreferences lives in bpy.types under its bl_idname, not its class
# name, so ask the addon entry rather than bpy.types.
step("prefs", lambda: hasattr(
    bpy.context.preferences.addons["exmateria_map"], "preferences"))

json.dump(out, open(r"@OUT@", "w"))
print("PROBE_OK")
'''


def check(results, name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def main():
    results = []
    tmp = Path(tempfile.mkdtemp(prefix="exmateria-release-zip-"))
    print("Building the release zip …")
    zip_a = tmp / "a.zip"
    zip_b = tmp / "b.zip"
    make_addon_zip.build(zip_a, ADDON_DIR)
    make_addon_zip.build(zip_b, ADDON_DIR)

    names = zipfile.ZipFile(zip_a).namelist()
    roots = {n.split("/")[0] for n in names}
    check(results, "zip root is one exmateria_map/ directory",
          roots == {"exmateria_map"}, ", ".join(sorted(roots)))
    check(results, "exmateria_map/__init__.py is at that root",
          "exmateria_map/__init__.py" in names)
    check(results, "no __pycache__ / .pyc rides along",
          not [n for n in names if "__pycache__" in n or n.endswith((".pyc", ".pyo"))])
    check(results, "no CLAUDE.md rides along",
          not [n for n in names if n.endswith("CLAUDE.md")])

    src_py = {p.relative_to(ADDON_DIR).as_posix() for p in ADDON_DIR.rglob("*.py")
              if "__pycache__" not in p.parts}
    zip_py = {n[len("exmateria_map/"):] for n in names if n.endswith(".py")}
    missing = sorted(src_py - zip_py)
    check(results, "every source .py is in the zip",
          not missing, f"{len(src_py)} files" if not missing else f"missing {missing}")
    check(results, "pcsx_handlers.lua is in the zip",
          "exmateria_map/pcsx_handlers.lua" in names)
    check(results, "the vendored library is in the zip",
          "exmateria_map/_vendor/exmateria_map/build.py" in names)
    check(results, "two builds are byte-identical",
          zip_a.read_bytes() == zip_b.read_bytes())

    print("Installing it into an isolated Blender …")
    out_json = tmp / "probe.json"
    script = (PROBE.replace("@ZIP@", str(zip_a))
                   .replace("@OUT@", str(out_json))
                   .replace("@ENTRIES@", repr(ENTRY_POINTS)))
    script_path = tmp / "probe.py"
    script_path.write_text(script)
    env = isolated_env("blender_release_zip")
    # A fresh scripts/addons each run: an install that silently did nothing
    # would otherwise pass against the PREVIOUS run's copy.
    res_root = Path(env["BLENDER_USER_RESOURCES"])
    installed = res_root / "scripts" / "addons" / "exmateria_map"
    if installed.exists():
        subprocess.run(["rm", "-rf", str(installed)], check=True)
    proc = subprocess.run([BLENDER, "--background", "--factory-startup",
                           "--python", str(script_path)],
                          capture_output=True, text=True, env=env, timeout=600)
    if not out_json.exists():
        print(proc.stdout[-4000:])
        print(proc.stderr[-2000:], file=sys.stderr)
        check(results, "Blender ran the probe", False, "no probe.json written")
        probe = {}
    else:
        probe = json.loads(out_json.read_text())
        # The operator returns {'CANCELLED'} in -b while succeeding, so grade
        # the install by what registered, never by a return set.
        check(results, "the addon enables", probe.get("enabled"))
        mod_file = probe.get("module_file") or ""
        check(results, "the module that registered is the INSTALLED copy",
              str(res_root) in mod_file, mod_file)
        for idname in ENTRY_POINTS:
            check(results, f"operator {idname} registered",
                  probe.get("ops", {}).get(idname))
        vb = probe.get("vendor_build_file") or ""
        check(results, "the vendored build module imports from the install",
              str(res_root) in vb, vb)
        check(results, "addon preferences registered", probe.get("prefs"))
        for err in probe.get("errors", []):
            print("    (in Blender) " + err.replace(chr(10), chr(10) + "      "))

    failed = [n for n, ok, _ in results if not ok]
    print()
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
