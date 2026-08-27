"""Command-line front ends for the two interchange legs.

``exmateria-map-dump``  base map + arrangement -> document + PNG sidecars
``exmateria-map-build`` document + base map    -> the patcher's bundle directory

Both take the base map from the extracted disc tree (``EXMATERIA_ASSETS_DIR``,
else a discovered ``project-assets/``); neither ever touches an ISO. The
bundle ``build`` writes is the map leg's input (``fft-iso-patcher``,
``docs/map-leg-v1.md`` §1.2): the GNS with the disc's original LBAs, plus one
blob per non-pad resource of the arrangement.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import corpus
from .build import BuildRefusal, build_bundle
from .dump import DumpError, write_bundle
from .mapfile import BindError


def _map_dir(explicit: Path | None) -> Path:
    directory = explicit or corpus.map_dir()
    if directory is None:
        raise SystemExit("no corpus found; set EXMATERIA_ASSETS_DIR to the "
                         "extracted disc tree")
    return Path(directory)


def dump_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="exmateria-map-dump",
        description="Write the schema-v1 interchange document for one "
                    "(map, arrangement), plus its PNG sidecars.")
    parser.add_argument("map", type=int, help="map number, 0-125")
    parser.add_argument("arrangement", type=int, help="arrangement byte, 0-5")
    parser.add_argument("out", type=Path, help="directory to write into")
    parser.add_argument("--corpus", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        path = write_bundle(_map_dir(args.corpus), args.map, args.arrangement,
                            args.out)
    except (DumpError, BindError) as exc:
        print(f"cannot dump: {exc}", file=sys.stderr)
        return 1
    print(f"{path}")
    return 0


def build_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="exmateria-map-build",
        description="Turn an interchange document into the patcher's "
                    "resource bundle. Refusals are named and exit 1.")
    parser.add_argument("document", type=Path, help="the .json document")
    parser.add_argument("out", type=Path, help="bundle directory to write")
    parser.add_argument("--corpus", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        bundle = build_bundle(args.document, _map_dir(args.corpus), args.out)
    except (BuildRefusal, BindError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    for warning in bundle.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    total = sum(len(v) for v in bundle.resources.values())
    print(f"{bundle.name}: {len(bundle.resources)} resource(s), {total:,} B "
          f"+ {bundle.gns_name} -> {args.out}")
    return 0
