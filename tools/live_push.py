"""The live authoring rig: Blender open, PCSX-Redux open, push in real time.

Export from the addon, see it in the running battle about a quarter of a second
later. No ISO, no reboot, no walk back to the map.

    # emulator already running with the web server on 8080, sitting in a battle
    python3 tools/live_push.py --watch ~/maps/MAP022.a0 --map 22

`--watch` is the directory the addon's export operator writes (schema §1: the
document plus one PNG sidecar per distinct sheet). Every time a sidecar
changes, its indices are repacked to the disc's 4bpp layout and pushed into the
emulator's VRAM.

## How the push works, and why it is not a VRAM poke

This pcsx-redux fork exposes **no VRAM write** -- `PCSX.GPU` has only
`takeScreenShot`, and `POST /api/v1/gpu/vram/raw` is a 400. But a savestate
*is* a VRAM image, and `PCSX.createSaveState()` is bound to Lua. So a push is:

    save the CURRENT moment -> patch the sheet's bytes -> load it back

Because it saves first, the battle resumes exactly where it was; this is a
push, not a rewind. Measured on a Gariland battle: **~0.25 s** per push, of
which ~0.10 s is the 19 MB state crossing the disk.

A real poke would be smaller and faster, and it is a ~10-line change to our own
fork rather than a research project: `GPU::partialUpdateVRAM(x, y, w, h,
pixels)` already exists and is what the emulator itself calls for a CPU->VRAM
transfer. It is simply not registered in `pcsxlua.cc`. Until it is, this is the
rig.

## The two things that will bite

**The origin moves.** The sheet's offset *inside the savestate* is not stable
between saves of the same session -- measured 17378312 / 17378314 / 17378318 /
17378325 on four consecutive pushes, because the protobuf fields ahead of VRAM
are variable-length. It is re-derived every push (windowed around the last one,
so it costs nothing) and re-checked. A cached constant would write every row at
a few bytes' skew, which is a corrupt texture rather than an error.

**The game owns VRAM.** The sheet is uploaded at map load and not re-uploaded
per frame, which is what makes a push stick. A state change that reloads the
map (weather, time of day, leaving and re-entering) will upload the disc's
bytes over your push. That is not a bug to fix here -- it is the reminder that
this rig shows you a picture, and `build` is what puts bytes on a disc.
"""

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "addons" / "exmateria_map"))

from exmateria_map import corpus, mapfile                    # noqa: E402
from exmateria_map.png_indexed import pack_4bpp, read_indexed_png   # noqa: E402
import live_link as L                                        # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vram_swap_sheet as V                                  # noqa: E402

STATE_RAW = "/tmp/exmateria_live_state.raw"
PATCHED_RAW = "/tmp/exmateria_live_patched.raw"
# What this rig last pushed, so a RESTART can find its own sheet in VRAM.
# Without it a fresh process only knows the disc's blob, and the moment you
# have pushed anything the rig can no longer locate the rectangle it just
# wrote -- it refuses, correctly, and is then stuck until the map reloads.
LIVE_CACHE = Path("/tmp/exmateria_live_sheet.bin")
LAST_PUSH = "the last push"


