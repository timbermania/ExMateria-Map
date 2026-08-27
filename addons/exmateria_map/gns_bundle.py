"""ADR-0004 decision 31: authorship happens in Blender, patching stays on the CLI.

Decision 7 ruled that the addon never imports `exmateria_map`, on the argument
that "`build` never sees a disc, so the artist runs `fft-iso-patcher` on a CLI
either way". That prices two different CLI trips as one. Patching is the
patcher's; `dump` and `build` are authorship steps, and an artist who installs
this addon should never meet them.

So the addon vendors the package (`_vendor/`, guarded byte-for-byte by
`tests/test_vendored_package.py`) and grows the two ends that need it:

  File > Import > FFT Map (.GNS)   -- a disc GNS straight into a scene
  File > Export > FFT Map bundle   -- the GNS verbatim plus one blob per
                                      resource, which is what the patcher eats

Neither end asks the artist for a directory or a map number: the picked GNS
path IS the address (`mapfile.address`).

The interchange document is untouched -- decision 6 and decision 7's other half
still stand, and `import_map.document` / `export_map.document` still speak it.
Import here goes GNS -> `dump.write_bundle` into a scratch directory -> the
same `import_document.build` the document operator calls, so the in-Blender
path and the CLI path cannot diverge into two different importers.
"""
import json
import os
import shutil
import tempfile
from pathlib import Path

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy.types import Operator

from ._vendor.exmateria_map import dump as pkg_dump
from ._vendor.exmateria_map import mapfile as pkg_mapfile

#: The Marker property holding the GNS this scene was imported from. Export
#: needs it -- the bundle carries that GNS verbatim and builds against that
#: base map -- and it survives a `.blend` save because it is an ID property on
#: an object, which is where the addon's dozen other `exmateria_map/*` props
#: already live. Deliberately not a preference: a preference is one value for
#: every scene, and the thing being remembered is per scene.
GNS_PATH = "exmateria_map/gns_path"

#: Blender does not keep a reference to the strings a dynamic `EnumProperty`
#: callback returns, and collects them out from under the UI -- the enum then
#: draws blank or garbage. Holding the last-built list here is the standard
#: fix, not a cache for speed.
_ENUM_ITEMS = []


def abspath(filepath):
    """Blender's `//`-relative form resolved, and nothing else.

    Not `Path.resolve()`: `project-assets/` is a symlink in every worktree but
    the canonical one, and resolving would record a path that names another
    checkout.
    """
    return str(Path(bpy.path.abspath(filepath or "")))


def arrangement_items(self, context):
    """The DUMPABLE arrangements of the picked map, for the browser sidebar.

    `dump.arrangements()` is the wrong population for a control: it enumerates
    arrangements that name a mesh row, and 49 of the disc's 197 name one that
    carries no 0x40 chunk. Offering those puts entries in the dropdown that
    refuse when picked, on 17 maps.

    101 of 121 maps have exactly one, and Blender draws a single-item enum as
    an inert button -- so on five maps in six this control is effectively
    invisible, which is what decision 31 part 3 intends.
    """
    global _ENUM_ITEMS
    try:
        map_dir, number = pkg_mapfile.address(getattr(self, "filepath", ""))
        found = pkg_dump.dumpable_arrangements(map_dir, number)
    except Exception:
        found = []
    if not found:
        _ENUM_ITEMS = [("0", "Arrangement 0", "")]
    else:
        _ENUM_ITEMS = [
            (str(a), f"Arrangement {a}",
             f"Import MAP{number:03d} arrangement {a}") for a in found]
    return _ENUM_ITEMS


