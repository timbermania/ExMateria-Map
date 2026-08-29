"""The compile: a **Painting** and a row binding become a **Sheet**.

ADR-0186 decision 15 separates it into a **search** and a **fit**, and says
plainly that there are not two compiles -- there is one compile and one thing
that moves its input:

* `compile_sheet(polygons, art)` is the **fit**.  Histogram each row's group
  off the Painting, quantise to sixteen lattice colours, nearest-index every
  texel.  The binding is held: `palette_id` is read, never written.
* `select_binding(polygons, art)` is the **search**.  It moves the binding and
  returns a new `palette_id` per polygon; recompiling under it is the caller's
  next call, not a second kind of compile.

The binding needs no schema of its own.  It IS `palette_id`, which already
dumps, builds and pushes -- decision 15 again.

Three rules this module exists to hold:

* **The chart is the atom, and the search may never split one** (decision 3).
  A chart never straddles a row before or after, so moving one moves no texel
  the artist painted and the UV layout is invariant under re-grouping.  The
  moment the atom can be split, that invariant is gone.
* **Never seed the search from the incumbent** (decision 8).  Measured on
  MAP022 a0: seeding scores **44.41**, not seeding **34.68**, on the same
  scorer.  The incumbent is run as a CANDIDATE and the winner is the minimum
  over candidates, which is how "never lose to what the disc already ships" is
  bought -- by selection, not by seeding.
* **Regressions are reported per chart, never ruled on per chart**
  (decision 9).  A chart's error depends on the pooled bag of the row it is
  in, so a per-chart RULE has no fixed point: moving one chart back
  invalidates every other comparison.  Rule per map; report per chart.

`bpy`-free, like `charts.py`, `pack.py`, `islands.py`, `convert.py` and
`quantise.py` (ADR-0007 decision 4).
"""
import collections

# Imported two ways, so the import has to work two ways -- see `convert.py`'s
# note.  Inside Blender these are submodules; under plain `pytest` the addon
# directory is on `sys.path` and they are top-level.
try:
    from .charts import charts
    from .quantise import error, nearest, quantise, snap
except ImportError:
    from charts import charts
    from quantise import error, nearest, quantise, snap

__all__ = ["compile_sheet", "select_binding", "score_and_palettes",
           "snapped", "measure", "regressions", "texel_addresses",
           "held_and_free", "Compiled", "Selection", "ROWS"]

SHEET_W, SHEET_H = 256, 1024
PAGE = 256
ROWS = 16

#: Passes of the reassign/requantise loop.  Bounded and fixed for
#: `quantise.LLOYD_PASSES`'s reason: the trace is not monotone, so a
#: convergence test buys nothing that taking the minimum over candidates does
#: not already buy, and an artist should not wait an image-dependent time.
SEARCH_PASSES = 6


#: What the compile produced.  `binding` is the `palette_id` per polygon it
#: compiled UNDER -- echoed back so a caller never has to re-derive it -- and
#: `chart_error` is parallel to `atoms`, which is decision 9's report.
Compiled = collections.namedtuple(
    "Compiled", "palettes indices binding atoms chart_error error texels")

#: What the search chose.  `binding` is a `palette_id` per polygon;
#: `is_incumbent` is True when the incumbent won, which decision 8 makes a
#: legitimate outcome and the Consequences require be SAID rather than left
#: looking like a button that did nothing.
Selection = collections.namedtuple(
    "Selection", "binding atoms error incumbent_error scored is_incumbent")


def texel_addresses(polygon):
    """The flat sheet addresses one polygon reads, row-major.

    The UV rectangle, not the triangle: that is what `islands.py` packs and
    what `convert.py` blits, so the compile has to claim the same set or a
    texel inside an island's box would end up with no row and no index.
    """
    us = [c[0] for c in polygon["uv"]]
    vs = [c[1] for c in polygon["uv"]]
    base = polygon["texture_page"] * PAGE
    return [(base + v) * SHEET_W + u
            for v in range(min(vs), max(vs) + 1)
            for u in range(min(us), max(us) + 1)]


def atom_texels(polygons, atom):
    """Every texel one chart reads, each one once.

    De-duplicated because a chart's own polygons may overlap -- 37.1% of the
    overlap inside a chart is a FOLD (Amendment 2) -- and a texel counted
    twice would weight that fold's colour twice in the histogram the row is
    quantised from.
    """
    seen = {}
    for i in atom:
        for t in texel_addresses(polygons[i]):
            seen[t] = True
    return list(seen)


