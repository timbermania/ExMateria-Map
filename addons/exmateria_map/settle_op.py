"""The settle: the loop closes itself -- ADR-0186 Amendment 7, decisions 28-30.

    1. paint    2. click Recalculate/Re-select    3. click Push    4. see it

Steps 2 and 3 are what this module deletes.  An artist paints, stops, and the
map is in the emulator without a gesture in between.  Both buttons stay; they
are the manual override rather than the path.

**How it knows painting stopped.**  Amendment 7 named `wm.operators` as the
intended witness and left it open.  Measured in Blender 5.2.0 LTS it is the
wrong one -- it is not populated by `bpy.ops` calls at all, so a probe cannot
even establish a baseline with it -- and the other two candidates fail for
their own reasons: `bpy.msgbus` does not fire on a paint stroke, and 5.2
exposes no undo-stack depth to Python (`ed.undo_history`'s `item` is an
`IntProperty`, not an enum).  What works is the canvas's own content.  A
digest of the painting is stable when untouched, moves on a SINGLE texel, and
holds still the moment the artist stops.  Measured on a 256x1024 sheet:
`foreach_get` plus a `crc32` is **1.1 ms**, well under 1% of one core at 4 Hz
(and the same rig measures Amendment 9's `blake2b` at 5.4 ms, which is the
provenance for trusting the rest of these).  The scale that decided the hash is not that one -- at Amendment 10's
N = 4 the same poll is 18.7 ms end to end, where a `blake2b` was 89 ms and
36% of a core.  See `canvas_digest`.

**Why it is not an operator** (decision 29).  The compile runs as a plain
function from `bpy.app.timers`, so it pushes no undo step and Ctrl+Z keeps
taking back brush strokes.  Automating the write-back *as an operator* would
fill the undo stack with compiles within a minute of painting, and that -- not
the write-back itself -- is the thing that would have made this unusable.
`MAP_OT_live_push` is `{"REGISTER"}` and not `{"UNDO"}`, so calling it from
here costs no undo step either.

**Why it holds off in Edit Mode.**  `readable_mesh` reads a mesh by toggling
the artist out of Edit Mode and back.  That is right for a button the artist
just pressed and wrong for a timer: it would yank the mode out from under a
gesture nobody asked it to interrupt.  A settle skipped that way is not lost --
the canvas still differs from what was compiled, so the first tick after Edit
Mode ends catches it.  Decision 29's self-healing argument, applied to a
second case.
"""
import array
import json
import threading
import time
import traceback
import zlib

import bpy

from .compile_op import (_subject_of, animated_rows_of, animation_note_once,
                         compile_off_thread, land_compile, read_for_compile,
                         stamp_compile)
from . import live_link_ui
from .export_document import image_rgb
from .import_document import _prefs, editing_units, marker_in_scene
from .settle_clock import QUIET_DEFAULT, SettleClock
from .worker import spawn

#: How often the canvas is looked at.  Four times a second: fast enough that
#: the quiet interval means what it says, slow enough that the digest is a few
#: per cent of one core -- 7% at Amendment 10's N = 4, which is what decision
#: 42 bought and why the scale did not have to move this number.
TICK = 0.25

#: One clock per map, by object name -- a scene may hold more than one.
_CLOCKS = {}

#: One LIGHTING clock per map, beside `_CLOCKS` and sharing its quiet period
#: -- ADR-0186 Amendment 14 decision 59.
#:
#: A SECOND INSTANCE rather than a fourth witness folded into the canvas
#: digest, and the reason is decision 58: a lighting change must not COMPILE.
#: One composite digest would fire correctly and then lose *which* witness
#: moved, and the only way out of `_step` was `_launch` -- so every lamp nudge
#: would silently take the full compile path, quantising colours and
#: downsampling for a change that moved no texel.  Two clocks keep the answer
#: to "what moved" in the shape of the question.
_LIGHTING = {}

#: What a worker finished, waiting for the main thread to land it.  Written
#: from the thread and read from the timer; a single slot, because the clock
#: never lets a second compile start while one is in flight.
_RESULT = []

#: When to try pushing again, and whether the last failure has been said.
#: Amendment 7's rule is "check once, go quiet, stop trying" and the first
#: build read that as a LATCH -- one failure disabled the automatic push for
#: the rest of the session.  That is wrong for the ordinary workflow: an
#: artist opens Blender, converts and paints before starting the emulator, so
#: the very first settle fails and nothing ever pushes again.  Going quiet is
#: about not NAGGING and not stalling the timer, so it is a back-off: retried
#: on a slow clock, and reported once per spell rather than once per settle.
PUSH_RETRY = 30.0
_PUSH = {"quiet_until": 0.0, "said": None, "why": "settle"}

