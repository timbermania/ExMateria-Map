# The live link — pushing a whole map from Blender into a running battle

The artist edits a map in Blender and sees it in PCSX-Redux a moment later, without an
ISO, a reboot, or a walk back to the map. Not a normals rig and not a texture rig: **the
document**, every field of it that the engine reads.

Two legs of this already exist and are proven — `tools/live_geometry.py` pushes vertex
positions by direct RAM poke, `tools/live_push.py` pushes a texture sheet by savestate
round trip. This document is the design that generalises them to the whole document and
moves them into the addon, settled by grilling on 2026-08-25. The decisions below are the
design; each one's *reason* is the part worth keeping. Six were settled by that grilling;
decision 7 is the button's, and was settled in the building.

Evidence markers are the repo's: **[LIVE]** measured in a running emulator, **[STATIC]**
read out of the Ghidra exports and not yet confirmed against hardware. Every address is
cited as an address, never as a line of a regenerated disassembly.

## 0. What is built (2026-08-26, ticket #587)

The `bpy`-free core is `addons/exmateria_map/live_link.py` — ADR-0005 decisions 1–3, one
copy, no identity guard. It holds the descriptor block, the sink addresses, the write plan,
the gate and a small `urllib` Lua-over-HTTP client. Its assertions run under plain `pytest`
(`tests/test_live_link.py`); the emulator-gated ones are `tests/live_normals_audit.py`,
**23/23**, every check seeded with the defect it catches.

**The loop no longer leaves Blender.** `live_link_ui.py`'s *Push to PCSX* button assembles
the document in memory and hands it to the core — no file, no CLI, ~0.9 s for the whole of
MAP022 a0 (454 polygons, six bucket/field plans, self-check included). Decision 7 records
what it compares against and why. Graded by `tests/blender_live_push.py` (**29/29**,
headless, a fake emulator that really parses the wire form), and accepted the only way a
rendering change can be: A/B/A on a live Gariland battle, through the button in both
directions — 0 changed bytes untouched, 1,676 waved, 1,676 back.

| leg | state |
|---|---|
| positions | **built**, `tools/live_geometry.py`, 35/35 [LIVE] |
| texture sheet | **built and AIMED**, `tools/live_push.py` [LIVE] — decision 9 |
| descriptor block + gate | **built** [LIVE] — §2.1 |
| normals | **built** [LIVE] — §3 |
| the button | **built** [LIVE] — decision 7 |
| one copy (ADR-0005 dec. 3) | **done for the geometry leg** — see below |
| UV / CLUT / TPAGE | **built** [LIVE] — §5.2, both packet buffers |
| light rig | **built** [LIVE] — §2.2, A/B/A on a live battle |
| polygon COUNTS (shrink + growth) | **built** [LIVE] — decision 8, #598, A/B/A on a live battle. Both growth GATES are fake-RAM only |
| bytes 6-7 (binding + VISIBLE_ANGLES) | **built** — 1,816/1,816 against a captured Gariland RAM |
| terrain grid | not built — §5 |
| `polygons[].unknown_untextured` | not built — no located sink, its own ticket |

`tools/live_geometry.py` is a CLI over the core now: `plan_at` builds the per-vertex writes,
`apply` does them, `LuaClient` is the transport, and its own `VERTEX_STRIDE` / `COORD_BYTES`
/ `RAM_BASE` / `blob` / `push` are gone. Its **needle search stays** — it answers *is the
declared map the loaded one*, which the descriptor gate deliberately does not claim and
decision 1 still asks — and it is the core's own write plan laid end to end, so the search
and the write cannot disagree about what a vertex is. `tests/live_geometry_audit.py` is
**35/35** across the move, with the stride seed still going red (6,729 bytes). That seed had
to move too: it patched `live_geometry.VERTEX_STRIDE`, which the module no longer reads, so
leaving it would have been an INERT seed reading exactly like a blind check.

**The sheet push aims** (decision 9, fixes 3 and 4). `--night` / `--weather` resolve through
`live_link.aim`, so "aimed at" means the same thing here as in the addon, and the tool can
name the rig row, the palette row and the kind it landed on. The locate no longer anchors on
the aimed state's blob: **every** TEXTURE row of the map contributes two anchors — its
sidecar and its disc blob — and `vram_swap_sheet.identify` names which one VRAM actually
holds. *Locate by what is there, write what you aim at.* Graded by `tests/test_live_push.py`
(9, `bpy`-free), whose savestates are synthetic because Gariland boots into one state and a
cross-state aim cannot be staged on it at all.

**That gap is closed (2026-08-26).** It used to say *"aim within one group until leg 3
lands, or expect garbage rather than a stale picture"* — a cross-group aim moved the sheet
without the CLUTs, half of decision 9's `(texture_sheet, palettes)` atom, and 559 of the
corpus's 774 groups carry palettes of their own. The warning is deleted rather than softened
because **its reason is gone**: the button now pushes both halves, planning each before
applying either, so an aim that cannot resolve one does not move the other. Neither field is
in `UNPUSHED` any more.

`tools/live_push.py` and `tools/vram_swap_sheet.py` are **deleted**. ADR-0005 wanted the VRAM
leg *ported* into the addon; what actually happened is that the thing to port turned out not
to be needed. The savestate round trip existed solely because two docstrings held that this
fork cannot write VRAM, and **that was false** — `POST /api/v1/gpu/vram/raw` writes perfectly
well, and a bare POST is a 400 only because the rectangle belongs in the query string.
Measured [LIVE] by A/B/A on a Gariland battle, 2026-08-26. What survived the move is the
geometry (`locate`, `identify`, `diff`, the page stride and row pitch), which was always
about VRAM; the savestate was only ever the container it was read through. The origin drift,
the search window, the live cache and the size-settling poll went with the container.

One thing the button settled for free. Every earlier proof pushed a document from `dump`;
the button pushes one **assembled out of Blender**, so `export(import(doc)) == doc` — 148/148
on the corpus, but never against the live rig — now has a live arm: an untouched import of
MAP022 a0, pressed once, changes **0 of 10,644 coordinate bytes** across all four buckets.

---

## 1. The finding that made this cheap: shading is per frame, from the normals

The prior reading of this seam was that FFT resolves lighting **at map load** into the
polygons' GPU primitive colour bytes, and therefore that writing a new normal into RAM
would change nothing on screen until something re-lit the map. That reading was wrong,
and the disassembly says so plainly.

Four map-polygon renderers are called **every frame** from `combat_effect_color_dispatch`
(`0x800E840C`) — a fact `research/working_documents/MAP_ILLUMINATION_APPLIER_HOLY_E015.md`
§11.1 already recorded for a different purpose. Reading `FUN_8012cc54` (`0x8012CC54`, the
textured-triangle renderer), the per-polygon body is:

- `copFunction(2, 0x480012)` per vertex — RTPS, the perspective transform, reading the
  **position** array;
- six words loaded into GTE registers from a **second, parallel array** — three SVECTORs
  at 8 bytes each, one per vertex;
- `copFunction(2, 0xd80420)` — low six bits `0x20`, **NCT (Normal Color Triple)**: three
  normals in, three Gouraud vertex colours out.

So the engine re-lights every textured polygon from its normals on every frame. Nothing
is baked. **A poked normal lands on the next frame, exactly as a poked position does.**
**[LIVE]** — zeroing all 12,128 normal bytes of MAP022 a0 darkens the whole map on the
next frame (mean framebuffer luminance 11.08 → 7.11 of 31) and restoring them verbatim
brings it back to 11.08. A/B/A, `tests/live_normals_audit.py` §7.

The load-time write that prompted the older reading is real but is a different path.
`FUN_800f4dd4` writes the polygon render fields at load, and the `0xB0` chunk's sole
consumer — case `0x2c` of `map_palette_texture_dispatch` (`0x800F26BC`) — writes `0x80`
into the primitive colour bytes of the **untextured** class. That class is unlit by
construction: `FUN_8012d2b4` / `FUN_8012d568` take their single flat colour from
`DAT_800f5b58`, the additive brightness overlay §3 of the illumination document describes.
It was never the lighting path. The two conclusions are compatible; only the inference
from one to the other was wrong.

## 2. The sinks, and where they are

The render dispatch sets every array pointer from a **static base** each frame. The bases
are not per-session allocations, which is why the same four addresses recur across
sessions:

| bucket | positions | stride | normals | primitive packet |
|---|---|---|---|---|
| textured triangle | `0x8011A2D8` | `0x18` | `0x801251D4` | `DAT_8011a2d4 + i*0x28` — POLY_GT3 |
| textured quad | `0x8011C498` | `0x20` | `0x80127394` | `DAT_8011a2d4 + 0x3840 + i*0x34` — POLY_GT4 |
| untextured triangle | `0x80122004` | `0x18` | — none, unlit | `DAT_8011a2d4 + 0xC878 + i*0x14` — POLY_G3 |
| untextured quad | `0x80122604` | `0x20` | — none, unlit | `DAT_8011a2d4 + 0xCD78 + i*0x18` — POLY_G4 |

The pointer globals the renderers actually read are `DAT_800fa6b8` / `DAT_800fbdf8`
(textured triangle positions / normals), `DAT_800fa6bc` / `DAT_800fbdfc` (textured quad),
and `DAT_800f7a58` / `DAT_800f7a5c` (the two untextured position arrays); per-bucket
counts are `DAT_800f5b84`, `88`, `8c`, `90`. All ten read exactly what this table
predicts, confirmed live on 2026-08-26. [LIVE]

