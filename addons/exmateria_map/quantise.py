"""The quantiser's `bpy`-free core, and the COLOUR chart that measures it.

ADR-0007: the quantiser is a layer **above** `paint.resolve()`, not a branch
inside it. `resolve()` is scoped to one CLUT row and stays exact-match; this
module's job is to arrange that fewer colours need refusing.

This module imports `bpy` **never** -- only the stdlib. That is ADR-0005
decision 2 as ADR-0007 decision 4 applies it, and it is what keeps the core
testable under plain `pytest`. The panel and the operator live in a `bpy`
sibling.

## Why a lattice comes first

A CLUT entry is a BGR555 word, so a channel carries five bits and expands
back to eight as ``c8 = c5 * 255 // 31``. Only 32 of the 256 byte values are
reachable per channel and only 32,768 of the 16.7M sRGB colours in total
(0.195%). That is the **unrepresentable** half of `CONTEXT.md`'s pair: no
palette decision can help a colour off this lattice, and the remedy is to
snap it. The **unreferenced** half -- on the lattice, absent from *this* row
-- is a palette decision, and which one is a choice about fidelity.

Conflating the two measures the format's bit depth as though it were palette
scarcity, which is why every report this module produces separates them.
"""

#: The 32 byte values a BGR555 channel can expand to, ascending. 224 of the
#: 256 byte values do not survive the 8 -> 5 -> 8 round trip.
LATTICE = tuple(c * 255 // 31 for c in range(32))


def snap(rgb):
    """The nearest colour the format can hold -- one channel at a time.

    `(c8 * 31 + 127) // 255` is the disc-side quantise
    (`exmateria_map/document.py`), and it is round-to-NEAREST rather than a
    truncation: the lattice steps by 255/31 = 8.23, so truncating puts a byte
    up to eight off when four is available.
    """
    return tuple(LATTICE[(c * 31 + 127) // 255] for c in rgb)


def on_lattice(rgb):
    """Can a **CLUT row** hold this colour at all? If not it is
    **unrepresentable**, and the only remedy is `snap`."""
    return snap(rgb) == tuple(rgb)


#: The two reasons a painted colour is not a legal texel (`CONTEXT.md`).
UNREPRESENTABLE = "unrepresentable"
UNREFERENCED = "unreferenced"


def refusal_kind(rgb, entries):
    """Why `resolve()` refused this colour, or `None` if it did not.

    The order is not cosmetic. A colour off the lattice is
    **unrepresentable** even when its `snap` lands on an entry the row
    already holds, because the remedy is the snap and no palette decision
    reaches it. Calling that one **unreferenced** is exactly the conflation
    `CONTEXT.md` warns about, and it reports the format's bit depth as though
    it were palette scarcity.
    """
    rgb = tuple(rgb)
    if not on_lattice(rgb):
        return UNREPRESENTABLE
    if rgb in set(tuple(e) for e in entries):
        return None
    return UNREFERENCED


def partition(colours, entries):
    """`{"resolved": n, "unreferenced": n, "unrepresentable": n}` over a bag
    of painted colours. The three add up to the bag: nothing is dropped."""
    out = {"resolved": 0, UNREFERENCED: 0, UNREPRESENTABLE: 0}
    accept = set(tuple(e) for e in entries)
    for rgb in colours:
        rgb = tuple(rgb)
        if not on_lattice(rgb):
            out[UNREPRESENTABLE] += 1
        elif rgb in accept:
            out["resolved"] += 1
        else:
            out[UNREFERENCED] += 1
    return out


# ---------------------------------------------------------------------------
# The colour chart.
# ---------------------------------------------------------------------------

#: Every colour a CLUT entry can hold: 32 levels cubed. One texel each is
#: 12.5% of a 256x1024 sheet, and the whole sheet is exactly eight copies.
#: A 24-bit colour chart instead of this one refuses 99.8% of its pixels to an
#: integer division -- a spectacular red number that measures bit depth and
#: says nothing about palettes.
FULL_GAMUT = 32 ** 3

#: The off-gamut band's red channel: each lattice value pushed to the
#: MIDPOINT of its gap, which is off the lattice by construction (the gaps
#: are 8 or 9 wide). 255 has no gap above it, so it moves down instead.
#: The map is injective, so the band inherits the cube order's distinctness.
_BAND_RED = tuple(v + 4 if v != 255 else v - 4 for v in LATTICE)


def _reverse15(i):
    out = 0
    for _ in range(15):
        out = (out << 1) | (i & 1)
        i >>= 1
    return out


def _cube_point(i):
    """The i-th lattice colour, in an order whose every PREFIX is spread over
    the cube rather than clustered in one corner.

    The index is bit-reversed and then read as an MSB-first Morton code, so
    the coarsest bit of each channel varies fastest: the first eight are the
    eight octant corners, the first 64 a 4x4x4 grid, and every power of two
    an exact uniform sub-grid. A plain enumeration would give a scaled run
    1,000 neighbours of black and call it a sample of colour space.
    """
    code = _reverse15(i)
    r = g = b = 0
    for k in range(5):
        triple = (code >> (12 - 3 * k)) & 7
        r = (r << 1) | ((triple >> 2) & 1)
        g = (g << 1) | ((triple >> 1) & 1)
        b = (b << 1) | (triple & 1)
    return LATTICE[r], LATTICE[g], LATTICE[b]


def colour_chart(colours=FULL_GAMUT, off_gamut=0, texels=None):
    """`texels` RGB triples: `colours` distinct on-lattice colours, then
    `off_gamut` distinct colours BETWEEN lattice points, then the sequence
    repeating until the sheet is full.

    The two dials move independently on purpose. The distinct colour count
    is what `paint._gate` costs are a function of; the texel count is what
    the resolve's diff loop costs are a function of. A scaled run that could
    not move one without the other could not tell the two curves apart.

    The band is not decoration. Its job is to prove the run can tell
    **unrepresentable** from **unreferenced** (ADR-0007 decision 5): without
    it, an implementation that lumps both into one bucket passes.
    """
    if colours > FULL_GAMUT:
        raise ValueError(
            f"{colours} colours, but a BGR555 CLUT entry reaches only "
            f"{FULL_GAMUT}; the rest are unrepresentable by the format")
    seq = [_cube_point(i) for i in range(colours)]
    if off_gamut > FULL_GAMUT:
        raise ValueError(f"{off_gamut} off-gamut colours, but the band is "
                         f"one per lattice point and there are {FULL_GAMUT}")
    for i in range(off_gamut):
        r, g, b = _cube_point(i)
        seq.append((_BAND_RED[LATTICE.index(r)], g, b))
    if texels is None:
        return seq
    if texels < len(seq):
        raise ValueError(
            f"{len(seq)} colours will not fit in {texels} texels; returning "
            f"the first {texels} would report the wrong dial")
    out = []
    while len(out) < texels:
        out.extend(seq[:texels - len(out)])
    return out


# ---------------------------------------------------------------------------
# Phase (b): the quantiser, and the bar it has to clear.
# ---------------------------------------------------------------------------

def naive_palette(split=(4, 2, 2)):
    """Sixteen entries from a uniform subdivision of the RGB cube -- `split`
    cells per channel, the midpoint of each cell, snapped to the lattice.

    This is the BASELINE, and its whole virtue is that it can be computed
    without looking at the image. A quantiser that cannot beat it is not
    doing anything an artist could not have done by hand, and beating it is
    a bar that stays meaningful when the algorithm changes -- unlike an
    absolute error figure, which would encode today's algorithm as the
    specification.
    """
    nr, ng, nb = split
    if nr * ng * nb != 16:
        raise ValueError(f"{split} is {nr * ng * nb} cells, not the 16 a "
                         f"CLUT row holds")
    mid = lambda i, n: (2 * i + 1) * 256 // (2 * n)      # noqa: E731
    return [snap((mid(i, nr), mid(j, ng), mid(k, nb)))
            for i in range(nr) for j in range(ng) for k in range(nb)]


def _nearest(rgb, entries):
    """`(index, squared distance)` of the closest entry, in BYTE space --
    the space the CLUT carries and the space the artist chose the colour in
    (`paint.py`'s module docstring). Lowest index on a tie, matching §3.5."""
    best_i, best_d = 0, None
    for i, e in enumerate(entries):
        d = ((rgb[0] - e[0]) ** 2 + (rgb[1] - e[1]) ** 2
             + (rgb[2] - e[2]) ** 2)
        if best_d is None or d < best_d:
            best_i, best_d = i, d
            if d == 0:
                break
    return best_i, best_d


def nearest(rgb, entries):
    """The index a texel of this colour resolves to -- the compile's own
    resolve step (ADR-0186 decision 15), and the same rule `error` scores
    against, so a compiled sheet and its reported error cannot disagree about
    which entry a colour went to."""
    return _nearest(rgb, entries)[0]


def error(counts, entries):
    """Count-weighted mean squared distance from each colour to its NEAREST
    entry. The referee for "beats the baseline", and nothing more: no
    absolute value of it is ever asserted."""
    total = sum(counts.values())
    if not total:
        return 0.0
    return sum(n * _nearest(rgb, entries)[1]
               for rgb, n in counts.items()) / total


#: Refinement passes after the median cut. Bounded and fixed, because a
#: convergence test on a SNAPPED centroid can oscillate between two lattice
#: points forever, and because a quantiser whose cost depends on the image
#: is a quantiser an artist waits an unpredictable time for. Measured to
#: converge by the fourth pass on every bag tried; eight is the margin.
LLOYD_PASSES = 8


def _extent(items):
    lo = [min(c[ch] for c, _ in items) for ch in range(3)]
    hi = [max(c[ch] for c, _ in items) for ch in range(3)]
    return [hi[ch] - lo[ch] for ch in range(3)]


def _centroid(items):
    total = sum(n for _, n in items)
    return snap(tuple((sum(c[ch] * n for c, n in items) + total // 2) // total
                      for ch in range(3)))


def _median_cut(items, k):
    """Split the cube into `k` boxes, always cutting the box that costs the
    most -- population times its longest side -- at its weighted median.

    Median cut rather than a uniform grid because the whole point is to
    spend entries where the image actually is; and it is the initialiser
    rather than the answer because a box centroid is not a local optimum.
    """
    boxes = [list(items)]
    while len(boxes) < k:
        best, best_cost = None, 0
        for i, box in enumerate(boxes):
            if len(box) < 2:
                continue
            cost = sum(n for _, n in box) * max(_extent(box))
            if cost > best_cost:
                best, best_cost = i, cost
        if best is None:
            break                       # every box is a single colour
        box = boxes[best]
        axis = _extent(box).index(max(_extent(box)))
        box.sort(key=lambda it: (it[0][axis], it[0]))
        half = sum(n for _, n in box) / 2.0
        acc, cut = 0, 1
        for j, (_, n) in enumerate(box):
            acc += n
            if acc >= half:
                cut = min(max(j + 1, 1), len(box) - 1)
                break
        boxes[best:best + 1] = [box[:cut], box[cut:]]
    return boxes


def refine(entries, counts):
    """One Lloyd step: reassign every colour to its nearest entry, move each
    entry to the centroid of what landed on it, snap back onto the lattice.
    An entry nothing landed on stays put.

    Public because the step is **not monotone** -- snapping a moved centroid
    can land it further from its own cluster than the entry it replaced -- and
    a caller that cannot run one step cannot see that. Measured: 61 of 3,000
    randomly weighted bags have a pass that raises the error. That is why
    `quantise` returns the best palette it SAW rather than the last one.
    """
    buckets = [[] for _ in entries]
    for rgb, n in counts.items():
        buckets[_nearest(tuple(rgb), entries)[0]].append((tuple(rgb), n))
    return [_centroid(b) if b else entries[i]
            for i, b in enumerate(buckets)]


def quantise(counts, k=16, passes=LLOYD_PASSES):
    """`k` lattice colours chosen for this bag of painted colours.

    `counts` maps an RGB byte triple to how many texels carry it. The result
    is a **CLUT row**: every entry is a colour the format can hold, and there
    are exactly `k` of them even when the bag has fewer distinct colours --
    duplicate entries within one 16-set are legal, and `resolve()` takes the
    lowest index on a duplicate (§3.5).

    Median cut for the initial boxes, then `passes` rounds of `refine`. The
    best palette SEEN is returned, not the last one, because `refine` is not
    monotone -- see its docstring. `passes=0` is the median cut alone, which
    is a real answer and the one to compare the refinement against.

    The bag is read in sorted order, so two callers that walked the sheet
    differently get the same row.
    """
    items = sorted((tuple(c), n) for c, n in counts.items() if n > 0)
    if not items:
        return []

    entries = [_centroid(b) for b in _median_cut(items, k) if b]
    best, best_err = list(entries), error(counts, entries)
    ordered = dict(items)                 # sorted, so `refine` is order-free
    for _ in range(passes):
        moved = refine(entries, ordered)
        if moved == entries:
            break
        entries = moved
        err = error(counts, entries)
        if err < best_err:
            best, best_err = list(entries), err

    while len(best) < k:                # §3.5: a duplicate entry is legal
        best.append(best[-1])
    return best[:k]
