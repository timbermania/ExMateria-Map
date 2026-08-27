"""The **Log** — the running record of Outcomes, rendered into a Text datablock.

ADR-0185 decision 5. An **Outcome** is the stored line list from one run of an
export, a bake or a push, held on the marker (`last_export`, `last_bake`,
`last_push`). Those three stay the data; this is a *view* of them, and holds
nothing they do not — except their **sequence**, which the three-key model
cannot express. *"I pushed, then exported, and the export refused"* is a
sentence about order.

**Why a Text datablock and not a panel.** A Blender label cannot be selected
and neither can an operator's toast, so a Text editor is the only surface in
this application where a line can be got out with the mouse. That was already
the escape hatch `map.copy_report` wrote to; it was rewritten on every press,
never on screen, and therefore never a log.

**Why it does not rearrange anything.** `show()` fills Text editors that are
already open and are showing *nothing*, or are already showing this block. A
Text editor holding the artist's own script is never taken -- the same rule
`paint.image_editor_spaces()` follows for Image editors, and for the same
reason: an add-on fills the editors that are there, it does not commandeer
them. The Map workspace's third pane is one it built itself, so filling that is
not an exception to the rule; it is the rule applied to an empty editor.
"""
import time

import bpy

#: Unchanged from `import_document.REPORT_TEXT_NAME`, deliberately: a `.blend`
#: saved before the Log existed already carries a block under this name, and it
#: becomes the Log rather than being orphaned beside one.
LOG_NAME = "exmateria-map report"

#: Lines kept. A session of pushes should not grow the `.blend` without bound,
#: and the oldest entry is the one nobody is reading. The trim is by line and
#: not by entry so a single enormous refusal cannot outlive everything else.
MAX_LINES = 2000

_RULE = "─" * 52


def block(create=True):
    """The Log's Text datablock, creating it on first use."""
    blk = bpy.data.texts.get(LOG_NAME)
    if blk is None and create:
        blk = bpy.data.texts.new(LOG_NAME)
    return blk


def _body(lines):
    """An entry's lines, indented. Kept separate from `render` so the duplicate
    test can compare BODIES -- comparing rendered entries compares their
    stamps, which differ by construction, so nothing ever matched and Copy
    appended a second time every press."""
    return "\n".join(f"  {line}" for line in lines) if lines else "  (nothing)"


def _last_body(text):
    """The body of the entry currently at the end of the Log, or None."""
    if _RULE not in text:
        return None
    tail = text.rstrip("\n").rsplit(_RULE, 1)[-1].split("\n")
    return "\n".join(tail[2:]) if len(tail) > 2 else ""   # ["", header, ...]


def render(title, subject, lines):
    """One entry: a rule, a stamped header, then the Outcome's own lines.

    The time is the point of the header. Without it two pushes read as one
    event repeated, which is exactly the confusion the sequence is meant to
    resolve.
    """
    head = f"{_RULE}\n[{time.strftime('%H:%M:%S')}] {title}"
    if subject:
        head += f" — {subject}"
    return f"{head}\n{_body(lines)}\n"


def append(title, subject, lines, unless_duplicate=False):
    """Append one Outcome to the Log and return the block.

    `unless_duplicate` is for `map.copy_report`, which copies an Outcome the
    Log already holds: pressing Copy must not make the artist's own history
    say the thing happened twice.

    Never raises. An export that succeeded must not report failure because a
    text block could not be written.
    """
    try:
        blk = block()
        if blk is None:
            return None
        current = blk.as_string()
        if unless_duplicate and _last_body(current) == _body(lines):
            # The same Outcome, copied a moment later, is the same Outcome.
            return blk
        text = current + render(title, subject, lines)
        kept = text.splitlines()[-MAX_LINES:]
        blk.from_string("\n".join(kept) + "\n")
        return blk
    except Exception:                                               # noqa: BLE001
        return None


def text_editor_spaces():
    """Every Text editor space in EVERY workspace, as (space, protected).

    Protected means it is showing a text that is not the Log -- the artist's
    script, or a datablock they opened -- and must be left alone. `bpy.data.
    screens` and not `context.screen`, because the pane this wants to reach is
    in the Map workspace and the button may be pressed from another one.
    """
    out = []
    for screen in getattr(bpy.data, "screens", ()) or ():
        for area in getattr(screen, "areas", ()) or ():
            if area.type != "TEXT_EDITOR":
                continue
            for space in area.spaces:
                if space.type != "TEXT_EDITOR":
                    continue
                cur = getattr(space, "text", None)
                out.append((space, cur is not None and cur.name != LOG_NAME))
    return out


def show(blk=None):
    """Put the Log in every free Text editor and scroll it to the newest entry.

    Returns how many it filled. Zero is a normal answer -- the artist may have
    no Text editor open, or every one of them may be holding their own script.
    """
    blk = blk or block(create=False)
    if blk is None:
        return 0
    n = 0
    last = max(0, len(blk.lines) - 1)
    for space, protected in text_editor_spaces():
        if protected:
            continue
        try:
            space.text = blk
            # The newest entry is at the bottom, which is off screen by
            # default: a log you have to scroll to is a log nobody reads.
            space.top = max(0, last - 2)
            blk.current_line_index = last
        except Exception:                                           # noqa: BLE001
            continue
        n += 1
    return n


def record(title, subject, lines, unless_duplicate=False):
    """Append and reveal -- the one call an operator makes."""
    blk = append(title, subject, lines, unless_duplicate=unless_duplicate)
    if blk is not None:
        show(blk)
    return blk
