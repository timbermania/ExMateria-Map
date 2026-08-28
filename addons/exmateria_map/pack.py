"""Placing islands into the sheet's four 256x256 texture pages.

ADR-0186 Amendment 6 decision 22 gives every textured POLYGON an island --
decision 2 said every chart, Amendment 1 every UV-connected piece of one --
and this is where the islands go.  The budget is the format's own sheet:
131,072 bytes of 4bpp indices, four 256x256 pages, and nothing else to spend.

**Gutter 0.**  The PSX GPU point-samples: no filtering, no mipmaps, so a
texel's neighbour never bleeds into it.  The shipped data relies on this --
every one of the 147 textured resources abuts polygon UV boxes at zero gutter
and 141 of 147 abut across CLUT rows (`workspace/gutter.py`).  Islands
therefore abut exactly, and a gutter here would be a capacity loss the format
does not ask for: it is the difference between 84.1% and 98.9% of the sheet.

`bpy`-free, like `quantise.py` and `charts.py` (ADR-0007 decision 4).
"""

PAGE = 256
PAGES = 4


class PackRefusal(RuntimeError):
    """The islands do not fit the sheet, and the compile will not pretend.

    ADR-0186 decision 11: never auto-scale.  Auto-scaling always produces a
    map, which is exactly `build`'s reason for refusing rather than writing a
    bad patch -- it would work, produce a legal sheet, and never be able to
    say it had given up.
    """


def _refuse(islands, unplaced, capacity):
    needed = sum(w * h for w, h in islands)
    over = needed - capacity
    biggest = sorted(set(islands), key=lambda wh: -wh[0] * wh[1])[:3]
    named = ", ".join(f"{w}x{h}" for w, h in biggest)
    if over > 0:
        why = (f"{needed:,} texels of island into a {capacity:,}-texel sheet, "
               f"over by {over:,}")
    else:
        why = (f"{needed:,} texels of island fit a {capacity:,}-texel sheet on "
               f"area, but {len(unplaced)} island(s) could not be placed: the "
               f"pack fragmented")
    raise PackRefusal(f"{why}; the largest islands are {named}")


def _split(free, x, y, w, h):
    """Replace every free rect the placed box cuts with the pieces left over.

    MaxRects: a free rectangle is not a shelf, so the space ABOVE a placed box
    survives as its own candidate.  That is the whole difference from the
    shelf packer -- which loses it, and with it the exact tilings.
    """
    out = []
    for fx, fy, fw, fh in free:
        if x >= fx + fw or x + w <= fx or y >= fy + fh or y + h <= fy:
            out.append((fx, fy, fw, fh))
            continue
        if x > fx:
            out.append((fx, fy, x - fx, fh))
        if x + w < fx + fw:
            out.append((x + w, fy, fx + fw - (x + w), fh))
        if y > fy:
            out.append((fx, fy, fw, y - fy))
        if y + h < fy + fh:
            out.append((fx, y + h, fw, fy + fh - (y + h)))

    pruned = []
    for i, a in enumerate(out):
        if not any(i != j and _contains(b, a) for j, b in enumerate(out)):
            pruned.append(a)
    return pruned


def _contains(outer, inner):
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    if (ox, oy, ow, oh) == (ix, iy, iw, ih):
        return False
    return (ox <= ix and oy <= iy
            and ox + ow >= ix + iw and oy + oh >= iy + ih)


def pack(islands, pages=PAGES, size=PAGE):
    """Place `islands`, a sequence of `(width, height)` in texels.

    Returns a list of `(page, x, y)` parallel to `islands`.  Placements abut
    with no gutter and never overlap.  Raises `PackRefusal` if they do not
    fit -- never scales anything down (ADR-0186 decision 11).
    """
    free = {page: [(0, 0, size, size)] for page in range(pages)}
    placements = [None] * len(islands)

    # Biggest first: a big island placed late has to find a hole that the
    # small ones have already cut up.
    order = sorted(range(len(islands)),
                   key=lambda i: (-islands[i][0] * islands[i][1],
                                  -max(islands[i])))
    for i in order:
        w, h = islands[i]
        best = None
        for page in range(pages):
            for fx, fy, fw, fh in free[page]:
                if fw < w or fh < h:
                    continue
                # Best-short-side-fit: leave the squarest offcut, which is
                # what a later island is most likely to be able to use.
                short = min(fw - w, fh - h)
                long_ = max(fw - w, fh - h)
                if best is None or (short, long_) < best[0]:
                    best = ((short, long_), page, fx, fy)
        if best is None:
            continue
        _, page, x, y = best
        placements[i] = (page, x, y)
        free[page] = _split(free[page], x, y, w, h)

    unplaced = [i for i, at in enumerate(placements) if at is None]
    if unplaced:
        _refuse(islands, unplaced, pages * size * size)
    return placements
