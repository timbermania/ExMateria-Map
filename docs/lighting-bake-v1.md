# The lighting bake — solving FFT shading data from a Blender lighting setup

Design record for the addon's **bake**: the artist lights a map with ordinary
Blender lamps, judges it by eye, and presses one button; the addon solves
backwards for the FFT data that reproduces what they set up — the per-corner
normals and the 45-byte light rig — and writes them to the disc through the
interchange. Modern lighting in, retro lighting out.

Resolved by grilling on 2026-08-25, against ADR-0004 decisions 3, 4, 7, 19, 24
and 25. Every figure below is produced by
[`workspace/measure_bake.py`](../workspace/measure_bake.py) over the disc corpus;
re-run it rather than trusting this page.

**This document decides the bake. It does not decide the rig's ownership** —
that was an ADR-0004 amendment, filed as its own decision ticket on
[#517](https://github.com/timbermania/fft-monorepo/issues/517) and **resolved as
decision 27** ([#558](https://github.com/timbermania/fft-monorepo/issues/558)):
the rig is authored, declared per map state by the presence of a new field beside
`map_states[].light_rig`, riding `version: 2`. §8 states what that amendment had
to say; it says it.

**Build state.** The **normals half is built** — §11 records the authoring
decisions §1–§10 left open, and §9 carries the measured result.

The rig half is now built in **two pieces, and only one of them is here**:

- **The WRITE PATH is built** (decision 27). A rig Override is promoted on export
  into `map_states[].authored_light_rig`, the document stamps `version: 2`, and
  `build` writes the 45 bytes at pointer `0x64` — refusing a texture row by kind,
  a chunkless mesh row, and a gradient that is not the state's own. Graded end to
  end by `tests/blender_authored_rig.py`. [#576](https://github.com/timbermania/fft-monorepo/issues/576)
  landed with it: `read_light_rig` now takes the resource's *kind*, so the four
  MAP062 a0 texture rows stop reading a rig out of sheet pixels — which is
  exactly the predicate "which states can receive bytes" needs, and it had to be
  right before any byte reached a disc.
- **The SOLVE is not built.** §5's alternating least squares — deriving the 21
  numbers from the Blender lamps — and §6's re-solve of the other states are
  still design. Today the artist types them into decision 25's rig Override and
  export promotes what they typed. The pipe is finished; §5 fills it.

## 1. Why an inverse

The forward surface is 21 numbers per map state — ambient, three gains, three
directions — plus one normal per polygon corner, and the corners are the larger
half by four orders of magnitude. Decision 25 made the 21 editable as an
**Override**, and typing into them is exactly as slow as it sounds. Nothing makes
the corners editable at all, and nothing sensibly could: a mesh in this corpus
carries thousands of them, and #358 measured that they are not a surface
description anyone could derive — **85.3%** of textured polygons carry differing
corner normals and Blender's own smooth average reproduces only **75.5%**.

They are authored shading data. So the useful control is not a slider per corner;
it is a lighting rig the artist already knows how to drive, and a solve.

## 2. What the format can hold, measured

The shading model is `indexed_color.gdshader`'s, per decision 24:
`texture x (ambient + diffuse)`, with `diffuse = sum_i gain_i * max(0, N . L_i)`
and **each light clamped independently** (shader lines 147-149), baked per
corner because that is the PSX GTE's sample rate (`lighting_per_vertex = true`;
decision 24 rules the per-pixel branch *"the measured-wrong branch"*).

Write `diffuse_c = A[c,:] . N` with `A[c,:] = sum_i gain_i[c] * L_i` — the model
is linear in the normal inside any one clamp region, and `A` is a sum of three
outer products, so its rank is bounded by the number of lights carrying a
non-zero gain:

| lights with a non-zero gain | resources | |
|---|---|---|
| 0 | 11 | 6.83% |
| 2 | 124 | **77.02%** |
| 3 | 26 | 16.15% |

Over the 161 rig-bearing geometry-bearing resources, **`rank(A) = 3` is reachable
on at most 16.15%**. On the rest the achievable diffuse colours are a plane, and
the pre-image of a target colour is a line rather than a point.

That is not an ill-posedness to be rescued. There is no ground-truth geometric
normal the solver could be wrong about — decision 3 already establishes these are
authored shading data, and #358's 85.3% / 75.5% prices the alternative. A
line of solutions is a **family of equally valid answers**; §4 picks one by an
explicit criterion.

What the rank does constrain is far more useful to know:

| | median | p90 | max |
|---|---|---|---|
| **chroma** spread reachable by choosing the normal | **8.15 deg** | 23.65 | 72.30 |
| **peak diffuse luma** reachable | 2.019 | 3.940 | 11.054 |

**A normal is a brightness dial, not a colour dial.** Whatever normal you pick,
the diffuse lands inside roughly an 8-degree cone in RGB. Occlusion, contact
shadow and falloff — luminance phenomena — bake into normals essentially
losslessly. A *coloured* gradient across the mesh (warm key, cool fill) cannot be
held by any normal set and must come out of the rig, which is global to the mesh.

The endpoints belong to the rig too, over 273,128 shipped corners:

| | |
|---|---|
| corners in the **dark cap** (diffuse exactly 0 — the normal changes nothing) | **13.23%** |
| corners **saturating** for a white texel (`ambient + diffuse >= 1` everywhere) | **34.38%** |
| ambient floor as a share of the lit ceiling (luma) | median **0.102** |

`ambient` is the floor a corner falls to when it faces away from every light, and
the gains are the ceiling. **The rig owns both endpoints; the normals decide where
each corner lands between them.** A third of corners are already clamped at the
top, exert no pull on any fit, and must not be moved by it (§4).

## 3. The target is corner darkness

The bake reads one **scalar** per corner — the luma of the lighting Blender's
lamps deliver there — not an RGB triple.

- It is evaluated **analytically per corner**, never read back from a render. A
  render is per pixel and camera-dependent, and Cycles' bounce and specular have
  no FFT counterpart, so a rendered target would carry information the format
  provably cannot hold.
- It is the **lighting term against a white albedo** — `map_light_debug` mode 4,
  the quantity `import_document.bake_light()` already writes into the `diffuse`
  CORNER `FLOAT_COLOR` attribute. Fitting the final textured pixel would mean
  dividing out albedo, which is unstable on dark texels and, worse,
  **state-dependent**: each map state carries its own 16-entry CLUT, so §6's
  re-solve of the other states would inherit a different division per state.
- It is **scalar because chroma is not the normal's to give** (§2, 8.15 deg). Fitting
  three components to a one-dimensional capability invites a solver to trade real
  brightness accuracy for imaginary colour accuracy.

The residual the artist *reads* is albedo-weighted, because that is the error they
can see. Same fit, honest reporting, one multiply.

## 4. The normal solve is closed form

With a scalar target, `luma(diffuse(N)) = v . N` where `v = sum_{i in S} luma(gain_i) * L_i`
for the active clamp set `S`. One linear equation plus `|N| = 1` cuts a **circle**
out of the unit sphere. Every normal on that circle renders the target exactly.

The tie-break is **minimum change from the corner's existing ROM normal**, and the
nearest point on a circle has a closed form: the component along `v` is pinned by
the target, the perpendicular component points wherever the old normal already
did. Per corner, independent, O(1), no iteration.

The per-light clamp is handled by enumerating active sets — at most 7 non-empty —
solving each in closed form and discarding any whose clamp assumption its own
answer violates.

Minimum-change is close to forced, for a reason that outranks taste:

- **It makes the identity case exact.** Bake with the lighting unchanged and the
  target *is* the current picture, so the nearest point on the circle to the old
  normal is the old normal. Normals come back byte-identical and the round-trip
  instrument stays at 148/148 EXACT. Smoothness or nearest-to-geometry would both
  drift the file on every idle re-bake — and nearest-to-geometry fights the format,
  which spent effort pointing 79.7% of its normals more than 15 degrees away from it.
- **It disposes of the clamped 34.38% for free.** A corner the target cannot pull
  does not move. Any other regulariser would shove a third of the mesh around in
  service of a target with no opinion about it, and report "converged".

Exercised on real bytes — scramble every sampled normal, then solve it back to its
own darkness, 6,127 corners across the corpus:

| | |
|---|---|
| luminance error | median **0.000000**, **max 0.000000** /255 |
| corners hit exactly | **6,127 — 100.00%** |
| **infeasible** (no normal renders the target) | **0** |
| angle between the recovered normal and the ROM's | median **16.37 deg**, max 107.70 |
| chroma error | median 0.00 deg, p99 14.52 |

Corners are hit **exactly, not approximately** — the answer lands on the target
rather than converging toward it, so there is no convergence oracle to build and
no local minimum to escape.

**This table read 97.01% and 183 infeasible until #559 was built, and the 2.99%
was this page's own solver, not the format.** The per-light clamp makes the model
piecewise, so a set's circle is only the truth inside that set's own region; taking
each set's unconstrained nearest point and *discarding* it when it leaves the
region throws away every answer that sits on the region's **boundary**. Those
boundary points are closed form too — where the circle crosses each light's
terminator plane, two more per light per set — and with them the same experiment
recovers every corner. Two consequences worth stating plainly:

- The target here is read off a **real normal**, so a solution provably exists at
  every corner and any "infeasible" was always a search failure by construction.
  A negative result about the format has to be taken with an instrument that
  searches the whole of it.
- The **empty** active set is not representable in `luma_vectors` at all — its `v`
  is the zero vector — so §2's 13.23% dark cap could not answer "stay where you
  are" and was pushed onto some light's terminator instead. That renders
  identically, which is exactly why a luma-error metric never saw it, and it moves
  the bytes: 954 of 1,922 corners on MAP005 a0.

Genuine infeasibility still exists and still means what §7 says — it is what
happens when the **artist's lamps** ask for more light than the gains can deliver,
which is a different population from this one and a real message to the rig.

Storage costs nothing visible. Taking a continuous solved normal to the on-disc
i16 triple, 6,127 samples: angular error median **0.0054 deg** (max 0.0116),
shading error median **0.0131/255**, max **0.3228**, and **0 of 6,127 at or above
half a display level** — matching decision 25's finding for directions.

**Created faces** have no ROM normal to be near; they seed from the smooth normal
of their own geometry, and the export leg's new-face defaults table gains a row.

## 5. The rig solve

Fixing the normals and solving for the rig is the overdetermined half: 18 free
parameters (3 ambient + 9 gains + 6 direction DOF) against 3 residuals per corner
over thousands of corners, and **linear in the gains and the ambient** for fixed
directions. Alternating least squares — linear solve for gains and ambient, a
nonlinear step on the 6 direction DOF — is the shape.

Two constraints inherited rather than chosen:

- Gains are an **unclamped float triple in Godot's uniform units**, capped at
  16.06 (the i16 ceiling), with no colour/strength decomposition — decision 25
  measured the split is not injective, so two artists would store different bytes
  for one picture.
- Directions are solved and stored as a **Blender-space unit vector**. Decision 25
  measured `spherical_to_cartesian . vector_to_sphere` is not the identity
  (it returns `(-x, y, -z)`), so the angle pair both references serialise is a
  trap; and every direction magnitude in the corpus is within 2 LSB of 4096, so
  length carries nothing.

## 6. One bake touches every state of the arrangement

Geometry rides the arrangement — 148 of 148, zero exceptions (decision 4, #365) —
while the rig is per map state. So the normals are shared by every state and the
rig is not, and the sharing is not rare:

| distinct rigs on a geometry-bearing arrangement | arrangements | |
|---|---|---|
| 1 | 50 | 34.97% |
| 2 | 4 | 2.80% |
| 3 | 20 | 13.99% |
| 4 | 11 | 7.69% |
| 5 | 10 | 6.99% |
| 6 | 2 | 1.40% |
| **10** | **46** | **32.17%** |
| **more than one** | **93** | **65.03%** |

MAP005 arrangement 0 is the shape: twenty state rows, ten distinct rigs — a
day/night by five-weather table someone hand-authored, day/none warm at ambient
`[82,50,46]`, night/none cool at `[48,74,84]`, heavy rain flattened to `[48,48,48]`
with the key dimmed more than threefold — and **exactly one resource carrying
geometry**. Ten pictures, one normal set.

So a bake on day/clear lands its rig on one row and its normals under all ten.
**The other states have their rigs re-solved**, each keeping its own current
picture as its target, so it reproduces what it looked like as closely as the
format allows. That fit is the cheap overdetermined one from §5, and it converts a
silent side effect into a line in the report.

Two limits on that:

- It **writes rigs on states the artist never opened**. Decision 27 permits that
  explicitly, and requires every state the bake touched to be named in its report.
- It **cannot create a rig where the resource holds none**, and the population is far smaller
  than this page first said. The original text joined **two different
  populations** with an "and" — *"38.5% of states carry no rig of their own
  (decision 25) **and** a resource whose `0x64` pointer is `0` has no 45-byte chunk
  to overwrite"* — when only the second is the writer's. Decision 25's 38.5% was
  measured for the **panel**, which lists every non-pad row and must render all of
  them; a writer asks a different question. Measured
  (`workspace/measure_rig_write.py`) over the 1,370 non-pad rows:

  | population | count | |
  |---|---|---|
  | **mesh rows carrying a `0x64` chunk — the write population** | **717** | 52.3% |
  | texture rows, which cannot hold a rig **by kind** | 640 | 46.7% |
  | **mesh rows with `0x64 == 0` — the real cannot-write set** | **13** | 1.8% of mesh rows |

  So 13 rows, not 691 states: `MAP001.11`, `MAP006.31`, `MAP012.10`, `MAP014.9`,
  `MAP041.5`, `MAP041.11`, `MAP053.10`, `MAP053.19`, `MAP053.22`, `MAP061.9`,
  `MAP083.10`, `MAP083.38`, `MAP096.17`. Creating the chunk is the byte decision 19
  forbids the writer to manufacture, and decision 26 lifted that refusal for `0xB0`
  **alone**, so they are skipped and reported with the normals half still running.
  **Ask which population an instrument was built for before reusing its rate.**
- **Five arrangements carry no rig anywhere at all** — MAP041 a1, MAP041 a2,
  MAP053 a1, MAP053 a2, MAP083 a1 — and are a named check arm, the case where the
  rig solve must do nothing and say so rather than divide by an empty bearer set.
- Rig-less states keep borrowing under decision 25's `(night, weather)` key match
  and inherit whatever their partner's re-solve produced — so a state can move
  without having been fitted, and the report says so.

## 7. The residual report

Two numbers, never one:

- **Brightness residual** — the normals could not reach it. Actionable: the fit
  landed short, and §4 names which corners.
- **Chroma residual** — the format cannot hold it. Not actionable at all: an ~8
  degree gamut is flattening the artist's warm/cool gradient to one hue, and no
  amount of fiddling changes that.

Collapsing them into one RMS would hide the difference and send the artist chasing
the unfixable half. Infeasible corners (§4) are reported as their own category with
the rig change that would fix them, because that is what they mean.

**The bake never refuses.** The export leg's palette gate refuses because an
off-palette pixel produces bytes that *cannot be written*; an imperfect light fit
produces perfectly valid bytes and is an aesthetic judgement the artist is looking
directly at. Refusing there would apply the house style to the wrong class of
failure.

## 8. Ownership, schema, and what this needs from ADR-0004

The two halves have very different legal standing.

**The normals half needs nothing.** Decision 3 puts normals in **Blender's**
column; `polygons[].normals` is an authored schema-v1 field; `build` writes it;
and [`interchange-export-v1.md`](interchange-export-v1.md) §2 already specifies
`normals <- corner attribute -> inverse map -> i16`. Solved normals reach the disc
the moment the export operator exists — no ADR change, no schema change.

**The rig half needed an amendment, and got one — ADR-0004 decision 27.** What
follows is the argument as it stood before that decision; it is left as written
because decision 27 rests on it. Decision 3 puts the 45-byte rig in the **Base
map** column, narrowed by #370 to *"the jobs Blender cannot do at all"*; schema
§7.1 marks `light_rig` *"derived, information-bearing, `build` ignores it"*; and
decision 25's scope line reads *"the preview only. Nothing is written back;
decision 3's ownership table is untouched."*

None of that is about difficulty. #478 already ran the write: `put_dir_lights` /
`put_amb_light_rgb` / `put_background` over chunk `0x64` is byte-exact on **776 of
776** resources — one of the three stages that scored 100%, because the 45 bytes are
exactly what their counts describe. What blocks it is a decision.

The amendment should take the shape #438 already established for terrain: a
**bounded slice** moves into Blender's column, gated on an explicit declaration —
here, a state carrying an **Override**. Every other state stays carried byte for
byte, so an untouched document declares nothing and the 148/148 EXACT identity
round trip is unaffected. That is the argument decision 3 has accepted once
already.

**The authored rig rides `version: 2`, not a new optional key on v1.** Schema §2's
refusal rule exists so a `build` that does not understand a field refuses rather
than guesses. A v1 `build` handed a document with an authored rig would ignore it
— §7.1 says so — and emit a map that silently dropped the artist's lighting. That
is precisely what the refusal rule was written to prevent, and a version bump is
what makes it fire.

## 9. Checks, and the defect each one catches

Every check ships with the defect it catches, seeded and re-run. All three assert
on **the picture, never on the normals**: §4 measures a median 16.37 degrees between two
normals that render identically, so a test demanding the original normals back
would fail a solver that was entirely correct — the shape of `axis 2 asserts
nothing`. Tolerance is **1/255 worst channel**, above the 0.3228 quantisation
ceiling and below anything visible.

| check | asserts | seed |
|---|---|---|
| **Fixed point** | bake with the lighting unchanged returns normals and rig **byte-identical**, and the corpus stays 148/148 EXACT | perturb the minimum-change tie-break (§4) and watch the corpus go red |
| **Recovery** | scramble the normals, re-solve against the map's own render, the render returns within tolerance | disable active-set enumeration (§4) and watch the clamped 34.38% drag it out |

Measured, all three green: **148/148** arrangements byte-identical, **790/790**
recovered at a worst error of 0.000000/255 with 0 unreachable, and a hue gradient
reported as a **34.94 deg** median chroma while brightness stays at **0.000/255**.
`tests/blender_bake.py` runs them; `tests/bake_mutation_audit.py` seeds **nine**
defects into the shipped solve one at a time in a scratch copy and grades which
checks go red — 9/9 caught, none blind. One prediction above was wrong: disabling
active-set enumeration is caught by the **fixed point**, not by recovery.
| **Honest residual** | a target with per-corner hue variation wider than the gamut is reported as **chroma** error, not as a small combined RMS | collapse the two-part residual into one number and watch it fail |

The recovery check must scramble and assert on the *render*; a check that seeded by
mutating shared code would move both arms together and pass on unfixed code.

## 10. Accepted limits

- **Faces are linearised.** The corners are the format's sample rate, so the
  interior of a polygon is linear interpolation between its corners. A lamp that
  curves *inside* a face — a point lamp close to a large polygon, a shadow edge
  crossing a face's middle — cannot be held. More samples do not fix that; only
  more geometry does, which is a different feature. It goes in the report, not in a
  subdivision operator.
- **Chroma is the rig's, globally.** §2. Per-corner hue is unreachable by
  construction.
- **The fit is not albedo-weighted.** §3 keeps the solve state-independent, which
  §6's re-solve needs, at the cost of spending equal effort on corners the artist
  cannot see.
- **A borrowing state can move without being fitted.** §6.

## 11. The authoring viewport

> **Amended by ADR-0004 decision 30 ([#559]).** Three clauses below are superseded.
> *"A bake is a pure function of the lamps"* was false as written — the zero-lamp case
> early-returned without writing, so deleting every lamp froze the map instead of
> unlighting it. *"The bake IS the preview"* survives, but `Live` and the `Bake` button
> are replaced by a single **Lamp authority** switch. And the lamps the solve reads are
> now scoped to **the map's collection**, not the whole scene. *"Lamps arrive only when
> asked"* is unchanged: switching authority on does not seed them.

§1–§10 specify the solve and never say how the artist *sees* the lamps they are
solving from, and there was no mechanism at all: the preview material is
`ShaderNodeEmission → ShaderNodeOutputMaterial`, unlit by construction, because
that is how decision 24 reproduces PSX Gouraud faithfully — a material graph
cannot do it. So a Sun lamp changed nothing, in all six `DEBUG_MODES`. Resolved by
grilling on 2026-08-25; the eleven answers below are the design, and each one's
reason is the part worth keeping.

**The bake is the preview.** No live-lamp viewport. Press *Bake to FFT* and the
existing mode-0 preview re-bakes from the **solved** normals, so what is on screen
is the bytes that will be written. A live analytic preview was rejected for the
same reason §3 rejects a rendered target: it would show the artist the **target**,
and target and fit disagree on every corner the format cannot hold — §2's 13.23%
dark cap, its 34.38% already saturating, and §4's genuinely infeasible. Measured
at ~19 ms on the corpus's largest mesh (MAP096 a0, 3,307 corners), so pressing it
repeatedly *is* the loop.

**Lamps arrive only when asked**, through a *Seed Lamps from Rig* button.
Seeding on every import would put three lamps into every scene the render-graded
checks build, which is the by-construction guarantee decision 25 said "stops
holding without anyone noticing". Note this is **not** the shape the rig itself
now takes: the rig is exposed on every state without asking, because exposing a
number changes no picture, while seeding a lamp adds an object that does.

**All three lamps, even the dead one.** §2 measured a dead third slot on 77.02% of
rig-bearing resources. It seeds at strength 0 keeping the ROM's direction, and
white, because `colour = gain / max(gain)` is undefined at zero. A dead light is an
empty slot the format holds open, not an absence; hiding it leaves the artist no
way to know a third light exists. Named `FFT light 1/2/3` — the ROM has no notion
of key or fill, and naming them so would assert a fact the data does not carry.

**Shadows off on a seeded lamp.** The FFT rig is three infinite directional lights
and casts none, so a seeded lamp with Blender's default shadows on would darken
every occluded corner the instant the artist touched the map — and the fixed point
could never pass. The first shadow enabled is then a deliberate, visible act.

**Every lamp type, with falloff and ray-cast shadows**, honouring Blender's own
per-lamp `use_shadow`. Suns alone would make the feature nearly pointless: FFT's
rig *is* three suns, so the only thing worth authoring is what three suns cannot
say — a torch pooling light on a wall, a pillar's shadow across a floor. §2 already
names occlusion, contact shadow and falloff as the phenomena normals hold
losslessly. Measured at 0.91 µs a ray, 9.1 ms for three lamps over the largest
mesh. The world background is ignored: ambient is the rig's, not a lamp's.

**Blender's own units, unscaled.** A Sun's strength is an irradiance; point, spot
and area are watts falling off as `1/(4πd²)`. On a map 280 units across — one tile
is 28 (`TILE_UNITS`) and the importer scales nothing — a default 1000 W lamp reads
about 0.00001. Rescaling distances into tiles was rejected as a made-up 28× fudge
inside the maths, decision 24's "third structure matching neither reference" again;
instead the report **names every lamp's peak contribution**, which turns an
invisible failure into a number. The 19 ms loop is what makes that survivable.

**Lighting is evaluated against the SHADING normal.** #358 measured 79.7% of ROM
normals pointing more than 15° off the geometry, so they are a normal map and
lighting a normal map means using it as the receiver. This is what lets a torch add
its pool *on top of* the artist's existing shading rather than flattening the
original authorship away — and it is what makes seed → bake exact.

**Every bake starts from the ROM's normals**, never from the last bake's output,
both as that receiver and as §4's tie-break. A bake is then a pure function of the
lamps: nudge one and back again and the file returns exactly, and two artists with
the same scene get the same bytes. Chaining from the live normals would make the
result depend on the *route* taken.

**An out-of-reach corner takes the nearest reachable brightness**, not its ROM
value. Leaving it would make turning a lamp up **snap** a patch of the map back to
its original value the moment the target crossed the ceiling — a discontinuity that
reads as a defect rather than as a limit.

**Aimed at the state on screen.** §6: 65.03% of geometry-bearing arrangements carry
more than one rig and 32.17% carry ten, all sharing one normal set. The bake solves
for the previewed state because that is the picture being judged, and **names every
other state that moves** — decision 27 requires exactly that. The five arrangements
with no rig anywhere do nothing and say so; the bake never refuses (§7).

**Zero-length ROM normals are left untouched.** `bake_light` renders a zero-length
normal as diffuse 0, so lighting one through its face geometry would contradict the
very preview the bake is judged by, and would rewrite a byte the disc holds at zero
on purpose. The `imported` face attribute separates them from a face the artist
created, which genuinely has no data yet and does seed from geometry. Measured:
they were the whole of the fixed point's remaining failure, 30 arrangements.

**Where this lives.** In this document, not in ADR-0004. Those decisions are about
who owns which bytes, and decision 27 already moved the only boundary at issue;
nothing above moves another. The one thing that needed recording elsewhere is the
lamps' *kind*: decision 25's three-way split of what an object carries — document
data, view state, Override — cannot classify them, so `CONTEXT.md` gains
**authoring input** as a fourth term, separated from an Override not by provenance
but by direction of travel. An Override *is* the stored thing; a lamp is only ever
consumed by a solve.

## Re-running the measurements

```bash
cd exmateria-map/workspace
EXMATERIA_ASSETS_DIR=/path/to/project-assets python3 measure_bake.py
```

Six measurements, one pass, deterministic, stdlib only, a few minutes over the
full corpus. An uncommitted measurement is not reproducible; every figure on this
page comes from that script.

[#559]: https://github.com/timbermania/fft-monorepo/issues/559
