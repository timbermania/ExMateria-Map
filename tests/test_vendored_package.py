"""ADR-0004 decision 31 part 5: the vendored copy is guarded, not trusted.

The addon ships a verbatim copy of the whole `exmateria_map` package at
`addons/exmateria_map/_vendor/exmateria_map/` so that a Blender install is
self-contained -- decision 7's goal (one zip, no `pip install`) preserved, with
a larger zip.  The package stays authoritative.

This is `test_the_addon_and_the_package_share_one_png_codec` scaled from one
file to a directory, and it is deliberately a TREE comparison rather than a
membership rule: a rule about which modules to copy is a thing that drifts, and
nothing in this repo would catch the drift (there is no CI).  A file added to
the package and not to the copy fails here, as does a byte changed in either.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "exmateria_map"
VENDORED = ROOT / "addons" / "exmateria_map" / "_vendor" / "exmateria_map"


def tree(root):
    """`{relative path: bytes}` for every real source file under `root`.

    `__pycache__` is excluded: it is a build product of whichever interpreter
    ran last, so including it would make the guard fail on a stale `.pyc` --
    a red that says nothing about the copy.
    """
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


def test_the_addon_vendors_the_whole_package_verbatim():
    assert VENDORED.is_dir(), (
        f"decision 31 part 1: the addon vendors the package at {VENDORED}, "
        f"and nothing is there"
    )
    package, vendored = tree(PACKAGE), tree(VENDORED)

    missing = sorted(set(package) - set(vendored))
    extra = sorted(set(vendored) - set(package))
    assert not missing, (
        f"the package holds {len(missing)} file(s) the vendored copy does "
        f"not: {missing}; the copy is the WHOLE package, not a subset"
    )
    assert not extra, (
        f"the vendored copy holds {len(extra)} file(s) the package does not: "
        f"{extra}; the package is authoritative, so an edit made in the copy "
        f"is an edit made in the wrong tree"
    )

    drifted = sorted(n for n in package if package[n] != vendored[n])
    assert not drifted, (
        f"{len(drifted)} vendored file(s) differ from the package: {drifted}; "
        f"the addon would import a disc format the package no longer writes"
    )
