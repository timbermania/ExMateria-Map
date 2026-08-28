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

import bpy
from bpy.props import BoolProperty
from bpy.types import Operator, Panel

from . import live_link as L
from . import live_vram as VR
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


class MAP_OT_live_push(Operator):
    """Push the scene into a running PCSX-Redux battle. No file is written."""

    bl_idname = "map.live_push"
    bl_label = "Push to PCSX"
    bl_description = ("Push this map into a running PCSX-Redux battle over the "
                      "Lua web server. Edits the picture the game is rendering "
                      "-- it does not touch the ISO and does not survive a map "
                      "reload")
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
        from .authoring import suspended
        lines = []

        def say(kind, text, keep=True):
            self.report({kind}, text)
            if keep:
                lines.append(("REFUSE: " if kind == "ERROR" else "") + text)

        def finish(status, ob=None):
            if ob is not None:
                ob[LAST_PUSH_KEY] = json.dumps(lines)
            # Every finish, refusals included -- a push that refused is the one
            # the artist most wants to read.
            from .report_log import record
            record("Push to PCSX-Redux", ob.name if ob is not None else "",
                   lines)
            # ...and to the TERMINAL.  The panel used to draw a status row and
            # the refusals; the artist's rule is that a run's output is console
            # output, so this is where the deleted rows went.  Printed from the
            # OPERATOR, once per push -- never from a `draw`, which runs on
            # every redraw of the region.  `record` above keeps the selectable
            # copy in the Log; this is the one a terminal `tail -f` sees, and
            # between them the deleted panel text has two homes rather than
            # none.
            for line in lines:
                print(f"EXMATERIA-MAP push: {line}")
            return {status}

        ob, problem = find_marker(context)
        if problem:
            self.report({"ERROR"}, problem)
            return {"CANCELLED"}

        # The cheapest failure first: an emulator that is not there costs two
        # seconds to find out about and the assemble below costs more.
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
            return finish("CANCELLED", ob)

        # 1. the document, in memory. Export's own refusals, not a second copy.
        with suspended():                     # §6.1, as on the import side
            doc, _files, rep = assemble(ob)
        for w in rep.warnings:
            say("WARNING", w)
        if rep.refusals:
            say("ERROR", f"{len(rep.refusals)} refusal(s), nothing pushed: "
                         + "; ".join(rep.refusals[:12])
                         + (" ..." if len(rep.refusals) > 12 else ""))
            return finish("CANCELLED", ob)

        # 2. the gate
        try:
            descriptors = L.check_descriptors(L.read_descriptor_block(client))
        except L.LiveLinkError as e:
            say("ERROR", f"gate: {e}")
            return finish("CANCELLED", ob)
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
            return finish("CANCELLED", ob)
        try:
            for w in L.check_capacity(descriptors, counts):
                say("WARNING", w)
            L.check_followers(descriptors, counts)
        except L.LiveLinkError as e:
            say("ERROR", f"{e} -- nothing was pushed")
            return finish("CANCELLED", ob)

        # 3b. the plan, before the self-check, so a document that cannot be
        # planned is refused before the artist waits on six round trips.
        try:
            plans = L.plan_document(primary, doc)
        except L.LiveLinkError as e:
            say("ERROR", f"{e} -- nothing was pushed")
            return finish("CANCELLED", ob)

        # 4. the base map, for the self-check AND for reading the push's own
        # answer back. It costs no round trip.
        #
        # Its length is the IMPORTED list's, never the live descriptor's
        # counts. After a shrink the descriptor carries the SHRUNK counts, so a
        # base sized off them would check and compare the wrong slot range on
        # the second press of the same session.
        base = base_polygons(ob)
        base_plans = None
        base_counts = counts
        if base is not None:
            base_counts = L.bucket_counts(base)
            base_plans = L.plan_document(primary, {"polygons": base})
        if self.replace_loaded_map:
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
                return finish("CANCELLED", ob)
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
        elif not self.skip_selfcheck:
            ok, said = selfcheck(client, base_plans, _LAST_PUSH.get(ob.name))
            if not ok:
                for line in said:
                    say("ERROR", line)
                return finish("CANCELLED", ob)
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
            return finish("CANCELLED", ob)

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
            return finish("CANCELLED", ob)
        total += count_total
        _LAST_PUSH[ob.name] = plans

        # 5b. the light rig, decision 9's other atom -- and it is TWO
        # transports, not one. Only the DIRECTION matrix is re-composed from
        # RAM every frame; the gains and the ambient are loaded into GTE
        # control registers at map load and were not seen to re-load, so a
        # RAM-only push would put this state's angles over the last-loaded
        # state's brightness (§2.2). Both halves or neither.
        states = doc.get("map_states") or []
        index = int(ob.get("exmateria_map/preview_state") or 0)
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
                if self.replace_loaded_map:
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
        lines.extend(unpushed_lines({f for _, f in plans}
                                    | {k[1] for k in packet_plans}))
        lines.append("a picture, not a disc: a map reload uploads the disc's "
                     "bytes back over all of it -- `build` is what ships")
        print(f"EXMATERIA-MAP: pushed {total:,} byte(s) to {host}:{port} "
              f"({len(doc['polygons'])} polygons)")
        return finish("FINISHED", ob)


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


def register():
    bpy.utils.register_class(MAP_OT_live_push)
    bpy.utils.register_class(MAP_PT_live_push)


def unregister():
    bpy.utils.unregister_class(MAP_PT_live_push)
    bpy.utils.unregister_class(MAP_OT_live_push)


classes = (MAP_OT_live_push, MAP_PT_live_push)
