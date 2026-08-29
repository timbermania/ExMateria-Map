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
import array, hashlib, json, sys, bpy
sys.path.insert(0, r"@ADDONPKG@")
import exmateria_map
exmateria_map.register()

# NOTHING in this harness may reach a real emulator. Since the compile buttons
# push, every `recalculate_palettes` below would otherwise open a socket to the
# default port -- and this box may have a PCSX-Redux on it, mid-battle.
from exmateria_map import live_link as _L
_L.DEFAULT_PORT = 9                                  # the discard port

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

# `scale="1"` NAMED, not defaulted.  Everything below this line is grading
# criterion 1 -- "nothing that exists today moves" -- which is a claim about
# the 1x Painting specifically.  Riding the operator's default would silently
# re-aim the whole harness at whatever that default becomes, and criterion 1
# would stop being tested by anything.  The default has an arm of its own at
# the foot of this file.
r = bpy.ops.exmateria_map.convert_manifold(scale="1")
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
# ADR-0186 Amendment 10 decision 37 -- the shrink runs in FRONT of the compile,
# and the STAMP is what item 9 left open.
#
# The compile never learns N existed: it is handed a 256x1024 buffer at every
# scale.  So a 4x Painting whose every NxN block is one colour -- which is what
# a replicate makes -- must compile to the SAME Sheet its 1x original did,
# exactly.  That is criterion 2 carried through the compile rather than
# stopping at the bake.
#
# The second arm is the one that would have shipped SILENTLY.  `stamp_compile`
# records what the compile read; `compare_stamp` compares that against the
# digest `_assemble` takes over `image_rgb(painting)` -- the FULL-RESOLUTION
# master.  Hand the stamp the shrunk art and the two can never agree, so every
# 4x map warns "compiled from an EARLIER painting" on every export and every
# push, forever, having just been compiled.  `settle_op` calls `stamp_compile`
# with `image_rgb(painting)` too, so that is a second, independent reason the
# answer is forced rather than chosen.
# ---------------------------------------------------------------------------
from exmateria_map.convert_op import _write_art
from exmateria_map import resample as _R
_sheet_c = sheet_of_state(ob, state)
_master1 = image_rgb(art_now)              # the 1x painting the compile read
_plane1 = image_indices(img)               # the Sheet it produced

_p4 = _write_art(source_art_name(_sheet_c), _R.expand(_master1, 256, 1024, 4))
ck("control: the Painting really is 4x now", tuple(_p4.size) == (1024, 4096),
   tuple(_p4.size))
r = bpy.ops.exmateria_map.recalculate_palettes()
ck("Recalculate palettes finishes on a 4x Painting", r == {"FINISHED"}, r)
ck("...and lands the SAME Sheet the 1x painting landed (criterion 2, through "
   "the compile)", image_indices(img) == _plane1)
ck("...and the map still shows what was painted", compiled_colours() == want)

_rep4 = assemble(ob)[2]
ck("...and the map does NOT report itself stale: the stamp hashes the "
   "full-resolution master, not the shrunk art",
   not any("EARLIER painting" in _l for _l in _rep4.lines()),
   [_l for _l in _rep4.lines() if "painting" in _l][:3])

art_now = _write_art(source_art_name(_sheet_c), _master1)
r = bpy.ops.exmateria_map.recalculate_palettes()
ck("control: back at 1x the map is exactly where it was",
   r == {"FINISHED"} and image_indices(img) == _plane1
   and compiled_colours() == want)

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
# ADR-0186 Amendment 7 decision 25 -- every way OUT of Blender compiles first.
#
# Amendment 5 made the one failure audible: the warning fires on every push,
# reaches the toast, the Log and the terminal, and the panel draws a live
# freshness label above both buttons. The artist pushed a stale sheet anyway,
# repeatedly, and read the result as the addon being broken. A signal the
# artist has to act on is a button with extra steps, so the compile moves onto
# the way out and the buttons become the manual override.
#
# Driven through the real EXPORT operator rather than through
# `ensure_compiled` alone, because the claim decision 25 makes is about the
# EXITS: `assemble` still warns (it is the signal, and it is still real on the
# direct-paint path and in a reopened `.blend`), and what must be true is that
# no exit can reach it while stale.
# ---------------------------------------------------------------------------
for _i in range(16000, 20000, 4):
    _px[_i], _px[_i + 1], _px[_i + 2] = 0.0, 0.0, 1.0      # blue, elsewhere
painting_now.pixels.foreach_set(_px)
painting_now.update()

ck("a painted map is stale before an exit runs",
   CO.freshness(ob, sheet_key, painting_now)[0] == "stale",
   CO.freshness(ob, sheet_key, painting_now))

import os
_exit_dir = os.path.join(r"@TMP@", "exit-compiles")
os.makedirs(_exit_dir, exist_ok=True)
_r = bpy.ops.export_map.document(directory=_exit_dir)
ck("an export compiles on the way out, with no button pressed",
   _r == {"FINISHED"}
   and CO.freshness(ob, sheet_key, painting_now)[0] == "fresh",
   f"{_r} {CO.freshness(ob, sheet_key, painting_now)}")

# Decision 27's sequencing, and it is load-bearing: the compile must land
# BEFORE `assemble` reads the mesh, because `palette_id` is one of the three
# packet fields a push sends. If it landed after, the exported map and the
# pushed map would be different maps -- the exact failure decision 16 existed
# to prevent, relocated.
_stamped = CO.painting_stamp(ob).get(sheet_key)
ck("...and what left is what was compiled, not what preceded it",
   _stamped == hashlib.sha256(image_rgb(painting_now)).hexdigest(),
   f"stamp {_stamped}")

ck("...so the exit's own report carries no staleness warning",
   not any("EARLIER painting" in w or "never been compiled" in w
           for w in assemble(ob)[2].warnings),
   assemble(ob)[2].warnings)

# Amendment 8 left this open and decision 28 closes it: a settle compiles, and
# the push that follows it comes straight back through `ensure_compiled`.
# Without a guard the loop pays for the search twice on every settle, and the
# search is 3.4 s on a painted canvas.  `fresh` is provable -- this process
# hashed this painting and the stamp agrees -- so the second run is a no-op and
# says so by compiling nothing.
ck("a second exit over an unchanged painting compiles nothing",
   CO.ensure_compiled(ob) == [], CO.ensure_compiled(ob))

for _i in range(24000, 28000, 4):
    _px[_i], _px[_i + 1], _px[_i + 2] = 1.0, 1.0, 0.0      # yellow, elsewhere
painting_now.pixels.foreach_set(_px)
painting_now.update()
_notes = CO.ensure_compiled(ob)
ck("...and one that WAS painted in between still compiles",
   len(_notes) == 1 and "compiled on the way out" in _notes[0], _notes)
ck("...leaving it fresh again",
   CO.freshness(ob, sheet_key, painting_now)[0] == "fresh",
   CO.freshness(ob, sheet_key, painting_now))

# The buttons are the manual override now, not the path -- they must still be
# there, and still do what they did.
ck("both compile buttons survive as the manual override",
   CO.MAP_OT_recalculate_palettes.poll(bpy.context)
   and CO.MAP_OT_reselect_clusters.poll(bpy.context))


# ---------------------------------------------------------------------------
# ADR-0186 Amendment 7 decisions 28-30 -- the loop closes itself.
#
# A settle is a pause after painting stops, and the hard part is telling that
# apart from the pause between two strokes of one gesture. Driven by hand here
# because `bpy.app.timers` does not fire under `--background`: there is no
# event loop to pump them, which is what makes this gradeable at all.
# ---------------------------------------------------------------------------
import time as _time
from exmateria_map import settle_op as SO

