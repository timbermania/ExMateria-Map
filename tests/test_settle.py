"""The settle: when a pause after painting becomes a compile (ADR-0186 Am. 7).

Decision 28 closes the loop by firing on a **settle** -- a pause of about 1.5 s
after painting stops -- so no button is pressed in the normal loop.  The hard
part is not the compile, which already exists; it is knowing that painting
STOPPED, and the difference between "stopped" and "between two strokes of one
gesture" is the whole of what this file grades.

`SettleClock` is `bpy`-free on purpose (ADR-0007 decision 4).  It is handed a
time and a digest of the canvas and answers one question -- compile now, or not
-- so the rule can be tested without a window, a brush or a clock.
"""

import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
ADDON = PKG / "addons" / "exmateria_map"
sys.path.insert(0, str(ADDON))
sys.path.insert(0, str(PKG))

from settle_clock import SettleClock                        # noqa: E402

QUIET = 1.5


def test_a_canvas_nobody_painted_never_asks_for_a_compile():
    """The ordinary tick, and it must cost nothing and say nothing.  A poll
    that fired on an untouched canvas would recompile a map forever."""
    clock = SettleClock(quiet=QUIET)
    clock.compiled("aaa")

    assert [clock.observe(t, "aaa") for t in (0.0, 1.0, 5.0, 60.0)] == \
        [None, None, None, None]


def test_a_gesture_in_progress_does_not_fire_between_two_strokes():
    """Decision 28's 1.5 s is *"long enough not to fire between two strokes of
    one gesture"*, and this is the case it exists for: the canvas keeps
    changing, so the pause never completes and nothing fires -- however long
    the artist paints for."""
    clock = SettleClock(quiet=QUIET)
    clock.compiled("aaa")

    fired = [clock.observe(t / 4.0, f"stroke-{t // 3}")
             for t in range(1, 60)]

    assert fired == [None] * 59, (
        f"{sum(1 for f in fired if f)} settle(s) fired mid-gesture")


def test_a_pause_after_painting_fires_exactly_once():
    """The loop closing itself.  The canvas moves, then holds still, and one
    compile is asked for -- one, not one per tick."""
    clock = SettleClock(quiet=QUIET)
    clock.compiled("aaa")

    assert clock.observe(0.0, "bbb") is None, "the change itself is not a settle"
    assert clock.observe(1.0, "bbb") is None, "1.0 s is not yet 1.5 s"
    assert clock.observe(1.6, "bbb") == "bbb", "1.6 s of quiet is a settle"
    assert clock.observe(2.0, "bbb") is None, "and it does not fire again"
    assert clock.observe(9.0, "bbb") is None


def test_a_compile_in_flight_is_never_joined_by_a_second():
    """Decision 30 puts the compile on a worker thread, so a tick can arrive
    while one is running.  Two compiles of one map at once would race on the
    mesh and on the sheet."""
    clock = SettleClock(quiet=QUIET)
    clock.compiled("aaa")
    clock.observe(0.0, "bbb")
    assert clock.observe(1.6, "bbb") == "bbb"

    assert clock.observe(3.0, "bbb") is None
    assert clock.observe(9.0, "ccc") is None, \
        "even a fresh painting waits for the compile that is already running"


def test_a_repaint_mid_compile_is_QUEUED_and_not_lost():
    """The handoff's rule, and the one that makes the loop self-healing: paint
    that arrives while a compile is running must be caught by the next settle
    rather than dropped because the compile that ran did not include it."""
    clock = SettleClock(quiet=QUIET)
    clock.compiled("aaa")
    clock.observe(0.0, "bbb")
    clock.observe(1.6, "bbb")                    # fires; "bbb" now in flight

    clock.observe(2.0, "ccc")                    # the artist paints again
    clock.compiled("bbb")                        # ...and "bbb" lands

    assert clock.observe(2.2, "ccc") is None, "the pause starts from the paint"
    assert clock.observe(3.7, "ccc") == "ccc", \
        "the repaint was dropped: nothing will ever compile it"


def test_undoing_a_stroke_is_caught_by_the_next_settle():
    """Decision 29: *"Undoing a stroke leaves the sheet stale for one settle
    and the next settle catches it; the loop is self-healing and needs no undo
    hook."*  An undo is just the canvas changing back."""
    clock = SettleClock(quiet=QUIET)
    clock.compiled("aaa")
    clock.observe(0.0, "bbb")
    assert clock.observe(1.6, "bbb") == "bbb"
    clock.compiled("bbb")

    clock.observe(2.0, "aaa")                    # Ctrl+Z: back to the start
    assert clock.observe(3.6, "aaa") == "aaa", \
        "the sheet is compiled from a painting that no longer exists"


def test_the_quiet_interval_is_a_PREFERENCE_and_not_a_constant():
    """Amendment 7 says so in as many words: 1.5 s is a first guess and
    *"nothing measured it"*.  An artist who paints in slow deliberate dabs
    needs a longer one."""
    slow = SettleClock(quiet=4.0)
    slow.compiled("aaa")
    slow.observe(0.0, "bbb")

    assert slow.observe(2.0, "bbb") is None
    assert slow.observe(4.1, "bbb") == "bbb"
