#!/usr/bin/env python3
"""Publish one immutable pre-release after the CI dependency gates succeed."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
from fetch import extract
from runtime import RELEASE, CPU_RELEASE, TARGETS, CPU_TARGETS, REVISION, digest, audit_cache, CRATE_VERSION, verify, lock_cmake

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--assets", type=Path, required=True)
parser.add_argument("--variant", choices=["default", "cpu"], default="default")
parser.add_argument("--prepare-only", action="store_true", help="Generate hashes and notes without calling GitHub")
args = parser.parse_args()
cpu = args.variant == "cpu"
release = CPU_RELEASE if cpu else RELEASE
targets = CPU_TARGETS if cpu else TARGETS
lock_name = "cpu-runtime-lock.json" if cpu else "runtime-lock.json"
assets = []
lines = []
manifests = {}
for target in targets:
    name = f"llama-cpp-runtime-{release}-{target}.tar.gz"
    archive = args.assets / name
    expected = archive.with_name(name + ".sha256").read_text().split()[0]
    if digest(archive) != expected:
        raise RuntimeError(f"Release archive hash mismatch: {target}")
    path = args.assets / f"{target}{'-cpu' if cpu else ''}.manifest.json"
    manifest = json.loads(path.read_text())
    with tempfile.TemporaryDirectory() as temporary:
        unpacked = Path(temporary) / "bundle"
        extract(archive, unpacked)
        if verify(unpacked) != manifest:
            raise RuntimeError("Published manifest differs from the verified archive payload")
    if manifest["source_commit"] != os.environ["GITHUB_SHA"] or manifest["target"] != target:
        raise RuntimeError("Mixed source commits or targets in release inputs")
    manifests[target] = {"archive": name, "sha256": expected, "manifest_sha256": digest(path)}
    if (manifest.get("schema") != 2 or manifest["release"] != release
            or manifest["crate_version"] != CRATE_VERSION or manifest["llama_cpp_revision"] != REVISION):
        raise RuntimeError("Release inputs do not match the current native ABI")
    audit_cache(manifest["primary_cmake"], target, variant=args.variant, glibc228="linux_abi" in manifest)
    manifests[target].update(features=manifest["features"], cmake=lock_cmake(manifest["primary_cmake"]))
    lines.append(f"{expected}  {name}\n")
    assets += [archive, path]
checksums = args.assets / "SHA256SUMS"
checksums.write_text("".join(lines), encoding="utf-8")
index = args.assets / lock_name
index.write_text(json.dumps({"release": release, "source_commit": os.environ["GITHUB_SHA"],
                             "targets": manifests}, indent=2) + "\n", encoding="utf-8")
notes = args.assets / "release-notes.md"
coverage = ("Linux x64, Windows x64 and Windows ARM64 must pass real CPU embedding tests using a separately rebuilt\n"
            "external-mode Rust consumer. Static common/CPU libraries only; no CUDA payload.\n" if cpu else
            "Five native targets must pass real CPU embedding tests using a separately rebuilt\n"
            "external-mode Rust consumer. Linux x64 and Windows x64 must additionally pass\n"
            "CUDA 13.1 primary and CUDA 12.4 fallback real-offload tests on T4 GPU runners.\n"
            "No NVIDIA runtime libraries are included; use the host CUDA runtime/driver.\n")
coverage = coverage.rstrip("\n")
notes.write_text(f"""Native SDK preview for llama-cpp-rs 0.1.162.

Upstream baseline: utilityai/llama-cpp-rs@955aa2f72463c2535cc5ab894d5d52f27a38cde2.
Source: {os.environ['GITHUB_SHA']}. Native source is pinned in every manifest.

{coverage}

Archive and manifest digests are in {lock_name}. Pin them before consumption.
See native/README.md for offline linkage, supported features and SDK layout.
This pre-release does not publish crates.io packages and does not change zg.

Evidence: {os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}
""", encoding="utf-8")
if args.prepare_only:
    print(f"Prepared {release}: {index}; nothing published")
    raise SystemExit(0)
# Do not update an existing release or overwrite assets. A new bundle revision
# is required after publication, including when the source changes.
existing = subprocess.run(["gh", "release", "view", release], capture_output=True)
if existing.returncode == 0:
    raise RuntimeError(f"{release} already exists; refusing to replace immutable artifacts")
subprocess.run(["gh", "release", "create", release, "--target", os.environ["GITHUB_SHA"],
                "--prerelease", "--title", release, "--notes-file", str(notes),
                *map(str, assets + [checksums, index])], check=True)
