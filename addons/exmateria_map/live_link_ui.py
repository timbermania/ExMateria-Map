"""The button: push the scene into a running battle without leaving Blender.

`live_link.py` is the `bpy`-free core -- addresses, gate, write plan, transport
(ADR-0005 decision 2). This module is the half that needs `bpy`, and it is
deliberately thin: it assembles the document the export operator would have
written, and hands it straight to the core.

**Nothing is written to disk.** `export_document.assemble(ob)` already returns
`(doc, files, report)` *in memory* -- the export operator calls it and only then
writes a bundle. The push calls the same function and skips the write, so the
loop is edit -> press -> look, with no file and no CLI in it. It also means the
button is the first thing that runs a document **exported out of Blender**
through the live rig; every earlier proof used a document from `dump`.

## The order, which is `tools/live_map.py`'s

1. `assemble(ob)` -- refuse on `rep.refusals`, exactly as export does (§9.4).
   The palette gate and every range check are export's, not a second copy.
2. `check_descriptors(read_descriptor_block(client))` -- the gate.
3. The write-path self-check (below), unless it has to be skipped.
4. `plan_document` + `apply`, then report changed bytes and what was `UNPUSHED`.

Step 4 builds its plan **before** step 3 runs, so a document whose polygon
counts do not match the loaded map is refused before a single byte moves and
before the artist waits on six round trips.

## What the self-check compares against, and why it is not the disc

The CLI reads the base map off the disc for this. The addon cannot: it never
imports `exmateria_map` and has no corpus (ADR-0004 §7). Three options were on
the table -- skip the check, declare the map and read the disc anyway, or
compare against the document's own geometry. The third is tautological and the
second is not available here.

The fourth is what this uses: the marker's **`positions_shadow` /
`normals_shadow`** corner attributes. Import writes them from the document and
nothing but a re-import touches them, so they hold what the disc held, *not*
what the artist has since edited -- which is exactly the base the check wants.
It costs no disc read, no corpus and no map declaration, and it makes the check
strictly stronger than the CLI's: a document imported from a **different** map
than the one loaded mismatches, so the check that was only ever about the rig's
arithmetic now also catches the identity error decision 2 explicitly stopped
claiming.

Two cases it cannot cover, both named on the spot rather than papered over:

- **A face the artist added has no shadow** (it zero-fills). Adding geometry
  usually fails the count check first, but adding one face and deleting another
  does not, so a scene with new faces skips the self-check with a warning.
- **The second push of a session legitimately differs from the disc**, because
  the first one edited exactly these bytes. The CLI's answer is "reload the
  savestate"; a button pressed repeatedly cannot ask that. So the last push's
  own plan is kept in memory for the session and tried first: RAM holding what
  *we* last wrote there is a pass, and the pristine base is the fallback for a
  map that has since reloaded. The chain is still anchored -- the first push of
  a session has no `_LAST_PUSH` entry and is checked against the disc's bytes.

`_LAST_PUSH` is per process on purpose. A fresh Blender against an emulator
that was pushed to yesterday finds RAM matching neither candidate and says so,
which is the truth.
"""

from __future__ import annotations

import json
import textwrap
import threading
import traceback

import bpy
from bpy.props import BoolProperty
from bpy.types import Operator, Panel

from . import live_link as L
from . import live_vram as VR
from . import worker
from .export_document import (assemble, describe_divergence, find_marker,
                              markers)
from .import_document import _prefs, marker_in_scene, state_rig

#: `(bucket, field) -> writes` of the last push this **process** made, per
#: marker object name. See the module docstring: it is what lets the artist
#: press the button twice without reloading a savestate, and it is deliberately
#: not persisted -- a claim about the emulator's RAM does not survive either
#: process, and a stale one would turn the self-check into a rubber stamp.
_LAST_PUSH: dict[str, dict] = {}

#: Custom property the panel reads back. An operator report is gone by the time
#: the artist looks up from the viewport -- the same reason export stores its
#: own lines (`exmateria_map/last_export`).
LAST_PUSH_KEY = "exmateria_map/last_push"


#: Where import stores the document's polygon list, verbatim (`TOP_LEVEL`;
#: `import_document.py`'s marker-JSON block). Export's own docstring calls the
#: marker's sections "import-time snapshots handed back verbatim", and that is
#: exactly what the self-check wants for a base.
MARKER_POLYGONS = "exmateria_map/polygons"


def base_map_dir(ob):
    """The extracted disc tree this scene's base map lives in, or `None`.

    The animation chunks are the one thing a push reads off the disc
    (decision 11 part 3): the interchange document carries neither, because
    schema §8 puts both on the *carried from base* side. The address comes from
    the `MAP###.GNS` the scene already remembers -- decision 31 part 4's "the
    picked path is the ENTIRE address" -- so nothing new is asked of the artist.

    `None` when the scene does not remember one, which is a document imported
    from JSON rather than from a GNS. That costs the animation INSTALL and
    nothing else.
    """
    from .gns_bundle import remembered_gns
    gns = remembered_gns(ob)
    if not gns:
        return None
    try:
        from ._vendor.exmateria_map import mapfile as pkg_mapfile
        map_dir, _number = pkg_mapfile.address(bpy.path.abspath(gns))
    except Exception:                    # a wrong or absent tree is not a crash
        return None
    return map_dir if map_dir.is_dir() else None


def base_polygons(ob):
    """The document that was IMPORTED -- the self-check's base map.

    Read off the marker, not reconstructed from the mesh. Decision 8, as
    amended: the imported polygon list is already stored here verbatim, in
    import order, at import length, so the earlier version was rebuilding from
    a mutable mesh a thing that was never lost -- and getting it wrong in three
    ways the artist can reach.

    It walked `me.polygons` in CURRENT order and handed the result to
    `plan_document`, which assigns document index -> RAM slot. Delete face 5 of
    24 and every survivor shifts down a slot, so the base claimed slot 5 held
    surviving-face-5's bytes when slot 5 still held old face 5, and the
    self-check fired on a perfectly healthy shrink while blaming the rig's
    arithmetic, a wrong map, or a prior push. All three were wrong. It also
    returned `None` the moment any face was new, standing the highest-value
    check in the build down exactly during growth, which is when this decision
    most wanted it. Reordering was the worst of the three: it pushed garbage
    with a GREEN self-check, because both sides moved together.

    Reading the stored document is immune to deletion, reordering, extrusion
    **and** retexturing, needs no new attribute, and never returns `None` on a
    face the artist created. `None` means only what it says: this mesh did not
    come from an import.
    """
    raw = ob.get(MARKER_POLYGONS)
    if raw is None:
        return None
    return json.loads(raw)


def selfcheck(client, base_plans, prev_plans):
    """Read the whole check, then let `live_link.diagnose_selfcheck` judge it.

    Returns `(ok, lines)`. It used to raise on the FIRST plan that matched
    neither candidate, in sorted-key order -- so `normals` was judged before
    `positions` had been looked at, and an emulator holding a previous
    session's bake was reported as possibly the wrong map. Every plan is
    evaluated now and the pattern across them is what decides; the reasoning
    lives in the core, where `pytest` can grade it.

    `prev_plans` is still tried first when it exists: after a push it is what
    RAM holds, so checking it first costs one read per bucket instead of two.
    """
    results = {}
    for key, writes in sorted(base_plans.items()):
        prev = (prev_plans or {}).get(key)
        matched, differ, total = None, 0, 0
        for what, candidate in (("this session's last push", prev),
                                ("the base map's own bytes", writes)):
            if candidate is None:
                continue
            differ, total = L.verify(client, candidate)
            if differ == 0:
                matched = what
                break
        results[key] = (matched, differ, total)
    return L.diagnose_selfcheck(results)


def _by_address(plans, extra=()):
    """Every planned byte, flattened to `{address: byte}`."""
    out = {}
    for writes in list(plans.values()) + [list(extra)]:
        for address, data in writes:
            for k, byte in enumerate(data):
                out[address + k] = byte
    return out


def authored_bytes(base_plans, plans, base_extra=(), extra=()):
    """How many bytes this document differs from the map that was IMPORTED.

    Free: both plans are already built and comparing them is arithmetic rather
    than a round trip. `None` when there is no base (a mesh that did not come
    from an import).

    **By address, over the union of the two.** It used to `zip` the two plans
    per key, which truncates to the shorter one -- so a pure growth compared
    the base's every byte against the new plan's first N and found no
    difference at all, reported **0 authored bytes**, and `interpret` printed
    *"byte-identical to the map you imported... check Lamp authority is ON"*
    over a push that had just added a polygon. That is a third cause for the
    zero this whole readout exists to disambiguate.

    An address only one side plans is a difference: on growth the base had
    nothing there, and on a shrink the document no longer claims what is
    there. The count writes are passed in as `extra` and count too -- lowering
    a bucket to zero is the whole of what "the artist deleted that bucket"
    looks like, and it moves two bytes.
    """
    if base_plans is None:
        return None
    was, now = _by_address(base_plans, base_extra), _by_address(plans, extra)
    return sum(1 for a in set(was) | set(now) if was.get(a) != now.get(a))


def interpret(changed, authored):
    """Say which of the two zeroes this is.

    `apply` returns bytes that CHANGED, and a zero has two completely different
    causes that the number cannot tell apart -- measured, both printing
    `pushed 0 changed byte(s)`:

      * the document is byte-identical to the map that was imported, so there
        was never anything to push. Moving lamps with **Lamp authority off**
        does exactly this: the panel says the lamps are "not in charge", no
        normal moves, and the push then truthfully reports nothing;
      * the document IS edited and the emulator already holds it, because this
        is the second press with no change in between.

    The first reads as a broken button and the second as a working one. Naming
    which is which is the whole reason `authored_bytes` is computed.
    """
    if authored is None:
        return []
    if authored == 0:
        return ["nothing to push: this document is byte-identical to the map "
                "you imported. If you moved lamps, check **Lamp authority** is "
                "ON -- with it off the lamps are not in charge and no normal "
                "moves."]
    if changed == 0:
        return [f"already live: all {authored:,} of your authored byte(s) were "
                f"already in RAM -- this is the document the emulator is "
                f"holding."]
    return [f"{authored:,} byte(s) differ from the imported map; {changed:,} "
            f"of them were not already in RAM."]


