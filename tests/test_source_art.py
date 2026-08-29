"""Source art is a document member `build` is blind to (ADR-0186 dec. 4, 5).

Decision 4 keeps the true-colour painting inside the interchange document --
the compile has no inverse, so the irreplaceable half of an authored map does
not live outside this package's artifact.  Decision 5 puts it in its own
top-level section rather than in `map_states[].texture_sheet`, so that `build`
stays blind to it **by construction**: `build` reads only what that field
names (`build.py:_sheet_bytes`), and never enumerates the document's keys.

This is where "by construction" is checked rather than asserted.
"""

import copy
import shutil
import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))

from exmateria_map import corpus                             # noqa: E402
from exmateria_map.build import build                        # noqa: E402
from exmateria_map.dump import dump                          # noqa: E402

MAP_DIR = corpus.map_dir()
pytestmark = pytest.mark.skipif(
    MAP_DIR is None,
    reason="corpus absent; set EXMATERIA_ASSETS_DIR to the extracted disc tree",
)


@pytest.fixture(scope="module")
def scratch(tmp_path_factory):
    out = tmp_path_factory.mktemp("source-art")
    shutil.copytree(MAP_DIR, out / "MAP")
    return out / "MAP"


#: What a converted document carries: one entry per map state, deduplicated
#: by the painting's own content hash, named from this section and never from
#: `map_states[].texture_sheet`.
SOURCE_ART = {
    "MAP022.a0.source-0ea1b3c7.png": {"states": [0, 1, 2]},
    "MAP022.a0.source-9f42d10b.png": {"states": [3]},
}


def test_build_writes_the_same_bytes_with_source_art_present(scratch):
    """The whole of decision 5, in one comparison.  A document carrying
    source art must build to the byte the same map as one without it --
    otherwise the compile's input has leaked into the disc."""
    document, _ = dump(MAP_DIR, 22, 0)

    without = build(copy.deepcopy(document), scratch)

    carrying = copy.deepcopy(document)
    carrying["source_art"] = copy.deepcopy(SOURCE_ART)
    with_art = build(carrying, scratch)

    assert with_art.gns == without.gns, "source art reached the GNS"
    assert (list(with_art.resources) == list(without.resources)), \
        "source art changed which resources `build` writes"
    for name, data in without.resources.items():
        assert with_art.resources[name] == data, \
            f"{name} differs when the document carries source art"
    assert with_art.modelled == without.modelled, \
        "source art changed which spans `build` claims to have written"


def test_source_art_does_not_raise_the_version_floor(scratch):
    """`version` names the OLDEST `build` that can honour the document
    (schema §2).  `authored_light_rig` raises it to 2 because a v1 `build`
    handed one emits a map that silently drops the artist's lighting -- a
    WRONG map.  Source art is not like that: a v1 `build` ignores it and
    emits the RIGHT map, because the compile has already written the sheet
    sidecars `map_states[].texture_sheet` names.

    So the floor stays where it was.  If this ever fails, source art has
    stopped being inert to `build` and decision 5 has been broken.
    """
    document, _ = dump(MAP_DIR, 22, 0)
    floor = document["version"]

    carrying = copy.deepcopy(document)
    carrying["source_art"] = copy.deepcopy(SOURCE_ART)
    build(carrying, scratch)                     # accepted at the same floor

    assert carrying["version"] == floor


def test_the_control_build_is_NOT_blind_to_map_states_texture_sheet(scratch):
    """The positive control the two tests above need.

    "`build` ignored source art" and "`build` ignores everything" look
    identical from a passing test.  This is the same document with the same
    kind of edit made to the field decision 5 says source art must never sit
    in -- and `build` notices immediately.  That is the difference decision 5
    is buying: one field is read, the other is not, and which one the
    painting lives in decides whether it can reach the disc.
    """
    from exmateria_map.build import BuildRefusal

    document, _ = dump(MAP_DIR, 22, 0)
    named = copy.deepcopy(document)
    changed = 0
    for state in named["map_states"]:
        if state.get("texture_sheet"):
            state["texture_sheet"] = "MAP022.a0.source-0ea1b3c7.png"
            changed += 1
    assert changed, "the fixture must actually rename a texture_sheet"

    with pytest.raises(BuildRefusal):
        build(named, scratch, sidecar_dir=scratch)


# ---------------------------------------------------------------------------
# The Painting's sidecar (ADR-0186 decision 5).
#
# Decision 4 forbids the Painting living only in the `.blend`, so it has to be
# written beside the document -- and the sheets already are, as PNGs, which
# makes schema §1's "document and all sidecars in one directory" hold for the
# irreplaceable half too.  The sheets' PNG is 8-bit INDEXED; a painting has no
# palette at all, so it needs the truecolour codec these two tests grade.
# ---------------------------------------------------------------------------

