"""Push a whole interchange document into a running battle — one frame, no ISO.

    python3 tools/live_map.py --map 22 --arrangement 0             # verify only
    python3 tools/live_map.py --map 22 --arrangement 0 --document d.json

The arithmetic and the addresses live in the **addon** — `live_link.py`, the
`bpy`-free core (ADR-0005 decisions 1-3). This file is a CLI over it and holds
no layout knowledge of its own: one copy, no identity guard, and `tools/`
imports the addon rather than the other way round. That deliberately reverses
the `png_indexed.py` precedent, and the discriminator is which artifact ships.

## What it does, in order

1. Read the **descriptor block** and gate on it (`live-link-v1.md` §2.1). No
   map loaded, or a slice running off the end of its array, and it refuses.
2. Read the base map off the disc and assert that the planned addresses
   already hold **its own bytes**, zero differing. This is the check that
   catches a stride, vertex-offset or field-mask error in the rig's own
   arithmetic. It reads and never writes, so a failing check cannot damage
   what it condemned. `--no-selfcheck` skips it — and note it fires on a
   healthy rig once you have already pushed, because a push edits exactly
   these bytes; reload the savestate for a pristine map.
3. Push the document, and **name every field it could not push**.

## What it is not

It edits the mesh the game is **rendering**, which is downstream of the map
file. It proves nothing about the disc, it does not survive a map reload
(weather, time of day, leaving and re-entering all upload the disc's bytes back
over everything pushed), and `build` remains the only thing that writes bytes
anyone else can load. It is a loupe: see the edit now, ship it with `build`.

## Getting into a battle

    local f = Support.File.open("reference-assets/thief_whats_this.sstate", "READ")
    PCSX.loadSaveState(f) f:close() PCSX.resumeEmulator()

lands in the Gariland Fight with all four buckets of MAP022 a0 in RAM.
PCSX-Redux's own GUI savestates are **gzipped** and `PCSX.loadSaveState` fails
*silently* on one — `gunzip -c` first. Full recipe in `live_geometry.py`'s
docstring.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "addons" / "exmateria_map"))

from exmateria_map import corpus, mapfile          # noqa: E402
import live_link as L                              # noqa: E402

#: `live_link.BUCKETS` -> the key `mapfile.Mesh` uses.
MESH_KEY = dict(zip(L.BUCKETS, ("tt", "tq", "ut", "uq")))


def base_document(map_dir: Path, map_id: int, arrangement: int):
    """The base map's own geometry, in document shape — what the self-check
    rewrites and what the document is checked against."""
    files = mapfile.bind(Path(map_dir), map_id)
    for row in sorted((r for r in files.arrangement_rows(arrangement)
                       if r.is_mesh and not r.is_pad), key=lambda r: r.sector):
        mesh = mapfile.read_mesh(files.by_sector[row.sector].read_bytes())
        if mesh:
            polygons = []
            for bucket in L.BUCKETS:
                key = MESH_KEY[bucket]
                for i in range(len(mesh.positions[key])):
                    poly = {"kind": bucket,
                            "positions": [list(v) for v in mesh.positions[key][i]]}
                    if key in mesh.normals:
                        poly["normals"] = [list(v) for v in mesh.normals[key][i]]
                    polygons.append(poly)
            return {"polygons": polygons}, files.by_sector[row.sector].name
    return None, None


def main() -> int:
    p = argparse.ArgumentParser(prog="live_map")
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
    base, source = base_document(Path(map_dir), args.map, args.arrangement)
    if base is None:
        raise SystemExit(f"MAP{args.map:03d} a{args.arrangement} carries no mesh")

    client = L.LuaClient(host=args.host, port=args.port)
    if not client.ping():
        raise SystemExit(f"no emulator answering on {args.host}:{args.port}")

    # 1. the gate
    try:
        descriptors = L.check_descriptors(L.read_descriptor_block(client))
    except L.LiveLinkError as e:
        raise SystemExit(f"gate: {e}")
    primary = descriptors[0]
    print(f"{source}: " + ", ".join(
        f"{n} {b}" for b, n in zip(L.BUCKETS, primary.counts) if n))
    animated = [d.index for d in descriptors[1:] if not d.is_empty()]
    if animated:
        print(f"  {len(animated)} animated mesh(es) share these arrays "
              f"(descriptors {animated}); slices honoured via start index")

    # 2. the write-path self-check
    if not args.no_selfcheck:
        try:
            plans = L.plan_document(primary, base)
            for (bucket, field), writes in sorted(plans.items()):
                L.selfcheck(client, writes)
            n = sum(len(w) for w in plans.values())
            print(f"self-check: {n:,} vertex run(s) at the planned addresses "
                  f"hold the base map's own bytes, 0 differ")
        except L.LiveLinkError as e:
            raise SystemExit(str(e))

    if args.document is None:
        print("no --document: verified only, nothing pushed")
        return 0

    # 3. the push
    document = json.loads(args.document.read_text())
    try:
        plans = L.plan_document(primary, document)
    except L.LiveLinkError as e:
        raise SystemExit(f"{e}\n  nothing was pushed")

    total = 0
    for (bucket, field), writes in sorted(plans.items()):
        changed = L.apply(client, writes)
        total += changed
        print(f"  {bucket:20s} {field:10s} {changed:7,} byte(s) changed")
    print(f"pushed {total:,} changed byte(s)")

    pushed = {f for _, f in plans}
    skipped = dict(L.UNPUSHED)
    if "normals" in pushed:
        skipped.pop("map_states[].light_rig", None)
    print("\nnot pushed (decision 4 -- named, not refused):")
    for field, why in sorted(skipped.items()):
        print(f"  {field:28s} {why}")
    print("\nThis is a picture, not a disc. A map reload uploads the disc's "
          "bytes back over all of it; `build` is what ships.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
