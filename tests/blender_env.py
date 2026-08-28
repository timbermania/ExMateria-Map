"""Per-run isolation for every Blender these suites launch.

**Why this module exists.** The suites install the addon into the Blender they
start (`bpy.ops.preferences.addon_install`), and `--factory-startup` does **not**
isolate `BLENDER_USER_RESOURCES` -- that flag resets *preferences*, not the
scripts directory. With no `env=` on the `subprocess.run`, the child inherited
the caller's environment, so running a suite INSTALLED a snapshot of the tree
over the artist's own
`~/.config/blender/<ver>/scripts/addons/exmateria_map`.

Measured 2026-08-27, and it cost a round trip with the artist: they reported the
light controls still sitting in the Preview panel after a commit that moved
them. The repo was correct, both suites were green, and the addon their Blender
was loading was a copy stamped at the minute a suite had last been run. **A
green suite said nothing about the addon being clicked, because the suite was
the thing that had overwritten it.**

It cuts both ways, and the other direction was already on the record inside
`blender_roundtrip.py`: `addon_enable` imports `exmateria_map` from
`bpy.utils.user_resource("SCRIPTS")`, so a suite with no isolation GRADES
whatever is installed there rather than the tree it believes it is testing.
Four seeded runs in parallel installed over each other and the audit read 4/13,
three of them graded against a fourth's code.

So isolation is the DEFAULT for every launch here, never an opt-in -- an opt-in
is the thing that failed, since it required every caller to remember.

An explicit `BLENDER_USER_RESOURCES` already in the environment still wins:
parallel seeded runs need a directory each, and that is how they get one.
"""
import os
import sys
from pathlib import Path

#: One directory per suite, under `tests/`. Per SUITE rather than shared so two
#: different suites can run at once; runs of the SAME suite reuse it, which is
#: fine because `addon_install` overwrites. Two runs of the same suite in
#: parallel must pass their own `BLENDER_USER_RESOURCES` -- see the docstring.
ROOT = Path(__file__).resolve().parent / ".blender_userres"


def isolated_env(tag=None):
    """The environment to launch Blender with, isolated from the artist's own.

    `tag` names the sub-directory; it defaults to the running script's stem,
    which is the suite's name. Returns a fresh dict -- never mutates
    `os.environ`, so a caller that launches twice is unaffected by the first.
    """
    env = dict(os.environ)
    if env.get("BLENDER_USER_RESOURCES"):
        return env
    tag = tag or Path(sys.argv[0]).stem or "blender"
    root = ROOT / tag
    # `scripts/addons` precreated: `addon_install` will make it, but a suite
    # that only ENABLES (never installs) needs it to exist or the enable finds
    # nothing and the failure reads as a broken addon rather than a missing dir.
    (root / "scripts" / "addons").mkdir(parents=True, exist_ok=True)
    env["BLENDER_USER_RESOURCES"] = str(root)
    return env


def audit_launchers():
    """Every suite here that starts Blender must isolate it. Returns offenders.

    A ratchet, and it is graded on SOURCE because no suite can observe its own
    launch: by the time the checks run, the process is already inside the
    Blender in question. So this reads the sibling scripts instead.

    The rule is narrow and mechanical on purpose -- a `subprocess.run` that
    starts Blender must pass `env=`. That is exactly the line whose absence
    installed a snapshot over the artist's addon, and a broader rule ("be
    careful") is the one that was already in force and did not hold.
    """
    import re
    offenders = []
    for path in sorted(Path(__file__).resolve().parent.glob("blender_*.py")):
        if path.name == Path(__file__).name:
            continue
        src = path.read_text()
        for m in re.finditer(r"(?:proc|p)\s*=\s*subprocess\.run\((.{0,600}?)\)\n",
                             src, re.S):
            call = m.group(1)
            if "blender" not in call.lower() and "BLENDER" not in call:
                continue
            if "env=" not in call:
                offenders.append(f"{path.name}: launches Blender with no env=")
    return offenders
