"""Does the Convert button do, inside Blender, what `convert()` proves offline?

`tests/test_convert.py` grades the `bpy`-free core against the disc: 135
resources, 58,123 polygons, zero texels moved.  None of that touches the
operator, and `bpy`-gated code ships green under `pytest` -- the addon's
`CLAUDE.md` says so and it has happened.  What this grades is the wiring:

  * the mesh -> polygons read and the UV write-back are inverses, so a face
    reads the same texels through its rewritten UVs as it did before;
  * every POLYGON owns its texels afterwards (no texel with two polygon
    readers -- ADR-0186 Amendment 6 decision 22, the strictly stronger form
    of the chart oracle this used to check);
  * `texture_page` follows the island the face landed in;
  * the operator is registered and reachable, and the Paint panel draws it.

Run:  python3 tests/blender_convert.py [blender]
"""
import json
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
ADDON_DIR = PKG / "addons" / "exmateria_map"
FIXTURES = PKG / "tests" / "fixtures"
FIXTURE = FIXTURES / "MAP001.a0.stub.json"
TMP = Path(__file__).resolve().parent / ".blender_convert"
REPORT = TMP / "report.json"

def stage_stub():
    staged = TMP / FIXTURE.name
    staged.write_text(FIXTURE.read_text())
    for st in json.loads(FIXTURE.read_text())["map_states"]:
        name = st.get("texture_sheet")
        if name:
            (TMP / name).write_bytes((FIXTURES / name).read_bytes())
    return staged


def stage_real_map(number=22, arrangement=0):
    """A real arrangement, dumped straight out of the disc tree.

    MAP022 a0: 385 textured polygons over 5 sheets, and real texel sharing
    between charts -- which is the thing the operator is claimed to remove."""
    sys.path.insert(0, str(PKG))
    try:
        from exmateria_map import corpus
        from exmateria_map.dump import write_bundle
    except Exception:
        return None
    map_dir = corpus.map_dir()
    if map_dir is None:
        return None
    try:
        return write_bundle(map_dir, number, arrangement, TMP)
    except Exception:
        return None


