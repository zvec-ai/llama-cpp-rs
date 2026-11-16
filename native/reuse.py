#!/usr/bin/env python3
"""Repackage an ABI-compatible SDK from successful CI without changing its payload."""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
import zipfile

from fetch import extract
from runtime import RELEASE, ROOT, digest, sdk_features, verify, write_json

REPO = "zvec-ai/llama-cpp-rs"


def api(path):
    return json.loads(subprocess.check_output(["gh", "api", f"repos/{REPO}/{path}"], text=True))


def repack(archive, output, target, variant, source_commit, provenance, packaging_commit):
    if output.exists():
        raise RuntimeError("Refusing to overwrite an existing bundle")
    extract(archive, output)
    manifest = verify(output, allow_legacy=True)
    if (manifest["target"] != target or manifest["features"] != sdk_features(target, variant)
            or manifest["source_commit"] != source_commit):
        raise RuntimeError("Reused SDK does not match its source run, target or features")
    original_files = manifest["files"].copy()
    original_manifest_sha = digest(output / "manifest.json")
    manifest.update(release=RELEASE, variant=variant, packaging_commit=packaging_commit,
                    provenance={**provenance, "archive_sha256": digest(archive),
                                "manifest_sha256": original_manifest_sha,
                                "original_release": manifest["release"]})
    write_json(output / "manifest.json", manifest)
    verified = verify(output)
    if verified["files"] != original_files:
        raise RuntimeError("Repack changed native payload hashes")
    return verified


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", required=True, help="Comma-separated successful native workflow run IDs")
    parser.add_argument("--target", required=True)
    parser.add_argument("--variant", required=True, choices=["cpu", "metal", "cuda"])
    parser.add_argument("--bundle", type=Path, default=Path("target/native/bundle"))
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]+(?:,[0-9]+)*", args.runs):
        parser.error("--runs must contain numeric run IDs")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    expected_features = sdk_features(args.target, args.variant)
    for run_id in args.runs.split(","):
        run = api(f"actions/runs/{run_id}")
        if (run["conclusion"] != "success" or run["path"] != ".github/workflows/native-release.yml"
                or run["repository"]["full_name"] != REPO):
            raise RuntimeError("Only successful native SDK runs in this repository may be reused")
        artifacts = api(f"actions/runs/{run_id}/artifacts?per_page=100")
        if artifacts["total_count"] > 100:
            raise RuntimeError("Too many artifacts; refusing an incomplete inventory")
        names = {f"native-{args.target}-{args.variant}", f"native-{args.target}"}
        candidates = sorted((a for a in artifacts["artifacts"] if a["name"] in names and not a["expired"]),
                            key=lambda a: a["created_at"], reverse=True)
        for artifact in candidates:
            with tempfile.TemporaryDirectory() as temp:
                temp = Path(temp)
                zipped = temp / "artifact.zip"
                with zipped.open("wb") as output:
                    subprocess.run(["gh", "api", f"repos/{REPO}/actions/artifacts/{artifact['id']}/zip"],
                                   stdout=output, check=True)
                if artifact.get("digest") != "sha256:" + digest(zipped):
                    raise RuntimeError("GitHub artifact digest mismatch")
                # SDK artifacts contain only three flat regular files.
                with zipfile.ZipFile(zipped) as archive:
                    members = archive.infolist()
                    if len(members) != 3 or len({m.filename for m in members}) != 3:
                        raise RuntimeError("Unexpected SDK artifact inventory")
                    for member in members:
                        if (member.is_dir() or len(PurePosixPath(member.filename).parts) != 1
                                or re.search(r"[\\\\:]", member.filename) or member.filename in (".", "..")
                                or member.file_size > 2 * 1024**3):
                            raise RuntimeError("Unsafe artifact ZIP entry")
                    archive.extractall(temp)
                paths = list(temp.glob("*.manifest.json"))
                if len(paths) != 1:
                    raise RuntimeError("Missing SDK manifest sidecar")
                manifest = json.loads(paths[0].read_text())
                if manifest["features"] != expected_features:
                    continue
                archives = list(temp.glob("*.tar.gz"))
                if len(archives) != 1:
                    raise RuntimeError("Expected exactly one SDK archive")
                archive = archives[0]
                if digest(archive) != archive.with_name(archive.name + ".sha256").read_text().split()[0]:
                    raise RuntimeError("Source SDK archive hash mismatch")
                result = repack(archive, args.bundle.resolve(), args.target, args.variant, run["head_sha"],
                                {"run": run["html_url"], "artifact_id": artifact["id"],
                                 "artifact_sha256": digest(zipped)}, head)
                if result["provenance"]["manifest_sha256"] != digest(paths[0]):
                    raise RuntimeError("Source manifest sidecar mismatch")
                print(f"Repacked {args.target}/{args.variant}; native build {result['source_commit']}; packaging {head}")
                return
    raise RuntimeError("No compatible SDK found in the specified successful runs")


if __name__ == "__main__":
    main()
