"""Proof that ``build`` writes the bytes it claims and refuses the rest.

Every check here ships with the defect it catches. A refusal test that only
asserted "this raises" would pass against a ``build`` that refuses everything,
so each one first proves the *unmutated* document builds -- the seed has to
move something, or the check is blind.

The corpus is the oracle for the identity axis (``exmateria-map-roundtrip``,
1,575 files). This file is the axis that instrument cannot reach: what happens
when the document is *wrong*.
"""

from __future__ import annotations

import copy
import hashlib
import shutil
import struct

import pytest

from exmateria_map import build as build_leg
from exmateria_map import corpus, document as schema, mapfile
from exmateria_map.build import BuildRefusal, build, splice
from exmateria_map.dump import dump
from exmateria_map.png_indexed import unpack_4bpp, write_indexed_png

MAP_DIR = corpus.map_dir()
pytestmark = pytest.mark.skipif(
    MAP_DIR is None,
    reason="corpus absent; set EXMATERIA_ASSETS_DIR to the extracted disc tree",
)

BASE_MAP = 1
BASE_ARRANGEMENT = 0


@pytest.fixture(scope="module")
def scratch(tmp_path_factory):
    """A writable copy of one map, so a test may damage the *base* too."""
    out = tmp_path_factory.mktemp("map")
    for path in sorted(MAP_DIR.glob(f"MAP{BASE_MAP:03d}.*")):
        shutil.copy2(path, out / path.name)
    return out


@pytest.fixture(scope="module")
def base_document(scratch):
    doc, _sheets = dump(scratch, BASE_MAP, BASE_ARRANGEMENT)
    return doc


@pytest.fixture
def doc(base_document):
    return copy.deepcopy(base_document)


def built(document, scratch, **kwargs):
    return build(document, scratch, **kwargs)


def identical(bundle, scratch) -> bool:
    return all((scratch / name).read_bytes() == data
               for name, data in bundle.resources.items())


def refuses(document, scratch, *fragments) -> str:
    with pytest.raises(BuildRefusal) as exc:
        build(document, scratch)
    message = str(exc.value)
    for fragment in fragments:
        assert fragment in message, message
    return message


# ---------------------------------------------------------------------------
# the identity -- the seed every other test is measured against
# ---------------------------------------------------------------------------

def test_untouched_document_rebuilds_the_base_byte_for_byte(doc, scratch):
    bundle = built(doc, scratch)
    assert identical(bundle, scratch)
    assert bundle.gns == (scratch / bundle.gns_name).read_bytes()
    assert bundle.resources, "a bundle with no resources proves nothing"


def test_one_changed_polygon_changes_exactly_one_chunk(doc, scratch):
    """The seed that proves the identity above is not a copy."""
    doc["polygons"][0]["positions"][0][0] += 1
    bundle = built(doc, scratch)
    changed = [n for n, data in bundle.resources.items()
               if (scratch / n).read_bytes() != data]
    assert changed, "moving a vertex changed nothing -- build is not writing"
    for name in changed:
        original = (scratch / name).read_bytes()
        mesh = mapfile.read_mesh(original)
        offsets = [i for i in range(len(original))
                   if original[i] != bundle.resources[name][i]]
        assert all(mesh.start <= o < mesh.end for o in offsets), name


# ---------------------------------------------------------------------------
# §6.1 primary mesh -- the counts are derived, never stored
# ---------------------------------------------------------------------------

def test_counts_are_derived_from_the_polygon_list(doc):
    packed = build_leg.pack_primary_mesh(doc["polygons"])
    counts = struct.unpack_from("<HHHH", packed, 0)
    from collections import Counter
    seen = Counter(p["kind"] for p in doc["polygons"])
    assert counts == tuple(seen[k] for k in schema.BUCKETS)

    dropped = [p for p in doc["polygons"] if p["kind"] != schema.BUCKETS[0]]
    assert struct.unpack_from("<HHHH", build_leg.pack_primary_mesh(dropped), 0)[0] == 0


def test_encoder_constants_are_written_not_carried(doc):
    """b3 = 0x78 and b7 = 0x00 are not document fields (§5.2, decision 19)."""
    packed = build_leg.pack_primary_mesh(doc["polygons"])
    tt, tq, ut, uq = struct.unpack_from("<HHHH", packed, 0)
    offset = 8 + (tt * 3 + tq * 4 + ut * 3 + uq * 4) * 6 + (tt * 3 + tq * 4) * 6
    assert packed[offset + 3] == schema.CLUT_WORD_HIGH_BYTE
    assert packed[offset + 7] == schema.PROPERTY_BYTE_7


# ---------------------------------------------------------------------------
# §6.2 visible angles -- the whole table, overlaid
# ---------------------------------------------------------------------------

def test_dead_slots_are_carried_not_synthesised(doc, scratch):
    """A dead slot that is not 0x8000 must survive. 1,110 of MAP001.a0's
    1,600 slots are dead, and the corpus does not fill them uniformly."""
    slots = bytearray.fromhex(doc["carry"]["visible_angles_slots"])
    live = sum(1 for _ in doc["polygons"])
    dead_index = schema.SLOT_CAPACITY[0][1] - 1        # last textured-triangle slot
    struct.pack_into("<H", slots, dead_index * 2, 0x1234)
    doc["carry"]["visible_angles_slots"] = bytes(slots).hex()

    bundle = built(doc, scratch)
    name = doc["base"]["geometry_source"]
    data = bundle.resources[name]
    offset = mapfile.visible_angles_offset(data) + mapfile.VISIBLE_ANGLES_HEADER_BYTES
    assert mapfile.u16(data, offset + dead_index * 2) == 0x1234
    assert live > 0


def test_live_slots_come_from_the_polygons(doc, scratch):
    doc["polygons"][0]["visible_angles"] = 0x4321
    bundle = built(doc, scratch)
    data = bundle.resources[doc["base"]["geometry_source"]]
    offset = mapfile.visible_angles_offset(data) + mapfile.VISIBLE_ANGLES_HEADER_BYTES
    assert mapfile.u16(data, offset) == 0x4321


def test_carry_null_is_refilled_from_the_base(doc, scratch):
    """The addon's export leaves `carry` null; `build` refills it (§11)."""
    doc["carry"] = {"note": "", "visible_angles_unknown_896": None,
                    "visible_angles_slots": None}
    assert identical(built(doc, scratch), scratch)


def test_a_short_slot_table_refuses(doc, scratch):
    doc["carry"]["visible_angles_slots"] = doc["carry"]["visible_angles_slots"][:-4]
    refuses(doc, scratch, "visible_angles_slots", "3198 bytes")


