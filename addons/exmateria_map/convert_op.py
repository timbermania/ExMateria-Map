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
from bpy.props import EnumProperty
from bpy.types import Operator

from . import resample
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


def _write_art(name, art, pack=True):
    """The true-colour source art as a Blender image.

    `Non-Color`, holding `byte / 255` -- exactly the space the CLUT image is
    in, so the preview's light multiply sees the same numbers it saw through
    the CLUT lookup and the viewport does not change.  That is the visible
    form of "conversion is visually lossless".

    Rows flip: this buffer is top-scanline-first like the sidecar PNG, and
    Blender's pixel row 0 is the BOTTOM.  That flip is per ROW and stays per
    row at every scale (ADR-0186 Amendment 10) -- flipping in N-row blocks
    leaves each block internally upside down, which at N = 1 is
    indistinguishable from correct and at N > 1 is a shredded picture.

    **The size comes from the buffer**, through the one home the scale rule
    has (`resample.scale_of_buffer`).  It used to be a hardcoded
    `w, h = 256, 1024`, which took a 4x buffer, wrote its top strip into a 1x
    image, and said nothing -- `img.pixels[:]` never sees the rest, because
    the loop is bounded by `w` and `h` and not by `len(art)`.

    `pack` is a parameter because the Painting and the native canvas want
    OPPOSITE answers.  The Painting is the master and must survive a reload,
    so it packs.  The canvas is derived, and ADR-0186 Amendment 10 decision 39
    says it is never saved into the `.blend` -- regenerated from the master on
    load, so a file cannot be reopened holding a canvas that disagrees with its
    master.  Cross-session staleness is unreachable rather than guarded.

    Cost, measured rather than feared: building the float buffer is 0.04 s at
    1x and **0.65 s at 4x**, once per conversion.  An earlier note here
    guessed the plain Python list was unviable at 4x and it is not; the
    `array` + `foreach_set` idiom this file uses elsewhere is 2.6x faster and
    is not needed for that.
    """
    n = resample.scale_of_buffer(len(art))
    if n is None:
        raise ValueError(
            f"{len(art)} art byte(s) is no legal Painting: it must be "
            f"3 * 256k * 1024k for k in {list(resample.SCALES)} "
            f"(ADR-0186 Amendment 10 dec. 43)")
    w, h = 256 * n, 1024 * n
    img = bpy.data.images.get(name)
    if img is not None and tuple(img.size) != (w, h):
        # RESIZED, never removed and remade.  Removing it frees the datablock
        # every reference is holding: the material's `source_art` node loses
        # its image, so the model goes untextured; an Image Editor the artist
        # is painting in empties; and any Python name for it dies with
        # `ReferenceError: StructRNA of type Image has been removed`.  That
        # cost a run here and would have cost the artist their viewport the
        # first time they changed the scale.  `scale()` resamples in place --
        # whatever it leaves behind is overwritten wholesale below.
        img.scale(w, h)
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
    if pack:
        img.pack()                # or it reloads BLANK from a path it has not got
    img.update()
    return img


def native_canvas_name(sheet):
    return f"exmateria_map.canvas/{sheet}"


#: What was last DERIVED into each native canvas, keyed by image name.
#:
#: Module state and nothing else -- not a datablock, not an
#: `exmateria_map/...` key, and so never in the `.blend`.  ADR-0186
#: Amendment 10 decision 39 makes the canvas itself unsaved, and a baseline
#: that outlived the session would describe a canvas that no longer exists:
#: every pixel would read as freshly painted and one reopen would flatten the
#: whole master.  That is the failure `paint.py`'s `resolve()` records on the
#: colour axis, and here it is unreachable rather than guarded.
_CANVAS_WAS = {}


def _derive_canvas(sheet, master, w, h, n):
    """Shrink the master into the native canvas and record what was written."""
    small = resample.shrink(master, w, h, n)
    img = _write_art(native_canvas_name(sheet), small, pack=False)
    _CANVAS_WAS[img.name] = small
    return img


def _painting_and_sheet(ob):
    from .paint import painting_of
    sheet = sheet_of_state(ob, int(ob.get("exmateria_map/preview_state") or 0))
    return sheet, painting_of(ob, sheet)


