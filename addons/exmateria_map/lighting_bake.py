"""The lighting bake — solving FFT shading data from a Blender lighting setup.

Design: `../../docs/lighting-bake-v1.md`, whose figures come from
`../../workspace/measure_bake.py`.  This module is the NORMALS half: the artist
lights a map with ordinary Blender lamps, presses one button, and every corner
normal is re-solved so the map's OWN light rig reproduces what the lamps deliver.
The rig half (§5-§6) is not built here.

Three things about the shape, because none of them is obvious:

**Lighting is evaluated against the SHADING normal, not the geometric one.**
FFT corner normals are authored shading data, not a surface description --
#358 measured 79.7% of them pointing more than 15 degrees off the geometry and
85.3% of textured polygons carrying corners that disagree with each other.  So
they are a normal map, and lighting a normal map means using it as the receiver.
That is also what makes the feature usable: seeding the lamps from the map's own
rig reproduces the ROM picture EXACTLY, and adding a torch then adds its pool on
top of the artist's existing shading instead of flattening it away.  Evaluating
against the geometric normal would wipe the original authorship on the first
bake.

**Every bake starts from the ROM's normals** (`normals_shadow`), never from the
last bake's output -- both as the receiver above and as §4's minimum-change
tie-break.  That makes a bake a pure function of the lamps: nudge a lamp and
back again and you land exactly where you started, and two artists with the same
scene get the same bytes.  Chaining from the live normals would make the file
depend on the ROUTE the artist took, and would never converge on the corners
§4 cannot reach.

**The unit answer is rescaled by the corner's own magnitude.**  The `normals`
attribute holds the raw i16 triple, not a unit vector, and the disc's magnitudes
are 4095/4096/4097 rather than a constant.  Writing a unit vector -- or a fixed
4096 -- would move 9% of corners by an LSB and the fixed-point check could never
be byte-exact.  There is deliberately NO "if unchanged, write the original back"
shortcut: that would make the identity case pass without exercising the solve.
"""
import json
import math

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty
from mathutils import Vector
from mathutils.bvhtree import BVHTree

from .import_document import (GAIN_SCALE, _fft_to_blender, _mag, _unit,
                              bake_light, object_states, resolved_rig,
                              set_normals_edited)

# Rec.601, the same weights `workspace/measure_bake.py:42` measured the design's
# figures with.  Every luma on both sides of the solve goes through this.
LUMA = (0.299, 0.587, 0.114)

# The three rig slots, as scene objects.  Named rather than key/fill/back: the
# ROM carries no notion of a light's ROLE, and naming one "key" asserts a fact
# the data does not hold.
LAMP_NAME = "FFT light {}"
LAMP_TAG = "exmateria_map/rig_slot"      # found by FLAG, never by parsing a name

# A shadow ray starts off the surface along the face normal; below this the
# origin lands back on the face it left and every corner reads as occluded.
SHADOW_EPS = 1e-3
# Below this a gain contributes nothing and its slot counts as dead (§2's
# "lights with a non-zero gain" census).
GAIN_EPS = 1e-12
# When a candidate counts as rendering the target exactly.  Blender stores a
# lamp's `energy` and `color` as float32, so seeding a gain and reading it back
# as `colour x strength` cannot round-trip in float64: measured on MAP115 a0, a
# corner facing light 2 dead on got a target 2.9e-8 ABOVE what that light can
# physically deliver, its set was rejected as out of reach, and 18 corners moved
# for a luma error of 4e-8.  That is the precision floor of representing a rig
# as lamps, not slack in the solve -- 1e-6 in gain units is 0.00026 of a display
# level, three orders below the 0.3228/255 quantisation §4 already accepts, and
# `tests/bake_mutation_audit.py` seeds the tie-break to prove the fixed point
# still bites at this tolerance.
EXACT_EPS = 1e-6


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _luma(c):
    return LUMA[0] * c[0] + LUMA[1] * c[1] + LUMA[2] * c[2]


# ---------------------------------------------------------------------------
# The rig, as the solve sees it
# ---------------------------------------------------------------------------

def rig_frames(rig):
    """`(dirs, gains, live)` -- unit Blender-space directions, gain triples in
    Godot's uniform units, and the indices whose gain is non-zero.

    The unit conversions are `bake_light`'s, so the forward model here and the
    preview's bake are the same arithmetic: a gain is the raw i16 over 8 over
    255, a direction is the raw i16 triple normalised.  §2 measured 77.02% of
    rig-bearing resources carrying exactly TWO live lights and 6.83% none at
    all, so `live` is routinely shorter than three.
    """
    if not rig:
        return [], [], []
    gains = [[c / GAIN_SCALE for c in rig["colors"][i]] for i in range(3)]
    dirs = []
    for i in range(3):
        v = _fft_to_blender(tuple(float(c) for c in rig["directions"][i]))
        m = _mag(v) or 1.0
        dirs.append([c / m for c in v])
    live = [i for i in range(3) if _luma(gains[i]) > GAIN_EPS]
    return dirs, gains, live


def forward_rgb(n, dirs, gains, live):
    """The rig's diffuse at a normal -- decision 24's model, each light clamped
    independently (`indexed_color.gdshader:146-149`)."""
    acc = [0.0, 0.0, 0.0]
    for i in live:
        k = _dot(n, dirs[i])
        if k > 0.0:
            for c in range(3):
                acc[c] += gains[i][c] * k
    return acc


def forward_luma(n, dirs, glum, live):
    return sum(glum[i] * k for i in live if (k := _dot(n, dirs[i])) > 0.0)


