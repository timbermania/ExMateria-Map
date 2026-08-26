# Interchange import (Blender addon) — field-by-field design, schema v1

Design record for the addon's import leg, resolved by
[The addon's import side](https://github.com/timbermania/fft-monorepo/issues/520) on
[Executing ADR-0004: dump, build, the Blender addon, and the patcher map leg](https://github.com/timbermania/fft-monorepo/issues/517).
What it pins: every schema-v1 field's named import target (object / attribute /
material / image / custom property), the inverse export read, the axis-2
assertions the import must satisfy (ADR-0004 decision 8), and the linked
headless prototype.

The field contract remains
[`interchange-schema-v1.md`](interchange-schema-v1.md); this document decides
where each field lands in Blender and how the export leg reads it back. The
import leg builds on the proven model of
[`workspace/roundtrip426.py`](../workspace/roundtrip426.py) (148/148 EXACT)
and replaces the `#519`-era probe attribute names with schema-v1 names.

## 1. Document objects and collection (Q1)

- **One mesh object per document**, named `<map>.a<arrangement>` (e.g.
  `MAP001.a0`).
- **One collection per document**, same name, holding: the mesh object, the
  grid footprint object (when `terrain_grid` is non-null), and the created-tile
  objects. Re-import deletes and rebuilds that collection.
- **Quads stay quads** — no triangulation. Corner order is the PSX
  triangle-strip ring `0,1,3,2`, reversed on import (decision 14): loops are
  laid `2,3,1,0`; a face the per-face flip records is laid back in strip order
  `0,1,3,2`. Triangles: `2,1,0` (reversal of `0,1,2`).
- **Welded by exact position.** The `(x, z, −y)` axis map is bijective on i16,
  so the transformed corner coordinates are integer-exact and weld by exact
  match of the transformed position.
- **Per-face flip** (decision 14's residue register): a BOOL face attribute
  `fft_ring_flipped` (the one addon-internal name; kept from the probe as its
  registered name) records which textured faces were reversed a second time so
  export can undo it and axis 1 stays exact.

## 2. Axis frame (decisions 8, 14)

`(x, y, z)_fft → (x, z, −y)_blender`, applied to **positions and to normals**.
The inverse, used by export, is `(bx, by, bz) → (bx, −by, bz)`. det = +1, so
the map does not mirror. The rotation about up has no data assertion (four
unmirrored candidates tied at the top, #460); it is the **pinned constant**
read back from `blender_axis_baseline.json` (decision 8, #474).

## 3. Attributes and the shadow rule (Q2)

Document fields take their **schema-v1 names verbatim** as import targets:

- face INT: `visible_angles`, `palette_id`, `palette_byte_high_nibble`,
  `texture_page`, `unknown_texture_value_6a`, `texture_byte6_high_nibble`,
  `terrain_x`, `terrain_z`, `terrain_level` (the nested `terrain` object
  flattened), `unknown_untextured_0`…`unknown_untextured_3`
- face BOOL: `textured` (tri/quad falls out of the corner count)
- CORNER FLOAT_VECTOR: `normals` — the transformed authored values, **never
  spherical or averaged** (#427's rule; Blender's smooth average reproduces
  only 75.5%, decision 3). Untextured polygons carry zero normals.
- UV layer `UVMap`: half-texel-centred and v-flipped,
  `((u+0.5)/256, 1 − (page·256 + v + 0.5)/1024)`; the `texture_page` band is
  encoded in the v band. Untextured polygons get `(0,0)` UVs.

**Shadow rule.** Every carried field gets a same-domain twin named
`<field>_shadow` (face INT → face INT, CORNER FLOAT_VECTOR → CORNER FLOAT_VECTOR,
face BOOL → face BOOL; **positions get a CORNER position shadow**), written
once at import and never artist-touched. Export's divergence list is the
per-face comparison of each field against its shadow; the position shadow
answers "did this face move".

## 4. Materials and textures (Q3, decision 7)

- **Two material slots per document.** Slot 0 = the shared per-scene unlit
  grey `exmateria_map_unlit_grey` (used by untextured polygons **and** the
  tile objects). Slot 1 = one interchange preview material,
  `<map>.a<arrangement>_preview`. The face material index follows the
  `textured` flag. No per-state / per-palette / per-sheet material
  multiplicity.
- **Images, built at import.** The addon decodes each distinct sheet
  sidecar's raw indices (stdlib PNG/zlib, the same decoder the build side
  uses) into one **index image** per distinct sheet: 256 wide × 1024 tall,
  the four `texture_page` bands stacked. One **16×16 CLUT image** per
  `map_states` entry with valid `palettes` (row = CLUT, 16 entries).
- **Graph.** Index image sampled at UV → column-lookup into the CLUT image
  row given by the face's `palette_id` Attribute node → colour × (ambient +
  corner diffuse) → clamp → sRGB decode → Emission. Blender never holds palette
  indices and export recovers them by exact match against the 16 CLUT entries
  (else it refuses).
- **The whole chain multiplies in PSX BYTE space** (#427). The PSX
  multiplies the 8-bit CLUT value, so both images are `Non-Color`, the
  multiply is clamped at 1.0 (#358's max gain is an ordinary 13.55× and would
  otherwise run past it), and the sRGB decode happens **once at the very
  end** — the display's own sRGB encode then hands the byte back unchanged.
  Multiplying in linear space instead is not a subtlety: at light 1.0 it
  renders CLUT byte 128 as **188**, so it washes out the palette before any
  lighting is involved. This is not introspectable — it is pinned by
  `tests/blender_lighting_calibration.py`, which renders known bytes and
  reads the file back.
- **The corner `diffuse` attribute is BAKED, not computed in the graph.** It
  holds `sum over the 3 lights of gain_i · max(0, N·L_i)` per corner, from the
  selected state's `light_rig` (schema §7.1). Per corner
  because that is the PSX GTE's sample rate: a graph is evaluated per
  *fragment*, which interpolates the normal and clamps after, dissolving
  exactly the facet edges the artist authors normals against. The dot is
  taken in Blender space on both vectors — the same proper rotation on each,
  so it equals `dot(raw normal, raw light direction)`, which #427 measured
  identical to Godot's spherical round-trip on 273,128 of 273,128 corners.
- **`ambient` is a GRAPH CONSTANT, not a corner attribute** (decision 24). It
  is `[u8 × 3]` — one triple per map state — and both references make it a
  constant: the PSX GTE holds it in a register and Gouraud-interpolates only
  the per-vertex output, and the game declares `ambient_light` a `uniform`
  against a `v_diffuse` `varying`. The sum node feeding the multiply **must
  stay unclamped**: ambient + diffuse routinely exceeds 1.0 and only the final
  pixel saturates. The split is pixel-identical to a summed bake — linear
  interpolation commutes with adding a constant — and
  `tests/blender_light_debug.py` asserts that by render, seeding a clamp on the
  sum node to prove the assertion can fail.
- **Rig-absent rule (decision 7).** A state with no `light_rig` borrows one
  from a same-arrangement sibling that has one, and the panel names the
  borrow. An arrangement with none anywhere bakes diffuse 0 against ambient
  1.0 — albedo only — and the panel says so. No default rig is ever
  substituted.
- **Debug view modes (decision 24).** Godot's `map_light_debug` 0–5 and
  `map_light_boost`, as an N-panel enum + slider that rewires **which stage
  feeds the sRGB decode**: 0 normal, 1 normals-as-colour, 2 lighting-only,
  3 ambient-only, 4 diffuse-only, 5 albedo-only. One decode group serves all
  six, because the game overrides `final` in its debug branch and then runs the
  same output conversion on it. Mode 1 encodes the **raw FFT triple** — the
  addon stores normals rotated by decision 14 while the game's map conversion
  is the identity permutation, so an unswizzled encode agrees on red and
  disagrees on green and blue. The mode and boost are **view state**: registered
  Object properties, deliberately outside the `exmateria_map/…` custom
  properties that carry the document in the ROM's own shape.
- **State selection** is the previewed state (N-panel, decision 7); the
  default state is `geometry_source`'s. Switching state rewires which CLUT
  image the graph samples, **re-bakes the corner diffuse and rewrites the
  ambient constant** — the rig is
  per map state (#358: 776 resources, 654 distinct rigs), and the bake is
  1.39 ms on the corpus's largest mesh. The index image and every schema-v1
  attribute are state-independent. The debug mode is view state and
  **persists** across a state switch: an artist comparing normals between
  states should not be returned to mode 0 each time.
- **Edge — `palettes: null` on a state:** fall back to the sidecar's
  true-colour expansion (display-only PLTE read from the PNG); the panel says
  "no CLUT in this state". The preview renders but is untrusted-colour,
  matching the build-side refusal.

## 4b. The rig Override (decision 25)

The rig's **ambient, three gains and three directions** are editable in the
N-panel, **preview-only** — nothing is written to the document or the disc, so
decision 3's ownership table is untouched.

- **An Override** is the edited rig, held per map state on an Object
  `CollectionProperty`. It is neither document data nor view state: the axis is
  **provenance** — is this sourced from the imported document? — and an
  Override is ROM-shaped without being document-sourced. It never enters
  `exmateria_map/map_states`, which is asserted on that property's bytes rather
  than inferred (`blender_roundtrip.py`).
- **Resolution order is `override → own → keyed partner`**, through the one
  `apply_state_light` path a state switch also uses, so an edit and a state
  switch cannot drift apart.
- **Editing surfaces differ because the data do.** Ambient is a colour and fits
  0–1 (corpus maximum 160/255 = 0.627). A **gain is not a colour** — it reaches
  **13.55×** and Blender hard-clamps a `subtype='COLOR'` widget to 0–1 — so it
  is a plain unclamped float triple in Godot's own uniform units, capped at
  16.06, the i16 ceiling. A **direction** is a unit vector edited in **Blender
  space**; its length carries nothing (every disc magnitude is within 2 LSB of
  4096) and the references' `(elevation, azimuth)` pair is a trap, since
  `spherical_to_cartesian ∘ vector_to_sphere` is measured **not** to be the
  identity — it returns `(−x, y, −z)`.
- **Edits quantize to the ROM immediately.** `override_rig` rounds ambient to
  u8 and a gain to i16 before the bake, so ambient `0.125` renders as `32/255`.
  The artist is shown exactly what the 45 bytes can hold.
- **A borrowing state refuses live editing.** Its values are shown read-only
  with the source named, and one explicit operator
  (`exmateria_map.mint_rig_override`) mints an Override seeded from it. 636 of
  the 691 rig-less states corpus-wide are `texture` rows, so a slider that
  silently minted a rig would mostly fire on a misclick — and decision 7's rule
  is that the preview *says* what it does not know.
- **`gradient` is shown read-only**, both swatches, labelled as the game's
  screen backdrop (`MapComposer._apply_gradient_from_manifest` →
  `ScreenEffectOverlay`). It is carried so the Override is the whole 45 bytes,
  but it is not shading and so not a parity target.
- **The signal is the panel line plus a viewport badge**, drawn only while an
  Override is live. The badge exists because this repo compares pictures by
  screenshotting the viewport against a Godot capture and the N-panel is not in
  frame; suppressing it when clean keeps an unedited preview pixel-identical to
  what it was before the feature existed.
- **An Override survives save → reopen** (`blender_reload_persistence.py`,
  which runs a CLEAN and an EDITED round and fails if the edit does not move
  the picture — otherwise the EDITED round would be vacuous).

## 4a. Where the import browser opens

An operator that assigns `filepath` in `invoke` **overrides Blender's own
last-directory memory**, which is otherwise free. Assigning it from
`scene.render.filepath` — `/tmp/` on a fresh start, so its parent is `/` —
therefore drops the artist at the filesystem root on every launch. The addon
keeps the memory itself: `last_dir` on its `AddonPreferences` (preferences,
not scene properties, because the point is surviving a restart), written on a
successful import and read by `start_filepath`. It falls back to the old
`scene.render.filepath` derivation when there is no memory yet, and to that
same fallback when the module is imported directly rather than enabled as an
addon — which is what the headless harnesses do.

## 5. Terrain: grid footprint and tile objects (Q4, decisions 10–13, 15)

Scale (from the proven decision-13 prototype): **28 world units per tile**,
**12 world Y per `height` step**.

- **Grid footprint** — one object, `<map>.a<arrangement>_grid`, present iff
  `terrain_grid` is non-null. Single-quad mesh at Z=0 carrying the **extent
  only** (decision 13; one face, decision 15): corners `(0,0,0)`,
  `(size_x·28, 0, 0)`, `(size_x·28, size_z·28, 0)`, `(0, size_z·28, 0)`.
  No material, `display_type = 'WIRE'` — it must not be drawn as if it were
  terrain. Its `size_x` / `size_z` custom properties (schema-v1 names, each
  with a `_shadow` twin) carry the writable target extent the growth surface
  writes (decisions 10, 16).
- **Created tiles** — one 28×28 plane object per **level-0 record** in the
  document's `terrain` list, named `tile_<x>_<z>_L<level>`, at
  `(x·28, z·28, height·12)` (the record is authoritative; Z is derived and
  locked, decision 13). `height` defaults to 0 when undeclared. **Level-1
  records never become objects** (decision 13).
- **Import-time object rule.** Every level-0 record in the list gets an
  object. The §7.2 class split (drift-named vs growth-created) is `build`'s
  job against the base resource it reads off disk; a self-contained addon
  (decision 7) cannot re-derive the pre-growth extent from the document alone
  (`terrain_grid` is the grown extent; `terrain_digest` is a digest, not the
  payload). Every record in the list is already artist-declared (an
  untouched document declares nothing, decision 22), so the "existing tiles
  not drawn as objects" population decision 13 ruled out — the whole 256-tile
  grid — never appears; a drift-named tile's object shows exactly its three
  declared fields, nothing more.
- **Record fields on the tile object** — custom object properties,
  schema-v1 names verbatim (`x`, `z`, `level`, and up to 20 payload fields:
  `surface_type`, `height`, `depth`, `slope_height`, `slope_type`,
  `thickness`, `shading`, `rotation`, `unknown_1`, the nine 0/1
  `unknown_*` bits, `impassable`, `unselectable`, `pass_through_only`),
  **written only for declared fields** (an absent field is not zero — it
  carries or defaults per class, and that is build's call; writing defaults
  would corrupt the inverse export), each with a `<field>_shadow` twin.
  Export must not parse object names; `x`/`z`/`level` are props.
- **`base.floor_steps`** — carried for the live drift overlay (decisions 15,
  23): the base's `round(−max(ys)/12)` step plus the base's
  `slope_height` / `slope_type` pair per occupied tile. Lives in the marker's
  `exmateria_map/base` JSON (§6); the drift panel reads it.

## 6. Carried payload: the marker's custom properties (Q4d)

The import stub's named marker object carries **one JSON property per
top-level document section**:

| marker custom property | payload |
|---|---|
| `exmateria_map/base` | the whole `base` object (map, arrangement, resources, geometry_source, digests, terrain_grid, floor_steps) |
| `exmateria_map/polygons` | the `polygons` array |
| `exmateria_map/terrain_records` | the `terrain` array (or `null`) |
| `exmateria_map/map_states` | the `map_states` array |
| `exmateria_map/carry` | the `carry` object |

Split of labour: **authored fields are read back from the scene graph**
(attributes, positions, tile properties, grid properties, material slot
assignment); **carried sections are written back verbatim from the marker
JSON**. The marker JSON is the import-time snapshot — the shadow twins are
the divergence mechanism, not the JSON.

## 7. Field-by-field import targets and inverse export read

| document field | import target | inverse export read |
|---|---|---|
| `format`, `version` | validation only (refusal rule, §2 of the schema) | export writes the constants |
| `base` (all sub-fields) | marker `exmateria_map/base` (JSON) | marker JSON verbatim, **except** `terrain_grid` ← the grid object's `size_x`/`size_z` props (`null` when no grid object) |
| `kind` | corner count + face `textured` | `textured` + 3/4 corners → the bucket string |
| `positions` | mesh corner positions, `(x, z, −y)`, welded by exact position | loop positions → `(bx, −by, bz)` → i16 |
| `visible_angles` | face INT (+shadow) | face attribute |
| `normals` (textured) | CORNER `normals` FLOAT_VECTOR, transformed (+shadow) | corner attribute → inverse map → i16 |
| `uv` (textured) | `UVMap` layer: `((u+0.5)/256, 1 − (page·256 + v + 0.5)/1024)` | decode with rounding; the page comes back from the v band |
| `palette_id`, `palette_byte_high_nibble`, `texture_page`, `unknown_texture_value_6a`, `texture_byte6_high_nibble` | face INTs, schema-v1 names (+shadows) | face attributes (`palette_id` also drives the CLUT row in the graph) |
| `terrain` (polygon binding) | face INTs `terrain_x` / `terrain_z` / `terrain_level` (+shadows) | the three attributes → nested object; out-of-grid values are warning-only, never refusal (decision 9) |
| `unknown_untextured[4]` | face INTs `unknown_untextured_0`…`_3` (+shadows) | the four attributes → array |
| (addon-internal) | face BOOL `fft_ring_flipped` | undoes the second ring reversal on export |
| `terrain` records | tile objects (§5); level-1 records via the marker JSON | level-0 from the tile object properties (the record is authoritative); level-1 from the marker JSON |
| `map_states` | marker `exmateria_map/map_states` (JSON) + the derived CLUT images + preview wiring | marker JSON verbatim |
| `carry` | marker `exmateria_map/carry` (JSON) | verbatim |

Material slot assignment is checked against the `textured` flag for
consistency; it is a check, not a data source.

## 8. Axis-2 assertions the import must satisfy (Q5, decision 8)

Binding mechanism: expected counts in git, no CI, a **fixed expectation, not
a ratchet** — the residue is a property of the frozen 1997 data. The baseline
is [`blender_axis_baseline.json`](../blender_axis_baseline.json) (no
`--update` flag; expectations were derived offline first, so the instrument
is not its own oracle). Four data groups plus one configuration assertion:

| group | expectation | what it catches |
|---|---|---|
| `weld` | 73,485 verts of 281,096 corners; 0 unwelded | an unwelded import (281,096 loose vertices, which no artist can edit) |
| `winding` (post-flip, textured quads only) | 51,734 aligned / **0 anti** / 2,448 ambiguous / 234 degenerate of 54,416; hard bar `anti == 0` | a bowtie — plain Newell over positions, **not** Blender's `polygon.normal` (which is bowtie-blind: it reports 0 degenerate faces in both corner orders) |
| `ring` (pre-flip bucket + flip count) | 50,480 / 1,254 / 2,448 / 234; **1,353 flipped** (99 tri + 1,254 quad) | a ring-order mistake the post-flip bucket self-heals (the flip drives `anti` to zero under *either* ring order and under any `det = +1` map) |
| `up` | 16,209 of 16,670 floor-like polygons point +Z (97.23%) | a map laid on its side (the winding dot is invariant under every `det = +1` axis map) |
| pinned constant | `(x, z, −y)`, ring reversed, read back from the baseline | the rotation about up — no data assertion exists for it, so the read-back *is* the ratification |

The flip predicate the import implements: textured faces only, plain Newell
over the as-imported (pre-flip) ring against the mean of the transformed
authored corner normals, **flip iff the cosine < −0.5** (axis-2's own
threshold, so the flip and the bucket agree by construction). Untextured
polygons carry no authored normal and are never flipped.

**Object-level counts:** exactly one mesh object per document, named
`<map>.a<arrangement>`, face count = polygon count, quads stay quads;
exactly two material slots; the grid object present iff `terrain_grid` is
non-null (exactly one, single face); tile object count = level-0 record
count, zero objects for level-1 records.

**Scope notes:** winding/ring cover textured quads only (untextured polygons
carry no authored normal — no second path; triangles are bowtie-immune by
construction). The Newell path (positions) and the authored-normal path are
independent document fields — the #427 pattern. The `up` group reads the
authored normal and nothing else (no Newell requirement), so it moves under
a flipped axis and nothing else.

## 9. Prototype link

The proven headless corpus import is
[`workspace/roundtrip426.py`](../workspace/roundtrip426.py) — the import leg
this design formalizes, with `#519`-era probe attribute names. Live run,
Blender 5.2.0 LTS background, `EXMATERIA_ASSETS_DIR` = the populated
`project-assets/`:

```
RT426 mode weld=True bowtie=False ring=REVERSED axis=('x', 'z', '-y')
RT426 arrangements  148 / 148 EXACT
RT426 faces dropped by Blender on import: 0
RT426 time 4.7s  (0.03s per arrangement)
RT426 AXIS 2 -- asserted against blender_axis_baseline.json
RT426   groups moved: none
RT426 PASS
```

(The schema-v1 attribute names in §3 are the model this design formalizes;
the addon's real import leg and the move of the axis assertions into
`tests/blender_roundtrip.py` are the next tickets' build work.)
