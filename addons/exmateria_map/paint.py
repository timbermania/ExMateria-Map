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
import textwrap

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


#: Blender's own colour shelf, carrying the ACTIVE CLUT row's 16 entries.
PALETTE_SHELF = "exmateria_map/CLUT"


def sync_palette_shelf(ob, state, pal):
    """Put the active row's 16 colours on Blender's colour shelf and bind it.

    The gate is an EXACT byte match (`interchange-export-v1.md` §3.6), and
    nothing in this addon ever showed the artist what the sixteen legal colours
    are -- so every colour was chosen by eye against a gate that accepts only a
    perfect hit.  #423's finding was that forcing a brush setting cannot LOCK
    the artist to the palette ("a Palette is a shelf, not a lock"), and the
    conclusion drawn was to let the GATE keep colours honest.  That is right,
    and it is not a reason to withhold the shelf: the gate says no, the shelf
    says what yes looks like.

    Measured: a `PaletteColor` stores `byte / 255` exactly (41, 24, 8 and
    180, 156, 123 both round-trip), so a click on a swatch hands the brush a
    colour the gate accepts rather than one that misses by a unit.

    Rebuilt rather than recreated, so the artist's own palette selection in the
    Tool tab keeps pointing at the same datablock as the row changes."""
    entries = clut_entries(ob, state, pal)
    if not entries:
        return None
    shelf = bpy.data.palettes.get(PALETTE_SHELF)
    if shelf is None:
        shelf = bpy.data.palettes.new(PALETTE_SHELF)
    while len(shelf.colors) > len(entries):
        shelf.colors.remove(shelf.colors[len(shelf.colors) - 1])
    while len(shelf.colors) < len(entries):
        shelf.colors.new()
    for slot, rgb in zip(shelf.colors, entries):
        slot.color = tuple(c / 255.0 for c in rgb)
    scene = getattr(bpy.context, "scene", None)
    ip = getattr(getattr(scene, "tool_settings", None), "image_paint", None)
    if ip is not None:
        ip.palette = shelf
    return shelf


# ---------------------------------------------------------------------------
# The brush: whose property PAINTS, and the one-time seed.
# ---------------------------------------------------------------------------

def paint_owner(ip, unified_flag):
    """The datablock a paint property is really read from, or None.

    `unified_paint_settings` does not MIRROR the brush, it REPLACES it, and the
    flags that switch it default to on.  Measured headful, probe phase `brush`
    (`-b` cannot reach any of this: `ImagePaint.brush` is read-only and
    `bpy.data.brushes` is empty under `--factory-startup`):

    * one stroke armed with `brush.color` red AND `unified.color` blue lands
      **blue** at the default `use_unified_color = True`, and **red** with the
      flag off.  Nothing consults the shelf's active swatch at stroke time.
    * a swatch CLICK follows the same rule -- probe phase `swatchclick`, which
      clicks the template for real through `Window.event_simulate`: unified on,
      the click writes `unified.color` and leaves `brush.color` untouched;
      unified off, the other way round.

    So the click-to-arm path is sound end to end, and a panel that draws
    `brush.color` unconditionally is showing a value that neither paints nor
    receives the click -- which is why `painting with:` photographed BLACK
    while the artist's clicks were landing in `unified.color`.

    This is Blender's own rule, from `bl_ui/properties_paint_common.py`
    (`UnifiedPaintPanel.prop_unified_color`: `prop_owner = ups if
    ups.use_unified_color else brush`).  Written out rather than imported
    because `seed_brush` needs it with no panel in hand, and a UI helper is not
    somewhere an operator should be reaching.
    """
    # No `ip is None` guard: `getattr(None, ...)` is not an error, so the two
    # lines below already answer None for it. A guard that cannot be made to
    # fail is a line no arm can grade -- seeded, and the harness stayed green.
    ups = getattr(ip, "unified_paint_settings", None)
    if ups is not None and getattr(ups, unified_flag, False):
        return ups
    return getattr(ip, "brush", None)


#: The mark that makes the brush setup a DEFAULT rather than a rule.  On the
#: SCENE, beside the `tool_settings` it guards: the settings and the record that
#: they were seeded are one datablock, saved and loaded together, so a `.blend`
#: can never come back carrying one without the other.
BRUSH_SEED_MARK = "exmateria_map/brush_seeded"

#: "default the brush to pixel and size to 1", as two properties.
BRUSH_SEED_SIZE = 1
BRUSH_SEED_FALLOFF = "CONSTANT"


