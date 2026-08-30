"""The live link's `bpy`-free core (ADR-0005 decisions 1-3, `docs/live-link-v1.md`).

Only the parts that run without an emulator live here. The transport and the
sinks are proven against a running machine by `tests/live_geometry_audit.py`;
what this file holds is the arithmetic, which is where an off-by-one in a
stride or a field offset actually hides.

The descriptor fixture is 1,368 bytes lifted verbatim out of a running
Gariland battle (`reference-assets/thief_whats_this.sstate`, descriptor block
`0x800FBE00`, 9 x `0x98`). The expectations come from the *disc* -- MAP022 a0's
own polygon counts -- so neither side of the assertion is computed by the code
under test.
"""

import sys
from pathlib import Path

import pytest

ADDON = Path(__file__).resolve().parent.parent / "addons" / "exmateria_map"
sys.path.insert(0, str(ADDON))

import live_link  # noqa: E402
import live_link as L  # noqa: E402
import json  # noqa: E402
import struct  # noqa: E402

FIXTURE = (Path(__file__).resolve().parent / "fixtures"
           / "map022_a0_descriptors.hex")


@pytest.fixture(scope="module")
def descriptors() -> bytes:
    return bytes.fromhex(FIXTURE.read_text().replace("\n", ""))


def test_primary_descriptor_carries_map022_a0_polygon_counts(descriptors):
    """MAP022 a0 is 24 / 361 / 18 / 51 on the disc; the engine says the same."""
    d = live_link.parse_descriptor(descriptors, 0)
    assert d.counts == (24, 361, 18, 51)


# --- the sanity gate -------------------------------------------------------
# It replaces `live_geometry.py`'s needle search for the PUSH direction: the
# artist already has the document open in Blender, so there is nothing to
# identify. What still has to be caught is "no map is loaded" and "this is not
# a descriptor block", and the engine's own array bounds are what says so.

def test_a_loaded_map_passes_the_gate(descriptors):
    live_link.check_descriptors(descriptors)     # must not raise


def test_an_unloaded_map_is_refused():
    """All-zero RAM is what a descriptor block looks like before a map lands."""
    with pytest.raises(live_link.LiveLinkError, match="no map is loaded"):
        live_link.check_descriptors(bytes(live_link.DESCRIPTOR_STRIDE * 9))


def test_a_count_past_the_engines_array_is_refused(descriptors):
    """711 textured quads is one more than the engine's array holds."""
    block = bytearray(descriptors)
    at = live_link.DESCRIPTOR_COUNTS + 2          # textured_quad's count
    block[at:at + 2] = (711).to_bytes(2, "little")
    with pytest.raises(live_link.LiveLinkError, match=r"textured_quad \[0, 711\)"):
        live_link.check_descriptors(bytes(block))


def test_the_bound_is_on_start_plus_count_not_count_alone(descriptors):
    """The arrays are SHARED and SLICED -- a slice that fits still has to fit
    *where it is*. 300 quads is legal; 500 + 300 runs off the end."""
    block = bytearray(descriptors)
    at = live_link.DESCRIPTOR_STRIDE              # descriptor 1
    s = at + live_link.DESCRIPTOR_STARTS + 2      # textured_quad's start
    c = at + live_link.DESCRIPTOR_COUNTS + 2      # textured_quad's count
    block[s:s + 2] = (500).to_bytes(2, "little")
    block[c:c + 2] = (300).to_bytes(2, "little")
    assert block != descriptors, "the seed changed nothing -- it proves nothing"
    with pytest.raises(live_link.LiveLinkError,
                       match=r"textured_quad \[500, 800\)"):
        live_link.check_descriptors(bytes(block))


# --- the sink addresses ----------------------------------------------------
# Four position bases and two normal bases, all measured [LIVE]. What makes
# them checkable offline is that they are not six independent numbers: the
# engine's array bounds relate them, and one of them lands on a known function.

def test_the_textured_triangle_arrays_are_exactly_360_triangles_long():
    """ADR-0004 decision 28 bounds the array at 360; the spacing must agree,
    and must agree for the normal array too -- it is the same shape."""
    pos = live_link.SINKS["textured_quad"].positions \
        - live_link.SINKS["textured_triangle"].positions
    nrm = live_link.SINKS["textured_quad"].normals \
        - live_link.SINKS["textured_triangle"].normals
    assert pos == nrm == 360 * live_link.POLYGON_STRIDE["textured_triangle"]


def test_the_untextured_triangle_array_is_exactly_64_triangles_long():
    """The bound a count-shaped search finds (`slti s0,0x40`), read back out
    of the spacing of two independently measured bases."""
    span = live_link.SINKS["untextured_quad"].positions \
        - live_link.SINKS["untextured_triangle"].positions
    assert span == 64 * live_link.POLYGON_STRIDE["untextured_triangle"]


def test_the_quad_normal_array_ends_at_the_first_byte_of_its_renderer():
    """`0x80127394 + 710 * 32 == 0x8012CC54`, which is `FUN_8012cc54`, the
    textured-triangle renderer. The array runs up to the first byte of code --
    a wrong stride or a wrong bound would not land there."""
    end = (live_link.SINKS["textured_quad"].normals
           + 710 * live_link.POLYGON_STRIDE["textured_quad"])
    assert end == live_link.TEXTURED_TRIANGLE_RENDERER == 0x8012CC54


def test_the_unlit_buckets_have_no_normal_array():
    """`FUN_8012d2b4` / `FUN_8012d568` take one flat colour from
    `DAT_800f5b58`. There is nothing to light and nothing to push."""
    for bucket in ("untextured_triangle", "untextured_quad"):
        assert live_link.SINKS[bucket].normals is None


# --- the write plan --------------------------------------------------------
# Six coordinate bytes per vertex, at an address the descriptor's start index
# decides. Gariland has no animated meshes, so all its start indices are 0 and
# it cannot exercise the slicing at all -- these tests do it synthetically.

def _descriptor(start_tq: int = 0, count_tq: int = 2) -> live_link.Descriptor:
    return live_link.Descriptor(index=0, starts=(0, start_tq, 0, 0),
                                counts=(0, count_tq, 0, 0))


QUADS = [[(1, 2, 3), (4, 5, 6), (7, 8, 9), (10, 11, 12)],
         [(-1, -2, -3), (-4, -5, -6), (-7, -8, -9), (-10, -11, -12)]]


def test_a_slice_with_start_zero_begins_at_the_arrays_measured_base():
    writes = live_link.plan(_descriptor(), "textured_quad", "positions", QUADS)
    assert writes[0][0] == live_link.SINKS["textured_quad"].positions


@pytest.mark.parametrize("start", [0, 1, 7, 180, 708])
def test_positions_and_normals_of_one_polygon_track_together(start):
    """One start index governs positions, normals and packets together -- so
    however the slice moves, the two arrays stay a fixed distance apart."""
    d = _descriptor(start_tq=start)
    pos = live_link.plan(d, "textured_quad", "positions", QUADS)
    nrm = live_link.plan(d, "textured_quad", "normals", QUADS)
    sink = live_link.SINKS["textured_quad"]
    gap = sink.normals - sink.positions
    assert [a for a, _ in nrm] == [a + gap for a, _ in pos]


def test_the_plan_writes_six_of_every_eight_bytes():
    """Bytes 6-7 of each vertex are the polygon's own metadata -- the terrain
    binding word and VISIBLE_ANGLES. Garbage there shatters the map, so the
    plan never names them."""
    writes = live_link.plan(_descriptor(), "textured_quad", "positions", QUADS)
    assert len(writes) == 2 * 4                        # two quads, four vertices
    assert all(len(b) == 6 for _, b in writes)
    starts = [a for a, _ in writes]
    assert [b - a for a, b in zip(starts, starts[1:])] == [8] * 7


def test_the_plan_writes_the_documents_own_length_not_the_loaded_maps():
    """#598 lifted the equality that used to stand here. The plan's length is
    the DOCUMENT's -- that is what makes a shrink and a growth possible at all
    -- and what bounds it is `check_capacity` and `check_followers`, not an
    equality that also forbade every legal deletion.

    Addressing is unchanged: still `base + (start + i) * stride`, so the
    descriptor still decides WHERE, only no longer HOW MANY."""
    d = _descriptor(start_tq=7, count_tq=2)
    grew = live_link.plan(d, "textured_quad", "positions", QUADS * 3)   # 6
    shrank = live_link.plan(d, "textured_quad", "positions", QUADS[:1])  # 1
    assert len(grew) == 6 * 4 and len(shrank) == 1 * 4
    base = live_link.SINKS["textured_quad"].positions + 7 * 32
    assert grew[0][0] == base and shrank[0][0] == base
    assert grew[-1][0] == base + 5 * 32 + 3 * 8


# --- the packer ------------------------------------------------------------
# A record is [offset][length][data]. If the length field is too narrow for
# the length, every byte after it is parsed as the NEXT record's address --
# so an over-long write does not fail, it writes wherever those data bytes
# happen to point. That segfaulted the emulator once; hence these.

def test_a_write_longer_than_the_length_field_is_refused():
    with pytest.raises(live_link.LiveLinkError, match="too long to encode"):
        live_link.pack_writes([(live_link.RAM_BASE, b"\x00" * 0x10000)])


def test_every_record_is_exactly_as_long_as_it_says():
    """Walk the packed string the way the Lua does. If the two disagree the
    walk lands mid-record and every later offset is garbage."""
    writes = [(live_link.RAM_BASE + 0x10, b"\xaa" * 6),
              (live_link.RAM_BASE + 0x2000, bytes(range(200))),
              (live_link.RAM_BASE + 0x100, b"\xff")]
    packed = live_link.pack_writes(writes)
    i, seen = 0, []
    while i < len(packed):
        off = int(packed[i:i + 8], 16)
        n = int(packed[i + 8:i + 12], 16)
        i += 12
        seen.append((off + live_link.RAM_BASE, bytes.fromhex(packed[i:i + n * 2])))
        i += n * 2
    assert seen == writes


def test_a_write_outside_main_ram_is_refused():
    with pytest.raises(live_link.LiveLinkError, match="outside main RAM"):
        live_link.pack_writes([(live_link.RAM_BASE + live_link.RAM_BYTES, b"\x00")])


# --- the whole-document push ----------------------------------------------

def _document(n_tq: int = 2) -> dict:
    return {"version": 1, "polygons": [
        {"kind": "textured_quad",
         "positions": [[1, 2, 3], [4, 5, 6], [7, 8, 9], [10, 11, 12]],
         "normals": [[0, 0, -4096]] * 4} for _ in range(n_tq)]}


def test_a_document_plans_both_fields_of_every_bucket_it_carries():
    plans = live_link.plan_document(_descriptor(), _document())
    assert sorted(plans) == [("textured_quad", "metadata"),
                             ("textured_quad", "normals"),
                             ("textured_quad", "positions")]
    assert all(len(plans[k]) == 2 * 4
               for k in (("textured_quad", "normals"),
                         ("textured_quad", "positions")))
    # Two shorts per polygon, not one per vertex: bytes 6-7 of vertices 0 and 1.
    assert len(plans[("textured_quad", "metadata")]) == 2 * 2


def test_an_unlit_bucket_is_reported_skipped_not_planned():
    """`untextured_*` have no normal array -- unlit by construction. Decision 4
    says push what has a sink and NAME what was skipped, never refuse."""
    doc = {"version": 1, "polygons": [
        {"kind": "untextured_quad",
         "positions": [[1, 2, 3], [4, 5, 6], [7, 8, 9], [10, 11, 12]]}]}
    d = live_link.Descriptor(index=0, starts=(0, 0, 0, 0), counts=(0, 0, 0, 1))
    plans = live_link.plan_document(d, doc)
    assert ("untextured_quad", "positions") in plans
    assert ("untextured_quad", "normals") not in plans


def test_a_document_that_grew_plans_every_polygon_it_carries():
    """The whole point of #598, at the document seam: three quads planned into
    a map that loaded two. The gates -- not the plan -- are what say whether
    those three may be written."""
    plans = live_link.plan_document(_descriptor(count_tq=2), _document(n_tq=3))
    assert len(plans[("textured_quad", "positions")]) == 3 * 4
    assert len(plans[("textured_quad", "metadata")]) == 3 * 2


def test_a_document_that_shrank_plans_only_what_is_left():
    plans = live_link.plan_document(_descriptor(count_tq=9), _document(n_tq=1))
    assert len(plans[("textured_quad", "positions")]) == 1 * 4


# --- the self-check's diagnosis -------------------------------------------
# `verify` is non-destructive on purpose: a check that writes is a check that
# can damage what it was inspecting. It also cannot, alone, tell a wrong
# address from an already-pushed map -- both make RAM differ from the disc --
# so it reports the evidence and names both, rather than asserting one.

def test_verify_is_clean_when_ram_holds_what_the_plan_expects():
    writes = [(live_link.RAM_BASE + 0x100, b"abcdef")]
    ram = {live_link.RAM_BASE + 0x100: b"abcdef"}
    assert live_link.verify(_FakeClient(ram), writes) == (0, 6)


def test_verify_counts_the_bytes_that_differ():
    writes = [(live_link.RAM_BASE + 0x100, b"abcdef")]
    ram = {live_link.RAM_BASE + 0x100: b"abXdeY"}
    assert live_link.verify(_FakeClient(ram), writes) == (2, 6)


def test_the_selfcheck_names_an_already_pushed_map_as_a_cause():
    """The first version said only "the address arithmetic is wrong". After a
    successful push RAM legitimately differs from the disc, so that message
    blamed the rig for the rig having worked."""
    writes = [(live_link.RAM_BASE + 0x100, b"abcdef")]
    client = _FakeClient({live_link.RAM_BASE + 0x100: b"XXXXXX"})
    with pytest.raises(live_link.LiveLinkError) as e:
        live_link.selfcheck(client, writes)
    assert "already been pushed" in str(e.value)
    assert "arithmetic" in str(e.value)
    assert client.writes == [], "a check must not write"


def test_the_selfcheck_names_the_wrong_map_as_a_cause():
    """The third cause, and the one decision 2's amendment created: the gate
    checks that *a* map is loaded, never that it is the document's. So a
    mismatch here is as likely to be `--map 23` against a Gariland battle as it
    is to be a stride bug, and a message naming only the rig sends the reader
    to the wrong file."""
    writes = [(live_link.RAM_BASE + 0x100, b"abcdef")]
    with pytest.raises(live_link.LiveLinkError) as e:
        live_link.selfcheck(_FakeClient({live_link.RAM_BASE + 0x100: b"XXXXXX"}),
                            writes)
    assert "not the base's map" in str(e.value)


class _FakeClient:
    """Enough of `LuaClient` for the arithmetic. No emulator, no HTTP."""

    def __init__(self, ram: dict[int, bytes]):
        self.ram, self.writes = ram, []

    def read(self, address: int, length: int) -> bytes:
        for base, data in self.ram.items():
            if base <= address and address + length <= base + len(data):
                return data[address - base:address - base + length]
        raise AssertionError(f"unmapped read at 0x{address:08X}+{length}")


# --- the GPU primitive packets: uv, palette_id, texture_page ----------------
# Rooted in `FUN_800f5578`, the packet writer (NOT `FUN_800f4dd4`, which loads
# the position arrays and the descriptor block). It writes, per textured
# triangle at stride 0x28 from the packet base:
#
#     +0x0C/0D, +0x18/19, +0x24/25   the three (u, v) byte pairs
#     +0x0E                          CLUT, as `src & 0x3F | 0x7800`
#     +0x1A                          TPAGE, copied verbatim
#
# and per textured quad at stride 0x34 from base+0x3840, the same offsets plus
# a fourth UV pair at +0x30/31.
#
# Verified live in a Gariland battle (MAP022 a0) on 2026-08-25: 1,516 of 1,516
# UV corners match the disc exactly, `palette_id` maps 1:1 onto `0x7800|id`
# across 10 distinct values, and `texture_page` 1:1 onto the TPAGE word's low
# two bits across 3.

def test_packet_layout_matches_the_psx_primitive_strides():
    """The layout's last field must fit inside the bucket's packet stride."""
    for bucket, lay in L.PACKET_LAYOUT.items():
        stride = L.SINKS[bucket].packet_stride
        assert len(lay.uv) == L.VERTS[bucket]
        for off in (*lay.uv, lay.clut, lay.tpage):
            assert 0 <= off + 2 <= stride, (bucket, hex(off), hex(stride))


def test_plan_packets_uv_writes_one_pair_per_corner():
    polys = [{"uv": [[1, 2], [3, 4], [5, 6]]},
             {"uv": [[7, 8], [9, 10], [11, 12]]}]
    d = L.Descriptor(index=0, starts=(0, 0, 0, 0), counts=(2, 0, 0, 0))
    w = L.plan_packets(d, 0x80100000, "textured_triangle", "uv", polys, b"")
    assert len(w) == 6
    assert w[0] == (0x80100000 + 0x0C, bytes([1, 2]))
    assert w[1] == (0x80100000 + 0x18, bytes([3, 4]))
    assert w[2] == (0x80100000 + 0x24, bytes([5, 6]))
    # the second polygon is one whole packet along
    assert w[3] == (0x80100000 + 0x28 + 0x0C, bytes([7, 8]))


def test_plan_packets_honours_the_start_index():
    """The packets are shared and sliced exactly as the position arrays are."""
    polys = [{"uv": [[1, 2], [3, 4], [5, 6]]}]
    d = L.Descriptor(index=1, starts=(5, 0, 0, 0), counts=(1, 0, 0, 0))
    w = L.plan_packets(d, 0x80100000, "textured_triangle", "uv", polys, b"")
    assert w[0][0] == 0x80100000 + 5 * 0x28 + 0x0C


def test_plan_packets_preserves_the_bits_it_does_not_own():
    """CLUT keeps the engine's `0x7800`; TPAGE keeps whatever base VRAM column
    the map was loaded into. The document owns 4 bits of one and 2 of the other
    -- `data[o+2] & 0x0F` and `data[o+6] & 3` -- and nothing else."""
    stride = 0x28
    cur = bytearray(stride)
    cur[0x0E:0x10] = struct.pack("<H", 0x7809)      # palette 9
    cur[0x1A:0x1C] = struct.pack("<H", 0x000E)      # tpage base 0x0C, page 2
    d = L.Descriptor(index=0, starts=(0, 0, 0, 0), counts=(1, 0, 0, 0))

    w = L.plan_packets(d, 0, "textured_triangle", "palette_id",
                       [{"palette_id": 3}], bytes(cur))
    assert w == [(0x0E, struct.pack("<H", 0x7803))]

    w = L.plan_packets(d, 0, "textured_triangle", "texture_page",
                       [{"texture_page": 1}], bytes(cur))
    assert w == [(0x1A, struct.pack("<H", 0x000D))]

    # page 3 is the one the Gariland battle never showed -- 7,207 polygons in
    # the corpus use it, and it must not need a base of 0x0C to be right.
    w = L.plan_packets(d, 0, "textured_triangle", "texture_page",
                       [{"texture_page": 3}], bytes(cur))
    assert w == [(0x1A, struct.pack("<H", 0x000F))]
    cur[0x1A:0x1C] = struct.pack("<H", 0x01F4)      # a wholly different base
    w = L.plan_packets(d, 0, "textured_triangle", "texture_page",
                       [{"texture_page": 3}], bytes(cur))
    assert w == [(0x1A, struct.pack("<H", 0x01F7))]


