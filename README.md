# exmateria-map

FFT PSX map resources — read, write, and prove byte-exactness against the disc.

Stdlib-only, `requires-python >= 3.11`. That floor is the intersection of every
consumer: `fft-iso-patcher` (3.11), the `godot-learning` tools venv (3.13), and
Blender 5.2's system Python (3.14). A Blender addon cannot `pip install`, so the
no-dependency rule is a constraint, not a preference.

## Installing the Blender addon

Download **`exmateria_map-<version>.zip`** from
[Releases](https://github.com/timbermania/ExMateria-Map/releases). Leave it
zipped — Blender installs the zip and unpacks it itself.

In Blender: **Edit ▸ Preferences ▸ Add-ons**, then whichever of these your
Blender shows —

- the **▾** menu at the top right ▸ **Install from Disk…**, or
- an **Install…** button, on a Blender whose Extensions system isn't running.

— pick the zip, and tick the checkbox beside **ExMateria Map** to enable it.
The two panels differ only in the button: this is a legacy `bl_info` add-on,
and both routes land it in `scripts/addons/exmateria_map` and register it.
Which one you get is decided by whether Blender's own Extensions add-on
registered, not by anything shipped here. (Dropping the zip onto the Blender
window goes through that same Extensions machinery on 4.2 and up; the
Preferences route above works either way, which is why it is the one written
down.)

It has no dependencies to install — the package is stdlib-only and the addon
vendors it, which is the whole reason for that rule (a Blender addon cannot
`pip install`). Blender 4.0 is the declared floor; 5.2 is what it is developed
and tested against.

**Check that it took.** On enable the addon prints its own provenance to the
console:

```
EXMATERIA-MAP: addon 0.2.0 loaded from /…/scripts/addons/exmateria_map
```

That line exists because "am I looking at my own work?" had no answer once and
cost a round trip. On Windows the console is **Window ▸ Toggle System Console**;
on Linux and macOS, start Blender from a terminal.

**To update**, install the newer zip the same way — it overwrites — and restart
Blender, because Python caches imported modules for the life of the process.

**Working from a clone** instead of a release: don't copy the tree, link it.
`tools/dev_install.sh` symlinks `addons/exmateria_map` into every Blender it
finds under `~/.config/blender`, so there is only ever one copy and "did my
edit land" stops being a question you can get wrong.

### Building the zip

```bash
python3 tools/make_addon_zip.py            # -> dist/exmateria_map-<version>.zip
python3 tests/blender_release_zip.py       # installs it into a scratch Blender and grades it
```

The builder is deterministic and drops `__pycache__`, `*.pyc` and the
agent-facing `CLAUDE.md`; the version comes from `bl_info`. The grader is the
one that matters — a zip missing `_vendor/` still installs, still enables and
still registers every operator, and only fails later when an artist picks a
`MAP###.GNS`, so the suite installs the actual artifact into an isolated
Blender and checks that what registered came from the zip.

## The two legs

`dump` reads a base map into the schema-v1 interchange document
(`docs/interchange-schema-v1.md`); `build` turns a document back into the map's
resource bundle. The Blender addon speaks the document in between, and the
patcher (`fft-iso-patcher`) ingests the bundle.

```bash
uv run exmateria-map-dump  22 0 ./MAP022.a0      # document + PNG sidecars
uv run exmateria-map-build ./MAP022.a0/MAP022.a0.json ./bundle
```

### Or don't run either — the addon vendors this package

An artist installing the Blender addon should never meet those two commands.
The addon ships a verbatim copy of this package under `_vendor/`, so:

- **File ▸ Import ▸ FFT Map (.GNS)** — pick a `MAP###.GNS` out of the extracted
  disc tree. The path is the whole address: its folder is the tree and
  `name[3:6]` is the number, so nothing else is asked for. A sidebar dropdown
  picks the arrangement on the 20 maps that have more than one.
- **File ▸ Export ▸ FFT Map bundle** — pick a folder and get the GNS verbatim
  plus one blob per resource: the same bundle `exmateria-map-build` writes,
  byte for byte, ready for the patcher.

Patching the ISO is still `fft-iso-patcher`'s job and still a CLI trip;
authorship is not. ADR-0004 decision 31.

`build`'s whole model is one sentence: **new bytes = base bytes, with the named
chunks replaced**. The `0x40` primary mesh, the `0x44` palettes, the `0x68`
terrain grid and the `0xB0` visible-angle table are written from the document;
every other byte — the grayscale set, the texture and palette animations, the
mesh animations, the unnamed slack — is carried *at its offset, by construction*.
The 45-byte light rig at `0x64` joins the written list only when a map state
declares an `authored_light_rig` and the document stamps `version: 2`; a
document that declares none is exactly the carried case above. There is no list of carried things and no digest promising
they survived, because nothing ever reads them. That is the difference between
this and a rebuilder: GaneshaDx rebuilds the resource from its own model and is
byte-exact on **0 of 795** mesh resources.

Exactly one chunk can be *created* rather than replaced. Ten of 169
geometry-carrying resources ship with no `0xB0` table at all, and against one of
those `build` manufactures a whole 4,096-B chunk — but only if the document adds
polygons or authors a visible-angle mask, and never for any other section. The
resource grows, so the bundle then needs a patcher run with `allow_relocate` and
free space to land in; `build` says so in a warning, because it never sees the
patcher's recipe and so cannot refuse on it.

Refusals are named and come in schema §10's order — format/version, base
identity by digest, pointer validity, polygon capacity against the engine's
arrays, terrain classification, fan-out correspondence. A refusal means `build` cannot write bytes it can
defend; it is never a warning that got louder.

## The round-trip instrument

`dump → build → cmp` over the whole corpus. It converts *"this writes the exact
same format GaneshaDx does"* from a claim into a measurement.

```bash
uv run exmateria-map-roundtrip            # report + PASS/FAIL
uv run exmateria-map-roundtrip --builder identity   # the carry ratchet's zero point
uv run exmateria-map-roundtrip --write-baseline
uv run pytest                             # the binding check
```

The corpus is the extracted disc tree under `project-assets/fft-extract/MAP/` —
**1,575 files: 121 GNS + 658 textures + 796 mesh resources**. It is local-only
and gitignored. Discovery honours `EXMATERIA_ASSETS_DIR`, the same env var
`fft-iso-patcher`'s tests use, then walks up for `project-assets/`.

### Two axes, both pass/fail

**Coverage** — how many files came back byte-identical, per class. A builder may
always fall back to carrying bytes through opaquely, so 100% is achievable by
construction and any miss is a *bug*. There is no exception list.

**Carry** — how many bytes were reproduced *opaquely* rather than reconstructed
from the model. This is the axis that measures progress, and it is a **ratchet**:
it may never rise. The per-region breakdown is diagnostic; the pass/fail is on the
per-class total, so a refactor that moves bytes between regions doesn't trip it.

`identity_builder` — copy everything, declare everything carried — is the honest
zero point, and it is still what the mutation seeds mutate. The real writer runs
the document leg: today the instrument reports **1575/1575 at 3.79% carry**
(mesh 14.62%, texture 2.74%, GNS 100% — the GNS is carried verbatim on purpose,
the #372 patcher contract). `PrimaryMesh` fell from 4,757,178 carried bytes to
200 and `PolygonRenderProperties` from 724,992 to 28,672; what is left is the
49 arrangements that carry no `0x40` chunk at all, so they have no document and
nothing to rebuild from. They are named in the run's report, not dropped.

### What it reports on failure

| class | reported |
|---|---|
| `length` | sizes differ — *first differing offset* is undefined when one file is a prefix of the other |
| `bytes` | file, first differing offset, owning section + delta, expected vs actual, total differing bytes, regions touched |
| `carry` | cmp green but carry rose |

```
MAP001.11 [mesh] first diff at 4336 -> GrayscalePalettes (+40); expected 0xCE, got 0x31
    1 differing byte(s) across: GrayscalePalettes
!! MAP005.8: HEADER POINTER TABLE DIFFERS -- every section boundary moved; attribution below is suspect
MAP010.9 [mesh] LENGTH: original 39,716 B, rebuilt 39,700 B (delta -16)
```

Attribution runs against the **original**, never the rebuilt file. The original is
the oracle; attributing against the writer's own output would let a wrong pointer
table relabel its own damage.

Per class: mesh resources attribute to a header section (`Terrain (+4)`), textures
to a pixel row (`Texture row 7, px 208-209`), GNS files to a record
(`GNS record[2] (+7)`).

### How it is binding

There is no CI in this repo, and this harness does not add any — the oracle is
ROM-derived data that cannot be published, and the format is frozen 1997 data that
cannot drift. What makes the bar binding is **`roundtrip_baseline.json`, checked
into git**: a regression is a diff in a tracked file that someone has to explain.

Three guards against silence:

- A **partial corpus raises** rather than reporting a smaller pass. `pytest` skips
  visibly when the corpus is absent; it never goes quietly green.
- An **end-to-end needle test** seeds a flip in the last mesh resource the loop
  reaches and requires exactly one failure — so "compared 1,575 files" cannot be
  confused with "compared none".
- A **mutation audit** — `python3 tests/build_mutation_audit.py` — seeds 33
  defects into the shipped `build`/`dump`/`document`/PNG code, one at a time in
  a scratch copy, and records which checks go red. **33/33 caught, none blind.**
  Four of the seeds are the four defects the workspace scaffold actually
  shipped, so "these checks would have caught them" is measured, not asserted.
  Its own grader has been the failure mode twice (an interpreter without
  `pytest`; pytest's ANSI codes breaking the `FAILED` regex) — a run with no
  test summary now reports `HARNESS_DID_NOT_RUN`, never silence.

### What it does not prove

The instrument answers for the corpus as `dump` can reach it. **49 arrangements
carry no primary mesh**, so they have no `geometry_source`, no document, and no
claim here; their 83 resources fall back to carrying and say so in the report.
Reading `148/148` as `1,575/1,575` is the error the report exists to prevent.

The **Blender leg** is guarded separately and does not run here:
`tests/blender_corpus.py` drives the real addon operators over the same 148
arrangements and asserts `export(import(doc)) == doc` whole-document. Composed
with this instrument that closes the chain — and `tests/blender_gns_bundle.py`
checks it directly, end to end: a `MAP###.GNS` imported into Blender and
exported back out as a bundle, asserted **byte for byte** against what
`exmateria-map-dump | exmateria-map-build` produces from the same untouched
map.

## Origins

`exmateria-map`'s home was settled in
[#360](https://github.com/timbermania/fft-monorepo/issues/360); the acceptance bar
and this instrument in
[#367](https://github.com/timbermania/fft-monorepo/issues/367). Both are tickets on
the map [Blender → FFT map authoring](https://github.com/timbermania/fft-monorepo/issues/355).
