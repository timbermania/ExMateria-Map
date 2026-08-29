"""Grade the *Push to PCSX* button against a fake emulator, headless.

The button (`addons/exmateria_map/live_link_ui.py`) assembles the document in
memory and hands it to the `bpy`-free core. Its arithmetic is the core's and is
already covered by `tests/test_live_link.py`; what this harness grades is the
half that only exists inside Blender — the operator's order, its four refusal
paths, the shadow-based self-check, and the session memory that lets the artist
press the button twice.

**Why a fake emulator and not the real one.** Two reasons, and the second is
the interesting one:

1. It runs anywhere, in seconds, with no ISO and no savestate. The real
   emulator is `tests/live_normals_audit.py`'s job and remains the acceptance.
2. **Gariland cannot exercise the start index.** Every one of its four start
   indices is 0, so a rig that reads polygon `i` at `base + i * stride` — the
   trap `live_link.py`'s docstring names — is *correct there*. The fake RAM
   below seeds the geometry at `base + (start + i) * stride` with start indices
   of 5 / 7 / 3 / 2, so a rig that ignored them fails, and the harness seeds
   exactly that defect to prove the check can go red.

Nothing here writes a file: `export_document.write_bundle` is replaced with a
detonator, because "the push writes no bundle" is a claim worth a check rather
than a comment.

Run:  python3 tests/blender_live_push.py [blender-binary]
"""
import json
import subprocess
import sys
import zipfile
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
ADDON_DIR = PKG / "addons" / "exmateria_map"
FIXTURES = PKG / "tests" / "fixtures"
FIXTURE = FIXTURES / "MAP001.a0.stub.json"
TMP = Path(__file__).resolve().parent / ".blender_live_push"
REPORT = TMP / "report.json"

#: A run that stops early has caught nothing. `live_normals_audit.py` learned
#: this the hard way — it printed PASS directly under "the audit itself broke".
EXPECTED_CHECKS = 258