def test_plan_packets_writes_the_documents_own_length_too():
    """UV needs no held bytes, so growth is only a longer plan."""
    d = live_link.Descriptor(index=0, starts=(0, 3, 0, 0), counts=(0, 2, 0, 0))
    polys = [{"uv": [[1, 2], [3, 4], [5, 6], [7, 8]]} for _ in range(5)]
    writes = live_link.plan_packets(d, live_link.PACKET_BASES[0],
                                    "textured_quad", "uv", polys, b"")
    assert len(writes) == 5 * 4


def test_a_masked_field_still_refuses_short_bytes_for_a_LOADED_slot():
    """`palette_id` and `texture_page` are read-modify-write: the document owns
    four bits of the CLUT halfword and two of the TPAGE's, and zeroing the rest
    would point every face at VRAM row 0. Passing nothing for a slot the map
    actually loaded is refused rather than treated as zero."""
    d = live_link.Descriptor(index=0, starts=(0, 0, 0, 0), counts=(0, 2, 0, 0))
    polys = [{"palette_id": 3} for _ in range(2)]
    with pytest.raises(live_link.LiveLinkError, match="read-modify-write"):
        live_link.plan_packets(d, live_link.PACKET_BASES[0], "textured_quad",
                               "palette_id", polys, b"")


def test_a_grown_slot_is_not_a_slot_with_nothing_in_it():
    """This asserted the opposite until the live A/B/A. "Past the end of what
    the loader filled there is nothing of the engine's to keep" is false: the
    array is fixed-capacity and the count only says how many slots are DRAWN,
    so a grown slot's halfword still holds bits that are not ours. Short bytes
    are a caller error now, in both directions -- one rule, no special case."""
    d = live_link.Descriptor(index=0, starts=(0, 0, 0, 0), counts=(0, 1, 0, 0))
    stride = live_link.SINKS["textured_quad"].packet_stride
    current = bytearray(stride)
    struct.pack_into("<H", current, live_link.PACKET_LAYOUT["textured_quad"].clut,
                     0x7805)
    with pytest.raises(live_link.LiveLinkError, match="read-modify-write"):
        live_link.plan_packets(d, live_link.PACKET_BASES[0], "textured_quad",
                               "palette_id", [{"palette_id": 9}] * 2,
                               bytes(current))


def test_plan_packets_refuses_an_unlit_bucket():
    """The two untextured buckets have no CLUT, no TPAGE and no UV: their
    packets are G3/G4, and `FUN_800f5578` writes only a flag byte into them."""
    d = L.Descriptor(index=2, starts=(0, 0, 0, 0), counts=(1, 0, 0, 0))
    with pytest.raises(L.LiveLinkError):
        L.plan_packets(d, 0, "untextured_triangle", "uv", [{"uv": []}], b"")


def test_plan_packets_needs_current_bytes_for_a_masked_field():
    """UV is a whole write; CLUT and TPAGE are read-modify-write, so a caller
    that forgets to read RAM first must be refused rather than silently
    zeroing the bits the engine owns."""
    d = L.Descriptor(index=0, starts=(0, 0, 0, 0), counts=(1, 0, 0, 0))
    with pytest.raises(L.LiveLinkError):
        L.plan_packets(d, 0, "textured_triangle", "palette_id",
                       [{"palette_id": 3}], b"")


# --- the packets are DOUBLE BUFFERED ----------------------------------------
# `FUN_800ee104` initialises them:
#
#     DAT_8011a2d4 = &DAT_800fc55c;          <- the base starts at buffer A
#     do { ...; puVar4 = puVar4 + 0xee28; iVar2 = iVar2 + 1; } while (iVar2 < 2);
#
# so there are exactly two, 0xEE28 apart. Sampling `PACKET_BASE_POINTER` live in
# a Gariland battle returns 0x800FC55C and 0x8010B384 and nothing else, which is
# the same two addresses.
#
# This is why the position and normal arrays need ONE write and the packets need
# TWO: those arrays are static and shared by both buffers. Measured -- a palette
# push into the buffer the pointer happened to name changed 385 bytes and the
# screen did not move.

def test_packet_bases_are_the_two_the_engine_alternates():
    assert L.PACKET_BASES == (0x800FC55C, 0x8010B384)
    assert L.PACKET_BASES[1] - L.PACKET_BASES[0] == L.PACKET_BUFFER_STRIDE
    assert L.PACKET_BUFFER_STRIDE == 0xEE28


def test_a_live_pointer_outside_the_two_buffers_is_refused():
    """The guard exists because writing the wrong base is silent: every address
    is inside main RAM, `apply` reports a plausible changed-byte count, and the
    only symptom is a picture that does not move."""
    for good in L.PACKET_BASES:
        L.check_packet_base(good)
    with pytest.raises(L.LiveLinkError):
        L.check_packet_base(0x800FC55C + 4)
    with pytest.raises(L.LiveLinkError):
        L.check_packet_base(0x80000000)


def test_both_buffers_are_planned_and_they_differ_by_the_stride():
    polys = [{"uv": [[1, 2], [3, 4], [5, 6]]}]
    d = L.Descriptor(index=0, starts=(0, 0, 0, 0), counts=(1, 0, 0, 0))
    a = L.plan_packets(d, L.PACKET_BASES[0], "textured_triangle", "uv", polys, b"")
    b = L.plan_packets(d, L.PACKET_BASES[1], "textured_triangle", "uv", polys, b"")
    assert [x for x, _ in b] == [x + L.PACKET_BUFFER_STRIDE for x, _ in a]
    assert [y for _, y in b] == [y for _, y in a]


# --- the aim (decision 9) ---------------------------------------------------

def _states() -> list[dict]:
    """MAP022 a0's first two groups, verbatim from `dump`.

    Two rows per `(night, weather)`: a TEXTURE row (kind 23) carrying the sheet
    and a mesh row carrying the rig. The values are the DISC's, so neither side
    of an assertion is computed by the code under test.
    """
    return [
        {"resource": "MAP022.8", "kind": 23, "night": 0, "weather": 0,
         "palettes": None, "texture_sheet": "MAP022.a0.sheet-fb193f75.png",
         "light_rig": None},
        {"resource": "MAP022.9", "kind": 46, "night": 0, "weather": 0,
         "palettes": ["<16 cluts>"], "texture_sheet": None,
         "light_rig": {"colors": [[1616, 1648, 1696], [2192, 2096, 2032],
                                  [816, 1088, 1536]],
                       "directions": [[1816, -3661, 269], [3943, -551, 962],
                                      [-2030, 3349, 1200]],
                       "ambient": [49, 54, 56],
                       "gradient": [8, 51, 116, 60, 192, 220]}},
        {"resource": "MAP022.12", "kind": 23, "night": 0, "weather": 1,
         "palettes": None, "texture_sheet": "MAP022.a0.sheet-6c19e818.png",
         "light_rig": None},
        {"resource": "MAP022.13", "kind": 48, "night": 0, "weather": 1,
         "palettes": None, "texture_sheet": None,
         "light_rig": {"colors": [[1128, 1088, 1136], [1632, 1536, 1512],
                                  [800, 1072, 1520]],
                       "directions": [[1816, -3661, 269], [3943, -551, 962],
                                      [-2030, 3349, 1200]],
                       "ambient": [49, 54, 56],
                       "gradient": [33, 70, 82, 60, 144, 156]}},
        {"resource": "MAP022.16", "kind": 23, "night": 0, "weather": 2,
         "palettes": None, "texture_sheet": "MAP022.a0.sheet-6c19e818.png",
         "light_rig": None},
        {"resource": "MAP022.17", "kind": 48, "night": 0, "weather": 2,
         "palettes": None, "texture_sheet": None,
         "light_rig": {"colors": [[728, 688, 736], [1232, 1136, 1112],
                                  [784, 1056, 1504]],
                       "directions": [[1816, -3661, 269], [3943, -551, 962],
                                      [-2030, 3349, 1200]],
                       "ambient": [49, 54, 56],
                       "gradient": [42, 49, 56, 76, 93, 97]}},
    ]


def test_one_aim_resolves_to_the_two_rows_that_hold_the_picture():
    """Decision 9: `(night, weather)` names a GROUP, not a row. The sheet's
    pixels are the TEXTURE row's and the rig is the mesh row's, so an aim at
    EITHER row of a group has to reach both."""
    at_texture = live_link.aim(_states(), 0)
    at_mesh = live_link.aim(_states(), 1)
    assert at_texture.sheet_row["resource"] == "MAP022.8"
    assert at_texture.rig_row["resource"] == "MAP022.9"
    assert at_mesh.sheet_row == at_texture.sheet_row
    assert at_mesh.rig_row == at_texture.rig_row


def test_an_aim_whose_group_carries_no_palettes_says_so():
    """146 of the corpus's 774 groups carry no palettes, 71 no TEXTURE row and
    7 no rig -- and MAP022 a0's own weathers 1-4 are in the first set.

    The absence has to survive as a `None` for decision 4's "name what was
    skipped" to have anything to name, and a silent reach into the next group
    would push a NEIGHBOURING weather's colours while reporting success.
    """
    at_w0 = live_link.aim(_states(), 1)
    at_w1 = live_link.aim(_states(), 3)
    assert at_w0.palette_row["resource"] == "MAP022.9"
    assert at_w1.palette_row is None


# --- the light rig (decision 9's atom, §2.2) --------------------------------

def _rig() -> dict:
    """MAP022.9's rig, verbatim from `dump` -- the state the Gariland battle is
    in. Every expectation below was read off the running machine BEFORE it was
    written here, so neither side is computed by the code under test."""
    return _states()[1]["light_rig"]


def test_the_rig_plan_writes_the_files_own_bytes_at_the_three_addresses():
    """§2.2: RAM holds `pack_light_rig`'s output, sliced -- not a re-derivation
    of it. Measured byte for byte against 0x800F5AF4 on a live battle."""
    from exmateria_map import mapfile
    packed = mapfile.pack_light_rig(_rig())
    writes = dict(live_link.plan_rig(_rig()))
    assert writes[0x800F5AF4] == packed[0:18]
    assert writes[0x800F5B14] == packed[18:36]
    assert len(writes) == 3


def test_the_gains_are_PLANAR_and_the_directions_are_INTERLEAVED():
    """The row-vs-column question this was held back for, and the two matrices
    answer it DIFFERENTLY. The disc stores the colours planar -- all three
    lights' red, then all three greens -- which is already the GTE's colour
    matrix order, while the directions are per light. A plan that transposed
    both or neither would put nine plausible numbers in the wrong nine slots,
    and the picture would change rather than break."""
    writes = dict(live_link.plan_rig(_rig()))
    gains = struct.unpack("<9h", writes[0x800F5AF4])
    dirs = struct.unpack("<9h", writes[0x800F5B14])
    assert gains[:3] == (1616, 2192, 816)      # the three lights' RED
    assert dirs[:3] == (1816, -3661, 269)      # light one's x, y, z


def test_the_ambient_is_widened_from_three_bytes_to_three_int32():
    """On disc it is `[u8 x 3]` at +36; in RAM it is three 32-bit words, and
    `SetBackColor` shifts each left by 4 on its way to the GTE. Writing three
    BYTES at 0x800F5B40 would set red and leave green and blue at whatever the
    last map left there."""
    writes = dict(live_link.plan_rig(_rig()))
    assert len(writes[0x800F5B40]) == 12
    assert struct.unpack("<3i", writes[0x800F5B40]) == (49, 54, 56)


def test_the_ram_half_is_forty_eight_bytes():
    """39 on disc, 48 in RAM -- the ambient widens. Decision 9's atom is the
    disc's 39; this is what those 39 cost to place."""
    assert sum(len(b) for _, b in live_link.plan_rig(_rig())) == 48


def test_the_gte_half_carries_the_gains_and_the_ambient_and_NOT_the_directions():
    """§2.2's finding, and the reason there are two halves at all. The direction
    matrix is recomposed with the camera rotation and re-loaded into the GTE
    every frame, so the RAM write reaches it on its own. The colour matrix and
    the background colour are loaded ONCE at map load, so a RAM-only push moves
    this state's angles over the last-loaded state's brightness -- a rig
    belonging to no real state, which is exactly what the atom forbids."""
    regs = dict(live_link.plan_rig_gte(_rig()))
    assert sorted(regs) == [13, 14, 15, 16, 17, 18, 19, 20]
    assert not (set(regs) & {8, 9, 10, 11, 12})       # LLM reloads itself


def test_the_background_colour_registers_are_the_ambient_times_sixteen():
    """`SetBackColor` (0x8001D168) is `sll aN, aN, 4` then `ctc2` -- and the
    live machine's cnt13-15 read 784 / 864 / 896 against a disc ambient of
    49 / 54 / 56."""
    regs = dict(live_link.plan_rig_gte(_rig()))
    assert [regs[13], regs[14], regs[15]] == [49 * 16, 54 * 16, 56 * 16]


def test_the_colour_matrix_registers_pack_two_shorts_per_word():
    """cnt16-20 is `m[3][3]` in five words, the ninth short alone in the last.
    Read off the live machine as 1616,2192 | 816,1648 | 2096,1088 | 1696,2032 |
    1536,-."""
    regs = dict(live_link.plan_rig_gte(_rig()))
    assert regs[16] == (1616 & 0xFFFF) | (2192 << 16)
    assert regs[17] == (816 & 0xFFFF) | (1648 << 16)
    assert regs[20] == 1536


def test_a_negative_gain_survives_the_word_packing():
    """The direction matrix is routinely negative and the gains need not be
    positive either; a short packed into the high half of a word without a mask
    would carry its sign bits into the next register."""
    rig = _rig()
    rig["colors"] = [[-1, 2, 3], [4, 5, 6], [7, 8, 9]]
    regs = dict(live_link.plan_rig_gte(rig))
    assert regs[16] == 0xFFFF | (4 << 16)
    assert all(0 <= v <= 0xFFFFFFFF for v in regs.values())


@pytest.mark.parametrize("field,value", [
    ("colors", [[40000, 0, 0], [0, 0, 0], [0, 0, 0]]),
    ("ambient", [300, 0, 0]),
    ("directions", [[0, 0, 0], [0, 0, 0], [0, 0]]),
])
def test_a_rig_that_does_not_fit_the_format_is_refused(field, value):
    """Refuse rather than truncate. A gain that does not fit a signed short is
    not a bright light, it is a wrapped one, and it would land as a plausible
    negative in the middle of the colour matrix."""
    rig = _rig()
    rig[field] = value
    with pytest.raises(live_link.LiveLinkError):
        live_link.plan_rig(rig)


# --- the mutation audit's GRADER, per harness -------------------------------
# "A graded axis the grader does not parse reads as BLIND while the harness
# prints the defect on every run." That has now cost this package five times,
# most recently when `push` was routed to the `CHECK FAIL` branch while
# `blender_live_push.py` prints `  FAIL `. A seed that patches real code and
# changes real behaviour still reads BLIND, which is indistinguishable from a
# check that does not exist.
#
# So the grader is graded: one real failure line per harness, in that harness's
# own format, must come back as a named failure.

_HARNESS_FAILURE_LINES = {
    "rt":     "CHECK FAIL live_probe_imported: {'CANCELLED'}\nSUMMARY: 1/2\n",
    "prefs":  "CHECK FAIL prefs_persist: nope\nSUMMARY: 1/2\n",
    "rig":    "  FAIL rig_v2_written: nope\nSUMMARY: 19/20 checks passed\n",
    "push":   "  FAIL an untouched import changes zero bytes: 4\n"
              "SUMMARY: 33/34 checks passed\n",
    "corpus": "MISMATCH MAP001.a0 normals\nSUMMARY: 147/148 EXACT, "
              "export_refused=0\n",
}


@pytest.mark.parametrize("which", sorted(_HARNESS_FAILURE_LINES))
def test_the_audit_grader_parses_each_harness_own_failure_format(which):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_ema", Path(__file__).resolve().parent / "export_mutation_audit.py")
    ema = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ema)
    red = ema.failures(_HARNESS_FAILURE_LINES[which], which)
    assert red, f"the {which!r} grader saw no failure in its own output format"
    assert "HARNESS_DID_NOT_RUN" not in red
    assert all(r.strip() for r in red)


