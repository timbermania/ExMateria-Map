"""File > Import/Export: the interchange import.

Decision 7's surface is `File > Import/Export` plus an N-panel; import stays
a plain operator so the addon stays testable headless. The operator validates
the document (the refusal rule and top-level shape, schema v1 §2–§3), then
builds the scene graph per docs/interchange-import-v1.md:

- one mesh object + one collection named `<map>.a<arrangement>`, re-import
  deletes and rebuilds the collection (§1);
- decision 14's axis frame `(x, y, z) -> (x, z, -y)` on positions AND
  normals, the PSX triangle-strip ring reversed on import, and the per-face
  flip of contrary-wound textured polygons recorded in the BOOL face
  attribute `fft_ring_flipped` (§1);
- every carried field as a schema-v1 named attribute with its `<field>_shadow`
  twin, written once at import and never artist-touched (§3);
- the terrain grid footprint and the level-0 tile objects (§5);
- one JSON custom property per top-level document section on the mesh
  object (§6);
- two material slots: the shared per-scene unlit grey (slot 0, untextured
  polygons and tiles) and the interchange preview material (slot 1), the
  face index following the `textured` flag (§4);
- the preview graph (§4): the sidecar's raw indices decoded into one
  256x1024 float index image per distinct sheet, one 16x16 CLUT image per
  `map_states` entry with valid palettes (PLTE fallback when `palettes` is
  null), UV -> index -> CLUT(palette_id row) -> colour x (ambient + corner
  diffuse) -> clamp -> sRGB decode -> emission.  The whole chain multiplies in
  PSX BYTE space (#427), so both images are `Non-Color` and the decode happens
  once at the end, where the display's own sRGB encode hands the byte back.
  The lighting term is split the way BOTH references split it — the PSX GTE
  holds ambient in a register and Gouraud-interpolates only the per-vertex
  output; Godot declares `ambient_light` a `uniform` against a `v_diffuse`
  `varying` — so `ambient` is a graph constant per map state and the corner
  `diffuse` attribute carries `sum(gain . max(0, N.L))` BAKED PER CORNER from
  the selected state's `light_rig` (schema §7.1).  Per corner because that is
  the PSX GTE's sample rate; a graph computing it per fragment dissolves the
  facet edges.  A rig-less arrangement bakes diffuse 0 against ambient 1.0 —
  albedo only — and the panel says so.  The N-panel state selector rewires the
  CLUT (and, when the state names a different sheet, the index image), RE-BAKES
  the diffuse and rewrites ambient (the rig is per map state), and pins the
  Standard view transform.

The N-panel also carries Godot's `map_light_debug` / `map_light_boost` as a
mode enum that rewires WHICH stage feeds the sRGB decode (`set_light_debug`).
Those two are VIEW state and are registered Object properties, kept out of the
`exmateria_map/...` JSON custom properties that carry the document in the ROM's
own shape.

The preview reaches parity with `indexed_color.gdshader` on the SHADING model
only.  Known limitation, not an omission: the game reproduces the PSX Ordering
Table by writing a per-FACE constant `DEPTH`, so overlapping or interpenetrating
polygons sort by face centroid there and by true per-pixel nearness here.  A
Blender material has no depth output — Surface, Volume, Displacement is the
whole set — so this cannot be reproduced in the preview graph at all.
"""
import json
import os
import textwrap
from pathlib import Path

import bpy
from bpy.props import (CollectionProperty, EnumProperty, FloatProperty,
                       FloatVectorProperty, IntProperty, IntVectorProperty,
                       StringProperty)
from bpy.types import (AddonPreferences, Menu, Operator, Panel,
                       PropertyGroup)

from . import png_indexed
from .live_link import DEFAULT_HOST as LIVE_DEFAULT_HOST
from .live_link import DEFAULT_PORT as LIVE_DEFAULT_PORT

FORMAT = "exmateria-map/interchange"
#: What an ordinary export stamps. `version` is the OLDEST addon/`build` that
#: can handle the document, not a format serial (ADR-0004 decision 27), so this
#: is a floor and ACCEPTED_VERSIONS is what the import leg takes.
VERSION = 1
ACCEPTED_VERSIONS = (1, 2)
#: A document carrying an authored light rig stamps this, and only then.
AUTHORED_RIG_VERSION = 2
#: The `map_states` field an authored rig rides in (schema §7.1). Its PRESENCE
#: is the declaration, so an untouched document carries no key at all.
AUTHORED_RIG = "authored_light_rig"
TOP_LEVEL = ("format", "version", "base", "polygons", "terrain", "map_states", "carry")
REQUIRED_BASE = ("map", "arrangement", "resources", "geometry_source", "geometry_digest")

UNLIT_GREY = "exmateria_map_unlit_grey"
TILE_UNITS = 28      # world units per terrain tile (decision 13's scale)
HEIGHT_STEP = 12     # world Y per terrain `height` step

TEXTURED_KINDS = {"textured_quad", "textured_triangle"}
# The FF FF terrain binding -- "this face is not on the grid".  NOT schema
# §5.2's worked example {255, 127, 0}, which is FF FE, a shipped
# OUT-OF-GRID binding on a live polygon (export-v1 §8.1).
SENTINEL_BINDING = {"x": 255, "z": 127, "level": 1}

# ---------------------------------------------------------------------------
# ADR-0004 decision 14 — the axis frame, as a PINNED CONSTANT.
#
# Handedness and up are forced by the corpus (Newell against the file's own
# normals; 97.26% of floor-like polygons); the 90-degree rotation about up is
# not separable by any property of the file, so this constant IS the
# ratification — the harness reads it back against
# `exmateria-map/blender_axis_baseline.json` (decision 8's fixed expectation).
# ---------------------------------------------------------------------------
AXIS_NAME = ("x", "z", "-y")
REVERSE_RING = True
WIND = 0.5           # the flip predicate's +-0.5 threshold (decision 14's residue)


def _fft_to_blender(v):
    return (v[0], v[2], -v[1])


def _blender_to_fft(v):
    return (v[0], -v[2], v[1])


def validate(doc):
    """The refusal rule and top-level shape (schema v1). Returns a problem list."""
    problems = []
    if doc.get("format") != FORMAT:
        problems.append(f"format is {doc.get('format')!r}, must be {FORMAT!r}")
    if doc.get("version") not in ACCEPTED_VERSIONS:
        problems.append(f"version is {doc.get('version')!r}, must be one of "
                        f"{ACCEPTED_VERSIONS!r}")
    for key in TOP_LEVEL:
        if key not in doc:
            problems.append(f"missing top-level key {key!r}")
    base = doc.get("base")
    if isinstance(base, dict):
        for key in REQUIRED_BASE:
            if key not in base:
                problems.append(f"missing base.{key}")
    return problems


# ---------------------------------------------------------------------------
# decision 14's import geometry.
# ---------------------------------------------------------------------------

def ring(n):
    """doc corner order -> the PSX triangle-STRIP ring (0,1,3,2 for quads)."""
    if n == 4:
        return (0, 1, 3, 2)
    return tuple(range(n))


def import_order(n, flipped=False):
    """doc corner indices, in the order they are laid into Blender's loops.

    Decision 14 reverses the ring on import.  A polygon the 1997 data wound
    against its own convention is reversed a SECOND time, and that second
    reversal is what the BOOL face attribute records, so export can undo it."""
    o = ring(n)
    if REVERSE_RING:
        o = tuple(reversed(o))
    if flipped:
        o = tuple(reversed(o))
    return o


def _newell(p):
    """Plain Newell over a corner ring — NOT Blender's `polygon.normal`, which
    is robust to self-intersection and therefore cannot see a bowtie at all."""
    g = [0.0, 0.0, 0.0]
    n = len(p)
    for i in range(n):
        a, b = p[i], p[(i + 1) % n]
        g[0] += (a[1] - b[1]) * (a[2] + b[2])
        g[1] += (a[2] - b[2]) * (a[0] + b[0])
        g[2] += (a[0] - b[0]) * (a[1] + b[1])
    return g


def _mag(v):
    return (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) ** 0.5


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _wound_against(p):
    """Does this polygon need decision 14's per-face flip?

    The dot is taken at WIND's own threshold, so the flip and the post-import
    winding bucket agree by construction.  Untextured polygons carry no
    authored normal and so have no second path: never flipped."""
    if p["kind"] not in TEXTURED_KINDS:
        return False
    n = len(p["positions"])
    r = ring(n)
    tp = [_fft_to_blender(tuple(p["positions"][k])) for k in r]
    tn = [_fft_to_blender(tuple(p["normals"][k])) for k in r]
    if REVERSE_RING:
        tp = tp[::-1]
        tn = tn[::-1]
    g = _newell(tp)
    acc = [sum(v[k] for v in tn) / len(tn) for k in range(3)]
    gm, am = _mag(g), _mag(acc)
    if gm < 1e-3 or am < 1e-6:
        return False
    return _dot(g, acc) / (gm * am) < -WIND


def _uv_enc(u, v, page):
    """Half-texel centred, v flipped; sheet 256 wide x 1024 tall, page picks
    the band (import-v1 §7)."""
    return ((u + 0.5) / 256.0, 1.0 - (page * 256 + v + 0.5) / 1024.0)


# ---------------------------------------------------------------------------
# Attribute names (schema-v1, import-v1 §7) and the shadow rule (§3).
# ---------------------------------------------------------------------------
FACE_INTS = ("visible_angles", "palette_id", "palette_byte_high_nibble",
             "texture_page", "unknown_texture_value_6a",
             "texture_byte6_high_nibble", "terrain_x", "terrain_z",
             "terrain_level", "unknown_untextured_0", "unknown_untextured_1",
             "unknown_untextured_2", "unknown_untextured_3")
CORNER_VECS = ("normals",)                       # positions get a shadow only
TILE_PAYLOAD_FIELDS = ("surface_type", "height", "depth", "slope_height",
                       "slope_type", "thickness", "shading", "rotation",
                       "unknown_1", "unknown_0a", "unknown_0b", "unknown_5a",
                       "unknown_5b", "unknown_5c", "unknown_6b", "unknown_6c",
                       "unknown_6d", "pass_through_only", "impassable",
                       "unselectable")


def _remove_collection(name):
    """Re-import deletes and rebuilds the document's collection (import-v1 §1).

    **It destroys what the ADDON made and spares what the ARTIST made**
    (decision 30).  Membership alone used to decide, and that was safe while
    nothing of the artist's lived in the collection.  Scoping the lamps into it
    changed that: a torch the artist added would be destroyed by the next
    re-import, so the lighting work would not survive one.  The rule is now the
    same one export already uses — an `exmateria_map/*` FLAG is what the addon
    owns.

    Survivors are handed back so the caller can re-link them into the rebuilt
    collection.  Re-homing them to the scene root instead would leave them
    OUTSIDE the map's collection, which is exactly where a lamp stops being in
    scope — a subtler way of losing the same work.
    """
    kept = []
    col = bpy.data.collections.get(name)
    if col is not None:
        for ob in list(col.objects):
            if any(k.startswith("exmateria_map/") for k in ob.keys()):
                bpy.data.objects.remove(ob, do_unlink=True)
            else:
                col.objects.unlink(ob)
                kept.append(ob)
        bpy.data.collections.remove(col)
    ob = bpy.data.objects.get(name)
    if ob is not None:
        bpy.data.objects.remove(ob, do_unlink=True)
    for m in list(bpy.data.meshes):
        if m.users == 0:
            bpy.data.meshes.remove(m)
    for m in list(bpy.data.materials):
        if m.users == 0:
            bpy.data.materials.remove(m)
    return kept


