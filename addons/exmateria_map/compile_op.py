"""The two buttons the compile has (ADR-0186 Amendment 3 decision 15).

**One compiled truth, two buttons.** *Recalculate palettes* recompiles with
the **row binding** held; *Re-select clusters* moves the binding first and
then recompiles. There are not two compiles -- there is one compile
(`compile_map.compile_sheet`) and one thing that moves its input
(`compile_map.select_binding`).

Neither runs behind the artist's back. Decision 16: the Painting/Compiled
toggle computes nothing and a push ships the Sheet as it stands, so WYSIWYG
holds by construction rather than by synchronisation. Which also means a
**stale** Sheet is a legal map -- decision 13 -- and neither of these buttons
is a gate on anything.

Both are converted-path only. On the direct-paint path the Sheet is the
authored half and `paint.resolve()` is what writes it; there is nothing here
to compile from.
"""
import collections
import hashlib
import json

import bpy
from bpy.types import Operator

from . import compile_map
from .convert_op import (_face_ordered, clut_rows_of, source_art_name,
                         write_clut_rows_of)
from . import resample
from . import crumbs
from .export_document import (image_floats, image_rgb, master_key,
                              readable_mesh, remember_master, rgb_from_floats,
                              set_image_indices)
from .import_document import marker_in_scene
from .paint import (active_palette, index_image, painting_of, section,
                    sheet_of_state)

#: Charts named in the operator's own toast. The Log gets all of them
#: (decision 9 says NAME every chart whose error rose); a toast that scrolled
#: would name none of them legibly.
TOASTED = 3


#: Sheets whose Painting this PROCESS has hashed and found to match the
#: stamp.  Process-lifetime and deliberately not persisted, for
#: `live_link_ui._LAST_PUSH`'s reason: a claim about a comparison nobody in
#: this session performed is a rubber stamp.  It exists so the panel can say
#: **fresh** only where fresh was checked -- see `freshness`.
_VERIFIED = {}

#: Where the compile records which Painting it compiled.  A marker property
#: and NOT a document member: it is a note about a cache, not map data, and
#: `_assemble` builds the document from an explicit list of keys, so nothing
#: here can reach a disc.
STAMP = "compiled_from"


def painting_stamp(ob):
    return section(ob, STAMP, {}) or {}


def stamp_compile(ob, sheet, painting, art):
    """Record which Painting this compile read, and clear `is_dirty`.

    Two signals, because neither is enough alone.  The digest is exact and
    survives a save; `is_dirty` is free to read in a panel `draw` but is
    cleared by the pack a `.blend` save performs, so on its own it would
    report **fresh** on a map that was painted, saved and reopened without
    compiling -- wrong in the direction that matters.
    """
    digest = hashlib.sha256(art).hexdigest()
    stamps = dict(painting_stamp(ob))
    stamps[sheet] = digest
    ob[f"exmateria_map/{STAMP}"] = json.dumps(stamps)
    _VERIFIED[painting.name] = digest
    painting.pack()          # `is_dirty` False until the next brush stroke
    painting.update()
    return digest


def compare_stamp(ob, digests):
    """Digest per sheet vs. the stamp -- one warning line per disagreement.

    Called from `_assemble`, so the export report and the push report both
    carry it. It WARNS: decision 13 makes a stale Sheet a complete, legal map
    that ships and renders, and Amendment 5 adds only that it must not do so
    silently.
    """
    stamps = painting_stamp(ob)
    notes = []
    for sheet, digest in sorted(digests.items()):
        was = stamps.get(sheet)
        img = bpy.data.images.get(source_art_name(sheet))
        if img is not None:
            _VERIFIED[img.name] = digest if was == digest else None
        if was is None:
            notes.append(
                f"{sheet}: this painting has never been compiled, so the "
                f"sheet being shipped is the one Convert baked. Press "
                f"Recalculate palettes to compile what you painted")
        elif was != digest:
            notes.append(
                f"{sheet}: the sheet was compiled from an EARLIER painting "
                f"and is being shipped as it stands -- a stale sheet is still "
                f"a legal map. Press Recalculate palettes to catch it up")
    return notes