def test_every_declared_harness_is_graded():
    """A harness in HARNESS with no branch in `failures` cannot fail."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_ema", Path(__file__).resolve().parent / "export_mutation_audit.py")
    ema = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ema)
    assert set(ema.HARNESS) <= set(_HARNESS_FAILURE_LINES), (
        "a harness gained a seed but this grader test does not cover it")
    for which in ema.HARNESS:
        assert ema.failures(_HARNESS_FAILURE_LINES[which], which)


def test_the_report_names_the_other_states_that_share_the_aimed_sheet():
    """Decision 27's rule -- every state the act touched is NAMED in its report
    -- carried over to the push, where it is needed more.

    MAP022 a0's weathers 1 and 2 name ONE sidecar between them (and its night
    rows share a single sheet across four weathers). So "pushed weather 1" is a
    third of the truth: weather 2 moved with it, and the artist who is not told
    finds out by looking at the wrong state later.
    """
    states = _states()
    moved = live_link.also_moved(states, live_link.aim(states, 3))
    assert moved["texture_sheet"] == [(0, 2)]


def test_the_light_rig_has_left_unpushed_because_it_has_a_sink():
    """Decision 9: `map_states[].light_rig` does not go green until all 39
    bytes move. All 39 move -- 18 gains and 18 directions into RAM verbatim and
    the ambient widened to three words, with the gains and the ambient also
    written to the GTE registers nothing re-loads.

    Naming a field that IS pushed is the mirror of decision 4's failure: the
    artist reads "not pushed" beside a change they can see on screen and stops
    trusting the box.
    """
    assert "map_states[].light_rig" not in live_link.UNPUSHED
    assert live_link.plan_rig(_rig())
    assert live_link.plan_rig_gte(_rig())


def test_the_sheet_and_the_palettes_are_no_longer_named_as_unpushed():
    """The mirror of decision 4's failure, and the ratchet's other arm.

    Naming a field that IS pushed is as bad as dropping one silently: the
    artist reads "not pushed: map_states[].texture_sheet" beside a repaint they
    can see on screen, and stops trusting the box. Both of these left
    `UNPUSHED` the way the light rig did -- by getting a sink -- and this test
    is what stops them drifting back in.

    The sinks are in two different memories, which is the whole subtlety of the
    leg: the sheet's pixels are VRAM and stay there, the palettes are VRAM's
    CLUT rows and are re-uploaded from main RAM every frame, so the palette
    push goes to RAM (`CLUT_BLOCK`) and the sheet push goes to VRAM.
    """
    assert "map_states[].texture_sheet" not in live_link.UNPUSHED
    assert "map_states[].palettes" not in live_link.UNPUSHED
    assert live_link.plan_palettes(_palettes())


def test_what_is_left_in_unpushed_really_has_no_sink():
    """The ratchet's first arm: the two that remain are named because nothing
    writes them, not because nobody revisited the list."""
    assert set(live_link.UNPUSHED) == {"the terrain grid",
                                       "polygons[].unknown_untextured"}


# --- bytes 6-7: the binding word and VISIBLE_ANGLES (#598) -------------------
# `live_geometry.py` measured what lives in the fourth `short` of each vertex,
# on MAP022 a0, 454 of 454 polygons: vertex 0 holds the terrain BINDING word,
# vertex 1 holds VISIBLE_ANGLES, and vertices 2-3 hold zero. The tests below do
# not take that on trust -- they re-measure it against the checked-in Gariland
# savestate, which is a frozen capture of a running battle and therefore an
# oracle nothing in this module computes.

SAVESTATE = (Path(__file__).resolve().parent.parent.parent
             / "reference-assets" / "thief_whats_this.sstate")

live_ram = pytest.mark.skipif(
    not SAVESTATE.exists(),
    reason="reference-assets/thief_whats_this.sstate absent")


@pytest.fixture(scope="module")
def gariland_ram(descriptors) -> bytes:
    """Main RAM out of the savestate, located by VERIFYING rather than by an
    offset constant: the descriptor fixture is 1,368 bytes of that same battle,
    so the one place it occurs pins where 0x800FBE00 landed in the file."""
    blob = SAVESTATE.read_bytes()
    at = blob.find(descriptors)
    assert at >= 0, "the descriptor block is not in this savestate"
    assert blob.find(descriptors, at + 1) < 0, "two candidate RAM offsets"
    base = at - (live_link.DESCRIPTOR_BASE - live_link.RAM_BASE)
    return blob[base:base + live_link.RAM_BYTES]


@pytest.fixture(scope="module")
def map022_a0() -> list[dict]:
    """MAP022 a0's polygons, off the disc."""
    corpus = pytest.importorskip("exmateria_map.corpus")
    from exmateria_map import dump as _dump
    map_dir = corpus.map_dir()
    if map_dir is None or not map_dir.exists():
        pytest.skip("corpus absent; set EXMATERIA_ASSETS_DIR")
    return _dump.dump(map_dir, 22, 0)[0]["polygons"]


# --- the light rig, against real RAM (§2.2) ---------------------------------
# Everything §2.2 claims about where the rig lives was measured once, against a
# running emulator, and then written into prose -- which grades nothing. The
# savestate is a frozen capture of that same battle, so the claim can be an
# assertion on every commit instead. `gariland_ram` is #598's locator, reused
# rather than copied: it pins main RAM by VERIFYING (the descriptor fixture
# occurs exactly once) rather than by an offset constant.


@live_ram
def test_the_rig_plan_is_what_a_running_battle_HOLDS(gariland_ram):
    """§2.2's three addresses, against the bytes a real Gariland battle had.

    This is the whole of the rig's RAM layout in one assertion, and each part
    of it is a thing that was guessed wrong at some point before it was
    measured: the gains are PLANAR (all three lights' red, then green, then
    blue) while the directions are per light, and the ambient is three 32-bit
    words where the disc has three bytes. Transpose either matrix, or write the
    ambient as bytes, and this goes red.

    It is deliberately the same `plan_rig` the push calls, not a re-derivation:
    the question is whether what the button writes is what the engine holds.
    """
    for address, planned in live_link.plan_rig(_rig()):
        at = address - live_link.RAM_BASE
        assert gariland_ram[at:at + len(planned)] == planned, (
            f"0x{address:08X}: the battle held "
            f"{gariland_ram[at:at + len(planned)].hex()}, the plan writes "
            f"{planned.hex()}")


@live_ram
def test_the_rig_the_battle_holds_is_the_DISCS_rig(gariland_ram):
    """The other half of the chain, so neither end is assumed.

    The test above says the PLAN matches RAM; this says the canonical disc
    writer does, with the addon's copy out of the loop entirely. So a drift
    between the two copies of the planar packing cannot hide: this one pins
    `mapfile.pack_light_rig` to the engine, the one above pins `plan_rig` to
    the engine, and `test_the_rig_plan_writes_the_files_own_bytes_at_the_three_
    addresses` pins them to each other. Any one of the three going red names
    which link moved.
    """
    from exmateria_map import mapfile
    packed = mapfile.pack_light_rig(_rig())
    at = live_link.RIG_GAINS - live_link.RAM_BASE
    assert gariland_ram[at:at + 18] == packed[0:18]
    at = live_link.RIG_DIRECTIONS - live_link.RAM_BASE
    assert gariland_ram[at:at + 18] == packed[18:36]
    at = live_link.RIG_AMBIENT - live_link.RAM_BASE
    assert struct.unpack("<3i", gariland_ram[at:at + 12]) == tuple(packed[36:39])


@live_ram
def test_the_stale_bytes_before_the_direction_matrix_are_NOT_a_second_one(
        gariland_ram):
    """0x800F5B08..13 looks like a copy of the directions and is not one.

    The loader bulk-copies the file's first 32 bytes to 0x800F5AF4, then the
    PSX `MATRIX` struct's 2-byte pad at 0x800F5B06 eats file bytes 18-19, and
    the real direction block is written separately at 0x800F5B14. What is left
    between them is the tail of that copy -- elements 1..6 of the sequence, not
    0..5. Reading it as data puts the matrix two bytes early, which is a rig
    that is almost right, and this pins the shape so nobody re-derives it.
    """
    packed_dirs = dict(live_link.plan_rig(_rig()))[live_link.RIG_DIRECTIONS]
    pad = live_link.RIG_GAINS + 18 - live_link.RAM_BASE
    assert gariland_ram[pad:pad + 2] == b"\x00\x00", "the MATRIX pad is not zero"
    leftover = gariland_ram[pad + 2:pad + 14]
    assert leftover == packed_dirs[2:14], "not the bulk copy's tail"
    assert leftover != packed_dirs[0:12], "it is NOT the matrix from the start"


@live_ram
def test_the_metadata_plan_is_what_a_running_battle_holds(gariland_ram,
                                                          descriptors,
                                                          map022_a0):
    """The whole of bytes 6-7, against real RAM: 1,816 of 1,816 bytes.

    This is the acceptance the design asks of an untouched push -- zero changed
    bytes across the wider byte set -- run offline, on all four buckets, before
    an emulator is ever started. A rule wrong about the binding's packing, the
    `|1` textured flag, the vertex it lands on or the stride fails here."""
    d = live_link.parse_descriptor(descriptors, 0)
    total = differ = 0
    for bucket in live_link.BUCKETS:
        polys = [p for p in map022_a0 if p["kind"] == bucket]
        for address, data in live_link.plan_metadata(d, bucket, polys):
            o = address - live_link.RAM_BASE
            differ += sum(1 for a, b in zip(gariland_ram[o:o + len(data)], data)
                          if a != b)
            total += len(data)
    assert total == 454 * 2 * 2, total
    assert differ == 0, f"{differ} of {total} metadata bytes differ from RAM"


@live_ram
def test_the_textured_flag_is_a_real_transformation(map022_a0):
    """Bit 0 of the VISIBLE_ANGLES word is SET in RAM on every textured polygon
    and clear on every untextured one, and NO disc value carries it -- so `| 1`
    is the engine's own mark, not a coincidence that happens to agree. Without
    this the `|1` could be dropped and the savestate check above would still
    have to fail, but for a reason nothing names."""
    assert not any(p["visible_angles"] & 1 for p in map022_a0)


def test_the_binding_word_packs_the_way_the_disc_packs_it():
    """`mapfile.read_mesh` reads `(data[o+1], data[o] >> 1, data[o] & 1)` as
    `(x, z, level)`, so the halfword is `x << 8 | z << 1 | level`. FF FF is the
    sentinel `{255, 127, 1}` -- *this face is not on the grid* -- and FF FE is
    `{255, 127, 0}`, an ordinary binding pointing outside it. The two differ by
    one bit and mean opposite things (CONTEXT.md)."""
    sentinel = {"x": 255, "z": 127, "level": 1}
    outside = {"x": 255, "z": 127, "level": 0}
    assert live_link.binding_word({"terrain": sentinel}) == 0xFFFF
    assert live_link.binding_word({"terrain": outside}) == 0xFFFE
    assert live_link.binding_word({"terrain": {"x": 5, "z": 9, "level": 0}}) \
        == 0x0512


def test_a_null_visible_angles_writes_the_new_face_default():
    """`visible_angles` is null on the 10 of 169 resources with no 0xB0 chunk.
    Those write `0x8000` -- what `stamp_new_faces` already gives a new face --
    so RAM never holds a value the document cannot name. MAP022 HAS a 0xB0
    chunk, so no test on the only map we hold a savestate for reaches this."""
    assert live_link.visible_angles_word({"visible_angles": None},
                                         textured=False) == 0x8000
    assert live_link.visible_angles_word({"visible_angles": None},
                                         textured=True) == 0x8001


def test_the_metadata_plan_writes_two_shorts_per_polygon_at_the_start_index():
    """Two `(address, 2 bytes)` writes per polygon -- vertex 0 and vertex 1 --
    off the descriptor's start index, like every other plan here. Vertices 2
    and 3 are left alone: RAM holds zero there and the document has nothing to
    say about them."""
    d = live_link.Descriptor(index=0, starts=(0, 7, 0, 0), counts=(0, 2, 0, 0))
    polys = [{"kind": "textured_quad", "visible_angles": 0x8000,
              "terrain": {"x": 1, "z": 2, "level": 0}} for _ in range(2)]
    writes = live_link.plan_metadata(d, "textured_quad", polys)
    base = live_link.SINKS["textured_quad"].positions + 7 * 32
    assert [a for a, _ in writes] == [base + 6, base + 8 + 6,
                                      base + 32 + 6, base + 32 + 8 + 6]
    assert all(len(b) == 2 for _, b in writes)


# --- the count write (#598) --------------------------------------------------
# The count is the switch. The dispatch at 0x800E840C recomputes
# `count = descriptor[+0x90 + 2*bucket]` immediately before each renderer call,
# every frame -- so lowering it stops slots being drawn on the next frame and
# raising it starts them, with no reload and no reallocation.

def test_the_documents_bucket_counts_are_what_the_engine_loaded(descriptors,
                                                                map022_a0):
    """The document's four counts, off the disc, against the four the running
    battle holds. Neither side is computed by the code under test."""
    d = live_link.parse_descriptor(descriptors, 0)
    assert live_link.bucket_counts(map022_a0) == d.counts


def test_the_count_plan_writes_the_block_the_engine_already_holds(descriptors):
    """Plan the counts that are already there and the bytes must be the
    fixture's own, at the fixture's own offsets. That is the same assertion
    `selfcheck` makes of the geometry, made of the descriptor."""
    d = live_link.parse_descriptor(descriptors, 0)
    for address, data in live_link.plan_counts(d, d.counts):
        o = address - live_link.DESCRIPTOR_BASE
        assert descriptors[o:o + len(data)] == data, hex(address)


def test_the_count_plan_is_four_shorts_at_the_dispatchs_own_offsets():
    d = live_link.Descriptor(index=0, starts=(0, 0, 0, 0), counts=(1, 2, 3, 4))
    writes = live_link.plan_counts(d, (9, 8, 7, 6))
    assert [a for a, _ in writes] == [
        live_link.DESCRIPTOR_BASE + live_link.DESCRIPTOR_COUNTS + 2 * k
        for k in range(4)]
    assert [struct.unpack("<H", b)[0] for _, b in writes] == [9, 8, 7, 6]


def test_the_count_plan_follows_the_descriptor_it_is_given():
    """Descriptor 3's counts are 3 * 0x98 further on. A plan that assumed the
    primary would write the wrong instance's slice length."""
    d = live_link.Descriptor(index=3, starts=(0, 0, 0, 0), counts=(1, 1, 1, 1))
    assert live_link.plan_counts(d, (1, 1, 1, 1))[0][0] == (
        live_link.DESCRIPTOR_BASE + 3 * live_link.DESCRIPTOR_STRIDE
        + live_link.DESCRIPTOR_COUNTS)


def test_a_bucket_the_document_emptied_still_gets_its_count_written():
    """`plan_document` skips a bucket with no polygons, so a count write driven
    by the plan dict would leave an emptied bucket's old count standing and the
    engine would keep drawing slots the document no longer has. Zero is a legal
    count to write, and the four buckets -- not the plan -- are what drives it."""
    doc = [{"kind": "textured_quad", "positions": [], "visible_angles": 0}]
    assert live_link.bucket_counts(doc) == (0, 1, 0, 0)
    assert len(live_link.plan_counts(
        live_link.Descriptor(index=0, starts=(0,) * 4, counts=(0, 5, 0, 0)),
        live_link.bucket_counts(doc))) == 4


# --- the two growth gates (#598) --------------------------------------------
# Built and seeded red BEFORE the count refusal was lifted, because the loader
# does not bound-check these arrays (ADR-0004 decision 28): a count above
# capacity is not a refusal, it is memory corruption. **Neither gate is
# reachable on MAP022** -- it has no animated mesh and its 24/361/18/51 sit far
# under 360/710/64/256 -- so both are graded here and by `blender_live_push.py`
# fake RAM, and NOT by the emulator. That is said out loud wherever they are
# claimed green.

def test_the_addons_capacity_constants_are_the_packages():
    """A copy, deliberately (ADR-0004 §7 keeps the addon off the package), and
    `test_build.py::test_corpus_maxima_still_hold` recomputes the package's from
    the disc -- so this is what stops the two drifting apart silently."""
    schema = pytest.importorskip("exmateria_map.document")
    assert live_link.ENGINE_CAPACITY == dict(schema.ENGINE_CAPACITY)
    assert live_link.CORPUS_MAX == dict(schema.CORPUS_MAX)


def _block(primary, animated=None) -> bytes:
    """A descriptor block written by hand -- not by the module under test."""
    block = bytearray(live_link.DESCRIPTOR_STRIDE * live_link.DESCRIPTOR_COUNT)
    for index, counts in [(0, primary)] + list(animated or []):
        at = index * live_link.DESCRIPTOR_STRIDE + live_link.DESCRIPTOR_COUNTS
        struct.pack_into("<4H", block, at, *counts)
    return bytes(block)


def test_growth_past_the_engines_array_is_refused_by_name():
    d = live_link.read_descriptors(_block((24, 361, 18, 51)))
    with pytest.raises(live_link.LiveLinkError, match="711.*710"):
        live_link.check_capacity(d, (24, 711, 18, 51))


def test_capacity_counts_the_animated_meshes_too():
    """`build` §10.4 bounds the SUM: the loader's destination cursors are
    shared across the primary mesh and its AnimatedMesh 1-8 and are never
    bound-checked. 650 documented quads is fine alone and is not fine on
    `MAP103.53724`, which parks 85 more in the same array."""
    d = live_link.read_descriptors(_block((24, 650, 18, 51),
                                          [(1, (0, 85, 0, 0))]))
    live_link.check_capacity(d, (24, 620, 18, 51))            # 705, fits
    with pytest.raises(live_link.LiveLinkError, match="735"):
        live_link.check_capacity(d, (24, 650, 18, 51))        # 735, does not


def test_above_the_corpus_maximum_warns_and_does_not_refuse():
    """Ground no shipped map has tested. `build` warns rather than refusing
    here and so does the push -- the same two-tier arithmetic on the same
    constants."""
    d = live_link.read_descriptors(_block((24, 361, 18, 51)))
    said = live_link.check_capacity(d, (24, 700, 18, 51))
    assert any("700" in w and "683" in w for w in said), said
    assert live_link.check_capacity(d, (24, 361, 18, 51)) == []


def test_growth_into_a_bucket_with_a_follower_is_refused_not_shoved():
    """Growing the primary shoves every FOLLOWING slice: its data must move and
    its start at +0x88 must be rewritten. No savestate in this repo reaches any
    of the twelve maps that have one, so the shove could only ever be verified
    against fake RAM -- and a refusal costs an artist a walk to `build` while a
    wrong shove is unbounded memory corruption."""
    d = live_link.read_descriptors(_block((24, 361, 18, 51),
                                          [(1, (0, 10, 0, 0))]))
    with pytest.raises(live_link.LiveLinkError, match="build"):
        live_link.check_followers(d, (24, 362, 18, 51))


def test_the_follower_gate_is_PER_BUCKET():
    """The four arrays are independent, and a follower in one says nothing
    about another. No shipped resource animates untextured_triangle at all."""
    d = live_link.read_descriptors(_block((24, 361, 18, 51),
                                          [(1, (0, 10, 0, 0))]))
    live_link.check_followers(d, (25, 361, 19, 52))     # grew the other three


def test_shrinking_a_bucket_with_a_follower_is_allowed():
    """Nothing moves on a shrink -- the slots past the end stop being drawn and
    every follower's +0x88 stays valid. Refusing here would cost the artist the
    one direction that is safe on all twelve."""
    d = live_link.read_descriptors(_block((24, 361, 18, 51),
                                          [(1, (0, 10, 0, 0))]))
    live_link.check_followers(d, (24, 300, 18, 51))


def test_the_refusal_names_the_live_counts_and_points_at_build():
    d = live_link.read_descriptors(_block((24, 361, 18, 51),
                                          [(3, (0, 10, 0, 0))]))
    with pytest.raises(live_link.LiveLinkError) as e:
        live_link.check_followers(d, (24, 400, 18, 51))
    said = str(e.value)
    assert "textured_quad" in said and "361" in said and "400" in said
    assert "build" in said and "descriptor 3" in said


def test_gariland_can_grade_NEITHER_gate(descriptors):
    """Said out loud, because both gates are claimed green off fake RAM alone.
    MAP022 a0 has no animated mesh and its counts sit far under the engine's
    arrays, so the only map this repo holds a savestate for cannot reach either
    refusal from any document an artist could author."""
    d = live_link.read_descriptors(descriptors)
    assert all(x.is_empty() for x in d[1:]), "MAP022 a0 grew a follower"
    for bucket, count in zip(live_link.BUCKETS, d[0].counts):
        assert count < live_link.CORPUS_MAX[bucket]


def test_the_unpushed_key_names_the_terrain_GRID_not_the_binding():
    """CONTEXT.md's *Binding vs the terrain grid*. A push writes 454 terrain
    BINDINGS on MAP022 a0 and cannot touch the grid, so "not pushed: terrain"
    printed on that press tells the artist a feature that works is broken --
    and "terrain" unqualified could mean either one."""
    assert "terrain" not in live_link.UNPUSHED
    assert "BINDING" in live_link.UNPUSHED["the terrain grid"]
    assert "unknown_untextured" in " ".join(live_link.UNPUSHED)


