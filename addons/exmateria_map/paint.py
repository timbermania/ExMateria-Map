"""The paint surface and the resolve pass — block 2 of
`docs/interchange-export-v1.md` (§3, §4).

Decision 7's exact-match gate, and the only thing that lets a repainted sheet
reach the disc: the document carries **indices**, not colours, so a painted
pixel has to be recovered as an index or refused as a colour.

Three artifacts per distinct sheet (§4), of which import already built the
middle one:

- the **index buffer** — the 4-bit indices.  The source of truth, and export's
  input.  It is stored as the index image's R channel: 0..15 is exact in
  float32, and §4.2 has the index image move only on a successful resolve, so
  the two are the same data in two shapes rather than two sources.
- the **index image** — the mesh preview's source, state-independent.
- the **paint image** — 256 x 1024, the buffer re-coloured under the ACTIVE
  palette.  The artist's paint target, and the only one of the three that is
  ever out of step with the others.

**The resolve is one code path** (§4.1), on triggers and never on a timer
(§3.3): diff the paint image against the buffer expanded under the OUTGOING
palette; a differing pixel was painted under that palette, so resolve it
against that palette's 16 entries, lowest index on a duplicate (§3.5); an
off-palette colour enters the sticky refusal list (§3.6).  Then write the
buffer back to the index image and re-colour the paint image under the
INCOMING palette.

**An unchanged pixel is never re-resolved** (§3.4).  That is what makes the
identity structural rather than re-derived: a pixel the artist did not paint
round-trips its exact index no matter what the palette says — including an
index whose colour duplicates another's, which no colour match could recover.

**Byte space, not sRGB.**  The paint image is `Non-Color` and holds
`byte / 255`.  The gate is EXACT match (§3.1), so a colour-managed round trip
that moved one channel by one would refuse every pixel the artist painted —
the same reason the preview chain multiplies in PSX byte space (#427).  The
cost is that the paint image displays its bytes raw, which is the space the
CLUT carries and the space the artist is choosing colours in.
"""
import array
import json

import bpy
from bpy.props import IntProperty
from bpy.types import Operator, Panel

from .export_document import SHEET_H, SHEET_W, markers, section

PAINT_PREFIX = "exmateria_map/paint_"
# §4.4: the sticky list has to remember WHICH pixels, or "clears only by
# re-painting the pixel" is not expressible.  A whole sheet is 262,144 pixels;
# an artist who paints that many off-palette does not need per-pixel tracking
# any more, so the stored prefix is capped and the true count is kept.
STICKY_PIXEL_CAP = 100_000

_CACHE = {}          # paint image name -> the floats this module last wrote


# ---------------------------------------------------------------------------
# Reading the scene's colour state.
# ---------------------------------------------------------------------------

def _floats(img):
    buf = array.array("f", bytes(4 * img.size[0] * img.size[1] * 4))
    img.pixels.foreach_get(buf)
    return buf


def clut_entries(ob, state_index, palette_id):
    """The 16 (r, g, b) BYTE triples of one CLUT row of one map state.

    Read off the 16x16 CLUT image import built, not off the document: the image
    is what the preview shows, so the gate accepts exactly the colours the
    artist can see."""
    names = section(ob, "state_cluts", []) or []
    if not (0 <= state_index < len(names)):
        return []
    img = bpy.data.images.get(names[state_index])
    if img is None or tuple(img.size) != (16, 16):
        return []
    px = _floats(img)
    row = max(0, min(15, int(palette_id)))
    return [tuple(int(round(px[(row * 16 + col) * 4 + k] * 255.0))
                  for k in range(3)) for col in range(16)]


def active_palette(ob):
    """§3.2 — the single active palette: the N-panel override when it is set,
    else the selected face's CLUT row.

    Never a union across states or palettes: a union makes the recovered index
    ambiguous, because the same colour may be entry 2 in one CLUT and entry 9
    in another, and the document carries one `palette_id` per face, not a
    colour -> (state, index) table."""
    state = int(ob.get("exmateria_map/preview_state") or 0)
    override = getattr(ob, "exmateria_map_palette_override", -1)
    if isinstance(override, int) and override >= 0:
        return state, int(override)
    me = ob.data
    attr = me.attributes.get("palette_id")
    if attr is not None and len(me.polygons):
        i = me.polygons.active
        if not isinstance(i, int) or not (0 <= i < len(me.polygons)):
            i = next((k for k, p in enumerate(me.polygons) if p.select), 0)
        return state, int(attr.data[i].value)
    return state, 0


