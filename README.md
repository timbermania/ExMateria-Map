# ExMateria Map

A Blender add-on for editing Final Fantasy Tactics (PSX) maps — open a map out
of the disc, repaint or reshape it, watch the change in the running game, and
write it back.

## What you need

| Thing | Where |
| --- | --- |
| An FFT disc image (`.bin` + `.cue`, US `SCUS-94221`) | yours to find |
| A PlayStation BIOS (e.g. `SCPH1001.BIN`) | yours to find — OpenBIOS will not run FFT |
| **CDmage** B5 1.02.1 (Windows; Wine elsewhere) | [ffhacktics.com/wiki/Tools](https://ffhacktics.com/wiki/Tools) |
| **Blender** 5.2 LTS | [blender.org/download/lts](https://www.blender.org/download/lts/) |
| **PCSX-Redux** | [grumpycoders/pcsx-redux](https://github.com/grumpycoders/pcsx-redux) |
| **The add-on**, `exmateria_map-<version>.zip` | [Releases](https://github.com/timbermania/ExMateria-Map/releases) — leave it zipped |


## Quick start

Each step has one check. If a check fails, stop there.

1. **Extract the disc.** Open the image in CDmage, browse to `MAP/`, extract it
   all to a folder. The add-on reads map files out of that folder, not out of
   your `.bin`.
   → *Check:* **1575 files.** A truncated extract looks just like a good one.

2. **Install the add-on.** **Edit ▸ Preferences ▸ Add-ons**, then the **▾** menu
   (top right) ▸ **Install from Disk…** — or an **Install…** button, on a
   Blender without the Extensions system. Pick the zip, tick **ExMateria Map**.
   → *Check:* the console prints `EXMATERIA-MAP: addon 0.1.0 loaded from …`.
   Console is **Window ▸ Toggle System Console** on Windows; elsewhere, the
   terminal you started Blender from.

3. **Give PCSX-Redux a BIOS.** Put it in a folder of its own. Start it,
   **Configuration ▸ Emulation** → set **BIOS file**, tick **Fast boot**, hard
   reset with `Shift`+`F8`.
   → *Check:* **File ▸ Open Disk Image**, pick your `.cue`, press `F5`. It boots.

4. **Point the add-on at PCSX-Redux.** **Edit ▸ Preferences ▸ Add-ons ▸
   ExMateria Map** → set **PCSX-Redux folder** to that folder; leave host and
   port at `localhost` / `8080`. Then press **Launch PCSX-Redux**, which starts
   it with the flags the link needs — or **Set up auto-load**, so your own
   double-click works.
   → *Check:* `curl -s http://localhost:8080/api/v1/lua/ping` → `pong`. A window
   on screen is *not* the check — the emulator runs fine with the link unloaded.

5. **Save a state with a map on screen.** Play until a map is being drawn — the
   Orbonne Monastery opening is one — and press `F1`. Cutscenes count; menus,
   the world map and the title screen do not. The link edits the map the game is
   drawing, so there has to be one. The save state should have character dialogue open.
   → *Check:* a state file lands in the emulator's folder.

6. **Import a map.** **File ▸ Import ▸ FFT Map (.GNS)**, out of the folder from
   step 1. Any map works; the one on screen looks best. Orbonne is `MAP056.GNS`,
   its chapel `MAP062.GNS`.
   → *Check:* Blender switches to a three-pane **Map** workspace.

7. **Push it.** In the 3D viewport press `N`, pick the **Map** tab, and press
   **Push to PCSX** — or **Replace the loaded map**, if you imported a different
   map than the one on screen.
   → *Check:* the map on screen changes on the next frame.

PCSX-Redux keys: `F5` run · `F6` pause · `F1` save state · `F2` load state ·
`Esc` menu.

## The Map tab

Panels hold buttons; what a run has to say goes to the console.

- **Push to PCSX-Redux** — push · replace the loaded map · host/port · launch
- **Preview** — which map state you are looking at
- **Paint** — the texture sheet (its twin lives in the Image Editor)
- **Terrain** — grid size, growth, drift
- **Lighting Bake** — lamps and the light rig

## When nothing pushes

| You see | It is | Do |
| --- | --- | --- |
| `connection refused` | no emulator on that port | press **Launch PCSX-Redux**; check the port |
| `404: URL Not found.` | emulator up, link not loaded | relaunch it from Blender |
| refused on the descriptor block | no map loaded | load a state with a map on screen |
| pushed, nothing changed | fields with nowhere to go | read the console — the push names them |
| it reverted on its own | the game reloaded the map | expected; export to keep it |

## Making it permanent

A push edits what the game is drawing right now; a map reload puts the disc's
bytes back. To keep an edit, **File ▸ Export ▸ FFT Map bundle (GNS +
resources)**, then import those files over their originals with CDmage.

That works while nothing grew past its allocation. The limit is **sectors, not
bytes**, and there is no spare room — `MAP` is packed end to end. Repainting is
always safe; adding polygons can spill a file, and then it cannot go back
without moving others first.

## Updating

Install the newer zip the same way — it overwrites — then **restart Blender**,
because Python caches imported modules for the life of the process.

## Under the hood

- [The library, the CLI legs and the round-trip
  instrument](docs/library-and-round-trip.md) — `dump`/`build`, what `build`
  guarantees, and the byte-exactness measurement over all 1,575 files.
- [The interchange schema](docs/interchange-schema-v1.md) — the document the
  add-on and the library both speak.
- [The live link](docs/live-link-v1.md) · [lighting
  bake](docs/lighting-bake-v1.md).

Blender 4.0 is the declared floor; 5.2 is what this is developed and tested
against.
