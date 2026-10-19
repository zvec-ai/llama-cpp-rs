#!/usr/bin/env python3
"""Fetch an explicitly SHA256-pinned release archive, without running Cargo.

The caller must pin the archive digest in source control; do not discover it
from an unpinned remote checksums file during builds.
"""
import argparse
from pathlib import Path, PurePosixPath
import os
import re
import subprocess
import tarfile
import tempfile

from runtime import RELEASE, TARGETS, digest, verify


def extract(archive, destination):
    with tarfile.open(archive, "r:gz") as tar:
        names = set()
        total = 0
        for member in tar.getmembers():
            name = PurePosixPath(member.name)
            if (not member.isfile() or name.is_absolute() or ".." in name.parts
                    or "\\" in member.name or ":" in member.name or member.name in names):
                raise RuntimeError(f"Unsafe archive entry: {member.name}")
            names.add(member.name)
            total += member.size
            if total > 8 * 1024**3 or len(names) > 10000:
                raise RuntimeError("Native archive exceeds extraction limits")
        tar.extractall(destination, filter="data")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, choices=TARGETS)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{64}", args.sha256):
        parser.error("--sha256 must be a pinned lowercase SHA-256 digest")
    output = args.output.resolve()
    if output.exists():
        parser.error("--output must not already exist (no implicit cache replacement)")
    output.parent.mkdir(parents=True, exist_ok=True)
    name = f"llama-cpp-runtime-{RELEASE}-{args.target}.tar.gz"
    url = f"https://github.com/zvec-ai/llama-cpp-rs/releases/download/{RELEASE}/{name}"
    with tempfile.TemporaryDirectory(prefix=".llama-runtime-", dir=output.parent) as temporary:
        temporary = Path(temporary)
        archive = temporary / name
        subprocess.run(["curl.exe" if os.name == "nt" else "curl", "--fail", "--location", "--proto", "=https",
                        "--retry", "3", "--max-time", "1200", "--output", str(archive), url], check=True)
        if digest(archive) != args.sha256:
            raise RuntimeError("Archive SHA-256 mismatch")
        extracted = temporary / "bundle"
        extracted.mkdir()
        extract(archive, extracted)
        manifest = verify(extracted)
        if manifest["target"] != args.target:
            raise RuntimeError("Archive target mismatch")
        extracted.rename(output)
    print(f"LLAMA_CPP_BUILD_MODE=external\nLLAMA_CPP_RUNTIME_DIR={output}\n"
          f"LLAMA_CPP_RUNTIME_MANIFEST_SHA256={digest(output / 'manifest.json')}")


if __name__ == "__main__":
    main()