def _new_material(name, grey=0.5):
    """An unlit emission material — the preview bypasses Blender's lighting
    entirely (the #427 mechanism); S2 grows the preview node graph in place."""
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (200, 0)
    emit = nt.nodes.new("ShaderNodeEmission")
    emit.location = (0, 0)
    emit.inputs["Color"].default_value = (grey, grey, grey, 1.0)
    nt.links.new(emit.outputs["Emission"], out.inputs["Surface"])
    return mat


# ---------------------------------------------------------------------------
# §4 — textures and the preview graph.
#
# The addon never touches the ROM: sheets arrive as sidecar PNGs (the
# dump leg's 8-bit indexed files).  `png_indexed` decodes them with
# stdlib zlib; the raw 4-bit indices become one float index image per
# distinct sheet (exact integer reads), and each `map_states` entry's
# palettes become a 16x16 CLUT image (row = CLUT, 16 entries).
# ---------------------------------------------------------------------------


def _find_sidecar(name, doc_path):
    """Locate a sheet sidecar: next to the document first, then the assets
    roots (EXMATERIA_ASSETS_DIR, then the repository's project-assets)."""
    cands = [Path(doc_path).parent / name] if doc_path else []
    env = os.environ.get("EXMATERIA_ASSETS_DIR")
    if env:
        root = Path(env).expanduser()
        cands += [root / "MAP" / name, root / name]
    here = Path(__file__).resolve().parent
    up = here
    for _ in range(4):
        cand = up / "project-assets" / "fft-extract" / "MAP" / name
        cands.append(cand)
        if up.parent == up:
            break
        up = up.parent
    for c in cands:
        if c.is_file():
            return c
    return None


def _hex_rgb(s):
    s = s.lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def _persist(img):
    """Generated images live only in RAM: a saved `.blend` regenerates them
    BLANK on reload, and blank means index 0, which is `#000000` on every FFT
    CLUT — so every textured face reopens PURE BLACK while the untextured ones
    still shade.  Packing writes the buffer into the `.blend` itself.
    """
    img.update()
    try:
        img.pack()
    except RuntimeError as e:              # never fail an import over a preview
        print(f"EXMATERIA-MAP: warning: could not pack '{img.name}': {e}; "
              f"the preview will reload blank if this .blend is saved")


def _index_image(name, indices, w, h):
    """256x1024 float index image; R holds the exact 0..15 index per pixel.

    PNG row 0 is the TOP scanline; Blender's pixel row 0 is the BOTTOM, so
    rows are flipped on the way in.  The v band of a `texture_page` p is
    p*256..p*256+255 of the sheet, matching `_uv_enc`'s 1 - (..)/1024.
    """
    img = bpy.data.images.new(name, 256, 1024, alpha=False, float_buffer=True)
    img.colorspace_settings.name = "Non-Color"   # R holds a 0..15 INDEX, not colour
    px = [0.0] * (w * h * 4)
    for y in range(h):
        dst_row = (h - 1 - y) * w
        for x in range(w):
            j = (dst_row + x) * 4
            px[j] = float(indices[y * w + x])
            px[j + 3] = 1.0
    img.pixels[:] = px
    _persist(img)
    return img


def _clut_image(name, rows):
    """16x16 CLUT image: pixel (col, row) = CLUT `row`'s entry `col`."""
    img = bpy.data.images.new(name, 16, 16, alpha=False, float_buffer=True)
    # Non-Color: the entries are PSX BYTE-space values and the chain multiplies
    # in byte space (#427).  The sRGB decode happens once, after the multiply.
    img.colorspace_settings.name = "Non-Color"
    px = [0.0] * (16 * 16 * 4)
    for row in range(16):
        for col in range(16):
            j = (row * 16 + col) * 4
            r, g, b = rows[row][col]
            px[j], px[j + 1], px[j + 2], px[j + 3] = r / 255.0, g / 255.0, b / 255.0, 1.0
    img.pixels[:] = px
    _persist(img)
    return img


def _state_clut_rows(st, plte_rows):
    """The 16 CLUT rows of a state: the document's palettes when it has
    them, else the sidecar's display-only PLTE repeated (the §4 edge:
    untrusted-colour preview, panel says 'no CLUT in this state')."""
    if st.get("palettes"):
        rows = []
        for row in range(16):
            ent = st["palettes"][row] if row < len(st["palettes"]) else None
            cols = [_hex_rgb(c) for c in ent["colors"]] if ent else []
            rows.append(cols + [(0, 0, 0)] * (16 - len(cols)))
        return rows
    base = plte_rows[:16] + [(0, 0, 0)] * (16 - len(plte_rows))
    return [list(base) for _ in range(16)]


def _build_textures(doc, bname, doc_path):
    """The §4 images + wiring info.

    Returns (sheet_images {sheet: image name or None}, clut_names [per state],
    state_sheets [per state], default_state_index, default_index_image_name).
    A missing sidecar degrades to a black image + a warning, never a refusal.
    """
    states = doc["map_states"]
    sheets = []
    for st in states:
        s = st.get("texture_sheet")
        if s and s not in sheets:
            sheets.append(s)
    sheet_images = {}
    plte_by_sheet = {}
    for s in sheets:
        path = _find_sidecar(s, doc_path)
        img_name = f"exmateria_map/{s}_index"
        if path is None:
            sheet_images[s] = None
            print(f"EXMATERIA-MAP: warning: sidecar '{s}' not found; "
                  f"preview renders black for that sheet")
            continue
        try:
            w, h, indices, palette, _alpha = png_indexed.read_indexed_png(
                path.read_bytes())
            if w != 256 or h != 1024:
                raise ValueError(f"{s}: {w}x{h}, expected 256x1024")
            img = bpy.data.images.get(img_name)
            if img is not None:
                bpy.data.images.remove(img)
            img = _index_image(img_name, indices, w, h)
            plte_by_sheet[s] = palette[:16] + [(0, 0, 0)] * (16 - len(palette))
        except Exception as e:
            sheet_images[s] = None
            print(f"EXMATERIA-MAP: warning: could not decode sidecar '{s}': {e}; "
                  f"preview renders black for that sheet")
            continue
        sheet_images[s] = img_name
    # display-only PLTE read for the `palettes: null` edge (§4): the state's
    # own sheet when it has one, else the default sheet's
    clut_names, state_sheets = [], []
    for i, st in enumerate(states):
        s = st.get("texture_sheet")
        state_sheets.append(s)
        if not st.get("palettes"):
            plte = plte_by_sheet.get(s)
            if plte is None and sheets:
                plte = plte_by_sheet.get(sheets[0])
            plte = plte or [(0, 0, 0)] * 16
        else:
            plte = None
        name = f"exmateria_map/{bname}_clut_{i}"
        old = bpy.data.images.get(name)
        if old is not None:
            bpy.data.images.remove(old)
        img = _clut_image(name, _state_clut_rows(st, plte))
        clut_names.append(img.name)
    # default state: the entry whose resource IS the geometry source (§4)
    default = 0
    for i, st in enumerate(states):
        if st.get("resource") == doc["base"]["geometry_source"]:
            default = i
            break
    default_index = None
    for s in sheets:  # first decodable sheet supplies the default index image
        if sheet_images.get(s):
            default_index = sheet_images[s]
            break
    return sheet_images, clut_names, state_sheets, default, default_index


def state_rig(states, i):
    """The rig the preview lights state `i` with, and where it came from.

    Decision 7's rig-absent rule: a state carrying no 45-byte rig borrows from
    a same-arrangement sibling, and the borrow is named in the panel, never
    silent.  Decision 7 never said WHICH sibling, and taking the first one in
    the document — what this did until decision 25 — is wrong on most of the
    cases it fires on.

    The borrow is not an edge case.  Over the corpus (148 documents, 1,795 map
    states) **38.5% of states carry no rig of their own**; decision 7's "8 of
    169" counts GEOMETRY-BEARING RESOURCES, while the panel's state list is
    every row, and 636 of the 691 rig-less states are `texture` rows and 42
    `pad`.  Against the first-in-document rig, the paired rig differs for
    **76.8%** of them, and differs in AMBIENT ALONE — a flat shift across the
    whole mesh — for **66.4%**.  MAP001.a0 is the clean case: texture states
    11/13/15/17 all rendered the Initial's `[60, 60, 52]` while their partners
    carry `[60, 76, 84]`, `[64, 76, 80]`, `[68, 76, 76]`, `[70, 76, 74]` — a
    dusk ramp the preview flattened to noon.

    The rule is a KEY MATCH, not a position.  96.7% of rig-less states sit
    immediately before a rig-bearing one, which invites `i + 1`, but the
    position is a shadow of the real pairing: **99.4%** of rig-less states have
    a rig-bearing state sharing their `(night, weather)`, **95.1%** of those
    matches are unique, and the positional nearest is a member of the matching
    set **99.4%** of the time.  So the key match selects, nearest tie-breaks
    the 4.9% that are not unique (preferring the LATER state, since 96.7% of
    partners sit at +1), and 4 states corpus-wide have no keyed partner at all
    and fall back to nearest among all bearers.

    Returns `(rig, source_resource)`, with `rig` None when nothing in the
    arrangement carries one — 46 states corpus-wide, which render albedo.
    """
    if not (0 <= i < len(states)):
        return None, None
    own = state_own_rig(states[i])
    if own:
        return own, states[i].get("resource")
    bearers = [j for j, st in enumerate(states) if state_own_rig(st)]
    if not bearers:
        return None, None
    key = (states[i].get("night"), states[i].get("weather"))
    keyed = [j for j in bearers
             if (states[j].get("night"), states[j].get("weather")) == key]
    # `-(j - i)` breaks a distance tie toward the LATER state.
    j = min(keyed or bearers, key=lambda j: (abs(j - i), -(j - i)))
    return state_own_rig(states[j]), states[j].get("resource")


def state_own_rig(state):
    """The rig THIS state renders with, authored first (decision 27).

    `authored_light_rig` is what `build` will write to the resource, so it is
    what the state's picture will actually be on disc; `light_rig` beside it is
    the ROM's, kept derived and information-bearing (schema §7.1).  Previewing
    the ROM's while exporting the artist's would put a lie on screen -- the
    exact failure decision 25's provenance line exists to prevent -- and it
    makes a borrow source honest too: a sibling lends what it will SHIP.
    """
    return state.get(AUTHORED_RIG) or state.get("light_rig")


