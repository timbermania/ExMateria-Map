"""The **Map workspace** — the sheet, the map, and a Log along the bottom.

BUILT, not bundled. ADR-0185 decision 1 as amended, and **Amendment 4** for the
shape: an Image Editor for the sheet beside a material-shaded 3D viewport for
the map, both with their `Map` sidebar open, and the **Log** running along the
bottom as a band — converted from the Timeline the duplicate already inherited,
and split off the viewport only as a fallback. Added by one button, or by an
import if the artist asked (Amendment 2).

**Two panes, not three.** The Log pane of Amendment 1 is dropped rather than
moved: the viewport widens to fill it, and its sidebar comes to rest against
the Outliner + Properties column the duplicate inherited. A third pane would
buy a second 280 px column, and **a sidebar `UI` region is 280 px whatever the
pane** (`Region.width` is read-only), so no arrangement of splits widens the
controls. The win is not width but *isolation*: `bl_category` means only our
panels draw there.

**Why built and not shipped as a `.blend`.** The ADR rejected generating the
layout because `bpy.ops.workspace.add()` returns `PASS_THROUGH`. That is true
-- and true *headful* as well, not only under `-b`, which the ADR assumed.
But it is the wrong operator: `bpy.ops.workspace.duplicate()` returns
`FINISHED` in **both** modes. Building the layout keeps a binary asset out of
a package that is otherwise stdlib-only, keeps it away from a leak scanner
that cannot read a `.blend` (`publish/common.sh` greps with `-I`), and pins no
Blender version. All of it is measured by `workspace/workspace_probe.py`.

**Why it is a timer and not four lines in `execute`.** Everything an area
needs happens on a screen a window is *showing*, and the switch into a new
workspace does not land until the next redraw:

- `area.type = "TEXT_EDITOR"` on a screen no window is showing sets `type` and
  `ui_type` and **never swaps `spaces.active`** -- not on the next redraw, and
  **not even once the window switches to that workspace**. The area keeps
  drawing as a 3D viewport for the rest of the session while `area.type`
  reports the editor you asked for. Assigning the same value again once the
  screen is visible is what fixes it (probe phase `typeset`, arms B and C).
  **So a check that reads `area.type` is blind to the whole defect** -- the
  first version of this file passed one and shipped three 3D viewports.
- `area.x` / `area.width` are stale until the screen re-lays out, so the panes
  cannot be told apart until a tick after the split.

Hence `_layout_when_visible`: split, let it lay out, split again, assign by
position, then select the tab. Every tick re-checks that the artist is still
in the workspace we made -- clicking away stops it, which is decision 4 at the
granularity of a redraw.

**The button does not run in the artist's window.** Blender opens Preferences as
a **separate, temporary window**: `Window.parent` is set, `Screen.is_temporary`
is True, and it holds exactly one `PREFERENCES` area. An operator invoked from
a panel drawn there gets *that* window as `context.window`, so laying a
workspace out on `context.window.screen` finds no 3D viewport at all -- and the
first release of this module did exactly that, handing the artist an unmodified
duplicate of whatever workspace they were on, timeline and all, with nothing
arranged and nothing said. Every entry point therefore resolves `_main_window`
rather than trusting `context.window`. `temp_override` cannot be used to
reproduce this from a script (*"Overriding context with temporary screen isn't
supported"*), which is why the probe never caught it.

**And `temp_override` is refused outright while a temporary screen is ACTIVE**,
whatever you override it *to* — `TypeError: Overriding context with an active
temporary screen isn't supported`. So a click made from Preferences cannot use
one at all, even to reach the artist's own window. Nothing in `execute` may
touch context: it resolves the window, checks its preconditions, duplicates the
workspace with a **bare** operator call, and arms the timer. Measured: by the
time a timer callback runs, `bpy.context.screen` is the main window's and not
temporary even with Preferences still open, so every `temp_override` in the
layout is safe there (probe phase `prefscontext`).

**The tab is not part of the layout.** `region.active_panel_category` is
writable on 5.2 (the addon's old note saying otherwise is retired), but it is
an *enum over the categories that currently exist*, so it refuses until a
panel of that category is registered for that editor **and** the region has
drawn once. `focus_tab` is idempotent and forgiving. Under Amendment 4 every
panel carries `bl_category = "Map"` in a sidebar, so the assignment now
SUCCEEDS — which is also what makes a refusal a real failure rather than the
expected answer it was while the panels were still in Properties.

**What `-b` can and cannot grade.** None of the layout happens in background
mode: the screen never lays out and the timers never tick. The suite grades
that the operator runs and that a workspace by the right name appears, and
nothing about the panes -- reading `area.type` there would grade the one field
that lies. The layout is graded headful by the probe's `build` phase. That is
ADR-0185's own conclusion, arrived at honestly.
"""
import bpy
from bpy.types import Operator

