"""The one-time act that makes a map authorable (ADR-0186 decision 7).

Converting a map unwraps it into islands, packs them, and rewrites the UVs to
match.  It is one-way, and it is **visually lossless**: every island is a copy
of the texels the chart already read, under the row it already named, so the
first compile is a no-op.  That is an exact claim, not an approximate one --
`tests/test_convert.py` checks it against the disc's own sheet.

What conversion does NOT do is decide anything about colour.  Every polygon
keeps its `palette_id`; only where its texels live changes.

`bpy`-free, like `charts.py`, `pack.py` and `islands.py` (ADR-0007 dec. 4).
"""

# Imported two ways, so the import has to work two ways.  Inside Blender this
# is a submodule of the `exmateria_map` package and the import must be
# relative; under plain `pytest` the addon directory is on `sys.path` and the
# siblings are top-level modules, because `exmateria_map/__init__` imports
# `bpy` and cannot be loaded at all.  A `bpy`-gated module ships green under
# pytest -- that is what this idiom is for.
try:
    from .charts import charts
    from .islands import islands
    from .pack import pack
except ImportError:
    from charts import charts
    from islands import islands
    from pack import pack

__all__ = ["convert", "repack", "clut_rows", "islands", "charts"]

SHEET_W = 256
PAGE = 256


def _rgb(entry):
    """`#RRGGBB` -> (r, g, b).  The document's own spelling (schema 6.4)."""
    h = entry.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def clut_rows(palettes):
    """A state's `palettes` -> 16 rows of 16 (r, g, b).

    `stp` is not colour -- it is the PSX's semi-transparency bit, carried by
    the document and meaningless in a painting -- so it is dropped here and
    the compile re-decides it.
    """
    if palettes and not isinstance(palettes[0], dict):
        return [list(row) for row in palettes]      # already resolved
    return [[_rgb(c) for c in row["colors"]] for row in palettes]


def convert(polygons, sheets, palettes):
    """Unwrap, pack, and BAKE `polygons` into true-colour source art.

    `sheets` is the disc's 4bpp index plane(s), one 0..15 value per texel,
    256x1024 row-major -- the v band of `texture_page` p is p*256..p*256+255.
    `palettes` is the matching state's 16 CLUT rows, the document's
    `map_states[].palettes` shape.  Both may be a single value or parallel
    sequences: a map has one mesh and several map STATES, and the UVs are
    rewritten once, so every state bakes under that same unwrap.

    Returns `(converted, art, moved)` -- copies of the polygons with `uv` and
    `texture_page` rewritten, the source art as **RGB bytes**, three per
    texel, and the disc's own index plane carried to the new layout.  The
    last two are the pair `CONTEXT.md` calls the **Painting** and the
    **Sheet**, and they are in the same 256x1024 layout as each other.

    The bake is what makes this the OTHER authoring path.  A texel's index is
    resolved through the CLUT row of the chart that reads it -- and a chart is
    cut at a `palette_id` change (decision 2), so every texel of an island
    resolves through exactly one row and the answer is never ambiguous.  What
    comes out carries no palette at all: it is an ordinary colour picture, the
    artist paints it with whatever they like, and the compile quantises it
    back down to sixteen colours a row.

    `moved` is why there is no third thing to keep in step (Amendment 3
    decision 14).  The Sheet pictures the UV layout it was compiled under, so
    a conversion that rewrites every UV and leaves it behind does not produce
    a STALE map -- it produces one whose mesh and sheet disagree, which is not
    a map at all.  It is carried through the same blit as the Painting rather
    than gated on afterwards, so the incoherence is unreachable instead of
    guarded.
    """
    one = isinstance(sheets, (bytes, bytearray))
    index_planes = [sheets] if one else list(sheets)
    rows = [clut_rows(palettes)] if one else [clut_rows(p) for p in palettes]
    if len(rows) != len(index_planes):
        raise ValueError(f"{len(index_planes)} sheet(s) but {len(rows)} "
                         f"palette set(s); a state's sheet and its CLUT are "
                         f"one pair")

    art = [bytearray(3 * len(plane)) for plane in index_planes]
    moved = [bytearray(len(plane)) for plane in index_planes]

    def bake(island, src, dst, w):
        row = polygons[island["members"][0]]["palette_id"]
        for plane, clut, out in zip(index_planes, rows, art):
            for k in range(w):
                r, g, b = clut[row][plane[src + k] & 0xF]
                at = 3 * (dst + k)
                out[at], out[at + 1], out[at + 2] = r, g, b

    converted = _replace(polygons, _each(bake, _carry_indices(index_planes,
                                                              moved)))
    baked = [bytes(a) for a in art]
    carried = [bytes(m) for m in moved]
    return (converted,
            baked[0] if one else baked,
            carried[0] if one else carried)


