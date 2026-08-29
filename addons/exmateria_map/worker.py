"""Running Python off Blender's main thread -- and the one thing that makes it
work.

**A worker thread does not free Blender's UI.**  That is the sentence this
module exists for, and it is the opposite of what decision 30 and decision 28
assumed when they sent the compile and the push to `threading.Thread`.  Python
has one interpreter lock; a worker doing pure-Python work holds it, and a
Blender window redraw is not one Python call but one per panel `draw()`, of
which its own UI has dozens.  Every one of them waits out a GIL switch
interval.

Measured on this box, Blender 5.2.0, with `wm.redraw_timer(DRAW_WIN_SWAP)` --
which is Blender drawing its own windows, not a proxy for it:

| | Blender fps | worker throughput |
|---|---|---|
| idle, no worker | 586 | -- |
| a worker, `switchinterval` 5 ms (CPython's default) | **8.7** | 212 |
| a worker, `switchinterval` 0.5 ms | 83.3 | 206 |
| a worker, **`switchinterval` 0.1 ms** | **218.4** | 183 |
| a worker, `sleep(0)` between chunks | 17.2 | 207 |

8.7 fps is the artist's *"blender was unusable -- basically"*, and it is what a
thread bought on its own.  0.1 ms costs the worker **14 %** of its throughput
and hands the UI back 25x, which is the trade this module makes.  `sleep(0)` is
in the table because it is the obvious fix and it is nearly worthless: yielding
at a chunk boundary does nothing about the 5 ms every OTHER Python entry waits.

**Scoped, not global.**  The interval is lowered while one of *our* workers is
alive and restored when the last one finishes, so a session with nothing in
flight runs on CPython's default and no other addon inherits our trade.  It is
a refcount because the settle can have a compile and a push in flight at once.

The GIL is also why "put it on a thread" is never the whole answer here: the
work that stays on the main thread still blocks outright.  See
`export_document.image_rgb`'s cache and `assemble`'s `sidecars` flag, which are
the other half of the same report.
"""
import sys
import threading

#: Measured above.  Not a tunable: the table is the argument for it, and a
#: number an artist can move is a number that can put them back at 8.7 fps.
SWITCH_INTERVAL = 0.0001

_LOCK = threading.Lock()
_STATE = {"live": 0, "was": None}


def _enter():
    with _LOCK:
        if not _STATE["live"]:
            _STATE["was"] = sys.getswitchinterval()
            sys.setswitchinterval(SWITCH_INTERVAL)
        _STATE["live"] += 1


def _leave():
    with _LOCK:
        _STATE["live"] -= 1
        if not _STATE["live"] and _STATE["was"] is not None:
            sys.setswitchinterval(_STATE["was"])
            _STATE["was"] = None


def live():
    """How many of our workers are running.  For the harnesses."""
    return _STATE["live"]


def spawn(name, fn):
    """Start `fn` on a daemon thread with the UI's share of the GIL protected.

    The refcount is taken BEFORE the thread starts, so a worker that finishes
    immediately cannot drop the interval back before it was ever raised.
    """
    _enter()

    def run():
        try:
            fn()
        finally:
            _leave()

    thread = threading.Thread(target=run, daemon=True, name=name)
    thread.start()
    return thread