_BUF = [None]


def resume_pushing():
    """The artist pushed by hand, so there may be an emulator again."""
    _PUSH["quiet_until"] = 0.0
    _PUSH["said"] = None


def _quiet(context):
    prefs = _prefs(context)
    return float(getattr(prefs, "settle_quiet", QUIET_DEFAULT) or QUIET_DEFAULT)


def _enabled(context):
    return bool(getattr(_prefs(context), "settle_on", True))


def _auto_push(context):
    """Does a compile push afterwards?  Governs the settle AND both buttons.

    One switch rather than two, because "should this reach the emulator by
    itself" is one question however the compile was started.
    """
    return bool(getattr(_prefs(context), "auto_push", True))


def canvas_digest(img):
    """A digest of what the artist has painted -- the whole witness.

    `foreach_get` into a reused buffer and a `crc32` over it.  Indexing
    `img.pixels` in Python instead measured **212 ms**, which is why the
    buffer is not optional.

    **`zlib.crc32`, not `blake2b`** -- ADR-0186 Amendment 10 decision 42.
    Amendment 9 sized the poll at 5.4 ms on a 256x1024 sheet, "about 2 % of
    one core at 4 Hz".  Amendment 10 adds a scale N, and at N = 4 the same
    buffer is 67 MB and the same poll is 85.8 ms -- 34 % of one core,
    permanently, while the artist paints.  `zlib.crc32` is 14.2 ms for it,
    needs no change to `TICK` or to the quiet interval, and zlib's is
    hardware-accelerated, which is what makes this a one-line change rather
    than a re-think of the clock.

    It is also the better witness rather than merely the cheaper one.
    Amendment 9's requirement is that the digest move on a **single texel**;
    a one-channel nudge is a change inside one 32-bit float, a burst of at
    most 32 bits, and CRC-32 detects every burst that short by construction
    where blake2b makes it only overwhelmingly likely.  A collision costs
    nothing here -- one settle does not fire and the next stroke fires it --
    because this witness is in-memory, never persisted, and never compared
    across sessions.

    Scoped to this function alone.  The two hashes that carry weight are
    untouched: `compile_op.stamp_compile`'s sha256, which is persisted on the
    marker and is what `_VERIFIED` refuses to rubber-stamp, and
    `export_source_art`'s sha256, which names sidecars and dedupes paintings
    across states.  Those are content IDENTITY; this is a change DETECTOR.

    Graded by `tests/blender_settle_witness.py`, which asserts the cost
    relative to blake2b on the same buffer rather than in milliseconds.
    """
    n = img.size[0] * img.size[1] * img.channels
    buf = _BUF[0]
    if buf is None or len(buf) != n:
        buf = _BUF[0] = array.array("f", [0.0]) * n
    img.pixels.foreach_get(buf)
    return format(zlib.crc32(buf), "08x")


def _clock(name, quiet):
    clock = _CLOCKS.get(name)
    if clock is None:
        clock = _CLOCKS[name] = SettleClock(quiet=quiet)
    clock.quiet = quiet
    return clock


