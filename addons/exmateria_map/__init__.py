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


def register():
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
