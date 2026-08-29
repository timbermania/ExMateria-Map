"""Decision 11: a swap ERASES the host map's animation table, and installs the
new map's palette half.

The reported symptom was *"one chunk of map got the wrong palette and it's
animated"* after a `Replace the loaded map`. The push had landed -- `CLUT_BLOCK`
rows 0-12 were the pushed map's, 0 of 416 bytes off. What was repainting the
wall 4.49 times a second was the **replaced** map's `0x6c` instruction table,
still running in RAM. So the unit of the fix is the table, not the palettes.

Everything asserted here has a source outside the code under test:

- **the corpus** (`project-assets/fft-extract/MAP/`, 110 resources carry a
  `0x6c`) for what a table looks like and what the set of them contains;
- **`reference-assets/thief_whats_this.sstate`**, a real Gariland battle, for
  what the LIVE table holds at `0x80121D7C` -- which is how the address, the
  four runtime bytes and the content guard are graded offline rather than
  against a running emulator.

The one thing this file cannot grade is the behavioural readback (rows moving
over a dwell), because a savestate is one instant. `tests/blender_live_push.py`
drives it against the fake emulator; a live battle is what proved it.
"""

import json
import sys
from pathlib import Path

import pytest

ADDON = Path(__file__).resolve().parent.parent / "addons" / "exmateria_map"
sys.path.insert(0, str(ADDON))

import live_link as L  # noqa: E402

from exmateria_map import corpus, mapfile  # noqa: E402

MAP_DIR = corpus.map_dir()
needs_corpus = pytest.mark.skipif(
    MAP_DIR is None or not MAP_DIR.exists(),
    reason="corpus absent; set EXMATERIA_ASSETS_DIR to the extracted disc tree")


@pytest.fixture(scope="module")
def map022_9() -> bytes:
    return (MAP_DIR / "MAP022.9").read_bytes()


@pytest.fixture(scope="module")
def map002_9() -> bytes:
    return (MAP_DIR / "MAP002.9").read_bytes()


# --- what a table NAMES (the readback's expected side) ----------------------

@needs_corpus
def test_the_host_maps_table_names_the_three_rows_measured_animating(map022_9):
    """Gariland animates CLUT rows 13, 14 and 15 at 4.49 steps a second, and
    no others -- measured live, by sampling `CLUT_BLOCK` at 20 Hz. The table is
    where the push has to get that set from, because on a swap the emulator is
    the only other witness and it is the thing being corrected."""
    recs = mapfile.read_animation_instructions(map022_9)
    assert L.animation_rows(recs) == [13, 14, 15]


@needs_corpus
def test_the_pushed_map_names_NO_rows_and_that_is_not_an_error(map002_9):
    """`MAP002.9` carries no `0x6c` and no `0x70` at all -- so on the reported
    pair a swap can only ever LOSE an animation, never gain one, and the
    expected readback is *nothing moves*. A `None` table has to reduce to the
    empty set rather than raise, or the easiest case on the whole leg (install
    nothing, prove nothing moves) would be the one that refuses."""
    assert mapfile.read_animation_instructions(map002_9) is None
    assert L.animation_rows(None) == []


# --- the savestate: a real battle's RAM, offline -----------------------------

SAVESTATE = (Path(__file__).resolve().parent.parent.parent
             / "reference-assets" / "thief_whats_this.sstate")

needs_savestate = pytest.mark.skipif(
    not SAVESTATE.exists(),
    reason="reference-assets/thief_whats_this.sstate absent")

FIXTURE = (Path(__file__).resolve().parent / "fixtures"
           / "map022_a0_descriptors.hex")


@pytest.fixture(scope="module")
def gariland_ram() -> bytes:
    """Main RAM out of the Gariland savestate, located by VERIFYING: the
    descriptor fixture is 1,368 bytes of that same battle, so the one place it
    occurs pins where `0x800FBE00` landed in the file. Same technique as
    `test_live_link.py`; repeated rather than shared because a fixture that
    crosses files is a fixture nobody reads before trusting."""
    descriptors = bytes.fromhex(FIXTURE.read_text().replace("\n", ""))
    blob = SAVESTATE.read_bytes()
    at = blob.find(descriptors)
    assert at >= 0, "the descriptor block is not in this savestate"
    assert blob.find(descriptors, at + 1) < 0, "two candidate RAM offsets"
    base = at - (L.DESCRIPTOR_BASE - L.RAM_BASE)
    return blob[base:base + L.RAM_BYTES]


def _at(ram: bytes, address: int, length: int) -> bytes:
    return ram[address - L.RAM_BASE:address - L.RAM_BASE + length]


# --- which rows MOVED (the readback's measured side) -------------------------

def _clut_with(block: bytes, frame: list[int], rows) -> bytes:
    """`block` with `frame`'s sixteen BGR555 words painted into `rows`."""
    out = bytearray(block)
    packed = b"".join(int(w).to_bytes(2, "little") for w in frame)
    for row in rows:
        out[row * L.CLUT_ROW_BYTES:(row + 1) * L.CLUT_ROW_BYTES] = packed
    return bytes(out)


@needs_corpus
@needs_savestate
def test_the_rows_that_move_between_two_real_frames_are_the_ones_named(
        gariland_ram, map022_9):
    """The readback's measured side, driven by the disc's own frames over the
    battle's own CLUT block -- neither side computed by the code under test.

    `MAP022.9`'s animation steps frame 0 -> 1 on rows 13, 14 and 15 together.
    Sampling the block before and after one step has to report exactly those
    three, which is the same equality a live 0.6 s dwell measured at 4.49
    steps a second.
    """
    block = _at(gariland_ram, L.CLUT_BLOCK, L.CLUT_BLOCK_BYTES)
    frames = mapfile.read_palette_animation(map022_9)
    rows = L.animation_rows(mapfile.read_animation_instructions(map022_9))
    before = _clut_with(block, frames[0], rows)
    after = _clut_with(block, frames[1], rows)
    assert L.moved_clut_rows(before, after) == [13, 14, 15]


