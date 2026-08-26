"""Mutation audit for the #557 export leg: does each check catch its defect?

The package's bar is that *every check ships with the defect it catches,
seeded and re-run*.  The seeds inside `blender_roundtrip.py` do that from the
SCENE side — break one value, assert the export speaks, repair it.  This
script does it from the other side: it seeds a defect into the shipped export
code itself, one at a time, and records which checks go red.

Two rules the seeds obey:

- **One arm.**  Each mutation lands in a SCRATCH copy of the package, never in
  the tree; a mutation to shared code moves both sides together and passes on
  unfixed code.
- **Prove the seed moved something.**  A seed can be INERT and read exactly
  like a blind check.  `walkable = is_t` is the worked example: for a LOADED
  face both arms of the walkable rule write the same three numbers (a FF FF
  binding's raw attributes ARE the sentinel), so no document can differ and
  there is nothing to catch.  Measured over the 6 corpus arrangements that
  carry an FF FF binding: 0 documents differ.  It is replaced here by the two
  forms that are NOT inert.

Run:  python3 tests/export_mutation_audit.py [blender-binary]
      (needs EXMATERIA_ASSETS_DIR, like the corpus harness)
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
EXPORT = "addons/exmateria_map/export_document.py"
IMPORT = "addons/exmateria_map/import_document.py"
AUTHOR = "addons/exmateria_map/authoring.py"
PAINT = "addons/exmateria_map/paint.py"
LIGHTING = "addons/exmateria_map/lighting_bake.py"
LIVELINK = "addons/exmateria_map/live_link.py"
BLENDER = sys.argv[1] if len(sys.argv) > 1 else "blender"

# (label, file, anchor, replacement, harness)  -- the anchor must appear ONCE.
MUTATIONS = [
    ("bucket_sort_removed", EXPORT,
     "    out.sort(key=lambda r: r[0])",
     "    pass  # MUTANT: bucket order not enforced", "rt"),
    ("stamp_is_a_noop", EXPORT,
     "        va[i].value = NEW_FACE_VISIBLE_ANGLES",
     "        pass  # MUTANT: no stamp", "rt"),
    ("sentinel_is_ff_fe", EXPORT,
     'SENTINEL_BINDING = {"x": 255, "z": 127, "level": 1}\n\nTILE_IMPORTED',
     'SENTINEL_BINDING = {"x": 255, "z": 127, "level": 0}  # MUTANT\n\n'
     'TILE_IMPORTED', "rt"),
    ("grid_taken_from_marker", EXPORT,
     '    base["terrain_grid"] = export_grid(ob, rep)',
     '    export_grid(ob, rep)  # MUTANT: keep the marker snapshot', "rt"),
    ("writes_before_the_gate", EXPORT,
     "        doc, files, rep = assemble(ob)\n"
     '        ob["exmateria_map/last_export"] = json.dumps(rep.lines())',
     "        doc, files, rep = assemble(ob)\n"
     "        write_bundle(doc, files, self.filepath\n"
     "                     if os.path.isdir(self.filepath)\n"
     "                     else str(Path(self.filepath).parent))  # MUTANT\n"
     '        ob["exmateria_map/last_export"] = json.dumps(rep.lines())', "rt"),
    ("index_rows_not_flipped", EXPORT,
     "        src, row = (h - 1 - y) * w, y * w",
     "        src, row = y * w, y * w  # MUTANT: no row flip", "rt"),
    ("export_order_is_identity", EXPORT,
     "    order = import_order(n, flipped)",
     "    order = tuple(range(n))  # MUTANT: the ring reversal is not undone",
     "rt"),
    ("uv_page_band_ignored", EXPORT,
     "                v = g - page * SHEET_W if 0 <= page <= 3 else g",
     "                v = g % 256  # MUTANT: wrap instead of test the band",
     "rt"),
    ("drift_rule_dropped", EXPORT,
     "            if k not in DRIFT_FIELDS:", "            if False:  # MUTANT",
     "rt"),
    ("off_palette_ignored", EXPORT,
     "    for entry in off_palette_list(ob):",
     "    for entry in []:  # MUTANT", "rt"),
    # See the module docstring: the `is_t` form of this one is INERT.  These
    # two are the forms that actually change a document / an attribute.
    ("walkable_always_false", IMPORT,
     "        walkable_attr.data[i].value = bool(is_t and t != SENTINEL_BINDING)",
     "        walkable_attr.data[i].value = False  # MUTANT", "rt"),
    ("walkable_always_true", IMPORT,
     "        walkable_attr.data[i].value = bool(is_t and t != SENTINEL_BINDING)",
     "        walkable_attr.data[i].value = True  # MUTANT", "rt"),
    # --- blocks 3 and 4: the authoring surfaces export reads the output of ---
    ("growth_ignores_the_shrink_refusal", AUTHOR,
     "    if x < was_x:\n        x = was_x",
     "    if False:\n        x = was_x  # MUTANT", "rt"),
    ("growth_ignores_the_area_ceiling", AUTHOR,
     "    if x * z > GROWTH_AREA_MAX:", "    if False:  # MUTANT", "rt"),
    ("growth_grows_the_whole_grid", AUTHOR,
     "            if (x >= was_x or z >= was_z) and (x, z) not in have]",
     "            if (x, z) not in have]  # MUTANT: ignores the pre-growth edge",
     "rt"),
    ("growth_declares_its_seed", AUTHOR,
     '                    o[f + "_declared"] = False',
     '                    o[f + "_declared"] = True  # MUTANT', "rt"),
    ("drift_coverage_centroid_only", AUTHOR,
     "                if (gx, gz) == (tx, tz) or _inside_xy(",
     "                if (gx, gz) == (tx, tz) and _inside_xy(  # MUTANT",
     "corpus"),   # the fixture's one floor quad IS its own centroid tile
    ("drift_bottom_is_the_top", AUTHOR,
     "        low = min(v[2] for v in ring)",
     "        low = max(v[2] for v in ring)  # MUTANT",
     "corpus"),   # the fixture's one floor quad is FLAT, so min == max
    ("drift_never_deletes_a_handle", AUTHOR,
     "            bpy.data.objects.remove(o, do_unlink=True)   # drift cleared",
     "            pass  # MUTANT: a cleared drift keeps its quad", "rt"),
    ("drift_floor_threshold_ignored", AUTHOR,
     "        if _newell_up(ring) < FLOOR_COS:",
     "        if False:  # MUTANT: every polygon is a floor", "corpus"),
    ("drift_step_rounds_down", AUTHOR,
     "        step_now = int(round(bottom / HEIGHT_STEP))",
     "        step_now = int(bottom // HEIGHT_STEP)  # MUTANT", "corpus"),
    # --- block 2: the palette gate -----------------------------------------
    ("paint_recolour_erases_the_refusal", PAINT,
     "        recolour(ob, paint, buffer, state_in, pal_in, unresolved(ob))",
     "        recolour(ob, paint, buffer, state_in, pal_in)  # MUTANT", "rt"),
    ("paint_highest_index_wins", PAINT,
     "            lookup.setdefault(e, i)", "            lookup[e] = i  # MUTANT",
     "rt"),
    ("paint_re_resolves_unchanged_pixels", PAINT,
     "                continue                      # §3.4: never re-resolved",
     "                pass  # MUTANT: every pixel re-resolved", "rt"),
    ("paint_off_palette_is_best_effort", PAINT,
     "                off.setdefault(tuple(int(round(px[j + k] * 255.0))\n"
     "                                     for k in range(3)), []).append(i)",
     "                buffer[i] = 0  # MUTANT: guess instead of refuse", "rt"),
    ("paint_gate_never_clears", PAINT,
     "        still = [p for p in pixels\n"
     "                 if tuple(int(round(px[p * 4 + k] * 255.0))\n"
     "                          for k in range(3)) not in accept]",
     "        still = list(pixels)  # MUTANT", "rt"),
    ("paint_export_is_not_a_trigger", EXPORT,
     "    rep.paint = on_trigger(ob)", "    rep.paint = None  # MUTANT", "rt"),
    ("warning_counts_unreachable_bindings", EXPORT,
     "    return t[\"x\"] < GROWTH_AXIS_MAX and t[\"z\"] < GROWTH_AXIS_MAX",
     "    return True  # MUTANT: every idle value is a binding", "rt"),
    ("warning_suppresses_everything", EXPORT,
     "        if not names_a_tile(t):",
     "        if True:  # MUTANT: nothing ever warns", "rt"),
    # --- the browser's directory memory ------------------------------------
    # The first is the bug that actually shipped: `blender_roundtrip.py` runs
    # --factory-startup, so it proved the SETTER and never the memory.
    ("prefs_never_saved", IMPORT,
     "        bpy.ops.wm.save_userpref()",
     "        pass  # MUTANT: the property is set and never persisted",
     "prefs"),
    ("prefs_saved_against_the_users_setting", IMPORT,
     '    if not getattr(context.preferences, "use_preferences_save", False):\n'
     "        return",
     "    if False:  # MUTANT: save regardless of Auto-Save Preferences\n"
     "        return", "prefs"),
    ("prefs_export_shares_the_import_memory", EXPORT,
     '        remember_dir(context, str(path), field="last_export_dir")',
     "        remember_dir(context, str(path))  # MUTANT", "prefs"),
    # The MAP022 report: a typed folder name fell back to its PARENT, and the
    # report did not name the directory, so it read as nothing written.
    ("outdir_falls_back_to_the_parent", EXPORT,
     "    if path.exists() or path.suffix:\n        return str(path.parent)\n"
     "    return str(path)",
     "    return str(path.parent)  # MUTANT", "rt"),
    ("outdir_ignores_the_browser_directory", EXPORT,
     '    if directory:\n        return str(Path(bpy.path.abspath(directory)))',
     "    if False:  # MUTANT\n        return str(directory)", "rt"),
    ("axis_map_transposed", EXPORT,
     "            v = _blender_to_fft([int(round(c)) for c in co])",
     "            v = tuple(int(round(c)) for c in co)  # MUTANT: no axis map",
     "corpus"),
    ("carry_dropped", EXPORT,
     '           "carry": section(ob, "carry")}', '           "carry": None}  # MUTANT',
     "corpus"),
    # --- the authored light rig (ADR-0004 decision 27) --------------------
    ("export_never_promotes_the_override", EXPORT,
     "        state[AUTHORED_RIG] = rig",
     "        pass  # MUTANT: the Override stays on screen and never ships",
     "rig"),
    ("export_promotes_to_every_state", EXPORT,
     "        ov = find_override(ob, i)\n        if ov is None:\n            continue",
     "        ov = find_override(ob, i) or next(\n"
     "            iter(ob.exmateria_map_rig_overrides), None)  # MUTANT\n"
     "        if ov is None:\n            continue", "rig"),
    ("export_writes_the_overrides_gradient", EXPORT,
     '        rig["gradient"] = list(base_rig.get("gradient") or [0] * 6)',
     "        pass  # MUTANT: the artist's 6 carried bytes, not the state's",
     "rig"),
    ("export_refuses_an_unwritable_override", EXPORT,
     '            rep.warn(f"map state {i} ({state.get(\'resource\')}) carries a rig "',
     '            rep.refuse(f"map state {i} ({state.get(\'resource\')}) carries a rig "  # MUTANT',
     "rig"),
    # Graded on the CORPUS: an untouched export declares no rig, so stamping 2
    # anyway is a whole-document mismatch on all 148 and invisible to the rig
    # harness, where the document really does carry one.
    ("export_stamps_2_unconditionally", EXPORT,
     "    version = (AUTHORED_RIG_VERSION\n"
     "               if any(st.get(AUTHORED_RIG) for st in states) else VERSION)",
     "    version = AUTHORED_RIG_VERSION  # MUTANT: every document, always",
     "corpus"),

    # The solve must not fire on its own. Both of these SHIPPED, in the commit
    # that added the live bake, and both were caught by `rt` and by the corpus
    # -- never by the bake harness, which only ever moves a LAMP.
    #
    # `live_bake_unprimed_on_import` used to sit here, seeding `prime_live` to
    # `pass`. It is GONE rather than moved: under decision 30 import lands with
    # Lamp authority OFF, so the handler skips a fresh object and the failure
    # cannot occur at all -- measured, `rt` is 315/315 with `prime_live`
    # neutered. A seed on code that has been deleted is the inert kind that
    # reads exactly like a check that caught it. What replaces it is a seed on
    # the rule that now carries the weight: the switch's DEFAULT.
    ("authority_defaults_on", LIGHTING,
     "        default=False, update=_authority_update)",
     "        default=True, update=_authority_update)  # MUTANT: import lands ON",
     "rt"),
    # The packets are DOUBLE BUFFERED (`FUN_800ee104`: two, 0xEE28 apart), and
    # a one-buffer push is SILENT -- the plan reproduces that buffer's own bytes
    # exactly, so the self-check reads green while the screen never moves.
    # Measured: 385 bytes changed into one buffer and the picture did not move;
    # 770 into both and the whole map retextured.
    ("packets_one_buffer_only", LIVELINK,
     "PACKET_BASES = (0x800FC55C, 0x800FC55C + PACKET_BUFFER_STRIDE)",
     "PACKET_BASES = (0x800FC55C,)  # MUTANT: only the buffer the pointer names",
     "push"),
    # `texture_page` owns two bits of the TPAGE halfword and nothing else.
    # Reconstructing the word from a base measured on ONE map is right in a
    # Gariland battle (0x0C) and wrong anywhere loaded into another VRAM column
    # -- which is why the fake's base is deliberately 0x0140.
    ("packet_field_rebuilt_not_masked", LIVELINK,
     '                       struct.pack("<H", (held & ~mask) | (poly[field] & mask))))',
     '                       struct.pack("<H", (0x7800 if field == "palette_id"'
     ' else 0x000C) | (poly[field] & mask))))  # MUTANT',
     "push"),
    # Writing the wrong packet base is silent: every address is inside main RAM
    # and `apply` reports a plausible count.
    ("packet_base_unchecked", LIVELINK,
     "    if live not in PACKET_BASES:",
     "    if False:  # MUTANT: any pointer will do",
     "push"),
    ("live_bake_fires_on_state_change", LIGHTING,
     "    out = []\n    for lamp in sorted(scene_lamps(scene",
     '    out = [int(ob.get("exmateria_map/preview_state", 0))]  # MUTANT\n'
     "    for lamp in sorted(scene_lamps(scene",
     "rt"),
]

HARNESS = {"rt": "tests/blender_roundtrip.py",
           "corpus": "tests/blender_corpus.py",
           "prefs": "tests/blender_prefs_persist.py",
           "rig": "tests/blender_authored_rig.py",
           "push": "tests/blender_live_push.py"}


def run(which, cwd):
    args = [sys.executable, HARNESS[which], BLENDER]
    if which == "corpus":
        args += ["--limit", "8"]        # 8 arrangements is enough to move a
    p = subprocess.run(args, cwd=str(cwd), capture_output=True,  # whole-doc diff
                       text=True, env=dict(os.environ), timeout=1800)
    return p.stdout


def failures(out, which):
    """The named checks that are red — the unit a seed is graded in."""
    if which == "rig":
        # Grade off the RED CHECK LINES, not off the verdict line, and demand
        # the SUMMARY that follows them.
        #
        # Both halves are scars. Keying on `FAILED:` alone scored four seeds as
        # caught by `HARNESS_DID_NOT_RUN` -- the harness had run, printed their
        # defect as a red check, and then bailed before its verdict, so the
        # grader recorded the SILENCE rather than the finding. Dropping the
        # SUMMARY demand instead would score a Blender that never started as
        # clean. Check the grader before the check; this audit has now been
        # bitten by exactly that four times.
        red = sorted({l.split()[1] for l in out.splitlines()
                      if l.startswith("  FAIL ")})
        if "SUMMARY:" not in out:
            return red + ["HARNESS_DID_NOT_RUN"]
        return red
    if which == "push":
        # `blender_live_push.py` prints `  FAIL <name>: <detail>` -- the RIG
        # format, not the `CHECK FAIL` one. Routing it to the `rt` branch below
        # made all three packet seeds read BLIND while the harness printed their
        # defect on every run: measured by hand, the mask seed takes that
        # harness to 32/34. Fifth time this audit has been bitten by its own
        # grader; the shape is always "a graded axis the grader does not parse".
        # Split on the COLON, not on whitespace -- these check names are prose,
        # so `line.split()[1]` would key every one of them on its first word.
        red = sorted({l.split("FAIL ", 1)[1].split(":", 1)[0]
                      for l in out.splitlines() if l.startswith("  FAIL ")})
        if "SUMMARY:" not in out:
            return red + ["HARNESS_DID_NOT_RUN"]
        return red
    if which in ("rt", "prefs"):
        return sorted({line.split(":")[0].replace("CHECK FAIL ", "")
                       for line in out.splitlines()
                       if line.startswith("CHECK FAIL")})
    bad = sorted({line.split()[1] for line in out.splitlines()
                  if line.startswith("MISMATCH")})
    if "export_refused=0" not in out:
        bad.append("EXPORT_REFUSED")
    # The drift column is a graded axis, not a note: without this line the four
    # coverage-rule seeds all read BLIND while the harness was printing their
    # defect on every run.
    m = re.search(r"drift_wrong=(\d+)/", out)
    if m is None or int(m.group(1)):
        bad.append("DRIFT_WRONG")
    if not re.search(r"CORPUS (\d+)/\1 EXACT", out):
        bad.append("NOT_EXACT")
    return bad


def scratch(tmp):
    dst = Path(tmp) / "pkg"
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(PKG, dst, symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", ".blender_*"))
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

    with tempfile.TemporaryDirectory(prefix="exmateria-mutate-") as tmp:
        base = {}
        for which in HARNESS:
            base[which] = failures(run(which, scratch(tmp)), which)
            print(f"BASELINE {which}: {base[which] or 'clean'}", flush=True)
            if base[which]:
                print("FAIL: the unmutated scratch copy is already red; "
                      "a mutation audit on a red baseline grades nothing")
                sys.exit(1)

        blind = []
        for label, rel, old, new, which in MUTATIONS:
            dst = scratch(tmp)
            f = dst / rel
            s = f.read_text()
            if s.count(old) != 1:
                print(f"SEED-BROKEN {label}: anchor appears {s.count(old)}x "
                      f"in {rel}", flush=True)
                blind.append(label)
                continue
            f.write_text(s.replace(old, new))
            caught = [c for c in failures(run(which, dst), which)
                      if c not in base[which]]
            print(f"{'CAUGHT ' if caught else 'BLIND  '} {label:28} -> "
                  f"{caught[:6] or 'NOTHING'}", flush=True)
            if not caught:
                blind.append(label)

    print(f"\n{len(MUTATIONS) - len(blind)}/{len(MUTATIONS)} seeds caught; "
          f"blind: {blind or 'none'}")
    print("PASS" if not blind else "FAIL")
    sys.exit(1 if blind else 0)


if __name__ == "__main__":
    main()