def unpushed_lines(pushed_fields):
    """`live_link.UNPUSHED`, minus what this push actually covered.

    The light rig used to be popped here whenever normals were pushed, on the
    reasoning that pushing normals IS the rig's effect. That was true of the
    map's own shading and false of everything else the rig drives -- the gains
    and the ambient reach the GTE directly, and no normal moves them. It has a
    sink of its own now (§2.2) so it is out of `UNPUSHED` altogether, and a
    push that could NOT resolve a rig says so in its own line instead.
    """
    return [f"not pushed: {field} -- {why}"
            for field, why in sorted(L.UNPUSHED.items())]


def picture_plan(at, at_vram, sheets, clut_ram, clut_vram):
    """The sheet's rectangles, the palettes' TWO sinks, or a refusal.

    Decision 2's atom, planned as one. `bpy`-free and pure so the composition
    itself is testable -- the operator around it is neither.

    Returns `(rects, clut_rects, writes, notes)`: the sheet's VRAM rectangles,
    the palettes' VRAM rectangles, and the palettes' RAM writes. A `note` is a
    thing the artist must be told that is not a failure: decision 10's "this
    state declares no palettes, so none were pushed" is the common one, and
    38.5% of corpus states are in exactly that position. A genuine problem
    raises instead, and takes the whole atom with it.

    **The palettes are planned into both memories, and that is a correction.**
    This function used to plan the RAM block alone, on a measurement that the
    engine re-uploads it every frame. It does -- on the **42** textured
    resources of 169 whose `0x70` chunk carries a palette ANIMATION, which is
    what performs that re-upload. On the other **127** nothing re-uploads it
    and a RAM-only push is byte-perfect and invisible. Neither sink is a
    fallback for the other; see `live_link.plan_palettes` and §2.3.
    """
    notes = []
    if at.sheet_row is None:
        raise L.LiveLinkError(
            f"the group night={at.night} weather={at.weather} carries no "
            "TEXTURE row, so there is no sheet to push (71 of the corpus's "
            "774 groups are like this)")
    name = at.sheet_row.get("texture_sheet")
    blob = (sheets or {}).get(name)
    if blob is None:
        raise L.LiveLinkError(
            f"the sheet {name!r} has no pixel buffer in this scene, so there "
            "is nothing to send. Its image was never loaded, or it is not "
            "256x1024 -- the document still names it and `build` will still "
            "ship the sidecar on disk")

    rects = VR.plan_sheet(blob, at_vram)
    for rc in rects:
        VR.check_rect(rc)

    # #646: the sheet goes to the address MOST of the engine's packets agree
    # on, and the ones that named a different page are a note rather than a
    # refusal. They are not a rounding error to the artist -- they are the
    # faces that will still be wearing the old map's picture, and five stale
    # faces read as "the push half worked" unless somebody says otherwise.
    if at_vram.sheet_dissent or at_vram.clut_dissent:
        notes.append(
            f"{at_vram.sheet_dissent} polygon(s) of {at_vram.witnesses} point "
            f"at a different texture PAGE and {at_vram.clut_dissent} at "
            f"different CLUT rows. The sheet went to "
            f"({at_vram.sheet_x}, {at_vram.sheet_y}), which the rest agree on, "
            "so those keep the texture that was already in VRAM. 23 of the "
            "corpus's 169 textured resources carry a second page band; a "
            "tenth dissenting is still a refusal (#646)")

    writes, clut_rects = [], []
    if at.palette_row is None or not at.palette_row.get("palettes"):
        notes.append(
            f"palettes: none pushed -- the group night={at.night} "
            f"weather={at.weather} declares none of its own, so the map keeps "
            "the CLUTs it is already showing (decision 10). 38.5% of corpus "
            "states are like this and render with a keyed partner's")
    else:
        # Decision 5 at the RAM sink: the block is checked against what the GPU
        # is actually showing before a byte of it is written, because a second
        # copy of the same 512 bytes sits elsewhere in RAM and pushing into
        # that one moves nothing at all.
        #
        # What this check does NOT establish is that a write to `CLUT_BLOCK`
        # arrives. It passed on Orbonne -- both sides held Orbonne's -- and the
        # push still never reached the screen, because on a map with no palette
        # animation nothing re-uploads the block. Agreement means the address
        # is the right one; the VRAM rectangles below are what makes it visible.
        L.check_clut_block(clut_ram, clut_vram)
        rows = L.clut_rows(at.palette_row["palettes"])
        writes = [(L.CLUT_BLOCK + i * L.CLUT_ROW_BYTES, b) for i, b in rows]
        clut_rects = VR.plan_clut(rows, at_vram)
        for rc in clut_rects:
            VR.check_rect(rc)
    return rects, clut_rects, writes, notes


def picture_lines(at, rects, writes, sheet_changed, clut_changed,
                  unheld_rects, clut_differ, notes,
                  clut_vram_changed=0, unheld_clut=()):
    """What the sheet-and-palette push moved, and what did not hold.

    Decision 3 lives here: the rows that did not take are NAMED from a
    readback, never predicted. Some CLUT rows are engine-animated -- rows 13-15
    on MAP022 a0 -- and a push cannot make those stick, but an artist who is
    not told WHICH ones reads one reverting swatch as a rig that does not work.

    The palette line carries **two** byte counts because the palettes have two
    sinks, and an artist reading one number could not tell the two failures
    apart: a RAM-only push is invisible on the 127 resources with no palette
    animation, and a VRAM-only one is overwritten within a frame on the 42
    that have one.
    """
    out = [f"picture: {sheet_changed:,} VRAM byte(s) of texture sheet + "
           f"{clut_vram_changed:,} VRAM byte(s) of palette + "
           f"{clut_changed:,} RAM byte(s) of palette, aimed at "
           f"night={at.night} weather={at.weather} kind {at.kind}"]
    if not sheet_changed and rects:
        out.append("  the sheet was already live -- these are the bytes the "
                   "emulator is already holding, not a push that failed")
    for rc, n in unheld_rects:
        out.append(f"  {rc.label} did NOT hold: {n:,} byte(s) read back "
                   "different -- the game has reloaded the map over the push")
    if writes and not clut_vram_changed and not clut_changed:
        out.append("  the palettes were already live in both memories")
    for rc, n in unheld_clut:
        out.append(
            f"  {rc.label} did not hold in VRAM: {n:,} byte(s) read back "
            "different. On a map that ANIMATES its palettes the RAM block is "
            "re-uploaded over these rows every frame and wins, which is "
            "expected -- the RAM sink is the durable one there")
    if clut_differ:
        out.append(
            f"  {clut_differ} palette byte(s) did not hold in RAM. The engine "
            "repaints some CLUT rows itself (rows "
            + ", ".join(str(r) for r in L.CLUT_ANIMATED_MEASURED)
            + " on MAP022 a0), and it wins every frame -- this is the "
              "palette ANIMATION, whose source chunk (0x70) still has no "
              "reader. Everything else took")
    out.extend("  " + n for n in notes)
    return out


def push_picture(client, vram, at, at_vram, sheets, say):
    """Apply decision 2's atom and report it. Returns the report lines."""
    try:
        clut_vram = VR.clut_block(vram.read(), at_vram)
        clut_ram = client.read(L.CLUT_BLOCK, L.CLUT_BLOCK_BYTES)
        rects, clut_rects, writes, notes = picture_plan(
            at, at_vram, sheets, clut_ram, clut_vram)
    except (L.LiveLinkError, VR.VramError) as e:
        say("WARNING", f"sheet and palettes NOT pushed: {e}")
        return []

    try:
        sheet_changed = VR.apply(vram, rects)
        # RAM last, and that ordering is the one thing here that matters. On
        # the 42 resources that animate their palettes the engine re-uploads
        # `CLUT_BLOCK` over these rows every frame, so whichever sink is
        # written last is not what the artist sees -- but a VRAM write made
        # AFTER the RAM one would be reverted to the same bytes, while a RAM
        # write made after a VRAM one is what makes both agree. The readback
        # below then reports what actually held.
        clut_vram_changed = VR.apply(vram, clut_rects)
        clut_changed = L.apply(client, writes)
    except (L.LiveLinkError, VR.VramError) as e:
        say("ERROR", f"the picture push FAILED part way: {e}")
        return []

    unheld = VR.verify(vram, rects)
    unheld_clut = VR.verify(vram, clut_rects)
    clut_differ, _compared = L.verify(client, writes)
    return picture_lines(at, rects, writes, sheet_changed, clut_changed,
                         unheld, clut_differ, notes,
                         clut_vram_changed, unheld_clut)


def animation_edit_lines(map_dir, doc, state) -> list[str]:
    """What `Push to PCSX` says about the map's own animation (decision 11
    part 5). It explains; it does not act.

    Read from the base resource like the install is, and for the same reason --
    the document carries neither chunk. A tree it cannot read costs the
    SENTENCE and nothing else, so it is not worth a warning of its own.
    """
    if state is None:
        return []
    try:
        records, _frames, source = L.base_animation(
            map_dir, doc, state.get("resource"))
    except L.LiveLinkError:
        return []
    return L.animation_report(records, source)


