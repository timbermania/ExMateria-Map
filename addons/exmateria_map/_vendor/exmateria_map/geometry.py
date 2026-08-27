"""Floor coverage and the drift set (decisions 12, 15, 23).

``build`` has to tell a *drift-named* tile from any other pre-growth tile
(schema §7.2), and schema §4 puts ``base.floor_steps`` out of reach --
``dump`` computes it, ``build`` ignores it. So ``build`` recomputes the drift
set from the two things it does trust: the document's polygons and the base
map's own terrain. This module is that computation, and it is deliberately the
same rule the addon's drift checker runs (``authoring.drifted``): a tile the
addon would never flag must not be a tile ``build`` accepts a fix for.

All coordinates are raw disc coordinates. Y is negative-upward in this data,
which is why a floor's *bottom* is ``-max(ys)``.
"""

from __future__ import annotations

import math
from collections import defaultdict

from .document import FLOOR_COS, HEIGHT_STEP, TILE_UNITS


def ring(positions) -> list:
    """Raw quads are PSX triangle-STRIP order; the ring is (0, 1, 3, 2) (#426)."""
    p = list(positions)
    return [p[0], p[1], p[3], p[2]] if len(p) == 4 else p


def newell_normal(poly) -> tuple[float, float, float]:
    nx = ny = nz = 0.0
    n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        nx += (a[1] - b[1]) * (a[2] + b[2])
        ny += (a[2] - b[2]) * (a[0] + b[0])
        nz += (a[0] - b[0]) * (a[1] + b[1])
    m = math.sqrt(nx * nx + ny * ny + nz * nz)
    return (nx / m, ny / m, nz / m) if m else (0.0, 0.0, 0.0)


def point_in_polygon_xz(px: float, pz: float, poly) -> bool:
    """Ray cast on the XZ projection."""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        zi, zj = poly[i][2], poly[j][2]
        if (zi > pz) != (zj > pz):
            xi, xj = poly[i][0], poly[j][0]
            if px < (xj - xi) * (pz - zi) / (zj - zi) + xi:
                inside = not inside
        j = i
    return inside


def floor_coverage(rings, size_x: int, size_z: int) -> dict[tuple[int, int], list[int]]:
    """``{(x, z): [ring index, ...]}`` -- probe516's bucket rule, verbatim.

    A floor-like polygon (``|ny| >= FLOOR_COS``) covers its centroid's tile and
    every tile inside its XZ bounding box whose centre it contains.
    """
    bucket: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, poly in enumerate(rings):
        if abs(newell_normal(poly)[1]) < FLOOR_COS:
            continue
        cx = sum(v[0] for v in poly) / len(poly)
        cz = sum(v[2] for v in poly) / len(poly)
        tx, tz = int(cx // TILE_UNITS), int(cz // TILE_UNITS)
        bx0 = int(min(v[0] for v in poly) // TILE_UNITS)
        bx1 = int(max(v[0] for v in poly) // TILE_UNITS)
        bz0 = int(min(v[2] for v in poly) // TILE_UNITS)
        bz1 = int(max(v[2] for v in poly) // TILE_UNITS)
        for gx in range(max(bx0, 0), min(bx1, size_x - 1) + 1):
            for gz in range(max(bz0, 0), min(bz1, size_z - 1) + 1):
                if (gx, gz) == (tx, tz) or point_in_polygon_xz(
                        gx * TILE_UNITS + TILE_UNITS / 2,
                        gz * TILE_UNITS + TILE_UNITS / 2, poly):
                    bucket[(gx, gz)].append(index)
    return dict(bucket)


def floor_bottoms(rings, size_x: int, size_z: int) -> dict[tuple[int, int], int]:
    """``{(x, z): bottom}`` in world Y, one entry per floor-covered tile."""
    return {key: -max(v[1] for i in indices for v in rings[i])
            for key, indices in floor_coverage(rings, size_x, size_z).items()}


def floor_steps(rings, size_x: int, size_z: int) -> dict[tuple[int, int], int]:
    """``{(x, z): step}`` -- the tile's floor expressed in terrain height points."""
    return {key: int(round(bottom / HEIGHT_STEP))
            for key, bottom in floor_bottoms(rings, size_x, size_z).items()}


def drifted(base_rings, document_rings, size_x: int, size_z: int) -> dict[tuple[int, int], tuple[int, int]]:
    """``{(x, z): (step_now, base_step)}`` -- decision 15's population.

    The tiles whose live floor no longer sits at the base's step. A tile no
    floor covers any more is *not* drifted: there is nothing to compare, so
    there is nothing to warn about (``authoring.drifted``, same rule).
    """
    base = floor_steps(base_rings, size_x, size_z)
    now = floor_bottoms(document_rings, size_x, size_z)
    out = {}
    for key, step in base.items():
        bottom = now.get(key)
        if bottom is None:
            continue
        step_now = int(round(bottom / HEIGHT_STEP))
        if step_now != step:
            out[key] = (step_now, step)
    return out
