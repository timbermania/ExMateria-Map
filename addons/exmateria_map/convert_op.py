"""The button that converts a map for surface painting (ADR-0186 dec. 7).

Two ways to author a map's art, and this is the door between them.

**Old school** — do not press it. The sheet stays exactly as the disc laid it
out, `resolve()` gates every pixel against one **CLUT row**, and the artist
paints indices the way the 1997 tools did (ADR-0007, and Amendment 2 there
scopes that ADR to this path).

**Converted** — press it once. Every chart gets its own texels, so a stroke on
one surface can no longer repaint another, and the artist is free to paint with
whatever tool they like and let the compile work out what the format can hold.

It is **one-way** and it is **visually lossless**: every island is a copy of the
texels the chart already read, under the row it already named, so pressing it
changes nothing you can see. `tests/test_convert.py` proves that over 135
resources and 58,123 polygons, exactly.

WHAT IT DOES NOT YET DO. It removes sharing **between** charts -- corpus-wide
7.07% of texels have two chart readers before and **0.00%** after. It does not
make a chart internally manifold: 37.1% of the overlap *inside* a chart is a
**fold** rather than a seam, and 945 charts (8.5%) hold at least one
(ADR-0186 Amendment 2, `workspace/folds.py`). Those need a real per-chart
unwrap, which resamples, so it is a second increment behind this same button
and not a silent part of this one.
"""
import bpy
from bpy.types import Operator

from .convert import convert
from .export_document import (export_order, image_indices, readable_mesh,
                              set_image_indices, uv_dec)
from .import_document import _uv_enc, marker_in_scene
from .paint import (active_palette, index_image, paint_image_name,
                    section, sheet_of_state)

SHEET_W = 256


def _face_ordered(me):
    """The mesh -> a polygons list in FACE order.

    Deliberately not `export_document.export_polygons`, which sorts into the
    schema's bucket order (tt->tq->ut->uq) and so does not index by face.
    Writing UVs back needs the face, and `charts`/`islands`/`convert` need
    only positions, uv, palette_id and texture_page.
    """
    tex = me.attributes["textured"].data
    flip = me.attributes["fft_ring_flipped"].data
    page_of = me.attributes["texture_page"].data
    pal_of = me.attributes["palette_id"].data
    uvl = me.uv_layers["UVMap"].data
    verts, loops = me.vertices, me.loops

    out = []
    for i, f in enumerate(me.polygons):
        n = f.loop_total
        inv = export_order(n, flip[i].value)
        pos = [[int(round(c)) for c in
                verts[loops[f.loop_start + inv[k]].vertex_index].co]
               for k in range(n)]
        q = {"kind": i, "positions": pos}          # `kind` carries the face
        if tex[i].value:
            page = page_of[i].value
            q["texture_page"] = page
            q["palette_id"] = pal_of[i].value
            q["uv"] = []
            for k in range(n):
                u, g = uv_dec(*uvl[f.loop_start + inv[k]].uv)
                q["uv"].append([u, g - page * SHEET_W])
        out.append(q)
    return out


def _write_back(me, converted):
    """UVs and `texture_page` from a converted polygons list, face by face."""
    flip = me.attributes["fft_ring_flipped"].data
    page_of = me.attributes["texture_page"].data
    uvl = me.uv_layers["UVMap"].data
    for i, f in enumerate(me.polygons):
        q = converted[i]
        if "uv" not in q:
            continue
        page = q["texture_page"]
        page_of[i].value = page
        inv = export_order(f.loop_total, flip[i].value)
        for k, (u, v) in enumerate(q["uv"]):
            uvl[f.loop_start + inv[k]].uv = _uv_enc(u, v, page)


def clut_rows_of(ob, state):
    """All sixteen CLUT rows of a state, as 0..255 triples.

    The CLUT image is `Non-Color` and holds `byte / 255` (paint.py's
    `clut_entries` reads it the same way); pixel `(col, row)` is row `row`'s
    entry `col`.
    """
    name = (section(ob, "state_cluts", {}) or {})
    img = None
    if isinstance(name, list) and 0 <= state < len(name):
        img = bpy.data.images.get(name[state])
    if img is None or tuple(img.size) != (16, 16):
        return None
    px = list(img.pixels)
    return [[tuple(int(round(px[(r * 16 + c) * 4 + k] * 255.0))
                   for k in range(3)) for c in range(16)] for r in range(16)]


