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


@pytest.mark.parametrize("field", ["map_states[].palettes",
                                   "map_states[].texture_sheet"])
def test_every_per_state_field_without_a_sink_is_named(field):
    """Decision 4: push what has a sink and NAME what was skipped, never refuse.

    §3's coverage table lists three per-state data fields; the light rig has a
    sink of its own now (§2.2) and these two are what is left. The panel reads `UNPUSHED` on every
    push, so a field missing from it is dropped SILENTLY -- the one thing
    decision 4 forbids, and the reason the artist's "I changed the preview
    state and nothing happened" had no answer on screen.
    """
    assert field in live_link.UNPUSHED


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