def freshness(ob, sheet, painting):
    """`(state, text)` for the panel -- cheap enough for a `draw`.

    Never reads a pixel: `Image.is_dirty` is Blender's own bit and flips on
    any write. The four states are exhaustive and each is honest about what
    was actually checked -- in particular there is no path that says FRESH
    without this process having hashed the picture.
    """
    if painting is None:
        return "none", ""
    if sheet not in painting_stamp(ob):
        return "never", "not compiled yet"
    if painting.is_dirty:
        return "stale", "painted since the last compile"
    digest = painting_stamp(ob).get(sheet)
    if _VERIFIED.get(painting.name) == digest:
        return "fresh", "compiled from this painting"
    # Packed-and-clean, but nothing in THIS process compared the pixels -- a
    # reopened `.blend` looks exactly like this whether or not it was painted
    # before it was saved. Saying `fresh` here is the one wrong answer.
    return "unknown", "not verified since this file opened"


def _subject(context):
    """The map, its active state, and everything the compile reads off them.

    Returns `(ob, state, sheet, painting, index, rows)` or a refusal string.
    """
    ob = marker_in_scene(context)
    if ob is None or "exmateria_map/base" not in ob:
        return "no map in this scene"
    return _subject_of(ob)


def _subject_of(ob):
    """`_subject` from the OBJECT rather than the context.

    The three ways out of Blender (ADR-0186 Amendment 7 decision 25) each hold
    an `ob` and run where `marker_in_scene` would be the wrong question -- an
    export names its subject, it does not look one up.
    """
    state, _ = active_palette(ob)
    sheet = sheet_of_state(ob, state)
    if not sheet:
        return "no texture sheet in this arrangement"
    painting = painting_of(ob, sheet)
    if painting is None:
        return (f"{sheet} has no painting: press Convert first, or paint the "
                f"sheet directly -- the compile has nothing to compile from")
    idx = index_image(ob, sheet)
    if idx is None:
        return f"sheet image for {sheet!r} is not loaded"
    rows = clut_rows_of(ob, state)
    if rows is None:
        return f"no CLUT for state {state}; there is nowhere to write sixteen rows"
    return ob, state, sheet, painting, idx, rows


def _write_binding(me, polygons, binding):
    """`palette_id` per face -- the binding, and it needs no other schema.

    Decision 15: it IS `palette_id`, which already dumps (`dump.py:72`),
    builds (`build.py:130`) and pushes. `_face_ordered` indexes by face, which
    is what makes this a straight write-back.
    """
    pal = me.attributes["palette_id"].data
    moved = 0
    for i, q in enumerate(polygons):
        if "uv" not in q or binding[i] is None:
            continue
        if pal[i].value != binding[i]:
            pal[i].value = binding[i]
            moved += 1
    return moved


def _land(ob, state, idx, compiled):
    """Both sinks, in one place: the sixteen rows and the Sheet.

    The Sheet is a cache (decision 13) and this is the only thing that writes
    it on the converted path, so there is no second copy to fall out of step
    with the palettes it was compiled against.
    """
    write_clut_rows_of(ob, state, compiled.palettes)
    set_image_indices(idx, compiled.indices)


def _report_regressions(ob, title, before, after, compiled, extra=()):
    """Decision 9: rule per map, REPORT per chart.

    A chart's error depends on the pooled bag of the row it is in, so moving
    one back invalidates every other comparison and a per-chart rule has no
    fixed point. But a global mean can improve while one corner of the mesh
    gets visibly worse, so every chart whose error rose is named -- in the
    Log, where a line can be selected with the mouse.
    """
    rose = compile_map.regressions(before, after, compiled.atoms)
    lines = list(extra)
    lines.append(f"{len(compiled.atoms)} charts, {compiled.texels:,} texels")
    if rose:
        lines.append(f"{len(rose)} chart(s) got WORSE (the map improved on "
                     f"average; these did not):")
        for i, was, now, size in rose:
            lines.append(f"  chart {i} ({size} polygon(s)): "
                         f"{was:.2f} -> {now:.2f}")
    else:
        lines.append("no chart got worse")
    from .report_log import record
    record(title, ob.name, lines)
    return rose