def write_clut_rows_of(ob, state, rows):
    """Write sixteen CLUT rows back into a state's 16x16 image.

    The inverse of `clut_rows_of`, beside it so the two agree about the pixel
    layout: `(col, row)` is row `row`'s entry `col`, `Non-Color`, `byte / 255`.
    The image is the addon's palette surface and `export_palettes` re-emits
    `map_states[].palettes` from it, so this is where a compile's palettes
    reach the document -- there is no second sink to keep in step.
    """
    names = section(ob, "state_cluts", {}) or {}
    img = None
    if isinstance(names, list) and 0 <= state < len(names):
        img = bpy.data.images.get(names[state])
    if img is None or tuple(img.size) != (16, 16):
        return None
    px = [0.0] * (16 * 16 * 4)
    for r in range(16):
        for c in range(16):
            j = (r * 16 + c) * 4
            entry = rows[r][c] if c < len(rows[r]) else (0, 0, 0)
            px[j], px[j + 1], px[j + 2] = (v / 255.0 for v in entry)
            px[j + 3] = 1.0
    img.pixels[:] = px
    img.pack()
    img.update()
    return img


def source_art_name(sheet):
    return f"exmateria_map.source/{sheet}"


def _write_art(name, art):
    """The true-colour source art as a Blender image.

    `Non-Color`, holding `byte / 255` -- exactly the space the CLUT image is
    in, so the preview's light multiply sees the same numbers it saw through
    the CLUT lookup and the viewport does not change.  That is the visible
    form of "conversion is visually lossless".

    Rows flip: this buffer is top-scanline-first like the sidecar PNG, and
    Blender's pixel row 0 is the BOTTOM.
    """
    w, h = 256, 1024
    img = bpy.data.images.get(name)
    if img is not None and tuple(img.size) != (w, h):
        bpy.data.images.remove(img)
        img = None
    if img is None:
        img = bpy.data.images.new(name, w, h, alpha=False, float_buffer=True)
        img.colorspace_settings.name = "Non-Color"
    px = [0.0] * (w * h * 4)
    for y in range(h):
        src, dst = y * w, (h - 1 - y) * w
        for x in range(w):
            j, at = (dst + x) * 4, 3 * (src + x)
            px[j] = art[at] / 255.0
            px[j + 1] = art[at + 1] / 255.0
            px[j + 2] = art[at + 2] / 255.0
            px[j + 3] = 1.0
    img.pixels[:] = px
    img.pack()                    # or it reloads BLANK from a path it has not got
    img.update()
    return img


def _show_source_art(ob, img):
    """Point the preview at the painting instead of index -> CLUT.

    The CLUT node's colour feeds the light multiply; the source art replaces
    it at that same input, sampled `Closest` through the same UVs.  Node names
    are stable here precisely so a stage can be rewired in place -- the state
    selector already does it for the two image nodes.
    """
    rewired = 0
    for slot in ob.material_slots:
        mat = slot.material
        nt = getattr(mat, "node_tree", None)
        if nt is None:
            continue
        clut = nt.nodes.get("exmateria_map.clut")
        mix = nt.nodes.get("exmateria_map.multiply")
        if clut is None or mix is None:
            continue
        node = nt.nodes.get("exmateria_map.source_art")
        if node is None:
            node = nt.nodes.new("ShaderNodeTexImage")
            node.name = "exmateria_map.source_art"
            node.location = (360, 340)
        node.image = img
        node.interpolation = "Closest"
        node.extension = "CLIP"
        # The material's ACTIVE image texture node is what Texture Paint in
        # `MATERIAL` mode writes into.  `nodes.active` and
        # `Material.paint_active_slot` are two views of ONE pointer -- measured
        # both ways round: setting the slot moves the active node, and setting
        # the active node moves the slot.
        #
        # This graph carries THREE image texture nodes -- `exmateria_map.clut`,
        # `exmateria_map.index`, `exmateria_map.source_art`, in that creation
        # order -- and Blender's default is slot 0.  So without this line,
        # entering Texture Paint on a converted map and painting on the model
        # lands the stroke in the **CLUT**.  Which is the worst place it could
        # go: the node is unlinked by the rewire below, so nothing in the
        # viewport changes and the artist sees no damage, while
        # `export_document`'s §6.4 reads that image's pixels straight back to
        # re-emit `map_states[].palettes`.  Silent, and it ships.
        #
        # An object with ONE image texture node needs none of this, which is
        # why painting on any other Blender object "just works".  This makes
        # ours behave the same, and is why the addon needs no `Paint on the
        # model` button (ADR-0185 Amendment 5).
        nt.nodes.active = node
        uv = nt.nodes.get("exmateria_map.index")
        if uv is not None and uv.inputs["Vector"].links:
            nt.links.new(uv.inputs["Vector"].links[0].from_socket,
                         node.inputs["Vector"])
        # `link.from_node.name`, NOT `link.from_node is clut`.  Blender hands
        # back a FRESH Python wrapper for a nested struct on every access, so
        # `is` is False against the correct node and the swap silently does
        # nothing -- the node gets built, the image gets assigned, and the
        # viewport still shows index -> CLUT.  The addon's CLAUDE.md records
        # this trap; it cost a cycle here anyway.
        for link in list(mix.inputs[1].links) + list(mix.inputs[2].links):
            if link.from_node.name == clut.name:
                socket = link.to_socket
                nt.links.remove(link)
                nt.links.new(node.outputs["Color"], socket)
                rewired += 1
    return rewired