def histogram(art, addresses):
    """`{(r, g, b): texels}` for a set of addresses of the Painting."""
    counts = {}
    for t in addresses:
        at = 3 * t
        c = (art[at], art[at + 1], art[at + 2])
        counts[c] = counts.get(c, 0) + 1
    return counts


def snapped(bag):
    """One histogram, folded onto the lattice -- the SEARCH's bag.

    A CLUT row is sixteen BGR555 words, so it cannot hold an off-lattice
    colour at all: ranking bindings on the true painting asks the quantiser to
    tell apart differences no row could express (ADR-0186 Amendment 7,
    decision 31).  Snapping first is therefore the better-posed question and
    not a shortcut, and it caps the bag at the lattice's 32,768 colours where
    a painted canvas measured 136,133.

    It folds the histogram rather than the Painting: `snap` runs once per
    DISTINCT colour a chart reads instead of once per texel, and the counts
    add the same way either order.  The **fit** (`compile_sheet`) is untouched
    and still runs on the true painting -- what ships is a picture of what was
    painted, and only the ranking happens on the lattice.
    """
    out = {}
    for c, n in bag.items():
        k = snap(c)
        out[k] = out.get(k, 0) + n
    return out


def _pool(bags, members):
    out = {}
    for i in members:
        for c, n in bags[i].items():
            out[c] = out.get(c, 0) + n
    return out


def _atoms_of(polygons, atoms):
    return [list(a) for a in (charts(polygons) if atoms is None else atoms)]


def _row_palettes(bags, rows, incumbent):
    """One quantised CLUT row per group, and what to do with an empty one.

    A row no chart is bound to reads nothing, so any sixteen lattice colours
    are legal there -- but the incumbent's own row is the least surprising
    answer and the one that leaves a `dump`ed map's unused rows where they
    were, so it is kept when the caller hands one over.
    """
    out = []
    for r in range(ROWS):
        members = [i for i, row in enumerate(rows) if row == r]
        pooled = _pool(bags, members) if members else None
        if pooled:
            out.append(quantise(pooled, ROWS))
        elif incumbent and r < len(incumbent):
            out.append([snap(tuple(c)) for c in incumbent[r]][:ROWS])
        else:
            out.append([(0, 0, 0)] * ROWS)
    return out


def compile_sheet(polygons, art, atoms=None, incumbent=None):
    """The fit: `(Painting, binding)` -> palettes and a Sheet.

    `art` is the Painting, three RGB bytes per texel in `convert()`'s layout.
    `atoms` is the chart partition; it defaults to `charts(polygons)` and is
    accepted explicitly because a caller that has just MOVED the binding must
    keep the atoms it scored, not re-derive them -- `charts` cuts at a
    `palette_id` change, so re-deriving after a re-bind gives a different,
    finer partition.  `incumbent` is the disc's own sixteen rows, used only
    for rows nothing is bound to.

    Nothing here is a decision about the layout: no UV moves, no island is
    re-placed, and the Sheet that comes out pictures exactly the layout the
    polygons already have.  That is what makes it a cache (decision 13) rather
    than a step the artist has to keep in sync.
    """
    atoms = _atoms_of(polygons, atoms)
    rows = [polygons[a[0]]["palette_id"] for a in atoms]
    addresses = [atom_texels(polygons, a) for a in atoms]
    bags = [histogram(art, t) for t in addresses]
    palettes = _row_palettes(bags, rows, incumbent)

    indices = bytearray(SHEET_W * SHEET_H)
    chart_error, weights = [], []
    for i, addr in enumerate(addresses):
        entries = palettes[rows[i]]
        lut = {}
        for t in addr:
            at = 3 * t
            c = (art[at], art[at + 1], art[at + 2])
            j = lut.get(c)
            if j is None:
                j = lut[c] = nearest(c, entries)
            indices[t] = j
        chart_error.append(error(bags[i], entries))
        weights.append(sum(bags[i].values()))

    total = sum(weights)
    mean = (sum(e * w for e, w in zip(chart_error, weights)) / total
            if total else 0.0)
    binding = [None] * len(polygons)
    for i, atom in enumerate(atoms):
        for m in atom:
            binding[m] = rows[i]
    return Compiled(palettes, bytes(indices), binding, atoms,
                    chart_error, mean, total)


