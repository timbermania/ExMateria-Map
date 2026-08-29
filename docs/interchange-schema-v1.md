# Interchange document — schema v1

The complete field-level contract for the interchange document (ADR-0004, decision 6):
one JSON document per `(map, arrangement)`, plus 8-bit indexed PNG sidecars for texture
sheets. `dump` writes it from a base map; the Blender addon speaks it (decision 7);
`build` is the only thing that turns it back into disc bytes.

Every field below has a **type**, a **meaning**, a **source** (the ADR-0004 decision that
pins it, `corpus` for a measured fact, or `schema` for a decision taken at this document's
level), and an **example** from `MAP001` arrangement 0 (resource `MAP001.9`, the
geometry source; its primary-mesh section sha256
`9a3204c2c005c0f239176a512e91d4b9e75578b2eb00cfbed783098b6e2af87e`).

**Discipline (decision 6).** Raw on-disc integers throughout. No enums, no names for
terrain fields (decision 11), no booleans except where the on-disc value is a bit.
The document is the only thing the addon speaks; `build` is the only thing that writes
disc bytes. Anything this document does not pin is in the [gaps](#9-gaps-and-their-tickets).

## 1. Document and sidecar files

| | |
|---|---|
| **Document name** | `<MAP>.a<arrangement>.json` — `MAP001.a0.json` (`schema`) |
| **Sidecar name** | `<MAP>.a<arrangement>.sheet-<sha256[:8]>.png`, one file per *distinct* texture sheet, deduplicated by the sheet's 131,072-byte sha256 (`schema`; decision 3 makes the sheet external art *per map state*, and 76 of 121 maps carry more than one distinct sheet in an arrangement — up to 8 — so dedup is what keeps the sidecar count sane: `MAP005` a0 has 10 sheet rows, 7 distinct) |
| **Painting name** | `<MAP>.a<arrangement>.source-<sha256[:8]>.png`, one file per *distinct* painting, deduplicated by the picture's own 786,432 RGB bytes (ADR-0186 decisions 5, 6). Present only on a **converted** map; see §7.3b |
| **Layout** | document and all sidecars in one directory; `map_states[].texture_sheet` is a bare file name in that directory (`schema`) |
| **PNG** | 256 × 1024, 8-bit indexed (decision 6; GaneshaDx `TextureResourceData.cs:15-16` — `TextureWidth = 256`, `TextureHeight = 1024`); pixel `v` is the disc row. The PLTE the exporter writes is display-only (majority-vote colours); **indices are authoritative**, `build` ignores PLTE (decision 6) |

A sheet's pixel `(u, v)` maps to sheet byte `((v * 256 + u) >> 1)`; nibble order is
low-nibble-first (pixel `(v*256+u)` even → low nibble). Corpus UV bounds: `u ≤ 252`,
`v ≤ 249` (`corpus`), consistent with 256 × 1024.

## 2. `version` and the refusal rule

| field | type | meaning | source | example |
|---|---|---|---|---|
| `format` | string | must be exactly `"exmateria-map/interchange"` | `schema` | `"exmateria-map/interchange"` |
| `version` | int | `1` or `2` — the **oldest `build` that can handle this document** | `schema` | `1` |

**Refusal rule.** `build` refuses any document whose `format` is not
`"exmateria-map/interchange"` or whose `version` is not one it accepts, before reading
anything else. A document with a higher version is refused, never guessed at: forward
compatibility is a new `version` value and a `build` change, not a reinterpretation of
this one.

**`version` is a floor, not a serial (ADR-0004 decision 27).** It names the oldest `build`
that can honour the document, so a `build` accepts every value at or below its own:

| stamped | means | who stamps it |
|---|---|---|
| `1` | nothing in this document needs a `build` newer than schema v1 | `dump`, always; the addon's export unless a state declares an authored rig |
| `2` | at least one `map_states` entry carries `authored_light_rig` (§7.1) | the addon's export, and only then |

A v1 `build` therefore **refuses** a v2 document rather than ignoring the field, which is
the whole point: §7.1 has `build` ignore the *derived* `light_rig`, so a v1 `build` handed
an authored one would emit a map that silently dropped the artist's lighting. Stamping
every document `2` was rejected — it refuses ordinary documents that have nothing new in
them.

## 3. Top-level shape

```json
{
  "format": "exmateria-map/interchange",
  "version": 1,
  "base": { … },
  "polygons": [ … ],
  "terrain": [ … ],
  "map_states": [ … ],
  "carry": { … },
  "source_art": { … }
}
```

| key | type | source |
|---|---|---|
| `base` | object | decisions 4, 5 |
| `polygons` | array (flat, all four kinds) | decision 6 |
| `terrain` | array or `null` | decisions 11, 22, 23 |
| `map_states` | array, one entry per non-pad GNS row of the arrangement | decisions 3, 4, 6 |
| `carry` | object | decisions 3, 6 |
| `source_art` | object, **absent** unless the map has been converted | ADR-0186 decisions 4, 5, 6 |

The polygon **list order is the on-disk order**: buckets in the order
`textured_triangle`, `textured_quad`, `untextured_triangle`, `untextured_quad`, and within
a bucket the disc row order. `dump` emits in that order; `build` writes the document's
order. The four u16 counts are **derived** from the list, not stored (`schema`: decision 6's
single flat list makes a `counts` object redundant and driftable — example, `MAP001.a0`:
75 / 330 / 24 / 61).

## 4. `base`

`base` is `dump`'s witness of where the document came from. `build` re-reads the base map
from these names and digests and refuses on mismatch (decision 5: "refuses a base map that
is not the one `dump` came from").

| field | type | meaning | source | example |
|---|---|---|---|---|
| `map` | string | map name, `MAP000`…`MAP125` | decision 4 | `"MAP001"` |
| `arrangement` | int 0..5 | the arrangement byte (GNS record byte 2; the corpus uses 0..5, not a boolean) | decision 4 | `0` |
| `resources` | array of `{name, sha256}` | every **non-pad** resource of the arrangement, GNS row order. `name` is the resource file name (`MAP001.8`), `sha256` of the whole resource file. `build` opens these as the base bytes for every rewrite and carry | decision 5 | `{"name": "MAP001.9", "sha256": "9a3204c2…"}` |
| `geometry_source` | string | the resource whose `0x40` section `dump` read the polygons from | decision 4 | `"MAP001.9"` |
| `geometry_digest` | string (sha256 hex) | digest of that resource's primary-mesh section (the region from the `0x40` pointer to the end of the terrain-binding block, §6) | decision 4, 5 | `"9a3204c2…"` |
| `terrain_source` | string or `null` | the resource whose `0x68` chunk `dump` read the grid from; `null` when the arrangement has no valid terrain chunk (6 of 196 arrangements) | decision 21; `schema` for the pick rule (§7.3) | `"MAP001.9"` |
| `terrain_digest` | string or `null` | digest of that chunk's 4,098-byte payload (first 2 B + both 2,048-B levels) | decision 21 | — |
| `terrain_grid` | `{"size_x": u8, "size_z": u8}` or `null` | the grid extent: the chunk's first two bytes, and — after growth — the **writable target extent** (decision 10, 16: the grown extent the document authorises) | decision 10, 21 | `{"size_x": 10, "size_z": 13}` |
| `terrain_tiles` | array of `[x, z, level, b0, b1, b2, b3, b4, b5, b6, b7]` | derived, information-bearing: **every slot of both levels** of the base's `0x68` chunk, raw and undecoded, in slot order (level 0's `size_z * size_x`, then level 1's). Level 1 begins at a fixed 2,048-byte stride, not packed after level 0. Carried so the addon can *draw* the terrain grid without the document declaring a record (ADR-0187 decision 1); `[]` when the arrangement has no valid terrain chunk. `dump` computes it; `build` ignores it | ADR-0187 decision 1, 2 | `[0, 0, 0, 3, 0, 2, 0, 0, 0, 32, 0]` |
| `floor_steps` | array of `[x, z, step, slope_height, slope_type]` | derived, information-bearing: one row per occupied tile whose floor `step` is the base mesh's `round(−max(ys) / 12)`; carried so the addon's drift panel (decision 15, 23) can show all three base values. `dump` computes it; `build` ignores it | decision 15, 23 | `[0, 0, 2, 0, 0]` |

## 5. `polygons`

One entry per polygon, flat. All `positions`/`normals` are signed 16-bit (i16, −32768…32767)
on-disc values; all uvs are u8.

### 5.1 Fields common to every polygon

| field | type | meaning | source | example |
|---|---|---|---|---|
| `kind` | one of `"textured_triangle"`, `"textured_quad"`, `"untextured_triangle"`, `"untextured_quad"` | the bucket; string, not int, because the bucket drives the 10-/12-byte property layout and the slot-table row — the only place the schema uses a string (`schema`) | decision 6 | `"textured_quad"` |
| `positions` | `[3 or 4] × [i16, i16, i16]` | vertices, disc order | decision 6 | `[[252,-96,280],[252,-144,280],[266,-96,280],[266,-144,280]]` |
| `visible_angles` | int 0..65535 | the polygon's visible-angle mask, the u16 that sits in its `0xB0` slot. New polygons default to `0x8000` (32768) — the disc's own fill in 171,626 of 172,488 unused slots | decision 5, 6 | `32768` |

### 5.2 Fields of textured polygons only

| field | type | meaning | source | example |
|---|---|---|---|---|
| `normals` | `[3 or 4] × [i16, i16, i16]` | per-vertex normals, carried verbatim (never computed — decision 3: Blender's smooth average reproduces only 75.5%) | decision 3, 6 | `[[0,0,-4096],[0,0,-4096],[799,0,-4017],[799,0,-4017]]` |
| `uv` | `[3 or 4] × [u8, u8]` | corner UVs in the property-block order (§6.1) | decision 6 | `[[128,68],[128,28],[137,68],[137,28]]` |
| `palette_id` | int 0..15 | low nibble of the CLUT word — which of the map state's 16 CLUTs this face uses (Blender-owned, decision 3) | decision 3, 6 | `0` |
| `palette_byte_high_nibble` | int 0..15 | high nibble of the same byte; unnamed on disc, carried | decision 6 | `0` |
| `texture_page` | int 0..3 | low 2 bits of byte 6 | decision 6 | `1` |
| `unknown_texture_value_6a` | int 0..3 | bits 2–3 of byte 6; unnamed on disc, carried | decision 6 | `3` |
| `texture_byte6_high_nibble` | int 0..15 | high nibble of byte 6; unnamed on disc, carried | decision 6 | `0` |
| `terrain` | `{"x": 0..255, "z": 0..127, "level": 0..1}` | the polygon's terrain binding, raw on-disc values (one u8 `x`, one byte `(z<<1)|level`). **Out-of-grid values are legal** (`MAP001.a0` ships `{"x":255,"z":127,"level":0}` on a live polygon): decision 9 — geometry may leave the grid, a binding may not; validation is warning-only on both ends, never refusal | decision 5, 6, 9 | `{"x":255,"z":127,"level":0}` |

The CLUT word's high byte (b3) and property byte 7 (b7) are constants corpus-wide —
`0x78` and `0x00` on all 73,888 textured polygons (`corpus`) — so `build` writes them and
the document does not carry them: decision 19 forbids a per-polygon byte the corpus shows
constant.

### 5.3 Fields of untextured polygons only

| field | type | meaning | source | example |
|---|---|---|---|---|
| `unknown_untextured` | `[u8, u8, u8, u8]` | the four raw property bytes of an untextured polygon | decision 6 | `[1, 0, 0, 0]` |

Example untextured quad: `positions`
`[[280,-144,294],[280,-120,308],[280,-96,294],[280,-48,308]]`, `visible_angles` `32768`.

## 6. Disc encoding (what `build` writes)

Reference layouts. All multi-byte values little-endian. These are what `rebuild366.py`
proves byte-exact over the corpus; they are pinned here so `build` and the addon agree
without either owning the other.

### 6.1 Primary-mesh section (`0x40`)

```
+0   u16 textured_triangle count
+2   u16 textured_quad count
+4   u16 untextured_triangle count
+6   u16 untextured_quad count
+8   positions: all polygons, bucket order tt→tq→ut→uq,
            tri = 3 × 6 B, quad = 4 × 6 B (i16 x,y,z each)
       normals: textured polygons only (tt→tq), same vertex counts
       textured properties: 10 B per tri, 12 B per quad:
            b0  = u0        b1  = v0
            b2  = CLUT word low byte  (b2 & 0x0F = palette_id,
                                     b2 >> 4 = palette_byte_high_nibble)
            b3  = CLUT word high byte = 0x78 (constant, encoder-written)
            b4  = u1        b5  = v1
            b6  = page|value|hi     (b6 & 3 = texture_page,
                                     (b6 >> 2) & 3 = unknown_texture_value_6a,
                                     b6 >> 4 = texture_byte6_high_nibble)
            b7  = 0x00 (corpus-wide)
            b8  = u2        b9  = v2
            b10 = u3        b11 = v3        (quads only)
       untextured properties: 4 B per polygon, ut→uq, the unknown_untextured bytes
       terrain bindings: 2 B per textured polygon (tt→tq):
            byte 0 = z*2 + level, byte 1 = x
end  (section end; `geometry_digest` is over +0…end)
```

### 6.2 Visible-angle chunk (`0xB0`, 4,096 B)

```
+0     896 B unknown header — carried, byte for byte (carry.visible_angles_unknown_896)
+896   1,600 u16 slots in fixed bucket order:
         slots [0, 512)     textured_triangle
         slots [512, 1280)  textured_quad
         slots [1280, 1344) untextured_triangle
         slots [1344, 1600) untextured_quad
```

Slot `i` of a bucket is polygon `i` of that bucket (disc order). Live slots come from
`polygons[].visible_angles`; every slot the document's counts do not describe is taken from
`carry.visible_angles_slots` at the same position (§8). Corpus live-slot values:
`32768` (403×), `39104` (9×), `34848` (7×), `36864` (6×), `34816` (6×), `38912` (6×) on
`MAP001.a0`.

**When the base has no chunk (ADR-0004 decision 26).** 10 of 169 geometry-carrying resources
carry none, and on those `carry.visible_angles_unknown_896` and `carry.visible_angles_slots`
are both `null` (§8). `build` **manufactures** a whole 4,096-B chunk when — and only when —
either trigger fires:

1. the document's polygon counts **exceed** the base's, or
2. any polygon carries a **non-`null`** `visible_angles`.

What it writes is fixed: the corpus's single 896-B header, then 1,600 slots, every one
`0x8000` except the ones the document authors. The header is a **corpus constant**, not a
carried field — `sha256 45ca29ccdb1fd2c38469be5bb07c2021e596f43cbf79228a97251f572865ec56`,
**159 of 159** chunk-carrying resources byte-identical, 879 of 896 bytes zero, the rest a
4-byte tag `12 12 34 34` and thirteen u32 `1`s. Derive it from any chunk-carrying resource and
assert the 159/159 identity; do not paste a hex literal.

Neither trigger fires on an untouched document — all nine affected arrangements dump every
mask `null` — so the identity round trip stays **1,575 / 1,575** (§10).

**It is written to the `geometry_source` and to nothing else.** Not the fan-out rule of §8:
that carries an *existing* chunk across an arrangement's mesh rows, and there is none to
carry. Eight of the nine arrangements cannot tell the two apart — the source is their only
non-texture row — but **MAP099 a0** can: it pairs the chunkless `MAP099.7` with **nine**
753-byte state resources that carry no `0x40` at all. Unscoped, `build` would stamp slot
`0xB0` of each with 753 and hang a 4,096-B chunk off a file five times smaller than the chunk.
That arrangement is where the scope is a claim about a non-empty set, and it is where the
mutation seed for it has to be graded.

The manufacture emits a **warning**, never a refusal: it names the resource, the 4,096-B
growth, and that the patcher will need `allow_relocate = true` and `[free_space].ranges`
([#522]). `build` cannot refuse on that key — it never sees the recipe. Measured: **0 of 10**
of these resources have room for 4,096 B in place, so the relocation is unconditional.

### 6.3 Terrain chunk (`0x68`, 4,098 B payload)

```
+0   SizeX (u8)
+1   SizeZ (u8)
+2   level 0: 256 records × 8 B, record at index  z*SizeX + x
+2050 level 1: 256 records × 8 B, record at index  z*SizeX + x
```

The loader (`FUN_80183ea0`) copies the 4,096 B after the size pair verbatim — RAM layout
== disc layout (decision 21). Indices `≥ SizeX*SizeZ` (the pad) are loaded and swept but
unaddressable by `tile_height_lookup`; the writer reproduces them verbatim (decisions 19,
20, 21). Some resources hold 2 unnamed bytes between the chunk and the next section
pointer (158 of 228 terrain-carrying resources); those are ordinary un-parsed bytes and
carry (`corpus`).

**Record bit layout** (GaneshaDx `TerrainTile.cs`; verified against the corpus):

| byte | bits | field |
|---|---|---|
| b0 | 7 | `unknown_0a` |
| b0 | 6 | `unknown_0b` |
| b0 | 0–5 | `surface_type` |
| b1 | 0–7 | `unknown_1` |
| b2 | 0–7 | `height` |
| b3 | 5–7 | `depth` |
| b3 | 0–4 | `slope_height` |
| b4 | 0–7 | `slope_type` |
| b5 | 7 | `unknown_5a` |
| b5 | 6 | `unknown_5b` |
| b5 | 5 | `unknown_5c` |
| b5 | 0–4 | `thickness` |
| b6 | 7 | `pass_through_only` |
| b6 | 6 | `unknown_6b` |
| b6 | 5 | `unknown_6c` |
| b6 | 4 | `unknown_6d` |
| b6 | 2–3 | `shading` |
| b6 | 1 | `impassable` |
| b6 | 0 | `unselectable` |
| b7 | 0–7 | `rotation` |

`b4` is `slope_type` and `b3 & 0x1F` is `slope_height`; `gdxterrain370.py`'s comment had
these two swapped — `edit443.py`, GaneshaDx, and decision 23's three-field set agree.

### 6.4 Palette chunk (`0x44`, 512 B)

16 CLUTs × 16 entries × 2 B. Entry `j` of CLUT `i` is a 16-bit BGR555 word at
`p + i*32 + j*2`: `R = w & 0x1F`, `G = (w >> 5) & 0x1F`, `B = (w >> 10) & 0x1F`,
STP bit = `w >> 15`.

**Colour round-trip.** `dump` expands to 8-bit: `c8 = c5 * 255 // 31`, written as
`#RRGGBB`. `build` quantises back: `c5 = (c8 * 31 + 127) // 255`. The round trip is exact
for every value `dump` produces (both are the nearest-5-bit-of-8-bit mapping); a value the
addon invents off that lattice is quantised, which is the decision-7 exact-match gate's
job to catch, not `build`'s. The STP bit never enters the colour: it rides the per-CLUT
`stp` mask (§7.1). Corpus: 1,178 STP bits set across 651 palette-carrying resources —
the mask is live data, not decoration.

### 6.5 Texture sheet (131,072 B)

4-bpp, 2 indices per byte, low nibble first (§1). The sidecar PNG's pixel indices are the
authoritative data; repacking is `byte(v*256+u pair) = even | odd<<4`.

## 7. `map_states` and `terrain`

### 7.1 `map_states`

One entry per **non-pad** GNS row of the arrangement (type-49 rows are **terminators**, out of
the document and carried whole — ADR-0004 decision 29; decision 3/4 assign every non-pad row an
owner, and a terminator has no owner because it has no content, only a repeat of the last real
record's `(lba, length)`).
Each row is a map state (arrangement × time × weather, decision 3/4), and states differ:
mesh rows carry their own 512-B palette chunks (they differ across 77 arrangements' rows —
`corpus`), and texture rows are the per-state sheets (up to 8 distinct per arrangement).

| field | type | meaning | source | example |
|---|---|---|---|---|
| `resource` | string | the resource file name (matches a `base.resources` entry) | decision 4 | `"MAP001.9"` |
| `kind` | int | the GNS type code, raw: 23 texture, 46 Initial, 47 Override, 48 Alternate | decision 4; `schema` (raw code, not a name) | `46` |
| `night` | int 0..1 | GNS record byte 3, bit 7 | decision 4 | `0` |
| `weather` | int 0..7 | GNS record byte 3, bits 4–6, raw | decision 4 | `3` |
| `palettes` | array of 16 `{colors: ["#RRGGBB" × 16], stp: 0..65535}` or `null` | the state's CLUTs when its resource has a valid `0x44` chunk (`0 < p`, `p+512 ≤ len`); `stp` bit `i` = the STP bit of entry `i` (§6.4). `null` otherwise | decision 3, 6 | `{"colors": ["#83734A","#101008","#181808","#202010", …16], "stp": 0}` |
| `texture_sheet` | string or `null` | sidecar file name when the resource is a 131,072-B sheet; `null` otherwise | decision 3, 6 | `"MAP001.a0.sheet-b57ddf71.png"` |
| `light_rig` | object or `null` | the state's 45-byte light rig at pointer `0x64`, raw; `null` when the resource carries none (every texture row, and 8 of 169 geometry-bearing resources). **Derived, information-bearing, `build` ignores it** — the same standing as `base.floor_steps` (§4). Fields: `colors` `[3] x [i16 x 3]` (per-light GTE gain, `/8`, stored PLANAR on disc — all three reds, then greens, then blues — and routinely over 255: #358 measured 24.6% of components above it, max 3,456); `directions` `[3] x [i16 x 3]` (per-light direction, interleaved, `/4096`, unnormalised, in the mesh normals' own object space); `ambient` `[u8 x 3]`; `gradient` `[u8 x 6]` (the background gradient, carried, not previewed) | decision 7 (the preview needs it); `schema` for the shape | `{"colors": [[6000,5760,4800],[400,400,1600],[0,0,0]], "directions": [[-3750,-1237,-1087],[3592,-251,1949],[0,-4096,0]], "ambient": [60,60,52], "gradient": [36,80,76,104,184,200]}` |
| `authored_light_rig` | object, or **absent** | the rig the artist authored, in the same shape as `light_rig`, which `build` **writes** to the state's resource at pointer `0x64` (ADR-0004 decision 27). **The presence of the field is the declaration** — decision 22's `terrain: null` shape — so an untouched document carries none and is byte-identical to today. `dump` never writes it; the addon's export promotes a live rig Override into it, and a document that carries one stamps `version: 2` (§2). Only a **mesh** resource that already carries the 45-byte chunk can receive it: a texture row has none *by kind*, and the 13 chunkless mesh rows have no bytes to overwrite — decision 19 forbids manufacturing them and decision 26's exception is `0xB0` alone. The 6 `gradient` bytes **echo the state's own verbatim**: the solve owns 39 bytes and carries 6, which keeps decision 25's parity boundary where it was | decision 27; `schema` for the name | `{"colors": [[6000,5760,4800], …], "directions": […], "ambient": [60,60,52], "gradient": [36,80,76,104,184,200]}` |

Example entries, `MAP001.a0`:

```json
{"resource": "MAP001.9",  "kind": 46, "night": 0, "weather": 3,
 "palettes": [ {"colors": ["#83734A","#101008","#181808","#202010","…"], "stp": 0}, "…" ],
 "texture_sheet": null}

{"resource": "MAP001.8",  "kind": 23, "night": 0, "weather": 3,
 "palettes": null, "texture_sheet": "MAP001.a0.sheet-b57ddf71.png"}
```

The GNS itself is not in the document: `build` emits the arrangement's GNS with the
original LBAs and the patcher owns placement and the `(lba, length)` fixup (the #372
patcher contract). `map_states` is the art-facing decode, not a re-encoder. The fixup reaches
the type-49 terminators too, since they echo the last real record — the patcher's rule is
*"when that record belongs to this arrangement"*, which is exactly sufficient because the echoed
resource is referenced from another arrangement in **0 of 121** maps (decision 29).

### 7.2 `terrain`

A sparse list of terrain records (`terrain: []` or `null` when the arrangement declares
none). Each record:

| field | type | meaning | source |
|---|---|---|---|
| `x` | int 0..255 | tile column (raw) | decision 11, 21 |
| `z` | int 0..127 | tile row (raw) | decision 11, 21 |
| `level` | int 0..1 | storey | decision 11 |
| + up to 20 payload fields | | `surface_type` 0..63 · `height` 0..255 · `depth` 0..7 · `slope_height` 0..31 · `slope_type` 0..255 · `thickness` 0..31 · `shading` 0..3 · `rotation` 0..255 · `unknown_1` 0..255 · the nine 0/1 bits `unknown_0a, unknown_0b, unknown_5a, unknown_5b, unknown_5c, unknown_6b, unknown_6c, unknown_6d, pass_through_only` · `impassable` 0/1 · `unselectable` 0/1 | decision 11 (8 raw integers + named `unknown_*` bits), §6.3 |

An **absent field is not zero** — it carries or defaults per the record's class:

| class (decided by `build`, against `base.terrain_grid`) | record legality | absent fields |
|---|---|---|
| **Drift-named tile** — inside the pre-growth extent, named by the drift warning (decisions 12, 15) | only `height`, `slope_height`, `slope_type` may be declared; any other declared payload field refuses (decision 23: the pin bytes stay unreachable) | carry from the base |
| **Growth-created tile** — outside the pre-growth extent, inside the document's `terrain_grid` extent | the whole record may be declared (decisions 10, 11) | level default: level 0 → height 0, impassable; level 1 → `00 00 00 00 00 00 01 00` (height 0, unselectable) — but decision 20 narrows this: growth writes nothing new, the bytes past the old edge become live as they stand, and `build` stamps a default only into a slot that already holds it (a no-op) |
| **Carried tile** — any other pre-growth tile (ADR-0187 decision 15) | refused — "that tile is still the base's" (decisions 3, 11, 12) | carry from the base, and since ADR-0187 the addon *draws* them from `base.terrain_tiles` |
| **Outside the document's extent** | refused — "there is no byte to write it to" (decision 11) | — |

Example record — the shape only. The plain tile at `(0,0)` of `MAP001.a0` (raw
`03 00 02 00 00 00 20 00`) is a **carried** tile, so this record is *illegal* and
`build` refuses it; a legal one names a drift-named or growth-created tile:

```json
{"x": 0, "z": 0, "level": 0, "surface_type": 3, "height": 2, "unknown_6c": 1}
```

### 7.3 The terrain-source pick (`schema`)

`dump` scans the arrangement's resources for **valid** `0x68` chunks (pointer in range,
4,098 B present, `SizeX, SizeZ ≥ 1`, `2 + 2*SizeX*SizeZ ≤ 4098`). The corpus holds at most
one distinct valid chunk per arrangement (texture-sheet resources carry garbage chunks —
`SizeX*SizeZ` out of range or zero — that never match). `terrain_source` is the geometry
source when it holds the valid chunk, else the holding resource (`MAP053` a0: the 9×9 grid
lives in the `Initial` row, not in the geometry source — the one arrangement where the
naive rule fails). `build` fans the written chunk out to every resource whose base payload
is byte-identical to `terrain_source`'s base payload (decision 2's correspondence
principle); every other `0x68` chunk — garbage or not — is carried untouched.

### 7.3b `source_art` — the Painting

A map carries **two pictures**: the **Sheet** (`map_states[].texture_sheet`, the
131,072-byte 4bpp resource the game reads) and the **Painting** (the artist's own
true-colour picture on the converted authoring path). ADR-0186 Amendment 3 records
which half survives on which path; this section is the Painting's schema.

```json
"source_art": {
  "MAP022.a0.source-0ea1b3c7.png": { "states": [0, 1, 2] },
  "MAP022.a0.source-9f42d10b.png": { "states": [3] }
}
```

| field | type | meaning |
|---|---|---|
| *key* | string | a bare sidecar file name in the document's directory (§1) |
| `states` | array of int | the `map_states` indices this painting is the source for, ascending |

Five properties, each a decision rather than a convenience:

- **It is absent, not empty, on an unconverted map.** ADR-0186 decision 7's shape:
  *the presence of `source_art` is the declaration*, so a document that never met
  the compile is byte-for-byte the document it always was — which is what
  `export(import(doc)) == doc` asserts over all 148 corpus arrangements.
- **It never sits in `map_states[].texture_sheet`.** `build` reads only what that
  field names, and never enumerates the document's keys, so it is blind to source
  art **by construction** rather than by a rule someone has to keep. That is checked
  rather than asserted: `tests/test_source_art.py` builds the same map with and
  without the section and compares every resource byte, with a positive control
  that puts the same name in `texture_sheet` and watches `build` refuse.
- **It does not raise the `version` floor.** §2's floor names the oldest `build`
  that can honour the document. A v1 `build` handed source art ignores it and emits
  the **right** map, because the compile has already written the sheet sidecars
  `texture_sheet` names — unlike `authored_light_rig`, which a v1 `build` would drop
  and so emit a wrong one.
- **The PNG is 8-bit TRUECOLOUR (colour type 2), not indexed.** A painting has no
  palette; that is the whole point of the path it belongs to. The two sidecar kinds
  share a directory and a `.png` suffix, so each reader refuses the other's colour
  type outright (`png_indexed.read_rgb_png` / `read_indexed_png`).
- **The Painting's SCALE is derived from its dimensions, never stored.** ADR-0186
  Amendment 10 decision 43. A painting is `256k × 1024k` for k ∈ {1, 2, 4, 8}; the
  compile shrinks it to the Sheet's 256×1024 by box average before quantising, so
  `k = 1` is the Painting as it has always been. A `scale` field would be the
  redundant, driftable copy §3 refuses for the polygon counts — the PNG already
  carries its own width and height. **All of one document's paintings must agree on
  k**: N belongs to the map, not to a state, and a document holding a 4× painting
  for one state and a 1× painting for another is incoherent. That agreement IS the
  check, which is why no field is needed to make one.

The file name's hash is over the RGB bytes, not over the PNG, so two identical
paintings share one file whatever the encoder chose — the same rule the sheets use,
where the name comes from the packed 4bpp rather than from the image.

The name also carries **`@Nx` for k > 1 only** —
`MAP022.a0.source-0ea1b3c7@4x.png`. Tagging `@1x` would change every existing key in
this section and break the whole-document `export(import(doc)) == doc` identity
asserted over all 148 arrangements, so bare means 1× and the suffix lands on
ADR-0186 decision 7's shape a third time: *absence is the declaration*. Like the
hash beside it, the suffix is a **label and not the truth** — `_build_source_art`
opens the PNG and checks the real dimensions rather than trusting the name.

**Export refuses; import warns and degrades.** A Painting whose dimensions are not a
legal `256k × 1024k`, or which disagrees with the document's other paintings, is
refused **by name** on export — never skipped silently, because decision 4 makes the
Painting the irreplaceable half of an authored map and §10's posture is to refuse
and say so. On import it warns, is skipped, and that state previews through the
CLUT: *"an import that lost a file must still open; it is the export that refuses"*
(`_build_source_art`).

## 8. `carry`

The per-resource items `build` takes from the base that are not in the document's data
(decisions 3, 6). Arrangement-level, because the corpus's sibling chunks agree:
all 16 multi-row arrangements carry byte-identical `0xB0` chunks (`corpus`) — and `build`
re-checks that, refusing otherwise (§10).

| field | type | meaning | source | example |
|---|---|---|---|---|
| `note` | string | human-facing description of what is carried; ignored by `build` | `schema` | `"light rig, grayscale set, texture/palette animations, mesh animations, GNS"` |
| `visible_angles_unknown_896` | string (1,792 hex chars) or `null` | the `0xB0` chunk's first 896 B, from `geometry_source`'s chunk; `null` when the base resource has no `0xB0` chunk | decision 6 | `"12123434010000000100000001000000…"` |
| `visible_angles_slots` | string (6,400 hex chars) or `null` | the **whole** base slot table, 1,600 u16 in disc order (§6.2). `build` overlays the document's live values onto it and keeps every slot the document does not describe — no truncation, no manufactured slot (decision 19) | decision 6, 19; whole-table shape is `schema` (the #366 prototype carried dead slots only; the full table is what makes add/remove symmetric) | `"…1110…"` (dead slots are `0x1110` on `MAP001.a0`) |

**Carried whole, out of the document entirely** (decision 3's second owner, decision 18):
the 45-byte light rig, the grayscale palette set, texture/palette animations, mesh
animations. These live *inside* the resource files; `build`'s model is "base bytes with
the named chunks replaced" (§10), so everything without a valid pointer is carried at its
offset by construction. No field, no digest, no promise — decision 18 keeps GaneshaDx the
oracle for all of it.

**The rig is carried on the WRITE side and read on the READ side, and those are different
claims.** `map_states[].light_rig` (§7.1) is `dump`'s decode of the same 45 bytes, present
so the addon can light the preview at all — decision 7 makes the preview `albedo x (ambient
+ sum(gain . max(0, N.L)))`, and the addon speaks only the interchange, so a rig that never
enters the document is a rig the preview cannot have. It changes nothing about ownership:
`build` ignores the field, writes nothing from it, and the bytes still reach the disc by
being carried at their offset. Editing it edits a picture, not a map.

## 9. Gaps and their tickets

Everything the ADR leaves under-specified that schema v1 has to live with. Each gap has a
ticket; none blocks the round-trip instrument, which works on the shipped corpus.

| gap | what is under-specified | v1 behaviour | ticket |
|---|---|---|---|
| **No `0xB0` chunk at the base** | ~~10 of 169 geometry-carrying resources have no 4,096-B `0xB0` chunk. Decisions 5–6 assume one~~ — **closed by ADR-0004 decision 26** | `build` **manufactures** one under two named triggers (§6.2); otherwise it writes nothing and the resource keeps its absent chunk. The v1 refusal is withdrawn | [#524] |
| **GNS pad rows** | ~~type-49 rows are in the arrangement but have no owner decision; they never hold art~~ — **closed by ADR-0004 decision 29**, which also corrects this entry: they are **terminators**, not arrangement rows. 1,533 of them (51.3% of every parsed GNS record; 1,454 real rows), all trailing, all echoing the last real record's `(lba, length)` — **1,533/1,533** | out of the document and out of `map_states`; the GNS is carried whole. A terminator has **no arrangement** — byte 2's `0x01` is filler, and `parse_gns` reading it as one manufactures a phantom arrangement 1 in 84 maps (recorded, not yet fixed: #517 plans, execution happens off it) | [#525] |

[#522]: https://github.com/timbermania/fft-monorepo/issues/522
[524]: https://github.com/timbermania/fft-monorepo/issues/524
[525]: https://github.com/timbermania/fft-monorepo/issues/525

## 10. `build`'s acceptance rules (the backstop)

In order:

1. **Version/format** — §2. Unknown → refuse. `version` is the oldest `build` that can
   handle the document, so this `build` accepts `1` and `2`; a document that declares an
   `authored_light_rig` (§7.1) and stamps less than `2` refuses, because a document whose
   own version says an older `build` could read it is wrong about itself.
2. **Base identity** — `base.map`, `base.arrangement`, every `base.resources[].sha256`,
   `geometry_digest`, `terrain_digest` against the base map as found on disk (decision 5).
   Mismatch → refuse.
3. **Pointers** — assert every section pointer of every resource lands inside the file
   (decision 22: "a pointer at or past EOF is wrong on its own terms"). Invalid chunk
   pointers (§7.3's validity test) mean the chunk is absent, not the file is corrupt.
4. **Polygon capacity** — per bucket, `document[b] + base_anim[b]` beyond the **engine's
   array** refuses (ADR-0004 decision 28). The bound is **360 / 710 / 64 / 256**, not the slot
   table's 512 / 768 / 64 / 256, and `base_anim[b]` is the base resource's `AnimatedMesh1`–`8`
   counts summed — the loader's four destination cursors are shared across the primary mesh and
   all eight animated sections and are never bound-checked, so the **sum** is what overflows.
   Above the corpus maximum (350 / 683 / 58 / 241) and at or below the bound, `build` **warns**:
   `tt` 351–360, `tq` 684–710, `ut` 59–64, `uq` 242–256 is ground the disc never tested. Real
   headroom on a resource with no animated meshes is **10 / 27 / 6 / 15**; the tightest in the
   corpus is `MAP103.10`, whose 140 animated textured triangles leave the primary mesh **220**.

   > **Corrigendum.** This rule originally clamped at the slot table — *"a document bucket
   > count beyond its capacity (512 / 768 / 64 / 256, §6.2) refuses: `build` cannot describe a
   > polygon with no slot"* — and quoted headroom 162 / 95 / 6 / 15. The reason was wrong and so
   > were two of the four numbers. §6.2 is **not** amended: the table really does carry 1,600
   > slots and case `0x2c` really does walk all of them. What it lacks is an array behind slots
   > 360–511 and 710–767, which the engine reads and discards. Nothing shipped is affected —
   > **0 of 169** geometry-carrying resources exceed the bound on the sum, so `build_corpus`
   > stays 148/148 EXACT `refused=0` and the identity round trip stays 1,575/1,575.
5. **Terrain records** — classified per §7.2; a mis-classed record (drift tile declaring a
   pin byte; any record outside the document's extent) refuses.
6. **Fan-out correspondence** — every `0x40`-carrying resource in the arrangement whose
   section digest differs from `geometry_digest`, or every `0xB0`/`0x68` payload that is
   not the carried/identical one, refuses: `build` "never silently rewrites" (decision 2).
   The corpus is 10/10 mesh, 16/16 `0xB0`, and one-valid-chunk-per-arrangement on `0x68`
   (`corpus`). A manufactured `0xB0` (§6.2) can never trip this: each of
   the nine affected arrangements has exactly **one** geometry-carrying mesh row, and it is
   the chunkless one, so there is no sibling chunk to disagree with. (`MAP083` a0 is *not*
   affected — its `geometry_source` `MAP083.9` carries a chunk; the chunkless `MAP083.10` is
   a non-source sibling `build` never writes a `0xB0` to.)

**Warnings, never refusals:** out-of-grid `terrain` bindings on polygons (decision 9); the
`floor(centroid/28)` drift check, with #357's nine unexplained files as a named suppression
list (decision 5). The identity round trip — `dump → build → cmp` on an untouched document
— must complete with zero bytes changed; that is the instrument's coverage axis (decision 8).

> **Correction (built).** This paragraph originally also required *zero warnings* on the
> identity round trip. It is empirically false, in the same way §8.1 of export-v1 was:
> **8 of 148 arrangements ship a terrain binding that names a tile a legal grid could
> hold and that this arrangement's grid does not cover** — MAP000 a0, MAP011 a1/a3,
> MAP024 a0, MAP093 a0, MAP097 a0, MAP098 a0, MAP118 a0. Decision 9 says both ends warn
> and neither refuses, so the alternatives were "warn and fail the clause" or "keep the
> clause and go silent on the very arrangement decision 9 cites" (MAP011). The clause
> loses. `build` warns on exactly the population `export_document.names_a_tile` warns on,
> and the two legs report the same 8 — which is itself the check that they still agree.

## 11. What `dump` fills, what export leaves, what `build` fills

| data | `dump` (from base) | addon export (authored) | `build` |
|---|---|---|---|
| `polygons` (every field) | all | all (face attributes, decision 5/7) | writes §6.1; constants `0x78`/`0x00` come from the encoder |
| `terrain` | `null` — an untouched document declares nothing (decision 22) | growth records + drift fixes only | merges per §7.2 |
| `map_states` palettes | all | all (the decision-7 exact-match gate lives here) | writes §6.4 into every valid-`0x44` resource |
| `map_states` texture sheets | all sidecars | repainted sheets | repacks §6.5 into every 131,072-B resource |
| `base.*` | all | unchanged | verifies (§10.2) |
| `carry.*` | from `geometry_source` | `null` — the addon's export path leaves `carry` `null`; `build` refills it from the base's `0xB0` chunk (no new polygons is the normal path) | fills, asserts sibling identity (§10.6) |
| light rig (derived) | `map_states[].light_rig`, read-only (§7.1) | unchanged — handed back verbatim | ignores the field; the bytes are carried at offset by construction (§8) |
| light rig (authored) | never — `dump` declares nothing | `map_states[].authored_light_rig`, promoted from a live rig Override, on the states that can receive bytes; `version: 2` when any does | writes the 45 bytes at pointer `0x64` of that state's resource (decision 27); refuses a texture row, a chunkless mesh row, and a `gradient` that is not the base's own |
| grayscale, animations | not in document | not in document | carried at offset by construction (§8) |

`build`'s per-resource assembly, in one line: **new bytes = base bytes, with the
`0x40` section replaced on fan-out targets, the `0x44` chunk replaced on every
valid-`0x44` resource (from that resource's own `map_states` entry), the `0x68` payload
replaced on `terrain_source` and its byte-identical copies, the `0xB0` chunk rebuilt on
`0xB0`-carrying resources, and every other byte untouched.** Resource *lengths* may change
(the mesh section grows); the patcher owns placement, `allow_relocate=False` is the
requestable guarantee (decision 2).

## 12. Corpus invariants the schema relies on (measured, 121 maps / 1,575 resources)

- 796 mesh resources; 169 carry geometry; 111 arrangements have >1 mesh resource; the 10
  arrangements with >1 geometry-carrying row hold byte-identical `0x40` sections.
- 148 arrangements carry geometry; time/weather never does (decision 4).
- All 658 texture files are exactly 131,072 B (decision 2).
- 651 resources carry a valid `0x44` palette chunk; palettes differ across states in 77
  arrangements (per-state, §7.1). 1,178 STP bits set corpus-wide.
- 228 resources carry a valid `0x68` chunk; 227 carry a garbage one in a texture resource;
  one distinct valid chunk per arrangement; 158 have 2 unnamed trailing bytes.
- `0xB0`: 4,096 B; 10 geometry-carrying resources lack it (§9); all 16 multi-row
  arrangements' chunks byte-identical.
- 73,888 terrain bindings; raw ranges `x ≤ 255`, `z ≤ 127` (7-bit), `level ≤ 1`;
  out-of-grid bindings ship on live polygons (`MAP001.a0`: `x=255, z=127`).
- Property byte 3 = `0x78` and byte 7 = `0x00` on 73,888/73,888 textured polygons —
  encoder constants, not document fields; byte 6's mid bits are `3` on 99.58% but take 2
  and 0 on 182 polygons, and its high nibble is `1` on 78 — carried, not assumed.
- Slot capacities vs corpus maxima: 512/350, 768/683, 64/58, 256/241.
- MAP125 arr0 is 17×16 = 272 tiles: no pad at all (decision 16); the largest grid.

## 13. What is built (2026-08-25)

Both legs ship in the package: `exmateria_map/dump.py` and `exmateria_map/build.py`,
over `mapfile.py` (the reader both share), `document.py` (the codecs both share),
`geometry.py` (the floor-coverage and drift rule `build` recomputes) and
`png_indexed.py` (byte-identical to the addon's copy, guarded by a test — the addon
writes the sidecars and `build` reads them).

**Measured.** `dump → build → cmp` over the whole corpus: **1,575 / 1,575 files
byte-identical**, 148 arrangements, 0 refusals, 8 warned (above). The carry ratchet
falls from 100% to **3.79%** — `PrimaryMesh` 4,757,178 → 200 carried bytes,
`PolygonRenderProperties` 724,992 → 28,672, `TexturePalettes` → 9,216, and the texture
class → 2.74%. What is left is the **49 arrangements that carry no `0x40` chunk**: they
have no `geometry_source`, so they have no document, and their 83 resources carry. The
instrument names them rather than dropping them.

**The authored light rig (§7.1, ADR-0004 decision 27).** `build` writes the 45
bytes at pointer `0x64` of the state's own resource; `mapfile.pack_light_rig` is
the reader's exact inverse on **776 of 776** rig-bearing mesh resources across
the corpus. The write is graded end to end by `tests/blender_authored_rig.py` —
`dump` → the real import operator → edit the state's exposed rig → the real
export operator → `build` → the bytes — 21 checks: declared on the writable
state and no other, `version: 2`, the gradient echoed from the state (not from
the Override, which the harness deliberately pushes off it so the check is not
inert), ambient and the gains byte-exact, **only that resource and only those 45
bytes moved**, a direction inside 0.05° of the aim, and
`export(import(v2 doc)) == v2 doc` so reopening a saved map does not lose the
artist's lighting. An **authored** rig on a state
that can hold no rig — a texture row, or a mesh row whose `0x64` is zero — is
**warned about and left preview-only**, never refused: the rig is exposed on
borrowing states deliberately, so refusing would turn an ordinary preview action
into a failed export. Exposure alone never warns; only an edit does.

**End to end, on the acceptance map.** `dump` MAP022 a0 → the addon's real import
operator → the addon's real export operator → `build` → 20 resources + the GNS, all
byte-identical to the disc; the bundle then passes all six of the patcher's
verifications against the retail ISO (`fft_iso_patcher.assets.map.resolve_map`, 668
byte patches).

### Four places the workspace scaffold diverged from this document

`workspace/schemav1.py` was the scaffold that stood in for `dump` while the addon was
built. Graduating it into the package found four divergences; the package follows this
document, and the addon's corpus harness is green on the corrected shape.

| field | scaffold | this document | measured |
|---|---|---|---|
| `carry.visible_angles_slots` | a list of ints, dead slots read one **byte** at a time | 6,400 hex chars, the whole 1,600-slot table | wrong at **1,110 of 1,600** slots on `MAP001.a0` alone |
| `base.terrain_digest` | over `2 + 2*SizeX*SizeZ` bytes | over the **4,098-byte** payload (§4) | differs on 146 of 148 arrangements |
| `base.resources` | every resource of the **map** | the **arrangement's** non-pad resources (§4) | 40 vs 21 on `MAP001.a0`; differs on 64 arrangements |
| `map_states` | included type-49 **pad** rows | non-pad rows only (§7.1, #525) | 52 vs 20 on `MAP002.a1`; 20 arrangements affected |

`map_states[].kind`, `night` and `weather` were also names and booleans in the scaffold
(`"texture"`, `False`, `"none"`); §7.1 pins them as the raw integers `23`, `0`, `0`.
