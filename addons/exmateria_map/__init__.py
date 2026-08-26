"""ExMateria Map — the Blender addon leg of the exmateria-map interchange.

Speaks only the interchange document (schema v1,
`exmateria-map/docs/interchange-schema-v1.md`) and its PNG sidecars; stdlib
`json`/`zlib` only; never imports `exmateria_map` (ADR-0004, decisions 6, 7).
"""
import bpy

from . import (authoring, export_document, import_document, lighting_bake,
               live_link_ui, paint)

bl_info = {
    "name": "ExMateria Map",
    "author": "timbermania",
    "version": (0, 1, 0),
    "blender": (4, 0, 0),
    "category": "Import-Export",
}


def register():
    import_document.register()
    export_document.register()
    paint.register()
    authoring.register()
    lighting_bake.register()
    live_link_ui.register()


def unregister():
    live_link_ui.unregister()
    lighting_bake.unregister()
    authoring.unregister()
    paint.unregister()
    export_document.unregister()
    import_document.unregister()
