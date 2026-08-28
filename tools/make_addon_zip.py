#!/usr/bin/env python3
"""Build the installable Blender addon zip — the artifact a release carries.

Blender installs a legacy `bl_info` addon from a zip whose ROOT holds one
directory with an `__init__.py` in it. So the arcnames here are
`exmateria_map/...`, never the files bare: a zip of loose modules installs a
directory named after the ZIP and the package's own relative imports break.

What is left out is as much of the point as what goes in:

- `__pycache__/` and `*.pyc` — bytecode compiled by whatever Python last read
  this tree. Blender's is 3.14 here, but a shipped `.pyc` for the wrong
  version is at best ignored and at worst runs stale code, which is the
  failure `tools/dev_install.sh` exists to prevent.
- `CLAUDE.md` — the agent-facing working notes. 52 KB of monorepo-internal
  detail in an artist's addon folder.

Deterministic on purpose: sorted entries and a fixed timestamp, so two builds
of the same tree are byte-identical and a re-upload can be recognised as a
re-upload.

    python tools/make_addon_zip.py                 # -> dist/exmateria_map-0.1.0.zip
    python tools/make_addon_zip.py --out /tmp/a.zip
"""
import argparse
import ast
import sys
import zipfile
from pathlib import Path

ADDON_DIR = Path(__file__).resolve().parent.parent / "addons" / "exmateria_map"

#: Matched against each path part (directories included) and against the file
#: name. Anything hit is dropped, with its whole subtree.
EXCLUDE_NAMES = {"__pycache__", "CLAUDE.md", "AGENTS.md", ".DS_Store"}
EXCLUDE_SUFFIXES = (".pyc", ".pyo")

#: Fixed zip timestamp (1980-01-01, the zip epoch) so the build is reproducible.
EPOCH = (1980, 1, 1, 0, 0, 0)


def addon_version(addon_dir=ADDON_DIR):
    """The `bl_info["version"]` tuple, read as source rather than imported.

    Importing means importing `bpy`, which exists only inside Blender. The
    addon's own `__init__` starts with `import bpy`, so this parses instead.
    """
    tree = ast.parse((addon_dir / "__init__.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "bl_info" for t in node.targets):
            info = ast.literal_eval(node.value)
            return ".".join(str(n) for n in info["version"])
    raise SystemExit(f"no bl_info version in {addon_dir / '__init__.py'}")


def wanted(path, addon_dir):
    rel = path.relative_to(addon_dir)
    if any(part in EXCLUDE_NAMES for part in rel.parts):
        return False
    if path.name.endswith(EXCLUDE_SUFFIXES):
        return False
    if any(part.startswith(".aside-") or ".aside-" in part for part in rel.parts):
        return False
    return True


def build(out_path, addon_dir=ADDON_DIR):
    files = sorted(p for p in addon_dir.rglob("*") if p.is_file() and wanted(p, addon_dir))
    if not any(p.relative_to(addon_dir).as_posix() == "__init__.py" for p in files):
        raise SystemExit(f"no __init__.py under {addon_dir} — that zip would not install")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            arc = Path("exmateria_map") / f.relative_to(addon_dir)
            info = zipfile.ZipInfo(arc.as_posix(), date_time=EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, f.read_bytes())
    return files


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=None,
                    help="output zip (default dist/exmateria_map-<version>.zip)")
    ap.add_argument("--addon-dir", type=Path, default=ADDON_DIR)
    args = ap.parse_args(argv)

    addon_dir = args.addon_dir.resolve()
    version = addon_version(addon_dir)
    out = args.out or (addon_dir.parent.parent / "dist" / f"exmateria_map-{version}.zip")
    files = build(out, addon_dir)
    size = out.stat().st_size
    print(f"{out}  ({len(files)} files, {size / 1024:.0f} KB, addon version {version})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