from exmateria_map import png_indexed                        # noqa: E402


def a_painting(w=256, h=1024):
    """A picture no two texels of which agree by accident, in all three
    channels independently, so a codec that transposed rows or dropped a
    channel cannot round-trip by luck."""
    return bytes(b for y in range(h) for x in range(w)
                 for b in ((x * 7 + y * 13) & 0xFF,
                           (x * 31 + y * 3) & 0xFF,
                           (x ^ (y * 5)) & 0xFF))


def test_a_painting_survives_the_png_round_trip_byte_for_byte():
    rgb = a_painting()
    w, h, back = png_indexed.read_rgb_png(png_indexed.write_rgb_png(rgb))
    assert (w, h) == (256, 1024)
    assert back == rgb


def test_the_painting_codec_refuses_an_indexed_png_rather_than_guessing():
    """The two sidecar kinds share a directory and a suffix, so the only thing
    telling them apart is the PNG's own colour type.  Read one as the other
    and every texel is wrong in a way no downstream check can name."""
    sheet = png_indexed.write_indexed_png(bytes(256 * 1024),
                                          [(0, 0, 0)] * 16)
    with pytest.raises(ValueError):
        png_indexed.read_rgb_png(sheet)
    with pytest.raises(ValueError):
        png_indexed.read_indexed_png(png_indexed.write_rgb_png(a_painting()))


def hand_rolled_png(data, w, h, filts, bpp, palette=None):
    """A PNG encoded HERE, choosing the scanline filter PER ROW.

    An independent oracle: our own writers emit filter 1 (paintings) and
    filter 0 (index sheets) and nothing else, so a round trip through them
    grades two fifths of one reader.  A painting may arrive from any tool, and
    a filter the reader gets wrong corrupts it silently -- the CRC covers the
    chunk, never the picture.

    `filts` is one filter per scanline, because that is what a PNG actually
    carries: the choice is per row, not per file.  ADR-0186 Amendment 13
    decision 57 vectorises filters 1 and 2 and leaves 3 and 4 on the byte
    loop, so a file that CHANGES filter between rows is the one that crosses
    both arms -- and a whole-picture decode, which is the obvious way to
    vectorise this, gets exactly that file wrong.

    `bpp` is the reader's other lane count: 3 for the Painting, 1 for the
    Sheet.  Both are graded, because the numpy Sub path reshapes by it.
    """
    import struct as _s
    import zlib as _z
    stride = bpp * w
    raw = bytearray()
    prev = bytes(stride)
    for y in range(h):
        row = data[y * stride:(y + 1) * stride]
        filt = filts[y]
        raw.append(filt)
        for x in range(stride):
            left = row[x - bpp] if x >= bpp else 0
            up = prev[x]
            upleft = prev[x - bpp] if x >= bpp else 0
            if filt == 0:
                raw.append(row[x])
            elif filt == 1:
                raw.append((row[x] - left) & 0xFF)
            elif filt == 2:
                raw.append((row[x] - up) & 0xFF)
            elif filt == 3:
                raw.append((row[x] - ((left + up) >> 1)) & 0xFF)
            else:
                raw.append((row[x] - png_indexed._paeth(left, up, upleft))
                           & 0xFF)
        prev = row

    def chunk(tag, body):
        return (_s.pack(">I", len(body)) + tag + body
                + _s.pack(">I", _z.crc32(tag + body) & 0xFFFFFFFF))

    ctype = 3 if palette is not None else 2
    out = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", _s.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0)))
    if palette is not None:
        out += chunk(b"PLTE", bytes(b for e in palette for b in e))
    return out + chunk(b"IDAT", _z.compress(bytes(raw), 6)) + chunk(b"IEND", b"")


def hand_rolled_rgb_png(rgb, w, h, filt):
    """`hand_rolled_png` with one filter on every scanline."""
    return hand_rolled_png(rgb, w, h, [filt] * h, 3)


@pytest.mark.parametrize("filt", [0, 1, 2, 3, 4])
def test_the_reader_honours_every_scanline_filter(filt):
    rgb = a_painting(32, 24)
    w, h, back = png_indexed.read_rgb_png(
        hand_rolled_rgb_png(rgb, 32, 24, filt))
    assert (w, h) == (32, 24)
    assert back == rgb, f"filter {filt} decodes wrong"