#: What one whole compile did.  A tuple rather than a report, because the
#: operator, the exits and (decision 28) the settle each say it differently.
Compiled = collections.namedtuple(
    "Compiled",
    "polygons atoms before before_mean chosen compiled moved animated")


def compile_now(ob, state, sheet, painting, idx, rows):
    """The whole compile -- search, re-bind, fit, land, stamp.

    A PLAIN FUNCTION and not an operator, which is decision 29 and is the
    whole of what keeps the undo stack usable: run as an operator on a timer
    it would fill the undo stack with compiles within a minute of painting,
    and Ctrl+Z would stop taking back brush strokes.  `MAP_OT_reselect_clusters`
    is a caller of this, not the other way round.

    It is the WHOLE compile, not the binding-held half (decision 26).  The
    quantiser's objective is globally coupled -- a row's sixteen colours are
    fitted to the pooled bag of every chart bound to it -- so a fit under a
    binding chosen for the picture that was replaced is fitted to a question
    nobody is asking any more.
    """
    polygons, floats = read_for_compile(ob, painting)
    done = compile_off_thread(polygons, floats, rows,
                              animated_rows_of(ob, state))
    moved = land_compile(ob, state, sheet, painting, idx, done.master, polygons,
                         done.chosen, done.compiled, done.key)
    return Compiled(polygons, done.atoms, done.before, done.before_mean,
                    done.chosen, done.compiled, moved, done.animated)


#: The middle of a compile, with nothing of Blender in it.  `master` is the
#: Painting at its own scale -- carried out because `stamp_compile` hashes it
#: and the worker is where it gets built (Amendment 10 decision 37).
Off = collections.namedtuple(
    "Off", "atoms before before_mean chosen compiled master animated key")


def shrink_to_sheet(master, w, h):
    """The Painting, box-averaged to the one resolution the compile speaks.

    ADR-0186 Amendment 10 decision 37: the shrink runs in FRONT of the
    compile, and `compile_sheet`, `select_binding`, `score_and_palettes`,
    `error` and `regressions` never learn N existed -- they are handed a
    256x1024 true-colour buffer exactly as they are today.
    """
    n = resample.scale_of(w, h)
    if n is None:
        raise ValueError(
            f"the Painting is {w}x{h}, which is not a legal Painting: it must "
            f"be 256k x 1024k for k in {list(resample.SCALES)} "
            f"(ADR-0186 Amendment 10 dec. 43)")
    return resample.shrink(master, w, h, n)


#: `(map dir, resource name) -> the CLUT rows its `0x6c` animates`.  A cache
#: with no invalidation, and it needs none: the extracted disc tree is frozen
#: 1997 data and `build` carries `0x6c` verbatim (schema §8), so the answer for
#: a resource cannot change while Blender is open.  It exists because a settle
#: compiles every few seconds and each read is a sha256 over a resource file.
_ANIMATED = {}