@needs_savestate
def test_a_block_that_did_not_change_reports_no_rows(gariland_ram):
    """The whole of the expected answer on a map with an empty table, so it
    cannot be a special case: two identical samples name nothing."""
    block = _at(gariland_ram, L.CLUT_BLOCK, L.CLUT_BLOCK_BYTES)
    assert L.moved_clut_rows(block, block) == []


# --- the readback CONTRACT, phrased as the goal ------------------------------
# Decision 10's amendment moved the goal line to the artist's own words: *"when
# you replace the map, the goal is the total removal of the old map, and adding
# the new map."* So the readback is a set equality, and each direction of a
# failure is a different sentence about that goal.

def test_the_rows_that_moved_matching_the_rows_named_is_the_whole_check():
    ok, lines = L.check_animation_readback(moved=[13, 14, 15],
                                           expected=[13, 14, 15])
    assert ok
    assert any("13, 14, 15" in line for line in lines)


def test_a_host_row_still_moving_is_the_OLD_map_not_removed():
    """The reported bug, exactly: the push landed and row 13 kept cycling. The
    verdict has to name that row and say which half of the goal it failed, or
    the artist reads a generic mismatch and cannot tell an erase that did not
    happen from an install that did not."""
    ok, lines = L.check_animation_readback(moved=[13, 14, 15], expected=[])
    assert not ok
    said = " ".join(lines).lower()
    assert "13, 14, 15" in said
    assert "not fully removed" in said


def test_a_pushed_row_not_moving_is_the_NEW_map_not_added():
    ok, lines = L.check_animation_readback(moved=[], expected=[13, 14, 15])
    assert not ok
    said = " ".join(lines).lower()
    assert "13, 14, 15" in said
    assert "not added" in said


def test_both_halves_can_fail_at_once_and_both_are_reported():
    """A swap between two animating maps fails both ways in one press, and a
    report that stopped at the first would send the next session looking for
    one bug where there are two."""
    ok, lines = L.check_animation_readback(moved=[13], expected=[5])
    assert not ok
    said = " ".join(lines).lower()
    assert "not fully removed" in said and "not added" in said
    assert len(lines) == 2


def test_nothing_moving_when_nothing_is_named_PASSES(map002_9=None):
    """The `MAP002` case, which is the easiest possible first arm and must not
    be a special case in the code: an empty table expects an empty set, and an
    empty set is what a fully erased host produces."""
    ok, lines = L.check_animation_readback(moved=[], expected=[])
    assert ok
    assert any("nothing" in line for line in lines)


# --- two samples are not enough, and the corpus says why ---------------------

@needs_corpus
@needs_savestate
def test_two_samples_can_land_on_two_IDENTICAL_frames_of_a_running_row(
        gariland_ram, map022_9):
    """`MAP022.9`'s frame 3 is byte-identical to frame 1 -- that is the "yo-yo"
    that is really a plain forward loop over four frames (#624). So a pair of
    samples that happens to straddle two steps reads rows 13/14/15 as *not
    moving* while they never stopped, and the readback would report *the new
    map not added* about a perfectly healthy install.

    A dwell is wall-clock over HTTP against an emulator whose speed is not
    ours, so which pair of frames a two-sample readback lands on is not a
    thing this code gets to choose. Sampling ACROSS the dwell instead of only
    at its ends is what removes the coincidence.
    """
    block = _at(gariland_ram, L.CLUT_BLOCK, L.CLUT_BLOCK_BYTES)
    frames = mapfile.read_palette_animation(map022_9)
    rows = L.animation_rows(mapfile.read_animation_instructions(map022_9))
    assert frames[1] == frames[3], "the premise: the cycle repeats a frame"

    ends = [_clut_with(block, frames[1], rows), _clut_with(block, frames[3], rows)]
    assert L.moved_clut_rows(*ends) == [], "the trap this test exists for"

    across = [_clut_with(block, f, rows) for f in frames[:4]]
    assert L.moved_clut_rows(*across) == [13, 14, 15]


def test_one_sample_cannot_report_movement_and_says_so():
    """A readback built from a single sample would report *nothing moved* on
    every push, for every map -- a clean bill nothing measured."""
    block = bytes(L.CLUT_BLOCK_BYTES)
    with pytest.raises(L.LiveLinkError):
        L.moved_clut_rows(block)


# --- the dwell: how long the readback has to watch ---------------------------

@needs_corpus
def test_the_dwell_is_one_step_of_the_slowest_row_this_map_animates(map022_9):
    """`max(duration)/60`. Gariland's three palette records are 12 ticks each,
    which is the ~0.213 s per step measured live at 4.49 steps a second."""
    recs = mapfile.read_animation_instructions(map022_9)
    assert L.animation_dwell(recs) == pytest.approx(12 / 60)


@needs_corpus
def test_a_map_that_animates_nothing_has_no_dwell(map002_9):
    """`MAP002.9` carries no table, so there is nothing to wait for and the
    readback still runs -- two samples, expecting no movement."""
    assert L.animation_dwell(mapfile.read_animation_instructions(map002_9)) == 0.0


@needs_corpus
def test_the_dwell_ignores_the_TEXTURE_records_because_they_run_to_four_seconds():
    """The corpus's slowest record is 240 ticks -- 4.00 s -- and it is a
    texture record. That is not time to spend inside a button press, which is
    why the texture half of decision 11 is byte-confirmed and reported in
    different words. Every PALETTE record in the corpus is <= 30 ticks, so the
    behavioural dwell is <= 0.5 s on every map there is."""
    slowest_palette = slowest_any = 0
    for path in sorted(MAP_DIR.glob("MAP*.*")):
        if path.suffix == ".GNS":
            continue
        recs = mapfile.read_animation_instructions(path.read_bytes())
        if recs is None:
            continue
        for r in recs:
            if not any(r.raw):
                continue
            slowest_any = max(slowest_any, r.duration)
            if r.is_palette:
                slowest_palette = max(slowest_palette, r.duration)
        assert L.animation_dwell(recs) <= 0.6, path.name
    assert slowest_palette == 30 and slowest_any == 240