SCRIPT_TEMPLATE = r'''
import json
import re
import struct
import sys
import traceback

import bmesh
import bpy
import inspect

PKG = "@ADDONPKG@"
ZIP = "@ZIP@"
JSON = "@JSON@"
DISC = "@DISC@"
DISC2 = "@DISC2@"
OUT = "@OUT@"

checks, notes = {}, []


def check(n, cond, detail=""):
    checks[n] = bool(cond)
    print(("  ok   " if cond else "  FAIL ") + n + (f": {detail}" if detail else ""))


def write_report(fatal=None):
    with open(OUT, "w") as f:
        json.dump({"checks": checks, "notes": notes, "fatal": fatal}, f, indent=1)


try:
    bpy.ops.preferences.addon_install(filepath=ZIP)
except Exception as e:
    print(f"INSTALL: {e}")
bpy.ops.preferences.addon_enable(module='exmateria_map')
sys.path.insert(0, PKG)

from exmateria_map import (export_document, live_link as L,
                           live_link_ui as UI, live_vram as VR)

DOC = json.loads(open(JSON).read())


# --- the fake emulator -----------------------------------------------------

class FakeRam:
    """Enough of an emulator to be a machine, not a stub -- **both transports**.

    It stands in for three things the addon talks to, because they are one
    machine: `POST /api/v1/cpu/ram/raw` (the default since #606), the packed-Lua
    walk (`exec`, the fork path), and the `gte` Lua handler the light rig goes
    through. Each is really parsed and really applied -- the packer, the record
    header, the clustering and the changed-byte count are all under test rather
    than mocked away.
    """

    def __init__(self):
        self.mem = bytearray(L.RAM_BYTES)
        self.cp2c = [0] * 32          # the GTE control registers
        self.up = True
        self.handlers = True          # was the addon's `.lua` `-dofile`d?
        self.execs = 0
        self.gets = 0
        self.gte_execs = 0
        self.lua_walks = 0            # packed-Lua record walks -- the fork path
        self.tick = 0                 # the animation's frame cursor

    # -- transport
    def ping(self):
        return not self.check()

    def check(self):
        """`LuaClient.check`'s three states, because the UI gates on all three.

        `handlers` is the middle one -- an emulator up but launched without
        `-dofile`. It is a separate flag from `up` here for the same reason it
        is a separate message there: they are different mistakes.
        """
        if not self.up:
            return "no emulator answering on localhost:8080"
        if not self.handlers:
            return "pcsx-redux is running ... but has no `ping` Lua handler"
        return ""

    def call(self, handler, query="", timeout=30.0):
        """`GET /api/v1/lua/<handler>?<query>` -- the stock transport.

        The length ceiling is enforced here as the server would, rather than
        assumed away: over it a real pcsx-redux routes elsewhere and 404s, so a
        fake that accepted any length would grade a push that cannot happen.
        """
        self.execs += 1
        if not self.up:
            raise L.TransportError("no emulator answering")
        if not self.handlers:
            raise L.NoHandlerError(f"no `{handler}` Lua handler")
        path = f"/api/v1/lua/{handler}" + (f"?{query}" if query else "")
        assert len(path) <= L.URL_LIMIT, f"{len(path)}-byte URL would 404"
        if handler == "ping":
            return "pong\n"
        if handler == "gte":
            self.gte_execs += 1
            n = 0
            for pair in query.split("&"):
                index, _, value = pair.partition("=")
                if index.isdigit() and value.isdigit() and int(index) <= 31:
                    self.cp2c[int(index)] = int(value)
                    n += 1
            return f"{n}\n"
        raise L.NoHandlerError(f"no `{handler}` Lua handler")

    def animate(self):
        """Run the `0x6c` table this RAM holds, one step per fetch.

        Not decoration. Decision 11's readback grades a row **moving**, and a
        stub whose CLUT block never changed would pass every push -- including
        one that erased nothing, which is the reported bug. So the fake does
        what the engine does: walk the live table, and repaint each animated
        CLUT row from the live `0x70` frames.

        Inert while the table is zero, which is every arm above this one.
        """
        table = bytes(self.mem[L.ANIM_TABLE - L.RAM_BASE:
                               L.ANIM_TABLE - L.RAM_BASE + L.ANIM_TABLE_BYTES])
        records = L.read_animation_table(table) or ()
        if not L.animation_rows(records):
            return
        self.tick += 1
        o = L.ANIM_FRAMES - L.RAM_BASE
        frames = bytes(self.mem[o:o + L.ANIM_FRAMES_BYTES])
        for r in records:
            if r.clut_row is None or not r.frame_count:
                continue
            # Byte 19 is the run flag, and the disc ships it CLEAR: the loader
            # arms the records at map load. Measured [LIVE] 2026-08-28 -- a
            # verbatim install of `MAP022.9`'s chunk sat at the right address,
            # byte-perfect, and did not move a pixel until byte 19 was set. A
            # fake that ran an unarmed record would have passed that install.
            if r.raw[L.ANIM_RUN_FLAG_BYTE] != L.ANIM_RUN_FLAG:
                continue
            # Byte 14 is the frame count on the DISC and the engine's own
            # cursor in a running table -- `MAP022.9` reads 4 there off the
            # disc and 0x81 in a savestate -- so it is clamped to the 16 frames
            # that exist. Unclamped it indexes past the `0x70` block, and a
            # `bytearray` slice assignment with a short value RESIZES the
            # array: two of them shifted every byte of RAM above the CLUT
            # block down by 64 and made an untouched table read as corrupt.
            f = self.tick % min(r.frame_count, 16)
            d = L.CLUT_BLOCK - L.RAM_BASE + r.clut_row * L.CLUT_ROW_BYTES
            row = frames[f * 32:f * 32 + 32]
            assert len(row) == L.CLUT_ROW_BYTES, "a short row would RESIZE RAM"
            self.mem[d:d + L.CLUT_ROW_BYTES] = row
        assert len(self.mem) == L.RAM_BYTES, "the fake's RAM changed size"

    def rebuild_camera(self, honest=True):
        """What the engine does with `work_rotation` every frame.

        `build_camera_view_matrix` recomposes `CAMERA_VIEW_MATRIX` from the
        angles on each frame, which is the only reason reading it back says
        anything: it is the ENGINE's arithmetic on the artist's write, not a
        readback of the write itself.

        The honest arm is admittedly circular -- the fake composes with the
        same `L.camera_rotation` the button checks against -- and it is not
        where the value is. `honest=False` is: it models a write that landed
        somewhere the engine never rebuilds from, which is precisely the one
        thing decision 12 leaves open, and it is what proves the button can
        REPORT a pose that did not take instead of going green over an
        unchanged picture.
        """
        if not honest:
            return
        angles = struct.unpack_from("<3h", self.mem,
                                    L.WORK_ROTATION - L.RAM_BASE)
        r = L.camera_rotation(*angles)
        o = L.CAMERA_VIEW_MATRIX - L.RAM_BASE
        self.mem[o:o + 18] = struct.pack(
            "<9h", *[max(-32768, min(32767, round(r[i][j] * 4096)))
                     for i in range(3) for j in range(3)])

    # -- `POST /api/v1/cpu/ram/raw`, driven through the real `RamClient`
    def get(self):
        if not self.up:
            raise L.TransportError("no emulator answering")
        self.animate()
        self.execs += 1
        # Counted apart from `execs` because ADR-0186 Amendment 7 decision 32
        # is a claim about THIS number: stock's GET always returns the whole
        # 2 MB, so every one of these is 2 MB moved.
        self.gets += 1
        return bytes(self.mem)

    def post(self, offset, data):
        if not self.up:
            raise L.TransportError("no emulator answering")
        self.execs += 1
        assert 0 <= offset and offset + len(data) <= L.RAM_BYTES
        self.mem[offset:offset + len(data)] = data

    def read(self, address, length):
        o = address - L.RAM_BASE
        if o < 0 or o + length > L.RAM_BYTES:
            raise L.LiveLinkError("outside main RAM")
        return bytes(self.mem[o:o + length])

    def exec(self, code, timeout=180.0):
        """The packed-Lua walk -- the **fork** path, kept and still graded.

        Nothing the button does reaches it any more -- the operator builds a
        `RamClient` unconditionally -- but `tools/live_geometry.py` drives it
        on purpose, so the fake keeps a real parser for it and the arms below
        assert that a push never trips it. The rig does not come through it
        either: `apply_gte` is a URL now, which is what stock can receive.
        """
        self.execs += 1
        if not self.up:
            raise L.TransportError("no emulator answering")
        self.lua_walks += 1
        m = re.search(r'local p = "([0-9a-f]*)"', code)
        if m is None:
            raise AssertionError("unrecognised Lua: " + code[:120])
        p, i, changed = m.group(1), 0, 0
        while i < len(p):
            o = int(p[i:i + 8], 16)
            n = int(p[i + 8:i + 12], 16)
            i += L.RECORD_HEADER
            data = bytes.fromhex(p[i:i + n * 2])
            assert len(data) == n, "a record is not as long as it says"
            for k, c in enumerate(data):
                if self.mem[o + k] != c:
                    self.mem[o + k] = c
                    changed += 1
            i += n * 2
        return str(changed)

    # -- seeding, in longhand
    def poke(self, address, data):
        o = address - L.RAM_BASE
        self.mem[o:o + len(data)] = data


STARTS = (5, 7, 3, 2)          # deliberately NOT Gariland's four zeroes


def descriptor_block(counts, starts=STARTS, followers=()):
    """Nine descriptors, written by hand rather than by the module under test.

    `followers` is `[(index, counts)]` for the eight AnimatedMesh instances.
    MAP022 a0 has none -- 15 resources over 12 maps do -- so this is the only
    place the follower gate can be asked anything at all.
    """
    block = bytearray(L.DESCRIPTOR_STRIDE * L.DESCRIPTOR_COUNT)
    for k in range(4):
        block[L.DESCRIPTOR_STARTS + 2 * k:L.DESCRIPTOR_STARTS + 2 * k + 2] = \
            int(starts[k]).to_bytes(2, "little")
        block[L.DESCRIPTOR_COUNTS + 2 * k:L.DESCRIPTOR_COUNTS + 2 * k + 2] = \
            int(counts[k]).to_bytes(2, "little")
    for index, fc in followers:
        at = index * L.DESCRIPTOR_STRIDE + L.DESCRIPTOR_COUNTS
        for k in range(4):
            block[at + 2 * k:at + 2 * k + 2] = int(fc[k]).to_bytes(2, "little")
    return bytes(block)


def doc_counts(doc):
    return tuple(sum(1 for p in doc["polygons"] if p["kind"] == b)
                 for b in L.BUCKETS)


def seed_geometry(ram, doc, honour_start=True):
    """Write the document's own geometry where the engine would hold it.

    The address arithmetic is spelled out here rather than borrowed from
    `live_link.plan`: a seed computed by the code under test cannot fail.
    """
    for b in L.BUCKETS:
        polys = [p for p in doc["polygons"] if p["kind"] == b]
        if not polys:
            continue
        stride = len(polys[0]["positions"]) * 8
        start = STARTS[L.BUCKETS.index(b)] if honour_start else 0
        sink = L.SINKS[b]
        for i, p in enumerate(polys):
            for field, base in (("positions", sink.positions),
                                ("normals", sink.normals)):
                if base is None or field not in p:
                    continue
                for k, (x, y, z) in enumerate(p[field]):
                    ram.poke(base + (start + i) * stride + k * 8,
                             int(x).to_bytes(2, "little", signed=True)
                             + int(y).to_bytes(2, "little", signed=True)
                             + int(z).to_bytes(2, "little", signed=True))


def seed_metadata(ram, doc, honour_start=True):
    """Bytes 6-7 of vertices 0 and 1, in longhand.

    `live_geometry.py` identified these on MAP022 a0, 454 of 454 polygons, and
    `tests/test_live_link.py` re-measures the whole rule against the checked-in
    Gariland savestate. The arithmetic is spelled out AGAIN here for the same
    reason the geometry's is: a seed computed by the code under test cannot
    fail.
    """
    for b in L.BUCKETS:
        polys = [p for p in doc["polygons"] if p["kind"] == b]
        if not polys:
            continue
        textured = b.startswith("textured")
        stride = len(polys[0]["positions"]) * 8
        start = STARTS[L.BUCKETS.index(b)] if honour_start else 0
        base = L.SINKS[b].positions
        for i, p in enumerate(polys):
            t = p.get("terrain")
            bind = 0 if not t else (t["x"] << 8) | (t["z"] << 1) | t["level"]
            va = p.get("visible_angles")
            va = 0x8000 if va is None else va
            at = base + (start + i) * stride
            ram.poke(at + 6, bind.to_bytes(2, "little"))
            ram.poke(at + 8 + 6, (va | (1 if textured else 0))
                     .to_bytes(2, "little"))


def scribble_metadata(ram, doc, honour_start=True):
    """Leave every polygon wearing somebody else's metadata.

    This is what a mid-mesh deletion does without the write: positions, normals
    and the packet follow a polygon to its new slot and these two shorts do
    not, so the survivor arrives carrying the previous occupant's
    VISIBLE_ANGLES -- which culls the quad away into a hole rather than
    mis-colouring it.
    """
    touched = []
    for b in L.BUCKETS:
        polys = [p for p in doc["polygons"] if p["kind"] == b]
        if not polys:
            continue
        stride = len(polys[0]["positions"]) * 8
        start = STARTS[L.BUCKETS.index(b)] if honour_start else 0
        base = L.SINKS[b].positions
        for i in range(len(polys)):
            at = base + (start + i) * stride
            ram.poke(at + 6, b"\xad\xde")
            ram.poke(at + 8 + 6, b"\xef\xbe")
            touched += [at + 6, at + 8 + 6]
    return touched


# The primitive packets, in longhand, for the same reason the geometry is: a
# seed computed by the code under test cannot fail.  `FUN_800f5578` writes them
# at load, and `FUN_800ee104` makes TWO of them 0xEE28 apart.
PACKET_A = 0x800FC55C
PACKET_BUFFER_STRIDE = 0xEE28
PACKET_REGION = {"textured_triangle": (0x0000, 0x28),
                 "textured_quad":     (0x3840, 0x34)}
PACKET_UV_AT = {"textured_triangle": (0x0C, 0x18, 0x24),
                "textured_quad":     (0x0C, 0x18, 0x24, 0x30)}
#: A TPAGE base the real machine does NOT use.  Gariland's packets read 0x0C,
#: 0x0D, 0x0E -- so a sink that reconstructed the word as `0x0C | page` instead
#: of masking the loaded one would be correct there and wrong here, which is the
#: whole point of choosing a different column.  `texture_page` owns two bits;
#: nothing else in the halfword is ours.
FAKE_TPAGE_BASE = 0x0140
FAKE_CLUT_ROW = 0x7800


def seed_packets(ram, doc, honour_start=True):
    """Write the document's own UVs, palettes and texture pages into BOTH
    primitive buffers, and point the base pointer at the first."""
    for b, (region, stride) in PACKET_REGION.items():
        polys = [p for p in doc["polygons"] if p["kind"] == b]
        if not polys:
            continue
        start = STARTS[L.BUCKETS.index(b)] if honour_start else 0
        for buf in range(2):
            base = PACKET_A + buf * PACKET_BUFFER_STRIDE + region
            for i, poly in enumerate(polys):
                at = base + (start + i) * stride
                for k, off in enumerate(PACKET_UV_AT[b]):
                    u, v = poly["uv"][k]
                    ram.poke(at + off, bytes((u & 0xFF, v & 0xFF)))
                ram.poke(at + 0x0E, (FAKE_CLUT_ROW | (poly["palette_id"] & 0x0F))
                         .to_bytes(2, "little"))
                ram.poke(at + 0x1A, (FAKE_TPAGE_BASE | (poly["texture_page"] & 3))
                         .to_bytes(2, "little"))
    ram.poke(L.PACKET_BASE_POINTER, PACKET_A.to_bytes(4, "little"))


def seed_rig(ram, rig):
    """The rig where the map loader would have left it -- longhand, again.

    The default is to seed NOTHING, so the three addresses start at zero and a
    rig check after a push is measuring the push. Seeding the rig the push is
    about to write would be an inert seed: it reads exactly like a blind check.
    """
    if rig is None:
        return
    ram.poke(L.RIG_GAINS, struct.pack(
        "<9h", *(rig["colors"][i][c] for c in range(3) for i in range(3))))
    ram.poke(L.RIG_DIRECTIONS, struct.pack(
        "<9h", *(v for row in rig["directions"] for v in row)))
    ram.poke(L.RIG_AMBIENT, struct.pack("<3i", *rig["ambient"]))


def fresh_ram(doc=DOC, counts=None, honour_start=True, rig=None,
              followers=(), clut=False):
    ram = FakeRam()
    ram.poke(L.DESCRIPTOR_BASE,
             descriptor_block(counts or doc_counts(doc), followers=followers))
    seed_geometry(ram, doc, honour_start=honour_start)
    seed_metadata(ram, doc, honour_start=honour_start)
    seed_packets(ram, doc, honour_start=honour_start)
    seed_rig(ram, rig)
    # Off by default for the same reason the rig is: a comparison side that
    # seeds what the push is about to write is an inert seed, and reads exactly
    # like a blind check. Switched on only where the check is about something
    # ELSE and the palette write would otherwise show up as its difference.
    if clut:
        block = clut_block_bytes(doc)
        if block is not None:
            ram.poke(L.CLUT_BLOCK, block)
    return ram


def fresh_vram(ram, doc=DOC):
    vram = FakeVramClient()
    seed_clut(ram, vram, doc)
    return vram


class FakeVramClient:
    """Enough of `live_vram.VramClient` to be a machine, not a stub.

    VRAM really is a byte buffer, so this is not a mock of the endpoint -- it
    is the endpoint's semantics: a POST paints `width` words a row at `(x, y)`
    and a GET hands back the megabyte. `check_rect` runs on every write, so a
    rectangle the real fork would 400 fails here too.
    """

    def __init__(self):
        self.vram = bytearray(VR.VRAM_BYTES)
        self.posted = []
        self.reads = 0

    def ping(self):
        return True

    def read(self):
        self.reads += 1
        return bytes(self.vram)

    def write_rect(self, rc):
        VR.check_rect(rc)
        self.posted.append(rc.label)
        for r in range(rc.height):
            o = (rc.y + r) * VR.PITCH + rc.x * 2
            self.vram[o:o + rc.width * 2] = rc.data[r * rc.width * 2:
                                                    (r + 1) * rc.width * 2]


def clut_block_bytes(doc):
    """A state's palettes packed as the 512-byte block the loader leaves."""
    pals = next((st["palettes"] for st in doc["map_states"] if st.get("palettes")),
                None)
    if not pals:
        return None
    block = b"".join(w.to_bytes(2, "little")
                     for row in pals
                     for w in L._clut_words(row, 0))[:L.CLUT_BLOCK_BYTES]
    return block.ljust(L.CLUT_BLOCK_BYTES, b"\x00")


def seed_clut(ram, vram, doc):
    """Put a state's palettes where the map loader would have left them, in
    BOTH memories, so `check_clut_block` has something coherent to check.

    The two must agree because that is the real invariant: the RAM block is
    what the engine uploads to the VRAM rows every frame. Seeding only one
    would make the check pass or fail for a reason the rig does not have.
    """
    block = clut_block_bytes(doc)
    if block is None:
        return
    ram.poke(L.CLUT_BLOCK, block)
    for row in range(L.CLUT_ROWS):
        o = VR.CLUT_Y * VR.PITCH + row * L.CLUT_ENTRIES * 2
        vram.vram[o:o + L.CLUT_ENTRIES * 2] = block[row * 32:(row + 1) * 32]


RAM = None

#: VRAM is derived from whichever RAM is current rather than assigned beside
#: it. Seventeen checks build a fresh `RAM` and none of them is about VRAM;
#: making each one remember a second line would mean the one that forgot got a
#: stale CLUT block and a `check_clut_block` refusal that had nothing to do
#: with what it was testing.
_VRAM = {}


def vram_for(ram):
    if _VRAM.get("ram") is not ram:
        _VRAM["ram"], _VRAM["vram"] = ram, fresh_vram(ram)
    return _VRAM["vram"]


def _ram_client(host=None, port=None):
    """A real `RamClient` wired to the fake endpoint.

    Patched rather than duck-typed so the clustering, the bounds checks and
    the changed-byte count are the shipped ones -- this transport is the
    addon's default, and a fake that reimplemented `write` would grade the
    fake.
    """
    c = _REAL_RAM_CLIENT()
    c._get, c._post = RAM.get, RAM.post
    return c


#: Captured before the patch below, or `_ram_client` calls itself forever.
_REAL_RAM_CLIENT = L.RamClient

L.LuaClient = lambda host=None, port=None: RAM
L.RamClient = _ram_client
UI.VR.VramClient = lambda host=None, port=None: vram_for(RAM)


# --- the scene -------------------------------------------------------------

def marker():
    return bpy.data.objects[f"{DOC['base']['map']}.a{DOC['base']['arrangement']}"]


def push(**kw):
    """Drive the real operator; Blender raises on `report({"ERROR"})`."""
    try:
        return bpy.ops.map.live_push(**kw), None
    except RuntimeError as e:
        return {"CANCELLED"}, str(e)


def last_push():
    return json.loads(marker().get(UI.LAST_PUSH_KEY) or "[]")


try:
    bpy.ops.import_map.document(filepath=JSON)
    ob = marker()
    check("the scene imported", ob is not None and len(ob.data.polygons) == 2,
          f"{ob and len(ob.data.polygons)} faces")

    # A push must never write a bundle. Detonate if it tries.
    def _no_bundle(*a, **k):
        raise AssertionError("the push wrote a bundle")
    export_document.write_bundle = _no_bundle

    # ---- refusal 1: no emulator ------------------------------------------
    RAM = fresh_ram()
    RAM.up = False
    res, err = push()
    check("no emulator is refused", res == {"CANCELLED"}, str(res))
    check("no emulator names the host and port",
          any("no emulator answering" in ln for ln in last_push()),
          str(last_push()))
    check("no emulator costs no round trip", RAM.execs == 0, RAM.execs)

    # ---- refusal 1b: the emulator is up, `-dofile` was forgotten ----------
    # The likeliest real failure on a stock install, and the one a two-state
    # gate misdiagnoses: every upstream endpoint answers, only ours 404s, so
    # "no emulator answering" would send the artist to check a port that was
    # never the problem.
    RAM = fresh_ram()
    RAM.handlers = False
    res, err = push()
    check("missing handlers are refused", res == {"CANCELLED"}, str(res))
    check("missing handlers are not reported as a missing emulator",
          any("handler" in ln for ln in last_push())
          and not any("no emulator answering" in ln for ln in last_push()),
          str(last_push()))

    # ---- refusal 2: the gate ---------------------------------------------
    RAM = fresh_ram(counts=(0, 0, 0, 0))
    before = bytes(RAM.mem)
    res, err = push()
    check("an unloaded map is refused", res == {"CANCELLED"}, str(res))
    check("the gate's reason reaches the marker",
          any("no map is loaded" in ln for ln in last_push()), str(last_push()))
    check("a refused gate writes nothing", bytes(RAM.mem) == before)

    # ---- refusal 3: the two GROWTH gates ---------------------------------
    # #598 lifted the count equality that used to stand here, so a differing
    # count is no longer the refusal -- these two are, and they were built and
    # seeded red BEFORE the equality came out. The loader does not bound-check
    # the four arrays (ADR-0004 decision 28), so a count above capacity is not
    # a wrong picture, it is memory corruption.
    #
    # **Neither is gradable on the emulator.** MAP022 a0 has no animated mesh
    # and its 24/361/18/51 sit far under 360/710/64/256, so no document an
    # artist could author reaches either refusal there. Both are claimed green
    # off this fake RAM alone, and that is said here rather than implied.
    #
    # The document carries one textured_quad; a map that loaded NONE makes it
    # a growth.
    RAM = fresh_ram(counts=(0, 0, 0, 1), followers=[(1, (0, 10, 0, 0))])
    before = bytes(RAM.mem)
    res, err = push()
    check("growth into a bucket with a follower is refused",
          res == {"CANCELLED"}, str(res))
    check("the follower refusal names the live counts and points at `build`",
          any("textured_quad" in ln and "descriptor 1" in ln
              and "`build`" in ln for ln in last_push()), str(last_push()))
    check("a refused follower gate writes nothing", bytes(RAM.mem) == before)

    RAM = fresh_ram(counts=(0, 0, 0, 1), followers=[(2, (0, 710, 0, 0))])
    before = bytes(RAM.mem)
    res, err = push()
    check("growth past the engine's array is refused",
          res == {"CANCELLED"}, str(res))
    check("the capacity refusal names decision 28's unchecked array",
          any("711" in ln and "710" in ln and "bound-check" in ln
              for ln in last_push()), str(last_push()))
    check("a refused capacity gate writes nothing", bytes(RAM.mem) == before)

    # ---- refusal 4: the packet base pointer names neither buffer ---------
    # The packets are double buffered (`FUN_800ee104`: two, 0xEE28 apart) and
    # the base is the one address in this module that is not static. Writing
    # the wrong one is SILENT -- every address stays inside main RAM, `apply`
    # reports a plausible changed-byte count, and the only symptom is a picture
    # that does not move. Without this arm `packet_base_unchecked` read BLIND,
    # because the fake always seeds a VALID pointer and a guard that never
    # fires is a guard nothing grades.
    RAM = fresh_ram()
    RAM.poke(L.PACKET_BASE_POINTER, (0x80100000).to_bytes(4, "little"))
    before = bytes(RAM.mem)
    res, err = push()
    check("a stray packet base is refused", res == {"CANCELLED"}, str(res))
    check("a stray packet base names both buffers",
          any("0x800FC55C" in ln and "0x8010B384" in ln for ln in last_push()),
          str(last_push()))
    check("a stray packet base writes nothing", bytes(RAM.mem) == before)

    # ---- the happy path, on an untouched import --------------------------
    RAM = fresh_ram()
    res, err = push()
    check("an untouched import pushes", res == {"FINISHED"}, f"{res} {err}")
    check("an untouched import changes zero bytes",
          any("pushed 0 changed byte(s)" in ln for ln in last_push()),
          str(last_push()))
    # The two zeroes. `apply` returning 0 has two causes that the number cannot
    # tell apart, and shipping only the number cost a real "the button never
    # writes anything" report: a lighting change made with Lamp authority OFF
    # moves no normal, so the document is unedited and the push truthfully says
    # zero -- which reads exactly like a broken button.
    check("an unedited document says so, rather than only saying zero",
          any("nothing to push" in ln and "Lamp authority" in ln
              for ln in last_push()), str(last_push()))
    # Decision 4's rule, not a count: every field with no sink is NAMED. A
    # magic ">= 4" was what stood here, and it went red the moment three fields
    # gained a sink -- which is a check on the tally, not on the rule.
    _named = {ln.split("not pushed: ", 1)[1].split(" -- ", 1)[0]
              for ln in last_push() if ln.startswith("not pushed:")}
    check("the report names what has no sink",
          _named and _named <= set(L.UNPUSHED), f"named {_named}")
    check("what the report names is really unpushed",
          not (_named & {"polygons[].uv", "polygons[].palette_id",
                         "polygons[].texture_page", "terrain",
                         "polygons[].terrain", "polygons[].visible_angles"}),
          f"named a field that DOES push: {_named}")
    # A push writes the per-polygon BINDING and cannot touch the terrain GRID.
    # "not pushed: terrain" on the press that wrote 454 bindings reads as the
    # feature being broken, and the word could mean either thing.
    check("the report says terrain GRID, not bare terrain",
          "the terrain grid" in _named and "terrain" not in _named,
          f"named {_named}")
    check("pushing normals covers the light rig",
          not any("light_rig" in ln for ln in last_push()), str(last_push()))
    # The packets are planned for BOTH buffers, every textured bucket, every
    # field. One buffer would be a push the screen never shows.
    check("the report counts the packet plans over both buffers",
          any("packet plan(s) over 2 buffers" in ln for ln in last_push()),
          str(last_push()))

    # ---- pushing from EDIT MODE ------------------------------------------
    # Reported from use as a bare `IndexError` out of `stamp_new_faces` after
    # deleting a face -- which is the exact gesture the shrink leg exists for.
    # In Edit Mode the geometry lives in the BMesh and the Mesh datablock's
    # ATTRIBUTE arrays read as size 0 while `me.polygons` still reports a
    # count, so the two disagree and the first lookup blows up. The deletion is
    # not what breaks it; READING IN EDIT MODE is, so this arm needs no
    # deletion to seed it.
    RAM = fresh_ram()
    UI._LAST_PUSH.clear()
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    check("Edit Mode really does empty the attribute arrays",
          len(ob.data.attributes["imported"].data) == 0
          and len(ob.data.polygons) > 0,
          f"{len(ob.data.attributes['imported'].data)} vs "
          f"{len(ob.data.polygons)}")
    res, err = push()
    check("a push from EDIT MODE finishes instead of raising",
          res == {"FINISHED"}, f"{res} {err}")
    check("the artist is left in Edit Mode, where they were",
          ob.mode == "EDIT", ob.mode)
    bpy.ops.object.mode_set(mode="OBJECT")

    # ---- bytes 6-7 are written on EVERY push ------------------------------
    # The document owns the terrain BINDING word and VISIBLE_ANGLES, and the
    # push used to leave both alone on the reasoning that an existing polygon
    # owns valid ones. A re-slot breaks that reasoning: the survivor arrives
    # wearing the previous occupant's, and a wrong VISIBLE_ANGLES culls the
    # quad into a hole rather than mis-colouring it. Seeded as the defect it
    # catches -- every polygon scribbled, an untouched document pushed, and
    # RAM demanded back byte for byte.
    RAM = fresh_ram()
    UI._LAST_PUSH.clear()
    pristine = bytes(RAM.mem)
    scribbled = scribble_metadata(RAM, DOC)
    check("the scribble moved something",
          scribbled and all(RAM.read(a, 2) != pristine[a - L.RAM_BASE:
                                                       a - L.RAM_BASE + 2]
                            for a in scribbled),
          f"{len(scribbled)} short(s)")
    # The self-check now READS these bytes, so it refuses a RAM nobody in this
    # session wrote -- which is what it is for, and is why the repair arm below
    # has to stand it down to reach the write at all.
    res, err = push()
    check("the self-check sees bytes 6-7 too",
          res == {"CANCELLED"} and any("metadata" in ln for ln in last_push()),
          f"{res} {last_push()}")

    # The write itself, over EVERY polygon of EVERY bucket -- which an edit to
    # one face cannot grade. `skip_selfcheck` exists for exactly this.
    res, err = push(skip_selfcheck=True)
    # Scoped to the shorts the scribble touched: the push also writes the
    # light rig, which `fresh_ram` does not seed, so whole-RAM equality would
    # be reporting a different leg's business as this one's.
    _left = [a for a in scribbled
             if RAM.read(a, 2) != pristine[a - L.RAM_BASE:a - L.RAM_BASE + 2]]
    check("a push repairs scribbled metadata on every bucket",
          res == {"FINISHED"} and not _left,
          f"{res} {err}; left " + ", ".join(f"0x{a:08X}" for a in _left))

    # And the artist's own path: an edited VISIBLE_ANGLES reaches RAM. The
    # document owns this word now, so the live picture shows what the document
    # says -- the same rule as every other field.
    RAM = fresh_ram()
    UI._LAST_PUSH.clear()
    _va_was = ob.data.attributes["visible_angles"].data[0].value
    ob.data.attributes["visible_angles"].data[0].value = 0x8004
    res, err = push()
    _at = (L.SINKS["textured_quad"].positions + STARTS[1] * 32 + 8 + 6)
    check("an edited VISIBLE_ANGLES reaches RAM, with the textured flag",
          res == {"FINISHED"}
          and RAM.read(_at, 2) == (0x8005).to_bytes(2, "little"),
          f"{res} {err}; RAM holds {RAM.read(_at, 2).hex()}")
    ob.data.attributes["visible_angles"].data[0].value = _va_was

    # ---- REPLACING the loaded map: a hot swap ----------------------------
    # The self-check's premise is that RAM ALREADY HOLDS the document's own
    # bytes. That premise is the identity claim decision 7 recovered as a side
    # effect, and it is exactly what replacing the loaded map violates on
    # purpose -- so the default mode has to go on refusing a foreign map, and
    # the swap mode has to not.
    #
    # Seeded as a coherent FOREIGN MAP rather than as garbage. A savestate of
    # another battle holds a different map's polygons at these very addresses,
    # in the same shape, with the descriptor declaring its own counts; garbage
    # would pass an arm that only looked for "RAM is not ours" and would not
    # exercise the shrink the count leg has to do.
    FOREIGN = json.loads(json.dumps(DOC))
    for _p in FOREIGN["polygons"]:
        _p["positions"] = [[x + 100, y, z] for x, y, z in _p["positions"]]
    RAM = fresh_ram(doc=FOREIGN, counts=(0, 3, 0, 2))
    UI._LAST_PUSH.clear()
    _quad = L.SINKS["textured_quad"].positions + STARTS[1] * 32
    _ours = struct.pack("<hhh", *DOC["polygons"][0]["positions"][0])
    check("a foreign map is really in RAM, and it is not the document's",
          RAM.read(_quad, 6) != _ours,
          f"RAM {RAM.read(_quad, 6).hex()} vs document {_ours.hex()}")

    res, err = push()
    check("the default mode REFUSES a foreign map, as it always has",
          res == {"CANCELLED"}
          and any("self-check FAILED" in ln for ln in last_push()),
          f"{res} {last_push()}")

    res, err = push(replace_loaded_map=True)
    check("replacing the loaded map finishes instead of refusing",
          res == {"FINISHED"}, f"{res} {err}")
    check("...and the document's own geometry is what RAM holds after",
          RAM.read(_quad, 6) == _ours,
          f"RAM holds {RAM.read(_quad, 6).hex()}, document says {_ours.hex()}")
    # The counts are half of what "replaced" means: the foreign map declared
    # three quads and two untextured quads, the document has one of each, and
    # a swap that left the old counts standing would draw two slots of the map
    # it replaced.
    _counts = struct.unpack(
        "<4H", RAM.read(L.DESCRIPTOR_BASE + L.DESCRIPTOR_COUNTS, 8))
    check("...and the counts are the document's, not the replaced map's",
          _counts == doc_counts(DOC), f"{_counts} vs {doc_counts(DOC)}")
    # A weaker check reported in the same words as the strong one is worse
    # than no check: the artist reads "self-check passed" and believes the
    # thing that was not proved.
    check("...and the report says the proof is WEAKER, not that it passed",
          any("WEAKER" in ln for ln in last_push())
          and not any("the planned addresses hold" in ln
                      for ln in last_push()),
          str(last_push()))
    # The two legs a swap does not deliver, and it must not let the picture
    # imply otherwise. Both are footnotes when the artist is editing the map
    # the emulator has loaded and headlines when they have replaced it:
    #
    #   the terrain grid -- `UNPUSHED` names it on every push, but "the map
    #   looks right and COLLIDES wrong" is a curiosity on your own map and is
    #   the whole story on somebody else's: units walk the tiles of the map
    #   that was replaced.
    #
    #   the sheet and the CLUT block -- their VRAM addresses are DERIVED from
    #   the live packets (`derive_addresses`), and on a swap the packets they
    #   are derived from belonged to the map being replaced.
    _said = " ".join(last_push())
    check("...and it says units will walk the REPLACED map's tiles",
          "walk the map you replaced" in _said, _said[:400])
    check("...and it says the VRAM addresses came from the replaced map",
          "derived from the map you replaced" in _said, _said[:400])
    # The same two lines must NOT appear on an ordinary push, or they are
    # decoration rather than a warning -- an artist who reads them on every
    # press stops reading them on the press that matters.
    RAM = fresh_ram()
    UI._LAST_PUSH.clear()
    res, err = push()
    _plain = " ".join(last_push())
    check("...and an ordinary push carries neither line",
          res == {"FINISHED"}
          and "derived from the map you replaced" not in _plain
          and "walk the map you replaced" not in _plain
          and "WEAKER" not in _plain, f"{res} {_plain[:300]}")

    # ---- decision 11: a swap ERASES the host map's animation table -------
    # Reported by the artist, live: *"I just did a Replace the loaded map call
    # and it looks almost right except for one chunk of map which got the wrong
    # palette. It got the blue water palette and it's animated."* The push had
    # LANDED -- what repaints a correct push 4.49 times a second is the
    # replaced map's `0x6c` instruction table, still running in RAM.
    #
    # The fake runs that table (see `FakeRam.animate`), so these arms grade the
    # readback the way it is graded live: by rows MOVING, not by bytes.
    _ours = open(DISC + "/MAP001.9", "rb").read()
    _host = open(DISC + "/MAP099.9", "rb").read()
    _our_table, _our_frames = _ours[196:836], _ours[836:1348]
    _host_frames = _host[836:1348]
    # The table as a RUNNING engine holds it: bytes 14/16/18/19 of each palette
    # record are the engine's frame cursor and tick counter, not the map's
    # data. Seeded here so the guard's mask is exercised rather than assumed --
    # an unmasked comparison would match nothing and refuse a healthy swap.
    def running(table, slots):
        b = bytearray(table)
        for _i in slots:
            for _b, _v in zip((14, 16, 18, 19), (0x81, 0x02, 0x09, 0x01)):
                b[_i * 20 + _b] = _v
        return bytes(b)

    _running = running(_host[196:836], (0, 1))       # the HOST, mid-cycle
    _running_ours = running(_our_table, (2,))        # this document's own map

    ob["exmateria_map/gns_path"] = DISC + "/MAP001.GNS"
    check("the scene's remembered GNS resolves to an extracted disc tree",
          UI.base_map_dir(ob) is not None, str(UI.base_map_dir(ob)))

    def seed_animation(ram, table=_running, frames=_host_frames):
        ram.poke(L.ANIM_TABLE, table)
        ram.poke(L.ANIM_FRAMES, frames)

    # --- the EDIT path: never neutralise a map's own animation
    RAM = fresh_ram()
    UI._LAST_PUSH.clear()
    seed_animation(RAM, _running_ours, _our_frames)
    res, err = push()
    _said = " ".join(last_push())
    check("a push finishes with an animation running", res == {"FINISHED"},
          f"{res} {err}")
    check("...and it leaves the loaded map's OWN animation table ALONE",
          RAM.read(L.ANIM_TABLE, L.ANIM_TABLE_BYTES) == _running_ours,
          RAM.read(L.ANIM_TABLE, 20).hex())
    # `build` carries `0x6c`/`0x70` to the disc verbatim, so freezing the
    # animation here would preview a picture the shipped map cannot produce.
    check("...and it NAMES the rows this map's own table animates",
          "animates CLUT row(s) 13" in _said, _said[-500:])
    check("...and says the battle repaints them rather than warning",
          "repaints them" in _said and "animation NOT" not in _said,
          _said[-400:])

    # --- the REPLACE path: erase the host's table, install this map's
    RAM = fresh_ram(doc=FOREIGN, counts=(0, 3, 0, 2))
    UI._LAST_PUSH.clear()
    seed_animation(RAM)
    res, err = push(replace_loaded_map=True)
    _said = " ".join(last_push())
    _table = RAM.read(L.ANIM_TABLE, L.ANIM_TABLE_BYTES)
    _records = L.read_animation_table(_table)
    check("a swap over a running animation finishes", res == {"FINISHED"},
          f"{res} {err}")
    check("...and the host's CLUT rows are gone from the table",
          L.animation_rows(_records) == [13],
          str(L.animation_rows(_records)))
    # The scope is the TABLE, not the palettes: 94 of the corpus's 110 tables
    # drive TEXTURE regions, and the host's three point inside the pages this
    # push has just uploaded a foreign sheet to.
    check("...and so are its THREE texture records",
          not [r for r in _records if any(r.raw) and not r.is_palette],
          str([(r.x, r.y) for r in _records if any(r.raw) and not r.is_palette]))
    check("...and the pushed map's own palette record is installed, in its "
          "own slot",
          _table[40:40 + L.ANIM_RUN_FLAG_BYTE]
          == _our_table[40:40 + L.ANIM_RUN_FLAG_BYTE], _table[40:60].hex())
    # The one byte an install does not carry verbatim, because the map does not
    # own it. Without it the record is at the right address, byte-perfect, and
    # dead -- which is what the live machine showed on 2026-08-28.
    check("...and it is ARMED, which the disc's own bytes are not",
          _table[40 + L.ANIM_RUN_FLAG_BYTE] == L.ANIM_RUN_FLAG
          and _our_table[40 + L.ANIM_RUN_FLAG_BYTE] == 0,
          f"{_table[59]} vs disc {_our_table[59]}")
    check("...and its frames are in the loaded `0x70` block",
          RAM.read(L.ANIM_FRAMES, L.ANIM_FRAMES_BYTES) == _our_frames)
    check("...and the erase named the corpus resource it matched",
          "MAP099.9" in _said, _said[-600:])
    check("...and the readback reports the rows that MOVED",
          "CLUT row(s) 13 move and no others do" in _said, _said[-600:])
    # Decision 10's rule: a weaker check reported in the same words as the
    # strong one is worse than no check.
    check("...and the texture half says it is a BYTE confirmation",
          "BYTE confirmation" in _said, _said[-600:])
    check("...and the pushed map's own texture record is NOT installed",
          "erased and NOT installed" in _said, _said[-600:])

    # --- the seeded defect: an erase that does nothing must go RED
    _real_erase = L.plan_erase_animation
    L.plan_erase_animation = lambda: []
    RAM = fresh_ram(doc=FOREIGN, counts=(0, 3, 0, 2))
    UI._LAST_PUSH.clear()
    seed_animation(RAM)
    res, err = push(replace_loaded_map=True)
    _said = " ".join(last_push())
    L.plan_erase_animation = _real_erase
    check("an erase that writes nothing is CAUGHT by the readback",
          "NOT fully removed" in _said, _said[-600:])
    check("...and the verdict names the host rows that kept moving",
          "14, 15" in _said, _said[-600:])
    check("...and the surviving texture records are named too",
          "survived the erase" in _said, _said[-600:])

    # --- degradation: a base the document does not pin costs the INSTALL only
    ob["exmateria_map/gns_path"] = DISC2 + "/MAP001.GNS"
    RAM = fresh_ram(doc=FOREIGN, counts=(0, 3, 0, 2))
    UI._LAST_PUSH.clear()
    seed_animation(RAM)
    res, err = push(replace_loaded_map=True)
    _said = " ".join(last_push())
    _records = L.read_animation_table(RAM.read(L.ANIM_TABLE, L.ANIM_TABLE_BYTES))
    check("a base resource that is not the pinned one refuses the INSTALL",
          "animation NOT installed" in _said and "sha256" in _said,
          _said[-600:])
    check("...and the erase still happened, because it needs no disc read",
          not [r for r in _records if any(r.raw)],
          str([(r.x, r.y) for r in _records if any(r.raw)]))
    check("...and the whole push is not refused over an animation chunk",
          res == {"FINISHED"}, f"{res} {err}")

    # --- no tree at all: the guard cannot confirm, so it does not erase
    ob["exmateria_map/gns_path"] = ""
    RAM = fresh_ram(doc=FOREIGN, counts=(0, 3, 0, 2))
    UI._LAST_PUSH.clear()
    seed_animation(RAM)
    res, err = push(replace_loaded_map=True)
    _said = " ".join(last_push())
    check("with no disc tree the erase REFUSES rather than writing blind",
          "animation NOT erased" in _said, _said[-600:])
    check("...and the host's table is left exactly as it was",
          RAM.read(L.ANIM_TABLE, L.ANIM_TABLE_BYTES) == _running,
          RAM.read(L.ANIM_TABLE, 24).hex())
    check("...and the push still finishes", res == {"FINISHED"}, f"{res} {err}")

    # ---- ADR-0186 decision 49: the COMPILE reads the same rows -----------
    # The search must not move a chart on or off an animated CLUT row, and the
    # document says nothing about which rows those are -- schema §8 puts `0x6c`
    # on the carried-from-base side. So `compile_op.animated_rows_of` reads it
    # from the same place, through the same sha256-pinned reader, as the
    # install above. Graded HERE and not in `test_compile.py` because the
    # arithmetic is pytest's and what can silently go wrong is the ADDRESS: a
    # marker that resolves no tree returns `None` and the search runs
    # unbounded, which is the reported defect with nothing said.
    ob["exmateria_map/gns_path"] = DISC + "/MAP001.GNS"
    from exmateria_map.compile_op import (animated_rows_of, animation_note,
                                          animation_note_once)
    _state = int(ob.get("exmateria_map/preview_state") or 0)
    _CO = sys.modules["exmateria_map.compile_op"]

    _CO._ANIMATED.clear()
    check("the compile reads the rows this map ANIMATES off the disc",
          animated_rows_of(ob, _state) == (13,),
          f"{animated_rows_of(ob, _state)!r}; the install above proved the "
          f"same table names row 13")
    check("...by the same address the animation INSTALL uses, so the two "
          "cannot disagree about which rows are animated",
          tuple(sorted(set(L.animation_rows(L.base_animation(
              UI.base_map_dir(ob), {"base": json.loads(
                  ob["exmateria_map/base"])},
              json.loads(ob["exmateria_map/map_states"])[_state]["resource"]
          )[0])))) == animated_rows_of(ob, _state))

    _CO._ANIMATED.clear()
    ob["exmateria_map/gns_path"] = ""
    _unknown = animated_rows_of(ob, _state)
    check("a scene that remembers no tree answers NONE, not 'nothing "
          "animated'",
          _unknown is None, repr(_unknown))
    check("...and the compile SAYS the search was not bounded, rather than "
          "silently behaving as it did before decision 49",
          "not bounded" in (animation_note([], [], _unknown) or ""),
          repr(animation_note([], [], _unknown)))
    check("...while a map that really animates nothing says nothing at all",
          animation_note([], [], ()) is None,
          repr(animation_note([], [], ())))
    _CO._ANIMATED.clear()
    ob["exmateria_map/gns_path"] = DISC + "/MAP001.GNS"
    check("the note names the rows and counts what is held there",
          animation_note([{"palette_id": 13}, {"palette_id": 2}],
                         [[0], [1]], animated_rows_of(ob, _state))
          == ("CLUT row(s) 13 are ANIMATED on this map: 1 chart(s) are held "
              "on them and no other chart may move onto them (decision 49)"),
          repr(animation_note([{"palette_id": 13}, {"palette_id": 2}],
                              [[0], [1]], animated_rows_of(ob, _state))))

    # The UNBOUNDED half is a fact about the scene, not about this compile, so
    # `ensure_compiled` -- which runs on every settle, export, push and bundle
    # -- says it once. `tests/blender_convert.py` is where that surfaced: its
    # exit-compile report grew a second line that repeated forever.
    _CO._SAID_UNBOUNDED.clear()
    check("the automatic path says 'not bounded' the FIRST time",
          "not bounded" in (animation_note_once(("Ob", 0), [], [], None) or ""),
          repr(animation_note_once(("Ob", 0), [], [], None)))
    check("...and not again for the same subject, because nothing about the "
          "scene changed and a settle runs this every time",
          animation_note_once(("Ob", 0), [], [], None) is None,
          repr(animation_note_once(("Ob", 0), [], [], None)))
    check("...while the HELD half repeats, being an outcome of each compile",
          all(animation_note_once(("Ob", 0), [{"palette_id": 13}], [[0]],
                                  (13,)) is not None for _ in range(2)))

    # ---- GROWTH: a slot the loaded map never had -------------------------
    # The document carries one textured_quad and the map loaded none, so this
    # is the direction that used to be refused outright. Fake RAM only: MAP022
    # a0 cannot be made to load fewer polygons than its document has.
    RAM = fresh_ram(counts=(0, 0, 0, 1))
    UI._LAST_PUSH.clear()
    res, err = push()
    _d = L.parse_descriptor(
        RAM.read(L.DESCRIPTOR_BASE, L.DESCRIPTOR_STRIDE * 9), 0)
    check("a document that GREW pushes", res == {"FINISHED"}, f"{res} {err}")
    check("growth raised the bucket's count", _d.counts == doc_counts(DOC),
          str(_d.counts))
    check("the report names the per-bucket delta",
          any("textured_quad 0 -> 1" in ln for ln in last_push()),
          str(last_push()))
    # Note what this arm does NOT show: the document here is the imported one,
    # so "byte-identical to the map you imported" is the truth and the growth
    # is only against the LOADED MAP. The `authored_bytes` claim needs a face
    # the artist actually added, which is the next arm.
    _at = L.SINKS["textured_quad"].positions + STARTS[1] * 32
    check("the new slot carries the document's geometry and metadata",
          RAM.read(_at, 6) == fresh_ram().read(_at, 6)
          and RAM.read(_at + 8 + 6, 2) == fresh_ram().read(_at + 8 + 6, 2),
          RAM.read(_at, 16).hex())

    # ---- a face the ARTIST added: authored bytes must not be zero --------
    # `authored_bytes` used to `zip` the base plan against the new one, which
    # truncates to the shorter, so every byte a longer document added was
    # outside the comparison: a pure growth reported **0 authored bytes** and
    # `interpret` printed "byte-identical to the map you imported -- check
    # Lamp authority is ON" over a push that had just added a polygon. That is
    # a third cause for the zero this readout exists to disambiguate.
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    _vs = [bm.verts.new(v) for v in ((0, 0, 0), (0, 8, 0), (8, 8, 0), (8, 0, 0))]
    bm.faces.new(_vs)
    bm.to_mesh(ob.data)
    bm.free()
    RAM = fresh_ram()
    UI._LAST_PUSH.clear()
    res, err = push()
    check("a document with an ADDED face pushes", res == {"FINISHED"},
          f"{res} {err}")
    check("an added face is not reported as byte-identical to the import",
          not any("nothing to push" in ln for ln in last_push()),
          str(last_push()))
    check("an added face reports authored byte(s)",
          any("differ from the imported map" in ln or "already live" in ln
              for ln in last_push()), str(last_push()))
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    bm.faces.ensure_lookup_table()
    bm.faces.remove(bm.faces[-1])
    bm.verts.ensure_lookup_table()
    for v in [v for v in bm.verts if not v.link_faces]:
        bm.verts.remove(v)
    bm.to_mesh(ob.data)
    bm.free()

    # ---- a null `visible_angles` writes the 0x8000 default ---------------
    # `visible_angles` is null on the 10 of 169 resources with no 0xB0 chunk.
    # **MAP022 HAS one**, so the only map this repo holds a savestate for
    # cannot reach this case: it is fake-RAM graded, and it is seeded rather
    # than asserted from rest, because the fixture's own value IS 0x8000 and a
    # check against the default from the default is a check of nothing.
    RAM = fresh_ram()
    UI._LAST_PUSH.clear()
    _va = ob.data.attributes["visible_angles"]
    _va_was = _va.data[0].value
    _at = L.SINKS["textured_quad"].positions + STARTS[1] * 32 + 8 + 6
    _va.data[0].value = 0x8004
    push()
    check("the seed moved the word off the default",
          RAM.read(_at, 2) == (0x8005).to_bytes(2, "little"),
          RAM.read(_at, 2).hex())
    _va.data[0].value = -1                  # import's in-band spelling of null
    res, err = push()
    check("a null visible_angles writes 0x8000, with the textured flag",
          res == {"FINISHED"}
          and RAM.read(_at, 2) == (0x8001).to_bytes(2, "little"),
          f"{res} {err}; RAM holds {RAM.read(_at, 2).hex()}")
    _va.data[0].value = _va_was

    # ---- an emulator someone ALREADY pushed lighting to -------------------
    # Reported from use, and it walled the artist out completely: open a map in
    # a fresh Blender, press the button, get "the loaded map is not this
    # document's map". `_LAST_PUSH` is recorded only on a SUCCESSFUL push, so
    # the refusal could never establish the memory that would let the next
    # press through -- the only way out was reloading a savestate that was
    # never the problem.
    #
    # Seeded as the real thing: normals scribbled, positions and metadata left
    # exact, which is what a previous session's BAKE looks like from here.
    # Measured on the live emulator that produced the report: positions 0 of
    # 8,664 differ, metadata 0 of 1,444, normals 7,589 of 8,664.
    RAM = fresh_ram()
    UI._LAST_PUSH.clear()
    _n = 0
    for b in ("textured_triangle", "textured_quad"):
        polys = [p for p in DOC["polygons"] if p["kind"] == b]
        stride = len(polys[0]["positions"]) * 8 if polys else 0
        for i in range(len(polys)):
            at = (L.SINKS[b].normals
                  + (STARTS[L.BUCKETS.index(b)] + i) * stride)
            RAM.poke(at, b"\x11\x22\x33\x44\x55\x66")
            _n += 1
    check("the previous-push seed moved normals and nothing else", _n > 0, _n)
    res, err = push()
    check("an already-pushed-to emulator is NOT refused", res == {"FINISHED"},
          f"{res} {err}")
    check("it says the emulator was already pushed to, and by whom",
          any("ALREADY PUSHED TO" in ln and "another session" in ln
              for ln in last_push()), str(last_push()))
    check("it does NOT blame the map or the arithmetic",
          not any("not this document's map" in ln or "arithmetic is wrong" in ln
                  for ln in last_push()), str(last_push()))

    # ---- the seeded defect: a rig that ignored the start index -----------
    # Gariland's four start indices are 0, so this is the arm the emulator
    # cannot provide. Seeded at base + i*stride, the self-check MUST go red.
    RAM = fresh_ram(honour_start=False)
    UI._LAST_PUSH.clear()
    before = bytes(RAM.mem)
    res, err = push()
    check("geometry seeded without the start index fails the self-check",
          res == {"CANCELLED"}, f"{res} {err}")
    check("the self-check names all three causes",
          all(s in " ".join(last_push())
              for s in ("not this document's map", "reload the savestate",
                        "arithmetic is wrong")), str(last_push()))
    check("a failed self-check writes nothing", bytes(RAM.mem) == before)

    # ---- an edit reaches RAM ---------------------------------------------
    RAM = fresh_ram()
    UI._LAST_PUSH.clear()
    me = ob.data
    moved = me.vertices[0].co.copy()
    me.vertices[0].co.x += 7.0
    res, err = push()
    check("an edited mesh pushes", res == {"FINISHED"}, f"{res} {err}")
    changed = [ln for ln in last_push() if "changed byte(s)" in ln]
    check("an edit changes bytes", changed and "pushed 0 " not in changed[0],
          str(changed))
    check("an edited push reports what differs from the imported map",
          any("differ from the imported map" in ln for ln in last_push()),
          str(last_push()))
    # The whole of RAM, against the edited document seeded by the harness's
    # own longhand addressing. Equality says three things at once: the edit
    # landed, it landed at the start-index address, and nothing outside the
    # six coordinate bytes of a vertex was touched — the seeds leave a
    # polygon's two metadata bytes zero, so a scribble there would show.
    live_doc = export_document.assemble(ob)[0]
    # The rig is seeded on the comparison side because the push writes it and
    # the loader would have: without it this equality reports 48 bytes of
    # difference that are the feature working.
    # The palettes join the rig here, and for the same reason: the push writes
    # them and the loader would have, so without them this equality reports the
    # 472 bytes of the palette leg WORKING as a difference.
    _seeded = fresh_ram(live_doc, rig=DOC["map_states"][0]["light_rig"],
                        clut=True)
    check("RAM holds the edited document, at the start-index addresses",
          bytes(RAM.mem) == bytes(_seeded.mem),
          sum(1 for a, b in zip(RAM.mem, _seeded.mem) if a != b))

    # ---- the second press of the button ----------------------------------
    # RAM now holds the push, not the disc. The CLI's answer is "reload the
    # savestate"; a button pressed repeatedly cannot ask that.
    me.vertices[0].co.x += 3.0
    res, err = push()
    check("a second push is not blocked by its own first", res == {"FINISHED"},
          f"{res} {err}")

    # ---- the OTHER zero: edited, and the emulator already holds it ---------
    res, err = push()
    check("a re-press of an unchanged edit says `already live`, not `nothing`",
          any("already live" in ln for ln in last_push()), str(last_push()))

    # ---- the operator takes ONE transport, and it is the stock one ---------
    # There used to be a `live_ram_over_http` preference here and an arm that
    # drove the push both ways. The preference is gone: its off position needed
    # our pcsx-redux fork, and the failure it produced named `lua/exec`, not the
    # checkbox. So what is graded now is that the button CANNOT reach the fork
    # path -- `apply` delegates to `client.write`, and a `RamClient` has one.
    # The Lua walk itself is still graded, by `test_live_link.py`, where the
    # tools that deliberately choose it live.
    RAM = fresh_ram()
    UI._LAST_PUSH.clear()
    me.vertices[0].co.x += 1.0
    _execs = RAM.execs
    _gets = RAM.gets
    res, err = push()
    check("a push goes through the stock RAM endpoint", res == {"FINISHED"},
          f"{res} {err}")
    # ADR-0186 Amendment 7 decision 32, end to end.  Stock's GET always hands
    # back the whole of `m_wram` -- the `offset`/`size` parameters are POST-only
    # (`web-server.cc:118-122`) -- so this count IS megabytes moved.  Measured
    # 29 before the push held an image, which is half again the ~20 the
    # decision estimated off the code.
    #
    # It is not ONE, and the remainder is the decision's own doing: a write
    # DROPS the held image rather than updating it, so every write phase pays
    # a fresh before-image.  Buying those back would mean a write-through
    # image, which would make the self-check compare the plan against the plan
    # -- and decision 32 exists so that the self-check can stay.
    _push_gets = RAM.gets - _gets
    check("a push does not re-fetch main RAM once per read",
          _push_gets <= 16,
          f"{_push_gets} GET(s) = {_push_gets * L.RAM_BYTES / 1e6:.0f} MB "
          f"(29 = 61 MB before the image was held)")
    # `execs` counts the GET/POST pairs AND any `exec`; `gte_execs` counts only
    # the rig's URL. A push that touched the packed-Lua walk would show up as
    # the fake's `exec` parsing a record, which is the assertion below.
    check("no part of a push runs packed Lua", RAM.lua_walks == 0,
          f"{RAM.lua_walks} packed-Lua walk(s)")
    _live = export_document.assemble(ob)[0]
    _want = fresh_ram(_live, rig=DOC["map_states"][0]["light_rig"], clut=True)
    check("the push leaves RAM holding the edited document",
          bytes(RAM.mem) == bytes(_want.mem),
          sum(1 for a, b in zip(RAM.mem, _want.mem) if a != b))
    me.vertices[0].co.x -= 1.0

    # ---- a map reload puts the disc's bytes back --------------------------
    RAM = fresh_ram()               # what a reload does
    res, err = push()
    check("a reloaded map self-checks against the disc's bytes again",
          res == {"FINISHED"}, f"{res} {err}")

    # ---- refusal 4: export's own refusals, before any round trip ---------
    RAM = fresh_ram()
    UI._LAST_PUSH.clear()
    me.attributes["palette_id"].data[0].value = 999      # §5.1.2 range
    before = bytes(RAM.mem)
    res, err = push()
    check("an export refusal refuses the push", res == {"CANCELLED"},
          f"{res} {err}")
    check("the refusal is export's own, quoted",
          any("palette_id" in ln for ln in last_push()), str(last_push()))
    check("a refused document writes nothing", bytes(RAM.mem) == before)
    me.attributes["palette_id"].data[0].value = 0

    # ---- a new face no longer stands the self-check down -----------------
    # Decision 8 as amended: the base is the marker's STORED document, so a
    # face the artist created cannot blank it. The check is at full strength
    # during growth, which is when this decision most wanted it.
    me.attributes["imported"].data[0].value = False
    RAM = fresh_ram()
    UI._LAST_PUSH.clear()
    check("a new face still leaves a base to check against",
          UI.base_polygons(ob) is not None,
          "base_polygons went None on a face the artist created")
    res, err = push()
    check("a new face pushes with the self-check RUNNING, not skipped",
          res == {"FINISHED"}
          and not any("self-check SKIPPED" in ln for ln in last_push()),
          f"{res} {last_push()}")
    me.attributes["imported"].data[0].value = True

    # ---- the base geometry is the disc's, not the artist's ---------------
    me.vertices[0].co.x += 11.0
    base = UI.base_polygons(ob)
    live = export_document.assemble(ob)[0]["polygons"]
    check("the self-check's base is the import, not the live mesh",
          base is not None
          and [p["positions"] for p in base] != [p["positions"] for p in live],
          "the base moved with the edit")
    check("the base is the document that was imported",
          [p["positions"] for p in base]
          == [p["positions"] for p in DOC["polygons"]],
          json.dumps(base)[:200])

    # ---- the light rig, BOTH halves (decision 9's atom, §2.2) ------------
    # The expectations are written out in longhand -- the planar order
    # especially -- because the order IS what is under test. The disc stores
    # all three lights' RED, then all three greens, then all three blues,
    # which is already the GTE colour matrix's order; the directions are per
    # light. A plan that transposed both, or neither, puts nine plausible
    # numbers in the wrong nine slots and the map merely looks different.

    def rig_expect(colors, directions, ambient):
        planar = [colors[i][c] for c in range(3) for i in range(3)]
        return (struct.pack("<9h", *planar),
                struct.pack("<9h", *[v for row in directions for v in row]),
                struct.pack("<3i", *ambient),
                planar, ambient)

    DAY = ([[6000, 5760, 4800], [400, 400, 1600], [0, 0, 0]],
           [[-3750, -1237, -1087], [3592, -251, 1949], [0, -4096, 0]],
           [60, 60, 52])
    NIGHT = ([[240, 560, 1880], [48, 48, 496], [0, 0, 0]],
             [[-3750, -1237, -1087], [3592, -251, 1949], [0, -4096, 0]],
             [72, 76, 72])

    def check_rig(label, spec):
        gains, dirs, amb, planar, ambient = rig_expect(*spec)
        check(f"{label}: the gains are PLANAR at 0x800F5AF4",
              RAM.read(L.RIG_GAINS, 18) == gains,
              RAM.read(L.RIG_GAINS, 18).hex())
        check(f"{label}: the directions are interleaved at 0x800F5B14",
              RAM.read(L.RIG_DIRECTIONS, 18) == dirs,
              RAM.read(L.RIG_DIRECTIONS, 18).hex())
        check(f"{label}: the ambient is three int32 at 0x800F5B40",
              RAM.read(L.RIG_AMBIENT, 12) == amb,
              RAM.read(L.RIG_AMBIENT, 12).hex())
        # cnt16-20: m[3][3] two shorts to a word, the ninth alone.
        want = [(planar[0] & 0xFFFF) | ((planar[1] & 0xFFFF) << 16),
                (planar[2] & 0xFFFF) | ((planar[3] & 0xFFFF) << 16),
                (planar[4] & 0xFFFF) | ((planar[5] & 0xFFFF) << 16),
                (planar[6] & 0xFFFF) | ((planar[7] & 0xFFFF) << 16),
                planar[8] & 0xFFFF]
        check(f"{label}: the GTE colour matrix is cnt16-20",
              RAM.cp2c[16:21] == want, f"{RAM.cp2c[16:21]} != {want}")
        # SetBackColor is `sll aN, aN, 4` -- the x16 is the register's, not
        # ours, and shipping the raw byte would leave the map ~16x too dark.
        check(f"{label}: the background colour is the ambient x16",
              RAM.cp2c[13:16] == [v * 16 for v in ambient],
              f"{RAM.cp2c[13:16]} vs {ambient}")
        check(f"{label}: the DIRECTION registers are left alone",
              RAM.cp2c[8:13] == [0, 0, 0, 0, 0],
              f"cnt8-12 = {RAM.cp2c[8:13]} -- the compose re-loads them every "
              f"frame, so writing them hides that from anyone reading the plan")

    RAM = fresh_ram()
    res, err = push()
    check("a push with a rig finishes", res == {"FINISHED"}, f"{res} {err}")
    check("the rig push used the second transport at all", RAM.gte_execs >= 1,
          f"{RAM.gte_execs} GTE exec(s)")
    check_rig("state 0", DAY)
    check("the report names the rig push",
          any("light rig:" in ln and "GTE register" in ln
              for ln in last_push()), str(last_push()))
    check("the report warns that the GTE half is not reload-proof",
          any("does NOT survive a map reload" in ln for ln in last_push()),
          str(last_push()))
    check("the rig is no longer named as UNPUSHED",
          not any("map_states[].light_rig" in ln for ln in last_push()),
          str(last_push()))

    # The identity arm: a second press moves no RAM byte, and still writes the
    # registers -- because nothing in the machine reloads them, so "already
    # there" is not a reason to skip them.
    _before = bytes(RAM.mem)
    _gte_before = RAM.gte_execs
    push()
    check("a second push changes no rig RAM byte", bytes(RAM.mem) == _before)
    check("a second push writes the registers anyway",
          RAM.gte_execs > _gte_before, f"{RAM.gte_execs} vs {_gte_before}")

    # The AIM, on the rig. MAP001 a0's night state carries a different rig, so
    # moving the preview has to move the pushed bytes -- the arm that catches a
    # push wired to state 0 forever, which is exactly the complaint decision 9
    # was steered by ("I change map preview entry and hit push, nothing
    # happens").
    RAM = fresh_ram()
    _night = [i for i, st in enumerate(DOC["map_states"])
              if st.get("light_rig") and st["night"] == 1]
    check("the fixture has a night state with its own rig", len(_night) == 1,
          str(_night))
    ob["exmateria_map/preview_state"] = _night[0]
    res, err = push()
    check("a push aimed at the night state finishes", res == {"FINISHED"},
          f"{res} {err}")
    check_rig("the night state", NIGHT)
    check("the report names the night aim",
          any("night=1" in ln for ln in last_push()), str(last_push()))
    ob["exmateria_map/preview_state"] = 0

    # ---- the picture: the sheet to VRAM, the palettes to RAM --------------
    # The leg the artist actually pressed the button for. It spans two
    # memories: the sheet's pixels are VRAM and stay there, the palettes are
    # VRAM's CLUT rows and are re-uploaded from main RAM every frame, so a
    # palette write to VRAM is gone in 50 ms and the RAM block is the sink.
    RAM = fresh_ram()
    VRAM_NOW = vram_for(RAM)
    _clut_before = RAM.read(L.CLUT_BLOCK, L.CLUT_BLOCK_BYTES)
    res, err = push()
    check("a push with a sheet and palettes finishes", res == {"FINISHED"},
          f"{res} {err}")
    check("the sheet reached VRAM as four page rectangles",
          VRAM_NOW.posted[:4] == [f"texture page {p}" for p in range(4)],
          str(VRAM_NOW.posted))
    check("the report names the picture push",
          any("picture:" in ln and "texture sheet" in ln for ln in last_push()),
          str(last_push()))
    check("the sheet is no longer named as UNPUSHED",
          not any("map_states[].texture_sheet" in ln for ln in last_push()),
          str(last_push()))
    check("the palettes are no longer named as UNPUSHED",
          not any("map_states[].palettes" in ln for ln in last_push()),
          str(last_push()))

    # The sheet in VRAM is the DOCUMENT's blob, byte for byte -- the whole
    # point of surfacing `pack_4bpp`'s output from `assemble` rather than
    # PNG-encoding it and decoding it again on the way in.
    _doc, _files, _rep = export_document.assemble(ob)
    _at = L.aim(_doc["map_states"], 0)
    _blob = _rep.sheets[_at.sheet_row["texture_sheet"]]
    _wit = L.packet_witnesses(RAM, L.read_descriptors(
        L.read_descriptor_block(RAM))[0], _doc)
    _atv = VR.derive_addresses(_wit)
    check("VRAM holds the document's own 4bpp blob, byte for byte",
          VR.verify(VRAM_NOW, VR.plan_sheet(_blob, _atv)) == [],
          str([(r.label, n) for r, n in
               VR.verify(VRAM_NOW, VR.plan_sheet(_blob, _atv))]))
    check("the sheet blob is the disc's 131,072-byte layout",
          len(_blob) == VR.SHEET_BYTES, str(len(_blob)))

    # ---- the dissenting minority must be NAMED (#646) --------------------
    # Measured on a live Orbonne Monastery (MAP062): 380 of MAP022 a0's 385
    # witnesses put the sheet at (768, 0) and five put it at (768, 256). The
    # sheet now goes to the address the 380 agree on, which means those five
    # polygons keep whatever was already in VRAM -- and an artist who is not
    # told that reads five stale faces as a push that half worked.
    #
    # This fixture carries ONE textured polygon, so a minority cannot exist in
    # it. `picture_plan` is pure and takes the derivation as an argument, so it
    # is asked directly rather than through a document that cannot pose the
    # question.
    _cv = VR.clut_block(VRAM_NOW.read(), _atv)
    _cr = RAM.read(L.CLUT_BLOCK, L.CLUT_BLOCK_BYTES)
    _, _, _, _notes = UI.picture_plan(
        _at, _atv._replace(sheet_dissent=5, clut_dissent=2), _rep.sheets,
        _cr, _cv)
    _said = " ".join(_notes)
    check("a dissenting minority is NAMED, not silently dropped",
          "5 polygon(s)" in _said and "2" in _said
          and "keep the texture" in _said, _said[:300])
    _, _, _, _clean = UI.picture_plan(_at, _atv, _rep.sheets, _cr, _cv)
    check("...and a map whose packets all agree gets no such line",
          not any("keep the texture" in n for n in _clean), str(_clean))

    # ---- the palettes go to BOTH sinks ------------------------------------
    # This block replaces `no CLUT rectangle was ever POSTed to VRAM`, which
    # asserted the old decision: the engine re-uploads `CLUT_BLOCK` every
    # frame, so RAM is the sink and a VRAM CLUT write is gone in 50 ms.
    #
    # That is true on 42 of the corpus's 169 textured resources and false on
    # the other 127. The per-frame re-upload IS the palette ANIMATION, which
    # only the 42 carry -- and `MAP022.9` (Gariland, where it was measured) is
    # one of them. Measured [LIVE] 2026-08-27 on Orbonne (`MAP062.8`, no
    # animation): the RAM push was byte-perfect, 0 of 512 off the document,
    # and all 16 VRAM CLUT rows still held Orbonne's. Nothing re-uploaded it,
    # so the palettes never reached the screen at all.
    check("the palettes reached the RAM block",
          RAM.read(L.CLUT_BLOCK, L.CLUT_BLOCK_BYTES) == clut_block_bytes(DOC),
          "the CLUT block in RAM is not the document's")
    check("a CLUT row already holding its bytes is not re-POSTed",
          not any("CLUT" in lbl for lbl in VRAM_NOW.posted),
          "decision 6 at the new sink: `seed_clut` put the document's own "
          "palettes in both memories, so a correct push moves nothing -- "
          f"{VRAM_NOW.posted}")

    # ...and now the arm that would have caught it. The seeded fixture holds
    # the DOCUMENT's palettes in both memories, which is the one state in
    # which a push that reaches no sink at all still looks perfect. A swap is
    # the opposite: the map on screen is not the one in Blender, so both
    # memories hold somebody ELSE's colours.
    RAM = fresh_ram()
    VRAM_NOW = vram_for(RAM)
    _foreign = bytes(((b * 7 + 3) & 0xFF) for b in range(L.CLUT_BLOCK_BYTES))
    RAM.poke(L.CLUT_BLOCK, _foreign)
    for _row in range(L.CLUT_ROWS):
        _o = VR.CLUT_Y * VR.PITCH + _row * L.CLUT_ENTRIES * 2
        VRAM_NOW.vram[_o:_o + 32] = _foreign[_row * 32:(_row + 1) * 32]
    # Both memories agree, which is what `check_clut_block` demands and what
    # MAP062 really looked like before the push -- and note that it PASSED
    # there and the write still went nowhere. Agreement says `CLUT_BLOCK` is
    # the block feeding the screen; it does not say a write to it arrives.
    res, err = push()
    check("a push over a FOREIGN CLUT block finishes", res == {"FINISHED"},
          f"{res} {err}")
    check("the palettes reached VRAM's CLUT rows too",
          VR.clut_block(VRAM_NOW.read(), _atv) == clut_block_bytes(DOC),
          "VRAM's CLUT rows are not the document's -- a RAM-only palette push "
          "is inert on the 127 resources that carry no palette animation")
    check("...as one rectangle per row, named so a readback can report one",
          [lbl for lbl in VRAM_NOW.posted if "CLUT" in lbl]
          == [f"CLUT row {r}" for r in range(L.CLUT_ROWS)],
          str([lbl for lbl in VRAM_NOW.posted if "CLUT" in lbl]))
    check("the RAM sink was written in the same press",
          RAM.read(L.CLUT_BLOCK, L.CLUT_BLOCK_BYTES) == clut_block_bytes(DOC),
          "the RAM block is not the document's -- the VRAM sink must not "
          "REPLACE it: on the 42 animating resources RAM is the only durable "
          "one, and it overwrites these rows on the next frame")
    check("the report names both sinks",
          any("VRAM byte(s) of palette" in ln for ln in last_push())
          and any("RAM byte(s) of palette" in ln for ln in last_push()),
          str(last_push()))

    # Decision 6: skip the write when the bytes already match, and say so.
    _posted_before = len(VRAM_NOW.posted)
    push()
    check("a second press re-POSTs no sheet page",
          len(VRAM_NOW.posted) == _posted_before,
          f"{VRAM_NOW.posted[_posted_before:]}")

    # Decision 10: a group that declares no palettes pushes its SHEET anyway
    # and says why. 38.5% of corpus states are in that position, and refusing
    # the press for one would strand a perfectly pushable sheet.
    RAM = fresh_ram()
    _v = vram_for(RAM)
    _night_sheet = [i for i, st in enumerate(DOC["map_states"])
                    if st["night"] == 1]
    ob["exmateria_map/preview_state"] = _night_sheet[0]
    res, err = push()
    ob["exmateria_map/preview_state"] = 0
    check("a group with no TEXTURE row is named, not crashed on",
          res in ({"FINISHED"}, {"CANCELLED"}),
          f"{res} {err}")
    check("the no-sheet group is explained by name",
          any("night=1" in ln for ln in last_push()), str(last_push()))

    # ---- the base survives a DELETION, at IMPORT length -------------------
    # The old base walked `me.polygons` in CURRENT order, so deleting face 5
    # of 24 shifted every survivor down a slot, the base plan claimed slot 5
    # held surviving-face-5's shadow when slot 5 still held old face 5, and
    # the self-check fired on a perfectly healthy shrink. The marker holds
    # the imported list verbatim and a deleted face cannot reach it.
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.faces.ensure_lookup_table()
    bm.faces.remove(bm.faces[0])
    bm.to_mesh(me)
    bm.free()
    base = UI.base_polygons(ob)
    check("a deletion leaves the base at IMPORT length",
          base is not None and len(base) == len(DOC["polygons"])
          and len(me.polygons) < len(DOC["polygons"]),
          f"{len(me.polygons)} faces left, base {base and len(base)}")
    check("the base after a deletion is still the imported document",
          base is not None
          and [p["positions"] for p in base]
          == [p["positions"] for p in DOC["polygons"]],
          json.dumps(base)[:200])

    # ---- SHRINK, and a bucket emptied to zero ----------------------------
    # The mesh has just lost its only textured_quad, so this document empties
    # that bucket. `plan_document` skips a bucket with no polygons, so a count
    # write driven by the plan dict would leave the old count standing and the
    # engine would go on drawing a slot the document no longer has.
    RAM = fresh_ram()
    UI._LAST_PUSH.clear()
    res, err = push()
    _d = L.parse_descriptor(
        RAM.read(L.DESCRIPTOR_BASE, L.DESCRIPTOR_STRIDE * 9), 0)
    check("a document that SHRANK pushes", res == {"FINISHED"}, f"{res} {err}")
    check("an emptied bucket's count is written to ZERO",
          _d.counts == (0, 0, 0, 1), str(_d.counts))
    check("the report names the shrink", 
          any("textured_quad 1 -> 0" in ln for ln in last_push()),
          str(last_push()))
    # The second press of the same session, after a shrink. The base is the
    # IMPORTED list, so it still plans the slot range the import occupied --
    # a base sized off the live descriptor would now be one bucket short and
    # would check a range that is not the one the import wrote.
    check("the base is still the import's length after the shrink",
          len(UI.base_polygons(ob)) == len(DOC["polygons"]),
          str(len(UI.base_polygons(ob))))
    res, err = push()
    check("a second press after a shrink is not blocked by the first",
          res == {"FINISHED"}, f"{res} {err}")

    # ---- the report is COPYABLE, and collapsed by default -----------------
    # Reported from use, twice: "it shoves a ton of crap in the right space",
    # and "when errors come up I can't copy the contents". A Blender label
    # cannot be selected and neither can an operator's error toast, so the
    # refusal the artist most wants to paste was the one thing they could not
    # get out of the application.
    #
    # A panel cannot be drawn in --background (there is no region), so the
    # draw path is exercised against a recording stand-in. That catches what
    # actually breaks here -- a missing property, a wrong operator id, an
    # attribute error in draw -- while leaving pixels to a real Blender.
    class FakeLayout:
        def __init__(self, sink):
            self.sink = sink
            #: `(idname, props)` per `operator()` call. `sink` records the id;
            #: a button that presets a property is only half described by it,
            #: and the preset half is where `skip_selfcheck` could reach the
            #: panel without anything noticing.
            self.ops = []

        def box(self):
            return self
        def row(self, **kw):
            return self
        def column(self, **kw):
            return self
        def label(self, text="", icon=""):
            self.sink.append(("label", text, icon))
        def prop(self, *a, **kw):
            # The NAME, not just the icon. `prop` on a property that does not
            # exist draws nothing in a real Blender, exactly as `operator` on an
            # unregistered id does -- so the name has to survive to where it can
            # be resolved against the object.
            self.sink.append(("prop", a[1] if len(a) > 1 else "", ""))
        def operator(self, idname, **kw):
            self.sink.append(("operator", idname, ""))
            props = type("Props", (), {"key": "", "title": ""})()
            self.ops.append((idname, props))
            return props

    from exmateria_map import import_document as IMP

    # ADR-0185 decision 5: the panel is a STATUS ROW plus the refusals, and the
    # Log pane is where a report is read.  There is no "open" size any more and
    # no `exmateria_map_report_expanded` -- the disclosure triangle is what made
    # a refusal hideable, which is the one thing this block must never allow.
    _stored_lines = json.loads(ob[UI.LAST_PUSH_KEY])
    _stored_refusals = [ln for ln in _stored_lines if ln.startswith("REFUSE")]
    rows_closed = []
    IMP._stored_report(FakeLayout(rows_closed), ob, UI.LAST_PUSH_KEY, "Last push:")
    _row_labels = [r for r in rows_closed if r[0] == "label"]
    check("the report is a status row and its refusals, never the whole report",
          len(_stored_lines) > len(_stored_refusals) + 1
          and len(_row_labels) == 1 + len(_stored_refusals),
          f"{len(_row_labels)} label row(s) for {len(_stored_lines)} report "
          f"line(s), {len(_stored_refusals)} of them refusals -- the "
          f"precondition is that there IS something it is not drawing")
    check("the report still offers the copy button",
          any(r[0] == "operator" and r[1] == "map.copy_report"
              for r in rows_closed), str(rows_closed[:4]))
    check("the report draws no disclosure triangle",
          not any(r[0] == "prop" for r in rows_closed)
          and not hasattr(bpy.types.Object, "exmateria_map_report_expanded"),
          str(rows_closed[:4]))

    # A refusal is the whole reason the report exists and is never hidden.
    _saved = ob[UI.LAST_PUSH_KEY]
    ob[UI.LAST_PUSH_KEY] = json.dumps(
        ["REFUSE: the sky fell on it", "pushed 0 changed byte(s)"])
    rows_refuse = []
    IMP._stored_report(FakeLayout(rows_refuse), ob, UI.LAST_PUSH_KEY, "Last push:")
    check("a REFUSE line is drawn in full",
          any("sky fell" in str(r[1]) for r in rows_refuse), str(rows_refuse))
    check("the header counts the refusals",
          any("refusal" in str(r[1]) for r in rows_refuse), str(rows_refuse))

    # The clipboard is the point of the feature and is NOT observable here:
    # measured, not assumed -- a bare `wm.clipboard = x` does not round-trip in
    # --background. So this arm is a CONTROL on that limitation rather than a
    # check of the feature, and it is written to go RED if a future Blender
    # starts round-tripping, which is the signal to strengthen it. The write
    # itself is covered by the text block below: one `execute`, two sinks, and
    # the observable sink stands in for the one a headless run cannot see.
    bpy.context.view_layer.objects.active = ob
    bpy.context.window_manager.clipboard = "canary-123"
    check("the clipboard is unobservable headless, so the TEXT BLOCK grades it",
          bpy.context.window_manager.clipboard != "canary-123",
          "the clipboard round-trips now -- assert the report content here")
    res_copy = bpy.ops.map.copy_report(key=UI.LAST_PUSH_KEY, title="Last push:")
    check("the copy operator runs to completion", res_copy == {"FINISHED"},
          str(res_copy))
    check("the copy operator writes a SELECTABLE text block",
          IMP.REPORT_TEXT_NAME in bpy.data.texts
          and "sky fell" in bpy.data.texts[IMP.REPORT_TEXT_NAME].as_string(),
          str(list(bpy.data.texts.keys())))
    # Pressed twice, it must not breed datablocks.
    bpy.ops.map.copy_report(key=UI.LAST_PUSH_KEY, title="Last push:")
    check("pressing copy twice reuses the one text block",
          sum(1 for t in bpy.data.texts if t.name.startswith(IMP.REPORT_TEXT_NAME))
          == 1, str(list(bpy.data.texts.keys())))
    ob[UI.LAST_PUSH_KEY] = _saved

    # The bake panel shares the renderer rather than carrying a third copy.
    from exmateria_map import lighting_bake as LB
    ob["exmateria_map/last_bake"] = json.dumps(["a bake line"])
    rows_bake = []
    LB._bake_report(FakeLayout(rows_bake), ob)
    check("the bake report shares the collapsing renderer",
          any(r[0] == "operator" and r[1] == "map.copy_report"
              for r in rows_bake), str(rows_bake))
    del ob["exmateria_map/last_bake"]

    # ---- `What a push carries` is DELETED, and the limit is not ----------
    # It was a DEFAULT_CLOSED sub-panel plus a `NOT_CARRIED` prose table.
    # Reported from use: *"I don't care about the 'what a push carries'
    # section. delete it. that belongs in a console or something ... you are
    # putting console stuff in the ui area."*
    check("the carries sub-panel is gone",
          not hasattr(UI, "MAP_PT_live_push_carries")
          and not hasattr(UI, "NOT_CARRIED"),
          "MAP_PT_live_push_carries / NOT_CARRIED is still here -- the artist "
          "deleted the panel, not the limit it documented")

    # ADR-0186 Amendment 3's Consequences: *"the panel whose job is to say what
    # a push carries is wrong about the leg this loop depends on."*  It said the
    # sheet and the CLUT rows had no live sink and to use `tools/live_push.py`
    # -- for as long as step 5c had been pushing both, and after that tool was
    # deleted.  A panel that RESTATES `UNPUSHED` can disagree with it.  The
    # deletion settles that permanently: there is now only the table, read once
    # per push by `unpushed_lines`, and these arms hold it that way.
    said = " ".join(UI.unpushed_lines(set()))
    check("the limit no longer names a tool that is deleted",
          "live_push.py" not in said and "no live sink" not in said,
          said[:300])
    check("...and every line it says is a field `UNPUSHED` names",
          all(any(f in ln for f in L.UNPUSHED) for ln in
              UI.unpushed_lines(set())),
          f"{UI.unpushed_lines(set())} vs {sorted(L.UNPUSHED)}")
    # The other direction, and the one a restating panel got wrong: a field
    # that gains no sink must APPEAR, prose or no prose.  Seeded rather than
    # asserted off today's entries, which would pass on a function that ignored
    # the table entirely.
    L.UNPUSHED["a field with no sink and no prose"] = "seeded"
    try:
        seeded_said = " ".join(UI.unpushed_lines(set()))
    finally:
        del L.UNPUSHED["a field with no sink and no prose"]
    check("a new UNPUSHED field is said with no edit to the addon",
          "a field with no sink and no prose" in seeded_said,
          seeded_said[:300])
    # ...and it reaches the artist, which is the whole of what the panel did.
    # `finish` stores the lines, records them to the Log AND prints them; the
    # push panel draws no text at all now, so if this call went the limit would
    # be nowhere.
    import ast as _a
    import pathlib as _pl
    _src = _a.parse(_pl.Path(UI.__file__).read_text())
    # The anchor is `_transport` as well as `execute` because the push's
    # transport half moved OUT of the operator, so the settle can run it off
    # the main thread. `next` takes a default for a reason: without one this
    # raised `StopIteration` and killed the harness, which reports as FATAL
    # rather than as the one failed check it is.
    _ex = next((n for n in _a.walk(_src)
                if isinstance(n, _a.FunctionDef)
                and n.name in ("execute", "_transport")
                and any("unpushed_lines" == getattr(c.func, "id", None)
                        for c in _a.walk(n) if isinstance(c, _a.Call))), None)
    check("the push operator is what says it now",
          _ex is not None,
          "no push code calls unpushed_lines -- the sub-panel was "
          "deleted TO the console, so the console has to receive it")

    # ---- the push LEAVES THE MAIN THREAD ---------------------------------
    # Reported from use: *"when I am painting, I will let go and stop, and
    # then in a bit it will randomly freeze for a bit before starting again --
    # it's awkward and slow."* The freeze was `push_after_compile` calling the
    # operator and waiting out the whole round trip on Blender's own thread.
    # Measured on MAP022 a0, this box: `assemble` is 375 ms of Blender reads
    # that cannot leave, and the transport is about 670 ms that is not work at
    # all -- 16 whole-RAM GETs at 31 ms plus 5 whole-VRAM GETs at 34 ms, spent
    # waiting on another process. So the transport moved to a worker, the same
    # split decision 30 already made for the compile.
    import threading as _th
    import time as _tm
    from exmateria_map import settle_op as _SO

    # 1. THE CONTRACT, and it is the whole of why this is safe: the transport
    #    may name `bpy` nowhere. Not "rarely" and not "only on the happy path"
    #    -- `bpy` from a worker is undefined behaviour, and the symptom is a
    #    crash in a thread whose traceback the artist never sees. Read off the
    #    SOURCE rather than by running it, because most of this function is
    #    refusal branches a runtime arm would never take.
    def _names_in(tree, *fns):
        return {n.name: sorted({c.id for c in _a.walk(n)
                                if isinstance(c, _a.Name)
                                and c.id in ("bpy", "self")})
                for n in _a.walk(tree)
                if isinstance(n, _a.FunctionDef) and n.name in fns}

    _TRANSPORT = ("push_transport", "_transport")
    check("the push's transport half names no `bpy` and no `self`",
          _names_in(_src, *_TRANSPORT) == {k: [] for k in _TRANSPORT},
          str(_names_in(_src, *_TRANSPORT)))
    # ...and the arm can go red, seeded on the source it just read. Without
    # this it passes just as well against a function that was renamed away.
    _seed = _a.parse(_pl.Path(UI.__file__).read_text().replace(
        "    # 2. the gate", "    bpy.context.scene\n    # 2. the gate", 1))
    check("...and a single `bpy` put back into it is caught",
          _names_in(_seed, *_TRANSPORT) != {k: [] for k in _TRANSPORT},
          str(_names_in(_seed, *_TRANSPORT)))

    # 2. IT RETURNS WHILE THE TRANSPORT IS STILL RUNNING. The fake's first
    #    whole-RAM GET is held on an Event, which is the emulator being slow
    #    -- exactly the thing the artist was waiting for.
    RAM = fresh_ram()
    UI._LAST_PUSH.clear()
    UI._BG.update(thread=None, ob_name="", done=None, pending=False)
    _SO.resume_pushing()
    _gate = _th.Event()
    _real_get = FakeRam.get

    def _held_get(self, *a, **k):
        _gate.wait(20.0)
        return _real_get(self, *a, **k)

    FakeRam.get = _held_get
    _t0 = _tm.monotonic()
    _r = _SO.push_after_compile(ob, "test")
    _returned_ms = (_tm.monotonic() - _t0) * 1000
    check("the settle's push returns while the transport is still running",
          _r == {"RUNNING_MODAL"} and UI.background_push_busy(),
          f"{_r} busy={UI.background_push_busy()} after {_returned_ms:.0f}ms")
    # A second settle inside that window is COALESCED, not queued: queueing
    # would send the emulator a sheet the artist has already painted over.
    check("a push started while one is in flight is coalesced",
          _SO.push_after_compile(ob, "test") is None and UI._BG["pending"],
          str(UI._BG))
    check("nothing lands while the transport is still in flight",
          UI.background_push_land() is None)
    _gate.set()
    for _ in range(600):
        if not UI.background_push_busy():
            break
        _tm.sleep(0.02)
    FakeRam.get = _real_get
    check("the worker finished", not UI.background_push_busy())

    # 3. THE REPORT LANDS ON THE MAIN THREAD. `push_report` writes the marker
    #    property and the Log's Text datablock, both of which are `bpy` -- so
    #    a worker that landed its own report would be the very crash arm 1
    #    exists to prevent.
    ob[UI.LAST_PUSH_KEY] = "[]"
    _SO._drain_push()
    check("the finished push's report reaches the marker",
          any("pushed" in ln for ln in last_push()), str(last_push())[:200])
    # ...and the coalesced one is sent, rather than dropped. The artist stopped
    # painting twice; both sheets must reach the emulator, the second last.
    check("the coalesced push is sent once the first one lands",
          UI.background_push_busy() or UI._BG["done"] is not None,
          str(UI._BG))
    for _ in range(600):
        if not UI.background_push_busy():
            break
        _tm.sleep(0.02)
    _SO._drain_push()
    check("and the queue empties rather than looping",
          UI._BG["done"] is None and not UI._BG["pending"], str(UI._BG))

    # 4. A REFUSAL IS STILL IMMEDIATE. `lua.check()` and `assemble`'s refusals
    #    both run on the calling thread, so "there is no emulator" is still an
    #    answer the settle gets now rather than a tick later -- which is what
    #    keeps the back-off in `push_after_compile` working the way it did.
    RAM.up = False
    _SO.resume_pushing()
    _r = _SO.push_after_compile(ob, "test")
    check("a push with no emulator is refused on the calling thread",
          _r == {"CANCELLED"} and not UI.background_push_busy(), str(_r))
    check("...and it still backs off rather than latching",
          _SO._PUSH["quiet_until"] > _tm.monotonic())
    RAM.up = True
    _SO.resume_pushing()
    UI._BG.update(thread=None, ob_name="", done=None, pending=False)

    # ---- the two panels an artist reaches PCSX-Redux through --------------
    # The emulator does not load the live link's handlers by itself, so the
    # addon has to offer a way in. Both surfaces are drawn here because a
    # `draw` that raises renders everything BEFORE it and nothing after -- a
    # wrong operator id would take out the rest of the panel and the harness
    # would see a green push.
    rows_push = []
    _push_layout = FakeLayout(rows_push)
    UI.MAP_PT_live_push.draw(
        type("P", (), {"layout": _push_layout})(), bpy.context)
    push_ops = [r[1] for r in rows_push if r[0] == "operator"]
    check("the push panel offers the push", "map.live_push" in push_ops,
          str(push_ops))
    # Reported from use as confusion: the launch button was in the preferences
    # only, and the moment an artist needs it is the moment a push has just
    # come back "no emulator answering" -- in the viewport, not there.
    check("...and launching the emulator, at the button that needs it",
          "exmateria_map.launch_pcsx" in push_ops, str(push_ops))

    # The swap is a BUTTON, not a hidden keyword. It is the artist's only way
    # to say "the emulator holds a different map and replacing it is the
    # point", and the operator cannot infer it -- no RAM address holding the
    # current map id is known (decision 2), so the declaration has to come
    # from the person who loaded the savestate.
    _swap = [pr for i, pr in _push_layout.ops
             if i == "map.live_push" and getattr(pr, "replace_loaded_map",
                                                 False)]
    check("the push panel offers replacing the loaded map as its own button",
          len(_swap) == 1, f"{len(_swap)} of {_push_layout.ops}")
    # `replace_loaded_map` presets a property, and `FakeLayout` accepts any
    # attribute -- so a name the operator never registered would record
    # perfectly here and raise in a real Blender. Blender's own answer is what
    # settles it.
    _rna = [pr.identifier
            for pr in bpy.ops.map.live_push.get_rna_type().properties]
    check("...and it is a property the operator really registered",
          "replace_loaded_map" in _rna, str(_rna))
    # The escape hatch stays out of reach. A swap has a proof it can pass, so
    # an artist never needs the one that has none -- and a panel that offered
    # it would turn "I could not get a push through" into a habit of pushing
    # thousands of bytes to unproven addresses.
    check("...and no button on this panel presets `skip_selfcheck`",
          not any(getattr(pr, "skip_selfcheck", False)
                  for _, pr in _push_layout.ops),
          str([(i, vars(pr)) for i, pr in _push_layout.ops]))

    # ---- decision 12: the camera sync ------------------------------------
    # The artist's rule: *"what I see in Blender should be what I see in
    # PCSX-Redux."* The arithmetic is the core's and `tests/test_live_link.py`
    # has it against the battle savestate; what is graded here is the half that
    # only exists inside Blender -- the section's four controls, the one
    # viewport the sync refuses, and whether a match REPORTS a pose that did
    # not land rather than going green over an unchanged picture.

    class Ctx:
        """`bpy.context` with a viewport bolted on.

        Blender in `--background` has no `space_data`, so a panel that reads
        the 3D view cannot be drawn against the real context -- and drawing it
        against a fake one is not a compromise: `view_perspective` is a
        property of the SPACE, so the panel has to be graded with a space and
        without one, and the second is a real state (a `draw` that raises
        renders everything before it and nothing after).
        """

        def __init__(self, real, space):
            self._real, self.space_data = real, space

        def __getattr__(self, name):
            return getattr(self._real, name)

    class FakeView:
        """A `RegionView3D`, using Blender's own `mathutils` for the rotation
        so the quaternion-to-matrix step is really exercised."""

        def __init__(self, perspective="ORTHO", rotation=None,
                     location=(182.0, 154.0, -4.75), distance=336.0):
            import mathutils
            self.view_perspective = perspective
            self.view_location = mathutils.Vector(location)
            self.view_rotation = rotation or mathutils.Quaternion((1, 0, 0, 0))
            self.view_distance = distance

    _view = FakeView()
    rows_cam = []
    _cam_layout = FakeLayout(rows_cam)
    UI.MAP_PT_live_camera.draw(
        type("P", (), {"layout": _cam_layout})(),
        Ctx(bpy.context, type("S", (), {"region_3d": _view})()))
    cam_ops = [r[1] for r in rows_cam if r[0] == "operator"]
    cam_props = [r[1] for r in rows_cam if r[0] == "prop"]
    check("the camera section offers `Match camera`",
          "map.live_camera_match" in cam_ops, str(cam_ops))
    # Decision 12: the ortho toggle IS its own indicator, which is why there is
    # no warning line beside it. That only holds if what is drawn shows the
    # current state -- a plain operator button would not.
    check("...and the ortho toggle, drawn as the view's own property",
          "view_perspective" in cam_props, str(cam_props))
    check("...and the zoom dial", "live_camera_zoom_dial" in cam_props,
          str(cam_props))
    # The panel's own rule, from use: *"you are putting console stuff in the ui
    # area."* A section that grew a warning line about unit sprites would be
    # the same defect the deleted sub-panel was.
    check("...and no prose at all",
          not [r for r in rows_cam if r[0] == "label"], str(rows_cam))
    rows_cam_bare = []
    UI.MAP_PT_live_camera.draw(
        type("P", (), {"layout": FakeLayout(rows_cam_bare)})(), bpy.context)
    check("the section draws with no viewport at all rather than raising",
          True, f"{len(rows_cam_bare)} rows")

    _prefs_now = bpy.context.preferences.addons["exmateria_map"].preferences
    check("the zoom dial is a preference that really exists",
          hasattr(_prefs_now, "live_camera_zoom_dial"))

    # ---- a match, against the fake emulator ------------------------------
    RAM = fresh_ram()
    _before = bytes(RAM.mem)
    _client = L.RamClient()
    _pose, _lines = UI.push_camera(_client, _view, dial=1.0)
    _touched = sorted({L.RAM_BASE + i for i in range(L.RAM_BYTES)
                       if RAM.mem[i] != _before[i]})
    _spans = sorted({a for a in (L.WORK_POSITION, L.WORK_ROTATION,
                                 L.SPRITE_SCALE, L.CAMERA_VERTICAL_DATUM)})
    _in_span = all(any(a <= t < a + 12 for a in _spans) for t in _touched)
    check("a match writes the camera sinks and nothing else",
          _in_span and _touched,
          f"{len(_touched)} bytes, first {hex(_touched[0]) if _touched else '-'}")
    check("...at the engine's own widths and values",
          RAM.read(L.WORK_POSITION, 12) == struct.pack(
              "<3i", 745472, 19456, 630784)
          and RAM.read(L.WORK_ROTATION, 6) == struct.pack("<3h", 1024, 0, 0)
          and RAM.read(L.SPRITE_SCALE, 12) == struct.pack(
              "<3i", 4096, 4096, 4096)
          and RAM.read(L.CAMERA_VERTICAL_DATUM, 4) == struct.pack("<i", 120),
          RAM.read(L.WORK_ROTATION, 6).hex())
    # The dial has to reach the write. A dial the panel draws and the push
    # ignores is the same shape of defect as a `prop` on a property that does
    # not exist: everything renders, nothing moves.
    RAM = fresh_ram()
    UI.push_camera(L.RamClient(), _view, dial=2.0)
    check("...and the dial reaches the write",
          RAM.read(L.SPRITE_SCALE, 4) == struct.pack("<i", 8192),
          RAM.read(L.SPRITE_SCALE, 4).hex())

    # ---- the readback, which is the engine's arithmetic, not ours ---------
    RAM = fresh_ram()
    RAM.rebuild_camera(honest=False)         # a frame at the ENGINE's own pose
    _pose, _lines = UI.push_camera(L.RamClient(), _view)
    check("a pose the engine never rebuilt from is REPORTED, not hidden",
          any("did not" in ln or "disagree" in ln for ln in _lines),
          str(_lines))
    RAM = fresh_ram()
    _client = L.RamClient()
    _real_write = _client.write

    def _write_then_frame(plan):
        n = _real_write(plan)
        RAM.rebuild_camera()                 # the vsync -- see below
        return n

    _client.write = _write_then_frame
    # The fake makes that vsync land BETWEEN the write and the read. The real
    # button does NOT wait for one -- a localhost round trip is a fraction of a
    # 16.7 ms frame, so the readback can genuinely precede the engine's rebuild
    # and report a disagreement about a write that was fine. This harness
    # cannot represent that boundary: pressing twice is what tells the two
    # apart, and it is one of the reasons the continuous sync does not read
    # back at all.
    _pose, _lines = UI.push_camera(_client, _view)
    # Positively, not by the absence of a complaint: a `push_camera` that
    # skipped the readback altogether would say nothing either way and pass an
    # arm written as "no disagreement was reported".
    check("...and the engine's own matrix agreeing is what says it landed",
          any("rebuilt its own view matrix" in ln for ln in _lines)
          and not any("did not" in ln for ln in _lines), str(_lines))

    # ---- the one viewport the sync refuses -------------------------------
    RAM = fresh_ram()
    _before = bytes(RAM.mem)
    _refused = ""
    try:
        UI.push_camera(L.RamClient(), FakeView(perspective="CAMERA"))
    except L.LiveLinkError as e:
        _refused = str(e)
    check("looking through a scene camera is refused, and named",
          "scene camera" in _refused, _refused or "no refusal")
    check("...and a refused match writes NOTHING",
          bytes(RAM.mem) == _before)
    # Perspective is NOT refused: the toggle is the indicator, and the addon
    # does not reach in and change a view the artist set.
    RAM = fresh_ram()
    UI.push_camera(L.RamClient(), FakeView(perspective="PERSP"))
    check("a perspective viewport still syncs",
          RAM.read(L.WORK_ROTATION, 6) == struct.pack("<3h", 1024, 0, 0))

    # ---- the continuous sync, decision 12 part 2 -------------------------
    # The ticker's decisions are graded in `test_live_link.py`, where they need
    # neither `bpy` nor a socket. What is graded HERE is the half that has
    # both: that a tick really writes, really does NOT read, and that the panel
    # and the preferences reach the thing that decides.
    check("...and the continuous sync toggle", "live_camera_sync" in cam_props,
          str(cam_props))
    check("the sync toggle is a preference that really exists, and is ON",
          getattr(_prefs_now, "live_camera_sync", None) is True,
          repr(getattr(_prefs_now, "live_camera_sync", "missing")))

    RAM = fresh_ram()
    _tick_ticker = L.CameraSyncTicker()
    _client = L.RamClient()
    _reads = []
    _client.read_live = lambda *a, **k: _reads.append(a) or b"\0" * 18
    _lines = UI.sync_camera(_client, _view, ticker=_tick_ticker)
    check("a tick writes the pose the viewport is holding",
          RAM.read(L.WORK_ROTATION, 6) == struct.pack("<3h", 1024, 0, 0),
          RAM.read(L.WORK_ROTATION, 6).hex())
    # The readback is the BUTTON's instrument. On a timer it is a second round
    # trip and a Log line per tick to re-answer what one press already
    # answered -- so its absence is asserted, not assumed.
    check("...and does NOT read back, unlike the button",
          _reads == [], f"{len(_reads)} reads")
    check("...and says nothing while it is working",
          _lines == [], str(_lines))

    RAM = fresh_ram()
    _before = bytes(RAM.mem)
    UI.sync_camera(L.RamClient(), _view, ticker=_tick_ticker)
    check("a tick on an UNCHANGED view writes nothing at all",
          bytes(RAM.mem) == _before,
          "a still viewport must cost no traffic -- this is what makes ON by "
          "default free")
    RAM = fresh_ram()
    _moved = FakeView(location=(182.0, 155.0, -4.75))
    UI.sync_camera(L.RamClient(), _moved, ticker=_tick_ticker)
    check("...and a tick on a MOVED view writes again",
          RAM.read(L.WORK_POSITION, 12) != _before[
              L.WORK_POSITION - L.RAM_BASE:][:12],
          RAM.read(L.WORK_POSITION, 12).hex())

    # A dead emulator, which is the state the toggle spends most of its life in.
    class _DeadClient:
        def write(self, plan):
            raise L.LiveLinkError("connection refused")

    _dead_ticker = L.CameraSyncTicker()
    _said = UI.sync_camera(_DeadClient(), _view, ticker=_dead_ticker)
    check("a tick that cannot reach the emulator says so once",
          any("cannot reach" in ln for ln in _said), str(_said))
    check("...and does not say it again on the next tick",
          UI.sync_camera(_DeadClient(), _view, ticker=_dead_ticker) == [])
    check("...and backs the rate off while it is failing",
          _dead_ticker.interval() == L.CAMERA_SYNC_BACKOFF,
          str(_dead_ticker.interval()))
    RAM = fresh_ram()
    _back = UI.sync_camera(L.RamClient(), _view, ticker=_dead_ticker)
    check("...and the pose it could not deliver is delivered on recovery",
          RAM.read(L.WORK_ROTATION, 6) == struct.pack("<3h", 1024, 0, 0)
          and any("again" in ln for ln in _back), str(_back))

    # The two viewports a tick must survive rather than refuse. A press is a
    # request and `push_camera` raises; a tick is a standing offer.
    RAM = fresh_ram()
    _before = bytes(RAM.mem)
    _idle = UI.sync_camera(L.RamClient(), FakeView(perspective="CAMERA"),
                           ticker=L.CameraSyncTicker())
    check("a viewport looking through a scene camera makes the sync IDLE",
          any("idle" in ln for ln in _idle) and bytes(RAM.mem) == _before,
          str(_idle))
    _none = UI.sync_camera(L.RamClient(), None, ticker=L.CameraSyncTicker())
    check("...and so does having no 3D viewport at all, rather than raising",
          any("idle" in ln for ln in _none), str(_none))

    # The timer is the thing that makes any of this happen without a press, and
    # `register()` is the only place it is armed.
    check("the addon armed the camera timer at register()",
          bpy.app.timers.is_registered(UI._camera_sync_timer),
          "an unarmed timer is a toggle that does nothing, with a panel row "
          "saying it is on")
    # The arm that keeps this harness off the artist's running emulator, and
    # the reason it is needed is measured rather than assumed: `--background`
    # Blender holds a window with a VIEW_3D area, so `_first_region_3d` finds a
    # real viewport here and an unguarded tick would POST a pose to whatever is
    # listening on port 8080.
    check("a headless Blender really does hold a viewport to be fooled by",
          bpy.app.background and UI._first_region_3d() is not None,
          f"background={bpy.app.background}, "
          f"region_3d={UI._first_region_3d()!r}")
    RAM = fresh_ram()
    _before = bytes(RAM.mem)
    check("...so the timer disarms itself there rather than poking port 8080",
          UI._camera_sync_timer() is None and bytes(RAM.mem) == _before,
          "returning None unregisters it; a tick that ran would have written "
          "the pose of a viewport nobody is looking at")

    # ---- decision 13: isolate the map ------------------------------------
    # An ACT, not a ticker. The walk and the gate arithmetic are the core's and
    # `tests/test_live_link.py` has them against the battle savestate; what is
    # graded here is the half that only exists inside Blender -- the panel's
    # two buttons, the session memory that makes a second press safe, and the
    # sinks an isolate must NOT touch.

    UNIT_STRIDE = 0x440

    def seed_units(ram, flags=((1, 1),) * 4, head=None):
        """Plant a unit list and the code gates the poke targets.

        The nodes are chained through `node+0x0` with the id at `node+0x4` --
        the offsets `unit_sprite_object_find` itself uses -- and each gate gets
        eight bytes of its own, so a poke that restored a constant instead of
        the saved bytes would be visible. The two renderers get a real
        `addiu sp,sp,-0x40` prologue; the camera leash is a LEAF and gets its
        real `lui a2,0x800a` entry instead, because a fixture that gave every
        gate the same shape would let a gate aimed at the middle of a function
        pass. That the shipped address really is a leaf is
        `tests/test_live_link.py`'s arm, against the savestate.
        """
        base = 0x800B0000
        for i, (show, dispatch) in enumerate(flags):
            node = base + i * UNIT_STRIDE
            nxt = 0 if i + 1 == len(flags) else node + UNIT_STRIDE
            ram.mem[node - L.RAM_BASE:node - L.RAM_BASE + 4] = struct.pack(
                "<I", nxt)
            ram.mem[node + 4 - L.RAM_BASE] = i
            ram.mem[node + L.UNIT_SHOW - L.RAM_BASE:
                    node + L.UNIT_SHOW - L.RAM_BASE + 2] = struct.pack(
                        "<H", show)
            ram.mem[node + L.UNIT_DISPATCH - L.RAM_BASE:
                    node + L.UNIT_DISPATCH - L.RAM_BASE + 2] = struct.pack(
                        "<H", dispatch)
        ram.mem[L.UNIT_LIST_HEAD - L.RAM_BASE:
                L.UNIT_LIST_HEAD - L.RAM_BASE + 4] = struct.pack(
                    "<I", base if head is None else head)
        # The camera zoom at its 1.0x identity, because a fresh RAM is all
        # zeroes and an isolate that ZEROED `sprite_scale` would then change
        # nothing and pass the arm that exists to catch exactly that. Measured:
        # seeded with the defect, the arm was green until this line existed.
        ram.mem[L.SPRITE_SCALE - L.RAM_BASE:
                L.SPRITE_SCALE - L.RAM_BASE + 12] = struct.pack(
                    "<3i", L.ZOOM_ONE, L.ZOOM_ONE, L.ZOOM_ONE)
        for _name, address in L.CODE_GATES:
            o = address - L.RAM_BASE
            words = ((0x3C06800A, 0x8CC61C48)
                     if address == L.CAMERA_LEASH
                     else (0x27BDFFC0, 0xAFBF0014))
            ram.mem[o:o + 8] = struct.pack("<II", *words)
        return base

    def forget_isolate():
        UI._ISOLATED["units"], UI._ISOLATED["gates"] = [], []

    rows_iso = []
    UI.MAP_PT_live_isolate.draw(
        type("P", (), {"layout": FakeLayout(rows_iso)})(), bpy.context)
    iso_ops = [r[1] for r in rows_iso if r[0] == "operator"]
    check("the isolate section offers `Isolate map`",
          "map.live_isolate" in iso_ops, str(iso_ops))
    check("...and `Restore`", "map.live_restore" in iso_ops, str(iso_ops))
    # Two buttons and NOT a checkbox: re-ticking an already-ticked box is a
    # no-op, and re-pressability is the mechanism decision 13 chose.
    check("...and no checkbox, because a re-press is the mechanism",
          not [r for r in rows_iso if r[0] == "prop"], str(rows_iso))
    # This sidebar's rule, from use: *"you are putting console stuff in the ui
    # area."* The cursor's uncertain target goes to the console, once a press.
    check("...and no prose at all",
          not [r for r in rows_iso if r[0] == "label"], str(rows_iso))
    # `FakeLayout.operator` records whatever string it is handed, so a panel
    # naming an UNREGISTERED operator records perfectly and draws NOTHING in a
    # real Blender. Both of these have to exist on `bpy.ops` as well.
    check("both buttons name operators that are really registered",
          hasattr(bpy.ops.map, "live_isolate")
          and hasattr(bpy.ops.map, "live_restore"),
          "a `classes` tuple nothing iterates registers NOTHING, and the "
          "panel row is simply missing")

    # ---- an isolate, against the fake emulator ---------------------------
    RAM = fresh_ram()
    forget_isolate()
    _units = seed_units(RAM)
    _before = bytes(RAM.mem)
    _iso_lines = UI.isolate_map(L.RamClient())
    _touched = sorted({L.RAM_BASE + i for i in range(L.RAM_BYTES)
                       if RAM.mem[i] != _before[i]})
    _allowed = set()
    for _i in range(4):
        _n = _units + _i * UNIT_STRIDE
        _allowed |= {_n + L.UNIT_SHOW, _n + L.UNIT_SHOW + 1,
                     _n + L.UNIT_DISPATCH, _n + L.UNIT_DISPATCH + 1}
    for _a in [g[1] for g in L.CODE_GATES]:
        _allowed |= set(range(_a, _a + 8))
    check("an isolate writes the unit flags and the code gates, and "
          "nothing else",
          _touched and set(_touched) <= _allowed,
          f"{len(_touched)} bytes, {len(set(_touched) - _allowed)} outside")
    check("...zeroing BOTH halfwords on every unit",
          all(RAM.read(_units + _i * UNIT_STRIDE + _o, 2) == b"\x00\x00"
              for _i in range(4)
              for _o in (L.UNIT_SHOW, L.UNIT_DISPATCH)),
          "+0x1d8 is the whole-dispatch early-out at 0x80086770, and the "
          "ground shadow follows from the same branch")
    check("...and stubbing every code gate with `jr ra; nop`",
          all(RAM.read(_a, 8) == L.RETURN_STUB
              for _name, _a in L.CODE_GATES),
          RAM.read(L.HUD_RENDERER, 8).hex())
    # The third gate, named. The camera leash is why the artist can pan during
    # dialogue but not during a battle: `FUN_8006FE58` steps `camera_work_position`
    # back toward the battle's own target every frame, so a pushed camera drifts
    # home in about a second. It rides the SAME press as the units because that
    # is where the artist asked for it -- one act, one way back.
    check("...the camera leash among them, so a pushed camera stays put",
          L.CAMERA_LEASH in [g[1] for g in L.CODE_GATES]
          and RAM.read(L.CAMERA_LEASH, 8) == L.RETURN_STUB,
          "measured live: cut, the camera holds (120.000, -5.000, 80.000); "
          "restored, it drifts 191 units back to the battle's target")
    check("...and the press SAYS the camera is unleashed",
          any("camera" in ln for ln in _iso_lines), str(_iso_lines))
    # The trap named in the decision record. Despite its name `sprite_scale` is
    # the camera ZOOM: `build_camera_view_matrix` scales the shared `R` by it,
    # read by both the map affine transform and `project_all_unit_sprites`.
    # Zeroing it collapses the MAP, which is the one thing this feature exists
    # to leave on screen.
    check("...and does not touch sprite_scale, which is the camera zoom",
          RAM.read(L.SPRITE_SCALE, 12) == _before[
              L.SPRITE_SCALE - L.RAM_BASE:L.SPRITE_SCALE - L.RAM_BASE + 12],
          "zeroing 0x800C7CA0 collapses the map -- it is the zoom, not a "
          "sprite size, and plan_camera already writes it")
    # The report is a count of UNITS. `0 changed` already means *already
    # isolated*, so a report in bytes would make one sentence mean two
    # opposite things.
    check("the report counts units, not bytes",
          any("4 of 4" in ln for ln in _iso_lines), str(_iso_lines[:1]))
    check("...and names the cursor's uncertain target for the artist's eye",
          any(f"{L.CURSOR_RENDERER_FALLBACK:08X}" in ln for ln in _iso_lines),
          "the acceptance is the artist looking, and *knife gone* / *knife "
          "there but not bobbing* are different answers")
    # Decision 13 shipped boxed dialogue as the one leg with NO located gate,
    # and the isolate said so every press. Amendment 2 located it, so the arm
    # that graded the apology now grades the gate -- and the apology must be
    # GONE, because a press that hides the box and still says it cannot is the
    # worse of the two defects.
    check("...and the boxed-dialogue gate is stubbed too",
          RAM.read(L.DIALOGUE_BOX_RENDERER, 8) == L.RETURN_STUB,
          "0x8012E65C is event_portrait_render_ft4, the per-frame builder of "
          "the box POLY_FT4s -- frame, text and speaker portrait alike")
    check("...and the press no longer apologises for boxed dialogue",
          not any("NOT hidden" in ln for ln in _iso_lines), str(_iso_lines))

    # ---- restore -----------------------------------------------------------
    UI.restore_map(L.RamClient())
    check("a restore puts the battle back byte for byte",
          bytes(RAM.mem) == _before,
          f"{sum(1 for i in range(L.RAM_BYTES) if RAM.mem[i] != _before[i])} "
          f"bytes still differ")

    # The defect that would make Restore wrong rather than absent.
    # `unit_sprite_object_show` writes `1` to both fields, and copying that
    # would REVEAL a unit the battle had legitimately hidden.
    RAM = fresh_ram()
    forget_isolate()
    _units = seed_units(RAM, flags=((1, 1), (0, 0), (1, 1)))
    _before = bytes(RAM.mem)
    UI.isolate_map(L.RamClient())
    UI.restore_map(L.RamClient())
    check("a restore writes the SAVED value, so a unit the battle had already "
          "hidden stays hidden",
          RAM.read(_units + UNIT_STRIDE + L.UNIT_SHOW, 2) == b"\x00\x00"
          and RAM.read(_units + UNIT_STRIDE + L.UNIT_DISPATCH, 2) == b"\x00\x00"
          and bytes(RAM.mem) == _before,
          "writing the constant 1 reveals a unit not yet cued to appear, one "
          "a {46} erased, or one off the roster")

    # ---- re-pressable, which is the whole answer to the emulator drifting --
    RAM = fresh_ram()
    forget_isolate()
    _units = seed_units(RAM)
    _before = bytes(RAM.mem)
    UI.isolate_map(L.RamClient())
    _mid = bytes(RAM.mem)
    _again = UI.isolate_map(L.RamClient())
    check("a second isolate changes nothing and says so",
          bytes(RAM.mem) == _mid
          and any("already isolated" in ln for ln in _again), str(_again[:1]))
    check("...and does not lose the way back",
          UI.restore_map(L.RamClient()) and bytes(RAM.mem) == _before,
          "the second walk reads back what the first press wrote, so a memory "
          "that REPLACED itself would save show=0 for the whole roster and "
          "Restore would leave the battle empty")

    # A unit spawning mid-battle is answered by pressing Isolate again -- which
    # is why there is no ticker and no readback.
    RAM = fresh_ram()
    forget_isolate()
    _units = seed_units(RAM, flags=((1, 1), (1, 1)))
    _before = bytes(RAM.mem)
    UI.isolate_map(L.RamClient())
    seed_units(RAM, flags=((1, 1), (1, 1), (1, 1)))     # one spawned
    _spawn_before = bytes(RAM.mem)
    UI.isolate_map(L.RamClient())
    check("a unit that spawns while isolated is hidden by a second press",
          RAM.read(_units + 2 * UNIT_STRIDE + L.UNIT_DISPATCH, 2) == b"\x00\x00",
          "one press, not a per-tick round trip")
    UI.restore_map(L.RamClient())
    check("...and it is restored to the flags it spawned with",
          bytes(RAM.mem) == _spawn_before,
          "the spawned unit joins the memory with its OWN saved values")

    # ---- the not-in-a-battle case ----------------------------------------
    RAM = fresh_ram()
    forget_isolate()
    _before = bytes(RAM.mem)
    _empty = UI.isolate_map(L.RamClient())
    check("a null list head reports `found no units` rather than `0 changed`",
          any("no units" in ln for ln in _empty), str(_empty[:1]))
    check("...and writes nothing AT ALL, not even the code gates",
          bytes(RAM.mem) == _before,
          "a null head is indistinguishable from not being in a battle, and "
          "decision 13 rules that case *found nothing, wrote nothing* -- "
          "poking an overlay that is not loaded is the same mistake wearing "
          "a constant")
    check("...and a restore after it has nothing saved to write back",
          any("nothing to restore" in ln
              for ln in UI.restore_map(L.RamClient()))
          and bytes(RAM.mem) == _before,
          "saving the gates on a press that found no battle would hand "
          "Restore eight bytes of whatever was loaded at the time")

    # A chain the walk cannot finish still hides what it reached, and SAYS how
    # far it got -- the artist's call, and the second number is what makes it
    # safe rather than silent.
    RAM = fresh_ram()
    forget_isolate()
    _units = seed_units(RAM)
    struct.pack_into("<I", RAM.mem, _units + 2 * UNIT_STRIDE - L.RAM_BASE,
                     _units)                            # bend the third link
    _bent = UI.isolate_map(L.RamClient())
    check("a chain that goes bad hides what it reached and says how far",
          any("hid 3 units" in ln and "loops back" in ln for ln in _bent),
          str(_bent[:1]))

    # ---- Restore with nothing saved --------------------------------------
    forget_isolate()
    RAM = fresh_ram()
    _before = bytes(RAM.mem)
    _nothing = UI.restore_map(L.RamClient())
    check("a restore with no saved values says so rather than raising",
          any("nothing to restore" in ln for ln in _nothing)
          and bytes(RAM.mem) == _before, str(_nothing))
    check("...and names reloading the battle as the way back",
          any("reload the battle" in ln for ln in _nothing), str(_nothing))

    # ---- it is an ACT, and registers no timer -----------------------------
    # Decision 13 registers no timer, which sidesteps the trap the camera sync
    # had to be guarded against: `--background` Blender holds a window with a
    # VIEW_3D area, so a registered timer finds a real viewport headless and
    # would POST to port 8080 from a test run.
    check("isolate arms no timer of its own",
          [f for f in (getattr(UI, "_isolate_timer", None),)
           if f is not None and bpy.app.timers.is_registered(f)] == [],
          "isolate has no moving source: a ticker would spend a round trip "
          "per tick to learn there is nothing to do")

    # Both operators reach the Log, the way every other outcome in this addon
    # does -- a toast expires and the console scrolls.
    for _tag, _cls in (("isolate", "MAP_OT_live_isolate"),
                       ("restore", "MAP_OT_live_restore")):
        _src = inspect.getsource(getattr(UI, _cls).execute)
        check(f"the {_tag} operator records an outcome in the Log",
              "record(" in _src, f"{_cls}.execute never calls report_log.record")

    rows_prefs = []
    _prefs_obj = bpy.context.preferences.addons["exmateria_map"].preferences
    _prefs_obj.layout = FakeLayout(rows_prefs)
    IMP.MAP_AddonPreferences.draw(_prefs_obj, bpy.context)
    prefs_ops = [r[1] for r in rows_prefs if r[0] == "operator"]
    check("the preferences offer all three routes to the handlers",
          {"exmateria_map.launch_pcsx", "exmateria_map.setup_pcsx",
           "exmateria_map.copy_launch_command"} <= set(prefs_ops),
          str(prefs_ops))
    # The transport preference is GONE (#606 part 3). Its off position needed
    # our fork and failed as `lua/exec 404`, which names nothing an artist
    # could act on -- so it must not come back as a checkbox.
    check("no transport checkbox is offered to an artist",
          not hasattr(_prefs_obj, "live_ram_over_http"),
          "live_ram_over_http is back in the preferences")

    # The arm that would have caught the real defect, and did not exist.
    # `FakeLayout.operator` records whatever string it is handed, so a panel
    # naming an UNREGISTERED operator records perfectly and draws NOTHING in a
    # real Blender -- no error, no red button, just a missing row. All three
    # PCSX operators shipped that way: added to `import_document.classes`,
    # which nothing iterates, and not to `register()`, which is hand-written.
    # Resolving the id against `bpy.ops` is what tells the two apart.
    # `dir()`, not `hasattr`. `bpy.ops` resolves lazily, so
    # `hasattr(bpy.ops.anything, "anything")` is True for a group that does not
    # exist and a name that was never registered -- measured. The first version
    # of this arm used `hasattr` and could not fail; it passed on the very
    # defect it was written for, and `dir()` is what tells the two apart.
    def _resolves(idname):
        group, _, name = idname.partition(".")
        grp = getattr(bpy.ops, group, None)
        return grp is not None and name in dir(grp)

    _unresolved = sorted({i for i in push_ops + cam_ops + prefs_ops if not _resolves(i)})
    check("every operator these panels name is actually REGISTERED",
          not _unresolved, f"unregistered: {_unresolved}")

    # The same failure one field over, and it shipped too: `col.prop(self,
    # "live_pcsx_dir")` on a property that is not on the class draws NOTHING.
    # Both `live_pcsx_*` were deleted by a line-span edit meant for the
    # transport preference, the `prop` calls stayed, and the preferences panel
    # rendered two invisible rows. `FakeLayout.prop` recording a name proves
    # only that `draw` said it.
    _prefs_fields = [r[1] for r in rows_prefs if r[0] == "prop"]
    _missing_fields = sorted(f for f in _prefs_fields
                             if f and not hasattr(_prefs_obj, f))
    check("every preference these panels name actually EXISTS",
          not _missing_fields, f"no such property: {_missing_fields}")
    # ONE folder, not a folder and a binary. Both buttons take it, because the
    # launch sets `cwd` to it -- so the emulator's working directory is decided
    # rather than discovered, and the shim's folder and the launch's folder are
    # the same by construction. Two fields read as two setups; there is one.
    check("...including the ONE folder both PCSX buttons work from",
          "live_pcsx_dir" in _prefs_fields
          and hasattr(_prefs_obj, "live_pcsx_dir"), str(_prefs_fields))
    check("...and nothing asks the artist for the binary separately",
          "live_pcsx_binary" not in _prefs_fields
          and not hasattr(_prefs_obj, "live_pcsx_binary"),
          str(_prefs_fields))

    # And the trap one step upstream: `classes` reads like the registration
    # list and is not one. Held against `register()` so the next person to add
    # a class to the plausible-looking tuple finds out here.
    # `is_registered` is Blender's own answer, not a proxy for it: `bpy.types`
    # does not gain an entry for every registered class, so a membership test
    # there reports nine false positives on a healthy addon.
    _missing = sorted(c.__name__ for c in IMP.classes
                      if not getattr(c, "is_registered", False))
    check("every class in `classes` is one `register()` actually registers",
          not _missing, f"declared but never registered: {_missing}")
    # `live_link_ui` keeps its own `classes`, and the camera section is the
    # first thing added to it since the tuple was written -- which makes it
    # exactly the class that would fall in the gap above.
    _missing_ui = sorted(c.__name__ for c in UI.classes
                         if not getattr(c, "is_registered", False))
    check("...and the same for `live_link_ui`'s own tuple",
          not _missing_ui, f"declared but never registered: {_missing_ui}")

    # ---- the all-empty refusal -------------------------------------------
    # Zeroing all four counts would make `check_descriptors` read the block as
    # "no map is loaded", so every LATER push would be refused and the artist
    # could only reload the savestate -- the rig would have written itself out
    # of being able to fix itself.
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.faces.ensure_lookup_table()
    for f in list(bm.faces):
        bm.faces.remove(f)
    bm.to_mesh(me)
    bm.free()
    RAM = fresh_ram()
    UI._LAST_PUSH.clear()
    before = bytes(RAM.mem)
    res, err = push()
    check("a document with no polygons at all is refused",
          res == {"CANCELLED"}, f"{res} {err}")
    check("the all-empty refusal explains that it would lock the artist out",
          any("no polygons in any bucket" in ln and "savestate" in ln
              for ln in last_push()), str(last_push()))
    check("a refused all-empty document writes nothing",
          bytes(RAM.mem) == before)
except Exception:
    traceback.print_exc()
    write_report(fatal=traceback.format_exc()[-2000:])
    raise SystemExit(1)

write_report()
print(f"CHECKS {sum(checks.values())}/{len(checks)}")
'''