# ---------------------------------------------------------------------------
# §6.4 palettes
# ---------------------------------------------------------------------------

def _palette_state(document):
    return next(s for s in document["map_states"] if s["palettes"])


def test_a_repainted_clut_reaches_the_chunk(doc, scratch):
    state = _palette_state(doc)
    state["palettes"][0]["colors"][0] = "#FF0000"
    bundle = built(doc, scratch)
    data = bundle.resources[state["resource"]]
    word = mapfile.u16(data, mapfile.palette_offset(data))
    assert word & 0x1F == 31 and (word >> 5) & 0x3FF == 0


def test_the_stp_bit_is_live_data(doc, scratch):
    """1,178 STP bits are set corpus-wide; dropping the mask is a real defect."""
    state = _palette_state(doc)
    state["palettes"][0]["stp"] = 0x0001
    bundle = built(doc, scratch)
    data = bundle.resources[state["resource"]]
    assert mapfile.u16(data, mapfile.palette_offset(data)) >> 15 == 1


def test_palettes_declared_for_a_resource_with_no_chunk_refuse(doc, scratch):
    sheet = next(s for s in doc["map_states"] if s["texture_sheet"])
    sheet["palettes"] = _palette_state(doc)["palettes"]
    refuses(doc, scratch, sheet["resource"], "no valid 0x44 chunk")


def test_a_valid_chunk_with_no_palettes_refuses(doc, scratch):
    _palette_state(doc)["palettes"] = None
    refuses(doc, scratch, "valid 0x44 chunk", "no palettes")


# ---------------------------------------------------------------------------
# the splice -- growth moves every later pointer
# ---------------------------------------------------------------------------

def test_splice_with_no_length_change_leaves_the_header_alone():
    data = bytearray(512)
    struct.pack_into("<I", data, mapfile.PRIMARY_PTR, 196)
    struct.pack_into("<I", data, mapfile.TERRAIN_PTR, 300)
    data[196:300] = bytes(range(104))
    out = splice(bytes(data), [(196, 300, bytes(104))])
    assert out[:196] == bytes(data[:196])
    assert out[196:300] == bytes(104)


def test_splice_shifts_every_pointer_past_a_grown_chunk():
    data = bytearray(512)
    struct.pack_into("<I", data, mapfile.PRIMARY_PTR, 196)
    struct.pack_into("<I", data, mapfile.TERRAIN_PTR, 300)
    out = splice(bytes(data), [(196, 300, bytes(120))])
    assert struct.unpack_from("<I", out, mapfile.PRIMARY_PTR)[0] == 196
    assert struct.unpack_from("<I", out, mapfile.TERRAIN_PTR)[0] == 316
    assert len(out) == 528


def test_splice_refuses_a_pointer_inside_a_resized_chunk():
    """A backstop, not a live case: measured over all 169 geometry-carrying
    resources, no section pointer lands inside the 0x40 section. If one ever
    did, `build` could not say where it should move to."""
    data = bytearray(512)
    struct.pack_into("<I", data, mapfile.PRIMARY_PTR, 196)
    struct.pack_into("<I", data, mapfile.TERRAIN_PTR, 250)      # mid-chunk
    with pytest.raises(BuildRefusal, match="inside the rewritten chunk"):
        splice(bytes(data), [(196, 300, bytes(120))])


def test_splice_refuses_overlapping_replacements():
    with pytest.raises(BuildRefusal, match="overlapping"):
        splice(bytes(256), [(0, 100, b""), (50, 120, b"")])


# ---------------------------------------------------------------------------
# §10.1 format and version
# ---------------------------------------------------------------------------

def test_a_higher_version_is_refused_not_guessed_at(doc, scratch):
    # 2 is now ACCEPTED (decision 27: `version` is the oldest `build` that can
    # handle the document, so this one takes every value at or below its own).
    # 3 is the next thing it has never heard of, and the rule is unchanged for it.
    doc["version"] = 3
    refuses(doc, scratch, "version is 3", "refused, never guessed at")


def test_a_v2_document_is_accepted(doc, scratch):
    """The accept half of the same rule -- without it, "refuses 3" is satisfied
    by a `build` that refuses everything above 1, which is the code this
    decision replaces."""
    doc["version"] = 2
    assert identical(built(doc, scratch), scratch)


def test_a_foreign_format_is_refused(doc, scratch):
    doc["format"] = "ganeshadx/mesh"
    refuses(doc, scratch, "format is")


def test_version_is_checked_before_anything_else(doc, scratch):
    """§2: refused "before reading anything else" -- a broken base must not
    change which message comes out."""
    doc["version"] = 99
    doc["base"]["resources"][0]["sha256"] = "0" * 64
    refuses(doc, scratch, "version is 99")


# ---------------------------------------------------------------------------
# §10.2 base identity
# ---------------------------------------------------------------------------

def test_a_wrong_resource_digest_refuses(doc, scratch):
    doc["base"]["resources"][0]["sha256"] = "0" * 64
    refuses(doc, scratch, "not the base map", doc["base"]["resources"][0]["name"])


def test_an_edited_base_file_refuses(doc, scratch, tmp_path):
    """The digest is on the file, so touching the disc is caught too."""
    other = tmp_path / "edited"
    other.mkdir()
    for path in sorted(scratch.glob("MAP*")):
        shutil.copy2(path, other / path.name)
    victim = other / doc["base"]["geometry_source"]
    data = bytearray(victim.read_bytes())
    data[-1] ^= 0xFF
    victim.write_bytes(bytes(data))
    with pytest.raises(BuildRefusal, match="not the base map"):
        build(doc, other)


def test_a_truncated_digest_is_named_as_a_pre_v1_document(doc, scratch):
    """A 16-char digest still PREFIXES the real one. Refusing it as "not the
    base map" sends the artist hunting a disc that is fine."""
    entry = doc["base"]["resources"][0]
    entry["sha256"] = entry["sha256"][:16]
    message = refuses(doc, scratch, "predates schema v1", "base map matches")
    assert "is not the base map" not in message, message


def test_a_wrong_geometry_digest_refuses(doc, scratch):
    """§10.2, and it must be §10.2 that speaks: §10.6's fan-out check compares
    the same two digests, so a test that only asserts "something refused"
    cannot tell the two rules apart and reads green with §10.2 deleted."""
    doc["base"]["geometry_digest"] = "1" * 64
    message = refuses(doc, scratch, "base.geometry_digest is",
                      "not the base map")
    assert "never silently rewrites" not in message, message
    assert message.startswith(doc["base"]["geometry_source"] + ":"), message


