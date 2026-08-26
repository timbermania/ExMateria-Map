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
EXPECTED_CHECKS = 86

SCRIPT_TEMPLATE = r'''
import json
import re
import struct
import sys
import traceback

import bmesh
import bpy

PKG = "@ADDONPKG@"
ZIP = "@ZIP@"
JSON = "@JSON@"
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

from exmateria_map import export_document, live_link as L, live_link_ui as UI

DOC = json.loads(open(JSON).read())


# --- the fake emulator -----------------------------------------------------

class FakeRam:
    """Enough of `LuaClient` to be a machine, not a stub.

    `exec` really parses the wire form `pack_writes` produced and really
    applies it byte by byte, so the packer, the record header and the
    changed-byte count are all under test rather than mocked away.
    """

    def __init__(self):
        self.mem = bytearray(L.RAM_BYTES)
        self.cp2c = [0] * 32          # the GTE control registers
        self.up = True
        self.execs = 0
        self.gte_execs = 0

    # -- transport
    def ping(self):
        return self.up

    def read(self, address, length):
        o = address - L.RAM_BASE
        if o < 0 or o + length > L.RAM_BYTES:
            raise L.LiveLinkError("outside main RAM")
        return bytes(self.mem[o:o + length])

    def exec(self, code, timeout=180.0):
        self.execs += 1
        # The rig's second transport (§2.2). Parsed for real, like the RAM
        # one: a stub that swallowed the call would let a mis-packed colour
        # matrix through, and the packing is the half most likely to be wrong
        # -- two shorts to a word, ninth alone, and the ambient shifted by 4.
        regs = re.findall(r"r\.CP2C\.r\[(\d+)\] = (\d+)", code)
        if regs:
            self.gte_execs += 1
            for index, value in regs:
                self.cp2c[int(index)] = int(value)
            return str(len(regs))
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
              followers=()):
    ram = FakeRam()
    ram.poke(L.DESCRIPTOR_BASE,
             descriptor_block(counts or doc_counts(doc), followers=followers))
    seed_geometry(ram, doc, honour_start=honour_start)
    seed_metadata(ram, doc, honour_start=honour_start)
    seed_packets(ram, doc, honour_start=honour_start)
    seed_rig(ram, rig)
    return ram


RAM = None
L.LuaClient = lambda host=None, port=None: RAM


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
    _seeded = fresh_ram(live_doc, rig=DOC["map_states"][0]["light_rig"])
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


def main():
    TMP.mkdir(exist_ok=True)
    if REPORT.exists():
        REPORT.unlink()                      # never grade on a stale report
    zf_path = ensure_addon()
    staged = TMP / FIXTURE.name
    staged.write_text(FIXTURE.read_text())
    doc = json.loads(FIXTURE.read_text())
    for st in doc["map_states"]:
        if st.get("texture_sheet"):
            (TMP / st["texture_sheet"]).write_bytes(
                (FIXTURES / st["texture_sheet"]).read_bytes())

    script = TMP / "run_check.py"
    script.write_text(SCRIPT_TEMPLATE
                      .replace("@ADDONPKG@", str(ADDON_DIR.parent))
                      .replace("@ZIP@", str(zf_path))
                      .replace("@JSON@", str(staged))
                      .replace("@OUT@", str(REPORT)))
    proc = subprocess.run(
        [sys.argv[1] if len(sys.argv) > 1 else "blender",
         "--background", "--factory-startup", "--python", str(script)],
        capture_output=True, text=True)
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