def seed_brush(scene, force=False):
    """Set the brush up ONCE, and then never again.  Returns what it did.

    ADR-0004 says the addon must *force* the brush.  The measurement behind
    that stands; the mechanism does not.  Reported from use: *"I don't know, I
    want to force it -- just when I open the workspace, that should be my brush
    as a default."*  So this SEEDS, in the shape `ensure_rig_exposure` already
    uses for rig Overrides: written at one defined moment, idempotent, and
    never re-asserted.  The artist sets the size to 4 and it stays 4 -- for the
    rest of the session and across a save, because `tool_settings` lives in the
    `.blend` -- and nothing here watches it or corrects it.

    That also dissolves the objection that kept #423 unbuilt for months.  The
    shipped comment refused on the grounds that a forced setting *"does NOT
    lock the artist to the palette"*, which is true and is about a different
    claim: a Palette is a shelf and does not constrain which colour is CHOSEN,
    while the falloff is what the brush DOES to that colour on the way down.
    A default is not trying to lock anything, so the force/lock axis the
    comment argued on was never the question.

    What is set, and why only this much:

    * `curve_distance_falloff_preset` -> `CONSTANT`.  It defaults to `CUSTOM`,
      which feathers every dab's edge.  The export gate is an EXACT byte match,
      so a feathered pixel is not a cosmetic near-miss -- it is a refusal.
      Re-measured on THIS addon's canvas, one stroke per arm on MAP022's sheet
      (probe phase `brush`): `CUSTOM` leaves **18.0%** of the stroke's pixels
      off-palette, `CONSTANT` leaves **0.0%**.  #423 measured 24.8% -> 0.0% on
      an sRGB byte image loaded from disk; the shape reproduces on a Non-Color
      float one.
    * the size, to 1, on whoever `paint_owner` says owns it.  `brush.size = 1`
      alone is discarded while `use_unified_size` is on -- which is its default.

    Nothing else.  #423 measured `hardness` and `use_paint_antialiasing` at no
    measurable effect and `use_accumulate` at a partial one, and the falloff
    preset is sufficient alone; every property this does not touch is one the
    artist keeps.

    Returns `"no brush"` WITHOUT marking when the brush is not resolved yet:
    `ImagePaint.brush` comes from the asset system on entering a paint mode and
    is None before that, so a seed that marked itself done having set nothing
    would be a default that silently never happened.  The callers are the two
    moments the artist named -- the workspace being built, and `Paint sheet` --
    and the mark is what stops the second from being the force they ruled out.
    """
    if scene is None:
        return "no scene"
    if not force and scene.get(BRUSH_SEED_MARK):
        return "already"
    ip = getattr(getattr(scene, "tool_settings", None), "image_paint", None)
    brush = getattr(ip, "brush", None)
    if brush is None:
        return "no brush"
    size_owner = paint_owner(ip, "use_unified_size")
    try:
        brush.curve_distance_falloff_preset = BRUSH_SEED_FALLOFF
        if size_owner is not None:
            size_owner.size = BRUSH_SEED_SIZE
    except (AttributeError, TypeError, ValueError) as e:
        return f"refused: {type(e).__name__}: {e}"
    scene[BRUSH_SEED_MARK] = 1
    return "seeded"


def active_face_index(ob):
    """The active face, correct in BOTH modes -- or None when there is none.

    `me.polygons.active` **FREEZES** in Edit Mode: measured, it holds its last
    Object-Mode value while the artist clicks from face to face, so anything
    watching it cannot fire in the only mode that can select a face.  The live
    answer is in the BMesh.  Third Edit-Mode assumption in this addon to be
    wrong the same way -- see `02b99279a` (the push) and `active_palette`.
    """
    me = ob.data
    if ob.mode == "EDIT":
        import bmesh
        bm = bmesh.from_edit_mesh(me)
        f = bm.faces.active
        if f is not None and f.is_valid and f.select:
            return f.index
        return next((g.index for g in bm.faces if g.select), None)
    a = me.polygons.active
    if isinstance(a, int) and 0 <= a < len(me.polygons):
        return a
    return next((k for k, q in enumerate(me.polygons) if q.select), None)


def active_palette(ob):
    """§3.2 — the single active palette: the override when it is set, else the
    selected face's CLUT row.  Works in Object AND Edit mode.

    Never a union across states or palettes: a union makes the recovered index
    ambiguous, because the same colour may be entry 2 in one CLUT and entry 9
    in another, and the document carries one `palette_id` per face, not a
    colour -> (state, index) table."""
    state = int(ob.get("exmateria_map/preview_state") or 0)
    override = getattr(ob, "exmateria_map_palette_override", -1)
    if isinstance(override, int) and override >= 0:
        return state, int(override)
    me = ob.data
    if ob.mode == "EDIT":
        # In Edit Mode the face attributes live in the BMesh and
        # `me.attributes["palette_id"].data` is EMPTY -- reading it raises
        # IndexError, which in a panel `draw` means the panel does not render
        # at all.  Same species as `02b99279a`, in the surface the artist uses
        # to CHOOSE a face.  Reading the BMesh also makes the row LIVE: click a
        # face and the panel follows it, without leaving the mode.
        import bmesh
        bm = bmesh.from_edit_mesh(me)
        layer = bm.faces.layers.int.get("palette_id")
        if layer is not None and len(bm.faces):
            f = bm.faces.active
            if f is None or not f.is_valid or not f.select:
                f = next((g for g in bm.faces if g.select), None)
            if f is not None:
                return state, max(0, min(15, int(f[layer])))
        return state, 0
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


