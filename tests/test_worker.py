"""`worker.spawn` -- the addon's rule for running Python off Blender's thread.

Plain `pytest`: the module names no `bpy`, which is what lets the rule be
graded without an emulator, a map or Blender.  The number the rule exists for
is in `worker.py`'s own table and is measured by `tests/blender_settle_stall.py`
against `wm.redraw_timer`; what is graded here is the mechanism -- that the
interval is actually lowered while a worker runs, actually restored when the
last one finishes, and that nothing in the addon goes around it.
"""
import ast
import sys
import threading
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
ADDON = PKG / "addons" / "exmateria_map"
# The addon's modules go on the path directly, as `test_compile.py` does: its
# `__init__` imports `bpy`, and `exmateria_map` already names the CLI package.
sys.path.insert(0, str(ADDON))

import worker                                          # noqa: E402


def _run(fn):
    done = threading.Event()
    seen = {}

    def body():
        seen["interval"] = sys.getswitchinterval()
        seen["live"] = worker.live()
        try:
            fn()
        finally:
            done.set()

    was = sys.getswitchinterval()
    thread = worker.spawn("test", body)
    assert done.wait(10.0), "the worker never ran"
    thread.join(10.0)
    return was, seen


def test_the_interval_is_lowered_while_a_worker_runs():
    was, seen = _run(lambda: None)
    # `approx`: `sys.setswitchinterval` stores a C double of the seconds and
    # 0.0001 does not round-trip exactly.
    assert seen["interval"] == pytest.approx(worker.SWITCH_INTERVAL)
    assert seen["interval"] < was, (seen["interval"], was)


def test_and_restored_when_the_last_one_finishes():
    was = sys.getswitchinterval()
    _run(lambda: None)
    assert sys.getswitchinterval() == was


def test_a_worker_that_raises_still_restores_it():
    """`finally`, not `else` -- a transport that throws is the ordinary case
    here (no emulator), and a session that left the interval down would carry
    the trade for the rest of its life."""
    was = sys.getswitchinterval()
    done = threading.Event()

    def body():
        try:
            raise RuntimeError("the transport raised")
        finally:
            done.set()

    hook = threading.excepthook
    threading.excepthook = lambda args: None      # the raise IS the fixture
    try:
        thread = worker.spawn("test", body)
        assert done.wait(10.0)
        thread.join(10.0)
    finally:
        threading.excepthook = hook
    assert sys.getswitchinterval() == was
    assert worker.live() == 0


def test_two_workers_refcount_rather_than_race():
    """The settle can have a compile and a push in flight at once, and the
    first to finish must not hand the UI back to CPython's default."""
    was = sys.getswitchinterval()
    hold, ran = threading.Event(), threading.Event()
    a = worker.spawn("a", lambda: hold.wait(10.0))
    while worker.live() < 1:
        pass
    b = worker.spawn("b", lambda: ran.set())
    assert ran.wait(10.0)
    b.join(10.0)
    # `b` has finished; `a` has not, so the interval is still ours.
    assert sys.getswitchinterval() == pytest.approx(worker.SWITCH_INTERVAL)
    hold.set()
    a.join(10.0)
    assert sys.getswitchinterval() == was


def _enter_line_before_thread(source):
    fn = next(n for n in ast.walk(ast.parse(source))
              if isinstance(n, ast.FunctionDef) and n.name == "spawn")
    at = {}
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            name = n.func.id if isinstance(n.func, ast.Name) else \
                getattr(n.func, "attr", "")
            if name in ("_enter", "Thread"):
                at.setdefault(name, n.lineno)
    return at["_enter"] < at["Thread"]


def test_the_refcount_is_taken_before_the_thread_starts():
    """Otherwise a worker that finishes immediately can `_leave` before
    `_enter` ever ran, and the interval goes DOWN and stays there."""
    assert _enter_line_before_thread((ADDON / "worker.py").read_text())
    # Seeded, on the same source, with the two swapped -- `ast.walk` is
    # breadth-first and a check that read its order rather than the line
    # numbers would pass either way.
    seeded = (ADDON / "worker.py").read_text().replace(
        "    _enter()\n\n    def run():",
        "    def run():", 1).replace(
        "    thread.start()\n    return thread",
        "    thread.start()\n    _enter()\n    return thread", 1)
    assert not _enter_line_before_thread(seeded), "the seed did not bite"


#: `settle_op` and `live_link_ui` are the two that got this wrong -- both
#: spawned a bare `Thread` and both were sold as "the freeze is off the main
#: thread". Anything else that wants a worker has to come through here too, or
#: it inherits the 8.7 fps.
ALLOWED = {"worker.py"}


def test_no_module_spawns_a_bare_thread():
    offenders = {}
    for path in sorted(ADDON.glob("*.py")):
        if path.name in ALLOWED:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "Thread"):
                offenders.setdefault(path.name, []).append(node.lineno)
    assert offenders == {}, offenders


def test_and_that_arm_bites(tmp_path):
    """Seeded, because a scan for an attribute name is exactly the check that
    goes quietly blind when the spelling moves."""
    seeded = tmp_path / "seeded.py"
    seeded.write_text("import threading\n"
                      "threading.Thread(target=lambda: None).start()\n")
    hits = [n.lineno for n in ast.walk(ast.parse(seeded.read_text()))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "Thread"]
    assert hits == [2]