def animated_rows_of(ob, state):
    """The CLUT rows this state's map ANIMATES -- main thread, off the disc.

    Decision 49.  The search must not move a chart on or off an animated row,
    and nothing in the document says which rows those are: schema §8 puts the
    `0x6c` and `0x70` chunks on the *carried from base* side, so the only place
    to read them is the base resource on the extracted disc tree.  That is the
    same place, by the same sha256-pinned reader, that the live push already
    reads them from (`live-link-v1.md` decision 11 part 3) -- a second route to
    these bytes would be a second chance to disagree about what a record is.

    Returns a tuple of row indices; `()` means *read, and this map animates
    nothing*, which is 1,465 of the corpus's 1,575 resources.  **`None` means
    the answer was not available** -- a `.blend` imported from JSON rather than
    from a GNS remembers no tree, and a moved corpus reads none.  The two are
    told apart because the search treats both as unbounded and only one of them
    is a fact about the map: `animation_note` says which happened rather than
    leaving the artist to infer it from a compile that behaved like the old
    one.  Unbounded is deliberately not a refusal -- the animated rows bound a
    search, they are not a precondition for compiling, and a map that would not
    compile because a directory moved is a worse failure than the one this
    fixes.
    """
    from .live_link_ui import base_map_dir
    from . import live_link as L
    map_dir = base_map_dir(ob)
    if map_dir is None:
        return None
    states = section(ob, "map_states", []) or []
    if not isinstance(states, list) or not 0 <= state < len(states):
        return None
    resource = (states[state] or {}).get("resource")
    if not resource:
        return None
    key = (str(map_dir), resource)
    if key not in _ANIMATED:
        try:
            records, _frames, _source = L.base_animation(
                map_dir, {"base": section(ob, "base", {}) or {}}, resource)
        except Exception:                  # noqa: BLE001
            # A tree that cannot be read costs the BOUND, not the compile.
            _ANIMATED[key] = None
        else:
            _ANIMATED[key] = tuple(sorted(set(L.animation_rows(records))))
    return _ANIMATED[key]


def animation_note(polygons, atoms, animated):
    """What the animation held out of the search -- decision 49's report.

    Said on every compile whose search was bounded, because a search whose
    freedom was cut and a search that simply found nothing better look
    identical in the two numbers printed beside them.  Said on every compile
    that could not read the table too, for decision 4's rule: name what was
    skipped rather than dropping it silently.  `None` -- and no line -- only
    when the table was read and the map animates nothing, which is the common
    map and has nothing to report.

    Pure, and the whole of the wording.  The AUTOMATIC callers go through
    `animation_note_once` instead; see it for why the two halves repeat
    differently.
    """
    if animated is None:
        return ("this scene remembers no readable disc tree, so the compile "
                "could not tell which CLUT rows this map ANIMATES and the "
                "search was not bounded by them. On a map with a palette "
                "animation that can move charts onto an animated row, where "
                "they show the cycle instead of their own colours "
                "(decision 49)")
    if not animated:
        return None
    rows = set(animated)
    held = sum(1 for a in atoms if polygons[a[0]]["palette_id"] in rows)
    named = ", ".join(str(r) for r in sorted(rows))
    return (f"CLUT row(s) {named} are ANIMATED on this map: {held} chart(s) "
            f"are held on them and no other chart may move onto them "
            f"(decision 49)")


#: Subjects that have already been told their search was UNBOUNDED, so the
#: automatic path says it once.  Keyed `(object name, state)`; a rename or a
#: reload says it again, which is the harmless direction.
_SAID_UNBOUNDED = set()


def animation_note_once(key, polygons, atoms, animated):
    """`animation_note` for the callers nobody pressed a button to reach.

    The two halves of the note repeat differently, and `tests/blender_convert.py`
    is where that showed up: `ensure_compiled` runs on every settle, every
    export, every push and every bundle, so a line it emits every time is a
    line the artist reads past.

    * The **held** half is an outcome of THIS compile -- which rows were held
      and how many charts sat on them -- so it belongs beside this compile's
      two error numbers every time, exactly like them.
    * The **unbounded** half is a fact about the SCENE.  It says the marker
      resolves no disc tree, which is true of a `.blend` imported from JSON
      from the moment it is opened until someone points it at one.  Repeating
      it on every settle is decision 49's own rejected alternative -- "warn
      and continue", a warning the artist meets constantly and can do nothing
      about -- wearing the log's clothes instead of a toast's.

    So the unbounded half is said ONCE per subject here, and the two buttons
    keep the plain `animation_note`: a press is a question, and a question
    asked twice deserves the answer twice.
    """
    note = animation_note(polygons, atoms, animated)
    if note is None or animated is not None:
        return note
    if key in _SAID_UNBOUNDED:
        return None
    _SAID_UNBOUNDED.add(key)
    return note