@needs_corpus
def test_the_two_duration_zero_palette_records_name_no_row_either(map022_9):
    """#654 is open -- `duration = 0` is undecoded on 2 palette and 93 texture
    records. On the palette side it turns out not to reach this leg at all:
    both records (`MAP053.8`, `MAP053.22`) carry `frame_count = 0` as well, so
    they animate nothing, name no row, and are never dwelled on. The floor
    below is what stands behind that, not what the corpus needs today."""
    found = []
    for path in sorted(MAP_DIR.glob("MAP*.*")):
        if path.suffix == ".GNS":
            continue
        recs = mapfile.read_animation_instructions(path.read_bytes())
        for r in recs or ():
            if any(r.raw) and r.is_palette and r.duration == 0:
                found.append((path.name, r.frame_count))
    assert found == [("MAP053.22", 0), ("MAP053.8", 0)]


def test_a_row_that_animates_with_duration_zero_gets_the_floor():
    """The assumption, made in the open: nothing in the corpus reaches it, so
    a foreign or authored table is what would, and a dwell computed from `0`
    is `0` -- a readback that watches for no time at all and reports *not
    added* about every row. The floor is the corpus's own slowest palette
    step, so an undecoded duration is watched for at least as long as the
    slowest animation anyone has measured."""
    recs = [_record(x=13 * 16, frame_count=4, duration=0)]
    assert L.animation_dwell(recs) == pytest.approx(L.ANIM_DWELL_FLOOR_TICKS / 60)
    assert L.ANIM_DWELL_FLOOR_TICKS == 30


def _record(*, x, y=mapfile.CLUT_VRAM_Y, width=16, height=1,
            frame_count=4, mode=3, duration=12, index=0):
    """One `0x6c` record, packed the way the disc packs it and read back by the
    shipped reader -- so a fixture cannot disagree with the decode."""
    raw = bytearray(20)
    for off, val in ((0, x), (2, y), (4, width), (6, height)):
        raw[off:off + 2] = int(val).to_bytes(2, "little")
    raw[14], raw[15], raw[17] = frame_count, mode, duration
    return mapfile.read_animation_instructions(
        _resource_with(bytes(raw) * 32, None))[index]


# --- part 2: the guard is on the CONTENT, not on the address -----------------
# The address was confirmed on ONE battle. Writing 640 bytes there on any other
# is a bet, and the bet is not the artist's to lose -- so the live table is
# compared against every `0x6c` chunk in the corpus and the erase is refused
# unless it matches a map's.

@needs_savestate
@needs_corpus
def test_the_live_table_is_the_loaded_maps_own_table(gariland_ram, map022_9):
    """The address, proved offline. `0x80121D7C` in a real Gariland battle
    holds `MAP022.9`'s `0x6c` chunk -- and only bytes 14, 16, 18 and 19 of its
    three RUNNING palette records differ, which is what "runtime state" means
    here. Every other byte of all 640 is the disc's."""
    live = _at(gariland_ram, L.ANIM_TABLE, L.ANIM_TABLE_BYTES)
    off = mapfile.animation_instruction_offset(map022_9)
    disc = map022_9[off:off + L.ANIM_TABLE_BYTES]
    assert live != disc, "if these were equal the mask below would prove nothing"
    differ = {(i // mapfile.ANIM_INSTRUCTION_STRIDE,
               i % mapfile.ANIM_INSTRUCTION_STRIDE)
              for i in range(L.ANIM_TABLE_BYTES) if live[i] != disc[i]}
    assert {rec for rec, _byte in differ} == {0, 1, 2}, "the three palette records"
    assert {byte for _rec, byte in differ} == set(L.ANIM_RUNTIME_BYTES)


@needs_savestate
@needs_corpus
def test_the_mask_makes_the_running_table_match_the_disc_exactly(
        gariland_ram, map022_9):
    live = _at(gariland_ram, L.ANIM_TABLE, L.ANIM_TABLE_BYTES)
    off = mapfile.animation_instruction_offset(map022_9)
    disc = map022_9[off:off + L.ANIM_TABLE_BYTES]
    assert L.mask_animation_runtime(live) == L.mask_animation_runtime(disc)


@needs_savestate
def test_the_DECOY_at_0x800F6DC4_shares_the_leading_eight_bytes(gariland_ram):
    """A second structure sits nearby on a 24-byte stride and repeats each
    record's `(x, y, w, h)`. Inspection cannot separate it from the real table
    -- a one-byte poke did -- so what stands between a future reader and the
    wrong address is the content guard, not a comment. Here is the reason it
    is needed, and the reason it works."""
    real = _at(gariland_ram, L.ANIM_TABLE, L.ANIM_TABLE_BYTES)
    decoy = _at(gariland_ram, 0x800F6DC4, L.ANIM_TABLE_BYTES)
    assert real[:8] == decoy[:8], "the trap: they agree where a reader looks"
    assert real[:24] != decoy[:24]


@needs_savestate
@needs_corpus
def test_the_guard_names_the_six_states_the_live_table_matches(
        gariland_ram):
    """Measured: the live table matches exactly 6 corpus resources and all six
    are `MAP022` states. The animation is per map STATE -- `MAP022.9/.31/.37/
    .43/.49/.55` each carry rows 13/14/15 and `.13/.17/.21/.25` carry none --
    so six is the right kind of answer and one would have been suspicious."""
    live = _at(gariland_ram, L.ANIM_TABLE, L.ANIM_TABLE_BYTES)
    matched = L.check_animation_table(live, L.animation_tables(MAP_DIR))
    assert matched == ["MAP022.31", "MAP022.37", "MAP022.43",
                       "MAP022.49", "MAP022.55", "MAP022.9"]


