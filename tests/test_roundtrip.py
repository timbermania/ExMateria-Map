"""Proof that the round-trip instrument reports correctly, and that the real
writer clears the bar it sets.

Two things live here, and they are different claims:

* **The instrument reports correctly.** Exercised by **seeded mutation**: take
  an original resource, damage it in a known way, and assert the harness names
  exactly that damage. A harness that cannot fail is not measuring anything, so
  these seeds stay even now that a writer exists.
* **The bar is achievable.** ``DocumentBuilder`` runs the real ``dump`` ->
  ``build`` chain over the corpus. This is the claim the seeds could never
  make: 1,575/1,575 byte-identical *through the schema-v1 document*, with the
  four modelled chunks out of the carry ratchet.
"""

from __future__ import annotations

import json

import pytest

from exmateria_map import corpus, roundtrip
from exmateria_map.corpus import CorpusError, Resource
from exmateria_map.roundtrip import BuildResult, identity_builder


MAP_DIR = corpus.map_dir()
pytestmark = pytest.mark.skipif(
    MAP_DIR is None,
    reason="corpus absent; set EXMATERIA_ASSETS_DIR to the extracted disc tree",
)


@pytest.fixture(scope="module")
def resources() -> list[Resource]:
    return corpus.load(MAP_DIR)


def _one(resources: list[Resource], name: str) -> Resource:
    for r in resources:
        if r.name == name:
            return r
    raise AssertionError(f"{name} missing from corpus")


def _flip(offset: int):
    """A builder that reproduces the file but flips one byte."""
    def builder(resource, original):
        data = bytearray(original)
        data[offset] ^= 0xFF
        return BuildResult(bytes(data), identity_builder(resource, original).carried)
    return builder


# --------------------------------------------------------------------------
# the anti-silence guard: a partial corpus must never read as a pass
# --------------------------------------------------------------------------

def test_corpus_is_complete(resources):
    counts = {k: sum(1 for r in resources if r.kind == k) for k in corpus.CLASSES}
    assert counts == corpus.EXPECTED_COUNTS
    assert len(resources) == corpus.EXPECTED_TOTAL == 1575


def test_partial_corpus_raises_rather_than_passing(tmp_path):
    (tmp_path / "MAP001.GNS").write_bytes(b"\x00" * 208)
    with pytest.raises(CorpusError):
        corpus.load(tmp_path)


# --------------------------------------------------------------------------
# seed 1 -- a byte flip is reported with its owning section
# --------------------------------------------------------------------------

def test_mesh_flip_names_offset_and_section(resources):
    res = _one(resources, "MAP001.11")
    original = res.path.read_bytes()
    offset = 4336                                   # GrayscalePalettes +40
    built = _flip(offset)(res, original)
    result = roundtrip.compare(res, original, built.data)

    assert result.status == "bytes"
    assert result.first_offset == offset
    assert result.owner == "GrayscalePalettes (+40)"
    assert result.expected_byte == original[offset]
    assert result.actual_byte == original[offset] ^ 0xFF
    assert result.differing_bytes == 1
    assert result.regions_touched == ("GrayscalePalettes",)
    assert not result.header_moved


def test_texture_flip_names_pixel_row(resources):
    res = next(r for r in resources if r.kind == "texture")
    original = res.path.read_bytes()
    built = _flip(1000)(res, original)
    result = roundtrip.compare(res, original, built.data)
    assert result.first_offset == 1000
    assert result.owner == "Texture row 7, px 208-209"
    assert result.regions_touched == ("row 7",)


def test_gns_flip_names_record(resources):
    res = _one(resources, "MAP001.GNS")
    original = res.path.read_bytes()
    built = _flip(47)(res, original)
    result = roundtrip.compare(res, original, built.data)
    assert result.first_offset == 47
    assert result.owner == "GNS record[2] (+7)"
    assert result.regions_touched == ("record[2]",)


# --------------------------------------------------------------------------
# seed 2 -- a moved pointer table is called out before its own damage
# --------------------------------------------------------------------------

def test_header_change_is_flagged_as_suspect(resources):
    res = _one(resources, "MAP001.11")
    original = res.path.read_bytes()
    built = _flip(104)(res, original)               # the Terrain pointer itself
    result = roundtrip.compare(res, original, built.data)

    assert result.header_moved is True
    assert result.first_offset == 104
    assert result.owner.startswith("Header:pointer-table[slot 0x68]")
    rendered = roundtrip.format_report(
        roundtrip.Report([result], {"mesh": 0}, {"mesh": 1}, {"mesh": len(original)}, {"mesh": {}}),
        [],
    )
    assert "HEADER POINTER TABLE DIFFERS" in rendered


# --------------------------------------------------------------------------
# seed 3 -- a length change is its own failure class
# --------------------------------------------------------------------------