SCRIPT = r'''
import array, json, sys, bpy
sys.path.insert(0, r"@ADDONPKG@")
import exmateria_map
exmateria_map.register()

checks = []
def ck(name, ok, detail=""):
    checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})

ck("operator is registered",
   hasattr(bpy.types, "EXMATERIA_MAP_OT_convert_manifold"))

import inspect
from exmateria_map import paint as P
src = inspect.getsource(P._PaintPanel.draw)
ck("the Paint panel draws the button", "convert_manifold" in src, src[:0])

bpy.ops.import_map.document(filepath=r"@JSON@")
from exmateria_map.import_document import marker_in_scene
ob = marker_in_scene(bpy.context)
ck("the fixture imported", ob is not None)

from exmateria_map.convert_op import _face_ordered
from exmateria_map.export_document import image_indices
from exmateria_map.paint import active_palette, index_image, sheet_of_state

state, _ = active_palette(ob)
img = index_image(ob, sheet_of_state(ob, state))
me = ob.data

def colour_blocks(polys, indices, rows):
    """What each face SHOWS: its indices through its own CLUT row."""
    out = []
    for q in polys:
        if "uv" not in q: continue
        us = [c[0] for c in q["uv"]]; vs = [c[1] for c in q["uv"]]
        base = q["texture_page"] * 256
        row = rows[q["palette_id"]]
        out.append([tuple(row[indices[(base+y)*256 + x] & 0xF]
                          for x in range(min(us), max(us)+1))
                    for y in range(min(vs), max(vs)+1)])
    return out


def art_blocks(polys, img):
    """The same rectangles read out of the true-colour source art image."""
    w, h = img.size
    px = list(img.pixels)
    out = []
    for q in polys:
        if "uv" not in q: continue
        us = [c[0] for c in q["uv"]]; vs = [c[1] for c in q["uv"]]
        base = q["texture_page"] * 256
        rowsout = []
        for y in range(min(vs), max(vs)+1):
            at = (h - 1 - (base + y)) * w        # Blender row 0 is the BOTTOM
            rowsout.append(tuple(
                tuple(int(round(px[(at + x) * 4 + k] * 255.0)) for k in range(3))
                for x in range(min(us), max(us)+1)))
        out.append(rowsout)
    return out

def index_blocks(polys, indices):
    """The same rectangles as raw 0..15 INDICES -- the Sheet, not the
    Painting.  Stronger than `colour_blocks`: nothing is resolved through a
    CLUT row, so two indices naming one colour cannot hide a texel that
    moved."""
    out = []
    for q in polys:
        if "uv" not in q: continue
        us = [c[0] for c in q["uv"]]; vs = [c[1] for c in q["uv"]]
        base = q["texture_page"] * 256
        out.append([tuple(indices[(base+y)*256 + x] & 0xF
                          for x in range(min(us), max(us)+1))
                    for y in range(min(vs), max(vs)+1)])
    return out

def polygon_sharing(polys):
    """Texels with more than one POLYGON reader (ADR-0186 Amdt 6 dec. 22).

    Strictly stronger than the chart form this replaced: a chart that FOLDS
    reads one rectangle from several of its own faces, and a chart-keyed
    count cannot see that.  Polygon identity needs no carrying either --
    conversion rewrites `texture_page` and `charts()` cuts at a page change,
    so re-deriving charts after a conversion reports a finer partition and a
    sharing residue that is not there.  An index is an index.
    """
    own = {}
    for n, q in enumerate(polys):
        if "uv" not in q: continue
        us = [c[0] for c in q["uv"]]; vs = [c[1] for c in q["uv"]]
        for x in range(min(us), max(us)+1):
            for y in range(min(vs), max(vs)+1):
                own.setdefault((q["texture_page"], x, y), set()).add(n)
    return sum(1 for s in own.values() if len(s) > 1)

from exmateria_map.convert_op import clut_rows_of, source_art_name
rows = clut_rows_of(ob, state)
ck("the state's CLUT resolved", rows is not None and len(rows) == 16)

before_polys = _face_ordered(me)
before_idx = image_indices(img)
before = colour_blocks(before_polys, before_idx, rows)
shared_before = polygon_sharing(before_polys)
pages_before = [q.get("texture_page") for q in before_polys]

# ADR-0186 Amendment 3 decision 17: `Paint sheet` is polymorphic and the
# Paint panel sheds its gate on a converted map.  A `draw` cannot be graded by
# reading its source -- what matters is what it EMITS -- so it is driven
# against a recording layout.  The stub is the panel: `draw` reads only
# `self.layout` and `self.says_where_the_sheet_went`.
class FakeLayout:
    """Records what a `draw` EMITS.  Every sub-layout is this same recorder,
    so a widget nested in a row or a box is still seen.

    The `__getattr__` fallback is deliberate: `UILayout` is wide and a panel
    grows widgets, and a recorder that raises on an unknown one turns an
    unrelated edit into a red arm about the gate.  A `draw` that raises
    renders everything before it and nothing after -- so an incomplete
    recorder reads exactly like the panel having stopped drawing."""
    def __init__(self, log): self.log = log
    def label(self, text="", **k): self.log.append(("label", text))
    def prop(self, data, prop, text="", **k):
        self.log.append(("prop", str(prop), text))
    def operator(self, idname, text="", **k):
        self.log.append(("operator", idname))
    def template_palette(self, data, prop, **k):
        self.log.append(("template_palette", str(prop)))
    def __getattr__(self, name):
        def sub(*a, **k):
            self.log.append((name,) + tuple(str(x) for x in a))
            return self
        return sub

class StubPanel:
    says_where_the_sheet_went = False
    def __init__(self, log): self.layout = FakeLayout(log)

def drawn(says_where=False):
    log = []
    stub = StubPanel(log)
    stub.says_where_the_sheet_went = says_where
    P._PaintPanel.draw(stub, bpy.context)
    return log

def emits(log, needle):
    return any(needle in str(part) for entry in log for part in entry)

# The direct-paint path first, as the CONTROL: every arm below is a claim that
# something STOPS being drawn, and an arm that was never drawn proves nothing.
bpy.ops.exmateria_map.paint_sheet()
# The CONTROL for the paint-mode arm after the conversion. `Paint sheet` puts
# the brush in `IMAGE` mode on an explicit canvas, and that setting is
# scene-wide and saved in the `.blend`. Captured HERE so that "MATERIAL after
# convert" is a claim about a CHANGE and not about a default that was never
# anything else.
mode_before_convert = bpy.context.tool_settings.image_paint.mode
canvas_before_convert = getattr(
    bpy.context.tool_settings.image_paint.canvas, "name", None)
ck("control: Paint sheet leaves the brush in IMAGE mode before conversion",
   mode_before_convert == "IMAGE", mode_before_convert)
before_draw = drawn()
ck("control: the gate IS drawn before conversion (Apply paint)",
   emits(before_draw, "apply_paint"))
ck("control: the REFUSED label is drawn before conversion",
   emits(before_draw, "REFUSED"))
ck("control: the sixteen legal colours are drawn before conversion",
   emits(before_draw, "legal colours"))
from exmateria_map import compile_op as _CO
ck("control: neither compile button polls before conversion",
   not _CO.MAP_OT_recalculate_palettes.poll(bpy.context)
   and not _CO.MAP_OT_reselect_clusters.poll(bpy.context))
ck("control: Paint sheet opens the recoloured INDEX picture before conversion",
   bpy.context.tool_settings.image_paint.canvas is not None
   and bpy.context.tool_settings.image_paint.canvas.name
   == P.paint_image_name(sheet_of_state(ob, state)),
   getattr(bpy.context.tool_settings.image_paint.canvas, "name", None))

# ADR-0186 decision 5's control: an unconverted map carries no `source_art`
# at all.  "The section is absent" and "the section is empty" are the same
# thing to a reader and very different things to `export(import(doc)) == doc`,
# which 148 corpus arrangements assert.
from exmateria_map.export_document import assemble, image_rgb
_doc_before, _files_before, _rep_before = assemble(ob)
ck("an unconverted map emits no source_art section",
   _doc_before is not None and "source_art" not in _doc_before)
ck("...and no source-art sidecar",
   not any(".source-" in n for n in (_files_before or {})))

# Convert FROM EDIT MODE, because that is where the artist already is: the
# button sits in the Paint panel, and selecting a face -- the gesture that
# chooses the palette row -- is only possible in Edit Mode.
#
# In Edit Mode the geometry lives in the BMesh and the Mesh datablock's
# attribute arrays read as size 0 while `me.polygons` still reports a count,
# so `_face_ordered`'s first attribute lookup was a bare `IndexError` naming a
# collection of size 0. It is the FOURTH time this addon has shipped that
# defect (the push, the panel, the resolve trigger), and it survives review
# every time because every other check in the package drives Object Mode --
# which is exactly why this one does not.
bpy.context.view_layer.objects.active = ob
bpy.ops.object.mode_set(mode="EDIT")
ck("the harness is really in Edit Mode (or this arm proves nothing)",
   ob.mode == "EDIT", ob.mode)
ck("the mesh attributes really do read EMPTY there (the defect's mechanism)",
   len(me.attributes["fft_ring_flipped"].data) == 0
   and len(me.polygons) > 0,
   f"{len(me.attributes['fft_ring_flipped'].data)} attrs vs "
   f"{len(me.polygons)} polygons")

r = bpy.ops.exmateria_map.convert_manifold()
ck("the operator finished", r == {"FINISHED"}, r)
ck("and it put the artist back in Edit Mode", ob.mode == "EDIT", ob.mode)
bpy.ops.object.mode_set(mode="OBJECT")

# ---------------------------------------------------------------------------
# ADR-0185 Amendment 5 -- painting on the MODEL, and where a stroke lands.
#
# Texture Paint in `MATERIAL` mode writes into the material's ACTIVE image
# texture node; `nodes.active` and `Material.paint_active_slot` are two views
# of one pointer. Our preview graph carries THREE image texture nodes, so
# Blender's default slot 0 is the CLUT -- unlinked by the conversion's rewire
# and therefore invisible, and still read back by `export_document` §6.4 to
# re-emit `map_states[].palettes`. A stroke there is silent and it ships.
#
# These arms MUST run before the `Paint sheet` call further down: that button
# re-arms `IMAGE` mode, which would make the paint-mode arm below vacuous.
_pmat = None
for _slot in ob.material_slots:
    if _slot.material is not None and _slot.material.name.endswith("_preview"):
        _pmat = _slot.material
_pnt = getattr(_pmat, "node_tree", None)
_texnodes = [n.name for n in _pnt.nodes if n.type == "TEX_IMAGE"] if _pnt else []
# The control: with one image texture node there is nothing to choose and the
# arms below would pass on any code at all.
ck("control: the preview material carries MORE than one image texture node",
   len(_texnodes) > 1, _texnodes)
ck("control: and the source art is NOT the first of them (slot 0 is not it)",
   bool(_texnodes) and _texnodes[0] != "exmateria_map.source_art",
   _texnodes)
ck("the conversion makes the source art the material's ACTIVE image node",
   getattr(getattr(_pnt, "nodes", None), "active", None) is not None
   and _pnt.nodes.active.name == "exmateria_map.source_art",
   getattr(getattr(_pnt, "nodes", None), "active", None)
   and _pnt.nodes.active.name)
ck("...so paint_active_slot resolves to the source art too",
   _pmat is not None
   and 0 <= _pmat.paint_active_slot < len(_texnodes)
   and _texnodes[_pmat.paint_active_slot] == "exmateria_map.source_art",
   f"slot {getattr(_pmat, 'paint_active_slot', None)} of {_texnodes}")
# `Paint sheet` before a conversion aims `IMAGE` mode at the index-derived
# paint copy, and the conversion DELETES that copy. Left alone, the artist
# strokes into a hole and nothing says why.
ck("the conversion puts the brush back into MATERIAL mode",
   bpy.context.tool_settings.image_paint.mode == "MATERIAL",
   bpy.context.tool_settings.image_paint.mode)
ck("...and the canvas it was aimed at really is gone (the reason it must)",
   canvas_before_convert is not None
   and bpy.data.images.get(canvas_before_convert) is None,
   canvas_before_convert)

# The viewport copy of the Paint panel no longer polls itself away, because
# the model IS the paint surface (ADR-0185 Amendment 5). The hint it carries
# is guarded separately, inside `draw`, and that guard is what makes deleting
# the poll safe -- so grade the guard, not only the absence.
ck("MAP_PT_paint_view defines no poll of its own",
   "poll" not in vars(P.MAP_PT_paint_view),
   sorted(k for k in vars(P.MAP_PT_paint_view) if not k.startswith("__")))
ck("control: there IS a free Image Editor space for the guard to see",
   any(not protected for _, protected in P.image_editor_spaces()),
   len(P.image_editor_spaces()))
ck("...so the viewport copy still withholds the no-editor hint",
   not emits(drawn(says_where=True), "no Image Editor open"))

after_polys = _face_ordered(me)
shared_after = polygon_sharing(after_polys)

art_img = bpy.data.images.get(source_art_name(sheet_of_state(ob, state)))
ck("the source art image exists", art_img is not None)
ck("the source art is packed (or it reloads BLANK)",
   art_img is not None and art_img.packed_file is not None)
after = art_blocks(after_polys, art_img) if art_img else []
ck("every face SHOWS the same colours in the source art",
   after == before,
   f"{sum(1 for a,b in zip(after,before) if a!=b)} of {len(before)} faces differ")

# By its NODES, not by slot index: slot 0 is the untextured grey and the
# preview material is slot 1, so a slot-0 check reads as "not rewired" on a
# correctly rewired scene.
node = None
for sl in ob.material_slots:
    nt = getattr(sl.material, "node_tree", None)
    if nt and nt.nodes.get("exmateria_map.multiply"):
        node = nt.nodes.get("exmateria_map.source_art")
ck("the preview samples the painting, not index -> CLUT",
   node is not None and node.image == art_img
   and any(l.to_node.name == "exmateria_map.multiply"
           for l in node.outputs["Color"].links))
# ADR-0186 Amendment 3 decision 14.  This arm used to assert the OPPOSITE --
# "the index sheet is left alone (the compile derives it later)" -- and that
# was the live defect written down as a requirement.  A conversion rewrites
# every UV, so a Sheet left on the disc's layout is not a STALE map, it is a
# mesh and a sheet that disagree; `build` ships it and the push reports
# success.  The Sheet is carried through the same blit as the Painting, so
# the question is not whether it changed but whether every face still reads
# what it read.
after_idx = image_indices(img)
ck("every face reads the same INDICES through its rewritten UVs",
   index_blocks(after_polys, after_idx) == index_blocks(before_polys,
                                                        before_idx),
   f"{sum(1 for a, b in zip(index_blocks(after_polys, after_idx), index_blocks(before_polys, before_idx)) if a != b)} faces differ")
ck("the index sheet really moved (the control for the arm above)",
   bytes(after_idx) != bytes(before_idx))
ck("the index sheet is still a legal 4bpp plane",
   all(0 <= v <= 15 for v in after_idx) and len(after_idx) == 256 * 1024,
   len(after_idx))
ck("the UVs actually moved",
   [q.get("uv") for q in after_polys] != [q.get("uv") for q in before_polys])
ck("no texel is read by two POLYGONS afterwards",
   shared_after == 0, f"before {shared_before}, after {shared_after}")
ck("there was sharing to remove (the control)", shared_before > 0, shared_before)
ck("texture_page is written, and stays legal",
   all(p is None or 0 <= p <= 3 for p in
       [q.get("texture_page") for q in after_polys]))

# Decision 17, after the conversion.  The Painting survives here, so nothing
# is ever refused: an artist reading `anything else is REFUSED` on a map where
# nothing is refused hunts a problem that does not exist.
r = bpy.ops.exmateria_map.paint_sheet()
ck("Paint sheet still finishes on a converted map", r == {"FINISHED"}, r)
canvas = bpy.context.tool_settings.image_paint.canvas
ck("Paint sheet arms the brush on the PAINTING, not on the index picture",
   canvas is not None and canvas.name == source_art_name(sheet_of_state(ob, state)),
   getattr(canvas, "name", None))
ck("and it does not rebuild the index-derived paint image",
   bpy.data.images.get(P.paint_image_name(sheet_of_state(ob, state))) is None)

after_draw = drawn()
ck("the panel does not draw Apply paint on a converted map",
   not emits(after_draw, "apply_paint"))
ck("the panel does not draw the REFUSED label on a converted map",
   not emits(after_draw, "REFUSED"))
ck("the panel does not draw the sixteen legal colours on a converted map",
   not emits(after_draw, "legal colours"))
ck("the panel does not draw the palette shelf on a converted map",
   not emits(after_draw, "template_palette")
   and not emits(after_draw, "press Paint sheet"))
ck("the panel does not draw the off-palette list on a converted map",
   not emits(after_draw, "off-palette"))
ck("the panel still draws Paint sheet on a converted map",
   emits(after_draw, "paint_sheet"))
ck("Apply paint is not reachable at all on a converted map",
   not P.MAP_OT_apply_paint.poll(bpy.context))

# ---------------------------------------------------------------------------
# ADR-0186 Amendment 3 decision 15 -- one compiled truth, two buttons.
#
# The offline core is graded by `tests/test_compile.py`; what is graded here is
# the wiring, because a `bpy`-gated module ships green under `pytest`. The
# strong arm is the ADR's own claim run through the real operator: a freshly
# converted map's Painting IS the disc's sheet baked through the disc's CLUT,
# so recompiling it must land on the same colour at every texel.
# ---------------------------------------------------------------------------
from exmateria_map import compile_op as CO
ck("both compile operators are registered",
   hasattr(bpy.types, "EXMATERIA_MAP_OT_recalculate_palettes")
   and hasattr(bpy.types, "EXMATERIA_MAP_OT_reselect_clusters"))
ck("the panel draws neither on the DIRECT-PAINT path (the control)",
   not emits(before_draw, "recalculate_palettes")
   and not emits(before_draw, "reselect_clusters"))
ck("the panel draws both on a converted map",
   emits(after_draw, "recalculate_palettes")
   and emits(after_draw, "reselect_clusters"))

def compiled_colours():
    """What the GAME would show: each polygon's texels through the row it
    names, indexed by the sheet."""
    plane = image_indices(img)
    rows_now = clut_rows_of(ob, state)
    out = []
    for q in _face_ordered(me):
        if "uv" not in q: continue
        row = rows_now[q["palette_id"]]
        us = [c[0] for c in q["uv"]]; vs = [c[1] for c in q["uv"]]
        base = q["texture_page"] * 256
        for v in range(min(vs), max(vs)+1):
            for u in range(min(us), max(us)+1):
                out.append(tuple(row[plane[(base+v)*256 + u] & 0xF]))
    return out

def painted_colours(art_image):
    w, h = art_image.size
    px = list(art_image.pixels)
    out = []
    for q in _face_ordered(me):
        if "uv" not in q: continue
        us = [c[0] for c in q["uv"]]; vs = [c[1] for c in q["uv"]]
        base = q["texture_page"] * 256
        for v in range(min(vs), max(vs)+1):
            at = (h - 1 - (base + v)) * w
            for u in range(min(us), max(us)+1):
                out.append(tuple(int(round(px[(at+u)*4 + k] * 255.0))
                                 for k in range(3)))
    return out

art_now = bpy.data.images.get(source_art_name(sheet_of_state(ob, state)))
want = painted_colours(art_now)
r = bpy.ops.exmateria_map.recalculate_palettes()
ck("Recalculate palettes finished", r == {"FINISHED"}, r)
got = compiled_colours()
ck("the first compile changes no colour, through the real operator",
   len(want) > 1000 and got == want,
   f"{sum(1 for a, b in zip(got, want) if a != b)} of {len(want)} texels differ")

# Decision 8: the incumbent is always a candidate, so on a map that is already
# exactly representable it must WIN -- and the operator must say so rather
# than look like a button that did nothing.
pal_before = [q.get("palette_id") for q in _face_ordered(me)]
r = bpy.ops.exmateria_map.reselect_clusters()
ck("Re-select clusters finished", r == {"FINISHED"}, r)
ck("...and left the binding alone on a map it cannot improve",
   [q.get("palette_id") for q in _face_ordered(me)] == pal_before)
ck("...and the map still shows what was painted",
   compiled_colours() == want)
ck("both compile buttons poll on a converted map",
   CO.MAP_OT_recalculate_palettes.poll(bpy.context)
   and CO.MAP_OT_reselect_clusters.poll(bpy.context))

# ---------------------------------------------------------------------------
# ADR-0186 Amendment 5 -- staleness is SHOWN, never gated on.
#
# Decision 13 keeps a stale Sheet a complete, legal map, so nothing here may
# refuse. What it may do is stop being silent: an artist who paints, pushes and
# sees nothing move cannot otherwise tell "my stroke was lost" from "the cache
# has not been rebuilt", and only one of those is a problem. Reported from use.
# ---------------------------------------------------------------------------
painting_now = bpy.data.images.get(source_art_name(sheet_of_state(ob, state)))
sheet_key = sheet_of_state(ob, state)

ck("straight after a compile the panel says the sheet is fresh",
   CO.freshness(ob, sheet_key, painting_now)[0] == "fresh",
   CO.freshness(ob, sheet_key, painting_now))
ck("...and the compile cleared the painting's dirty bit",
   not painting_now.is_dirty)
ck("...and an assemble raises no staleness warning",
   not any("stale" in w or "never been compiled" in w
           for w in assemble(ob)[2].warnings),
   assemble(ob)[2].warnings)

# Paint. `foreach_set` is the same datablock mutation a brush makes, and it is
# the only one available with no paint context (`ImagePaint.brush` is
# read-only and `bpy.data.brushes` is empty under --factory-startup).
_px = array.array("f", [0.0]) * (256 * 1024 * 4)
painting_now.pixels.foreach_get(_px)
for _i in range(0, 4000, 4):
    _px[_i], _px[_i + 1], _px[_i + 2] = 0.0, 1.0, 0.0      # green lines
painting_now.pixels.foreach_set(_px)
painting_now.update()

ck("painting on it makes the panel say STALE",
   CO.freshness(ob, sheet_key, painting_now)[0] == "stale",
   CO.freshness(ob, sheet_key, painting_now))
_warn = assemble(ob)[2].warnings
ck("...and a push/export report NAMES it rather than refusing",
   any("compiled from an EARLIER painting" in w for w in _warn), _warn)
ck("...and it is a WARNING, never a refusal (decision 13)",
   not any("painting" in r for r in assemble(ob)[2].refusals))

r = bpy.ops.exmateria_map.recalculate_palettes()
ck("recompiling clears it", r == {"FINISHED"}
   and CO.freshness(ob, sheet_key, painting_now)[0] == "fresh",
   f"{r} {CO.freshness(ob, sheet_key, painting_now)}")
ck("...and the warning is gone",
   not any("EARLIER painting" in w for w in assemble(ob)[2].warnings))

# THE HOLE, and the reason `is_dirty` alone is not the answer. A `.blend` save
# packs the image, so a map that was painted, saved and reopened WITHOUT being
# compiled comes back with `is_dirty` False and a stamp that no longer
# describes it. Reproduced by putting the session into exactly that state:
# paint, clear the dirty bit the way a save would, and forget what this
# process verified. Saying `fresh` here is the one wrong answer, because it is
# wrong in the direction that ships.
# DIFFERENT pixels, or this arm tests nothing: re-writing what the painting
# already holds leaves it genuinely fresh, and the check would be right to say
# so. (It did, on the first draft of this arm.)
for _i in range(8000, 12000, 4):
    _px[_i], _px[_i + 1], _px[_i + 2] = 1.0, 0.0, 0.0      # red, elsewhere
painting_now.pixels.foreach_set(_px)
painting_now.update()
painting_now.pack()                       # what a save does to a packed image
CO._VERIFIED.clear()                      # what a fresh process starts with

_said = CO.freshness(ob, sheet_key, painting_now)    # BEFORE assemble, which
_warn2 = assemble(ob)[2].warnings                    # verifies as a side effect
ck("a painted-then-saved-then-reopened map is NOT reported fresh",
   _said[0] != "fresh", _said)
ck("...it is reported UNKNOWN -- the cheap bit cannot know",
   _said[0] == "unknown", _said)
ck("...and the exact check on the way out still catches it",
   any("EARLIER painting" in w for w in _warn2), _warn2)
bpy.ops.exmateria_map.recalculate_palettes()

# ---------------------------------------------------------------------------
# ADR-0186 decisions 4, 5 and 6 -- the Painting survives OUTSIDE the `.blend`.
#
# Decision 4 forbids the irreplaceable half of an authored map living only in
# the `.blend`, and until this leg landed that was exactly where it lived.  The
# claim is a round trip: export the converted map, import what was written, and
# the painting that comes back is the one that went out, texel for texel.
# ---------------------------------------------------------------------------
import os
doc, files, rep = assemble(ob)
sheet_now = sheet_of_state(ob, state)
art_img = bpy.data.images.get(source_art_name(sheet_now))
sent = image_rgb(art_img) if art_img else b""

ck("the converted document carries a source_art section",
   doc is not None and bool(doc.get("source_art")),
   sorted((doc or {}).get("source_art") or {}))
entries = (doc or {}).get("source_art") or {}
ck("its one entry is named from its own content hash, not from the sheet",
   len(entries) == 1
   and list(entries)[0].startswith("MAP022.a0.source-")
   and list(entries)[0].endswith(".png")
   and "sheet-" not in list(entries)[0],
   list(entries))
ck("the painting is a sidecar file beside the document",
   all(n in files for n in entries), sorted(files))
ck("no map state names the painting in texture_sheet (decision 5)",
   all(st.get("texture_sheet") not in entries
       for st in (doc or {}).get("map_states") or []))

# Decision 6: one entry per map state, deduplicated by content.  MAP022 a0
# ships 20 states over 5 sheets and exactly one of them was converted, so the
# entry must name every state reading THAT sheet and no other.
# Off the marker's per-state sheet list, NOT off `map_states[state]`: the
# active palette state is usually a MESH row, which carries the palettes and
# names no sheet at all, so reading its `texture_sheet` hands back None and
# the expectation collapses to the empty list -- which then agrees with a
# section that named nothing.
_state_sheets = json.loads(ob["exmateria_map/state_sheets"])
expect_states = [i for i, s in enumerate(_state_sheets) if s == sheet_now]
ck("the entry names exactly the states that read the painted sheet",
   bool(expect_states) and entries
   and list(entries.values())[0]["states"] == expect_states,
   f"{list(entries.values())[0]['states'] if entries else None} vs {expect_states}")

# Write the bundle out and read it back with the SHIPPED importer.
out_dir = os.path.join(r"@TMP@", "roundtrip")
os.makedirs(out_dir, exist_ok=True)
doc_path = os.path.join(out_dir, "MAP022.a0.json")
open(doc_path, "w").write(json.dumps(doc))
for n, blob in files.items():
    open(os.path.join(out_dir, n), "wb").write(blob)

was = set(bpy.data.objects)
bpy.ops.import_map.document(filepath=doc_path)
fresh = [o for o in bpy.data.objects if o not in was
         and "exmateria_map/base" in o]
ck("the exported document re-imports", len(fresh) == 1, len(fresh))
if fresh:
    ob2 = fresh[0]
    state2, _ = active_palette(ob2)
    sheet2 = sheet_of_state(ob2, state2)
    back = bpy.data.images.get(source_art_name(sheet2))
    ck("the painting comes back as an image on the re-imported map",
       back is not None, source_art_name(sheet2) if sheet2 else None)
    ck("and it is the painting that went out, texel for texel",
       back is not None and image_rgb(back) == sent)
    ck("the painting image is packed (or it reloads BLANK)",
       back is not None and back.packed_file is not None)
    node2 = None
    for sl in ob2.material_slots:
        nt = getattr(sl.material, "node_tree", None)
        if nt and nt.nodes.get("exmateria_map.multiply"):
            node2 = nt.nodes.get("exmateria_map.source_art")
    ck("a converted map re-opens converted: the preview samples the painting",
       node2 is not None and node2.image == back
       and any(l.to_node.name == "exmateria_map.multiply"
               for l in node2.outputs["Color"].links))
    # The other half of "re-opens converted": the brush follows the document,
    # not the session that converted it.  `markers()` order decides which map
    # `Paint sheet` picks, so make the fresh one active first.
    bpy.context.view_layer.objects.active = ob2
    for o in bpy.data.objects:
        o.select_set(o is ob2)
    r2 = bpy.ops.exmateria_map.paint_sheet()
    ck("Paint sheet on the re-imported map opens the Painting",
       r2 == {"FINISHED"}
       and bpy.context.tool_settings.image_paint.canvas == back,
       f"{r2} {getattr(bpy.context.tool_settings.image_paint.canvas, 'name', None)}")

json.dump({"checks": checks}, open(r"@OUT@", "w"))
'''