class Pusher:
    """Holds the emulator connection and what it believes is in VRAM.

    `candidates` is `{name: sheet}` -- **every** sheet that could be in VRAM
    right now, not the one being written. That is decision 9's fix 3: the rig
    used to anchor on the *aimed* state's disc blob, so aiming at a state the
    battle is not in left nothing to match and the push refused. **Locate by
    what is there, write what you aim at.**
    """

    def __init__(self, agent, candidates: dict):
        self.agent = agent
        self.hint = None
        self.candidates = {n: b for n, b in candidates.items()
                           if len(b) == mapfile.TEXTURE_BYTES}
        cached = LIVE_CACHE.read_bytes() if LIVE_CACHE.is_file() else None
        if cached and len(cached) == mapfile.TEXTURE_BYTES:
            self._promote(LAST_PUSH, cached)
        if not self.candidates:
            raise V.SwapError("no sheet to anchor on: neither the disc's rows "
                              "nor an exported sidecar could be read")
        self.live_name, self.live = next(iter(self.candidates.items()))
        self.located = None         # what the last push found in VRAM, by name

    def _promote(self, name: str, sheet: bytes) -> None:
        """Put `name` at the front of the candidates, replacing any older one."""
        rest = {n: b for n, b in self.candidates.items() if n != name}
        self.candidates = {name: sheet, **rest}

    def find(self, state) -> tuple[int, str]:
        """The sheet's origin in this savestate, and the NAME of what is there.

        Locating and identifying are two questions and the answers differ the
        moment anything has been pushed. `locate` needs a candidate with
        distinctive rows to derive an origin at all -- an authored sheet may
        have none -- while `identify` says which candidate those bytes actually
        are. Deriving the origin from one sheet and then assuming the origin
        holds *that* sheet is how a rig writes a neighbouring state's art at a
        confident, wrong offset.
        """
        if self.hint is not None and self.live is not None:
            # The fast, content-independent path: the sheet we pushed last is
            # a few bytes from where it was, and it may be far too uniform for
            # a content scan to find (a flat checker has no distinctive row).
            try:
                return V.relocate(state, self.live, self.hint), self.live_name
            except V.SwapError:
                pass
        for name, blob in self.candidates.items():
            try:
                origin = V.locate(state, blob, hint=self.hint)
            except V.SwapError:
                continue
            who = V.identify(state, origin, self.candidates)
            if who is not None:
                return origin, who
        raise V.SwapError(
            "none of this map's sheets is in VRAM (" +
            ", ".join(self.candidates) + ") -- the emulator is not in this "
            "map, or the game reloaded it over the push"
        )

    def _lua(self, code):
        return self.agent.exec_lua(code, timeout=120)

    def _save(self) -> bytearray:
        self._lua(f'local s = PCSX.createSaveState()\n'
                  f'local f = Support.File.open("{STATE_RAW}", "TRUNCATE")\n'
                  f'f:writeMoveSlice(s); f:close(); return "ok"')
        # The emulator's write is not finished when the call returns; reading
        # straight away gets a prefix (7.5 MB of an eventual 19 MB, once).
        path, last = Path(STATE_RAW), -1
        for _ in range(200):
            size = path.stat().st_size
            if size == last and size:
                break
            last = size
            time.sleep(0.02)
        return bytearray(path.read_bytes())

    def push(self, sheet: bytes) -> tuple[int, float]:
        started = time.time()
        state = self._save()
        self.hint, self.located = self.find(state)
        changed = V.write(state, self.hint, sheet)
        Path(PATCHED_RAW).write_bytes(bytes(state))
        self._lua(f'local f = Support.File.open("{PATCHED_RAW}", "READ")\n'
                  f'PCSX.loadSaveState(f); f:close(); PCSX.resumeEmulator()\n'
                  f'return "ok"')
        self.live, self.live_name = sheet, LAST_PUSH
        self._promote(LAST_PUSH, sheet)
        LIVE_CACHE.write_bytes(sheet)
        return changed, time.time() - started


def sheet_from_sidecar(path: Path) -> bytes:
    width, height, indices, _plte, _trns = read_indexed_png(path.read_bytes())
    if (width, height) != (256, 1024):
        raise ValueError(f"{path.name}: {width}x{height}, expected 256x1024")
    return pack_4bpp(indices)


def load_document(watch: Path) -> dict:
    docs = sorted(watch.glob("MAP*.a*.json"))
    if not docs:
        raise SystemExit(f"no interchange document in {watch}")
    return json.loads(docs[0].read_text())


def aim_at(document: dict, night: int, weather: int) -> L.Aim:
    """`--night` / `--weather` resolved as decision 9's AIM, not as a lookup.

    They were already this key; they just resolved through a private loop that
    found a TEXTURE row and stopped, so the tool could not name the rig row,
    the palette row, or the kind it had landed on -- the three things the
    report is meant to say. `live_link.aim` is the shared resolver, and using
    it is what makes "aimed at" mean the same thing here and in the addon.

    The aim's third key is `kind`, and a CLI has no preview to read it from, so
    it is the group's first row. That costs nothing today: `aim` selects rows
    by what they CARRY, not by kind, and all 16 corpus groups holding two rigs
    hold byte-identical ones.
    """
    states = document["map_states"]
    index = next((i for i, s in enumerate(states)
                  if s["night"] == night and s["weather"] == weather), None)
    if index is None:
        raise SystemExit(f"the document carries no state at night={night} "
                         f"weather={weather}")
    return L.aim(states, index)


def sidecar_for(watch: Path, at: L.Aim) -> Path:
    """The sidecar the aim WRITES -- always the group's TEXTURE row."""
    if at.sheet_row is None:
        raise SystemExit(
            f"the group at night={at.night} weather={at.weather} carries no "
            f"TEXTURE row, so it has no sheet to push (71 of the corpus's 774 "
            f"groups are like this). Its rig and palettes are a different leg."
        )
    return watch / at.sheet_row["texture_sheet"]