def painting_scale_get(ob):
    """The Painting's N, read off the picture.  1 when there is no Painting.

    ADR-0186 Amendment 10 decision 43: N is DERIVED and never stored, because
    a picture already carries its own width and height and a stored `scale`
    would be the redundant, driftable copy.  A plain registered property would
    be exactly that copy -- `bpy.props` writes into the ID property store, so
    it would survive a rescale done any other way and then disagree with the
    image.  `get`/`set` is what keeps decision 43 true of the UI as well as of
    the document.
    """
    _sheet, painting = _painting_and_sheet(ob)
    if painting is None:
        return 1
    return resample.scale_of(painting.size[0], painting.size[1]) or 1


def _warn_down_conversion(sheet, old, new):
    """Say what a shrink just destroyed.  Never raises -- it is a report.

    It does NOT tell the artist to undo.  Whether Blender's undo restores image
    PIXEL data is not something this package has measured, and it cannot be:
    `ed.undo` is disabled in background mode, so every harness here is blind to
    it.  A line promising a way back that may not exist is worse than the
    silence decision 36 was closing -- it would stop the artist saving the file
    under a new name, which is the recovery that certainly does work.
    """
    try:
        from .report_log import record
        record("painting scale", sheet, [
            f"{old}x -> {new}x: the Painting was box-averaged down and the "
            f"detail between those two scales is GONE.",
            f"Raising it back to {old}x replicates pixels; it does not "
            f"recover what was averaged away.",
            "If that was not what you meant, close WITHOUT saving.",
        ])
    except Exception:                  # a log that failed must not eat the edit
        pass