def test_a_wrong_terrain_digest_refuses(doc, scratch):
    assert doc["base"]["terrain_digest"], "MAP001.a0 must carry terrain"
    doc["base"]["terrain_digest"] = "2" * 64
    refuses(doc, scratch, "terrain_digest", "4,098-byte payload")


def test_pad_rows_in_map_states_refuse(doc, scratch):
    """§7.1 / #525: type-49 rows are carried whole and never in the document.
    They echo the last real record's LBA, so a duplicated entry names a
    resource that IS in the arrangement -- nothing else here would notice."""
    doc["map_states"].append(copy.deepcopy(doc["map_states"][-1]))
    refuses(doc, scratch, "map_states holds", "pad rows")


def test_a_missing_resource_row_refuses(doc, scratch):
    doc["base"]["resources"].pop()
    refuses(doc, scratch, "base.resources names")


# ---------------------------------------------------------------------------
# §10.3 pointers
# ---------------------------------------------------------------------------

def test_a_pointer_past_eof_refuses(doc, scratch, tmp_path):
    other = tmp_path / "badptr"
    other.mkdir()
    for path in sorted(scratch.glob("MAP*")):
        shutil.copy2(path, other / path.name)
    name = doc["base"]["geometry_source"]
    victim = other / name
    data = bytearray(victim.read_bytes())
    struct.pack_into("<I", data, 0x94, len(data) + 8)          # an unused slot
    victim.write_bytes(bytes(data))
    for entry in doc["base"]["resources"]:
        if entry["name"] == name:
            entry["sha256"] = hashlib.sha256(bytes(data)).hexdigest()
    with pytest.raises(BuildRefusal, match="at or past the"):
        build(doc, other)


# ---------------------------------------------------------------------------
# §10.4 polygon capacity -- ADR-0004 decision 28
#
# The bound under test is the ENGINE's array, not the 0xB0 slot table, and it
# is on the SUM with the base's AnimatedMesh sections. Both halves need their
# own arm: a check that clamped at the slot table would still refuse *something*
# and read green, and a check that ignored the animated meshes would pass every
# document MAP001 can produce, because MAP001.9 carries none.
# ---------------------------------------------------------------------------

ANIM_MAP = 103          # MAP103.a0: MAP103.10 spends 140 tt / 85 tq on
ANIM_ARRANGEMENT = 0    # AnimatedMesh sections, the corpus's largest tt spend


def _refill(document, kind, count):
    """Replace ``kind`` wholesale with ``count`` copies of its first polygon."""
    template = next(p for p in document["polygons"] if p["kind"] == kind)
    document["polygons"] = (
        [p for p in document["polygons"] if p["kind"] != kind]
        + [copy.deepcopy(template) for _ in range(count)])
    return document


@pytest.fixture
def animated(tmp_path):
    """A writable copy of the arrangement whose base spends the most on
    AnimatedMesh sections -- the only arm where the sum can be told from the
    document's own count."""
    out = tmp_path / "animated"
    out.mkdir()
    for path in sorted(MAP_DIR.glob(f"MAP{ANIM_MAP:03d}.*")):
        shutil.copy2(path, out / path.name)
    document, _sheets = dump(out, ANIM_MAP, ANIM_ARRANGEMENT)
    return out, document


def test_the_engine_array_is_the_bound_not_the_slot_table(doc, scratch):
    """360 textured triangles build; 361 refuse. The slot table says 512, so a
    check clamped there would accept every count this test rejects."""
    kind, engine = schema.ENGINE_CAPACITY[0]
    assert engine < dict(schema.SLOT_CAPACITY)[kind]        # or the arm is void
    assert not identical(built(_refill(copy.deepcopy(doc), kind, engine),
                               scratch), scratch)
    message = refuses(_refill(doc, kind, engine + 1), scratch,
                      kind, str(engine), "decision 28")
    assert "512 is not the bound" in message


def test_the_bound_is_the_sum_with_the_base_animated_meshes(animated):
    """MAP103.10 spends 140 tt on its AnimatedMesh sections, so the document's
    own ceiling is 220 -- not 360. A rule that looked only at the document
    would accept 221 here, which is the defect this arm exists to catch."""
    directory, document = animated
    kind, engine = schema.ENGINE_CAPACITY[0]
    spent = mapfile.animated_mesh_counts(
        (directory / document["base"]["geometry_source"]).read_bytes())[0]
    assert spent == 140, spent
    ceiling = engine - spent

    bundle = build(_refill(copy.deepcopy(document), kind, ceiling), directory)
    assert bundle.resources
    message = refuses(_refill(document, kind, ceiling + 1), directory,
                      kind, str(spent), str(engine))
    assert f"is {engine + 1}" in message


def test_above_the_corpus_maximum_warns_and_still_builds(doc, scratch):
    """The untested band: 351..360 textured triangles on a base with no
    animated meshes. Warn, never refuse (decision 9's precedent)."""
    kind, ceiling = schema.CORPUS_MAX[0]
    quiet = built(_refill(copy.deepcopy(doc), kind, ceiling), scratch)
    assert not any("decision 28" in w for w in quiet.warnings), quiet.warnings

    loud = built(_refill(doc, kind, ceiling + 1), scratch)
    assert [w for w in loud.warnings if "decision 28" in w], loud.warnings
    assert f"{kind} {ceiling + 1}" in " ".join(loud.warnings)


def test_corpus_maxima_still_hold():
    """CORPUS_MAX and ENGINE_CAPACITY are pasted numbers; this is what keeps
    them true. Recomputed from the disc as the maximum, per bucket, of
    (primary mesh + AnimatedMesh1-8) over every geometry-carrying resource."""
    measured = dict.fromkeys(schema.BUCKETS, 0)
    seen = 0
    for number in mapfile.map_numbers(MAP_DIR):
        for path in mapfile.bind(MAP_DIR, number).by_sector.values():
            data = path.read_bytes()
            if len(data) == mapfile.TEXTURE_BYTES:
                continue
            mesh = mapfile.read_mesh(data)
            if mesh is None:
                continue
            seen += 1
            anim = mapfile.animated_mesh_counts(data)
            for i, bucket in enumerate(schema.BUCKETS):
                measured[bucket] = max(measured[bucket], mesh.counts[i] + anim[i])
    assert seen == 169, seen
    assert measured == dict(schema.CORPUS_MAX), measured
    for bucket, engine in schema.ENGINE_CAPACITY:
        assert measured[bucket] <= engine, (bucket, measured[bucket], engine)


# ---------------------------------------------------------------------------
# §10.6 fan-out correspondence
# ---------------------------------------------------------------------------

FANOUT_MAP = 4          # MAP004.a0: 9 geometry-carrying rows, 5 with a 0xB0 chunk
FANOUT_ARRANGEMENT = 0