def luma_vectors(dirs, glum, live):
    """Per active clamp set `S`, the `v` with `luma(diffuse(N)) == v . N` while
    exactly `S` is active.

    Enumerating `S` is what makes the per-light clamp tractable: inside one set
    the model is linear in the normal, and there are at most 7 non-empty sets.
    """
    out = {}
    for mask in range(1, 1 << len(live)):
        s = tuple(live[b] for b in range(len(live)) if mask & (1 << b))
        out[s] = [sum(glum[j] * dirs[j][k] for j in s) for k in range(3)]
    return out


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0]]


def solve_corner(target, n_start, dirs, gains, glum, live, vs):
    """The normal nearest `n_start` whose baked diffuse has luma `target`.

    `v . N = target` with `|N| = 1` cuts a CIRCLE out of the unit sphere, so the
    nearest point on it is closed form: the component along `v` is pinned by the
    target and the perpendicular component points wherever `n_start` already
    did.  Per corner, independent, no iteration and no convergence oracle.

    The per-light clamp makes the model piecewise, so the circle for an active
    set `S` is only the truth INSIDE `S`'s own region.  Taking each set's
    unconstrained nearest point and discarding it when it leaves the region --
    the shape `measure_bake.py` measured with -- throws away the cases where the
    real answer sits on the region's BOUNDARY, and reports them as unreachable.
    Measured on the recovery arm, where a solution provably exists because the
    target was read off a real normal: 24 of 790 corners on MAP000 a0, matching
    the 2.99% the design record attributes to targets "brighter than any normal
    reaches".  They are nothing of the kind.  So the boundary points are
    candidates too -- where the circle crosses each light's terminator plane,
    two more closed-form points per light per set.

    Every candidate is then scored on the TRUE piecewise model rather than the
    linearisation that produced it, which is what lets the clamp bookkeeping go
    away entirely: a candidate is kept because it renders the target, not
    because its own assumption survived.

    Returns `(normal, reached)`.  `reached` is about the OUTCOME.  When nothing
    renders the target -- the genuinely out-of-reach case, where the target is
    brighter than any normal extracts from these gains -- the nearest reachable
    normal is returned instead of leaving the corner where it was, so that
    turning a lamp up cannot SNAP a patch of the map back to its ROM value the
    moment the target crosses the ceiling.
    """
    # The EMPTY active set, which `luma_vectors` cannot carry: its `v` is the
    # zero vector, so `v . N = 0` holds for every normal and the nearest is
    # `n_start` itself.  §2 measured 13.23% of shipped corners in this dark cap.
    # Without it they are shoved onto some light's terminator plane, which
    # renders identically -- so a luma-error measurement never sees it -- but
    # moves the bytes, and the fixed point is byte-exact.
    if abs(target) <= 1e-12 and all(_dot(n_start, dirs[j]) <= 1e-9 for j in live):
        return list(n_start), True

    cands = [list(n_start)]
    for _s, v in vs.items():
        vn = math.sqrt(_dot(v, v))
        if vn < 1e-12:
            continue
        vh = [x / vn for x in v]
        if abs(target) > vn * (1.0 + 1e-6) + 1e-9:
            cands.append(list(vh))          # the brightest this set can do
            cands.append([-x for x in vh])
            continue
        # `a` is clamped rather than trusted: a target a float32 ULP past `vn`
        # is the same picture as one exactly at it, and rejecting it would send
        # a corner that is merely facing its light squarely down the
        # out-of-reach path.
        a = max(-1.0, min(1.0, target / vn))
        k = math.sqrt(max(0.0, 1 - a * a))
        e1 = _unit([n_start[c] - _dot(n_start, vh) * vh[c] for c in range(3)])
        if _mag(e1) < 1e-9:                 # n_start parallel to v: any perp
            e1 = _unit(_cross(vh, (0.0, 0.0, 1.0)))
            if _mag(e1) < 1e-9:
                e1 = _unit(_cross(vh, (0.0, 1.0, 0.0)))
        e2 = _unit(_cross(vh, e1))
        cands.append([a * vh[c] + k * e1[c] for c in range(3)])
        # Where this circle crosses light j's terminator: the region boundary.
        # `A cos t + B sin t = C` has a closed solution whenever |C| <= hypot.
        for j in live:
            A = k * _dot(e1, dirs[j])
            B = k * _dot(e2, dirs[j])
            c = -a * _dot(vh, dirs[j])
            r = math.hypot(A, B)
            if r < 1e-12 or abs(c) > r:
                continue
            base = math.atan2(B, A)
            off = math.acos(max(-1.0, min(1.0, c / r)))
            for t in (base + off, base - off):
                ct, st = math.cos(t), math.sin(t)
                cands.append([a * vh[q] + k * (ct * e1[q] + st * e2[q])
                              for q in range(3)])
    # A dark target the start does not already satisfy needs the corner driven
    # behind every terminator at once; that intersection is a cross product,
    # not a plane any of the circles above passes through.
    for i in range(len(live)):
        for j in range(i + 1, len(live)):
            c = _unit(_cross(dirs[live[i]], dirs[live[j]]))
            if _mag(c) > 1e-9:
                cands.append(list(c))
                cands.append([-x for x in c])

    cands = [c for c in cands if _mag(c) > 1e-9]
    exact = [c for c in cands
             if abs(forward_luma(c, dirs, glum, live) - target) <= EXACT_EPS]
    if exact:
        # minimum change, §4's tie-break: the largest dot with where it started
        return max(exact, key=lambda n: _dot(n, n_start)), True
    # `reached` is about the OUTCOME, not about which branch found it.
    pick = min(cands, key=lambda n: abs(forward_luma(n, dirs, glum, live) - target))
    return pick, abs(forward_luma(pick, dirs, glum, live) - target) <= EXACT_EPS


