# The Blender texture-paint SIGSEGV — what the coredumps establish

**Status: cause identified, REPRODUCED (6 of 7 launches), and a guard shipped
that holds it off (0 of 7, over 3x the strokes).** Written 2026-08-29. Harnesses:
`tests/blender_paint_crash.py --modal` (the segfault loop, both arms) and
`tests/blender_settle_modal_guard.py` (the guard, at the operator seam).

§3 and §6 each carry a correction to an earlier claim in this document that
turned out to be false; both are kept where the wrong claim stood rather than
being quietly edited away.

The artist's recipe: open Blender, import a GNS, convert it, replace the map
(a live push), then move the camera and paint until it dies. Ten Blender
SIGSEGVs on record going back to 2026-08-22; four from 2026-08-29 still have
their cores.

---

## §1 The finding

Blender 5.2 on Arch is stripped and has **no debuginfo** — `nm -D` yields 8
text symbols, `debuginfod.archlinux.org` answers 404 for build-id
`46032a6e5125961e5ecc253d1eec0fdfba2c782e`. The frames were therefore
identified by **disassembly and live data**, not by symbols.

**The faulting leaf is `closest_to_line_segment_v2`, called from
`dist_squared_to_line_segment_v2`.** Not inferred — read off the machine code.
Load base `0x560efe4b8000`; the wrapper at file offset `0x274b850` is:

```
push %rbp; mov %rdx,%rcx; mov %rsi,%rdx      # shuffle (p,l1,l2) -> (_,l1,l2)
lea -0x20(%rbp),%rdi; mov %rbx,%rsi          # r_close = a stack temp, p
call 0x...037c0                              # closest_to_line_segment_v2
movss (%rbx),%xmm0; subss -0x20(%rbp),%xmm0  # len_squared_v2v2(p, closest)
```

which is `BLI_math_geom.c`'s `dist_squared_to_line_segment_v2` verbatim. The
faulting instruction is `movss (%rdx),%xmm5` — the first load of `l1`.

**The caller is projective texture paint.** At the fault, `r14` points at a
struct whose first five fields are pointers followed by an `int` — the shape of
`ProjPaintState` (`View3D*, RegionView3D*, ARegion*, Depsgraph*, Scene*, int
source`). The code reads a byte at `+0x2165`, i.e. the struct is ≥ 8.5 KB,
which is `ProjPaintState`'s signature size: it embeds
`MemArena *arena_mt[BLENDER_MAX_THREADS]` and `BLENDER_MAX_THREADS` is 1024.
Independently, `r8` points at four consecutive float pairs —
`(0.623, 0.712) (0.623, 0.730) (0.549, 0.730) (0.549, 0.712)` — a **UV quad**.

So: 2D geometry, over UVs, inside `ProjPaintState`. That is
`paint_image_proj.cc`, and it is reached **both** from Blender's TBB task pool
(crashes 1–3) and from the main event loop (crash 4, whose stack contains no
`libtbb` at all and descends to `__libc_start_main`).

## §2 It is a use-after-free, and the freed memory was reused for floats

`rbx`, `rcx`, `rdx`, `rsi` and `r12` all hold **one** pointer,
`0x7f81c1f9a048` — the caller passed the same address as `p`, `l1` and `l2`.
That address is **not in any `PT_LOAD` of the core**: genuinely unmapped, 6.1 GB
above the top of the mmap arena (`0x7f805429f000`). Since Linux allocates mmap
regions *downward* from `mmap_base`, an address above the highest mapped one was
never a live allocation — it is arithmetic, not a stale block.

> ⚠ Instrument note: `info proc mappings` on a **core** lists only file-backed
> mappings (from `NT_FILE`). Against that, the *valid* pointers `r8`/`r10`/`r14`
> also read as "unmapped". Classify against the core's `PT_LOAD` segments
> instead — that is what the paragraph above uses.

Against the live UV array in `r8`:

```
0x7f81c1f9a048 - 0x7f7f9a0e1368 = 0x227EB8CE0 = 8 * 0x44FD719C
```

Exactly `uvlayer[0x44FD719C]`. And `0x44FD719C` **as an IEEE float is 2027.55**
— a sheet-pixel coordinate, not an index. Blender indexes UVs by corner index
(`PS_LOOPTRI_AS_UV_3` expands to `uvlayer[lt[0]], uvlayer[lt[1]],
uvlayer[lt[2]]`), so three *equal* UV pointers means the three corner indices
were equal, and their value was a float bit pattern.