def painting_of(ob, sheet):
    """The **Painting** for `sheet`, or `None` on the direct-paint path.

    ADR-0186 decision 7: *the presence of `source_art` is the declaration* --
    there is no mode switch and no flag to keep in step.  Amendment 3 decision
    17 applies that to the surface rather than the document, so everything
    that differs between the two authoring paths asks this one question here.

    `convert_op` imports this module, so the name it owns is fetched at call
    time rather than at import.
    """
    if not sheet:
        return None
    from .convert_op import source_art_name
    return bpy.data.images.get(source_art_name(sheet))


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


def _remember(ob, sheet, state, pal, entries=None):
    """Record the palette the paint image is now coloured under.

    The sixteen COLOURS are remembered alongside the (state, palette) pair,
    because since palette authoring landed the pair no longer identifies them:
    an artist can recolour an entry without the state or the row moving.  See
    `outgoing_entries` for what that memory is load-bearing for."""
    book = section(ob, "paint_palette", {}) or {}
    book[sheet] = [int(state), int(pal),
                   [list(e) for e in (entries if entries is not None
                                      else clut_entries(ob, state, pal))]]
    ob["exmateria_map/paint_palette"] = json.dumps(book)


def outgoing_palette(ob, sheet):
    got = (section(ob, "paint_palette", {}) or {}).get(sheet)
    if isinstance(got, list) and len(got) >= 2:
        return int(got[0]), int(got[1])
    return active_palette(ob)


def outgoing_entries(ob, sheet, state, pal):
    """The sixteen colours the paint image was LAST WRITTEN under.

    Not the same thing as the row's colours *now*: a CLUT edit moves the second
    and leaves the first where it was, and `resolve` needs the first to know
    what the artist changed.  A `.blend` saved before this was remembered has
    only the (state, palette) pair, and for those the row's current colours ARE
    what it was written under -- nothing could have edited them."""
    got = (section(ob, "paint_palette", {}) or {}).get(sheet)
    if isinstance(got, list) and len(got) >= 3 and got[2]:
        return [tuple(int(c) for c in e) for e in got[2]]
    return clut_entries(ob, state, pal)


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


def _gate(ob, off, entries, px, summary, buffer, lookup):
    """Add the newly-painted off-palette pixels, and drop the stored ones the
    palette now accepts (§4.4) -- whether the artist repainted the PIXEL or
    authored the ENTRY.

    A refusal is **pending**, not resolved: the buffer index does not
    represent it, which is why the re-colour has to paint it back from the
    sticky list rather than from the buffer.  So a stored pixel that becomes
    representable has to LAND on an index here.  Dropping it from the list
    without moving the buffer clears the refusal, silences export, and ships
    the pixel's import-time colour -- the artist's paint gone with nothing
    saying so.

    That is not the same event as the diff loop's, and it does not enter
    `painted`: the pixel was painted in an earlier pass.  It is counted as
    `recovered`, and it is why the index image moves on more than `resolved`.
    """
    kept = []
    for entry in sticky(ob):
        pixels = entry.get("pixels") or []
        if not pixels:
            kept.append(entry)          # over the cap: no pixels to re-check
            continue
        still = []
        for p in pixels:
            hit = lookup.get(tuple(int(round(px[p * 4 + k] * 255.0))
                                   for k in range(3)))
            if hit is None:
                still.append(p)
            elif buffer[p] != hit:
                # The diff loop already moved any pixel painted THIS pass, so
                # an index that still disagrees is one the palette rescued.
                buffer[p] = hit
                summary["recovered"] += 1
        summary["cleared"] += len(pixels) - len(still)
        if still:
            kept.append(dict(entry, pixels=still, count=len(still),
                             bbox=_bbox(still)))
    # Merge by NAME through an index, not by scanning `kept`.  The scan this
    # replaces re-evaluated `_hexcolor(rgb)` once per entry examined, inside a
    # walk over a list that grows as the loop runs, and `list.remove` was a
    # second linear pass -- so the whole gate was QUADRATIC in distinct
    # off-palette colours.  Measured on a real sheet by
    # `tests/blender_texture_stress.py`: 1,023 entries cost 0.088 s, 4,093
    # cost 1.356 s and 16,378 cost 21.536 s -- an exponent of 1.99, and 99.2%
    # of the whole resolve at 16k.  A full-gamut sheet has 32,768 distinct
    # colours and face-select is a trigger.
    #
    # The order is not incidental and is pinned by the round-trip harness: a
    # merged entry leaves its position and joins the tail, because that is the
    # order export prints its refusal lines in.
    at = {}
    for i, entry in enumerate(kept):
        at.setdefault(entry.get("color"), i)
    merged = set()
    tail = []
    for rgb, pixels in off.items():
        name = _hexcolor(rgb)
        i = at.get(name)
        if i is not None:
            merged.add(i)
            pixels = sorted(set((kept[i].get("pixels") or []) + pixels))
        tail.append({"color": name, "count": len(pixels),
                     "bbox": _bbox(pixels),
                     "pixels": pixels[:STICKY_PIXEL_CAP]})
    if merged:
        kept = [e for i, e in enumerate(kept) if i not in merged]
    kept.extend(tail)
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
    # `refused` is THIS pass; `off_palette` is the standing sticky total over
    # every pass, which is what export gates on.  Two different numbers, and
    # only the first one makes `resolved + refused == painted` -- no painted
    # pixel is silently lost -- an invariant anything above this can assert
    # (ADR-0007).
    summary = {"sheet": sheet, "painted": 0, "resolved": 0, "refused": 0,
               "recovered": 0, "off_palette": 0, "cleared": 0}
    if not sheet:
        return summary
    idx = index_image(ob, sheet)
    paint = bpy.data.images.get(paint_image_name(sheet))
    if idx is None or paint is None:
        return summary

    out_state, out_pal = outgoing_palette(ob, sheet)
    entries = clut_entries(ob, out_state, out_pal)
    # What the paint image is coloured under RIGHT NOW, which is the row's
    # current colours only until someone authors one of them.
    was_entries = outgoing_entries(ob, sheet, out_state, out_pal)
    buffer = read_buffer(idx)
    px = _floats(paint)
    # The fast path: nothing was painted.  `expand` costs a megabyte of float
    # writes, so compare against what this module last wrote and only rebuild
    # that when the cache is cold (a reopened .blend).
    #
    # The rebuild MUST use `was_entries`, not `entries`.  The cache is
    # per-process, so the second session with a file has none; rebuilding under
    # the current colours after a CLUT edit makes the baseline the NEW colour
    # while the image still holds the OLD one, and every pixel using that entry
    # reads as freshly painted in a colour the CLUT no longer contains.  The
    # artist gets a refused export -- or, where the old colour still matches
    # some other entry, a silently re-indexed sheet -- for a gesture they made
    # on the palette, not on the pixels.
    was = _CACHE.get(paint.name)
    if was is None:
        was = expand(buffer, was_entries)
        _CACHE[paint.name] = was
    off = {}
    # §3.5: a changed pixel matching several entries takes the LOWEST index.
    # Duplicate entries within one 16-set are legal, so the match rule has to
    # be total.  Built unconditionally -- sixteen entries -- because the gate
    # needs it even on a pass that painted nothing, to land the refusals an
    # authored entry has just made representable.
    lookup = {}
    for i, e in enumerate(entries):
        lookup.setdefault(e, i)
    if px != was:
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
                summary["refused"] += 1
            else:
                buffer[i] = hit
                summary["resolved"] += 1
    _gate(ob, off, entries, px, summary, buffer, lookup)

    if summary["resolved"] or summary["recovered"]:
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
    # An authored colour re-colours for the same reason a state change does:
    # the palette the image is coloured under is no longer the one in force.
    # Without this the artist recolours an entry and the canvas they paint on
    # keeps showing the colour it replaced.
    recoloured = list(was_entries) != list(entries)
    if summary["resolved"] or summary["painted"] or summary["recovered"] \
            or recoloured or (state_in, pal_in) != (out_state, out_pal):
        recolour(ob, paint, buffer, state_in, pal_in, unresolved(ob))
    _remember(ob, sheet, state_in, pal_in)
    sync_palette_shelf(ob, state_in, pal_in)
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


