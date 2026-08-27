"""Push map GEOMETRY into a running battle — a direct RAM write, no savestate.

Geometry lives in **main RAM**, and `PCSX.getMemPtr()` is a writable pointer
into it, so a poke lands on the next frame.

This paragraph used to open "the texture leg has to round-trip a savestate,
because this fork cannot write VRAM". **That was false**, and it is corrected
here rather than only superseded elsewhere because the addon's own `CLAUDE.md`
records a previous false premise in this area that outlived the code by four
months. The fork writes VRAM perfectly well: `POST /api/v1/gpu/vram/raw` needs
the rectangle in the QUERY STRING, and a bare POST -- which is what was tried
-- is a 400 for that reason and no other. Measured [LIVE] 2026-08-26 by A/B/A
on a Gariland battle. The savestate rig and `tools/vram_swap_sheet.py` are
deleted; the sheet is pushed by the addon's own button now
(`addons/exmateria_map/live_vram.py`).

    python3 tools/live_geometry.py --map 22 --arrangement 0 [--document doc.json]

## The layout, measured

FFT does **not** keep the `0x40` section's own bytes in RAM. Measured over 20
quads spread through the bucket: every single vertex is present (20/20), and no
*two consecutive* vertices are adjacent (0/20). The section has been unpacked
into per-polygon render structures.

All **four** polygon buckets are unpacked the same way, into **four separate
contiguous arrays**, each in disc order, each **8 bytes per vertex**:

    i16 x, i16 y, i16 z, then 2 bytes this tool does not own

so a triangle is 24 bytes and a quad is 32. On MAP022 a0 (24 / 361 / 18 / 51
polygons) in one session they sat at:

    textured_triangle    0x8011A2D8   24 B    | file order tt, tq, ut, uq --
    textured_quad        0x8011C498   32 B    | and RAM order is the same,
    untextured_triangle  0x80122004   24 B    | but the four arrays are NOT
    untextured_quad      0x80122604   32 B    | adjacent: 8 KB, 12 KB, 1 KB
                                              | of unrelated data sit between.

Every polygon of all four buckets matched the disc byte-for-byte (0 mismatches
of 10,644 coordinate bytes), so the arrays are verbatim, in order, complete.

**The two trailing bytes are not padding — they are the polygon's metadata**,
folded into the fourth `short` of the first two vertices. Identified on MAP022
a0 across all four buckets, **454 of 454 polygons, no exceptions**:

    vertex 0's 4th short = the terrain BINDING word, verbatim (textured
                           buckets); 0x0000 on the untextured ones
    vertex 1's 4th short = the polygon's VISIBLE_ANGLES word from the 0xB0
                           chunk, with bit 0 SET on textured polygons and
                           clear on untextured ones
    vertex 2 (and 3)     = 0x0000

They are persistent, not per-frame scratch: two RAM dumps two seconds apart
differ in 2,517 bytes elsewhere and in **zero** bytes of any polygon array.

And they matter. Seeding a wrong vertex stride scribbles coordinate bytes
through those two slots; repairing every coordinate afterwards leaves the map
**visibly shattered** — quads culled away into holes, which is exactly what a
corrupt visible-angles word buys you. Zeroing the same slots from pristine (all
1,419 non-zero bytes of the quad bucket) is visually inert, and zeroing them
from the shattered state heals it: measured A/B/A, screenshots either way. So
zero happens to be a benign value and garbage is not, which is why `push()`
writes the six coordinate bytes of each vertex and leaves bytes 6-7 exactly as
it found them rather than trusting to luck.

(That the *binding* and *visible-angles* words are right there, live and
writable, is a lead for a later session — this tool does not touch them.)

## Locating is verifying

There is no separate "find the base, then check it" step, because the check
*is* the search: take polygon 0's first vertex as a needle, and for **every**
occurrence of it in the 2 MB, test whether the whole bucket verifies at that
offset. Accept only if exactly **one** offset does.

That is strictly stronger than voting on probe agreement, and it works on the
buckets voting cannot reach -- the corpus has meshes with a single untextured
triangle (10 of them) and with two (10 more). Measured on MAP022: one polygon
is already enough to pin every bucket uniquely in 2 MB, and the search still
reports ambiguity when the needle deserves it (a degenerate all-zero polygon
matches thousands of offsets, and is refused).

`0x8011C498` is where the quads were in one session of one map. Nothing is
cached and nothing is hardcoded: a plausible-but-wrong base writes 24 or 32
bytes per polygon into whatever else lives there.

## The write path is checked too

Verifying proves the *read* arithmetic. Before any real push, the tool rewrites
the disc's own geometry over the top of itself and asserts the write changed
**zero** bytes -- which is the same assertion in the writer's arithmetic, and
the one that catches an off-by-one in the stride, the vertex offset, or the
6-of-8 mask. `--no-selfcheck` skips it; there is no good reason to.

## If the map shatters, look at your transform first

A first test wrote `y -= 260 * sin(quad_index / 9)`. The result was confetti --
and it was the *input* that was nonsense, not the write. Quad index is file
order, which has nothing to do with position: two quads adjacent in the bucket
can be opposite sides of a building, so every shared edge is torn. FFT stores
vertices per polygon -- nothing is welded, so nothing holds an edge together
for you.

Make the displacement a function of the vertex's **own x and z** and coincident
vertices move together, so surfaces stay surfaces. Scale matters too: a tile is
28 units and one height step is 12, so +-18 is a visible swell and +-260 is a
demolition. Verified: `y -= 18 * sin(x/90) * cos(z/90)` leaves Gariland plainly
Gariland, gently rolling.

## Getting into a battle

There is a checked-in one-step route, and it needs no ISO patch and no play-through:

    local f = Support.File.open(
      "reference-assets/thief_whats_this.sstate", "READ")
    PCSX.loadSaveState(f) f:close() PCSX.resumeEmulator()

lands in the Gariland Fight at the Thief's opening line, with all four buckets
of MAP022 a0 in RAM. (It was captured on the *vanilla* disc, so the map wears
its own texture -- the geometry is the same either way. For the patched gate
ISO, use a state captured against it.)

**The savestate blocker was gzip, not this build.** PCSX-Redux writes its GUI
savestates **gzipped** (`SCUS94221.sstate0` is 1.7 MB of a 19 MB state).
`PCSX.loadSaveState` takes the raw stream, and handed a gzip it fails
*silently*: the call returns, `pcall` reports success, and `pc` is left at the
boot vector `0x80000080` while the machine carries on booting to the title
screen -- which is exactly the symptom `docs/map-to-disc-gate.md` recorded for
`formation_screen_ramza_loaded.sstate`, and that file is the one gzipped state
in `reference-assets/`. So:

    gunzip -c ~/.../SCUS94221.sstate1 > /tmp/battle.raw

and load *that*. The separate claim that the `reference-assets` states crash
this build does not reproduce: `thief_whats_this`, `before_magic_city`,
`just_before_last_enemy_magic_city`, `magic_city_march_pc_22` and
`leaving_magic_city_gariland` all load, all leave the emulator alive, and all
land where their names say.

## One copy of the arithmetic

Every address, stride and byte-mask this file used to carry now lives in the
addon's `bpy`-free core (`addons/exmateria_map/live_link.py`, ADR-0005 decision
3) and this is a CLI over it: `plan_at` builds the per-vertex writes, `apply`
does them, `LuaClient` is the transport. What is **not** the core's, and is why
this file still exists, is `locate` -- the needle search that answers *is the
declared map the loaded one*, which the descriptor gate deliberately does not
claim (live-link-v1 decision 2, as amended) and decision 1 still asks.

## What this is and is not

It edits the mesh the game is **rendering**, which is downstream of the map
file. It proves nothing about the disc, it does not survive a map reload, and
`build` remains the only thing that writes bytes anyone else can load. It is a
loupe: see the edit now, ship it with `build` later.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "addons" / "exmateria_map"))

from exmateria_map import corpus, mapfile          # noqa: E402
from exmateria_map.document import BUCKETS, VERTS  # noqa: E402
import live_link as L                              # noqa: E402

#: `BUCKETS` (schema §3, disc order) -> the key `mapfile.Mesh.positions` uses.
MESH_KEY = dict(zip(BUCKETS, ("tt", "tq", "ut", "uq")))


class GeometryError(L.LiveLinkError):
    """Refuse rather than write 32 bytes per polygon at a guessed address.

    A subclass, so a caller that catches the core's `LiveLinkError` catches an
    ambiguous needle too -- the two failures are the same kind of refusal."""


def blob(bucket: str, polys: list) -> bytes:
    """The bucket's coordinates, six bytes per vertex, no padding.

    The needle, and nothing more: it is the core's own write plan laid end to
    end, so the search and the write cannot disagree about what a vertex is."""
    return b"".join(d for _, d in L.plan_at(0, bucket, polys))


def _lua_unhex(name: str, hexed: str) -> str:
    """Lua that rebuilds a binary string from a hex literal."""
    return (f'local _t_{name} = {{}}\n'
            f'local _h_{name} = "{hexed}"\n'
            f'for i = 1, #_h_{name}, 2 do\n'
            f'  _t_{name}[#_t_{name}+1] = '
            f'string.char(tonumber(_h_{name}:sub(i, i+1), 16))\n'
            f'end\n'
            f'local {name} = table.concat(_t_{name})\n')


def locate(client, bucket: str, polys: list, limit: int = 4096) -> int:
    """The one RAM offset at which *every* polygon of the bucket verifies.

    Raises when none does (the map is not loaded, or the mesh has already been
    edited) and when more than one does (the bucket is too weak a needle to
    write against).

    **Not retired by the descriptor block.** `live_link.check_descriptors` is
    the *push* direction's guard and deliberately claims nothing about which
    map is loaded (live-link-v1 decision 2, as amended). This answers exactly
    that question, which decision 1 still asks.
    """
    want = blob(bucket, polys)
    nverts = VERTS[bucket]
    stride = L.POLYGON_STRIDE[bucket]
    lua = f'''
local ffi = require("ffi")
local ram = ffi.string(PCSX.getMemPtr(), {L.RAM_BYTES})
{_lua_unhex("want", want.hex())}
local sig, hits, i = want:sub(1, {L.COORD_BYTES}), {{}}, 1
while #hits < {limit} do
  local j = ram:find(sig, i, true)
  if not j then break end
  i = j + 1
  local base, ok = j - 1, true
  for p = 0, {len(polys) - 1} do
    for k = 0, {nverts - 1} do
      local o = base + p*{stride} + k*{L.VERTEX_STRIDE}
      local w = (p*{nverts} + k) * {L.COORD_BYTES}
      if ram:sub(o + 1, o + {L.COORD_BYTES})
         ~= want:sub(w + 1, w + {L.COORD_BYTES}) then ok = false break end
    end
    if not ok then break end
  end
  if ok then hits[#hits+1] = tostring(base) end
end
return table.concat(hits, "/")
'''
    raw = client.exec(lua, timeout=180).strip()
    found = [int(x) for x in raw.split("/") if x]
    if not found:
        raise GeometryError(
            f"no offset in RAM carries this bucket's {len(polys)} polygon(s) "
            f"-- is the battle on this map, has it finished loading, and is "
            f"the mesh unedited?")
    if len(found) > 1:
        raise GeometryError(
            f"{len(found)} offsets carry this bucket byte-for-byte "
            f"({', '.join(f'0x{L.RAM_BASE + f:08X}' for f in found[:4])}...) "
            f"-- it is too weak a needle to write against; refusing")
    return found[0]


def push(client, base: int, bucket: str, polys: list) -> int:
    """Write the coordinates, leave bytes 6-7 of each vertex alone.

    Returns the number of bytes that actually *changed* -- zero when the caller
    pushes the geometry that is already there, which is the self-check on the
    write arithmetic. Both the arithmetic and the write are the core's now
    (ADR-0005 decision 3): `base` is an OFFSET, as `locate` returns it.
    """
    return L.apply(client, L.plan_at(L.RAM_BASE + base, bucket, polys))


def read_mesh_for(map_dir: Path, map_id: int, arrangement: int):
    files = mapfile.bind(Path(map_dir), map_id)
    rows = sorted((r for r in files.arrangement_rows(arrangement)
                   if r.is_mesh and not r.is_pad), key=lambda r: r.sector)
    for row in rows:
        mesh = mapfile.read_mesh(files.by_sector[row.sector].read_bytes())
        if mesh:
            return mesh, files.by_sector[row.sector].name
    return None, None


def main() -> int:
    p = argparse.ArgumentParser(prog="live_geometry")
    p.add_argument("--map", type=int, required=True)
    p.add_argument("--arrangement", type=int, default=0)
    p.add_argument("--document", type=Path, default=None,
                   help="interchange document to push; omit to verify only")
    p.add_argument("--no-selfcheck", action="store_true",
                   help="skip the zero-change rewrite (there is no good reason)")
    p.add_argument("--host", default=L.DEFAULT_HOST)
    p.add_argument("--port", type=int, default=L.DEFAULT_PORT)
    p.add_argument("--corpus", type=Path, default=None)
    args = p.parse_args()

    map_dir = args.corpus or corpus.map_dir()
    if map_dir is None:
        raise SystemExit("no corpus; set EXMATERIA_ASSETS_DIR")

    mesh, source = read_mesh_for(map_dir, args.map, args.arrangement)
    if mesh is None:
        raise SystemExit(f"MAP{args.map:03d} a{args.arrangement} carries no mesh")

    disc = {b: [list(poly) for poly in mesh.positions[MESH_KEY[b]]]
            for b in BUCKETS}
    live = [b for b in BUCKETS if disc[b]]
    empty = [b for b in BUCKETS if not disc[b]]

    client = L.LuaClient(host=args.host, port=args.port)
    if not client.ping():
        raise SystemExit(f"no emulator answering on {args.host}:{args.port}")

    print(f"{source}: " + ", ".join(f"{len(disc[b])} {b}" for b in BUCKETS))
    if empty:
        print(f"  ({', '.join(empty)}: none in this mesh, nothing to locate)")

    bases: dict[str, int] = {}
    for b in live:
        bases[b] = locate(client, b, disc[b])
        print(f"  {b:20s} {len(disc[b]):4d} @ 0x{L.RAM_BASE + bases[b]:08X} "
              f"({L.POLYGON_STRIDE[b]} B/polygon, "
              f"{L.VERTEX_STRIDE} B/vertex), verified whole")

    if not args.no_selfcheck:
        # `live_link.selfcheck` reads; this one writes the disc's own bytes
        # back and asserts zero changed. Both catch the same class of error --
        # the reading one is better and is what the button and `live_map.py`
        # use. This leg keeps the writing one because it is what the 35/35
        # audit was measured with, and re-measuring it is not free.
        for b in live:
            changed = push(client, bases[b], b, disc[b])
            if changed:
                raise SystemExit(
                    f"self-check FAILED: rewriting {b}'s own disc geometry "
                    f"changed {changed} byte(s); the write arithmetic is wrong "
                    f"-- nothing else was pushed")
        n = sum(len(disc[b]) * VERTS[b] * L.COORD_BYTES for b in live)
        print(f"self-check: rewrote {n:,} disc byte(s), 0 changed")

    if args.document is None:
        print("no --document: verify only, no geometry pushed")
        return 0

    document = json.loads(args.document.read_text())
    doc: dict[str, list] = {b: [] for b in BUCKETS}
    for poly in document["polygons"]:
        doc[poly["kind"]].append(poly["positions"])
    for b in BUCKETS:
        if len(doc[b]) != len(disc[b]):
            raise SystemExit(
                f"the document has {len(doc[b])} {b}, the base map has "
                f"{len(disc[b])}; adding or removing geometry needs `build`, "
                f"not a poke")

    total = 0
    for b in live:
        changed = push(client, bases[b], b, doc[b])
        total += changed
        print(f"  {b:20s} {changed:6,} byte(s) changed")
    print(f"pushed {total:,} changed byte(s) across {len(live)} bucket(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