def bake_light(me, rig):
    """Bake decision 7's DIFFUSE term into the CORNER `diffuse` attribute.

    The full term is `ambient + sum over the 3 lights of gain_i * max(0, N.L_i)`,
    but only the sum is baked here.  `ambient` is a per-state CONSTANT (`[u8 x 3]`,
    one triple for the whole mesh), so it lives in the graph as an RGB node that
    `set_ambient` rewrites per state.  That mirrors BOTH references: the PSX GTE
    holds ambient in a REGISTER and Gouraud-interpolates only the per-vertex
    output, and Godot declares `ambient_light` a `uniform` against a `v_diffuse`
    `varying`.  A second corner attribute would be a third structure matching
    neither, and redundant besides — linear interpolation commutes with adding a
    constant, so `interp(a + d) == a + interp(d)` and this split is
    PIXEL-IDENTICAL to the summed bake it replaces.  `tests/blender_light_debug.py`
    asserts that identity by render, and seeds a clamp on the sum node to prove
    the assertion can fail.

    The sum is baked PER CORNER because that is the sample rate the PSX GTE
    runs at (#427): a material graph is evaluated per FRAGMENT, so it
    interpolates the normal and clamps after, which dissolves exactly the
    facet edges the artist is authoring normals against — 16.7% of covered
    pixels off by more than 8/255 on MAP062, 35.4% of the corpus's textured
    polygons.  Blender interpolates a CORNER `FLOAT_COLOR` linearly, which is
    Gouraud.  A `NORMALIZE` on the graph side is not the fix and was measured
    to make it worse.  The bake is 1.39 ms for the corpus's largest mesh, so
    it re-runs on every state change.

    Units, straight off the document's raw integers: `colors` is an i16 GTE
    gain (`/8`, then `/255` into the byte space the multiply happens in — it
    routinely exceeds 255, #358's max is 3,456 = 13.55x); `directions` is an
    unnormalised i16 triple, normalised here.  Both the corner normal and the
    direction are read in Blender space (the same proper rotation on each), so
    the dot needs no coordinate convention — #427 measured it identical to
    Godot's spherical round trip on 273,128 of 273,128 corners.

    A None rig bakes diffuse 0 and `rig_ambient` returns flat 1.0, so the sum is
    1.0: albedo only, decision 7's rig-absent rule, unchanged by the split.  A
    zero-length normal bakes diffuse 0, so that corner renders ambient alone —
    also unchanged.

    Reads the LIVE `normals`, not the `_shadow` twin.  The lighting bake writes
    solved normals into `normals` and the preview is its only readout, so baking
    against the import-time snapshot would show the artist the picture they had
    BEFORE they pressed the button.  The two are identical on every untouched
    import — `build` writes both from the same source — so this changes no
    existing scene; `normals_shadow` stays exactly as it was, because
    `export_document.divergence` needs it to answer "what has the artist
    changed since import".
    """
    attr = me.attributes.get("diffuse")
    if attr is None:
        attr = me.attributes.new("diffuse", "FLOAT_COLOR", "CORNER")
    if not rig:
        for d in attr.data:
            d.color = (0.0, 0.0, 0.0, 1.0)
        return attr
    gains = [[c / 8.0 / 255.0 for c in rig["colors"][i]] for i in range(3)]
    dirs = []
    for i in range(3):
        v = _fft_to_blender(tuple(float(c) for c in rig["directions"][i]))
        m = _mag(v) or 1.0
        dirs.append([c / m for c in v])
    nrm = me.attributes["normals"].data
    for li, d in enumerate(attr.data):
        n = nrm[li].vector
        m = _mag(n)
        if m < 1e-9:                       # 1,383 zero-length normals corpus-wide
            d.color = (0.0, 0.0, 0.0, 1.0)
            continue
        nn = (n[0] / m, n[1] / m, n[2] / m)
        acc = [0.0, 0.0, 0.0]
        for i in range(3):
            k = _dot(nn, dirs[i])
            if k > 0.0:
                for c in range(3):
                    acc[c] += gains[i][c] * k
        d.color = (acc[0], acc[1], acc[2], 1.0)
    return attr


def rig_ambient(rig):
    """The ambient triple the graph constant carries.

    Decision 7's rig-absent rule included: with no rig the diffuse bake is 0, so
    ambient 1.0 makes the sum 1.0 and the multiply passes the CLUT byte through
    unchanged — albedo only, which is what the panel then says.
    """
    if not rig:
        return (1.0, 1.0, 1.0)
    return tuple(c / 255.0 for c in rig["ambient"])


def set_ambient(mat, rig):
    """Rewrite the preview graph's ambient constant for a map state.

    The counterpart of `bake_light` on the graph side: the rig is PER MAP STATE,
    so both run together on import and on every state switch.
    """
    if mat is None or mat.node_tree is None:
        return None
    node = mat.node_tree.nodes.get("exmateria_map.ambient")
    if node is None:
        return None
    r, g, b = rig_ambient(rig)
    node.outputs[0].default_value = (r, g, b, 1.0)
    return node


# --- decision 25: the rig Override -------------------------------------------
# An Override is the artist's edited light rig, held against a map state.  It is
# NOT document data (it is not what the import read, and it must never reach
# `exmateria_map/map_states`, whose whole job is to carry the ROM's own shape so
# a future export leg is a straight serialisation) and it is NOT view state (the
# debug mode and the boost have no ROM representation; a rig plainly does).  The
# axis that separates the three is PROVENANCE — is this sourced from the
# imported document? — and an Override is the one that is ROM-shaped without
# being document-sourced.  Resolution order is override -> own -> keyed partner.

# A gain is `raw / 8 / 255`, so the artist's float times this is the i16 on disc.
GAIN_SCALE = 8.0 * 255.0          # 2040
# The i16 ceiling in gain units.  The corpus maximum is 13.55x (raw 27,648);
# this is the format's, so an artist is bounded by the file and not by taste.
GAIN_MAX = 32767.0 / GAIN_SCALE   # 16.062...
DIR_SCALE = 4096.0                # the disc's fixed-point unit


def _unit(v):
    m = _mag(v)
    return tuple(c / m for c in v) if m > 1e-9 else (0.0, 0.0, 0.0)


def _override_update(self, context):
    """Re-bake the object this Override belongs to.

    A PropertyGroup does not know its owner, so the object is found by identity
    over the objects that carry one.  `bake_light` is 1.39 ms on the corpus's
    largest mesh, which is why a slider can drive it directly.
    """
    for ob in bpy.data.objects:
        if any(o == self for o in getattr(ob, "exmateria_map_rig_overrides", ())):
            apply_state_light(ob)
            return


class MAP_PG_rig_override(PropertyGroup):
    """One map state's edited rig, in the shape the ROM uses.

    The three editing surfaces are deliberately NOT uniform, because the three
    data are not: ambient is a colour and fits 0-1 (the corpus maximum is 160 of
    255, 0.627); a gain reaches 13.55x and so CANNOT be a colour widget, which
    Blender hard-clamps to 0-1 — that is the FEDS picker's defect, where 97.5%
    of emitters had no slider that could hold 255; and a direction is a unit
    vector whose length carries nothing, authored in BLENDER space because that
    is the frame the artist is looking at.  The references serialise
    (elevation, azimuth) instead, but that pair is a trap here:
    `spherical_to_cartesian . vector_to_sphere` is measured NOT to be the
    identity — it returns (-x, y, -z) — and is only correct as a whole pipeline
    including the exporter's own pre-negation.
    """
    state_index: IntProperty(name="Map state", default=-1)
    ambient: FloatVectorProperty(
        name="Ambient", subtype="COLOR", size=3, min=0.0, max=1.0,
        default=(0.0, 0.0, 0.0), update=_override_update,
        description="The per-state constant added to diffuse (u8 on disc)")
    gain_1: FloatVectorProperty(
        name="Gain 1", size=3, min=0.0, max=GAIN_MAX, soft_max=4.0,
        default=(0.0, 0.0, 0.0), update=_override_update,
        description="Light 1's GTE coefficient — not a colour; reaches 13.55x")
    gain_2: FloatVectorProperty(
        name="Gain 2", size=3, min=0.0, max=GAIN_MAX, soft_max=4.0,
        default=(0.0, 0.0, 0.0), update=_override_update,
        description="Light 2's GTE coefficient — not a colour; reaches 13.55x")
    gain_3: FloatVectorProperty(
        name="Gain 3", size=3, min=0.0, max=GAIN_MAX, soft_max=4.0,
        default=(0.0, 0.0, 0.0), update=_override_update,
        description="Light 3's GTE coefficient — not a colour; reaches 13.55x")
    dir_1: FloatVectorProperty(
        name="Direction 1", subtype="XYZ", size=3, default=(0.0, 0.0, 1.0),
        update=_override_update, description="Blender space; length is ignored")
    dir_2: FloatVectorProperty(
        name="Direction 2", subtype="XYZ", size=3, default=(0.0, 0.0, 1.0),
        update=_override_update, description="Blender space; length is ignored")
    dir_3: FloatVectorProperty(
        name="Direction 3", subtype="XYZ", size=3, default=(0.0, 0.0, 1.0),
        update=_override_update, description="Blender space; length is ignored")
    # Carried, never edited: the game draws it as a SCREEN BACKDROP
    # (MapComposer._apply_gradient_from_manifest -> ScreenEffectOverlay), which
    # is not shading and so not a parity target.  Kept so the Override is the
    # whole 45 bytes and a future save-back is a straight serialisation.
    gradient: IntVectorProperty(name="Gradient", size=6, default=(0,) * 6)

    GAINS = ("gain_1", "gain_2", "gain_3")
    DIRS = ("dir_1", "dir_2", "dir_3")


def override_rig(ov):
    """The Override as a rig dict — the same shape `light_rig` carries.

    Byte-exact for ambient and the gains (both are an integer scaled by a
    constant and back).  NOT byte-exact for a direction: the disc's magnitudes
    run 4094.4-4096.7 and this re-emits at exactly 4096, so a round trip can
    move an i16 by a couple of LSB.  That is not a defect and "exact" is the
    wrong bar for it — every consumer normalises, so the measured cost to the
    PICTURE is 0.0099 degrees worst case over the corpus, a 0.000/255 shading
    delta.  `blender_roundtrip.py` asserts the picture, not the bytes.
    """
    return {
        "colors": [[int(round(c * GAIN_SCALE)) for c in getattr(ov, g)]
                   for g in MAP_PG_rig_override.GAINS],
        "directions": [[int(round(c * DIR_SCALE))
                        for c in _blender_to_fft(_unit(getattr(ov, d)))]
                       for d in MAP_PG_rig_override.DIRS],
        "ambient": [int(round(c * 255.0)) for c in ov.ambient],
        "gradient": list(ov.gradient),
    }


def seed_override(ov, rig, index):
    """Fill an Override from a rig, converting into the editing units."""
    ov.state_index = index
    ov.ambient = tuple(c / 255.0 for c in rig["ambient"])
    for k, g in enumerate(MAP_PG_rig_override.GAINS):
        setattr(ov, g, tuple(min(GAIN_MAX, max(0.0, c / GAIN_SCALE))
                             for c in rig["colors"][k]))
    for k, d in enumerate(MAP_PG_rig_override.DIRS):
        setattr(ov, d, _unit(_fft_to_blender(
            tuple(float(c) for c in rig["directions"][k]))))
    ov.gradient = tuple(rig.get("gradient") or (0,) * 6)


def find_override(ob, i):
    """The Override held against state `i`, or None."""
    for ov in getattr(ob, "exmateria_map_rig_overrides", ()):
        if ov.state_index == i:
            return ov
    return None


def resolved_rig(ob, states, i):
    """The rig the preview lights state `i` with, and where it came from.

    Decision 25's resolution order: **override -> own -> keyed partner**.
    Returns `(rig, source_label, is_override)` so the panel can say which one
    won — an edited picture that reads as the ROM's is the failure mode this
    whole surface exists to prevent.
    """
    ov = find_override(ob, i)
    if ov is not None:
        return override_rig(ov), f"EDITED (state {i})", True
    rig, src = state_rig(states, i)
    return rig, src, False


def object_states(ob):
    """The object's document map states, or []."""
    try:
        return json.loads(ob["exmateria_map/map_states"])
    except (KeyError, ValueError, TypeError):
        return []


