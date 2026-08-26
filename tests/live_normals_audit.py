"""Seeded audit of the live NORMALS sink -- needs a running battle.

This is `live_geometry_audit.py`'s job for the second sink, and it exercises
the `bpy`-free core in the addon (`addons/exmateria_map/live_link.py`) rather
than `tools/`, because that is where the arithmetic now lives (ADR-0005).

Every check ships with the defect it catches, seeded on one arm, so a green run
is evidence the check can go red. Not part of `pytest`: it needs PCSX-Redux on
port 8080 with MAP022 a0 loaded and the mesh unedited. One line of Lua gets
you there -- see `tools/live_geometry.py`'s docstring -- and then:

    uv run python -u tests/live_normals_audit.py

**The emulator must be running the interpreter with the debugger on**
(`-interpreter -debugger`) only if you want §7's watchpoint arm to mean
anything; every other check works under the dynarec. See §7's own note.

Seeds are destructive, so each is bracketed by a raw snapshot and a verbatim
restore -- `apply()` cannot undo one, because it writes six bytes of every
eight and a seed can damage the other two.
"""

import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "addons" / "exmateria_map"))

from exmateria_map import corpus, mapfile          # noqa: E402
import live_link as L                              # noqa: E402

MAP, ARRANGEMENT = 22, 0
TEXTURED = ("textured_triangle", "textured_quad")
MESH_KEY = {"textured_triangle": "tt", "textured_quad": "tq"}

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((bool(ok), name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          + (f"  -- {detail}" if detail else ""), flush=True)


#: Set when the run did not reach the end of the checklist. A harness that
#: broke has not CAUGHT anything -- it has stopped measuring -- so it can never
#: print PASS, however many checks were green before it died. The first version
#: of this file printed "10/10 checks pass / PASS" immediately under "the audit
#: itself broke", which is the exact reading a green run is supposed to rule out.
INCOMPLETE = 22                                    # checks a whole run makes


def report(note: str = "") -> int:
    bad = [n for ok, n in results if not ok]
    if note:
        print(f"\n{note}")
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


def write_raw(client: L.LuaClient, address: int, data: bytes) -> int:
    """Verbatim byte restore -- the only thing that can undo a seed.

    Chunked to stay inside one record's length field. Handing `apply` an
    11,552-byte record is what segfaulted the emulator the first time this
    audit ran; `pack_writes` now refuses that outright, and this splits it.
    """
    n = L.RECORD_MAX
    return L.apply(client, [(address + o, data[o:o + n])
                            for o in range(0, len(data), n)])


def mean_luminance(client: L.LuaClient, tag: str) -> float:
    """The picture, as one number. Only a render settles a rendering
    question, and a mean is the cheapest render assertion that is not a
    human looking at two PNGs."""
    path = f"/tmp/exmateria-live-normals-{tag}.raw"
    wh = client.exec(
        f'local s = PCSX.GPU.takeScreenShot() '
        f'local f = Support.File.open("{path}", "CREATE") '
        f'f:writeMoveSlice(s.data) f:close() '
        f'return string.format("%d %d", s.width, s.height)').split()
    w, h = int(wh[0]), int(wh[1])
    raw = Path(path).read_bytes()
    total = 0
    for i in range(0, w * h * 2, 2):
        v = raw[i] | (raw[i + 1] << 8)
        total += (v & 0x1F) + ((v >> 5) & 0x1F) + ((v >> 10) & 0x1F)
    Path(path).unlink(missing_ok=True)
    return total / (w * h * 3)


def main() -> int:
    map_dir = corpus.map_dir()
    if map_dir is None:
        return report("no corpus; set EXMATERIA_ASSETS_DIR")

    files = mapfile.bind(Path(map_dir), MAP)
    mesh = source = None
    for row in sorted((r for r in files.arrangement_rows(ARRANGEMENT)
                       if r.is_mesh and not r.is_pad), key=lambda r: r.sector):
        mesh = mapfile.read_mesh(files.by_sector[row.sector].read_bytes())
        if mesh:
            source = files.by_sector[row.sector].name
            break
    if mesh is None:
        return report(f"MAP{MAP:03d} a{ARRANGEMENT} carries no mesh")

    disc = {b: [list(p) for p in mesh.normals[MESH_KEY[b]]] for b in TEXTURED}
    client = L.LuaClient(port=8080)
    if not client.ping():
        return report("no emulator on port 8080 -- this audit needs a battle")
    print(f"{source}: " + ", ".join(f"{len(disc[b])} {b}" for b in TEXTURED))

    # --- 1. the descriptor block gates the push
    print("\n1. the gate")
    block = L.read_descriptor_block(client)
    try:
        descriptors = L.check_descriptors(block)
        check("the loaded map passes the gate", True)
    except L.LiveLinkError as e:
        check("the loaded map passes the gate", False, str(e))
        return report()
    primary = descriptors[0]
    check("the descriptor's counts are the disc's polygon counts",
          primary.counts == mesh.counts, f"{primary.counts} vs {mesh.counts}")
    check("Gariland has no animated meshes",
          all(d.is_empty() for d in descriptors[1:]),
          f"{sum(1 for d in descriptors[1:] if not d.is_empty())} non-empty")

    # --- 2. SEED: the gate must refuse a block that is not one
    print("\n2. seed -- the gate must go red on garbage")
    for tag, seeded in (("all zero", bytes(len(block))),
                        ("all 0xFF", b"\xff" * len(block))):
        try:
            L.check_descriptors(seeded)
            check(f"a {tag} block is refused", False, "it was accepted")
        except L.LiveLinkError as e:
            check(f"a {tag} block is refused", True, str(e).split(" -- ")[0])

    # --- 3. the read arithmetic: RAM carries the disc's normals verbatim
    print("\n3. read -- the normal arrays are the disc's bytes")
    spans, snaps = {}, {}
    for b in TEXTURED:
        i = L.BUCKETS.index(b)
        spans[b] = primary.counts[i] * L.POLYGON_STRIDE[b]
        addr = L.SINKS[b].normals + primary.starts[i] * L.POLYGON_STRIDE[b]
        snaps[b] = client.read(addr, spans[b])
        want = L.plan(primary, b, "normals", disc[b])
        bad = sum(1 for a, data in want
                  if snaps[b][a - addr:a - addr + len(data)] != data)
        check(f"{b}: every vertex's normal matches the disc",
              bad == 0, f"{len(want) - bad}/{len(want)} vertices")

    # --- 4. the write path: rewriting the engine's own bytes changes zero
    print("\n4. write path -- zero-change rewrite")
    for b in TEXTURED:
        try:
            L.selfcheck(client, L.plan(primary, b, "normals", disc[b]))
            check(f"{b} rewrite changes 0 bytes", True)
        except L.LiveLinkError as e:
            check(f"{b} rewrite changes 0 bytes", False, str(e))

    # --- 5. SEED: a wrong stride must make that self-check go red
    print("\n5. seed -- a wrong stride must break the zero-change rewrite")
    b = "textured_quad"
    i = L.BUCKETS.index(b)
    addr = L.SINKS[b].normals + primary.starts[i] * L.POLYGON_STRIDE[b]
    real = L.VERTEX_STRIDE
    try:
        L.VERTEX_STRIDE = 6                        # the packed-file layout
        L.POLYGON_STRIDE[b] = L.VERTS[b] * 6
        try:
            L.selfcheck(client, L.plan(primary, b, "normals", disc[b]))
            check("a 6-byte vertex stride is caught", False, "it changed 0 bytes")
        except L.LiveLinkError as e:
            check("a 6-byte vertex stride is caught", "self-check FAILED" in str(e))
    finally:
        L.VERTEX_STRIDE = real
        L.POLYGON_STRIDE[b] = L.VERTS[b] * real
    # The self-check is non-destructive, so a caught seed must have written
    # NOTHING. The first version wrote to check, which meant a failing check
    # corrupted the array it had just condemned.
    check("a failed self-check writes nothing",
          write_raw(client, addr, snaps[b]) == 0, "the array is still pristine")

    # --- 6. SEED: the wrong ARRAY must be caught too
    print("\n6. seed -- pushing normals at the position base must go red")
    pos_addr = L.SINKS[b].positions + primary.starts[i] * L.POLYGON_STRIDE[b]
    pos_snap = client.read(pos_addr, spans[b])
    misplaced = [(a - L.SINKS[b].normals + L.SINKS[b].positions, d)
                 for a, d in L.plan(primary, b, "normals", disc[b])]
    changed = L.apply(client, misplaced)
    check("normals written over positions change bytes",
          changed != 0, f"changed={changed}")
    check("the misplacement is undone verbatim",
          write_raw(client, pos_addr, pos_snap) != 0)
    check("...and the position array is pristine again",
          write_raw(client, pos_addr, pos_snap) == 0)

    # --- 7. the picture: zeroed normals must darken the map, and only then
    #       does "the engine re-lights from these bytes every frame" mean
    #       anything. A `Read` watchpoint on these same addresses reports
    #       ZERO hits per frame while a watchpoint on the POSITION array
    #       reports two -- so the watchpoint is not the instrument here, and
    #       §5 of live-link-v1.md names the wrong one. A render is.
    print("\n7. the picture -- only a render settles a rendering question")
    time.sleep(0.5)
    lit = mean_luminance(client, "lit")
    flat = [[(0, 0, 0)] * L.VERTS[b] for _ in disc[b]]
    for bucket in TEXTURED:
        f = [[(0, 0, 0)] * L.VERTS[bucket] for _ in disc[bucket]]
        L.apply(client, L.plan(primary, bucket, "normals", f))
    time.sleep(0.5)
    dark = mean_luminance(client, "dark")
    check("zeroing every normal darkens the picture", dark < lit,
          f"mean luminance {lit:.2f} -> {dark:.2f}")
    check("...and it is not a rounding wobble", lit - dark > 0.5,
          f"delta {lit - dark:.2f}/31")
    for bucket in TEXTURED:
        j = L.BUCKETS.index(bucket)
        a = L.SINKS[bucket].normals + primary.starts[j] * L.POLYGON_STRIDE[bucket]
        write_raw(client, a, snaps[bucket])
    time.sleep(0.5)
    back = mean_luminance(client, "back")
    check("restoring them brings the light back", abs(back - lit) < 0.2,
          f"mean luminance {back:.2f} vs {lit:.2f}")

    # --- 8. push moves normals and leaves bytes 6-7 alone
    print("\n8. the two trailing bytes are not ours")
    before = client.read(addr, spans[b])
    nv = len(disc[b]) * L.VERTS[b]
    tilted = [[(v[0], v[1] + 1, v[2]) for v in poly] for poly in disc[b]]
    L.apply(client, L.plan(primary, b, "normals", tilted))
    after = client.read(addr, spans[b])
    check("every vertex's normal moved",
          sum(1 for k in range(nv)
              if before[k * 8:k * 8 + 6] != after[k * 8:k * 8 + 6]) == nv)
    check("no vertex's trailing 2 bytes moved",
          all(before[k * 8 + 6:k * 8 + 8] == after[k * 8 + 6:k * 8 + 8]
              for k in range(nv)))
    nonzero = sum(1 for k in range(nv)
                  if before[k * 8 + 6:k * 8 + 8] != b"\x00\x00")
    # Measured 2026-08-26: the trailing pair is 0x0000 for all 1,444 vertices
    # of the quad NORMAL array, and non-zero for most of the same vertices of
    # the POSITION array. The polygon metadata -- the terrain binding word on
    # vertex 0, VISIBLE_ANGLES on vertex 1 -- rides the positions only.
    # `live_geometry.py` measured that for positions and it does NOT carry to
    # normals, so the check above is vacuous here and saying so is the point:
    # the 6-of-8 mask is conservative in the normal arrays rather than
    # load-bearing, and a future sink must not read the reverse into it.
    check("normals carry no metadata in their trailing pair", nonzero == 0,
          f"{nonzero}/{nv} non-zero")
    pos_now = client.read(pos_addr, spans[b])
    pos_nonzero = sum(1 for k in range(nv)
                      if pos_now[k * 8 + 6:k * 8 + 8] != b"\x00\x00")
    check("...unlike positions, where the metadata lives", pos_nonzero > 0,
          f"{pos_nonzero}/{nv} non-zero in the POSITION array")

    for bucket in TEXTURED:
        j = L.BUCKETS.index(bucket)
        a = L.SINKS[bucket].normals + primary.starts[j] * L.POLYGON_STRIDE[bucket]
        write_raw(client, a, snaps[bucket])
    check("everything restored", all(
        write_raw(client,
                  L.SINKS[bk].normals
                  + primary.starts[L.BUCKETS.index(bk)] * L.POLYGON_STRIDE[bk],
                  snaps[bk]) == 0 for bk in TEXTURED))
    return report()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as e:                     # noqa: BLE001
        print(f"\nthe audit itself broke: {type(e).__name__}: {e}")
        raise SystemExit(report("HARNESS BROKE -- the checks below are all "
                                "that ran; a crash is not a catch"))