@needs_corpus
def test_a_match_is_informative_because_the_tables_are_mostly_DISTINCT():
    """A guard that matched everything would pass on anything. 110 corpus
    resources carry a `0x6c`; among them there are 83 distinct tables and the
    largest identical group is 6. So matching narrows the loaded map to a
    handful of states, and matching NOTHING is a real signal."""
    tables = L.animation_tables(MAP_DIR)
    assert len(tables) == 110
    on_disc, under_mask = {}, {}
    for name, table in tables.items():
        on_disc.setdefault(table, []).append(name)
        under_mask.setdefault(L.mask_animation_runtime(table), []).append(name)
    assert len(on_disc) == 83
    assert max(len(g) for g in on_disc.values()) == 6
    # The mask costs exactly one distinction, and it costs it INSIDE one map:
    # `MAP061.9` and `MAP061.10` differ only at byte 14 of record 0, which is
    # the frame count on the disc and a frame cursor in a running engine (the
    # savestate reads `0x81` where the disc reads `0x04`). `.10` is one of the
    # two resources whose frames live on a sibling, so the two states really
    # are near-identical tables. The guard says "some map's table", never
    # "this map's", so collapsing two states of one map costs it nothing --
    # but it is measured here rather than assumed away.
    assert len(under_mask) == 82
    collapsed = [sorted(g) for g in under_mask.values()
                 if len({tables[n] for n in g}) > 1]
    assert collapsed == [["MAP061.10", "MAP061.9"]]


@needs_savestate
def test_bytes_that_are_no_maps_table_are_REFUSED_rather_than_erased(
        gariland_ram):
    """The case that cannot be ruled out: if anything other than a map ever
    writes records there, the guard matches nothing and the push stops instead
    of writing 640 bytes of zeros over it. Graded with the DECOY, which is
    real engine data at a real address rather than invented noise."""
    decoy = _at(gariland_ram, 0x800F6DC4, L.ANIM_TABLE_BYTES)
    with pytest.raises(L.LiveLinkError) as e:
        L.check_animation_table(decoy, L.animation_tables(MAP_DIR))
    assert "110" in str(e.value)


@needs_corpus
def test_an_empty_candidate_set_cannot_confirm_anything_and_refuses(
        gariland_ram=None):
    """Decision 11 as amended: no corpus, no erase. The guard's whole purpose
    is to keep 640 bytes off an unverified address, and a candidate set of
    nothing verifies nothing -- so "matched 0 of 0" must not read as a pass."""
    with pytest.raises(L.LiveLinkError) as e:
        L.check_animation_table(bytes(L.ANIM_TABLE_BYTES), {})
    assert "extracted disc tree" in str(e.value)


# --- part 1: the erase -------------------------------------------------------

def test_erasing_writes_the_whole_table_as_zeros():
    """One write, 640 bytes, at the guarded address."""
    assert L.plan_erase_animation() == [(L.ANIM_TABLE, bytes(640))]


@needs_corpus
def test_zero_is_the_CORPUS_OWN_encoding_for_no_animation(map022_9):
    """This is why the erase writes what it writes rather than disabling a
    feature. 21 of `MAP022.9`'s 32 slots ship all-zero from the disc and the
    engine walks all 32 every frame, so a zeroed record is a shape the engine
    was already being handed -- not an off switch invented here."""
    recs = mapfile.read_animation_instructions(map022_9)
    assert sum(1 for r in recs if not any(r.raw)) == 21
    assert L.animation_rows([r for r in recs if not any(r.raw)]) == []


@needs_corpus
def test_the_erase_takes_the_TEXTURE_records_too_and_that_is_the_scope(map022_9):
    """The ask was "handle animated palettes" and the scope is the table.
    Gariland's own table is 3 palette records and 8 TEXTURE records, and those
    eight point at `x = 839..923, y = 28..128` -- inside the four VRAM pages a
    swap has just uploaded a foreign sheet to. Left running they would copy
    rectangles around inside the new sheet and be reported next, in words that
    sound unrelated to this bug."""
    recs = [r for r in mapfile.read_animation_instructions(map022_9) if any(r.raw)]
    palette = [r for r in recs if r.is_palette]
    texture = [r for r in recs if not r.is_palette]
    assert len(palette) == 3 and len(texture) == 8
    assert min(r.x for r in texture) == 839
    assert max(r.x + r.width for r in texture) == 928
    erased = L.plan_erase_animation()[0][1]
    assert set(erased) == {0}, "every record, not the palette ones"


# --- parts 3 and 4: install the palette half, refuse the rest ----------------

@needs_corpus
def test_installing_a_maps_animation_writes_its_records_and_its_frames(map022_9):
    """The records go back into their OWN slots -- a record's index is part of
    its identity, which is why `read_animation_instructions` keeps the empty
    ones -- and the frames go to the loaded `0x70` block."""
    recs = mapfile.read_animation_instructions(map022_9)
    frames = mapfile.read_palette_animation(map022_9)
    writes, notes = L.plan_install_animation(recs, frames)

    stride = mapfile.ANIM_INSTRUCTION_STRIDE
    assert [a for a, _ in writes] == [L.ANIM_TABLE + i * stride for i in (0, 1, 2)] \
        + [L.ANIM_FRAMES]
    assert [len(d) for _, d in writes] == [stride] * 3 + [L.ANIM_FRAMES_BYTES]
    for i, (_a, data) in enumerate(writes[:3]):
        # Every byte the map declares, verbatim. The one exception is the run
        # flag the LOADER owns -- see the arming test below.
        assert data[:L.ANIM_RUN_FLAG_BYTE] == recs[i].raw[:L.ANIM_RUN_FLAG_BYTE]