# ---------------------------------------------------------------------------
# Seeding the lamps (§ the authoring viewport)
# ---------------------------------------------------------------------------

def seed_lamps(ob, rig, collection=None):
    """Materialise the state's rig as three Sun lamps.  Returns them in order.

    All THREE, even though 77.02% of maps have a dead slot and 6.83% have three:
    a dead light is an empty slot the format is holding open, not an absence, and
    an artist who cannot see it has no way to know a third light is available.
    It arrives at strength 0, keeping the ROM's own direction, and white --
    `colour = gain / max(gain)` is undefined at zero, and white means turning the
    strength up gives a plain light rather than an arbitrary tint.

    **Shadows are off.**  The FFT rig is three infinite directional lights and
    casts none, so a seeded lamp with Blender's default shadows on would darken
    every occluded corner the instant the artist touched the map -- and the
    fixed-point check could never pass.  The first shadow the artist enables is
    then a deliberate, visible act.

    The split into `colour x strength` is not decision 25's refused
    decomposition: that refusal was about STORAGE ("two artists would store
    different bytes for one picture"), and nothing here is stored.  A lamp is an
    input to a solve, only the product is ever read, and the seeding's own
    direction is canonical anyway.
    """
    dirs, gains, _live = rig_frames(rig)
    if collection is None:
        collection = (ob.users_collection or [bpy.context.scene.collection])[0]
    co = [ob.matrix_world @ v.co for v in ob.data.vertices]
    if co:
        lo = Vector((min(c.x for c in co), min(c.y for c in co), min(c.z for c in co)))
        hi = Vector((max(c.x for c in co), max(c.y for c in co), max(c.z for c in co)))
        centre, radius = (lo + hi) * 0.5, max((hi - lo).length * 0.75, 1.0)
    else:
        centre, radius = Vector(), 10.0
    out = []
    for i in range(3):
        name = LAMP_NAME.format(i + 1)
        data = bpy.data.lights.new(name, "SUN")
        data.use_shadow = False
        peak = max(gains[i]) if gains else 0.0
        if peak > GAIN_EPS:
            data.color = tuple(c / peak for c in gains[i])
            data.energy = peak
        else:
            data.color = (1.0, 1.0, 1.0)
            data.energy = 0.0
        data.angle = 0.0                   # a hard sun: the rig has no penumbra
        lamp = bpy.data.objects.new(name, data)
        lamp[LAMP_TAG] = i
        collection.objects.link(lamp)
        # A sun emits down its local -Z, so point -Z along -L to face the map.
        d = Vector(dirs[i]) if dirs else Vector((0.0, 0.0, 1.0))
        if d.length < 1e-9:
            d = Vector((0.0, 0.0, 1.0))
        # Blender's DEFAULT rotation mode, deliberately.  Seeding these as
        # QUATERNION was a convenience on the writing side -- `to_track_quat`
        # hands one back -- and a trap on the artist's: Properties then shows
        # `W X Y Z` instead of the familiar `Rotation X/Y/Z`, and assigning
        # `rotation_euler` is SILENTLY IGNORED while that mode is set.
        # Measured: typing Euler angles moved 0 normals, the quaternion moved
        # 1,055.  "I rotated the light and nothing happened" is the symptom.
        lamp.rotation_euler = d.normalized().to_track_quat("Z", "Y").to_euler()
        # A Sun's POSITION is meaningless to the solve -- only its direction is
        # read.  It is not meaningless to the artist: left at the origin all
        # three stack on one another in the corner of a map 308 units wide,
        # invisible and impossible to click apart.  Park each one out along the
        # direction it shines FROM, so the scene reads the way the light does.
        lamp.location = centre + d.normalized() * radius
        out.append(lamp)
    return out


def scene_lamps(scene, ob):
    """Every lamp authoring `ob`: the `LIGHT` objects in THE MAP'S COLLECTION,
    minus the hidden ones.

    **Membership is what scopes** (decision 30).  The docstring here used to say
    the opposite — *"requiring it to be parented would be a rule with nothing
    behind it"* — and that was true when it was written.  Three things are behind
    it now: two maps in one scene share every lamp, and both `target_map` and
    `_live_handler` already walk the scene looking for a map; the default startup
    point light lands in the bake, which `lamp_signature`'s own docstring names as
    what made a state switch destructive; and a mode needs a boundary to be a mode
    *of* something.  The WRITER already scoped and the reader did not — `seed_lamps`
    links into `ob.users_collection[0]`, and every other object the addon owns is
    found by membership plus an `exmateria_map/*` flag.  Lamps were the sole
    exception.  Blender adds to the ACTIVE collection, so entering authority makes
    the map's collection active and `Add ▸ Light` lands in scope by itself.

    **Hiding a lamp excludes it, by any of the three switches.**  `hide_render`
    alone was the first rule and it is the wrong one: measured, an artist who hid
    a torch with the Outliner's EYE or MONITOR icon — the ordinary way to A/B a
    light — still had it contributing 1,019 corners to the bake, with nothing on
    screen to say so.  `visible_get()` is what covers all three, and it also
    picks up a hidden collection.  The rule is now the obvious one: if you cannot
    see it, it is not in the bake.

    `scene` is still taken because `visible_get()` is a VIEW-LAYER question and a
    lamp in the collection but not in this scene has no answer to it.
    """
    from .export_document import marker_collection
    col = marker_collection(ob) if ob is not None else None
    if col is None:
        return []
    in_scene = set(scene.objects)
    return [o for o in col.all_objects
            if o.type == "LIGHT" and o in in_scene
            and not o.hide_render and o.visible_get()]


