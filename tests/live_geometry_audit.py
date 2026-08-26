"""Seeded audit of the live geometry leg -- needs a running battle.

Every check here ships with the defect it catches, seeded on one arm, so a
green run is evidence the check can go red. It is **not** part of `pytest`:
it needs PCSX-Redux on port 8080 with MAP022 a0 (the Gariland Fight) loaded and
the mesh unedited. One line of Lua gets you there from any running emulator --
`PCSX.loadSaveState` on `reference-assets/thief_whats_this.sstate`, see
`tools/live_geometry.py`'s docstring -- and then:

    python3 -u tests/live_geometry_audit.py

`live_geometry.py` is a thin CLI over `live_link.py` now (ADR-0005 decision 3),
so this audit drives the CORE's arithmetic through it: `plan_at`, `apply` and
`LuaClient`. That is the point of running it after the move -- if the thinning
broke a stride or a mask, check 4's zero-change rewrite is what says so.

Every seed here is destructive, so each one is bracketed by a **raw byte
snapshot and a verbatim restore** -- `push()` cannot undo a seed, because it
only writes six bytes of every eight and the seeds damage the other two.
"""

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "addons" / "exmateria_map"))

from exmateria_map import corpus, dump                             # noqa: E402
from exmateria_map.document import BUCKETS, VERTS                  # noqa: E402
import live_geometry as G                                          # noqa: E402
import live_link as L                                              # noqa: E402

MAP, ARRANGEMENT = 22, 0

#: How many checks a WHOLE run makes. A run that stopped early has caught
#: nothing, and without this bar it prints PASS on the ones that did run --
#: which is what `live_normals_audit.py` shipped once and had to fix.
INCOMPLETE = 35

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((bool(ok), name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))


def read_ram(client, base: int, length: int) -> bytes:
    lua = (f'local ffi = require("ffi")\n'
           f'return (ffi.string(PCSX.getMemPtr() + {base}, {length}):gsub(".",'
           f' function(c) return string.format("%02x", string.byte(c)) end))')
    return bytes.fromhex(client.exec(lua, timeout=180).strip())


def write_ram(client, base: int, data: bytes) -> int:
    """Verbatim byte restore -- the only thing that can undo a seed."""
    lua = f'''
local mem = PCSX.getMemPtr()
local h = "{data.hex()}"
local n = 0
for i = 0, {len(data) - 1} do
  local c = tonumber(h:sub(i*2 + 1, i*2 + 2), 16)
  if mem[{base} + i] ~= c then mem[{base} + i] = c n = n + 1 end
end
return tostring(n)'''
    return int(client.exec(lua, timeout=300).strip())


