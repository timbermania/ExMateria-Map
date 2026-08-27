"""Vendored third-party-shaped code the addon does not own -- ADR-0004 §31.

`exmateria_map/` under here is a VERBATIM copy of the repository's
`exmateria-map/exmateria_map/` package.  The package is authoritative and the
copy is guarded by `tests/test_vendored_package.py`, which fails on any
difference in content or membership.  Never edit anything under
`_vendor/exmateria_map/` -- edit the package and re-copy.

The package is pure-Python and stdlib-only (`pyproject.toml`:
`dependencies = []`), so vendoring is a directory copy and nothing else: no
wheel, no install step, and the addon still installs as one zip through
Blender's own installer.
"""