def read_for_compile(ob, painting):
    """MAIN THREAD -- everything one compile reads out of Blender.

    Decision 30 sends the seconds between this and `land_compile` to a worker,
    and `bpy` may only be touched from the main thread.  So this returns the
    painting's RAW FLOATS rather than its RGB bytes: `foreach_get` is a C copy
    and the per-pixel walk that turns it into bytes is not, and at a 4x
    Painting that walk is **2.20 s** (0.08 s at 1x, which reproduces the 67 ms
    this docstring used to quote).  Returning bytes here would put those
    seconds on the main thread on every settle, which is a frozen Blender
    while the artist paints.

    Amendment 10 flags this risk and proposes fusing the read and the
    downsample into one pass.  Measured, that is the wrong fix: fusion saves
    the 12.6 MB intermediate, not the walk, and the walk is what costs.  The
    fix is WHICH THREAD, which is the split decision 30 already built.
    """
    # `readable_mesh`, because Edit Mode is where the artist already is: in
    # Edit Mode the attribute arrays read as size 0 while `me.polygons` still
    # reports a count, and this addon has shipped that defect four times.
    with readable_mesh(ob):
        return _face_ordered(ob.data), image_floats(painting)


def compile_off_thread(polygons, floats, rows, animated):
    """The seconds of a compile, and the only part worth a thread.

    Touches no `bpy` -- everything it calls is in the `bpy`-free half
    (ADR-0007 decision 4), which is what makes decision 30 possible and is a
    use nobody wrote that split for.  Do not reach for `bpy` in here to make
    anything easier.

    `animated` is decision 49's bound, and it arrives as plain integers for
    exactly that reason: it is read off the disc by `animated_rows_of` on the
    main thread, like everything else this function is handed.  It has **no
    default**, deliberately: a caller that forgot it would silently run the
    unbounded search, which is the reported defect wearing a green suite, and
    a required argument makes that a `TypeError` in the first harness that
    compiles anything.
    """
    # The atoms are fixed BEFORE the search and carried through.  `charts()`
    # cuts at a `palette_id` change, so re-deriving them after a re-bind gives
    # a different, finer partition and scores something other than what was
    # chosen.
    # The key comes off the buffer HERE, on the worker, so `land_compile` can
    # hand this master to `export_document.image_rgb` and the push that follows
    # a second later does not derive the same 12.6 M texels again on the main
    # thread.  See `export_document.master_key`.
    key = master_key(floats[0])
    master = rgb_from_floats(*floats)
    art = shrink_to_sheet(master, floats[1], floats[2])
    atoms = compile_map.charts(polygons)
    before, before_mean = compile_map.measure(polygons, art, rows, atoms)
    chosen = compile_map.select_binding(polygons, art, atoms,
                                        animated=animated)
    rebound = [dict(q, palette_id=chosen.binding[i])
               if "uv" in q and chosen.binding[i] is not None
               else q for i, q in enumerate(polygons)]
    compiled = compile_map.compile_sheet(rebound, art, atoms, rows)
    return Off(atoms, before, before_mean, chosen, compiled, master, animated,
               key)


def land_compile(ob, state, sheet, painting, idx, master, polygons, chosen,
                 compiled, key=None):
    """MAIN THREAD -- the mesh write-back, both sinks and the stamp.

    `set_image_indices` measured 76 ms, so this and `read_for_compile` are
    together the ~150 ms of a compile that has to be on the main thread.
    """
    if key is not None:
        # Before anything else: the push this settle is about to start reads
        # `image_rgb` on the MAIN thread, and this is the deposit that makes
        # that a 25 ms key lookup instead of a 1.2 s walk.
        remember_master(key, painting.size[0], painting.size[1], master)
    me = ob.data
    # `me.update()` is crumbed ON ITS OWN because it is the suspect: it tags the
    # depsgraph, which frees the EVALUATED mesh, and `ProjPaintState` caches
    # that mesh's corner and UV arrays for the whole of a modal stroke.  A trail
    # that ends on `mesh.update.enter` is the crash happening inside it; one
    # that ends just after `mesh.update.exit` is the next dab reading what it
    # freed.  Those are different findings and a single `land` span cannot tell
    # them apart.
    with crumbs.span("land_compile", mode=getattr(ob, "mode", "?"),
                     faces=len(polygons)):
        with readable_mesh(ob):
            with crumbs.span("write_binding"):
                moved = _write_binding(me, polygons, chosen.binding)
        _land(ob, state, idx, compiled)
        stamp_compile(ob, sheet, painting, master)
        with crumbs.span("mesh.update"):
            me.update()
    return moved