#: The workspace's name, and the `bl_category` its panels group under. One
#: string for both because the artist reads them as one thing: the Map tab.
WORKSPACE_NAME = "Map"
TAB = "Map"

#: Which revision of the layout a workspace WE built holds, and the ID
#: property that carries it.
#:
#: The workspace is write-once, and Amendment 4 is the first revision to find
#: out. `ensure_on_import` looked a workspace up BY NAME and, finding one,
#: switched to it and never rebuilt -- so an artist holding the previous layout
#: would have imported a map, been switched into the OLD panes, and reported
#: that nothing changed, for the third time. `_free_name()` would meanwhile
#: have handed the rebuild the name `Map.001`.
#:
#: A `Map` workspace with a STALE tag is ours and is removed and rebuilt. A
#: `Map` workspace with NO tag was not built by us -- the artist made one, or
#: it predates the tag -- and is left alone, which is what makes the removal
#: safe rather than presumptuous. The next layout revision costs one integer.
#:
#: 1 -- three panes: sheet | map | Log (Amendment 1).
#: 2 -- the console runs along the bottom and the sidebar is the column;
#:      no Log pane (Amendment 4).
LAYOUT_VERSION = 2
VERSION_KEY = "exmateria_map/layout_version"


def _ours(ws):
    """Did WE build this workspace? Untagged means no, and no means hands off."""
    return ws is not None and VERSION_KEY in ws.keys()


def _retire_stale(name=WORKSPACE_NAME):
    """Remove our own `Map` workspace if it holds a PREVIOUS layout.

    Returns True if one went, which is what lets the caller say `rebuilt`
    rather than `built`. A window showing it is moved off first.

    **`bpy.data.batch_remove` is the only route.** Measured on 5.2:
    `bpy.data.workspaces` has no `remove()` at all
    (`bpy_prop_collection: attribute "remove" not found`), and
    `bpy.ops.workspace.delete()` returns `FINISHED` and removes NOTHING -- it
    acts on `context.workspace`, and under `-b` the window switch that would
    have made the stale one current never lands. A FINISHED that deleted
    nothing is the worst of the three, because it reads as success.
    """
    ws = bpy.data.workspaces.get(name)
    if not _ours(ws) or ws[VERSION_KEY] == LAYOUT_VERSION:
        return False
    wm = getattr(bpy.context, "window_manager", None)
    for w in (list(wm.windows) if wm else []):
        if w.workspace == ws:
            other = next((o for o in bpy.data.workspaces if o != ws), None)
            if other is None:
                return False                 # the only workspace there is
            w.workspace = other
    try:
        bpy.data.batch_remove([ws])
    except (RuntimeError, ReferenceError, TypeError, AttributeError):
        return False
    return bpy.data.workspaces.get(name) is None