**Conclusion: an index array project paint walks was freed, its memory reused to
hold float pixel coordinates, and project paint read those floats back as corner
indices.** Whoever touches it next dies — sometimes a TBB bucket worker,
sometimes the main loop. That is why the same fault has two call stacks, and why
`# Python backtrace` is empty in all four: the addon is not on the stack when it
dies, it corrupted state that Blender dereferenced afterwards.

### What this rules out

- **Not the camera-sync worker.** Already exonerated on timing; also, that
  worker never touches `bpy` and the fault is nowhere near it.
- **Not "our thread races Blender's TBB pool".** Crash 4 has no second thread.
- **Not an `Image`/`ImBuf` being freed under a redraw** (the leading lead until
  now). The fault is in mesh/UV *indexing* state, not in a pixel buffer. An
  `images.remove()` with a live Image Editor is a real hazard and worth fixing
  on its own merits, but it is not what this backtrace shows.

## §3 The loop, and the gap it closed

`tests/blender_paint_crash.py` drives the artist's recipe: real `MAP022.GNS`
→ `paint_sheet` → `convert_manifold` → `live_push(replace_loaded_map=True)` →
Texture Paint → strokes in the 3D viewport with the view orbiting between them,
with incremental pushes and `paint_sheet` interleaved mid-paint.

It is **headful** (`--background` deletes the entire 3D-viewport paint path,
so it would remove the subject) and **timer-driven**, one step per tick, so the
event loop, the draw path and the addon's settle-clock timer all keep running
between steps. It is sharp: the paint canvas is fingerprinted at mode entry and
after the strokes, and a launch whose canvas did not move is graded **FAILED
HARNESS**, never green.

**Twelve launches did not reproduce it**, up to 3 cycles × 150 strokes, with
the live push against a real PCSX-Redux, and with `MALLOC_PERTURB_=165` +
`--debug-memory` so a use-after-free faults on the spot instead of reading
plausible bytes. (One of the twelve exited early with rc 0 — no coredump,
`/tmp/blender.crash.txt` untouched — so it was quit, not crashed, and graded
nothing either way. The runner separates the three non-survivals — SEGV /
ENDED EARLY / PAINTED NOTHING — because reported as one they read as a paint
failure.)

The gap was stroke *shape*, and it follows from §2:
`bpy.ops.paint.image_paint(stroke=[...])` is a single `exec` call, so
`ProjPaintState` is built and destroyed **inside** it. The artist's strokes are
**modal** — spread over many events — and that is the only window in which a
timer, a push, or a depsgraph re-evaluation can free mesh state *while*
`ProjPaintState` still points into it. So those twelve launches were not
evidence of anything: they could not reach the bug.

`--modal` closes it with `Window.event_simulate` (§6), and the crash arrived on
the first launch. No `/dev/uinput` device and no synthetic cursor were needed —
the survey of those routes that stood here is superseded, along with this
section's claim that `event_simulate` "is not exposed in the Arch 5.2 build",
which was **wrong**: see §6.

## §4 What frees it: the settle fires inside an open stroke

§2 poses one question — *what frees the evaluated mesh's triangulation while a
stroke is live?* It is the settle's own timer.

`settle_op._tick` runs at 4 Hz on `bpy.app.timers`, `persistent=True`, and
reaches `compile_op.land_compile`, which does (`compile_op.py:474-479`):

```python
me = ob.data
with readable_mesh(ob):
    moved = _write_binding(me, polygons, chosen.binding)
_land(ob, state, idx, compiled)
stamp_compile(ob, sheet, painting, master)
me.update()
```

`me.update()` on the original mesh tags the depsgraph, which **frees the
evaluated mesh** — and the evaluated mesh's corner and UV arrays are exactly
what `ProjPaintState` caches for the whole of a modal stroke. `readable_mesh`
only acts in `EDIT` mode; in `TEXTURE_PAINT` it yields straight through, so
nothing stands between the timer and the write.