def ensure_compiled(ob):
    """The one step every way OUT of Blender runs first (decision 25).

    A push, an export and a GNS bundle each call this **before** `assemble`
    reads the mesh -- and never from inside `assemble`, which is decision 25 in
    as many words: a function whose name says it reads the scene must not
    quietly rewrite the mesh.  The seam is the point, not the saving.

    The sequencing is load-bearing.  The compile moves `palette_id`, which is a
    document member and one of the three fields a live push writes into RAM
    (`live_link.py`), so a compile landing after `assemble` had read the mesh
    would make the pushed map and the exported map different maps -- the exact
    failure decision 16 existed to prevent, relocated.

    Scoped to the ACTIVE state, which is the same scope `Convert` has: Convert
    re-unwraps the whole mesh, so a converted map carries one Painting however
    many sheets its disc arrangement had.  Anything this cannot reach is left
    to decision 19's warning, which is still real on the direct-paint path and
    in a reopened `.blend`.

    Returns report lines, and an empty list is the ordinary answer for a map
    with nothing to compile -- an unconverted document, or the direct-paint
    path, where nothing here applies.
    """
    found = _subject_of(ob)
    if isinstance(found, str):
        return []
    ob_, state, sheet, painting, idx, rows = found
    # A compile this process can PROVE is already current is a no-op, and
    # skipping it is not a weakening of decision 25 -- `freshness` says `fresh`
    # only when this process hashed this painting and the stamp agrees, so
    # running it again would land the same sheet from the same bytes.
    #
    # It matters because of decision 28: a settle compiles, and the push that
    # follows it comes straight through here.  Without this the loop pays for
    # the search twice on every settle, and the search is 3.4 s on a painted
    # canvas -- decision 27's "the common case is free" is about the write-back
    # moving zero faces, not about the search being cheap.
    if freshness(ob_, sheet, painting)[0] == "fresh":
        return []
    done = compile_now(ob_, state, sheet, painting, idx, rows)
    lines = [f"{sheet}: compiled on the way out -- {len(done.atoms)} charts, "
             f"error {done.before_mean:.2f} -> {done.compiled.error:.2f}, "
             + ("the binding this map already had won"
                if done.chosen.is_incumbent
                else f"{done.moved} face(s) re-bound")]
    note = animation_note_once((ob_.name, state), done.polygons, done.atoms,
                               done.animated)
    if note:
        lines.append(note)
    return lines


def _push_after(ob, why):
    """Both buttons reach the emulator, which is what an artist expects.

    *"I think maybe after the re-cluster and re-palette calc it should just
    try to auto push -- is that not what we're doing?"*  It was not: the push
    was reachable only from a settle, so pressing a button left the emulator
    showing the old sheet and nothing said so.

    It costs one compile, not two: the push comes back through
    `ensure_compiled`, which finds the sheet fresh -- this function having just
    stamped it -- and skips.  Imported here rather than at module scope because
    `settle_op` imports this module.
    """
    from .settle_op import push_after_compile
    return push_after_compile(ob, why)