def test_growth_reads_the_HELD_packet_bytes_of_the_slots_it_grows_into():
    """A slot past the loaded count is not a slot with nothing in it.

    The engine's packet array is fixed-capacity and still holds whatever the
    last load -- or the last push -- left there; the count only says how many
    of them are DRAWN. Sizing the read off the descriptor's count made every
    grown slot read `held = 0`, so `palette_id` went in with the CLUT's 0x7800
    row bits cleared, and every one of those faces pointed at VRAM row 0.

    Caught by the live A/B/A and by nothing else. The byte count reported the
    third press as **0 changed bytes**, truthfully -- RAM did hold what the
    plan said, and the plan was wrong. Gariland came back geometrically whole
    and wearing the wrong palettes, half the map gone blue. That is a picture,
    not a number, which is why the acceptance is a render.
    """
    class _Client:
        def __init__(self):
            self.reads = []

        def read(self, address, length):
            self.reads.append((address, length))
            if address == live_link.PACKET_BASE_POINTER:
                return struct.pack("<I", live_link.PACKET_BASES[0])
            return bytes(length)

    d = live_link.Descriptor(index=0, starts=(0, 2, 0, 0), counts=(0, 3, 0, 0))
    doc = {"polygons": [{"kind": "textured_quad", "palette_id": 1,
                         "texture_page": 2,
                         "uv": [[0, 0], [1, 1], [2, 2], [3, 3]]}
                        for _ in range(9)]}
    client = _Client()
    live_link.plan_packets_document(client, d, doc)
    stride = live_link.SINKS["textured_quad"].packet_stride
    packet_reads = [n for a, n in client.reads if n != 4]
    assert packet_reads, client.reads
    assert all(n >= stride * 9 for n in packet_reads), packet_reads


def test_a_grown_slots_masked_field_still_keeps_the_engines_bits():
    """The other half of the same defect, at the `plan_packets` seam: given the
    held bytes, growth must mask exactly as an existing slot does."""
    d = live_link.Descriptor(index=0, starts=(0, 0, 0, 0), counts=(0, 1, 0, 0))
    stride = live_link.SINKS["textured_quad"].packet_stride
    clut = live_link.PACKET_LAYOUT["textured_quad"].clut
    current = bytearray(stride * 2)
    struct.pack_into("<H", current, clut, 0x7805)
    struct.pack_into("<H", current, stride + clut, 0x7803)
    writes = live_link.plan_packets(d, live_link.PACKET_BASES[0],
                                    "textured_quad", "palette_id",
                                    [{"palette_id": 9}, {"palette_id": 9}],
                                    bytes(current))
    assert [struct.unpack("<H", b)[0] for _, b in writes] == [0x7809, 0x7809]


# --- the self-check's DIAGNOSIS (#598 follow-up) -----------------------------
# It used to raise on the FIRST plan whose candidates both mismatched, and it
# iterated in sorted order -- metadata, normals, positions. So an emulator
# holding a previous session's baked normals failed at `normals` and reported
# "the loaded map is not this document's map" while holding, unexamined, the
# proof that it is. Measured on a live battle: positions 0 of 8,664 differ,
# metadata 0 of 1,444, normals 7,589 of 8,664.

def _results(**kw):
    """`{(bucket, field): (matched, differ, total)}`, the shape the UI builds."""
    return {("textured_quad", f): spec for f, spec in kw.items()}


def test_a_clean_selfcheck_passes_and_names_what_it_found():
    ok, lines = live_link.diagnose_selfcheck(_results(
        positions=("the base map's own bytes", 0, 8664),
        normals=("the base map's own bytes", 0, 8664),
        metadata=("the base map's own bytes", 0, 1444)))
    assert ok
    assert any("base map" in ln for ln in lines)


def test_normals_alone_differing_is_a_PREVIOUS_PUSH_and_proceeds():
    """The whole point. Positions and metadata matching at the addresses this
    module computed IS the arithmetic proof the check exists to get; normals
    differing on top of that says somebody pushed lighting here, which is the
    one cause that is harmless. Refusing walls the artist in, because
    `_LAST_PUSH` is only recorded on a SUCCESSFUL push -- so a refusal can
    never establish the memory that would let the next press through."""
    ok, lines = live_link.diagnose_selfcheck(_results(
        positions=("the base map's own bytes", 0, 8664),
        metadata=("the base map's own bytes", 0, 1444),
        normals=(None, 7589, 8664)))
    assert ok, lines
    said = " ".join(lines)
    assert "normals" in said
    assert "already" in said.lower() or "previous" in said.lower()


def test_it_does_NOT_claim_the_wrong_map_when_positions_matched_exactly():
    """The sentence that sent this back. Positions and metadata matching to the
    byte is incompatible with 'the loaded map is not this document's map', and
    saying it anyway sends the artist to reload a savestate that was never the
    problem."""
    _ok, lines = live_link.diagnose_selfcheck(_results(
        positions=("the base map's own bytes", 0, 8664),
        metadata=("the base map's own bytes", 0, 1444),
        normals=(None, 7589, 8664)))
    said = " ".join(lines).lower()
    assert "not this document's map" not in said
    assert "arithmetic is wrong" not in said


def test_positions_differing_is_still_refused():
    """Geometry at the computed addresses not holding the document's own bytes
    is the case the check was built for, and it stays a refusal."""
    ok, lines = live_link.diagnose_selfcheck(_results(
        positions=(None, 4000, 8664),
        metadata=(None, 900, 1444),
        normals=(None, 7589, 8664)))
    assert not ok
    said = " ".join(lines)
    assert "arithmetic" in said and "reload the savestate" in said


def test_the_refusal_reports_EVERY_plan_not_the_first_one_to_fail():
    """Three fields, three different stories. Reporting only the first meant
    the pessimistic one won on alphabetical order."""
    ok, lines = live_link.diagnose_selfcheck(_results(
        positions=(None, 4000, 8664),
        metadata=("the base map's own bytes", 0, 1444),
        normals=(None, 7589, 8664)))
    said = " ".join(lines)
    assert not ok
    assert "4,000" in said and "7,589" in said and "1,444" in said


# --- the SWAP mode's proof: bounds, not content ------------------------------
# `selfcheck` demands RAM ALREADY HOLDS the document's own bytes. That is the
# identity claim decision 7 recovered as a side effect, and it is exactly what
# replacing the loaded map violates on purpose. Deleting it is not on the
# table -- it is what catches an off-by-one in a stride, a vertex offset or a
# field mask before thousands of bytes go to a guessed address. What is on the
# table is a WEAKER proof that a swap can still pass: every planned address
# lands inside the array it names.


def test_the_array_extents_land_exactly_where_the_disassembly_says():
    """The oracle for the bound, and it is not a restatement of it.

    `SINKS`' six bases were measured one at a time against a live battle;
    `ENGINE_CAPACITY`'s four numbers are the `slti` immediates at
    `0x800F2A68`, `0x800F2BE4`, `0x800F2C2C` and `0x800F2C50`; the strides are
    the vertex counts. Three separately-sourced sets of constants, and each
    array's END has to land on the next thing that was measured -- the
    textured-quad normal array on `FUN_8012cc54`'s first byte, which is why
    that address is in the module at all. A wrong capacity or a wrong stride
    breaks the chain here rather than 22,720 bytes into somebody's map.
    """
    end = lambda b, f: live_link.array_extent(b, f)[1]
    assert end("textured_triangle", "positions") == \
        live_link.SINKS["textured_quad"].positions
    assert end("untextured_triangle", "positions") == \
        live_link.SINKS["untextured_quad"].positions
    assert end("textured_triangle", "normals") == \
        live_link.SINKS["textured_quad"].normals
    assert end("textured_quad", "normals") == \
        live_link.TEXTURED_TRIANGLE_RENDERER


def test_metadata_is_bounded_by_the_POSITION_array_it_lives_in():
    """Bytes 6-7 of a position vertex, not an array of its own."""
    assert live_link.array_extent("textured_quad", "metadata") == \
        live_link.array_extent("textured_quad", "positions")


def test_a_plan_that_runs_off_the_end_of_its_array_is_refused():
    """Ten quads planned from slot 705 of a 710-slot array.

    `check_capacity` cannot see this. It grades the DESCRIPTOR's counts, and a
    plan built at a wrong origin carries counts that fit perfectly -- which is
    the whole class of bug the content self-check was standing in front of.
    """
    stride = live_link.POLYGON_STRIDE["textured_quad"]
    origin = live_link.SINKS["textured_quad"].positions + 705 * stride
    plans = {("textured_quad", "positions"):
             live_link.plan_at(origin, "textured_quad", [[(0, 0, 0)] * 4] * 10)}
    with pytest.raises(live_link.LiveLinkError,
                       match="textured_quad positions"):
        live_link.check_plan_bounds(plans)


def test_a_legal_swap_plan_passes_and_the_line_COUNTS_what_it_proved():
    """A bound that reported nothing would read exactly like a bound that
    checked nothing, which is the failure mode `interpret` exists for one
    field over. Ten quads of four vertices is 240 planned bytes -- the
    document's shape, not the plan's own len().
    """
    d = live_link.Descriptor(index=0, starts=(0, 5, 0, 0),
                             counts=(0, 361, 0, 0))
    plans = {("textured_quad", "positions"):
             live_link.plan(d, "textured_quad", "positions",
                            [[(1, 2, 3)] * 4] * 10)}
    said = " ".join(live_link.check_plan_bounds(plans))
    assert "240" in said, said
    assert "textured_quad positions" in said, said


def test_the_bounds_proof_names_what_it_CANNOT_catch():
    """A weaker check reported in the same words as the strong one is worse
    than no check at all -- the artist reads "self-check passed" and believes
    the thing that was not proved. So the line has to say, in the artist's
    terms, which class of bug walks straight through it: the strides, vertex
    offsets and field masks `selfcheck` was standing in front of, all of which
    can be wrong and still land inside the array.
    """
    d = live_link.Descriptor(index=0, starts=(0, 0, 0, 0),
                             counts=(0, 361, 0, 0))
    plans = {("textured_quad", "positions"):
             live_link.plan(d, "textured_quad", "positions",
                            [[(1, 2, 3)] * 4] * 10)}
    said = " ".join(live_link.check_plan_bounds(plans)).lower()
    assert "stride" in said, said
    assert "weaker" in said, said
    assert "self-check passed" not in said, said


# ---------------------------------------------------------------------------
# The palette leg: `map_states[].palettes`, and its sink is main RAM.
# ---------------------------------------------------------------------------

def _palettes(n=16, entries=16):
    """The DOCUMENT's shape, from `docs/interchange-schema-v1.md` §6.4:
    `{"colors": ["#RRGGBB" x 16], "stp": u16}` per CLUT -- **not** the raw
    BGR555 words `mapfile.read_palettes` returns.

    Getting this wrong is not hypothetical: the first version of these tests
    invented the disc reader's shape, every one of them passed, and the button
    raised `int() argument must be a string...` on the first real document. A
    fixture is only an oracle if it comes from the spec.
    """
    return [{"colors": [f"#{(r * 16 + c) * 3 % 256:02X}0000"
                        for c in range(entries)], "stp": 0}
            for r in range(n)]


def _raw_palettes(n=16, entries=16):
    """The DISC's shape -- what `mapfile.read_palettes` returns."""
    return [[(r * 16 + c) | 0x8000 for c in range(entries)] for r in range(n)]


def test_the_palette_block_is_planned_as_one_write_per_declared_row():
    """The CLUT block is 16 rows of 16 BGR555 words at `CLUT_BLOCK`, and the
    engine re-uploads it to VRAM's y=480 every frame. Measured [LIVE] on a
    Gariland battle 2026-08-26: a write to VRAM's CLUT rows is reverted within
    50 ms, and the identical write to this RAM block holds and reaches the
    screen. Row-at-a-time rather than one 512-byte write because a row is what
    a refusal, a readback and an animation all happen to."""
    writes = live_link.plan_palettes(_palettes())
    assert [a for a, _ in writes] == [
        live_link.CLUT_BLOCK + i * 32 for i in range(16)]
    assert all(len(d) == 32 for _, d in writes)
    # The expected bytes come from the vendored writer `build` itself uses,
    # not from a second implementation of the hex->BGR555 packing here.
    from _vendor.exmateria_map.document import clut_from_json
    assert writes[0][1] == b"".join(
        w.to_bytes(2, "little") for w in clut_from_json(_palettes()[0]))


def test_a_state_that_declares_no_palettes_plans_nothing():
    """Decision 10. 38.5% of corpus states carry no palettes of their own and
    render with a keyed partner's, so `palettes: null` is a normal document --
    and its SHEET is still pushable, so refusing the press would be wrong."""
    assert live_link.plan_palettes(None) == []
    assert live_link.plan_palettes([]) == []


def test_a_short_clut_row_writes_only_the_entries_it_declares():
    """The entries a row does not declare are not ours to zero -- #496 settled
    that zero is the worst fill."""
    writes = live_link.plan_palettes([[0x8001, 0x8002, 0x8003]])
    assert len(writes) == 1
    assert writes[0] == (live_link.CLUT_BLOCK, b"\x01\x80\x02\x80\x03\x80")


def test_both_the_document_shape_and_the_disc_shape_are_accepted():
    """`map_states[].palettes` is `{"colors": [...], "stp": N}` and
    `mapfile.read_palettes` is raw BGR555 words. The push is driven from a
    document; the live probes and the corpus tools hold the other. Refusing
    either would just move the conversion somewhere less tested."""
    from _vendor.exmateria_map.document import clut_from_json
    doc_form = live_link.plan_palettes(_palettes())
    raw_form = live_link.plan_palettes(
        [clut_from_json(e) for e in _palettes()])
    assert doc_form == raw_form


def test_a_clut_entry_that_is_neither_shape_is_refused_by_name():
    """Not a crash inside `int()`. The button did exactly that on its first
    real document, and `int() argument must be a string` names nothing an
    artist or a maintainer can act on."""
    with pytest.raises(live_link.LiveLinkError) as exc:
        live_link.plan_palettes([{"colours": ["#000000"] * 16}])
    assert "CLUT row 0" in str(exc.value)


def test_the_clut_block_is_verified_against_vram_before_it_is_written():
    """Decision 5 at this sink. `CLUT_BLOCK` is one address, and a second copy
    of the same 512 bytes sits at 0x80099D76 -- measured, and writing THERE
    changes nothing on screen. So the address is not trusted for being written
    down: the block it names must match what the GPU is actually showing."""
    live = bytes(range(256)) * 2
    live_link.check_clut_block(live, live)                      # agrees: fine
    with pytest.raises(live_link.LiveLinkError) as exc:
        live_link.check_clut_block(bytes(512), live)
    assert "0x800E4EA4" in str(exc.value) or "CLUT" in str(exc.value)


def test_the_engine_animated_rows_are_excluded_from_the_comparison():
    """Rows the engine repaints will differ between a RAM read and a VRAM read
    taken microseconds apart, and that is not a mismatch worth refusing on --
    measured, rows 13-15 of MAP022 a0 move within 2 s while 0-12 do not. The
    check compares the rows that hold still, which is what makes it a check
    rather than a coin flip."""
    ram = bytearray(bytes(range(256)) * 2)
    vram = bytearray(ram)
    for row in (13, 14, 15):
        vram[row * 32] ^= 0xFF
    live_link.check_clut_block(bytes(ram), bytes(vram))          # tolerated
    vram[4 * 32] ^= 0xFF                                         # a STATIC row
    with pytest.raises(live_link.LiveLinkError):
        live_link.check_clut_block(bytes(ram), bytes(vram))


def _packet_ram(descriptor, document, clut_base=0x7800, tpage_base=12):
    """RAM holding the packets an engine WOULD hold for this document."""
    ram = {live_link.PACKET_BASE_POINTER: struct.pack(
        "<I", live_link.PACKET_BASES[0])}
    by_bucket = {b: [] for b in live_link.BUCKETS}
    for poly in document["polygons"]:
        by_bucket[poly["kind"]].append(poly)
    for bucket in live_link.PACKET_LAYOUT:
        polys = by_bucket[bucket]
        i = live_link.BUCKETS.index(bucket)
        sink = live_link.SINKS[bucket]
        stride = sink.packet_stride
        buf = bytearray(stride * max(descriptor.counts[i], len(polys)))
        lay = live_link.PACKET_LAYOUT[bucket]
        for p, poly in enumerate(polys):
            struct.pack_into("<H", buf, p * stride + lay.clut,
                             clut_base | poly["palette_id"])
            struct.pack_into("<H", buf, p * stride + lay.tpage,
                             tpage_base + poly["texture_page"])
        ram[live_link.PACKET_BASES[0] + sink.packet
            + descriptor.starts[i] * stride] = bytes(buf)
    return ram


def test_the_packet_witnesses_pair_every_live_halfword_with_its_document_field():
    """What `live_vram.derive_addresses` consumes. One tuple per textured
    polygon, and BOTH textured buckets -- a witness set drawn from triangles
    alone would agree about a sheet that quads disagreed with, and 361 of
    MAP022 a0's 385 textured polygons are quads."""
    doc = {"polygons":
           [{"kind": "textured_triangle", "palette_id": i % 16,
             "texture_page": i % 4} for i in range(24)]
           + [{"kind": "textured_quad", "palette_id": i % 16,
               "texture_page": i % 4} for i in range(361)]}
    desc = live_link.Descriptor(index=0, starts=(0, 0, 0, 0),
                                counts=(24, 361, 18, 51))
    wit = live_link.packet_witnesses(
        _FakeClient(_packet_ram(desc, doc)), desc, doc)
    assert len(wit) == 385
    assert {c - pid for c, _t, pid, _pg in wit} == {0x7800}
    assert {(t & 0xF) - pg for _c, t, _pid, pg in wit} == {12}


def test_a_document_with_nothing_textured_yields_no_witness():
    """It is `live_vram.derive_addresses` that refuses this, by name -- so what
    this must NOT do is invent one."""
    doc = {"polygons": [{"kind": "untextured_quad"}] * 4}
    desc = live_link.Descriptor(index=0, starts=(0, 0, 0, 0),
                                counts=(0, 0, 0, 4))
    assert live_link.packet_witnesses(
        _FakeClient(_packet_ram(desc, doc)), desc, doc) == []


# ---------------------------------------------------------------------------
# The RAM-over-HTTP transport (#606 part 1).
# ---------------------------------------------------------------------------