**Measured, before any fix** (a throwaway probe that spied on `land_compile`
and logged `ob.mode` at each call; MAP022 a0, real strokes into a real canvas): **3 settle lands, 3 of them with the object in
`TEXTURE_PAINT`, and a `_write_binding` in each.** Not a hazard in principle —
it happens on every settle the artist triggers by painting.

### Why the settle thinks the artist has stopped when they have not

`settle_clock.py` is explicit about its witness, and about how it was chosen:

> Amendment 7 named `wm.operators` as the intended witness and left it open.
> Measured in Blender 5.2.0 LTS it is the wrong one […] What does [answer] is
> the candidate the ADR listed third: the canvas's own content.

A canvas digest answers *"has the picture stopped changing"*. The settle reads
that as *"the artist has stopped painting"*. **They are different claims, and a
live stroke can satisfy the first:**

- a drag held still for the 1.5 s `QUIET_DEFAULT` — the artist pausing
  mid-gesture;
- a drag over texels that are **already the brush's colour**, which moves no
  texel at all however far the mouse travels.

The second is not exotic. The probe above hit it by accident: after the first
compile, strokes 3–7 all reported `dirty=False` — five live strokes that
changed nothing the digest could see.

So on the artist's recipe — *"move the camera around and do a bunch of painting
until it crashes"* — the settle fires **inside** an open `PAINT_OT_image_paint`,
writes the mesh, frees the evaluated mesh under the live `ProjPaintState`, and
the next dab reads freed memory. That memory has just been reused by the
compile's own float buffers, which is why §2 decodes the corner index as the
float `2027.55`.

## §5 The fix, and the witness that turned out to exist

`Window.modal_operators` **is** in Blender 5.2 — it is the witness Amendment 7
wanted and recorded as unavailable. It reads as missing because
`hasattr(bpy.types.Window, "modal_operators")` is `False` for an RNA property:
it must be asked of a window **instance**, `bpy.context.window`.

`settle_op._mid_gesture()` asks it, and `_tick` returns early while any modal
operator is running — both halves, because `_drain_push` lands a push that
comes back through `ensure_compiled` and reaches the same `land_compile` by the
other route. Deferring costs nothing: `SettleClock` keeps tracking `_seen`, so
paint that lands during a gesture is compiled on the first tick after it ends.
The compile is delayed, never dropped.

A second, unrelated defect fixed alongside it: `settle_op._launch`'s worker
closure read `ob.name`, `painting.name` and `idx.name` **from the worker
thread**, seconds later — a `bpy` read off the main thread, through pointers
Blender was free to move, in the function whose own docstring forbids exactly
that ("Names rather than datablock references cross the thread boundary"). The
names are now taken on the main thread, the shape `_land` already used.

### How the fix is graded

`tests/blender_settle_modal_guard.py`, headful, two arms:

| | with the guard | guard removed |
|---|---|---|
| arm A — gesture open, 24 ticks (6 s, 4x the quiet period) | **0 lands** | **1 land, in `TEXTURE_PAINT`** |
| arm B — same session, gesture closed | **1 land** | 0 (already consumed by A) |
| `modal_operators` seen during arm A | 24/24 | 24/24 |

Arm B is not decoration. Arm A alone would pass against a settle that never
lands for any reason — an unarmed clock, a clean canvas, an addon that failed
to register — so arm B is what proves the settle was loaded and would have
fired, and therefore that arm A's silence is the guard.

The subject is a **dummy modal operator**, not a paint stroke, because a stroke
driven from Python is one `exec` call and is never modal: it would populate
`modal_operators` with nothing and could not exercise the guard at all. The
guard asks *"is a modal operator running"*, and any modal operator answers it —
which makes the loop deterministic and needs no synthetic mouse input. The
artist's real stroke is `PAINT_OT_image_paint` in the same list.

## §6 Reproduced, and the direction test

**The crash reproduces on demand: 6 of 7 launches**, and the shipped guard
holds it off: **0 of 7**, over more strokes than the crashing arm ran (60
modal strokes against 21). The rate is high, not certain — which is exactly
why arm B is run to a larger stroke count rather than an equal one. One
command, both arms:

```
python3 tests/blender_paint_crash.py --modal --no-guard --expect-crash \
        --launches 4 --cycles 1 --strokes 3      # SEEDED AS EXPECTED
python3 tests/blender_paint_crash.py --modal \
        --launches 4 --cycles 1 --strokes 3      # HELD
```