def animation_install(client, map_dir, doc, state):
    """Install the pushed map's palette animation and grade the whole leg.

    Yields `(report kind, line)`. The readback runs whatever the install did:
    the erase has already happened, so *nothing moves* is a real result about
    the removal even when there was nothing to add.

    The two halves are reported in **different words** on purpose (decision
    10): the palette half is graded behaviourally, by rows moving over the
    dwell, and the texture half is a byte read-back -- its slowest record runs
    to 4.00s a step and that is not time to spend inside a press.
    """
    records = None
    if state is not None:
        try:
            records, frames, source = L.base_animation(
                map_dir, doc, state.get("resource"))
            writes, notes = L.plan_install_animation(records, frames)
        except L.LiveLinkError as e:
            yield "WARNING", (
                f"animation NOT installed: {e}. The replaced map's animation "
                "IS gone -- the erase needs nothing from the disc -- so this "
                "costs the new map's animation, not the removal")
            records = None
        else:
            L.apply(client, writes)
            for note in notes:
                yield "INFO", f"{note} (from {source})"
    else:
        yield "WARNING", ("animation NOT installed: this document carries no "
                          "map states, so there is no resource to read one "
                          "from. The replaced map's animation IS gone")

    expected = L.animation_rows(records)
    try:
        ok, lines = L.readback_animation(client, expected,
                                         L.animation_dwell(records))
    except L.LiveLinkError as e:
        yield "WARNING", f"animation NOT read back: {e}"
    else:
        for line in lines:
            yield ("INFO" if ok else "WARNING"), line
    try:
        ok, lines = L.confirm_animation_erased(client)
    except L.LiveLinkError as e:
        yield "WARNING", f"animation: the table could not be re-read: {e}"
    else:
        for line in lines:
            yield ("INFO" if ok else "WARNING"), line


def rig_lines(states, index, rig_source, ram, registers):
    """What the rig push moved, and every state that moved with it.

    Decision 27's rule -- every state the act touched is NAMED -- and the rig
    needs it as much as the sheet does, because a rig-less state renders with a
    keyed partner's rig (38.5% of corpus states carry none of their own), so
    the resource the bytes came from is not the state that was aimed at.
    """
    at = L.aim(states, index)
    out = [f"light rig: {ram:,} RAM byte(s) + {registers} GTE register(s), "
           f"aimed at night={at.night} weather={at.weather} kind {at.kind}"
           + (f", rig from {rig_source}" if rig_source else "")]
    shared = sorted({(s["night"], s["weather"]) for s in states
                     if s.get("resource") == rig_source
                     and (s["night"], s["weather"]) != (at.night, at.weather)})
    if shared:
        out.append(f"  that rig also renders {', '.join(map(str, shared))}")
    out.append("  the GTE half does NOT survive a map reload OR a state "
               "change -- the RAM half is what a reload reads back")
    return out



class _Say:
    """The push's report, collected -- and the reason it can leave the main
    thread.

    `Operator.report` is `bpy`, so a worker may not call it; a background push
    therefore builds its lines with `report=None` and the main thread hands
    them to the operator, the Log and the terminal when it lands them
    (`push_report`). The one behavioural difference is the toast, which a
    settle never had anyway -- nobody pressed a button.
    """

    def __init__(self, report=None):
        self.lines = []
        self._report = report

    def __call__(self, kind, text, keep=True):
        if self._report is not None:
            self._report({kind}, text)
        if keep:
            self.lines.append(("REFUSE: " if kind == "ERROR" else "") + text)


def push_gather(context, ob, say, *, replace_loaded_map=False,
                skip_selfcheck=False):
    """MAIN THREAD -- everything a push needs OUT of Blender, as plain data.

    This split is what lets the settle stop freezing the artist between
    strokes. Every `bpy` read the push does is here -- the preferences, the
    compile, `assemble`, the marker's imported polygons, its preview state and
    its base map directory -- and `push_transport` below does the rest with
    none at all, which is what makes it safe to run on a worker.

    Measured on MAP022 a0 (454 polygons), this box: `assemble` is **375 ms**
    and cannot leave the main thread, while the transport is about **670 ms**
    of round trip -- 16 whole-RAM GETs at 31 ms each plus 5 whole-VRAM GETs at
    34 ms -- and can. So the half that moves is the bigger half, and it is
    also the half that is waiting on another process rather than working.

    (The GET count is not a redundancy to fix: `hold()` answers reads from one
    image, and every `apply` drops it on purpose, because the reads AFTER a
    write -- `verify`, the packet witnesses, the picture's readback -- are the
    ones whose whole job is to see what landed.)

    Returns the keyword arguments `push_transport` takes, or `None` if the
    push was refused before any of it; in that case `say` already holds why.
    """
    from .authoring import suspended
    prefs = _prefs(context)
    host = getattr(prefs, "live_host", "") or L.DEFAULT_HOST
    port = int(getattr(prefs, "live_port", 0) or L.DEFAULT_PORT)
    # #606: two clients, because the push writes two things that live in
    # different places -- not because there is a choice to make. `client`
    # is main RAM (`POST /api/v1/cpu/ram/raw`); `lua` is the light rig's
    # GTE half, which writes coprocessor control registers that are not
    # `m_wram` and that no HTTP endpoint reaches. Both are stock
    # pcsx-redux. The transport preference that used to pick between them
    # is gone (part 3): its off position needed our fork, which is not a
    # decision to put in front of an artist.
    lua = L.LuaClient(host=host, port=port)
    client = L.RamClient(host=host, port=port)
    # The cheapest failure first: an emulator that is not there costs two
    # seconds to find out about and the assemble below costs more.
    problem = lua.check()
    if problem:
        # Three states, not two (`LuaClient.check`): an emulator running
        # without the handlers is the likeliest failure here and the one a
        # bare "no emulator answering" would misdiagnose.
        #
        # The remedy is named at the point of failure rather than left in
        # the preferences, because this refusal IS the moment the artist
        # finds out they need it.
        say("ERROR", problem + "\n    -- or press 'Launch PCSX-Redux' "
                               "below, which starts it with the handlers "
                               "already loaded")
        return None

    # 1. the document, in memory. Export's own refusals, not a second copy.
    #    The compile runs FIRST and outside `assemble` (decision 25): the
    #    binding it moves is one of the three fields this push writes into
    #    RAM, so a compile landing after `assemble` read the mesh would push
    #    a different map than it exported.
    with suspended():                     # §6.1, as on the import side
        from .compile_op import ensure_compiled
        for note in ensure_compiled(ob):
            say("INFO", note)
        # `sidecars=False`: the push consumes `doc` and `rep.sheets` and
        # throws `files` away, and encoding them is 0.9 s of the artist's
        # frozen Blender at a 4x Painting.  A bundle write must never pass it.
        doc, _files, rep = assemble(ob, sidecars=False)
    for w in rep.warnings:
        say("WARNING", w)
    if rep.refusals:
        say("ERROR", f"{len(rep.refusals)} refusal(s), nothing pushed: "
                     + "; ".join(rep.refusals[:12])
                     + (" ..." if len(rep.refusals) > 12 else ""))
        return None

    return {
        "client": client, "lua": lua, "host": host, "port": port,
        "doc": doc, "rep": rep, "ob_name": ob.name,
        # The three marker reads the transport used to make for itself. They
        # are cheap, but they are `bpy`, and `bpy` from a worker is undefined
        # behaviour rather than a slow answer.
        "base": base_polygons(ob),
        "index": int(ob.get("exmateria_map/preview_state") or 0),
        "anim_dir": base_map_dir(ob),
        "replace_loaded_map": replace_loaded_map,
        "skip_selfcheck": skip_selfcheck,
    }


def push_transport(say, *, client, lua, host, port, doc, rep, ob_name, base,
                   index, anim_dir, replace_loaded_map, skip_selfcheck):
    """NO `bpy` FROM HERE DOWN -- the half a settle runs off the main thread.

    Everything it needs is a plain object `push_gather` read for it: `doc` and
    `base` are JSON-shaped lists and dicts, `rep.sheets` is `{name: bytes}`
    (`export_sheets` hands back the disc's own 131,072-byte layout rather than
    a Blender image), `anim_dir` is a `Path`, and the three clients are HTTP.

    Keep it that way. A `bpy` call added here is not a slow push; it is a
    crash in another thread with no traceback the artist will ever see.
    """
    # The push's main-RAM image, held for the length of the push and dropped
    # at every exit (ADR-0186 Amendment 7, decision 32). A `with` rather than
    # the `ExitStack` this used to need: the transport has one entry and every
    # exit is a `return` out of it, so there is nothing left to unwind by hand.
    with client.hold():
        return _transport(
            say, client=client, lua=lua, host=host, port=port, doc=doc,
            rep=rep, ob_name=ob_name, base=base, index=index,
            anim_dir=anim_dir, replace_loaded_map=replace_loaded_map,
            skip_selfcheck=skip_selfcheck)