def repack(polygons, art, sheets):
    """Re-place every island of an ALREADY converted map (decision 10).

    Not a second `convert`: the compile has no inverse, so there is no going
    back to indices, and what moves here is the artist's own picture.  Every
    island is re-placed and none is ever resized, so every move is a whole
    number of texels and the painting is carried without being resampled.
    Incremental placement into free space is what this refuses -- it
    fragments, so a map that would fit under a fresh pack fails after enough
    edits, which is the worst kind of failure to diagnose.

    `art` is RGB, three bytes per texel, and `sheets` is the compiled index
    plane beside it -- the **Painting** and the **Sheet**, moved together for
    decision 14's reason.  Either may be one picture or several, and they are
    parallel.  Returns `(converted, art, moved)`, the same triple `convert`
    returns.
    """
    one = isinstance(art, (bytes, bytearray))
    sources = [art] if one else list(art)
    planes = ([sheets] if isinstance(sheets, (bytes, bytearray))
              else list(sheets))
    if len(planes) != len(sources):
        raise ValueError(f"{len(sources)} painting(s) but {len(planes)} "
                         f"sheet(s); a state's Painting and its Sheet are "
                         f"one pair")
    out = [bytearray(len(a)) for a in sources]
    moved = [bytearray(len(p)) for p in planes]

    def blit(island, src, dst, w):
        for original, shifted in zip(sources, out):
            shifted[3 * dst:3 * (dst + w)] = original[3 * src:3 * (src + w)]

    replaced = _replace(polygons, _each(blit, _carry_indices(planes, moved)))
    return (replaced,
            bytes(out[0]) if one else [bytes(o) for o in out],
            bytes(moved[0]) if one else [bytes(m) for m in moved])


def _carry_indices(planes, out):
    """The third carry (Amendment 3 decision 14): the raw 4bpp indices.

    Nothing is resolved and nothing is decided -- a texel's index arrives at
    its new address unchanged, which is the exact claim
    `test_every_polygon_reads_the_same_indices_after_a_convert` checks.  It is
    the stronger of the two carries: a bake resolves through a CLUT row, so
    two different indices naming the same colour would satisfy *zero colours
    moved*; nothing hides an index that moved.
    """
    def carry(island, src, dst, w):
        for plane, shifted in zip(planes, out):
            shifted[dst:dst + w] = plane[src:src + w]
    return carry


def _each(*carries):
    """Run several carries over one walk, so the Painting and the Sheet can
    never be moved by two different unwraps."""
    def carry(island, src, dst, w):
        for one in carries:
            one(island, src, dst, w)
    return carry


def _replace(polygons, carry):
    """The move both paths share: unwrap, pack, rewrite the UVs, and call
    `carry(island, src, dst, width)` for each row of each island."""
    found = islands(polygons)
    placements = pack([i["size"] for i in found])
    replaced = [dict(p) for p in polygons]

    for island, (page, dx, dy) in zip(found, placements):
        sx, sy = island["source"]
        w, h = island["size"]
        for line in range(h):
            carry(island,
                  ((island["page"] * PAGE + sy + line) * SHEET_W) + sx,
                  ((page * PAGE + dy + line) * SHEET_W) + dx,
                  w)
        for member in island["members"]:
            polygon = replaced[member]
            polygon["uv"] = [[u - sx + dx, v - sy + dy]
                             for u, v in polygon["uv"]]
            polygon["texture_page"] = page
    return replaced