def test_adjacent_runs_coalesce_into_one_request():
    """`POST /api/v1/cpu/ram/raw` takes ONE contiguous run per request, and a
    geometry plan is thousands of six-byte runs. One POST each would be slower
    than the Lua path it replaces, so the win is entirely in coalescing: the
    runs are clustered, the gaps are filled from the before-image, and each
    cluster goes as a single request."""
    writes = [(live_link.RAM_BASE + 0, b"ab"),
              (live_link.RAM_BASE + 2, b"cd"),
              (live_link.RAM_BASE + 4, b"ef")]
    image = bytes(64)
    assert live_link.cluster_writes(writes, image, gap=0) == [
        (live_link.RAM_BASE + 0, b"abcdef")]


def test_a_gap_smaller_than_the_threshold_is_filled_from_the_before_image():
    """The bytes between two runs are not ours. They are written back exactly
    as read so the request is a no-op over them — which is what makes filling a
    gap safe, and also what bounds how big a gap may be."""
    writes = [(live_link.RAM_BASE + 0, b"ab"), (live_link.RAM_BASE + 6, b"cd")]
    image = bytes(range(64))
    out = live_link.cluster_writes(writes, image, gap=8)
    assert len(out) == 1
    address, data = out[0]
    assert address == live_link.RAM_BASE
    assert data == b"ab" + bytes(range(2, 6)) + b"cd"


def test_a_gap_larger_than_the_threshold_stays_two_requests():
    """The threshold is a collateral bound, not a tuning knob. Everything in a
    filled gap is read-modify-written, so a run that spanned megabytes would
    write back a stale copy of whatever the ENGINE changed in between — the one
    way this transport can be wrong where the Lua path cannot."""
    writes = [(live_link.RAM_BASE + 0, b"ab"), (live_link.RAM_BASE + 900, b"cd")]
    out = live_link.cluster_writes(writes, bytes(1024), gap=64)
    assert [a for a, _ in out] == [live_link.RAM_BASE, live_link.RAM_BASE + 900]


def test_clustering_refuses_a_write_outside_main_ram():
    """The same bound `pack_writes` enforces, kept when the transport changed.
    The endpoint 400s it too, but a 400 names neither the write nor the field."""
    with pytest.raises(live_link.LiveLinkError):
        live_link.cluster_writes([(live_link.RAM_BASE - 4, b"ab")], bytes(64))
    with pytest.raises(live_link.LiveLinkError):
        live_link.cluster_writes(
            [(live_link.RAM_BASE + live_link.RAM_BYTES - 1, b"ab")], bytes(64))


def test_out_of_order_and_overlapping_runs_are_ordered_before_clustering():
    """A plan is built per bucket and per field, so it arrives unsorted. The
    old Lua walk did not care; a cluster does — an unsorted plan would produce
    a negative-length gap and silently truncate."""
    writes = [(live_link.RAM_BASE + 4, b"ef"), (live_link.RAM_BASE + 0, b"ab"),
              (live_link.RAM_BASE + 2, b"cd")]
    assert live_link.cluster_writes(writes, bytes(64), gap=0) == [
        (live_link.RAM_BASE + 0, b"abcdef")]


class _FakeHttp:
    """The endpoint as a byte array. GET hands back the whole 2 MB, POST takes
    one contiguous run — which is the constraint the clustering exists for."""

    def __init__(self):
        self.ram = bytearray(live_link.RAM_BYTES)
        self.gets = self.posts = 0

    def get(self):
        self.gets += 1
        return bytes(self.ram)

    def post(self, offset, data):
        self.posts += 1
        assert 0 <= offset and offset + len(data) <= live_link.RAM_BYTES
        self.ram[offset:offset + len(data)] = data


def _ram_client(http):
    c = live_link.RamClient()
    c._get = http.get
    c._post = http.post
    return c


def test_the_http_client_writes_and_reports_the_bytes_that_CHANGED():
    """`apply`'s contract is unchanged across the transport swap: it returns
    bytes that changed, and the whole self-check leans on that number being
    zero when the engine already holds the plan. The Lua walk counted in the
    interpreter; here the GET has already provided the before-image, so the
    count is free."""
    http = _FakeHttp()
    client = _ram_client(http)
    writes = [(live_link.RAM_BASE + 0, b"ab"), (live_link.RAM_BASE + 2, b"cd")]
    assert client.write(writes) == 4
    assert http.ram[:4] == b"abcd"
    assert client.write(writes) == 0          # already there


def test_a_whole_plan_costs_one_get_and_one_post():
    """The headline. A bucket's plan is thousands of six-byte runs; the Lua
    path hex-encoded every one of them and looped in the interpreter."""
    http = _FakeHttp()
    client = _ram_client(http)
    writes = [(live_link.RAM_BASE + i * 8, b"abcdef") for i in range(500)]
    client.write(writes)
    assert (http.gets, http.posts) == (1, 1)


class _DeafHttp(_FakeHttp):
    """An emulator that takes a POST and does not honour it.

    The one shape a cache can launder: if a read-back after a write can be
    answered from bytes WE sent, the self-check passes on an engine that
    never took them.
    """

    def post(self, offset, data):
        self.posts += 1


def test_a_push_fetches_the_consoles_RAM_once_and_holds_it():
    """ADR-0186 Amendment 7 decision 32.  `GET /api/v1/cpu/ram/raw` always
    returns the whole of `m_wram` -- stock's `offset`/`size` are POST-only, so
    there is no range read to reach for -- and a push made roughly twenty of
    them between the descriptor read, the packet fields, the per-bucket
    before-images and the self-check.  About 40 MB moved to write tens of
    kilobytes.

    `hold()` is a scope rather than a cache with a lifetime, because the
    console is RUNNING: an image held past the push it was fetched for would
    answer the next push's descriptor read with the last push's RAM.
    """
    http = _FakeHttp()
    client = _ram_client(http)

    with client.hold():
        for offset in (0x000, 0x400, 0x800, 0xC00):
            client.read(live_link.RAM_BASE + offset, 4)

    assert http.gets == 1


def test_the_held_image_is_dropped_when_the_push_ENDS():
    """Two pushes are two images.  The console runs between them."""
    http = _FakeHttp()
    client = _ram_client(http)
    for _ in range(2):
        with client.hold():
            client.read(live_link.RAM_BASE, 4)
            client.read(live_link.RAM_BASE + 4, 4)
    assert http.gets == 2


def test_a_held_image_can_never_answer_a_read_back_with_OUR_OWN_bytes():
    """Decision 32: *"The self-check is not traded away for speed. It stays on
    every automatic push; this decision exists so that it can."*

    A write-through cache would be faster and would make the self-check a
    tautology -- it would compare the plan against the plan.  The console here
    accepts the POST and ignores it, which is exactly what a wrong address
    looks like, and the read-back has to report the console's bytes.
    """
    http = _DeafHttp()
    client = _ram_client(http)

    with client.hold():
        client.read(live_link.RAM_BASE, 4)          # fetches and holds
        client.write([(live_link.RAM_BASE, b"abcd")])
        got = client.read(live_link.RAM_BASE, 4)

    assert got == bytes(4), (
        "the read-back was answered from the held image; a self-check on this "
        "client would pass against an engine that took none of the plan")
    assert http.gets == 2, "a write must drop the image, not update it"


def test_ping_asks_the_console_even_inside_a_hold():
    """`ping` is a liveness question and a held image cannot answer one: the
    emulator can go away mid-push, and a cached 2 MB would say it had not."""
    http = _FakeHttp()
    client = _ram_client(http)
    with client.hold():
        client.read(live_link.RAM_BASE, 4)
        assert client.ping()
    assert http.gets == 2


def test_reads_come_from_the_same_window_the_writes_go_to():
    http = _FakeHttp()
    http.ram[0x400:0x408] = b"12345678"
    client = _ram_client(http)
    assert client.read(live_link.RAM_BASE + 0x400, 8) == b"12345678"


def test_a_read_outside_main_ram_is_refused_by_the_http_client_too():
    """The Lua client refuses this by name and the endpoint answers 400. The
    swap must not quietly downgrade a named refusal to a status code."""
    client = _ram_client(_FakeHttp())
    with pytest.raises(live_link.LiveLinkError):
        client.read(live_link.RAM_BASE - 1, 4)
    with pytest.raises(live_link.LiveLinkError):
        client.read(live_link.RAM_BASE + live_link.RAM_BYTES - 2, 8)


def test_apply_delegates_so_either_transport_drives_the_same_plan():
    """The point of the port: every caller of `apply` — geometry, metadata,
    packets, counts, palettes — is untouched, and the transport is a
    constructor argument. `apply_gte` is the one thing that cannot move: the
    GTE control registers are not `m_wram`, so that leg stays on Lua."""
    http = _FakeHttp()
    client = _ram_client(http)
    writes = [(live_link.RAM_BASE + 16, b"xyz")]
    assert live_link.apply(client, writes) == 3
    assert http.ram[16:19] == b"xyz"


# --- the stock path (#606 part 2) -------------------------------------------
# Everything below exists because the addon must run on an UNMODIFIED
# pcsx-redux. The one constraint that shapes it: on stock, a Lua handler
# receives its payload only through the URL -- a POST body is not exposed to
# Lua at all -- and the URL is capped, where overflowing it is a silent 404
# rather than an error. So these are length tests as much as transport tests.


class _FakeLua:
    """Enough of `LuaClient` to record what went over the wire.

    Records the *path*, not the pairs: the whole risk on this leg is a URL that
    got too long, and a fake that only remembered the writes could not see it.
    """

    def __init__(self, replies=None, raises=None):
        self.calls, self.replies, self.raises = [], replies, raises
        self.host, self.port = "localhost", 8080

    def call(self, handler, query="", timeout=30.0):
        if self.raises is not None:
            raise self.raises
        self.calls.append(f"/api/v1/lua/{handler}" + (f"?{query}" if query else ""))
        if self.replies is not None:
            return self.replies.pop(0)
        return str(len(query.split("&"))) + "\n"


def test_the_rig_reaches_the_gte_through_the_url_not_a_posted_body():
    """The headline of the stock port. `apply_gte` used to POST Lua source,
    which is the one thing a stock pcsx-redux cannot receive: `req.body` does
    not exist for a Lua handler, an urlencoded POST arrives with `req.form`
    empty, and a multipart POST hands over the part headers with the values
    concatenated. Measured, all four content types."""
    lua = _FakeLua()
    writes = [(13, 111), (14, 222), (15, 333), (16, 444),
              (17, 555), (18, 666), (19, 777), (20, 0xFFFFFFFF)]
    assert live_link.apply_gte(lua, writes) == 8
    assert lua.calls == ["/api/v1/lua/gte?13=111&14=222&15=333&16=444"
                         "&17=555&18=666&19=777&20=4294967295"]


def test_the_whole_light_rig_is_one_request_and_half_the_url_budget():
    """Eight registers is what `plan_rig_gte` emits, and it fits with room --
    which is the reason this leg is possible at all. If a rig change ever made
    this two requests the push still works; if it made one request too long it
    would 404 in silence, which is what `URL_LIMIT` exists to prevent."""
    lua = _FakeLua()
    live_link.apply_gte(lua, live_link.plan_rig_gte(_rig()))
    assert len(lua.calls) == 1
    assert len(lua.calls[0]) < live_link.URL_LIMIT // 2


def test_a_long_write_list_splits_by_MEASURED_length_not_a_pair_count():
    """A pair is 3 to 13 bytes wide depending on the value, so any fixed
    pairs-per-request either wastes the budget or -- the direction that
    matters -- occasionally overruns it. Every chunk must fit, including the
    path prefix the client will put in front of it."""
    writes = [(i % 32, 0xFFFFFFFF) for i in range(60)]
    queries = live_link.gte_queries(writes)
    assert len(queries) > 1
    for q in queries:
        assert len(f"/api/v1/lua/gte?{q}") <= live_link.URL_LIMIT
    rebuilt = "&".join(queries).split("&")
    assert rebuilt == [f"{i}={v}" for i, v in writes]


def test_a_url_past_the_ceiling_is_a_named_refusal_not_a_silent_404():
    """`BUFFER_SIZE = 256` in `web-server.cc` and `onUrl` parses each read
    chunk as a whole URI instead of accumulating, so an over-long request line
    resolves to a path that does not exist. Bisected on a live emulator: 251
    bytes runs the handler, 252 does not. Left to the server, a caller reads
    that as "the handler is missing" and goes looking for the launch flag."""
    client = live_link.LuaClient()
    with pytest.raises(live_link.LiveLinkError) as e:
        client.call("gte", "x" * live_link.URL_LIMIT)
    assert "silent 404" in str(e.value)


def test_a_dropped_register_is_an_error_and_not_a_half_applied_rig():
    """The handler skips anything its `%d+` cannot parse, in silence. An
    unchecked count is the difference between a rig that failed and a rig that
    half-applied and looked plausible on screen."""
    lua = _FakeLua(replies=["3\n"])
    with pytest.raises(live_link.TransportError) as e:
        live_link.apply_gte(lua, [(13, 1), (14, 2), (15, 3), (16, 4)])
    assert "3 of 4" in str(e.value)


def test_the_guards_that_make_the_query_string_safe_to_build_are_still_there():
    """Older than the transport and the reason it is safe: an index outside
    0-31 or a value outside 32 bits is refused before it reaches a URL. A
    negative in particular would not match the handler's `%d+` and would be
    dropped there without a word."""
    for bad in [(32, 1), (-1, 1), (13, -1), (13, 0x100000000)]:
        with pytest.raises(live_link.LiveLinkError):
            live_link.apply_gte(_FakeLua(), [bad])
    assert _FakeLua().calls == []


def test_the_ping_gate_tells_a_missing_handler_from_a_missing_emulator():
    """Three states, not two. `-dofile` is the step artists forget, and an
    emulator running without it answers every upstream endpoint perfectly and
    404s ours -- so folding that into "no emulator answering" misdiagnoses an
    emulator that is plainly on their screen."""
    ready = _FakeLua(replies=["pong\n"])
    assert live_link.LuaClient.check(ready) == ""

    missing = _FakeLua(raises=live_link.NoHandlerError("no `ping` handler"))
    assert "handler" in live_link.LuaClient.check(missing)

    absent = _FakeLua(raises=live_link.TransportError("refused"))
    assert "no emulator answering" in live_link.LuaClient.check(absent)


def test_the_launch_command_names_the_handler_file_that_ships_with_the_addon():
    """The route for an artist who starts the emulator their own way, so the
    addon has to be able to hand the whole line over. The path has to be the
    installed one -- an artist who unzipped the addon somewhere has no way to
    reconstruct it."""
    line = live_link.launch_command(9000)
    assert "-webserver-port 9000" in line
    assert line.endswith("pcsx_handlers.lua")
    assert Path(live_link.HANDLERS).is_file()


def test_the_shipped_handler_file_calls_nothing_the_fork_alone_has():
    """The whole point of shipping it. One fork-only binding in here and an
    artist's unmodified emulator stops answering -- and it would fail on THEIR
    machine, not on ours, where every fork binding resolves fine."""
    source = Path(live_link.HANDLERS).read_text()
    body = "\n".join(l for l in source.splitlines() if not l.startswith("--"))
    for fork_only in ("getLuaConsole", "PCSX.SPU", "PCSX.GPU."):
        assert fork_only not in body
    assert "req.body" not in body and "req.form" not in body
    for handler in ("H.ping", "H.gte"):
        assert handler in body
    assert "H.exec" not in body


# --- getting the handlers loaded without a terminal --------------------------
# Three routes, because the emulator loads nothing by itself. `launch_argv` is
# Blender starting it; the shim is the artist's own double-click working. The
# shim has two halves and one without the other accomplishes nothing, which is
# the property most of these arms are about.


def test_ONE_folder_answers_both_routes(tmp_path):
    """The artist is asked for a folder, not a folder and a binary. It works
    because the launch SETS the working directory to that same folder -- so
    "where the emulator lives" and "where it runs" are made to be one thing
    rather than being two questions with two answers to get wrong.

    The `cwd=` that makes that true is the operator's, not this function's;
    what is asserted here is that the argv it hands over came from the folder.
    """
    (tmp_path / "pcsx-redux").write_text("#!/bin/sh\n")
    (tmp_path / "pcsx-redux").chmod(0o755)
    argv = live_link.launch_argv(str(tmp_path), 9000)
    assert argv[0] == str(tmp_path / "pcsx-redux")
    assert argv[1:5] == ["-webserver", "-webserver-port", "9000", "-dofile"]
    assert argv[5] == live_link.HANDLERS
    # The shim goes in that same folder, and that is the whole point.
    assert Path(live_link.install_shim(str(tmp_path))).parent == tmp_path


def test_the_launch_argv_is_a_list_because_paths_can_hold_spaces(tmp_path):
    """`Program Files` is not an edge case. A string through a shell would need
    quoting rules; not having a shell needs none."""
    d = tmp_path / "My Emulators"
    d.mkdir()
    (d / "pcsx-redux").write_text("#!/bin/sh\n")
    (d / "pcsx-redux").chmod(0o755)
    assert live_link.launch_argv(str(d))[0] == str(d / "pcsx-redux")


def test_a_folder_with_no_emulator_in_it_is_named_not_guessed(tmp_path):
    """The launch is the only half that needs the executable -- the shim just
    needs somewhere to write. So this refusal names the folder and what was
    looked for, rather than letting `Popen` answer with a `FileNotFoundError`
    about a path the artist never typed."""
    with pytest.raises(live_link.LiveLinkError) as e:
        live_link.launch_argv(str(tmp_path))
    assert str(tmp_path) in str(e.value) and "pcsx-redux" in str(e.value)
    with pytest.raises(live_link.LiveLinkError):
        live_link.launch_argv("")
    # ...and the OTHER route still works in that same folder.
    assert live_link.install_shim(str(tmp_path))


def test_a_binary_that_is_not_executable_is_not_the_binary(tmp_path):
    """A README named `pcsx-redux`, or a download that never finished. Handing
    it to `Popen` produces a permission error naming a path, which reads as a
    broken button rather than a wrong folder."""
    (tmp_path / "pcsx-redux").write_text("not an emulator\n")
    (tmp_path / "pcsx-redux").chmod(0o644)
    assert live_link.find_binary(str(tmp_path)) == ""


def test_the_shim_loads_the_addons_handlers_rather_than_copying_them(tmp_path):
    """Two lines, not a copy: reinstalling the addon has to change what the
    emulator runs, and it cannot if the artist is running a snapshot taken the
    day they pressed the button."""
    path = live_link.install_shim(str(tmp_path))
    text = Path(path).read_text()
    assert Path(path).name == live_link.SHIM_NAME
    assert live_link.HANDLERS in text
    assert "Support.extra.dofile" in text
    assert "H.gte" not in text                 # loads them, does not carry them