# ---------------------------------------------------------------------------
# Reaching the artist's Image Editors.
# ---------------------------------------------------------------------------

#: What an Image Editor may be showing that this add-on must NOT displace.
#: A Render Result and a compositor Viewer Node are not the artist's files --
#: they are windows onto a running process, and the Rendering and Compositing
#: workspaces exist to hold them.  Measured: a stock 5.2 startup has FIVE
#: Image Editor areas (Compositing, Rendering, Shading, Texture Paint, UV
#: Editing), so a walk that does not exclude these two would blow away a render
#: the artist is looking at, in a workspace they never asked this button about.
PROTECTED_IMAGE_TYPES = {"RENDER_RESULT", "COMPOSITING"}

#: The hint the panel and the operator both give when there is nowhere to show
#: the sheet.  One string, so the two surfaces cannot drift apart.
NO_EDITOR_HINT = ("open the 'UV Editing' or 'Texture Paint' tab along the top "
                  "of the window, or split an area (drag a corner) and set its "
                  "editor type to Image Editor -- or skip the editor and paint "
                  "on the model itself: Texture Paint mode in the 3D viewport "
                  "already has this image as its canvas")


def image_editor_spaces():
    """Every Image Editor space in EVERY workspace, as (space, protected).

    An add-on does not rearrange the artist's screen: nothing in Blender's own
    bundled scripts sets `area.type` or opens a window to show the user
    something.  It fills the editors that are already there.  This is
    `CLIP_spaces_walk` (`bl_operators/clip.py`) with its `all_screens` arm
    taken -- `bpy.data.screens`, not `context.screen`, which is the whole
    defect: the artist presses the button from Layout, Layout has no Image
    Editor, and the old walk found nothing while the report said "open".
    Walking every screen loads UV Editing's editor BEFORE the artist switches
    to it, so switching is all that is left to do.

    Every space of the area, not `spaces.active`: an area keeps one space per
    editor type it has been, and Compositing's Image Editor area carries a
    NODE_EDITOR space alongside.  `space.type` uses the same identifiers as
    `area.type` (`clip.py` passes one string for both)."""
    out = []
    for screen in getattr(bpy.data, "screens", ()) or ():
        for area in getattr(screen, "areas", ()) or ():
            if area.type != "IMAGE_EDITOR":
                continue
            for space in area.spaces:
                if space.type != "IMAGE_EDITOR":
                    continue
                cur = getattr(space, "image", None)
                out.append((space,
                            cur is not None
                            and cur.type in PROTECTED_IMAGE_TYPES))
    return out