@needs_corpus
def test_the_installed_table_names_the_rows_the_readback_will_expect(map022_9):
    """The install and the readback have to agree by construction, or the leg
    grades itself. Rebuild the table from the writes and read it back with the
    same function the readback's expected side uses."""
    recs = mapfile.read_animation_instructions(map022_9)
    frames = mapfile.read_palette_animation(map022_9)
    writes, _notes = L.plan_install_animation(recs, frames)

    table = bytearray(L.ANIM_TABLE_BYTES)
    for address, data in writes:
        if address == L.ANIM_FRAMES:
            continue
        table[address - L.ANIM_TABLE:address - L.ANIM_TABLE + len(data)] = data
    assert L.animation_rows(L.read_animation_table(bytes(table))) == [13, 14, 15]


@needs_corpus
def test_the_texture_records_are_erased_and_NOT_installed(map022_9):
    """Part 4. A palette record needs no translation -- the CLUT line is
    `y = 480` on every map. A texture record is absolute VRAM against its own
    map's sheet base, and that base is assigned by the LOADER: it is in neither
    the document nor the base resource. Rebasing by the dominant value would be
    right for most and silently wrong for the rest, which is the failure
    `live_vram.derive_addresses` exists to prevent."""
    recs = mapfile.read_animation_instructions(map022_9)
    frames = mapfile.read_palette_animation(map022_9)
    writes, notes = L.plan_install_animation(recs, frames)
    assert len(writes) == 4, "3 palette records + the frames, and no more"
    said = " ".join(notes)
    assert "8" in said and "#653" in said


@needs_corpus
def test_the_texture_records_the_corpus_holds_are_worth_refusing_over():
    """Not a hypothetical. 439 + 80 + 18 is how the corpus's 577 texture
    records split across VRAM bands: 479 sit at `x >= 768`, 80 at `x = 0` and
    18 elsewhere. Rebasing by the dominant band would put 98 records in the
    wrong place with nothing to say which."""
    bands = {"x>=768": 0, "x==0": 0, "other": 0}
    for path in sorted(MAP_DIR.glob("MAP*.*")):
        if path.suffix == ".GNS":
            continue
        for r in mapfile.read_animation_instructions(path.read_bytes()) or ():
            if not any(r.raw) or r.is_palette:
                continue
            bands["x>=768" if r.x >= 768 else
                  "x==0" if r.x == 0 else "other"] += 1
    assert bands == {"x>=768": 479, "x==0": 80, "other": 18}
    assert sum(bands.values()) == 577


@needs_corpus
def test_a_map_with_no_animation_installs_nothing_and_does_not_refuse(map002_9):
    """The easiest possible first arm, and the reported push's own: `MAP002.9`
    carries neither chunk, so the whole install is *nothing*, and the readback
    that grades it is *nothing moves*."""
    writes, notes = L.plan_install_animation(
        mapfile.read_animation_instructions(map002_9),
        mapfile.read_palette_animation(map002_9))
    assert writes == []
    assert any("no animation" in n for n in notes)


def test_a_record_naming_a_rectangle_OUTSIDE_vram_is_refused_not_written():
    """`is_palette` does not screen for this -- it asks where a record points,
    not whether the place exists -- so the screen is its own step."""
    good = _record(x=13 * 16)
    bad = _record(x=61440)
    assert bad.is_palette, "the trap: it passes the palette test"
    writes, notes = L.plan_install_animation([good, bad], [[0] * 16] * 16)
    assert [a for a, _ in writes] == [L.ANIM_TABLE, L.ANIM_FRAMES]
    assert any("61440" in n for n in notes)


@needs_corpus
def test_the_out_of_vram_records_are_absent_records_the_corpus_really_holds():
    """~40 records name an `x` of 3,840 / 61,440 / 61,680 and 24 name a `y` of
    3,840 / 4,080 / 61,440 / 65,520. These are *absent* records, never corrupt
    files -- schema §10.3's terrain rule applied here.

    Two of those six values are recorded one nibble low in decision 11's own
    text (`61,632` for `61,680`; `4,032` / `65,472` for `4,080` / `65,520`).
    Read here with the shipped decoder, against the whole corpus."""
    xs, ys, outside = set(), set(), 0
    for path in sorted(MAP_DIR.glob("MAP*.*")):
        if path.suffix == ".GNS":
            continue
        for r in mapfile.read_animation_instructions(path.read_bytes()) or ():
            if not any(r.raw):
                continue
            if r.x >= 1024:
                xs.add(r.x)
            if r.y >= 512:
                ys.add(r.y)
            if (r.x + r.width > 1024) or (r.y + r.height > 512):
                outside += 1
    assert sorted(xs) == [3840, 61440, 61680]
    assert sorted(ys) == [3840, 4080, 61440, 65520]
    assert outside == 84


def test_records_with_no_frames_to_read_are_a_refusal():
    """An installed record with no frames behind it points the engine at the
    HOST map's `0x70` block -- the replaced map's colours, cycling on the new
    map's rows. That is the reported bug with an extra step, so it refuses."""
    with pytest.raises(L.LiveLinkError) as e:
        L.plan_install_animation([_record(x=13 * 16)], None)
    assert "0x70" in str(e.value)


# --- part 3's source: the BASE resource, pinned by the document --------------
# The interchange document carries neither chunk (schema §8 puts both on the
# *carried from base* side), so the animation is read off the extracted disc
# tree. What makes that read verifiable rather than hopeful is the document's
# own per-resource `sha256`.

@pytest.fixture(scope="module")
def map022_a0_document():
    from exmateria_map import dump as _dump
    return _dump.dump(MAP_DIR, 22, 0)[0]


