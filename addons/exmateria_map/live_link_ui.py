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
from .export_document import (assemble, describe_divergence, find_marker,
                              markers)
from .import_document import (_prefs, _stored_report, marker_in_scene,
                              state_rig)

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
    """Demand that RAM holds the disc's bytes, or this session's last push.

    Returns a one-line description of which of the two it found. Raises
    `LiveLinkError` -- naming every cause, because a mismatch here has three
    and the first version of this check in the core named only one -- when
    neither candidate matches.

    `prev_plans` is tried first when it exists: after a push it is what RAM
    holds, so checking it first costs one read per bucket instead of two.
    """
    found = set()
    for key, writes in sorted(base_plans.items()):
        prev = (prev_plans or {}).get(key)
        differ, total = 0, 0
        for what, candidate in (("this session's last push", prev),
                                ("the base map's own bytes", writes)):
            if candidate is None:
                continue
            differ, total = L.verify(client, candidate)
            if differ == 0:
                found.add(what)
                break
        else:
            raise L.LiveLinkError(
                f"write-path self-check FAILED on {key[0]} {key[1]}: "
                f"{differ:,} of {total:,} byte(s) at the planned addresses "
                "hold neither the imported document's own geometry nor "
                "anything this session pushed. Either the loaded map is not "
                "this document's map, or something else pushed to this "
                "emulator (reload the savestate), or this rig's address "
                "arithmetic is wrong. Nothing was written.")
    return " and ".join(sorted(found)) if found else "nothing to check"


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
        client = L.LuaClient(host=host, port=port)
        if not client.ping():
            say("ERROR", f"no emulator answering on {host}:{port} -- launch "
                         "pcsx-redux with -webserver and load a battle")
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
        if base is None:
            say("WARNING",
                "self-check SKIPPED: this scene has faces that were not "
                "imported (or no `_shadow` attributes), so there is no "
                "base geometry to compare RAM against. The descriptor "
                "gate is the only guard on this push")
        elif not self.skip_selfcheck:
            try:
                held = selfcheck(client, base_plans, _LAST_PUSH.get(ob.name))
            except L.LiveLinkError as e:
                say("ERROR", str(e))
                return finish("CANCELLED", ob)
            say("INFO", f"self-check: the planned addresses hold {held}",
                keep=False)

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
                rig_regs = L.apply_gte(client, L.plan_rig_gte(rig))
            except L.LiveLinkError as e:
                say("WARNING", f"light rig NOT pushed: {e}")
            else:
                rig_reported = rig_lines(states, index, rig_source,
                                         rig_ram, rig_regs)
        elif states:
            say("WARNING",
                "light rig NOT pushed: no state in this arrangement carries "
                "one, so the map renders albedo (46 states corpus-wide)")

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
        lines.extend(unpushed_lines({f for _, f in plans}
                                    | {k[1] for k in packet_plans}))
        lines.append("a picture, not a disc: a map reload uploads the disc's "
                     "bytes back over all of it -- `build` is what ships")
        print(f"EXMATERIA-MAP: pushed {total:,} byte(s) to {host}:{port} "
              f"({len(doc['polygons'])} polygons)")
        return finish("FINISHED", ob)


class MAP_PT_live_push(Panel):
    """N-panel: push the scene into a running PCSX-Redux battle.

    Its own section, next to the operator it drives, rather than a tail on the
    preview panel — reported from use as "the menus are a mess".  It also has
    something to say that no other panel does, and that has to be said WHERE THE
    BUTTON IS: what a push carries, and what it does not.
    """
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"
    bl_category = "Map"
    bl_label = "Push to PCSX-Redux"
    bl_order = 5

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
        prefs = _prefs(context)
        if prefs is not None:
            row = layout.row(align=True)
            row.prop(prefs, "live_host", text="")
            row.prop(prefs, "live_port", text="")

        # Reported from use: "when I change map preview entry and hit push
        # nothing happens - shouldn't it update the texture?"  It cannot, and
        # for two independent reasons, neither of which was anywhere on screen:
        #
        #   1. the previewed state is VIEW state.  `export_document` never reads
        #      `exmateria_map/preview_state`, so the document is byte-identical
        #      whichever state is on screen -- and `apply` reports only CHANGED
        #      bytes, so the second push of an unchanged document is a truthful
        #      zero.
        #   2. the texture sheet and the CLUT have NO LIVE SINK in this module
        #      at all (`live_link.UNPUSHED`): the packet's CLUT and TPAGE fields
        #      are located but not built, and the sheet is pushed only by
        #      `tools/live_push.py`, through a savestate round trip.
        #
        # `UNPUSHED` was already named in the last-push report, which is read
        # AFTER the click and only once there has been one.  A limit the artist
        # has to trigger the disappointment to discover is not documented.
        box = layout.box()
        box.label(text="A push carries GEOMETRY, NORMALS, UVs, "
                       "PALETTE IDs and TEXTURE PAGES.", icon="INFO")
        # Wrapped at 60, not `_stored_report`'s 88: this box is always drawn,
        # where a report is occasional, and at 88 the second line ran past the
        # Properties editor's width in the default layout.
        for line in ("A palette ID is a row INDEX, not the row's COLOURS.",
                     "Not the texture sheet's PIXELS, and not the CLUT rows "
                     "themselves — one atom, no live sink here. "
                     "Use tools/live_push.py.",
                     "Not the light rig — 0x800F5B14 is the DIRECTION "
                     "matrix; the gains and ambient are not located.",
                     "Not yet the preview state — decision 9 has the push "
                     "AIM at it, but the legs it aims are not built."):
            for k, chunk in enumerate(textwrap.wrap(line, 60) or [""]):
                box.label(text=("    " if k else "") + chunk)
        _stored_report(layout, ob, LAST_PUSH_KEY, "Last push:")


def register():
    bpy.utils.register_class(MAP_OT_live_push)
    bpy.utils.register_class(MAP_PT_live_push)


def unregister():
    bpy.utils.unregister_class(MAP_PT_live_push)
    bpy.utils.unregister_class(MAP_OT_live_push)


classes = (MAP_OT_live_push, MAP_PT_live_push)
