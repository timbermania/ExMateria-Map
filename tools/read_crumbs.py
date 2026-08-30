"""Read the breadcrumb trail a crashed Blender left behind.

`addons/exmateria_map/crumbs.py` writes one file per process, named for the PID,
so a trail pairs with a coredump by PID alone -- which is the join Blender's own
crash report cannot make, since `/tmp/blender.crash.txt` is overwritten by
whichever Blender died last and carries no PID at all.

    python3 tools/read_crumbs.py              # every trail, newest first
    python3 tools/read_crumbs.py --pid 927186 # one, in full
    python3 tools/read_crumbs.py --crashed    # only trails whose PID has a core

What to look for, in order:

* a `land_compile.enter` with **no** matching `.exit` -- the process died inside
  the mesh write;
* a `mesh.update.exit` in the last few lines -- the depsgraph was just tagged,
  so the evaluated mesh was freed and the next dab is reading it;
* `tick gesture=True` runs -- the guard holding a compile out of an open stroke;
* a `worker.begin` with no `worker.end` -- a thread was still running.
"""
import argparse
import os
import re
import subprocess
import tempfile
from pathlib import Path

PAT = re.compile(r"exmateria-map-crumbs-(\d+)\.log$")


def cored_pids():
    """PIDs systemd-coredump has a core for."""
    try:
        out = subprocess.run(["coredumpctl", "list", "--no-pager"],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:                                         # noqa: BLE001
        return set()
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) > 5 and parts[4].isdigit():
            pids.add(int(parts[4]))
    return pids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int)
    ap.add_argument("--tail", type=int, default=40)
    ap.add_argument("--crashed", action="store_true",
                    help="only trails whose PID left a coredump")
    args = ap.parse_args()

    found = []
    for f in Path(tempfile.gettempdir()).glob("exmateria-map-crumbs-*.log"):
        m = PAT.search(f.name)
        if m:
            found.append((f.stat().st_mtime, int(m.group(1)), f))
    found.sort(reverse=True)
    if not found:
        print("no crumb trails found -- the addon writes one per Blender "
              "process, so this means none has run since it was installed")
        return 0

    cores = cored_pids() if args.crashed else set()
    shown = 0
    for mtime, pid, f in found:
        if args.pid and pid != args.pid:
            continue
        if args.crashed and pid not in cores:
            continue
        lines = f.read_text(errors="replace").splitlines()
        mark = "  <-- HAS A COREDUMP" if pid in cores or (
            not args.crashed and pid in cored_pids()) else ""
        print(f"\n=== pid {pid}  {len(lines)} crumbs  "
              f"last written {os.path.getmtime(f):.0f}{mark}")
        keep = lines if args.pid else lines[-args.tail:]
        for line in keep:
            print("  " + line)
        # The finding, said rather than left to the reader.
        opened = [x for x in lines if ".enter" in x]
        closed = [x for x in lines if ".exit" in x]
        if len(opened) > len(closed):
            last = opened[-1].split()[2] if opened else "?"
            print(f"  >>> {len(opened) - len(closed)} span(s) entered and never "
                  f"left -- the process died inside `{last}`")
        shown += 1
    if not shown:
        print("no trail matched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