@needs_corpus
def test_the_animation_is_read_from_the_states_own_base_resource(
        map022_a0_document, map022_9):
    """The animation is per map STATE, not per arrangement -- `MAP022.9/.31/
    .37/.43/.49/.55` each carry rows 13/14/15 while `.13/.17/.21/.25` carry
    none -- so decision 9's existing aim already picks the right resource and
    no new aiming rule is needed."""
    recs, frames, source = L.base_animation(
        MAP_DIR, map022_a0_document, "MAP022.9")
    assert L.animation_rows(recs) == [13, 14, 15]
    assert frames == mapfile.read_palette_animation(map022_9)
    assert source == "MAP022.9"


@needs_corpus
def test_a_state_of_the_same_arrangement_that_animates_NOTHING(
        map022_a0_document):
    """`MAP022.13` is the same map and the same document, one weather row
    over, and it carries no table at all. If the aim were per arrangement this
    would read Gariland's rows on a state that has none."""
    recs, frames, _source = L.base_animation(
        MAP_DIR, map022_a0_document, "MAP022.13")
    assert recs is None and frames is None
    assert L.animation_rows(recs) == []


@needs_corpus
def test_a_base_resource_whose_bytes_are_not_the_pinned_ones_is_REFUSED(
        map022_a0_document):
    """The pin is the whole reason this read is allowed to happen. A tree that
    is not the document's own is a tree whose `0x6c` records mean something
    else, and installing those would animate rows this map never named."""
    doc = json.loads(json.dumps(map022_a0_document))
    for entry in doc["base"]["resources"]:
        if entry["name"] == "MAP022.9":
            entry["sha256"] = "00" * 32
    with pytest.raises(L.LiveLinkError) as e:
        L.base_animation(MAP_DIR, doc, "MAP022.9")
    assert "sha256" in str(e.value) and "MAP022.9" in str(e.value)


@needs_corpus
def test_a_resource_the_document_does_not_pin_is_refused(map022_a0_document):
    with pytest.raises(L.LiveLinkError) as e:
        L.base_animation(MAP_DIR, map022_a0_document, "MAP999.1")
    assert "MAP999.1" in str(e.value)


@needs_corpus
def test_no_disc_tree_costs_the_INSTALL_and_says_so(map022_a0_document):
    """Decision 11's degradation rule for the half it still holds for: the
    install needs the base resource, so a missing tree costs it. The erase is
    a separate act with a separate guard."""
    with pytest.raises(L.LiveLinkError) as e:
        L.base_animation(None, map022_a0_document, "MAP022.9")
    assert "extracted disc tree" in str(e.value)


@needs_corpus
def test_the_frames_may_live_on_a_SIBLING_resource_and_are_still_found():
    """`MAP053.19` declares a palette animation with a NULL `0x70` pointer and
    its frames sit on `MAP053.8` -- the same sharing `palettes` and
    `light_rig` already do across a state group. A reader that assumed the two
    chunks travel together would refuse two perfectly ordinary maps.

    `MAP053` a1's `base.resources` is `['MAP053.19']` alone, so the sibling is
    **not pinned by this document**, and the provenance says which resource
    the frames came from rather than reporting them in the same words as a
    pinned read."""
    from exmateria_map import dump as _dump
    doc = _dump.dump(MAP_DIR, 53, 1)[0]
    assert [r["name"] for r in doc["base"]["resources"]] == ["MAP053.19"]
    recs, frames, source = L.base_animation(MAP_DIR, doc, "MAP053.19")
    assert mapfile.palette_animation_offset(
        (MAP_DIR / "MAP053.19").read_bytes()) is None, "the premise"
    assert frames == mapfile.read_palette_animation(
        (MAP_DIR / "MAP053.8").read_bytes())
    assert "MAP053.8" in source and "MAP053.19" in source


# --- the readback against a client: a HELD image cannot answer it ------------

class _AnimatingHttp:
    """The endpoint as a byte array whose CLUT rows step one frame per GET --
    a running animation, without an emulator."""

    def __init__(self, block: bytes, frames, rows):
        self.ram = bytearray(L.RAM_BYTES)
        self.block, self.frames, self.rows = block, frames, rows
        self.gets = self.posts = 0
        self._paint(0)

    def _paint(self, frame):
        painted = _clut_with(self.block, self.frames[frame % len(self.frames)],
                             self.rows)
        o = L.CLUT_BLOCK - L.RAM_BASE
        self.ram[o:o + L.CLUT_BLOCK_BYTES] = painted

    def get(self):
        self._paint(self.gets)
        self.gets += 1
        return bytes(self.ram)

    def post(self, offset, data):
        self.posts += 1
        self.ram[offset:offset + len(data)] = data


def _client(http):
    c = L.RamClient()
    c._get, c._post = http.get, http.post
    return c


@needs_savestate
@needs_corpus
def test_the_readback_samples_the_CONSOLE_not_the_pushs_held_image(
        gariland_ram, map022_9):
    """The trap this arm exists for. `RamClient.hold()` fetches main RAM once
    for the length of a push and answers every `read` from that one image --
    which is correct for planning and for the self-check, and fatal here: N
    samples of one instant are N identical blocks, so every row reads still and
    a healthy install is reported as *the new map not added*.

    A readback is the one thing in a push that must ask the console again.
    """
    block = _at(gariland_ram, L.CLUT_BLOCK, L.CLUT_BLOCK_BYTES)
    frames = mapfile.read_palette_animation(map022_9)
    rows = L.animation_rows(mapfile.read_animation_instructions(map022_9))
    http = _AnimatingHttp(block, frames[:3], rows)
    client = _client(http)

    with client.hold():
        assert client.read(L.CLUT_BLOCK, 32) == client.read(L.CLUT_BLOCK, 32)
        ok, lines = L.readback_animation(client, expected=rows, dwell=0.0,
                                         sleep=lambda _s: None)
    assert ok, lines


