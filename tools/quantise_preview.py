#!/usr/bin/env python3
"""Look at what the quantiser does — three panels, side by side.

`ADR-0007` builds the quantiser against a computable baseline and reports the
result as an error number. A number is the right thing to ASSERT and the wrong
thing to judge a picture by, so this writes the picture:

    original  |  naive 4x2x2 baseline  |  quantised

Both 16-colour panels are legal **CLUT rows** — every entry on the BGR555
lattice — so what you are looking at is what the disc could actually hold.

    python3 tools/quantise_preview.py <image> [-o out.png] [--max-side 512]
    python3 tools/quantise_preview.py --sheet <MAP###.a#.json> [--state N]

`--sheet` takes a dumped map document and expands its texture sheet under one
state's sixteen CLUT rows, which is the project's own art rather than a
photograph: 256 colours of real FFT tileset in, sixteen out.

ImageMagick decodes the input (arbitrary PNG/JPEG is not a job for a stdlib
subset); the output is written here with `zlib`, so nothing downstream needs it.
"""
import argparse
import json
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG / "addons" / "exmateria_map"))

import quantise as Q                                            # noqa: E402
from png_indexed import read_indexed_png                        # noqa: E402

SPLITS = ((4, 2, 2), (2, 4, 2), (2, 2, 4))


# ---------------------------------------------------------------------------
# Getting pixels in, and a picture out.
# ---------------------------------------------------------------------------

def load_image(path, max_side):
    """-> (width, height, bytes of RGB). Downscaled, because the quantiser's
    cost is in DISTINCT colours and a full-resolution photograph has more of
    them than the picture needs to make its point."""
    geom = ["-resize", f"{max_side}x{max_side}>"]
    dims = subprocess.run(["magick", str(path), *geom, "-format", "%w %h",
                           "info:-"], capture_output=True, check=True)
    w, h = (int(v) for v in dims.stdout.split()[:2])
    raw = subprocess.run(["magick", str(path), *geom, "-depth", "8", "RGB:-"],
                         capture_output=True, check=True)
    return w, h, raw.stdout


def load_sheet(doc_path, state):
    """A dumped map document's texture sheet, expanded under one state's
    sixteen rows -- the 256-colour form the artist would be editing."""
    doc = json.loads(Path(doc_path).read_text())
    sheets = [st["texture_sheet"] for st in doc["map_states"]
              if st.get("texture_sheet")]
    rows = [st["palettes"] for st in doc["map_states"] if st.get("palettes")]
    if not sheets or not rows:
        raise SystemExit(f"{doc_path} carries no sheet + palette pair")
    w, h, idx, _pal, _a = read_indexed_png(
        (Path(doc_path).parent / sheets[0]).read_bytes())
    rows = rows[min(state, len(rows) - 1)]

    def rgb(text):
        s = text.lstrip("#")
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))

    # The sheet is one 4bpp image read by polygons bound to DIFFERENT rows, and
    # which texel belongs to which row is a property of the mesh, not the file.
    # Banding it -- row r over the r-th sixteenth of the height -- shows all
    # 256 colours at once without inventing a binding that is not in the data.
    band = h // 16
    out = bytearray()
    for y in range(h):
        entries = [rgb(c) for c in rows[min(y // band, 15)]["colors"]]
        for x in range(w):
            out += bytes(entries[idx[y * w + x] & 0x0F])
    return w, h, bytes(out)


def load_groups(doc_path):
    """-> (texel index -> the palette group that reads it, sorted group list).

    A sheet is NOT a sixteen-colour image. It is a 4bpp image read by polygons
    each bound to one of the state's sixteen **CLUT rows**, so the budget is
    sixteen colours PER GROUP -- MAP022 a0 uses ten of them, which is 160.
    Quantising a whole sheet to one row is ADR-0007's rung 5, the tail case
    where every row is referenced and full, and it is the WORST case rather
    than the normal one.

    A polygon claims the bounding box of its UVs, which is the convention the
    corpus measurements already use (`workspace/corpus423.py`). About half a
    percent of texels are claimed by polygons on DIFFERENT rows -- ADR-0007's
    conflict set -- and those are attributed to the lowest-numbered claimant.
    """
    doc = json.loads(Path(doc_path).read_text())
    tex = [p for p in doc["polygons"] if "uv" in p]
    owners, shared = {}, 0
    for p in tex:
        pg, pid = p["texture_page"], p["palette_id"]
        us = [c[0] for c in p["uv"]]
        vs = [c[1] for c in p["uv"]]
        for v in range(min(vs), max(vs) + 1):
            base = (pg * 256 + v) * 256
            for u in range(min(us), max(us) + 1):
                cur = owners.get(base + u)
                if cur is not None and cur != pid:
                    shared += 1
                owners[base + u] = min(cur, pid) if cur is not None else pid
    return owners, sorted({p["palette_id"] for p in tex}), shared


def sheet_per_group(doc_path, owners, state):
    """The document's own sheet as a true-colour image: every texel coloured
    under the CLUT row the mesh says reads it. Texels no polygon claims get
    nothing, because nothing on the map displays them."""
    doc = json.loads(Path(doc_path).read_text())
    sheets = [st["texture_sheet"] for st in doc["map_states"]
              if st.get("texture_sheet")]
    rows = [st["palettes"] for st in doc["map_states"] if st.get("palettes")]
    w, h, idx, _p, _a = read_indexed_png(
        (Path(doc_path).parent / sheets[0]).read_bytes())
    rows = rows[min(state, len(rows) - 1)]

    def rgb(text):
        s = text.lstrip("#")
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))

    table = [[rgb(c) for c in r["colors"]] for r in rows]
    out = bytearray(w * h * 3)
    for texel, pid in owners.items():
        out[texel * 3:texel * 3 + 3] = bytes(table[pid][idx[texel] & 0x0F])
    return bytes(out)


def write_rgb_png(path, w, h, rgb):
    raw = bytearray()
    for y in range(h):
        raw.append(0)                       # filter 0, as png_indexed does
        raw += rgb[y * w * 3:(y + 1) * w * 3]

    def chunk(tag, body):
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

    Path(path).write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b""))