def a_sheet(w=64, h=48):
    """An index picture whose every row differs from the one above it, so an
    `Up` decode that dropped the previous row cannot round-trip by luck."""
    return bytes(((x * 5 + y * 3) & 0xF) for y in range(h) for x in range(w))


CYCLE = [0, 1, 2, 3, 4]


def test_the_reader_honours_a_file_that_changes_filter_every_scanline():
    """The file that separates a row-by-row decode from a whole-picture one.

    Decision 57's fast path is `cumsum` for Sub and an elementwise add for Up.
    Done over the whole IDAT at once -- which is how it was first written --
    both are correct only if every scanline chose the same filter.  Ours do.
    A painting exported by another tool does not have to, and PNG encoders
    routinely pick per row.  This is that file: five filters cycling, so every
    vectorised row is preceded by a byte-loop row and vice versa.
    """
    rgb = a_painting(32, 24)
    filts = [CYCLE[y % 5] for y in range(24)]
    w, h, back = png_indexed.read_rgb_png(hand_rolled_png(rgb, 32, 24,
                                                          filts, 3))
    assert (w, h) == (32, 24)
    assert back == rgb


@pytest.mark.parametrize("filt", [0, 1, 2, 3, 4])
def test_the_index_reader_honours_every_filter_on_its_own_lane_width(filt):
    """`_unfilter`'s `bpp` is 1 here and 3 for a painting, and the numpy Sub
    path RESHAPES by it -- so a lane count that only ever saw 3 would decode
    an index sheet's Sub rows as three interleaved sums of the wrong stride.
    Getting it wrong does not raise; it smears the picture."""
    idx = a_sheet()
    palette = [(i * 16, 255 - i * 16, i) for i in range(16)]
    png = hand_rolled_png(idx, 64, 48, [filt] * 48, 1, palette)
    w, h, back, pal, _alpha = png_indexed.read_indexed_png(png)
    assert (w, h) == (64, 48)
    assert back == idx, f"filter {filt} decodes wrong at bpp=1"
    assert pal == palette


def test_every_codec_gives_the_same_bytes_with_numpy_taken_away(monkeypatch):
    """Decision 53's fallback, FORCED.

    This module is the only one that keeps a pure-Python arm, because the
    package ships `dependencies = []` and three copies of this file are held
    byte-identical.  Decision 52's objection to a fallback -- that it is a
    second implementation of a byte-exact transform which nothing ever runs --
    applies here too unless something runs it.  This is that something: every
    codec is asked twice on the same input, once with numpy and once with the
    module's handle set to `None`, and the two must agree byte for byte.

    The numpy arm is asserted to be the LIVE one first.  Without that, a
    machine where the import failed would compare the fallback against itself
    and pass while grading nothing.
    """
    assert png_indexed.numpy is not None, (
        "numpy is in [dependency-groups] dev precisely so this arm is the "
        "one that runs by default; without it this test is vacuous")

    idx = a_sheet(256, 8)
    rgb = a_painting(32, 24)
    filts = [CYCLE[y % 5] for y in range(24)]
    paint_png = hand_rolled_png(rgb, 32, 24, filts, 3)
    sheet_png = hand_rolled_png(idx, 256, 8, [1] * 8, 1,
                                [(i, i, i) for i in range(16)])

    def all_four():
        return (png_indexed.pack_4bpp(idx),
                png_indexed.unpack_4bpp(png_indexed.pack_4bpp(idx)),
                png_indexed.write_rgb_png(rgb, 32, 24),
                png_indexed.read_rgb_png(paint_png),
                png_indexed.read_indexed_png(sheet_png))

    fast = all_four()
    monkeypatch.setattr(png_indexed, "numpy", None)
    slow = all_four()
    assert fast == slow
    # ...and the fallback is not merely self-consistent: it decodes what the
    # oracle encoded.
    assert slow[3][2] == rgb
    assert slow[1] == idx


def test_the_4bpp_pack_truncates_an_out_of_range_index_the_same_way_either_arm():
    """The masks are the only place the two arms could disagree on a value the
    disc format cannot hold.  An index above 15 is a compile bug; what must
    NOT happen is the numpy arm wrapping one way and the loop another, which
    would make the fallback a differently-wrong codec rather than the same
    one."""
    wild = bytes(range(256))
    fast = png_indexed.pack_4bpp(wild)
    import unittest.mock as _m
    with _m.patch.object(png_indexed, "numpy", None):
        slow = png_indexed.pack_4bpp(wild)
    assert fast == slow
    assert fast == bytes((wild[i] & 0xF) | ((wild[i + 1] & 0xF) << 4)
                         for i in range(0, 256, 2))