def test_the_shim_refuses_to_clobber_a_pcsx_lua_it_did_not_write(tmp_path):
    """The one file in this flow holding something the addon cannot
    regenerate. PCSX-Redux's Lua editor has `Auto save` ON by default, so
    `pcsx.lua` is a document it writes back -- which means an existing one is
    probably the artist's own script, and overwriting it destroys work."""
    theirs = tmp_path / live_link.SHIM_NAME
    theirs.write_text("-- my own debugging script\nprint('hello')\n")
    with pytest.raises(live_link.LiveLinkError) as e:
        live_link.install_shim(str(tmp_path))
    assert "not ours" in str(e.value)
    assert theirs.read_text().startswith("-- my own")
    assert "Support.extra.dofile" in str(e.value)   # tells them what to add


def test_a_shim_we_wrote_is_rewritten_rather_than_refused(tmp_path):
    """Otherwise moving the addon strands the artist on a shim pointing at a
    path that no longer exists, with a button that refuses to fix it."""
    live_link.install_shim(str(tmp_path), handlers="/old/pcsx_handlers.lua")
    path = live_link.install_shim(str(tmp_path), handlers="/new/pcsx_handlers.lua")
    text = Path(path).read_text()
    assert "/new/" in text and "/old/" not in text


def test_install_shim_names_the_setting_rather_than_writing_a_stray_file():
    """An empty directory is not a path to write to -- it is the artist not
    having told us where their emulator runs."""
    with pytest.raises(live_link.LiveLinkError) as e:
        live_link.install_shim("")
    assert "PCSX-Redux folder" in str(e.value)


def test_enabling_the_lua_editor_changes_one_key_and_leaves_the_rest(tmp_path):
    """The other half of the shim, and the half that is somebody else's config
    file. Measured: with `ShowLuaEditor` off the emulator is up and answering
    `cpu/ram` while `lua/ping` is a 404 -- so the shim alone does nothing, and
    this must not be skipped as the invasive-looking step."""
    settings = tmp_path / "pcsx.json"
    settings.write_text(json.dumps({
        "gui": {"ShowLuaEditor": False, "ShowLuaConsole": False},
        "emulator": {"Debug": {"WebServer": True, "WebServerPort": 8080}}}))
    assert live_link.enable_lua_editor(str(settings)) is True
    data = json.loads(settings.read_text())
    assert data["gui"]["ShowLuaEditor"] is True
    assert data["gui"]["ShowLuaConsole"] is False          # untouched
    assert data["emulator"]["Debug"]["WebServerPort"] == 8080
    assert live_link.enable_lua_editor(str(settings)) is False   # idempotent


def test_missing_emulator_settings_are_named_not_created(tmp_path):
    """Writing a `pcsx.json` the emulator never wrote is how you hand it a
    settings file with one key in it. Ask them to start it once instead."""
    with pytest.raises(live_link.LiveLinkError) as e:
        live_link.enable_lua_editor(str(tmp_path / "nope.json"))
    assert "start it once" in str(e.value)
    assert not (tmp_path / "nope.json").exists()


# --- decision 12: the camera model, against the battle savestate ------------
# Decision 12 states the engine's camera is `R = Rx(pitch)*Ry(yaw)*Rz(roll)`,
# right-handed elementary rotations with POSITIVE signs, 4096 = 360 degrees.
# That claim was fitted twice -- to 65 live cinematic samples (F4 in
# `camera_framing_pivot_decode.md`) and again, offline and in a BATTLE, to the
# savestate below. Both fits then went into prose, which grades nothing.
#
# These are that fit as an assertion. The oracle is the savestate: the angles
# and the matrix are both read out of a frozen running battle, and nothing in
# `live_link` computes either one, so a composition that stops reproducing the
# engine's own matrix has nowhere to hide.
#
# The rivals are asserted too. A fit that only checks the winner cannot report
# that the winner stopped winning -- and one of the rivals here is the trap
# decision 12 names: `renames_high.tsv` mislabels the scratch struct's angle
# offsets, so composing from the LABELLED order (yaw at `+0x78`, roll at
# `+0x7C`) is a plausible wrong move that a winner-only test would pass.

#: `work_rotation` -- pitch, yaw, roll as three `short`s, 4096 = 360 degrees.
#: Yaw is stored UNWRAPPED: this battle holds 4608 = 4096 + 512 = 405 degrees.
CAMERA_ANGLES_AT = 0x800A7784

#: `camera_view_matrix` -- the engine's own composed R, nine `short`s at
#: 4096 = 1.0, read by both the map affine transform and the sprite projector.
CAMERA_MATRIX_AT = 0x80098A24

#: The composition has to land within the 4096-quantization floor, not exactly:
#: each entry is a product of two 12-bit fixed-point sines. The measured fit is
#: 0.00172 -- about seven LSB -- and this bar is eight.
MATRIX_FLOOR = 8 / 4096


def _matrix_error(got, want) -> float:
    """Largest entry-wise gap between a composed R and the engine's stored one.

    `want` is the raw 4096-scaled `short`s straight out of RAM.
    """
    return max(abs(got[i][j] - want[i * 3 + j] / 4096)
               for i in range(3) for j in range(3))


@live_ram
def test_the_battle_camera_is_Rx_Ry_Rz_with_POSITIVE_signs(gariland_ram):
    """The engine's own composed matrix, reproduced from the engine's own
    angles. Neither side of this is computed by the addon."""
    pitch, yaw, roll = struct.unpack_from(
        "<3h", gariland_ram, CAMERA_ANGLES_AT - live_link.RAM_BASE)
    stored = struct.unpack_from(
        "<9h", gariland_ram, CAMERA_MATRIX_AT - live_link.RAM_BASE)

    error = _matrix_error(live_link.camera_rotation(pitch, yaw, roll), stored)
    assert error < MATRIX_FLOOR, (
        f"Rx({pitch})*Ry({yaw})*Rz({roll}) misses the battle's own view "
        f"matrix by {error:.5f}, past the {MATRIX_FLOOR:.5f} quantization "
        f"floor")


@live_ram
def test_the_rival_rotation_ORDERS_do_not_reproduce_the_battles_matrix(
        gariland_ram):
    """`Ry*Rx*Rz` and `Rz*Ry*Rx` are the two orders a reader of the raw
    disassembly would reach for; both land two orders of magnitude out."""
    pitch, yaw, roll = struct.unpack_from(
        "<3h", gariland_ram, CAMERA_ANGLES_AT - live_link.RAM_BASE)
    stored = struct.unpack_from(
        "<9h", gariland_ram, CAMERA_MATRIX_AT - live_link.RAM_BASE)

    x = live_link.rotation_x(pitch)
    y = live_link.rotation_y(yaw)
    z = live_link.rotation_z(roll)
    rivals = {
        "Ry*Rx*Rz": live_link.mat3_multiply(live_link.mat3_multiply(y, x), z),
        "Rz*Ry*Rx": live_link.mat3_multiply(live_link.mat3_multiply(z, y), x),
    }
    for name, rival in rivals.items():
        error = _matrix_error(rival, stored)
        assert error > 0.1, (
            f"{name} reproduces the battle's matrix to {error:.5f} as well -- "
            f"this savestate can no longer separate the orders")


@live_ram
def test_a_FLIPPED_sign_on_either_angle_does_not_reproduce_it(gariland_ram):
    """Decision 12's signs are positive on both axes. Negating either one is
    reachable through this same function, so the test drives it that way."""
    pitch, yaw, roll = struct.unpack_from(
        "<3h", gariland_ram, CAMERA_ANGLES_AT - live_link.RAM_BASE)
    stored = struct.unpack_from(
        "<9h", gariland_ram, CAMERA_MATRIX_AT - live_link.RAM_BASE)

    for name, angles in (("pitch", (-pitch, yaw, roll)),
                         ("yaw", (pitch, -yaw, roll))):
        error = _matrix_error(live_link.camera_rotation(*angles), stored)
        assert error > 0.1, (
            f"negating {name} still reproduces the battle's matrix to "
            f"{error:.5f}; the sign convention is not pinned by this fit")


@live_ram
def test_the_scratch_struct_holds_the_pose_at_a_FOUR_byte_stride(gariland_ram):
    """The trap, and it is not the one decision 12 names.

    Decision 12 reports the scratch struct's angle offsets as MISLABELLED --
    `renames_high.tsv` calls `+0x74/78/7C` pitch/yaw/roll while the struct
    reads `[302, 0, 4608]` against a camera yaw of 4608, so "the yaw is at
    `+0x7C`, the slot labelled roll". That reading is at a TWO-byte stride.
    The fields are four bytes apart -- `renames_high.tsv` says so itself, in
    the aliases it gives for the same three fields (`camera_scratch_pitch`
    `0x80057790`, `_yaw` `0x80057794`, `_roll` `0x80057798`) -- and at that
    stride the struct reads `[302, 4608, 0]` and the labels are simply right.

    So the mislabelling was an artifact of the stride, and this test is the
    stride: it asserts the pose the scratch really holds AND reproduces the
    `[302, 0, 4608]` a two-byte read yields, so neither claim can be made
    against this savestate again without the other showing up beside it.
    """
    pose = struct.unpack_from(
        "<3i", gariland_ram,
        live_link.SCRATCH_ANGLES - live_link.RAM_BASE)
    live = struct.unpack_from(
        "<3h", gariland_ram, live_link.WORK_ROTATION - live_link.RAM_BASE)
    assert pose == live, (
        "the scratch struct's angles do not agree with `work_rotation` at a "
        "four-byte stride; the labels really are wrong")

    narrow = struct.unpack_from(
        "<3h", gariland_ram, live_link.SCRATCH_ANGLES - live_link.RAM_BASE)
    assert narrow == (302, 0, 4608), (
        "the two-byte read no longer yields decision 12's triple, so this "
        "savestate is not the one that finding was made against")


@live_ram
def test_the_scratch_struct_is_pinned_by_its_CONTENT_not_by_a_label(
        gariland_ram):
    """`0x8005771C` is reached through a pointer cell in the engine, so the
    base is worth confirming rather than trusting. Its position and zoom are
    byte-identical to `work_position` and `sprite_scale` in this battle, which
    is three independent words agreeing at once."""
    assert struct.unpack_from(
        "<I", gariland_ram,
        live_link.SCRATCH_STRUCT_PTR - live_link.RAM_BASE
    )[0] == live_link.SCRATCH_STRUCT

    assert struct.unpack_from(
        "<3i", gariland_ram, live_link.SCRATCH_POSITION - live_link.RAM_BASE
    ) == struct.unpack_from(
        "<3i", gariland_ram, live_link.WORK_POSITION - live_link.RAM_BASE)

    assert struct.unpack_from(
        "<i", gariland_ram, live_link.SCRATCH_ZOOM - live_link.RAM_BASE
    )[0] == struct.unpack_from(
        "<i", gariland_ram, live_link.SPRITE_SCALE - live_link.RAM_BASE)[0]


@live_ram
def test_the_LABELLED_order_read_at_the_WRONG_stride_is_what_fails(
        gariland_ram):
    """Decision 12's 0.948 is real -- it is what a two-byte read of the scratch
    struct composes to. Kept as an assertion because the number is what makes
    the stride finding above legible: get the stride wrong and the camera model
    looks broken rather than the read."""
    stored = struct.unpack_from(
        "<9h", gariland_ram, CAMERA_MATRIX_AT - live_link.RAM_BASE)
    narrow = struct.unpack_from(
        "<3h", gariland_ram, live_link.SCRATCH_ANGLES - live_link.RAM_BASE)
    error = _matrix_error(live_link.camera_rotation(*narrow), stored)
    assert 0.9 < error < 1.0, (
        f"the two-byte read composes to {error:.3f}, not the ~0.948 decision "
        f"12 measured")


def test_the_camera_sink_addresses_are_the_RE_RECORDS():
    """`live_link`'s sink constants against the addresses decision 12 cites.
    Both spellings are here on purpose: the tests above read RAM through the
    literals, so a drifted constant in the module would otherwise be graded by
    nothing at all."""
    assert live_link.WORK_ROTATION == CAMERA_ANGLES_AT
    assert live_link.CAMERA_VIEW_MATRIX == CAMERA_MATRIX_AT
    assert live_link.WORK_POSITION == 0x800E4E74
    assert live_link.SPRITE_SCALE == 0x800C7CA0
    assert live_link.CAMERA_TRACKED_TARGET == 0x800A77B0
    assert live_link.CAMERA_VERTICAL_DATUM == 0x800A77B4


@live_ram
def test_the_vertical_datum_is_the_engines_own_160(gariland_ram):
    """Decision 12's third part rests on `camera_tracked_target` reading
    `{256, 160, 640}` -- the `160` being why the optical centre lands at screen
    y=160 on a 240-line frame rather than at the midpoint 120. If that word is
    not 160 there is no 40-unit gap to correct and the whole correction is
    aimed at nothing."""
    target = struct.unpack_from(
        "<3i", gariland_ram,
        live_link.CAMERA_TRACKED_TARGET - live_link.RAM_BASE)
    assert target == (256, 160, 640)

    datum = struct.unpack_from(
        "<i", gariland_ram,
        live_link.CAMERA_VERTICAL_DATUM - live_link.RAM_BASE)[0]
    assert datum == target[1], "the datum constant does not address the 160"
    assert live_link.SCREEN_CENTRE_DATUM == 120
    assert datum - live_link.SCREEN_CENTRE_DATUM == 40, (
        "the gap the sync corrects is not the 40 world units decision 12 "
        "measured")


@live_ram
def test_camera_current_w_is_NOT_the_zoom_in_a_running_battle(gariland_ram):
    """`renames_high.tsv` offers `camera_current_w` (`0x801B8B04`) and a design
    was about to take it. It reads 0 in this battle -- the whole
    `saved`/`start`/`current` block is an idle effect save/restore slot -- while
    the live zoom is `sprite_scale`, at 1.0x. A push aimed at the labelled word
    would write a scale nothing reads."""
    current_w = struct.unpack_from(
        "<i", gariland_ram, 0x801B8B04 - live_link.RAM_BASE)[0]
    assert current_w == 0, (
        "camera_current_w is non-zero here; the reason this sink was rejected "
        "no longer holds")

    scale = struct.unpack_from(
        "<3i", gariland_ram, live_link.SPRITE_SCALE - live_link.RAM_BASE)
    assert scale == (4096, 4096, 4096), "the battle is not at 1.0x zoom"


# --- decision 12: a Blender view rotation becomes a pose --------------------
# The sync's one piece of real arithmetic. Blender hands over `view_rotation`,
# the rotation taking VIEW space to WORLD space, and the engine wants pitch and
# yaw in its own units. The two spaces disagree about everything: Blender's
# view space is +X right, +Y up, +Z toward the viewer; FFT's screen space is X
# right, Y DOWN, Z into the screen; and their world axes are related by the
# ratified frame above.
#
# There is no savestate for this half -- the savestate holds a pose the ENGINE
# authored, and nothing in it says which Blender view it corresponds to. So the
# oracle is geometry: the three axis-aligned viewports whose FFT pose can be
# worked out by hand, and then a spec that says what "synced" MEANS for every
# other view.

#: Blender's three axis-aligned viewports as `view_rotation` matrices. The
#: columns of each are the world directions of view +X (right), +Y (up) and
#: +Z (toward the viewer), which is what `view_rotation` is.
TOP_VIEW = ((1, 0, 0), (0, 1, 0), (0, 0, 1))
FRONT_VIEW = ((1, 0, 0), (0, 0, -1), (0, 1, 0))
RIGHT_VIEW = ((0, 0, 1), (1, 0, 0), (0, 1, 0))


def test_a_TOP_viewport_is_pitch_straight_down(gariland_ram=None):
    """Numpad 7. Blender looks down its own -Z; through `(x, z, -y)` that is
    FFT +Y, and PSX Y is DOWN -- so the camera is looking straight down and the
    pitch is a quarter turn, 1024. Yaw is nothing: the view's right is Blender
    +X, which is FFT +X already."""
    assert live_link.camera_angles(TOP_VIEW) == (1024, 0, 0)


def test_a_FRONT_viewport_is_the_engines_ZERO_pose():
    """Numpad 1. Blender looks along its own +Y, which is FFT +Z -- straight
    into the screen with no pitch and no yaw. This is the one view where every
    axis already agrees, so a sign error anywhere else still passes here; it is
    in the set as the fixed point, not as the interesting case."""
    assert live_link.camera_angles(FRONT_VIEW) == (0, 0, 0)


def test_a_RIGHT_viewport_is_a_quarter_turn_of_YAW():
    """Numpad 3. Blender looks along its own -X, which is FFT -X. For that to
    come out of the screen's +Z the camera has turned a quarter turn about the
    down axis: yaw 1024, pitch nothing. Negate the yaw and this lands on 3072."""
    assert live_link.camera_angles(RIGHT_VIEW) == (0, 1024, 0)


def _turntable(azimuth: float, elevation: float):
    """A Blender turntable orbit as a `view_rotation`: spin about Blender +Z
    (the world's up), then tilt about the view's own right axis. This is what
    the default navigation produces and it never rolls, which is the family the
    sync is defined on."""
    z = live_link.rotation_z(azimuth)
    x = live_link.rotation_x(elevation)
    return live_link.mat3_multiply(z, x)


def _apply(m, v):
    return tuple(sum(m[i][j] * v[j] for j in range(3)) for i in range(3))


def test_a_synced_pose_puts_the_viewports_own_AXES_where_they_belong():
    """The specification, on 96 turntable views at once.

    "Synced" means: whatever direction is to the RIGHT in the Blender viewport
    is to the right on the emulator's screen, whatever is UP is up, and
    whatever is going AWAY from the artist is going into the screen. Written
    out, that is three direction identities -- and they are the definition, not
    a second way of computing the answer, which is why they can disagree with
    the arithmetic.

    Screen +Y is DOWN and screen +Z is INTO the screen, so Blender's up and
    toward-the-viewer both come out negated.
    """
    want = {"right": (1, 0, 0), "up": (0, -1, 0), "toward the viewer": (0, 0, -1)}
    for azimuth in range(0, 4096, 256):
        for elevation in (128, 302, 512, 900, 1024, 1500):
            view = _turntable(azimuth, elevation)
            rotation = live_link.camera_rotation(*live_link.camera_angles(view))
            for axis, (name, expect) in enumerate(want.items()):
                in_view = tuple(1 if i == axis else 0 for i in range(3))
                in_world = _apply(view, in_view)
                on_screen = _apply(rotation, live_link.fft_from_blender(in_world))
                gap = max(abs(a - b) for a, b in zip(on_screen, expect))
                assert gap < 0.002, (
                    f"at azimuth {azimuth} elevation {elevation}, what is "
                    f"{name} in Blender lands at {on_screen} on the emulator's "
                    f"screen, not {expect}")


def test_ROLL_is_clamped_to_zero_rather_than_pushed():
    """Decision 12's exception, and the reason for it: FFT has a roll axis and
    has never used it, so `Rz`'s placement in the composition is assumed and
    never confirmed. A rolled Blender view is reachable -- trackball mode, or a
    view-align -- and the sync answers it with the nearest unrolled pose rather
    than making the artist the first person ever to drive an unverified path."""
    rolled = live_link.mat3_multiply(_turntable(0, 512),
                                     live_link.rotation_z(700))
    assert live_link.camera_angles(rolled)[2] == 0