def main():
    TMP.mkdir(exist_ok=True)
    if REPORT.exists():
        REPORT.unlink()                     # never grade on a stale report
    # The stub fixture has ONE textured face, so it cannot exercise the claim
    # this harness exists for -- there is no sharing on it to remove, and the
    # control arm says so rather than letting a vacuous PASS stand.  Stage a
    # real map when the corpus is there.
    staged = stage_real_map() or stage_stub()
    print(f"  document: {staged.name}\n")

    script = TMP / "run_convert.py"
    script.write_text(SCRIPT
                      .replace("@ADDONPKG@", str(ADDON_DIR.parent))
                      .replace("@JSON@", str(staged))
                      .replace("@TMP@", str(TMP))
                      .replace("@OUT@", str(REPORT)))
    # Isolate this Blender from the artist's OWN install. Without it the
    # `addon_install` in the script above overwrites the addon they are
    # clicking, and `addon_enable` then grades that copy rather than this
    # tree. `--factory-startup` does NOT do this -- see `blender_env`.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from blender_env import isolated_env
    proc = subprocess.run(
        [sys.argv[1] if len(sys.argv) > 1 else "blender",
         "--background", "--factory-startup", "--python", str(script)],
        capture_output=True, text=True,
                          env=isolated_env())
    if not REPORT.exists():
        sys.stdout.write(proc.stdout[-3000:])
        sys.stdout.write("\n[stderr]\n" + proc.stderr[-3000:])
        print("\nFAIL: no report written")
        sys.exit(1)

    checks = json.loads(REPORT.read_text())["checks"]
    bad = [c for c in checks if not c["ok"]]
    for c in checks:
        print(f"  [{'PASS' if c['ok'] else 'FAIL'}] {c['name']}"
              + (f"   {c['detail']}" if c["detail"] else ""))
    print(f"\nSUMMARY: {len(checks) - len(bad)}/{len(checks)} checks passed")
    print("PASS" if not bad else "FAIL")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