class MAP_OT_convert_manifold(Operator):
    """Give every chart its own texels, so a stroke cannot repaint another \
surface. One-way, and it changes nothing you can see"""
    bl_idname = "exmateria_map.convert_manifold"
    bl_label = "Convert"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        ob = marker_in_scene(context)
        return ob is not None and "exmateria_map/base" in ob

    def execute(self, context):
        ob = marker_in_scene(context)
        state, _ = active_palette(ob)
        sheet = sheet_of_state(ob, state)
        if not sheet:
            self.report({"ERROR"}, "no texture sheet in this arrangement")
            return {"CANCELLED"}
        # NOT `bpy.data.images.get(sheet)`.  The image is named from the
        # marker's own `sheet_images` map, not from the sidecar's file name.
        img = index_image(ob, sheet)
        if img is None:
            self.report({"ERROR"}, f"sheet image for {sheet!r} is not loaded")
            return {"CANCELLED"}

        rows = clut_rows_of(ob, state)
        if rows is None:
            self.report({"ERROR"}, f"no CLUT for state {state}; nothing to "
                                   f"resolve the sheet's indices through")
            return {"CANCELLED"}

        me = ob.data
        # `readable_mesh`, because Edit Mode is where the artist already is.
        # In Edit Mode the geometry lives in the BMesh and the Mesh datablock's
        # attribute arrays read as **size 0** while `me.polygons` still reports
        # a count, so `_face_ordered`'s first lookup is a bare `IndexError`
        # naming a collection of size 0. That is the fourth time this addon has
        # shipped that defect -- the push, the panel and the resolve trigger all
        # had it -- and it survives review every time because the whole test
        # suite drives Object Mode. `Object.update_from_editmode()` is NOT the
        # fix; only leaving Edit Mode restores the arrays (see the context
        # manager's own docstring).
        try:
            with readable_mesh(ob):
                polygons = _face_ordered(me)
                converted, art, moved = convert(polygons,
                                                image_indices(img), rows)
                _write_back(me, converted)
        except RuntimeError as e:      # readable_mesh refuses a non-active edit
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        except Exception as e:                     # PackRefusal names the cost
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}

        painting = _write_art(source_art_name(sheet), art)
        # The Sheet rides the same blit as the Painting (Amendment 3 decision
        # 14).  Leaving it on the disc's layout is not a stale cache -- the
        # UVs have all moved, so the mesh and the sheet picture different
        # things, `build` ships that and the push reports success.  Carrying
        # it makes the incoherence unreachable instead of gated.
        set_image_indices(img, moved)
        rewired = _show_source_art(ob, painting)
        me.update()

        # The recoloured PAINT copy is a picture of the OLD layout, and
        # `paint.resolve` diffs against it.  Left behind it would show the
        # artist the pre-conversion sheet and read every moved texel as a
        # fresh stroke.  Dropping it makes the next `Paint sheet` rebuild it
        # from the index image this operator just rewrote.
        stale = bpy.data.images.get(paint_image_name(sheet))
        if stale is not None:
            bpy.data.images.remove(stale)

        # ...and the brush has to come off it.  `Paint sheet` sets
        # `image_paint.mode = "IMAGE"` and points `canvas` at the picture it
        # hands over (`paint.py`), and that setting is scene-wide and saved in
        # the `.blend`.  On the direct-paint path the picture it names is the
        # image REMOVED just above -- so an artist who pressed `Paint sheet`
        # before converting would enter Texture Paint on the model, stroke,
        # and have nothing happen at all, with nothing said about why.
        #
        # Back to `MATERIAL`, where `_show_source_art`'s active node decides
        # the destination.  This is cleanup of state THIS operator invalidated,
        # not an override of a choice the artist made about this map: pressing
        # `Paint sheet` again re-arms `IMAGE` mode on the painting, which is
        # also correct and is the atlas route.
        ip = getattr(getattr(context, "tool_settings", None),
                     "image_paint", None)
        if ip is not None:
            try:
                ip.mode = "MATERIAL"
            except (AttributeError, TypeError):
                pass

        faces = sum(1 for q in converted if "uv" in q)
        self.report({"INFO"},
                    f"converted: {faces} textured faces unwrapped and baked "
                    f"into {painting.name} ({rewired} material(s) now show "
                    f"the painting); the index sheet moved with them; every "
                    f"chart owns its own texels")
        return {"FINISHED"}


CLASSES = (MAP_OT_convert_manifold,)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
