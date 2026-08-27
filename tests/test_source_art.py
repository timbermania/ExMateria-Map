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


def hand_rolled_rgb_png(rgb, w, h, filt):
    """A truecolour PNG encoded HERE, under one chosen scanline filter.

    An independent oracle: `write_rgb_png` writes filter 1 and nothing else,
    so a round trip through it grades one fifth of the reader.  A painting may
    arrive from any tool, and a filter the reader gets wrong corrupts it
    silently -- there is no checksum on the picture, only on the chunk.
    """
    import struct as _s
    import zlib as _z
    raw = bytearray()
    prev = bytes(3 * w)
    for y in range(h):
        row = rgb[3 * y * w:3 * (y + 1) * w]
        raw.append(filt)
        for x in range(len(row)):
            left = row[x - 3] if x >= 3 else 0
            up = prev[x]
            upleft = prev[x - 3] if x >= 3 else 0
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

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", _s.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", _z.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


@pytest.mark.parametrize("filt", [0, 1, 2, 3, 4])
def test_the_reader_honours_every_scanline_filter(filt):
    rgb = a_painting(32, 24)
    w, h, back = png_indexed.read_rgb_png(
        hand_rolled_rgb_png(rgb, 32, 24, filt))
    assert (w, h) == (32, 24)
    assert back == rgb, f"filter {filt} decodes wrong"
