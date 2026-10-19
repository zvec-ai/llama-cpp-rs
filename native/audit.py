#!/usr/bin/env python3
"""Audit ELF/PE imports and relocatability, retaining evidence for acceptance."""
import argparse
import os
from pathlib import Path
import re
import subprocess
from runtime import verify

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--bundle", type=Path, required=True)
parser.add_argument("--logs", type=Path, required=True)
args = parser.parse_args()
manifest = verify(args.bundle)
args.logs.mkdir(parents=True, exist_ok=True)
evidence = []
for path in sorted((args.bundle / "lib").rglob("*")):
    if not path.is_file():
        continue
    relative = path.relative_to(args.bundle).as_posix()
    if ".so" in path.name:
        text = subprocess.check_output(["readelf", "-d", str(path)], text=True)
        evidence.append(f"=== {relative}\n{text}")
        imports = re.findall(r"\(NEEDED\).*?\[([^]]+)\]", text)
        rpath = re.findall(r"\((?:RPATH|RUNPATH)\).*?\[([^]]+)\]", text)
        if rpath != ["$ORIGIN/.." if "/cuda12/" in relative else "$ORIGIN"]:
            raise RuntimeError(f"Non-relocatable RPATH: {relative}: {rpath}")
        if "ggml-cuda" in path.name:
            major = "12" if "/cuda12/" in relative else "13"
            if not any(name.startswith("libcublas.so." + major) for name in imports):
                raise RuntimeError(f"Wrong CUDA imports: {relative}: {imports}")
        elif any(re.match(r"lib(cuda|cudart|cublas)", name) for name in imports):
            raise RuntimeError(f"Core/CPU library directly depends on CUDA: {relative}")
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
        text = subprocess.check_output(["file", str(path)], text=True)
        evidence.append(text)
(args.logs / "native-imports.txt").write_text("\n".join(evidence), encoding="utf-8")
print(f"Audited {len(evidence)} native files; platform contract verified")