def ensure_addon():
    TMP.mkdir(exist_ok=True)
    zf_path = TMP / "exmateria_map.zip"
    if zf_path.exists():
        zf_path.unlink()
    with zipfile.ZipFile(zf_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(ADDON_DIR.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                zf.write(f, f.relative_to(ADDON_DIR.parent))
    return zf_path


#: A resource carrying only the two animation chunks, built here rather than
#: taken from the corpus so this harness keeps running with no ISO. The layout
#: is `mapfile`'s: a 196-byte header of section pointers, the `0x6c` table
#: (32 records of 20 bytes) and the `0x70` frames (16 x 16 BGR555 words).
ANIM_HEADER = 196
ANIM_TABLE_BYTES = 640
ANIM_FRAMES_BYTES = 512


def anim_record(x, y=480, w=16, h=1, frames=4, mode=3, duration=1):
    r = bytearray(20)
    for off, val in ((0, x), (2, y), (4, w), (6, h)):
        r[off:off + 2] = int(val).to_bytes(2, "little")
    r[14], r[15], r[17] = frames, mode, duration
    return bytes(r)


def anim_resource(records):
    """`records` in slots 0.., plus 16 distinct frames, in a real resource."""
    head = bytearray(ANIM_HEADER)
    head[0x6C:0x70] = ANIM_HEADER.to_bytes(4, "little")
    head[0x70:0x74] = (ANIM_HEADER + ANIM_TABLE_BYTES).to_bytes(4, "little")
    table = bytearray(ANIM_TABLE_BYTES)
    for i, rec in enumerate(records):
        if rec is not None:              # `None` leaves the slot empty, and a
            table[i * 20:(i + 1) * 20] = rec   # record's slot is its identity
    # Every frame distinct, so a row that steps really changes bytes and a row
    # that does not really does not.
    frames = bytearray()
    for f in range(16):
        for e in range(16):
            frames += ((f * 97 + e * 5) & 0x7FFF).to_bytes(2, "little")
    return bytes(head + table + frames)


def write_disc_tree(root):
    """A two-map extracted disc tree: the map this document is a diff against,
    and the FOREIGN map the emulator is pretending to have loaded.

    Two, not one, because the content guard's whole job is to recognise the
    HOST's table -- which on a swap is not the document's map. One resource
    would let a guard that compared against the document's own map pass.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "MAP001.GNS").write_bytes(b"\x00" * 20)
    # The pushed map: CLUT row 13, and one texture record that must NOT be
    # installed (its VRAM base is the loader's, #653).
    # Slot 2, not slot 0. A record's index is part of its identity, so the
    # install writes it back to its own slot -- and putting it anywhere but on
    # top of the host's makes the seeded "erase does nothing" arm able to fail:
    # the host's slots 0 and 1 survive and the readback sees them still moving.
    ours = anim_resource([None, None, anim_record(13 * 16),
                          anim_record(850, y=100, w=8, h=8)])
    (root / "MAP001.9").write_bytes(ours)
    # The host: CLUT rows 14 and 15, and three texture records. All three of
    # its rows sit inside `CLUT_ANIMATED_MEASURED`, so the engine-animated-row
    # tolerance in `check_clut_block` behaves exactly as it does on a real map.
    theirs = anim_resource([anim_record(14 * 16), anim_record(15 * 16),
                            anim_record(700, y=40, w=8, h=8),
                            anim_record(712, y=40, w=8, h=8),
                            anim_record(724, y=40, w=8, h=8)])
    (root / "MAP099.9").write_bytes(theirs)
    return ours, theirs


def main():
    TMP.mkdir(exist_ok=True)
    if REPORT.exists():
        REPORT.unlink()                      # never grade on a stale report
    zf_path = ensure_addon()
    disc = TMP / "disc"
    ours, _theirs = write_disc_tree(disc)
    # A second tree that is the SAME game and a DIFFERENT build of this map:
    # the host's table still matches, so the erase is confirmed, and the
    # document's pin does not, so the install is refused. That is decision
    # 11's degradation rule, and it is the half of it that survives contact.
    disc_bad = TMP / "disc-wrong-pin"
    write_disc_tree(disc_bad)
    (disc_bad / "MAP001.9").write_bytes(
        anim_resource([None, None, anim_record(9 * 16),
                       anim_record(850, y=100, w=8, h=8)]))
    staged = TMP / FIXTURE.name
    doc = json.loads(FIXTURE.read_text())
    # The stub's `sha256` for `MAP001.9` is the schema's example digest, and
    # decision 11's read of the base resource is PINNED by it. Re-pin the
    # staged copy against the tree above, so the animation arms exercise a
    # verified read rather than a skipped one -- the mismatch is its own arm.
    import hashlib
    for entry in doc["base"]["resources"]:
        if entry["name"] == "MAP001.9":
            entry["sha256"] = hashlib.sha256(ours).hexdigest()
    staged.write_text(json.dumps(doc))
    for st in doc["map_states"]:
        if st.get("texture_sheet"):
            (TMP / st["texture_sheet"]).write_bytes(
                (FIXTURES / st["texture_sheet"]).read_bytes())

    script = TMP / "run_check.py"
    script.write_text(SCRIPT_TEMPLATE
                      .replace("@ADDONPKG@", str(ADDON_DIR.parent))
                      .replace("@ZIP@", str(zf_path))
                      .replace("@JSON@", str(staged))
                      .replace("@DISC@", str(disc))
                      .replace("@DISC2@", str(disc_bad))
                      .replace("@OUT@", str(REPORT)))
    # Isolate this Blender from the artist's OWN install. Without it the
    # `addon_install` in the script above overwrites the addon they are
    # clicking, and `addon_enable` then grades that copy rather than this
    # tree. `--factory-startup` does NOT do this -- see `blender_env`.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from blender_env import isolated_env
    proc = subprocess.run(
        [sys.argv[1] if len(sys.argv) > 1 else "blender",
         "--background", "--factory-startup", "--python", str(script)],
        capture_output=True, text=True,
                          env=isolated_env())
    sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stdout.write("\n[stderr]\n" + proc.stderr[-4000:])
    if not REPORT.exists():
        print("\nFAIL: the harness did not run to completion — no report")
        sys.exit(1)
    report = json.loads(REPORT.read_text())
    checks = report["checks"]
    failed = [n for n, ok in checks.items() if not ok]
    print(f"\nSUMMARY: {len(checks) - len(failed)}/{len(checks)} checks passed")
    # A run that DIED has not passed, whatever the checks it got through say.
    # This printed `PASS` under a traceback until 2026-08-26: the count bound
    # below is not the same guard, because a run that adds arms before the
    # crash point can die and still clear it. Grade the crash itself.
    if report.get("fatal"):
        print("FATAL:", report["fatal"][-600:])
        print("FAIL: the harness died — the checks after the crash never ran")
        sys.exit(1)
    if len(checks) < EXPECTED_CHECKS:
        print(f"FAIL: {len(checks)} checks ran, {EXPECTED_CHECKS} expected — "
              "a run that stopped early has caught nothing")
        sys.exit(1)
    if failed:
        print("FAILED:", ", ".join(failed))
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