#: The ONE vertical split, as a fraction of the band measured from the left:
#: the sheet against the map. Chosen to keep the sheet at the ~628 px the probe
#: photographed at 2560 px while the viewport takes the rest -- the Log pane is
#: gone, so the map gets the 556 px it used to hold. Which PIECE `area_split`
#: hands back varies (it is always the smaller one, probe phase `splitrule2`,
#: seven factors from 0.20 to 0.80); nothing here relies on that, because the
#: panes are told apart by POSITION one tick later, when position means
#: something.
SHEET_SPLIT = 0.30

#: The Log band's share of the viewport's height, used ONLY when there is no
#: Timeline to convert. See `_timeline`.
LOG_BAND = 0.12

#: How long the layout waits for the workspace switch to land, and then for a
#: sidebar to learn it has a `Map` tab: 40 ticks of 0.2 s. It gives up
#: silently -- there is nothing the artist could do about it, and until
#: decision 3 lands the tab legitimately does not exist.
TICK = 0.2
TICK_BUDGET = 40


def _main_window(windows=None):
    """The artist's window -- never the Preferences one.

    Takes the window list so it can be exercised without a second window: the
    only way to get one in Blender is to open Preferences for real, and a
    headless harness cannot.
    """
    if windows is None:
        wm = getattr(bpy.context, "window_manager", None)
        windows = list(wm.windows) if wm else []
    else:
        windows = list(windows)
    for w in windows:
        if w.parent is None and not w.screen.is_temporary:
            return w
    return windows[0] if windows else None


def _has_viewport(screen):
    """Is there a 3D viewport to build the layout out of?

    False for the Preferences window's screen, and for any workspace the
    artist has left without one. Checked BEFORE anything is created, so the
    refusal is a message rather than a useless duplicate.
    """
    return any(a.type == "VIEW_3D" for a in screen.areas)


def _split(window, screen, area, direction, factor):
    """Split `area`; False if the context would not take it, so the caller retries.

    `temp_override` raises while a temporary screen is active. Measured, a timer
    callback does not see one even with Preferences open — but a refusal here
    costs one tick and a raise costs the artist a traceback, so it is caught.
    """
    region = next((r for r in area.regions if r.type == "WINDOW"), None)
    if region is None:
        return False
    try:
        with bpy.context.temp_override(window=window, screen=screen, area=area,
                                       region=region,
                                       space_data=area.spaces.active):
            bpy.ops.screen.area_split(direction=direction, factor=factor)
    except TypeError:                  # an active temporary screen: try later
        return False
    return True


def _biggest(screen, area_type="VIEW_3D"):
    """The widest area of a type, or None."""
    same = [a for a in screen.areas if a.type == area_type]
    return max(same, key=lambda a: a.width, default=None)


def _band(screen, y, height):
    """The row of areas the original viewport occupied, left to right."""
    return sorted((a for a in screen.areas
                   if abs(a.y - y) < 4 and abs(a.height - height) < 4),
                  key=lambda a: a.x)


def _timeline(screen):
    """The factory Timeline strip, which is where the Log band comes from.

    ADR-0185 Amendment 4: *"move the console so it runs across the bottom like
    in most sane programs with a console."* The workspace is a **duplicate**,
    so it inherits one -- a 74 px strip already spanning the viewport's width
    and already stopping at the Outliner/Properties column, which is the shape
    the artist asked for. Converting it costs no split and takes no height from
    the viewport, and a band that stops where Blender's own Timeline stops is
    the engine's idiom rather than a truncation.

    `ui_type`, not `type`: a Timeline IS a `DOPESHEET_EDITOR`, and so is the
    artist's dope sheet. Taking one for the other would convert their animation
    editor into a Log.
    """
    for a in screen.areas:
        if a.type == "DOPESHEET_EDITOR" and a.ui_type == "TIMELINE":
            return a
    return None


def focus_tab(screen, tab=TAB):
    """Select the `Map` sidebar tab wherever it exists; return the editors set.

    Refusals are expected and are not failures: the property is an enum over
    the categories a region currently knows about, so it raises `TypeError`
    until a panel of that category is registered for that editor and the
    region has drawn once.
    """
    done = []
    for area in screen.areas:
        for region in area.regions:
            if region.type != "UI":
                continue
            try:
                region.active_panel_category = tab
            except (TypeError, AttributeError):
                continue
            done.append(area.type)
    return done