def apply_state_light(ob):
    """Re-bake the current state's light from the RESOLVED rig.

    The one path both a state switch and an Override edit run through, so the
    two cannot drift apart.
    """
    states = object_states(ob)
    if not states or "exmateria_map/preview_state" not in ob:
        return None
    i = int(ob["exmateria_map/preview_state"])
    rig, src, edited = resolved_rig(ob, states, i)
    mat = preview_material(ob)
    bake_light(ob.data, rig)
    set_ambient(mat, rig)
    ob["exmateria_map/light_source"] = src or ""
    apply_light_debug(ob)
    _tag_redraw()
    return rig


# Godot's `map_light_debug` (indexed_color.gdshader:52), enumerated for the
# N-panel.  Mode 0 is the shipped preview; the rest isolate one stage each.
DEBUG_MODES = (
    ("0", "Normal", "albedo x (ambient + diffuse) — the shipped preview"),
    ("1", "Normals as colour", "0.5 + 0.5*n, in the RAW FFT triple so the "
                               "colours match a Godot capture"),
    ("2", "Lighting only", "ambient + diffuse against a white albedo"),
    ("3", "Ambient only", "the state's ambient constant alone"),
    ("4", "Diffuse only", "the baked directional sum alone"),
    ("5", "Albedo only", "the CLUT colour, no lighting"),
)

# Which node feeds the sRGB decode in each mode.
_DEBUG_SOURCE = {
    0: "exmateria_map.multiply",
    1: "exmateria_map.normal_encode",
    2: "exmateria_map.light_sum",
    3: "exmateria_map.ambient",
    4: "exmateria_map.diffuse",
    5: "exmateria_map.clut",
}
# Godot exaggerates 2/3/4 by `map_light_boost` and then clamps; 0/1/5 ignore it.
_DEBUG_BOOSTED = (2, 3, 4)


def _out(node):
    """The node's colour-ish output.  RNA hands out a fresh wrapper per access,
    so everything here addresses nodes and sockets by NAME, never by identity."""
    for key in ("Color", "Vector", "Result"):
        if key in node.outputs:
            return node.outputs[key]
    return node.outputs[0]


def set_light_debug(mat, mode=0, boost=1.0):
    """Rewire which stage feeds the sRGB decode — Godot's `map_light_debug`.

    Godot overrides `final` in the debug branch and then runs the SAME
    `pow(final, 2.2)` output conversion on it, so every mode here routes through
    the one decode group and only its INPUT changes.  That is what makes this a
    rewire rather than six materials.

    Mode 1 encodes the RAW FFT triple, not Blender's stored vector: the addon
    holds normals rotated by decision 14's `(x, y, z) -> (x, z, -y)`, while
    Godot's map conversion is the identity permutation, so an unswizzled encode
    would agree on red and disagree on green and blue.  A zero-length normal
    encodes to neutral grey here; Godot's `normalize(vec3(0))` is undefined, so
    the preview is defined where the game is not.
    """
    if mat is None or mat.node_tree is None:
        return None
    nt = mat.node_tree
    dec = nt.nodes.get("exmateria_map.srgb_decode")
    boost_node = nt.nodes.get("exmateria_map.boost")
    mode = int(mode)
    src = nt.nodes.get(_DEBUG_SOURCE.get(mode, _DEBUG_SOURCE[0]))
    if dec is None or boost_node is None or src is None:
        return None
    for link in list(nt.links):
        if link.to_node.name == dec.name:
            nt.links.remove(link)
        elif (link.to_node.name == boost_node.name
              and link.to_socket.name == "Color1"):
            nt.links.remove(link)
    b = float(boost)
    boost_node.inputs["Color2"].default_value = (b, b, b, 1.0)
    if mode in _DEBUG_BOOSTED:
        nt.links.new(_out(src), boost_node.inputs["Color1"])
        nt.links.new(_out(boost_node), dec.inputs["Color"])
    else:
        nt.links.new(_out(src), dec.inputs["Color"])
    return src


def _srgb_decode_group():
    """Exact sRGB byte-space -> linear, per channel (#427).

    The PSX multiplies the 8-bit CLUT value by the light term, so the whole
    chain multiplies in BYTE space and converts only at the very end — the
    display's sRGB encode then hands the byte back unchanged.  Multiplying in
    linear space instead would darken every lit texel by the gamma.
    """
    name = "exmateria_map_srgb_to_linear"
    g = bpy.data.node_groups.get(name)
    if g is not None:
        return g
    g = bpy.data.node_groups.new(name, "ShaderNodeTree")
    g.interface.new_socket("Color", in_out="INPUT", socket_type="NodeSocketColor")
    g.interface.new_socket("Color", in_out="OUTPUT", socket_type="NodeSocketColor")
    gin = g.nodes.new("NodeGroupInput")
    gin.location = (-400, 0)
    gout = g.nodes.new("NodeGroupOutput")
    gout.location = (600, 0)
    sep = g.nodes.new("ShaderNodeSeparateColor")
    sep.location = (-200, 0)
    com = g.nodes.new("ShaderNodeCombineColor")
    com.location = (400, 0)
    g.links.new(gin.outputs["Color"], sep.inputs["Color"])
    for ch in range(3):
        lo = g.nodes.new("ShaderNodeMath")
        lo.operation = "DIVIDE"
        lo.inputs[1].default_value = 12.92
        add = g.nodes.new("ShaderNodeMath")
        add.operation = "ADD"
        add.inputs[1].default_value = 0.055
        div = g.nodes.new("ShaderNodeMath")
        div.operation = "DIVIDE"
        div.inputs[1].default_value = 1.055
        pw = g.nodes.new("ShaderNodeMath")
        pw.operation = "POWER"
        pw.inputs[1].default_value = 2.4
        gt = g.nodes.new("ShaderNodeMath")
        gt.operation = "GREATER_THAN"
        gt.inputs[1].default_value = 0.04045
        mix = g.nodes.new("ShaderNodeMix")
        mix.data_type = "FLOAT"
        for k, node in enumerate((lo, add, div, pw, gt, mix)):
            node.location = (k * 100 - 100, -ch * 260 - 40)
        g.links.new(sep.outputs[ch], lo.inputs[0])
        g.links.new(sep.outputs[ch], add.inputs[0])
        g.links.new(add.outputs[0], div.inputs[0])
        g.links.new(div.outputs[0], pw.inputs[0])
        g.links.new(sep.outputs[ch], gt.inputs[0])
        g.links.new(gt.outputs[0], mix.inputs["Factor"])
        g.links.new(lo.outputs[0], mix.inputs["A"])
        g.links.new(pw.outputs[0], mix.inputs["B"])
        g.links.new(mix.outputs["Result"], com.inputs[ch])
    g.links.new(com.outputs["Color"], gout.inputs["Color"])
    return g


def _preview_material(name, index_image, clut_image):
    """The §4 graph: UV -> index image (exact) -> CLUT row(palette_id) ->
    colour x (ambient + corner diffuse) -> clamp -> sRGB decode -> emission.

    Node names are stable so the state selector can rewire the two image nodes
    and the ambient constant in place, and so `set_light_debug` can rewire which
    stage feeds the decode.  The debug stages (the normal encoder and the boost
    multiply) are built unconditionally and left unlinked in mode 0 — an unlinked
    branch costs nothing to evaluate, and building them lazily would mean the
    graph's shape depended on the view state."""
    mat = bpy.data.materials.get(name)
    if mat is not None:
        bpy.data.materials.remove(mat)
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (960, 0)
    emit = nt.nodes.new("ShaderNodeEmission")
    emit.location = (760, 0)
    dec = nt.nodes.new("ShaderNodeGroup")
    dec.node_tree = _srgb_decode_group()
    dec.name = "exmateria_map.srgb_decode"
    dec.location = (660, 0)
    mix = nt.nodes.new("ShaderNodeMixRGB")
    mix.blend_type = "MULTIPLY"
    mix.name = "exmateria_map.multiply"
    mix.location = (560, 0)
    mix.inputs["Fac"].default_value = 1.0
    # the PSX saturates the FINAL pixel; #358's max gain is an ordinary 13.55x
    # multiply, so without this a lit texel runs past 1.0 unclamped
    mix.use_clamp = True
    # ambient + diffuse.  MUST stay unclamped: the sum routinely exceeds 1.0
    # (#358's max gain is 13.55x) and the PSX saturates only the FINAL pixel,
    # which is `mix` above.  Clamping here would darken every overbright texel
    # and is the defect tests/blender_light_debug.py seeds.
    lsum = nt.nodes.new("ShaderNodeMixRGB")
    lsum.blend_type = "ADD"
    lsum.name = "exmateria_map.light_sum"
    lsum.location = (360, -60)
    lsum.inputs["Fac"].default_value = 1.0
    lsum.use_clamp = False
    # ambient is a per-state CONSTANT (`[u8 x 3]`), so it is a graph value, not
    # a corner attribute — Godot's `ambient_light` uniform, the GTE's register.
    amb = nt.nodes.new("ShaderNodeRGB")
    amb.name = "exmateria_map.ambient"
    amb.location = (160, -60)
    amb.outputs[0].default_value = (1.0, 1.0, 1.0, 1.0)
    light = nt.nodes.new("ShaderNodeAttribute")
    light.attribute_name = "diffuse"
    light.name = "exmateria_map.diffuse"
    light.location = (160, -260)
    clut = nt.nodes.new("ShaderNodeTexImage")
    clut.image = clut_image
    clut.interpolation = "Closest"
    clut.name = "exmateria_map.clut"
    clut.location = (360, 100)
    comb = nt.nodes.new("ShaderNodeCombineXYZ")
    comb.name = "exmateria_map.clut_uv"
    comb.location = (160, 160)
    addx = nt.nodes.new("ShaderNodeMath")
    addx.operation = "ADD"
    addx.inputs[1].default_value = 0.5
    addx.name = "exmateria_map.idx_half"
    addx.location = (0, 220)
    divx = nt.nodes.new("ShaderNodeMath")
    divx.operation = "DIVIDE"
    divx.inputs[1].default_value = 16.0
    divx.name = "exmateria_map.idx_norm"
    divx.location = (120, 220)
    addy = nt.nodes.new("ShaderNodeMath")
    addy.operation = "ADD"
    addy.inputs[1].default_value = 0.5
    addy.name = "exmateria_map.pid_half"
    addy.location = (0, 80)
    divy = nt.nodes.new("ShaderNodeMath")
    divy.operation = "DIVIDE"
    divy.inputs[1].default_value = 16.0
    divy.name = "exmateria_map.pid_norm"
    divy.location = (120, 80)
    pal = nt.nodes.new("ShaderNodeAttribute")
    pal.attribute_name = "palette_id"
    pal.name = "exmateria_map.palette"
    pal.location = (-160, 80)
    sep = nt.nodes.new("ShaderNodeSeparateColor")
    sep.name = "exmateria_map.index_split"
    sep.location = (0, 300)
    idx = nt.nodes.new("ShaderNodeTexImage")
    idx.image = index_image
    idx.interpolation = "Closest"
    idx.name = "exmateria_map.index"
    idx.location = (-360, 300)
    uv = nt.nodes.new("ShaderNodeUVMap")
    uv.uv_map = "UVMap"
    uv.name = "exmateria_map.uv"
    uv.location = (-560, 300)
    # ---- debug stages (unlinked in mode 0; see set_light_debug) ----
    # `map_light_boost`: Godot exaggerates modes 2/3/4 and THEN clamps.
    boost = nt.nodes.new("ShaderNodeMixRGB")
    boost.blend_type = "MULTIPLY"
    boost.name = "exmateria_map.boost"
    boost.location = (560, -420)
    boost.inputs["Fac"].default_value = 1.0
    boost.inputs["Color2"].default_value = (1.0, 1.0, 1.0, 1.0)
    boost.use_clamp = True
    # normals-as-colour, in the RAW FFT triple: normalise in Blender space (the
    # rotation is proper, so the magnitude is invariant and the order does not
    # matter), then apply `_blender_to_fft` (x, y, z) -> (x, -z, y) as a
    # separate/negate/combine before the 0.5 + 0.5*n encode.
    nattr = nt.nodes.new("ShaderNodeAttribute")
    # Live `normals`, matching the bake above: after a lighting bake mode 1 must
    # paint the SOLVED normals, since those are the ones that reach the disc.
    nattr.attribute_name = "normals"
    nattr.name = "exmateria_map.normal"
    nattr.location = (-560, -420)
    nunit = nt.nodes.new("ShaderNodeVectorMath")
    nunit.operation = "NORMALIZE"
    nunit.name = "exmateria_map.normal_unit"
    nunit.location = (-360, -420)
    nsep = nt.nodes.new("ShaderNodeSeparateXYZ")
    nsep.name = "exmateria_map.normal_split"
    nsep.location = (-180, -420)
    nnegz = nt.nodes.new("ShaderNodeMath")
    nnegz.operation = "MULTIPLY"
    nnegz.inputs[1].default_value = -1.0
    nnegz.name = "exmateria_map.normal_negz"
    nnegz.location = (0, -520)
    ncomb = nt.nodes.new("ShaderNodeCombineXYZ")
    ncomb.name = "exmateria_map.normal_fft"
    ncomb.location = (180, -420)
    nenc = nt.nodes.new("ShaderNodeVectorMath")
    nenc.operation = "MULTIPLY_ADD"
    nenc.name = "exmateria_map.normal_encode"
    nenc.location = (360, -420)
    nenc.inputs[1].default_value = (0.5, 0.5, 0.5)
    nenc.inputs[2].default_value = (0.5, 0.5, 0.5)
    L = nt.links.new
    L(nattr.outputs["Vector"], nunit.inputs[0])
    L(nunit.outputs["Vector"], nsep.inputs["Vector"])
    L(nsep.outputs["Z"], nnegz.inputs[0])
    L(nsep.outputs["X"], ncomb.inputs["X"])
    L(nnegz.outputs[0], ncomb.inputs["Y"])
    L(nsep.outputs["Y"], ncomb.inputs["Z"])
    L(ncomb.outputs["Vector"], nenc.inputs[0])
    L(uv.outputs["UV"], idx.inputs["Vector"])
    L(idx.outputs["Color"], sep.inputs["Color"])
    L(sep.outputs["Red"], addx.inputs[0])
    L(addx.outputs[0], divx.inputs[0])
    L(divx.outputs[0], comb.inputs["X"])
    L(pal.outputs["Factor"], addy.inputs[0])
    L(addy.outputs[0], divy.inputs[0])
    L(divy.outputs[0], comb.inputs["Y"])
    L(comb.outputs["Vector"], clut.inputs["Vector"])
    L(clut.outputs["Color"], mix.inputs["Color1"])
    L(amb.outputs[0], lsum.inputs["Color1"])
    L(light.outputs["Color"], lsum.inputs["Color2"])
    L(lsum.outputs["Color"], mix.inputs["Color2"])
    L(dec.outputs["Color"], emit.inputs["Color"])
    L(emit.outputs["Emission"], out.inputs["Surface"])
    set_light_debug(mat, 0, 1.0)          # links multiply -> decode
    return mat


