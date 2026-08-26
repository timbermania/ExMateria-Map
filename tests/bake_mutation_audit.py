"""Mutation audit for the lighting bake: does each check catch its defect?

The package's bar is that every check ships with the defect it catches, seeded
and re-run.  Two rules the seeds obey, both learned here the hard way:

- **One arm.**  Each mutation lands in a SCRATCH copy of the package, never in
  the tree.  A mutation to shared code moves both sides together and passes on
  unfixed code — which is why `blender_bake.py` writes its OWN forward model
  (`own_forward`) rather than grading the solver with `lighting_bake`'s.
- **Prove the seed moved something.**  An INERT seed reads exactly like a blind
  check.  Every seed below is a defect that was actually observed during the
  build, with the measured damage recorded beside it, so none of them is
  hypothetical.

Seeds 1-3 are §9's own three.  Seeds 4-9 are defects §9 did not anticipate and
the corpus run found: each one was RED at 148/148 before it was fixed.  Seed 10
guards the operator path, which the three solver checks cannot see at all
because they call `bake_normals` directly.

Run:  EXMATERIA_ASSETS_DIR=... python3 tests/bake_mutation_audit.py [blender]
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
BAKE = "addons/exmateria_map/lighting_bake.py"
HARNESS = "tests/blender_bake.py"
BLENDER = sys.argv[1] if len(sys.argv) > 1 else "blender"

# (label, file, anchor, replacement) -- the anchor must appear exactly ONCE.
MUTATIONS = [
    # ---- §9's three -------------------------------------------------------
    ("tiebreak_inverted", BAKE,
     "        return max(exact, key=lambda n: _dot(n, n_start)), True",
     "        return min(exact, key=lambda n: _dot(n, n_start)), True  # MUTANT"),
    ("active_sets_disabled", BAKE,
     "    for mask in range(1, 1 << len(live)):",
     "    for mask in [(1 << len(live)) - 1] if live else []:  # MUTANT"),
    ("residual_collapsed", BAKE,
     '        self.say(f"chroma residual: median {med:.2f} deg, max {mx:.2f} deg "\n'
     '                 f"-- NOT actionable, the format holds one hue (~8 deg gamut)")',
     '        pass  # MUTANT: one combined number instead of two'),

    # ---- the six the corpus run found ------------------------------------
    # 13.23% of corners sit in the dark cap; without the empty set they are
    # shoved onto a light's terminator, which renders identically and moves the
    # bytes.  Measured: 954 of 1,922 on MAP005 a0.
    ("empty_active_set_dropped", BAKE,
     "    if abs(target) <= 1e-12 and all(_dot(n_start, dirs[j]) <= 1e-9 for j in live):\n"
     "        return list(n_start), True",
     "    pass  # MUTANT: no empty active set"),
    # The region-boundary candidates.  Without them a target read off a real
    # normal reads as unreachable: 24 of 790 on MAP000 a0.
    ("boundary_candidates_dropped", BAKE,
     "            for t in (base + off, base - off):",
     "            for t in []:  # MUTANT: no region-boundary candidates"),
    # Zero-length ROM normals are what the preview renders unlit; lighting them
    # through face geometry moved 30 arrangements.
    ("inert_normals_lit", BAKE,
     "            if inert[li]:\n                rep.inert += 1\n                continue",
     "            pass  # MUTANT: zero-length ROM normals get relit"),
    # `matrix_world` is stale until the view layer recomputes: every seeded sun
    # read as pointing straight down and 1,738 of 1,922 corners moved.
    ("stale_matrix_world", BAKE,
     "    if depsgraph is None:\n        bpy.context.view_layer.update()",
     "    pass  # MUTANT: trust a stale matrix_world"),
    # The disc's magnitudes are 4095/4096/4097, so a flat rescale moves ~9%.
    ("flat_magnitude", BAKE,
     "            m = old.length if old.length > 1e-9 else 4096.0",
     "            m = 4096.0  # MUTANT: ignore the corner's own magnitude"),
    # The FFT rig casts no shadows, so a seeded lamp with shadows on darkens
    # every occluded corner the instant the artist touches the map.
    ("seeded_lamps_cast_shadows", BAKE,
     '        data.use_shadow = False',
     '        data.use_shadow = True  # MUTANT'),
    # A hidden lamp must leave the bake, by any of the three switches.
    ("hidden_lamps_still_bake", BAKE,
     'if o.type == "LIGHT" and o in in_scene\n'
     '            and not o.hide_render and o.visible_get()]',
     'if o.type == "LIGHT" and o in in_scene\n'
     '            and not o.hide_render]  # MUTANT'),
    # The three non-SUN paths in `_lamp_irradiance` shipped untested.
    ("falloff_dropped", BAKE,
     "    atten = 1.0 / (4.0 * math.pi * dist2)",
     "    atten = 1.0  # MUTANT: no inverse-square falloff"),
    ("spot_cone_ignored", BAKE,
     "        if cos_a <= cos_outer:\n            return None",
     "        if False:  # MUTANT: the cone never clips\n            return None"),
    ("area_double_sided", BAKE,
     "        if facing <= 0.0:                              # single-sided, as Blender\n"
     "            return None",
     "        facing = abs(facing)  # MUTANT: area lights emit both ways"),
    # Without the lamp signature the handler sees its OWN mesh writes as a
    # change and re-bakes forever; with it, an idle update costs nothing.
    ("live_signature_dropped", BAKE,
     "    sig = lamp_signature(scene, ob)\n    if _LIVE_SIG.get(live_key(ob)) == sig:\n        return",
     "    sig = None  # MUTANT: no signature guard"),
    # Lamp authority must actually gate the handler.
    ("authority_ignored", BAKE,
     'if _is_map(cand) and getattr(cand, "exmateria_map_lamp_authority", False):',
     'if _is_map(cand):  # MUTANT: Lamp authority is ignored'),
    # Off COMMITS, it does not revert (decision 30). The losing candidate would
    # destroy the two things with no ROM copy to revert to: a hand-edited
    # normal, and a face the artist CREATED, whose `normals_shadow` is blank.
    ("authority_off_reverts", BAKE,
     "    if not (getattr(self, \"exmateria_map_lamp_authority\", False) and _is_map(self)):\n"
     "        return                          # off COMMITS: no write",
     "    if not (getattr(self, \"exmateria_map_lamp_authority\", False) and _is_map(self)):\n"
     "        if _is_map(self):  # MUTANT: off REVERTS to the imported normals\n"
     "            me = self.data\n"
     "            sh = me.attributes.get(\"normals_shadow\")\n"
     "            nr = me.attributes.get(\"normals\")\n"
     "            if sh and nr:\n"
     "                for _li in range(len(nr.data)):\n"
     "                    nr.data[_li].vector = tuple(sh.data[_li].vector)\n"
     "        return"),
    # Zero lamps under authority is DARKNESS, not "nothing to do". The early
    # return this restores is the whole defect decision 30 was written for:
    # measured on MAP001 a0, delete every lamp and 0 normals changed.
    ("zero_lamp_early_return", BAKE,
     '        rep.say("no lamps in this map\'s collection -- solving for ambient alone")',
     '        rep.say("no lamps")  # MUTANT: the early return is back\n'
     "        return rep"),
    # The solve is scoped to THE MAP'S collection. Unscoped, a stray sun
    # anywhere in the scene contributed 1,019 corners with nothing on screen to
    # say so, and two maps side by side shared every lamp.
    ("lamps_unscoped", BAKE,
     "    col = marker_collection(ob) if ob is not None else None\n"
     "    if col is None:\n"
     "        return []",
     "    col = None  # MUTANT: back to every LIGHT in the scene\n"
     "    if col is None:\n"
     "        return [o for o in scene.objects\n"
     "                if o.type == \"LIGHT\" and not o.hide_render and o.visible_get()]"),
    # QUATERNION mode silently ignores `rotation_euler`, so the artist aims the
    # light through the ordinary Rotation fields and NOTHING happens.
    ("lamps_seeded_quaternion", BAKE,
     '        lamp.rotation_euler = d.normalized().to_track_quat("Z", "Y").to_euler()',
     '        lamp.rotation_mode = "QUATERNION"  # MUTANT\n'
     '        lamp.rotation_quaternion = d.normalized().to_track_quat("Z", "Y")'),
    # Aiming a lamp means SELECTING it, which makes it the active object. Polling
    # the panel on `context.object` hid the whole bake surface at exactly that
    # moment -- the artist follows the instruction and the button disappears.
    ("poll_on_active_object", BAKE,
     "    ob, _problem = find_marker(context)\n    if _is_map(ob):\n        return ob",
     "    return context.object if _is_map(context.object) else None  # MUTANT"),
    # The residual report is the whole of §7's actionable half, and it reaches
    # the artist only through the object property the panel reads.  The three
    # solver checks call `bake_normals` directly and cannot see this at all.
    ("report_never_stored", BAKE,
     '        rep = bake_normals(self, context)\n'
     '        self["exmateria_map/last_bake"] = json.dumps(rep.lines)',
     '        rep = bake_normals(self, context)\n'
     '        pass  # MUTANT: the report never reaches the panel'),
]


def run(cwd):
    p = subprocess.run([sys.executable, HARNESS, BLENDER], cwd=str(cwd),
                       capture_output=True, text=True, env=dict(os.environ),
                       timeout=2400)
    return p.stdout + "\n" + p.stderr[-2000:]


def failures(out):
    """The named checks that are red — the unit a seed is graded in.

    NO_VERDICT is not decoration.  A harness that dies before printing anything
    produces no FAIL lines, which reads exactly like a clean run; this package
    has twice had a grader miss the column it had just gained, so the absence of
    a verdict is graded as loudly as a failure.
    """
    bad = set()
    for line in out.splitlines():
        if line.startswith("FAIL fixed point"):
            bad.add("FIXED_POINT")
        elif line.startswith("FAIL recovery"):
            bad.add("RECOVERY")
        elif line.startswith("FAIL honest residual"):
            bad.add("HONEST")
        elif line.startswith("FAIL operators"):
            bad.add("OPERATORS")
        elif line.startswith("FAIL lamp types"):
            # Added late, and its absence is why the four lamp seeds first
            # registered as NO_VERDICT rather than as this axis. That is the
            # grader defect the addon CLAUDE.md names twice: "a graded axis the
            # grader does not parse reads as BLIND while the harness prints the
            # defect on every run." Here a fallback happened to save it. When a
            # seed reads blind or lands on the wrong axis, check the grader.
            bad.add("LAMP_TYPES")
        elif line.startswith("FAIL zero lamps"):
            # Decision 30's axis. Added with check 6, and added HERE in the same
            # edit: a graded axis the grader does not parse reads as BLIND while
            # the harness prints the defect on every run.
            bad.add("ZERO_LAMPS")
        elif line.startswith("FAIL harness error"):
            bad.add("HARNESS_ERROR")
    if not re.search(r"1\. FIXED POINT\s+(\d+)/(\d+)", out):
        bad.add("NO_VERDICT")
    if "PASS all six checks" not in out and not bad:
        bad.add("NO_VERDICT")
    return sorted(bad)


def scratch(tmp):
    dst = Path(tmp) / "pkg"
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(PKG, dst, symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", ".blender_*",
                                                  ".git", "*.egg-info"))
    return dst


def check_seeds():
    """Every seed's anchor must appear EXACTLY ONCE in its target, right now.

    A seed whose anchor stops matching patches nothing, and a mutant that
    patches nothing produces a clean run -- which reads exactly like a check
    that caught the defect. The audit loop already grades that as blind, but it
    finds out 15-40 minutes in and only for the seeds it reaches. This is the
    same question asked in a second, so it can be run after any edit to the
    files these anchors quote.
    """
    bad = []
    for row in MUTATIONS:
        label, rel, old = row[0], row[1], row[2]
        n = Path(rel).read_text().count(old)
        if n != 1:
            bad.append((label, rel, n))
    for label, rel, n in bad:
        print(f"SEED-BROKEN {label}: anchor appears {n}x in {rel}")
    print(f"{len(MUTATIONS) - len(bad)}/{len(MUTATIONS)} seed anchors match exactly")
    return 1 if bad else 0


def main():
    if "--check-seeds" in sys.argv:
        sys.exit(check_seeds())

    with tempfile.TemporaryDirectory(prefix="exmateria-bake-mutate-") as tmp:
        base = failures(run(scratch(tmp)))
        print(f"BASELINE: {base or 'clean'}", flush=True)
        if base:
            print("FAIL: the unmutated scratch copy is already red; "
                  "a mutation audit on a red baseline grades nothing")
            sys.exit(1)

        blind = []
        for label, rel, old, new in MUTATIONS:
            dst = scratch(tmp)
            f = dst / rel
            s = f.read_text()
            if s.count(old) != 1:
                print(f"SEED-BROKEN {label}: anchor appears {s.count(old)}x "
                      f"in {rel}", flush=True)
                blind.append(label)
                continue
            f.write_text(s.replace(old, new))
            caught = [c for c in failures(run(dst)) if c not in base]
            print(f"{'CAUGHT' if caught else 'BLIND '} {label:28} -> "
                  f"{caught or 'NOTHING'}", flush=True)
            if not caught:
                blind.append(label)

    print(f"\n{len(MUTATIONS) - len(blind)}/{len(MUTATIONS)} seeds caught; "
          f"blind: {blind or 'none'}")
    print("PASS" if not blind else "FAIL")
    sys.exit(1 if blind else 0)


if __name__ == "__main__":
    main()
