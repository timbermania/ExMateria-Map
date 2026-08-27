"""The chart: a run of textured polygons welded across a shared mesh edge.

ADR-0186 decision 2 makes the chart the compile's ISLAND -- the region of the
sheet one run of surface owns -- and decision 3 makes it the re-grouping ATOM
the clusterer moves between CLUT rows but may never split.  Those two together
are what keeps the UV layout still while rows are reassigned.

`bpy`-free, like `quantise.py` and for the same reason (ADR-0007 decision 4).
"""


def ring(n):
    """doc corner order -> the PSX triangle-STRIP ring (0,1,3,2 for quads).

    The same rule as `import_document.ring`, restated rather than imported:
    that module needs `bpy` and this one must not.
    """
    if n == 4:
        return (0, 1, 3, 2)
    return tuple(range(n))


def charts(polygons):
    """Partition `polygons` into charts.  Returns a list of lists of indices
    INTO `polygons`, each inner list ascending and the outer list ordered by
    first member."""
    textured = [i for i, p in enumerate(polygons) if "uv" in p]

    edges = {}
    for i in textured:
        p = polygons[i]
        pos = [tuple(p["positions"][j]) for j in ring(len(p["positions"]))]
        for k in range(len(pos)):
            e = tuple(sorted((pos[k], pos[(k + 1) % len(pos)])))
            edges.setdefault(e, []).append(i)

    parent = list(range(len(polygons)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for sharers in edges.values():
        # Over every PAIR on the edge, not consecutive ones: a fold puts three
        # polygons on one edge, and the two that share a row must weld through
        # the one that does not.
        for n, a in enumerate(sharers):
            for b in sharers[n + 1:]:
                if (polygons[a]["palette_id"] == polygons[b]["palette_id"]
                        and polygons[a]["texture_page"]
                        == polygons[b]["texture_page"]):
                    parent[find(a)] = find(b)

    groups = {}
    for i in textured:
        groups.setdefault(find(i), []).append(i)
    return sorted(groups.values())