def sheet_of_state(ob, state_index):
    sheets = section(ob, "state_sheets", []) or []
    if 0 <= state_index < len(sheets) and sheets[state_index]:
        return sheets[state_index]
    return next((s for s in sheets if s), None)


def index_image(ob, sheet):
    name = (section(ob, "sheet_images", {}) or {}).get(sheet)
    return bpy.data.images.get(name) if name else None


def paint_image_name(sheet):
    return f"{PAINT_PREFIX}{sheet}"


# ---------------------------------------------------------------------------
# The buffer <-> paint image expansion.
# ---------------------------------------------------------------------------

def read_buffer(img):
    """The 0..15 index per pixel, in BLENDER row order.

    Row order does not matter in here — the buffer and the paint image share
    it, and only export flips into the PNG's top-first order."""
    px = _floats(img)
    return bytearray(int(round(px[i * 4])) & 0xF
                     for i in range(img.size[0] * img.size[1]))


def expand(buffer, entries):
    """The buffer re-coloured under one palette: `byte / 255` per channel,
    Non-Color, so a read back is the byte again."""
    n = len(buffer)
    out = array.array("f", bytes(4 * n * 4))
    lut = [tuple(c / 255.0 for c in e)
           for e in (entries or [(0, 0, 0)] * 16)]
    for i in range(n):
        r, g, b = lut[buffer[i] & 0xF]
        j = i * 4
        out[j], out[j + 1], out[j + 2], out[j + 3] = r, g, b, 1.0
    return out


def _persist(img):
    img.update()
    try:
        img.pack()
    except RuntimeError:
        pass


def recolour(ob, paint, buffer, state, pal, keep=()):
    """Re-colour the paint image from the buffer — but leave the artist's
    UNRESOLVED pixels standing.

    §4.1 says the paint image is re-coloured from the buffer, and §4.4 says a
    sticky refusal "clears only by re-painting the pixel".  Taken literally the
    first erases the second: an off-palette pixel never reached the buffer, so
    re-colouring from the buffer paints the artist's mistake away, the next
    resolve sees a colour the palette accepts, and the refusal clears itself
    without anyone fixing anything.  The refused pixels are the one thing the
    re-colour has to preserve, and preserving them is also what makes the
    refusal VISIBLE: the bad pixel stays on screen until it is repainted."""
    px = expand(buffer, clut_entries(ob, state, pal))
    for pixel, rgb in keep:
        j = pixel * 4
        if 0 <= j + 3 < len(px):
            px[j], px[j + 1], px[j + 2] = (c / 255.0 for c in rgb)
    paint.pixels.foreach_set(px)
    _persist(paint)
    _CACHE[paint.name] = px


def _remember(ob, sheet, state, pal):
    book = section(ob, "paint_palette", {}) or {}
    book[sheet] = [int(state), int(pal)]
    ob["exmateria_map/paint_palette"] = json.dumps(book)


def outgoing_palette(ob, sheet):
    got = (section(ob, "paint_palette", {}) or {}).get(sheet)
    if isinstance(got, list) and len(got) == 2:
        return int(got[0]), int(got[1])
    return active_palette(ob)


def ensure_paint_image(ob, sheet):
    """The artist's paint target, created on demand under the active palette."""
    idx = index_image(ob, sheet)
    if idx is None:
        return None
    name = paint_image_name(sheet)
    img = bpy.data.images.get(name)
    if img is not None and tuple(img.size) == (SHEET_W, SHEET_H):
        return img
    if img is not None:
        bpy.data.images.remove(img)
    img = bpy.data.images.new(name, SHEET_W, SHEET_H, alpha=False,
                              float_buffer=True)
    img.colorspace_settings.name = "Non-Color"
    state, pal = active_palette(ob)
    recolour(ob, img, read_buffer(idx), state, pal)
    _remember(ob, sheet, state, pal)
    return img


# ---------------------------------------------------------------------------
# §3.6 / §4.4 — the sticky off-palette list.
# ---------------------------------------------------------------------------

def sticky(ob):
    return section(ob, "off_palette", []) or []


def _store_sticky(ob, entries):
    if entries:
        ob["exmateria_map/off_palette"] = json.dumps(entries)
    elif "exmateria_map/off_palette" in ob:
        del ob["exmateria_map/off_palette"]


def _hexcolor(rgb):
    return "#%02X%02X%02X" % tuple(rgb)