@pytest.fixture
def fanout(tmp_path):
    """A writable copy of the arrangement with the widest fan-out."""
    out = tmp_path / "fanout"
    out.mkdir()
    for path in sorted(MAP_DIR.glob(f"MAP{FANOUT_MAP:03d}.*")):
        shutil.copy2(path, out / path.name)
    document, _sheets = dump(out, FANOUT_MAP, FANOUT_ARRANGEMENT)
    return out, document


def _damage(directory, document, name, offset):
    """Flip a byte in a base resource and re-bless its digest, so the seed
    lands on the rule under test and not on §10.2's identity check."""
    victim = directory / name
    data = bytearray(victim.read_bytes())
    data[offset] ^= 0xFF
    victim.write_bytes(bytes(data))
    for entry in document["base"]["resources"]:
        if entry["name"] == name:
            entry["sha256"] = hashlib.sha256(bytes(data)).hexdigest()


def _mesh_resources(directory, document):
    """The arrangement's geometry-carrying resources -- *excluding* the texture
    sheets. A 131,072-byte sheet has no header, so `read_mesh` reads garbage
    out of it and reports a mesh: filtering on that alone put four texture
    files into this population and made the first version of the seed below
    land on a resource `build` never looks at."""
    return [e["name"] for e in document["base"]["resources"]
            if (directory / e["name"]).stat().st_size != mapfile.TEXTURE_BYTES
            and mapfile.read_mesh((directory / e["name"]).read_bytes())]


def test_the_fanout_fixture_really_fans_out(fanout):
    directory, document = fanout
    names = [e["name"] for e in document["base"]["resources"]]
    meshes = _mesh_resources(directory, document)
    chunks = [n for n in names
              if mapfile.visible_angles_offset((directory / n).read_bytes()) is not None]
    assert len(meshes) > 1 and len(chunks) > 1, (meshes, chunks)
    digests = {mapfile.mesh_digest((directory / n).read_bytes(),
                                   mapfile.read_mesh((directory / n).read_bytes()))
               for n in meshes}
    assert len(digests) == 1, "the corpus holds byte-identical sibling meshes"
    assert identical(build(document, directory), directory)


def test_a_mesh_sibling_that_disagrees_refuses(fanout):
    """Decision 2: `build` never silently rewrites a resource whose base does
    not match the one the document came from."""
    directory, document = fanout
    others = [n for n in _mesh_resources(directory, document)
              if n != document["base"]["geometry_source"]]
    mesh = mapfile.read_mesh((directory / others[0]).read_bytes())
    _damage(directory, document, others[0], mesh.start + 8)
    with pytest.raises(BuildRefusal, match="never silently rewrites"):
        build(document, directory)


def test_a_visible_angle_sibling_that_disagrees_refuses(fanout):
    """§8: all 16 multi-row arrangements carry byte-identical 0xB0 chunks, and
    `build` re-checks it rather than picking one."""
    directory, document = fanout
    source = document["base"]["geometry_source"]
    others = [n for n in (e["name"] for e in document["base"]["resources"])
              if n != source
              and mapfile.visible_angles_offset((directory / n).read_bytes()) is not None]
    offset = mapfile.visible_angles_offset((directory / others[0]).read_bytes())
    _damage(directory, document, others[0], offset + 4)
    with pytest.raises(BuildRefusal, match="0xB0 chunk differs"):
        build(document, directory)


def test_a_second_valid_terrain_chunk_that_disagrees_refuses(doc, scratch, tmp_path):
    """§7.3: one distinct valid chunk per arrangement. MAP001.a0 carries two,
    which is what makes this seed land on the rule and not on an empty set."""
    names = [e["name"] for e in doc["base"]["resources"]]
    holders = [n for n in names
               if mapfile.terrain_offset((scratch / n).read_bytes()) is not None]
    assert len(holders) > 1, holders
    other = tmp_path / "terrfan"
    other.mkdir()
    for path in sorted(scratch.glob("MAP*")):
        shutil.copy2(path, other / path.name)
    victim = next(n for n in holders if n != doc["base"]["terrain_source"])
    offset = mapfile.terrain_offset((other / victim).read_bytes())
    _damage(other, doc, victim, offset + 40)
    with pytest.raises(BuildRefusal, match="one distinct"):
        build(doc, other)


# ---------------------------------------------------------------------------
# §6.2 / #524 -- a base with no 0xB0 chunk, and the ONE manufacture
# ---------------------------------------------------------------------------

NO_SLOTS_MAP = 116      # MAP116.a0's geometry source carries no 0xB0 chunk
#: MAP083 a0 names decision 26's scope from the other side: its geometry
#: source MAP083.9 HAS a chunk, and its chunkless MAP083.10 is a sibling
#: `build` never writes a 0xB0 to. That is why the affected set is nine
#: arrangements and not ten.
CHUNKED_SOURCE_MAP = 83
#: MAP099 a0 is the only one of the nine with non-texture siblings the
#: manufacture could reach: nine 753-byte state resources with no `0x40` at
#: all. `build` writes the chunk to the geometry source and to nothing else,
#: and this is the arrangement where that is a claim about a non-empty set --
#: on the other eight the source is the arrangement's only non-texture row, so
#: a test there cannot tell scoped from unscoped.
SIBLINGS_MAP = 99


@pytest.fixture
def slotless(tmp_path):
    out = tmp_path / "slotless"
    out.mkdir()
    for path in sorted(MAP_DIR.glob(f"MAP{NO_SLOTS_MAP:03d}.*")):
        shutil.copy2(path, out / path.name)
    document, _sheets = dump(out, NO_SLOTS_MAP, 0)
    return out, document


def _source(bundle, document) -> bytes:
    return bundle.resources[document["base"]["geometry_source"]]


def _slots(data, offset) -> list[int]:
    o = offset + mapfile.VISIBLE_ANGLES_HEADER_BYTES
    return [mapfile.u16(data, o + i * 2) for i in range(schema.SLOT_TOTAL)]


def test_a_slotless_base_still_round_trips(slotless):
    """Neither trigger fires on an untouched document -- the counts equal the
    base's and every mask dumps `null` -- so nothing is manufactured and the
    identity round trip is untouched (decision 26). This is the seed every
    manufacture test below is measured against."""
    directory, document = slotless
    assert document["carry"]["visible_angles_slots"] is None
    assert document["carry"]["visible_angles_unknown_896"] is None
    assert all(p["visible_angles"] is None for p in document["polygons"])
    bundle = build(document, directory)
    assert identical(bundle, directory)
    assert mapfile.visible_angles_offset(_source(bundle, document)) is None
    assert not [w for w in bundle.warnings if "0xB0" in w]