> **Correction, 2026-08-26.** This paragraph used to end *"reading the global is strictly
> better than hardcoding the base — it survives whatever the index selects."* That is true
> of what the renderer **reads** and false of what anything should **write**: all ten
> globals are recomputed from scratch every frame, immediately before the four renderer
> calls, so a poke into one lasts less than a frame. An implementation built on the old
> sentence would poke the counts, see nothing change, and lose a day. What is authoritative
> is the block they are recomputed *from* — §2.1.

### 2.1 The descriptor block is the thing to write

Base `0x800FBE00`, stride `0x98`, **9 entries** — the primary mesh and the eight
animated-mesh instances:

```
0x800FBE00  primary mesh
0x800FBE98  0x800FBF30  0x800FBFC8  0x800FC060
0x800FC0F8  0x800FC190  0x800FC228  0x800FC2C0     <- the 8 animated-mesh instances
```

Fields, as offsets **within** a descriptor, all `ushort`, both runs in `BUCKETS` order:

| offset | meaning |
|---|---|
| `+0x88` … `+0x8E` | textured-triangle / textured-quad / untextured-triangle / untextured-quad **start index** |
| `+0x90` … `+0x96` | the same four **counts** |

Each frame the dispatch does, per bucket:

```
DAT_800f5b84 = descriptor[+0x90]                             // the count
DAT_800fa6b8 = 0x8011A2D8 + descriptor[+0x88] * 0x18         // positions
DAT_800fbdf8 = 0x801251D4 + descriptor[+0x88] * 0x18         // normals
renderer(..., DAT_8011a2d4 + descriptor[+0x88] * 0x28, ...)  // GT3 packets
```

**One start index governs positions, normals and packets together**, each with its own
stride — a strong self-check on the reading, and it holds for all four buckets.

Confirmed **[LIVE]**, one call: descriptor 0 in the Gariland battle reads
`24 / 361 / 18 / 51`, exactly MAP022 a0's polygon counts, and all eight animated-mesh
descriptors read zero. The stride `0x98` is confirmed two further ways — the loader
`FUN_800f4dd4` is called at exactly those nine addresses, and the dispatch's own instance
loop starts at `iVar8 = 0x98`.

Two consequences for *"push the whole map over it"*:

1. **The four arrays are shared and sliced.** Polygon `i` of the primary mesh is at
   `base + (start + i) * stride`, not `base + i * stride`. Gariland has no animated
   meshes, so all its start indices are 0 and the two forms agree — which means a rig
   that ignores `start` looks correct on the map everything is tested against and
   silently stops being correct elsewhere.
2. **Changing polygon counts means writing `+0x90..+0x96`** and re-deriving every
   following instance's start index. The rig does not: it refuses a document whose
   per-bucket counts differ from what is loaded. Growing a mesh is `build`'s job.

**All four position bases match, exactly, what `live_geometry.py` measured live** —
`0x8011A2D8` / `0x8011C498` / `0x80122004` / `0x80122604`, 35/35 in the audit, 0 mismatches
of 10,644 coordinate bytes. The search that found them was rediscovering a static address.
That is not wasted work and the search is not retired: see §5. [LIVE]

Two arithmetic self-checks fall out and both hold. The textured-triangle region is
`0x8011C498 − 0x8011A2D8 = 0x21C0 = 360 × 24`, and the normal region is
`0x80127394 − 0x801251D4 = 0x21C0` — the same size, matching the engine's 360-triangle
bound (ADR-0004 decision 28). And the quad normal array's end,
`0x80127394 + 710 × 32 = 0x8012CC54`, is **exactly the address of `FUN_8012cc54`** — the
array runs up to the first byte of code. A wrong stride or a wrong bound would not land
there. Both relations, and the 64-triangle spacing of the untextured pair, are asserted in
`tests/test_live_link.py` — they are what makes six measured constants checkable without an
emulator. [LIVE]

**The light rig's DIRECTIONS are per frame too.** Immediately before the four renderers, the
dispatch composes the rig's light-direction matrix at `0x800F5B14` with the camera rotation
(`0x800FBDD4`) into `0x800F7E34` and loads that as the GTE light matrix (`0x8001D0D8` =
`SetLightMatrix`, the neighbour of `SetRotMatrix` at `0x8001D0A8`). So a poke at
`0x800F5B14` re-lights the map on the next frame. **[LIVE]** — see §2.2, which also has the
gains and the ambient, and the reason they behave nothing like this.

**UV, CLUT and TPAGE stick.** They live in the GPU primitive packets, are written once at
load by **`FUN_800f5578`**, and are never rewritten — the per-frame renderer touches only
the three screen XYs and the three vertex colours NCT produced. So they are pokeable *and*
persistent, which is what makes a texture-page or palette edit hold. Measured: a write is
byte-identical a second later. **[LIVE]**

> **Correction, 2026-08-25.** This paragraph credited `FUN_800f4dd4`. That function is the
> POSITION loader — it fills the four coordinate arrays and the descriptor block's
> `+0x88..+0x96` and touches no packet field. The packet writer is `FUN_800f5578`, and
> there are **two** packet buffers, not one — see §5.2.

### 2.2 The light rig: three data, three GTE registers, ONE of which reloads per frame

**[LIVE], 2026-08-26.** Decision 9 left the gains and ambient *"view-independent and
therefore loaded from somewhere this document has not yet located"*. They are located, every
byte was **predicted from the disc before it was read**, and the answer changes what a rig
push has to be.

| datum | on disc | in RAM | GTE control | reloaded |
|---|---|---|---|---|
| gains | `pack_light_rig()[0:18]`, planar | `0x800F5AF4`, 18 B **verbatim** | LCM, `cnt16-20` | at map load; **no re-load observed** |
| directions | `pack_light_rig()[18:36]`, interleaved | `0x800F5B14`, 18 B **verbatim** | LLM, `cnt8-12` | **every frame** |
| ambient | `pack_light_rig()[36:39]`, 3 x u8 | `0x800F5B40`, 3 x **`int32`** | BK, `cnt13-15`, value **x16** | at map load; **no re-load observed** |

**The row-vs-column question `plan_rig` was waiting on is answered by the file itself.**
`read_light_rig` regroups the disc's planar colours into per-light RGB triples for the
document; RAM holds the **file's** bytes, planar, untouched. So the write is
`pack_light_rig(rig)` sliced, not a transposition of `light_rig["colors"]` — and the two are
not the same nine numbers in a different order, they are the same *bytes*. Measured:
`RAM[0x800F5AF4:+18] == pack_light_rig(day)[0:18]`, byte for byte, and the ambient triple
read back as `[49, 54, 56]` against the file's `+36..38`.

The libgte stub family the leads guessed at decodes cleanly out of `scus_disassembly.txt`,
five functions spaced `0x30` apart, and it confirms both leads **statically** as well:
`0x8001D0A8` `SetRotMatrix` (`cnt0-4`), `0x8001D0D8` `SetLightMatrix` (`cnt8-12`),
**`0x8001D108` `SetColorMatrix`** (`cnt16-20`), `0x8001D138` `SetTransMatrix` (`cnt5-7`),
**`0x8001D168` `SetBackColor`** — `sll a0,a0,4` / `sll a1,a1,4` / `sll a2,a2,4` then
`ctc2` into `cnt13-15`, which *is* the `x16` measured on the live machine.

#### A RAM-only rig push renders ONE THIRD of a rig

This is the finding that costs something. The direction matrix is recomposed with the camera
rotation (`0x800FBDD4`) into `0x800F7E34` and re-loaded into the GTE **every frame**, so a
poke at `0x800F5B14` lands on the next one — measured, the compose followed the poke within
a frame, and the picture moved **90,859** subpixels. The gains and the ambient are loaded at
map load and **were not seen to re-load** — a weaker statement than "once", and the
difference is load-bearing: see the scope note below. Poking every RAM copy of the gains
moves nothing:

| poked | subpixels changed | verdict |
|---|---|---|
| `0x800F5AF4` (the source) | 7,651 | noise |
| `0x800F6A7C` | 5,856 | noise |
| `0x800F79C4` | 7,716 | noise |
| `0x80174018` | 1,147 | noise |
| **GTE `cnt16-20` direct** | **91,388** | the map turns red |
| **GTE `cnt13-15` direct** | **90,454** | the map's unlit faces turn red |

against a **6,546**-subpixel noise floor measured on four untouched frames of the same scene.
So **the rig atom needs a second transport**: `apply`'s RAM write plan reaches the directions
and nothing else, and the addon's core has to write the GTE control registers as well. That
is not a re-opening of decision 9's atom — it is the atom's own argument arriving at a wider
answer than the decision could see. *A rig belonging to no real state* is exactly what a
RAM-only push produces: **this state's angles over the last-loaded state's brightness.**

The register write survives ordinary play: a red LCM held across **seven seconds and a
dialogue transition** driven by a real X press, so nothing in this scene re-loads it. What is
**not** measured is a spell or an attack animation, either of which may set its own colour
matrix; assume the push is transient until that is tested.

**Accepted the only way a rendering change can be.** MAP022 a0's *night* rig
(`MAP022.31`) pushed onto the running *day* battle, RAM and registers together: day -> night
**89,004** subpixels, night -> day **88,798**, and day -> day **2,799**, under the floor.
A/B/A, and the map is visibly night.