def _plain_quad_mesh(name, corners):
    me = bpy.data.meshes.new(name)
    me.from_pydata([tuple(float(c) for c in v) for v in corners], [], ((0, 1, 2, 3),))
    me.update()
    return me


def build_grid(base, collection):
    """The grid footprint object (import-v1 §5): single quad, extent only, WIRE."""
    tg = base.get("terrain_grid")
    if not tg:
        return None
    sx, sz = tg["size_x"], tg["size_z"]
    me = _plain_quad_mesh(
        f"grid {sx}x{sz}",
        [(0, 0, 0), (sx * TILE_UNITS, 0, 0),
         (sx * TILE_UNITS, sz * TILE_UNITS, 0), (0, sz * TILE_UNITS, 0)])
    ob = bpy.data.objects.new(f"{base['map']}.a{base['arrangement']}_grid", me)
    collection.objects.link(ob)
    ob.display_type = "WIRE"
    ob["exmateria_map/grid"] = True     # export finds it by FLAG, not by name
    ob["size_x"] = sx
    ob["size_x_shadow"] = sx
    ob["size_z"] = sz
    ob["size_z_shadow"] = sz
    # Decision 16's typed fields are REGISTERED properties (they need an update
    # callback to clamp); `size_x`/`size_z` stay ID properties because they are
    # document data in the ROM's own shape, and that is what export reads.  The
    # busy flag keeps seeding them here from firing the clamp against a shadow
    # that is only half written.
    ob["_extent_busy"] = True
    try:
        ob.exmateria_map_size_x = sx
        ob.exmateria_map_size_z = sz
    except (AttributeError, TypeError):     # module imported without register()
        pass
    finally:
        del ob["_extent_busy"]
    return ob


def build_tile(record, collection):
    """One 28x28 plane per level-0 record (import-v1 §5).

    Record fields are schema-v1 custom properties, written ONLY for declared
    fields (an absent field is not zero) plus `x`/`z`/`level` — each with a
    `<field>_shadow` twin.  The record is authoritative: Z is
    height * HEIGHT_STEP, locked."""
    height = record.get("height", 0)
    x, z, level = record["x"], record["z"], record["level"]
    cx, cz = x * TILE_UNITS, z * TILE_UNITS
    h = height * HEIGHT_STEP
    me = _plain_quad_mesh(
        f"tile {x} {z}",
        [(cx, cz, h), (cx + TILE_UNITS, cz, h),
         (cx + TILE_UNITS, cz + TILE_UNITS, h), (cx, cz + TILE_UNITS, h)])
    ob = bpy.data.objects.new(f"tile_{x}_{z}_L{level}", me)
    collection.objects.link(ob)
    # The flag export reads, and its KIND: a tile the document named is neither
    # a drift handle nor growth-created, so decision 23's declare-only-the-three
    # rule (export-v1 §5.1.3) does not reach it.
    ob["exmateria_map/tile"] = "imported"
    for key in ("x", "z", "level"):
        ob[key] = record[key]
        ob[f"{key}_shadow"] = record[key]
    # Export-v1 §1/§6.3: a payload field carries a DECLARED-FLAG twin beside
    # its value.  Presence alone cannot express the state growth and the drift
    # checker both need — a value SHOWN beside a field the record does not
    # declare (§7.2's seed, §6.3's base value) — and "an absent field is not
    # zero" (schema §7.2) makes the distinction load-bearing.
    for key in TILE_PAYLOAD_FIELDS:
        declared = key in record
        ob[f"{key}_declared"] = declared
        if declared:
            ob[key] = record[key]
            ob[f"{key}_shadow"] = record[key]
    ob.data.materials.append(_new_material(UNLIT_GREY))
    ob.data.polygons[0].material_index = 0
    return ob