class IMPORT_OT_gns(Operator):
    """Import an FFT map straight from the extracted disc tree."""
    bl_idname = "import_map.gns"
    bl_label = "FFT Map (MAP###.GNS)"
    bl_description = ("Import a map from the extracted disc tree; pick its "
                      "MAP###.GNS and the arrangement")
    bl_options = {"REGISTER", "UNDO"}

    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.GNS;*.gns", options={"HIDDEN"})
    arrangement: EnumProperty(
        name="Arrangement",
        description="Which arrangement of this map to import",
        items=arrangement_items)

    def invoke(self, context, event):
        from .import_document import _prefs
        last = bpy.path.abspath(getattr(_prefs(context), "last_dir", "") or "")
        if last and os.path.isdir(last):
            # The trailing separator is what makes the browser open INSIDE the
            # directory; without it the path reads as a file to select, the
            # same shape `export_document.start_directory` uses.
            self.filepath = os.path.join(last, "")
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, context):
        col = self.layout.column(align=True)
        col.label(text="Pick MAP###.GNS from the extracted disc tree.")
        col.label(text="Its folder and number are read from the path.")
        col.prop(self, "arrangement")

    def execute(self, context):
        from .authoring import suspended
        from .import_document import build, remember_dir
        path = abspath(self.filepath)
        try:
            map_dir, number = pkg_mapfile.address(path)
        except pkg_mapfile.AddressError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        try:
            arrangement = int(self.arrangement)
        except (TypeError, ValueError):
            self.report({"ERROR"}, "no arrangement picked")
            return {"CANCELLED"}
        if arrangement not in pkg_dump.dumpable_arrangements(map_dir, number):
            self.report({"ERROR"},
                        f"MAP{number:03d} a{arrangement} carries no primary "
                        f"mesh; there is no geometry to import")
            return {"CANCELLED"}

        # The document and its sidecars are written to a scratch directory and
        # imported from there -- byte for byte what `exmateria-map-dump` would
        # have written, read by the same importer. The directory goes away
        # because the sheet images are packed into the `.blend` at build time
        # (`import_document._persist`), so nothing keeps a path into it.
        scratch = Path(tempfile.mkdtemp(prefix="exmateria-map-"))
        try:
            doc_path = pkg_dump.write_bundle(map_dir, number, arrangement,
                                             scratch)
            document = json.loads(Path(doc_path).read_text())
            with suspended():           # §6.1, as on the document import side
                ob = build(document, context, doc_path)
        except Exception as e:
            self.report({"ERROR"}, f"could not import {Path(path).name} "
                                   f"a{arrangement}: {e}")
            return {"CANCELLED"}
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

        ob[GNS_PATH] = path
        remember_dir(context, path)
        from .workspace import ensure_on_import
        ensure_on_import(context)
        self.report({"INFO"},
                    f"imported MAP{number:03d} a{arrangement} "
                    f"({len(document['polygons'])} polygons, "
                    f"{len(document['map_states'])} states)")
        return {"FINISHED"}


def import_menu(self, context):
    self.layout.operator(IMPORT_OT_gns.bl_idname, text="FFT Map (.GNS)")


# ---------------------------------------------------------------------------
# decision 31 part 4: export writes a BUNDLE, not a file
# ---------------------------------------------------------------------------

def remembered_gns(ob):
    """The base map this scene was imported from, or `""`.

    A scene imported from an interchange document has none -- the document is a
    diff against a base map, not an archive of one -- so export has to ask.
    """
    return (ob.get(GNS_PATH) or "") if ob is not None else ""


class MAP_OT_pick_base_map(Operator):
    """Name the base map for a scene that does not remember one, then export.

    A file pick, never a settings page (decision 31 part 4). It exists because
    an interchange document names its base map only by digest: `build` can tell
    the artist the tree is wrong, but it cannot find the right one.
    """
    bl_idname = "exmateria_map.pick_base_map"
    bl_label = "Pick the base map (MAP###.GNS)"
    bl_options = {"REGISTER"}

    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.GNS;*.gns", options={"HIDDEN"})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, context):
        col = self.layout.column(align=True)
        col.label(text="This scene came from an interchange document,")
        col.label(text="which is a diff against a base map, not a copy of it.")
        col.label(text="Pick the MAP###.GNS it was authored against.")

    def execute(self, context):
        from .export_document import find_marker
        ob, problem = find_marker(context)
        if problem:
            self.report({"ERROR"}, problem)
            return {"CANCELLED"}
        path = abspath(self.filepath)
        try:
            pkg_mapfile.address(path)
        except pkg_mapfile.AddressError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        ob[GNS_PATH] = path
        return bpy.ops.export_map.bundle("INVOKE_DEFAULT")


