#!/usr/bin/env python3
"""Download all public release assets without credentials and compare prepared pins."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import tempfile

from fetch import extract
from runtime import RELEASE, TARGETS, asset_stem, digest, verify


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    names = [asset_stem(t, v) + suffix for t, variants in TARGETS.items() for v in variants
             for suffix in (".tar.gz", ".manifest.json")]
    names += ["runtime-lock.json", "SHA256SUMS", "validation-report.md"]
    args.output.mkdir(parents=True, exist_ok=False)

    def download(name):
        output = args.output / name
        url = f"https://github.com/zvec-ai/llama-cpp-rs/releases/download/{RELEASE}/{name}"
        subprocess.run(["curl", "--fail", "--location", "--silent", "--show-error", "--proto", "=https",
                        "--retry", "5", "--retry-all-errors", "--max-time", "1200", "--output", str(output), url], check=True)
        if digest(output) != digest(args.expected / name):
            raise RuntimeError(f"Public download differs from prepared asset: {name}")
        return name

    with ThreadPoolExecutor(max_workers=4) as pool:
        for name in pool.map(download, names):
            print(f"Public SHA256 verified: {name}", flush=True)
    for target, variants in TARGETS.items():
        for variant in variants:
            stem = asset_stem(target, variant)
            with tempfile.TemporaryDirectory() as temp:
                bundle = Path(temp) / "bundle"
                extract(args.output / (stem + ".tar.gz"), bundle)
                if verify(bundle) != json.loads((args.output / (stem + ".manifest.json")).read_text()):
                    raise RuntimeError("Public archive and sidecar differ")
    print("Verified all 19 public assets and all 8 SDK payloads without GitHub credentials")


if __name__ == "__main__":
    main()
