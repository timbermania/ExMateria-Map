"""ExMateria Map — the Blender addon leg of the exmateria-map interchange.

Speaks only the interchange document (schema v1,
`exmateria-map/docs/interchange-schema-v1.md`) and its PNG sidecars; stdlib
`json`/`zlib` only.

Under ADR-0004 decision 31 it also vendors the whole `exmateria_map` package
at `_vendor/` -- guarded byte-for-byte by `tests/test_vendored_package.py` --
so a map can go from the extracted disc tree into a scene and back out as a
patcher bundle without a CLI trip. That leg lives in `gns_bundle`. Decision 6
(the interchange shape) and decision 7's other half are untouched.
"""
import bpy

from . import (authoring, compile_op, convert_op, export_document,
               gns_bundle, import_document, lighting_bake, live_link_ui,
               paint, workspace)

bl_info = {
    "name": "ExMateria Map",
    "author": "timbermania",
    "version": (0, 1, 0),
    "blender": (4, 0, 0),
    "category": "Import-Export",
}


def _say_where_this_came_from():
    """Print the addon's own provenance, once, at register.

    The question *"am I looking at my own work?"* had no answer in this
    application, and on 2026-08-27 it cost a round trip: the artist reported a
    panel unchanged, the repo was correct, both suites were green, and Blender
    was loading a COPY of the tree that a test run had installed hours earlier.
    Nothing on screen or in the console said which of the two it was running.

    So the addon says. To the console and not to a panel -- a panel is for
    things you press, and this is a run's output.

    A **symlink** into a source tree cannot go stale, and `resolve()` shows
    where it points. A real directory under `scripts/addons` is a snapshot and
    CAN, which is the case that gets called out, because that is the one that
    was silently wrong.
    """
    try:
        from pathlib import Path
        here = Path(__file__).parent
        real = here.resolve()
        ver = ".".join(str(n) for n in bl_info["version"])
        if real != here:
            print(f"EXMATERIA-MAP: addon {ver} loaded from {here}")
            print(f"EXMATERIA-MAP:   -> {real} (symlink; always current)")
        else:
            print(f"EXMATERIA-MAP: addon {ver} loaded from {real}")
            if "scripts/addons" in real.as_posix():
                print("EXMATERIA-MAP:   this is a COPY of a source tree, not a "
                      "link to one -- it can be STALE. See "
                      "exmateria-map/tools/dev_install.sh")
    except Exception:                      # never take the addon down for a print
        pass


def register():
    _say_where_this_came_from()
    import_document.register()
    gns_bundle.register()
    export_document.register()
    paint.register()
    convert_op.register()
    compile_op.register()
    authoring.register()
    lighting_bake.register()
    live_link_ui.register()
    workspace.register()


def unregister():
    workspace.unregister()
    live_link_ui.unregister()
    lighting_bake.unregister()
    authoring.unregister()
    compile_op.unregister()
    convert_op.unregister()
    paint.unregister()
    export_document.unregister()
    gns_bundle.unregister()
    import_document.unregister()