class MAP_OT_recalculate_palettes(Operator):
    """Re-choose this state's sixteen colours a row from the painting, with \
each chart staying on the CLUT row it already reads"""
    bl_idname = "exmateria_map.recalculate_palettes"
    bl_label = "Recalculate palettes"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return not isinstance(_subject(context), str)

    def execute(self, context):
        found = _subject(context)
        if isinstance(found, str):
            self.report({"ERROR"}, found)
            return {"CANCELLED"}
        ob, state, sheet, painting, idx, rows = found

        master = image_rgb(painting)
        try:
            art = shrink_to_sheet(master, *painting.size)
        except ValueError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        try:
            # `readable_mesh`, because Edit Mode is where the artist already
            # is: in Edit Mode the attribute arrays read as size 0 while
            # `me.polygons` still reports a count, and this addon has shipped
            # that defect four times.
            with readable_mesh(ob):
                polygons = _face_ordered(ob.data)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        atoms = compile_map.charts(polygons)
        before, before_mean = compile_map.measure(polygons, art, rows, atoms)
        compiled = compile_map.compile_sheet(polygons, art, atoms, rows)
        _land(ob, state, idx, compiled)

        stamp_compile(ob, sheet, painting, master)
        rose = _report_regressions(
            ob, "Recalculate palettes", before, compiled.chart_error, compiled,
            extra=[f"state {state}, sheet {sheet}",
                   f"binding HELD; error {before_mean:.2f} -> "
                   f"{compiled.error:.2f}"])
        self.report({"INFO"},
                    f"recompiled {compiled.texels:,} texels: error "
                    f"{before_mean:.2f} -> {compiled.error:.2f}, binding held"
                    + (f"; {len(rose)} chart(s) worse "
                       f"({', '.join(str(r[0]) for r in rose[:TOASTED])}"
                       + (", ..." if len(rose) > TOASTED else "") + ")"
                       if rose else "; no chart worse"))
        _push_after(ob, "Recalculate palettes")
        return {"FINISHED"}


class MAP_OT_reselect_clusters(Operator):
    """Search for a better assignment of charts to CLUT rows and recompile. \
The binding the disc ships is always one of the candidates, so this can \
never make the map worse -- and can legitimately change nothing"""
    bl_idname = "exmateria_map.reselect_clusters"
    bl_label = "Re-select clusters"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return not isinstance(_subject(context), str)

    def execute(self, context):
        found = _subject(context)
        if isinstance(found, str):
            self.report({"ERROR"}, found)
            return {"CANCELLED"}
        ob, state, sheet, painting, idx, rows = found

        try:
            done = compile_now(ob, state, sheet, painting, idx, rows)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        (polygons, atoms, before, before_mean, chosen, compiled,
         moved, _animated) = done

        # Two different measurements, and they are labelled apart on purpose.
        # The search RANKS on the lattice (decision 31), so its pair of numbers
        # is about a question no CLUT row can tell apart from the painting;
        # what the artist will actually see is the fit's, measured on the true
        # painting under the rows this map is carrying right now.
        head = [f"state {state}, sheet {sheet}",
                f"{chosen.scored} candidates scored (the incumbent is one of "
                f"them); ranked on the lattice "
                f"{chosen.incumbent_error:.2f} -> {chosen.error:.2f}",
                f"on the painting {before_mean:.2f} -> {compiled.error:.2f}"]
        note = animation_note(polygons, atoms, done.animated)
        if note:
            head.append(note)
        rose = _report_regressions(ob, "Re-select clusters", before,
                                   compiled.chart_error, compiled, extra=head)
        if chosen.is_incumbent:
            # A no-op is a RESULT here, not a failure -- decision 8 puts the
            # incumbent in the candidate set precisely so it can win. Saying
            # "nothing moved" is the difference between that and a button that
            # looks broken.
            self.report({"INFO"},
                        f"the binding this map already has won, out of "
                        f"{chosen.scored} candidates (error "
                        f"{compiled.error:.2f}): nothing moved, and the "
                        f"palettes were recompiled under it")
            _push_after(ob, "Re-select clusters")
            return {"FINISHED"}
        self.report({"INFO"},
                    f"re-bound {moved} of {len(polygons)} face(s) across "
                    f"{len(atoms)} charts: error "
                    f"{before_mean:.2f} -> {compiled.error:.2f}"
                    + (f"; {len(rose)} chart(s) worse -- see the Log"
                       if rose else "; no chart worse"))
        _push_after(ob, "Re-select clusters")
        return {"FINISHED"}


CLASSES = (MAP_OT_recalculate_palettes, MAP_OT_reselect_clusters)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
