"""One-shot fixture sidecar generator (outputs are committed; re-run after
the chain changes to refresh them).

Reads the REAL MAP001.a0 state group through the proven chain (schemav1)
and writes, next to the tracked fixture document:

- MAP001.a0.sheet-b57ddf71.png
    the MAP001.8 texture sheet as an 8-bit indexed PNG (16-entry PLTE from
    the Initial state's first CLUT, filter-0 rows) — the interchange
    sidecar the addon's reader must decode;
- MAP001.a0.sheet-b57ddf71.samples.json
    32 deterministic index samples + the PLTE, so the reader test has
    ground truth without the ROM;
and patches tests/fixtures/MAP001.a0.stub.json's geometry-source state with
the chain's real 16x16 palettes (schema-v1: int stp, hex colours).
"""
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    sv = load(PKG / "workspace" / "schemav1.py", "schemav1_fixture")
    doc, sheets = sv.doc_with_sheets(1, 0)

    fixture = json.loads((HERE / "fixtures" / "MAP001.a0.stub.json").read_text())
    sheet_name = fixture["map_states"][1]["texture_sheet"]
    assert sheet_name is not None, "fixture state 1 lost its texture_sheet"
    digest8 = sheet_name.rsplit("sheet-", 1)[1][:8]
    hit = [h for h in sheets if h.startswith(digest8)]
    assert len(hit) == 1, f"sheet digest {digest8} not in the chain's output"
    raw, sidecar = sheets[hit[0]]
    assert sidecar == sheet_name, f"chain names the sheet {sidecar}, fixture says {sheet_name}"

    # proven sheet decode (mapread.read_sheet): low nibble first, row-major
    indices = bytearray(len(raw) * 2)
    for i, b in enumerate(raw):
        indices[2 * i] = b & 0xF
        indices[2 * i + 1] = b >> 4
    assert len(indices) == 256 * 1024

    # the geometry-source state's palettes become the fixture's real CLUTs
    geo = doc["base"]["geometry_source"]
    state0 = [s for s in doc["map_states"] if s["resource"] == geo][0]
    assert state0.get("palettes"), f"{geo} carries no palettes"
    fixture["map_states"][0]["palettes"] = state0["palettes"]
    (HERE / "fixtures" / "MAP001.a0.stub.json").write_text(
        json.dumps(fixture, indent=1) + "\n")

    plte_hex = state0["palettes"][0]["colors"]
    out_png = HERE / "fixtures" / sheet_name
    n = sv.write_png_indexed(bytes(indices), plte_hex, out_png, 256, 1024)

    samples = {}
    for i in range(32):
        u, v, page = (i * 37) % 256, (i * 113) % 256, i % 4
        samples[f"{u},{page},{v}"] = indices[(page * 256 + v) * 256 + u]
    stem = sheet_name[:-4] if sheet_name.endswith(".png") else sheet_name
    (HERE / "fixtures" / (stem + ".samples.json")).write_text(
        json.dumps({"plte": plte_hex, "samples": samples}, indent=1) + "\n")
    print(f"wrote {out_png.name} ({n} bytes), "
          f"{stem}.samples.json, fixture palettes "
          f"({len(state0['palettes'])} CLUTs x 16)")


if __name__ == "__main__":
    main()
