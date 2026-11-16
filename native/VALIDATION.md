# SDK validation

## Unified release — 2026-11-16

[zvec-0.1.162-r1](https://github.com/zvec-ai/llama-cpp-rs/releases/tag/zvec-0.1.162-r1)
was published as a public pre-release at 2026-11-16 04:08:42 UTC.
[The complete workflow](https://github.com/zvec-ai/llama-cpp-rs/actions/runs/43794911773)
finished successfully: eight SDK jobs, Linux and Windows GPU jobs, metadata,
publication, and eight public-download consumer jobs.
Packaging commit: `a88ee9e02af6a0e7707e388beb9b54141f960de7`.
Consumer pins: `24f44438703bfbd6271d172c9ba883d010e37938`.

### Artifacts and compatibility

- One release contains eight SDK archives, eight matching manifests,
  `runtime-lock.json`, `SHA256SUMS`, and `validation-report.md`: 19 assets.
- CPU: macOS x64, Linux x64/ARM64, Windows x64/ARM64. Metal: macOS ARM64.
  CUDA: Linux and Windows x64, each with CPU, CUDA 13.1 primary and CUDA 12.4
  fallback. NVIDIA runtime/driver libraries are provided by the host.
- Linux SDKs and separately linked consumers passed on glibc 2.28, including
  CPU execution without NVIDIA libraries. Audited ceilings are GLIBC 2.28,
  GLIBCXX 3.4.25 and CXXABI 1.3.11.
- Published macOS deployment targets are ARM64 15.5 and x64 14.0.
- Seven native payloads were reused from the successful November 13 workflows
  at `af2a2ecac4afb30edf26953945721dda1db8ec20`; their bytes and original build
  commits are preserved. Intel macOS was rebuilt at the packaging commit to
  explicitly disable Metal. Repackaging provenance is recorded separately.
- The embedded index is byte-identical to both the trusted CI artifact and
  anonymously downloaded public index. Its SHA256 is
  `0c58747cc3e1a2063133dedb89de7cf313b6aaf249254ef35173fe630ef9430f`.

### Acceptance results

- Every final archive passed payload/manifest/ABI checks, fresh external Rust
  consumer linking without native compilation, real CPU inference with both
  checksum-pinned models, and platform dependency audits.
- Metal and both CUDA plugins on Linux/Windows passed real GPU offload with
  both models: 25/25 layers, valid 768-dimensional embeddings, and CPU/GPU
  cosine similarity above 0.999. The minimum was 0.9997334716 overall and
  0.9999283345 for EmbeddingGemma 2. Evidence matches the final manifest hashes.
- The publisher downloaded and rehashed all 19 draft assets before publication.
  After publication, CI anonymously downloaded all 19 assets and verified all
  eight archive payloads. All eight public SDK consumers then passed cold HTTP
  SDK download, offline cache reuse, both models on CPU, and platform audits.
- A separate macOS ARM64 project consumed the fork through a Cargo Git dependency
  pinned to `24f44438703bfbd6271d172c9ba883d010e37938`. It downloaded the public
  SDK into an empty cache, built without native compilation, and reran the build
  script with Cargo and SDK downloads disabled to verify offline reuse. Both
  models passed CPU and Metal inference from that downloaded SDK, with 25/25
  layers offloaded and cosine similarities 0.9998066001 and 0.9999283776.
  Evidence is under `target/unified-git-public-*` (not committed).
- Final local checks passed: 21 Python tests, 27 Rust contract tests (the two
  real-source tests were not repeated; their earlier successful execution is
  recorded below), formatting, contract Clippy with warnings denied, actionlint,
  and whitespace checks.
- Paid hosted Apple GPU capacity was unavailable. A repository-scoped,
  one-job ephemeral Apple GPU runner executed the same Metal gates, then
  automatically deregistered. Its installation and work checkout were removed;
  the repository runner count returned to zero.

No crates.io package was published. The safe Rust API remains unchanged from
upstream `955aa2f72463c2535cc5ab894d5d52f27a38cde2`; no zg code or dependency
was changed. Published assets are immutable; a change requires a new revision.

## Historical upstream refresh validation — 2026-11-13

Code under test: `af2a2ecac4afb30edf26953945721dda1db8ec20` on
`codex/upstream-main-sdk-refresh`, based on upstream
`955aa2f72463c2535cc5ab894d5d52f27a38cde2` (crate 0.1.162).
Native source: llama.cpp v0.9.0,
`cd959bae99ff4bed22bef2604f45241c09bbb8c8`.

### Local checks (macOS ARM64)

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

### CI

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

### Release state on 2026-11-13

At that point no Release or crates.io package had been published, and zg was
unchanged. Verified metadata for all eight SDK configurations was available in the CI
`release-metadata` artifacts and locally under `target/verified-release-metadata/`.
Embedded SDK locks were empty pending publication: `auto` built the verified
source and `prebuilt` required future pins. The unified release and populated
pins described above supersede that state.