def _free_name(base=WORKSPACE_NAME):
    if base not in bpy.data.workspaces:
        return base
    n = 1
    while f"{base}.{n:03d}" in bpy.data.workspaces:
        n += 1
    return f"{base}.{n:03d}"


def _tab_when_drawn(window, name):
    """Timer callback: select the `Map` tab, retrying until a sidebar has one.

    Used on its own when the workspace already exists -- switching to an
    artist's existing Map workspace must not re-split it.
    """
    state = {"left": TICK_BUDGET}

    def tick():
        ws = bpy.data.workspaces.get(name)
        state["left"] -= 1
        if ws is None or state["left"] <= 0:
            return None
        if window.workspace != ws:
            return TICK
        return None if focus_tab(window.screen) else TICK

    return tick


def _layout_when_visible(window, name):
    """Timer callback: lay the new workspace out, one step per redraw.

    ADR-0185 Amendment 4's layout::

        +--------------------------+--------------------------------+---------+
        | Image Editor  +----------+ 3D Viewport         +----------+ Outliner|
        | (the sheet)   | Map tab  | (the map)           | Map tab  +---------+
        +---------------+----------+---------------------+----------+ Props   |
        | Log -- bottom band                                        | (theirs)|
        +-----------------------------------------------------------+---------+

    **Two panes, not three.** The Log pane is dropped rather than moved: the
    viewport widens to fill it and its own `Map` sidebar comes to rest against
    Blender's Properties column. A third pane would buy a second 280 px column,
    which is width the panels cannot use -- they are too *tall*, not too
    narrow, and the two decisions that fix height (6, and 5's in-panel half)
    land with this.

    Nothing is carried between ticks but plain integers. A `bpy` struct held
    across a screen re-layout is a pointer into memory Blender may have moved,
    so the panes are re-found from the screen every time.
    """
    state = {"step": 0, "left": TICK_BUDGET, "x": 0, "y": 0, "w": 0, "h": 0}

    def tick():
        ws = bpy.data.workspaces.get(name)
        state["left"] -= 1
        if ws is None or state["left"] <= 0:
            return None
        if window.workspace != ws:
            # The switch has not landed yet, or the artist moved on. Either
            # way the only safe thing to do is nothing.
            return TICK
        screen = window.screen

        if state["step"] == 0:
            view = _biggest(screen, "VIEW_3D")
            if view is None:
                return None
            state["x"], state["y"] = view.x, view.y
            state["w"], state["h"] = view.width, view.height
            strip = _timeline(screen)
            if strip is not None:
                strip.type = "TEXT_EDITOR"
                state["step"] = 2                 # the viewport is untouched
                return TICK
            if not _split(window, screen, view, "HORIZONTAL", LOG_BAND):
                return TICK                       # context refused; try again
            state["step"] = 1
            return TICK

        if state["step"] == 1:
            # The fallback split left two stacked pieces where the viewport
            # was. The LOWER one is the band -- by position, now that the
            # screen has laid out and a position is a fact.
            stack = sorted((a for a in screen.areas
                            if a.type == "VIEW_3D"
                            and abs(a.x - state["x"]) < 4
                            and abs(a.width - state["w"]) < 4),
                           key=lambda a: a.y)
            if len(stack) >= 2:
                stack[0].type = "TEXT_EDITOR"
                top = stack[-1]
                state["y"], state["h"] = top.y, top.height
            state["step"] = 2
            return TICK

        if state["step"] == 2:
            view = _biggest(screen, "VIEW_3D")
            if view is not None and not _split(window, screen, view,
                                               "VERTICAL", SHEET_SPLIT):
                return TICK
            state["step"] = 3
            return TICK

        if state["step"] == 3:
            band = _band(screen, state["y"], state["h"])
            if len(band) >= 2:
                band[0].type = "IMAGE_EDITOR"
                view = band[-1]
                view.spaces.active.shading.type = "MATERIAL"
                # Both sidebars open: the `Map` tab is the column now, and a
                # closed one is the whole landing invisible.
                for area in (band[0], view):
                    try:
                        area.spaces.active.show_region_ui = True
                    except AttributeError:      # an editor with no sidebar
                        pass
            # The band exists to hold the Log; an empty Text editor is the band
            # not doing its job. `show` fills editors that are empty or already
            # ours and never one holding the artist's own script, so this is
            # the general rule applied to an editor we just made, not an
            # exception to it.
            #
            # `block()`, not a bare `show()`: on a fresh session no Outcome has
            # been recorded yet, so the datablock does not exist and `show`
            # returns 0 with nothing to fill -- measured, the band came up
            # blank (probe phase `build`, `log_pane_holds_the_log`). Creating
            # it here means the band carries its own name from the moment it
            # appears, which is what tells the artist what the strip is for
            # before the first export.
            from .report_log import block, show
            show(block())
            state["step"] = 4
            return TICK

        return None if focus_tab(screen) else TICK

    return tick