def painting_scale_set(ob, value):
    """Rescale the Painting in place, snapping `value` to a legal scale.

    Raising is a REPLICATE and is lossless -- the same replicate the bake does,
    which is why converting at 1x and then raising this to 4 is byte-identical
    to converting at 4x (grading criterion 2, restated as a gesture).  Lowering
    box-averages and cannot be undone, so a value between two scales snaps UP
    (`resample.snap_scale`, which holds the rule and the reasoning).

    The ratio is always itself a legal scale -- `SCALES` is a power-of-two
    ladder, so every quotient of two members is a member -- which is what lets
    2 -> 4 replicate directly instead of going down to 1 and back up, losing
    the 2x detail on the way through.

    **A down-conversion is REPORTED.**  Decision 36 makes it "a deliberate,
    warned act", and a property setter has no dialog to warn in -- but it has
    the Log, which is where everything else this addon does with consequences
    goes.  Silence here would be decision 11's blur handed back without a word;
    the artist can still undo, and the line is what tells them they need to.
    """
    sheet, painting = _painting_and_sheet(ob)
    if painting is None:
        return
    old = resample.scale_of(painting.size[0], painting.size[1])
    if old is None:
        return
    new = resample.snap_scale(value)
    if new == old:
        return
    from .export_document import image_rgb
    w, h = painting.size[0], painting.size[1]
    master = image_rgb(painting)
    if new > old:
        master = resample.expand(master, w, h, new // old)
    else:
        master = resample.shrink(master, w, h, old // new)
        _warn_down_conversion(sheet, old, new)
    _write_art(source_art_name(sheet), master)
    # The canvas is derived FROM the master, so a master that changed shape
    # leaves it the wrong size and its baseline describing a picture that no
    # longer exists -- `write_through_canvas` would then compare buffers of
    # different lengths on the very next tick.  Re-derive rather than guard.
    if bpy.data.images.get(native_canvas_name(sheet)) is not None:
        _derive_canvas(sheet, master, 256 * new, 1024 * new, new)


def write_through_canvas(ob, sheet, painting):
    """Stamp the native canvas's CHANGED pixels back into the master.

    ADR-0186 Amendment 10 decision 39, and the first stage of the settle tick:
    *write-through -> shrink -> compile -> push*.  Not on the mode switch,
    which would leave the Sheet and anything pushed stale for as long as the
    artist paints natively, and not behind a button.

    Only the pixels that DIFFER are written, and `resample.write_through` says
    why: stamping every block would be correct on the canvas and catastrophic
    underneath it, since one native stroke would flatten the entire N-times
    painting.  That is grading criterion 4.

    A canvas with no baseline is RE-DERIVED rather than stamped through.  It
    means this session did not put those pixels there -- a reopened file, whose
    canvas decision 39 declines to save -- so there is nothing to say which of
    them are strokes.  Stamping all of them would flatten the master; stamping
    none of them and carrying on would leave the canvas disagreeing with the
    master for the rest of the session.  Re-deriving is decision 39's own
    answer, moved from load time (where there is no hook) to the first tick
    that notices.
    """
    canvas = bpy.data.images.get(native_canvas_name(sheet))
    if canvas is None or painting is None:
        return 0
    n = resample.scale_of(*painting.size)
    if not n or n == 1:
        return 0
    from .export_document import image_rgb
    w, h = painting.size[0], painting.size[1]
    was = _CANVAS_WAS.get(canvas.name)
    if was is None:
        _derive_canvas(sheet, image_rgb(painting), w, h, n)
        return 0
    now = image_rgb(canvas)
    if now == was:
        return 0
    master, changed = resample.write_through(image_rgb(painting), w, h, n,
                                             now, was)
    if changed:
        _write_art(source_art_name(sheet), master)
        _CANVAS_WAS[canvas.name] = now
    return changed


def apply_canvas(ob):
    """Point the paint target at the master, or at the derived native canvas.

    ADR-0186 Amendment 10 decision 40.  This is the half of the Canvas control
    that does something, and what it does is move where a stroke LANDS -- see
    `_show_source_art`, where `nodes.active` and `Material.paint_active_slot`
    are two views of one pointer.  That is the reason Canvas could not be a
    third `PREVIEW_MODES` item: those change "nothing about the document".

    Derived on demand and never cached against a stamp.  The canvas is a pure
    function of the master (decision 35), and the settle tick is the only thing
    that writes the master, so re-deriving on a switch cannot be wrong -- where
    a stale cache could be, and would be exactly the cross-session staleness
    decision 39 removes by refusing to save the canvas at all.

    At N = 1 there is nothing to switch between, so this is a no-op rather than
    a rebuild of the same picture under a second name.  It does NOT touch the
    brush (decision 41): the chunkiness is delivered by the canvas, and seeding
    a size here would be the force #423 spent months rejecting, over
    `tool_settings` that live in the `.blend`.
    """
    from .paint import painting_of
    sheet = sheet_of_state(ob, int(ob.get("exmateria_map/preview_state") or 0))
    painting = painting_of(ob, sheet)
    if painting is None:
        return None
    n = resample.scale_of(*painting.size)
    if not n or n == 1:
        return None
    if str(getattr(ob, "exmateria_map_canvas", "HIGH")) != "NATIVE":
        _show_source_art(ob, painting)
        return painting
    from .export_document import image_rgb
    canvas = _derive_canvas(sheet, image_rgb(painting), painting.size[0],
                            painting.size[1], n)
    _show_source_art(ob, canvas)
    return canvas


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

    #: ADR-0186 Amendment 10 decision 36.  The Painting is baked at N pixels
    #: per texel; the **Sheet** is not, ever -- it is the disc's own resource
    #: and carries no scale (decision 35).
    #:
    #: The default is **4**, decision 36's own number.  It was staged at 1
    #: while decision 40's Canvas (High / Native) control did not exist -- an
    #: artist handed a 4x canvas with no native view has no gesture that means
    #: "one texel" -- and that blocker is gone: Canvas ships, and the scale is
    #: a number field in the Paint panel beside the two compile buttons, so it
    #: no longer has to be found in the redo panel to be changed.
    #:
    #: What it costs, said out loud: a 4x Painting is sixteen times the pixels,
    #: which is 12.6 MB of buffer per state, a compile that box-averages
    #: sixteen texels per output pixel, and a settle digest over all of it.
    #: The default is what most maps will be authored at, so that is the
    #: budget the settle tick is now sized against.
    scale: EnumProperty(
        name="Painting scale",
        description="Pixels per texel in the Painting. Higher lets you author "
                    "above the sheet's own resolution; the compile box-averages "
                    "it back down. The Sheet the game reads never changes size",
        items=[(str(n), f"{n}x", f"{256 * n} x {1024 * n} -- "
                + ("one pixel per texel" if n == 1
                   else f"{n * n} pixels per texel"))
               for n in resample.SCALES],
        default="4",
    )

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
                                                image_indices(img), rows,
                                                scale=int(self.scale))
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