# These harnesses import the addon rather than ENABLE it, so `_prefs` returns
# None and both settings fall back to their defaults -- the real 1.5 s, which
# is what the artist gets. The switch is driven by replacing `_enabled`.
SO._CLOCKS.clear()
SO._RESULT.clear()

# The push is SPIED, never performed. This box may have a PCSX-Redux running
# on the default port, and a test suite that pushed into someone's live
# battle would be a defect of its own.
_pushes = []
_real_push = SO._push
SO._push = lambda _ob: _pushes.append(_ob.name)

# The settle must not be the operator. `MAP_OT_reselect_clusters` carries
# {"REGISTER", "UNDO"}, so a settle that went through it would stack an undo
# step per compile and Ctrl+Z would stop taking back brush strokes -- decision
# 29, and the whole reason the compile is a plain function.
_op_calls = [0]
_real_execute = CO.MAP_OT_reselect_clusters.execute
def _spy(self, context):
    _op_calls[0] += 1
    return _real_execute(self, context)
CO.MAP_OT_reselect_clusters.execute = _spy


def _settle(seconds, painting_between=None):
    """Pump the timer by hand for `seconds`, optionally painting as we go."""
    _end = _time.monotonic() + seconds
    while _time.monotonic() < _end:
        if painting_between is not None:
            painting_between()
        SO._tick()
        _time.sleep(SO.TICK)


_gesture = [0]
def _keep_painting():
    """A stroke that actually MOVES the canvas.

    The first draft cycled seven regions through one colour, so after seven
    calls it repainted what was already there -- the canvas went static, the
    settle fired mid-"gesture", and the arm blamed the code for the fixture.
    """
    _gesture[0] += 1
    _v = (_gesture[0] % 97) / 97.0
    _o = 32000 + (_gesture[0] % 7) * 400
    for _i in range(_o, _o + 400, 4):
        _px[_i], _px[_i + 1], _px[_i + 2] = _v, 0.5, 0.0
    painting_now.pixels.foreach_set(_px)
    painting_now.update()


bpy.ops.exmateria_map.recalculate_palettes()      # a known-fresh start
ck("the settle starts from a fresh sheet",
   CO.freshness(ob, sheet_key, painting_now)[0] == "fresh",
   CO.freshness(ob, sheet_key, painting_now))
SO._CLOCKS.clear()
_settle(1.0)
ck("an untouched canvas settles into nothing",
   CO.freshness(ob, sheet_key, painting_now)[0] == "fresh"
   and not SO._RESULT and not _pushes,
   f"{CO.freshness(ob, sheet_key, painting_now)} {len(SO._RESULT)} {_pushes}")

# A gesture in progress -- the canvas keeps moving, so the pause never
# completes. This is the case decision 28's 1.5 s exists for, and it holds for
# as long as the artist keeps painting rather than for 1.5 s.
_settle(4.0, painting_between=_keep_painting)
ck("a gesture in progress never settles, however long it runs",
   CO.freshness(ob, sheet_key, painting_now)[0] == "stale" and not _pushes,
   f"{CO.freshness(ob, sheet_key, painting_now)} {_pushes}")

# ...and now the artist stops.
_settle(20.0)
ck("a pause after painting compiles, with no button pressed",
   CO.freshness(ob, sheet_key, painting_now)[0] == "fresh",
   CO.freshness(ob, sheet_key, painting_now))
ck("...and it was NOT the operator, so no undo step was stacked",
   _op_calls[0] == 0, f"{_op_calls[0]} operator call(s)")
ck("...and the push fired on the same settle (decision 28 is both)",
   _pushes == [ob.name], _pushes)
_again = CO.ensure_compiled(ob)          # ONCE: calling it twice in the detail
ck("...and a settled canvas does not settle again",   # reports the second answer
   _again == [], _again)

# Decision 28 rests on the poll being affordable, and the poll is not just the
# digest -- every tick also resolves the map's active state, sheet, painting,
# index image and CLUT rows. Measured here rather than assumed, because this
# runs four times a second for as long as Blender is open.
_t0 = _time.perf_counter()
for _ in range(20):
    SO._step()
_tick_ms = (_time.perf_counter() - _t0) / 20 * 1000
ck("a settle tick is affordable at 4 Hz",
   _tick_ms < 25.0,
   f"{_tick_ms:.1f} ms/tick = {_tick_ms * 4 / 10:.1f}% of one core")

# Edit Mode. `readable_mesh` reads a mesh by toggling the artist OUT of Edit
# Mode and back, which is right for a button they just pressed and hostile
# from a timer. A settle skipped this way is not lost: the canvas still
# differs from what was compiled, so the first tick after Edit Mode ends
# catches it.
_keep_painting()
bpy.context.view_layer.objects.active = ob
bpy.ops.object.mode_set(mode="EDIT")
SO._CLOCKS.clear()
_settle(3.0)
ck("a settle holds off while the artist is in Edit Mode",
   CO.freshness(ob, sheet_key, painting_now)[0] == "stale"
   and ob.mode == "EDIT",
   f"{CO.freshness(ob, sheet_key, painting_now)} {ob.mode}")
bpy.ops.object.mode_set(mode="OBJECT")
_settle(20.0)
ck("...and the first tick after Edit Mode ends catches it",
   CO.freshness(ob, sheet_key, painting_now)[0] == "fresh",
   CO.freshness(ob, sheet_key, painting_now))

# The switch, because an automation with no off is a trap.
_real_enabled = SO._enabled
SO._enabled = lambda _c: False
_keep_painting()
SO._CLOCKS.clear()
_settle(4.0)
ck("turning the settle off stops it dead",
   CO.freshness(ob, sheet_key, painting_now)[0] == "stale",
   CO.freshness(ob, sheet_key, painting_now))
SO._enabled = _real_enabled
_settle(20.0)
ck("...and turning it back on resumes it",
   CO.freshness(ob, sheet_key, painting_now)[0] == "fresh",
   CO.freshness(ob, sheet_key, painting_now))

# The REAL push path, which nothing graded before -- the arms above spy it, so
# every one of them proves only that `_push` was CALLED. That gap is where the
# artist's "the auto push doesn't seem to be working" lived, and all three
# defects in it were SILENT.
#
# Pointed at the discard port: this box may have a PCSX-Redux on the default
# one, and a suite that pushed into someone's live battle would be a defect of
# its own.
SO._push = _real_push                     # `_L.DEFAULT_PORT` is 9 from the top
SO.resume_pushing()

_r = SO.push_after_compile(ob, "test")
ck("a push with no emulator is REFUSED rather than silently swallowed",
   _r is not None and "FINISHED" not in _r, _r)
ck("...and it BACKS OFF rather than latching for the session",
   SO._PUSH["quiet_until"] > _time.monotonic(),
   SO._PUSH["quiet_until"] - _time.monotonic())
ck("...so a push inside the back-off does not stall on it again",
   SO.push_after_compile(ob, "test") is None)
SO.resume_pushing()
ck("...and pressing Push by hand clears it at once",
   SO._PUSH["quiet_until"] == 0.0 and SO._PUSH["said"] is None)

_real_auto = SO._auto_push
SO._auto_push = lambda _c: False
ck("the auto-push switch turns it off without touching the compile",
   SO.push_after_compile(ob, "test") is None)
SO._auto_push = _real_auto