# --- decision 12: zoom is a DIAL -------------------------------------------
# The emulator's frame is a fixed 256x240 and a Blender viewport is whatever
# shape the artist dragged it to, so the two can agree on at most one axis.
# Rather than pick one, the push derives a zoom from the Blender view distance
# and multiplies it by a factor the artist can turn -- which deliberately
# removes the one contested number in the whole camera model from the design.
# The RE record does not settle the horizontal store-to-pixel factor: F15
# measured 1:1, F20's decomposition implies 2x, and F19's finding was that
# godot's horizontal came out compressed 0.82x. Under a dial, nothing here
# depends on which is right.

def test_the_reference_distance_is_where_the_dial_reads_ONE_TIMES():
    """The dial's origin: at the reference distance, with the dial at rest, the
    push emits the engine's own 1.0x. Everything else is relative to this."""
    assert live_link.camera_zoom(live_link.ZOOM_REFERENCE_DISTANCE) == 4096


def test_zooming_IN_in_blender_zooms_in_on_the_emulator():
    """The relationship the artist actually feels, stated without reference to
    what the constant is: halve the view distance and the picture doubles.
    An implementation that got the sense backwards would still pass the test
    above."""
    near = live_link.camera_zoom(live_link.ZOOM_REFERENCE_DISTANCE / 2)
    far = live_link.camera_zoom(live_link.ZOOM_REFERENCE_DISTANCE * 4)
    assert near == 8192
    assert far == 1024


def test_the_dial_MULTIPLIES_what_the_view_distance_derived():
    """The dial calibrates the relationship once; it does not replace it.

    Driven at the reference distance so the assertion is about the dial and
    not about where the rounding of an awkward distance happens to land.
    """
    at_rest = live_link.camera_zoom(live_link.ZOOM_REFERENCE_DISTANCE)
    assert live_link.camera_zoom(
        live_link.ZOOM_REFERENCE_DISTANCE, 2.0) == 2 * at_rest
    assert live_link.camera_zoom(
        live_link.ZOOM_REFERENCE_DISTANCE, 0.5) == at_rest // 2


def test_a_zoom_past_the_games_own_ENVELOPE_is_not_clamped():
    """Decision 1: the pose is pushed faithfully. The pad can only reach
    0xC00-0x1000, 0.75x to 1.0x, and clamping to that would hand the artist
    back the same envelope that makes the map uninspectable in the first
    place. This is what a well-meaning bounds check would break."""
    close = live_link.camera_zoom(live_link.ZOOM_REFERENCE_DISTANCE / 8)
    assert close == 8 * 4096, "the push clamped a zoom into the pad's envelope"
    assert live_link.camera_zoom(live_link.ZOOM_REFERENCE_DISTANCE * 8) == 512


def test_a_DEGENERATE_view_distance_does_not_emit_a_zero_scale():
    """Blender will hand over a view distance of 0 -- it is what an orbit
    driven all the way in reads. A scale of 0 collapses the map to a point and
    an artist would read that as the sync being broken, so the guard is here
    and not in the panel. It is a guard against a degenerate view, NOT the
    envelope clamp the test above forbids."""
    for distance in (0.0, -1.0, 1e-9):
        raw = live_link.camera_zoom(distance)
        assert raw > 0, f"view distance {distance} emitted scale {raw}"
        assert raw <= live_link.ZOOM_RAW_MAX


# --- decision 12: the pose, and the plan that writes it --------------------
# The pose is the three things the engine needs -- where the camera is aimed,
# which way it is turned, how much of the world it holds -- in the engine's own
# raw words. It is a value rather than a write so that the continuous leg can
# ask "has this changed?" without touching the emulator.

TOP_OF_GARILAND = dict(
    pivot=(182.0, 154.0, -4.75),        # the battle's own optical centre
    view_rotation=TOP_VIEW,
    view_distance=336.0,                # ZOOM_REFERENCE_DISTANCE, so 1.0x
)


def test_a_pose_is_the_three_things_the_engine_needs():
    """Every word here is a literal, not a re-derivation: the position is the
    savestate's own `work_position`, the angles are the hand-worked top view,
    and the zoom is the engine's 1.0x."""
    pose = live_link.camera_pose(**TOP_OF_GARILAND)
    assert pose.position == (745472, 19456, 630784)
    assert pose.angles == (1024, 0, 0)
    assert pose.zoom == 4096


def test_the_plan_writes_the_pose_at_the_ENGINES_widths():
    """`work_position` is three WORDS and `work_rotation` is three SHORTS. A
    plan that packed the angles as words would put the yaw eight bytes past
    where anything reads it, and the map would still be there, turned wrong."""
    plan = dict(live_link.plan_camera(live_link.camera_pose(**TOP_OF_GARILAND)))

    assert plan[live_link.WORK_POSITION] == struct.pack(
        "<3i", 745472, 19456, 630784)
    assert plan[live_link.WORK_ROTATION] == struct.pack("<3h", 1024, 0, 0)
    assert plan[live_link.SPRITE_SCALE] == struct.pack("<3i", 4096, 4096, 4096)


def test_the_plan_pokes_the_engines_own_VERTICAL_DATUM():
    """Decision 12's third part, as a write. Uncorrected, a pose that is right
    in every other respect still leaves the two views 40 world units apart
    vertically, because FFT frames the action two thirds down the frame -- the
    artist's reported symptom, surviving everything else being right."""
    plan = dict(live_link.plan_camera(live_link.camera_pose(**TOP_OF_GARILAND)))
    assert plan[live_link.CAMERA_VERTICAL_DATUM] == struct.pack("<i", 120)


def test_the_SCRATCH_sink_writes_its_angles_four_bytes_apart():
    """The stride finding, as shipped behaviour rather than as a comment. The
    scratch struct's angles are word slots; `work_rotation`'s are shorts. Both
    plans exist because which one survives a live battle is the one thing
    decision 12 leaves open, and a plan that got this width wrong would answer
    that A/B with a false negative."""
    pose = live_link.camera_pose(**TOP_OF_GARILAND)
    scratch = dict(live_link.plan_camera(pose, live_link.CAMERA_SINK_SCRATCH))

    assert scratch[live_link.SCRATCH_ANGLES] == struct.pack("<3i", 1024, 0, 0)
    assert scratch[live_link.SCRATCH_POSITION] == struct.pack(
        "<3i", 745472, 19456, 630784)
    assert scratch[live_link.SCRATCH_ZOOM] == struct.pack("<i", 4096)
    assert live_link.WORK_POSITION not in scratch, (
        "the scratch plan also writes `work_position`, so an A/B between them "
        "cannot tell which sink carried the picture")


def test_the_default_sink_is_work_position_and_the_A_B_is_NAMED():
    """F14 measured `work_position` sticking and camera-scratch pokes not, so
    that is the default. Both are offered because F14 was measured on a
    cinematic, where an interpolator is running, and the artist's loop is a
    battle idle."""
    assert live_link.CAMERA_SINK_DEFAULT == live_link.CAMERA_SINK_WORK
    pose = live_link.camera_pose(**TOP_OF_GARILAND)
    assert live_link.plan_camera(pose) == live_link.plan_camera(
        pose, live_link.CAMERA_SINK_WORK)
    with pytest.raises(live_link.LiveLinkError, match="sink"):
        live_link.plan_camera(pose, "gte")


def test_two_identical_views_make_an_identical_POSE():
    """What lets the continuous leg skip a write. A pose is a value."""
    assert (live_link.camera_pose(**TOP_OF_GARILAND)
            == live_link.camera_pose(**TOP_OF_GARILAND))


# --- decision 12: the one thing the sync refuses ---------------------------

def test_a_FREE_viewport_syncs_whether_it_is_ortho_or_perspective():
    """The ortho toggle is a prerequisite -- FFT is orthographic, so in a
    perspective viewport no arithmetic can make the pictures match -- but it is
    NOT forced. The addon does not reach in and change a view the artist set,
    and the toggle is its own indicator, which is what the panel's rule
    demands: *"you are putting console stuff in the ui area"*."""
    live_link.check_view_syncable("ORTHO")          # must not raise
    live_link.check_view_syncable("PERSP")


def test_looking_through_a_SCENE_CAMERA_is_refused_and_says_so():
    """`view_location` and `view_rotation` then describe the last FREE view,
    not what is on screen. Pushing them would sync the emulator to a viewport
    the artist is not looking at -- a stale pose that looks exactly like the
    bug this feature exists to fix."""
    with pytest.raises(live_link.LiveLinkError) as e:
        live_link.check_view_syncable("CAMERA")
    assert "scene camera" in str(e.value)


# --- decision 12: the automatable half of "did the poke stick" -------------
# A byte readback of a write is a readback of your own bytes. This is not that:
# `CAMERA_VIEW_MATRIX` is rebuilt by the engine every frame from
# `work_rotation`, so requiring it to equal the matrix the pushed pose implies
# is BEHAVIOURAL -- the engine did the composing. It is the same distinction
# decision 11 paid for, where a byte readback passed a dead animation.

@live_ram
def test_the_engines_own_matrix_AGREES_with_the_pose_that_produced_it(
        gariland_ram):
    """The savestate is a push that already landed: the angles at
    `work_rotation` and the matrix at `CAMERA_VIEW_MATRIX` are the engine's
    own, one composed from the other."""
    stored = gariland_ram[live_link.CAMERA_VIEW_MATRIX - live_link.RAM_BASE:][:18]
    angles = struct.unpack_from(
        "<3h", gariland_ram, live_link.WORK_ROTATION - live_link.RAM_BASE)
    agrees, error = live_link.camera_readback(stored, angles)
    assert agrees, error


@live_ram
def test_a_pose_that_never_LANDED_is_reported_as_disagreeing(gariland_ram):
    """The arm that matters. If the write goes to a sink the engine does not
    rebuild from -- which is the one thing decision 12 leaves open -- the
    matrix still holds the pose the game itself is using, and this is what
    says so instead of a green button over an unchanged picture."""
    stored = gariland_ram[live_link.CAMERA_VIEW_MATRIX - live_link.RAM_BASE:][:18]
    agrees, error = live_link.camera_readback(stored, (302, 2048, 0))
    assert not agrees
    assert error > 0.1


# --- the continuous sync's decisions, decision 12 part 2 -------------------
#
# The timer itself needs `bpy` and a socket; what it DECIDES needs neither, so
# the decisions live here as a pure object and are graded here. Every test
# below is a defect the ticker could plausibly ship: pushing an unchanged view,
# forgetting that a failed write never landed, or saying the same thing sixty
# times a second.

def _pose(pitch, yaw=0, x=0):
    return live_link.CameraPose(position=(x, 0, 0), angles=(pitch, yaw, 0),
                                zoom=4096)


def test_an_unchanged_view_is_not_pushed_again():
    """A still viewport must cost no traffic at all.

    This is what makes decision 12's "ON by default costs nothing" true. A
    ticker that pushes every tick is 20 writes a second into a running battle
    for a viewport nobody is touching."""
    t = live_link.CameraSyncTicker()
    p = _pose(300)
    assert t.wants(p)
    t.succeeded(p)
    assert not t.wants(p)


def test_a_moved_view_is_pushed_again():
    t = live_link.CameraSyncTicker()
    t.succeeded(_pose(300))
    assert t.wants(_pose(301))


def test_a_FAILED_write_does_not_count_as_pushed():
    """The retry is the whole point: an emulator that was not up yet must get
    the pose the moment it is, without the artist having to nudge the view."""
    t = live_link.CameraSyncTicker()
    p = _pose(300)
    assert t.wants(p)
    t.failed("connection refused")
    assert t.wants(p)


def test_a_failure_is_announced_ONCE_not_once_per_tick():
    """At 20 Hz an unguarded report is 1,200 identical lines a minute in the
    console and in the Log -- which is how a surface that is meant to carry
    provenance becomes the thing you turn off."""
    t = live_link.CameraSyncTicker()
    first = t.failed("connection refused")
    assert any("connection refused" in ln for ln in first)
    assert t.failed("connection refused") == []
    assert t.failed("connection refused") == []


def test_a_failure_BACKS_OFF_the_tick_rate():
    t = live_link.CameraSyncTicker()
    assert t.interval() == live_link.CAMERA_SYNC_INTERVAL
    t.failed("connection refused")
    assert t.interval() == live_link.CAMERA_SYNC_BACKOFF
    assert live_link.CAMERA_SYNC_BACKOFF > live_link.CAMERA_SYNC_INTERVAL


def test_a_recovery_restores_the_rate_and_says_so_ONCE():
    t = live_link.CameraSyncTicker()
    t.failed("connection refused")
    back = t.succeeded(_pose(300))
    assert any("again" in ln for ln in back)
    assert t.interval() == live_link.CAMERA_SYNC_INTERVAL
    assert t.succeeded(_pose(301)) == []


def test_a_success_with_no_failure_behind_it_says_NOTHING():
    """The sync is meant to be invisible while it works. Only the transitions
    are worth a line."""
    t = live_link.CameraSyncTicker()
    assert t.succeeded(_pose(300)) == []


def test_reset_forgets_the_pose_so_re_enabling_pushes_at_once():
    """Toggling the sync off and on again must resend, because the battle's
    camera moved on its own while it was off."""
    t = live_link.CameraSyncTicker()
    p = _pose(300)
    t.succeeded(p)
    assert not t.wants(p)
    t.reset()
    assert t.wants(p)


def test_an_IDLE_reason_is_announced_once_and_does_not_back_off():
    """Looking through a scene camera is not an emulator problem, so it must
    not slow the tick down -- the artist leaves camera view and the sync has to
    be live again on the next frame, not two seconds later."""
    t = live_link.CameraSyncTicker()
    said = t.idle("the viewport is looking through a camera")
    assert any("camera" in ln for ln in said)
    assert t.idle("the viewport is looking through a camera") == []
    assert t.interval() == live_link.CAMERA_SYNC_INTERVAL


def test_leaving_an_idle_state_lets_it_be_announced_again():
    """A once-only report keyed on nothing would go silent forever after the
    first time; it is keyed on the STATE CHANGING."""
    t = live_link.CameraSyncTicker()
    t.idle("looking through a camera")
    t.succeeded(_pose(300))
    assert t.idle("looking through a camera") != []


# --- the transport leaves the main thread (live-link amendment) ------------
# MEASURED against the running emulator on 2026-08-29: EVERY request to
# pcsx-redux costs a fixed ~32 ms service wait -- a 404 that does no work at
# all is as expensive as a 2 MB RAM read, and the 2 MB body itself streams in
# 0.5 ms. A changed-pose tick is FOUR requests, so it takes ~128 ms against a
# 50 ms timer period, and it takes them on Blender's own thread. That is the
# artist orbiting into a frozen UI.
#
# The flight slot is what makes the worker safe. It is graded here, with no
# `bpy` and no socket, for the same reason the rest of the ticker is.


def test_a_write_IN_FLIGHT_coalesces_the_next_pose_rather_than_queueing_it():
    """A queue would send the emulator a pose the artist has already orbited
    past -- the same rule `background_push_start` follows for the sheet.

    One slot: the second claim is refused, and the tick that is refused simply
    drops its pose. The NEXT tick offers the latest one instead, so what the
    emulator gets is always where the viewport is now."""
    t = live_link.CameraSyncTicker()
    assert t.begin(_pose(300)) is True
    assert t.begin(_pose(301)) is False
    assert t.begin(_pose(302)) is False


def test_the_pose_is_remembered_only_when_the_WORKER_says_it_landed():
    """`wants` is the retry, and handing a pose to a thread is not evidence it
    arrived. Remembering it at hand-off would drop the pose whose write failed
    -- exactly the bug `test_a_FAILED_write_does_not_count_as_pushed` covers,
    reintroduced one layer up."""
    t = live_link.CameraSyncTicker()
    p = _pose(300)
    t.begin(p)
    assert t.wants(p), "handing it to a worker is not landing it"
    t.landed(p, None)
    t.drain()
    assert not t.wants(p)


def test_a_worker_that_FAILED_frees_the_slot_and_still_retries():
    t = live_link.CameraSyncTicker()
    p = _pose(300)
    t.begin(p)
    t.landed(p, "connection refused")
    said = t.drain()
    assert any("connection refused" in ln for ln in said)
    assert t.wants(p), "a failed write is not a push"
    assert t.begin(p) is True, "the slot must be free again"
    assert t.interval() == live_link.CAMERA_SYNC_BACKOFF


def test_the_slot_is_freed_by_landing_so_the_sync_cannot_wedge():
    """A slot that is claimed and never released stops the sync forever, and
    it does it SILENTLY -- the ticker would report nothing at all."""
    t = live_link.CameraSyncTicker()
    t.begin(_pose(300))
    assert t.begin(_pose(301)) is False
    t.landed(_pose(300), None)
    assert t.begin(_pose(301)) is True


def test_drain_says_nothing_when_no_worker_has_reported():
    t = live_link.CameraSyncTicker()
    assert t.drain() == []


def test_reset_frees_the_flight_slot_too():
    """Toggling the sync off mid-write must not leave the slot claimed, or
    switching it back on syncs nothing and says nothing about why."""
    t = live_link.CameraSyncTicker()
    t.begin(_pose(300))
    t.reset()
    assert t.begin(_pose(301)) is True


# --- decision 13: the unit list walk, against the battle savestate ----------
# `unit_sprite_list_head` (0x80098A54) heads a singly-linked list of sprite
# display objects. The shape is not this module's guess: `unit_sprite_object_find`
# (0x8007A6E4) is the engine's own id -> node getter and the disassembly records
# exactly how it reads the chain -- `lw node+0x0` for the next pointer, `lbu
# node+0x4` for the id. A BYTE, which is the one thing the design left to the
# label set: read as a word the id is 0x0061000A, and the report would name a
# unit nothing else in the engine calls by that number.
#
# The oracle for "the walk lands on real units" is independent of this file and
# of that disassembly: the F13 probe walked this same list against a running
# battle and recorded Agrias's node address in the label set. Nothing here
# computes 0x800B7748.

AGRIAS_NODE = 0x800B7748          # fft-ghidra renames_high.tsv, 0x80098a54, F13


import contextlib as _contextlib  # noqa: E402


@_contextlib.contextmanager
def _patched(module, **names):
    """Swap module constants for the length of a rival reading."""
    was = {n: getattr(module, n) for n in names}
    for n, v in names.items():
        setattr(module, n, v)
    try:
        yield
    finally:
        for n, v in was.items():
            setattr(module, n, v)


def _image_client(ram: bytes):
    """A real `RamClient` answering from a RAM image -- the savestate, or one
    the test has seeded a defect into. The client is real so the walk is
    exercised through `hold()` and `write()` rather than through a duck."""
    http = _FakeHttp()
    http.ram = bytearray(ram)
    return _ram_client(http)