def anchors(document: dict, map_dir: Path, watch: Path,
            at: L.Aim) -> dict[str, bytes]:
    """Every sheet that could be in VRAM right now, best guess first.

    Decision 9's fix 3. What is in VRAM is whatever the battle loaded, plus
    whatever this rig has already pushed over it -- and neither is necessarily
    the state being aimed at. So each of the map's TEXTURE rows contributes two
    anchors: the sidecar (what the artist has exported, which is what is on
    screen if the last push landed) and the disc's blob (what a freshly loaded
    map holds). Only the WRITE is aimed.

    Short blobs are dropped rather than refused: a mesh resource is 2,048 bytes
    of palettes and rig, and comparing one against a 131,072-byte rectangle is
    not a near miss, it is a different question.
    """
    out: dict[str, bytes] = {}

    def add(name: str, blob: bytes | None) -> None:
        if (blob is not None and len(blob) == mapfile.TEXTURE_BYTES
                and blob not in out.values()):
            out.setdefault(name, blob)

    rows = [s for s in document["map_states"] if s["texture_sheet"]]
    if at.sheet_row is not None:
        rows = [at.sheet_row] + [s for s in rows if s is not at.sheet_row]
    for row in rows:
        try:
            add(f"the sidecar {row['texture_sheet']}",
                sheet_from_sidecar(watch / row["texture_sheet"]))
        except (OSError, ValueError):
            pass
        try:
            add(f"the disc's {row['resource']}",
                (map_dir / row["resource"]).read_bytes())
        except OSError:
            pass
    return out


def describe(document: dict, at: L.Aim) -> str:
    """The aim, everyone who moves with it, and what is NOT going with it.

    Decision 27's rule -- every state the act touched is NAMED in its report --
    carried to the push, which needs it more: the sheet is shared by every
    state naming the same sidecar, so "pushed weather 1" can be a third of the
    truth. Decision 4's other half is here too: the CLUTs the sheet is read
    through are ONE ATOM with it and this leg does not move them, so the line
    that says so is not a footnote.
    """
    row = at.sheet_row or at.rig_row or {}
    lines = [f"aimed at night={at.night} weather={at.weather} kind {at.kind}"
             f" -> {row.get('resource', '?')}"]
    for field, where in L.also_moved(document["map_states"], at).items():
        if where:
            lines.append(f"  the {field} also shows in "
                         + ", ".join(str(k) for k in where))
    lines.append(f"  NOT pushed -- map_states[].palettes: "
                 f"{L.UNPUSHED['map_states[].palettes']}")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(prog="live_push", description=__doc__.splitlines()[0])
    p.add_argument("--watch", type=Path, required=True,
                   help="the directory the addon exports into")
    p.add_argument("--map", type=int, required=True)
    p.add_argument("--arrangement", type=int, default=0)
    p.add_argument("--night", type=int, default=0)
    p.add_argument("--weather", type=int, default=0)
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--once", action="store_true", help="push once and exit")
    p.add_argument("--poll", type=float, default=0.4)
    p.add_argument("--corpus", type=Path, default=None)
    args = p.parse_args()

    try:
        from pcsx_agent import PcsxAgent
    except ImportError:
        raise SystemExit("pcsx_agent not importable: pip install -e ../pcsx-agent")

    map_dir = args.corpus or corpus.map_dir()
    if map_dir is None:
        raise SystemExit("no corpus; set EXMATERIA_ASSETS_DIR")

    document = load_document(args.watch)
    at = aim_at(document, args.night, args.weather)
    sidecar = sidecar_for(args.watch, at)
    candidates = anchors(document, Path(map_dir), args.watch, at)
    if not candidates:
        raise SystemExit(f"no {mapfile.TEXTURE_BYTES}-byte sheet in {map_dir} "
                         f"or {args.watch} to anchor the locate on")

    agent = PcsxAgent(port=args.port)
    if not agent.ping():
        raise SystemExit(f"no emulator on port {args.port}")
    pusher = Pusher(agent, candidates)
    print(f"watching {sidecar.name} (map {args.map} a{args.arrangement})")
    print(describe(document, at))

    seen = None
    while True:
        try:
            blob = sheet_from_sidecar(sidecar)
        except (OSError, ValueError) as exc:
            time.sleep(args.poll)                # mid-write from Blender
            continue
        digest = hashlib.sha256(blob).hexdigest()[:8]
        if digest != seen:
            try:
                changed, elapsed = pusher.push(blob)
            except V.SwapError as exc:
                print(f"  REFUSED: {exc}")
                seen = digest
                if args.once:
                    return 1
                time.sleep(args.poll)
                continue
            print(f"  pushed {digest} over {pusher.located}: "
                  f"{changed:,} VRAM byte(s) in {elapsed:.2f}s")
            seen = digest
        if args.once:
            return 0
        time.sleep(args.poll)


if __name__ == "__main__":
    raise SystemExit(main())
