"""The sheet pusher's AIM (`tools/live_push.py`, decision 9's fixes 3 and 4).

`live_push.py` is the VRAM leg: it round-trips a savestate to put an authored
texture sheet into a running battle. What is asserted here is not the swap
arithmetic -- `vram_swap_sheet` owns that and self-checks it -- but the two
things decision 9 named as broken:

- **fix 3, the locate anchor.** The rig anchored on the *aimed* state's disc
  blob and located VRAM by matching it, so any cross-state aim refused: the
  battle is sitting in weather 0 and you aim at weather 1, and weather 1's
  blob is not in VRAM to be found. *Locate by what is there, write what you
  aim at.*
- **fix 4, the aim itself.** `--night` / `--weather` were a CLI pair resolved
  by a private loop; they are decision 9's key, so they resolve through
  `live_link.aim` and the announcement names the aim and everyone who moves
  with it.

The savestates here are synthetic: a buffer with a sheet laid out at the real
page stride and row pitch. That is enough to exercise every branch of the
locate, and it is the only way to exercise a cross-state aim at all -- Gariland
boots into one state and the rig cannot make it boot into another.
"""

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "tools"), str(ROOT / "addons" / "exmateria_map")):
    if p not in sys.path:
        sys.path.insert(0, p)

import live_push as LP                      # noqa: E402
import vram_swap_sheet as V                 # noqa: E402
from exmateria_map.png_indexed import pack_4bpp, write_indexed_png   # noqa: E402


# --- fixtures ---------------------------------------------------------------

def _bytes(seed: str, n: int) -> bytes:
    """Deterministic high-entropy filler.

    Entropy is not decoration: `vram_swap_sheet._distinctive` skips any row
    with fewer than 8 distinct values, and `_scan` only votes on rows that
    match the buffer exactly ONCE. A fixture of zeros would locate nothing and
    every one of these tests would pass or fail for the wrong reason.
    """
    out = bytearray()
    block = hashlib.sha256(seed.encode()).digest()
    while len(out) < n:
        out += block
        block = hashlib.sha256(block).digest()
    return bytes(out[:n])


def _sidecar(path: Path, seed: str) -> bytes:
    """Write a 256x1024 indexed PNG and return the 4bpp blob it packs to."""
    indices = bytes(b & 0xF for b in _bytes(seed, 256 * 1024))
    path.write_bytes(write_indexed_png(indices, [(i * 16, 0, 0) for i in range(16)]))
    return pack_4bpp(indices)


def _savestate(sheet: bytes, origin: int = 4096) -> bytearray:
    """A buffer holding `sheet` where the emulator's VRAM would hold it."""
    span = origin + (V.PAGES - 1) * V.PAGE_STRIDE + V.ROWS * V.PITCH
    dec = bytearray(span + V.PITCH)
    V.write(dec, origin, sheet)
    return dec


DISC = {"MAP022.8": _bytes("disc-8", V.SHEET_BYTES),
        "MAP022.12": _bytes("disc-12", V.SHEET_BYTES),
        "MAP022.16": _bytes("disc-16", V.SHEET_BYTES)}


def _document() -> dict:
    """MAP022 a0's first three groups, shaped as `dump` writes them.

    Two rows per `(night, weather)`: a TEXTURE row carrying the sheet and a
    mesh row carrying the rig. Weathers 1 and 2 name ONE sidecar between them
    -- that is the disc's own arrangement, and it is what makes the report's
    "who else moved" line necessary rather than decorative.
    """
    def texture(res, night, weather, sheet):
        return {"resource": res, "kind": 23, "night": night, "weather": weather,
                "palettes": None, "texture_sheet": sheet, "light_rig": None}

    def mesh(res, night, weather, kind, palettes=None):
        return {"resource": res, "kind": kind, "night": night, "weather": weather,
                "palettes": palettes, "texture_sheet": None,
                "light_rig": {"colors": [[1, 2, 3]] * 3,
                              "directions": [[4, 5, 6]] * 3,
                              "ambient": [7, 8, 9],
                              "gradient": [0] * 6}}

    return {"map_states": [
        texture("MAP022.8", 0, 0, "MAP022.a0.sheet-aaaa0000.png"),
        mesh("MAP022.9", 0, 0, 46, palettes=["<16 cluts>"]),
        texture("MAP022.12", 0, 1, "MAP022.a0.sheet-bbbb1111.png"),
        mesh("MAP022.13", 0, 1, 48),
        texture("MAP022.16", 0, 2, "MAP022.a0.sheet-bbbb1111.png"),
        mesh("MAP022.17", 0, 2, 48),
        mesh("MAP022.47", 1, 0, 48),          # a night group with NO sheet
    ]}