def test_truncation_reports_length_not_offset(resources):
    res = _one(resources, "MAP001.11")
    original = res.path.read_bytes()

    def truncate(resource, data):
        return BuildResult(data[:-16], identity_builder(resource, data).carried)

    result = roundtrip.compare(res, original, truncate(res, original).data)
    assert result.status == "length"
    assert result.rebuilt_len == result.original_len - 16
    assert result.differing_bytes == 16
    assert result.first_offset is None              # every shared byte matches


def test_growth_reports_length(resources):
    res = _one(resources, "MAP001.11")
    original = res.path.read_bytes()

    def grow(resource, data):
        return BuildResult(data + b"\x00" * 4, identity_builder(resource, data).carried)

    result = roundtrip.compare(res, original, grow(res, original).data)
    assert result.status == "length"
    assert result.rebuilt_len == result.original_len + 4


# --------------------------------------------------------------------------
# seed 4 -- a rising carry ratchet fails, a falling one does not
# --------------------------------------------------------------------------

def test_rising_carry_is_a_regression(resources):
    report = roundtrip.run(resources[:5], identity_builder)
    lower = {"carry_total": {k: v - 1 for k, v in report.carry_total.items()}}
    higher = {"carry_total": {k: v + 1 for k, v in report.carry_total.items()}}
    assert roundtrip.carry_regressions(report, lower)        # rose above baseline
    assert not roundtrip.carry_regressions(report, higher)   # fell below baseline
    assert not roundtrip.carry_regressions(report, {"carry_total": report.carry_total})


def test_carry_regression_fails_even_when_bytes_match(resources):
    report = roundtrip.run(resources[:5], identity_builder)
    assert report.coverage_ok                                # cmp is green ...
    regressions = roundtrip.carry_regressions(
        report, {"carry_total": {k: 0 for k in report.carry_total}}
    )
    assert regressions                                       # ... and it still fails
    assert "CARRY REGRESSIONS" in roundtrip.format_report(report, regressions)


# --------------------------------------------------------------------------
# the live corpus, against the committed baseline
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def document_builder():
    """The real writer over the whole corpus. Built once: it decodes and
    re-encodes 658 texture sheets through PNG, which is the slow half."""
    return roundtrip.DocumentBuilder(MAP_DIR)


def test_the_real_writer_reproduces_the_whole_corpus(resources, document_builder):
    report = roundtrip.run(resources, document_builder)
    assert report.coverage_ok, roundtrip.format_report(report, [])
    assert report.matched == corpus.EXPECTED_COUNTS

    baseline = roundtrip.load_baseline()
    assert baseline is not None, "roundtrip_baseline.json must be committed"
    assert baseline["coverage"] == corpus.EXPECTED_COUNTS
    assert not roundtrip.carry_regressions(report, baseline)


def test_the_writer_refuses_nothing_on_the_corpus(document_builder):
    assert document_builder.refused == []
    assert document_builder.arrangements == 148


def test_every_arrangement_without_a_mesh_is_named_not_dropped(document_builder):
    """A file the document leg cannot reach must be countable, or 148/148
    reads as 1,575/1,575."""
    assert document_builder.skipped, "the skipped population is not empty"
    for label, reason in document_builder.skipped:
        assert "no primary mesh" in reason or "binding" in reason, (label, reason)


def test_the_identity_builder_is_now_a_carry_regression(resources):
    """The ratchet has to be live in the direction that matters. If carrying
    every byte still passed the baseline, the baseline would be recording
    nothing."""
    report = roundtrip.run(resources, identity_builder)
    baseline = roundtrip.load_baseline()
    regressions = roundtrip.carry_regressions(report, baseline)
    assert {r.kind for r in regressions} == {"mesh", "texture"}, regressions


def test_baseline_is_valid_json_and_covers_every_class():
    baseline = json.loads(roundtrip.DEFAULT_BASELINE.read_text())
    for kind in corpus.CLASSES:
        assert kind in baseline["carry_total"]
        assert kind in baseline["total_bytes"]


# --------------------------------------------------------------------------
# end-to-end: a needle in 1,575 files, seeded in the LAST one the loop reaches
# --------------------------------------------------------------------------

def test_corpus_run_finds_a_single_seeded_flip(resources):
    """Proves the run loop reaches every file, not just the early ones.

    A green ``test_full_corpus_...`` alone cannot distinguish "compared 1,575
    files" from "compared none". Seeding the final mesh resource and requiring
    exactly one failure does distinguish them.
    """
    target = [r for r in resources if r.kind == "mesh"][-1]
    offset = 200

    def builder(resource, original):
        base = identity_builder(resource, original)
        if resource.name != target.name:
            return base
        data = bytearray(original)
        data[offset] ^= 0xFF
        return BuildResult(bytes(data), base.carried)

    report = roundtrip.run(resources, builder)
    failures = report.failures
    assert len(failures) == 1, [f.name for f in failures]
    assert failures[0].name == target.name
    assert failures[0].first_offset == offset
    assert failures[0].differing_bytes == 1
    assert not report.coverage_ok
    assert report.matched[target.kind] == corpus.EXPECTED_COUNTS[target.kind] - 1
