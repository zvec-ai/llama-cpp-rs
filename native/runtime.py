#!/usr/bin/env python3
"""Build and verify native SDKs through the sys crate's pinned source resolver."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
RELEASE = "zvec-0.1.154-r1"
CPU_RELEASE = "zvec-0.1.154-cpu-r2"
SOURCE_LOCK = json.loads((ROOT / "llama-cpp-sys-2/build_support/source-lock.json").read_text(encoding="utf-8"))
REVISION = SOURCE_LOCK["revision"]
ARCH13 = "75-real;80-real;86-real;89-real;90-real;90-virtual"
ARCH12 = "50-virtual;61-virtual;70-virtual;75-virtual;80-virtual;86-real"
TARGETS = {
    "aarch64-apple-darwin": ["common", "metal"],
    "x86_64-apple-darwin": ["common"],
    "aarch64-unknown-linux-gnu": ["common"],
    "x86_64-unknown-linux-gnu": ["common", "cuda", "dynamic-backends", "dynamic-link"],
    "x86_64-pc-windows-msvc": ["common", "cuda", "dynamic-backends", "dynamic-link"],
}
CPU_TARGETS = {target: ["common"] for target in
               ("x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc", "aarch64-pc-windows-msvc")}


def sdk_features(target, variant="default"):
    if variant == "cpu":
        if target not in CPU_TARGETS:
            raise RuntimeError("CPU-only SDKs support Linux x64 and Windows x64/ARM64")
        return CPU_TARGETS[target]
    if target not in TARGETS:
        raise RuntimeError(f"{target} requires --variant cpu")
    return TARGETS[target]


def digest(path):
    with Path(path).open("rb") as stream:
        value = hashlib.sha256()
        while block := stream.read(1024 * 1024):
            value.update(block)
        return value.hexdigest()


def run(args, **kwargs):
    print("+", " ".join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), check=True, cwd=ROOT, **kwargs)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def cargo_build(target, target_dir, env, features=None):
    command = ["cargo", "build", "--locked", "--release", "-p", "native-smoke",
               "--target", target, "--target-dir", target_dir, "--message-format=json"]
    if "cuda" in (TARGETS[target] if features is None else features):
        command += ["--features", "cuda"]
    try:
        result = run(command, env=env, stdout=subprocess.PIPE, text=True)
    except subprocess.CalledProcessError as error:
        # Cargo's rendered Rust/linker diagnostics live in JSON stdout.
        print(error.stdout or "", flush=True)
        raise
    messages = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    scripts = [m for m in messages if m.get("reason") == "build-script-executed"
               and re.search(r"(?:[/#]|^)llama-cpp-sys-2(?:[#@:/]|$)", m["package_id"])]
    if len(scripts) != 1:
        raise RuntimeError(f"Expected exactly one sys build script: {scripts}")
    binary = next(m["executable"] for m in messages
                  if m.get("reason") == "compiler-artifact" and m.get("executable")
                  and m["target"]["name"] == "native-smoke")
    return Path(scripts[0]["out_dir"]), Path(binary)


def cache_entries(path):
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if match := re.match(r"([^#/:][^:]*):[^=]+=(.*)", line):
            result[match[1]] = match[2]
    return result


def audit_cache(cache, target, fallback=False, variant="default"):
    cuda = "cuda" in sdk_features(target, variant)
    expected = {"CMAKE_BUILD_TYPE": "Release", "GGML_NATIVE": "OFF", "GGML_OPENMP": "OFF",
                "LLAMA_BUILD_TESTS": "OFF", "LLAMA_BUILD_EXAMPLES": "OFF", "LLAMA_BUILD_TOOLS": "OFF",
                "LLAMA_BUILD_APP": "OFF", "LLAMA_BUILD_SERVER": "OFF", "LLAMA_CURL": "OFF",
                "LLAMA_BUILD_COMMON": "OFF" if fallback else "ON",
                "BUILD_SHARED_LIBS": "ON" if cuda else "OFF"}
    if cuda:
        expected.update(GGML_BACKEND_DL="ON", GGML_CUDA="ON", GGML_CUDA_NCCL="OFF",
                        CMAKE_CUDA_ARCHITECTURES=ARCH12 if fallback else ARCH13)
        if fallback:
            expected["GGML_CPU"] = "OFF"
        else:
            expected["GGML_CPU_ALL_VARIANTS"] = "ON"
    else:
        expected.update(GGML_CUDA="OFF", GGML_CPU="ON")
        if variant == "cpu":
            expected.update(GGML_BACKEND_DL="OFF", GGML_CPU_ALL_VARIANTS="OFF",
                            GGML_METAL="OFF", GGML_VULKAN="OFF", GGML_OPENCL="OFF", GGML_HIP="OFF")
    for key, value in expected.items():
        if cache.get(key) != value:
            raise RuntimeError(f"Build contract mismatch: {key}={cache.get(key)!r}, expected {value}")
    compiler = cache.get("CMAKE_CXX_COMPILER", "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if compiler in ("clang-cl", "clang-cl.exe") and "/EHsc" not in cache.get("CMAKE_CXX_FLAGS", "").split():
        raise RuntimeError("Build contract mismatch: clang-cl C++ build requires /EHsc")
    return {key: value for key, value in cache.items()
            if key.startswith(("GGML_", "LLAMA_")) or key in expected
            or key in ("CMAKE_CXX_COMPILER", "CMAKE_CXX_FLAGS", "CMAKE_CXX_FLAGS_RELEASE",
                       "CMAKE_CUDA_COMPILER", "CMAKE_MSVC_RUNTIME_LIBRARY", "CMAKE_OSX_DEPLOYMENT_TARGET")}


def copy_file(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if digest(source) != digest(destination):
            raise RuntimeError(f"Conflicting library: {destination}")
    else:
        # Materialize links: archives contain only regular files and directories.
        shutil.copy2(source, destination)


def recover_incomplete_cmake(target_dir, target):
    # Upstream skips configuration whenever CMakeCache.txt exists, even when
    # a previous configure failed before generating the install project.
    builds = Path(target_dir) / target / "release/build"
    for cache in builds.glob("llama-cpp-sys-2-*/out/build/CMakeCache.txt"):
        if not (cache.parent / "cmake_install.cmake").is_file():
            print(f"Reconfiguring incomplete cached CMake project: {cache.parent}", flush=True)
            cache.replace(cache.with_suffix(".txt.incomplete"))


def verify_source(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError(f"Invalid pinned source directory: {root}")
    files = {}
    for directory, subdirectories, names in os.walk(root):
        directory = Path(directory)
        if directory == root:
            subdirectories[:] = [name for name in subdirectories if name != ".git"]
            names = [name for name in names if name != ".git"]
        for name in subdirectories:
            if (directory / name).is_symlink():
                raise RuntimeError("Symlink in pinned source tree")
        for name in names:
            path = directory / name
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(f"Invalid pinned source file: {path}")
            files[path.relative_to(root).as_posix()] = digest(path)
    tree = hashlib.sha256()
    for name, value in sorted(files.items()):
        encoded = name.encode("utf-8")
        tree.update(len(encoded).to_bytes(8, "big"))
        tree.update(encoded)
        tree.update(value.encode("ascii"))
    if tree.hexdigest() != SOURCE_LOCK["tree_sha256"]:
        raise RuntimeError(f"Pinned llama.cpp {REVISION} source tree mismatch: {root}")
    return root


def source_from_build(out):
    metadata = dict(line.removeprefix("cargo:").split("=", 1)
                    for line in (out.parent / "output").read_text(encoding="utf-8").splitlines()
                    if line.startswith(("cargo:source_dir=", "cargo:source_revision=")))
    if metadata.get("source_revision") != REVISION or not metadata.get("source_dir"):
        raise RuntimeError("Primary build did not report the locked source revision/directory")
    return verify_source(metadata["source_dir"])


def primary(target, output, target_dir, variant="default"):
    if output.exists():
        raise RuntimeError(f"Refusing to overwrite existing bundle: {output}")
    features = sdk_features(target, variant)
    env = os.environ.copy()
    env["LLAMA_CPP_BUILD_MODE"] = "source"
    # The two Linux containers share /work/target, not /root/.cargo.
    env.setdefault("LLAMA_CPP_SOURCE_CACHE", str(ROOT / "target/native-source"))
    env.pop("LLAMA_CPP_RUNTIME_DIR", None)
    if "cuda" in features:
        nvcc = subprocess.check_output(["nvcc", "--version"], text=True)
        if "release 13.1," not in nvcc:
            raise RuntimeError("Primary plugin requires CUDA 13.1")
        env["CMAKE_CUDA_ARCHITECTURES"] = ARCH13
    if target == "x86_64-apple-darwin":
        env["MACOSX_DEPLOYMENT_TARGET"] = "14.0"
    if target == "aarch64-pc-windows-msvc":
        # The pinned ggml ARM backend rejects MSVC cl.exe; retain MSVC SDK/CRT.
        env.update(CC="clang-cl", CXX="clang-cl", CMAKE_GENERATOR="Ninja")
    recover_incomplete_cmake(target_dir, target)
    out, _ = cargo_build(target, target_dir, env, features=features)
    source_root = source_from_build(out)
    write_json(target_dir / "source-root.json", {"revision": REVISION, "path": str(source_root)})
    cache = audit_cache(cache_entries(out / "build/CMakeCache.txt"), target, variant=variant)
    # Cargo reports exactly this build's OUT_DIR. Never select by newest glob.
    libraries = []
    for line in (out.parent / "output").read_text(encoding="utf-8").splitlines():
        if line.startswith("cargo:rustc-link-lib="):
            value = line.split("=", 1)[1]
            if value not in libraries:
                libraries.append(value)
    output.mkdir(parents=True)
    for directory in [out, out / "lib", out / "lib64", out / "bin", out / "backends",
                      out / "build/common", out / "build/common/Release", out / "build/bin", out / "build/bin/Release"]:
        if directory.is_dir():
            for path in sorted(directory.iterdir()):
                if path.is_file() and re.search(r"\.(?:a|lib|dll|dylib|so(?:\.\d+)*)$", path.name):
                    destination = output / "lib" / path.name
                    if not destination.exists():
                        copy_file(path, destination)
    if "cuda" in features and "linux" in target:
        for path in sorted((output / "lib").glob("*.so*")):
            run(["patchelf", "--set-rpath", "$ORIGIN", path])
    copy_file(out / "bindings.rs", output / "bindings.rs")
    for name, source in {"llama.cpp-MIT": source_root / "LICENSE",
                         "llama-cpp-rs-MIT": ROOT / "LICENSE-MIT", "llama-cpp-rs-APACHE": ROOT / "LICENSE-APACHE",
                         "jsonhpp-MIT": source_root / "licenses/LICENSE-jsonhpp",
                         "cpp-httplib-MIT": source_root / "vendor/cpp-httplib/LICENSE"}.items():
        copy_file(source, output / "licenses" / name)
    manifest = {"schema": 1, "release": CPU_RELEASE if variant == "cpu" else RELEASE, "crate_version": "0.1.154",
                "llama_cpp_revision": REVISION, "target": target, "features": features,
                "linkage": "dynamic" if "cuda" in features else "static",
                "link_libraries": libraries, "primary_cmake": cache,
                "baseline": {"repository": "zvec-ai/zvec-grep", "commit": "cc8fa72938db42b648687fccd99b088e94520186"},
                "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "rustc": subprocess.check_output(["rustc", "--version"], text=True).strip()}
    write_json(output / "manifest.json", manifest)
    seal(output)


def fallback(output, target_dir, cuda_root):
    manifest = read_json(output / "manifest.json")
    target = manifest["target"]
    if "cuda" not in manifest["features"]:
        raise RuntimeError("Fallback only exists on the two x64 CUDA targets")
    if not (target_dir / "source-root.json").is_file():
        raise RuntimeError("Fallback source record is missing; run primary first")
    source_record = read_json(target_dir / "source-root.json")
    if source_record.get("revision") != REVISION:
        raise RuntimeError("Fallback requires primary's matching pinned source; run primary first")
    source = verify_source(source_record["path"])
    nvcc = cuda_root / "bin" / ("nvcc.exe" if os.name == "nt" else "nvcc")
    version = subprocess.check_output([str(nvcc), "--version"], text=True)
    if "release 12.4," not in version:
        raise RuntimeError("Fallback plugin requires CUDA 12.4")
    build = target_dir / "cuda12"
    command = ["cmake", "-S", source, "-B", build, "-G", "Ninja",
               "-DCMAKE_BUILD_TYPE=Release", "-DBUILD_SHARED_LIBS=ON", "-DGGML_BACKEND_DL=ON",
               "-DGGML_NATIVE=OFF", "-DGGML_CPU=OFF", "-DGGML_CUDA=ON", "-DGGML_CUDA_NCCL=OFF",
               "-DGGML_OPENMP=OFF", "-DLLAMA_BUILD_TESTS=OFF", "-DLLAMA_BUILD_EXAMPLES=OFF",
               "-DLLAMA_BUILD_TOOLS=OFF", "-DLLAMA_BUILD_COMMON=OFF", "-DLLAMA_BUILD_APP=OFF",
               "-DLLAMA_BUILD_SERVER=OFF", "-DLLAMA_CURL=OFF", f"-DCMAKE_CUDA_COMPILER={nvcc}",
               f"-DCUDAToolkit_ROOT={cuda_root}", f"-DCMAKE_CUDA_ARCHITECTURES={ARCH12}"]
    if os.name == "nt":
        command += ["-DCMAKE_C_COMPILER=cl", "-DCMAKE_CXX_COMPILER=cl", "-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreadedDLL"]
    source_env = os.environ.copy()
    source_env["GIT_CEILING_DIRECTORIES"] = str(source.parent)
    run(command, env=source_env)
    run(["cmake", "--build", build, "--config", "Release", "--target", "ggml-cuda",
         "--parallel", os.environ.get("CMAKE_BUILD_PARALLEL_LEVEL", "2")])
    filename = "ggml-cuda.dll" if os.name == "nt" else "libggml-cuda.so"
    paths = [p for p in build.rglob(filename) if p.is_file() and not p.is_symlink()]
    if len(paths) != 1:
        raise RuntimeError(f"Expected one fallback plugin: {paths}")
    destination = output / "lib/cuda12" / filename
    copy_file(paths[0], destination)
    if os.name != "nt":
        run(["patchelf", "--set-rpath", "$ORIGIN/..", destination])
    manifest["fallback_cmake"] = audit_cache(cache_entries(build / "CMakeCache.txt"), target, fallback=True)
    write_json(output / "manifest.json", manifest)
    seal(output)


def seal(output):
    manifest = read_json(output / "manifest.json")
    manifest["files"] = {p.relative_to(output).as_posix(): digest(p) for p in sorted(output.rglob("*"))
                         if p.is_file() and p.name != "manifest.json"}
    write_json(output / "manifest.json", manifest)


def verify(output):
    manifest = read_json(output / "manifest.json")
    if manifest["release"] not in (RELEASE, CPU_RELEASE) or manifest["llama_cpp_revision"] != REVISION:
        raise RuntimeError("Wrong release/ABI")
    for name, expected in manifest["files"].items():
        path = output / name
        if not path.resolve().is_relative_to(output.resolve()) or path.is_symlink() or digest(path) != expected:
            raise RuntimeError(f"Invalid payload/checksum: {name}")
    actual = {p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file() and p.name != "manifest.json"}
    if actual != manifest["files"].keys():
        raise RuntimeError("Unexpected payload files")
    if manifest["release"] == CPU_RELEASE:
        if (manifest.get("target") not in CPU_TARGETS or manifest["features"] != ["common"]
                or manifest.get("linkage") != "static"
                or any(re.search(r"cuda|cublas|nvidia|\.dll$|\.so(?:\.|$)", name, re.I)
                       for name in manifest["files"])):
            raise RuntimeError("CPU SDK requires static common/CPU libraries without GPU payloads")
        audit_cache(manifest["primary_cmake"], manifest["target"], variant="cpu")
    if "cuda" in manifest["features"]:
        filename = "ggml-cuda.dll" if "windows" in manifest["target"] else "libggml-cuda.so"
        if not (output / "lib/cuda12" / filename).is_file() or "fallback_cmake" not in manifest:
            raise RuntimeError("Missing CUDA 12 fallback")
        cpu = list((output / "lib").glob("*ggml-cpu-*"))
        if len(cpu) < 2:
            raise RuntimeError("Missing CPU backend variants")
        if any(re.search(r"(?:cublas|cudart|nvcuda|libcuda\.)", name) for name in manifest["files"]):
            raise RuntimeError("NVIDIA runtime libraries must not be redistributed")
    return manifest


def external(output, target_dir):
    manifest = verify(output)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("CMAKE_", "GGML_", "LLAMA_"))}
    env.update(LLAMA_CPP_BUILD_MODE="external", LLAMA_CPP_RUNTIME_DIR=str(output),
               LLAMA_CPP_RUNTIME_MANIFEST_SHA256=digest(output / "manifest.json"),
               LIBCLANG_PATH="/external-mode-must-not-use-libclang", CXX="/external-mode-must-not-compile-cpp",
               CUDA_PATH="/external-mode-must-not-need-cuda-toolkit")
    out, binary = cargo_build(manifest["target"], target_dir, env, features=manifest["features"])
    if (out / "build").exists() or (out / "backends").exists():
        raise RuntimeError("External mode unexpectedly built native code")
    command = ["cargo", "test", "--locked", "--release", "-p", "native-smoke",
               "--target", manifest["target"], "--target-dir", target_dir]
    if "cuda" in manifest["features"]:
        command += ["--features", "cuda"]
    test_env = env.copy()
    if os.name == "nt":
        test_env["PATH"] = str(output / "lib") + os.pathsep + env["PATH"]
    elif "linux" in manifest["target"]:
        test_env["LD_LIBRARY_PATH"] = str(output / "lib")
    run(command, env=test_env)
    destination = output.parent / "probe" / binary.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, destination)
    if "linux" in manifest["target"] and "cuda" in manifest["features"]:
        run(["patchelf", "--set-rpath", "$ORIGIN/../bundle/lib", destination])
    return destination


def package(output, destination):
    manifest = verify(output)
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"llama-cpp-runtime-{manifest['release']}-{manifest['target']}.tar.gz"
    if archive.exists():
        raise RuntimeError(f"Refusing to overwrite {archive}")
    with tarfile.open(archive, "w:gz", compresslevel=6) as tar:
        for path in sorted(output.rglob("*")):
            if path.is_file():
                info = tar.gettarinfo(str(path), arcname=path.relative_to(output).as_posix())
                info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = ""
                with path.open("rb") as data:
                    tar.addfile(info, data)
    (destination / (archive.name + ".sha256")).write_text(f"{digest(archive)}  {archive.name}\n", encoding="utf-8")
    suffix = "-cpu" if manifest["release"] == CPU_RELEASE else ""
    shutil.copy2(output / "manifest.json", destination / f"{manifest['target']}{suffix}.manifest.json")
    print(f"Archive: {archive.name}, {archive.stat().st_size:,} bytes, SHA256 {digest(archive)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["primary", "fallback", "verify", "external", "package"])
    parser.add_argument("--bundle", type=Path, default=Path("target/native/bundle"))
    parser.add_argument("--target", choices=sorted(TARGETS.keys() | CPU_TARGETS.keys()))
    parser.add_argument("--variant", choices=["default", "cpu"], default="default")
    parser.add_argument("--target-dir", type=Path, default=Path("target/native-build"))
    parser.add_argument("--cuda-root", type=Path, default=Path("/usr/local/cuda"))
    parser.add_argument("--dist", type=Path, default=Path("target/dist"))
    args = parser.parse_args()
    output, target_dir = args.bundle.resolve(), args.target_dir.resolve()
    if args.command == "primary":
        if not args.target:
            parser.error("primary requires --target")
        primary(args.target, output, target_dir, args.variant)
    elif args.command == "fallback":
        fallback(output, target_dir, args.cuda_root.resolve())
    elif args.command == "external":
        external(output, target_dir)
    elif args.command == "package":
        package(output, args.dist.resolve())
    else:
        verify(output)


if __name__ == "__main__":
    main()