@pytest.fixture()
def rig(tmp_path):
    """A watch directory, a map directory, and the blobs both hold."""
    watch, map_dir = tmp_path / "watch", tmp_path / "maps"
    watch.mkdir(), map_dir.mkdir()
    document = _document()
    (watch / "MAP022.a0.json").write_text(json.dumps(document))
    sheets = {name: _sidecar(watch / name, name) for name in
              ("MAP022.a0.sheet-aaaa0000.png", "MAP022.a0.sheet-bbbb1111.png")}
    for res, blob in DISC.items():
        (map_dir / res).write_bytes(blob)
    for res in ("MAP022.9", "MAP022.13", "MAP022.17", "MAP022.47"):
        (map_dir / res).write_bytes(_bytes(res, 2048))     # a mesh row, not a sheet
    return {"watch": watch, "map_dir": map_dir, "document": document,
            "sheets": sheets}


class FakeEmulator:
    """`PCSX.createSaveState` / `PCSX.loadSaveState` over a bytearray.

    The real rig cannot exercise a cross-state aim: Gariland boots into one
    state, and a push that refuses is indistinguishable from a push that had
    nothing to do. Here VRAM is a variable, so "the battle is in weather 0 and
    the artist aims at weather 1" is a fixture rather than a wish.
    """

    def __init__(self, vram: bytearray):
        self.vram = vram
        self.saves = 0
        self.loads = 0

    def ping(self):
        return True

    def exec_lua(self, code, timeout=None):
        if "createSaveState" in code:
            self.saves += 1
            Path(LP.STATE_RAW).write_bytes(bytes(self.vram))
        elif "loadSaveState" in code:
            self.loads += 1
            self.vram = bytearray(Path(LP.PATCHED_RAW).read_bytes())
        return "ok"


@pytest.fixture()
def tmp_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(LP, "STATE_RAW", str(tmp_path / "state.raw"))
    monkeypatch.setattr(LP, "PATCHED_RAW", str(tmp_path / "patched.raw"))
    monkeypatch.setattr(LP, "LIVE_CACHE", tmp_path / "live_sheet.bin")


# --- fix 4: the flags are the AIM -------------------------------------------

def test_the_night_weather_pair_resolves_through_the_shared_aim(rig):
    """Decision 9's key is `(night, weather, kind)` and `live_link.aim` is what
    resolves it. The pusher had a private loop that found a TEXTURE row and
    nothing else, so it could not name the rig row, the palette row, or the
    kind it had landed on -- the three things the report is supposed to say."""
    at = LP.aim_at(rig["document"], night=0, weather=1)
    assert (at.night, at.weather) == (0, 1)
    assert at.sheet_row["resource"] == "MAP022.12"
    assert at.rig_row["resource"] == "MAP022.13"
    assert at.palette_row is None          # MAP022 a0's weathers 1-4 carry none


def test_a_group_the_document_does_not_carry_is_refused_by_name(rig):
    with pytest.raises(SystemExit) as exc:
        LP.aim_at(rig["document"], night=0, weather=7)
    assert "night=0" in str(exc.value) and "weather=7" in str(exc.value)


def test_a_group_with_no_texture_row_is_refused_by_name(rig):
    """71 of the corpus's 774 groups carry no TEXTURE row. The aim still
    resolves -- it has a rig -- so the refusal belongs to the SHEET, and it has
    to say which group it is refusing or the artist reads it as "the rig is
    broken"."""
    at = LP.aim_at(rig["document"], night=1, weather=0)
    assert at.sheet_row is None
    with pytest.raises(SystemExit) as exc:
        LP.sidecar_for(rig["watch"], at)
    assert "night=1" in str(exc.value) and "weather=0" in str(exc.value)


def test_the_announcement_names_the_aim_and_who_moves_with_it(rig):
    """Decision 27's rule carried to the push: every state the act touched is
    NAMED. MAP022 a0's weathers 1 and 2 share one sidecar, so "pushed weather
    1" is a third of the truth and the artist who is not told finds out by
    looking at the wrong state later."""
    at = LP.aim_at(rig["document"], night=0, weather=1)
    said = LP.describe(rig["document"], at)
    assert "night=0" in said and "weather=1" in said
    assert "kind 23" in said or "kind=23" in said
    assert "(0, 2)" in said or "weather 2" in said