# ---------------------------------------------------------------------------
# The target: what the lamps deliver, per corner
# ---------------------------------------------------------------------------

def _lamp_irradiance(lamp, p):
    """`(direction_to_light, radiance_scale)` at world point `p`, or None.

    Blender's own radiometry, unscaled (the units decision): a SUN's strength is
    an irradiance and does not fall off; POINT/SPOT/AREA are watts and fall off
    as `1 / (4 pi d^2)`.  On a map 280 units across that makes a default 1000 W
    lamp read about 0.00001, which is why the report names every lamp's peak
    rather than leaving the artist to guess -- inventing a scale factor here
    would be decision 24's "third structure matching neither reference".

    The emitter's SIZE is ignored: radius and shape only soften shadow edges and
    add penumbra, which is per-pixel detail the corner sample rate cannot hold
    (§10, faces are linearised).
    """
    d = lamp.data
    mat = lamp.matrix_world
    if d.type == "SUN":
        return (-(mat.to_quaternion() @ Vector((0.0, 0.0, -1.0)))).normalized(), 1.0
    delta = mat.translation - p
    dist2 = delta.length_squared
    if dist2 < 1e-12:
        return None
    to_light = delta.normalized()
    atten = 1.0 / (4.0 * math.pi * dist2)
    if d.type == "SPOT":
        axis = (mat.to_quaternion() @ Vector((0.0, 0.0, -1.0))).normalized()
        cos_a = axis.dot(-to_light)
        cos_outer = math.cos(d.spot_size * 0.5)
        if cos_a <= cos_outer:
            return None
        blend = max(1e-6, d.spot_blend)
        cos_inner = math.cos(d.spot_size * 0.5 * (1.0 - blend))
        if cos_a < cos_inner:
            t = (cos_a - cos_outer) / max(1e-12, cos_inner - cos_outer)
            atten *= t * t * (3.0 - 2.0 * t)          # smoothstep, as Cycles
    elif d.type == "AREA":
        axis = (mat.to_quaternion() @ Vector((0.0, 0.0, -1.0))).normalized()
        facing = axis.dot(-to_light)
        if facing <= 0.0:                              # single-sided, as Blender
            return None
        atten *= facing
    return to_light, atten


def lamp_target(ob, lamps, depsgraph=None):
    """Per corner, the RGB the lamps deliver -- and each lamp's peak.

    Returns `(rgb_per_corner, peaks, receivers, inert)`.  `receivers` is the
    shading normal each corner was lit through, unit, which is also §4's
    tie-break start; a face the ARTIST created has no ROM normal and falls back
    to its own geometry.

    `inert` marks the ~1,383 corners corpus-wide that ship with a ZERO-LENGTH
    normal on an imported face.  Those are left alone entirely.  `bake_light`
    renders a zero-length normal as diffuse 0 -- the corner shows ambient and
    nothing else -- so lighting it through its face geometry instead would
    contradict the very preview the bake is judged by, and would rewrite a byte
    the disc holds at zero on purpose.  Measured: they were the WHOLE of the
    fixed point's failure, 40 of 40 movers on MAP006 a0 and 30 arrangements
    corpus-wide, every one of them on an imported face.  `imported` is what
    separates them from a new face, which genuinely has no data yet.

    Shadows are cast against the map's own geometry, per lamp, honouring
    Blender's own `use_shadow` toggle -- measured at 0.91 us a ray, 9.1 ms for
    three lamps over the corpus's largest mesh.
    """
    # `matrix_world` is STALE until the view layer recomputes it: a lamp whose
    # rotation was just assigned still reports the identity, so every sun reads
    # as pointing straight down and the target is silently wrong.  Measured --
    # before the update all three seeded suns returned to_light (0, 0, 1); after
    # it, each returned its own rig direction exactly.
    if depsgraph is None:
        bpy.context.view_layer.update()
    me = ob.data
    mw = ob.matrix_world
    nm = mw.to_3x3().inverted_safe().transposed()
    shadow = me.attributes.get("normals_shadow")
    verts, loops = me.vertices, me.loops
    world = [mw @ v.co for v in verts]

    imported = me.attributes.get("imported")
    receivers, origins, inert = [], [], []
    for poly in me.polygons:
        face_n = (nm @ poly.normal).normalized()
        rom = imported.data[poly.index].value if imported else True
        for li in range(poly.loop_start, poly.loop_start + poly.loop_total):
            n = Vector(shadow.data[li].vector) if shadow else Vector()
            blank = n.length <= 1e-9
            receivers.append((nm @ n).normalized() if not blank else face_n)
            inert.append(bool(blank and rom))
            origins.append(world[loops[li].vertex_index] + face_n * SHADOW_EPS)

    rgb = [[0.0, 0.0, 0.0] for _ in loops]
    peaks = []
    if not lamps:
        return rgb, peaks, receivers, inert

    bvh = BVHTree.FromPolygons(
        [tuple(p) for p in world], [tuple(p.vertices) for p in me.polygons])
    for lamp in lamps:
        col, energy = tuple(lamp.data.color), lamp.data.energy
        cast = lamp.data.use_shadow
        peak = 0.0
        for li in range(len(loops)):
            got = _lamp_irradiance(lamp, origins[li])
            if got is None:
                continue
            to_light, atten = got
            k = receivers[li].dot(to_light)
            if k <= 0.0:
                continue
            if cast and bvh.ray_cast(origins[li], to_light)[0] is not None:
                continue
            gain = energy * atten * k
            for c in range(3):
                rgb[li][c] += col[c] * gain
            peak = max(peak, gain * _luma(col))
        peaks.append((lamp.name, lamp.data.type, peak))
    return rgb, peaks, receivers, inert