def build(window=None, name=None):
    """Create the Map workspace, switch to it, and arm its layout.

    Returns the workspace, or None if Blender refused to duplicate. The panes
    land over the next few redraws -- see `_layout_when_visible` and the module
    docstring for why none of it can happen here.
    """
    window = window or _main_window()
    if window is None or not _has_viewport(window.screen):
        return None
    if name is None:
        # Before `_free_name()`, not after: our own previous layout is what
        # would otherwise push this one to `Map.001`.
        _retire_stale()
    before = {w.name for w in bpy.data.workspaces}
    # BARE, with no `temp_override`.  This runs in the click's own context, and
    # a click made from the Preferences window has an active temporary screen,
    # where `temp_override` raises whatever you override it to.  The duplicate
    # takes `context.workspace`, which a Preferences window shares with its
    # parent, so bare is also correct and not merely tolerated.
    bpy.ops.workspace.duplicate()
    made = [w for w in bpy.data.workspaces if w.name not in before]
    if not made:
        return None
    ws = made[0]
    ws.name = name or _free_name()
    ws[VERSION_KEY] = LAYOUT_VERSION
    window.workspace = ws
    # "just when I open the workspace, that should be my brush as a default."
    # ONCE, and never re-asserted -- `paint.seed_brush` carries the mark and
    # the reasoning. It returns "no brush" and stays unmarked when no paint
    # mode has been entered yet (the brush is an asset and does not exist
    # before that), in which case the first `Paint sheet` takes it instead.
    from . import paint
    paint.seed_brush(getattr(bpy.context, "scene", None))
    bpy.app.timers.register(_layout_when_visible(window, ws.name),
                            first_interval=TICK)
    return ws


def ensure_on_import(context):
    """Offer the Map workspace at the end of an import, if the artist asked.

    ADR-0185 decision 4 said the workspace is **not** hooked to import, on the
    reasoning that *"an import that throws the artist out of the layout they
    built is `marker_in_scene`'s failure one level up."* Reported from use, the
    artist wants exactly that on a GNS or interchange import -- so the decision
    is Amendment 2, and the way it survives its own objection is that the
    artist owns the switch (`workspace_on_import`, on by default). An import
    that moves you is a preference you set; the failure the decision named was
    an import that moves you with no say in it.

    Never raises: an import that succeeded must not report failure because a
    screen could not be arranged. Returns what it did, for the harness.
    """
    prefs = None
    try:
        prefs = context.preferences.addons[__package__].preferences
    except (AttributeError, KeyError):
        pass
    if prefs is not None and not getattr(prefs, "workspace_on_import", True):
        return "off"
    window = _main_window()
    if window is None or not _has_viewport(window.screen):
        return "no viewport"
    retired = _retire_stale()
    existing = bpy.data.workspaces.get(WORKSPACE_NAME)
    if existing is not None:
        # Untagged, therefore not ours: switch to it and touch nothing.
        window.workspace = existing
        bpy.app.timers.register(_tab_when_drawn(window, existing.name),
                                first_interval=TICK)
        return "switched"
    if build(window) is None:
        return "refused"
    return "rebuilt" if retired else "built"