def test_adding_a_polygon_against_a_slotless_base_manufactures_the_chunk(slotless):
    """Trigger 1. The chunk is a whole new section: it lands past the last byte
    of the base and its zero pointer becomes a real offset."""
    directory, document = slotless
    source = document["base"]["geometry_source"]
    before = (directory / source).read_bytes()
    grown = (len(build_leg.pack_primary_mesh(
                 document["polygons"] + [copy.deepcopy(document["polygons"][0])]))
             - len(build_leg.pack_primary_mesh(document["polygons"])))
    assert grown > 0

    document["polygons"].append(copy.deepcopy(document["polygons"][0]))
    bundle = build(document, directory)
    data = _source(bundle, document)

    offset = mapfile.visible_angles_offset(data)
    assert offset is not None, "trigger 1 did not fire -- no chunk was made"
    assert mapfile.pointer(before, mapfile.VISIBLE_ANGLES_PTR) == 0
    assert len(data) == len(before) + grown + mapfile.VISIBLE_ANGLES_BYTES
    assert offset + mapfile.VISIBLE_ANGLES_BYTES == len(data), \
        "the manufactured chunk is appended, so it is the file's last section"


def test_an_authored_mask_alone_manufactures_the_chunk(slotless):
    """Trigger 2, fired on its own: the polygon count is untouched, so only a
    non-`null` mask can be what brought the chunk into existence."""
    directory, document = slotless
    source = document["base"]["geometry_source"]
    before = (directory / source).read_bytes()
    document["polygons"][0]["visible_angles"] = 0x1234

    bundle = build(document, directory)
    data = _source(bundle, document)
    offset = mapfile.visible_angles_offset(data)
    assert offset is not None, "trigger 2 did not fire -- no chunk was made"
    assert len(data) == len(before) + mapfile.VISIBLE_ANGLES_BYTES

    # Polygon 0 of MAP116 a0 is a textured_QUAD -- the map has no textured
    # triangles at all -- so its slot is 512, not 0. The bucket bases are the
    # table's, fixed, and a manufactured table has to honour them or the engine
    # reads the mask off the wrong polygon.
    row = 0
    for kind, capacity in schema.SLOT_CAPACITY:
        if kind == document["polygons"][0]["kind"]:
            break
        row += capacity
    assert row == 512, "MAP116 a0's first polygon is no longer a textured quad"

    slots = _slots(data, offset)
    assert slots[row] == 0x1234
    assert set(slots[:row] + slots[row + 1:]) == {schema.DEFAULT_VISIBLE_ANGLES}, \
        "every slot the document does not author is the disc's own dead fill"


def test_the_manufactured_header_is_the_corpus_constant():
    """Decision 26's blob is DERIVED and then measured, twice: the shape in
    `mapfile` must reproduce the recorded sha256, and that sha256 must still be
    what all 159 chunk-carrying resources open with. A pasted hex literal would
    pass the first and tell us nothing; only the disc can answer the second."""
    header = mapfile.visible_angles_header()
    assert len(header) == mapfile.VISIBLE_ANGLES_HEADER_BYTES
    assert (hashlib.sha256(header).hexdigest()
            == mapfile.VISIBLE_ANGLES_HEADER_SHA256)

    agree, chunkless = 0, 0
    for path in sorted(MAP_DIR.iterdir()):
        if path.suffix == ".GNS" or not path.name.startswith("MAP"):
            continue
        data = path.read_bytes()
        if len(data) == mapfile.TEXTURE_BYTES or mapfile.read_mesh(data) is None:
            continue
        offset = mapfile.visible_angles_offset(data)
        if offset is None:
            chunkless += 1
        elif data[offset:offset + len(header)] == header:
            agree += 1
    assert (agree, chunkless) == (159, 10), (agree, chunkless)


def test_the_manufacture_warns_that_the_patcher_must_relocate(slotless):
    """The manufacture is a warning and can never be a refusal -- `build` never
    sees the patcher's recipe. It has to say enough for the recipe to be
    written: what grew, by how much, and which two keys (#522)."""
    directory, document = slotless
    document["polygons"][0]["visible_angles"] = 0x1234
    bundle = build(document, directory)
    named = [w for w in bundle.warnings if "0xB0" in w]
    assert len(named) == 1, bundle.warnings
    warning = named[0]
    for fragment in (document["base"]["geometry_source"], "4096",
                     "allow_relocate", "[free_space].ranges", "decision 26"):
        assert fragment in warning, warning


def test_the_manufacture_moves_no_other_pointer(slotless):
    """Decision 26's Scope: this chunk and no other. The mask-only trigger adds
    no polygon, so nothing else in the file changes length -- every other slot
    of the 49 must be byte-for-byte what it was, and no other zero becomes an
    offset."""
    directory, document = slotless
    before = (directory / document["base"]["geometry_source"]).read_bytes()
    was = {slot: mapfile.pointer(before, slot)
           for slot in range(0, mapfile.HEADER_BYTES, 4)}
    document["polygons"][0]["visible_angles"] = 0x1234
    data = _source(build(document, directory), document)
    now = {slot: mapfile.pointer(data, slot)
           for slot in range(0, mapfile.HEADER_BYTES, 4)}
    assert now.pop(mapfile.VISIBLE_ANGLES_PTR) == len(before)
    was.pop(mapfile.VISIBLE_ANGLES_PTR)
    assert now == was
    assert data[mapfile.HEADER_BYTES:len(before)] == \
        before[mapfile.HEADER_BYTES:], "past the header nothing may move"


def test_a_chunkless_sibling_of_a_chunked_source_is_never_written_to(tmp_path):
    """MAP083 a0 is the tenth chunkless resource and NOT one of the nine: its
    geometry source carries a chunk, so the manufacture path is never reached
    and MAP083.10 keeps its absent chunk however much the document grows."""
    directory = tmp_path / "chunked-source"
    directory.mkdir()
    for path in sorted(MAP_DIR.glob(f"MAP{CHUNKED_SOURCE_MAP:03d}.*")):
        shutil.copy2(path, directory / path.name)
    document, _sheets = dump(directory, CHUNKED_SOURCE_MAP, 0)

    source = document["base"]["geometry_source"]
    siblings = [e["name"] for e in document["base"]["resources"]
                if e["name"] != source
                and mapfile.read_mesh((directory / e["name"]).read_bytes())
                is not None
                and mapfile.visible_angles_offset(
                    (directory / e["name"]).read_bytes()) is None]
    assert siblings, "this map no longer has the chunkless sibling it is for"
    assert mapfile.visible_angles_offset(
        (directory / source).read_bytes()) is not None

    grown = (len(build_leg.pack_primary_mesh(
                 document["polygons"] + [copy.deepcopy(document["polygons"][0])]))
             - len(build_leg.pack_primary_mesh(document["polygons"])))
    document["polygons"].append(copy.deepcopy(document["polygons"][0]))
    document["polygons"][0]["visible_angles"] = 0x1234
    bundle = build(document, directory)
    for name in siblings:
        data = bundle.resources[name]
        assert mapfile.visible_angles_offset(data) is None, name
        assert mapfile.pointer(data, mapfile.VISIBLE_ANGLES_PTR) == 0, name
        # It DOES grow -- decision 2's fan-out writes the new primary mesh to
        # every 0x40-carrying resource in the arrangement. By the mesh's 48 B
        # and not by the chunk's 4,096.
        assert len(data) == len((directory / name).read_bytes()) + grown, name
    assert not [w for w in bundle.warnings if "0xB0" in w]


