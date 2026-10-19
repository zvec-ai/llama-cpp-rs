#!/usr/bin/env python3
"""Publish one immutable pre-release after the CI dependency gates succeed."""
import argparse
import json
import os
from pathlib import Path
import subprocess
from runtime import RELEASE, TARGETS, digest

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--assets", type=Path, required=True)
args = parser.parse_args()
assets = []
lines = []
manifests = {}
for target in TARGETS:
    name = f"llama-cpp-runtime-{RELEASE}-{target}.tar.gz"
    archive = args.assets / name
    expected = archive.with_name(name + ".sha256").read_text().split()[0]
    if digest(archive) != expected:
        raise RuntimeError(f"Release archive hash mismatch: {target}")
    path = args.assets / f"{target}.manifest.json"
    manifest = json.loads(path.read_text())
    if manifest["source_commit"] != os.environ["GITHUB_SHA"] or manifest["target"] != target:
        raise RuntimeError("Mixed source commits or targets in release inputs")
    manifests[target] = {"archive": name, "sha256": expected, "manifest_sha256": digest(path)}
    lines.append(f"{expected}  {name}\n")
    assets += [archive, path]
checksums = args.assets / "SHA256SUMS"
checksums.write_text("".join(lines), encoding="utf-8")
index = args.assets / "runtime-lock.json"
index.write_text(json.dumps({"release": RELEASE, "source_commit": os.environ["GITHUB_SHA"],
                             "targets": manifests}, indent=2) + "\n", encoding="utf-8")
notes = args.assets / "release-notes.md"
notes.write_text(f"""Native SDK preview for llama-cpp-rs 0.1.154.

Baseline: zvec-ai/zvec-grep@cc8fa72938db42b648687fccd99b088e94520186.
Source: {os.environ['GITHUB_SHA']}. Native source is pinned in every manifest.

Five native targets passed real CPU embedding tests using a separately rebuilt
external-mode Rust consumer. Linux x64 and Windows x64 additionally passed
CUDA 13.1 primary and CUDA 12.4 fallback real-offload tests on T4 GPU runners.
No NVIDIA runtime libraries are included; use the host CUDA runtime/driver.

Archive and manifest digests are in runtime-lock.json. Pin them before consumption.
See native/README.md for offline linkage, supported features and SDK layout.
This pre-release does not publish crates.io packages and does not change zg.

Evidence: {os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}
""", encoding="utf-8")
# Do not update an existing release or overwrite assets. A new bundle revision
# is required after publication, including when the source changes.
existing = subprocess.run(["gh", "release", "view", RELEASE], capture_output=True)
if existing.returncode == 0:
    raise RuntimeError(f"{RELEASE} already exists; refusing to replace immutable artifacts")
subprocess.run(["gh", "release", "create", RELEASE, "--target", os.environ["GITHUB_SHA"],
                "--prerelease", "--title", RELEASE, "--notes-file", str(notes),
                *map(str, assets + [checksums, index])], check=True)