def show_in_image_editors(img):
    """Load `img` into every unprotected Image Editor; return (shown, skipped).

    Returning the COUNTS is the point.  This function opens nothing, so the
    caller cannot honestly say "opened", and it is the zero here that the
    report and the panel turn into `NO_EDITOR_HINT` -- distinguishing "there is
    nowhere to show it" from "every editor is holding a render", which are
    different problems with different answers."""
    shown = skipped = 0
    for space, protected in image_editor_spaces():
        if protected:
            skipped += 1
            continue
        space.image = img
        shown += 1
    return shown, skipped


class MAP_OT_apply_paint(Operator):
    """Resolve the paint image into the index buffer now (§4.2's explicit
    trigger)."""
    bl_idname = "exmateria_map.apply_paint"
    bl_label = "Apply paint"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        """Decision 18: it stays, on the DIRECT-PAINT path only.

        Not merely undrawn on a converted map -- `execute` calls
        `ensure_paint_image`, which would rebuild the gate's canvas on a map
        whose Painting survives the compile and so has nothing to refuse.  A
        button reachable from search that resurrects a gate the panel has
        stopped drawing is the worse half of the two."""
        found = markers(context.scene)
        if not found:
            return False
        ob = context.object if context.object in found else found[0]
        state = int(ob.get("exmateria_map/preview_state") or 0)
        return painting_of(ob, sheet_of_state(ob, state)) is None

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
        # Decision 17.  `Paint sheet` has never handed anyone the indices --
        # it hands them a picture and arms the brush on it.  Which picture is
        # the only thing that differs: on the direct-paint path the Painting
        # is scratch, expanded from the indices by `recolour`, and the gate
        # resolves strokes back down; on a converted map the Painting is the
        # authored half and the Sheet is the derived one, so a stroke on the
        # index-derived copy would be a stroke on what is now a cache.
        painting = painting_of(ob, sheet)
        if painting is not None:
            img = painting
        else:
            img = ensure_paint_image(ob, sheet) if sheet else None
            # The shelf is the gate's surface: sixteen colours under a label
            # saying everything else is refused.  Nothing is refused here.
            sync_palette_shelf(ob, state, pal)
        if img is None:
            self.report({"ERROR"}, "no decodable sheet in this arrangement")
            return {"CANCELLED"}
        shown, skipped = show_in_image_editors(img)
        # This used to say: *"#423 measured that a forced brush setting does
        # NOT lock the artist to the palette -- a Palette is a shelf, not a
        # lock -- so the GATE, not the brush, is what keeps a colour honest."*
        # That reads one #423 finding as a reason to skip another, and they are
        # about different hops.  *A Palette is a shelf, not a lock* is about
        # which colour is CHOSEN: `brush.color` accepts an arbitrary value
        # while a palette is set, which is why the gate exists.  *The addon
        # must force the brush* is about what the brush DOES to that colour on
        # the way down: the default falloff feathers the dab's edge and put
        # 18.0% of a stroke off-palette even when the artist picked a legal
        # entry (re-measured on this canvas, probe phase `brush`).  The gate
        # cannot fix that; it can only refuse it.
        #
        # So the brush IS set up -- once, as a default, by `seed_brush`, and
        # not as the force ADR-0004 asked for.  Here as well as at
        # `workspace.build()` because the brush is an asset that does not exist
        # until a paint mode has been entered, and this is the other moment the
        # artist named; the mark on the scene is what keeps the second call
        # from re-stomping settings they have since changed.
        seeded = seed_brush(context.scene)
        try:
            context.tool_settings.image_paint.canvas = img
            context.tool_settings.image_paint.mode = "IMAGE"
        except (AttributeError, TypeError):
            pass
        head = (f"{img.name}, state {state}, true colour"
                if painting is not None
                else f"{img.name}, state {state} palette {pal}")
        if seeded == "seeded":
            head += f", brush seeded to {BRUSH_SEED_FALLOFF}/{BRUSH_SEED_SIZE}"
        if shown:
            self.report({"INFO"}, f"{head}, shown in {shown} Image Editor(s)")
        elif skipped:
            self.report({"WARNING"},
                        f"{head} -- the {skipped} open Image Editor(s) are all "
                        f"showing a render result, which this button will not "
                        f"displace. To see the sheet: {NO_EDITOR_HINT}")
        else:
            self.report({"WARNING"},
                        f"{head} -- NO Image Editor is open, so nothing "
                        f"appeared. To see it: {NO_EDITOR_HINT}")
        return {"FINISHED"}