def measure(polygons, art, palettes, atoms=None):
    """Score palettes the compile did NOT choose -- `(chart_error, mean)`.

    Decision 9's report needs a BEFORE, and the before is the sixteen rows
    already on the map: what the artist is looking at right now, scored
    against the Painting they have since edited.  It is deliberately the same
    `quantise.error` the compile minimises, so the two numbers in one report
    line are the same measurement of two palettes and not two measurements.
    """
    atoms = _atoms_of(polygons, atoms)
    rows = [polygons[a[0]]["palette_id"] for a in atoms]
    bags = [histogram(art, atom_texels(polygons, a)) for a in atoms]
    per = [error(bags[i], palettes[rows[i]]) for i in range(len(atoms))]
    weights = [sum(b.values()) for b in bags]
    total = sum(weights)
    return per, (sum(e * w for e, w in zip(per, weights)) / total
                 if total else 0.0)


def score_and_palettes(bags, rows):
    """The objective, and the sixteen rows it implies, from ONE quantise pass.

    `(count-weighted mean squared error of the whole map, {row: entries})`.
    The error is the only thing selection compares; the palettes are what the
    next pass needs to let every chart pick the row that fits it best.

    They are returned together because they are the same sixteen `quantise`
    calls (ADR-0186 Amendment 7, decision 31).  Scoring a binding and then
    re-quantising the same rows from the same members ran the quantiser twice
    over identical input -- provably a refactor to fuse, and worth 23 % of a
    search that `cProfile` puts almost entirely inside `quantise._nearest`.

    A row nothing is bound to is absent rather than empty: it reads nothing,
    so it has no colours to be scored on and is not a row a chart may move to.
    """
    total = err = 0
    palettes = {}
    for r in range(ROWS):
        members = [i for i, row in enumerate(rows) if row == r]
        if not members:
            continue
        pooled = _pool(bags, members)
        entries = quantise(pooled, ROWS)
        palettes[r] = entries
        n = sum(pooled.values())
        err += error(pooled, entries) * n
        total += n
    return (err / total if total else 0.0), palettes


def held_and_free(incumbent, animated):
    """Split the search's freedom by the map's `0x6c` table (decision 49).

    `animated` is the set of CLUT rows this map's animation instruction table
    drives -- `live_link.animation_rows` reads it off the base resource, and
    it is EMPTY for most maps, which is what makes this a no-op there.

    An animated row is not a colour the search may spend.  A polygon bound to
    one does not show the sixteen colours the compile wrote there: the engine
    repaints that row out of the `0x70` frames several times a second, so what
    it shows is the cycle, whatever was quantised underneath it.  The scorer
    cannot see that -- it ranks on colour error alone -- and MAP022 measures
    what follows: rows 14 and 15 are animated and EMPTY on the disc, so they
    look free, and one painted canvas put 152 of 385 polygons onto animated
    rows.  The artist's report is exactly that picture: *"a bunch of polygons
    would turn blue and shimmer like water"*.

    So the rule is **the search never changes whether a chart is animated**,
    and it is one rule in both directions:

    * A chart the disc put on an animated row is **held**.  It keeps the row
      it has, which keeps the cycle it has -- and the animated rows are not
      interchangeable either, because each record carries its own frames, so
      moving water from row 13 to row 14 would swap one animation for another.
    * Every other chart searches over the **un**animated rows only.

    This refuses nothing the map already ships and needs no new authoring
    surface, which is what makes it the smaller answer than a row the artist
    marks: `build` carries `0x6c` and `0x70` verbatim (schema §8), so the
    addon cannot say "this row is no longer animated" and a fix that assumed
    it could would be proposing a document member.  What it costs is the
    ability to move water off its row by search; that is an authoring act, and
    `palette_id` is still the artist's to set.

    Returns `(movable, free_rows)` -- the chart indices the search may move,
    and the rows it may move them to.  With no animation that is every chart
    and all sixteen rows, which is today's search unchanged.
    """
    animated = set(animated or ())
    free_rows = [r for r in range(ROWS) if r not in animated]
    movable = [i for i, r in enumerate(incumbent) if r not in animated]
    return movable, free_rows


