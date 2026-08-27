"""The two animation chunks: `0x6c`'s instruction table and `0x70`'s frames.

`0x70` was declared in `mapfile` and never read (#624). Decoding it turned out
to need `0x6c` as well, because the frames alone do not say **which** CLUT rows
they drive — that is `0x6c`'s job.

Every claim below was rooted statically in the corpus and then **validated on a
live Gariland battle** (2026-08-27), which is the only reason the field
semantics are asserted rather than guessed:

- `0x6c` is 32 records of 20 bytes. `MAP022.9`'s first three read
  `(x=208, y=480, w=16, h=1)`, `(224, …)`, `(240, …)` — a VRAM rectangle, and
  `x/16` is CLUT rows **13, 14, 15**.
- Those are exactly the rows measured animating live, and no others.
- `0x70` is 16 frames of 16 BGR555 words. The live rows cycle frames **0→1→2→3**
  and frame 3 is byte-identical to frame 1, which is why it *looks* like a
  ping-pong and is really a plain forward loop.
- Record byte 14 is `0x04` = the frame count, and byte 17 is `0x0c` = 12, which
  matches the measured ~0.213 s per step (~12.8 fields at 60 Hz).

The engine side is one function (`ra = 0x80092794`): `0x8009269C` writes each
animated entry into `live_link.CLUT_BLOCK` and `0x800926AC` writes it into the
map's loaded `0x44` block, in the same loop body. Both confirmed by watchpoint.
"""

from __future__ import annotations

import pytest

from exmateria_map import corpus, mapfile

MAP_DIR = corpus.map_dir()
pytestmark = pytest.mark.skipif(
    MAP_DIR is None,
    reason="corpus absent; set EXMATERIA_ASSETS_DIR to the extracted disc tree",
)


@pytest.fixture(scope="module")
def map022_9() -> bytes:
    return (MAP_DIR / "MAP022.9").read_bytes()


# --- 0x70: the frames -------------------------------------------------------

def test_the_frame_chunk_is_sixteen_palettes_of_sixteen_colours(map022_9):
    """Same shape as the `0x44` palette chunk, and the same 512 bytes — which
    is what made "a second palette bank" the first (wrong) guess. It is not a
    bank: three of its rows are all zero, and the rows it does fill are FRAMES
    of one animation rather than palettes of one map state."""
    frames = mapfile.read_palette_animation(map022_9)
    assert frames is not None
    assert len(frames) == 16
    assert all(len(f) == 16 for f in frames)


def test_frames_one_and_three_are_identical_which_is_the_yo_yo(map022_9):
    """Measured live: the cycle reads 0, 1, 2, 1, 0, 1, 2, 1 … and that is NOT
    a ping-pong playback mode. It is a plain forward loop over four frames
    whose third entry repeats the second — the yo-yo is in the DATA. Asserting
    it here is what stops a reader inventing a mode the engine does not have."""
    frames = mapfile.read_palette_animation(map022_9)
    assert frames[1] == frames[3]
    assert frames[0] != frames[1] != frames[2]


def test_a_resource_with_no_animation_chunk_reads_none():
    """`0x70` is populated on 84 of the corpus's resources and absent on the
    rest, so `None` is the common answer and must not be an empty list — the
    two mean different things to a writer."""
    assert mapfile.read_palette_animation(b"\x00" * mapfile.HEADER_BYTES) is None


# --- 0x6c: the instruction table -------------------------------------------

def test_the_instruction_table_is_thirty_two_records_of_twenty_bytes(map022_9):
    records = mapfile.read_animation_instructions(map022_9)
    assert records is not None
    assert len(records) == 32


def test_map022_names_clut_rows_13_14_15_and_nothing_else(map022_9):
    """The load-bearing one. These are the rows measured animating on the live
    battle, and the table is where that set comes from — so a decode that named
    any other row would be refuted by the emulator."""
    rows = [r.clut_row for r in mapfile.read_animation_instructions(map022_9)
            if r.is_palette]
    assert rows == [13, 14, 15]


def test_the_palette_records_carry_the_measured_frame_count_and_duration(map022_9):
    """Four frames at 12 ticks. Live: a full cycle took ~0.85 s and a step
    ~0.213 s, which is 4 x 12 fields at 60 Hz to within the sampling jitter."""
    pal = [r for r in mapfile.read_animation_instructions(map022_9) if r.is_palette]
    assert [r.frame_count for r in pal] == [4, 4, 4]
    assert [r.duration for r in pal] == [12, 12, 12]