def test_the_manufacture_reaches_the_geometry_source_and_nothing_else(tmp_path):
    """Decision 26's Scope, measured against a non-empty set. MAP099 a0 pairs
    the chunkless source with nine 753-byte siblings that carry no `0x40`;
    unscoped, `build` would stamp slot 0xB0 of each one with 753 and hang a
    4,096-B chunk off a file five times smaller than the chunk."""
    directory = tmp_path / "siblings"
    directory.mkdir()
    for path in sorted(MAP_DIR.glob(f"MAP{SIBLINGS_MAP:03d}.*")):
        shutil.copy2(path, directory / path.name)
    document, _sheets = dump(directory, SIBLINGS_MAP, 0)

    source = document["base"]["geometry_source"]
    siblings = [e["name"] for e in document["base"]["resources"]
                if e["name"] != source
                and len((directory / e["name"]).read_bytes())
                != mapfile.TEXTURE_BYTES]
    assert len(siblings) == 9, siblings
    assert mapfile.visible_angles_offset(
        (directory / source).read_bytes()) is None

    document["polygons"][0]["visible_angles"] = 0x1234
    bundle = build(document, directory)
    assert mapfile.visible_angles_offset(_source(bundle, document)) is not None
    for name in siblings:
        assert bundle.resources[name] == (directory / name).read_bytes(), name


def test_splice_turns_a_zero_pointer_into_the_offset_it_appended_at():
    """The one mechanic the splice had never done. A zero slot is skipped by
    the shift loop by construction -- an absent section has no offset to move
    -- so appending is the only way it can ever become non-zero."""
    data = bytearray(400)
    struct.pack_into("<I", data, 0x40, 196)
    out = splice(bytes(data), [], manufactured=(mapfile.VISIBLE_ANGLES_PTR, b"ZZZZ"))
    assert len(out) == 404
    assert struct.unpack_from("<I", out, mapfile.VISIBLE_ANGLES_PTR)[0] == 400
    assert struct.unpack_from("<I", out, 0x40)[0] == 196, "an untouched pointer"
    assert out[400:] == b"ZZZZ"


def test_splice_manufacturing_into_an_occupied_slot_refuses():
    """A manufactured section is only ever an ABSENT one."""
    data = bytearray(400)
    struct.pack_into("<I", data, mapfile.VISIBLE_ANGLES_PTR, 196)
    with pytest.raises(BuildRefusal, match="already points at"):
        splice(bytes(data), [], manufactured=(mapfile.VISIBLE_ANGLES_PTR, b"ZZZZ"))


# ---------------------------------------------------------------------------
# §7.2 / §10.5 -- terrain classification
# ---------------------------------------------------------------------------

def _drift_a_tile(document):
    """Raise every floor by one height step, so the drift checker names the
    tiles the base's floor_steps covers. Returns one drifted (x, z)."""
    for poly in document["polygons"]:
        for vertex in poly["positions"]:
            vertex[1] -= schema.HEIGHT_STEP
    return tuple(document["base"]["floor_steps"][0][:2])


def test_a_pre_growth_tile_the_drift_checker_does_not_name_refuses(doc, scratch):
    doc["terrain"] = [{"x": 0, "z": 0, "level": 0, "height": 9}]
    refuses(doc, scratch, "still the base's", "drift checker does not name it")


def test_a_drift_named_tile_may_declare_its_three_fields(doc, scratch):
    x, z = _drift_a_tile(doc)
    doc["terrain"] = [{"x": x, "z": z, "level": 0, "height": 9}]
    bundle = built(doc, scratch)
    data = bundle.resources[doc["base"]["terrain_source"]]
    offset = mapfile.terrain_offset(data) + 2
    size_x = doc["base"]["terrain_grid"]["size_x"]
    record = schema.decode_record(
        data[offset + (z * size_x + x) * 8:offset + (z * size_x + x) * 8 + 8])
    assert record["height"] == 9


def test_a_drift_named_tile_may_not_declare_a_pin_byte(doc, scratch):
    x, z = _drift_a_tile(doc)
    doc["terrain"] = [{"x": x, "z": z, "level": 0, "surface_type": 5}]
    refuses(doc, scratch, "may declare only", "surface_type")


def test_an_undeclared_field_carries_from_the_base(doc, scratch):
    x, z = _drift_a_tile(doc)
    size_x = doc["base"]["terrain_grid"]["size_x"]
    before = (scratch / doc["base"]["terrain_source"]).read_bytes()
    offset = mapfile.terrain_offset(before) + 2 + (z * size_x + x) * 8
    original = before[offset:offset + 8]
    doc["terrain"] = [{"x": x, "z": z, "level": 0, "height": 9}]
    after = built(doc, scratch).resources[doc["base"]["terrain_source"]]
    rebuilt = after[offset:offset + 8]
    assert rebuilt[2] == 9
    assert rebuilt[:2] == original[:2] and rebuilt[3:] == original[3:]


def test_a_record_outside_the_documents_extent_refuses(doc, scratch):
    grid = doc["base"]["terrain_grid"]
    doc["terrain"] = [{"x": grid["size_x"], "z": 0, "level": 0, "height": 1}]
    refuses(doc, scratch, "outside the document", "no byte to write it to")


def test_a_growth_created_tile_may_declare_the_lot(doc, scratch):
    grid = doc["base"]["terrain_grid"]
    z = grid["size_z"]
    grid["size_z"] = z + 1
    doc["terrain"] = [{"x": 0, "z": z, "level": 0, "surface_type": 5,
                       "height": 3, "rotation": 2}]
    bundle = built(doc, scratch)
    data = bundle.resources[doc["base"]["terrain_source"]]
    offset = mapfile.terrain_offset(data)
    assert data[offset + 1] == z + 1
    record = schema.decode_record(
        data[offset + 2 + (z * grid["size_x"]) * 8:
             offset + 2 + (z * grid["size_x"]) * 8 + 8])
    assert (record["surface_type"], record["height"], record["rotation"]) == (5, 3, 2)