def _brush_box(layout, _ip):
    """The brush's own controls, at the bottom of the Paint panel.

    Drawn on BOTH authoring paths.  The palette block above it is a statement
    about the indexed path -- the sixteen legal colours, the REFUSED labels --
    and decision 17 rightly withholds all of it on a converted map.  The brush
    is not: size and falloff are the same question whichever path the artist is
    on, and this is the half they asked to have in this tab.
    """
    # Reported from use: *"I like how you put the tool options at the bottom
    # now so I can have both -- but can we also expose other things down there,
    # especially brush size."*  Same wall as the picker above, same answer: a
    # sidebar region shows exactly ONE tab, so what the artist needs while
    # painting has to be brought here rather than chased in `Tool`.
    #
    # Every row asks `paint_owner` who owns the property instead of reaching
    # for the brush, and draws the unified flag beside it, because the flag is
    # what decides whether the number above it is the one that paints -- and it
    # defaults to on, which is how "size to 1" went nowhere. Blender draws its
    # own brush rows the same way (`UnifiedPaintPanel.prop_unified`).
    _ups = getattr(_ip, "unified_paint_settings", None)
    _size_owner = paint_owner(_ip, "use_unified_size")
    _brush = getattr(_ip, "brush", None)
    if _size_owner is not None or _brush is not None:
        box = layout.box()
        box.label(text="brush", icon="BRUSH_DATA")
        if _size_owner is not None:
            row = box.row(align=True)
            row.prop(_size_owner, "size", text="size")
            if _ups is not None:
                row.prop(_ups, "use_unified_size", text="",
                         icon="BRUSHES_ALL")
        if _brush is not None:
            # The falloff IS what "pixel" means here: measured 18.0% of a
            # stroke off-palette at the `CUSTOM` default and 0.0% at
            # `CONSTANT`, and an off-palette pixel is an export refusal,
            # not a soft edge. Shown so the artist can see which one they
            # are on, and put it back without leaving the tab.
            box.prop(_brush, "curve_distance_falloff_preset",
                     text="falloff")
            _strength = paint_owner(_ip, "use_unified_strength")
            if _strength is not None:
                row = box.row(align=True)
                row.prop(_strength, "strength", text="strength")
                if _ups is not None:
                    row.prop(_ups, "use_unified_strength", text="",
                             icon="BRUSHES_ALL")
            box.prop(_brush, "blend", text="blend")
        # The seed, as a button. `seed_brush` runs once per `.blend` and
        # never re-asserts, which is the whole point -- so the way back to
        # the defaults after the artist has moved them is a thing they
        # press, not something that quietly happens behind them.
        box.operator(MAP_OT_seed_brush.bl_idname, icon="EVENT_P")


class MAP_OT_seed_brush(Operator):
    """Put the brush back to the pixel default: hard-edged, one pixel wide.

    `seed_brush` is a DEFAULT -- it runs once per `.blend` and never re-asserts
    -- so this is the way back to it after the artist has moved the settings,
    and it is a button precisely so that going back is their decision. `force`
    is the whole difference between the two.
    """
    bl_idname = "exmateria_map.seed_brush"
    bl_label = "Pixel brush"
    bl_description = ("Set the brush to a hard 1-pixel stamp -- the falloff "
                      "preset the exact-match export gate needs. Your own "
                      "settings are not restored afterwards; press it again "
                      "whenever you want them back")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        got = seed_brush(context.scene, force=True)
        if got == "seeded":
            self.report({"INFO"}, f"brush: falloff {BRUSH_SEED_FALLOFF}, "
                                  f"size {BRUSH_SEED_SIZE}")
            return {"FINISHED"}
        self.report({"WARNING"},
                    f"brush not set up ({got}) -- enter Texture Paint first, "
                    f"the brush is an asset and does not exist until then")
        return {"CANCELLED"}