def test_a_palette_record_is_a_sixteen_by_one_rectangle_on_the_clut_line(map022_9):
    """What `is_palette` actually tests, spelled out. The CLUT block sits at
    y=480 and a row is 16 entries wide and 1 tall; `x/16` is the row index, and
    it agrees with `live_clut - palette_id == 0x7800` derived independently
    from the live packets."""
    pal = [r for r in mapfile.read_animation_instructions(map022_9) if r.is_palette]
    assert [(r.x, r.y, r.width, r.height) for r in pal] == [
        (208, 480, 16, 1), (224, 480, 16, 1), (240, 480, 16, 1)]


# --- the corpus arm ---------------------------------------------------------

#: The corpus's single misaligned palette record. Named rather than tolerated
#: silently, so that a SECOND one appearing is a test failure and not a shrug.
KNOWN_MISALIGNED = {"MAP056.48"}


def _palette_records():
    for path in sorted(MAP_DIR.glob("MAP*.*")):
        if path.suffix == ".GNS":
            continue
        data = path.read_bytes()
        for r in mapfile.read_animation_instructions(data) or ():
            if r.is_palette:
                yield path.name, data, r


def test_every_palette_record_in_the_corpus_lands_on_a_real_clut_row():
    """A shape asserted on one map is a shape asserted on one map. If `x/16`
    were the wrong reading, the corpus would hold rows outside 0..15 — and it
    holds all sixteen and nothing else, across 128 records."""
    rows, misaligned, seen = set(), set(), 0
    for name, _data, r in _palette_records():
        seen += 1
        if r.is_row_aligned:
            assert 0 <= r.clut_row <= 15, f"{name}: row {r.clut_row}"
            rows.add(r.clut_row)
        else:
            misaligned.add(name)
    assert seen >= 100, f"only {seen} palette records found; the glob is wrong"
    assert rows == set(range(16))
    assert misaligned == KNOWN_MISALIGNED, misaligned


def test_the_one_misaligned_record_is_otherwise_an_ordinary_palette_record():
    """`MAP056.48` reads `x = 85` where the other 127 read a multiple of 16 —
    `0x55` against `0x50`, and row 5 is the single most common value in the
    table. Every OTHER field of it is ordinary (4 frames, mode 3), which is
    what makes "retail typo" a better reading than "a shape this decode has
    not understood"."""
    recs = [r for n, _d, r in _palette_records() if n == "MAP056.48"]
    assert [r.x for r in recs] == [85]
    assert recs[0].frame_count == 4 and recs[0].mode == 3
    assert recs[0].clut_row is None


def test_the_frames_may_live_on_a_DIFFERENT_resource_of_the_same_map():
    """The two chunks are one feature but not one resource.

    `MAP053.19` and `MAP061.10` each declare a palette animation with a **null**
    `0x70` pointer, and their frames sit on `MAP053.8` / `MAP061.8`. That is not
    a contradiction and not a decode error — it is the same sharing that
    `palettes` and `light_rig` already do across a state group, and a writer
    that assumed instruction and frames travel together would refuse two
    perfectly ordinary maps.

    The invariant that DOES hold: the frames exist somewhere in the map.
    """
    orphans, shared = [], []
    for path in sorted(MAP_DIR.glob("MAP*.GNS")):
        stem = path.stem
        sibs = [q for q in sorted(MAP_DIR.glob(f"{stem}.*")) if q.suffix != ".GNS"]
        blobs = [q.read_bytes() for q in sibs]
        wants = [(q.name, d) for q, d in zip(sibs, blobs)
                 if any(r.is_palette and r.frame_count
                        for r in mapfile.read_animation_instructions(d) or ())]
        if not wants:
            continue
        anywhere = any(mapfile.palette_animation_offset(d) is not None
                       for d in blobs)
        for name, data in wants:
            if mapfile.palette_animation_offset(data) is None:
                (shared if anywhere else orphans).append(name)
    assert not orphans, f"palette animation with frames nowhere in the map: {orphans}"
    assert set(shared) == {"MAP053.19", "MAP061.10"}, shared