# ---------------------------------------------------------------------------
# The bake
# ---------------------------------------------------------------------------

class BakeReport:
    """§7's two-part residual, never collapsed into one number.

    Brightness is actionable -- the normals could not reach it, and the rig is
    what fixes it.  Chroma is not actionable at all: §2 measured the reachable
    chroma spread at a median 8.15 degrees, so a warm-key/cool-fill gradient is
    flattened to one hue by the format and no amount of fiddling changes that.
    One combined RMS would hide the difference and send the artist chasing the
    half that cannot move.
    """

    def __init__(self):
        self.lines = []
        self.corners = self.reached = self.inert = self.moved = 0
        self.bright = []                   # |achieved - target| luma, /255
        self.chroma = []                   # degrees between achieved and target RGB
        self.peaks = []
        self.touched = []
        self.short = 0.0                   # the worst shortfall, in gain units
        self.reach = 0.0                   # what |v| reached there

    def say(self, line):
        self.lines.append(line)

    @staticmethod
    def _stat(a):
        a = sorted(a)
        return (a[len(a) // 2], a[-1]) if a else (0.0, 0.0)

    def summarise(self):
        miss = self.corners - self.reached
        med, mx = self._stat(self.bright)
        self.say(f"brightness residual: median {med:.3f}/255, max {mx:.3f}/255 "
                 f"-- actionable, the rig is what raises it")
        med, mx = self._stat(self.chroma)
        self.say(f"chroma residual: median {med:.2f} deg, max {mx:.2f} deg "
                 f"-- NOT actionable, the format holds one hue (~8 deg gamut)")
        if miss:
            need = (self.short / self.reach) if self.reach > 1e-12 else 0.0
            self.say(f"{miss} of {self.corners} corner(s) out of reach, set to the "
                     f"nearest reachable" +
                     (f"; about {need:.2f}x more gain would reach the worst"
                      if need > 1 else ""))
        else:
            self.say(f"all {self.corners} corner(s) reached exactly")
        if self.inert:
            self.say(f"{self.inert} corner(s) ship a zero-length normal and are "
                     f"left untouched -- the preview renders those unlit")
        return self.lines


def bake_normals(ob, context=None, depsgraph=None):
    """Solve every corner normal so the state's own rig reproduces the lamps.

    §6: geometry rides the ARRANGEMENT while the rig is per state, so one normal
    set serves every state's picture -- 65.03% of geometry-bearing arrangements
    carry more than one rig and 32.17% carry ten.  The solve is aimed at the
    state on screen, because that is the picture the artist is judging; every
    other state sharing these normals is NAMED in the report rather than moving
    silently.
    """
    scene = (context or bpy.context).scene
    rep = BakeReport()
    me = ob.data
    states = object_states(ob)
    i = int(ob.get("exmateria_map/preview_state", 0))
    rig, src, edited = resolved_rig(ob, states, i) if states else (None, None, False)

    rep.say(f"aimed at state {i}" + (f" (rig from {src})" if src else "") +
            (" [EDITED override]" if edited else ""))
    if not rig:
        rep.say("this arrangement carries no light rig at all -- nothing to "
                "solve against, no normal changed")
        return rep
    dirs, gains, live = rig_frames(rig)
    if not live:
        rep.say("every gain in this rig is zero -- nothing to solve against, "
                "no normal changed")
        return rep

    # ZERO LAMPS IS A TARGET, not a reason to stop (decision 30).  The early
    # return that used to live here made "a bake is a pure function of the
    # lamps" false as written: measured on MAP001 a0, aim one lamp for 1,498
    # corners off the ROM, then HIDE every lamp -- 0 change; DELETE every lamp
    # -- 0 change.  The map stayed lit by lamps that no longer existed.  The
    # solver was already built for the zero case: `solve_corner`'s empty-active-
    # set fast path returns the ROM normal unchanged when it already sits behind
    # every terminator (§2 measured 13.23% of shipped corners in that dark cap),
    # and the cross-product candidates drive the rest behind all terminators at
    # once, so target 0 is always EXACTLY reachable by construction.  Zero lamps
    # under authority is a map lit by its AMBIENT alone.
    lamps = scene_lamps(scene, ob)
    if not lamps:
        rep.say("no lamps in this map's collection -- solving for ambient alone")

    rgb, rep.peaks, receivers, inert = lamp_target(ob, lamps, depsgraph)
    for name, kind, peak in rep.peaks:
        rep.say(f"lamp {name} ({kind.lower()}): peak {peak:.6g}")

    glum = [_luma(g) for g in gains]
    vs = luma_vectors(dirs, glum, live)
    shadow = me.attributes["normals_shadow"].data
    nrm = me.attributes["normals"].data
    textured = me.attributes["textured"].data

    for poly in me.polygons:
        if not textured[poly.index].value:
            continue            # untextured faces carry no normals in the format
        for li in range(poly.loop_start, poly.loop_start + poly.loop_total):
            if inert[li]:
                rep.inert += 1
                continue
            target_rgb = rgb[li]
            target = _luma(target_rgb)
            start = receivers[li]
            n, reached = solve_corner(target, start, dirs, gains, glum, live, vs)
            got = forward_rgb(n, dirs, gains, live)
            rep.corners += 1
            rep.reached += bool(reached)
            rep.bright.append(abs(_luma(got) - target) * 255.0)
            a, b = _mag(got), _mag(target_rgb)
            if a > 1e-9 and b > 1e-9:
                cosang = max(-1.0, min(1.0, _dot(got, target_rgb) / (a * b)))
                rep.chroma.append(math.degrees(math.acos(cosang)))
            if not reached:
                v = max((math.sqrt(_dot(x, x)) for x in vs.values()), default=0.0)
                if target - v > rep.short - rep.reach:
                    rep.short, rep.reach = target, v
            # Rescale to the corner's OWN i16 magnitude: the disc's are
            # 4095/4096/4097, so a unit vector or a flat 4096 would move 9% of
            # corners by an LSB and the fixed point could never be byte-exact.
            old = Vector(shadow[li].vector)
            m = old.length if old.length > 1e-9 else 4096.0
            put = (n[0] * m, n[1] * m, n[2] * m)
            nrm[li].vector = put
            # The badge keys on DIVERGENCE, never on the solve having run: a
            # fixed-point solve reproduces the ROM's normals exactly, and
            # badging that would contaminate the very comparison the badge
            # exists to protect.  Same 1e-4 as `divergence`'s own read.
            if any(abs(x - y) > 1e-4 for x, y in zip(put, old)):
                rep.moved += 1

    sharing = [j for j in range(len(states))
               if (states[j] or {}).get("resource") is not None and j != i]
    if sharing:
        rep.touched = sharing
        rep.say(f"these normals are shared by {len(sharing)} other state(s), "
                f"whose pictures move too: " +
                ", ".join(str(j) for j in sharing[:12]) +
                (" ..." if len(sharing) > 12 else ""))
    rep.summarise()
    if rep.moved:
        # The badge's normals axis, marked where the addon WRITES -- decision 30.
        set_normals_edited(ob)
        rep.say(f"{rep.moved} corner(s) now differ from the document's normals")
    bake_light(me, rig)                    # the preview IS the bake's readout
    return rep


# ---------------------------------------------------------------------------
# Live bake — re-solve whenever a lamp moves
# ---------------------------------------------------------------------------
#
# "The bake IS the preview" settled that there is no separate live view of the
# LAMPS, because a target preview shows a picture the format cannot hold.  Firing
# the REAL bake automatically has none of that problem: what appears is the fit,
# because it is the fit.  So this is the same decision, triggered on a change
# instead of on a click.
#
# It is only safe because a bake is a pure function of the lamps — every solve
# starts from the ROM normals, so running it a hundred times during a drag
# accumulates nothing and the hundredth answer equals the first from that pose.
#
# Guarded FOUR ways, the shape `authoring.py`'s drift checker already needed: a
# re-entry flag (the bake writes mesh attributes, which re-enters this handler),
# the import/export operators' suspend, the depsgraph's own change list, and a
# LAMP SIGNATURE.  The signature is what actually stops the loop: our own writes
# do not change any lamp, so the second pass returns immediately.

_LIVE_BUSY = False
#: object -> the lamp signature it was last solved (or primed) against.
#:
#: Keyed by `session_uid`, NOT by name. A name is reused the moment the artist
#: deletes a map and imports another -- the collection is named after the
#: arrangement -- and the new object would then inherit the DEAD one's
#: signature, read as "the lamps have not changed", and sit unprimed with no
#: live bake until something moved a lamp. `session_uid` is unique per datablock
#: for the life of the process, so an entry can never outlive its object into a
#: successor.
_LIVE_SIG = {}
# How many times the addon has actually re-solved -- by the handler OR by the
# authority switch's ON edge, which is what makes "off writes nothing" gradeable
# as WORK rather than as an output diff.  Exported because the only
# honest way to grade the signature guard is to count WORK, not output: a bake is
# a pure function of the lamps, so a handler re-baking forever on idle updates
# produces byte-identical normals every time and an output-diff check reads
# green.  Measured -- `live_signature_dropped` was BLIND until this existed.
_LIVE_RUNS = 0


def live_key(ob):
    """`_LIVE_SIG`'s key -- see its comment. Falls back to the name only if a
    datablock somehow has no `session_uid`, which no shipped Blender does."""
    return getattr(ob, "session_uid", None) or ob.name


def lamp_signature(scene, ob):
    """Everything about the lamps and the aimed state that the solve reads.

    Deliberately not a hash of the mesh: the bake WRITES the mesh, so a mesh
    signature would differ on the pass our own edit triggers and the handler
    would chase its own tail forever.

    **The previewed state is deliberately NOT in it.**  It was, and that made
    looking at a second state an EDIT: `set_preview_state` changes the property,
    the signature moved, and the handler re-solved the ROM normals against
    whatever lamps happened to be in the scene -- in a default Blender that is
    the startup point light nobody put there on purpose.  `blender_roundtrip`'s
    `light_baked_borrowed` / `state2` / `back_to_default` / `borrow_keyed` all
    assert the opposite and all four went red on the commit that added this.

    Aiming is not the issue -- the bake really is aimed at the state on screen,
    and a lamp moved after a state switch still solves for the new one, because
    the aim is read at bake time and not from this signature.  What the state
    must not do is TRIGGER a solve: one normal set serves every state's picture
    (ADR-0004 decision 27, §6), so a re-solve re-shades all of them, and that
    decision's own rule is that every state a bake touched is NAMED in its
    report.  A view change that re-shades ten states is exactly the silent move
    the rule exists to forbid.  The Bake button re-aims deliberately.
    """
    out = []
    for lamp in sorted(scene_lamps(scene, ob), key=lambda o: o.name):
        d = lamp.data
        out.append((lamp.name, d.type, tuple(round(c, 6) for c in lamp.matrix_world[0]),
                    tuple(round(c, 6) for c in lamp.matrix_world[1]),
                    tuple(round(c, 6) for c in lamp.matrix_world[2]),
                    round(d.energy, 6), tuple(round(c, 6) for c in d.color),
                    bool(d.use_shadow),
                    round(getattr(d, "spot_size", 0.0), 6),
                    round(getattr(d, "spot_blend", 0.0), 6)))
    return repr(out)


@persistent
def _live_handler(scene, depsgraph=None):
    global _LIVE_BUSY
    if _LIVE_BUSY or depsgraph is None:
        return
    from . import authoring
    if authoring._SUSPEND:
        return
    ob = None
    for cand in scene.objects:
        if _is_map(cand) and getattr(cand, "exmateria_map_lamp_authority", False):
            ob = cand
            break
    if ob is None:
        return
    sig = lamp_signature(scene, ob)
    if _LIVE_SIG.get(live_key(ob)) == sig:
        return
    _LIVE_SIG[live_key(ob)] = sig
    try:
        _LIVE_BUSY = True
        global _LIVE_RUNS
        _LIVE_RUNS += 1
        rep = bake_normals(ob, depsgraph=depsgraph)
        ob["exmateria_map/last_bake"] = json.dumps(rep.lines)
    except (ReferenceError, KeyError, AttributeError):
        pass
    finally:
        _LIVE_BUSY = False


def _authority_update(self, context):
    """The authority switch changed on `self` (decision 30).

    **ON** re-solves once immediately, so handing the lamps the map shows what
    they actually deliver rather than waiting for the next nudge.

    **OFF writes nothing at all.**  `normals` keeps what it holds -- the ROM's,
    a hand edit, or the last solve -- because reverting would destroy the two
    things that have no ROM copy to revert to: a hand-edited normal, and a face
    the artist CREATED, whose `normals_shadow` is blank.  The lamps are the
    saved artefact; they live in the `.blend` and switching back on re-solves.

    The signature is dropped on BOTH edges so the next ON re-solves rather than
    reading a stale "the lamps have not changed".

    **`_LIVE_BUSY` is held across the solve**, the same re-entry guard
    `_live_handler` uses.  Without it the ON edge solves TWICE: dropping the
    signature leaves no entry, the solve writes the mesh, that write tags the
    depsgraph, and the handler runs with nothing to dedupe against.  Measured --
    `on_edge_solves` read 2.  The second solve is harmless in output, because a
    solve is a pure function of the lamps, which is exactly why an output diff
    could never have found this: it has to be counted as WORK.
    """
    global _LIVE_RUNS
    global _LIVE_BUSY
    _LIVE_SIG.pop(live_key(self), None)
    if not (getattr(self, "exmateria_map_lamp_authority", False) and _is_map(self)):
        return                          # off COMMITS: no write
    try:
        _LIVE_BUSY = True
        _LIVE_RUNS += 1
        rep = bake_normals(self, context)
        self["exmateria_map/last_bake"] = json.dumps(rep.lines)
        _LIVE_SIG[live_key(self)] = lamp_signature(context.scene, self)
    finally:
        _LIVE_BUSY = False


# ---------------------------------------------------------------------------
# Operators and the panel
# ---------------------------------------------------------------------------

def _is_map(ob):
    return ob is not None and "exmateria_map/preview_state" in ob


def target_map(context):
    """The map this bake acts on — found in the SCENE, not taken from the
    selection, which is the export leg's own rule (`find_marker`, §9.1).

    Polling on `context.object` was a defect: aiming a lamp means SELECTING the
    lamp, which makes it the active object, which hid this whole panel and
    disabled the Bake button at exactly the moment the artist wanted them.  The
    export operator never had that problem because it asks the scene.
    """
    from .export_document import find_marker, markers
    ob, _problem = find_marker(context)
    if _is_map(ob):
        return ob
    for cand in markers(context.scene):
        if _is_map(cand):
            return cand
    return None


def clear_seeded(scene):
    """Remove the lamps a previous seed left, found by FLAG not by name — the
    same rule export uses for the grid and tile objects."""
    gone = 0
    for lamp in [o for o in scene.objects if o.type == "LIGHT" and LAMP_TAG in o]:
        data = lamp.data
        bpy.data.objects.remove(lamp, do_unlink=True)
        if data.users == 0:
            bpy.data.lights.remove(data)
        gone += 1
    return gone


class MAP_OT_seed_rig_lamps(bpy.types.Operator):
    """Create three Sun lamps reproducing this state's light rig"""
    bl_idname = "map.seed_rig_lamps"
    bl_label = "Seed Lamps from Rig"
    bl_description = ("Create three Sun lamps that reproduce the previewed "
                      "state's light rig exactly, to light the map from")
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return target_map(context) is not None

    def execute(self, context):
        ob = target_map(context)
        states = object_states(ob)
        i = int(ob.get("exmateria_map/preview_state", 0))
        rig, src, _edited = resolved_rig(ob, states, i) if states else (None, None, False)
        if not rig:
            self.report({"WARNING"},
                        "this arrangement carries no light rig — nothing to seed")
            return {"CANCELLED"}
        clear_seeded(context.scene)
        lamps = seed_lamps(ob, rig)
        live = sum(1 for l in lamps if l.data.energy > GAIN_EPS)
        self.report({"INFO"},
                    f"seeded 3 lamp(s) from state {i}"
                    + (f" (rig from {src})" if src else "")
                    + f"; {live} live, {3 - live} at strength 0. Shadows are OFF "
                      f"— the FFT rig casts none.")
        return {"FINISHED"}


class MAP_OT_restore_imported_normals(bpy.types.Operator):
    """Put the document's own corner normals back, over imported faces only"""
    bl_idname = "map.restore_imported_normals"
    bl_label = "Restore Imported Normals"
    bl_description = ("Write the document's own corner normals back over every "
                      "face that came from it. This is the way back from a "
                      "solve — switching Lamp authority off COMMITS what the "
                      "lamps wrote, it does not revert it")
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return target_map(context) is not None

    def execute(self, context):
        ob = target_map(context)
        me = ob.data
        shadow = me.attributes.get("normals_shadow")
        nrm = me.attributes.get("normals")
        if shadow is None or nrm is None:
            self.report({"WARNING"},
                        "this mesh carries no imported normals to restore")
            return {"CANCELLED"}
        # IMPORTED faces only.  A face the artist CREATED has a blank
        # `normals_shadow`, so restoring one would ZERO it rather than reset it
        # -- which is the case that killed "off reverts to the ROM" as a design.
        imported = me.attributes.get("imported")
        faces = skipped = 0
        for poly in me.polygons:
            if imported is not None and not imported.data[poly.index].value:
                skipped += 1
                continue
            moved = False
            for li in range(poly.loop_start, poly.loop_start + poly.loop_total):
                was = tuple(shadow.data[li].vector)
                if tuple(nrm.data[li].vector) != was:
                    nrm.data[li].vector = was
                    moved = True
            faces += bool(moved)
        # The lamps must stop being in charge, or the next nudge silently undoes
        # this.  Decision 30 makes Restore "the way back"; a way back the next
        # lamp move reverses is not one.  It is released rather than reverted --
        # the lamps themselves are untouched and still in the `.blend`.
        released = bool(ob.exmateria_map_lamp_authority)
        if released:
            ob.exmateria_map_lamp_authority = False
        set_normals_edited(ob, False)
        states = object_states(ob)
        i = int(ob.get("exmateria_map/preview_state", 0))
        rig, _src, _edited = resolved_rig(ob, states, i) if states else (None, None, False)
        if rig:
            bake_light(me, rig)            # the preview IS the readout, restored too
        me.update()
        self.report({"INFO"},
                    f"restored {faces} face(s) to the document's normals"
                    + (f"; left {skipped} face(s) you created alone" if skipped else "")
                    + ("; Lamp authority released" if released else ""))
        return {"FINISHED"}


def _bake_report(layout, ob):
    """The last bake's lines, in the export leg's shape."""
    try:
        lines = json.loads(ob.get("exmateria_map/last_bake") or "[]")
    except (ValueError, TypeError):
        return
    if not lines:
        return
    box = layout.box()
    box.label(text="Last bake:", icon="INFO")
    for line in lines[:12]:
        box.label(text=line[:90], icon="INFO")
    if len(lines) > 12:
        box.label(text=f"... and {len(lines) - 12} more")


class MAP_PT_lighting_bake(bpy.types.Panel):
    """N-panel: seed the lamps, bake them back into the map."""
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"
    bl_category = "Map"
    bl_label = "ExMateria Map Lighting Bake"
    bl_order = 1

    @classmethod
    def poll(cls, context):
        return target_map(context) is not None

    def draw(self, context):
        layout = self.layout
        ob = target_map(context)
        # `scene_lamps` itself, never a second count: the panel's job is to
        # report what the solve reads, and a re-implementation drifts from it.
        # It did -- this counted `not hide_render` alone, so hiding every lamp
        # with the Outliner's EYE left the panel insisting three were live while
        # the bake saw none.
        n = len(scene_lamps(context.scene, ob))
        # ONE switch, not three (decision 30).  At ~19 ms on the corpus's
        # largest mesh there is no performance case for a manual refresh, and a
        # "Live off" while authority is ON would put the screen in disagreement
        # with the lamps that are by definition authoritative.
        layout.prop(ob, "exmateria_map_lamp_authority",
                    text="Lamp authority", toggle=True, icon="OUTLINER_OB_LIGHT")
        layout.operator(MAP_OT_seed_rig_lamps.bl_idname, icon="LIGHT_SUN")
        layout.operator(MAP_OT_restore_imported_normals.bl_idname, icon="LOOP_BACK")
        if ob.exmateria_map_lamp_authority:
            layout.label(text=f"{n} lamp(s) in this map's collection"
                              + ("" if n else " — ambient only"))
        else:
            layout.label(text=f"{n} lamp(s) in this map's collection, "
                              f"not in charge", icon="INFO")
        _bake_report(layout, ob)


classes = (MAP_OT_seed_rig_lamps, MAP_OT_restore_imported_normals,
           MAP_PT_lighting_bake)


def register():
    # Per OBJECT, therefore per ARRANGEMENT (decision 4) -- never per map state,
    # because 65.03% of geometry-bearing arrangements carry more than one rig
    # and 32.17% carry ten, all sharing one normal set.  Per object also lets
    # two documents sit side by side and be compared, the reason already on the
    # record for the debug mode and the Overrides.
    #
    # DEFAULT OFF, and import lands off: with it on, an import into a lamp-less
    # scene would solve against no light and flatten the map -- the failure
    # `prime_live` was written for (205 of 243 normals moved on MAP000 a0).
    bpy.types.Object.exmateria_map_lamp_authority = BoolProperty(
        name="Lamp authority",
        description="Hand this map's normals to the lamps in its collection. "
                    "ON: they are the authority and the solve runs live off any "
                    "lamp change; zero lamps means ambient alone. OFF: the lamps "
                    "leave scope and the normals keep what they hold — off "
                    "COMMITS, it does not revert",
        default=False, update=_authority_update)
    for c in classes:
        bpy.utils.register_class(c)
    if _live_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_live_handler)


def unregister():
    if _live_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_live_handler)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
    del bpy.types.Object.exmateria_map_lamp_authority
