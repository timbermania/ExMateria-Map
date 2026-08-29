"""Proof that ``dump`` carries the base map's terrain grid (ADR-0187).

``base.terrain_tiles`` is derived, information-bearing payload: ``dump``
computes it, ``build`` ignores it (schema §4, the same standing as
``base.floor_steps``). It exists so the addon can *draw* the grid without
declaring a single record -- decision 22's ``"terrain": None`` is untouched.

**The oracle is independent of the code under test.** The 4,098-byte payload is
located by scanning the resource for the window whose sha256 matches
``base.terrain_digest`` -- never by calling ``mapfile.terrain_offset``, which is
the helper ``dump`` itself uses. The slot formula
``2 + level*2048 + (z*size_x + x)*8`` is transcribed from GaneshaDx
(``MeshResourceData.ProcessTerrain``, whose ``_currentByteIndex += 2048 -
width*length*8`` at each level's end is what makes level 1 start at a fixed
2,048-byte stride rather than packed).
"""

from __future__ import annotations

import hashlib

import pytest

from exmateria_map import corpus
from exmateria_map.dump import dump

MAP_DIR = corpus.map_dir()
pytestmark = pytest.mark.skipif(
    MAP_DIR is None,
    reason="corpus absent; set EXMATERIA_ASSETS_DIR to the extracted disc tree",
)

BASE_MAP = 1
BASE_ARRANGEMENT = 0

TERRAIN_CHUNK_BYTES = 4098
LEVEL_STRIDE = 2048
RECORD_BYTES = 8


@pytest.fixture(scope="module")
def document():
    doc, _sheets = dump(MAP_DIR, BASE_MAP, BASE_ARRANGEMENT)
    return doc


@pytest.fixture(scope="module")
def payload(document):
    """The base's 0x68 payload, found by DIGEST -- not by the pointer helper."""
    base = document["base"]
    data = (MAP_DIR / base["terrain_source"]).read_bytes()
    hits = [i for i in range(len(data) - TERRAIN_CHUNK_BYTES + 1)
            if hashlib.sha256(
                data[i:i + TERRAIN_CHUNK_BYTES]).hexdigest() == base["terrain_digest"]]
    assert len(hits) == 1, f"{len(hits)} windows match base.terrain_digest"
    return data[hits[0]:hits[0] + TERRAIN_CHUNK_BYTES]


def slot(payload, x, z, level, size_x):
    o = 2 + level * LEVEL_STRIDE + (z * size_x + x) * RECORD_BYTES
    return list(payload[o:o + RECORD_BYTES])


# --------------------------------------------------------------------------
# the oracle itself has to be able to fail
# --------------------------------------------------------------------------

def test_the_pinned_slots_are_what_the_disc_holds(document, payload):
    """MAP001 a0, read by hand. A test whose oracle drifted with the code
    would agree with any implementation; these literals came off the disc."""
    grid = document["base"]["terrain_grid"]
    assert (grid["size_x"], grid["size_z"]) == (10, 13)
    sx = grid["size_x"]
    assert slot(payload, 0, 0, 0, sx) == [3, 0, 2, 0, 0, 0, 32, 0]
    assert slot(payload, 9, 12, 0, sx) == [21, 0, 12, 2, 37, 0, 20, 0]
    assert slot(payload, 0, 0, 1, sx) == [0, 0, 0, 0, 0, 0, 1, 0]


# --------------------------------------------------------------------------
# decision 1 -- level 0
# --------------------------------------------------------------------------

def test_level_0_carries_every_slot_in_slot_order(document, payload):
    grid = document["base"]["terrain_grid"]
    sx, sz = grid["size_x"], grid["size_z"]
    tiles = document["base"]["terrain_tiles"]
    level0 = [t for t in tiles if t[2] == 0]
    assert [(t[0], t[1]) for t in level0] == [(x, z) for z in range(sz)
                                              for x in range(sx)]
    for t in level0:
        assert t[3:] == slot(payload, t[0], t[1], 0, sx), \
            f"tile ({t[0]}, {t[1]}) L0 does not carry the disc's bytes"


# --------------------------------------------------------------------------
# decision 1 -- level 1, at the 2,048-byte stride
# --------------------------------------------------------------------------

