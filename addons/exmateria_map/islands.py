"""The region of the sheet a chart owns.

ADR-0186 decision 2 gives every chart an island.  Amendment 1 measured what
that island may be: **not** the chart's shipped UV bounding box, which puts 76
of 147 resources over the sheet on area alone, because a chart the disc
scattered across a page has a hull that is almost entirely air (median 1.00x
its members' area, but p90 3.11x and max 624x).

So an island is a chart's UV-CONNECTED piece, and a piece is kept whole only
while its hull costs no more than its members do apart.  Every island is a
verbatim rectangle of texels the disc already ships, so a conversion is an
integer blit and is exactly lossless.  A chart may own several islands; they
move between CLUT rows together, which is what decision 3 actually requires.

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


def _touching(boxes):
    """Group boxes that overlap or abut.  -> list of lists of indices."""
    parent = list(range(len(boxes)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, (ax0, ay0, ax1, ay1) in enumerate(boxes):
        for j in range(i + 1, len(boxes)):
            bx0, by0, bx1, by1 = boxes[j]
            if ax0 <= bx1 and bx0 <= ax1 and ay0 <= by1 and by0 <= ay1:
                parent[find(i)] = find(j)

    groups = {}
    for i in range(len(boxes)):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def _union_area(boxes):
    """Exact area of a union of rectangles, by a sweep over x.

    NOT the sum of their areas: 16.14% of the disc's read texels have more
    than one polygon reader, so members of one chart overlap, and a sum
    double-counts the overlap.  The split rule below compares the hull
    against this -- a sum lets a hull with air in it pass, and that air is
    another chart's paint carried into this island.
    """
    xs = sorted({b[0] for b in boxes} | {b[2] for b in boxes})
    total = 0
    for x0, x1 in zip(xs, xs[1:]):
        spans = sorted((b[1], b[3]) for b in boxes
                       if b[0] <= x0 and b[2] >= x1)
        covered, end = 0, None
        for y0, y1 in spans:
            if end is None or y0 > end:
                covered += y1 - y0
                end = y1
            elif y1 > end:
                covered += y1 - end
                end = y1
        total += covered * (x1 - x0)
    return total


def _hull(boxes):
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def islands(polygons):
    """The islands `polygons` owns, in chart order.

    Each is a dict: `chart` (index into `charts(polygons)`), `members`
    (polygon indices), `page`, `source` (x, y) on that page, `size` (w, h).
    """
    found = []
    for c, members in enumerate(charts(polygons)):
        boxes = [_box(polygons[i]) for i in members]
        for group in _touching(boxes):
            piece = [boxes[g] for g in group]
            x0, y0, x1, y1 = _hull(piece)
            apart = _union_area(piece)
            if (x1 - x0) * (y1 - y0) <= apart:
                # The hull is exactly tiled by its members -- it costs no
                # more than they do apart and carries nothing they do not
                # read -- so keep the piece whole: larger islands pack
                # better and keep the chart's seams shared.
                chosen = [(sorted(members[g] for g in group),
                           (x0, y0), (x1 - x0, y1 - y0))]
            else:
                # A hull that is mostly air: split it back to its members.
                chosen = [([members[g]], (boxes[g][0], boxes[g][1]),
                           (boxes[g][2] - boxes[g][0],
                            boxes[g][3] - boxes[g][1]))
                          for g in group]
            for mem, source, size in chosen:
                found.append({"chart": c, "members": mem,
                              "page": polygons[mem[0]]["texture_page"],
                              "source": source, "size": size})
    return found