Arm B is not green by being quiet: it reports `PAINTED_EVER: True` and
`MODAL_EVER: True`, so it opened real modal strokes that deposited real pixels
and survived anyway. Its crumb trail shows the guard doing the work —
**24 consecutive ticks `gesture=True`** with no `land.begin` between them.

The reproduced backtrace carries the artist's own signature — `+0x5177960`,
`+0x281903b`, `libtbb` below — and its crumb trail ends:

```
6.210  tick gesture=False pending=1
6.210  land.begin
6.210  land_compile.enter mode=TEXTURE_PAINT faces=454
6.216  mesh.update.exit ms=0.0
6.460  tick gesture=False pending=0        <- last line; the process died here
```

`gesture=False` at 6.210 while `PAINT_OT_image_paint` was open is the defect
itself, injected on purpose by `--no-guard`.

### The two things that kept it hidden for eleven launches

**1. `exec` strokes cannot reach this bug.**
`bpy.ops.paint.image_paint(stroke=[...])` builds `ProjPaintState` and destroys
it inside the one call, so no timer, push or depsgraph re-evaluation can free
mesh state while it still points into it. Every arm before 2026-08-29 was an
exec arm. They are kept — they grade the operator sequence cheaply — but a
green run of one is not evidence about this crash.

**2. The harness out-ran its own subject.**
Exec arms fire a stroke every 0.02 s; `QUIET_DEFAULT` is 1.5 s. The canvas was
never quiet, so the settle was starved of the very event under investigation.
A full launch lived 10.1 s and landed the settle exactly once. `ms_hold` is the
fix: stand still for 2.4 s **with the stroke open** — the artist's pause
mid-gesture, which is the whole hypothesis in one step.

### Correction: `Window.event_simulate` IS available in this build

This document and the harness both previously said it was not. The method is in
`bpy.types.Window.bl_rna.functions` on the stock Arch 5.2 build and raises
`Not running with '--enable-event-simulate' enabled` until the launcher passes
that flag — which `--modal` now does. `hasattr` is not the check: it answers
**False on the TYPE** for an RNA function, and True on an instance of a build
that refuses every call. That is the same mistake that earlier reported
`Window.modal_operators` as absent, made twice in one investigation.

So no uinput, no nested compositor, no virtual-pointer client: one CLI flag.
The earlier survey of those routes is superseded and deleted.

### Correction: a modal stroke can be started from Python

`bpy.ops.paint.image_paint("INVOKE_DEFAULT")` returns `{'RUNNING_MODAL'}` and
`window.modal_operators` then holds `PAINT_OT_image_paint`. This document once
said driving a modal stroke needed synthetic input. It survives 200 UV rewrites
that way — because the operator is modal but **no stroke has begun**;
`ProjPaintState` is built on the first dab, not at invoke. Invoke alone is
therefore not enough, and that is what `event_simulate` supplies.

## §7 The crumb trail — what the addon was doing when it died

Blender's crash report logs **operators** and a C backtrace. The settle is
neither: it runs from `bpy.app.timers`, so nothing it does appears there, and
`# Python backtrace` is empty in all five crashes because the addon is not on
the stack when the process dies — it corrupted state Blender dereferenced
afterwards. The report says what died and never says what we were doing.

`addons/exmateria_map/crumbs.py` writes one line per event to
`$TMPDIR/exmateria-map-crumbs-<pid>.log`, and the PID is the join to the
coredump that `/tmp/blender.crash.txt` cannot make (it is overwritten by
whichever Blender died last and carries no PID). Crumbed: every settle tick with
the guard's verdict, `land_compile`, `write_binding`, **`me.update()` on its
own**, and every worker's spawn / begin / end from inside the thread.

Read it with `tools/read_crumbs.py [--crashed] [--pid N]`, which also reports
spans entered and never left — a trail ending on `land_compile.enter` is the
process dying inside the mesh write, and one ending just after
`mesh.update.exit` is the next dab reading what it freed. Those are different
findings, which is why `me.update()` gets its own span rather than being folded
into the `land_compile` one.

There is no `fsync`: a segfault kills the process, not the kernel, so the
`write(2)` that line buffering already issues is enough. `fsync` would defend
against a power cut — not the failure under investigation — at the cost of a
syscall stall inside the 4 Hz tick.