class EXPORT_OT_bundle(Operator):
    """Export the scene as a patcher bundle: the GNS plus its resources."""
    bl_idname = "export_map.bundle"
    bl_label = "FFT Map bundle (GNS + resources)"
    bl_description = ("Write the GNS verbatim plus one blob per resource -- "
                      "what the ISO patcher's map leg reads")
    bl_options = {"REGISTER"}

    filepath: StringProperty(subtype="DIR_PATH")
    directory: StringProperty(subtype="DIR_PATH")
    filter_folder: BoolProperty(default=True, options={"HIDDEN"})
    #: Set when the base map was just picked, or by a script. SKIP_SAVE so a
    #: one-off answer is not silently reused for the next scene.
    gns_path: StringProperty(subtype="FILE_PATH", options={"SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        from .export_document import markers
        return bool(markers(context.scene))

    def invoke(self, context, event):
        from .export_document import find_marker, start_directory
        ob, problem = find_marker(context)
        if problem:
            self.report({"ERROR"}, problem)
            return {"CANCELLED"}
        if not (self.gns_path or remembered_gns(ob)):
            # Ask for the base map first; that operator chains back here.
            return bpy.ops.exmateria_map.pick_base_map("INVOKE_DEFAULT")
        self.filepath = start_directory(context)
        self.directory = self.filepath
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, context):
        col = self.layout.column(align=True)
        col.label(text="Pick a FOLDER. It receives the map's GNS")
        col.label(text="verbatim and one file per resource --")
        col.label(text="what fft-iso-patcher's map leg reads.")

    def execute(self, context):
        from ._vendor.exmateria_map import build as pkg_build
        from .authoring import suspended
        from .export_document import (assemble, describe_divergence,
                                      find_marker, output_directory)
        from .import_document import remember_dir

        ob, problem = find_marker(context)
        if problem:
            self.report({"ERROR"}, problem)
            return {"CANCELLED"}

        gns = abspath(self.gns_path) if self.gns_path else remembered_gns(ob)
        if not gns:
            # §31: asked for, never guessed. A plausible tree here is a bundle
            # built against the wrong base map, which patches without error.
            self.report({"ERROR"},
                        "this scene does not remember a base map; run "
                        "File > Export > FFT Map bundle from the file menu so "
                        "it can ask for the MAP###.GNS, or pass gns_path")
            return {"CANCELLED"}
        try:
            map_dir, _number = pkg_mapfile.address(gns)
        except pkg_mapfile.AddressError as e:
            self.report({"ERROR"}, f"base map: {e}")
            return {"CANCELLED"}

        with suspended():               # §6.1, as on the document export side
            document, sidecars, rep = assemble(ob)
        ob["exmateria_map/last_export"] = json.dumps(rep.lines())
        from .report_log import record
        summary = [describe_divergence(rep)] + list(rep.lines())
        for w in rep.warnings:
            self.report({"WARNING"}, w)
        self.report({"INFO"}, describe_divergence(rep))
        if rep.refusals:                # §9.4 -- nothing written, every reason
            self.report({"ERROR"},
                        f"{len(rep.refusals)} refusal(s), nothing written: "
                        + "; ".join(rep.refusals[:12])
                        + (" ..." if len(rep.refusals) > 12 else ""))
            record("Export bundle REFUSED", ob.name,
                   summary + [f"{len(rep.refusals)} refusal(s), nothing written"])
            return {"CANCELLED"}

        # The sidecars go to a scratch directory because `build` reads repaints
        # from a directory (schema §6.5) -- the same shape `exmateria-map-build`
        # sees, so the two produce the same bytes or neither does.
        scratch = Path(tempfile.mkdtemp(prefix="exmateria-map-"))
        try:
            for name, blob in sidecars.items():
                (scratch / name).write_bytes(blob)
            bundle = pkg_build.build(document, map_dir, sidecar_dir=scratch)
        except Exception as e:
            # A wrong tree lands here by design: the document pins a sha256 per
            # resource, so `build` names the mismatch rather than patching.
            self.report({"ERROR"}, f"could not build against "
                                   f"{Path(gns).name}: {e}")
            return {"CANCELLED"}
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

        for w in bundle.warnings:
            self.report({"WARNING"}, w)
        directory = output_directory(self.filepath, self.directory)
        try:
            bundle.write(Path(directory))
        except Exception as e:
            self.report({"ERROR"}, f"could not write into {directory}: {e}")
            return {"CANCELLED"}

        ob[GNS_PATH] = gns              # so the artist is asked at most once
        # `remember_dir` stores the PARENT of what it is given, so it is handed
        # a file that is actually in the bundle rather than a sentinel name.
        remember_dir(context, str(Path(directory) / bundle.gns_name),
                     field="last_export_dir")
        self.report({"INFO"},
                    f"wrote {bundle.gns_name} + {len(bundle.resources)} "
                    f"resource(s) to {directory}")
        print(f"EXMATERIA-MAP: bundle {bundle.name} -> {directory} "
              f"({len(bundle.resources)} resources, "
              f"{len(bundle.warnings)} warning(s))")
        return {"FINISHED"}


def export_menu(self, context):
    self.layout.operator(EXPORT_OT_bundle.bl_idname,
                         text="FFT Map bundle (GNS + resources)")


def register():
    bpy.utils.register_class(IMPORT_OT_gns)
    bpy.utils.register_class(MAP_OT_pick_base_map)
    bpy.utils.register_class(EXPORT_OT_bundle)
    try:
        bpy.types.TOPBAR_MT_file_import.append(import_menu)
        bpy.types.TOPBAR_MT_file_export.append(export_menu)
    except AttributeError:              # the 4.x line `bl_info` still supports
        from bpy.utils import menu_registry
        menu_registry.append(bpy.types.TOPBAR_MT_file_import, import_menu)
        menu_registry.append(bpy.types.TOPBAR_MT_file_export, export_menu)


def unregister():
    try:
        bpy.types.TOPBAR_MT_file_export.remove(export_menu)
        bpy.types.TOPBAR_MT_file_import.remove(import_menu)
    except AttributeError:
        from bpy.utils import menu_registry
        menu_registry.remove(bpy.types.TOPBAR_MT_file_export, export_menu)
        menu_registry.remove(bpy.types.TOPBAR_MT_file_import, import_menu)
    bpy.utils.unregister_class(EXPORT_OT_bundle)
    bpy.utils.unregister_class(MAP_OT_pick_base_map)
    bpy.utils.unregister_class(IMPORT_OT_gns)