def lighting_digest(scene, ob):
    """The three lighting witnesses of Amendment 14 decision 59, as one value.

    Composite here, and deliberately NOT with the canvas digest.  All three of
    these reach the same act -- `push_after_compile(ob, "lighting")` -- so
    hashing them together loses nothing; folding them in with the canvas would
    lose the one distinction decision 58 turns on, and lose it silently.

    **A -- `lighting_bake.lamp_signature`, and only while a map holds Lamp
    authority** (decision 60).  With authority off no lamp moves a normal:
    `_live_handler` returns before it even computes a signature.  Arming anyway
    would spend an `assemble` on every lamp nudge to report that there was
    nothing to send, which the panel already says as *"not in charge"*.  The
    authority holder is found the way `_live_handler` finds it -- the first map
    in the scene holding it -- so the two cannot disagree about who is in
    charge.  Scoped to A alone: an Override move involves no lamp, and pushes
    with authority off.

    Switching authority ON moves this, and correctly -- `_authority_update`
    re-solves on the ON edge, so there really are new normals to send.
    Switching it OFF moves it too, and that one push carries bytes nobody
    changed.  A deliberate toggle costing one redundant push is not worth a
    second piece of state to keep true.

    **B -- every rig Override, in `editing_units`.**  The same read-back
    `rig_is_dirty` compares in, so a float32 round trip cannot read as a
    change.  Not scoped to the previewed state: the panel lets an artist edit
    any state's rig, and all of them export.

    **C -- the previewed state**, which is a PUSH trigger and never a bake
    (decision 61).  `lamp_signature` excludes it for exactly the opposite
    reason: a state switch that re-SOLVES re-shades every state from one view
    change, and `blender_roundtrip`'s `light_baked_borrowed` / `state2` /
    `back_to_default` / `borrow_keyed` all went red the day someone conflated
    the two.  Pushing is not solving, and nothing this function feeds can bake.
    """
    from . import lighting_bake
    holder = next((cand for cand in scene.objects
                   if lighting_bake._is_map(cand)
                   and getattr(cand, "exmateria_map_lamp_authority", False)),
                  None)
    lamps = "" if holder is None else lighting_bake.lamp_signature(scene, holder)
    return json.dumps([
        lamps,
        [editing_units(ov)
         for ov in getattr(ob, "exmateria_map_rig_overrides", ())],
        ob.get("exmateria_map/preview_state"),
    ])


def _lighting_clock(name, quiet, digest):
    """The map's lighting clock, PRIMED from what the scene already holds.

    The priming is not a nicety.  A fresh `SettleClock` treats its first
    observation as a change -- that is what makes it a change detector -- so an
    unprimed one fires a quiet period after the map first comes into view, and
    an artist would get an unasked-for push on every import and every file
    reload.  Measured: `blender_lighting_push.py` arm 0 is red without this
    line and green with it.

    `compiled()` is how the canvas clock declares a starting point too (a map
    that has just been converted is compiled from what it holds).  This says
    the same thing about the lighting: what is in the scene when the map
    arrives is the baseline, not an edit.
    """
    clock = _LIGHTING.get(name)
    if clock is None:
        clock = _LIGHTING[name] = SettleClock(quiet=quiet)
        clock.compiled(digest)
    clock.quiet = quiet
    return clock


def _launch(ob, state, sheet, painting, idx, rows, digest):
    """Read on the main thread, compile off it (decision 30).

    Names rather than datablock references cross the thread boundary: a
    pointer held across seconds of another thread's work is a pointer Blender
    is free to move.

    The animated rows are read HERE, with everything else the compile takes off
    Blender, because decision 49's bound is the settle's problem before it is a
    button's: painting alone re-binds on this path, and the artist's report --
    *"I choose a color, and then paint, ... a bunch of polygons would turn blue
    and shimmer like water"* -- names no button at all.
    """
    polygons, floats = read_for_compile(ob, painting)
    animated = animated_rows_of(ob, state)

    def work():
        try:
            off = compile_off_thread(polygons, floats, rows, animated)
        except Exception:                                 # noqa: BLE001
            traceback.print_exc()
            _RESULT.append(None)
            return
        _RESULT.append((ob.name, state, sheet, painting.name, idx.name,
                        polygons, off, digest))

    # `worker.spawn`, never a bare `Thread`: a compile thread that does not
    # protect the UI's share of the GIL drops Blender to 8.7 fps for its whole
    # length, which is the freeze this settle was built to remove.
    spawn(f"exmateria-settle:{ob.name}", work)


def _land(clock):
    """MAIN THREAD -- put the finished compile into the scene, then push."""
    done = _RESULT.pop(0)
    if done is None:
        clock.abandoned()
        return
    name, state, sheet, painting_name, idx_name, polygons, off, digest = done
    ob = bpy.data.objects.get(name)
    painting = bpy.data.images.get(painting_name)
    idx = bpy.data.images.get(idx_name)
    if ob is None or painting is None or idx is None:
        clock.abandoned()                     # the map went away mid-compile
        return
    land_compile(ob, state, sheet, painting, idx, off.master, polygons,
                 off.chosen, off.compiled, off.key)
    clock.compiled(digest)
    print(f"EXMATERIA-MAP settle: compiled {sheet} -- {len(off.atoms)} charts, "
          f"error {off.before_mean:.2f} -> {off.compiled.error:.2f}, "
          + ("binding unmoved" if off.chosen.is_incumbent else "re-bound"))
    note = animation_note_once((ob.name, state), polygons, off.atoms,
                               off.animated)
    if note:
        print(f"EXMATERIA-MAP settle: {note}")
    _push(ob)


