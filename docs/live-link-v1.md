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
what it compares against and why. Graded by `tests/blender_live_push.py` (**177/177**,
headless, a fake emulator that really parses the wire form), and accepted the only way a
rendering change can be: A/B/A on a live Gariland battle, through the button in both
directions — 0 changed bytes untouched, 1,676 waved, 1,676 back.

**The palettes reach the screen on 169 maps of 169, not 42** (2026-08-27). The palette leg
pushed main RAM alone, on a measurement taken on Gariland that generalised to 42 maps and
not to the other 127; on those the push was byte-perfect and invisible. It now writes both
sinks off one packing. This is a defect in the shipped editing loop, not in the swap — it
was inert on 127 maps before decision 10 existed. §2.3 carries the measurement and the
amendment; the VRAM half is **[fake]** until a render says otherwise.

**A whole map can be REPLACED, not only edited** (decision 10). *"I just want to be able to
hot swap entire maps."* A second button, *Replace the loaded map*, pushes this document over
whatever battle is loaded; the content self-check is exchanged for `live_link.check_plan_bounds`,
which is weaker and says so. Graded on a seeded foreign map in `tests/blender_live_push.py`.
Two legs it does not deliver and reports rather than hides: the terrain grid (units walk the
map you replaced) and the derived VRAM addresses (§5.3). **[fake]** — the acceptance A/B/A
against a real second savestate has not been run; §5.3 is what has been measured offline.