def _bbox(pixels):
    xs = [p % SHEET_W for p in pixels]
    ys = [p // SHEET_W for p in pixels]
    return [min(xs), min(ys), max(xs), max(ys)]


def unresolved(ob):
    """[(pixel, (r, g, b))] — every pixel still on the sticky list, with the
    colour the artist painted it.  What a re-colour must not erase."""
    out = []
    for entry in sticky(ob):
        rgb = entry.get("color") or "#000000"
        rgb = tuple(int(rgb.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        for p in entry.get("pixels") or ():
            out.append((p, rgb))
    return out


def _gate(ob, off, entries, px, summary):
    """Add the newly-painted off-palette pixels, and drop the stored ones the
    artist has since repainted to a colour this palette accepts (§4.4)."""
    accept = set(entries)
    kept = []
    for entry in sticky(ob):
        pixels = entry.get("pixels") or []
        if not pixels:
            kept.append(entry)          # over the cap: no pixels to re-check
            continue
        still = [p for p in pixels
                 if tuple(int(round(px[p * 4 + k] * 255.0))
                          for k in range(3)) not in accept]
        summary["cleared"] += len(pixels) - len(still)
        if still:
            kept.append(dict(entry, pixels=still, count=len(still),
                             bbox=_bbox(still)))
    for rgb, pixels in off.items():
        same = next((e for e in kept if e.get("color") == _hexcolor(rgb)), None)
        if same is not None:
            pixels = sorted(set((same.get("pixels") or []) + pixels))
            kept.remove(same)
        kept.append({"color": _hexcolor(rgb), "count": len(pixels),
                     "bbox": _bbox(pixels),
                     "pixels": pixels[:STICKY_PIXEL_CAP]})
    _store_sticky(ob, kept)
    summary["off_palette"] = sum(e.get("count", 0) for e in kept)


# ---------------------------------------------------------------------------
# §4.1 — the one resolve code path.
# ---------------------------------------------------------------------------

def resolve(ob, sheet=None, incoming=None):
    """Diff, resolve, gate, write back, re-colour.  Returns a summary dict.

    `incoming` is the palette the paint image should end up coloured under;
    the active one by default."""
    state_now, pal_now = active_palette(ob)
    if sheet is None:
        sheet = sheet_of_state(ob, state_now)
    summary = {"sheet": sheet, "painted": 0, "resolved": 0, "off_palette": 0,
               "cleared": 0}
    if not sheet:
        return summary
    idx = index_image(ob, sheet)
    paint = bpy.data.images.get(paint_image_name(sheet))
    if idx is None or paint is None:
        return summary

    out_state, out_pal = outgoing_palette(ob, sheet)
    entries = clut_entries(ob, out_state, out_pal)
    buffer = read_buffer(idx)
    px = _floats(paint)
    # The fast path: nothing was painted.  `expand` costs a megabyte of float
    # writes, so compare against what this module last wrote and only rebuild
    # that when the cache is cold (a reopened .blend).
    was = _CACHE.get(paint.name)
    if was is None:
        was = expand(buffer, entries)
        _CACHE[paint.name] = was
    off = {}
    if px != was:
        # §3.5: a changed pixel matching several entries takes the LOWEST
        # index.  Duplicate entries within one 16-set are legal, so the match
        # rule has to be total.
        lookup = {}
        for i, e in enumerate(entries):
            lookup.setdefault(e, i)
        for i in range(len(buffer)):
            j = i * 4
            if (px[j] == was[j] and px[j + 1] == was[j + 1]
                    and px[j + 2] == was[j + 2]):
                continue                      # §3.4: never re-resolved
            summary["painted"] += 1
            hit = lookup.get(tuple(int(round(px[j + k] * 255.0))
                                   for k in range(3)))
            if hit is None:
                off.setdefault(tuple(int(round(px[j + k] * 255.0))
                                     for k in range(3)), []).append(i)
            else:
                buffer[i] = hit
                summary["resolved"] += 1
    _gate(ob, off, entries, px, summary)

    if summary["resolved"]:
        # §4.2: the mesh always previews the COMMITTED state, so the index
        # image moves only here, on a successful resolve.
        n = len(buffer)
        ipx = array.array("f", bytes(4 * n * 4))
        for i in range(n):
            ipx[i * 4] = float(buffer[i])
            ipx[i * 4 + 3] = 1.0
        idx.pixels.foreach_set(ipx)
        _persist(idx)

    state_in, pal_in = incoming if incoming else (state_now, pal_now)
    if summary["resolved"] or summary["painted"] or \
            (state_in, pal_in) != (out_state, out_pal):
        recolour(ob, paint, buffer, state_in, pal_in, unresolved(ob))
    _remember(ob, sheet, state_in, pal_in)
    return summary


# ---------------------------------------------------------------------------
# §4.2 — the triggers.
# ---------------------------------------------------------------------------

def on_trigger(ob):
    """Face select, state change, override change, export — §3.3's set.  A
    scene with no paint image has nothing to resolve and costs nothing."""
    if ob is None or "exmateria_map/base" not in ob:
        return None
    sheet = sheet_of_state(ob, int(ob.get("exmateria_map/preview_state") or 0))
    if not sheet or bpy.data.images.get(paint_image_name(sheet)) is None:
        return None
    return resolve(ob, sheet)


def _override_update(self, context):
    on_trigger(self)


class MAP_OT_apply_paint(Operator):
    """Resolve the paint image into the index buffer now (§4.2's explicit
    trigger)."""
    bl_idname = "exmateria_map.apply_paint"
    bl_label = "Apply paint"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(markers(context.scene))

    def execute(self, context):
        found = markers(context.scene)
        ob = context.object if context.object in found else found[0]
        state = int(ob.get("exmateria_map/preview_state") or 0)
        sheet = sheet_of_state(ob, state)
        if sheet:
            ensure_paint_image(ob, sheet)
        s = resolve(ob)
        if s["off_palette"]:
            self.report({"ERROR"},
                        f"{s['resolved']} pixel(s) resolved, "
                        f"{s['off_palette']} OFF-PALETTE — export refuses "
                        f"while any remain")
        else:
            self.report({"INFO"}, f"{s['painted']} painted, "
                                  f"{s['resolved']} resolved, "
                                  f"{s['cleared']} cleared")
        return {"FINISHED"}


class MAP_OT_paint_sheet(Operator):
    """Open this arrangement's sheet as the paint target (§4's button)."""
    bl_idname = "exmateria_map.paint_sheet"
    bl_label = "Paint sheet"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return bool(markers(context.scene))

    def execute(self, context):
        found = markers(context.scene)
        ob = context.object if context.object in found else found[0]
        state, pal = active_palette(ob)
        sheet = sheet_of_state(ob, state)
        img = ensure_paint_image(ob, sheet) if sheet else None
        if img is None:
            self.report({"ERROR"}, "no decodable sheet in this arrangement")
            return {"CANCELLED"}
        for area in getattr(context.screen, "areas", ()) or ():
            if area.type == "IMAGE_EDITOR":
                area.spaces.active.image = img
                break
        # #423 measured that a forced brush setting does NOT lock the artist to
        # the palette — a Palette is a shelf, not a lock — so the GATE, not the
        # brush, is what keeps a colour honest.  The shelf is still worth
        # setting up.
        try:
            context.tool_settings.image_paint.canvas = img
            context.tool_settings.image_paint.mode = "IMAGE"
        except (AttributeError, TypeError):
            pass
        self.report({"INFO"}, f"{img.name} open under state {state} "
                              f"palette {pal}")
        return {"FINISHED"}


class MAP_PT_paint(Panel):
    """N-panel: the active palette, the two buttons, and the sticky list."""
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"
    bl_category = "Map"
    bl_label = "ExMateria Map Paint"
    bl_order = 2

    def draw(self, context):
        ob = context.object
        layout = self.layout
        if ob is None or "exmateria_map/base" not in ob:
            return
        state, pal = active_palette(ob)
        sheet = sheet_of_state(ob, state)
        if not sheet:
            layout.label(text="no texture sheet in this arrangement",
                         icon="INFO")
            return
        layout.label(text=f"sheet {sheet}", icon="TEXTURE")
        layout.label(text=f"active palette: state {state}, CLUT row {pal}")
        layout.prop(ob, "exmateria_map_palette_override")
        row = layout.row(align=True)
        row.operator(MAP_OT_paint_sheet.bl_idname, icon="BRUSH_DATA")
        row.operator(MAP_OT_apply_paint.bl_idname, icon="CHECKMARK")
        entries = sticky(ob)
        if not entries:
            layout.label(text="no off-palette pixels", icon="CHECKMARK")
            return
        box = layout.box()
        box.label(text=f"{len(entries)} off-palette colour(s) — EXPORT REFUSES",
                  icon="ERROR")
        for e in entries[:8]:
            box.label(text=f"{e.get('color')}  {e.get('count')} px  "
                           f"bbox {e.get('bbox')}")
        if len(entries) > 8:
            box.label(text=f"... and {len(entries) - 8} more")


CLASSES = (MAP_OT_apply_paint, MAP_OT_paint_sheet, MAP_PT_paint)


def register():
    bpy.types.Object.exmateria_map_palette_override = IntProperty(
        name="Palette override",
        description="Resolve painted pixels against this CLUT row instead of "
                    "the selected face's; -1 follows the selection (§3.2)",
        default=-1, min=-1, max=15, update=_override_update)
    for c in CLASSES:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
    del bpy.types.Object.exmateria_map_palette_override
