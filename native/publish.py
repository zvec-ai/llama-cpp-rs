#!/usr/bin/env python3
"""Prepare all eight SDKs, verify a draft's uploaded bytes, then publish once."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from fetch import extract
from runtime import RELEASE, TARGETS, digest, verify, lock_cmake, asset_stem, write_json
from e2e import MODELS

REPO = "zvec-ai/llama-cpp-rs"


def verify_evidence(root, target, variant, manifest_sha):
    platform = root / f"evidence-{target}-{variant}"
    if not (platform / "native-imports.txt").is_file():
        raise RuntimeError(f"Missing platform audit: {target}/{variant}")
    if "linux" in target and not (platform / "linux-abi.json").is_file():
        raise RuntimeError("Missing Linux ABI audit")
    records = [json.loads(path.read_text()) for path in platform.rglob("result.json")]
    if variant == "cuda":
        gpu = root / ("gpu-evidence-windows" if "windows" in target else "gpu-evidence-linux")
        records += [json.loads(path.read_text()) for path in gpu.rglob("result.json")]
    modes = ["cpu", "cuda12", "cuda13"] if variant == "cuda" else ["cpu", "metal"] if variant == "metal" else ["cpu"]
    verified = []
    for kind in MODELS:
        for mode in modes:
            matches = [r for r in records if r.get("model_kind") == kind and r.get("mode") == mode
                       and r.get("manifest_sha256") == manifest_sha and r.get("model_sha256") == MODELS[kind][1]]
            if len(matches) != 1 or matches[0].get("cpu_dimension") != 768:
                raise RuntimeError(f"Missing/ambiguous exact-archive inference evidence: {target}/{variant}/{kind}/{mode}")
            if mode != "cpu" and not matches[0].get("cosine_similarity", 0) >= 0.999:
                raise RuntimeError("GPU similarity gate failed")
            verified.append(matches[0])
    return verified


def prepare(directory, evidence, packaging_commit, run_url):
    assets, targets, rows = [], {}, []
    for target, variants in TARGETS.items():
        targets[target] = {}
        for variant in variants:
            stem = asset_stem(target, variant)
            archive = directory / f"{stem}.tar.gz"
            sidecar = directory / f"{stem}.manifest.json"
            expected = archive.with_name(archive.name + ".sha256").read_text().split()[0]
            if digest(archive) != expected:
                raise RuntimeError(f"Release archive hash mismatch: {target}/{variant}")
            manifest = json.loads(sidecar.read_text())
            with tempfile.TemporaryDirectory() as temporary:
                unpacked = Path(temporary) / "bundle"
                extract(archive, unpacked)
                if verify(unpacked) != manifest:
                    raise RuntimeError("Manifest sidecar differs from the verified archive payload")
            if (manifest["target"] != target or manifest["variant"] != variant
                    or manifest.get("packaging_commit", manifest["source_commit"]) != packaging_commit
                    or not re.fullmatch(r"[0-9a-f]{40}", manifest["source_commit"])):
                raise RuntimeError("Mixed packaging commits, targets or variants in release inputs")
            records = verify_evidence(evidence, target, variant, digest(sidecar))
            targets[target][variant] = {"archive": archive.name, "sha256": expected,
                "manifest_sha256": digest(sidecar), "features": manifest["features"],
                "cmake": lock_cmake(manifest["primary_cmake"]), "source_commit": manifest["source_commit"]}
            rows.append(f"- `{target}/{variant}`: {len(records)} inference checks passed; "
                        f"native build `{manifest['source_commit']}`; manifest `{digest(sidecar)}`.")
            assets += [archive, sidecar]
    actual = {p.name for p in directory.glob("*.tar.gz")} | {p.name for p in directory.glob("*.manifest.json")}
    if actual != {p.name for p in assets}:
        raise RuntimeError("Release inputs contain unexpected SDK archives or manifests")
    index = directory / "runtime-lock.json"
    write_json(index, {"schema": 2, "release": RELEASE, "packaging_commit": packaging_commit, "targets": targets})
    report = directory / "validation-report.md"
    report.write_text(f"# SDK validation\n\nRelease: `{RELEASE}`. Packaging: `{packaging_commit}`.\n"
        f"\n[Complete workflow evidence]({run_url}).\n\n"
        "All eight exact archives were extracted and verified, linked into fresh Rust consumers without native compilation, "
        "and tested with checksum-pinned EmbeddingGemma and EmbeddingGemma 2 weights (768 dimensions). "
        "Linux linkage, CPU inference and CUDA tests used glibc 2.28; ABI ceilings are GLIBC 2.28, GLIBCXX 3.4.25, CXXABI 1.3.11. "
        "Metal and both CUDA plugins passed actual offload and CPU/GPU cosine similarity >= 0.999. "
        "CUDA tests used Linux and Windows Tesla T4 runners.\n\n" + "\n".join(rows) +
        "\n\nRepackaged binaries retain their original build commit and payload hashes in each manifest; "
        "packaging provenance is recorded separately. Evidence above is from the final archives, not earlier packages.\n",
        encoding="utf-8")
    assets += [index, report]
    checksums = directory / "SHA256SUMS"
    checksums.write_text("".join(f"{digest(p)}  {p.name}\n" for p in sorted(assets)), encoding="utf-8")
    assets.append(checksums)
    notes = directory / "release-notes.md"
    notes.write_text(f"Native SDK preview for llama-cpp-rs 0.1.162, llama.cpp v0.9.0.\n\n"
        f"One release contains all 8 CPU/Metal/CUDA SDKs. Select by target and variant in `runtime-lock.json`.\n\n"
        "- CPU: macOS x64, Linux x64/ARM64, Windows x64/ARM64.\n"
        "- Metal: macOS ARM64 (CPU + Metal).\n"
        "- CUDA: Linux x64 and Windows x64 (CPU + CUDA 13.1 primary + CUDA 12.4 fallback).\n"
        "- Linux minimum glibc: 2.28. NVIDIA runtime/driver libraries are supplied by the host.\n\n"
        f"Packaging commit: `{packaging_commit}`. Original native build commits are preserved in the manifests.\n\n"
        f"See `validation-report.md`, `SHA256SUMS` and [workflow evidence]({run_url}). "
        "Consumers use an index pinned in the fork; no remote latest-version lookup occurs during builds.\n\n"
        "This pre-release publishes SDKs only, does not publish crates.io packages, and does not change zg. "
        "Published assets are never overwritten; changes require a new revision.\n", encoding="utf-8")
    return assets, notes


def publish(assets, notes, commit):
    # Listing must succeed: authentication/network errors are not 'not found'.
    releases = json.loads(subprocess.check_output(["gh", "api", f"repos/{REPO}/releases?per_page=100"], text=True))
    if any(r["tag_name"] == RELEASE for r in releases):
        raise RuntimeError(f"{RELEASE} already exists; refusing to modify it")
    subprocess.run(["gh", "release", "create", RELEASE, "--repo", REPO, "--target", commit,
                    "--draft", "--prerelease", "--title", RELEASE, "--notes-file", str(notes),
                    *map(str, assets)], check=True)
    remote = json.loads(subprocess.check_output(["gh", "release", "view", RELEASE, "--repo", REPO,
                                                "--json", "assets,isDraft,isPrerelease"], text=True))
    if not remote["isDraft"] or not remote["isPrerelease"]:
        raise RuntimeError("Expected a draft pre-release")
    if {a["name"] for a in remote["assets"]} != {p.name for p in assets} or len(remote["assets"]) != 19:
        raise RuntimeError("Draft asset inventory mismatch; leaving it unpublished")
    with tempfile.TemporaryDirectory() as temporary:
        subprocess.run(["gh", "release", "download", RELEASE, "--repo", REPO, "--dir", temporary], check=True)
        for path in assets:
            if digest(Path(temporary) / path.name) != digest(path):
                raise RuntimeError("Draft download hash mismatch; leaving it unpublished")
    subprocess.run(["gh", "release", "edit", RELEASE, "--repo", REPO, "--draft=false", "--prerelease"], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    commit = os.environ["GITHUB_SHA"]
    run_url = f"https://github.com/{REPO}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    assets, notes = prepare(args.assets, args.evidence, commit, run_url)
    if args.prepare_only:
        print(f"Prepared {RELEASE}: {len(assets)} assets; nothing published")
    else:
        publish(assets, notes, commit)


if __name__ == "__main__":
    main()