def test_shrinking_the_grid_refuses(doc, scratch):
    doc["base"]["terrain_grid"]["size_z"] -= 1
    doc["terrain"] = []
    refuses(doc, scratch, "shrinks the base")


def test_an_out_of_range_payload_value_refuses(doc, scratch):
    x, z = _drift_a_tile(doc)
    doc["terrain"] = [{"x": x, "z": z, "level": 0, "slope_height": 64}]
    refuses(doc, scratch, "slope_height")


def test_a_non_integer_payload_field_refuses(doc, scratch):
    x, z = _drift_a_tile(doc)
    doc["terrain"] = [{"x": x, "z": z, "level": 0, "height": "tall"}]
    refuses(doc, scratch, "not an integer")


def test_a_tile_declared_twice_refuses(doc, scratch):
    x, z = _drift_a_tile(doc)
    doc["terrain"] = [{"x": x, "z": z, "level": 0, "height": 1},
                      {"x": x, "z": z, "level": 0, "height": 2}]
    refuses(doc, scratch, "declared twice")


def test_terrain_records_with_no_terrain_chunk_refuse(scratch):
    """MAP001.a0 has a chunk, so this needs an arrangement that has none."""
    document, _sheets = dump(scratch, BASE_MAP, BASE_ARRANGEMENT)
    document["base"]["terrain_source"] = None
    document["base"]["terrain_digest"] = None
    document["terrain"] = [{"x": 0, "z": 0, "level": 0, "height": 1}]
    refuses(document, scratch, "no chunk to write them to")


# ---------------------------------------------------------------------------
# §6.5 texture sheets
# ---------------------------------------------------------------------------

def test_a_repainted_sidecar_reaches_the_sheet(doc, scratch, tmp_path):
    state = next(s for s in doc["map_states"] if s["texture_sheet"])
    original = (scratch / state["resource"]).read_bytes()
    indices = bytearray(unpack_4bpp(original))
    indices[0] = (indices[0] + 1) % 16
    sidecars = tmp_path / "sidecars"
    sidecars.mkdir()
    (sidecars / state["texture_sheet"]).write_bytes(
        write_indexed_png(bytes(indices), [(i, i, i) for i in range(16)]))
    for other in doc["map_states"]:
        if other["texture_sheet"] and other["texture_sheet"] != state["texture_sheet"]:
            raw = (scratch / other["resource"]).read_bytes()
            (sidecars / other["texture_sheet"]).write_bytes(
                write_indexed_png(unpack_4bpp(raw), [(i, i, i) for i in range(16)]))
    bundle = build(doc, scratch, sidecar_dir=sidecars)
    rebuilt = bundle.resources[state["resource"]]
    assert rebuilt != original
    assert rebuilt[0] == (original[0] & 0xF0) | (indices[0] & 0xF)


