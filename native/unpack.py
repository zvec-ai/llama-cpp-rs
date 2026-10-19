#!/usr/bin/env python3
"""Extract one CI artifact with the same safety checks as release downloads."""
import argparse
from pathlib import Path
from fetch import extract
from runtime import digest, verify

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--artifacts", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
archives = list(args.artifacts.glob("*.tar.gz"))
if len(archives) != 1:
    raise RuntimeError(f"Expected one native archive, found {archives}")
archive = archives[0]
expected = archive.with_name(archive.name + ".sha256").read_text().split()[0]
if digest(archive) != expected:
    raise RuntimeError("Artifact archive checksum mismatch")
args.output.mkdir(parents=True, exist_ok=False)
extract(archive, args.output)
verify(args.output)