# The artist's own proposal: "after the re-cluster and re-palette calc it
# should just try to auto push -- is that not what we're doing?" It was not.
_button_pushes = []
_real_pac = SO.push_after_compile
SO.push_after_compile = lambda _ob, _why: _button_pushes.append(_why)
bpy.ops.exmateria_map.recalculate_palettes()
bpy.ops.exmateria_map.reselect_clusters()
ck("BOTH compile buttons try to push once they have compiled",
   _button_pushes == ["Recalculate palettes", "Re-select clusters"],
   _button_pushes)
SO.push_after_compile = _real_pac
SO._push = lambda _ob: None

CO.MAP_OT_reselect_clusters.execute = _real_execute
SO._enabled = lambda _c: False    # the arms below are not about the settle



# ---------------------------------------------------------------------------
# ADR-0186 Amendment 10 -- an illegal Painting is REFUSED BY NAME.
#
# `export_source_art` read `if img is None or tuple(img.size) != (SHEET_W,
# SHEET_H): continue`, so a Painting that was not exactly 256x1024 was dropped
# from the document with no warning and no refusal.  Decision 4 calls the
# Painting the irreplaceable half of an authored map, and decision 11 exists
# because "an artist who painted detail and got back a blur, with nothing
# saying so, has no way to find out why".  Widening the size check is not the
# fix; the fix is that it becomes a refusal that says the name and the size.
#
# Schema 7.3b: EXPORT refuses.  Import warns and degrades -- a different
# posture on the same fact, and not this harness's arm.
#
# Placed BEFORE the round-trip block below, which imports a second copy of the
# map and leaves `ob` a removed StructRNA.
# ---------------------------------------------------------------------------
_sheet_art = sheet_of_state(ob, state)
_art = bpy.data.images.get(source_art_name(_sheet_art))
_keep = array.array("f", [0.0]) * (256 * 1024 * 4)
_art.pixels.foreach_get(_keep)

ck("control: the painting IS in the document before this arm touches it",
   bool((assemble(ob)[0] or {}).get("source_art")))

_art.scale(300, 1024)              # 300 is not 256k for any k in {1,2,4,8}
_doc_bad, _files_bad, _rep_bad = assemble(ob)
ck("an illegally-sized Painting is REFUSED, not silently dropped",
   any("painting" in r.lower() for r in _rep_bad.refusals), _rep_bad.refusals)
ck("...and the refusal names the IMAGE and the SIZE it found",
   any(_art.name in r and "300" in r and "1024" in r
       for r in _rep_bad.refusals), _rep_bad.refusals)

# A legal scale is ACCEPTED, and carries `@Nx` for N > 1 only (decision 43).
# This arm makes the state directly rather than converting at 4x, and keeps
# doing so now that the operator would: it is the EXPORT half of the rule, and
# reaching it through the bake would make it a claim about the bake as well --
# a 4x export would then be untested whenever the conversion was the thing
# that broke.  The conversion has its own arms further down.
_art.scale(1024, 4096)             # 256*4 x 1024*4
_doc_4x, _files_4x, _rep_4x = assemble(ob)
_keys4 = sorted((_doc_4x or {}).get("source_art") or {})
ck("a 4x Painting is accepted, not refused",
   bool(_keys4) and not any("painting" in r.lower() for r in _rep_4x.refusals),
   f"{_keys4} {_rep_4x.refusals}")
ck("...and its sidecar name carries @4x",
   bool(_keys4) and _keys4[0].endswith("@4x.png"), _keys4)
ck("...and the sidecar it wrote is a 4x PNG, not a 1x one",
   bool(_keys4) and _files_4x.get(_keys4[0], b"")[16:24]
   == (1024).to_bytes(4, "big") + (4096).to_bytes(4, "big"),
   _files_4x.get(_keys4[0], b"")[16:24].hex() if _keys4 else None)

_art.scale(256, 1024)
_art.pixels.foreach_set(_keep)
_art.update()
_keys1 = sorted((assemble(ob)[0] or {}).get("source_art") or {})
ck("control: restored to 1x, the painting is back and its name is BARE",
   bool(_keys1) and "@" not in _keys1[0], _keys1)


# ---------------------------------------------------------------------------
# ADR-0186 Amendment 10 decision 35 -- `_write_art` sizes itself from the ART.
#
# It hardcoded `w, h = 256, 1024`, so a 4x buffer would have been written into
# a 1x image: `img.pixels[:]` would raise, or -- worse, at a size that happens
# to fit -- the picture would silently be the top-left sixteenth of itself.
#
# The trap the handoff names is the ROW FLIP: this buffer is top-scanline-first
# and Blender`s pixel row 0 is the BOTTOM, and the flip is easy to get wrong at
# a new size.  TWO arms, because they see different things and the first one
# alone was measured BLIND to the defect it was written for.
#
# Arm 1 is the disc: the real converted 1x painting, replicated to 4x, written,
# read back, and SHRUNK must be the 1x painting again.  That is grading
# criterion 2 at this seam.  **It cannot see a permutation INSIDE an NxN
# block** -- measured, by seeding the flip as `(h//n - 1 - y//n)*n + y%n`,
# which reverses the four rows within each block and is identical to correct at
# N = 1.  `shrink` averages the block, and an average is order-blind; `expand`
# fed it four equal rows in the first place.  114/114 stayed green.
#
# Arm 2 is a round trip on a buffer whose every row and every column is
# distinguishable, against the SHIPPED `image_rgb` -- which derives its own
# flip and is proven at 1x by the arms above.  It reds on any permutation of
# rows or columns, the within-block one included.  (A round trip is only blind
# to a flip when BOTH sides are changed together; only `_write_art` is under
# change here.)
# ---------------------------------------------------------------------------
from exmateria_map.convert_op import _write_art
from exmateria_map import resample as _R

_art1 = image_rgb(_art)              # the REAL converted painting, from the disc
_rows1 = [_art1[3 * 256 * y:3 * 256 * (y + 1)] for y in range(1024)]
ck("control: the painting is not vertically symmetric "
   "(or arm 1 below is vacuous)",
   _art1 != b"".join(reversed(_rows1)))

_img4 = _write_art("exmateria_map.test/painting4x",
                   _R.expand(_art1, 256, 1024, 4))
ck("_write_art sizes the image from the BUFFER, not a hardcoded 256x1024",
   tuple(_img4.size) == (1024, 4096), tuple(_img4.size))
ck("arm 1: a 4x Painting written and read back SHRINKS to the 1x one, "
   "byte for byte (criterion 2 at this seam)",
   _R.shrink(image_rgb(_img4), 1024, 4096, 4) == _art1)

# R = the row`s low byte, G = the column`s, B = the row`s HIGH byte: every row
# and every column of the 4x picture is then named by its own content, so no
# permutation of either can survive the round trip.
_xs = bytes(x & 0xFF for x in range(1024))
_probe = bytearray(3 * 1024 * 4096)
for _y in range(4096):
    _r = bytearray(3 * 1024)
    _r[0::3] = bytes((_y & 0xFF,)) * 1024
    _r[1::3] = _xs
    _r[2::3] = bytes(((_y >> 8) & 0xFF,)) * 1024
    _probe[3 * 1024 * _y:3 * 1024 * (_y + 1)] = _r
_probe = bytes(_probe)
_img4.pixels.foreach_set(array.array("f", [0.0]) * (1024 * 4096 * 4))
_img4 = _write_art("exmateria_map.test/painting4x", _probe)
ck("arm 2: every row and column of a 4x Painting comes back where it went in",
   image_rgb(_img4) == _probe)
bpy.data.images.remove(_img4)