def _seed(bags, movable, free_rows, incumbent):
    """The starting assignment -- and NOT the incumbent (decision 8).

    Charts are ordered by their mean colour, green first, and cut into equal
    runs over the rows the search may use.  Any spread-out start does; what
    matters is that it is not the binding being compared against, because a
    search that starts there measured 44.41 against 34.68 for one that does
    not.

    A chart the animation holds (`held_and_free`) starts on its incumbent row,
    because that is the only row it may ever be on -- the seed is a place for
    the search to start, and a chart that cannot move has nowhere else.
    """
    def key(i):
        n = sum(bags[i].values()) or 1
        return tuple(sum(c[ch] * k for c, k in bags[i].items()) / n
                     for ch in (1, 0, 2))

    rows = list(incumbent)
    order = sorted(movable, key=key)
    for place, i in enumerate(order):
        rows[i] = free_rows[place * len(free_rows) // len(order)]
    return rows


def select_binding(polygons, art, atoms=None, passes=SEARCH_PASSES,
                   animated=None):
    """The search: which CLUT row each chart should read through (decision 8).

    The candidate set is the incumbent plus every pass of the clusterer, and
    the winner is the minimum total error over that set.  That is what makes
    the clusterer's non-monotone trace irrelevant -- the output is a minimum
    over candidates, not a last pass -- and what buys "never lose to what the
    disc already ships" without seeding from it.

    `animated` is the CLUT rows this map's `0x6c` table drives, and it bounds
    the search rather than scoring it -- see `held_and_free` for why an
    animated row is not a colour the search may spend.  It defaults to nothing
    animated, which is most of the corpus and is today's search exactly.

    Returns a `Selection`.  `is_incumbent` is a real outcome, not a failure:
    the disc's own binding can be the best one, and a caller must say so
    rather than report a search that looks like it did nothing.
    """
    atoms = _atoms_of(polygons, atoms)
    bags = [snapped(histogram(art, atom_texels(polygons, a))) for a in atoms]

    incumbent = [polygons[a[0]]["palette_id"] for a in atoms]
    best = list(incumbent)
    best_err = incumbent_err = score_and_palettes(bags, incumbent)[0]
    scored = 1

    movable, free_rows = held_and_free(incumbent, animated)
    free = set(free_rows)
    if not movable or not free_rows:
        # Nothing the search may move: the incumbent is the whole candidate
        # set, and that is a RESULT (decision 8), not a refusal.
        binding = [None] * len(polygons)
        for i, atom in enumerate(atoms):
            for m in atom:
                binding[m] = best[i]
        return Selection(binding, atoms, best_err, incumbent_err, scored, True)

    rows = _seed(bags, movable, free_rows, incumbent)
    for _ in range(passes + 1):
        # Scoring this binding ALREADY quantised every row off what is in it
        # now, and that is exactly what the next step needs to let every chart
        # pick the row that fits it best -- so it comes back with the score
        # rather than being computed a second time (decision 31).  A chart
        # moves whole or not at all.
        err, palettes = score_and_palettes(bags, rows)
        scored += 1
        if err < best_err:
            best, best_err = list(rows), err
        moved = list(rows)
        for i in movable:
            pick, pick_err = None, None
            for r, entries in palettes.items():
                # An animated row may hold members -- the charts held on it --
                # so it is quantised and scored like any other.  It is just not
                # a row a movable chart may be offered.
                if r not in free:
                    continue
                e = error(bags[i], entries)
                if pick_err is None or e < pick_err:
                    pick, pick_err = r, e
            moved[i] = pick if pick is not None else rows[i]
        if moved == rows:
            break
        rows = moved

    binding = [None] * len(polygons)
    for i, atom in enumerate(atoms):
        for m in atom:
            binding[m] = best[i]
    return Selection(binding, atoms, best_err, incumbent_err, scored,
                     best == incumbent)


def regressions(before, after, atoms, limit=None):
    """Decision 9's report: every chart whose error ROSE, worst first.

    A global mean can improve while one corner of the mesh gets visibly worse,
    so the compile names them.  It never rules on them: `before` and `after`
    are measured under two different pooled bags, so "move this one back" is
    not a move that can be evaluated on its own.
    """
    out = [(i, before[i], after[i], len(atoms[i]))
           for i in range(min(len(before), len(after)))
           if after[i] > before[i]]
    out.sort(key=lambda e: e[2] - e[1], reverse=True)
    return out if limit is None else out[:limit]
