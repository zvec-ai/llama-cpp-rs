# Native SDK integration on upstream 0.1.162

This upgrade starts at `utilityai/llama-cpp-rs@955aa2f72463c2535cc5ab894d5d52f27a38cde2`
(upstream `main`, inspected 2026-11-13). It uses upstream's llama.cpp v0.9.0
revision `cd959bae99ff4bed22bef2604f45241c09bbb8c8` and checked-in, portable
Rust bindings. The safe Rust API and native inference sources are unchanged.

The necessary build/distribution changes are ported from
`zvec-ai/llama-cpp-rs@79305567e63ee551eb1798c0537eab87d9595a23` as separate
commits: external SDK consumption, lazy source acquisition, automatic SDK/cache
selection, and platform SDK tooling. The separate context-thread-budget branch
is not part of that production baseline and is not included.

## Upgrade state

The two embedded runtime locks intentionally have no targets. Old 0.1.154
archives are not ABI-compatible and must not be reused. Until new archives are
verified, published and pinned, `auto` builds from the locked source and
`prebuilt` fails with an explicit missing-SDK error. Local SDKs can be consumed
through `external`. Test fixtures are synthetic selection metadata, not
downloadable production artifacts.

No zg dependency or code is changed. No crates.io package is published.
Pushing the upgrade branch runs native validation without publishing a Release.
Publishing additionally requires explicit `publish=true` on a
`prebuilt/0.1.162*` release branch and all platform/GPU gates to pass.

## Consumer modes

- `LLAMA_CPP_BUILD_MODE=auto` (default): select a matching checksum-pinned SDK;
  otherwise compile the exact pinned source. SDK download/checksum failures
  are errors, never implicit source fallbacks.
- `prebuilt`: require an exact target/features/native-options match.
- `source`: verify and compile the source in `build_support/source-lock.json`.
- `external`: use `LLAMA_CPP_RUNTIME_DIR` and the explicitly pinned
  `LLAMA_CPP_RUNTIME_MANIFEST_SHA256`, without downloads or C++ compilation.

SDK manifest schema 2 checks the crate version, native revision, target,
features, linkage, payload hashes, and a fingerprint of upstream's checked-in
bindings plus the C++ wrappers. It does not copy generated Rust code into
OUT_DIR. A change to those ABI inputs requires a matching SDK rebuild.

SDK cache: `$CARGO_HOME/llama-cpp-runtimes`, overridden by
`LLAMA_CPP_RUNTIME_CACHE`. `LLAMA_CPP_RUNTIME_ARCHIVE` supplies a pinned local
archive. Source cache: `$CARGO_HOME/llama-cpp-sources`, overridden by
`LLAMA_CPP_SOURCE_CACHE`. `LLAMA_CPP_SOURCE_DIR` and
`LLAMA_CPP_SOURCE_ARCHIVE` supply a byte-identical source tree/archive.
`LLAMA_CPP_OFFLINE=1` or `CARGO_NET_OFFLINE=true` prohibits downloads.
Cache entries are verified and populated atomically under a file lock.

The fork removes the source submodule to avoid Cargo fetching it before SDK
selection. `generate-bindings` accepts `LLAMA_CPP_SOURCE_DIR` for maintainer
regeneration. Consumer builds use upstream's already generated bindings.

## Platforms and compatibility

The default matrix builds macOS ARM64/Intel, Linux ARM64 CPU, and Linux/Windows
x64 CUDA SDKs. A separate `variant=cpu` matrix covers Linux x64, Windows x64,
and Windows ARM64 CPU SDKs. Vulkan/custom features retain source-build support.

Linux builds and runtime acceptance use glibc 2.28 environments. The ABI ceiling
is GLIBC 2.28, GLIBCXX 3.4.25 and CXXABI 1.3.11. `LLAMA_SUBPROCESS=OFF` avoids
newer glibc subprocess interfaces while keeping common inference APIs enabled.
Windows ARM64 uses clang-cl with the MSVC SDK/CRT and C++ exceptions enabled.
CUDA 13.1 is primary, with a CUDA 12.4 fallback plugin sharing the same core.
NVIDIA runtime/driver libraries are supplied by the deployment environment.

## Validation and release

```sh
cargo fmt --all --check
cargo test --locked -p native-contract-tests
python3 native/runtime.py primary --target aarch64-apple-darwin
python3 native/runtime.py package
python3 native/unpack.py --artifacts target/dist --output target/relocated/bundle
python3 native/runtime.py external --bundle target/relocated/bundle --target-dir target/external-consumer
python3 native/e2e.py --bundle target/relocated/bundle --mode metal
python3 native/e2e.py --bundle target/relocated/bundle --mode metal --model-kind embeddinggemma2 --logs target/evidence/gemma2
python3 native/audit.py --bundle target/relocated/bundle --logs target/evidence
python3 -m unittest discover -s native -p 'test_*.py' -v
```

Use Python 3.12+ and the pinned Rust toolchain. Native compilation requires
CMake, a C++ toolchain, and the selected backend tools. SDK consumers do not
need bindgen, CMake or a CUDA toolkit. SDK builds use Release native libraries
regardless of the Rust consumer's debug/release profile.

Tests cover checksum-pinned EmbeddingGemma and EmbeddingGemma 2 weights, valid
768-dimensional embeddings, actual GPU offload and CPU/GPU cosine similarity
of at least 0.999. CUDA tests check which plugin was loaded. Linux additionally
checks CPU inference without NVIDIA libraries and audits every shared library
plus a separately linked consumer on glibc 2.28.

After all platform gates pass, prepare metadata with
`python3 native/publish.py --assets <merged-artifacts> --variant <default|cpu> --prepare-only`.
Publish the exact verified archives under new immutable release names, then
import the generated locks. Both default and CPU locks include features,
configuration and archive/manifest hashes. Consumers pin a fork commit;
they do not trust a changing remote index.

For later upstream refreshes, start at an explicitly recorded upstream SHA,
retain its bindings and safe API, replay the small build integrations, update
source/archive/tree hashes, clear incompatible SDK pins, and repeat native
validation. Avoid carrying old release evidence forward as evidence for a new ABI.
