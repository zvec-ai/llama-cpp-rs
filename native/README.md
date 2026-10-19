# Versioned native runtime SDK

This is a minimal fork of upstream tag `0.1.154`. `llama-cpp-2`'s safe API and
the source-build implementation are unchanged. `llama-cpp-sys-2/build.rs` gains
an offline, fail-closed external mode. No crates.io packages are published.

## Baseline and supported configurations

Baseline: `zvec-ai/zvec-grep`, branch `codex/cuda-static-linking`, commit
`cc8fa72938db42b648687fccd99b088e94520186`.
Native source: llama.cpp `5f55650a78f92aff4d48d671423e888fac0469ff`.
Bundle release: `zvec-0.1.154-r1`; further changes require a new bundle revision.

| Rust target | Native linkage | sys features |
| --- | --- | --- |
| aarch64-apple-darwin | static, CPU/Metal | common, metal |
| x86_64-apple-darwin | static, upstream Apple backends | common |
| aarch64-unknown-linux-gnu | static, CPU | common |
| x86_64-unknown-linux-gnu | shared core + CPU variants + CUDA plugins | common, cuda, dynamic-link, dynamic-backends |
| x86_64-pc-windows-msvc | shared core + CPU variants + CUDA plugins | common, cuda, dynamic-link, dynamic-backends |

`common` is intentional: upstream's safe crate still enables the sys crate's
default features even when its own defaults are disabled, exactly as in zg.
Apple backend defaults, including embedded Metal shaders and Accelerate, are
left to the pinned upstream build. Intel macOS sets deployment target 14.0,
matching zg. OpenMP is disabled. Release native builds are used for both debug
and release Rust consumers. Linux GNU builds use Ubuntu 24.04; do not claim an
older glibc baseline. MSVC uses the dynamic CRT.

CUDA 13.1.1 primary architectures:
`75-real;80-real;86-real;89-real;90-real;90-virtual`.
CUDA 12.4.1 fallback architectures:
`50-virtual;61-virtual;70-virtual;75-virtual;80-virtual;86-real`.
The fallback is **only** `lib/cuda12/ggml-cuda.dll` or
`lib/cuda12/libggml-cuda.so`, never a second core library. NCCL is disabled.
NVIDIA driver/runtime libraries are supplied by the deployment environment.

## Consumer contract

Pin the fork Git commit and the matching `runtime-lock.json` archive/manifest
digests in the consuming repository. Do not track a moving branch or fetch a
remote checksums file as the source of trust on every build.

Fetch once (Python 3.12+ and curl), then build offline:

```sh
python native/fetch.py --target aarch64-apple-darwin \
  --sha256 <pinned-archive-sha256> --output /absolute/path/to/runtime
export LLAMA_CPP_BUILD_MODE=external
export LLAMA_CPP_RUNTIME_DIR=/absolute/path/to/runtime
export LLAMA_CPP_RUNTIME_MANIFEST_SHA256=<pinned-manifest-sha256>
cargo build --locked
```

The safe crate dependency must use `default-features = false`; enable `cuda`
and `dynamic-backends` together on the two CUDA targets. Unsupported feature
combinations, wrong target/version, source-build overrides, missing files,
extra files, symlinks and hash failures are rejected. No source fallback and
no network access occur in `build.rs`. Source mode remains the upstream default
for compatibility and can be requested explicitly with
`LLAMA_CPP_BUILD_MODE=source`; do not set the runtime directory in source mode.

Each archive contains per-target pre-generated `bindings.rs`, a `manifest.json`,
native link/runtime libraries under `lib/`, and licenses. Generated bindings
are produced alongside the matching binary by the maintainer, **not** by the
consumer. The sys crate still has its upstream Rust build dependencies; external
mode does not execute bindgen, CMake, a C++ compilation or nvcc. A normal Rust
linker, target SDK and platform C++ runtime remain necessary (including the Apple
compiler-runtime search path on macOS).

The manifest records the native feature set, emitted link libraries, relevant
CMake cache entries, compiler information, source revision and payload SHA256s.
`DEP_LLAMA_RUNTIME_DIR` / `DEP_LLAMA_RUNTIME_MANIFEST` are available to direct
sys dependents. The existing `DEP_LLAMA_BACKENDS_DIR` contract is preserved.

For distribution, copy required shared libraries/plugins to the application's
runtime layout, configure executable loader paths, and call the existing
`load_backends_from_path` with the relocated directory. The SDK includes linker
archives/import libraries as well as runtime files; it is **not** an end-user
application package. Avoid shipping the entire SDK when only runtime files are
needed. Linux plugins use `$ORIGIN`, with `$ORIGIN/..` for the fallback. Windows
consumers must arrange DLL search paths before process startup (or use zg's
existing delay-load setup). This step does not change zg's loader or packaging.

## Build and release

`native-release.yml` runs on pushes to `prebuilt/0.1.154`. All five native builds
and real CPU inference tests run on ordinary hosted runners. **Only** the Linux
x64 and Windows x64 runtime verification jobs use `gpu-runners`, labels
`gpu-linux-t4` and `gpu-windows-t4`. They download the already-built SDK and Rust
probe; they do not rebuild native code.

The primary build invokes the upstream Cargo build script, then harvests the
exact OUT_DIR reported by Cargo (no guessed paths or latest-artifact selection).
The fallback uses the same pinned CMake options/toolchains as zg. CMake cache
assertions catch configuration drift. The release archive is then re-extracted
at another path and used to compile the probe in external mode with intentionally
invalid libclang/C++/CUDA tool paths. Rust contract tests cover rejection cases.

Every platform runs a real checksum-pinned EmbeddingGemma 300M Q8 embedding and
checks dimension/finiteness. Linux also runs without NVIDIA libraries installed.
GPU tests exercise CUDA 12-only and CUDA 13 environments, assert actual layer
offload and the loaded plugin path, and compare CPU/GPU embeddings (cosine
similarity >= 0.999). Native dependency audits and inference logs are retained
as Actions artifacts. A published pre-release means all gates passed; it does
not claim every CUDA architecture was executed (hardware verification is T4).

The release job publishes all five SDKs, per-target manifests, `SHA256SUMS` and
`runtime-lock.json`. Existing releases/assets are never overwritten. Upstream
crate publishing and automated update workflows are guarded to upstream's repo
and disabled in this fork. `main`, imported tags and the native submodule are
not modified. zg integration is a separate acceptance-gated step.

Local entry points: `python native/runtime.py --help`, `python native/e2e.py
--help`; unit tests: `python -m unittest discover -s native -p 'test_*.py' -v`.