**A swap ERASES the host map's animation table** (decision 11, 2026-08-28). Reported as
*"one chunk of map got the blue water palette and it's animated"* after a Replace. The push
had landed -- what repainted it 4.49 times a second was the replaced map's `0x6c` instruction
table, still running at `0x80121D7C`. A swap now guards that table by **content** against the
110 `0x6c` chunks in the extracted disc tree, erases it, and installs the pushed map's own
palette records and `0x70` frames read from its **base resource, pinned by the document's
sha256**. The scope is the table: 94 of the 110 drive TEXTURE regions, and Gariland's eight
point inside the pages a swap has just uploaded a foreign sheet to. Texture records are
erased and not installed (#653: their VRAM base is the loader's). The readback is
**behavioural** -- the set of CLUT rows that move over the dwell must equal the set the
pushed map's own table names -- and the texture half is a byte confirmation reported in
different words. On *Push to PCSX* the animation is named and never touched. Graded by
`tests/test_live_animation.py` (49, plain `pytest`, rooted in the corpus and in the Gariland
savestate) and `tests/blender_live_push.py`, whose fake emulator now runs the table it holds.

**It runs on a stock pcsx-redux** (#606 part 2). Every endpoint the push uses is upstream —
main RAM is `/api/v1/cpu/ram/raw`, VRAM is `/api/v1/gpu/vram/raw`, the Lua dispatch is
`/api/v1/lua/<handler>` — and the two handlers no endpoint can supply (`ping`, and `gte` for
the light rig's cop2 control registers) ship with the addon as `pcsx_handlers.lua`:

    pcsx-redux -webserver -webserver-port 8080 -dofile addons/exmateria_map/pcsx_handlers.lua

The emulator loads nothing by itself, so the preferences panel offers three routes and the
artist takes the first that fits: **Launch PCSX-Redux** (Blender spawns it, flags already
right), **Set up auto-load** (a `pcsx.lua` shim in the emulator's working directory plus
*Show Lua editor* ticked in its `pcsx.json` — after which a plain double-click works, measured
end to end), or **Copy launch command**. The shim's two halves are both load-bearing: with
the Lua editor pane hidden the emulator is up and `cpu/ram` answers 200 while `lua/ping` is a
404. It refuses to overwrite a `pcsx.lua` it did not write, because that editor's `Auto save`
is on by default and an existing one is the artist's own script.

The constraint that shaped it: on stock a Lua handler receives its payload **only through
the URL** — a POST body is not exposed to Lua at all — and the request line is capped, where
overflowing it is a **silent 404** rather than an error (bisected: 251 bytes runs, 252 does
not; `POST` moves the cliff to 250, so the bound is `len(method) + 1 + len(url) <= 255`).
`live_link.URL_LIMIT` refuses by name rather than let a caller read that 404 as a missing
handler, and `gte_queries` splits a write list by measured length rather than a pair count.
The eight-register rig is one request at under half the budget.

The fork is still what `tools/live_*.py` drive, and the packed-Lua walk is kept for them: it
is the faster of the two and it accepts a write longer than 65,535 bytes. There is **no
transport preference** — `live_ram_over_http` stood in the panel from part 1 and was removed
in part 3, because its off position needed the fork and failed as `lua/exec 404: URL Not
found.`, which names nothing an artist could act on. `blender_live_push.py` now asserts the
opposite property: that a push never trips the packed-Lua walk at all.

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
| palettes, RAM sink | **built** [LIVE] — §2.3, A/B/A. Correct on the 42 resources that animate their palettes, inert on the other 127 |
| palettes, VRAM sink | **built**, **[fake]** — §2.3, `plan_clut`. The only sink that reaches the screen on those 127; a render has not confirmed it |
| polygon COUNTS (shrink + growth) | **built** [LIVE] — decision 8, #598, A/B/A on a live battle. Both growth GATES are fake-RAM only |
| bytes 6-7 (binding + VISIBLE_ANGLES) | **built** — 1,816/1,816 against a captured Gariland RAM |
| camera sync (Blender viewport → battle camera) | **built** — decision 12 and its three amendments. The button, the continuous toggle, the section and the arithmetic, accepted on a live Gariland battle by the artist (*"this works incredible"*), which is what closes the sink A/B in `work_position`'s favour. Emulator → Blender is not built |
| isolate the map (units, shadows, HUD, cursor, camera leash, boxed dialogue) | **built** — decision 13, 2026-08-28 (Amendment 1 adds the camera leash, Amendment 2 the dialogue box). Two buttons in an `Isolate` panel: the list walk, the per-unit `+0xa`/`+0x1d8` gate and the four code pokes, restored from SAVED values. The walk and both plans are re-measured against the Gariland savestate on every `pytest` run — 11 units, the ids read as the **byte** `unit_sprite_object_find` matches on — and the Blender half is graded by `blender_live_push.py`. **Not accepted on a picture yet**: the artist's eye is the acceptance, and the cursor's poke target is the one address decision 13 ships uncertain. Boxed dialogue is still out |
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
to be needed. The savestate round trip existed solely because two docstrings held that
pcsx-redux cannot write VRAM, and **that was false** — `POST /api/v1/gpu/vram/raw` writes perfectly
well, and a bare POST is a 400 only because the rectangle belongs in the query string.
Measured [LIVE] by A/B/A on a Gariland battle, 2026-08-26. What survived the move is the
geometry (`locate`, `identify`, `diff`, the page stride and row pitch), which was always
about VRAM; the savestate was only ever the container it was read through. The origin drift,
the search window, the live cache and the size-settling poll went with the container.

**The viewport aims the battle camera** (decision 12, 2026-08-28). Reported as *"Blender is
looking at one part of the map and the emulator at another"*, which makes an authored map
impossible to compare against what the engine renders. A *Match camera* button in a new
`Camera` section pushes the Blender view's pose -- pivot, pitch, yaw, zoom -- into the running
battle, **faithfully**: past the pad's eight yaw notches, its 13-degree pitch band and its two
zoom steps, because that envelope is the very thing that makes a map uninspectable. It also
pokes the engine's own vertical datum to 120, which is what closes the 40-world-unit gap the
artist was actually seeing; the cost, named, is that the emulator's framing is then not
authentic.

The camera model itself is now an **assertion** rather than prose: `Rx(pitch)*Ry(yaw)*Rz(roll)`
reproduces the engine's own view matrix in
`reference-assets/thief_whats_this.sstate` within the 4096-quantization floor, and the rival
orders and both sign flips are asserted to fail, because a fit that only checks the winner
cannot report that the winner stopped winning. The Blender half has no savestate to be graded
against -- the savestate holds a pose the *engine* authored -- so it is graded by geometry: the
three axis-aligned viewports whose FFT pose can be worked out by hand, and a spec on 96
turntable views saying what "synced" means. `tests/blender_live_push.py` is **193/193**.

**[fake]** in one specific sense: which link in the chain survives a write during a running
battle has not been measured, and the button carries a **behavioural** readback that says so on
every press -- it requires `CAMERA_VIEW_MATRIX`, which the engine recomposes every frame, to be
the matrix the pushed pose implies. Amendment 1 to decision 12 reverses that A/B's favourite on
the evidence of F14: `work_position` is the default sink, not the scratch struct.

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

#### What triggers a rig push (2026-08-29)

This section is the **sink**; it says nothing about when anything arrives at it.
Until ADR-0186 Amendment 14 the answer was *"only when the source art changed"* —
`settle_op`'s clock watched the painting's pixels alone, so every byte in the
table above had a working transport and no trigger, and moving a light reached
the emulator only if a brush stroke happened to follow it. Amendment 14 gives
the settle clock three more witnesses — the lamps (under **Lamp authority**),
the rig **Override**s, and the previewed state — and routes them to this sink
with no compile in between. The 39 bytes here are unchanged by it; the six
gradient bytes stay out, for the reason above.

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

**The palettes are written to BOTH memories, and the amendment is the finding.**

> **Amended 2026-08-27.** What stood here was: *"the palettes are NOT written to VRAM, and
> this is the finding — their address is right and it is not a sink."* It was measured by
> writing one CLUT row on a live **Gariland** and reading it back at four delays, with a
> sheet write made in the same session as the control:
>
>     VRAM (x=80, y=480)      written, 0/32 differ immediately
>                             32/32 back to the ORIGINAL bytes at 50 ms, 0.2 s and 1 s
>     VRAM sheet row          written, 0/128 differ immediately AND after 1 s
>     RAM 0x800E4EA4 + 160    written, 0/32 differ after 1 s, and VRAM's row 5
>                             moved to match within 0.3 s
>
> Every number is still true. The conclusion drawn from them — *"the engine re-uploads the
> whole CLUT block from main RAM every frame"* — is true **on Gariland and on 41 other maps,
> and false on 127.**

> **Note on the denominator (2026-08-27).** "42 of 169" counts *textured resources carrying a
> `0x70`*. The chunk that decides behaviour is the `0x6c` **instruction table**, and it is
> carried by **138** resources: 60 drive CLUT rows, 94 drive texture regions (44 do both), 28
> tables are present but empty. Scoping a fix by the 42 scopes it to the palette half of a
> table whose majority is texture records. See `CONTEXT.md`, *Animation instruction*.

**The per-frame re-upload is the palette ANIMATION doing its work**, and only 42 of the
corpus's 169 textured resources carry one. `mapfile.read_palette_animation` is present on
`MAP022.9` — Gariland, where all four delays above were measured — and **absent on
`MAP062.8`**, Orbonne. Surveyed over `project-assets/fft-extract/MAP/`, 2026-08-27:

| textured resources | carry a `0x70` palette animation | do not |
|---|---|---|
| 169 | **42** | **127** |

So on 127 maps nothing re-uploads the block after map load, and the two sinks swap roles:

    MAP062, RAM push          0 of 512 bytes off the document -- byte-perfect
    ...and VRAM's CLUT rows   all 16 rows still ORBONNE's, and nothing ever moved them
    MAP062, VRAM CLUT write   0 of 512 back at +0.0 s, +0.5 s and +2.0 s
                              -- and it SURVIVED a full map load

Measured [LIVE] on the artist's Orbonne, 2026-08-27. **A RAM-only palette push has always
been inert on those 127 maps, swap or no swap.** It was only ever measured on Gariland, which
is one of the 42, and the artist met it as *"right texture, wrong palettes"*.

**The push therefore writes both, and neither is a fallback for the other:**

- **RAM `0x800E4EA4`** — durable and winning on the 42 that animate, where the engine
  re-uploads it over the VRAM rows every frame. Unchanged.
- **VRAM's CLUT column** — the only sink the artist can see on the other 127. On the 42 it is
  harmlessly overwritten on the next frame, which *is* the behaviour the four delays above
  measured.

One packing feeds both (`live_link.clut_rows`), aimed at RAM by `live_link.plan_palettes` and
at VRAM by `live_vram.plan_clut`. Two sinks for one document field is two chances to write
different colours, and the divergence would surface as *"the palettes are wrong on some
maps"* — the hardest possible symptom to trace back to a planner — so the shared packing is
the point and `test_the_two_sinks_carry_the_SAME_bytes_for_a_row` is what holds it.

`live_vram` used to offer no palette writer at all, with
`test_this_module_offers_no_way_to_write_a_palette` holding the absence. That guard was
converted rather than deleted (the shape `92a587bcd` used for #646's three arms): what it
protects now is the thing two sinks can actually get wrong.

**`check_clut_block` guards less than it looks like it does.** It compares the RAM block
against what VRAM is showing before writing, to tell `CLUT_BLOCK` from the second copy at
`0x80099D76`. On MAP062 it **passed** pre-push — both sides held Orbonne's — and the write
still went nowhere. Agreement establishes that the address is the one feeding the screen; it
does **not** establish that a write to it arrives. What answers that is the VRAM readback,
which the push now takes on both sinks.

**A second copy of those 512 bytes sits at `0x80099D76`, and a push into it does not reach
the screen** — writing row 5 there moved 0 of 32 VRAM bytes. A content scan finds both, so
the address is not trusted for being written down: `live_link.check_clut_block` compares the
block against what the GPU is actually showing before a byte of it is written, which is
decision 2's locate-by-verify at the one address here a scan cannot settle.

That block was first written up here as an *"inert twin"*, which was right about its
behaviour and **wrong about what it is** (corrected 2026-08-27, #624). It is the map's own
`0x44` chunk as the loader left it, and it is not idle: each animated entry is written into
**both** blocks in one loop body — `0x800926AC` into this one, `0x8009269C` into `CLUT_BLOCK`
— confirmed by watchpoint (60 and 20 hits, one writer each). A push into it is ineffective
because nothing re-uploads a *static* row from either block after map load, not because the
block is dead. Anything that wants to hold an **animated** row has to contend with both.

> **Amended 2026-08-27 (the animated-palette leg).** What stood here called that loop body
> *"the palette-animation routine … one function, `ra = 0x80092794`"*. **It is not the
> animation.** The function containing both stores is `clut_strip_load_base` @`0x80092620`,
> a shared CLUT loader taking `(source, block, row)`, and it has exactly **two** callers,
> both inside `color_field_dispatch` @`0x800926D8` — the `{33}` **Color Field** opcode
> handler, which `CONTEXT.md` classifies as a *modulator*. `ra = 0x80092794` is the
> instruction after that function's single-row `jal`, so the watchpoint identified the
> **helper**, one frame below whoever called it. The palette animation is one of
> `color_field_dispatch`'s 24 call sites and **has not been identified**. Every byte
> measurement above stands; the name attached to them did not.
>
> The corollary is that a `Write` watchpoint answers *who stores* and not *what drives* —
> and here the two were different functions. What settles the second question is the data:
> the map's own `0x6c` table names its animated rows, and poking that table moves the
> picture (see decision 11).

**`CLUT_BLOCK` is block 0 of fourteen, not a 512-byte block.** `clut_strip_load_base`
computes its destination as `0x800E4EA4 + block*512`, and `clut_view_strip_init`
@`0x80093048` initialises **14** blocks (its loop runs `0 → 0x851C` step `0x982`).
`flush_clut_view_strip` @`0x80092F98` uploads all 7,168 bytes as one `256 x 14` rectangle at
VRAM `(0, 494)` — but only when `DAT_800995EC` is non-zero, a dirty flag set by
`color_field_dispatch` and cleared by the flush. Measured [LIVE] 2026-08-27: blocks 1-13
against VRAM rows 495-507 are **0 of 512 bytes different each**, and VRAM `(0, 480)` — the
line the polygons actually sample — is **0 of 512** against block 0.

That dirty flag is the mechanism behind the 42/127 split named above, stated properly: a map
with no animation never runs the path that sets `DAT_800995EC`, so nothing carries the RAM
block to VRAM and a write there is byte-perfect and invisible. Which also means the split is
a property of *the flag being set*, not of the animation as such — a leg that ever wants the
RAM sink to work on the other 127 has a candidate, unmeasured, and named here rather than
attempted.

**Some CLUT rows are engine-animated and cannot be pushed.** Writing all 16 and reading back
named rows 13, 14 and 15 as reverted on MAP022 a0. That set is still *reported from the
readback rather than predicted* (decision 3) — but that is now a rule about what a report may
claim, **not a limit on what is knowable**, and the two reasons this paragraph used to give
for it are both false:

- *"The period is unknown."* It is byte 17 of the record. `MAP022.9` reads 12 ticks; measured
  [LIVE] 2026-08-27 at **4.49 steps/s** on all three rows, and poking that byte to 60 took the
  poked row alone to **0.99 steps/s** while its two siblings held 4.46. Corpus-wide the
  slowest palette record is 30 ticks, so **0.6 s** is a dwell that cannot miss one.
- *"A probe short enough to run inside a press can report nothing animated."* Only a probe
  that guesses. One sized from the table's own `duration` bytes cannot, which is what
  decision 11's readback does.

It is also why no disc resource matches all 16 live rows — the live block differs from
`MAP022.9`'s own `0x44` chunk by 35 bytes over rows 0, 7, 8, 10, 13 and 14. **Only 13, 14 and
15 of those are the animation**, and this paragraph used to attribute all six to it.
`MAP022.9`'s `0x6c` carries exactly three palette records, naming 13/14/15 and nothing else,
and a 2.5 s readback finds those three rows moving and the other thirteen still. Whatever
moved rows 0, 7, 8 and 10 away from the disc's bytes did it **once**, and is unidentified.

`mapfile.PALETTE_ANIM_PTR` (`0x70`) **has a reader** since #624 —
`read_palette_animation` for the frames, `read_animation_instructions` for the table that says
which rows they drive.

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
| `map_states[].palettes` | `0x800E4EA4` in **main RAM** **and** VRAM's CLUT column — both, see §2.3 | **built**, the addon's button — RAM half A/B/A screenshot [LIVE]; **the VRAM half is [fake] pending a render** |
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

> **Second amendment, 2026-08-27 — and the identity claim comes back as a MODE.** The
> write-path self-check above recovers, as a side effect, the very claim this amendment
> stopped making: RAM holding the document's own bytes *is* an identification. Decision 10
> names that, and makes it a choice rather than an accident — the artist says whether they are
> editing the loaded map or replacing it, because no RAM address holding the current map id is
> known and the declaration has to come from the person who loaded the savestate.

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

### Decision 10 — *editing the loaded map* and *replacing it* are two acts, not one with a flag

Reported as the whole of what the loop is for: *"I just want to be able to hot swap entire
maps."* Load any battle, push any `MAP###.a#` document, see that map.

Most of it was already built and this decision does not rebuild any of it. The engine's four
polygon arrays are **fixed-capacity and engine-global** (ADR-0004 decision 28), not per-map
allocations, so a different map's polygons go in the same slots — a swap is *"write different
bytes into slots that already exist"*, which is exactly what the push does. Decision 8's count
ordering already handles a size change in both directions. What stood in the way was one
thing: **the self-check's premise.**

`selfcheck` demands that RAM already hold the document's own bytes. That is decision 7's
mechanism, and it is also — as decision 7's own text says — the identity claim decision 2's
amendment stopped making, *recovered as a side effect*. A swap violates it on purpose. And it
cannot simply be stood down: it is what catches an off-by-one in a stride, a vertex offset or
a field mask, before thousands of bytes go to a guessed address.

So the check is **exchanged, not dropped**, and the two acts are named:

- **Push to PCSX** — edit the map the emulator has loaded. `selfcheck` as before.
- **Replace the loaded map** — install this document over whatever is there.
  `live_link.check_plan_bounds` in its place.

The bounds proof is the one fact about these addresses that does not depend on which map is
loaded: every planned address must land inside the array it names. It grades the **plan**, not
the descriptor's counts — `check_capacity` already does the latter and cannot see a plan built
at a wrong origin, which is precisely the class of bug the content check was standing in front
of. The extents are cross-checked rather than restated: `SINKS`' six bases were measured one
at a time against a live battle, `ENGINE_CAPACITY`'s four numbers are `slti` immediates, the
strides are vertex counts, and each array's end has to land on the next thing measured — the
textured-quad normal array's on `FUN_8012cc54`'s first byte, which is why that address is in
the module at all.

**It is weaker, and the report says so.** A wrong stride that stays inside the array passes
here and would have failed there. A weaker check reported in the same words as the strong one
is worse than no check: the artist reads *"self-check passed"* and believes the thing that was
not proved.

**Two buttons, not a checkbox.** A checkbox reads as a setting on one act, and would leave the
artist to notice on their own that the check quietly became a different one. `skip_selfcheck`
stays hidden and stays out of the panel — a swap now has a proof it can pass, so an artist
never needs the escape hatch that has none.

**Two legs a swap does not deliver, both said in the report rather than left to be found.**

- *The terrain grid.* `UNPUSHED` has named it on every push. On the artist's own map it is a
  curiosity about one field; on somebody else's it is the whole story — units walk the map
  that was replaced while looking at this one. So the swap says it in its own words. **This is
  a picture, not a playable map**; `build` is what ships one.
- *The sheet and the CLUT rows.* Their VRAM addresses are **derived** from the live packets
  (decision 5), and on a swap those packets belong to the map being replaced. The derivation
  is self-consistent — the packet plan keeps the loaded map's base bits and replaces only the
  two masked fields, so the polygons point at the column the sheet is written to — but it is
  the *replaced* map's layout both halves agree on. §5.3 is what that measured out to.

Graded against the fake emulator by seeding a coherent **foreign map**: the document's own
polygons displaced, at the same addresses, with the descriptor declaring three quads where the
document has one. Garbage would have passed an arm that only asked *"is RAM not ours"* and
would not have exercised the shrink. The default mode refuses it, as it always has; the swap
replaces the geometry **and** the counts.

> **Amended 2026-08-27 — the goal line moves.** Stated by the artist, in these words:
> *"when you replace the map, the goal is the total removal of the old map, and adding the new
> map."* This decision's closing line — *"This is a picture, not a playable map"* — was
> written as the **intended end state**. It is now **outstanding debt**. Nothing about the
> built behaviour changes; what changes is that the terrain grid, and anything else the host
> map still owns after a swap, is no longer settled scope that a future session may read as
> decided. `UNPUSHED` names them; naming is the interim, not the answer.
>
> The first item paid down under this line is the host map's **animation table**, which was
> not in the list at all because nobody had looked: see decision 11.

### Decision 11 — a swap erases the host map's animation table, and installs the new map's palette half

Reported by the artist, live: *"I just did a Replace the loaded map call and it looks almost
right except for one chunk of map which got the wrong palette. It got the blue water palette
and it's animated."*

Closed end to end, offline, from the corpus and one live battle:

| | |
|---|---|
| host | `sstate2` = **`MAP022.9`**, Gariland. Its `0x6c` names CLUT rows **13, 14, 15** |
| pushed | **`MAP002.9`** — descriptor counts `49 / 395 / 4 / 52`, read live |
| overlap | MAP002 names rows 13 and 14 on **59 of its 444** textured polygons (13.3%) |
| the push itself | **landed** — `CLUT_BLOCK` rows 0-12 are MAP002's, **0 of 416 bytes off** |

So the wall is not a failed push. It is a correct push being repainted 4.49 times a second by
a map that was supposed to be gone. `MAP002` carries **no `0x6c` and no `0x70` at all**, so on
this pair a swap can only ever lose an animation, never gain one.

Worth knowing for the next report of this: **14 corpus resources carry `MAP022.9`'s `0x70`
frames byte for byte** (`MAP001.9`, `MAP003.9`, `MAP006.12`, `MAP007.9`, …). The blue water
cycle is a shared asset, so *"it looks like water from a map I have never opened"* will recur.

#### The scope is the table, not the palettes

The ask was "handle animated palettes." Palette records are the **minority** of the chunk that
causes this: 138 resources carry a `0x6c`, 60 drive CLUT rows and **94 drive texture regions**.
Gariland's own table is 3 palette records and **8 texture records**, and those eight point at
`x = 839..923, y = 28..208` — *inside* the four VRAM pages MAP002's sheet was just uploaded to.
A fix scoped to palettes leaves them copying rectangles around inside the new sheet and would
be reported next, in words that sound unrelated. The unit is the table.

#### The five parts

**1. Erase the host's table.** The live table is at **`0x80121D7C`** — 20-byte stride, disc
layout, byte-identical to the loaded resource's `0x6c` except bytes 14/16/18/19 of the running
palette records. Zeroing a record stops that record's animation and nothing else. Measured
[LIVE] 2026-08-27, self-controlled:

    baseline                     row13 4.49/s   row14 4.49/s   row15 4.49/s
    record 0 duration 12 -> 60   row13 0.99/s   row14 4.46/s   row15 4.46/s
    record 0 zeroed              row13 0.00/s   row14 4.57/s   row15 4.57/s
    restored                     row13 4.46/s   row14 4.46/s   row15 4.46/s

An all-zero record is **the corpus's own encoding for no animation** — 21 of `MAP022.9`'s 32
slots ship that way and the engine already walks them every frame — so this writes what the
disc writes rather than disabling a feature. A second structure at `0x800F6DC4` (24-byte
stride, sharing each record's leading 8 bytes) is **not** what drives the picture; the poke is
what separated them, and inspection would not have.

**2. Guard it by content, not by the address.** Decision 5's locate-by-verify. The address was
confirmed on one battle; writing 640 bytes there on any other is a bet. Compare the live table
against all 110 `0x6c` chunks in the corpus, ignoring the runtime bytes, and **refuse unless it
matches a map's**. Measured: the live table matches exactly 6 resources and all six are MAP022
states; corpus-wide there are 83 distinct tables among 110 carriers, largest identical group 6.
This also stops the case that cannot be ruled out — if anything other than a map ever writes
records there, it matches nothing and we stop instead of erasing it.

**3. Install the new map's palette records and frames, read from its BASE.** The interchange
document does not carry `0x6c` or `0x70`; schema §8 and `build` put both on the *carried from
base* side. So the live link reads the previewed state's **base resource** off the extracted
disc tree, which is a dependency the package already declares — `CONTEXT.md`, *Base map*: a
document "is a diff against it, never a replacement… and pins the one it expects by a sha256
per resource." The pin makes the read **verifiable** rather than hopeful.

Rejected: putting the chunks in the document. It would make them look authorable when nothing
in the preview can show an animation, and it would put `build` in the business of writing bytes
it currently copies, on the one leg whose entire value is byte-exactness over 1,575 files. The
shape chosen does not foreclose it — if animation ever becomes authorable, the install reads
the document instead of the base and nothing else changes.

The animation is **per map state**, not per arrangement (`MAP022.9/.31/.37/.43/.49/.55` each
carry `[13,14,15]`; `.13/.17/.21/.25` carry none), so decision 9's existing aim already picks
the right resource and no new aiming rule is needed.

**4. Texture records are erased and NOT installed.** A palette record needs no translation —
the CLUT line is `y = 480` on every map, forced by the packet encoding that gave `0x7800` on
385 of 385 polygons. A texture record is **absolute VRAM** against its own map's sheet base,
and that base is assigned by the loader: it is in neither the document nor the base resource,
and it is not a constant — 439 of 577 texture records sit in the `x >= 768` band but 80 sit at
`x = 0` and 18 at `x = 192 / 704`. Rebasing by the dominant value would be right for most and
silently wrong for ~98 records with no way to tell which, which is the failure
`live_vram.derive_addresses` exists to prevent (*"one disagreeing witness is a refusal, not a
vote"*). **Named debt, not accepted scope** (#653), per decision 10's amendment.

A further ~40 records name rectangles **outside VRAM** (`x = 3840, 61440, 61632`;
`y = 3840, 4032, 61440, 65472`). Those are *absent* records, never corrupt files — schema
§10.3's terrain rule applied here — and must be refused rather than written. `is_palette` does
not screen for it.

**5. Only on *Replace*. On *Push to PCSX*, report.** The rule is one line: **neutralise foreign
animation; never neutralise a map's own.** On the edit path the animation belongs to the
document's own map, `build` will carry `0x6c`/`0x70` to the disc verbatim, and freezing it
would show the artist a picture the shipped map can never produce — the loupe lying in exactly
the way the shared palette packing exists to prevent. So that path names the rows this map
animates, read from its own table, and says the colours are in the document and on the disc and
the battle repaints them. That reporting half is needed on the *Replace* path anyway
(decision 4), so this is **cheaper than saying nothing**, not dearer.

#### Order, and what the push may claim

**Erase, then palettes and sheet, then install.** Erasing first means the last host frame is
overwritten by the document's colours rather than racing them; installing last means a pushed
map's own animation is not immediately flattened by the static palette write behind it.

The readback is **behavioural, and phrased as the goal**: sample `CLUT_BLOCK` twice across the
dwell and require that **the set of rows that move equals the set the pushed map's own table
names**. A host row still moving is the old map not removed; a pushed row not moving is the new
map not added; and for a map with an empty table it reduces correctly to *nothing moves*. The
dwell is `max(duration)/60`, which is **≤ 0.6 s** for every palette record in the corpus.

A byte readback is **not** sufficient on its own here and the reason is on the record:
`check_clut_block` **passed on Orbonne with both sides holding Orbonne's palettes while the
write went nowhere**. Bytes prove the values are at an address, never that anything reads it.

**The texture half is byte-confirmed only** — its dwell runs to 4.00 s and that is not spent
inside a press — and it is reported in **different words**, because decision 10 already states
the rule: *"a weaker check reported in the same words as the strong one is worse than no
check."*

#### It degrades in one direction

**Erasing needs nothing from the disc** — it is a write of zeros to a verified, content-guarded
address. Only the *install* needs the base resource. So a missing extracted disc tree, or a
sha256 that does not match the document's pin, costs the install and not the removal: the
artist gets a correct static picture and a line saying the animation was not installed and why.
The whole push is **not** refused over an animation chunk, and the animation leg is **not**
skipped silently.

#### Open, and named rather than assumed

- **The animation's caller is unidentified.** Every write to `CLUT_BLOCK` goes through
  `clut_strip_load_base`, whose only callers are inside `color_field_dispatch` — 24 call sites,
  and which one ticks the map animation is not known. Nothing in this decision depends on it:
  the lever is the data, not the code path.
- **`duration = 0`** (#654), on 2 palette and 93 texture records, is undecoded. It may mean *every
  tick* or *inert*. A dwell sized from it computes to zero, so those records need a floor, and
  the floor is reported as an assumption rather than hidden.
- **The sheet base derivation** (#653), which blocks part 4.

> **BUILT 2026-08-28.** `live_link.py`'s animation section, `live_link_ui.py`'s
> steps 5b-bis and 5d, and the two buttons. Graded by
> `tests/test_live_animation.py` (49 checks, plain `pytest`) and
> `tests/blender_live_push.py` (23 new arms, 177/177, two seeded defects).
>
> **The savestate answers part 2 offline.**
> `reference-assets/thief_whats_this.sstate` is a real Gariland battle, and
> `0x80121D7C` in it holds `MAP022.9`'s `0x6c` chunk, differing at exactly
> bytes 14/16/18/19 of its three running palette records and **nowhere else in
> all 640**. So the address, the runtime mask and the content guard are all
> graded without an emulator, and the decoy at `0x800F6DC4` is in the same
> image, sharing each record's leading eight bytes -- which is the arm that
> shows why the guard is on the content.
>
> **Three numbers above are wrong, corrected by measurement:**
>
> - **83 distinct tables on the disc, but 82 under the mask.** Masking byte 14
>   collapses `MAP061.9` and `MAP061.10`, which differ only there -- byte 14 is
>   the frame count on the disc and the engine's frame cursor in a running
>   table. Two states of ONE map, and the guard claims "some map's table" and
>   not "this map's", so it loses nothing.
> - **479 texture records sit at `x >= 768`, not 439.** 479 + 80 + 18 = 577,
>   which is the arithmetic part 4 already does.
> - **Three of the six out-of-VRAM coordinates are one nibble low**: `x`
>   reads 3,840 / 61,440 / **61,680** and `y` reads 3,840 / **4,080** / 61,440 /
>   **65,520**. 84 non-empty records name a rectangle that does not fit in the
>   1024x512 frame buffer; 40 of them by `x` alone.
>
> **Two things this decision does not say, and both would have shipped a check
> that cannot fail:**
>
> - **A pair of samples is not a readback.** `MAP022.9`'s frame 3 is
>   byte-identical to its frame 1 (§`test_palette_animation.py`), so a pair
>   that straddles two steps reads three running rows as still and calls a
>   healthy install *the new map not added*. `moved_clut_rows` is variadic,
>   samples five times **across** the dwell, and refuses a single sample.
> - **A held image cannot answer a readback.** `RamClient.hold()` answers every
>   `read` from one 2 MB fetch (Amendment 7, decision 32), so five samples of it
>   would be five copies of one instant. `read_live` is the one read in the
>   module that must cost a fetch, and it neither reads nor fills the held
>   image.
>
> **One departure from the letter of the degradation rule.** *"A missing
> extracted disc tree costs the install and not the removal"* cannot hold, and
> part 2 is why: the erase's only proof that it is writing to a map's table is
> a match against the corpus, and a candidate set of nothing verifies nothing.
> `check_animation_table({})` **refuses**, and says so in those words. The
> animation leg therefore degrades as a unit; the half of the rule that does
> hold -- a base resource whose sha256 does not match the document's pin costs
> the install and leaves the erase standing -- is built and graded. The rest of
> the push is unaffected either way.
>
> **#654's palette half is empty.** Both `duration = 0` palette records
> (`MAP053.8`, `MAP053.22`) carry `frame_count = 0` as well, so they animate
> nothing, name no row, and are never dwelled on. The floor stands behind an
> authored or foreign table instead, is set to the corpus's own slowest palette
> step (30 ticks), and is reported as the assumption it is. The 93 texture
> records are untouched by this leg.
>
> **Two things the live machine refuted, both after the offline build passed.**
>
> - **A disc record is INERT until the loader arms it, and byte 19 is the
>   flag.** The running emulator held `MAP022.9`'s three palette records at
>   `0x80121D7C` byte for byte off the disc -- the state a verbatim install
>   leaves -- and rows 13, 14 and 15 were **still**. Measured [LIVE]
>   2026-08-28, one byte at a time with the record's own siblings as the
>   control: writing **byte 19 = 1** into record 0 alone started row 13 and left
>   14 and 15 at zero, and putting the record back stopped it again. Byte 14
>   (`0x81`) and byte 16 (`0x02`) alone did nothing, so the engine initialises
>   the rest from the record it is handed. The disc ships byte 19 clear on 127
>   of 128 palette records and every running record in the savestate carries
>   it, so it is the loader's field. `plan_install_animation` sets it and
>   carries every other byte verbatim.
>
>   **This is the case the behavioural readback exists for.** The chunk really
>   was at the address, byte-perfect, and nothing read it -- the same shape as
>   `check_clut_block` passing on Orbonne while the write went nowhere, and the
>   second instance of it on this leg.
>
> - **The guard refused the table this leg leaves behind.** After one Replace
>   the table holds the pushed map's palette records and otherwise-empty slots,
>   and **no map on the disc ships a table like that** -- so the second press
>   matched 0 of 110 and refused, on the machine the leg was built for. A live
>   slot that is EMPTY where a candidate's is not is what this leg itself
>   produces, and it is now forgiven; a slot that is PRESENT and different is
>   still a refusal. An **all-empty** table matches nothing rather than
>   everything: it is compatible with every map on the disc, which is not a
>   match, and there is nothing there to erase anyway. The cost is named -- on
>   a host with no animation table, this document's own animation is not
>   installed, because an empty table cannot confirm the address. That is what
>   every push did before this decision existed, and it is **#659**.
>
> **Accepted [LIVE] 2026-08-28**, whole leg, shipped code, against a running
> Gariland battle: the guard matched the six MAP022 states, the erase changed
> 24 bytes, the install 27, and the readback said *"CLUT row(s) 13, 14, 15 move
> and no others do, which is exactly what this map's own table names"*. The
> texture half read back clear.
>
> **The frames may live on a sibling.** `MAP053.19` and `MAP061.10` declare a
> palette animation with a null `0x70` pointer and keep their frames on `.8`.
> `MAP053` a1 pins one resource, so the sibling is **not** pinned by the
> document, and the provenance says which resource the frames came from rather
> than reporting them in the same words as a pinned read.

### Decision 12 — the Blender viewport drives the battle camera, and the engine's own datum is what makes the centres agree

Reported by the artist: *"Blender is looking at one part of the map and the emulator at
another"*, so an authored map cannot be compared against what the engine renders. Settled by
grilling on 2026-08-28, before any of it was built.

**Built 2026-08-28, except the continuous timer.** The `bpy`-free arithmetic is in
`live_link.py` -- `camera_rotation`, `camera_angles`, `camera_position`, `camera_zoom`,
`camera_pose`, `plan_camera`, `camera_readback`, `check_view_syncable` -- and the *Match
camera* button and the `Camera` section are `live_link_ui.py`'s. Graded by
`tests/test_live_link.py` (the camera model against the battle savestate, and the Blender
half against geometry) and `tests/blender_live_push.py` (**193/193**, headless, seeded).
The **timer is built too, on 2026-08-28**, after the button was accepted on a live battle:
`CameraSyncTicker` in `live_link.py` holds what a tick decides, `sync_camera` and
`_camera_sync_timer` in `live_link_ui.py` are the tick and the `bpy.app.timers` callback, and
the `Sync camera continuously` preference is the toggle, ON by default. **209/209** on
`blender_live_push.py`, 179 in `test_live_link.py`. See Amendment 3.

**Two of this decision's own premises were refuted by re-reading the savestate, and the
amendment at the end of it carries both.** Neither changes what is built; one changes which
sink is the default and one deletes a trap that was about to be designed around.

#### Why the push goes one way only

The direction is **Blender → emulator**, and the reason is not preference. The battle camera's
player-reachable envelope is tiny (`research/wiki_articles/battle_camera_system.txt` §2.2):

| axis | control | what the pad can reach |
|---|---|---|
| yaw | L1/R1 | full 360° but **snaps to 45°** — 8 poses, nothing between |
| pitch | D-pad | **`0x12E`–`0x1C0` only** (26.5°–39.4°), and only ever *more* down |
| roll | none | fixed 0 |
| zoom | L2/R2 | **`0xC00`–`0x1000` only** (0.75×–1.0×) |
| position | none | not player-controlled — smooth-tracked to the cursor tile by `FUN_8008B6E4` |

*"I can't move the camera freely in game."* Emulator → Blender would therefore only ever
replay eight poses the artist already cannot escape. It is **not built**, and it is named here
rather than silently omitted, the way decision 4 handles unpushed sinks.

#### The camera model, and what was already closed before this decision existed

The engine is `screen = R·(world − work_position) + camera_tracked_target`, orthographic,
with `R = Rx(pitch)·Ry(yaw)·Rz(roll)` — right-handed elementary rotations, **positive** signs,
4096 = 360°, on PSX world axes (X lateral, Y **down**, Z depth).

This is not re-derived here. F4 in `camera_framing_pivot_decode.md` fitted it to **65 live
(angles → R) samples** off a chapel cinematic at max error 0.0019. This decision confirmed it a
second time, **offline and in a battle** — `reference-assets/thief_whats_this.sstate`, Gariland,
RAM located by the same verify-don't-offset locator `test_live_link.py` already uses:

| read | value |
|---|---|
| `work_position` `0x800E4E74` | `[745472, 19456, 630784]` = `(182.0, 4.75, 154.0)` world units = tile `(6.5, ·, 5.5)` |
| `work_rotation` `0x800A7784/86/88` | `[302, 4608, 0]` — pitch 26.5°, yaw 405°, roll 0 |
| `sprite_scale` `0x800C7CA0` | `[4096, 4096, 4096]` = 1.0× |
| `camera_view_matrix` `0x80098A24` | `[2892,0,2896; 1294,3661,-1293; -2589,1830,2584]` |
| scratch `+0x68/6C/70` | `[745472, 19456, 630784]` — **byte-identical to `work_position`** |
| scratch `+0x74/78/7C` | `[302, 0, 4608]` |
| scratch `+0x80` | `4096` |
| `saved`/`start`/`current` `0x801B8AD8…` | all `[4096, 4096, 4096, w=0]` |

`Rx(302)·Ry(4608)·Rz(0)` reproduces that matrix at **max error 0.00172** — the 4096-quantization
floor. `Ry·Rx·Rz` and `Rz·Ry·Rx` land at 0.316, `Rx(−p)·Ry(y)` at 0.894, `Rx(p)·Ry(−y)` at 1.414.
The model holds in **both** game modes, cinematic and battle.

Five things that reading fixed, each of which a design was about to be built on:

* ~~**The scratch struct's angle offsets are mislabelled.**~~ `renames_high.tsv` calls
  `+0x74/78/7C` pitch/yaw/roll, and the live struct read `[302, 0, 4608]` while the camera's
  yaw is 4608, so this decision put the yaw at `+0x7C`, the slot labelled roll. **REFUTED —
  see Amendment 1.** That triple is what a *two-byte* stride yields; the fields are four
  bytes apart and the labels are right.
* **`camera_current_w` (`0x801B8B04`) is not the zoom.** It reads **0** in a running battle;
  the whole `saved`/`start`/`current` block is an idle effect save/restore slot. The live zoom
  is `sprite_scale` (`0x800C7CA0`), mirrored at scratch `+0x80`.
* **The scratch struct is live in battle idle**, not cinematic-only — its XYZ is byte-identical
  to `work_position`. ~~It is therefore the leading sink, upstream of everything the per-vsync
  ticker fans out.~~ The first half stands; the inference does not. Byte-identity is what a
  copy in *either* direction looks like, and F14 settles the direction the other way. **See
  Amendment 1.**
* **Yaw is stored unwrapped.** 4608 = 4096 + 512 = 405°, not 45°. Consumers mask `& 0xfff`
  (`unit_anim_state_machine`, `0x80085C0C`). A write must not normalise blindly.
* **The projection is orthographic, measured** — F15's depth sweep is flat at +20.0 px/tile
  across 280 units where perspective would swing ~25%, and F20 reproduced a real unit's screen
  store to the pixel with `R·SV/4096 + TR` while `H·v/SZ` gave (210,143) against an actual
  (235,160). `H = 512` is set and is used for **sprite scale / OTZ only**.
  `battle_camera_system.txt` §1.2 still describes a 28° perspective frustum. It is the older,
  refuted source; do not reopen `h`.

#### The units already agree, and there is no scale factor to invent

`TILE_UNITS = 28` (`import_document.py:91`, ADR-0004 decision 13) — the addon imports geometry
at **FFT world scale**, so 1 Blender unit *is* 1 FFT world unit. `work_position` is s32 20.12,
so `raw / 4096` is world units. The axis map is pinned at `AXIS_NAME = ("x", "z", "-y")`
(`import_document.py:109`, ADR-0004 decision 14) and ratified by `blender_axis_baseline.json`:

    blender = (fft.x, fft.z, -fft.y)          fft = (blender.x, -blender.z, blender.y)

det = **+1**, a rotation. This is *not* the map `godot-learning` uses — `psx_position_to_godot`
is `(x, −y, z)`, det = **−1**, a reflection — and godot needed a different map for the camera
than for the units it films (F19). See the reuse note below.

#### The five decisions inside this one

**1. The pose is pushed faithfully — no clamping to the game's envelope.** Yaw between the
notches, pitch outside 26.5°–39.4°, zoom past 1.0×, pivot anywhere. Clamping would hand back
the same eight poses the artist cannot escape, which is the whole reason this exists.

The cost is named and not warned about in the UI: **unit sprites will be wrong.**
`unit_anim_state_machine` picks each sprite's SEQ slot and mirror flags from
`(work_rotation_y + unit_facing) >> 10` and `>> 8` — the octant is quantized off camera yaw, so
between the notches sprites pop to the nearest octant and stop agreeing with the terrain, and at
an unreachable pitch they are still upright billboards. Terrain is a matrix and is unaffected,
and terrain is what is being compared. Decision 4's rule: push what has a sink, **name** what is
skipped, never refuse.

**2. A *Match camera* button first; the continuous toggle is built on top of it.** Not a
compromise — sequencing. The button is the same arithmetic and the same write plan with none of
the cadence risk, and it is the instrument that answers whether the sink holds at all. A sink
that survives one frame is still *visible and photographable* through a button; through a 20 Hz
timer the same sink strobes, which is the hardest failure to read and the easiest to mistake for
broken arithmetic. The enable toggle then gates the timer only, so **ON by default** costs
nothing when no emulator is running.

⚠ **And, since 2026-08-29, a THIRD door: *Push to PCSX* aims the camera too** —
ADR-0186 Amendment 16 decision 75, asked for by name once Manual mode existed
(*"it's basically like the same as if you had automatic on, and did one push of
everything"*). The leg is unconditional, it does **not** read the continuous
toggle — that toggle gates the TIMER, and a press is the artist asking — and it
can never fail a delivered push: a viewport this arithmetic refuses is reported
as `camera: not aimed -- <why>` under a `FINISHED`. *Match camera* is unchanged;
it is no longer the only way in.

**3. The centres agree, and the engine's own constant is what makes them.** F20 decomposes the
GTE translation exactly: `TR = camera_tracked_target − R·work_position`, with
`camera_tracked_target` = `0x800A77B0` = `{256, 160, 640}`. The `160` at **`0x800A77B4`** *is*
the vertical datum — not a fitted constant, the engine's own named word — which is why
`work_position` lands at screen y=160 on a 240-line frame instead of 120. FFT frames the action
⅔ down, leaving headroom.

Uncorrected, a perfect sync still leaves the two views **40 world units — 1.43 tiles — apart
vertically**, which presents as *"they are looking at different parts of the map"*: the reported
symptom, surviving every other part of the sync being right.

The push therefore pokes **`0x800A77B4` = 120**. One extra word, in the same push. The
correction lives in the engine rather than in `live_link.py`, so it scales with zoom for free,
there is no hand-tuned 40 to keep right, and it is robust to the one thing F20 leaves open —
whether terrain takes the same datum as sprites. `0x80098A24` is documented as read by **both**
the map affine transform and `project_all_unit_sprites`, and TR was recovered from the GTE
**control** registers, which are global for the frame, so they almost certainly share it. If
they do not, the datum poke says so on the first framebuffer dump instead of after a hand-tuned
constant ships.

Two costs, named: the emulator's framing is then **not authentic** — it is not how FFT would
frame that shot, and everything rides 40 px higher — and `0x800A77B4` is maintained per-frame by
`smooth_track_camera_target` (`FUN_8008B6E4`), so it may not stick. That is the same unknown as
the main sink question and is answered by the same A/B, at no extra cost. The fallback if it
does not stick is to apply `R⁻¹·(0, −40, 0)` to the pushed position in `live_link.py`; because
TR is added **after** R, the correction is a pure screen-space vertical pan and is
yaw/pitch-independent.

The rule this serves, from the artist: ***"what I see in Blender should be what I see in
PCSX-Redux."*** It is therefore graded by a **picture**, not by bytes — the tradition decision 11
paid for, where a byte readback passed a dead animation.

**4. Zoom is a dial, and pixel aspect is not corrected at all.** The emulator's frame is a fixed
256×240; a Blender viewport is whatever shape the artist dragged it to, so the two can only agree
on one axis. Rather than pick one, the push derives a zoom from the Blender view distance and
multiplies it by a **user-adjustable factor in the panel** — so zooming in Blender still moves
the emulator, and the dial calibrates the relationship once. *"Just make the center axis align
and we can dial in a zoom in the UI."*

This deliberately removes the one contested number in the whole camera model from the design.
The horizontal store-to-pixel factor is **not settled** in the RE record: F15 measured
`screen_x ≈ view_x` at 1:1 while F20's decomposition (`work_position` → store 256 → on-screen
128) implies a factor of two, and F19's entire finding was godot's horizontal being compressed
0.82×. Under this decision nothing in the addon depends on which is right. Pixel aspect is
likewise not corrected anywhere: *"if we want a PAR-less comparison we can watch the VRAM viewer
in pcsx redux."*

**5. Roll is forced to zero.** This is a deliberate exception to decision 1 above, and the
difference is the point: a pitch or yaw outside the game's envelope is **unreachable but
well-understood**, whereas roll is **reachable in Blender but unmeasured**. FFT has a roll axis
and has never used it — fixed 0, no control, roll = 0 in all 65 of F4's samples *and* in the
battle savestate above — so `Rz`'s placement in the composition is *assumed*, never confirmed.
Blender's default turntable orbit cannot roll either; it takes trackball mode or a view-align to
get there. Clamping costs the artist a rotation they would have to go out of their way to reach;
not clamping makes them the first person ever to drive an unverified path, and a wrong picture
would read as broken arithmetic. **What would unblock it:** one live capture with a non-zero
roll, fitted the way F4 fitted the other two.

#### The Blender side

The panel section is *Camera*, in `MAP_PT_live_push`'s existing `Map` sidebar
(`bl_category = "Map"`, `live_link_ui.py:956`) — the sidebar the Map workspace already opens.
It carries the enable toggle (**ON by default**), an **orthographic ↔ perspective** toggle for
the viewport, the zoom dial, and the *Match camera* button. No prose: the ortho toggle **is** its
own indicator, which is what the panel's own rule demands (*"you are putting console stuff in the
ui area"*).

The ortho toggle is a prerequisite, not a convenience — FFT is orthographic, so in a perspective
viewport no arithmetic can make the pictures match and the mismatch is invisible in the UI. It is
still **not forced**: the addon does not reach in and change a view the artist set. Looking
through a scene camera (`view_perspective == 'CAMERA'`) is different — `view_location` and
`view_rotation` then describe the last *free* view, not what is on screen — so the sync
**refuses and says so** rather than pushing a stale pose.

`depsgraph_update_post` does **not** fire on view navigation; orbiting changes no datablock. The
continuous leg uses `bpy.app.timers.register` (`workspace.py:451/486/532`) or a
`SpaceView3D.draw_handler_add` (the viewport badge, `import_document.py:2178`) — the two shapes
this addon already has. Not a third.

#### Where the arithmetic lives, and what was taken from `godot-learning`

The Blender-pose → FFT-raws arithmetic goes in **`addons/exmateria_map/live_link.py`**: imports
`bpy` never, stdlib only (ADR-0005 decision 2), one copy (decision 6). The toggle, the section
and the timer are `live_link_ui.py`'s, the only parts allowed to need `bpy`.

`godot-learning` is **reference for understanding the camera, not a dependency** — the artist's
own framing. Read and taken: the rotation order and units, which F4 established there and which
are re-confirmed above. Read and **not** taken, with reasons, so this is answered rather than
silently dropped:

| file | why not |
|---|---|
| `CameraCalib.gd` | `GODOT_CAMERA_SIZE = 12.6` is in godot units, where 1 unit = 1 tile (`PsxUnits.tile_to_game` divides by 28); Blender is 28 units per tile, so it is off by 28× used directly. Its own file calls it *"dialled in"*. Decision 4 above means no such constant is needed. |
| `PSXCameraConvert.psx_position_to_godot` | godot's axis map is a **mirror** (det −1) where the addon's is a **rotation** (det +1), and godot needed a different map for the camera than for the units it films (F19). |
| `PSXCameraConvert.psx_angles_to_godot_rotation` | discards roll and returns a Godot Euler triple whose signs were tuned against that mirrored space. The RE record says it outright: *"the empirical Godot rotation is NOT R."* |

#### What is not proven, and the way out if the poke does not stick

**Which link in the chain survives a write during a live battle is not established.** The chain
is scratch struct → `camera_per_vsync_ticker` (`FUN_801439C0`, per vsync) → `FUN_8008BA60` /
`FUN_8008B834` / `FUN_8008B30C` → `work_rotation` / `work_position` → `build_camera_view_matrix`
→ GTE. `work_position` is documented as *"poking it sticks and re-projects the scene"* **[LIVE]**
(F14), but that was a settled savestate, not a battle with a controller running. The scratch
struct is the leading candidate on the evidence above. This is the first live measurement, and it
is an A/B: poke, read back one frame later, poke the other, compare — with a **framebuffer dump**
as the witness, because only a render settles a rendering question.

There is an automatable half as well: poke a known pose, then read `0x80098A24` and require the
engine's own derived view matrix to equal the one that was intended. That is a *behavioural*
readback in decision 11's sense — the engine rebuilt it from the write — rather than a byte
readback of the write itself.

**If it does not stick, the way out is to pause the emulator while sync is on.** Nothing runs, so
nothing overwrites: poke, step one frame to redraw, and the picture is exactly the pushed pose,
regardless of which link is the real sink. The cost is a frozen game, which for parking on a
battle screen and comparing geometry is not a cost. `RamClient.exec` already runs arbitrary Lua,
so this is a few lines and not a subsystem. It is the **fallback**, not the design.

Also not handled, and named: what happens when the game legitimately wants the camera — a spell
effect or a cutscene writing the same scratch fields. Sync will fight it. The artist's loop is a
static battle screen, so this is left rather than solved.

> **Amendment 1, 2026-08-28 — the scratch angles were never mislabelled, and `work_position`
> is the sink that sticks.** Both come out of re-reading the same savestate and the same
> primary source this decision already cites, during the build; neither needed an emulator.
>
> **The stride, not the labels.** This decision reads `[302, 0, 4608]` at scratch `+0x74` and
> concludes the yaw sits in the slot labelled roll. That reading is at a **two-byte** stride.
> The fields are four bytes apart, and `renames_high.tsv` says so itself in the aliases it
> gives for the same three fields — `camera_scratch_pitch` `0x80057790`, `camera_scratch_yaw`
> `0x80057794`, `camera_scratch_roll` `0x80057798`, twelve bytes for three angles. At four
> bytes the struct reads `[302, 4608, 0]` and agrees with `work_rotation` word for word.
>
> The 0.948 is real and is kept as an assertion, because that number is what makes this
> legible: get the stride wrong and it is the **camera model** that looks broken, not the
> read. `tests/test_live_link.py` asserts the four-byte pose, the two-byte triple, and the
> 0.948 the two-byte triple composes to, so neither claim can be made again without the other
> beside it. The struct's base is confirmed by **content** in the same test — its position and
> zoom are byte-identical to `work_position` and `sprite_scale`, three words agreeing at once.
>
> **The direction of the copy, and therefore the ranking.** This decision makes the scratch
> struct the leading candidate for a live write on the strength of that byte-identity. But a
> copy in *either* direction produces byte-identity, so the savestate cannot rank them — and
> **F14 ranks them**, statically at `0x80143AC8/0x80143B24` and validated live: the per-vsync
> ticker copies `work_position` → scratch → GTE, *"so a `work_position` poke **sticks and
> re-projects**; the handoff had it backwards"*. F14's own rig note is blunter still:
> ***"Camera-scratch pokes do NOT stick"*** — an interpolator re-drives `+0x68` every frame
> back to the keyframe target.
>
> That was measured on a **cinematic**, where an interpolator is running, and the artist's
> loop is a battle idle where one may not be. So this does not close the A/B, it reverses its
> favourite: `plan_camera` plans **both** sinks and `CAMERA_SINK_DEFAULT` is `work_position`.
> F14 is the finding this decision's own reading list omits — it cites F4, F6, F15, F19 and
> F20 — and it is the one that answers the question the decision left open.

> **Amendment 2, 2026-08-28 — the readback ships in the button, as a report.** The
> "automatable half" above is not a follow-up; it is what *Match camera* does on every press.
> It reads `CAMERA_VIEW_MATRIX` one fetch after the write and requires it to be the matrix the
> pushed pose implies — the engine recomposed it, so agreement means the write reached
> something downstream really consumes.
>
> It **reports** rather than refuses, which the decision above does not say and which matters:
> the way out for a sink that does not stick is to pause the emulator, and a paused emulator
> runs no frame in which to rebuild anything. A refusal here would break the fallback.
>
> `tests/blender_live_push.py`'s fake emulator grew the engine's per-frame rebuild for this,
> and the arm that carries the weight is the one where it does **not** rebuild: that models a
> write landing somewhere the engine never composes from, and it proves the button can report
> a pose that did not take rather than going green over an unchanged picture. The agreeing arm
> is written **positively** — a `push_camera` that skipped the readback would say nothing
> either way and would pass an arm phrased as "no disagreement was reported".

> **Amendment 3, 2026-08-28 — the sink question is closed by the picture, and the timer is
> built on it.** Decision 12 left one live unknown: whether a poke survives a running battle,
> to be settled by an A/B with a framebuffer dump as the witness. It was settled instead by the
> acceptance this feature is graded on — the artist pressed *Match camera* on a live battle and
> reported ***"this works incredible."*** That is the picture, and it is the bar §6 of the
> handoff sets. `CAMERA_SINK_WORK` sticks; the scratch-struct arm of `plan_camera` stays,
> unused and asserted, because Amendment 1 reversed the ranking on evidence and not on a run.
>
> The timer follows, and its risk is **cadence**, not arithmetic — the arithmetic is the
> button's and is proven. So the decisions live in `live_link.py` as `CameraSyncTicker`, where
> a plain `pytest` grades them with no `bpy` and no socket, and three of them are the defects
> it would otherwise ship: only a **changed** pose is written, so a still viewport costs
> nothing and decision 2's *"ON by default costs nothing"* is true rather than aspirational; a
> **failed** write is not a push, so an emulator started after Blender gets the view the moment
> it answers; and only state **changes** are reported, because at 20 Hz an unguarded line is
> 1,200 identical entries a minute in the console and the Log. A failure backs the rate off to
> 2 s. An *idle* reason — a viewport looking through a scene camera — does not, because it is
> not the emulator's fault and leaving camera view has to be live on the next frame.
>
> **The tick does not read back.** The readback is the button's instrument; on a timer it is a
> second round trip and a Log line per tick to re-answer what one press already answered. Its
> absence is asserted, not assumed.
>
> Two things measured while building it, both of which change what the harnesses can claim:
>
> * **`--background` Blender holds a window with a `VIEW_3D` area** — `['PROPERTIES',
>   'OUTLINER', 'DOPESHEET_EDITOR', 'VIEW_3D']` under `--factory-startup`. So a registered
>   timer finds a real `region_3d` headless and would POST a pose to whatever is listening on
>   port 8080: **every harness run would drive the artist's live emulator.** The timer returns
>   `None` under `bpy.app.background`, which unregisters it, and the harness asserts both
>   halves — that the viewport really is there to be fooled by, and that the tick declines it.
> * **The button's readback races the frame.** It reads `CAMERA_VIEW_MATRIX` immediately after
>   the write; the engine rebuilds it once per ~16.7 ms frame and a localhost round trip is a
>   fraction of that, so the read can precede the rebuild and report a disagreement about a
>   write that was fine. The fake emulator lands the vsync between the two and therefore cannot
>   represent this. Pressing twice distinguishes them. Not fixed, named — and it is a third
>   reason the tick does not read back.

### Decision 13 — isolating the map is a set of gates over engine state, not a document push

Reported by the artist, straight after the camera sync made aiming possible: *"Hide units and
dialogue boxes so when we work on the map everything is isolated... The general goal is to
have the map be the only thing we can see."* Settled by grilling on 2026-08-28, before any of
it was built, the way 12 was. **Built the same day** — see §0's row for what that
covers and the one thing it does not (the artist has not looked yet).

Three things the build settled that this decision left open, recorded here rather than
amended into the text above, because none of them changes a choice:

* **The id at `node+0x4` is a BYTE.** `unit_sprite_object_find` reads it with `lbu`
  (`0x8007A6FC`). Read as a word the Gariland list's first id is `0x0061000A`, and the
  report would name a unit by a number nothing in the engine uses. The walk's other two
  offsets were right as written.
* **A null head holds back the CODE pokes too.** *Found nothing, wrote nothing* reads
  naturally as a rule about the walk's own writes, but the HUD and cursor gates are fixed
  addresses in `BATTLE.BIN` and poking an overlay that is not loaded is the same mistake
  wearing a constant. Isolate outside a battle now writes nothing at all.
* **A second press must MERGE its saved values, not replace them.** The second walk reads
  back what the first press wrote, so a session memory that replaced itself would save
  `show = 0` for the whole roster and Restore would leave the battle empty. Merging on
  node address keeps the first press's answer and still admits a unit that spawned since —
  which is what makes re-pressability answer the mid-battle spawn without a ticker.

Every decision before this one pushes a **document field** at a **live sink**. This one pushes
nothing. It writes engine state that has no document behind it, for the sole purpose of taking
something off the screen, and it restores from a value saved before the write. `CONTEXT.md`
gains **isolation write** for that, because calling it a live sink would break the definition
decision 4's whole reporting rule leans on.

#### The set is four things, and the ask names two of them

In a live battle the non-terrain pixels are: unit sprites, their **ground shadows**, the
bottom-left **vitals HUD**, and the **tile cursor** (the on-grid knife). A feature that hides
units and leaves five shadows and an HP bar has not delivered the ask, so the enumeration comes
first and the set is the feature's scope.

**Boxed dialogue is skipped, and named here rather than silently omitted** (decision 4's rule). *(Superseded by Amendment 2: the gate was found and boxed dialogue is hidden. What follows is the reasoning as it stood, including the three functions that turned out to be the wrong half.)*
The artist's loop is a *battle* with a map loaded; boxed dialogue is a cinematic thing that the
map-authoring savestate never shows. It is also the only one of the five with **no located
gate**: `event_display_message_handler` (`0x801308C0`), `event_dialogue_tick` (`0x8012F6D4`)
and `event_text_glyph_reader` (`0x8014CE80`) decode the *text pipeline*, and the label set holds
no box-drawing primitive with a hide switch. The lead, if it is ever wanted, is the per-frame
rendering fiber `event_display_message_handler` registers through `PTR_DAT_80165F98` — a
co-routine that can be *not* registered is a better gate than suppressing glyphs. It is the one
leg of the ask that would turn a session into a research errand, and it is the one leg left out.

#### Units and shadows are ONE lever, and it is the engine's own hide

`unit_sprite_render_dispatch` (`0x80086640`) does this seven instructions in:

    80086768  lhu  v0, 0x1d8(s3)
    80086770  beq  v0, zero, LAB_80086B10      ; the epilogue

`+0x1d8 == 0` is a **whole-dispatch early-out**, and it sits *before* the `+0x298` shadow
test at `0x80086ACC` and the `jal unit_shadow_render` at `0x80086AF0`. So the shadow follows
from the same branch: there is no second gate to build, and `unit_shadow_disable`
(`0x8008C2A4`) is named here only to record that it is **not needed**.

The write is the engine's own. `unit_sprite_object_hide` (`0x8008D18C`, the `{46} Erase Unit`
backend) does `sh zero,0xa(v1)` **and** `sh zero,0x1d8(v1)`; `unit_sprite_object_show`
(`0x8008D138`, `{44} Draw Unit`) writes `1` to both. Hiding a unit is a thing this engine does
to itself every cinematic — `SCENARIO6_UNIT_REVEAL_VISIBILITY.md` is the living document on
`unit[+0xa]`, grounded live.

**The lever is per-unit and NOT the list head, and the handoff's ranking of the two was
wrong.** `unit_sprite_list_head` (`0x80098A54`) carries 21 XREFs — three writes, **eighteen
reads** — and one reader is `unit_sprite_object_find` (`0x8007A6E4`), the id -> node getter that
the shadow toggles and the `{47}` ghost gate call. `FUN_8007A724`, which also walks it, has
**21 callers** across `0x80068xxx`-`0x80073xxx`: gameplay, not rendering. So *"the unit's own
state is untouched, so turn order and AI cannot notice"* is not established for a null head; it
is established for the per-unit flags, which is what the dispatch itself reads.

**The direction question is answered statically and cost no emulator time.** The three writers
of the head are all list surgery, one of them (`FUN_80088018` @ `0x8008801C`, single caller
`0x8008ED8C`) a battle-teardown clear. `+0xa`/`+0x1d8` are written at unit **spawn**
(`0x80087BB8`/`0x80087BC0`, off a held-flag) and by the event opcodes — **not per frame**. A
poke sticks. This is the class of mistake the camera sync was burned by, and here the
disassembly settles it without a watchpoint.

| address | what | why it is here |
|---|---|---|
| `0x80086770` | the `+0x1d8` early-out | the shadow follows for free |
| `0x8008D18C` | `unit_sprite_object_hide` | the two fields, and that they move together |
| `0x8008D138` | `unit_sprite_object_show` | writes `1` to both — the engine's restore, not ours |
| `0x8007A6E4` | `unit_sprite_object_find` | one of eighteen readers of the head |

#### The HUD and the cursor have no flag, so they take a code poke

This is the first write this addon makes to the **instruction stream**. Every sink before it is
data — descriptor block, packet buffers, palettes, camera pose. It is named as its own gate kind
rather than smuggled in.

There is no data switch to find. The nearest thing is `g_cursor_anim_pause` (`0x800960F0`), and
its own label says it skips the phase/accumulator advance: it **freezes the bob, it does not
hide the cursor**. So the gate is `jr ra; nop` (`0x03E00008`, `0x00000000`) over a renderer's
first two instructions, restored by writing back the eight saved bytes. The technique is not new
to this package — `workspace/probe496.py` already pokes `0x03e00008` and nops a guard branch.

| target | state |
|---|---|
| `build_unit_vitals_window` `0x801363DC` | confirmed a real function head (`addiu sp,sp,-0x248`), calls `draw_number_small_font` 3x. **No direct `jal` caller** — it is dispatched through a pointer, which is what makes the entry poke the only practical gate rather than merely the easiest |
| `FUN_8008924C` (calls `tile_cursor_bob_render` @ `0x80089294`) | the **first** cursor target, and the uncertain one |
| `tile_cursor_bob_render` `0x8007E304` | the second candidate, and probably wrong: its own label says it subtracts the table offset from cursor sprite Y *before* `rotate_vector`, so nulling it likely leaves the knife drawn and unbobbed |

**The uncertainty is shipped, not hidden.** The cursor target is a named constant, one line to
change, and the acceptance below resolves it in one press. Naming one address and asserting it
was correct is what this document does not do.

#### It is an ACT, and the ticker is the wrong precedent

The camera sync earns a timer because its **source** changes continuously: every viewport orbit
is a new pose, and Amendment 3's economics — write only a *changed* pose — are what make it
free. Isolate has no moving source. The artist flips it twice a session, so a ticker would spend
a round trip per tick to learn there is nothing to do, and to learn even that it would have to
**read back**, which Amendment 3 refused for the camera tick on three stated grounds. Nothing
re-derives these fields per frame, so there is nothing to fight.

So: two buttons, no state in the UI.

* **Isolate map** pokes, and is **idempotent and re-pressable**. That is the whole answer to the
  three ways the emulator drifts out from under Blender — a restarted emulator, a *Replace the
  loaded map*, a unit spawning mid-battle. One press, not a per-tick round trip.
* **Restore** writes back **saved values, not constants**. `unit_sprite_object_show` writes `1`
  to both fields, and copying that would be a defect: a unit the game had *legitimately* hidden —
  not yet revealed, erased by a `{46}`, off-roster — would be wrongly revealed by an un-isolate.
  The saved value is the only correct restore.

**The cost, named rather than discovered:** Blender holds the saved values, so if Blender dies
while isolated the restore is lost and the artist reloads the battle. Decision 3 already puts
this loop on the poke-don't-patch side of that line, so it is in character — and persisting
emulator state into a `.blend` would be worse, because it goes stale the moment the emulator
restarts.

#### The walk hides what it can reach, and says how far it got

One `hold()` fetch answers the whole walk (Amendment 7 of decision 32's mechanism): read the
head, follow `node+0x0`, take the id at `node+0x4` and the two flags, then one `write` batch.
**One round trip, not one per node.**

Four ways the walk can be unsure: a null head (indistinguishable from *not in a battle*), a
circular chain or an out-of-range/misaligned next-pointer, a chain longer than a roster can be
(`entd_to_roster_loader_16` loads 16 ENTD slots, plus up to three `{47}` ghosts), and the artist
pressing Isolate outside a battle.

**It hides what it reached and carries on** — the artist's call, against the recommendation of a
refusal. Two things make that safe rather than silent, and they are the decision:

* **It only ever writes to a node it validated.** The walk stops following a bad link; it does
  not write to an address derived from garbage. The not-in-a-battle case therefore degrades to
  *found nothing, wrote nothing*.
* **The report is units found and units hidden, not bytes changed.** This is what a refusal was
  protecting and it is recoverable without one. *"hid 8 of 8"* and *"hid 3, then the chain went
  bad"* are different sentences, and a null head says **found no units** instead of colliding
  with the `0 changed` that already means *already isolated*. A count that means two opposite
  things is the defect a refusal would have avoided; a second number avoids it too.

Cycle detection is by **visited node address**, which is exact, with the 32-node cap as a
backstop rather than as the mechanism.

#### Where it sits, and how it is graded

`MAP_PT_live_isolate`, `VIEW_3D`, **`bl_order` 2** — with Push (0) and Camera (1), on the reason
`_HOMES` already carries for Camera: *both are the live link, and the artist presses them in one
breath*. Aim the camera, hide the units, look at the map. Preview / PaintView / Terrain /
LightingBake shift to 3/4/5/6; the permutation arm fails loudly if the renumber is wrong.

Not inside the Camera panel, which the handoff suggested: that panel's docstring defends **four
controls and no prose** as this sidebar's rule, and "Camera" stops describing it the moment it
hides units. Two buttons and not a checkbox, because re-ticking an already-ticked box is a no-op
and re-pressability is the mechanism Q4 chose.

* **`pytest` against `reference-assets/thief_whats_this.sstate`** for everything structural —
  the walk finds the capture's units, the ids match, the flags read as expected, and a **seeded**
  circular link and a **seeded** overlong chain are caught. No emulator. It is how decision 12
  refuted two of its own premises before running anything.
* **`tests/blender_live_push.py`** for the Blender half — headless, fake emulator, every check
  ships the defect it catches, `EXPECTED_CHECKS` as a floor. 209 at the time of writing.
* **Acceptance is the artist's eye on their own battle**, the way Amendment 3 closed the
  camera's last unknown on *"this works incredible"*. Bytes-changed grades the mechanism; only
  looking grades the feature — and the shadow and the cursor's poke target are both places where
  the mechanism can report success while the screen disagrees.

> **Amendment 1, 2026-08-28 — the camera leash is a third code gate, and it rides the same
> press.** The artist's report: *"if we go into battle state the camera is linked to a position,
> like the cursor — but during dialogue it's not. This is why we can do smooth camera movement
> and panning during dialogue, but not during battle — it is constantly fighting to get back in
> position."* Measured, not inferred: pushed to `(100, 0, 100)`, `camera_work_position` drifts
> **191 units** back to the battle's own target over about a second and then holds.
>
> The leash is **`FUN_8006FE58`** (`0x8006FE58`), a per-frame step-toward-target integrator that
> adds the signed velocities `DAT_800A1C48` / `DAT_800A1C4C` into `camera_work_position` against
> a clamp of `DAT_800961B4 * 28 + 14`. Cut with `jr ra; nop` the camera holds at
> `(120.000, -5.000, 80.000)`; restored it drifts to `(233.914, -17.941, 44.029)`.
>
> **Static analysis named the wrong function**, and this is the reason the address is recorded
> here rather than re-derived. The first answer was `FUN_8008B440`, the countdown-gated glide —
> cutting it changed the trajectory not at all, and its counter `DAT_8009616A` reads 0 for the
> whole pull-back. Worse, the writer set was **incomplete**: four functions were known and
> grepping every store into `camera_work_position` found **six**, with the answer in one of the
> two that were missing. The other five are innocent, each cut alone and measured:
> `FUN_8008B440`, `FUN_800700BC`, `FUN_8006EF00`, `FUN_8008B30C`, `FUN_8008B2C4`.
>
> **It goes in `CODE_GATES`, beside the HUD and the cursor**, on the artist's own direction —
> *"it would go in the same place as where we hide the units"*. One act, one way back: the leash
> is saved before the write and restored with everything else, and the same null-head rule holds
> (not in a battle → write nothing, gates included). It is **not** wired to the camera push,
> which would have made a second piece of state to keep straight and would have left the leash
> cut with no press that puts it back.
>
> **It is a LEAF**, and that broke a guard rather than the feature. `test_live_link.py` asserted
> every gate's entry word was an `addiu sp,sp,-N` prologue; `FUN_8006FE58` has no frame at all —
> 141 instructions to its `jr ra` at `0x80070088`, no `sp` adjust, no `ra` save, no `jal`. The
> guard now classifies **by gate name**: the two renderers must still be prologue-shaped, and the
> leash must walk to a `jr ra` with no frame built. Name-based on purpose, so a prologue function
> cannot silently re-file itself as a leaf to escape the stricter check. A leaf is in fact the
> *safer* poke of the two — there is no half-built frame to strand.
>
> Boxed dialogue is still not gated here. It has since been hidden a different way — by clearing
> its palette, CLUT `0x7C3C`, in a savestate (`research/hide_dialogue_box.py`) — which is an
> offline patch and not a live poke, so decision 13's *no located gate* stands as written.

> **Amendment 2, 2026-08-28 — boxed dialogue HAS a gate, and it is one draw with the portrait.**
> Decision 13 shipped boxed dialogue as *the one leg of the ask with no located gate*, and every
> press said so. That is now false. **`event_portrait_render_ft4`** (`0x8012E65C`) is the
> per-frame builder of the box's `POLY_FT4`s, and `jr ra; nop` over its entry takes the frame,
> the text **and the speaker portrait** off the screen together — A/B/A against
> `scenario6_delita_tough_dialogue_pc334`, with the box back byte-for-byte on restore.
>
> The three functions decision 13 named really were the wrong half: they are the *text pipeline*,
> not the draw. So is `dialog_box_compositor` (`0x8014C18C`) — it composites the box **once** at
> open, so cutting it mid-dialogue leaves the picture untouched, which is measured here rather
> than reasoned about.
>
> **It is scoped to boxed dialogue, and that is why it is its own gate.** Cut against a battle
> with the action menu, the unit panel and a damage number on screen, the picture does not move.
> Cut across a running cutscene, three CROSS presses still advance the scene — the dialogue task
> ticks, it simply draws nothing.
>
> **How the portrait was found, and why the palette was the wrong lever.** The box was first
> hidden by zeroing CLUT `0x7C3C` in a savestate (`research/hide_dialogue_box.py`), and the
> **portrait survived it** — so the portrait does not read the box palette. A whole-VRAM bisect
> against the box-hidden picture put the portrait's pixels in a single 16×24-halfword block at
> VRAM `(848, 424)`, inside the unit-SPR portrait column at x832 that `boxed_dialog_decode.md`
> already documents. But no palette anywhere in VRAM changed it: painting every other 32-row band
> green moved nothing, and clearing the block to index 0 left an opaque plate rather than a hole.
> The lever was never in VRAM — it is the draw, and the existing label set already named it.
> A picture-first hunt found the *pixels*; the labels found the *gate*.
>
> `research/hide_dialogue_box.py` is kept, not deleted: it is the offline answer for a savestate
> with no emulator attached, and it is the measurement that proved the portrait is a separate
> consumer.


### Decision 14 — the push is split at the `bpy` line, and the transport runs off the main thread

Reported from use, on the settle loop: *"when I am painting, I will let go and stop, and then
in a bit it will randomly freeze for a bit before starting again — it's awkward and slow."*

The freeze was the push. `settle_op.push_after_compile` called `bpy.ops.map.live_push()` and
waited out the whole round trip on Blender's own thread, so the artist's UI was locked for the
length of an HTTP conversation with another process. ADR-0186 decision 30 had already made this
split for the *compile* — "read on the main thread, compile off it" — and the push had simply
never been given the same treatment.

**The measurement, before the design.** `MAP_OT_live_push.execute` was instrumented against the
harness's fake emulator (which is a real `RamClient` wired to a byte buffer, so the clustering,
the bounds checks and the changed-byte count are the shipped ones) and the round trips were
counted; the per-trip latency is the real emulator's, measured on localhost.

| leg | cost | main thread? |
|---|---|---|
| `assemble(ob)`, MAP022 a0, 454 polygons | **375 ms** | yes, and it cannot leave — it *is* the Blender read |
| whole-RAM GETs (`/api/v1/cpu/ram/raw`, 2 MB) | **16 × 31 ms = 498 ms** | no |
| whole-VRAM GETs (`/api/v1/gpu/vram/raw`, 1 MB) | **5 × 34 ms = 171 ms** | no |
| the push's own planning and diffing | tens of ms | no |

So the half that could move was the bigger half, and it was also the half that is not *work* —
it is waiting on another process.

**The sixteen GETs are not a redundancy to remove.** `RamClient.hold()` answers every read from
one image, and every `apply` drops it on purpose: the reads *after* a write — `verify`, the
packet witnesses, the picture's readback — exist precisely to see what landed, and a hold that
survived a write would turn each of them into a tautology. The traffic is the price of the
checks, and the checks are the reason this rig is trustworthy. Moving it off the main thread
costs nothing and keeps all of them.

**The cut is at the `bpy` line, and it is enforced.** Three functions in `live_link_ui.py`:

- `push_gather(context, ob, say)` — MAIN THREAD. Every `bpy` read the push makes: the
  preferences, `ensure_compiled`, `assemble`, the marker's imported polygons, its preview state,
  its base map directory. Returns the keyword arguments the transport takes, all plain data —
  `doc` and `base` are JSON-shaped, `rep.sheets` is `{name: bytes}` because `export_sheets`
  hands back the disc's own 131,072-byte layout rather than a Blender image, and `anim_dir` is a
  `Path`.
- `push_transport(say, **kw)` — **no `bpy` at all.** Everything from the descriptor gate to the
  animation install, inside one `hold()`.
- `push_report(ob, lines)` — MAIN THREAD. The marker property, the Log's Text datablock and the
  terminal print. Split out of the old nested `finish` because a background push lands its
  report long after the operator that started it returned.

`MAP_OT_live_push` runs all three in a row and is otherwise unchanged: **the button still
blocks**, because the artist who pressed it is waiting for the answer and an operator has to
return a status. It is the *settle* that goes to a worker.

**The `bpy`-free contract is graded on the source, not on a run.** Most of `push_transport` is
refusal branches a runtime arm would never take, and the failure mode is not a slow push — it is
a crash in another thread with no traceback the artist will ever see. So
`tests/blender_live_push.py` parses the shipped module and asserts that neither
`push_transport` nor `_transport` names `bpy` or `self`, and **seeds one `bpy` back into it** to
prove the arm can go red rather than merely passing against a function that was renamed away.

**A push in flight COALESCES the next one; it does not queue it.** A queue would send the
emulator a sheet the artist has already painted over. `background_push_start` drops a second
push and sets `pending`; when the running one lands, `_judge` sends the current document. One
slot, for the same reason `settle_op` keeps one compile slot.

**A refusal is still immediate.** `lua.check()` and `assemble`'s refusals both happen inside
`push_gather`, on the calling thread — so *"there is no emulator"* is an answer the settle gets
now rather than a tick later, and decision 28's back-off (retry on a slow clock, report once per
spell, never latch) keeps working exactly as it did. `push_after_compile` gained a third return
value for the case it cannot answer yet, `{"RUNNING_MODAL"}`; nothing reads it as success.

**The report lands on the tick that already exists.** `settle_op._tick` runs at 4 Hz and now
calls `_drain_push()` before `_step()`. A push does not get a timer of its own: a quarter of a
second of latency on a line the artist reads in the terminal is free, and a second timer would
be a second thing to unregister.

**What is left on the main thread is `assemble`, and it is 375 ms.** That is a real remaining
hitch and it is named rather than rounded away. It cannot be threaded — it is the Blender read
itself — so making it cheaper is a different question (its profile is dominated by
`image_indices` and the 4bpp repack, both of which re-run in full on a settle where only the
sheet moved). Not attempted here.

> **Two of the sentences above are wrong, and decision 15 corrects them.** The 375 ms was
> measured at a 1x Painting and the shipped default is 4x, where the same call is **2,398 ms**;
> and "the transport runs off the main thread" is not the same claim as "the main thread is
> free", because of the GIL. Read decision 15 before quoting any number from this one.


### Decision 15 — a worker THREAD does not free Blender's UI, and the push was doing the compile's work twice

Reported from use, after decision 14 shipped: *"release left click to stop painting … things are
fine for a second and then the push kicks in and pcsx-redux starts doing work. It takes maybe 3
seconds for pcsx-redux to settle, and that whole time blender was unusable — basically."*

Decision 14 halved a freeze and then reported the remainder as 375 ms. The artist was still
losing about **four seconds**, which is the gap between what that number claimed and what a
session actually costs. Both halves of the gap are measured below, and neither is the transport.

**The instrument first: `tests/blender_settle_stall.py`.** It dumps MAP022 a0, imports it,
converts it at the shipped default scale, paints into the Painting and then drives
`settle_op._launch` — *the real settle path, never a copy of it* — while the main thread runs a
heartbeat loop counting how much Python it gets through. That count is the point: a worker
thread does not stall the main thread once, it taxes **every** Python entry, and Blender's UI is
made of Python entries.

**Finding one — the "background" compile costs the UI 62x.** Measured headful with
`bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP')`, which is Blender drawing its own windows:

| | Blender fps |
|---|---|
| idle | 586 |
| one CPU-bound Python thread, CPython's default 5 ms switch interval | **8.7** |

8.7 fps *is* "unusable — basically", and it is what decision 30's and decision 14's threads
bought on their own. One window redraw enters Python once per panel `draw()`, and every entry
waits out a GIL switch interval. `sys.setswitchinterval` is the lever, and the trade is cheap:

| switch interval | Blender fps | worker throughput |
|---|---|---|
| 5 ms (default) | 8.7 | 212 |
| 0.5 ms | 83.3 | 206 |
| **0.1 ms** | **218.4** | 183 |
| `sleep(0)` between chunks | 17.2 | 207 |

`sleep(0)` is in the table because it is the obvious fix and it is nearly worthless — yielding at
a chunk boundary does nothing about the 5 ms every *other* Python entry waits. 0.1 ms costs the
worker 14 % and hands the UI back 25x. `addons/exmateria_map/worker.py` owns that trade:
`worker.spawn(name, fn)` lowers the interval while one of *our* workers is alive and restores it
when the last one finishes, refcounted because the settle can have a compile and a push in
flight at once. Both callers moved onto it, and `tests/test_worker.py` fails on any bare
`threading.Thread` anywhere else in the addon — seeded, because a scan for an attribute name is
exactly the check that goes quietly blind.

**Finding two — `assemble` is 2,398 ms at the shipped default, and two thirds of it is work no
push wants.** The 375 ms in decision 14 was measured before Amendment 10 made **N = 4** the
conversion default. Profiled on MAP022 a0 at 4x:

| leg | cost | who wants it |
|---|---|---|
| `rgb_from_floats` over 12.6 M texels | ~1,200 ms | **the compile already did this**, on its worker, a second earlier |
| `png_indexed.write_rgb_png` (Sub filter + zlib 9) over the result | ~900 ms | the *file* export. The push discards `files` |
| `image_indices` + the 4bpp repack + the sheets' own PNGs | ~300 ms | the blob is wanted; the PNGs are not |

So:

* **`assemble(ob, sidecars=False)`.** The push consumes `doc` and `rep.sheets` and throws
  `files` away. Both sidecar names are `sha256` of the *raw* blob — the packed 4bpp for a sheet,
  the RGB for a painting — never of the PNG, so skipping the encode changes no name, no digest
  and no document. It is not a flag to sprinkle: a bundle write must leave it True, or it writes
  a document whose sidecars are missing.
* **`export_document` remembers the master a compile derived.** `compile_off_thread` already
  builds the full-resolution RGB (`stamp_compile` hashes it); it now also takes a
  `master_key` — a sha256 of the float buffer it came from — and `land_compile` deposits the
  pair. `image_rgb` serves from that deposit when the key matches. The key is **sha256 and not
  `zlib.crc32`**, which is cheaper (24.6 ms against 14.2 ms on a 4x buffer) and is what
  `settle_op.canvas_digest` uses: that one is a change *detector*, where a collision costs a
  skipped settle, and this one keys a value that goes on to name a sidecar file. The 10 ms buys
  the key the same strength as the identity it feeds. A hit still pays the `foreach_get` and the
  key — 53 ms — and skips the 1,200 ms walk. A reload, an undo, a stroke or another tool's write
  all move the key and take the walk.
* **`rgb_from_floats` itself is three C-level strided moves.** `buf[c::4]` is a strided copy
  (9.8 ms for 4.2 M floats) and `flat[c::3] = …` a strided store, so the only Python left per
  texel is the scale-and-round; the row flip is one slice move per row rather than per texel; and
  `round(x)` on a float already returns an int, so the `int()` around it was a second call per
  channel and nothing else. Byte-identical, **1,031 ms → 673 ms**.

**The result, MAP022 a0 at N = 4, same box, same harness:**

| | before | after |
|---|---|---|
| the `bpy` read (`read_for_compile`) | 30 ms main thread | 31 ms |
| the compile worker | 1,440 ms at **1 %** of main-thread Python throughput | 970 ms at **25 %** |
| `land_compile` | 72 ms main thread | 76 ms |
| **`push_gather`** | **2,398 ms main thread, hard block** | **228 ms** |
| the transport | off-thread, unchanged | off-thread, unchanged |

The artist's hard block goes from ~2.5 s to ~0.33 s, and the second and a half that remains is
spent with the UI at ~200 fps rather than 8.7.

**What is graded, and where.** The behaviour is in `tests/blender_convert.py` — ten arms on a
really-converted MAP022, each with its control: `sidecars=False` writes no file and hands back
an identical document and identical sheets; a detonator in place of `write_rgb_png` never fires
with the flag off **and must fire with it on**; `image_rgb` is served from the deposit, and a
single changed texel takes the walk instead; the push after a compile re-derives no master, and
**with the deposit dropped the same push must walk**, or that arm is grading a push that never
wanted a master. The mechanism is in `tests/test_worker.py` (7 arms, plain `pytest`, no `bpy`).
The numbers are in `tests/blender_settle_stall.py`, which grades two *structural* floors and no
wall-clock budget — this box is shared, and a millisecond threshold on a contended machine is a
flake that teaches nothing.

**Acceptance is the artist's eye on their own painting session**, exactly as decision 14 said
and exactly as it did not get. A green harness is not the verdict -- and this one **has** it:
painting a real session after this landed, the artist reported *"I think this is better."*
That is what closes decision 14's open acceptance as well as this one's.

It is acceptance of a *direction*, not of the numbers. The bar the artist stated when asked
what "better" would have to mean is **the UI thread on top** -- the emulator may lag, tighter
updates are wanted but never at the cost of Blender's responsiveness, and the one thing that
must not happen is being prevented from painting. Decision 16 is scoped by that sentence.

**Not attempted, and named rather than rounded away.** The compile's remaining 970 ms is still
pure Python on a worker, and the honest fix for that is a *subprocess* — `compile_off_thread`
touches no `bpy` and imports only the stdlib, so it could run in a child process fed pickled
plain data, and a child process has its own GIL. That is an architecture change with a 50 MB
pickle on each leg, and it should not be started before someone measures whether 25 % of the
main thread's Python for a second is something the artist can still feel.


### Decision 16 — the camera sync's cost is ROUND TRIPS, not bytes, and its transport runs on a worker

Reported from use, on the continuous camera sync: *"blender is laggy when panning and moving
the camera and auto sync is on for the camera (which is default - I think) can we do a similar
kind of async-thing for camera movement - or is that different?"*

**It is different, and the difference is the decision.** Decision 15 found that a worker thread
does *not* free Blender's UI — the compile was CPU-bound Python holding the GIL, a worker took
Blender from 586 fps to 8.7, and only numpy fixed it. The obvious reading of the artist's
question is *"do for the camera what you did for the compile"*, and the obvious reading is
wrong in both directions: the camera's problem is not the compile's, and the compile's
treatment is not the camera's. A blocking socket read **releases** the GIL. For this leg the
thread that failed decision 15 is exactly the right answer.

**The measurement first, against the running emulator — because the plan was wrong.** The
design read off the source said the cost was the whole-RAM GET, *because it is 2 MB*: decision
14's table prices one at 31 ms, and `sync_camera` throws its changed-byte count away, so
deleting the read looked like ~31 ms of ~35 removed for a few lines. That is not what is
happening. Timing a socket by hand rather than a `urlopen` by wall clock separates the two
halves, and they are not the halves the plan named:

| request | median time to FIRST byte |
|---|---|
| `GET /api/v1/cpu/ram/raw` — 2 MB | **31.9 ms** |
| `GET /api/v1/gpu/vram/raw` — 1 MB | **32.3 ms** |
| `GET /api/v1/nonexistent` — a 404 that does **no work at all** | **36.1 ms** |
| `POST /api/v1/nonexistent` — a 404 that **writes nothing** | **32.3 ms** |

The 2 MB body then streams in **0.5 ms**. So the ~31 ms decision 14 attributed to *"a whole-RAM
GET"* is not the RAM and not the megabytes — it is a fixed **service wait** every request pays,
whatever it asks for and whatever it carries. A 404 is as expensive as the whole of main RAM.

**What that makes a camera tick.** `plan_camera` is four runs; the vertical datum sits 42 bytes
past `work_rotation`, inside `COALESCE_GAP`, so `cluster_writes` merges those two and leaves
three. One GET plus three POSTs — **four round trips, ~128 ms**, on Blender's own thread, asked
for every 50 ms. The artist orbiting changes the pose on every single tick by construction, so
the worst case is the normal case.

**Two of the four candidates die on that table, and one of them is the cheap one.**

* **Dropping the before-image GET is worth zero.** It looked like the cheapest fix by a wide
  margin. But the GET is what *pays for the clustering*: without an image there is nothing to
  fill the 42-byte gap from, and stock has no partial GET (`offset`/`size` are on the POST
  only), so the plan goes out as four separate POSTs. `0 GET + 4 POSTs` is the same four
  requests as `1 GET + 3 POSTs`. Measured on the rig below: ratio **0.263** against **0.265**.
* **Keep-alive is worth nothing either.** A fresh TCP connect per request is real, but it is
  not the cost — the 404 above pays the full wait on a connection that did no work.

**The instrument, and the one thing that makes it honest.** `tests/blender_camera_stall.py`
is `blender_settle_stall.py`'s shape — a main-thread heartbeat counting Python iterations,
because Blender's main loop is made of Python entries — driving the tick function the timer
calls, inside real Blender, with a viewport that orbits every tick. Its stub is
**latency-matched** to the table above. That is not a detail: a plain `http.server` on loopback
answers in **1.1 ms** and reports a throughput ratio of **0.96**, i.e. *no bug at all*. The
fake emulator in `blender_live_push.py` monkeypatches the transport and is blind to this by
construction, which is why every camera arm that already existed was green throughout.

| | main-thread ratio | achieved sync rate | worst freeze | requests/tick |
|---|---|---|---|---|
| the transport on Blender's thread (**seeded**) | **0.250** | 5.4 Hz of 20 | **146 ms** | 4.0 |
| the transport on a worker | **0.990** | **20.0 Hz** | 6 ms | 1.3 |

The seeded row is kept as an arm (`--seed`), not as a memory: it runs the pre-amendment
blocking tick and the harness *fails if the floors stay green*, which is the only way a floor
is shown to be able to go red. 1.3 requests per tick is the coalescing working — 60 ticks
became 20 writes, each carrying the pose the viewport held at the moment it was sent.

**The decisions the move creates, and they are the risky part.** The transport moving is
plumbing; what a tick decides while a write is in flight is not. `CameraSyncTicker` gains the
flight slot, and it stays `bpy`-free and socket-free so plain `pytest` grades it, for the same
reason the rest of that class is there.

* **One slot, and a tick that finds it taken COALESCES its pose — it does not queue.** A queue
  would hand the emulator a pose the artist has already orbited past, and at 20 Hz against a
  128 ms round trip it would grow without bound: the longer the artist moved, the further
  behind the sync would fall. The drop is free because `wants` never remembered the pose, so
  the next tick offers wherever the viewport is by then. Same rule as
  `background_push_start` and `SettleClock._flight`.
* **The pose is remembered only when the WORKER reports it landed.** Handing a pose to a thread
  is not evidence it arrived. Remembering it at hand-off would reintroduce, one layer up, the
  exact defect *"a failed write is not a push"* exists to prevent.
* **`landed` only records; every line is `drain`'s, on the main thread.** A worker may touch
  neither `bpy` nor the Log. It appends the result *before* it frees the slot, so a tick that
  sees the slot free cannot also miss the outcome that freed it.
* **`reset` frees the slot too**, or toggling the sync off mid-write leaves it claimed and the
  sync is wedged forever, silently.

**What is deliberately not fixed.** The emulator still sees ~128 ms of latency, and the four
round trips are still four. Getting below them would mean coalescing writes 251 KB apart into
one request — a read-modify-write over a quarter of main RAM every tick, which is precisely the
collateral hazard `COALESCE_GAP`'s comment exists to bound, against an engine that is running.
That trade is refused, and it is refused *by the artist's own bar*, quoted in decision 15: **the
UI thread on top; the emulator may lag; the number one goal is to not prevent the artist from
painting.** Orbiting is the same claim as painting.

**Acceptance is the artist's, not the harness's** — the standing rule in this document, and the
way decision 15 actually closed (*"its better"*). Four green floors are a precondition for
asking, not the answer.

**A note for the next reader of decision 14's table.** Its per-trip latencies are right and its
attribution is not: *"whole-RAM GETs, 16 × 31 ms"* reads as a claim about 2 MB, and the same
sixteen requests would cost the same 498 ms if they fetched one byte each. Wherever the push's
traffic is being reasoned about, the quantity that matters is the **number of requests**.


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

### 5.3 The savestates, and the map whose sheet spans TWO bands

**[LIVE], 2026-08-27, offline.** A savestate is a full RAM snapshot, so the emulator is not
needed to ask it anything: gzip, protobuf, top-level field 3 (`Memory`), its sub-field 1 (8 MB
of main RAM) at absolute offset **39**, and `check_descriptors` reads straight out of it. That
route answered two questions the live link had left open.

**Which map is in each savestate**, identified by *content* rather than by counts — the disc
resource's positions planned at the descriptor's own start indices and compared byte for byte:

| savestate | descriptor counts | resource | positions differing |
|---|---|---|---|
| `SCUS94221.sstate1` | — | — | no map loaded (the block does not pass the gate) |
| `SCUS94221.sstate2` | 24 / 361 / 18 / 51 | `MAP022.9` | **0 of 10,644** |
| `SCUS94221.sstate3` | 132 / 599 / 6 / 46 | `MAP062.8` | **0 of 17,964** |

The counts are unique across the 186 geometry-carrying resources in the corpus, so the search
returns one candidate; the byte comparison is what turns that into knowledge. `sstate3` is
therefore the pair a swap wants — MAP062 loaded, a MAP022 document to push, a pure shrink in
every bucket.

**Where each map's sheet and CLUT block live** — `packet_witnesses` + `derive_addresses`, run
against the same two snapshots:

| map | witnesses | sheet | CLUT |
|---|---|---|---|
| MAP022 | 385 | (768, 0), all agreeing | (0, 480), all agreeing |
| MAP062 | 731 | **725** at (768, 0), **6** at (768, 256) | 725 at (0, 480), 6 at (768, 480) |

Two findings, and the second is the one nobody was looking for.

1. **The two maps put their sheet and their CLUT block at the same address.** That is the
   assumption decision 10's swap rests on — writing your sheet into the host map's rectangles
   — and on this pair it holds. One pair is not the corpus; it is the pair the repo can reach.

2. **MAP062's sheet spans two 256-pixel bands, and it is the DISC that says so.** The six
   dissenting polygons carry `texture_byte6_high_nibble = 1`, which is bit 4 of the TPAGE word
   — y in 256-pixel units. `plan_sheet` writes four page rectangles side by side in **one**
   band (`PAGE_WIDTH` apart at `at.sheet_y`), so those six sample from VRAM no push writes.

   The consequence is live today and has nothing to do with swapping: `derive_addresses`
   refuses on a single dissenter — *"disagreement is a refusal, not a vote"*, decision 5 — so
   **the whole sheet-and-palette leg is lost on MAP062 over six polygons of 731**. Whether a
   majority address with a named minority is the better trade is decision 5's to reopen; it is
   not decided here. What did change is that the refusal now reports the **tally** rather than
   stopping at the first dissenter, because *"polygon 0 says (768, 0) and polygon 55 says
   (768, 256)"* cannot tell a two-band map from a corrupt packet, and those want opposite
   responses. Six of 731 reads as a map; half of 731 reads as a rig.

   **The corpus survey is run** (#646): `texture_byte6_high_nibble` over all **169 textured
   resources** says **23 of them (13.6%)** carry more than one nibble, always a tiny minority
   of `1`, worst case **18 of 539** on `MAP039.9` — and **no resource is split anywhere near
   the middle**. So the premise the refusal rests on, *"the packets are not describing the
   layout this module believes in"*, is measurably false for this shape: the majority address
   is never in doubt anywhere in the corpus, and today's policy costs the whole leg on one map
   in seven. Reopening decision 5 to derive the majority and NAME the minority is the
   recommendation on #646; it is not taken here, because weakening a refusal is the artist's
   call and this session's own rule was that a check gets a MODE, not a lenient version.

   *(Taken since, in `92a587bcd`: `derive_addresses` writes to the majority address and
   `picture_plan` names the minority. `DISSENT_LIMIT` is 10%, which no shipped map comes
   near — the worst is 3.3%.)*

3. **Why the dissenters dissent — and it makes the majority fix COMPLETE, not a compromise.**
   The obvious reading of "write to the majority address and name the minority" is that the
   named polygons keep the old map's picture: 380 faces right, 5 faces stale. Measured over
   the whole corpus, that reading is wrong, and the trade is better than it was sold as.

   All **78** dissenting polygons, across all 23 resources, have **every UV corner at the same
   point** — they sample a single texel:

   | | polygons | all UV corners identical |
   |---|---|---|
   | dissenting (second band) | 78 | **78 — 100%** |
   | every other textured polygon | 73,810 | 3,441 — **4.66%** |

   A polygon that samples one texel has no texture mapping to get wrong, so **the texture page
   is a don't-care on every one of them** — the stray second-band bit is junk riding along on a
   UV that was already junk. On MAP062 the six land on texel index 0 → CLUT entry `0x0000` →
   fully transparent, in a patch of that band which is otherwise empty. They draw nothing.
   Every one of the 78 is `texture_page = 1` with `palette_id = 4` (or `0` on `MAP043` and
   `MAP059`), which is the signature of a single degenerate construct repeated across the
   corpus rather than 23 independent authoring accidents.

   **Consequence:** the majority fix is not 380-of-385 — it is complete, and the note
   `picture_plan` emits is a disclosure rather than a defect count. And #646's **option 3**,
   *"teach `plan_sheet` two bands"*, buys nothing: it would spend a second set of rectangles
   painting texels no polygon meaningfully samples. It should be closed off.

## 6. Getting to a battle

`PCSX.loadSaveState` on `reference-assets/thief_whats_this.sstate` lands **in** the Gariland
battle in one call, with all four buckets of MAP022 a0 in RAM. PCSX-Redux's own GUI
savestates are **gzipped** and `PCSX.loadSaveState` fails *silently* on one — `gunzip -c`
first. Full recipe in `tools/live_geometry.py`'s docstring.