#### Two things that are NOT a second matrix, and one label to amend

`0x800F5B08..0x800F5B13` looks like a second copy of the directions and is not one. The
loader bulk-copies the file's first 32 bytes to `0x800F5AF4`, then zeroes the PSX `MATRIX`
struct's 2-byte pad at `0x800F5B06` — which clobbers file bytes 18-19 — and writes the real
18-byte direction block at `0x800F5B14`. What is left between them is the tail of the bulk
copy. Reading it as data would put the direction matrix two bytes early.

The label conflict at `0x800F5B40` **reconciles; neither side has to be wrong about its own
half.** `fft-ghidra`'s `renames_high_scenario.tsv:82` calls it `screen_tint_quad_rgb`,
written by `screen_tint_render_dispatch` case `0x5a`. At rest in a battle it holds the map
file's ambient triple verbatim and feeds `cnt13-15`. Both are writers of one address whose
meaning is *the GTE background colour* — which is exactly how a screen tint would be
implemented. The part of the label that is **wrong** is the consumer: `SUB_8001D168` is
`SetBackColor`, not a quad builder, and the disassembly above is the proof. Amending that
line belongs to `fft-ghidra`, not here.

#### What of this section is GRADED, and what is only recorded

This matters more than it looks. Everything above reads with one voice, and it is three
different kinds of claim — a reader cannot tell which lines have a guard behind them unless
the section says so.

**Graded on every commit, with no emulator.** The savestates in `reference-assets/` are
uncompressed, so main RAM is in the file and `test_live_link.py`'s `gariland_ram` fixture
pins it by *verifying* — the descriptor fixture occurs exactly once. On that:
`test_the_rig_plan_is_what_a_running_battle_HOLDS` asserts all three addresses and their
byte layouts; `test_the_rig_the_battle_holds_is_the_DISCS_rig` asserts the same RAM against
`mapfile.pack_light_rig` with the addon's copy out of the loop, so the two copies of the
planar packing are each pinned to the engine as well as to each other; and
`test_the_stale_bytes_before_the_direction_matrix_are_NOT_a_second_one` pins the bulk-copy
tail. The GTE packing and the aim are 20 arms in `blender_live_push.py`.

**Measured once, on a live machine, and graded by nothing.** The reload asymmetry itself —
that `cnt8-12` re-loads every frame while `cnt16-20` and `cnt13-15` do not — every subpixel
count in this section, and the A/B/A. They are session observations, reproducible by the
method in the trap note below but not by `pytest`. Treat a change in the engine's behaviour
here as undetected until someone looks again.

**Not measured at all.** Whether a spell or an attack animation sets its own colour matrix.
Deliberately left flagged rather than half-measured: reaching an attack needs real pad
driving, and a flag that reads as answered is worse than an open one.

**Two scope words in this section are load-bearing, so they are spelled out.** *"Loaded
once"* was the original wording and it overclaims: what was measured is that **no re-load
was observed** — a red colour matrix held across seven seconds and one dialogue transition,
in the opening scene of one battle. A map load re-loads it by definition, a map-state change
almost certainly does, and combat is the open item above. "Once" was doing the work of a
proof. And every RAM observation here is **MAP022 a0, `night=0 weather=0`** — one map, one
state, one arrangement. The addresses are static globals so they will not move, and the byte
*layout* is corpus-wide because RAM holds the file's bytes and `pack_light_rig` is proven
over all 1,575 resources; but *"the loader copies the file verbatim"* is measured on one
file, and Gariland is the map everything here is tested against.

#### The instrument trap this leg walked into first

**An emulator with no disc loads a savestate into a frozen machine, and every probe lies
plausibly.** RAM reads return the state's real bytes, `PCSX.GPU.takeScreenShot` returns the
state's real picture, `getCPUCycles` advances — and every poke reads back **STUCK** while
every screenshot is byte-identical, which is indistinguishable from *"the sink is real but
nothing reloads it"*. The first hour of this leg was measured on a corpse. `handlers.lua`'s
`vsync` counter is **not** the control: the healthy instance on the next port reports `0`.
The control that works is **three screenshots and a pixel diff** — a live battle has a
~6,500-subpixel floor, a dead one is byte-identical — and it is worth spending before any
rendering claim. Launch with `-iso <cue> -run` and let the game reach the shell first.

### 2.3 The picture: the sheet is VRAM, the palettes are RAM

The two halves of decision 9's `(texture_sheet, palettes)` atom live in different memories,
and the obvious guess about the second one is wrong.

**Both addresses are derived, not assumed.** The engine's own packets carry the VRAM
addresses it is rendering from, so the sheet and the CLUT block can be located without
reading a pixel. On MAP022 a0, measured [LIVE] 2026-08-26:

    live_tpage_low4 - doc_texture_page  = 12       385 of 385 polygons
    live_clut       - doc_palette_id    = 0x7800   385 of 385 polygons

A TPAGE's low nibble is the x base in 64-pixel units, so 12 is x = 768 with the y bit clear;
a CLUT attribute packs `(y << 6) | (x >> 4)`, so 0x7800 is y = 480, x = 0. A content scan of
a VRAM GET agrees independently — the sheet's own rows locate at byte offset 1536, which is
exactly (768, 0) — and `identify` names `MAP022.8` at 0 bytes different while all nine other
sheet-sized `MAP022.*` resources differ. **One disagreeing witness is a refusal, not a vote**
(`live_vram.derive_addresses`): 385 agreeing is what makes the address knowledge, and writing
131,072 bytes on the strength of 384 is how a rig corrupts VRAM with confidence.

Note a constraint that falls out of the nibble: the four pages must all fit, so `base + 3 ≤
15` and **x = 768 is the rightmost a four-page sheet can sit.**

**The sheet is written to VRAM, in four rectangles.** `POST .../vram/raw?x&y&width&height`,
one per page, at `(768 + p*64, 0)` 64×256. Each body is a *contiguous slice* of the packed
blob — the sheet is page-major and a rectangle's body is row-major at the same 128 bytes a
row, so no reshaping is involved. A single 256-wide rectangle would interleave the four pages
a row at a time, which is why four POSTs are the cheap shape and one is the expensive one.

**The palettes are NOT written to VRAM, and this is the finding.** Their address is right and
it is not a sink. Measured by writing one CLUT row and reading it back at four delays, with a
sheet write made in the same session as the control:

    VRAM (x=80, y=480)      written, 0/32 differ immediately
                            32/32 back to the ORIGINAL bytes at 50 ms, 0.2 s and 1 s
    VRAM sheet row          written, 0/128 differ immediately AND after 1 s
    RAM 0x800E4EA4 + 160    written, 0/32 differ after 1 s, and VRAM's row 5
                            moved to match within 0.3 s

The engine re-uploads the whole CLUT block from main RAM every frame and does **not**
re-upload the sheet. So a palette push aimed at VRAM works for one frame — long enough to
read back as a success, far too short for the artist to see. `0x800E4EA4` is the block that
feeds it; it matched the live VRAM CLUT rows 0 of 512 bytes different.

**A second copy of those 512 bytes sits at `0x80099D76`, and a push into it does not reach
the screen** — writing row 5 there moved 0 of 32 VRAM bytes. A content scan finds both, so
the address is not trusted for being written down: `live_link.check_clut_block` compares the
block against what the GPU is actually showing before a byte of it is written, which is
decision 2's locate-by-verify at the one address here a scan cannot settle.

