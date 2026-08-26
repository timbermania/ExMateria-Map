"""Mutation audit for the `build` leg: does each check catch its defect?

The package's bar is that *every check ships with the defect it catches,
seeded and re-run*. `tests/test_build.py` does that from the DOCUMENT side --
break a field, assert `build` refuses. This script does it from the other
side: it seeds a defect into the shipped `build`/`dump`/`document` code
itself, one at a time, and records which checks go red.

The rules the seeds obey are the export audit's:

- **One arm.** Each mutation lands in a SCRATCH copy of the package, never in
  the tree; a mutation to shared code moves both sides together and passes on
  unfixed code. `document.py` holds codecs BOTH legs use, so a seed there is
  read as "dump and build agree on something wrong" -- which is exactly the
  class of defect the corpus oracle cannot see and the unit tests must.
- **Prove the seed moved something.** A seed can be INERT and read exactly
  like a blind check. Two of the seeds below were, in their first form, and
  say so where they stand.
- **Check the grader before the check.** This audit read BLIND on nineteen
  live checks in a row, twice, for two different grader defects: an
  interpreter without `pytest` (so the run never happened and "no failures"
  read as clean) and pytest's ANSI colour codes breaking the `FAILED` regex.
  Both are guarded now -- a run with no pytest summary reports
  `HARNESS_DID_NOT_RUN`, never silence.

The last four seeds are not inventions: they are the four defects the
workspace scaffold actually shipped (schema §13), reproduced here so the
claim "these checks would have caught them" is measured rather than asserted.

Run:  python3 tests/build_mutation_audit.py         (needs EXMATERIA_ASSETS_DIR)
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
BUILD = "exmateria_map/build.py"
DUMP = "exmateria_map/dump.py"
DOC = "exmateria_map/document.py"
MAPFILE = "exmateria_map/mapfile.py"
PNG = "exmateria_map/png_indexed.py"
LIMIT = "24"                 # arrangements; enough to move a byte in every chunk

# (label, file, anchor, replacement, harness) -- the anchor must appear ONCE.
MUTATIONS = [
    # --- §6.1 the primary mesh ------------------------------------------
    ("mesh_counts_not_derived", BUILD,
     '    out = bytearray(struct.pack("<HHHH", *(len(by[k]) for k in BUCKETS)))',
     '    out = bytearray(struct.pack("<HHHH", *(n for _, n in SLOT_CAPACITY)))'
     "  # MUTANT", "corpus"),
    # NOT `for kind in BUCKETS` with a `.get("normals", [])`: an untextured
    # polygon has no `normals` key, so that form writes nothing extra and is
    # an INERT seed that reads exactly like a blind check.
    ("normals_only_for_triangles", BUILD,
     "    for kind in TEXTURED_BUCKETS:\n"
     "        for poly in by[kind]:\n"
     "            for v in poly[\"normals\"]:\n"
     '                out += struct.pack("<hhh", *v)',
     "    for kind in TEXTURED_BUCKETS[:1]:  # MUTANT: quads lose their normals\n"
     "        for poly in by[kind]:\n"
     '            for v in poly["normals"]:\n'
     '                out += struct.pack("<hhh", *v)', "corpus"),
    ("clut_high_byte_is_zero", DOC,
     "CLUT_WORD_HIGH_BYTE = 0x78", "CLUT_WORD_HIGH_BYTE = 0x00  # MUTANT",
     "corpus"),
    ("terrain_binding_bytes_swapped", BUILD,
     '            out += bytes([((t["z"] & 0x7F) << 1) | (t["level"] & 1), t["x"] & 0xFF])',
     '            out += bytes([t["x"] & 0xFF, ((t["z"] & 0x7F) << 1) | (t["level"] & 1)])'
     "  # MUTANT", "corpus"),
    ("uv_order_reversed", BUILD,
     "            uv = poly[\"uv\"]\n",
     "            uv = list(reversed(poly[\"uv\"]))  # MUTANT\n", "corpus"),

    # --- §6.2 the visible-angle table -----------------------------------
    ("dead_slots_zeroed", BUILD,
     "    table = list(slots)", "    table = [0] * len(slots)  # MUTANT",
     "corpus"),
    # Graded by `unit`, not `corpus`: on an untouched document the base table
    # ALREADY holds the live values, so not overlaying them changes no byte.
    # The corpus oracle is structurally blind to this one.
    ("live_slots_not_overlaid", BUILD,
     "            table[base + row] = value & 0xFFFF",
     "            pass  # MUTANT: the document's value never lands", "unit"),
    ("capacity_check_removed", BUILD,
     "        if total > capacity:", "        if False:  # MUTANT",
     "unit"),

    # --- §6.4 palettes ---------------------------------------------------
    ("stp_bit_dropped", DOC,
     "    return (r5 & 0x1F) | ((g5 & 0x1F) << 5) | ((b5 & 0x1F) << 10) | ((stp & 1) << 15)",
     "    return (r5 & 0x1F) | ((g5 & 0x1F) << 5) | ((b5 & 0x1F) << 10)  # MUTANT",
     "corpus"),
    ("colour_quantise_truncates", DOC,
     "    r5 = (r8 * 31 + 127) // 255", "    r5 = (r8 * 31) // 255  # MUTANT",
     "corpus"),
    ("palettes_written_from_the_wrong_state", BUILD,
     '        entry = states_by_resource[name].get("palettes")',
     '        entry = next(s["palettes"] for s in document["map_states"]\n'
     '                     if s.get("palettes"))  # MUTANT: not this resource\'s',
     "corpus"),

    # --- §6.3 / §7.2 terrain ---------------------------------------------
    ("undeclared_terrain_field_zeroed", DOC,
     "    out = bytearray(base)", "    out = bytearray(8)  # MUTANT", "unit"),
    ("terrain_size_bytes_not_stamped", BUILD,
     "    out[0], out[1] = size_x & 0xFF, size_z & 0xFF",
     "    pass  # MUTANT: growth never reaches the chunk", "unit"),
    ("pre_growth_tiles_are_editable", BUILD,
     "            if (x, z) not in drift:", "            if False:  # MUTANT",
     "unit"),
    ("drift_pin_bytes_allowed", BUILD,
     "            illegal = [k for k in declared if k not in DRIFT_FIELDS]",
     "            illegal = []  # MUTANT", "unit"),
    ("records_outside_the_extent_allowed", BUILD,
     "        if not (0 <= x < size_x and 0 <= z < size_z):",
     "        if False:  # MUTANT", "unit"),

    # --- §10 the acceptance rules ----------------------------------------
    ("version_check_removed", BUILD,
     "    if version not in schema.ACCEPTED_VERSIONS:", "    if False:  # MUTANT",
     "unit"),
    ("base_digest_check_removed", BUILD,
     "        if digest != declared:", "        if False:  # MUTANT", "unit"),
    ("geometry_digest_check_removed", BUILD,
     '    if got != base.get("geometry_digest"):', "    if False:  # MUTANT",
     "unit"),
    ("pointer_check_removed", BUILD,
     "            if pointer >= len(data):", "            if False:  # MUTANT",
     "unit"),
    ("fanout_mesh_check_removed", BUILD,
     '            if got != base["geometry_digest"]:', "            if False:  # MUTANT",
     "unit"),
    ("fanout_visible_angles_check_removed", BUILD,
     "                if chunk != reference:", "                if False:  # MUTANT",
     "unit"),
    ("map_states_row_count_unchecked", BUILD,
     "    if len(states) != len(rows):", "    if False:  # MUTANT", "unit"),

    # --- the splice -------------------------------------------------------
    ("splice_does_not_fix_pointers", BUILD,
     '        struct.pack_into("<I", out, slot, pointer + moved)',
     "        pass  # MUTANT: every section past a grown chunk is orphaned",
     "unit"),
    ("splice_overlap_unchecked", BUILD,
     "        if start < position:", "        if False:  # MUTANT", "unit"),

    # --- §6.5 sheets ------------------------------------------------------
    ("sheet_nibble_order_flipped", PNG,
     "    return bytes((indices[i] & 0xF) | ((indices[i + 1] & 0xF) << 4)",
     "    return bytes((indices[i + 1] & 0xF) | ((indices[i] & 0xF) << 4)  # MUTANT",
     "unit"),

    # --- §10.4 polygon capacity (decision 28) ------------------------------
    # All three are "unit": the corpus maxima sit UNDER every bound, so a seed
    # on this rule is INERT against build_corpus and would read exactly like a
    # blind check. The arms that can move are in tests/test_build.py.
    ("capacity_bound_is_the_slot_table", BUILD,
     "    for kind, capacity in ENGINE_CAPACITY:",
     "    for kind, capacity in SLOT_CAPACITY:  # MUTANT: 512/768, not 360/710",
     "unit"),
    ("capacity_ignores_the_animated_meshes", BUILD,
     "        total = declared + base_anim[kind]",
     "        total = declared  # MUTANT: the cursors are shared; the sum is not",
     "unit"),
    ("capacity_untested_band_silent", BUILD,
     "        if total > corpus_max[kind]:",
     "        if False:  # MUTANT: the band no shipped map tested goes quiet",
     "unit"),

    # --- §6.2 the one manufacture (decision 26) ---------------------------
    # All seven are "unit", for the same reason decision 28's three are: on the
    # nine affected arrangements every mask dumps `null` and no polygon is
    # added, so NEITHER trigger fires anywhere in the corpus and build_corpus
    # is inert against any mutation of this leg. A `corpus` seed here would
    # read exactly like a blind check and the audit would print PASS.
    ("manufacture_never_fires", BUILD,
     "        if reasons:", "        if False:  # MUTANT: the chunk is never made",
     "unit"),
    ("manufacture_fires_on_every_slotless_base", BUILD,
     "        reasons = ([f\"the document adds {', '.join(added)} polygons\"] if added\n"
     "                   else [])",
     "        reasons = [\"MUTANT: both triggers are now unconditional\"]",
     "unit"),
    ("manufactured_slots_are_not_the_dead_fill", BUILD,
     "                                      [DEFAULT_VISIBLE_ANGLES] * SLOT_TOTAL)",
     "                                      [0] * SLOT_TOTAL)  # MUTANT", "unit"),
    ("manufactured_header_is_the_wrong_bytes", MAPFILE,
     "VISIBLE_ANGLES_HEADER_TAG = bytes((0x12, 0x12, 0x34, 0x34))",
     "VISIBLE_ANGLES_HEADER_TAG = bytes((0x12, 0x12, 0x34, 0x35))  # MUTANT",
     "unit"),
    ("manufactured_pointer_left_at_zero", BUILD,
     '        struct.pack_into("<I", out, slot, len(out))',
     "        pass  # MUTANT: a chunk no section walk will ever dispatch",
     "unit"),
    # Graded on MAP099 a0, and it has to be: that is the ONLY one of the nine
    # whose arrangement holds a non-texture row besides the source (nine 753-B
    # state resources with no 0x40). On the other eight the source is the only
    # non-texture row, so scoped and unscoped produce identical bytes and the
    # seed reads exactly like a blind check -- which is what it did in its
    # first form, graded against MAP083 a0, where `manufacture` is None and the
    # mutated branch is never evaluated at all.
    ("manufacture_reaches_a_non_source_sibling", BUILD,
     "                    if manufacture is not None and name == geometry_source",
     "                    if manufacture is not None  # MUTANT: the siblings too",
     "unit"),
    ("manufacture_warning_silent", BUILD,
     "        capacity_warning,\n        manufacture,",
     "        capacity_warning,  # MUTANT: the relocation goes unannounced",
     "unit"),

    # --- warnings ---------------------------------------------------------
    ("out_of_grid_warning_suppressed", BUILD,
     "        bad.append((index, t))", "        pass  # MUTANT", "unit"),
    ("out_of_grid_warning_fires_on_everything", BUILD,
     "        if not (t[\"x\"] < GROWTH_AXIS_MAX and t[\"z\"] < GROWTH_AXIS_MAX):",
     "        if False:  # MUTANT: warn on the idle value too", "corpus"),
    ("binding_drift_warning_suppressed", BUILD,
     "            wrong.append((index, (x, z), derived))", "            pass  # MUTANT",
     "unit"),

    # --- the authored light rig (decision 27) ------------------------------
    # All "unit": no document `dump` writes declares a rig, so every one of
    # these is INERT against build_corpus and a `corpus` seed here would read
    # exactly like a blind check -- the same reason decisions 26 and 28's seeds
    # are unit. The arms that can move are in tests/test_build.py.
    ("accepted_versions_is_only_1", DOC,
     "ACCEPTED_VERSIONS = (1, 2)",
     "ACCEPTED_VERSIONS = (1,)  # MUTANT: a v2 document is refused", "unit"),
    ("authored_rig_version_gate_removed", BUILD,
     '    if declared_rigs and document.get("version", 0) < schema.AUTHORED_RIG_VERSION:',
     "    if False:  # MUTANT: a v1 document may declare an authored rig",
     "unit"),
    ("authored_rig_kind_test_removed", BUILD,
     '        if state.get("kind") not in mapfile.GNS_MESH_TYPES:',
     "        if False:  # MUTANT: a texture row may carry a rig", "unit"),
    ("authored_rig_written_to_a_chunkless_row", BUILD,
     "        offset = mapfile.light_rig_offset(raw[name], True)",
     "        offset = mapfile.light_rig_offset(raw[name], True) or 0x100\n"
     "        # MUTANT: manufactures 45 bytes decision 19 forbids", "unit"),
    ("authored_rig_gradient_unchecked", BUILD,
     '        if list(rig.get("gradient") or []) != base_rig["gradient"]:',
     "        if False:  # MUTANT: the artist may edit the carried 6 bytes",
     "unit"),
    ("authored_rig_never_spliced", BUILD,
     "        replacements[name].append((offset, offset + LIGHT_RIG_BYTES, blob))",
     "        pass  # MUTANT: the rig is packed and then dropped", "unit"),
    ("rig_reader_is_kind_blind", MAPFILE,
     "    if not is_mesh:\n        return None",
     "    if False:  # MUTANT: #576 again -- a rig read out of sheet pixels\n"
     "        return None", "unit"),
    ("rig_colors_written_interleaved", MAPFILE,
     '            struct.pack_into("<h", out, c * 6 + i * 2, int(value))',
     '            struct.pack_into("<h", out, i * 6 + c * 2, int(value))  # MUTANT',
     "unit"),
    ("rig_directions_written_planar", MAPFILE,
     '            struct.pack_into("<h", out, 18 + i * 6 + k * 2, int(value))',
     '            struct.pack_into("<h", out, 18 + k * 6 + i * 2, int(value))  # MUTANT',
     "unit"),
    ("rig_ambient_and_gradient_swapped", MAPFILE,
     '    for name, start, count in (("ambient", 36, 3), ("gradient", 39, 6)):',
     '    for name, start, count in (("ambient", 39, 3), ("gradient", 36, 6)):  # MUTANT',
     "unit"),

    # --- the four the workspace scaffold actually shipped (schema §13) -----
    ("scaffold_terrain_digest_over_the_grid_only", DUMP,
     "        terrain_digest = hashlib.sha256(payload).hexdigest()",
     "        terrain_digest = hashlib.sha256(\n"
     "            payload[:2 + 2 * size_x * size_z]).hexdigest()  # MUTANT",
     "corpus"),
    ("scaffold_resources_lists_the_whole_map", DUMP,
     "    for row in real:\n"
     "        path = files.by_sector[row.sector]\n"
     "        if path.name in seen:",
     "    for row in files.rows:  # MUTANT: every row of the MAP\n"
     "        path = files.by_sector[row.sector]\n"
     "        if path.name in seen:", "corpus"),
    ("scaffold_pad_rows_in_map_states", DUMP,
     "    states, sheets = [], {}\n    for row in real:",
     "    states, sheets = [], {}\n    for row in rows:  # MUTANT: pads included",
     "corpus"),
    ("scaffold_dead_slots_read_a_byte_at_a_time", DUMP,
     '                else b"".join(w.to_bytes(2, "little") for w in slots).hex()),',
     '                else b"".join(w.to_bytes(2, "little") for w in slots).hex()[::2]),'
     "  # MUTANT", "corpus"),
]

HARNESS = {"corpus": ["tests/build_corpus.py", "--limit", LIMIT],
           "unit": ["-m", "pytest", "-q", "-p", "no:cacheprovider",
                    "--color=no", "tests/test_build.py"]}

ANSI = re.compile(r"\x1b\[[0-9;]*m")

# The unit harness needs an interpreter that HAS pytest. `sys.executable` is
# whatever ran this script -- on this box, /usr/bin/python3, which does not.
# `python -m pytest` then exits 1 with "No module named pytest" and the grader
# reads a run that never happened as CLEAN: twenty checks in a row read BLIND
# while every one of them was fine. Check the grader before the check.
VENV = PKG / ".venv" / "bin" / "python3"
PYTHON = str(VENV) if VENV.exists() else sys.executable


def run(which, cwd):
    # cwd is the scratch, and `''` leads sys.path, so the mutated copy is the
    # one that imports -- the editable finder in the venv sits behind
    # PathFinder and never gets asked.
    p = subprocess.run([PYTHON, *HARNESS[which]], cwd=str(cwd),
                       capture_output=True, text=True,
                       env=dict(os.environ), timeout=1800)
    # pytest colourises even into a pipe on this box, and `FAILED\x1b[0m
    # tests/...` does not match a regex anchored on `FAILED `. That alone made
    # nineteen live checks read BLIND while every one of them was going red.
    return ANSI.sub("", p.stdout + p.stderr)


def failures(out, which):
    """The named checks that are red -- the unit a seed is graded in."""
    if which == "unit":
        if not re.search(r"\d+ (passed|failed|error)", out):
            # A run that never happened must never read as clean.
            return ["HARNESS_DID_NOT_RUN"]
        return sorted(set(re.findall(r"^FAILED [^:]+::(\w+)", out, re.M)))
    bad = sorted({line.split()[1].rstrip(":") for line in out.splitlines()
                  if line.startswith(("MISMATCH", "REFUSED"))})
    if not re.search(r"BUILD (\d+)/\1 EXACT", out):
        bad.append("NOT_EXACT")
    m = re.search(r"warned=(\d+)", out)
    if m is None or int(m.group(1)) != EXPECTED_WARNED:
        bad.append("WARNED_MOVED")
    return sorted(set(bad))


EXPECTED_WARNED = None          # measured from the clean baseline


def scratch(tmp):
    dst = Path(tmp) / "pkg"
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(PKG, dst, symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", ".blender_*",
                                                  ".pytest_cache", "*.egg-info"))
    return dst


def main():
    global EXPECTED_WARNED
    with tempfile.TemporaryDirectory(prefix="exmateria-build-mutate-") as tmp:
        clean = run("corpus", scratch(tmp))
        m = re.search(r"warned=(\d+)", clean)
        EXPECTED_WARNED = int(m.group(1)) if m else -1
        base = {"corpus": failures(clean, "corpus")}
        base["unit"] = failures(run("unit", scratch(tmp)), "unit")
        for which, red in base.items():
            print(f"BASELINE {which}: {red or 'clean'}"
                  + (f" (warned={EXPECTED_WARNED})" if which == "corpus" else ""),
                  flush=True)
            if red:
                print("FAIL: the unmutated scratch copy is already red; "
                      "a mutation audit on a red baseline grades nothing")
                return 1

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
            print(f"{'CAUGHT ' if caught else 'BLIND  '} {label:44} -> "
                  f"{caught[:5] or 'NOTHING'}", flush=True)
            if not caught:
                blind.append(label)

    print(f"\n{len(MUTATIONS) - len(blind)}/{len(MUTATIONS)} seeds caught; "
          f"blind: {blind or 'none'}")
    print("PASS" if not blind else "FAIL")
    return 1 if blind else 0


if __name__ == "__main__":
    raise SystemExit(main())
