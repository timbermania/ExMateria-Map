"""Locate and classify the map corpus.

The oracle is the extracted disc tree under ``project-assets/fft-extract/MAP/``,
which is local-only and gitignored. Discovery follows the same order as
``fft-iso-patcher/tests/_assets.py`` and honours the same env var, so a machine
configured for one is configured for both. Nothing here bakes in a user path.

The corpus is frozen 1997 PSX data, so its shape is an invariant, not a
measurement -- see ``EXPECTED_COUNTS``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple

from .sections import TEXTURE_BYTES

# The disc has exactly this many of each class. A run that finds fewer is
# looking at a partial corpus and must fail rather than report a smaller pass:
# a half-populated project-assets/ must never read as green.
EXPECTED_COUNTS: dict[str, int] = {"gns": 121, "texture": 658, "mesh": 796}
EXPECTED_TOTAL = sum(EXPECTED_COUNTS.values())   # 1575

CLASSES = ("gns", "texture", "mesh")


class Resource(NamedTuple):
    path: Path
    kind: str        # one of CLASSES

    @property
    def name(self) -> str:
        return self.path.name


def _walk_up_to_project_assets() -> Path | None:
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        candidate = parent / "project-assets"
        if candidate.is_dir():
            return candidate
    return None


def map_dir() -> Path | None:
    """Directory holding ``MAP###.GNS`` and its resources, or ``None``."""
    env_dir = os.environ.get("EXMATERIA_ASSETS_DIR")
    if env_dir:
        candidate = Path(env_dir)
        for base in (candidate / "MAP", candidate / "fft-extract" / "MAP", candidate):
            if base.is_dir() and any(base.glob("MAP*.GNS")):
                return base
        return None
    root = _walk_up_to_project_assets()
    if root is None:
        return None
    for base in (root / "fft-extract" / "MAP", root / "MAP"):
        if base.is_dir():
            return base
    return None


def classify(path: Path) -> str:
    if path.suffix.upper() == ".GNS":
        return "gns"
    if path.stat().st_size == TEXTURE_BYTES:
        return "texture"
    return "mesh"


def load(directory: Path | None = None) -> list[Resource]:
    """Every corpus file, classified, sorted by name.

    Raises ``CorpusError`` if the class counts don't match the disc.
    """
    directory = directory or map_dir()
    if directory is None:
        raise CorpusError("no corpus found; set EXMATERIA_ASSETS_DIR")

    resources = [
        Resource(p, classify(p))
        for p in sorted(directory.iterdir())
        if p.is_file()
    ]
    found = {k: sum(1 for r in resources if r.kind == k) for k in CLASSES}
    if found != EXPECTED_COUNTS:
        raise CorpusError(
            f"partial corpus at {directory}: found {found}, expected {EXPECTED_COUNTS}"
        )
    return resources


class CorpusError(RuntimeError):
    """The corpus is absent or incomplete. Never swallow this into a pass."""
