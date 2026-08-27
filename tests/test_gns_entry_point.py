"""ADR-0004 decision 31 part 2: the picked GNS path is the ENTIRE address.

`File > Import` picks `MAP###.GNS` and the addon asks for nothing else -- not
the extracted disc tree, not the map number.  Both are already in the path: the
disc tree is its parent, and the number is `name[3:6]`, which
`mapfile.map_numbers` has always parsed that way.

This is an ADDITION.  `bind`/`dump`/`build` keep their `(map_dir, number)`
signatures -- 28 call sites and both CLIs depend on them -- so the entry point
resolves an address and hands it to the existing calls rather than replacing
them.
"""

import pytest

from exmateria_map import corpus, dump, mapfile

MAP_DIR = corpus.map_dir()
needs_corpus = pytest.mark.skipif(
    MAP_DIR is None, reason="no extracted disc tree; set EXMATERIA_ASSETS_DIR")


# ---------------------------------------------------------------------------
# the address
# ---------------------------------------------------------------------------

def test_a_gns_path_addresses_its_own_directory_and_number(tmp_path):
    gns = tmp_path / "MAP022.GNS"
    gns.write_bytes(b"")
    assert mapfile.address(gns) == (tmp_path, 22)


def test_the_address_is_the_one_bind_already_takes(tmp_path):
    """The whole point of the entry point: what it returns is spendable at the
    existing signature, unchanged."""
    gns = tmp_path / "MAP007.GNS"
    gns.write_bytes(b"")
    map_dir, number = mapfile.address(gns)
    assert (map_dir / f"MAP{number:03d}.GNS") == gns


@needs_corpus
def test_every_gns_on_the_disc_addresses_the_number_map_numbers_parses():
    """`map_numbers` is the incumbent parser.  A second spelling of the same
    rule that disagreed with it on even one of 121 files would be a map opened
    as a different map."""
    numbers = mapfile.map_numbers(MAP_DIR)
    assert len(numbers) == 121
    for number in numbers:
        gns = MAP_DIR / f"MAP{number:03d}.GNS"
        assert mapfile.address(gns) == (MAP_DIR, number)


@pytest.mark.parametrize("name", [
    "MAP022.BIN",        # not a GNS at all
    "MAP022.8",          # a resource beside the GNS, easy to pick by mistake
    "MAPXXX.GNS",        # right shape, no number
    "GNS.GNS",           # too short to slice
    "TEST.GNS",          # a GNS-suffixed file that is not a map
])
def test_a_path_that_is_not_a_map_gns_is_a_named_refusal(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"")
    with pytest.raises(mapfile.AddressError) as e:
        mapfile.address(path)
    assert name in str(e.value)


def test_a_gns_that_is_not_there_refuses_by_name(tmp_path):
    """Without this the artist meets `FileNotFoundError` from inside `bind`,
    which reads as the addon being broken rather than the pick being wrong."""
    with pytest.raises(mapfile.AddressError):
        mapfile.address(tmp_path / "MAP022.GNS")


# ---------------------------------------------------------------------------
# decision 31 part 3: what the import browser's dropdown may offer
# ---------------------------------------------------------------------------
#
# `dump.arrangements()` enumerates the arrangements that NAME a mesh row. Not
# all of them can be dumped: 627 of 796 mesh resources carry no 0x40 chunk, and
# an arrangement whose every mesh row is chunkless has no geometry to import.
# Measured over the disc: 197 named, 148 dumpable.
#
# §31 pins the dropdown's population as "101 of 121 maps offer exactly one" and
# "20 offer more, up to five" -- and those are the DUMPABLE counts, not the
# named ones (named gives 84 and 37). Offering a named-but-chunkless
# arrangement would put an entry in the dropdown that refuses when picked, so
# the dropdown reads the dumpable list.

@needs_corpus
def test_the_dumpable_arrangements_are_the_ones_dump_actually_dumps():
    """The oracle is `dump.dump` itself -- the incumbent, which does the whole
    job. `dumpable_arrangements` only probes for the 0x40 chunk, so the two are
    different implementations of the same question and can disagree."""
    disagreed = []
    for number in mapfile.map_numbers(MAP_DIR):
        truth = []
        for a in dump.arrangements(MAP_DIR, number):
            try:
                dump.dump(MAP_DIR, number, a)
            except dump.DumpError:
                continue
            truth.append(a)
        if dump.dumpable_arrangements(MAP_DIR, number) != truth:
            disagreed.append(number)
    assert not disagreed, (
        f"{len(disagreed)} map(s) where the dropdown would offer an "
        f"arrangement that refuses when picked: {disagreed}")


@needs_corpus
def test_the_disc_names_197_arrangements_and_148_of_them_dump():
    named = sum(len(dump.arrangements(MAP_DIR, n))
                for n in mapfile.map_numbers(MAP_DIR))
    dumpable = sum(len(dump.dumpable_arrangements(MAP_DIR, n))
                   for n in mapfile.map_numbers(MAP_DIR))
    assert (named, dumpable) == (197, 148)


@needs_corpus
def test_101_maps_offer_one_arrangement_and_20_offer_more():
    """§31's stated population, and the reason the control is invisible on five
    maps in six. A build verified only on the 101 would pass with a dropdown
    hardcoded to zero."""
    sizes = [len(dump.dumpable_arrangements(MAP_DIR, n))
             for n in mapfile.map_numbers(MAP_DIR)]
    assert len(sizes) == 121
    assert sum(1 for s in sizes if s == 1) == 101
    assert sum(1 for s in sizes if s > 1) == 20
    assert max(sizes) == 5
    assert min(sizes) == 1, "a map with nothing to offer would be unimportable"


@needs_corpus
def test_map001_names_two_arrangements_and_only_the_first_carries_geometry():
    """The named/dumpable split on the map the addon's own fixtures use."""
    assert dump.arrangements(MAP_DIR, 1) == [0, 1]
    assert dump.dumpable_arrangements(MAP_DIR, 1) == [0]


@needs_corpus
def test_map011_is_the_five_arrangement_map_the_dropdown_has_to_survive():
    assert dump.arrangements(MAP_DIR, 11) == [0, 1, 2, 3, 4, 5]
    assert dump.dumpable_arrangements(MAP_DIR, 11) == [0, 1, 2, 3, 5]