def build(doc, context=None, doc_path=None):
    """Build the document into the scene.  Returns the mesh object."""
    base = doc["base"]
    name = f"{base['map']}.a{base['arrangement']}"
    # What the artist put in the old collection comes across into the new one.
    # Dropping it, or leaving it at the scene root, both lose the lighting work
    # a re-import is not supposed to touch (decision 30).
    kept = _remove_collection(name)
    col = bpy.data.collections.new(name)
    scene = context.scene if context is not None else bpy.context.scene
    scene.collection.children.link(col)
    for survivor in kept:
        col.objects.link(survivor)

    polys = doc["polygons"]
    flipped = [_wound_against(p) for p in polys]

    # Decision 14: doc corners are laid into Blender in `import_order` — the
    # ring reversed, reversed AGAIN for contrary-wound polygons.  from_pydata
    # generates the loops in the face tuple's own vertex order, so the loop
    # layout IS the import order and quads stay quads.  Vertices are welded by
    # exact position (import-v1 §1): the axis map is bijective on i16, so the
    # transformed coordinates are integer-exact and the weld is exact — the
    # baseline's `weld` group (73,485 of 281,096 corners, 0 unwelded) is its
    # assertion.
    verts, index, face_tuples, orders = [], {}, [], []
    for i, p in enumerate(polys):
        ps = [_fft_to_blender(tuple(c)) for c in p["positions"]]
        order = import_order(len(ps), flipped[i])
        orders.append(order)
        idx = []
        for k in order:
            v = ps[k]
            if v not in index:
                index[v] = len(verts)
                verts.append(v)
            idx.append(index[v])
        face_tuples.append(tuple(idx))

    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], face_tuples)
    me.update()

    # ---- corner attributes (CORNER domain == loop domain) ----
    pos_shadow = me.attributes.new("positions_shadow", "FLOAT_VECTOR", "CORNER")
    nrm = me.attributes.new("normals", "FLOAT_VECTOR", "CORNER")
    nrm_shadow = me.attributes.new("normals_shadow", "FLOAT_VECTOR", "CORNER")
    loop_base = 0
    for i, p in enumerate(polys):
        n = len(p["positions"])
        has_nrm = p["kind"] in TEXTURED_KINDS
        for slot, corner in enumerate(orders[i]):
            li = loop_base + slot
            fb = _fft_to_blender(tuple(p["positions"][corner]))
            pos_shadow.data[li].vector = (float(fb[0]), float(fb[1]), float(fb[2]))
            fb_n = _fft_to_blender(tuple(p["normals"][corner])) if has_nrm else (0.0, 0.0, 0.0)
            nrm.data[li].vector = (float(fb_n[0]), float(fb_n[1]), float(fb_n[2]))
            nrm_shadow.data[li].vector = (float(fb_n[0]), float(fb_n[1]), float(fb_n[2]))
        loop_base += n

    # ---- face attributes (schema-v1 names; every carried field shadowed, §3) ----
    face_attrs = {}
    for a in FACE_INTS:
        face_attrs[a] = me.attributes.new(a, "INT", "FACE")
        face_attrs[a + "_shadow"] = me.attributes.new(a + "_shadow", "INT", "FACE")
    textured_attr = me.attributes.new("textured", "BOOLEAN", "FACE")
    flip_attr = me.attributes.new("fft_ring_flipped", "BOOLEAN", "FACE")
    # Export-v1 §8's addon-internal trio.  None of the three enters the
    # document (§8.4), and each is written here so a face the ARTIST creates
    # is the one that reads the zero-filled default: Blender has no
    # per-attribute default, so `imported` carries §8's `authored` inverted
    # (see export_document's module docstring) and `walkable` and
    # `visible_angles_stamped` want False on a new face anyway.
    imported_attr = me.attributes.new("imported", "BOOLEAN", "FACE")
    walkable_attr = me.attributes.new("walkable", "BOOLEAN", "FACE")
    stamped_attr = me.attributes.new("visible_angles_stamped", "BOOLEAN", "FACE")
    for i, p in enumerate(polys):
        is_t = p["kind"] in TEXTURED_KINDS
        textured_attr.data[i].value = is_t
        flip_attr.data[i].value = flipped[i]
        imported_attr.data[i].value = True
        stamped_attr.data[i].value = True
        va = -1 if p.get("visible_angles") is None else p["visible_angles"]
        face_attrs["visible_angles"].data[i].value = va
        face_attrs["visible_angles_shadow"].data[i].value = va
        # `terrain` is a field of textured polygons only (schema §5.2); an
        # untextured face carries no binding, so its terrain attrs are zero,
        # like the rest of the textured-only attributes on that kind
        t = p.get("terrain") or {"x": 0, "z": 0, "level": 0}
        face_attrs["terrain_x"].data[i].value = t["x"]
        face_attrs["terrain_z"].data[i].value = t["z"]
        face_attrs["terrain_level"].data[i].value = t["level"]
        for k in ("terrain_x", "terrain_z", "terrain_level"):
            face_attrs[k + "_shadow"].data[i].value = face_attrs[k].data[i].value
        # §8: `walkable` initializes to `binding != FF FF sentinel`, textured
        # faces only -- an untextured polygon carries no binding at all.  It is
        # what shapes the exported binding, so the sentinel round-trips as the
        # sentinel and a live out-of-grid binding round-trips as itself.
        walkable_attr.data[i].value = bool(is_t and t != SENTINEL_BINDING)
        if is_t:
            for k in ("palette_id", "palette_byte_high_nibble", "texture_page",
                      "unknown_texture_value_6a", "texture_byte6_high_nibble"):
                    face_attrs[k].data[i].value = p[k]
                    face_attrs[k + "_shadow"].data[i].value = p[k]
        else:
            for j in range(4):
                a = f"unknown_untextured_{j}"
                face_attrs[a].data[i].value = p["unknown_untextured"][j]
                face_attrs[a + "_shadow"].data[i].value = p["unknown_untextured"][j]

    # ---- UV layer: one layer named UVMap, values on textured faces only (§7) ----
    uv = me.uv_layers.new(name="UVMap")
    loop_base = 0
    for i, p in enumerate(polys):
        n = len(p["positions"])
        if p["kind"] in TEXTURED_KINDS:
            for slot, corner in enumerate(orders[i]):
                u, v = p["uv"][corner]
                uv.data[loop_base + slot].uv = _uv_enc(u, v, p["texture_page"])
        loop_base += n

    # ---- textures + materials + per-face slot (§4) ----
    sheet_images, clut_names, state_sheets, default_state, default_index = \
        _build_textures(doc, name, doc_path)
    # Corner light (decision 7): `albedo x (ambient + sum(gain.max(0, N.L)))`,
    # the sum BAKED PER CORNER off the default state's `light_rig` (schema
    # §7.1).  A rig-less arrangement bakes flat 1.0 — albedo only.
    rig, rig_source = state_rig(doc["map_states"], default_state)
    bake_light(me, rig)
    if default_index is None:  # no decodable sidecar: the graph still stands
        default_index = "exmateria_map/black"
        if bpy.data.images.get(default_index) is None:
            _blk = bpy.data.images.new(default_index, 256, 1024, alpha=False,
                                       float_buffer=True)
            _blk.colorspace_settings.name = "Non-Color"
            _persist(_blk)
    grey = _new_material(UNLIT_GREY, grey=0.5)
    preview = _preview_material(f"{name}_preview",
                                bpy.data.images[default_index],
                                bpy.data.images[clut_names[default_state]])
    set_ambient(preview, rig)
    me.materials.clear()
    me.materials.append(grey)
    me.materials.append(preview)
    for i, p in enumerate(polys):
        me.polygons[i].material_index = 1 if p["kind"] in TEXTURED_KINDS else 0

    ob = bpy.data.objects.new(name, me)
    col.objects.link(ob)

    build_grid(base, col)
    recs = doc.get("terrain") or []          # schema v1: bare list or null
    for rec in recs:
        if rec.get("level", 0) == 0:
            build_tile(rec, col)

    # ---- marker JSON properties (§6) + §4 preview wiring ----
    for section in TOP_LEVEL:
        if section in ("format", "version"):
            continue
        ob[f"exmateria_map/{section}"] = json.dumps(doc[section])
    ob["exmateria_map/sheet_images"] = json.dumps(sheet_images)
    ob["exmateria_map/state_cluts"] = json.dumps(clut_names)
    ob["exmateria_map/state_sheets"] = json.dumps(state_sheets)
    ob["exmateria_map/preview_state"] = default_state
    ob["exmateria_map/light_source"] = rig_source or ""

    # §6: the drift checker owns the quads for the LIFETIME of the scene, and
    # that starts here.  An untouched document has no drift by construction
    # (decision 22), so this creates nothing on a clean import — it establishes
    # the count, and does it without depending on a handler having fired.
    from .authoring import sync_drift
    _n_drift, _n_fixed = sync_drift(ob)

    print(f"EXMATERIA-MAP: imported {name}: "
          f"{len(polys)} polygons, {len(recs)} terrain records, "
          f"{len(doc['map_states'])} states, "
          f"light rig {rig_source or 'ABSENT (albedo only)'}, "
          f"{_n_drift} drifted tile(s)")
    return ob


class MAP_AddonPreferences(AddonPreferences):
    """Where the artist last imported from, and last exported to.

    Preferences, not scene properties: the point is to survive a Blender
    restart, and a scene property dies with the file.

    This docstring used to say Blender "writes this out on quit whenever
    `Save & Load > Auto-Save Preferences` is on (the default)".  That is
    FALSE — measured across a real process boundary, a Python-assigned
    preference reads back `''` in a fresh Blender whether or not auto-save is
    on, and marking `preferences.is_dirty` does not help either.
    `remember_dir` saves explicitly; see its docstring.

    **Two fields, not one.**  Sharing a single memory means exporting into a
    build directory moves where the NEXT import opens — which presents as the
    import browser having forgotten, and is indistinguishable from the bug
    this pair of fields exists to fix.
    """
    bl_idname = __package__

    last_dir: StringProperty(
        name="Last document directory",
        description="The interchange directory the IMPORT browser opens in",
        subtype="DIR_PATH", default="")
    last_export_dir: StringProperty(
        name="Last export directory",
        description="The directory the EXPORT browser opens in",
        subtype="DIR_PATH", default="")
    # The live link's transport (`live_link_ui.py`).  Preferences and not a
    # scene property for the same reason as the two above: the emulator the
    # artist runs is a property of their machine, not of the map they opened.
    live_host: StringProperty(
        name="PCSX host",
        description="Host running PCSX-Redux's Lua web server",
        default=LIVE_DEFAULT_HOST)
    live_port: IntProperty(
        name="PCSX port",
        description="Port of PCSX-Redux's Lua web server "
                    "(-webserver -webserver-port N)",
        default=LIVE_DEFAULT_PORT, min=1, max=65535)

    def draw(self, context):
        self.layout.prop(self, "last_dir")
        self.layout.prop(self, "last_export_dir")
        row = self.layout.row(align=True)
        row.prop(self, "live_host")
        row.prop(self, "live_port")


def _prefs(context):
    """The addon's preferences, or None when the module was imported directly
    rather than enabled as an addon (the headless harnesses do exactly that)."""
    try:
        return context.preferences.addons[__package__].preferences
    except (AttributeError, KeyError):
        return None


def start_filepath(context):
    """Where the import file browser opens.

    The last directory imported from, when there still is one.  Setting this
    unconditionally from `scene.render.filepath` — which is `/tmp/` on a fresh
    start — is what made the artist re-navigate on every launch: Blender
    remembers the browser's last directory on its own, and an operator that
    assigns `filepath` in `invoke` overrides that memory every time.
    """
    prefs = _prefs(context)
    last = bpy.path.abspath(getattr(prefs, "last_dir", "") or "")
    if last and os.path.isdir(last):
        return os.path.join(last, "")          # a directory: no filename pinned
    return str(Path(context.scene.render.filepath).parent / "interchange.json")


def remember_dir(context, filepath, field="last_dir"):
    """Record the directory an import (or export) succeeded from — and PERSIST
    it, which setting the property does not do.

    Measured across a real process boundary: assigning the property reads back
    `''` in a fresh Blender, and so does assigning it plus
    `preferences.is_dirty = True`.  Only an explicit `wm.save_userpref()`
    survives.  The addon already knew this shape — enabling the addon
    persistently needs the same explicit save, `persistent=True` alone does not
    reach a fresh process — and the whole point of putting this on Preferences
    rather than the Scene was surviving a restart, so without the save the
    feature does nothing it was built for.

    Guarded on the user's own `use_preferences_save`: with it ON, Blender would
    write every preference on exit anyway, so saving here only makes it sooner
    and persists nothing extra; with it OFF the user has said not to persist
    preferences automatically, and an import is not the place to overrule that.
    """
    prefs = _prefs(context)
    if prefs is None:
        return
    try:
        setattr(prefs, field, str(Path(filepath).parent))
    except (AttributeError, TypeError, ValueError) as e:
        print(f"EXMATERIA-MAP: warning: could not remember {filepath}: {e}")
        return
    if not getattr(context.preferences, "use_preferences_save", False):
        return
    try:
        bpy.ops.wm.save_userpref()
    except RuntimeError as e:
        print(f"EXMATERIA-MAP: warning: could not save preferences, so "
              f"{field} will not survive a restart: {e}")


class IMPORT_OT_interchange_document(Operator):
    """Import an exmateria-map interchange document (JSON)."""
    bl_idname = "import_map.document"
    bl_label = "ExMateria Map Interchange"
    bl_description = "Import an exmateria-map interchange document (JSON)"
    bl_options = {"REGISTER", "UNDO"}

    filepath: StringProperty(subtype="FILE_PATH")

    def invoke(self, context, event):
        self.filepath = start_filepath(context)
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, context):
        layout = self.layout
        layout.label(text="ExMateria Map interchange document")

    def execute(self, context):
        try:
            doc = json.loads(Path(self.filepath).read_text())
        except Exception as e:
            self.report({"ERROR"}, f"could not read {self.filepath}: {e}")
            return {"CANCELLED"}
        problems = validate(doc)
        if problems:
            self.report({"ERROR"}, "; ".join(problems))
            return {"CANCELLED"}
        # §6.1: the drift checker is a scene-update handler, so hold it while
        # the operator's own mutations land -- a half-built scene is not one to
        # compute a drifted set from.  Imported inside `execute` because
        # `authoring` imports THIS module.
        from .authoring import suspended
        try:
            with suspended():
                ob = build(doc, context, self.filepath)
        except Exception as e:
            self.report({"ERROR"}, f"build failed: {e}")
            return {"CANCELLED"}
        # Nothing to prime.  `prime_live` used to live here, recording the
        # scene's lamps against a brand-new object so the update Blender fires
        # as the operator returns did not read it as "the lamps changed" and
        # solve the ROM normals away (205 of 243 moved on MAP000 a0).  Under
        # decision 30 import lands with Lamp authority OFF, so the handler skips
        # the object entirely and the failure cannot occur -- verified by
        # neutering `prime_live` and finding `blender_roundtrip.py` still
        # 315/315.  The invariant is now graded directly, on the SWITCH, by that
        # harness's `import_lands_authority_off`.
        remember_dir(context, self.filepath)
        return {"FINISHED"}