class _PaintPanel:
    """Paint's body, drawn from two editors (ADR-0185 Amendment 4).

    Paint is the one panel the placement rule lets out of the 3D viewport
    sidebar, because the Image Editor is the editor *rendering its actual
    subject* -- the sheet's pixels.  But registered ONLY for `IMAGE_EDITOR` it
    does not draw at all in a layout without one, which is every factory
    workspace except `UV Editing` / `Texture Paint` / `Shading` /
    `Compositing`.  So it is registered twice, and **both copies draw**: the
    viewport copy used to poll itself away wherever an Image Editor was
    visible, which was right while it was a fallback and wrong once the model
    became the paint surface.  See `MAP_PT_paint_view`.

    `NO_EDITOR_HINT` lives on the viewport copy alone.  Told "no Image Editor
    open" *inside an Image Editor* the artist would be reading a lie.
    """
    bl_region_type = "UI"
    bl_category = "Map"
    bl_label = "Paint"

    #: Does this copy tell the artist how to open an Image Editor?  False in
    #: the one that is already drawing inside one.
    says_where_the_sheet_went = False

    def draw(self, context):
        # The SCENE's map, not the selection's.  "There is only really one map
        # in the scene -- and having to have that map selected to see the
        # properties is annoying."  Paint was one of the two panels measured
        # NOT to obey the rule the rest of the package already states
        # (`marker_in_scene`, `lighting_bake.target_map`): aiming a lamp means
        # selecting the lamp, which made this panel go blank at exactly the
        # moment the artist reached for it.
        from .import_document import marker_in_scene
        ob = marker_in_scene(context)
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
        painting = painting_of(ob, sheet)
        row = layout.row(align=True)
        row.operator(MAP_OT_paint_sheet.bl_idname, icon="BRUSH_DATA")
        if painting is None:
            row.operator(MAP_OT_apply_paint.bl_idname, icon="CHECKMARK")
        # The other authoring path's door (ADR-0186 decision 7).  Beside Paint
        # sheet because that is where the artist already is when they decide
        # which way to work: leave it alone and the sheet stays exactly as the
        # disc laid it out, press it once and every chart owns its own texels.
        from .convert_op import MAP_OT_convert_manifold
        layout.operator(MAP_OT_convert_manifold.bl_idname, icon="MOD_UVPROJECT")
        # The button fills the Image Editors that exist; it opens none (see
        # `show_in_image_editors`).  So when there are none, say where the
        # sheet went and how to look at it -- HERE and not only in the
        # transient operator report, because this panel is where the artist is
        # already looking when the button appears to do nothing.
        spaces = image_editor_spaces()
        free = sum(1 for _, protected in spaces if not protected)
        if self.says_where_the_sheet_went and not free:
            box = layout.box()
            box.label(text=("every Image Editor holds a render"
                            if spaces else "no Image Editor open"),
                      icon="INFO")
            for line in textwrap.wrap(NO_EDITOR_HINT, 46):
                box.label(text=line)
        # Decision 17: on a converted map the gate is FALSE, so none of it is
        # drawn.  The sixteen legal colours, the palette shelf, the REFUSED
        # labels and the off-palette list are all statements about a path
        # where the Painting is scratch and a colour the sixteen cannot hold
        # is lost the moment it is committed.  Here the Painting survives the
        # compile, nothing is ever refused, and an artist reading `anything
        # else is REFUSED` on a map where nothing is refused hunts a problem
        # that does not exist.
        if painting is not None:
            layout.label(text="true colour — the compile decides the sixteen",
                         icon="COLOR")
            # Decision 15's two buttons, and they only exist here. On the
            # direct-paint path the Sheet is the AUTHORED half and
            # `resolve()` is what writes it, so there is nothing to compile
            # from. Neither runs behind the artist's back (decision 16): a
            # push ships the Sheet as it stands, so pressing one of these is
            # the only thing that ever moves it.
            from .compile_op import (MAP_OT_recalculate_palettes,
                                     MAP_OT_reselect_clusters, freshness)
            # ADR-0186 Amendment 5. Decision 13 makes a stale Sheet a legal
            # map and forbids GATING on freshness; it does not forbid saying
            # so. Without this the artist paints, pushes, sees nothing move,
            # and has no way to tell "my stroke was lost" from "the cache has
            # not been rebuilt" -- the two look identical and only one is a
            # problem. Reported from use, and it cost a session.
            state_of, said = freshness(ob, sheet, painting)
            if said:
                layout.label(text=said, icon={
                    "stale": "FILE_REFRESH", "fresh": "CHECKMARK",
                    "never": "INFO"}.get(state_of, "QUESTION"))
            col = layout.column(align=True)
            col.operator(MAP_OT_recalculate_palettes.bl_idname,
                         icon="COLORSET_10_VEC")
            col.operator(MAP_OT_reselect_clusters.bl_idname,
                         icon="GROUP_VCOL")
            _brush_box(layout, getattr(getattr(context, "tool_settings", None),
                                       "image_paint", None))
            return

        # The sixteen legal colours, and the colour being painted WITH.
        #
        # Reported from use: *"map has the palette I need to eye drop from,
        # tool has the color picker -- and I can't see them at the same
        # time."*  A sidebar region shows exactly ONE tab, always, so that
        # cannot be arranged around -- it is the wall ADR-0185 Amendment 4 hit
        # with Blender's `Item` tab, and it takes the same answer: bring what
        # is needed into OUR tab rather than chase theirs.  Neither is there a
        # second sidebar to move into: measured, `TOOLS` is a real 56 px region
        # in an Image Editor and a panel registered to it does not draw at all.
        #
        # `template_palette` is what makes this better than co-location: its
        # swatches are natively CLICK-TO-ARM, so a legal colour is one click
        # and the eyedrop round trip disappears rather than being made easier.
        #
        # **This file used to say the template "draws NOTHING without an active
        # paint brush".**  That is false, and the reason matters.  Measured
        # headful (`workspace/workspace_probe.py` phase `swatches`): it renders
        # in Texture Paint AND in Edit Mode, with its add/remove buttons greyed
        # in the latter.  What the old note was really measuring is
        # `TypeError: UILayout.template_palette(): takes at most 2 arguments,
        # got 3` -- the `color=True` kwarg, which does not exist on 5.2 -- and
        # a `draw` that raises renders everything emitted BEFORE it and nothing
        # after.  That is indistinguishable from a template that drew nothing,
        # which is how the wrong cause got written down.  TWO arguments.
        #
        # A panel `draw` must not mutate data, so this only DRAWS the shelf;
        # `resolve` and the Paint sheet operator are what build and bind it.
        _ip = getattr(getattr(context, "tool_settings", None),
                      "image_paint", None)
        _shelf = bpy.data.palettes.get(PALETTE_SHELF)
        _has_shelf = _shelf is not None and len(_shelf.colors)
        # On OUR shelf, not merely on a palette existing: the artist can point
        # `ip.palette` anywhere from the `Tool` tab, and a palette we did not
        # build is not the legal set.  Blessing theirs would put sixteen
        # arbitrary colours under a label that says the export refuses
        # everything else.
        # `==`, not `is`: an ID datablock's Python wrapper happens to be cached
        # so `is` holds here today, but a nested struct's is not, and the two
        # cases are not distinguishable at a glance. `==` compares the RNA
        # pointer, which is the question being asked.
        _live = bool(_has_shelf and _ip is not None and _ip.palette == _shelf)
        if _has_shelf:
            # TWO labels: at 280 px the one-liner rendered as
            # `row 5's 16 legal col...ng else is REFUSED:` -- and the half that
            # got eaten is the half that matters. Measured in the probe frame.
            layout.label(text=f"row {pal}'s {len(_shelf.colors)} legal colours")
            layout.label(text="anything else is REFUSED", icon="ERROR")
        if _live:
            # The colour being painted with, beside the colours allowed. It
            # lives in the `Tool` tab, and one tab is all a sidebar shows.
            #
            # `paint_owner`, NOT `_ip.brush`. This used to draw `brush.color`
            # unconditionally, and `use_unified_color` defaults to True -- so
            # the swatch showed a value that neither paints nor receives the
            # swatch click, and it photographed BLACK while the artist's clicks
            # were landing in `unified.color`. Measured both ways round in the
            # probe; see `paint_owner`.
            _colour = paint_owner(_ip, "use_unified_color")
            if _colour is not None:
                layout.prop(_colour, "color", text="painting with")
            layout.template_palette(_ip, "palette")
        elif _has_shelf:
            # No live binding -- the artist has pointed the palette elsewhere,
            # or nothing has bound it yet. Show the sixteen as REFERENCE only:
            # clicking one of these opens a picker on the shelf slot, which
            # edits the shelf rather than the CLUT and is overwritten by the
            # next sync, so it is never offered as the way to choose a colour.
            _grid = layout.grid_flow(row_major=True, columns=8, align=True)
            for _slot in _shelf.colors:
                _grid.prop(_slot, "color", text="")
            layout.label(text="press Paint sheet to put these on the brush")
        else:
            layout.label(text="press Paint sheet for this row's colours",
                         icon="INFO")

        _brush_box(layout, _ip)

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


