#!/usr/bin/env python3
"""Audit native dependencies and the Ubuntu 20.04 ABI ceiling."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
from runtime import verify

LINUX_LIMITS = {"GLIBC": "2.31", "GLIBCXX": "3.4.28"}


def required_versions(text):
    """Read imports only: exported version definitions are not requirements."""
    requirements = set()
    in_needs = False
    for line in text.splitlines():
        if re.match(r"^Version .* section ", line):
            in_needs = line.startswith("Version needs section ")
        if in_needs:
            requirements.update(re.findall(r"\bName: ((?:GLIBC|GLIBCXX)_[\w.]+)", line))
    return sorted(requirements)


def check_versions(requirements, label):
    for name in requirements:
        family, version = name.split("_", 1)
        limit = LINUX_LIMITS[family]
        if (not re.fullmatch(r"\d+(?:\.\d+)+", version)
                or tuple(map(int, version.split("."))) > tuple(map(int, limit.split(".")))):
            raise RuntimeError(f"Ubuntu 20.04 ABI exceeded: {label} needs {name}; maximum {family}_{limit}")


def readelf(path, option):
    return subprocess.check_output(["readelf", option, "--wide", str(path)],
                                   text=True, env={**os.environ, "LC_ALL": "C"})


def audit(bundle, logs, probe=None):
    manifest = verify(bundle)
    logs.mkdir(parents=True, exist_ok=True)
    evidence = []
    versions = {}
    linux = "linux" in manifest["target"]
    files = sorted(path for path in (bundle / "lib").rglob("*") if path.is_file())
    if linux:
        # Static SDK members acquire symbol versions at final link. Audit an
        # independently linked consumer as well as every shared core/plugin.
        probe = probe or bundle.parent / "probe" / "native-smoke"
        if not probe.is_file():
            raise RuntimeError(f"Linux ABI audit requires the external-mode consumer: {probe}")
        files.append(probe)
    for path in files:
        relative = "probe/native-smoke" if path == probe else path.relative_to(bundle).as_posix()
        if ".so" in path.name or path == probe:
            text = readelf(path, "--dynamic")
            evidence.append(f"=== {relative}\n{text}")
            imports = re.findall(r"\(NEEDED\).*?\[([^]]+)\]", text)
            rpath = re.findall(r"\((?:RPATH|RUNPATH)\).*?\[([^]]+)\]", text)
            if path != probe and rpath != ["$ORIGIN/.." if "/cuda12/" in relative else "$ORIGIN"]:
                raise RuntimeError(f"Non-relocatable RPATH: {relative}: {rpath}")
            if "ggml-cuda" in path.name:
                major = "12" if "/cuda12/" in relative else "13"
                if not any(name.startswith("libcublas.so." + major) for name in imports):
                    raise RuntimeError(f"Wrong CUDA imports: {relative}: {imports}")
            elif any(re.match(r"lib(cuda|cudart|cublas)", name) for name in imports):
                raise RuntimeError(f"Core/CPU library directly depends on CUDA: {relative}")
            text = readelf(path, "--version-info")
            evidence.append(f"=== {relative} symbol versions\n{text}")
            versions[relative] = required_versions(text)
            check_versions(versions[relative], relative)
        elif path.suffix == ".dll":
            text = subprocess.check_output(["dumpbin", "/DEPENDENTS", str(path)], text=True, errors="replace")
            evidence.append(f"=== {relative}\n{text}")
            imports = re.findall(r"^\s+(\S+\.dll)\s*$", text, flags=re.M | re.I)
            if "ggml-cuda" in path.name:
                major = "12" if "/cuda12/" in relative else "13"
                if f"cublas64_{major}.dll" not in [name.lower() for name in imports]:
                    raise RuntimeError(f"Wrong CUDA imports: {relative}: {imports}")
            elif any(re.match(r"(?:nvcuda|cudart|cublas)", name, re.I) for name in imports):
                raise RuntimeError(f"Core/CPU DLL directly depends on CUDA: {relative}")
        elif path.suffix == ".a" and "apple" in manifest["target"]:
            evidence.append(subprocess.check_output(["file", str(path)], text=True))
    (logs / "native-imports.txt").write_text("\n".join(evidence), encoding="utf-8")
    if linux:
        (logs / "linux-abi.json").write_text(json.dumps({"limits": LINUX_LIMITS,
            "requirements": versions}, indent=2) + "\n", encoding="utf-8")
    print(f"Audited {len(files)} native files; platform contract verified")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--logs", type=Path, required=True)
    parser.add_argument("--probe", type=Path)
    args = parser.parse_args()
    audit(args.bundle, args.logs, args.probe)


if __name__ == "__main__":
    main()