def _transport(say, *, client, lua, host, port, doc, rep, ob_name, base,
               index, anim_dir, replace_loaded_map, skip_selfcheck):
    """`push_transport`'s body, inside the `hold()`. Read that docstring."""
    # 2. the gate
    try:
        descriptors = L.check_descriptors(L.read_descriptor_block(client))
    except L.LiveLinkError as e:
        say("ERROR", f"gate: {e}")
        return "CANCELLED"
    primary = descriptors[0]

    # 3. the counts, and the two gates that stand between a raised count
    # and memory corruption. They run BEFORE anything is planned, let
    # alone written: the loader does not bound-check the four polygon
    # arrays (ADR-0004 decision 28) and nothing here re-derives a
    # following slice's start index.
    counts = L.bucket_counts(doc["polygons"])
    if not any(counts):
        say("ERROR",
            "this document has no polygons in any bucket. Pushing it "
            "would zero all four counts, and the gate reads an all-zero "
            "descriptor as 'no map is loaded' -- so every later push "
            "would be refused and the only way back would be reloading "
            "the savestate. Refusing to write.")
        return "CANCELLED"
    try:
        for w in L.check_capacity(descriptors, counts):
            say("WARNING", w)
        L.check_followers(descriptors, counts)
    except L.LiveLinkError as e:
        say("ERROR", f"{e} -- nothing was pushed")
        return "CANCELLED"

    # 3b. the plan, before the self-check, so a document that cannot be
    # planned is refused before the artist waits on six round trips.
    try:
        plans = L.plan_document(primary, doc)
    except L.LiveLinkError as e:
        say("ERROR", f"{e} -- nothing was pushed")
        return "CANCELLED"

    # 4. the base map, for the self-check AND for reading the push's own
    # answer back. It costs no round trip -- `push_gather` read it off the
    # marker on the main thread and handed it over as a plain list.
    #
    # Its length is the IMPORTED list's, never the live descriptor's
    # counts. After a shrink the descriptor carries the SHRUNK counts, so a
    # base sized off them would check and compare the wrong slot range on
    # the second press of the same session.
    base_plans = None
    base_counts = counts
    if base is not None:
        base_counts = L.bucket_counts(base)
        base_plans = L.plan_document(primary, {"polygons": base})
    if replace_loaded_map:
        # The swap's proof, and it is a DIFFERENT question from the
        # content check rather than a lenient version of it. `selfcheck`
        # asks whether RAM holds the document's own bytes -- which is the
        # identity claim decision 7 recovered as a side effect, and which
        # this mode violates on purpose. What survives is the one fact
        # about these addresses that does not depend on which map is
        # loaded: the engine's four polygon arrays are fixed-capacity and
        # engine-global (ADR-0004 decision 28), so a write outside one is
        # corruption whatever is in RAM.
        try:
            bounded = L.check_plan_bounds(plans)
        except L.LiveLinkError as e:
            say("ERROR", str(e))
            return "CANCELLED"
        # WARNING, not INFO. The artist has just stood down the highest-
        # value check in the build, and a line they have to go looking for
        # is a line that reads as "it passed".
        for line in bounded:
            say("WARNING", line)
        # The leg a swap cannot deliver, said in swap terms. `UNPUSHED`
        # already names the terrain grid on every push -- "the map looks
        # right and COLLIDES wrong" -- and on the artist's own map that is
        # a curiosity about one field. On somebody else's map it is the
        # whole story: the tile records are the map that is still loaded,
        # so the picture is this document and the walking is not. Said
        # HERE, as its own warning, because a line that only appears in a
        # list of two every push is a line that has been scrolled past.
        say("WARNING",
            "the terrain grid has no located sink, so units will walk "
            "the map you replaced while looking at this one -- heights, "
            "slopes and walkability are all still the loaded map's. This "
            "is a picture, not a playable map; `build` is what ships one")
    elif base is None:
        say("WARNING",
            "self-check SKIPPED: this scene has faces that were not "
            "imported (or no `_shadow` attributes), so there is no "
            "base geometry to compare RAM against. The descriptor "
            "gate is the only guard on this push")
    elif not skip_selfcheck:
        ok, said = selfcheck(client, base_plans, _LAST_PUSH.get(ob_name))
        if not ok:
            for line in said:
                say("ERROR", line)
            return "CANCELLED"
        # A pass with something to say is a WARNING, not a silent INFO:
        # "this emulator was already pushed to" is the one the artist
        # needs on the marker afterwards, because it explains a picture
        # that is not the disc's.
        for line in said:
            if line.startswith("the planned addresses hold"):
                say("INFO", f"self-check: {line}", keep=False)
            else:
                say("WARNING", f"self-check: {line}")

    # 4b. the packet plan: uv, palette_id, texture_page. Separate from
    # `plan_document` because two of its inputs are only knowable live --
    # the base pointer, which ALTERNATES between two buffers, and the bytes
    # currently held, which the two masked fields modify rather than
    # replace.
    try:
        packet_plans = L.plan_packets_document(client, primary, doc)
    except L.LiveLinkError as e:
        say("ERROR", f"packets: {e} -- nothing was pushed")
        return "CANCELLED"

    # 5. the push, and the ORDER is load-bearing. The dispatch at
    # 0x800E840C recomputes `count = descriptor[+0x90 + 2*bucket]`
    # immediately before each renderer call, every frame -- so a bucket
    # that SHRANK has its count lowered FIRST (the slots past the new end
    # stop being drawn before anything under them moves) and a bucket that
    # GREW has it raised LAST (its geometry, metadata and packets are all
    # in place before the renderer is told they exist). Neither leaves a
    # frame drawing a slot that is mid-write.
    live = primary.counts
    lowered = tuple(min(counts[k], live[k]) for k in range(len(L.BUCKETS)))
    count_total = L.apply(client, L.plan_counts(primary, lowered))
    total = 0
    for key, writes in sorted(plans.items()):
        total += L.apply(client, writes)
    packet_total = 0
    for key in sorted(packet_plans, key=lambda k: (k[2], k[0], k[1])):
        packet_total += L.apply(client, packet_plans[key])
    count_writes = L.plan_counts(primary, counts)
    count_total += L.apply(client, count_writes)
    # Through `apply` AND `verify`, so a zero cannot have two causes: a
    # count that did not move because it was already right reads exactly
    # like a count that did not move because the write never landed.
    differ, compared = L.verify(client, count_writes)
    if differ:
        say("ERROR",
            f"the polygon counts did NOT take: {differ} of {compared} "
            f"byte(s) at 0x{primary.address + L.DESCRIPTOR_COUNTS:08X} do "
            "not read back as written. The geometry HAS been written, so "
            "the map is now showing a mix -- reload the savestate.")
        return "CANCELLED"
    total += count_total
    _LAST_PUSH[ob_name] = plans

    # 5b. the light rig, decision 9's other atom -- and it is TWO
    # transports, not one. Only the DIRECTION matrix is re-composed from
    # RAM every frame; the gains and the ambient are loaded into GTE
    # control registers at map load and were not seen to re-load, so a
    # RAM-only push would put this state's angles over the last-loaded
    # state's brightness (§2.2). Both halves or neither.
    states = doc.get("map_states") or []
    rig, rig_source = state_rig(states, index) if states else (None, None)
    rig_reported = []
    if rig:
        try:
            rig_ram = L.apply(client, L.plan_rig(rig))
            rig_regs = L.apply_gte(lua, L.plan_rig_gte(rig))
        except L.LiveLinkError as e:
            say("WARNING", f"light rig NOT pushed: {e}")
        else:
            rig_reported = rig_lines(states, index, rig_source,
                                     rig_ram, rig_regs)
    elif states:
        say("WARNING",
            "light rig NOT pushed: no state in this arrangement carries "
            "one, so the map renders albedo (46 states corpus-wide)")

    # 5c. the sheet and the palettes -- decision 2's ATOM, and it spans two
    # memories. The sheet's pixels are VRAM and stay there (uploaded once
    # at map load, never re-uploaded); the palettes are VRAM's CLUT rows
    # and are NOT VRAM's to keep -- the engine re-uploads that block from
    # main RAM every frame, so a CLUT write to VRAM is gone in 50 ms.
    # Measured [LIVE] 2026-08-26 against a sheet write that held for a
    # second in the same session.
    #
    # Both are PLANNED before either is applied, which is what makes them
    # one act: a sheet shown through the wrong state's CLUTs is garbage
    # rather than a stale picture, so a half that cannot be planned takes
    # the other half with it.
    # 5b-bis. the host map's ANIMATION (decision 11), and it is the ERASE
    # half -- before the palettes and the sheet, so that the last frame the
    # replaced map painted is overwritten by this document's colours rather
    # than racing them.
    #
    # Only on a Replace. The rule is one line: **neutralise foreign
    # animation; never neutralise a map's own.** On the edit path the
    # animation belongs to this document's map, `build` carries `0x6c` and
    # `0x70` to the disc verbatim, and freezing it would preview a picture
    # the shipped map can never produce.
    anim_state = states[index] if states else None
    anim_erased = False
    if not replace_loaded_map:
        for line in animation_edit_lines(anim_dir, doc, anim_state):
            say("INFO", line)
    elif not any(client.read_live(L.ANIM_TABLE, L.ANIM_TABLE_BYTES)):
        # An empty table is compatible with every map on the disc, so it
        # cannot confirm the address -- and there is nothing there to
        # remove either. The cost is that this map's own animation is not
        # installed on a host that had none; that is the behaviour every
        # push had before decision 11, so it is a gap rather than a
        # regression, and it is named rather than left to be found (#659).
        say("INFO",
            "animation: the loaded map's instruction table is empty, so "
            "there is nothing of it to remove. This document's own "
            "animation is NOT installed either -- an empty table cannot "
            "confirm the address it would be written to")
    else:
        try:
            matched = L.check_animation_table(
                client.read_live(L.ANIM_TABLE, L.ANIM_TABLE_BYTES),
                L.animation_tables(anim_dir))
        except L.LiveLinkError as e:
            say("WARNING", f"animation NOT erased: {e}")
        else:
            say("INFO",
                f"animation: the loaded map's instruction table matches "
                f"{len(matched)} corpus resource(s) "
                f"({', '.join(matched[:6])}"
                f"{' ...' if len(matched) > 6 else ''}) -- erasing it, "
                "because it is the replaced map's and it repaints CLUT "
                "rows and sheet rectangles under this push")
            L.apply(client, L.plan_erase_animation())
            anim_erased = True

    sheet_reported = []
    if states:
        at = L.aim(states, index)
        try:
            witnesses = L.packet_witnesses(client, primary, doc)
            at_vram = VR.derive_addresses(witnesses)
        except (L.LiveLinkError, VR.VramError) as e:
            say("WARNING", f"sheet and palettes NOT pushed: {e}")
        else:
            # Where the sheet and the CLUT block live is DERIVED from the
            # live packets rather than hard-coded (`derive_addresses`:
            # neither base is FFT's to promise). Under a swap that
            # derivation is self-consistent -- the packet plan keeps the
            # loaded map's base bits and replaces only the two masked
            # fields, so the polygons point at the same column the sheet
            # is written to -- but it is the REPLACED map's layout that
            # both halves agree on, and whether a foreign map's four page
            # rectangles have the shape this document's sheet needs is
            # unmeasured. Say which map the address came from; do not
            # claim it is the right one.
            if replace_loaded_map:
                say("WARNING",
                    "the sheet's column and the CLUT block's row were "
                    "derived from the map you replaced -- the engine's "
                    "own packets are the only witness to either, and on a "
                    "swap those packets are the loaded map's. Both halves "
                    "agree with each other; whether that layout suits "
                    "this document's sheet is not measured")
            vram = VR.VramClient(host=host, port=port)
            sheet_reported = push_picture(
                client, vram, at, at_vram, rep.sheets, say)
    elif not states:
        say("WARNING", "sheet and palettes NOT pushed: this document "
                       "carries no map states")

    # 5d. the INSTALL half of decision 11, and the readback that grades
    # both halves. Last, so a pushed map's own animation is not immediately
    # flattened by the static palette write behind it.
    if anim_erased:
        for kind, line in animation_install(client, anim_dir, doc,
                                            anim_state):
            say(kind, line)

    say("INFO", describe_divergence(rep), keep=False)
    say("INFO", f"pushed {total + packet_total:,} changed byte(s) into "
                f"{host}:{port} ({len(doc['polygons'])} polygons, "
                f"{len(plans)} geometry plan(s), "
                f"{len(packet_plans)} packet plan(s) over "
                f"{len(L.PACKET_BASES)} buffers)")
    if packet_total:
        say("INFO", f"of those, {packet_total:,} byte(s) were texture "
                    f"packets -- UVs, palettes and texture pages")
    delta = [f"{b} {was} -> {now}" for b, was, now
             in zip(L.BUCKETS, live, counts) if was != now]
    if delta:
        say("INFO", "polygon counts: " + "; ".join(delta))
    for line in interpret(total, authored_bytes(
            base_plans, plans,
            L.plan_counts(primary, base_counts), count_writes)):
        say("INFO", line)
    for line in rig_reported:
        say("INFO", line)
    for line in sheet_reported:
        say("INFO", line)
    say.lines.extend(unpushed_lines({f for _, f in plans}
                                    | {k[1] for k in packet_plans}))
    say.lines.append("a picture, not a disc: a map reload uploads the disc's "
                     "bytes back over all of it -- `build` is what ships")
    print(f"EXMATERIA-MAP: pushed {total:,} byte(s) to {host}:{port} "
          f"({len(doc['polygons'])} polygons)")
    return "FINISHED"

