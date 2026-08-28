"""The region of the sheet a polygon owns.

ADR-0186 Amendment 6 decision 22: an island is a **polygon**, not a chart nor
a chart's UV-connected piece.  Every textured polygon gets its own rectangle
of the sheet, so after a conversion **no texel is read by two polygons**.

That supersedes Amendment 1's hull-vs-union split rule.  Amendment 1 was
reaching for the same property one level up -- a chart the disc scattered has
a hull that is almost entirely air, so copying the hull put 76 of 147
resources over the sheet -- and it stopped at the chart because Amendment 2
believed making a folded chart manifold "needs a real per-chart unwrap, which
resamples".  It does not.  An unwrap resamples; a **copy** does not.  Two
polygons reading one rectangle each get their own verbatim copy of it, the
copies are byte-identical the instant they are made, and the conversion stays
an integer blit and exactly lossless.  The only thing the split spends is
sheet area, and `workspace/island_split_cost.py` measured that at +1.5pp of
the sheet at the median and 0 refusals of 147.

A chart may own many islands.  `chart` survives on every island because
decision 3 re-groups by it -- a chart is never split between CLUT rows -- but
it is no longer the unit of *placement*.

`bpy`-free, like `charts.py` and `pack.py` (ADR-0007 decision 4).
"""

# Imported two ways, so the import has to work two ways.  Inside Blender this
# is a submodule of the `exmateria_map` package and the import must be
# relative; under plain `pytest` the addon directory is on `sys.path` and the
# siblings are top-level modules, because `exmateria_map/__init__` imports
# `bpy` and cannot be loaded at all.  A `bpy`-gated module ships green under
# pytest -- that is what this idiom is for.
try:
    from .charts import charts
except ImportError:
    from charts import charts

__all__ = ["islands", "charts"]


def _box(polygon):
    """The half-open texel rectangle a polygon reads: (x0, y0, x1, y1)."""
    us = [c[0] for c in polygon["uv"]]
    vs = [c[1] for c in polygon["uv"]]
    return min(us), min(vs), max(us) + 1, max(vs) + 1


def islands(polygons):
    """The islands `polygons` owns, in chart order.

    One per textured polygon.  Each is a dict: `chart` (index into
    `charts(polygons)`), `members` (a single-element list of polygon
    indices), `page`, `source` (x, y) on that page, `size` (w, h).

    `members` stays a list even at one element: decision 23 consolidates
    islands whose content and CLUT row are identical, and a consolidated
    island carries several.
    """
    found = []
    for c, members in enumerate(charts(polygons)):
        for m in members:
            x0, y0, x1, y1 = _box(polygons[m])
            found.append({"chart": c, "members": [m],
                          "page": polygons[m]["texture_page"],
                          "source": (x0, y0), "size": (x1 - x0, y1 - y0)})
    return found