try:
    _write_art("exmateria_map.test/painting-bad", bytes(3 * 300 * 1024))
    _bad = "no raise"
except ValueError as _e:
    _bad = str(_e)
ck("...and a buffer that is no legal Painting is REFUSED, not reshaped",
   "300" in _bad or ("legal" in _bad.lower() and "no raise" != _bad), _bad)


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


# ---------------------------------------------------------------------------
# What the SETTLE'S PUSH does not have to do -- the artist's second freeze.
#
# Reported from use: *"release left click ... it takes maybe 3 seconds ... that
# whole time blender was unusable"*.  The first round moved the transport to a
# worker and left the main thread holding `assemble`, sized at 375 ms.  That
# number was measured at N = 1.  At Amendment 10's default of **N = 4** the
# same call measured **2,398 ms**, and two thirds of it is work no push wants:
#
#   * `rgb_from_floats` over 12.6 M texels -- the SAME walk the compile ran on
#     its worker a second earlier, redone on the main thread;
#   * `write_rgb_png` at zlib level 9 over the result, for a sidecar file the
#     push throws away.
#
# The arms below are behavioural: a detonator in place of each, and a control
# for each proving the detonator bites.  A source-level "it does not call it"
# would pass just as well against a function that was renamed.
# ---------------------------------------------------------------------------
from exmateria_map import export_document as _ED
from exmateria_map import png_indexed as _PI

_doc_full, _files_full, _rep_full = assemble(ob)
_doc_bare, _files_bare, _rep_bare = assemble(ob, sidecars=False)
ck("sidecars=False writes no file",
   _files_full and not _files_bare, f"{len(_files_full)} vs {len(_files_bare)}")
ck("...and hands back the SAME document",
   json.dumps(_doc_bare, sort_keys=True) == json.dumps(_doc_full, sort_keys=True))
ck("...and the same 4bpp sheets, which are what the push actually sends",
   _rep_bare.sheets == _rep_full.sheets and bool(_rep_bare.sheets),
   sorted(_rep_bare.sheets))
ck("...including the sidecar NAMES, which come from sha256(blob) not the PNG",
   sorted(_rep_bare.sheets) == sorted(_rep_full.sheets)
   and sorted((_doc_bare or {}).get("source_art") or {})
       == sorted((_doc_full or {}).get("source_art") or {}))

def _boom(*a, **k):
    raise AssertionError("encoded a sidecar the push throws away")

_real_rgb_png, _real_idx_png = _PI.write_rgb_png, _PI.write_indexed_png
_PI.write_rgb_png = _PI.write_indexed_png = _boom
_ED.png_indexed.write_rgb_png = _ED.png_indexed.write_indexed_png = _boom
try:
    assemble(ob, sidecars=False)
    _no_encode = "no encode"
except AssertionError as _e:
    _no_encode = str(_e)
# ...and the control: with sidecars ON the same detonator MUST fire, or this
# pair grades a map that has no sidecars to encode.
try:
    assemble(ob)
    _did_encode = "no encode"
except AssertionError as _e:
    _did_encode = str(_e)
_PI.write_rgb_png, _PI.write_indexed_png = _real_rgb_png, _real_idx_png
_ED.png_indexed.write_rgb_png = _real_rgb_png
_ED.png_indexed.write_indexed_png = _real_idx_png
ck("sidecars=False encodes no PNG at all", _no_encode == "no encode", _no_encode)
ck("...and the control proves the detonator bites",
   _did_encode != "no encode", _did_encode)

# The master cache.  `image_rgb` may answer from what a compile deposited, and
# only for the exact float buffer it was deposited against.
_ED.forget_masters()
_buf, _bw, _bh = _ED.image_floats(art_img)
_key = _ED.master_key(_buf)
_ED.remember_master(_key, _bw, _bh, b"the deposited master")
_real_walk = _ED.rgb_from_floats
_ED.rgb_from_floats = _boom
try:
    _served = _ED.image_rgb(art_img)
except AssertionError:
    _served = "walked"
ck("image_rgb is served from what the compile deposited",
   _served == b"the deposited master", str(_served)[:60])

# ...and it MISSES when the pixels move, which is the whole safety of it: the
# key is a sha256 of the float buffer, not the image's name or its dirty bit.
_px = list(art_img.pixels[:4])
art_img.pixels[:4] = [1.0 - _px[0], _px[1], _px[2], _px[3]]
try:
    _after = _ED.image_rgb(art_img)
    _missed = "served a stale master"
except AssertionError:
    _missed = "walked"
art_img.pixels[:4] = _px
_ED.rgb_from_floats = _real_walk
ck("...and a single changed texel takes the walk instead", _missed == "walked",
   _missed)
_ED.forget_masters()

# The whole point, on the artist's own path: a compile, then the push's
# main-thread half, and the walk must not happen a second time.
_L.LuaClient.check = lambda self: ""            # pretend an emulator answers
from exmateria_map.compile_op import compile_now as _compile_now
from exmateria_map.compile_op import _subject_of as _subject_of_ob
from exmateria_map import live_link_ui as _UI
_subj = _subject_of_ob(ob)
_compile_now(*_subj)
_ED.rgb_from_floats = _boom
try:
    _kw = _UI.push_gather(bpy.context, ob, _UI._Say())
    _push_walk = "no walk"
except AssertionError as _e:
    _push_walk = str(_e)
finally:
    _ED.rgb_from_floats = _real_walk
ck("the push after a compile re-derives no master on the main thread",
   _push_walk == "no walk", _push_walk)
# The control: drop the deposit and the same push MUST walk, or the arm above
# is grading a push that never asked for a master at all.
_ED.forget_masters()
_ED.rgb_from_floats = _boom
try:
    _UI.push_gather(bpy.context, ob, _UI._Say())
    _push_walk_ctl = "no walk"
except AssertionError as _e:
    _push_walk_ctl = str(_e)
finally:
    _ED.rgb_from_floats = _real_walk
ck("...and with the deposit dropped it walks, so that arm is not vacuous",
   _push_walk_ctl != "no walk", _push_walk_ctl)
_ED.forget_masters()

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


# ---------------------------------------------------------------------------
# ADR-0186 Amendment 10 / schema 7.3b -- the IMPORT's posture on the scale.
#
# `_build_source_art` hard-checked `(w, h) != (256, 1024)`.  Widening it is
# only half the job: the other half is that the posture stays the one the
# schema names, and it is the OPPOSITE of the export's.  Export refuses -- a
# bundle that shipped without the artist`s painting is unrecoverable.  Import
# **warns and degrades** -- an import that lost a file must still open, and
# that sheet falls back to its index -> CLUT preview, which is exactly what a
# missing sidecar already does.
#
# Placed LAST: a scale change makes `_write_art` remove and rebuild the image
# under the same name, so every earlier handle on it (`_art`, `back`) is a
# removed StructRNA from here on.
# ---------------------------------------------------------------------------
from exmateria_map import png_indexed as _P