# --- fix 3: locate by what is THERE -----------------------------------------

def test_the_anchor_set_carries_every_sheet_that_could_be_in_vram(rig):
    """Fix 3. The rig anchored on the aimed state's disc blob alone, so a
    cross-state aim had nothing in VRAM to match and refused. What could be in
    VRAM is any of the map's TEXTURE rows, plus anything this rig has already
    pushed -- so all of them are anchors, and only the write is aimed."""
    at = LP.aim_at(rig["document"], night=0, weather=1)
    anchors = LP.anchors(rig["document"], rig["map_dir"], rig["watch"], at)
    assert DISC["MAP022.8"] in anchors.values()      # the UN-aimed weather 0
    assert DISC["MAP022.12"] in anchors.values()
    assert rig["sheets"]["MAP022.a0.sheet-bbbb1111.png"] in anchors.values()
    assert all(len(b) == V.SHEET_BYTES for b in anchors.values())


def test_the_anchor_set_skips_a_mesh_row(rig):
    """`MAP022.9` is 2,048 bytes of palettes and rig. A short blob in the
    anchor set is not a near miss -- `Pusher` would compare it against a
    131,072-byte rectangle and read past the end of what it means."""
    at = LP.aim_at(rig["document"], night=0, weather=0)
    anchors = LP.anchors(rig["document"], rig["map_dir"], rig["watch"], at)
    assert not any(name.endswith("MAP022.9") for name in anchors)


def test_a_cross_state_aim_finds_the_sheet_the_battle_is_actually_showing(rig, tmp_paths):
    """The whole of fix 3, in one assertion: the battle is in weather 0, the
    artist aims at weather 1, and the push locates weather 0's blob and NAMES
    it. Before this it raised `SwapError` -- "cannot find a known sheet in
    VRAM" -- which reads as an emulator fault rather than an aim."""
    at = LP.aim_at(rig["document"], night=0, weather=1)
    anchors = LP.anchors(rig["document"], rig["map_dir"], rig["watch"], at)
    state = _savestate(DISC["MAP022.8"])
    pusher = LP.Pusher(FakeEmulator(state), anchors)
    origin, who = pusher.find(state)
    assert V.diff(state, origin, DISC["MAP022.8"]) == 0
    assert "MAP022.8" in who


def test_the_push_writes_the_aimed_sheet_over_whatever_it_located(rig, tmp_paths):
    """*Locate by what is there, write what you aim at.* The two halves take
    different rows, and a push that wrote back what it located would be the
    identity round trip -- exact, and blind."""
    at = LP.aim_at(rig["document"], night=0, weather=1)
    anchors = LP.anchors(rig["document"], rig["map_dir"], rig["watch"], at)
    aimed = rig["sheets"]["MAP022.a0.sheet-bbbb1111.png"]
    emulator = FakeEmulator(_savestate(DISC["MAP022.8"]))
    pusher = LP.Pusher(emulator, anchors)
    changed, _elapsed = pusher.push(aimed)
    assert changed > 0
    assert emulator.loads == 1
    origin, _who = pusher.find(emulator.vram)
    assert V.diff(emulator.vram, origin, aimed) == 0


def test_the_second_push_finds_what_the_first_one_wrote(rig, tmp_paths):
    """An authored sheet may have no distinctive row at all (a flat checker is
    two byte values), so after the first push the rig can only find its own
    work by remembering it. That is what the hint and the live cache are for,
    and it is the branch that strands the artist when it breaks."""
    at = LP.aim_at(rig["document"], night=0, weather=1)
    anchors = LP.anchors(rig["document"], rig["map_dir"], rig["watch"], at)
    first = rig["sheets"]["MAP022.a0.sheet-bbbb1111.png"]
    second = bytes(b ^ 0x11 for b in first)
    emulator = FakeEmulator(_savestate(DISC["MAP022.8"]))
    pusher = LP.Pusher(emulator, anchors)
    pusher.push(first)
    pusher.push(second)
    origin, _who = pusher.find(emulator.vram)
    assert V.diff(emulator.vram, origin, second) == 0
