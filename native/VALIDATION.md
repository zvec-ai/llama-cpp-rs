# Upstream refresh validation — 2026-11-13

Code under test: `af2a2ecac4afb30edf26953945721dda1db8ec20` on
`codex/upstream-main-sdk-refresh`, based on upstream
`955aa2f72463c2535cc5ab894d5d52f27a38cde2` (crate 0.1.162).
Native source: llama.cpp v0.9.0,
`cd959bae99ff4bed22bef2604f45241c09bbb8c8`.

## Local checks (macOS ARM64)

- Rust formatting and whitespace checks passed.
- 19 Python packaging tests passed, including a real Git `autocrlf` checkout
  and macOS deployment-target metadata regression tests.
- 30 Rust SDK/source/cache contract tests passed, including the two tests
  against the actual pinned source archive and a read-only source tree.
- Source-mode safe library tests: 25 passed.
- External SDK library tests: 24 safe-wrapper tests and one sys test passed.
- Contract-test Clippy passed with warnings denied. Normal library Clippy
  passed; strict library Clippy reports warnings in unchanged upstream code.
- The binding generator compiled. The sys crate packaged successfully
  (21 files, about 60 KiB compressed).
- A relocated SDK built and ran a fresh Rust consumer with native compiler
  paths deliberately disabled. macOS dependency audit passed.
- A separate Cargo Git consumer initialized the SDK in `prebuilt` mode,
  first from a pinned local archive into an empty cache, then from that cache
  offline without an archive. Its test-only lock was confined to a temporary
  repository; production locks were not populated.
- EmbeddingGemma and EmbeddingGemma 2 produced finite, nonzero 768-dimensional
  embeddings on CPU and Metal, with 25/25 model layers offloaded. CPU/Metal
  cosine similarities were 0.9998066001 and 0.9999283776 respectively.
- A Windows ARM64 CI SDK passed archive, payload and ABI verification on macOS.
  Its bindings fingerprint matched the local/Linux fingerprint.

Local command output and inference evidence are under `target/` (not committed).
The ABI-bearing files are unchanged by the later CI/packaging fixes, so the
local SDK built at `6f6828a` remains valid for this code revision.

## CI

- [CPU matrix](https://github.com/zvec-ai/llama-cpp-rs/actions/runs/43382610061):
  passed on Linux x64, Windows x64 and Windows ARM64, including both models,
  rebuilt external consumers, platform audits and cross-platform release
  metadata preparation. Linux build and execution used glibc 2.28.
- [Default matrix](https://github.com/zvec-ai/llama-cpp-rs/actions/runs/43382597675):
  passed on macOS ARM64/Intel, Linux ARM64, and Linux/Windows x64 CUDA SDKs,
  including rebuilt consumers, both models on CPU, dependency audits and
  release metadata preparation.
- Linux and Windows Tesla T4 GPU jobs passed with both models and both CUDA
  plugins. All eight comparisons offloaded 25/25 layers and exceeded the
  0.999 CPU/GPU cosine threshold: minimum 0.9997334716 overall and
  0.9999283345 for EmbeddingGemma 2. CUDA 12.4-built fallback plugins used a
  CUDA 12.4 runtime on Linux and a CUDA 12.8 runtime on Windows; primary
  plugins used CUDA 13.1. The evidence manifest digests match the prepared
  release metadata for the exact tested archives.

The Windows GPU job passed on attempt 2 after an earlier cancellation
interrupted verification following a slow runtime installation. Successful
builds and the Linux GPU result were retained; no native code changed between
attempts. The final workflow conclusion is success, with publication skipped.

The earlier macOS 14 runner cancellation and Windows CRLF fingerprint mismatch
were fixed before these runs. This record does not count earlier runs as
validation of the final CI configuration.

## Release state

No Release or crates.io package has been published, and zg is unchanged.
Verified metadata for all eight SDK configurations is available in the CI
`release-metadata` artifacts and locally under `target/verified-release-metadata/`.
Embedded SDK locks remain empty until new immutable archives are published and
pinned. `auto` therefore builds the verified source; `prebuilt` requires the
future pins. External SDK use is validated as described above.