def main() -> int:
    map_dir = corpus.map_dir()
    if map_dir is None:
        raise SystemExit("no corpus; set EXMATERIA_ASSETS_DIR")
    mesh, source = G.read_mesh_for(Path(map_dir), MAP, ARRANGEMENT)
    disc = {b: [list(p) for p in mesh.positions[G.MESH_KEY[b]]] for b in BUCKETS}
    live = [b for b in BUCKETS if disc[b]]

    client = L.LuaClient()
    if not client.ping():
        raise SystemExit("no emulator on port 8080 -- this audit needs a battle")
    print(f"{source}: " + ", ".join(f"{len(disc[b])} {b}" for b in BUCKETS))

    # --- 1. every bucket locates, uniquely
    print("\n1. locate")
    bases = {}
    for b in live:
        try:
            bases[b] = G.locate(client, b, disc[b])
            check(f"{b} locates", True, f"0x{L.RAM_BASE + bases[b]:08X}")
        except G.GeometryError as e:
            check(f"{b} locates", False, str(e))
    check("the four bases are distinct", len(set(bases.values())) == len(bases))
    if len(bases) != len(live):
        return report()
    spans = {b: len(disc[b]) * L.POLYGON_STRIDE[b] for b in live}
    snaps = {b: read_ram(client, bases[b], spans[b]) for b in live}

    # --- 2. SEED: a bucket that is not in RAM must be refused, not guessed
    print("\n2. seed -- one wrong coordinate must make the bucket unfindable")
    for b in live:
        seeded = [[list(v) for v in poly] for poly in disc[b]]
        seeded[-1][-1][1] += 7                     # last vertex, y, off by 7
        try:
            G.locate(client, b, seeded)
            check(f"{b} refuses a mesh that is not there", False, "it located one")
        except G.GeometryError as e:
            check(f"{b} refuses a mesh that is not there", "no offset" in str(e))

    # --- 3. SEED: an ambiguous needle must be refused, not written
    print("\n3. seed -- a needle with no entropy must be refused as ambiguous")
    try:
        G.locate(client, "textured_quad", [[(0, 0, 0)] * 4])
        check("an all-zero polygon is refused", False, "it located exactly one")
    except G.GeometryError as e:
        check("an all-zero polygon is refused", "too weak a needle" in str(e))

    # --- 4. the write path: rewriting the disc's own bytes changes zero
    print("\n4. write path -- zero-change rewrite")
    for b in live:
        changed = G.push(client, bases[b], b, disc[b])
        check(f"{b} rewrite changes 0 bytes", changed == 0, f"changed={changed}")

    # --- 5. SEED: a wrong stride must make that self-check go red
    print("\n5. seed -- a wrong stride must break the zero-change rewrite")
    # The stride now lives in the core, so the seed goes there. Seeding
    # `G.VERTEX_STRIDE` would be INERT after the thinning -- the module no
    # longer reads it -- and an inert seed reads exactly like a blind check.
    b = "textured_quad"
    real_v, real_p = L.VERTEX_STRIDE, L.POLYGON_STRIDE[b]
    try:
        L.VERTEX_STRIDE = 6                        # the packed-file layout
        L.POLYGON_STRIDE[b] = VERTS[b] * 6
        changed = G.push(client, bases[b], b, disc[b])
        check("a 6-byte vertex stride is caught", changed != 0, f"changed={changed}")
    finally:
        L.VERTEX_STRIDE, L.POLYGON_STRIDE[b] = real_v, real_p
    repaired = write_ram(client, bases[b], snaps[b])
    check("the seed is undone verbatim", repaired != 0, f"restored={repaired} byte(s)")
    check("...and the restore is idempotent",
          write_ram(client, bases[b], snaps[b]) == 0)
    check("...and push agrees the bucket is pristine",
          G.push(client, bases[b], b, disc[b]) == 0)

    # --- 6. push moves coordinates and leaves bytes 6-7 alone
    print("\n6. the two trailing bytes are not ours")
    b = "textured_quad"
    nv = len(disc[b]) * VERTS[b]
    before = snaps[b]
    moved = [[(v[0], v[1] + 1, v[2]) for v in poly] for poly in disc[b]]
    G.push(client, bases[b], b, moved)
    after = read_ram(client, bases[b], spans[b])
    nonzero = sum(1 for i in range(nv) if before[i * 8 + 6:i * 8 + 8] != b"\x00\x00")
    check("every vertex's coordinates moved",
          sum(1 for i in range(nv)
              if before[i * 8:i * 8 + 6] != after[i * 8:i * 8 + 6]) == nv)
    check("no vertex's trailing 2 bytes moved",
          all(before[i * 8 + 6:i * 8 + 8] == after[i * 8 + 6:i * 8 + 8]
              for i in range(nv)))
    check("...and that is not vacuous", nonzero > 0,
          f"{nonzero}/{nv} trailing pairs are non-zero")
    write_ram(client, bases[b], before)
    check("restored", G.push(client, bases[b], b, disc[b]) == 0)

    # --- 7. what those two bytes actually are
    print("\n7. the trailing short is the polygon's metadata, not padding")
    data = None
    from exmateria_map import mapfile
    files = mapfile.bind(Path(map_dir), MAP)
    for row in sorted((r for r in files.arrangement_rows(ARRANGEMENT)
                       if r.is_mesh and not r.is_pad), key=lambda r: r.sector):
        blob = files.by_sector[row.sector].read_bytes()
        if mapfile.read_mesh(blob):
            data = blob
            break
    polys = dump.polygons(mesh, dump.visible_angle_slots(data))
    tt, tq, ut, uq = mesh.counts
    bind_at = mesh.end - (tt + tq) * 2
    doff = {BUCKETS[0]: 0, BUCKETS[1]: tt, BUCKETS[2]: tt + tq,
            BUCKETS[3]: tt + tq + ut}
    for b in live:
        n, verts = len(disc[b]), VERTS[b]
        ram = read_ram(client, bases[b], spans[b])
        textured = b in (BUCKETS[0], BUCKETS[1])
        row0 = 0 if b in (BUCKETS[0], BUCKETS[2]) else (tt if textured else ut)
        v0 = v1 = rest = 0
        for i in range(n):
            def word(k, i=i, verts=verts):
                o = i * verts * 8 + k * 8 + 6
                return struct.unpack("<H", ram[o:o + 2])[0]
            vis = polys[doff[b] + i]["visible_angles"]
            if textured:
                o = bind_at + (row0 + i) * 2
                v0 += word(0) == struct.unpack("<H", data[o:o + 2])[0]
                v1 += word(1) == (vis | 1)
            else:
                v0 += word(0) == 0
                v1 += word(1) == vis
            rest += all(word(k) == 0 for k in range(2, verts))
        check(f"{b}: vertex 0 carries "
              f"{'the terrain binding word' if textured else '0x0000'}",
              v0 == n, f"{v0}/{n}")
        check(f"{b}: vertex 1 carries visible_angles"
              f"{' | 1' if textured else ''}", v1 == n, f"{v1}/{n}")
        check(f"{b}: vertices 2.. carry 0x0000", rest == n, f"{rest}/{n}")

    # --- 8. SEED: that identification must not be a tautology
    print("\n8. seed -- the identification must go red on a wrong claim")
    b = "textured_quad"
    ram = read_ram(client, bases[b], spans[b])
    wrong = sum(1 for i in range(len(disc[b]))
                if struct.unpack("<H", ram[i * 32 + 6:i * 32 + 8])[0]
                == (polys[doff[b] + i]["visible_angles"] | 1))
    check("vertex 0 is NOT the visible-angles word",
          wrong < len(disc[b]), f"{wrong}/{len(disc[b])} would have matched")

    for b in live:
        write_ram(client, bases[b], snaps[b])
    return report()


def report() -> int:
    bad = [n for ok, n in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} checks pass")
    if bad:
        print("FAILED: " + "; ".join(bad))
    short = len(results) < INCOMPLETE
    if short:
        print(f"INCOMPLETE: {len(results)} of {INCOMPLETE} checks ran -- the "
              "run stopped early, so the ones that did not run are UNKNOWN, "
              "not green")
    ok = bool(results) and not bad and not short
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