def _import_with_art(tag, *paintings):
    """Write the round trip`s document with its source_art REPLACED, import it.

    Each painting is `(name, rgb, w, h, states)`; `states` None keeps the
    entry the round trip already wrote, which is the one naming the states
    that read the converted sheet.
    """
    _dir = os.path.join(r"@TMP@", tag)
    os.makedirs(_dir, exist_ok=True)
    _doc = json.loads(json.dumps(doc))
    _old = list(_doc["source_art"])[0]
    _doc["source_art"] = {}
    for _name, _rgb, _w, _h, _st in paintings:
        _doc["source_art"][_name] = (dict(_doc["source_art"].get(_name, {}),
                                          states=_st) if _st is not None
                                     else json.loads(json.dumps(
                                         json.loads(json.dumps(doc))
                                         ["source_art"][_old])))
        open(os.path.join(_dir, _name), "wb").write(
            _P.write_rgb_png(_rgb, _w, _h))
    open(os.path.join(_dir, "MAP022.a0.json"), "w").write(json.dumps(_doc))
    for _n, _b in files.items():
        if _n != _old:
            open(os.path.join(_dir, _n), "wb").write(_b)
    # Every painting image goes first.  They are named from the SHEET, so the
    # round trip above left one under the name this import would use -- and a
    # `bpy.data.images.get` that finds it cannot tell "this import made it"
    # from "this import made nothing and you are looking at the last one".
    # Measured: without this the 2x arm read a 256x1024 image and the failure
    # named the wrong cause.
    for _i in [_i for _i in bpy.data.images
               if _i.name.startswith("exmateria_map.source/")]:
        bpy.data.images.remove(_i)
    _was = set(bpy.data.objects)
    _log = io.StringIO()
    with contextlib.redirect_stdout(_log):
        bpy.ops.import_map.document(
            filepath=os.path.join(_dir, "MAP022.a0.json"))
    _new = [o for o in bpy.data.objects if o not in _was
            and "exmateria_map/base" in o]
    return (_new[0] if _new else None), _log.getvalue()

import contextlib, io
_stem = list(doc["source_art"])[0][:-4]          # ...source-<digest>

_ob2, _log2 = _import_with_art(
    "import2x",
    (_stem + "@2x.png", _R.expand(sent, 256, 1024, 2), 512, 2048, None))
ck("a map whose Painting is 2x still imports", _ob2 is not None, _log2[-300:])
_sheet2x = sheet_of_state(_ob2, active_palette(_ob2)[0]) if _ob2 else None
_img2x = bpy.data.images.get(source_art_name(_sheet2x)) if _sheet2x else None
ck("...and its Painting comes back at 2x, not refused for not being 256x1024",
   _img2x is not None and tuple(_img2x.size) == (512, 2048),
   tuple(_img2x.size) if _img2x else None)
ck("...and it is the picture that went in, texel for texel",
   _img2x is not None and tuple(_img2x.size) == (512, 2048)
   and _R.shrink(image_rgb(_img2x), 512, 2048, 2) == sent)

_ob3, _log3 = _import_with_art(
    "importbad", (_stem + ".png", bytes(3 * 300 * 1024), 300, 1024, None))
ck("a map whose Painting is an ILLEGAL size still OPENS (import degrades)",
   _ob3 is not None, _log3[-300:])
_sheetbad = sheet_of_state(_ob3, active_palette(_ob3)[0]) if _ob3 else None
_imgbad = bpy.data.images.get(source_art_name(_sheetbad)) if _sheetbad else None
ck("...and that sheet has NO painting, so it previews through the CLUT",
   _imgbad is None, getattr(_imgbad, "name", None))
ck("...and the warning names the size, the RULE and the consequence",
   "300" in _log3 and "warning" in _log3.lower()
   and "256k x 1024k" in _log3 and str(list(_R.SCALES)) in _log3
   and "CLUT" in _log3, _log3[-400:])


# Two paintings that DISAGREE on scale.  N is per map (decision 43), so this
# is not one map`s worth of art -- and the two legs take opposite postures on
# it, which is the whole reason both are checked from one fixture:  the import
# loads both and says so, the export refuses.  A second entry is fabricated on
# a state reading a DIFFERENT sheet, because dedup is by content and two
# entries on one sheet would collapse to one image either way.
_other = None
for _i, _s in enumerate(_state_sheets):
    if _s and _s != sheet_now:
        _other = _i
        break
ck("control: MAP022 a0 has a second sheet to hang the other painting on",
   _other is not None, _state_sheets)
_ob4, _log4 = _import_with_art(
    "importmixed",
    (_stem + "@2x.png", _R.expand(sent, 256, 1024, 2), 512, 2048, None),
    (_stem + "-two.png", sent, 256, 1024, [_other]))
ck("a map whose paintings DISAGREE on scale still imports", _ob4 is not None,
   _log4[-300:])
_mixed = sorted(i.name for i in bpy.data.images
                if i.name.startswith("exmateria_map.source/"))
ck("...and BOTH are loaded -- the import degrades, it does not drop one",
   len(_mixed) == 2, _mixed)
ck("...and the warning names the disagreement and points at the export",
   "disagree on scale" in _log4 and "refuse" in _log4, _log4[-400:])
ck("...and the EXPORT is the leg that refuses it (schema 7.3b)",
   _ob4 is not None
   and any("disagree" in r for r in assemble(_ob4)[2].refusals),
   assemble(_ob4)[2].refusals if _ob4 else None)


# ---------------------------------------------------------------------------
# ADR-0186 Amendment 10 decision 36 -- the Convert button carries the SCALE.
#
# The default is **1**, not decision 36's 4, and that is a staging choice
# rather than a disagreement: decision 40's Canvas (High / Native) control is
# not built, and an artist handed a 4x canvas with no native view has no
# gesture that means "one texel".  It is one line when that lands.  Keeping it
# at 1 is also what makes grading criterion 1 -- nothing existing moves --
# still say something: every arm above ran at the default.
#
# The claim here is end to end through the REAL operator on a FRESH import,
# because everything above converted at 1x and a conversion is one-way.
# ---------------------------------------------------------------------------
# An operator's own properties are NOT on the operator TYPE's `bl_rna`.  That
# route -- `bpy.types.EXMATERIA_MAP_OT_convert_manifold.bl_rna.properties` --
# returns only the base Operator members (`bl_idname`, `layout`, `options`,
# ...) and answers `None` for `scale`, which reads exactly like a missing
# property.  It is not: the arms below passed the property by keyword and the
# conversion ran at 2x.  Measured against a KNOWN-GOOD control before believing
# it -- `IMPORT_MAP_OT_document`, which certainly has a `filepath`, returns the
# identical base-only list.  The properties live on the generated
# `OperatorProperties` RNA, which `bpy.ops.<op>.get_rna_type()` hands back.
# Same shape as the `hasattr(bpy.ops.import_map, "gns")` trap this package's
# CLAUDE.md already records: a plausible introspection route that cannot see
# what it claims to see, so it grades the addon on the harness's own blindness.
_prop = bpy.ops.exmateria_map.convert_manifold.get_rna_type().properties.get("scale")
ck("Convert carries a scale", _prop is not None)
ck("...over exactly decision 36's set", _prop is not None
   and [i.identifier for i in _prop.enum_items] == [str(_n) for _n in _R.SCALES],
   [i.identifier for i in _prop.enum_items] if _prop else None)
# Decision 36's own number.  It shipped staged at 1 while decision 40's Canvas
# control did not exist -- a 4x canvas with no native view leaves the artist no
# gesture that means "one texel" -- and that blocker is built, so the staging
# expired (Amendment 11 decision 48).  This arm reads the DECLARATION; the arm
# at the foot of this file converts with no arguments at all and is the one
# that would notice `execute` ignoring it.
ck("...defaulting to 4, which is decision 36's number",
   _prop is not None and _prop.default == "4",
   getattr(_prop, "default", None))