class MAP_OT_set_preview_state(Operator):
    """Preview a different map state: rewire the CLUT image (and the index
    image when the state names a different sheet), §4."""
    bl_idname = "exmateria_map.set_preview_state"
    bl_label = "Set interchange preview state"
    state_index: IntProperty(default=0)

    @classmethod
    def poll(cls, context):
        ob = context.object
        return ob is not None and "exmateria_map/preview_state" in ob

    def execute(self, context):
        ob = context.object
        cluts = json.loads(ob["exmateria_map/state_cluts"])
        sheets = json.loads(ob["exmateria_map/state_sheets"])
        sheet_imgs = json.loads(ob["exmateria_map/sheet_images"])
        i = max(0, min(self.state_index, len(cluts) - 1))
        mat = None
        for slot in ob.data.materials:
            if slot is not None and slot.name.endswith("_preview"):
                mat = slot
                break
        if mat is None or mat.node_tree is None:
            self.report({"ERROR"}, "no preview material on this object")
            return {"CANCELLED"}
        clut_node = mat.node_tree.nodes.get("exmateria_map.clut")
        idx_node = mat.node_tree.nodes.get("exmateria_map.index")
        if clut_node is None:
            self.report({"ERROR"}, "preview material has no CLUT node")
            return {"CANCELLED"}
        clut_node.image = bpy.data.images[cluts[i]]
        s = sheets[i]
        if s and sheet_imgs.get(s) and idx_node is not None:
            idx_node.image = bpy.data.images[sheet_imgs[s]]
        # the rig is PER MAP STATE (#358: 776 resources, 654 distinct), so the
        # corner bake is part of the state, not of the import
        try:
            states = json.loads(ob["exmateria_map/map_states"])
        except (KeyError, ValueError, TypeError):
            states = []
        # decision 25: override -> own -> keyed partner.  `preview_state` is
        # set FIRST because `apply_state_light` reads it — one path serves both
        # a state switch and an Override edit, so the two cannot drift apart.
        # the debug mode is VIEW state, not map data: it survives a state
        # switch on purpose, so nothing here touches the decode's input link
        ob["exmateria_map/preview_state"] = i
        apply_state_light(ob)
        # §3.3/§4.2: a state change is a RESOLVE trigger, and it runs AFTER
        # `preview_state` moves.  §4.1 is one pass: what the artist painted was
        # painted under the OUTGOING palette (which `paint` still has stored),
        # and the paint image ends up re-coloured under the INCOMING one (which
        # is the active palette only once this line has run).  Imported here
        # because `paint` imports this module.
        from .paint import on_trigger
        on_trigger(ob)
        return {"FINISHED"}


def preview_material(ob):
    """The object's interchange preview material, or None."""
    if ob is None or ob.type != "MESH" or ob.data is None:
        return None
    for slot in ob.data.materials:
        if slot is not None and slot.name.endswith("_preview"):
            return slot
    return None


def apply_light_debug(ob):
    """Push the object's VIEW state onto its preview graph.

    The mode and boost are registered Object properties, deliberately NOT
    `exmateria_map/...` JSON custom properties: those carry the document in the
    ROM's own shape so a future export leg is a straight serialisation, and a
    view mode has no ROM representation at all.  Keeping the two apart is the
    rule, not a detail — `preview_state` is view state and lives beside them
    for the same reason.
    """
    mat = preview_material(ob)
    if mat is None or "exmateria_map/preview_state" not in ob:
        return None
    return set_light_debug(mat, int(ob.exmateria_map_light_debug),
                           float(ob.exmateria_map_light_boost))


def _light_debug_update(self, context):
    apply_light_debug(self)


class MAP_OT_pin_view_transform(Operator):
    """Pin the Standard view transform + sRGB display so the preview's
    palette colours are unmodified (the ADR's rig trap; #427)."""
    bl_idname = "exmateria_map.pin_view_transform"
    bl_label = "Pin Standard view transform (preview parity)"

    def execute(self, context):
        context.scene.view_settings.view_transform = "Standard"
        return {"FINISHED"}


class MAP_OT_mint_rig_override(Operator):
    """Give the previewed state its own editable rig, copied from whatever the
    preview is currently lighting it with.

    Decision 25 REFUSES live editing on a state that is borrowing.  636 of the
    691 rig-less states corpus-wide are `texture` rows, so a slider that
    silently turned "this state has no rig" into "this state has a rig I
    invented" would mostly fire on a misclick — and decision 7's standing rule
    is that the preview SAYS what it does not know rather than substituting a
    default.  Minting is that conversion made deliberate.
    """
    bl_idname = "exmateria_map.mint_rig_override"
    bl_label = "Give this state its own rig"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        ob = context.object
        return ob is not None and "exmateria_map/preview_state" in ob

    def execute(self, context):
        ob = context.object
        states = object_states(ob)
        i = int(ob["exmateria_map/preview_state"])
        if find_override(ob, i) is not None:
            self.report({"INFO"}, f"state {i} already has an Override")
            return {"CANCELLED"}
        rig, src = state_rig(states, i)
        if rig is None:
            # Nothing in the document carries a rig (46 states corpus-wide).
            # Seed the albedo-only picture the preview already shows — ambient
            # 1.0, diffuse 0 — so minting does not MOVE the render, it only
            # makes it editable.
            rig = {"colors": [[0, 0, 0]] * 3,
                   "directions": [[0, 0, int(DIR_SCALE)]] * 3,
                   "ambient": [255, 255, 255], "gradient": [0] * 6}
            src = "no rig in this document (albedo)"
        seed_override(ob.exmateria_map_rig_overrides.add(), rig, i)
        apply_state_light(ob)
        self.report({"INFO"}, f"state {i}: rig copied from {src}")
        return {"FINISHED"}


class MAP_OT_clear_rig_override(Operator):
    """Drop this state's Override and go back to the document's rig."""
    bl_idname = "exmateria_map.clear_rig_override"
    bl_label = "Revert this state to the ROM's rig"
    bl_options = {"REGISTER", "UNDO"}

    all_states: bpy.props.BoolProperty(default=False)

    @classmethod
    def poll(cls, context):
        ob = context.object
        return ob is not None and len(getattr(
            ob, "exmateria_map_rig_overrides", ())) > 0

    def execute(self, context):
        ob = context.object
        i = int(ob.get("exmateria_map/preview_state", -1))
        drop = [k for k, ov in enumerate(ob.exmateria_map_rig_overrides)
                if self.all_states or ov.state_index == i]
        for k in reversed(drop):
            ob.exmateria_map_rig_overrides.remove(k)
        apply_state_light(ob)
        self.report({"INFO"}, f"reverted {len(drop)} state(s)")
        return {"FINISHED"}


# --- the viewport badge ------------------------------------------------------
# The panel alone is not enough.  This repo compares pictures by screenshotting
# the viewport against a Godot capture, and the N-panel is not in frame — so an
# edited preview that carries no mark in the image itself is exactly the thing
# that gets mistaken for the ROM's.  The badge is drawn ONLY while an Override
# is live, so an unedited preview is pixel-identical to what it was before this
# feature existed and no comparison is contaminated by the guard against it.
_BADGE_HANDLE = None


def _tag_redraw():
    """Ask every 3D view to repaint, so the badge appears the moment an
    Override does.  Headless has no window manager — hence the guard."""
    wm = getattr(bpy.context, "window_manager", None)
    for w in getattr(wm, "windows", ()) or ():
        for area in w.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


#: Decision 30's normals-axis flag.  A CACHED boolean rather than a live
#: comparison against `normals_shadow`: the badge redraws on every viewport
#: frame and walking 16k corners there would cost more than the solve does.
#: Exact for everything the addon writes, since the addon sets it where it
#: writes; the NAMED blind spot is a hand edit made outside it, whose backstop
#: is `describe_divergence`'s "changed since import: N face(s) normals" at
#: export.
NORMALS_EDITED = "exmateria_map/normals_edited"


def set_normals_edited(ob, on=True):
    """Mark (or clear) this map's normals as no longer the document's."""
    if on:
        ob[NORMALS_EDITED] = True
    elif NORMALS_EDITED in ob:
        del ob[NORMALS_EDITED]
    _tag_redraw()


def normals_edited(ob):
    return bool(ob.get(NORMALS_EDITED))


def edited_objects(context):
    """Objects in view whose preview is being driven by an Override, or whose
    normals the addon has written since import.

    **Divergence, never the switch.**  A badge tied to Lamp authority would go
    silent exactly where edited normals now LIVE -- authority off, with a solve
    committed -- and decision 25's standing rule is that an edited picture must
    never be mistakable for the ROM's, because this repo compares maps by
    screenshotting the viewport and the N-panel is not in frame.
    """
    return [ob for ob in getattr(context, "visible_objects", None) or ()
            if len(getattr(ob, "exmateria_map_rig_overrides", ())) > 0
            or normals_edited(ob)]


def badge_text(obs):
    """The badge's line for `obs` — ONE badge naming both axes, not two
    competing for the corner.

    The artist's question is "is this the ROM's picture", and that has one
    answer with two causes (decision 30).  Split out from `_draw_badge` so it
    is gradeable: the drawing half needs a window manager and a populated
    `visible_objects`, neither of which a headless check has.
    """
    n = sum(len(getattr(ob, "exmateria_map_rig_overrides", ())) for ob in obs)
    axes = []
    if n:
        axes.append(f"light rig ({n} state(s))")
    if any(normals_edited(ob) for ob in obs):
        axes.append("normals")
    return "EDITED — " + ", ".join(axes) + " — not the ROM's"


def _draw_badge():
    try:
        import blf
    except ImportError:
        return
    obs = edited_objects(bpy.context)
    if not obs:
        return
    text = badge_text(obs)
    fid = 0
    blf.size(fid, 16)
    blf.color(fid, 1.0, 0.55, 0.15, 1.0)
    blf.position(fid, 24, 24, 0)
    blf.draw(fid, text)


def register_badge():
    global _BADGE_HANDLE
    if _BADGE_HANDLE is None:
        _BADGE_HANDLE = bpy.types.SpaceView3D.draw_handler_add(
            _draw_badge, (), "WINDOW", "POST_PIXEL")


def unregister_badge():
    global _BADGE_HANDLE
    if _BADGE_HANDLE is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_BADGE_HANDLE, "WINDOW")
        _BADGE_HANDLE = None


class MAP_PT_preview(Panel):
    """N-panel: the previewed state and its CLUT provenance (§4)."""
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"
    bl_category = "Map"
    bl_label = "ExMateria Map Preview"
    bl_order = 0

    def draw(self, context):
        ob = context.object
        if ob is None or "exmateria_map/preview_state" not in ob:
            return
        try:
            states = json.loads(ob["exmateria_map/map_states"])
        except (KeyError, ValueError, TypeError):
            return
        if not states:
            return
        layout = self.layout
        i = int(ob["exmateria_map/preview_state"])
        st = states[i] if i < len(states) else {}
        layout.menu(MAP_MT_preview_state.bl_idname,
                    text=state_menu_label(states, i),
                    icon="TEXTURE" if st.get("palettes") else "NONE")
        if st.get("palettes"):
            layout.label(text="CLUT from document palettes", icon="CHECKMARK")
        else:
            layout.label(text="no CLUT in this state "
                              "(sidecar PLTE, untrusted-colour)", icon="ERROR")
        ov = find_override(ob, i)
        src = ob.get("exmateria_map/light_source") or ""
        rig = state_own_rig(st)
        if ov is not None:
            layout.label(text="light: EDITED — this is NOT the ROM's rig; "
                              "export WRITES it (decision 27)",
                         icon="GREASEPENCIL")
        elif not src:
            layout.label(text="light: flat 1.0 — no light rig in this "
                              "arrangement (albedo only)", icon="ERROR")
        elif st.get(AUTHORED_RIG):
            layout.label(text=f"light: AUTHORED rig — this document already "
                              f"carries one for {src}, and `build` writes it",
                         icon="GREASEPENCIL")
        elif rig:
            layout.label(text=f"light: 45-byte rig from {src}", icon="LIGHT_SUN")
        else:
            layout.label(text=f"light: rig BORROWED from {src} "
                              f"(same night/weather)", icon="INFO")
        _draw_rig(layout, ob, i, ov, rig, src)
        layout.prop(ob, "exmateria_map_light_debug")
        row = layout.row()
        # Godot's boost exaggerates modes 2/3/4 only, and so does the graph
        row.enabled = ob.exmateria_map_light_debug in ("2", "3", "4")
        row.prop(ob, "exmateria_map_light_boost")
        layout.separator()
        layout.operator(MAP_OT_pin_view_transform.bl_idname)