def push_report(ob, lines):
    """MAIN THREAD -- the three homes a push's report has.

    Split out of the old `finish` because a background push lands its report
    long after the operator that started it returned, and two of the three are
    `bpy`: the marker property and the Log's Text datablock.
    """
    if ob is not None:
        ob[LAST_PUSH_KEY] = json.dumps(lines)
    # Every finish, refusals included -- a push that refused is the one
    # the artist most wants to read.
    from .report_log import record
    record("Push to PCSX-Redux", ob.name if ob is not None else "", lines)
    # ...and to the TERMINAL.  The panel used to draw a status row and
    # the refusals; the artist's rule is that a run's output is console
    # output, so this is where the deleted rows went.  Printed once per
    # push -- never from a `draw`, which runs on every redraw of the region.
    # `record` above keeps the selectable copy in the Log; this is the one a
    # terminal `tail -f` sees, and between them the deleted panel text has two
    # homes rather than none.
    for line in lines:
        print(f"EXMATERIA-MAP push: {line}")


def push_aims_the_camera(context, say, client):
    """The push's fourth leg: aim the battle where the viewport is aimed.

    ADR-0186 Amendment 16 **decision 75**, which supersedes decision 73.  Asked
    for by name once Manual mode existed: *"when I do push to pcsx I want it to
    move the camera too -- it's basically like the same as if you had automatic
    on, and did one push of everything"*.  That is the requirement stated
    exactly: **one press delivers the picture the ticker would have delivered**,
    so a Manual artist gets the whole of what Automatic gives them for one
    click rather than two.

    **Unconditional -- it does not read `live_camera_sync`.**  That switch is
    labelled *"Sync camera CONTINUOUSLY"* and it gates the TIMER; a press is
    the artist asking, which is the case it was never about.  With the sync on
    this is a no-op in effect -- the ticker has already sent this pose -- and
    with it off it is the whole point.

    **It can never turn a delivered push into a failure.**  The geometry has
    already landed by the time this runs, and a viewport that cannot be synced
    is a normal state rather than an error: none open (a push driven from a
    script or a search box), or one looking through a scene camera or a
    perspective lens, which `check_view_syncable` refuses because no arithmetic
    can make an orthographic engine agree with it.  Each is REPORTED and the
    push still says FINISHED.  A camera leg that could cancel a push that had
    already written 30,000 bytes would make this button less reliable than the
    two it replaces, which is the opposite of what was asked for.

    The `client` is the push's OWN -- the one `push_gather` built from the same
    host and port -- so there is no second place for those to be read from and
    no second connection to open.
    """
    rv3d = _region_3d(context)
    if rv3d is None:
        say("INFO", "camera: not aimed -- no 3D viewport to take one from")
        return
    dial = float(getattr(_prefs(context), "live_camera_zoom_dial", 1.0) or 1.0)
    try:
        _pose, lines = push_camera(client, rv3d, dial)
    except Exception as exc:                                  # noqa: BLE001
        # Every failure is the same story to the artist -- the map went, the
        # camera did not -- and the message says which. Broad on purpose: a
        # transport error, a refused view and a bug in the pose arithmetic must
        # all leave a FINISHED push finished.
        say("INFO", f"camera: not aimed -- {exc}")
        return
    for line in lines:
        say("INFO", line)


def push_now(context, ob, report=None, *, replace_loaded_map=False,
             skip_selfcheck=False):
    """The whole push, on THIS thread. What the button does.

    Returns `("FINISHED" | "CANCELLED", lines)`.

    Four legs since Amendment 16 decision 75, not three: the camera is aimed
    after the bytes land, and only after they land -- a push the emulator
    refused has nothing to look at, and a second refusal line would say the
    same thing twice.
    """
    say = _Say(report)
    kw = push_gather(context, ob, say, replace_loaded_map=replace_loaded_map,
                     skip_selfcheck=skip_selfcheck)
    status = "CANCELLED" if kw is None else push_transport(say, **kw)
    if status == "FINISHED":
        push_aims_the_camera(context, say, kw["client"])
    push_report(ob, say.lines)
    return status, say.lines


#: The one background push in flight, and what it finished with.
#:
#: A single slot rather than a queue, for the same reason `settle_op` keeps
#: one: the settle's own clock never starts a second compile while one is in
#: flight, and a queue of pushes would send the emulator a document the artist
#: has already painted over. A push that arrives while one is running is
#: COALESCED -- dropped, with `pending` set, so the caller can ask again the
#: moment the current one lands.
_BG = {"thread": None, "ob_name": "", "done": None, "pending": False}


def background_push_busy():
    thread = _BG["thread"]
    return thread is not None and thread.is_alive()


def background_push_start(context, ob):
    """Gather here, transport on a worker. Returns why it did NOT start, or "".

    The `bpy` half runs before the thread is created, so the worker is handed
    plain data and never touches Blender -- `push_transport`'s docstring is
    the contract, and this is the only caller that depends on it.
    """
    if background_push_busy() or _BG["done"] is not None:
        _BG["pending"] = True
        return "a push is already in flight"
    say = _Say()
    kw = push_gather(context, ob, say)
    if kw is None:
        _BG["done"] = (ob.name, "CANCELLED", say.lines)
        return ""
    _BG["pending"] = False

    def work():
        try:
            status = push_transport(say, **kw)
        except Exception as exc:                              # noqa: BLE001
            # A worker's traceback goes to the terminal and nowhere else, so
            # the report has to carry the reason back itself or the artist
            # gets a push that silently never happened.
            traceback.print_exc()
            say("ERROR", f"the background push raised: {exc!r}")
            status = "CANCELLED"
        _BG["done"] = (kw["ob_name"], status, say.lines)

    # `worker.spawn`, never a bare `Thread` -- see `worker`'s table: a
    # transport thread's own Python work costs the UI 25x if the GIL is left
    # on CPython's default switch interval.
    from .worker import spawn
    _BG["thread"] = spawn(f"exmateria-push:{ob.name}", work)
    return ""


def background_push_land():
    """MAIN THREAD -- land a finished background push, or return `None`.

    Called from the settle's own timer, which is already running at 4 Hz, so
    the push needs no timer of its own.
    """
    done = _BG["done"]
    if done is None or background_push_busy():
        return None
    _BG["done"], _BG["thread"] = None, None
    ob_name, status, lines = done
    push_report(bpy.data.objects.get(ob_name), lines)
    pending, _BG["pending"] = _BG["pending"], False
    return ob_name, status, lines, pending