def test_a_missing_sidecar_refuses(doc, scratch, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(BuildRefusal, match="not in"):
        build(doc, scratch, sidecar_dir=empty)


# ---------------------------------------------------------------------------
# warnings -- never refusals (§10)
# ---------------------------------------------------------------------------

def test_the_sentinel_binding_is_silent(doc, scratch):
    for poly in doc["polygons"]:
        if "terrain" in poly:
            poly["terrain"] = {"x": 255, "z": 127, "level": 1}
    assert not [w for w in built(doc, scratch).warnings if "outside the" in w]


def test_a_binding_a_legal_grid_could_hold_speaks(doc, scratch):
    grid = doc["base"]["terrain_grid"]
    target = {"x": grid["size_x"] + 2, "z": 2, "level": 0}
    assert target["x"] < build_leg.GROWTH_AXIS_MAX
    for poly in doc["polygons"]:
        if "terrain" in poly:
            poly["terrain"] = dict(target)
            break
    warnings = [w for w in built(doc, scratch).warnings if "outside the" in w]
    assert warnings, "a reachable out-of-grid binding must warn"


def test_the_binding_drift_check_can_fire(doc, scratch):
    """The identity is silent here, so the check has to be shown non-vacuous:
    an INERT seed reads exactly like a blind check."""
    assert not [w for w in built(doc, scratch).warnings if "drifted off" in w]
    moved = 0
    for poly in doc["polygons"]:
        t = poly.get("terrain")
        if not t or (t["x"] == 255 and t["z"] == 127) or (t["x"] == 0 and t["z"] == 0):
            continue
        if ((t["z"] << 1) | t["level"]) == t["x"]:
            continue
        for vertex in poly["positions"]:
            vertex[0] += schema.TILE_UNITS * 4
        moved += 1
        break
    assert moved == 1, "no polygon was eligible -- the seed did nothing"
    assert [w for w in built(doc, scratch).warnings if "drifted off" in w]


def test_the_nine_unexplained_maps_are_named_not_silent():
    assert "MAP000" in build_leg.DRIFT_CHECK_SUPPRESSED
    assert len(build_leg.DRIFT_CHECK_SUPPRESSED) == 9


# ---------------------------------------------------------------------------
# the addon's copy of the PNG codec must not drift from the package's
# ---------------------------------------------------------------------------

def test_the_addon_and_the_package_share_one_png_codec():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    package = (root / "exmateria_map" / "png_indexed.py").read_bytes()
    addon = (root / "addons" / "exmateria_map" / "png_indexed.py").read_bytes()
    assert package == addon, (
        "the addon writes the sidecars and `build` reads them; a codec that "
        "drifts between the two is a sheet that round-trips through neither"
    )


# ---------------------------------------------------------------------------
# the authored light rig -- ADR-0004 decision 27, schema §7.1
# ---------------------------------------------------------------------------

RIG_STATE = 1                  # MAP001.9, kind 46, carries a 0x64 chunk
TEXTURE_STATE = 0              # MAP001.8, kind 23 -- no rig, BY KIND
CHUNKLESS_STATE = 2            # MAP001.11, kind 47, 0x64 == 0


def authored(doc, index):
    """The state's own rig, promoted into the authored field unchanged."""
    state = doc["map_states"][index]
    rig = copy.deepcopy(state["light_rig"])
    state[schema.AUTHORED_RIG] = rig
    doc["version"] = schema.AUTHORED_RIG_VERSION
    return rig


def rig_bytes(data, name=None):
    """The 45 bytes at 0x64 of a mesh resource."""
    offset = mapfile.light_rig_offset(data, True)
    assert offset is not None, name
    return data[offset:offset + mapfile.LIGHT_RIG_BYTES]


def test_pack_light_rig_is_the_readers_exact_inverse_over_the_whole_corpus():
    """The writer did not exist anywhere in this repo, and the layout it has to
    invert is asymmetric -- `colors` PLANAR, `directions` INTERLEAVED. A single
    hand-built rig would not catch a transposed index; every rig the disc ships
    does."""
    seen = 0
    for number in mapfile.map_numbers(MAP_DIR):
        try:
            files = mapfile.bind(MAP_DIR, number)
        except mapfile.BindError:
            continue
        mesh_sectors = {r.sector for r in files.rows if r.is_mesh}
        for sector in sorted(mesh_sectors):
            path = files.by_sector[sector]
            data = path.read_bytes()
            rig = mapfile.read_light_rig(data, True)
            if rig is None:
                continue
            seen += 1
            assert mapfile.pack_light_rig(rig) == rig_bytes(data, path.name), path.name
    assert seen > 500, f"only {seen} rigs read -- the corpus holds far more"


def test_the_reader_does_not_invent_a_rig_out_of_texture_pixels():
    """#576. MAP062 a0's four texture rows hold `f0 0f 00 00` at 0x64, which is
    4,080 sheet PIXELS read as a plausible pointer. The seed arm is the
    pointer-shaped test itself: it still fires, which is what makes the kind
    test's `None` a decision rather than an absence."""
    files = mapfile.bind(MAP_DIR, 62)
    rows = [r for r in files.arrangement_rows(0) if not r.is_pad]
    invented = 0
    for row in rows:
        data = files.by_sector[row.sector].read_bytes()
        if row.is_mesh:
            continue
        assert mapfile.read_light_rig(data, row.is_mesh) is None
        assert mapfile.light_rig_offset(data, row.is_mesh) is None
        if mapfile.read_light_rig(data, True) is not None:
            invented += 1                      # the pointer test WOULD have fired
    assert invented == 4, f"{invented} texture rows read a rig from pixels, not 4"


def test_an_untouched_document_declares_no_rig_and_stamps_1(doc, scratch):
    assert doc["version"] == schema.VERSION == 1
    assert all(schema.AUTHORED_RIG not in st for st in doc["map_states"])
    assert identical(built(doc, scratch), scratch)


def test_a_declared_rig_reaches_the_disc_byte_exact(doc, scratch):
    rig = authored(doc, RIG_STATE)
    rig["ambient"] = [17, 34, 51]
    rig["colors"] = [[3456, -8, 4], [1, 2, 3], [-4, -5, -6]]
    rig["directions"] = [[4096, 0, 0], [0, -4096, 0], [-1234, 2345, -3456]]
    name = doc["map_states"][RIG_STATE]["resource"]

    bundle = built(doc, scratch)
    assert rig_bytes(bundle.resources[name]) == mapfile.pack_light_rig(rig)
    # and it reads back as the rig that was declared -- ambient and the gains
    # are integer-exact, and so is a direction once it is IN the file (the
    # lossy step is Blender's unit vector, upstream of the document).
    assert mapfile.read_light_rig(bundle.resources[name], True) == rig


def test_a_declared_rig_changes_those_45_bytes_and_nothing_else(doc, scratch):
    rig = authored(doc, RIG_STATE)
    rig["ambient"] = [1, 2, 3]
    name = doc["map_states"][RIG_STATE]["resource"]
    before = (scratch / name).read_bytes()
    after = built(doc, scratch).resources[name]

    assert len(after) == len(before)
    offset = mapfile.light_rig_offset(before, True)
    moved = {i for i, (a, b) in enumerate(zip(before, after)) if a != b}
    assert moved and moved <= set(range(offset, offset + mapfile.LIGHT_RIG_BYTES))
    # every OTHER resource of the arrangement is untouched
    bundle = built(doc, scratch)
    for other, data in bundle.resources.items():
        if other != name:
            assert data == (scratch / other).read_bytes(), other


def test_a_rig_declared_at_version_1_is_refused(doc, scratch):
    """The seed arm first: the same document at version 2 builds, so the
    refusal is about the STAMP and not about the field being unwriteable."""
    rig = authored(doc, RIG_STATE)
    rig["ambient"] = [9, 9, 9]
    name = doc["map_states"][RIG_STATE]["resource"]
    assert built(doc, scratch).resources[name] != (scratch / name).read_bytes()
    doc["version"] = 1
    refuses(doc, scratch, schema.AUTHORED_RIG, "version 1",
            "oldest `build` that can honour")


def test_a_texture_row_cannot_carry_a_rig_by_kind(doc, scratch):
    rig = authored(doc, RIG_STATE)                # a document that DOES build
    rig["ambient"] = [9, 9, 9]
    name = doc["map_states"][RIG_STATE]["resource"]
    assert built(doc, scratch).resources[name] != (scratch / name).read_bytes()
    doc["map_states"][TEXTURE_STATE][schema.AUTHORED_RIG] = copy.deepcopy(
        doc["map_states"][RIG_STATE][schema.AUTHORED_RIG])
    refuses(doc, scratch, "not a mesh type", "by kind")


def test_a_chunkless_mesh_row_cannot_carry_a_rig(doc, scratch):
    assert doc["map_states"][CHUNKLESS_STATE]["light_rig"] is None
    assert doc["map_states"][CHUNKLESS_STATE]["kind"] in mapfile.GNS_MESH_TYPES
    doc["map_states"][CHUNKLESS_STATE][schema.AUTHORED_RIG] = copy.deepcopy(
        doc["map_states"][RIG_STATE]["light_rig"])
    doc["version"] = schema.AUTHORED_RIG_VERSION
    refuses(doc, scratch, "0x64 pointer is zero", "decision 19")


def test_the_gradient_must_echo_the_bases_own(doc, scratch):
    rig = authored(doc, RIG_STATE)
    rig["ambient"] = [9, 9, 9]                    # the seed builds unmutated
    name = doc["map_states"][RIG_STATE]["resource"]
    assert built(doc, scratch).resources[name] != (scratch / name).read_bytes()
    rig["gradient"] = [g ^ 1 for g in rig["gradient"]]
    refuses(doc, scratch, "gradient", "verbatim")


def test_a_malformed_rig_is_refused_not_packed(doc, scratch):
    rig = authored(doc, RIG_STATE)
    rig["colors"] = [[70000, 0, 0], [0, 0, 0], [0, 0, 0]]
    refuses(doc, scratch, "not a 45-byte rig", "i16")
