#!/usr/bin/env python3
"""Run a downloaded external-mode consumer against real, pinned GGUF weights."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.request

from runtime import digest, verify

MODEL = "embeddinggemma-300M-Q8_0.gguf"
MODEL_SHA256 = "b5ce9d77a3fc4b3b39ccb5643c36777911cc4eb46a66962eadfa3f5f60490d63"
MODEL_URL = "https://huggingface.co/ggml-org/embeddinggemma-300M-GGUF/resolve/0f741b5a6585bd53aeb15cd1372c56f2a0f65e12/" + MODEL


def model_file(cache):
    cache.mkdir(parents=True, exist_ok=True)
    model = cache / MODEL
    if not model.exists():
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as stream:
            temporary = Path(stream.name)
        try:
            subprocess.run(["curl.exe" if os.name == "nt" else "curl", "--fail", "--location", "--retry", "3",
                            "--max-time", "900", "--output", str(temporary), MODEL_URL], check=True)
            if digest(temporary) != MODEL_SHA256:
                raise RuntimeError("Model checksum mismatch")
            temporary.replace(model)
        finally:
            temporary.unlink(missing_ok=True)
    if digest(model) != MODEL_SHA256:
        raise RuntimeError("Cached model checksum mismatch")
    return model


def infer(probe, bundle, model, mode, logs):
    env = os.environ.copy()
    if os.name == "nt":
        env["PATH"] = str(bundle / "lib") + os.pathsep + env["PATH"]
    elif "linux" in os.sys.platform:
        env["LD_LIBRARY_PATH"] = str(bundle / "lib") + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    result = subprocess.run([str(probe), str(bundle), str(model), mode], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", timeout=600)
    logs.mkdir(parents=True, exist_ok=True)
    (logs / f"{mode}.stderr.log").write_text(result.stderr, encoding="utf-8")
    (logs / f"{mode}.stdout.log").write_text(result.stdout, encoding="utf-8")
    if result.returncode:
        print(result.stderr, end="", flush=True)
    else:
        print("\n".join(line for line in result.stderr.splitlines()
                        if re.search(r"available device|offloaded|loaded CUDA backend", line)), flush=True)
    result.check_returncode()
    output = json.loads(result.stdout.strip().splitlines()[-1])
    if output["dimension"] != 768 or output["norm"] <= 0 or not all(math.isfinite(v) for v in output["embedding"]):
        raise RuntimeError("Invalid real embedding")
    offloaded = re.search(r"offloaded ([1-9]\d*)/\d+ layers to GPU", result.stderr)
    if mode == "cpu" and offloaded:
        raise RuntimeError("CPU test unexpectedly offloaded layers")
    if mode != "cpu" and not offloaded:
        raise RuntimeError("GPU selection without actual layer offload is not sufficient")
    if mode.startswith("cuda"):
        paths = re.findall(r"loaded CUDA backend from ([^\n]+)", result.stderr)
        normalized = [path.replace("\\", "/").lower() for path in paths]
        if not normalized or any(("/cuda12/" in path) != (mode == "cuda12") for path in normalized):
            raise RuntimeError(f"Wrong CUDA plugin was loaded: {paths}")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=Path("target/native/bundle"))
    parser.add_argument("--probe", type=Path)
    parser.add_argument("--model-cache", type=Path, default=Path("target/models"))
    parser.add_argument("--model", type=Path, help="Use existing weights, still checked against the pinned SHA256")
    parser.add_argument("--logs", type=Path, default=Path("target/evidence"))
    parser.add_argument("--mode", choices=["cpu", "cuda12", "cuda13", "metal"], default="cpu")
    args = parser.parse_args()
    bundle = args.bundle.resolve()
    verify(bundle)
    probe = args.probe or bundle.parent / "probe" / ("native-smoke.exe" if os.name == "nt" else "native-smoke")
    model = args.model.resolve() if args.model else model_file(args.model_cache.resolve())
    if digest(model) != MODEL_SHA256:
        raise RuntimeError("Model checksum mismatch")
    cpu = infer(probe.resolve(), bundle, model, "cpu", args.logs)
    evidence = {"model_sha256": MODEL_SHA256, "manifest_sha256": digest(bundle / "manifest.json"),
                "cpu_dimension": cpu["dimension"], "mode": args.mode}
    if args.mode != "cpu":
        gpu = infer(probe.resolve(), bundle, model, args.mode, args.logs)
        dot = sum(a * b for a, b in zip(cpu["embedding"], gpu["embedding"]))
        cosine = dot / (cpu["norm"] * gpu["norm"])
        if cosine < 0.999:
            raise RuntimeError(f"CPU/GPU embedding disagreement: cosine={cosine}")
        evidence["cosine_similarity"] = cosine
    (args.logs / "result.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence), flush=True)


if __name__ == "__main__":
    main()