class MAP_OT_live_push(Operator):
    """Push the scene into a running PCSX-Redux battle. No file is written."""

    bl_idname = "map.live_push"
    bl_label = "Push to PCSX"
    bl_description = ("Push this map into a running PCSX-Redux battle over the "
                      "Lua web server, and aim the battle's camera where this "
                      "viewport is aimed. Edits the picture the game is "
                      "rendering -- it does not touch the ISO and does not "
                      "survive a map reload")
    bl_options = {"REGISTER"}

    #: There is no good reason to skip it, so the panel does not offer it; the
    #: harnesses drive the operator directly and need to be able to.
    skip_selfcheck: BoolProperty(
        name="Skip the write-path self-check", default=False,
        options={"HIDDEN", "SKIP_SAVE"})

    #: The artist's declaration that the emulator holds a DIFFERENT map, and
    #: that replacing it is the point. It is a MODE, not a skip: the content
    #: self-check is exchanged for `check_plan_bounds`, never dropped. The
    #: distinction is the whole of why `skip_selfcheck` above stays hidden --
    #: a swap has a proof it can pass, so an artist never needs the escape
    #: hatch that has none.
    replace_loaded_map: BoolProperty(
        name="Replace the loaded map", default=False,
        description=(
            "Push this document over whatever map the emulator has loaded, "
            "instead of editing the one it holds. The write-path self-check "
            "cannot run -- it proves the addresses by asking RAM for the "
            "document's own bytes, which a different map does not hold -- so "
            "the addresses are bounds-checked against the engine's arrays "
            "instead. Weaker, and the report says so"),
        options={"SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        return bool(markers(context.scene))

    def execute(self, context):
        ob, problem = find_marker(context)
        if problem:
            self.report({"ERROR"}, problem)
            return {"CANCELLED"}
        # A settle that found no emulator went quiet rather than nag; pressing
        # the button by hand is the artist saying there may be one now.
        from .settle_op import resume_pushing
        resume_pushing()
        status, _lines = push_now(
            context, ob, self.report,
            replace_loaded_map=self.replace_loaded_map,
            skip_selfcheck=self.skip_selfcheck)
        return {status}



class MAP_PT_live_push(Panel):
    """`Map` sidebar, 3D viewport, FIRST: push the scene into a running
    PCSX-Redux battle.

    **Three controls and no prose.** It used to carry three blocks of text as
    well -- a `What a push carries` sub-panel, a *"Set the PCSX-Redux folder"*
    hint, and the last push's report -- and all three are gone.  Reported from
    use: *"I don't care about the 'what a push carries' section.  delete it.
    that belongs in a console or something.  same thing with the other 2
    warnings.  you are putting console stuff in the ui area."*

    That is a rule, not three deletions, and it is the one this panel is now
    built to: **a panel holds things you PRESS; what a run had to say goes to
    the console and to the Log.**  Nothing was lost to it -- see
    `MAP_OT_live_push.execute`'s `finish`, which now prints every line it
    stores.  The `UNPUSHED` list the sub-panel rendered is already one of those
    lines on every push (`unpushed_lines`), so it is said once per run, in the
    place a run's output belongs, instead of standing on screen forever.
    """
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Map"
    bl_label = "Push to PCSX-Redux"
    # FIRST in the tab.  Reported from use: *"the most important are going to
    # be opening pcsx redux, and pushing to pcsx redux."*  Both live in this
    # panel -- `Launch PCSX-Redux` and `Push to PCSX` -- which makes it the
    # only panel holding two of the artist's top controls, and it was `6`:
    # below Terrain, Lighting Bake and Export, at the bottom of the column.
    # The loop it closes is the tightest one in the addon (edit -> push ->
    # look at the emulator), so it goes where the loop's last click is a
    # glance away rather than a scroll.
    bl_order = 0

    @classmethod
    def poll(cls, context):
        return marker_in_scene(context) is not None

    def draw(self, context):
        layout = self.layout
        ob = marker_in_scene(context)
        if ob is None:
            return
        layout.operator(MAP_OT_live_push.bl_idname, icon="PLAY",
                        text="Push to PCSX")
        # Two buttons, not a checkbox beside one. They are different acts:
        # the first edits the map the emulator has loaded and is proved by
        # asking RAM for that map's own bytes; the second replaces whatever is
        # loaded and cannot be proved that way, because a different map does
        # not hold them. A checkbox reads as a setting on one act and would
        # leave the artist to notice that the check quietly became a weaker
        # one; a separate button is the declaration decision 2 says has to
        # come from the person who loaded the savestate, since no RAM address
        # holding the current map id is known.
        swap = layout.operator(MAP_OT_live_push.bl_idname,
                               icon="FILE_REFRESH",
                               text="Replace the loaded map")
        swap.replace_loaded_map = True
        prefs = _prefs(context)
        if prefs is not None:
            # ADR-0186 Amendment 16 decision 71 -- all on, or all off, right
            # here. Reported from use: *"i know there might be individual
            # toggles but i want an all on or all off next to where the auto
            # push button is."* Two positions and no prose, which is this
            # panel's rule; and like the ortho toggle in the Camera panel it is
            # drawn as the DERIVED state, so it is its own indicator and needs
            # no line of text beside it saying which way it is set.
            #
            # The individual switches stay in the addon preferences. They are
            # not deleted and they are not duplicated here: an artist who wants
            # the settle off but the push on can still say so, and this control
            # then reads `Mixed` rather than lying about it.
            layout.prop(prefs, "live_mode", expand=True)
            row = layout.row(align=True)
            row.prop(prefs, "live_host", text="")
            row.prop(prefs, "live_port", text="")
            # Launching the emulator belongs HERE, not only in the preferences.
            # The moment an artist needs it is the moment a push has just come
            # back "no emulator answering" -- and sending them to another window
            # to fix that is the friction the launch button existed to remove.
            # The binary is still a preference, because it is a property of
            # their machine and not of this map; only the ACTION is here.
            #
            # Deliberately no live status light. `draw` runs on every redraw of
            # this region, and a status would mean a socket connect on each one.
            layout.operator("exmateria_map.launch_pcsx", icon="CONSOLE",
                            text="Launch PCSX-Redux")


# --- `What a push carries`: DELETED (2026-08-27) -----------------------------
#
# It was `MAP_PT_live_push_carries`, a DEFAULT_CLOSED sub-panel, plus the
# `NOT_CARRIED` prose table that keyed `live_link.UNPUSHED`'s own field names.
# Reported from use: *"I don't care about the 'what a push carries' section.
# delete it.  that belongs in a console or something."*
#
# **The limit it documented is not deleted with it.**  The panel existed
# because *"I change map preview and hit push and nothing happens"* had two
# causes and neither was on screen; `unpushed_lines()` still exists, every
# push still calls it, and its lines still land in the operator report, in the
# Log, and -- new with this deletion -- on stdout.  The difference is that they
# are said ONCE PER PUSH, next to the push they describe, instead of standing
# in the column forever being scrolled past.  That is strictly better
# provenance: the old panel could not say whether a given push was affected,
# because it was a static restatement of a table.
#
# `NOT_CARRIED`'s prose is the one thing that genuinely went.  It explained two
# of `UNPUSHED`'s fields in the artist's terms ("Not the terrain GRID -- the
# tile records").  `UNPUSHED`'s own `why` strings are what the lines carry now.
# If those read badly in the console, fix them in `live_link.UNPUSHED`, which
# is the table everything reads -- do not reintroduce a second copy here, which
# is the defect ADR-0186 Amendment 3 already named once.


# --- decision 12: the camera sync -------------------------------------------
# *"Blender is looking at one part of the map and the emulator at another"*, so
# an authored map cannot be compared against what the engine renders. The push
# goes ONE WAY, Blender -> emulator, and the reason is not preference: the
# battle camera's player-reachable envelope is eight yaw notches, a
# thirteen-degree pitch band and two zoom steps, so a sync in the other
# direction would only ever replay poses the artist already cannot escape.
#
# All the arithmetic is `live_link`'s and `tests/test_live_link.py` grades it
# against the battle savestate. What is here is the half that needs Blender:
# unpacking a `RegionView3D`, the section's four controls, and the report.


#: The game's yaw quantum: 4096 / 8, the L1/R1 notch.
YAW_NOTCH = L.ANGLE_UNITS // 8


def push_camera(client, view, dial: float = 1.0,
                sink: str = L.CAMERA_SINK_DEFAULT):
    """Sync one Blender viewport to a running battle. Returns `(pose, lines)`.

    `view` is a `RegionView3D` and is duck-typed on purpose -- four attributes,
    all of them plain data -- so the harness can drive this with a viewport
    that Blender in `--background` does not have.

    The readback at the end is the automatable half of the one thing decision
    12 leaves open. It does not read back the bytes that were just written; it
    reads `CAMERA_VIEW_MATRIX`, which the engine recomposes from
    `work_rotation` every frame, and requires it to be the matrix this pose
    implies. The engine did the composing, so agreement means the write reached
    something downstream actually consumes -- the distinction decision 11 was
    reported for, where a byte readback passed a dead animation.

    It REPORTS rather than refuses. The fallback for a sink that does not stick
    is to pause the emulator, and a paused emulator runs no frame in which to
    rebuild anything, so a refusal here would break the way out.
    """
    L.check_view_syncable(view.view_perspective)
    pose = L.camera_pose(view.view_location,
                         view.view_rotation.to_matrix(),
                         float(view.view_distance), dial)
    client.write(L.plan_camera(pose, sink))

    pitch, yaw, _roll = pose.angles
    lines = [f"camera: pitch {pitch}, yaw {yaw}, roll 0 (4096 = 360 degrees), "
             f"zoom {pose.zoom}/4096, aimed at "
             f"{tuple(round(c / 4096, 2) for c in pose.position)}"]
    # Decision 12 part 3: the datum is poked, so say what that costs. FFT
    # frames the action two thirds down the frame; putting the optical centre
    # in the middle is what makes the two pictures line up, and it means the
    # emulator is no longer framing the shot the way the game would.
    lines.append(f"the engine's vertical datum is set to "
                 f"{L.SCREEN_CENTRE_DATUM} so the two views share a centre -- "
                 f"the emulator's framing is not authentic while a sync is on")
    # Decision 4's rule, one field over: push what has a sink, NAME what
    # breaks. Said once per match, in the console, where a run's output goes --
    # not as a standing line in the panel, which is the deleted sub-panel's
    # defect.
    if yaw % YAW_NOTCH:
        lines.append(f"yaw {yaw} is between the game's 45-degree notches, so "
                     f"UNIT SPRITES will not agree with the terrain: each one "
                     f"picks its SEQ slot from the camera yaw's octant and "
                     f"pops to the nearest. Terrain is a matrix and is "
                     f"unaffected, and terrain is what you are comparing")

    agrees, error = L.camera_readback(
        client.read_live(L.CAMERA_VIEW_MATRIX, 18), pose.angles)
    if agrees:
        lines.append(f"the engine rebuilt its own view matrix from this pose "
                     f"(error {error:.5f})")
    else:
        lines.append(f"the engine's view matrix did not follow this pose "
                     f"(error {error:.3f}) -- the write reached RAM but not a "
                     f"sink the engine rebuilds from. Pause the emulator and "
                     f"step a frame, which is the way out that needs no "
                     f"working sink at all")
    return pose, lines


def _region_3d(context):
    """The viewport this panel is drawn in, or the first 3D view in the screen.

    Both, because an operator invoked from the panel has `space_data` and one
    invoked from a search box may not.
    """
    space = getattr(context, "space_data", None)
    rv3d = getattr(space, "region_3d", None) if space is not None else None
    if rv3d is not None:
        return rv3d
    screen = getattr(context, "screen", None)
    for area in getattr(screen, "areas", None) or ():
        if area.type == "VIEW_3D":
            return area.spaces.active.region_3d
    return None


#: The continuous sync's memory, one per Blender session. Module scope for the
#: same reason `_LAST_PUSH` is: it is a claim about a process's RAM, and it
#: must not outlive the process that made it.
_CAMERA_TICKER = L.CameraSyncTicker()


def sync_camera(client, view, dial=1.0, ticker=None,
                sink=L.CAMERA_SINK_DEFAULT):
    """One tick of the continuous sync, SYNCHRONOUSLY. Returns the lines
    worth printing.

    ⚠ **The timer does not call this** — it calls `sync_camera_background`,
    and wiring it back here reinstates the artist's *"blender is laggy when
    panning"*: the transport is four HTTP round trips at ~32 ms each, and this
    function takes all four on the calling thread (decision 16).

    It is kept, and it has two jobs. It is the shape the ticker's decisions are
    graded against one call at a time, which is far easier to read than the
    worker path; and it is the **seeded defect** in
    `tests/blender_camera_stall.py --seed`, which is what proves that harness's
    floors can go red at all. Deleting it would take the seed with it.

    The same arithmetic and the same write plan as `push_camera`, with two
    differences, both `CameraSyncTicker`'s and both deliberate:

    * it writes only when the pose CHANGED, so a still viewport is free;
    * it does **not** read back. The readback is a second round trip and a line
      per tick to re-answer what pressing the button once already answered, and
      decision 11's lesson does not apply twice.

    A viewport that cannot be synced -- none open, or one looking through a
    scene camera -- is IDLE rather than an error: `push_camera` refuses because
    a press is a request, while a tick is a standing offer and the artist has
    not asked for anything.
    """
    ticker = _CAMERA_TICKER if ticker is None else ticker
    pose, idle = camera_pose_of(view, dial)
    if idle is not None:
        return ticker.idle(idle)
    if not ticker.wants(pose):
        return []
    try:
        client.write(L.plan_camera(pose, sink))
    except Exception as e:                   # any transport failure is the
        return ticker.failed(e)              # same story: it did not land
    return ticker.succeeded(pose)


def camera_pose_of(view, dial=1.0):
    """The `bpy` half of a tick: `(pose, None)`, or `(None, idle reason)`.

    Split out because it is the ONLY part of a tick that has to happen on
    Blender's own thread -- everything after it is a socket. `sync_camera` and
    `sync_camera_background` share it so the two cannot drift on which
    viewports they refuse.
    """
    if view is None:
        return None, "no 3D viewport to take a camera from"
    try:
        L.check_view_syncable(view.view_perspective)
    except L.LiveLinkError as e:
        return None, str(e)
    return L.camera_pose(view.view_location, view.view_rotation.to_matrix(),
                         float(view.view_distance), dial), None


def sync_camera_background(make_client, view, dial=1.0, ticker=None,
                           sink=L.CAMERA_SINK_DEFAULT):
    """One tick with the transport on a WORKER. What the timer calls.

    **Why this exists, measured against the running emulator 2026-08-29.**
    Every request to pcsx-redux costs a fixed **~32 ms service wait**: a 404
    that does no work at all is as expensive as the 2 MB whole-RAM GET, whose
    body then streams in half a millisecond. So the cost is the number of
    REQUESTS, not the bytes -- and a changed-pose tick is four of them (the GET
    for the before-image, then three POSTs, the datum coalescing into
    `work_rotation`). ~128 ms, on Blender's thread, every 50 ms.

    Measured on the latency-matched stub (`tests/blender_camera_stall.py`):
    the main thread kept **0.26** of its Python throughput and froze for 136 ms
    at a stretch, and the 20 Hz timer achieved 5.6 Hz. That is the artist's
    *"blender is laggy when panning"*.

    **A thread is the right answer HERE, and it was the wrong one for the
    compile** -- which is the interesting half of the artist's question. The
    compile was CPU-bound Python holding the GIL, where a worker took Blender
    to 8.7 fps (`worker.py`'s table) and only numpy fixed it. A socket read
    RELEASES the GIL, so this one really does leave the main thread free: the
    same stub measures **0.99** and the full 20 Hz once the transport moves.

    The emulator still sees ~128 ms of latency and that is allowed --
    decision 15's bar is *the UI thread on top; the emulator may lag; the
    number one goal is to not prevent the artist from painting.*

    `make_client` is a factory, not a client: the client is built ON the worker
    so nothing shared crosses the thread boundary.
    """
    ticker = _CAMERA_TICKER if ticker is None else ticker
    # What the WORKERS reported since the last tick, turned into lines here on
    # the main thread -- `landed` only records, because a worker may not touch
    # the Log or `bpy`.
    lines = ticker.drain()
    pose, idle = camera_pose_of(view, dial)
    if idle is not None:
        return lines + ticker.idle(idle)
    if not ticker.wants(pose):
        return lines
    if not ticker.begin(pose):
        return lines          # in flight: COALESCE, never queue -- see `begin`

    def job():
        try:
            make_client().write(L.plan_camera(pose, sink))
        except Exception as e:               # any transport failure is the
            ticker.landed(pose, e)           # same story: it did not land
        else:
            ticker.landed(pose, None)

    # `worker.spawn`, never a bare `Thread`: it is what protects the UI's share
    # of the GIL, and `tests/test_worker.py` fails the module that skips it.
    worker.spawn("exmateria-map camera sync", job)
    return lines


def _first_region_3d():
    """The 3D viewport the timer syncs, with no context to ask.

    A timer runs off the main loop and is handed nothing, so the window
    managers are walked directly. The FIRST 3D viewport wins, which is exact in
    the `Map` workspace -- Amendment 4's layout is Image Editor | 3D viewport,
    one of each -- and is a choice worth knowing about in a screen the artist
    split further.
    """
    for wm in bpy.data.window_managers:
        for window in wm.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    return area.spaces.active.region_3d
    return None


def _camera_sync_timer():
    """The `bpy.app.timers` callback. Returns seconds until the next look.

    `depsgraph_update_post` cannot serve this -- orbiting changes no datablock,
    so nothing fires. A `SpaceView3D.draw_handler_add` would fire, but it fires
    INSIDE the viewport's draw, and a blocking HTTP round trip there stalls the
    redraw the artist is orbiting. Between the two shapes this addon already
    has, the timer is the one that can be slow without being felt.

    The toggle gates this function, not the registration: an unregistered timer
    would need re-registering from a property callback, and a `poll` that reads
    one boolean every two seconds is cheaper than that is correct.
    """
    # MEASURED, and it is the arm that keeps a test run off the artist's
    # emulator: `--background` Blender still holds one window with a VIEW_3D
    # area (`['PROPERTIES', 'OUTLINER', 'DOPESHEET_EDITOR', 'VIEW_3D']` under
    # `--factory-startup`), so `_first_region_3d` finds a real `region_3d` and
    # a tick would compute a pose and POST it. There is no artist orbiting a
    # headless viewport, so there is nothing to sync -- returning None
    # unregisters the timer for the rest of that process.
    if bpy.app.background:
        return None
    context = bpy.context
    prefs = _prefs(context)
    if prefs is not None and not getattr(prefs, "live_camera_sync", True):
        _CAMERA_TICKER.reset()               # so switching it back on RESENDS
        return L.CAMERA_SYNC_BACKOFF
    host = getattr(prefs, "live_host", "") or L.DEFAULT_HOST
    port = int(getattr(prefs, "live_port", 0) or L.DEFAULT_PORT)
    dial = float(getattr(prefs, "live_camera_zoom_dial", 1.0) or 1.0)
    lines = sync_camera_background(
        lambda: L.RamClient(host=host, port=port), _first_region_3d(), dial)
    if lines:
        from .report_log import record
        record("Camera sync", "", lines)
        for line in lines:
            print(f"EXMATERIA-MAP camera: {line}")
    return _CAMERA_TICKER.interval()


class MAP_OT_live_camera_match(Operator):
    """Point the battle camera where this viewport is pointing."""

    bl_idname = "map.live_camera_match"
    bl_label = "Match camera"
    bl_description = ("Aim the running battle's camera the way this viewport "
                      "is aimed. Pushes the pose faithfully -- past the pad's "
                      "own eight yaw notches, its 13-degree pitch band and "
                      "its two zoom steps, which is the point")
    bl_options = {"REGISTER"}

    def execute(self, context):
        rv3d = _region_3d(context)
        if rv3d is None:
            self.report({"ERROR"}, "no 3D viewport to take a camera from")
            return {"CANCELLED"}
        prefs = _prefs(context)
        host = getattr(prefs, "live_host", "") or L.DEFAULT_HOST
        port = int(getattr(prefs, "live_port", 0) or L.DEFAULT_PORT)
        dial = float(getattr(prefs, "live_camera_zoom_dial", 1.0) or 1.0)
        try:
            _pose, lines = push_camera(L.RamClient(host=host, port=port),
                                       rv3d, dial)
        except L.LiveLinkError as e:
            self.report({"ERROR"}, str(e))
            lines = ["REFUSE: " + str(e)]
            status = "CANCELLED"
        else:
            self.report({"INFO"}, lines[0])
            status = "FINISHED"
        # The panel's rule: a panel holds things you PRESS, and what a run had
        # to say goes to the console and to the Log. Printed from the OPERATOR,
        # once per match -- never from a `draw`, which runs on every redraw.
        from .report_log import record
        record("Match camera", "", lines)
        for line in lines:
            print(f"EXMATERIA-MAP camera: {line}")
        return {status}


class MAP_PT_live_camera(Panel):
    """`Map` sidebar, under the push: aim the battle camera with the viewport.

    **Four controls and no prose**, which is this sidebar's rule and not a
    preference (*"you are putting console stuff in the ui area"*). In
    particular there is no warning that unit sprites break outside the game's
    yaw notches -- that is named in the decision record and, once per match, in
    the console, next to the match it describes.

    The ortho toggle is a prerequisite rather than a convenience: FFT is
    orthographic, so in a perspective viewport no arithmetic can make the
    pictures match. It is drawn as the view's OWN property so that it shows the
    current state -- which is what lets it be its own indicator, and is why
    there is no line of text beside it. It is still not forced; the addon does
    not reach in and change a view the artist set.
    """

    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Map"
    bl_label = "Camera"
    bl_order = 1

    def draw(self, context):
        layout = self.layout
        layout.operator(MAP_OT_live_camera_match.bl_idname,
                        icon="CAMERA_DATA", text="Match camera")
        rv3d = _region_3d(context)
        if rv3d is not None:
            layout.prop(rv3d, "view_perspective", expand=True)
        prefs = _prefs(context)
        if prefs is not None:
            layout.prop(prefs, "live_camera_sync")
            layout.prop(prefs, "live_camera_zoom_dial")


# --- decision 13: isolate the map ------------------------------------------
# An ACT, not a ticker. The camera sync earns a timer because its SOURCE changes
# continuously -- every viewport orbit is a new pose. Isolate has no moving
# source: the artist flips it twice a session, and nothing in the engine
# re-derives these fields per frame, so there is nothing to fight. A ticker
# would spend a round trip per tick to learn there is nothing to do, and to
# learn even that it would have to READ BACK, which decision 12's Amendment 3
# refused on three stated grounds.
#
# Two buttons and no state in the UI. `Isolate map` is idempotent and
# re-pressable, and that is the whole answer to the three ways the emulator
# drifts out from under Blender: a restarted emulator, a *Replace the loaded
# map*, a unit spawning mid-battle.

#: The saved values, held in BLENDER for the length of an isolate. The cost is
#: named rather than discovered: if Blender dies while isolated the restore is
#: lost and the artist reloads the battle. Decision 3 already puts this loop on
#: the poke-don't-patch side of that line -- and persisting emulator state into
#: a `.blend` would be worse, because it goes stale the moment the emulator
#: restarts.
_ISOLATED = {"units": [], "gates": []}


def isolate_map(client):
    """Take everything that is not the map off the screen. Returns lines.

    One `hold()` answers the whole walk, so a battle's roster costs one GET and
    not one per node, and the two write batches are the only other traffic.
    """
    with client.hold():
        walk = L.walk_units(client)
        gates = L.save_code_gates(client)

    # A re-press reads back what the first press wrote, so the memory MERGES
    # rather than replaces -- see `L.merge_saved`. The code gates are kept
    # outright for the same reason: a second `save_code_gates` returns the stub.
    _ISOLATED["units"] = L.merge_saved(_ISOLATED["units"], walk.units)
    if not _ISOLATED["gates"] and walk.found:
        _ISOLATED["gates"] = gates

    # A null head is indistinguishable from NOT BEING IN A BATTLE, and decision
    # 13 rules that case *found nothing, wrote nothing* -- so the code gates
    # are held back too, not just the unit writes. They are fixed addresses in
    # BATTLE.BIN rather than links off a walk, but poking an overlay that is
    # not loaded is the same mistake wearing a constant.
    changed = client.write(L.plan_hide_units(walk.units)
                           + L.plan_hide_code(_ISOLATED["gates"]))
    lines = [L.isolate_report(walk, len(walk.units), changed)]
    if not walk.found:
        return lines
    # The two gates whose target is not certain, said once per press, in the
    # console -- not as a standing line in the panel, which is the sidebar's
    # rule. The artist's eye is the acceptance and this is what it looks for.
    lines.append("the vitals HUD and the tile cursor are stubbed at their "
                 f"renderers (0x{L.HUD_RENDERER:08X}, "
                 f"0x{L.CURSOR_RENDERER:08X}) -- if the knife is still there, "
                 f"or still there but not bobbing, the cursor's target is the "
                 f"other candidate (0x{L.CURSOR_RENDERER_FALLBACK:08X})")
    # The third gate is the one the artist FEELS rather than sees: with it cut,
    # a pushed camera stays where it was put instead of drifting home over
    # about a second. Restore puts the leash back with everything else.
    lines.append(f"the camera leash is cut (0x{L.CAMERA_LEASH:08X}) -- the "
                 "battle's per-frame spring is what fights a pushed camera, "
                 "and while the map is isolated a camera push HOLDS")
    lines.append(f"boxed dialogue is stubbed at its renderer "
                 f"(0x{L.DIALOGUE_BOX_RENDERER:08X}) -- the frame, the text "
                 f"and the speaker portrait are one draw, and all three go")
    return lines


def restore_map(client):
    """Put back exactly what was saved. Returns lines."""
    units, gates = _ISOLATED["units"], _ISOLATED["gates"]
    if not units and not gates:
        return ["nothing to restore -- no isolate is holding any saved values. "
                "If the battle is still hidden, Blender was restarted while it "
                "was isolated and the way back is to reload the battle"]
    changed = client.write(L.plan_restore_units(units)
                           + L.plan_restore_code(gates))
    _ISOLATED["units"], _ISOLATED["gates"] = [], []
    return [f"restored {len(units)} units and {len(gates)} code gates "
            f"({changed} bytes changed) -- from the values saved before the "
            f"isolate, NOT from a constant, so a unit the battle had already "
            f"hidden stays hidden"]


def _isolate_client(context):
    prefs = _prefs(context)
    host = getattr(prefs, "live_host", "") or L.DEFAULT_HOST
    port = int(getattr(prefs, "live_port", 0) or L.DEFAULT_PORT)
    return L.RamClient(host=host, port=port)


class MAP_OT_live_isolate(Operator):
    """Hide everything in the running battle that is not the map."""

    bl_idname = "map.live_isolate"
    bl_label = "Isolate map"
    bl_description = ("Hide the units, their ground shadows, the vitals HUD "
                      "and the tile cursor in the running battle, and cut the "
                      "camera leash so a pushed camera holds, and take boxed "
                      "dialogue off too, so the map is the only thing on "
                      "screen. Re-pressable: press it again after restarting "
                      "the emulator or replacing the map")
    bl_options = {"REGISTER"}

    def execute(self, context):
        try:
            lines = isolate_map(_isolate_client(context))
        except L.LiveLinkError as e:
            self.report({"ERROR"}, str(e))
            lines, status = ["REFUSE: " + str(e)], "CANCELLED"
        else:
            self.report({"INFO"}, lines[0])
            status = "FINISHED"
        from .report_log import record
        record("Isolate map", "", lines)
        for line in lines:
            print(f"EXMATERIA-MAP isolate: {line}")
        return {status}


class MAP_OT_live_restore(Operator):
    """Put back everything the isolate hid."""

    bl_idname = "map.live_restore"
    bl_label = "Restore"
    bl_description = ("Put the units, shadows, HUD and cursor back the way "
                      "they were before the isolate -- from the values saved "
                      "at the time, so a unit the battle had already hidden "
                      "stays hidden")
    bl_options = {"REGISTER"}

    def execute(self, context):
        try:
            lines = restore_map(_isolate_client(context))
        except L.LiveLinkError as e:
            self.report({"ERROR"}, str(e))
            lines, status = ["REFUSE: " + str(e)], "CANCELLED"
        else:
            self.report({"INFO"}, lines[0])
            status = "FINISHED"
        from .report_log import record
        record("Restore", "", lines)
        for line in lines:
            print(f"EXMATERIA-MAP isolate: {line}")
        return {status}


class MAP_PT_live_isolate(Panel):
    """`Map` sidebar, under the camera: take everything but the map away.

    **`bl_order` 2**, with Push (0) and Camera (1), on the reason `_HOMES`
    already carries for Camera: both are the live link and the artist presses
    them in one breath. Aim the camera, hide the units, look at the map.

    Not inside the Camera panel: that panel's docstring defends **four controls
    and no prose** as this sidebar's rule, and "Camera" stops describing it the
    moment it hides units.

    Two buttons and not a checkbox. Re-ticking an already-ticked box is a
    no-op, and re-pressability is the whole mechanism -- a restarted emulator,
    a replaced map or a unit spawning mid-battle is answered by pressing
    Isolate again.
    """

    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Map"
    bl_label = "Isolate"
    bl_order = 2

    def draw(self, context):
        layout = self.layout
        layout.operator(MAP_OT_live_isolate.bl_idname, icon="HIDE_ON",
                        text="Isolate map")
        layout.operator(MAP_OT_live_restore.bl_idname, icon="HIDE_OFF",
                        text="Restore")


def register():
    bpy.utils.register_class(MAP_OT_live_push)
    bpy.utils.register_class(MAP_PT_live_push)
    bpy.utils.register_class(MAP_OT_live_camera_match)
    bpy.utils.register_class(MAP_PT_live_camera)
    bpy.utils.register_class(MAP_OT_live_isolate)
    bpy.utils.register_class(MAP_OT_live_restore)
    bpy.utils.register_class(MAP_PT_live_isolate)
    # `persistent`, because opening a .blend must not silently stop the sync --
    # the artist would read that as the feature breaking on their own file.
    if not bpy.app.timers.is_registered(_camera_sync_timer):
        bpy.app.timers.register(_camera_sync_timer,
                                first_interval=L.CAMERA_SYNC_BACKOFF,
                                persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_camera_sync_timer):
        bpy.app.timers.unregister(_camera_sync_timer)
    _CAMERA_TICKER.reset()
    bpy.utils.unregister_class(MAP_PT_live_isolate)
    bpy.utils.unregister_class(MAP_OT_live_restore)
    bpy.utils.unregister_class(MAP_OT_live_isolate)
    bpy.utils.unregister_class(MAP_PT_live_camera)
    bpy.utils.unregister_class(MAP_OT_live_camera_match)
    bpy.utils.unregister_class(MAP_PT_live_push)
    bpy.utils.unregister_class(MAP_OT_live_push)


classes = (MAP_OT_live_push, MAP_PT_live_push,
           MAP_OT_live_camera_match, MAP_PT_live_camera,
           MAP_OT_live_isolate, MAP_OT_live_restore, MAP_PT_live_isolate)