ck("...and the default is still REACHABLE where Blender shows it, which is "
   "no longer the only place -- the redo panel only draws for a REGISTER + "
   "UNDO operator, and the Paint panel now carries a Scale number besides",
   {"REGISTER", "UNDO"} <= set(
       bpy.types.EXMATERIA_MAP_OT_convert_manifold.bl_options),
   sorted(bpy.types.EXMATERIA_MAP_OT_convert_manifold.bl_options))

_was5 = set(bpy.data.objects)
for _i in [_i for _i in bpy.data.images
           if _i.name.startswith("exmateria_map.source/")]:
    bpy.data.images.remove(_i)
bpy.ops.import_map.document(filepath=r"@JSON@")
_ob5 = [o for o in bpy.data.objects if o not in _was5
        and "exmateria_map/base" in o][0]
bpy.context.view_layer.objects.active = _ob5
for o in bpy.data.objects:
    o.select_set(o is _ob5)
_state5, _ = active_palette(_ob5)
_sheet5 = sheet_of_state(_ob5, _state5)
_img5 = index_image(_ob5, _sheet5)
_me5 = _ob5.data

_r5 = bpy.ops.exmateria_map.convert_manifold(scale="2")
ck("Convert at 2x finished", _r5 == {"FINISHED"}, _r5)
_p5 = bpy.data.images.get(source_art_name(_sheet5))
ck("...and the Painting it baked is 2x",
   _p5 is not None and tuple(_p5.size) == (512, 2048),
   tuple(_p5.size) if _p5 else None)

# Decision 35: a conversion REPLICATES, it does not smooth -- so shrinking the
# 2x bake must give back exactly the 1x bake, and every 2x2 block must be one
# colour.  The second is the arm that separates a replicate from a resample;
# without it a smoothing bake would still shrink to something very close.
_shr = _R.shrink(image_rgb(_p5), 512, 2048, 2)
ck("...and it shrinks to the 1x bake this same map produced, byte for byte",
   _shr == _master1)
_blocky = all(image_rgb(_p5)[3 * (2 * _y * 512 + 2 * _x):
                             3 * (2 * _y * 512 + 2 * _x) + 3]
              == image_rgb(_p5)[3 * ((2 * _y + 1) * 512 + 2 * _x + 1):
                                3 * ((2 * _y + 1) * 512 + 2 * _x + 1) + 3]
              for _y in range(0, 1024, 97) for _x in range(0, 256, 13))
ck("...and every 2x2 block is FLAT: the bake replicated, it did not smooth",
   _blocky)

# The Sheet does NOT scale with it (decision 35): it is the disc's own
# resource and carries no N at all.
ck("...and the Sheet stayed 1x -- it is the disc's resource, not the canvas",
   tuple(_img5.size) == (256, 1024), tuple(_img5.size))


# ---------------------------------------------------------------------------
# ADR-0186 Amendment 10 decision 40 -- Canvas (High / Native) is its OWN
# control, and at N = 1 it is ABSENT rather than present-and-inert.
#
# Registered Object property, not an `exmateria_map/...` custom property, for
# the reason `apply_preview_source` already gives: those carry the document in
# the ROM's own shape and a view has no ROM representation.  It is NOT a third
# item on `PREVIEW_MODES` -- decision 40 -- so the arm below reads a second
# property and the existing one is untouched.
#
# The route is measured against a KNOWN-GOOD control first.  An operator's
# properties are invisible on its type's `bl_rna` (see the Convert block
# above); an ID type's are not, and this arm is what says so IN THIS RUN
# rather than by assumption.
# ---------------------------------------------------------------------------
from exmateria_map import import_document as _ID
_route = bpy.types.Object.bl_rna.properties
ck("control: this route DOES see a registered Object property",
   _route.get("exmateria_map_preview_source") is not None)
_cprop = _route.get("exmateria_map_canvas")
ck("Canvas is its own registered Object property (dec. 40)", _cprop is not None)
ck("...over High / Native, and it did NOT grow a third PREVIEW_MODE",
   _cprop is not None
   and [i.identifier for i in _cprop.enum_items] == ["HIGH", "NATIVE"]
   and [k for k, _l, _d in _ID.PREVIEW_MODES] == ["RAW", "QUANTISED"],
   ([i.identifier for i in _cprop.enum_items] if _cprop else None,
    [k for k, _l, _d in _ID.PREVIEW_MODES]))
ck("...defaulting to High -- the master is where authoring happens (dec. 35)",
   _cprop is not None and _cprop.default == "HIGH",
   getattr(_cprop, "default", None))

# What the panel EMITS, not what its source says.  `_ob5` is the 2x map this
# block just converted and is still the active object.
_draw5 = drawn()
ck("...and the Paint panel offers it on a 2x map",
   emits(_draw5, "exmateria_map_canvas"), _draw5)

# ---------------------------------------------------------------------------
# ADR-0186 Amendment 10 decision 40 -- the switch moves where strokes LAND.
#
# `_show_source_art` records that `nodes.active` and `Material.paint_active_slot`
# are two views of ONE pointer, and that is the whole reason Canvas could not
# be a `PREVIEW_MODES` item: those change "nothing about the document", and
# this changes what a stroke writes into.  So the arm reads the ACTIVE image
# texture node, not what the viewport samples.
#
# This block sits BEFORE the fresh 1x import below and not after it,
# because importing the same map a second time FREES the first object:
# `_ob5` becomes a dead StructRNA and every arm here dies on
# `ReferenceError: StructRNA of type Object has been removed` -- measured,
# and the harness then writes no report at all rather than one red arm.
#
# The native canvas is DERIVED (decision 35), so it is graded against
# `resample.shrink` of the master rather than against itself -- the master is
# the independent source of truth here, and an arm comparing the canvas to a
# second call of the code that built it would pass by construction.
# ---------------------------------------------------------------------------
from exmateria_map.convert_op import native_canvas_name

def _active_art(ob):
    """The image a Texture Paint stroke would land in, by name."""
    for _slot in ob.material_slots:
        _nt = getattr(_slot.material, "node_tree", None)
        if _nt is None or _nt.nodes.get("exmateria_map.source_art") is None:
            continue
        return getattr(getattr(_nt.nodes.active, "image", None), "name", None)
    return None

# Who owns `size` is not obvious and is not `tool_settings` -- measured:
# `ToolSettings` has no `unified_paint_settings` at all, it hangs off
# `image_paint`, and whether the BRUSH or the unified block holds the value
# depends on `use_unified_size`.  `paint.paint_owner` is the package's own
# answer to exactly that question, so the arm asks it rather than a second
# guess that could read a field the artist's brush is not using.
def _brush_size():
    _ip = bpy.context.tool_settings.image_paint
    return getattr(P.paint_owner(_ip, "use_unified_size"), "size", None)

_brush_before = _brush_size()
ck("control: there IS a brush size to be left alone", _brush_before is not None)
ck("control: at High, strokes land in the MASTER",
   _active_art(_ob5) == source_art_name(_sheet5), _active_art(_ob5))
ck("control: no native canvas exists until it is asked for",
   bpy.data.images.get(native_canvas_name(_sheet5)) is None)

_ob5.exmateria_map_canvas = "NATIVE"
_nat = bpy.data.images.get(native_canvas_name(_sheet5))
ck("Native derives a canvas at the SHEET's resolution",
   _nat is not None and tuple(_nat.size) == (256, 1024),
   tuple(_nat.size) if _nat else None)
ck("...whose pixels are the master shrunk -- derived, not painted (dec. 35)",
   _nat is not None and image_rgb(_nat) == _R.shrink(image_rgb(_p5), 512, 2048, 2))
ck("...and strokes now LAND there, not merely display there (dec. 40)",
   _active_art(_ob5) == native_canvas_name(_sheet5), _active_art(_ob5))