def push_after_compile(ob, why):
    """Start the push, and say what happened.  Shared by the settle and both
    buttons.

    **It no longer blocks.**  Reported from use: *"when I am painting, I will
    let go and stop, and then in a bit it will randomly freeze for a bit before
    starting again."*  That freeze was this line calling `bpy.ops.map.live_push`
    on the main thread and waiting out the whole round trip.  Measured on
    MAP022 a0, this box: the transport is about **670 ms** of it -- 16
    whole-RAM GETs at 31 ms each plus 5 whole-VRAM GETs at 34 ms -- and none of
    that time is work, it is waiting on another process.  So the push is split
    the same way the compile already is (decision 30): the `bpy` half runs
    here, the transport runs on a worker, and `_drain_push` lands the report on
    the next tick.  What is left on the main thread is `assemble`, 375 ms, and
    it cannot leave -- it IS the Blender read.

    Three return values, and the middle one is new: `{"FINISHED"}`/
    `{"CANCELLED"}` when the answer is already known (the emulator check and
    `assemble`'s refusals both happen on this thread, so "there is no
    emulator" is still an immediate no), `{"RUNNING_MODAL"}` once a worker has
    it, and `None` when there was nothing to do.  Nothing reads the middle one
    as success, which is the property that matters -- see below.

    The other half of decision 28, and the half that shipped broken.  Three
    things were wrong with the first build and all three made it SILENT, which
    is why "the auto push doesn't seem to be working" was the only symptom
    available:

    * a `RuntimeError` from `bpy.ops` was swallowed with a bare `pass` and no
      print, so a refused push and a successful one looked identical;
    * the outcome was judged from `ob["exmateria_map/last_push"]`, which the
      operator only rewrites on the paths that reach `finish` -- so a push that
      failed earlier than that was judged on the PREVIOUS push's message;
    * one failure disabled the automatic push for the rest of the session.

    It now judges the push by the report lines the push itself produced, says
    what happened, and backs off rather than latching.
    """
    if not _auto_push(bpy.context):
        return None
    if time.monotonic() < _PUSH["quiet_until"]:
        return None
    _PUSH["why"] = why
    if live_link_ui.background_push_start(bpy.context, ob):
        # Coalesced: a push is already in flight and this document will be
        # sent the moment it lands.  Queueing instead would send the emulator
        # a sheet the artist has already painted over.
        return None
    # The gather refuses on THIS thread -- no emulator, or an `assemble`
    # refusal -- so those answers are still immediate rather than a tick away.
    landed = live_link_ui.background_push_land()
    if landed is None:
        return {"RUNNING_MODAL"}
    return _judge(*landed)


def _judge(ob_name, status, lines, pending=False):
    """What the automatic push does about an outcome.  MAIN THREAD."""
    ob = bpy.data.objects.get(ob_name)
    if status == "FINISHED":
        _PUSH["quiet_until"], _PUSH["said"] = 0.0, None
        if pending and ob is not None:
            # Painting carried on while that push was in flight, so the sheet
            # it sent is already stale.  Send the current one.
            push_after_compile(ob, _PUSH["why"])
        return {status}

    # The push prints its own reasons to the terminal and puts them in the
    # Log; this says only what the AUTOMATIC push is now doing about it, and
    # says it once per spell rather than once per settle.
    reason = "; ".join(lines)[:200]
    _PUSH["quiet_until"] = time.monotonic() + PUSH_RETRY
    if _PUSH["said"] != reason:
        _PUSH["said"] = reason
        print(f"EXMATERIA-MAP {_PUSH['why']}: the automatic push was refused, "
              f"so it will try again in {PUSH_RETRY:.0f}s. The compile keeps "
              f"running, and Push to PCSX still works by hand.")
    return {status}


def _drain_push():
    """MAIN THREAD -- land a finished background push.  Called every tick.

    The push gets no timer of its own: this one already runs at 4 Hz, which is
    a quarter of a second of latency on a report the artist reads in the
    terminal.  A second timer would be a second thing to unregister.
    """
    landed = live_link_ui.background_push_land()
    if landed is not None:
        _judge(*landed)


def _push(ob):
    return push_after_compile(ob, "settle")