@needs_savestate
@needs_corpus
def test_the_readback_reports_a_host_row_that_the_erase_missed(
        gariland_ram, map022_9):
    """The reported bug, end to end against a client: the pushed map names no
    animated row, the console is still cycling three, and the verdict is *the
    old map not removed* naming 13, 14 and 15."""
    block = _at(gariland_ram, L.CLUT_BLOCK, L.CLUT_BLOCK_BYTES)
    frames = mapfile.read_palette_animation(map022_9)
    http = _AnimatingHttp(block, frames[:3], [13, 14, 15])
    ok, lines = L.readback_animation(_client(http), expected=[], dwell=0.0,
                                     sleep=lambda _s: None)
    assert not ok
    assert "13, 14, 15" in " ".join(lines)


@needs_savestate
def test_the_readback_spends_the_dwell_across_its_samples(gariland_ram):
    """The dwell is watched THROUGH, not waited out and then peeked at: a pair
    at the ends can land on two identical frames of a cycle that repeats one.
    Five samples over `dwell` means four waits of a quarter each."""
    block = _at(gariland_ram, L.CLUT_BLOCK, L.CLUT_BLOCK_BYTES)
    http = _AnimatingHttp(block, [[0] * 16], [])
    slept = []
    ok, _lines = L.readback_animation(_client(http), expected=[], dwell=0.8,
                                      sleep=slept.append)
    assert ok
    assert http.gets == L.ANIM_READBACK_SAMPLES
    assert slept == [0.8 / (L.ANIM_READBACK_SAMPLES - 1)] * (
        L.ANIM_READBACK_SAMPLES - 1)


# --- part 5: on `Push to PCSX` the animation is REPORTED, never frozen -------
# The rule is one line: **neutralise foreign animation; never neutralise a
# map's own.** On the edit path the animation belongs to the document's own
# map, `build` will carry `0x6c`/`0x70` to the disc verbatim, and freezing it
# would show the artist a picture the shipped map can never produce -- the
# loupe lying in exactly the way the shared palette packing exists to prevent.

@needs_corpus
def test_the_edit_path_names_the_rows_this_map_animates(map022_9):
    recs = mapfile.read_animation_instructions(map022_9)
    lines = L.animation_report(recs, "MAP022.9")
    said = " ".join(lines)
    assert "13, 14, 15" in said
    assert "MAP022.9" in said


@needs_corpus
def test_the_edit_path_says_the_battle_REPAINTS_those_rows(map022_9):
    """Not a warning that something failed -- an explanation of what the artist
    is looking at. The colours ARE in the document and they ARE on the disc;
    what the artist sees on those three rows is the engine's own cycle over
    them, and a push that quietly stopped it would preview a map the disc can
    never produce."""
    recs = mapfile.read_animation_instructions(map022_9)
    said = " ".join(L.animation_report(recs, "MAP022.9")).lower()
    assert "repaint" in said
    assert "0.2" in said or "0.21" in said, "the cycle's own step, from byte 17"


@needs_corpus
def test_the_edit_path_still_says_something_when_nothing_animates(map002_9):
    """Decision 4: name what has no sink on every push, rather than being
    silent on the maps where it happens not to matter."""
    lines = L.animation_report(
        mapfile.read_animation_instructions(map002_9), "MAP002.9")
    assert lines and "no animated" in " ".join(lines).lower()


# --- the texture half is BYTE-confirmed, and says so in different words ------
# Decision 10's rule: *a weaker check reported in the same words as the strong
# one is worse than no check.* The palette half is graded behaviourally, by
# rows moving. The texture half cannot be -- the corpus's slowest record is
# 240 ticks, 4.00 s, which is not time to spend inside a button press -- so it
# is read back as bytes and reported as bytes.

@needs_corpus
def test_the_texture_records_are_confirmed_GONE_by_reading_the_table_back(
        map022_9):
    http = _AnimatingHttp(bytes(L.CLUT_BLOCK_BYTES), [[0] * 16], [])
    client = _client(http)
    L.apply(client, L.plan_erase_animation())
    ok, lines = L.confirm_animation_erased(client)
    assert ok
    said = " ".join(lines).lower()
    assert "byte" in said
    assert "move" not in said, "the strong check's words are not borrowed"


@needs_corpus
def test_a_texture_record_left_running_is_NAMED_not_passed_over(map022_9):
    """Seeded with Gariland's own table: eight texture records at
    `x = 839..923`, inside the pages a swap has just written a sheet to."""
    http = _AnimatingHttp(bytes(L.CLUT_BLOCK_BYTES), [[0] * 16], [])
    off = mapfile.animation_instruction_offset(map022_9)
    o = L.ANIM_TABLE - L.RAM_BASE
    http.ram[o:o + L.ANIM_TABLE_BYTES] = map022_9[off:off + L.ANIM_TABLE_BYTES]
    ok, lines = L.confirm_animation_erased(_client(http))
    assert not ok
    said = " ".join(lines)
    assert "8" in said and "839" in said


@needs_corpus
def test_the_installed_palette_records_are_not_mistaken_for_leftovers(map022_9):
    """The confirmation runs AFTER the install, so the pushed map's own palette
    records are in the table by then. It asks about texture records only --
    they are the half nothing installs, so any survivor is the host's."""
    http = _AnimatingHttp(bytes(L.CLUT_BLOCK_BYTES), [[0] * 16], [])
    client = _client(http)
    L.apply(client, L.plan_erase_animation())
    recs = mapfile.read_animation_instructions(map022_9)
    frames = mapfile.read_palette_animation(map022_9)
    writes, _notes = L.plan_install_animation(recs, frames)
    L.apply(client, writes)
    ok, _lines = L.confirm_animation_erased(client)
    assert ok