That block was first written up here as an *"inert twin"*, which was right about its
behaviour and **wrong about what it is** (corrected 2026-08-27, #624). It is the map's own
`0x44` chunk as the loader left it, and it is not idle: the palette-animation routine writes
every animated frame into **both** blocks in one loop body — `0x800926AC` into this one,
`0x8009269C` into `CLUT_BLOCK`, same function, `ra = 0x80092794`, confirmed by watchpoint
(60 and 20 hits, one writer each). A push into it is ineffective because nothing re-uploads a
*static* row from either block after map load, not because the block is dead. Anything that
later wants to hold an **animated** row has to contend with both.

**Some CLUT rows are engine-animated and cannot be pushed.** Writing all 16 and reading back
named rows 13, 14 and 15 as reverted on MAP022 a0. That set is *reported from the readback,
never predicted* (decision 3): the period is unknown, and a probe short enough to run inside
a press can report "nothing animated" on a map that animates. It is also why no disc resource
matches all 16 live rows — the live block differs from `MAP022.9`'s own `0x44` chunk by 35
bytes over rows 0, 7, 8, 10, 13 and 14. That is the palette **animation**, whose source chunk
`mapfile.PALETTE_ANIM_PTR` (`0x70`) is populated and still has no reader.

**Where the bytes come from.** `export_document.export_sheets` already computes
`png_indexed.pack_4bpp(indices)` — exactly the 131,072-byte blob the push needs — and used to
discard it after hashing. `assemble()` surfaces it on the report now, so push and disc are the
same bytes *by construction*: the sidecar's name **is** that blob's SHA-256. PNG-encoding here
and decoding again in the pusher would be two more chances to differ and no more truth.

---

## 3. Coverage of the document

| document field | live sink | state |
|---|---|---|
| `polygons[].positions` | position arrays | **built** — `live_geometry.py`, 35/35 [LIVE] |
| `polygons[].normals` | normal arrays | **built** — `live_link.py`, 23/23 [LIVE] |
| `polygons[].uv` | packet UV bytes | **built** — `live_link.py`, 1,516/1,516 [LIVE] |
| `polygons[].palette_id` | packet CLUT field | **built** — 385/385, A/B/A screenshot [LIVE] |
| `polygons[].texture_page` | packet TPAGE field | **built** — 385/385 [LIVE] |
| `polygons[].visible_angles` | vertex 1's 4th short | **located** [LIVE] |
| `polygons[].terrain` (binding) | vertex 0's 4th short | **located** [LIVE] |
| `map_states[].texture_sheet` | VRAM, four page rectangles at the derived column | **built** — the addon's button, `live_vram.py`, A/B/A screenshot [LIVE] |
| `map_states[].palettes` | `0x800E4EA4` in **main RAM** — *not* the VRAM CLUT rows | **built** — the addon's button, `live_link.py`, A/B/A screenshot [LIVE] |
| `map_states[].light_rig` | `0x800F5AF4` + `0x800F5B14` + `0x800F5B40`,
  **and** GTE `cnt13-15` / `cnt16-20` | **built** — §2.2, 20/20 fake-RAM, A/B/A [LIVE] |
| `terrain` | terrain chunk in RAM | **not located** — decision 4 |
| `base`, `carry` | not authored — nothing to push | n/a |

Every field the artist can *see* has a sink. The one gap is `terrain`, which is height and
collision, not picture.

The rig's row is the only one in the table whose sink is not an address: two of its three
data reach the picture through **GTE control registers** that were not seen to re-load, so
the push writes those registers as well as the RAM the next map load will read (§2.2). It is
also the only sink with two lifetimes — the RAM half survives until the map reloads, the
register half until anything else touches the GTE, which is a bound nobody has measured.

---

## 4. The decisions

### Decision 1 — the pull is an identification, not a download

*"Download the currently-loaded map and convert it to the interchange document"* is not
constructible as literally written, and the reason is measured: main RAM holds an unpacked
**render view**, not the map. It has positions, normals, the two metadata shorts and the
primitive packets — and no terrain chunk, no palette chunk, no texture sheet source, no
`base` provenance, no `carry`. A document assembled from RAM would be mostly holes, and
schema §11's model is that **new bytes are the base's bytes with the named chunks
replaced**: a holed document does not under-specify a map, it *misrepresents* one, and the
1,575/1,575 identity round trip depends on that not happening.

So the emulator answers **which** `(map, arrangement)` is loaded, and `dump` reads the disc
tree and produces the **document**. That is strictly more capable than the literal ask,
because it always yields a complete document. Reconstructing from RAM was rejected as
buying only one thing — round-tripping edits made outside Blender — and nothing outside
Blender can edit that RAM except this rig, which already knows what it wrote.

### Decision 2 — the artist declares the map; locate-by-verify is the guard

No RAM address holding the current map id is known, and none needs to be. The artist
loaded the savestate; they know the map. `--map 22 --arrangement 0` is what both existing
tools already take.

What makes that safe is the guard that is already built: `live_geometry.py` takes the
declared map, then **locates by verifying** — polygon 0's first vertex is a needle, every
occurrence in the 2 MB is tested for whether the whole bucket verifies there, and only a
unique hit is accepted. A wrong declaration cannot locate, so the rig refuses rather than
writing 32 bytes per polygon into whatever else lives there. This is why §2's static bases
do not retire the search: the search is not how we find the array, it is how we prove the
declared map is the loaded one.

Self-identifying by fingerprint was rejected: it stops working the moment the artist has
pushed a geometry edit, which is the thing this rig exists to do.

> **Amendment, 2026-08-26 — the guard is the descriptor block, for the push direction.**
> Locate-by-verify was built for the **pull**: decision 1, where the emulator has to say
> *which* map is loaded. Pushing a whole map needs no such answer. *"I don't think we need
> to know what map is in pcsx redux, we're just gonna push our whole map over it anyway"* —
> and that is right: the artist has the document open in Blender, they edited it, there is
> nothing to identify. §2.1 also means the engine now *tells* you where every bucket lives,
> so the needle search is not needed to find the arrays either.
>
> The guard is not dropped, it is replaced by a cheaper and better one. `check_descriptors`
> reads the block, refuses when the primary mesh carries no polygons in any bucket (no map
> is loaded, or it has not finished loading), and refuses when any slice runs off the end of
> its array — `start + count` against ADR-0004 decision 28's capacities, `360 / 710 / 64 /
> 256`. The bound is on `start + count` and not on `count` alone, because the arrays are
> shared and sliced: 300 quads is legal and `500 + 300` is not.
>
> What still does the real work is §5's **write-path self-check**: before any push, rewrite
> the engine's own bytes over themselves and assert **zero** changed. That is the check that
> catches a stride, vertex-offset or field-mask error in the rig's own arithmetic, it costs
> nothing, and it is the single highest-value test in the build.
>
> `live_geometry.py`'s search is **not** retired by this. It answers a different question —
> *is the declared map the loaded one* — and decision 1 still asks it.

### Decision 3 — the loop pokes RAM; it does not patch the ISO

Patching the map into the ISO and forcing a reload would work, needs no addresses at all,
and is exact — what you see came off the disc through `build`. It was rejected on cadence:
a reload is seconds where a poke is one frame, and ADR-0004's lighting-bake §11 established
that pressing *Bake to FFT* repeatedly **is** the loop (~19 ms on the corpus's largest
mesh). A per-edit reload destroys that rhythm.

It stays the named fallback for anything a poke cannot reach, and it is the honest way to
answer *"does this actually ship correctly?"* — but that is a different question from
*"what does this look like?"*, and only the second one is asked dozens of times an hour.

The standing rule is unchanged and worth restating: **this rig shows you a picture, and
`build` is what puts bytes on a disc.** A live push touches no disc, so schema §11's carry
model is untouched and the identity round trip is not at risk. Blender is authority over
the live picture; the base map remains authority over every byte the document does not
name. There is no ownership conflict here, and the earlier suspicion that there was one
came from reading a *push* as a *write-back*.

### Decision 4 — push every field that has a sink, and name the ones skipped

`terrain` has no located sink. The push proceeds anyway and **reports what it could not
push**, the way the bake names every state it touched.

Refusing the whole push when a document declares an authored `terrain` was rejected: the
terrain chunk is the grid that bindings resolve against, not the picture, so a skipped
terrain push yields a *correct* picture with stale collision — never a wrong picture. A
refusal would block the artist on a mismatch they cannot see.

Locating the terrain chunk is **planned work, not a permanent gap**: the same play that
produced §2 — find the load-time writer in the decompilation — should produce it. It is
deliberately not a gate on the first working loop.

### Decision 5 — the live link lives in the addon, because the addon is the product

> Decisions 5 and 6 are recorded as **ADR-0005** — they are hard to reverse, and
> point 4 there deliberately reverses the direction `png_indexed.py` established.

`addons/exmateria_map/` is the shippable standalone authoring tool. A feature outside it
is a feature the product does not have. So the whole live link — transport, sinks, the
savestate round trip — goes **in the addon**.

The argument against was that this pushes emulator transport and RAM-layout constants into
the thing an artist installs, for a feature most installs never use, and that it makes the
pusher undrivable without Blender open. Decision 6 answers both without giving up the
product constraint. The addon today imports only `bpy`, the stdlib, and its own siblings;
that stays true, because the live link needs nothing else — a small `urllib`
Lua-over-HTTP client, byte arithmetic, and addresses.

`pcsx-agent` was rejected as a home: it is deliberately generic transport with no FFT
knowledge, and it must be edited in its own worktree, so hosting the sinks there would
both pollute it and slow every change. It remains the reference for the transport shape.

### Decision 6 — one copy, `bpy`-free, in the addon

The live-link core is bytes, addresses and HTTP; none of it needs `bpy`. Only the panel and
the button do. So the core is a `bpy`-free module **inside the addon**, and `tools/` keeps
a CLI that path-inserts the addon directory and imports it — the trick `live_push.py`
already uses to reach `vram_swap_sheet.py`.

The house pattern for shared code is two byte-identical copies with a guard
(`png_indexed.py`, asserted in `tests/test_build.py`). That was rejected here: a guard
asserting byte-identity is a guard someone must keep green, and **no second copy is
strictly better than a guarded one**. The addon still ships self-contained, because a
`bpy`-free module inside the addon is still inside the addon.

Two properties this preserves, and they are what make the thing buildable at all: the core
stays testable under plain `pytest` — the emulator-gated audits cannot be — and the pusher
stays drivable from a document with Blender closed, which is how each new sink gets seeded
and verified in the first place.

**Note the direction reverses the existing precedent, deliberately.** Shared *format* code
is the package's and the addon mirrors it; shared *live-rig* code is the addon's and
`tools/` imports it. The discriminator is which artifact ships.

### Decision 7 — the button self-checks against the marker's shadow attributes

The button is the loop: `assemble(ob)` already returns `(doc, files, report)` **in memory**,
so the push calls it and skips the write. Nothing about that was in question. What was, is
what the write-path self-check compares RAM against, because the CLI reads the base map off
the **disc** and the addon cannot — it never imports `exmateria_map` and has no corpus
(ADR-0004 §7).

Three options were on the table and none of them worked. Skipping the check gives up the
highest-value test in the build. Declaring `--map`/`--arrangement` in the panel names the
map but still cannot read it. Comparing against the document's **own** geometry is
tautological — it is the thing about to be written.

The fourth is what shipped: the marker's **`positions_shadow` / `normals_shadow`** corner
attributes. Import writes them from the document and only a re-import touches them, so they
hold what the disc held rather than what the artist has since edited. No corpus, no disc
read, no declaration — and *stronger* than the CLI's, because a document imported from a
different map than the one loaded now mismatches. That is exactly the identity claim
decision 2's amendment stopped making, recovered as a side effect.

Two cases it cannot cover, both named rather than papered over:

- **A face the artist added zero-fills its shadow.** Adding geometry usually fails the
  count check first, but adding one face and deleting another does not — so a scene with
  any un-imported face skips the self-check and says so in the report.
- **The second press of the button legitimately differs from the disc**, because the first
  edited exactly these bytes. The CLI's answer is "reload the savestate"; a button pressed
  repeatedly cannot ask that and stay a loop. So the last push's own plan is kept **in
  memory for the process** and tried first. The chain stays anchored: the first push of a
  session has no such entry and is checked against the disc's bytes, and a fresh Blender
  against an emulator someone pushed to yesterday matches neither candidate and refuses.

The session memory is deliberately not persisted. It is a claim about another process's
RAM, and a stale one would turn the self-check into a rubber stamp.

### Decision 8 — a document whose polygon counts differ from the loaded map

Settled by grilling on 2026-08-26, and it splits into three deliverables rather than one.

> **Amended, 2026-08-26 (second grilling).** Five of this decision's conclusions were
> reached against a repo that no longer exists — the packet leg landed the same day it was
> written — and two of its statements are simply false. The body below is left standing
> because its *reasoning* is what the amendments argue against; where the two disagree,
> **this block wins**.
>
> 1. **Shrink and growth ship together**, as one issue, under #587 — which keeps its
>    umbrella title and gains its first child. The three-deliverable split is dissolved:
>    the packet leg is built, and bytes 6-7 rides with the count work (3 below), so the
>    only thing left that is unique to growth is the capacity refusal, the follower gate
>    and the write-order flip. Hard ordering inside the issue: **both gates are built and
>    seeded red before the `>` refusal at `live_link.py:264` is lifted.** Neither gate is
>    reachable on MAP022 — it has no animated mesh, and its `24 / 361 / 18 / 51` sit far
>    under `360 / 710 / 64 / 256` — so **both are graded by `blender_live_push.py`'s fake
>    RAM, not by the emulator**, and that must be said out loud wherever they are claimed.
> 2. **`source_index` is not built.** The sub-section *"The self-check needs to know where
>    a face is"* has the right diagnosis and the wrong mechanism. The imported polygon
>    list is **already stored verbatim** at `ob["exmateria_map/polygons"]`
>    (`import_document.py:1339`; `TOP_LEVEL`, line 82) — export's own docstring calls the
>    marker's sections "import-time snapshots handed back verbatim". `base_polygons` is
>    reconstructing from a mutable mesh a thing that was never lost. Reading the stored
>    document instead is immune to deletion, reordering, extrusion **and** retexturing,
>    needs no new attribute, no `-1` sentinel, and never returns `None` — so the check is
>    at full strength during growth, which is when this decision most wanted it.
>    Two hazards a face attribute would have carried, both avoided: Blender has no
>    per-attribute default, so a from-scratch face would have zero-filled to slot **0**,
>    not `-1`; and §8's inheritance clause hands an extruded child its parent's whole
>    carried row, so two faces would have claimed one slot on the commonest growth edit.
>    The base plan must take its length from the **imported list**, never from the live
>    descriptor's counts, or a second push in one session addresses the wrong slot range.
> 3. **"Shrinking needs none of it" is wrong, and bytes 6-7 is a hard dependency of
>    shrink.** That sentence describes a *tail* deletion. A mid-mesh deletion re-slots
>    every surviving polygon — this decision says so itself two sub-sections earlier — and
>    positions, normals and the packet all follow a polygon to its new slot while bytes
>    6-7 do not. The survivor arrives wearing the previous occupant's `VISIBLE_ANGLES`,
>    which by this decision's own measurement **culls quads into holes**. Shrink without
>    the metadata write ships a feature that punches holes on the normal edit.
> 4. **"The document carries `terrain` and `visible_angles` for every polygon" is false.**
>    An untextured polygon carries neither (`export_document.py:340-344`); it carries
>    `unknown_untextured`, four raw property bytes (schema §5.2). The untextured buckets
>    have the same 8-byte vertex stride, so they have bytes 6-7 too, and **where those
>    four bytes land in RAM is not located** — the disc layout is structure-of-arrays and
>    the loader scatters it. So the rule is **the two textured buckets only**;
>    `unknown_untextured` joins `UNPUSHED` as an unlocated sink (decision 4) and gets its
>    own small ticket. Not zero-fill: #496 settled that zero is the worst fill, and this
>    byte range culls rather than mis-colours. The cost is stated rather than hidden — a
>    reordered untextured polygon leaves its four property bytes behind, on 69 of
>    MAP022 a0's 454 polygons.
> 5. **Three gaps this decision did not reach**, each found in the code it names:
>    `plan_document` skips a bucket with no polygons (`live_link.py:707`), so **the count
>    write is driven by the four buckets, not the plan dict**, and zero is a legal count
>    to write; a document emptied of *all* polygons must be **refused**, because
>    `check_descriptors`' `is_empty()` would then refuse every later push and the artist
>    could only reload the savestate; and `authored_bytes`' `zip` (`live_link_ui.py:181`)
>    truncates to the shorter plan, so a pure growth reports **0 authored bytes** and
>    `interpret` prints "byte-identical to the map you imported" over a push that just
>    added a polygon — a third cause for the zero this design is built to disambiguate.
>    It compares **by address** over the union, and the count writes count.
>
> **Refusals shrink actually owns**, since none of the three the handoff named can fire:
> the all-empty document, growth above the ceiling, the self-check failing, and the count
> write failing to verify. **Acceptance** is A/B/A on a live Gariland battle through the
> button — delete → push (holes) → restore → push (whole) → a third press changing **0
> bytes** across the wider byte set — plus fake-RAM arms for null `visible_angles`
> (MAP022 has a `0xB0` chunk and cannot reach it), the emptied bucket, the all-empty
> refusal, and both growth gates.

> **Amended again, 2026-08-26 (BUILT, #598).** The leg is built in both directions, and
> building it refuted the first amendment's point 4 and sharpened two other things. Where
> the three disagree, this block wins.
>
> 1. **Point 4 was wrong twice, and one measurement settles both.** It said "an untextured
>    polygon carries neither [`terrain` nor `visible_angles`]" and that where its bytes 6-7
>    land in RAM "is not located". `export_document` writes `visible_angles` **above** the
>    textured branch and `dump` reads it from the 0xB0 chunk for all four buckets, so every
>    polygon carries it; what an untextured polygon does not carry is `terrain`. And the
>    bytes ARE located. Measured against `reference-assets/thief_whats_this.sstate` — a
>    frozen capture of a running Gariland battle, located by verifying against the
>    descriptor fixture rather than by an offset constant — over all 454 of MAP022 a0's
>    polygons:
>
>    | | vertex 0's 4th short | vertex 1's 4th short | vertices 2-3 |
>    |---|---|---|---|
>    | textured (385) | the binding word, `x << 8 \| z << 1 \| level` | `visible_angles \| 1` | 0 |
>    | untextured (69) | 0 | `visible_angles` | 0 |
>
>    So the rule is **all four buckets**, not the two textured ones, and shrink's
>    hole-punching argument (point 3) applies to the untextured buckets exactly as it does
>    to the textured ones. What remains unlocated is `unknown_untextured`, the untextured
>    record's four raw property bytes — a **different** four bytes, which point 4 conflated
>    with these. It joins `UNPUSHED` and is not zero-filled (#496).
>
> 2. **Bit 0 of the VISIBLE_ANGLES word is the engine's own textured mark**, not data. No
>    `visible_angles` value on MAP022 a0's disc carries it (0 of 454) and RAM sets it on all
>    385 textured polygons and none of the 69 untextured ones, so `| 1` is a real
>    transformation rather than a coincidence that happens to agree. A push that dropped it
>    moves 385 bytes; the savestate check catches that, and is seeded with it.
>
> 3. **The live A/B/A, and the defect only it could catch.** On a running
>    Gariland battle, through the button: an untouched import pushed **0**
>    changed bytes; 60 textured quads deleted out of the MIDDLE of the bucket
>    pushed **9,316** and `361 -> 301`, and the house they belonged to was
>    visibly hollowed out on screen; the restore pushed **9,316** back and
>    `301 -> 361`, and the map was whole again at **4,210 differing subpixels**
>    from the baseline against a 2,000-5,800 noise floor measured on the same
>    machine at rest; a third press changed **0** bytes across the wider byte
>    set.
>
>    The first run of that A/B/A came back geometrically whole and **half the
>    map blue**, while reporting 0 changed bytes on the third press. Both were
>    true: RAM held what the plan said, and the plan was wrong.
>    `plan_packets_document` sized its read-modify-write read off the
>    DESCRIPTOR's count, so on the restore every slot past 301 read `held = 0`
>    and `palette_id` went in with the CLUT's `0x7800` row bits cleared --
>    every one of those faces pointing at VRAM row 0. **A slot past the loaded
>    count is not a slot with nothing in it**: the array is fixed-capacity and
>    the count only says how many are DRAWN. The read is sized off the
>    document's own length now, and short bytes are a caller error in both
>    directions rather than a licence to zero.
>
>    Nothing but a render could have found it. The byte count was 0, the
>    polygon counts were right, all 86 fake-RAM checks were green, and the
>    savestate check was green. **A byte count is not acceptance.**
>
> 4. **The whole of bytes 6-7 is graded without an emulator.** 1,816 of 1,816 bytes, all
>    four buckets, on every `pytest` run. That is the design's own acceptance — an untouched
>    push changing zero bytes across the wider byte set — run offline before an emulator is
>    ever started. It does **not** replace the live A/B/A, which is the only thing that can
>    say a deletion stops being drawn.
>
> 5. **Both growth gates remain fake-RAM only, and one addressing bound was missing.**
>    `check_capacity` bounds `build` §10.4's sum (primary + AnimatedMesh 1-8) **and** the
>    addressed end `start + count`, which the sum does not cover when a slice does not start
>    at 0. Neither gate is reachable on MAP022 — no animated mesh, and `24 / 361 / 18 / 51`
>    far under `360 / 710 / 64 / 256` — so both are claimed off `pytest` and
>    `blender_live_push.py` alone, and the code, the tests and the harness each say so.
>
> 6. **`UNPUSHED`'s `terrain` is now `the terrain grid`** (point 5's naming clause, built).
>    A push writes 454 terrain BINDINGS on MAP022 a0 and cannot touch the grid, so the bare
>    word told the artist a working feature was broken.
>
> 8. **What of this decision is GRADED, and what is only recorded.** The same
>    three tiers §2.2 states, because this block has the same problem: it reads
>    with one voice and it is three kinds of claim.
>
>    *Graded on every commit, with no emulator.* The whole of bytes 6-7 against
>    `gariland_ram` — 1,816 of 1,816, all four buckets — with two seeds proving
>    it not blind; the binding word's packing; the `0x8000` default; both growth
>    gates, in `pytest` and in 86 `blender_live_push.py` arms; the count write,
>    the emptied bucket, the all-empty refusal, `authored_bytes` over the union,
>    the naming, and the packet read-modify-write bound at both seams.
>    `test_the_addons_capacity_constants_are_the_packages` keeps the copied
>    `ENGINE_CAPACITY` / `CORPUS_MAX` pinned to `document.py`, which
>    `test_build.py` recomputes from the disc.
>
>    *Measured once, on a live machine, and graded by nothing.* The A/B/A
>    itself — `0`, `9,316` with `361 -> 301`, `9,316` with `301 -> 361`, `0` —
>    every subpixel count in it, and the 2,000-5,800 noise floor those are read
>    against. Above all: **that lowering the count actually stops slots being
>    drawn**, which is the whole premise and which no `pytest` can see. Session
>    observations. Treat a change here as undetected until someone looks again.
>
>    *Not measured at all.* **Growth past what the map ever loaded.** The live
>    A/B/A's growth leg is `301 -> 361`, a restore into slots MAP022 a0 had
>    filled at load, so the packets it grew into held the previous occupants'
>    bytes — which is the common case and is not the hard one. A bucket taken
>    ABOVE its loaded count on a real machine has never run: MAP022's four
>    arrays sit far under capacity and the fake-RAM growth arm reaches it only
>    by having RAM claim fewer polygons than the document carries, which is a
>    construction. What a genuinely new slot's packet halfword holds there is
>    unknown, and this design preserves whatever is in it rather than guessing.
>    Also unmeasured: `unknown_untextured`'s sink (#604).

> 7. **The harness printed `PASS` under a fatal traceback.** `EXPECTED_CHECKS` is not that
>    guard — a run that adds arms before the crash point dies and still clears the floor. A
>    fatal now exits 1. Found by this leg's first red arm, not by a check.

`plan` refuses when `len(polygons) != descriptor.counts[i]`, on the reasoning that "adding
or removing geometry runs into whatever slice sits after this one -- that needs `build`,
not a poke". That reason is right about *one* of the four arrays on *twelve* of the maps
and wrong everywhere else, and the engine already tells you which case you are in.

**The count is the switch.** The dispatch at `0x800E840C` recomputes
`count = descriptor[+0x90 + 2*bucket]` immediately before each renderer call, every frame
(§2.1). Raise it and the renderer draws more polygons on the next frame — no reload, no
reallocation. That is what makes growth a poke at all, and it is also the danger: the
loader does not bound-check these arrays (ADR-0004 decision 28), so a count above capacity
is not a refusal, it is memory corruption.

#### The follower problem is per bucket, and the engine answers it live

Growing the primary mesh shoves every *following* slice — its data must move **and** its
start at `+0x88` must be rewritten. But a follower only exists where an `AnimatedMesh`
section carries polygons, and the four arrays are independent. Measured over the whole
corpus (`mapfile.animated_mesh_counts`, 169 geometry-carrying resources):

| resource | primary | animated |
|---|---|---|
| `MAP002.11370` / `.12045` | 49, 395, 4, 52 | 0, 10, 0, 0 |
| `MAP006.14304` / `.14526` | 78, 328, 15, 53 | 6, 4, 0, 0 |
| `MAP036.27614` | 47, 414, 0, 51 | 44, 24, 0, 0 |
| `MAP038.27858` | 74, 273, 11, 42 | 0, 30, 0, 0 |
| `MAP040.28620` | 94, 267, 6, 53 | 0, 10, 0, 0 |
| `MAP041.29166` / `.29197` | 69, 396, 3, 21 | 75, 122, 0, 0 |
| `MAP047.31031` | 37, 321, 13, 58 | 0, 16, 0, 0 |
| `MAP053.34529` | 2, 281, 0, 65 | 10, 179, 0, 0 |
| `MAP055.34712` | 70, 303, 0, 0 | 4, 130, 0, 0 |
| `MAP073.40086` | 54, 449, 7, 77 | 50, 124, 0, 1 |
| `MAP083.45143` | 51, 225, 12, 22 | 0, 30, 0, 0 |
| `MAP103.53724` | 42, 247, 2, 49 | 140, 85, 0, 0 |

**15 resources, 12 distinct maps.** Per bucket, the number of the 169 with no follower at
all is **160** textured triangles, **154** textured quads, **169** untextured triangles —
no shipped resource animates that bucket — and **168** untextured quads, the exception
being `MAP073.40086` with exactly one.

So the gate is **per bucket**, and it costs nothing: `check_descriptors` already reads all
nine descriptors, so the addon asks the *running engine* whether descriptors 1–8 carry any
polygons in this bucket. No disc read, no corpus, none of ADR-0004 §7's problem.

**The relocation case is refused, not built.** Two reasons, and the second is the one that
decides it. First, no savestate in the repo reaches any of the twelve — Gariland is MAP022,
Orbonne is MAP056/MAP062 — so the shove could only ever be verified against
`blender_live_push.py`'s fake RAM. Second, the asymmetry: a refusal costs an artist on
twelve maps a walk to `build`, which does this correctly; a wrong shove is unbounded
memory corruption on a map no instrument here can watch. The refusal names the live counts
and points at `build`.

*Relocating the primary past the followers instead of shoving them* was considered and
rejected: it needs `new_count <= capacity - (primary + animated)`, which holds on
`MAP103` (710 − 332 = 378 ≥ 247) and fails on `MAP041` (710 − 518 = 192 < 396) and
`MAP073` (710 − 573 = 137 < 449). It fails on the maps with the most animated geometry,
which is the worst possible shape for a rule.

#### Growth waits on the packet leg; shrink does not

A **new** slot holds whatever was there, and that is a bigger claim than the counts:

1. **Positions and normals** — already built.
2. **Bytes 6–7 of vertices 0 and 1** — the terrain binding word and `VISIBLE_ANGLES`.
   Garbage in `VISIBLE_ANGLES` culls quads into holes, measured A/B/A.
3. **A GPU primitive packet.** A polygon the renderer draws with an uninitialised packet is
   not a wrong colour, it is undefined.

So **growth ships only once the packet sink is built and confirmed live**, writing UV,
CLUT and TPAGE from the document. *Cloning a sibling polygon's packet* — a byte copy
needing no layout knowledge — was on the table as a way to unblock growth first, and the
packet work in flight on this branch killed it: the packets are **double buffered**
(`0x800FC55C` / `0x8010B384`, `0xEE28` apart) and both copies must be written, so a clone
into whichever buffer the pointer happens to name lands in half the frames. [LIVE]

**Shrinking needs none of it.** Lower the count, and the slots past the end simply stop
being drawn — no new slot, no packet, no metadata, and no relocation even on the twelve,
because nothing moves and every follower's `+0x88` stays valid. It ships first, on its own.

The order is therefore **shrink → the packet leg → growth**, as three issues: this one
keeps shrink, the other two are filed under map #517.

#### The self-check needs to know where a face *is*, not where it is going

`base_polygons` returns `None` the moment any face is new, so the highest-value test in the
build stands down exactly when it is most wanted (decision 7 named this). Growth is not the
only casualty. It walks `me.polygons` in current mesh order and hands the result to
`plan_document`, which assigns document index → RAM slot — so **deleting face 5 of 24
shifts every surviving face down one slot**, the base plan claims slot 5 holds
surviving-face-5's shadow when slot 5 still holds old face 5, and `selfcheck` fires on a
perfectly healthy shrink while blaming the rig's arithmetic, a wrong map, or a prior push.
All three are wrong. Only a *tail* deletion survives, and nothing in the mesh can tell you
a deletion was at the tail, because the deleted face is gone.

The general form: once the face set changes, the self-check and the push **address
different things**. The push writes the *new* layout, document order `0..N-1`. The check
must read where each face *currently* lives.

So the marker gains one attribute: **`source_index`**, `INT` on `FACE`, written at import
in the `_shadow` family — never touched by editing, and a deleted face takes its value with
it. A face the artist created reads `-1`. The self-check is then built per face at
`base + source_index * stride`, excluding the new ones, and it is correct under deletion,
reordering **and** growth. Reordering is worth calling out on its own: today it pushes
garbage with a *green* self-check, because both sides move together.

There is no back-compatibility path for scenes imported before this attribute. Nothing of
value has been authored yet, and a degraded self-check that silently rubber-stamps is worse
than a refusal.

#### The document owns bytes 6–7, on every polygon

`plan` writes six of every eight bytes and leaves the two metadata shorts alone, on the
reasoning that an existing polygon owns valid ones and they are not ours. Growth cannot
inherit that rule, because a new slot's are garbage — and rather than split the rule by
slot, **the push writes them everywhere**. The document carries `terrain` and
`visible_angles` for every polygon; the live picture should show what the document says,
the same as every other field.

`visible_angles` is `null` on the **10 of 169** resources with no `0xB0` chunk —
`MAP000.10000`, `MAP053.34529`, `MAP053.34554`, `MAP083.45124`, `MAP099.52814`,
`MAP116.56116`, `MAP117.56188`, `MAP118.56266`, `MAP119.56344`, `MAP125.56421`. Those write
the `0x8000` default `stamp_new_faces` already gives a new face, so RAM never holds a value
the document cannot name. `MAP022` has a `0xB0` chunk, so **no test on the only map we hold
a savestate for exercises the null case.**

This is the highest-consequence write in the design: it now runs on every push, including
pushes that changed no geometry, and a wrong value here culls quads into holes rather than
mis-colouring them. The acceptance is the one §0 already uses — an untouched import must
still push **0 changed bytes**, now across the wider byte set. If that number moves, it is
the alarm, not a rounding error.

#### What falls out

- **Capacity**: refuse above `ENGINE_CAPACITY`, warn above `CORPUS_MAX` — the same two-tier
  arithmetic `build.py` §10.4 already runs, on the same constants.
- **Write order**: growth writes geometry, metadata and packets *first*, then raises the
  count; shrink lowers the count *first*. Neither leaves a frame drawing a slot that is
  mid-write.
- **Shrunk-past slots keep their stale bytes.** They are not drawn, and a later growth
  rewrites them in full before the count comes back up.
- **The count write goes through `apply` / `verify`** with the treatment `c05d92f6c` gave
  the push: a zero must not have two causes.

### Decision 9 — the push AIMS at the previewed state, and an aim is not a write

Settled by grilling on 2026-08-26. It answers the question the previous session raised as a
blocker, and the blocker was not one.

**The steer came from use, and the code already carries the complaint verbatim.**
`live_link_ui.py` records it: *"when I change map preview entry and hit push nothing happens
- shouldn't it update the texture?"* The panel answers it with two reasons, and the second —
*"the texture sheet and the CLUT have NO LIVE SINK in this module at all"* — is exactly what
this leg deletes.

**The objection was that a push following a view contradicts `CONTEXT.md`'s View state entry.
It does not, and `bake_normals` is the standing proof.** That function reads
`exmateria_map/preview_state`, resolves *that* state's rig, and solves `normals` against it —
and `normals` is, by `CONTEXT.md`'s own **Imported normals** entry, *"the only thing export
ships"*. So view state **already** steers bytes that reach the document and the disc, today,
through an aim. The rule is that view state never *enters* the document; it says nothing
about what may be *pointed at*. **View state may STEER an act without ENTERING it**, and that
sentence is now in the glossary as **Aim**.

Three readings were put up. **A** (the push reads view state) is **C** stated badly. **B**
("materialize the previewed state into the document", the user's own word) turned out to name
work that is already done — see below. **C** is this decision: the push aims, the way the
solve already aims.

**B was withdrawn: export IS the materialize, and there is no second kind of save.**
`export_sheets` repacks an edited sheet, re-hashes it, and renames **only the states that
named the old sidecar**; `export_rigs` promotes an Override to `authored_light_rig` **on that
state alone**, and a state with no Override gets no key, so an untouched document stays
byte-identical. Both are already per state, so decision 27's *"what happens to the other
nine"* has a shipped answer: nothing. A separate Materialize operator would be a third save
in a package that deliberately has one.

#### The aim's key is `(night, weather, kind)`

Not an index, and not a time of day. `night` is **one bit** and `weather` **three**, both
packed in GNS record byte 3 (`mapfile.GnsRow`), so the pair names a **group** of rows rather
than a row. Measured over the corpus, 197 arrangements and 774 groups:

| rows in the group | kinds | groups |
|---|---|---|
| 2 | TEXTURE + one mesh | 634 |
| 1 | a lone mesh, no TEXTURE | 118 |
| 3 | TEXTURE + Initial + Override | 20 |
| 4 | TEXTURE + three meshes | 2 |

`kind` picks within the group — 23 TEXTURE / 46 Initial / 47 Override / 48 Alternate — and
`(night, weather)` alone is ambiguous on **656 of 774**. The group is also where the picture
splits: a 131,072-byte TEXTURE row carries **no palette chunk** (`palette_offset` is `None`),
and the mesh row carries the palettes at offset 196 and the rig at `0x64`. **So one aim
resolves to two rows**, and a pusher that reads one of them has half a picture. MAP001 a0 is
the shape: `night=0 w=0` is TEXTURE + Initial (38,912 B, the geometry) + Override (6,144 B),
while `w=1..4` are TEXTURE + Alternate at **2,048 bytes** — two kilobytes of palettes and rig
and no geometry at all, which is decision 4's *"geometry rides the arrangement"* seen from
the disc side.

#### Two atoms, because a half-aimed push is a WRONG picture, not a stale one

Decision 4 says push what has a sink, name what is skipped, **never refuse** — and its stated
reason is that the skipped fields *"neither yield a **wrong** picture, only a stale one"*.
That reason reaches `terrain` and the unlit buckets. It does not reach the per-state data,
because aiming makes it coupled:

- **`(texture_sheet, palettes)` is one atom.** State 3's 4bpp indices read through state 0's
  CLUT rows is not a stale picture, it is garbage — and it is the *likely* outcome, since the
  sheet leg is built (`live_push.py`) and the palette leg is §3's *"same leg, unverified"*.
  They also cost nothing to keep together: both are VRAM rectangles inside the **same**
  savestate round trip, so pushing them as one act is cheaper than pushing them apart.
- **The rig is one atom, and it is 39 bytes, not 45.** `0x800F5B14` is the light-**direction**
  matrix; on the GTE the three **gains** go to the light colour matrix and **ambient** to the
  background colour registers, both view-independent and therefore loaded from somewhere this
  document has not yet located. Pushing directions alone under an aim gives state 3's angles
  over state 0's brightness — a rig belonging to neither state. The 6 gradient bytes stay out:
  decision 27 has them read-only and echoed verbatim, so the target is the 39 the solve owns.
  **`UNPUSHED`'s `map_states[].light_rig` does not go green until all 39 move.**

Decision 4 stands unamended. It is read at the atom, and its own reason is what says where
the atom is.

#### The report names the aim and everyone who shares what moved

Decision 27's rule — *"every state the bake touched is **named** in its report"* — carries
over unchanged, and the push needs it more, because the things it moves are shared four
different ways: **normals** by every state (one geometry per arrangement), **palettes** by
every state naming the same resource (`build.py` refuses two rows naming one resource with
differing palettes), the **sheet** by every state naming the same sidecar, and the **rig** by
the state alone. *"Pushed state 3"* is a third of the truth. The report says
`aimed at day / weather 3 / Initial`, and then who else moved with it. All of that is in the
document already, so it costs nothing.

**No divergence warning in v1.** The running game has a map state too and it need not be the
aim, but that is not a disagreement to report — Blender is the authority over the pushed
picture (`CONTEXT.md`, **Push vs write-back**) and the emulator's loaded state is only what
the battle happened to boot with. Decision 3 already says every push is temporary and a map
reload reverts it. Noted for when it is wanted: once the rig leg lands, reading `0x800F5B14`
back and matching it against `map_states` identifies the loaded state from a small RAM read,
and `vram_swap_sheet.identify` already answers the same question from a savestate.

#### The ordering changed, and the reason is an authorship gap

Grilling the aim turned up that **two of the three things it aims at cannot be authored at
all**, so the original order pushed data the artist could not change.

- **Palettes are read-only end to end in the addon.** `import_document` reads
  `map_states[].palettes` into a CLUT image for the preview; **nothing writes it back** —
  `assemble` copies `map_states` through untouched but for the sheet rename — and there is no
  CLUT editor. The PLTE export writes into a sidecar is display-only and `build` ignores it
  (decision 6).
- **Which is why texture authoring is stuck too.** The paint gate resolves a painted pixel
  against the active CLUT's 16 entries and refuses anything else (`interchange-export-v1.md`
  §3.6), so the artist can only **re-index** — rearrange which of 16 fixed colours a texel
  shows. A new colour cannot be introduced anywhere. **Palette editing is upstream of texture
  authoring, not beside it.**
- **The disc half is already built, and is not optional.** `build.pack_palette_chunk` packs 16
  CLUTs into the `0x44` chunk and writes them at `mapfile.palette_offset`, and `build`
  **refuses** a document whose resource carries a `0x44` chunk but no `palettes`. So palettes
  are mandatory and round-tripped, covered by the 1,575/1,575 identity trip. This is the
  inverse of decision 27's situation, where the disc side was the blocked thing: here it is
  done, and the whole gap is Blender-side.
- **The format answers "what about the other states" for us.** Palettes are per *resource*, so
  editing one necessarily moves every state naming it. There is no choice to make.

The order is therefore **the rig, then palette authoring, then the sheet**:

1. **The rig.** The one thing on the list the artist can fully author today — every
   state's rig is exposed, export promotes the ones edited, `build` writes 45 bytes
   (decision 27, proven to reach a disc) — and the one whose edit is currently
   **invisible in PCSX**. Smallest of the three.
2. **Palette authoring** — **built**: the 16×16 CLUT image is the edit surface and
   `export_palettes` re-emits `map_states[].palettes` from it. What is left here is the
   *push*, not the authoring.
3. **The sheet + palette push**, once there is something to push.

*"Follow the previewed state"* is not a fourth piece. It is a parameter to 1 and 3 and ships
with whichever lands first.

#### Named work that falls out

All four ride with the **rig** leg above — they are fixes, not research, and none is
worth a commit of its own.

- **`map_states[].palettes` is missing from `UNPUSHED`.** Decision 4 requires every field
  without a sink be named on every push; this one is named nowhere.
- **The panel box says a push carries "PALETTES".** It carries `polygons[].palette_id`, a row
  **index**. Two different things under one word, in the one place the artist reads.
- **The sheet pusher anchors on the wrong state.** `live_push.py` sets
  `disc_blob = (map_dir / resource).read_bytes()` from the **aimed** state's resource and
  locates VRAM by matching it. Aim across states and the blob is not in VRAM and the push
  refuses. **Locate by what is there, write what you are aiming at** — the two take different
  rows, and `vram_swap_sheet.identify` is the function for the first.
- **`live_push.py` aims by `--night` / `--weather`, defaulting `0/0`.** That is the same key
  as this decision, resolved from a CLI flag instead of the preview. It becomes the aim.

All four are **built**: 1 and 2 in `e632d4799`, 3 and 4 with `tests/test_live_push.py`. §0
records what fix 3 opens — a cross-group aim now lands, and until leg 3 it lands half an atom.

---

## 5. What is not proven yet

Everything marked [STATIC] above is read from the disassembly and has **not** been
confirmed against a running machine. Co-presence is not use, and a mechanism that *could*
explain something is not evidence that it did. Each wants one confirmation, and each is
cheap because the instrument exists:

| claim | confirmation | state |
|---|---|---|
| the normal arrays are at `0x801251D4` / `0x80127394` | they must match the **disc's** normal bytes exactly — the same assertion `live_geometry.py` already runs for positions | **[LIVE]** 0 mismatches of 9,096 bytes, 1,516/1,516 vertices |
| NCT re-lights per frame | ~~a **Read** watchpoint on the normal array~~ — **the wrong instrument, see §5.1**. Zero every normal and look | **[LIVE]** luminance 11.08 → 7.11 → 11.08 |
| poking a normal changes the picture | poke one, screenshot. Only a render can settle a rendering question | **[LIVE]** same A/B/A |
| the descriptor block at `0x800FBE00` | read `+0x90..+0x96` and expect the document's own polygon counts | **[LIVE]** `24 / 361 / 18 / 51` |
| a document assembled by the addon matches one from `dump` | push an untouched import and expect **zero** changed bytes | **[LIVE]** 0 of 10,644, MAP022 a0 |
| the shared-and-sliced start index | Gariland's four are 0, so only a **fake** RAM can ask: seed at `base + (start+i)*stride` and again at `base + i*stride` | **fake** — `blender_live_push.py`, the second arm goes red |
| `0x800F5B14` is the rig | a **Write** watchpoint, plus a poke and a screenshot | [STATIC] |
| the packet UV / CLUT / TPAGE offsets | ~~rewrite the packet's own bytes over themselves~~ — that is necessary and **not sufficient**, see §5.2. Predict every field from the disc, then move one and look | **[LIVE]** 0 differ of 9,144; palettes shifted and restored on screen |

### 5.2 The packets are DOUBLE BUFFERED, and a one-buffer push is silent

The row above used to name "rewrite the packet's own bytes over themselves and assert zero
bytes changed" as the confirmation. Run as written it **passes on a push that does
nothing**, and it took a screenshot to find out.

`FUN_800ee104` is where it is settled:

```c
DAT_8011a2d4 = &DAT_800fc55c;                    // the base starts at buffer A
do { ...; puVar4 = puVar4 + 0xee28; iVar2 = iVar2 + 1; } while (iVar2 < 2);
```

Exactly two primitive buffers, `0xEE28` apart — `0x800FC55C` and `0x8010B384`. Sampling
`PACKET_BASE_POINTER` live returns those two and nothing else. This is the one structural
difference from `positions` and `normals`: **those arrays are static and shared by both
buffers, so one write serves every frame; the packets are per-buffer.**

Measured, in a Gariland battle:

| what was written | bytes changed | what the screen did |
|---|---|---|
| every palette shifted by 7, one buffer | 385 | **nothing** |
| every palette shifted by 7, both buffers | 770 | the whole map retextured |
| restored, both buffers | 770 | back to the original, pixel for pixel |

The self-check would have read green on the first row, because a plan addressed at the
live pointer *does* reproduce that buffer's own bytes. What it cannot see is the other
buffer. An identity round trip is exact and blind — the same failure mode
`blender_corpus.py` records for the axis-2 baseline, in a different subsystem.

`check_packet_base` refuses a pointer that is neither, because writing the wrong base is
otherwise silent: every address involved is inside main RAM, `apply` reports a plausible
changed-byte count, and the only symptom is a picture that does not move.

---

### 5.1 A `Read` watchpoint cannot see the normal array, and that is not a finding about normals

The row above used to name a `Read` watchpoint as the instrument, on the reasoning that it
"must fire every frame, not once". Run as written it fires **zero** times, and the naive
reading of that — *the normals are not read per frame, so the whole design is wrong* — is
false. Measured, twice, on two fresh emulator processes:

| watchpoint | hits per frame |
|---|---|
| `Exec` on `FUN_8012cc54`, the textured-triangle renderer | **1** |
| `Read` on `0x8011A2D8`, textured-triangle **positions** | **2** |
| `Read` on `0x8011C498 + 180*0x20`, textured-quad positions | **2** |
| `Read` on `0x801251D4` / `0x80127394` / `0x80127394 + 180*0x20`, **normals** | **0** |

So the instrument is alive — it sees the renderer and it sees the position array in the
same loop iteration — and it is blind to whatever loads the normals, which the
decompilation says are `lwc2`'d straight into the GTE. Meanwhile zeroing those exact bytes
darkens the map. **A zero from this instrument is not evidence of absence**, and the
render A/B is what settles it.

Two operational notes that cost time:

- **Memory watchpoints need `-interpreter -debugger`.** Under the default dynarec, `Read`,
  `Write` *and* `Exec` breakpoints all report zero — including an `Exec` breakpoint on a
  function that is plainly executing. Always arm a control you know fires before believing
  a zero.
- **The callback's return value is `keepBP`.** Returning `false` deletes the breakpoint
  after its first hit, so a per-frame count silently freezes at 1. Return `true`.

The write path gets the check it already has: before any real push, rewrite the engine's
own bytes over the top of themselves and assert the write changed **zero** bytes. That
catches an off-by-one in a stride, a vertex offset, or a field mask — in the writer's own
arithmetic — and there is no good reason to skip it.

Two hazards carried forward from the built legs, both still live:

- **The savestate origin moves between saves** (17378312 / …314 / …318 / …325 on four
  consecutive pushes). It is re-derived every push. A cached constant writes every row at
  a few bytes' skew, which is a corrupt texture rather than an error.
- **A map reload uploads the disc's bytes over everything pushed.** Weather, time of day,
  leaving and re-entering. That is not a bug to fix; it is the reminder in decision 3.

And one from the geometry leg that will bite any new sink: FFT stores vertices **per
polygon**, welded to nothing, so a transform that is a function of anything other than the
vertex's own coordinates tears every shared edge. Confetti is usually the input, not the
write.

## 6. Getting to a battle

`PCSX.loadSaveState` on `reference-assets/thief_whats_this.sstate` lands **in** the Gariland
battle in one call, with all four buckets of MAP022 a0 in RAM. PCSX-Redux's own GUI
savestates are **gzipped** and `PCSX.loadSaveState` fails *silently* on one — `gunzip -c`
first. Full recipe in `tools/live_geometry.py`'s docstring.