def test_the_two_level_1_readings_disagree_on_this_map(document, payload):
    """The seed that makes the check below able to fail.

    Level 1 begins at a fixed 2,048-byte stride, not packed after level 0's
    ``size_x*size_z`` records. On a 10x13 grid the two readings are 1,006 bytes
    apart, so if they happened to agree the next test would pass against the
    wrong formula."""
    grid = document["base"]["terrain_grid"]
    sx, sz = grid["size_x"], grid["size_z"]
    packed_start = 2 + sx * sz * RECORD_BYTES
    padded = [slot(payload, x, z, 1, sx) for z in range(sz) for x in range(sx)]
    packed = [list(payload[packed_start + i * RECORD_BYTES:
                           packed_start + (i + 1) * RECORD_BYTES])
              for i in range(sx * sz)]
    assert padded != packed


def test_level_1_carries_every_slot_at_the_2048_stride(document, payload):
    grid = document["base"]["terrain_grid"]
    sx, sz = grid["size_x"], grid["size_z"]
    tiles = document["base"]["terrain_tiles"]
    level1 = [t for t in tiles if t[2] == 1]
    assert [(t[0], t[1]) for t in level1] == [(x, z) for z in range(sz)
                                              for x in range(sx)]
    for t in level1:
        assert t[3:] == slot(payload, t[0], t[1], 1, sx), \
            f"tile ({t[0]}, {t[1]}) L1 does not carry the disc's bytes"


def test_both_levels_are_carried_level_0_first(document):
    grid = document["base"]["terrain_grid"]
    n = grid["size_x"] * grid["size_z"]
    tiles = document["base"]["terrain_tiles"]
    assert len(tiles) == 2 * n
    assert [t[2] for t in tiles] == [0] * n + [1] * n


# --------------------------------------------------------------------------
# the arrangements with no grid, and the line decision 22 keeps
# --------------------------------------------------------------------------

def test_an_arrangement_with_no_terrain_chunk_carries_no_tiles():
    """``MAP011`` a2 and ``MAP051`` a1 carry no valid 0x68 (ADR-0187's
    Consequences). Empty list, the same shape ``floor_steps`` takes -- so the
    addon's read is a loop over nothing, not a ``None`` check."""
    doc, _sheets = dump(MAP_DIR, 11, 2)
    assert doc["base"]["terrain_source"] is None
    assert doc["base"]["terrain_grid"] is None
    assert doc["base"]["terrain_tiles"] == []
    assert doc["base"]["floor_steps"] == []


def test_carrying_the_grid_declares_no_record(document):
    """Decision 22 is untouched: the bytes are carried, and the document still
    declares nothing. ``base.terrain_tiles`` is the *base's*, and a document
    that named 130 records would have ``build`` refuse them one at a time."""
    assert document["terrain"] is None


# --------------------------------------------------------------------------
# seam 2 -- `build` ignores it (schema §4)
# --------------------------------------------------------------------------

def test_build_ignores_terrain_tiles(tmp_path_factory):
    """Derived means ``build`` never reads it. Damage every carried byte and
    the bundle must not move -- and the unmutated build has to succeed first,
    or the check would pass against a ``build`` that refuses everything."""
    import copy
    import shutil

    from exmateria_map.build import build

    scratch = tmp_path_factory.mktemp("map")
    for path in sorted(MAP_DIR.glob(f"MAP{BASE_MAP:03d}.*")):
        shutil.copy2(path, scratch / path.name)
    doc, _sheets = dump(scratch, BASE_MAP, BASE_ARRANGEMENT)
    clean = build(doc, scratch)
    assert clean.resources

    damaged = copy.deepcopy(doc)
    damaged["base"]["terrain_tiles"] = [
        [t[0], t[1], t[2]] + [(b ^ 0xFF) for b in t[3:]]
        for t in damaged["base"]["terrain_tiles"]]
    assert damaged["base"]["terrain_tiles"] != doc["base"]["terrain_tiles"]
    assert build(damaged, scratch).resources == clean.resources

    dropped = copy.deepcopy(doc)
    del dropped["base"]["terrain_tiles"]
    assert build(dropped, scratch).resources == clean.resources