def _lighting_only(ob, lighting, lit):
    """A lighting change with no compile behind it -- Amendment 14 decision 58.

    True if it sent one.  **It does not compile**: the compile is colour
    quantisation and the downsample from full resolution, and a lighting change
    moves no texel and no `palette_id`, so none of it applies.  It routes
    through `push_after_compile` rather than a second transport because there
    is nothing to add -- all 39 rig bytes and the mesh `normals` already ride
    that push, which is the amendment's whole finding.

    Marked sent IMMEDIATELY, unlike a compile: there is no landing to wait for,
    and a clock left in flight never fires again.  What was observed is what
    the push was handed, so that is what the clock has now seen out.
    """
    if lit is None:
        return False
    lighting.compiled(lit)
    push_after_compile(ob, "lighting")
    return True


def _step():
    context = bpy.context
    if not _enabled(context):
        return
    ob = marker_in_scene(context)
    if ob is None or "exmateria_map/base" not in ob:
        return
    quiet = _quiet(context)
    clock = _clock(ob.name, quiet)
    if _RESULT:
        _land(clock)
        return
    # See the module docstring: a timer may not take the artist out of Edit
    # Mode, and the next tick after they leave catches whatever it skipped.
    # Ahead of the subject lookup now, because it governs the LIGHTING push
    # too: that push assembles the document, which reads the mesh, and the
    # read is what may not yank the mode out from under a gesture.
    if getattr(ob, "mode", "OBJECT") == "EDIT":
        return
    # The lighting witnesses are read BEFORE the subject and do not depend on
    # one.  A map that has never been converted has no painting and so no
    # subject, and an artist who imports one and moves a lamp is owed the same
    # emulator picture as one who paints -- *"keep pcsx redux updated with
    # whatever the current config is"* names no act at all.
    lit_digest = lighting_digest(context.scene, ob)
    lighting = _lighting_clock(ob.name, quiet, lit_digest)
    lit = lighting.observe(time.monotonic(), lit_digest)
    found = _subject_of(ob)
    if isinstance(found, str):
        _lighting_only(ob, lighting, lit)     # nothing to compile; not an error
        return
    ob_, state, sheet, painting, idx, rows = found
    # Stage ONE of the tick (ADR-0186 Amendment 10 decision 39): a stroke made
    # on the native canvas becomes part of the master BEFORE anything reads the
    # master.  Ahead of `canvas_digest` on purpose -- the write-through is what
    # makes the digest move, so a native stroke settles and compiles on exactly
    # the same clock as a stroke made at full resolution.  A no-op at N = 1,
    # and a no-op when no native canvas exists, which is most maps.
    from .convert_op import write_through_canvas
    write_through_canvas(ob_, sheet, painting)
    digest = canvas_digest(painting)
    if clock.observe(time.monotonic(), digest) is None:
        if not _lighting_only(ob_, lighting, lit):
            _clear_a_stale_bit_nothing_else_can(ob_, sheet, painting, clock,
                                                digest)
        return
    # Both clocks settled on one tick: a single gesture that painted AND moved
    # a light.  The compile's own push carries the rig, so the lighting is
    # marked sent and `_launch` alone runs.  Two pushes for one gesture is the
    # easy mistake here, and it is silent -- the emulator would look right.
    if lit is not None:
        lighting.compiled(lit)
    _launch(ob_, state, sheet, painting, idx, rows, digest)


def _clear_a_stale_bit_nothing_else_can(ob, sheet, painting, clock, digest):
    """Paint that landed back on the colour it replaced is not a stale sheet.

    `freshness()` reads `Image.is_dirty`, which flips on any write and never
    flips back on its own -- an undone stroke, or a stroke in the colour that
    was already there, sets it while changing nothing.  Before the settle, the
    artist cleared that by pressing a button.  The settle correctly declines to
    recompile a canvas whose CONTENT is what it already compiled, so without
    this the panel would sit on `stale` for a sheet that is current and
    nothing would ever clear it.

    Costs one `image_rgb` and one sha256, once, because packing clears the bit
    that brought us here.
    """
    if not painting.is_dirty or digest != clock.last_compiled:
        return
    stamp_compile(ob, sheet, painting, image_rgb(painting))


def _tick():
    # A timer that raises is UNREGISTERED, silently, and the loop stops
    # closing itself with nothing said. Everything is caught and printed.
    try:
        _drain_push()
        _step()
    except Exception:                                     # noqa: BLE001
        traceback.print_exc()
    return TICK


def register():
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=TICK, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    _CLOCKS.clear()
    _LIGHTING.clear()
    _RESULT.clear()
    _BUF[0] = None