class MAP_PT_paint(_PaintPanel, Panel):
    """`Map` sidebar, Image Editor: the active palette, the two buttons, and
    the sticky list."""
    bl_space_type = "IMAGE_EDITOR"
    bl_order = 0


class MAP_PT_paint_view(_PaintPanel, Panel):
    """The same panel, in the 3D viewport -- where the model is painted.

    **There is no `poll`, and its absence is the decision.**  This copy used
    to stand down wherever an Image Editor was visible, because it existed as
    a FALLBACK: its whole reason to be was `says_where_the_sheet_went` --
    *"you have no Image Editor, here is where the sheet went."*  ADR-0186
    Amendment 6 inverts that premise.  After a conversion the sheet is not an
    unwrap at all (islands are placed by area fit, so adjacency in the atlas
    means nothing) and the MODEL is the paint surface.  The Map workspace has
    an Image Editor by construction, so under the old poll the artist painted
    on the model in one pane while brush size, falloff, strength and the
    colour picker lived in the other.

    Deleting it is the whole fix, because the poll conflated two questions --
    *"should this panel draw here"* and *"should it explain where the sheet
    went"* -- and the second **already has its own guard**, in
    `_PaintPanel.draw`: `if self.says_where_the_sheet_went and not free`.  The
    hint stays silent in the Map workspace on its own terms, because that
    pane's Image Editor holds the sheet -- an ordinary `IMAGE`, not a render
    -- so `image_editor_spaces()` counts it free.

    Consequence, and it is intended: in a layout holding both editors the
    panel draws in BOTH sidebars, in full.  Each pane's sidebar is
    independent, which is ordinary Blender, and the duplication lands entirely
    on the direct-paint branch -- the path where this copy has no job and the
    artist collapses it once.  A trimmed viewport variant was considered and
    rejected: `_PaintPanel` already carries one variation axis, and a second
    would make a two-case panel a four-case one.
    """
    bl_space_type = "VIEW_3D"
    # Renumbered again when `What a push carries` was deleted; the
    # remaining five keep their relative order.
    bl_order = 2
    says_where_the_sheet_went = True


CLASSES = (MAP_OT_apply_paint, MAP_OT_paint_sheet, MAP_OT_seed_brush,
           MAP_PT_paint, MAP_PT_paint_view)


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