@live_ram
def test_the_walk_finds_the_battles_units(gariland_ram):
    """The whole walk, against RAM a real Gariland battle held."""
    walk = live_link.walk_units(_image_client(gariland_ram))
    assert walk.complete, walk.ended
    assert AGRIAS_NODE in [u.address for u in walk.units]


@live_ram
def test_a_rival_reading_of_the_list_does_NOT_find_the_battles_units(
        gariland_ram):
    """The arm that gives the one above its meaning.

    Three rival readings of the same bytes -- the head one word off, the next
    pointer taken from `node+0x4` (where the id is) and the id from `node+0x0`
    -- and none of them may reach Agrias's node with a clean walk. Without this
    the test above is satisfied by any walk that happens to terminate.
    """
    client = _image_client(gariland_ram)
    rivals = {
        "head one word high": (live_link.UNIT_LIST_HEAD + 4,
                               live_link.UNIT_NEXT, live_link.UNIT_ID),
        "head one word low": (live_link.UNIT_LIST_HEAD - 4,
                              live_link.UNIT_NEXT, live_link.UNIT_ID),
        "next and id swapped": (live_link.UNIT_LIST_HEAD,
                                live_link.UNIT_ID, live_link.UNIT_NEXT),
    }
    truth = live_link.walk_units(client)
    for name, (head, nxt, ident) in rivals.items():
        with _patched(live_link, UNIT_LIST_HEAD=head, UNIT_NEXT=nxt,
                      UNIT_ID=ident):
            rival = live_link.walk_units(client)
        assert not (rival.complete and rival.units == truth.units), (
            f"the rival reading {name!r} is indistinguishable from the "
            f"engine's own")


@live_ram
def test_a_SEEDED_cycle_is_caught_and_what_it_reached_is_still_hidable(
        gariland_ram):
    """The artist's call: hide what you reached, then say the chain went bad.

    The link out of the third node is bent back at the head, which is a cycle
    no length cap would notice quickly and no null terminator would ever end.
    Two units are behind it and they are still hidden -- but `complete` is
    False and `ended` says so, because *"hid 3, then the chain went bad"* and
    *"hid 8 of 8"* are different sentences.
    """
    ram = bytearray(gariland_ram)
    walk = live_link.walk_units(_image_client(bytes(ram)))
    assert walk.found > 3, "the savestate has to hold a chain to bend"
    third = walk.units[2].address
    struct.pack_into("<I", ram, third + live_link.UNIT_NEXT - live_link.RAM_BASE,
                     walk.units[0].address)

    bent = live_link.walk_units(_image_client(bytes(ram)))
    assert not bent.complete
    assert "loops back" in bent.ended
    assert bent.found == 3
    assert [u.address for u in bent.units] == [u.address for u in walk.units[:3]]


@live_ram
def test_a_SEEDED_overlong_chain_stops_at_the_cap(gariland_ram):
    """A chain longer than a roster can be. `entd_to_roster_loader_16` loads 16
    ENTD slots and `{47}` adds at most three ghosts, so past the cap the bytes
    are not a unit list however well-formed they look. Seeded as a march
    through untouched RAM, so every node passes the range and alignment gates
    and only the cap can stop it."""
    ram = bytearray(gariland_ram)
    node = 0x80190000
    struct.pack_into("<I", ram, live_link.UNIT_LIST_HEAD - live_link.RAM_BASE,
                     node)
    for _ in range(live_link.UNIT_WALK_CAP + 8):
        struct.pack_into("<I", ram, node - live_link.RAM_BASE, node + 0x400)
        node += 0x400

    walk = live_link.walk_units(_image_client(bytes(ram)))
    assert not walk.complete
    assert "not a roster" in walk.ended
    assert walk.found == live_link.UNIT_WALK_CAP


@live_ram
def test_a_chain_that_leaves_main_RAM_writes_to_nothing_it_did_not_validate(
        gariland_ram):
    """The rule the whole degrade-gracefully answer rests on: the walk stops
    following a bad link, and never returns a node derived from one."""
    ram = bytearray(gariland_ram)
    walk = live_link.walk_units(_image_client(bytes(ram)))
    second = walk.units[1].address
    struct.pack_into("<I", ram, second + live_link.UNIT_NEXT - live_link.RAM_BASE,
                     0x1234ABCD)

    off = live_link.walk_units(_image_client(bytes(ram)))
    assert not off.complete and "left main RAM" in off.ended
    assert off.found == 2
    assert 0x1234ABCD not in [u.address for u in off.units]


def test_no_units_is_not_the_same_answer_as_no_bytes_changed():
    """A null head is indistinguishable from *not in a battle*, and it must not
    collide with the `0 changed` that already means *already isolated*. Found
    is its own number for exactly this."""
    walk = live_link.walk_units(_image_client(bytes(live_link.RAM_BYTES)))
    assert walk.found == 0
    assert walk.complete, "a null head is a well-formed empty list"
    assert walk.units == []


@live_ram
def test_the_unit_id_is_the_BYTE_the_engine_matches_on(gariland_ram):
    """Read as a word the first id is 0x0061000A, and the report would name a
    unit by a number nothing in the engine uses.

    Neither bound here is this module's. `unit_sprite_object_find` (0x8007A6E4)
    matches with `lbu node+0x4`, and `unit_sprite_object_exists` (0x8008CBB4)
    is called by the `{47}` free-slot scan over **slots 0..0x14** -- so a live
    battle's ids are small, distinct handles in that range, which a word read
    is not.
    """
    walk = live_link.walk_units(_image_client(gariland_ram))
    ids = [u.id for u in walk.units]
    assert ids and all(0 <= i <= 0x14 for i in ids), ids
    assert len(set(ids)) == len(ids), f"ids collide, so find() could not resolve them: {ids}"


# --- decision 13: the unit gate, and why restore is not a constant ----------

@live_ram
def test_hiding_a_unit_writes_the_two_halfwords_the_engine_zeroes(gariland_ram):
    """`unit_sprite_object_hide` (0x8008D18C) is `sh zero,0xa(v1)` **and**
    `sh zero,0x1d8(v1)`. The plan is the engine's own write, one node at a
    time: two halfwords per unit and nothing else, so a plan for 11 units is 22
    writes and every address is a node the walk validated."""
    walk = live_link.walk_units(_image_client(gariland_ram))
    plan = live_link.plan_hide_units(walk.units)

    assert len(plan) == 2 * walk.found
    assert all(data == b"\x00\x00" for _a, data in plan)
    nodes = {u.address for u in walk.units}
    for address, _data in plan:
        assert (address - live_link.UNIT_SHOW in nodes
                or address - live_link.UNIT_DISPATCH in nodes), (
            f"0x{address:08X} is not an offset into a validated node")
    assert {a for a, _ in plan} == (
        {u.address + live_link.UNIT_SHOW for u in walk.units}
        | {u.address + live_link.UNIT_DISPATCH for u in walk.units})


def test_restore_writes_the_SAVED_value_and_not_the_constant_one():
    """The defect this arm exists for: `unit_sprite_object_show` writes `1` to
    both fields, and copying that would REVEAL a unit the game had legitimately
    hidden -- not yet revealed, erased by a `{46}`, off-roster. Two units, one
    visible and one the battle had already hidden, and the restore has to put
    each one back the way it found it."""
    node = live_link.RAM_BASE + 0x1000
    units = [live_link.UnitNode(address=node, id=0, show=1, dispatch=1),
             live_link.UnitNode(address=node + 0x440, id=1, show=0,
                                dispatch=0)]
    restore = dict(live_link.plan_restore_units(units))

    assert restore[node + live_link.UNIT_SHOW] == b"\x01\x00"
    assert restore[node + live_link.UNIT_DISPATCH] == b"\x01\x00"
    assert restore[node + 0x440 + live_link.UNIT_SHOW] == b"\x00\x00"
    assert restore[node + 0x440 + live_link.UNIT_DISPATCH] == b"\x00\x00"


@live_ram
def test_isolate_then_restore_leaves_the_battles_RAM_byte_for_byte(gariland_ram):
    """The round trip, through a real `RamClient` against a real battle's RAM.
    Hide every unit, then restore, and the image is the one the savestate held
    -- which is the only statement that covers both plans at once."""
    client = _image_client(gariland_ram)
    walk = live_link.walk_units(client)
    assert client.write(live_link.plan_hide_units(walk.units)) > 0

    hidden = live_link.walk_units(client)
    assert [u.address for u in hidden.units] == [u.address for u in walk.units]
    assert all(u.show == 0 and u.dispatch == 0 for u in hidden.units)

    client.write(live_link.plan_restore_units(walk.units))
    assert client._get() == gariland_ram


@live_ram
def test_a_second_isolate_changes_nothing_which_is_what_re_pressable_means(
        gariland_ram):
    """Idempotent and re-pressable is the whole answer to the three ways the
    emulator drifts out from under Blender. The second press has to be a
    no-op the byte count can SAY is a no-op."""
    client = _image_client(gariland_ram)
    first = live_link.walk_units(client)
    assert client.write(live_link.plan_hide_units(first.units)) > 0

    again = live_link.walk_units(client)
    assert client.write(live_link.plan_hide_units(again.units)) == 0


# --- decision 13: the code poke, the first write to the instruction stream --

def _walks_to_jr_ra_without_a_frame(client, address, limit=512):
    """Does the function at `address` return without ever touching `sp`/`ra`?

    Walks the real instruction stream from `address` to its first `jr ra` and
    reports whether anything in between builds a frame (`addiu sp,sp,-N`),
    saves the return address (`sw ra,...`), or calls out (`jal`/`jalr`). That
    is the LEAF property, read from this battle's own RAM rather than asserted.

    Returns `(is_leaf, reason)` so a failure can say which instruction spoiled
    it instead of only that one did.
    """
    for i in range(limit):
        (word,) = struct.unpack("<I", client.read(address + i * 4, 4))
        op, rs, rt = word >> 26, (word >> 21) & 31, (word >> 16) & 31
        if op == 0 and (word & 0x3F) == 0x08 and rs == 31:      # jr ra
            return True, "reached jr ra"
        if op == 3:                                             # jal
            return False, f"jal at +0x{i * 4:X}"
        if op == 0 and (word & 0x3F) == 0x09:                   # jalr
            return False, f"jalr at +0x{i * 4:X}"
        if op == 9 and rs == 29 and rt == 29:                   # addiu sp,sp,N
            return False, f"sp adjust at +0x{i * 4:X}"
        if op == 0x2B and rs == 29 and rt == 31:                # sw ra,N(sp)
            return False, f"ra save at +0x{i * 4:X}"
    return False, f"no jr ra within {limit} instructions"


@live_ram
def test_every_code_gate_targets_a_real_FUNCTION_ENTRY(gariland_ram):
    """The one arm that says a poke target is not an address someone typed.

    `jr ra; nop` over a function's first two instructions is safe *because it
    returns before the frame is built* -- `sp` is never touched. Land it two
    instructions into a prologue instead and the poke returns with a
    half-adjusted stack.

    There are TWO shapes that satisfy that, not one:

    * a function that opens with `addiu sp,sp,-N` -- the poke jumps in FRONT of
      the prologue. `0x27BDFDB8` at the vitals window is `addiu sp,sp,-0x248`,
      the prologue the decision record cites, re-read here rather than quoted.
    * a LEAF -- a function that never touches `sp` or `ra` and never calls out.
      There is no frame to be half-built, so the poke is safe *a fortiori*.
      The camera leash is one, and demanding a prologue of it would reject the
      safer target of the two.

    So each gate is classified and checked against its own shape. The
    classification is asserted by name: widening this arm must not let a
    prologue function quietly re-file itself as a leaf to escape the check.
    """
    client = _image_client(gariland_ram)
    gates = live_link.save_code_gates(client)
    assert len(gates) == 4, [g.name for g in gates]
    assert {g.address for g in gates} == {live_link.HUD_RENDERER,
                                          live_link.CURSOR_RENDERER,
                                          live_link.CAMERA_LEASH,
                                          live_link.DIALOGUE_BOX_RENDERER}

    prologue = {live_link.HUD_RENDERER, live_link.CURSOR_RENDERER,
                live_link.DIALOGUE_BOX_RENDERER}
    leaf = {live_link.CAMERA_LEASH}
    for gate in gates:
        (word,) = struct.unpack("<I", gate.saved[:4])
        if gate.address in prologue:
            assert word >> 16 == 0x27BD, (
                f"{gate.name} at 0x{gate.address:08X} does not open a frame: "
                f"0x{word:08X}")
        else:
            assert gate.address in leaf, gate.name
            assert word >> 16 != 0x27BD, (
                f"{gate.name} opens a frame after all -- check it as a "
                f"prologue gate, not a leaf: 0x{word:08X}")
            is_leaf, why = _walks_to_jr_ra_without_a_frame(client, gate.address)
            assert is_leaf, (
                f"{gate.name} at 0x{gate.address:08X} is not a leaf: {why}")


@live_ram
def test_the_cursors_FALLBACK_is_also_a_function_entry(gariland_ram):
    """The uncertainty is shipped, not hidden: the cursor's target is one named
    constant and `CURSOR_RENDERER_FALLBACK` is the other candidate. It is
    checked too, so the day the artist reports *knife still there* the swap is
    one line and not a fresh investigation."""
    client = _image_client(gariland_ram)
    (word,) = struct.unpack(
        "<I", client.read(live_link.CURSOR_RENDERER_FALLBACK, 4))
    assert word >> 16 == 0x27BD, f"0x{word:08X}"
    assert live_link.CURSOR_RENDERER != live_link.CURSOR_RENDERER_FALLBACK


def test_the_poke_is_jr_ra_then_nop():
    """`0x03E00008` is `jr ra`, `0x00000000` is `nop`. Eight bytes, asserted as
    the ENCODED words rather than as a blob, because a byte-order slip here is
    a jump to whatever the swapped word decodes to."""
    assert live_link.RETURN_STUB == struct.pack("<II", 0x03E00008, 0x00000000)
    assert len(live_link.RETURN_STUB) == 8


@live_ram
def test_poking_then_restoring_the_code_leaves_the_battles_RAM_byte_for_byte(
        gariland_ram):
    """The round trip. Restore writes back the eight SAVED bytes -- there is no
    constant that could stand in for them here, which is the difference between
    this gate and the unit flags."""
    client = _image_client(gariland_ram)
    gates = live_link.save_code_gates(client)
    assert client.write(live_link.plan_hide_code(gates)) > 0

    for gate in gates:
        assert client.read(gate.address, 8) == live_link.RETURN_STUB

    client.write(live_link.plan_restore_code(gates))
    assert client._get() == gariland_ram


@live_ram
def test_a_second_poke_of_the_code_changes_nothing(gariland_ram):
    """Re-pressable, the same way the unit gate is -- and the saved bytes must
    come from a walk taken BEFORE the poke, or a second press would save the
    stub and restore would leave the HUD gone for good."""
    client = _image_client(gariland_ram)
    first = live_link.save_code_gates(client)
    assert client.write(live_link.plan_hide_code(first)) > 0

    again = live_link.save_code_gates(client)
    assert client.write(live_link.plan_hide_code(again)) == 0
    assert all(g.saved == live_link.RETURN_STUB for g in again), (
        "the second save reads the stub back, which is exactly why an isolate "
        "must not overwrite the saved values it is holding")


# --- decision 13: re-pressable, without losing the way back -----------------

@live_ram
def test_a_second_isolate_keeps_the_FIRST_saved_values(gariland_ram):
    """The defect that would make Isolate a one-way door.

    Isolate is idempotent and re-pressable -- that is the whole answer to the
    three ways the emulator drifts out from under Blender. But the second walk
    reads back what the FIRST press wrote, so a session memory that simply
    replaced itself would save `show = 0` for every unit and the restore would
    leave the battle empty. Merging keeps the first press's answer.
    """
    client = _image_client(gariland_ram)
    first = live_link.walk_units(client).units
    client.write(live_link.plan_hide_units(first))

    second = live_link.walk_units(client).units
    assert all(u.show == 0 for u in second), "the seed did not take"

    kept = live_link.merge_saved(first, second)
    assert all(u.show == 1 and u.dispatch == 1 for u in kept)

    client.write(live_link.plan_restore_units(kept))
    assert client._get() == gariland_ram


def test_a_unit_that_SPAWNS_while_isolated_is_added_to_the_memory():
    """The other half of re-pressable, and the reason the merge is not just
    *"ignore the second walk"*. A unit spawning mid-battle appears on the
    second press with its own real flags, and those are the values its restore
    needs -- so it joins the memory rather than being dropped."""
    node = live_link.RAM_BASE + 0x1000
    born = live_link.RAM_BASE + 0x2000
    first = [live_link.UnitNode(node, 0, 1, 1)]
    second = [live_link.UnitNode(node, 0, 0, 0),
              live_link.UnitNode(born, 7, 1, 1)]

    kept = {u.address: u for u in live_link.merge_saved(first, second)}
    assert len(kept) == 2
    assert kept[node].show == 1, "the first press's saved value must win"
    assert kept[born].show == 1 and kept[born].id == 7


# --- decision 13: the report is a COUNT OF UNITS, not a count of bytes ------

def test_a_null_head_says_found_no_units_and_not_zero_changed():
    """The defect a refusal was going to protect against, avoided with a second
    number instead. `0 changed` already means *already isolated*; if the
    not-in-a-battle case reported the same thing, one sentence would mean two
    opposite things."""
    line = live_link.isolate_report(
        live_link.UnitWalk([], "the list ended", True), hidden=0, changed=0)
    assert "no units" in line
    assert "already" not in line


def test_a_complete_walk_says_hid_N_of_N():
    walk = live_link.UnitWalk(
        [live_link.UnitNode(live_link.RAM_BASE + i * 0x440, i, 1, 1)
         for i in range(8)], "the list ended", True)
    line = live_link.isolate_report(walk, hidden=8, changed=16)
    assert "8 of 8" in line
    assert "went bad" not in line


def test_an_incomplete_walk_says_how_far_it_got_and_why():
    """*"hid 8 of 8"* and *"hid 3, then the chain went bad"* are different
    sentences, and the reason has to travel with the count -- a partial hide
    the artist cannot see the edge of is the thing that reads as a broken
    feature."""
    walk = live_link.UnitWalk(
        [live_link.UnitNode(live_link.RAM_BASE + i * 0x440, i, 1, 1)
         for i in range(3)], "the chain loops back to 0x800B9D88", False)
    line = live_link.isolate_report(walk, hidden=3, changed=6)
    assert "3" in line
    assert "loops back" in line


def test_already_isolated_is_its_own_sentence():
    """Zero bytes changed with units found is the re-press, and it must not
    read as a failure."""
    walk = live_link.UnitWalk(
        [live_link.UnitNode(live_link.RAM_BASE, 0, 0, 0)], "the list ended",
        True)
    line = live_link.isolate_report(walk, hidden=1, changed=0)
    assert "already" in line