# Decision 39: the canvas is NEVER saved into the `.blend`.  The master IS --
# so this is a claim about a difference, and the control says the master packs
# in the same run.  Without the control it would pass on a build that packed
# nothing at all, and the Painting would be the thing lost.
ck("...and the canvas is NOT saved into the .blend (dec. 39)",
   _nat is not None and _nat.packed_file is None)
ck("control: the MASTER is, so that arm is a difference and not a default",
   _p5.packed_file is not None)

# Decision 41, stated as an arm because the amendment states it as a decision:
# the chunkiness is the canvas's, not the brush's, and seeding on every toggle
# would be the force #423 spent months rejecting.
ck("...and it did NOT touch the brush (dec. 41)",
   _brush_size() == _brush_before, (_brush_size(), _brush_before))
# Orthogonality, the other half of decision 40: the view control is untouched
# by the canvas control.
ck("...and it did NOT touch the Painting/Compiled view",
   _ob5.exmateria_map_preview_source == "RAW",
   _ob5.exmateria_map_preview_source)
ck("...and the Sheet the game reads is still 1x and still there",
   tuple(_img5.size) == (256, 1024))

_ob5.exmateria_map_canvas = "HIGH"
ck("...and High puts them back in the master",
   _active_art(_ob5) == source_art_name(_sheet5), _active_art(_ob5))


# ---------------------------------------------------------------------------
# ADR-0186 Amendment 10 decision 39 -- the write-through rides the SETTLE tick,
# as its first stage: write-through -> shrink -> compile -> push.
#
# Not on the mode switch, which would leave the Sheet and anything pushed
# stale for the whole time the artist paints at native resolution -- deleting
# what Amendment 7 exists for -- and not behind a button, which contradicts
# "switch between the two seamlessly".
#
# The second arm is grading criterion 4 and is the reason this is graded at
# all: a native stroke must not erase detail it did not touch.  Stamping every
# block would be correct ON the canvas and catastrophic underneath it -- one
# native stroke would flatten the entire N-times painting -- so the arm is a
# claim about the WHOLE master, not about the block that changed.
# ---------------------------------------------------------------------------
SO._enabled = _real_enabled
SO._CLOCKS.clear()
SO._RESULT.clear()
_ob5.exmateria_map_canvas = "NATIVE"
_nat = bpy.data.images.get(native_canvas_name(_sheet5))
_before = image_rgb(_p5)

_NX, _NY = 10, 20                       # a native texel, top-scanline space
_ink = bytes((255, 0, 128))
_cpx = [0.0] * (256 * 1024 * 4)
_nat.pixels.foreach_get(_cpx)
_j = ((1024 - 1 - _NY) * 256 + _NX) * 4     # Blender row 0 is the BOTTOM
_cpx[_j], _cpx[_j + 1], _cpx[_j + 2] = 255 / 255.0, 0 / 255.0, 128 / 255.0
_nat.pixels.foreach_set(_cpx)
_nat.update()

_at = 2 * _NY * 512 + 2 * _NX
ck("control: the ink is not already the colour the master holds there",
   _before[3 * _at:3 * _at + 3] != _ink,
   tuple(_before[3 * _at:3 * _at + 3]))
_settle(4.0)
_after = image_rgb(_p5)
_block = {(2 * _NY + _dy) * 512 + 2 * _NX + _dx
          for _dy in (0, 1) for _dx in (0, 1)}
ck("a native stroke lands in the master as a flat N x N block (dec. 35)",
   all(_after[3 * _i:3 * _i + 3] == _ink for _i in sorted(_block)),
   [tuple(_after[3 * _i:3 * _i + 3]) for _i in sorted(_block)])
_moved = {_i for _i in range(512 * 2048)
          if _after[3 * _i:3 * _i + 3] != _before[3 * _i:3 * _i + 3]}
ck("...and NOTHING else in the master moved (criterion 4)",
   _moved == _block, (len(_moved), sorted(_moved)[:8]))

# The reopened-file case, which decision 39 creates and gives no hook for: the
# canvas datablock survives a reload because Blender saves the DATABLOCK, but
# its pixels do not, because decision 39 refuses to pack it -- and nothing
# records what was last derived into it.  Stamping that through would write
# whatever the reload left there over the master, at every pixel, which is the
# criterion-4 catastrophe arriving by the back door.  So a canvas with no
# baseline is RE-DERIVED and the strokes on it are discarded, which is what
# "regenerated from the master on load" means, moved to the first tick that
# notices.  Simulated by clearing the baseline, since a reload cannot be.
from exmateria_map import convert_op as _CV
_CV._CANVAS_WAS.clear()
_before2 = image_rgb(_p5)
_nat.pixels.foreach_get(_cpx)
_j2 = ((1024 - 1 - 40) * 256 + 30) * 4
_cpx[_j2], _cpx[_j2 + 1], _cpx[_j2 + 2] = 0.0, 1.0, 0.0
_nat.pixels.foreach_set(_cpx)
_nat.update()
ck("control: that second stroke really is a change to the canvas",
   image_rgb(_nat) != _R.shrink(_before2, 512, 2048, 2))
SO._CLOCKS.clear()
_settle(3.0)
ck("a canvas with NO baseline is re-derived, not stamped through (dec. 39)",
   image_rgb(_p5) == _before2)
ck("...and the re-derive puts the master back into it",
   image_rgb(_nat) == _R.shrink(_before2, 512, 2048, 2))
SO._enabled = lambda _c: False


# ---------------------------------------------------------------------------
# The scale as a NUMBER beside the compile buttons -- reported from use, and
# the reason is that the redo panel is not a UI.  Blender's Adjust Last
# Operation exists for exactly one gesture after the operator runs and is gone
# the moment you click anything else, so `Convert` + F9 is a power-user idiom
# and not somewhere an artist can go to change their mind.
#
# It STORES NOTHING.  Decision 43 derives N from the picture's own dimensions
# and refuses a stored `scale` key as the redundant, driftable copy; a plain
# registered property would be that copy, because `bpy.props` writes into the
# ID property store.  So this is `get`/`set`: the number READS the image, and
# assigning to it RESCALES the image.  The control arm is that the name never
# appears in `ob.keys()`.
#
# Up is lossless and down is not: `expand` replicates, so raising the number
# is the same replicate the bake does -- which is what makes "convert at 1x
# then set 4" byte-identical to "convert at 4x", criterion 2 restated as a
# gesture.  Lowering it box-averages and cannot be undone.
# ---------------------------------------------------------------------------
_m2 = image_rgb(_p5)                       # the 2x master as it now stands
ck("control: the scale is not STORED anywhere on the object (dec. 43)",
   "exmateria_map_painting_scale" not in _ob5.keys(), list(_ob5.keys()))
ck("the number READS the picture: a 2x Painting reports 2",
   _ob5.exmateria_map_painting_scale == 2,
   _ob5.exmateria_map_painting_scale)

_ob5.exmateria_map_canvas = "HIGH"          # so the paint target IS the master
_ob5.exmateria_map_painting_scale = 4
ck("...setting it to 4 rescales the Painting itself",
   tuple(_p5.size) == (1024, 4096), tuple(_p5.size))
# The datablock must SURVIVE the rescale.  Freeing and remaking it would empty
# the material's `source_art` node and any Image Editor showing it -- the
# artist's viewport goes untextured at the exact moment they change the scale,
# and nothing says why.
ck("...and the SAME image datablock survived it, so the paint target is intact",
   _active_art(_ob5) == source_art_name(_sheet5), _active_art(_ob5))