def marker_in_scene(context):
    """The map this panel acts on — found in the SCENE, not taken from the
    selection.

    Polling on `context.object` is a defect this package has already paid for
    once: aiming a lamp means SELECTING the lamp, which makes it the active
    object, which hides the panel at exactly the moment the artist reached for
    it (`lighting_bake.target_map`). Export's own `find_marker` is the rule.
    """
    from .export_document import find_marker, markers
    ob, _problem = find_marker(context)
    if ob is not None and "exmateria_map/preview_state" in ob:
        return ob
    for cand in markers(getattr(context, "scene", None) or bpy.context.scene):
        if "exmateria_map/preview_state" in cand:
            return cand
    return None


class MAP_MT_preview_state(Menu):
    """The map states, as a menu.

    Reported from use: "the map preview should be a drop down not a bunch of
    options taking up a ton of space."  It was one operator button per state, in
    a column -- and 32.17% of geometry-bearing arrangements carry TEN states, so
    the list was taller than everything else in the panel put together.  Picking
    a preview state is a choice of exactly one, which is what a menu is for.

    The operator behind each entry is unchanged (`state_index`, an int), so
    every harness that drives it keeps working.
    """
    bl_idname = "MAP_MT_preview_state"
    bl_label = "Preview state"

    def draw(self, context):
        ob = getattr(context, "object", None)
        try:
            states = json.loads(ob["exmateria_map/map_states"])
            i = int(ob["exmateria_map/preview_state"])
        except (KeyError, ValueError, TypeError):
            return
        for k, s in enumerate(states):
            op = self.layout.operator(
                "exmateria_map.set_preview_state",
                text=f"{k}: {s.get('resource')} ({s.get('kind')})",
                icon="CHECKMARK" if k == i else "BLANK1")
            op.state_index = k


def state_menu_label(states, i):
    """What the closed menu reads — the state in view, not the menu's name."""
    st = states[i] if i < len(states) else {}
    return f"State {i}: {st.get('resource')} ({st.get('kind')})"


class MAP_PT_export(Panel):
    """N-panel: write the interchange document to disk.

    Its own panel rather than a tail on the preview's, which is what made the
    preview panel three unrelated jobs in one column.
    """
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"
    bl_category = "Map"
    bl_label = "ExMateria Map Export"
    bl_order = 4

    @classmethod
    def poll(cls, context):
        return marker_in_scene(context) is not None

    def draw(self, context):
        layout = self.layout
        ob = marker_in_scene(context)
        if ob is None:
            return
        # Export-v1 §5: the operator reports refusals, warnings and the
        # divergence list to the N-panel as well as to the operator report,
        # because an operator report is gone by the time the artist looks.
        layout.operator("export_map.document", icon="EXPORT",
                        text="Export interchange document")
        _stored_report(layout, ob, "exmateria_map/last_export", "Last export:")


def _stored_report(layout, ob, key, title):
    """The last export's or push's refuse/warn lines (export-v1 §5.1/§5.2).

    Stored on the marker rather than left to the operator report, which is gone
    by the time the artist looks up from the viewport."""
    try:
        lines = json.loads(ob.get(key) or "[]")
    except (ValueError, TypeError):
        return
    if not lines:
        return
    box = layout.box()
    box.label(text=title, icon="INFO")
    # WRAP, do not truncate.  A Blender label does not wrap, and cutting at 90
    # ate the end of every line -- which on a push report is where the reason
    # lives ("...check Lamp authority is ON").  A cut sentence reads as a bug in
    # the thing being reported on.
    for line in lines[:12]:
        icon = "ERROR" if line.startswith("REFUSE") else "INFO"
        for k, chunk in enumerate(textwrap.wrap(line, 88) or [""]):
            box.label(text=chunk if k == 0 else "    " + chunk,
                      icon=icon if k == 0 else "BLANK1")
    if len(lines) > 12:
        box.label(text=f"... and {len(lines) - 12} more")


def _rig_box(layout, ov, editable):
    """The 21 controls, plus the 6 bytes that are shown and not edited."""
    box = layout.box()
    col = box.column(align=True)
    col.enabled = editable
    col.prop(ov, "ambient")
    for k in range(3):
        col.separator()
        col.label(text=f"Light {k + 1}")
        # A gain is NOT a colour: Blender hard-clamps a COLOR widget to 0-1 and
        # the corpus reaches 13.55x, so this is a plain float triple in Godot's
        # own uniform units.  The direction's LENGTH is ignored everywhere.
        col.prop(ov, MAP_PG_rig_override.GAINS[k], text="gain")
        col.prop(ov, MAP_PG_rig_override.DIRS[k], text="dir")
    col.separator()
    grad = box.column(align=True)
    grad.enabled = False
    grad.label(text="background gradient — the game draws this as a SCREEN "
                    "backdrop, not shading; carried, not previewed")
    grad.prop(ov, "gradient", text="top / bottom (u8)")


def _draw_rig(layout, ob, i, ov, own_rig, src):
    layout.separator()
    if ov is not None:
        _rig_box(layout, ov, True)
        row = layout.row(align=True)
        row.operator(MAP_OT_clear_rig_override.bl_idname,
                     text="Revert this state", icon="LOOP_BACK")
        n = len(ob.exmateria_map_rig_overrides)
        row.operator(MAP_OT_clear_rig_override.bl_idname,
                     text=f"Revert all ({n})", icon="TRASH").all_states = True
        return
    # No Override.  Decision 25 refuses live editing on a state that does not
    # own its rig; the values are still SHOWN, so the artist can see what the
    # preview is using and where it came from.
    box = layout.box()
    states = object_states(ob)
    authored = bool(0 <= i < len(states) and states[i].get(AUTHORED_RIG))
    if own_rig is None:
        box.label(text="no rig to show" if not src
                       else f"showing the rig borrowed from {src}", icon="INFO")
    elif authored:
        box.label(text=f"the AUTHORED rig this document carries for {src} — "
                       f"not the ROM's", icon="GREASEPENCIL")
    else:
        box.label(text=f"the ROM's rig, from {src}", icon="LIGHT_SUN")
    if own_rig is not None:
        col = box.column(align=True)
        col.enabled = False
        col.label(text=f"ambient  {tuple(own_rig['ambient'])}")
        for k in range(3):
            g = tuple(round(c / GAIN_SCALE, 3) for c in own_rig["colors"][k])
            col.label(text=f"gain {k + 1}   {g}")
        col.label(text=f"gradient {tuple(own_rig.get('gradient') or ())}")
    box.operator(MAP_OT_mint_rig_override.bl_idname, icon="GREASEPENCIL")


def menu_func(self, context):
    layout = self.layout
    layout.operator(IMPORT_OT_interchange_document.bl_idname, text="ExMateria Map Interchange (.json)")


def register():
    # View state, on the Object rather than the Scene: two documents can be
    # imported side by side and compared, so the mode cannot be global.
    bpy.types.Object.exmateria_map_light_debug = EnumProperty(
        name="Light debug",
        description="Isolate one stage of the lighting chain "
                    "(Godot's map_light_debug)",
        items=[(k, label, desc) for k, label, desc in DEBUG_MODES],
        default="0", update=_light_debug_update)
    bpy.types.Object.exmateria_map_light_boost = FloatProperty(
        name="Light boost",
        description="Exaggerate the isolated stage, then clamp "
                    "(Godot's map_light_boost); modes 2-4 only",
        default=1.0, min=0.0, soft_max=8.0, update=_light_debug_update)
    bpy.utils.register_class(MAP_AddonPreferences)
    # The PropertyGroup must register BEFORE the CollectionProperty that names
    # it.  The collection is Object-level for the same reason the debug mode is:
    # two documents can be imported side by side and compared.
    bpy.utils.register_class(MAP_PG_rig_override)
    bpy.types.Object.exmateria_map_rig_overrides = CollectionProperty(
        type=MAP_PG_rig_override,
        description="Decision 25 Overrides: edited light rigs, per map state. "
                    "Preview-only — never written to the document or the disc")
    bpy.utils.register_class(IMPORT_OT_interchange_document)
    bpy.utils.register_class(MAP_OT_set_preview_state)
    bpy.utils.register_class(MAP_OT_pin_view_transform)
    bpy.utils.register_class(MAP_OT_mint_rig_override)
    bpy.utils.register_class(MAP_OT_clear_rig_override)
    bpy.utils.register_class(MAP_MT_preview_state)
    bpy.utils.register_class(MAP_PT_preview)
    bpy.utils.register_class(MAP_PT_export)
    register_badge()
    # Blender 5.x removed `bpy.utils.menu_registry`; built-in menus take
    # `.append()` directly.  The fallback covers the 4.x line the addon still
    # supports per `bl_info["blender"]`.
    try:
        bpy.types.TOPBAR_MT_file_import.append(menu_func)
    except AttributeError:
        from bpy.utils import menu_registry
        menu_registry.append(bpy.types.TOPBAR_MT_file_import, menu_func)


def unregister():
    try:
        bpy.types.TOPBAR_MT_file_import.remove(menu_func)
    except AttributeError:
        from bpy.utils import menu_registry
        menu_registry.remove(bpy.types.TOPBAR_MT_file_import, menu_func)
    unregister_badge()
    bpy.utils.unregister_class(MAP_PT_export)
    bpy.utils.unregister_class(MAP_PT_preview)
    bpy.utils.unregister_class(MAP_MT_preview_state)
    bpy.utils.unregister_class(MAP_OT_clear_rig_override)
    bpy.utils.unregister_class(MAP_OT_mint_rig_override)
    bpy.utils.unregister_class(MAP_OT_pin_view_transform)
    bpy.utils.unregister_class(MAP_OT_set_preview_state)
    bpy.utils.unregister_class(IMPORT_OT_interchange_document)
    del bpy.types.Object.exmateria_map_rig_overrides
    bpy.utils.unregister_class(MAP_PG_rig_override)
    bpy.utils.unregister_class(MAP_AddonPreferences)
    del bpy.types.Object.exmateria_map_light_boost
    del bpy.types.Object.exmateria_map_light_debug


def viewport_draw_overlays(self, context):
    pass


classes = (MAP_AddonPreferences, MAP_PG_rig_override,
           IMPORT_OT_interchange_document, MAP_OT_set_preview_state,
           MAP_OT_pin_view_transform, MAP_OT_mint_rig_override,
           MAP_OT_clear_rig_override, MAP_MT_preview_state,
           MAP_PT_preview, MAP_PT_export)

if __name__ == "__main__":
    register()
