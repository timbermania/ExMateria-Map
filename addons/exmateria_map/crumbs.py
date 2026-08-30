"""A breadcrumb trail that SURVIVES a segfault.

Blender's own crash report (`/tmp/blender.crash.txt`) records **operators** and a
C backtrace.  The settle is neither.  It runs from `bpy.app.timers`, so nothing
it does appears in that trail, and `# Python backtrace` is empty in all five
crashes on record because the addon is not on the stack when the process dies --
it corrupted state that Blender dereferenced afterwards.  So the crash report
says what died and never says what we were doing.

This says what we were doing.  One line per event, with a monotonic timestamp
and the thread name, in a file named after the PID so it pairs with the
coredump.  Read it with `tools/read_crumbs.py`.

**Why line buffering is enough, and why there is no `fsync`.**  A segfault kills
the PROCESS, not the kernel.  `open(..., buffering=1)` issues one `write(2)` per
line, so by the time the call returns the bytes are already in the page cache
and will reach the disk whether or not this process survives.  `fsync` defends
against the machine losing power, which is not the failure being investigated,
and it would put a real syscall stall on every crumb -- including inside the
settle's 4 Hz tick.  So it is deliberately not used.  This is the difference
between durability against a crash and durability against a power cut, and only
the first one is needed here.

**Cost.**  A tick crumb is about 60 bytes and one `write(2)`.  At 4 Hz that is
under 1 MB/hour, and the file is capped.
"""
import os
import tempfile
import threading
import time

#: Bigger than any crash window and small enough to never matter on disk.  When
#: it is reached the file restarts with a marker rather than growing without
#: bound -- the interesting part of this trail is always its END.
CAP_BYTES = 16 * 1024 * 1024

_LOCK = threading.Lock()
_STATE = {"fh": None, "bytes": 0, "path": None, "t0": None}


def path():
    """Where this process's trail is, or `None` before the first crumb."""
    return _STATE["path"]


def _open():
    p = os.path.join(tempfile.gettempdir(),
                     f"exmateria-map-crumbs-{os.getpid()}.log")
    fh = open(p, "a", buffering=1)
    _STATE.update(fh=fh, bytes=0, path=p, t0=time.monotonic())
    fh.write(f"# exmateria-map crumbs, pid {os.getpid()}, "
             f"started {time.strftime('%F %T')}\n")
    return fh


def drop(event, **fields):
    """Record one event.  Never raises -- a broken trail must not break paint."""
    try:
        with _LOCK:
            fh = _STATE["fh"]
            if fh is None:
                fh = _open()
            elif _STATE["bytes"] > CAP_BYTES:
                fh.close()
                fh = open(_STATE["path"], "w", buffering=1)
                _STATE.update(fh=fh, bytes=0)
                fh.write("# --- restarted at the cap; earlier crumbs dropped ---\n")
            rest = " ".join(f"{k}={v}" for k, v in fields.items())
            line = (f"{time.monotonic() - _STATE['t0']:9.3f} "
                    f"{threading.current_thread().name:<28} {event}"
                    + (" " + rest if rest else "") + "\n")
            fh.write(line)
            _STATE["bytes"] += len(line)
    except Exception:                                         # noqa: BLE001
        pass


class span:
    """`with crumbs.span("land"): ...` -- crumbs both the entry and the exit.

    The EXIT is the half that matters.  A region that was entered and never left
    is the region the process died inside, and that is only visible if the exit
    is a separate line rather than a duration printed at the end.
    """

    def __init__(self, event, **fields):
        self.event, self.fields = event, fields

    def __enter__(self):
        self.t = time.monotonic()
        drop(self.event + ".enter", **self.fields)
        return self

    def __exit__(self, *exc):
        drop(self.event + ".exit", ms=f"{(time.monotonic() - self.t) * 1000:.1f}",
             raised=bool(exc[0]))
        return False