# Graded by the INVERSE function, not by re-running `expand` the way the code
# does -- that would recompute the expected value from the implementation and
# could never disagree with it.
ck("...losslessly: shrinking it back by 2 is the 2x master, byte for byte",
   _R.shrink(image_rgb(_p5), 1024, 4096, 2) == _m2)
ck("...and the number now reads 4", _ob5.exmateria_map_painting_scale == 4,
   _ob5.exmateria_map_painting_scale)
# Still on HIGH, deliberately.  Switching back to NATIVE first would call
# `apply_canvas`, which re-derives the canvas as well -- so the arm would pass
# whether or not the SETTER re-derived it, and the setter is what is on trial:
# it is the only path that can leave a canvas sized for a master that is gone.
ck("...and the native canvas followed it, not the size it was derived at",
   image_rgb(bpy.data.images[native_canvas_name(_sheet5)])
   == _R.shrink(image_rgb(_p5), 1024, 4096, 4))
_ob5.exmateria_map_canvas = "NATIVE"

# Decision 36 makes a down-conversion "a deliberate, warned act". A property
# setter has no dialog to warn in, so the warning is the Log -- and the arm
# needs BOTH directions, or a setter that logged on every change would pass it.
_LOG = "exmateria-map report"
_log_up = (bpy.data.texts[_LOG].as_string() if _LOG in bpy.data.texts else "")
ck("control: RAISING the scale is lossless and says nothing",
   "painting scale" not in _log_up.lower(), _log_up[-200:])

_ob5.exmateria_map_painting_scale = 2
ck("...and coming back down restores the 2x master exactly",
   tuple(_p5.size) == (512, 2048) and image_rgb(_p5) == _m2,
   tuple(_p5.size))
_log_down = (bpy.data.texts[_LOG].as_string() if _LOG in bpy.data.texts else "")
ck("...and LOWERING it is reported, because it destroyed detail (dec. 36)",
   "painting scale" in _log_down.lower()
   and len(_log_down) > len(_log_up), _log_down[-300:])

# A number field can be typed into and dragged, so it will be handed values
# that are no scale at all.  Snapped UP -- `resample.snap_scale` holds the
# rule and pytest enumerates it; this arm is only the witness that the
# property routes through it rather than implementing its own.
_ob5.exmateria_map_painting_scale = 3
ck("an illegal number snaps to a legal scale rather than being refused",
   _ob5.exmateria_map_painting_scale == 4
   and tuple(_p5.size) == (1024, 4096),
   (_ob5.exmateria_map_painting_scale, tuple(_p5.size)))
_ob5.exmateria_map_painting_scale = 2

_draw7 = drawn()
ck("...and the panel offers it where the compile buttons are",
   emits(_draw7, "exmateria_map_painting_scale"), _draw7)


# The absence arm, with its own control.  A fresh import converted at 1x --
# NOT `ob` from the top of this file, whose Painting the fixture above deleted:
# a panel that bailed early for want of an image would satisfy "does not emit"
# while proving nothing.  So the control asserts the panel drew at all.
_was6 = set(bpy.data.objects)
bpy.ops.import_map.document(filepath=r"@JSON@")
_ob6 = [o for o in bpy.data.objects if o not in _was6
        and "exmateria_map/base" in o][0]
bpy.context.view_layer.objects.active = _ob6
for o in bpy.data.objects:
    o.select_set(o is _ob6)
bpy.ops.exmateria_map.convert_manifold(scale="1")
_sheet6 = sheet_of_state(_ob6, active_palette(_ob6)[0])
_p6 = bpy.data.images.get(source_art_name(_sheet6))
ck("control: the 1x map converted and has a 1x Painting",
   _p6 is not None and tuple(_p6.size) == (256, 1024),
   tuple(_p6.size) if _p6 else None)
_draw6 = drawn()
ck("control: the panel DID draw for the 1x map",
   emits(_draw6, "convert_manifold"), _draw6)
ck("...and at N = 1 the Canvas control is ABSENT, not inert (dec. 40)",
   not emits(_draw6, "exmateria_map_canvas"), _draw6)


# ---------------------------------------------------------------------------
# The SHIPPED default (decision 36: "4 is the default at conversion").
#
# Every other conversion in this file names its scale, which is right for what
# each of them claims and leaves nobody testing what the artist actually gets.
# So this one passes NO arguments -- the only call here that goes through the
# operator's own default, and the only one that would notice it being staged
# back to 1 or mis-typed as an int.  Reading `.default` off the RNA is not a
# substitute: it says what is declared, not what `execute` does with it.
# ---------------------------------------------------------------------------
bpy.ops.exmateria_map.convert_manifold()
_p6d = bpy.data.images.get(source_art_name(_sheet6))
ck("converting with NO arguments gives a 4x Painting (dec. 36)",
   _p6d is not None and tuple(_p6d.size) == (1024, 4096),
   tuple(_p6d.size) if _p6d else None)
ck("...and the number field reads the 4 back off it",
   _ob6.exmateria_map_painting_scale == 4,
   _ob6.exmateria_map_painting_scale)
_draw6d = drawn()
ck("...and NOW the Canvas control is offered, which is why 4 can be the "
   "default at all (dec. 40)",
   emits(_draw6d, "exmateria_map_canvas"), _draw6d)


# ---------------------------------------------------------------------------
# The numpy guard (ADR-0186 Amendment 13 decision 54).
#
# Decision 52 refuses a retained pure-Python fallback because a branch nobody
# executes is untested code -- so the ONE branch that remains has to be
# executed on purpose.  A Blender without numpy cannot be arranged here (it is
# a hard dependency of the package and of Blender's own bundled add-ons), so
# the import is blocked instead and the package re-imported from cold.
#
# Last in the file deliberately: it purges `exmateria_map` from `sys.modules`
# and nothing after it could trust what it re-imports.
# ---------------------------------------------------------------------------
class _NoNumpy:
    def find_module(self, name, path=None):
        return None
    def find_spec(self, name, path=None, target=None):
        if name == "numpy" or name.startswith("numpy."):
            raise ImportError("no numpy (seeded)")
        return None

_saved = {k: v for k, v in sys.modules.items()
          if k == "exmateria_map" or k.startswith("exmateria_map.")
          or k == "numpy" or k.startswith("numpy.")}
for k in _saved:
    del sys.modules[k]
sys.meta_path.insert(0, _NoNumpy())
_frames = []
try:
    import exmateria_map as _cold
    _refusal = "IT IMPORTED ANYWAY"
except ImportError as _e:
    import traceback as _tb
    _refusal = str(_e)
    _frames = [f.filename for f in _tb.extract_tb(_e.__traceback__)]
finally:
    sys.meta_path.pop(0)
    for k in list(sys.modules):
        if k == "exmateria_map" or k.startswith("exmateria_map."):
            del sys.modules[k]
    sys.modules.update(_saved)

ck("without numpy the addon REFUSES to load, by name and not by traceback "
   "(dec. 54)",
   "needs numpy" in _refusal, _refusal[:160])
ck("...and the refusal names the interpreter running Blender, which is the "
   "one thing a traceback would not say",
   sys.executable in _refusal, _refusal[:160])
ck("...and it is the PACKAGE's own front door that raises, not the first of "
   "thirteen modules to reach for numpy (dec. 52)",
   bool(_frames) and _frames[-1].replace("\\", "/").endswith(
       "exmateria_map/__init__.py"),
   _frames[-1] if _frames else "did not raise at all")

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
