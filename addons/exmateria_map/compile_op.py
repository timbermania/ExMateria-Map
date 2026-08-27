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
import bpy
from bpy.types import Operator

from . import compile_map
from .convert_op import (_face_ordered, clut_rows_of, source_art_name,
                         write_clut_rows_of)
from .export_document import image_rgb, readable_mesh, set_image_indices
from .import_document import marker_in_scene
from .paint import active_palette, index_image, painting_of, sheet_of_state

#: Charts named in the operator's own toast. The Log gets all of them
#: (decision 9 says NAME every chart whose error rose); a toast that scrolled
#: would name none of them legibly.
TOASTED = 3


def _subject(context):
    """The map, its active state, and everything the compile reads off them.

    Returns `(ob, state, sheet, painting, index, rows)` or a refusal string.
    """
    ob = marker_in_scene(context)
    if ob is None or "exmateria_map/base" not in ob:
        return "no map in this scene"
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

        art = image_rgb(painting)
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

        art = image_rgb(painting)
        me = ob.data
        try:
            with readable_mesh(ob):
                polygons = _face_ordered(me)
                # The atoms are fixed BEFORE the search and carried through.
                # `charts()` cuts at a `palette_id` change, so re-deriving
                # them after a re-bind gives a different, finer partition and
                # scores something other than what was chosen.
                atoms = compile_map.charts(polygons)
                before, before_mean = compile_map.measure(polygons, art, rows,
                                                          atoms)
                chosen = compile_map.select_binding(polygons, art, atoms)
                moved = _write_binding(me, polygons, chosen.binding)
                rebound = [dict(q, palette_id=chosen.binding[i])
                           if "uv" in q and chosen.binding[i] is not None
                           else q for i, q in enumerate(polygons)]
                compiled = compile_map.compile_sheet(rebound, art, atoms, rows)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        _land(ob, state, idx, compiled)
        me.update()

        head = (f"state {state}, sheet {sheet}",
                f"{chosen.scored} candidates scored (the incumbent is one of "
                f"them); error {chosen.incumbent_error:.2f} -> "
                f"{chosen.error:.2f}")
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
                        f"{chosen.error:.2f}): nothing moved, and the "
                        f"palettes were recompiled under it")
            return {"FINISHED"}
        self.report({"INFO"},
                    f"re-bound {moved} of {len(polygons)} face(s) across "
                    f"{len(atoms)} charts: error "
                    f"{chosen.incumbent_error:.2f} -> {compiled.error:.2f}"
                    + (f"; {len(rose)} chart(s) worse -- see the Log"
                       if rose else "; no chart worse"))
        return {"FINISHED"}


CLASSES = (MAP_OT_recalculate_palettes, MAP_OT_reselect_clusters)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
