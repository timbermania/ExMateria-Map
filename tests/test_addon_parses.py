"""Every addon module PARSES, including the ones `bpy` keeps out of pytest.

Most of this package is covered by tests that import it. Four modules are not
importable here at all -- `export_document`, `import_document`, `live_link_ui`,
`authoring` and friends all `import bpy` at module scope, so the suite never
touches them and a syntax error in one is **completely invisible** to a green
run. Measured the hard way: a broken docstring in `export_document.py` was
committed with 294 tests passing, and only surfaced when Blender itself
refused to enable the addon.

`ast.parse` needs no `bpy`, so this is the one check that can cover them. It
proves nothing about behaviour -- the Blender harnesses do that -- but it turns
"the addon will not load at all" from a thing an artist discovers into a thing
the suite does.
"""

import ast
import sys
from pathlib import Path

import pytest

ADDON = Path(__file__).resolve().parent.parent / "addons" / "exmateria_map"
TOOLS = Path(__file__).resolve().parent.parent / "tools"

MODULES = sorted(p for p in ADDON.glob("*.py")) + sorted(TOOLS.glob("*.py"))


def test_the_module_list_is_not_empty():
    """A glob that matched nothing would make every check below vacuous, and
    a passing run of zero checks is the failure mode this whole file exists to
    stop happening twice."""
    assert len(MODULES) >= 10


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_the_module_parses(path):
    try:
        ast.parse(path.read_text(), filename=str(path))
    except SyntaxError as e:
        pytest.fail(f"{path.name}:{e.lineno}: {e.msg}")


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_every_docstring_is_terminated(path):
    """The specific shape of the bug above. A docstring whose closing quotes
    went missing does not usually fail to parse -- it silently swallows the
    code beneath it until the next string, which is how `export_sheets` lost
    its whole body while the file still tokenised in some editors."""
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        # Functions only. A CLASS whose body is one string is the ordinary
        # shape of an exception subclass -- `class VramError(RuntimeError):
        # """why"""` -- and this package has several.
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        # A function whose entire body is one string constant has lost its
        # code to an unterminated docstring (or is a stub, which this package
        # does not have).
        if len(body) == 1 and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            pytest.fail(
                f"{path.name}:{node.lineno}: {node.name} has nothing but a "
                "docstring -- its body was probably swallowed by a docstring "
                "that was never closed")