# --- the record the LOADER arms, not the one the disc ships -----------------
# Measured [LIVE] 2026-08-28 on a running Gariland battle, self-controlled: the
# table held `MAP022.9`'s three palette records, byte for byte off the disc,
# and rows 13/14/15 were **still**. Writing byte 19 = 1 into record 0 alone
# started row 13 and left 14 and 15 at zero; putting the record back stopped it
# again. So a disc record is INERT until the loader arms it, and an install
# that wrote the chunk verbatim would land a correct-looking, dead animation --
# exactly the case decision 11's behavioural readback exists to catch, and the
# case a byte readback would have called a pass.

@needs_corpus
def test_the_disc_ships_the_run_flag_CLEAR_on_every_palette_record():
    """127 of the corpus's 128 palette records read byte 19 = 0. It is not a
    field a map authors; it is the one the loader sets."""
    values = {}
    for path in sorted(MAP_DIR.glob("MAP*.*")):
        if path.suffix == ".GNS":
            continue
        for r in mapfile.read_animation_instructions(path.read_bytes()) or ():
            if any(r.raw) and r.is_palette:
                values[r.raw[L.ANIM_RUN_FLAG_BYTE]] = \
                    values.get(r.raw[L.ANIM_RUN_FLAG_BYTE], 0) + 1
    assert values[0] == 127 and sum(values.values()) == 128


@needs_corpus
def test_an_installed_record_is_ARMED_and_otherwise_the_discs(map022_9):
    """One byte differs from the chunk `build` will write to the disc, and it
    is the byte the engine owns. Everything a map declares is carried
    verbatim."""
    recs = mapfile.read_animation_instructions(map022_9)
    frames = mapfile.read_palette_animation(map022_9)
    writes, _notes = L.plan_install_animation(recs, frames)
    for (address, data), r in zip(writes, [x for x in recs if x.is_palette]):
        assert address == L.ANIM_TABLE + r.index * 20
        assert data[L.ANIM_RUN_FLAG_BYTE] == L.ANIM_RUN_FLAG
        assert data[:L.ANIM_RUN_FLAG_BYTE] == r.raw[:L.ANIM_RUN_FLAG_BYTE]


@needs_savestate
def test_the_run_flag_is_SET_in_a_real_battles_table(gariland_ram):
    """The other half of the same measurement, offline: every running palette
    record in the savestate carries it."""
    live = _at(gariland_ram, L.ANIM_TABLE, L.ANIM_TABLE_BYTES)
    running = [r for r in L.read_animation_table(live) if r.is_palette]
    assert running and all(r.raw[L.ANIM_RUN_FLAG_BYTE] == L.ANIM_RUN_FLAG
                           for r in running)


# --- the guard must accept the table THIS LEG leaves behind ------------------

@needs_corpus
def test_a_second_swap_is_not_refused_by_the_first_ones_result(map022_9):
    """Measured [LIVE] 2026-08-28: after one Replace the running table held
    `MAP022.9`'s three palette records and 32 empty slots, and the guard
    matched **none** of the 110 corpus tables -- because the host's eight
    texture records had been erased and no map on the disc ships a table like
    that. So the second press refused, on the machine the leg was built for.

    A live record that is EMPTY where a candidate's is not is what this leg
    itself produces, and what an engine that has finished with a record would
    produce. A live record that is present must still match exactly.
    """
    recs = mapfile.read_animation_instructions(map022_9)
    frames = mapfile.read_palette_animation(map022_9)
    writes, _notes = L.plan_install_animation(recs, frames)
    after = bytearray(L.ANIM_TABLE_BYTES)
    for address, data in writes:
        if address == L.ANIM_FRAMES:
            continue
        after[address - L.ANIM_TABLE:address - L.ANIM_TABLE + len(data)] = data
    matched = L.check_animation_table(bytes(after), L.animation_tables(MAP_DIR))
    assert matched == ["MAP022.31", "MAP022.37", "MAP022.43",
                       "MAP022.49", "MAP022.55", "MAP022.9"]


@needs_corpus
def test_an_empty_slot_is_forgiven_and_a_WRONG_one_is_not(map022_9):
    """The forgiveness is one-directional. A record that is present and
    different is still a refusal -- otherwise the guard would pass on any
    table whose junk happened to sit in slots the candidate leaves empty."""
    off = mapfile.animation_instruction_offset(map022_9)
    disc = map022_9[off:off + L.ANIM_TABLE_BYTES]
    wrong = bytearray(disc)
    wrong[3 * 20] ^= 0xFF                      # one byte of one texture record
    with pytest.raises(L.LiveLinkError):
        L.check_animation_table(bytes(wrong), L.animation_tables(MAP_DIR))


@needs_corpus
def test_an_all_empty_table_matches_NOTHING_rather_than_everything():
    """"Every slot is empty" is compatible with every map in the corpus, so
    treating it as a match would make the guard say yes to 640 bytes of zeros
    at any address. There is also nothing to erase, so the caller does not
    need it to say yes."""
    with pytest.raises(L.LiveLinkError) as e:
        L.check_animation_table(bytes(L.ANIM_TABLE_BYTES),
                                L.animation_tables(MAP_DIR))
    assert "no animation" in str(e.value).lower()


def _resource_with(table: bytes | None, frames: bytes | None) -> bytes:
    """A minimal resource carrying the two animation chunks, so a fixture is
    read back by the SHIPPED decoder rather than hand-decoded here. `None`
    leaves the pointer null, which is what a map with no animation ships."""
    head = bytearray(mapfile.HEADER_BYTES)
    body = bytearray()
    for slot, chunk in ((mapfile.ANIM_INSTRUCTION_PTR, table),
                        (mapfile.PALETTE_ANIM_PTR, frames)):
        if chunk is None:
            continue
        head[slot:slot + 4] = (mapfile.HEADER_BYTES + len(body)).to_bytes(4, "little")
        body += chunk
    return bytes(head + body)
