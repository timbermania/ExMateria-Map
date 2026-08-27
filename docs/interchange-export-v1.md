# Interchange export (Blender addon) — field-by-field design, schema v1

Design record for the addon's export leg, resolved by
[The addon's export side](https://github.com/timbermania/fft-monorepo/issues/521) on
[Executing ADR-0004: dump, build, the Blender addon, and the patcher map leg](https://github.com/timbermania/fft-monorepo/issues/517).
What it pins: the five export-leg blocks — round trip and carry, the palette gate,
the drift surface, growth, and new-face defaults — plus the refuse/warn/informational
list, the export operator's end-to-end shape, and why `export(import(doc)) == doc`.

The field contract remains
[`interchange-schema-v1.md`](interchange-schema-v1.md); the import leg's field targets
and inverse reads are in [`interchange-import-v1.md`](interchange-import-v1.md) §7.
This document is the export half of that table: for every document field it names the
Blender source export reads and the default a new face starts from. It formalizes the
proven model of
[`workspace/roundtrip426.py`](../workspace/roundtrip426.py) (148/148 EXACT), whose
`export()` is the headless reference this design replaces with schema-v1 names.

## 0. Build state (added by #557, 2026-08-25)

This document is the design; the addon leg is
[`addons/exmateria_map/export_document.py`](../addons/exmateria_map/export_document.py).
What is **built and asserted**: §1's five sources, §2 (round trip and carry,
including §2.1's verbatim `carry`), §2.2's identity — `export(import(doc)) ==
doc` holds **whole-document on all 148 corpus arrangements**
(`tests/blender_corpus.py`) — §4.5's sidecar repack/re-hash/rename, all four
§5.1 refusals, §5.2's warning, §5.3's divergence list, §8 in full, and §9's
all-or-nothing operator.

**§6 (the drift checker) and §7 (growth) are built too**, in
[`addons/exmateria_map/authoring.py`](../addons/exmateria_map/authoring.py) —
the depsgraph handler and its total sync, the handles with their declared-flag
records, the clamped extent fields, the four preview numbers, and the
idempotent "apply growth" commit.

The checker's live coverage rule is graded against `dump`'s own: decision 22
makes the drifted set **empty on every untouched import**, and it measures
**0 wrong of 16,346 floor tiles across all 148 arrangements**
(`tests/blender_corpus.py`). `dump` computes `base.floor_steps` from the ROM
resource and the checker recomputes the same number from the Blender mesh, so
that is two independent paths to one number, not one path run twice.

**§3/§4 (the palette gate) is built too**, in
[`addons/exmateria_map/paint.py`](../addons/exmateria_map/paint.py) — the paint
image, the one resolve code path, §3.5's lowest-index tie-break, §3.6's sticky
off-palette list, and §3.3's trigger set (face select, state change, override
change, export, plus the explicit *Apply paint* button; no timer).

**All five blocks are built.**

### §8.1's premise about `{255, 127, 0}` is wrong, and §5.2 paid for it

§8.1 calls `{255, 127, 0}` (FF FE) "a shipped out-of-grid binding, a different
thing" from the FF FF sentinel, and §5.2 therefore warns on it. Measured over
the corpus, that value is **38,975 bindings — 54% of every binding on the
disc** — and it names tile (255, 127), which **no legal grid can reach**:
decision 10 caps an axis at 18. It is the idle value a polygon carries when it
is not on the grid at all, not a binding pointing somewhere unusual.

The cost was a warning that fired on **136 of 148 arrangements** and was right
on 8. It surfaced the first time the operator ran in the GUI, as
`234 terrain binding(s) outside the 10x15 grid` with every one of the 234
reading `(255, 127, L0)`.

| population | count |
|---|---|
| flagged by a plain "outside the extent" test | 40,745 |
| …naming a tile no legal grid can hold | **40,542** (99.5%) — of which `{255,127,0}` 38,975, byte0==byte1 filler 1,093, other ≥18 474 |
| …naming a tile a legal grid could hold — the real warnings | **203**, on 8 arrangements |

`names_a_tile()` now gates the warning on decision 10's own ceiling. **Nothing
about the document changed**: `walkable` stays keyed to FF FF alone, a FF FE
binding still round-trips verbatim, and decision 9's "nothing is rewritten"
holds. Only who gets warned about changed. Whether FF FE should *also* read as
unbound on the export side is a **decision** and belongs on map #517 — it would
flip 38,975 bindings from FE to FF and break the identity, so it is not one to
take inside an instrument. A residue worth pricing there too: 149 of the
remaining 203 match #357's byte0==byte1 filler pattern, 148 of them the single
value `(17, 8, 1)` = bytes `11 11`.

Five departures from the letter, each forced by what the scene can
actually express:

- **A payload field's declared state is an explicit `<field>_declared` twin**,
  as §1 and §6.3 ask. Presence alone cannot hold §7.2's growth seed or §6.3's
  base value — a value *shown* beside a field the record does not declare.
- **A tile the document already declares gets no drift handle.** Two objects at
  one `(x, z, level)` would export two records for one tile, which schema §7.2
  refuses at build time. Decision 23's fix exists for tiles that carry no
  record, and decision 22 makes that every tile of an untouched document, so
  this only ever bites a hand-authored one. The panel reports the count
  separately rather than hiding it.
- **The drifted set is `base.floor_steps`' population exactly** (decision 15).
  A tile the artist newly covers with floor has no base step to compare
  against — `terrain` is `null`, so the base's own `height` is not in the
  document at all — and a tile that stops being covered has no current step.
  Neither is reported as drift.
- **A binding no legal grid can hold does not warn** — see the section above;
  the predicate is `names_a_tile()`, and it changes nothing the document says.
- **§4.1's re-colour leaves the UNRESOLVED pixels standing.** Read literally,
  §4.1's "the paint image is re-coloured under the incoming palette" erases
  §4.4's sticky refusals: an off-palette pixel never reached the buffer, so
  re-colouring from the buffer paints the artist's mistake away, the next
  resolve sees a colour the palette accepts, and the refusal clears itself
  without anyone fixing anything. The refused pixels are the one thing the
  re-colour has to preserve — and preserving them is also what makes the
  refusal visible, since the bad pixel stays on screen until it is repainted.

**One implementation deviation, recorded here.** §8 asks for a face BOOL
`authored` defaulting to **True**. Blender 5.2's Python API has no
per-attribute default — `mesh.attributes.new()` zero-fills and
`bpy.types.Attribute` carries no `default_value` — so the addon stores the
negation, `imported`, which zero-fills to the right answer. Semantics are §8's
exactly; the attribute is addon-internal and never enters the document (§8.4),
so the identity is unaffected. `visible_angles` is the one §8 default zero-fill
gets wrong (0 is a legal value, so the attribute cannot tell "new" from "set to
zero"); `stamp_new_faces` supplies 0x8000 at the head of every export, keyed on
`imported` and idempotent.

## 1. Scene model: the sources of truth

Export reads five kinds of scene state, all created or flagged by the addon:

- **The marker** — the named marker object (import §6) carrying one JSON custom
  property per top-level document section: `exmateria_map/base`,
  `exmateria_map/polygons`, `exmateria_map/terrain_records`,
  `exmateria_map/map_states`, `exmateria_map/carry`. Four of the five are
  import-time snapshots; `terrain_records` is the one carried section the artist
  edits — the N-panel's level-1 section (§7.4) — and export reads level-1 records
  from it (§2).
- **The mesh object** `<map>.a<arrangement>` — positions, corner `normals`, the
  `UVMap` layer, the schema-v1 face attributes with their `_shadow` twins (import
  §3), and three addon-internal face attributes that never enter the document:
  `authored`, `walkable`, `fft_ring_flipped` (§8).
- **The grid object** `<map>.a<arrangement>_grid` (present iff the document's
  `terrain_grid` is non-null; import §5) — its `size_x` / `size_z` custom
  properties are the writable target extent (§7.1).
- **Tile objects** — one flagged object per level-0 tile, drift handle or
  growth-created (§6, §7), carrying `x`, `z`, `level` and up to 20 payload
  fields, each with a declared-flag twin.
- **The images** — per distinct sheet: the index buffer (the addon's 4-byte-per-
  pixel byte array, 131,072 B; the source of truth), the index image (a Blender
  image data block, 256 × 1024, pixels 0–15; the mesh-preview source), and the
  paint image (256 × 1024 RGB under the active palette; the artist's paint
  target; §4). Per state with valid palettes: the 16 × 16 CLUT image (import
  §4) — the palette **edit surface**, and the source `map_states[].palettes` is
  re-emitted from (§2, schema §6.4). A state the document gives no palettes has
  a CLUT image fabricated from the sidecar's display-only PLTE; those pixels are
  not that state's data and are never written back.

The grid and tile objects carry addon-internal object-property flags stamped at
import / by the drift checker / by the growth commit. Export reads only flagged
objects; it never parses object names (import §5's rule, extended from tile
props to object selection).

## 2. Round trip and carry (block 1)

What export fills, per document field (schema §11's table, as amended by §2.1):

| doc field | export source |
|---|---|
| `format`, `version` | `"exmateria-map/interchange"`; `version` is `2` when any state carries an `authored_light_rig` and `1` otherwise — the oldest `build` that can read the document, tested on the FIELD rather than on whether this export declared anything |
| `base` | marker JSON verbatim, **except** `terrain_grid` ← the grid object's `size_x`/`size_z` props; `null` when no grid object (import §7's inverse read; no grid object ⇔ dump wrote `null`, schema §4) |
| `polygons` | the scene graph: `kind` ← the face `textured` flag + corner count; `positions` ← loop positions → inverse map → i16; `normals` ← corner attribute → inverse map → i16; `uv` ← `UVMap` decode; the palette/texture and `terrain`-binding face attributes (§8.1). Emitted in bucket order tt→tq→ut→uq (schema §3); a new face keeps its mesh-loop position within its bucket |
| `terrain` | level-0 records from the flagged tile objects (declared fields only; an object with no declared field produces no record, §6.3, §7.4) + level-1 records from the marker's `terrain_records` JSON. **`null` when nothing is declared — never `[]`** — else the array |
| `map_states` | marker JSON, except each buffer's `texture_sheet` ← its buffer's sidecar name (§4.5); `palettes` ← the state's 16 × 16 CLUT image, row by row, at the row's own declared length, with `stp` carried (a `null` stays `null`); `authored_light_rig` ← the state's Override **only when the artist moved something on it** (decision 25 Amendment 1) |
| `carry` | marker JSON verbatim (§2.1) |

### 2.1 Carry is written back verbatim from the marker

Superseding the `null` cell in schema §11. The two legs had disagreed: the
schema's `dump`-filled document leaves `carry` for `build` to refill from the
base's `0xB0` chunk, while the marker carries dump's `carry` object in the
scene. Export hands the marker's object back unchanged; `build` accepting
`null` **or** object is the permissive floor. The supersession is recorded here
and in #521's resolution only — the schema document is not amended.

### 2.2 Identity

`export(import(doc)) == doc` for an untouched document holds field by field:
`carry`, `base`, and `map_states` round-trip from the marker JSON (an
unchanged sheet buffer reproduces its sidecar's hash and hence its name; an
untouched CLUT image reproduces its colours byte-exactly, because schema §6.4's
8-bit expansion and the image read-back land on the same lattice; an untouched
rig declares nothing, so no `authored_light_rig` key appears);
`polygons` round-trip through the attributes import wrote — the `_shadow`
twins are the divergence watch (§5.3), not a second source; `terrain` is
`null` on both sides because an untouched document declares nothing (decision
22) and import therefore creates no tile objects.

## 3. Palette gate: index recovery (block 2)

Decision 7's exact-match gate, resolved: the 16 entries of the active palette
are the only colours export accepts for a sheet's pixels.

1. **The source of truth is the 4bpp index buffer.** Every RGB the artist sees
   on a sheet is a derived view (index → active-palette entry). Export
   recovers indices; it never keeps RGB.
2. **The gate's reference set is the single active palette** — the selected
   face's CLUT row, or the N-panel override. Never a union across states or
   palettes: a union makes the recovered index ambiguous (the same colour may
   be entry 2 in one CLUT and entry 9 in another), and the document carries one
   `palette_id` per face, not a colour→(state, index) table.
3. **Resolution is event-driven; there is no hot timer.** Resolve triggers:
   face select, state change, override change, and export. Between triggers the
   sheet is untouched.
4. **An unchanged pixel keeps its import-time index.** It is never re-resolved,
   so identity is structural, not re-derived: a pixel the artist did not paint
   round-trips its exact index no matter what the palette says.
5. **A changed pixel matching several entries takes the lowest index.**
   Duplicate entries within one 16-set are legal, so the match rule must be
   total; the lowest index is the tie-break.
6. **Off-palette is a refusal, not a warning** — per exact colour: the colour,
   the pixel count, and the bounding box (the #423 refusal shape). The refusal
   is sticky (§4.4) and export writes nothing while it is non-empty (§5.1, §9.4).

## 4. Paint surface and resolve flow (block 2)

Three artifacts per distinct sheet:

| artifact | what it is | role |
|---|---|---|
| **Index buffer** | the addon's 4-byte-per-pixel byte array (131,072 B) | the source of truth; export's input |
| **Index image** | a Blender image data block, 256 × 1024, pixels 0–15, one per distinct sheet | the mesh-preview source; state-independent; import builds it from the sidecar |
| **Paint image** | 256 × 1024 RGB, the index buffer re-coloured under the active palette | the artist's paint target; the N-panel's "paint sheet" button opens it in paint context with #423's forced brush settings |

**4.1 One resolve code path.** On each trigger (§3.3): diff the paint image
against `expand(index buffer, outgoing palette)`; differing pixels were painted
under the outgoing palette, so each is resolved against that palette's 16
entries (lowest index on duplicates, §3.5); an off-palette colour enters the
sticky refusal list (§3.6). Then the buffer is written back to the index image
and the paint image is re-coloured under the incoming palette.

**4.2 Triggers.** Face select, state change, override change, export — plus an
explicit **apply paint** button in the N-panel. No hot timer. The mesh always
previews the committed state: the index image is the preview source, and it
moves only on a successful resolve.

**4.3** — see §3.3 (the trigger set is the resolve set).

**4.4 Sticky refusals.** A refusal-list entry clears only by re-painting the
pixel to a colour the active palette accepts (a successful re-resolve drops the
pixel from the list). Export refuses while the list is non-empty.

**4.5 Sidecar writing.** The buffer is packed low-nibble-first (schema §6.5:
`byte(v·256+u pair) = even | odd<<4`, 131,072 B); the sidecar name is
`<MAP>.a<arrangement>.sheet-<sha256[:8]>.png` over the packed bytes (schema
§1); the PNG is written indexed — pixel indices = buffer, PLTE = the #366
texel-majority expansion (per-index majority colour), display-only, `build`
ignores PLTE (decision 6). Every `map_states` entry sharing the buffer gets
the new `texture_sheet` name; an unchanged buffer's hash reproduces its
imported name. Export writes its own files only and never deletes stale
sidecars: an earlier export's renamed file stays on disk, unreferenced, and a
re-export of an unchanged buffer rewrites the same file with identical content.

## 5. Refuse, warn, informational

**5.1 REFUSE — export writes nothing** (all reasons reported together, to the
operator report and the N-panel):

1. **Off-palette pixels** — the §3.6 sticky list non-empty; per exact colour:
   the colour, the count, the bounding box.
2. **Out-of-range attribute values** — `positions`/`normals` outside i16;
   `uv` outside u8; `palette_id` / `palette_byte_high_nibble` /
   `texture_byte6_high_nibble` outside 0..15; `texture_page` outside 0..3;
   `unknown_texture_value_6a` outside 0..3; `visible_angles` outside 0..65535;
   terrain-binding raws outside `x` 0..255 / `z` 0..127 / `level` 0..1;
   drift-record `height` / `slope_height` / `slope_type` outside the disc
   ranges (0..255 / 0..31 / 0..255, schema §6.3); grid `size_x` / `size_z`
   beyond the growth ceilings (§7.1), non-positive included. The report names
   the face (or tile / grid), the field, the value, and the allowed range.
3. **A drift record declaring a field other than `height` / `slope_height` /
   `slope_type`** — decision 23: the pin bytes stay unreachable.
4. **Not an interchange scene** — no marker object.

**5.2 WARN — proceed, reported to the N-panel and the operator report:**

- **Out-of-grid terrain bindings** — decision 9 verbatim: "Both ends warn;
  neither refuses, and nothing is rewritten." Non-sentinel, walkable faces
  whose signed derived pair is outside `0 <= x < size_x and 0 <= z < size_z`
  (the grid object's extent; vacuous when there is no grid object). The check
  is on the signed pair re-derived from positions, never on the wrapped
  attribute bytes — the encoding wraps (`byte0 = (z & 0x7F) << 1 | level`,
  `byte1 = x & 0xFF`), so a post-encode range test is blind (decision 9). The
  disc disqualifies a refusal: `MAP011` ships byte-identical geometry under
  two grids, and `MAP001.a0` ships the out-of-grid binding `{255, 127, 0}`.

**5.3 INFORMATIONAL — N-panel only, never blocks:**

- **The divergence list** — the per-face comparison of each shadowed attribute
  against its `_shadow` twin (import §3; decision 7's shadow mechanism). It
  answers "what has the artist changed since import" and drives the "faces
  added since import" count (§8).

## 6. Drift surface: the live checker and its handles (block 3)

**The live drift checker owns the quads; import does not.** An untouched
document has no drift by construction (decision 22: it declares nothing), so
import creates no drift quads and the checker creates and deletes them for the
lifetime of the scene.

1. **The checker is a scene-update / depsgraph handler, not a timer.** It runs
   on every scene update — a few milliseconds per arrangement, unthrottled —
   and is guarded by an addon-internal flag during import/export operator runs
   so the operators' own scene mutations do not race it.
2. **Total sync on every run.** Recompute the drifted set — level-0 floor tiles
   where `round(bottom/12)` ≠ the base step, integer-equality, the tile named
   by `build`'s own coverage rule from the geometry, never from the declared
   binding (decision 15; the base step and the base `slope_height` /
   `slope_type` pair per occupied tile come from the marker's `base` JSON,
   `floor_steps`, decisions 15, 23). Then: drifted tile with no quad → create
   it; quad over an undrifted tile → delete it; still drifted and holds a quad
   → keep it, declared fields surviving.
3. **The record lives on the handle object.** Each handle carries a declared-
   flag + value custom property per declarable field (decision 23's three:
   `height`, `slope_height`, `slope_type`; the base value shown beside each —
   decision 17's panel). Export reads the handles into `terrain` entries (§2).
   The consequences are the lifecycle: deleting a handle drops its record
   (the drift persists; the checker recreates the handle on the next update,
   all fields undeclared); drift clearing makes the checker delete the quad, so
   the record drops from the next export; one handle per drifted tile, level 0
   only.
4. **No viewport distinction between drifted and declared in v1.** The overlay
   is decision 17's translucent quad over the drifted tile, at the tile's own
   floor height, selectable (decision 23 makes it the handle); there is nothing
   to dismiss — the warning clears when the drift does. The N-panel count
   reads `N drifted, M with a declared fix`.
5. **Grid edge.** The checker is active only while the grid object is present;
   an arrangement without a terrain chunk shows no overlay, and the N-panel
   reads "no terrain grid in this arrangement." (Its terrain records are
   unwritable by `build` anyway — no `0x68` chunk to replace, so every record
   would sit outside the document's extent and refuse at build time.)

## 7. Growth (block 4)

1. **Field edit ≠ commit.** The `size_x` / `size_z` fields on the grid object's
   footprint (decision 16's surface): as they are typed, **both** ceilings
   clamp the field and the field names the one that stopped it —
   `SizeX·SizeZ ≤ 256` and `max(SizeX, SizeZ) ≤ 18` (decision 10), or the
   import-time extent via the `_shadow` twins (shrink is refused, decision
   10). The footprint quad resizes and the N-panel preview recomputes. No
   objects are created and the document is unchanged.
2. **"Apply growth" commits** (N-panel button). For every level-0 tile newly
   in the extent that lacks a tile object, it creates a `tile_<x>_<z>_L0`
   plane at `(x·28, z·28, 0)` (import §5's scale: 28 units per tile, 12 world
   Y per `height` step). The panel seeds the record from an existing record
   when one exists, else from decision 11's level-0 default (height 0,
   impassable); every declared flag starts false. Idempotent: pending =
   in-extent − has-object.
3. **The document's `terrain_grid` grows when the field is set, not at the
   button press** (§2: `terrain_grid` ← the grid object's props). The extent
   *is* the growth; the objects are authoring handles.
4. **An untouched tile object exports no record.** The record is
   `{x, z, level: 0}` + declared fields only (the per-field declared model;
   decision 23's boundary), so all declared flags false → no record, and an
   all-empty result is `terrain: null` (§2). The N-panel carries a level-1
   section for the selected tile, undeclared by default; it edits the marker's
   `terrain_records` JSON, which is export's level-1 source (§2).
5. **The fourth preview number** (decision 16: "tiles created that a file
   outside the map already names") reads the shipped snapshot table of
   external pins; the table file missing → the N-panel reads "pin table
   missing" and the number is `n/a`.

## 8. New-face defaults (block 5)

Mechanism: attribute defaults the addon creates the face with — no per-frame
pass. Import writes the loaded faces' values and overwrites the defaults; a
new face keeps its defaults until the artist changes them.

| attribute | type / domain | default | notes |
|---|---|---|---|
| `authored` | face BOOL | **True** | import explicitly sets False on loaded faces. Addon-internal, never in the document. v1 consumer: the "faces added since import" count (§5.3). Subdivide/extrude children inherit the parent's value (a loaded parent → False, inheriting its binding — decision 5) |
| `fft_ring_flipped` | face BOOL | False | import sets True only on faces its flip predicate reversed (import §8). The predicate keys on the authored normal; a zero-mean normal makes the cosine undefined, so a new face is never flipped |
| `walkable` | face BOOL | False | import initializes loaded faces `:= binding ≠ FF FF sentinel`. Addon-internal, never in the document; it shapes the exported `terrain` binding. Textured faces only |
| `normals` | corner FLOAT_VECTOR | (0, 0, 0) | "no authored normal"; zero is a legitimate corpus value (1,383 faces) |
| `visible_angles` | face INT | 32768 | 0x8000, the disc's own fill in 171,626 of 172,488 unused slots (decision 5) |
| `terrain_x` / `terrain_z` / `terrain_level` | face INT | 0 / 0 / 0 | meaningful only while `walkable` = True |
| `palette_id`, `palette_byte_high_nibble`, `texture_page`, `unknown_texture_value_6a`, `texture_byte6_high_nibble` | face INT | 0 each | |
| `unknown_untextured_0`…`_3` | face INT | 0 each | |
| `UVMap` | corner UV | (0, 0) | |

**8.1 What export writes for a new face — the terrain binding.**
`walkable` = False → export writes the `FF FF` sentinel, i.e. the document
triple `{"x": 255, "z": 127, "level": 1}` (`byte0 = (z & 0x7F) << 1 | level`
= 0xFF, `byte1 = x & 0xFF` = 0xFF — decision 9's wrapping encoding). This is
not the schema §5.2 example `{255, 127, 0}` = `FF FE` — that is a shipped
out-of-grid binding, a different thing. `walkable` = True → the raw
attributes, filled by the move re-derivation `floor(centroid/28)`, triggered
on face move and on the `walkable` False→True toggle; the attributes store
wrapped values (`x & 0xFF`, `z & 0x7F`), and the out-of-grid warning
re-derives the signed pair from positions (§5.2).

**8.2 The normal.** Verbatim corner attribute → (0, 0, 0). No fallback to a
computed normal: the document carries what it carries (decision 3: carried,
never computed), and "no authored normal" is the authored value for a new
face.

**8.3 The winding.** `fft_ring_flipped` = False → export writes the Blender
loop order through the fixed inverse position map. Deterministic and total. If
a culling side comes out wrong, the artist flips the face in Blender; the
axis-2 assertions assert the corpus import baseline and take no second path on
new faces.

**8.4 Identity.** `authored`, `walkable`, and `fft_ring_flipped` are
addon-internal and never enter the document, so `export(import(doc)) == doc`
is structurally unaffected by them. New-face normals and winding are resolved
here (the fog patch from #355), not filed.

## 9. The export operator, end-to-end

1. **Invocation and target.** One operator call exports one
   `(map, arrangement)` document. The target is the scene's interchange
   marker, found by its registered name; a scene without one is refused
   (§5.1.4). With multiple markers, the active object's marker wins; active
   object not a marker → refuse.
2. **The output directory is chosen by the operator.** The document
   `<MAP>.a<arrangement>.json` and every sidecar (§4.5) land in that one
   directory; `map_states[].texture_sheet` names are bare filenames in it
   (schema §1).
3. **Document assembly** per the §2 table. Object identification per §1's
   flags: only flagged grid/tile objects are read; names are never parsed.
4. **Gate → write order.** All §5.1 refusals are evaluated first. Zero
   refusals → write the document and its sidecars. Any refusal → write
   nothing (no partial files) and report every reason (face/field/value/
   allowed range) to the operator report and the N-panel. Warnings (§5.2) and
   the informational divergence list (§5.3) never block; they surface
   alongside the write.
5. **Idempotence.** Re-exporting an untouched scene reproduces the dump
   document's parsed form exactly (the 148/148 identity path); re-exporting
   after a repaint writes the new sidecar and leaves the old one in place.

## 10. Prototype link

The proven headless corpus round trip is
[`workspace/roundtrip426.py`](../workspace/roundtrip426.py) — import + export,
148/148 EXACT against `blender_axis_baseline.json`. Its `export()` is the
model this design formalizes, with two known deltas: its `#519`-era attribute
names (replaced by schema-v1 names at import §3), and its `walkable`
attribute — created always-False, since the document has no `walkable` field,
and written into the export dict where it is dead weight — which this design
replaces with the real `walkable` attribute and the §8.1 sentinel rule.

Promoting the prototype's `build()` / `export()` into the addon's operators,
and moving the axis assertions into `tests/blender_roundtrip.py`, is
off-map execution: no ticket on map #517 owns it.