def map_through(rgb, w, h, row):
    """Every pixel replaced by its nearest entry -- what the sheet looks like
    once it is a legal 4bpp image under `row`."""
    cache, out = {}, bytearray()
    for i in range(w * h):
        c = bytes(rgb[i * 3:i * 3 + 3])
        hit = cache.get(c)
        if hit is None:
            hit = cache[c] = bytes(row[Q._nearest(tuple(c), row)[0]])
        out += hit
    return bytes(out)


def swatches(row, w, height=28):
    """The sixteen entries as a strip, so the palette is visible and not just
    its effect."""
    out = bytearray()
    for _ in range(height):
        for x in range(w):
            out += bytes(row[min(x * 16 // w, 15)])
    return bytes(out)


def panel(tmp, name, w, h, rgb, row=None):
    """One labelled column: the image, and under it its palette strip."""
    img = tmp / f"{name}.png"
    if row is None:
        write_rgb_png(img, w, h, rgb)
        return img
    write_rgb_png(img, w, h + 28, rgb + swatches(row, w))
    return img


def groups_mode(args):
    """The comparison that matters: one CLUT row for the WHOLE sheet against
    one row per palette group.

    Quantising a sheet to sixteen colours is the question almost nobody is
    asking. A mesh binds each polygon to one of the state's sixteen rows, so
    the real budget is sixteen colours PER GROUP -- and a preview that ignores
    that is showing ADR-0007's rung 5, the tail case, as though it were the
    normal one.
    """
    owners, used, shared = load_groups(args.groups)
    w, h = 256, 1024
    if args.image:
        raw = subprocess.run(["magick", str(args.image), "-resize", f"{w}x{h}!",
                              "-depth", "8", "RGB:-"],
                             capture_output=True, check=True).stdout
        src = Path(args.image).name
    else:
        # No image: the document's OWN art, each texel coloured under the row
        # its polygons actually read. This is the case worth looking at --
        # vanilla art is already legal per group, so the right-hand panel must
        # come back UNCHANGED, and whatever the middle panel loses is exactly
        # what collapsing ten rows into one costs.
        raw = sheet_per_group(args.groups, owners, args.state)
        src = f"{Path(args.groups).name}'s own sheet"

    dark = bytes((16, 16, 16))
    claimed = sorted(owners)
    print(f"  source            {src}")
    print(f"  document          {Path(args.groups).name}")
    print(f"  palette groups    {len(used)} of 16 rows -> {used}")
    print(f"  claimed texels    {len(claimed):,} of {w * h:,} "
          f"({100.0 * len(claimed) / (w * h):.1f}% of the sheet)")
    print(f"  shared by 2 rows  {shared:,} (ADR-0007's conflict set)")

    def bag_of(texels):
        out = {}
        for t in texels:
            c = tuple(raw[t * 3:t * 3 + 3])
            out[c] = out.get(c, 0) + 1
        return out

    whole = bag_of(claimed)
    one_row = Q.quantise(whole, args.k)
    one_err = Q.error(whole, one_row)

    by_group, per_err, total = {}, 0.0, 0
    for pid in used:
        texels = [t for t in claimed if owners[t] == pid]
        if not texels:
            continue
        bag = bag_of(texels)
        by_group[pid] = Q.quantise(bag, args.k)
        per_err += Q.error(bag, by_group[pid]) * len(texels)
        total += len(texels)
    per_err /= max(1, total)

    print(f"  distinct colours  {len(whole):,}")
    print(f"  ONE row  ({args.k:>3} colours)   error {one_err:>10,.1f}")
    print(f"  PER group ({len(by_group) * args.k:>3} colours)  error "
          f"{per_err:>10,.1f}   x{one_err / per_err:.1f} better"
          if per_err else "  PER group: exact")

    def render(row_for):
        out = bytearray(dark * (w * h))
        cache = {}
        for t in claimed:
            row = row_for(t)
            c = bytes(raw[t * 3:t * 3 + 3])
            key = (id(row), c)
            hit = cache.get(key)
            if hit is None:
                hit = cache[key] = bytes(row[Q._nearest(tuple(c), row)[0]])
            out[t * 3:t * 3 + 3] = hit
        return bytes(out)

    orig = bytearray(dark * (w * h))
    for t in claimed:
        orig[t * 3:t * 3 + 3] = raw[t * 3:t * 3 + 3]

    tmp = Path(args.out).resolve().parent / ".quantise_preview"
    tmp.mkdir(exist_ok=True)
    cols = [
        (f"original -- {len(whole):,} colours (grey = not on the map)",
         panel(tmp, "a_original", w, h, bytes(orig))),
        (f"ONE row for the sheet -- {args.k} colours, error {one_err:,.0f}",
         panel(tmp, "b_one", w, h, render(lambda t: one_row), one_row)),
        (f"one row PER GROUP -- {len(by_group) * args.k} colours, error "
         f"{per_err:,.0f}",
         panel(tmp, "c_groups", w, h, render(lambda t: by_group[owners[t]]))),
    ]
    cmd = ["magick", "montage"]
    for label, img in cols:
        cmd += ["-label", label, str(img)]
    cmd += ["-tile", "3x1", "-geometry", "+10+10", "-background", "#1b1b1b",
            "-fill", "#e6e6e6", "-pointsize", "15", "-depth", "8",
            f"PNG24:{args.out}"]
    subprocess.run(cmd, check=True)
    print(f"\n  wrote {args.out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("image", nargs="?", help="any image ImageMagick can read")
    ap.add_argument("--sheet", help="a dumped MAP###.a#.json instead")
    ap.add_argument("--groups", metavar="DOC",
                    help="a dumped MAP###.a#.json whose polygon->CLUT-row "
                         "binding decides which texels share a palette; "
                         "compares one row for the whole sheet against one "
                         "row PER GROUP, which is the real budget")
    ap.add_argument("--state", type=int, default=0)
    ap.add_argument("-o", "--out", default="quantise_preview.png")
    ap.add_argument("--max-side", type=int, default=512)
    ap.add_argument("-k", type=int, default=16, help="entries in the row")
    args = ap.parse_args()
    if not args.image and not args.sheet and not args.groups:
        ap.error("give an image, or --sheet / --groups a dumped map document")

    if args.groups:
        return groups_mode(args)
    if args.sheet:
        w, h, rgb = load_sheet(args.sheet, args.state)
        source = f"{Path(args.sheet).name} state {args.state}"
    else:
        w, h, rgb = load_image(args.image, args.max_side)
        source = Path(args.image).name

    counts = {}
    for i in range(w * h):
        c = tuple(rgb[i * 3:i * 3 + 3])
        counts[c] = counts.get(c, 0) + 1

    naive = min((Q.error(counts, Q.naive_palette(s)), s) for s in SPLITS)
    naive_row = Q.naive_palette(naive[1])
    t0 = time.perf_counter()
    q_row = Q.quantise(counts, args.k)
    secs = time.perf_counter() - t0
    q_err = Q.error(counts, q_row)

    print(f"  source            {source}  ({w}x{h})")
    print(f"  distinct colours  {len(counts):,}  ->  {args.k}")
    print(f"  on the lattice    {sum(n for c, n in counts.items() if Q.on_lattice(c)) * 100.0 / (w * h):.1f}%"
          f" of pixels (the rest are UNREPRESENTABLE before any palette choice)")
    print(f"  naive {naive[1]}       error {naive[0]:>10,.1f}")
    print(f"  quantised         error {q_err:>10,.1f}"
          f"   {'x%.1f better' % (naive[0] / q_err) if q_err else '(exact)'}"
          f"   in {secs:.2f} s")

    tmp = Path(args.out).resolve().parent / ".quantise_preview"
    tmp.mkdir(exist_ok=True)
    cols = [
        (f"original -- {len(counts):,} colours",
         panel(tmp, "a_original", w, h, rgb)),
        (f"naive {naive[1]} -- error {naive[0]:,.0f}",
         panel(tmp, "b_naive", w, h, map_through(rgb, w, h, naive_row),
               naive_row)),
        (f"quantised -- error {q_err:,.0f}",
         panel(tmp, "c_quantised", w, h, map_through(rgb, w, h, q_row), q_row)),
    ]
    cmd = ["magick", "montage"]
    for label, img in cols:
        cmd += ["-label", label, str(img)]
    # PNG24 and -depth 8 on purpose: montage defaults to 16-bit RGBA here, and
    # `imv` renders that as a blank window rather than reporting anything.
    cmd += ["-tile", "3x1", "-geometry", "+10+10", "-background", "#1b1b1b",
            "-fill", "#e6e6e6", "-pointsize", "15", "-depth", "8",
            f"PNG24:{args.out}"]
    subprocess.run(cmd, check=True)
    print(f"\n  wrote {args.out}")


if __name__ == "__main__":
    main()