class MAP_OT_add_workspace(Operator):
    """Add the Map workspace: the sheet, the map, and the Log along the bottom.

    ADR-0185 decision 4: offered, never seized. This is a button and not an
    import side effect, because an import that throws the artist out of the
    layout they built is the failure `marker_in_scene` already taught this
    package about, one level up. (Amendment 2 hooks it to import as well, on a
    preference the artist owns -- the objection was to an import that moves you
    with no say in it.)
    """
    bl_idname = "exmateria_map.add_workspace"
    bl_label = "Add the Map workspace"
    bl_description = ("Add a workspace -- the texture sheet beside the map, "
                      "the Map controls in both sidebars, and the Log "
                      "along the bottom -- as a new tab on the top bar. "
                      "Nothing you already have open is changed")
    bl_options = {"REGISTER"}

    def execute(self, context):
        # NOT `context.window`: clicked from the addon preferences that is the
        # temporary Preferences window, whose screen holds one PREFERENCES area
        # and no viewport. See the module docstring.
        window = _main_window()
        if window is None:
            self.report({"ERROR"}, "no Blender window to build a workspace in")
            return {"CANCELLED"}
        if not _has_viewport(window.screen):
            self.report({"ERROR"},
                        "the workspace you are in has no 3D viewport to build "
                        "the Map layout from — switch to one that has and "
                        "press this again")
            return {"CANCELLED"}
        rebuilt = _retire_stale()
        existing = bpy.data.workspaces.get(WORKSPACE_NAME)
        if existing is not None:
            # Already theirs: switch to it and touch nothing else. Re-running
            # the layout would split a screen the artist has since arranged.
            window.workspace = existing
            bpy.app.timers.register(_tab_when_drawn(window, existing.name),
                                    first_interval=TICK)
            self.report({"INFO"},
                        f"the {WORKSPACE_NAME} workspace is already here — "
                        f"switched to it")
            return {"FINISHED"}
        ws = build(window)
        if ws is None:
            self.report({"ERROR"},
                        "Blender refused to duplicate the current workspace")
            return {"CANCELLED"}
        self.report({"INFO"}, f"{'rebuilt' if rebuilt else 'added'} the "
                              f"{ws.name} workspace")
        return {"FINISHED"}


def menu_func(self, context):
    """File ▸ Import, under the two importers.

    Not an import, and it sits in the import menu anyway: that menu is where
    an artist already goes to meet this addon, and the addon preferences --
    behind a disclosure triangle in a window that is not even the one the
    layout lands in -- was reachable without being findable. Reported from
    use. The preferences keep their copy: it is the same operator, so there is
    one behaviour and two doors, not two behaviours.
    """
    self.layout.separator()
    self.layout.operator(MAP_OT_add_workspace.bl_idname, icon="WORKSPACE")


classes = (MAP_OT_add_workspace,)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    # Blender 5.x removed `bpy.utils.menu_registry`; the fallback covers the
    # 4.x line `bl_info["blender"]` still claims. Same shape as the two
    # importers in `import_document` / `gns_bundle`.
    try:
        bpy.types.TOPBAR_MT_file_import.append(menu_func)
    except AttributeError:
        from bpy.utils import menu_registry
        menu_registry.append(bpy.types.TOPBAR_MT_file_import, menu_func)


def unregister():
    try:
        bpy.types.TOPBAR_MT_file_import.remove(menu_func)
    except AttributeError:
        from bpy.utils import menu_registry
        menu_registry.remove(bpy.types.TOPBAR_MT_file_import, menu_func)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
