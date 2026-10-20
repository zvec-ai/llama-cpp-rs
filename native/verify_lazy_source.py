#!/usr/bin/env python3
"""Opt-in macOS ARM64 verification using a fresh Cargo Home and Git dependency.

Creates only disposable fixtures/logs below --output (or target/lazy-source-*).
Never commits the working repository, pushes, publishes or dispatches CI.
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile

from runtime import ROOT, REVISION, read_json, verify_source, write_json


def verify(args):
    if os.name != "posix":
        raise RuntimeError("This opt-in guard harness currently requires a POSIX shell")
    host = next(line.split(": ", 1)[1] for line in subprocess.check_output(["rustc", "-vV"], text=True).splitlines() if line.startswith("host: "))
    if host != "aarch64-apple-darwin":
        raise RuntimeError("Actual build/inference in this harness is currently scoped to macOS ARM64")
    if args.output:
        output = args.output.resolve()
        output.mkdir(parents=True, exist_ok=False)
    else:
        (ROOT / "target").mkdir(exist_ok=True)
        output = Path(tempfile.mkdtemp(prefix="lazy-source-", dir=ROOT / "target"))
    print(f"Evidence: {output}", flush=True)
    report = {"host": host, "output": str(output), "checks": [], "status": "running",
              "scope": "local Git snapshot; no working-branch commit/push/CI or zg changes"}
    logs = output / "logs"
    logs.mkdir()
    cargo_home = output / "cargo-home"
    cargo_home.mkdir()
    assert not list(cargo_home.iterdir())
    report["cargo_home_started_empty"] = True
    snapshot = output / "snapshot"
    snapshot.mkdir()
    # Include current source edits but never the ignored legacy checkout/targets.
    files = subprocess.check_output(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT).split(b"\0")
    for name in filter(None, files):
        relative = Path(os.fsdecode(name))
        source = ROOT / relative
        if source.is_file():
            destination = snapshot / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    assert not (snapshot / ".gitmodules").exists()
    assert not (snapshot / "llama-cpp-sys-2/llama.cpp").exists()
    git = shutil.which("git")
    curl = shutil.which("curl")
    subprocess.run([git, "init", "--quiet", str(snapshot)], check=True)
    subprocess.run([git, "add", "--all"], cwd=snapshot, check=True)
    subprocess.run([git, "-c", "core.hooksPath=/dev/null", "-c", "user.name=Local verification fixture", "-c", "user.email=fixture@invalid", "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "local verification fixture"], cwd=snapshot, check=True)
    commit = subprocess.check_output([git, "rev-parse", "HEAD"], cwd=snapshot, text=True).strip()
    assert "160000 " not in subprocess.check_output([git, "ls-tree", "-r", "HEAD"], cwd=snapshot, text=True)
    report["fixture_commit"] = commit
    report["native_revision"] = REVISION
    consumer = output / "consumer"
    (consumer / "src").mkdir(parents=True)
    (consumer / "src/main.rs").write_bytes((ROOT / "native/smoke/src/main.rs").read_bytes())
    manifest = '[workspace]\n[package]\nname="lazy-source-probe"\nversion="0.0.0"\nedition="2021"\n[dependencies]\nserde_json="1"\n'
    for crate in ["llama-cpp-2", "llama-cpp-sys-2"]:
        options = ', default-features=false' if crate == "llama-cpp-2" else ''
        manifest += f'{crate}={{git={json.dumps(snapshot.as_uri())}, rev="{commit}"{options}}}\n'
    (consumer / "Cargo.toml").write_text(manifest, encoding="utf-8")
    guards = output / "guards"
    sdk_guards = output / "sdk-guards"
    guards.mkdir()
    sdk_guards.mkdir()
    git_trace, curl_trace = output / "git.trace", output / "curl.trace"
    wrappers = {
        guards / "git": f'#!/bin/sh\nprintf "%s\\n" "$*" >> {shlex.quote(str(git_trace))}\ncase "$*" in *ggml-org/llama.cpp*|*submodule*) echo "unexpected native Git fetch" >&2; exit 93;; esac\nexec {shlex.quote(git)} "$@"\n',
        guards / "curl": f'#!/bin/sh\nprintf "%s\\n" "$*" >> {shlex.quote(str(curl_trace))}\nif [ "$LAZY_SOURCE_ALLOW_FETCH" != 1 ]; then echo "unexpected download" >&2; exit 94; fi\ncase "$*" in *https://codeload.github.com/ggml-org/llama.cpp/tar.gz/{REVISION}*) exec {shlex.quote(curl)} "$@";; *) echo "unexpected download URL" >&2; exit 95;; esac\n',
        sdk_guards / "cmake": '#!/bin/sh\necho "CMake must not run for an SDK" >&2\nexit 97\n',
    }
    for path, content in wrappers.items():
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)
    original_path = os.environ["PATH"]
    env = os.environ.copy()
    for name in list(env):
        if name.startswith(("LLAMA_CPP_", "GGML_", "CMAKE_")) or name in ("LIBCLANG_PATH", "CARGO_NET_OFFLINE"):
            env.pop(name)
    env.update(CARGO_HOME=str(cargo_home), CARGO_TARGET_DIR=str(output / "build"),
               CARGO_NET_GIT_FETCH_WITH_CLI="true", CARGO_BUILD_JOBS="4",
               PATH=os.pathsep.join([str(guards), original_path]), LAZY_SOURCE_ALLOW_FETCH="0")
    sdk_lock = read_json(ROOT / "llama-cpp-sys-2/build_support/runtime-lock.json")["targets"][host]
    sdk_cache, source_cache = output / "sdk-cache", output / "source-cache"

    def run(label, command, variables, failure=None):
        print(f"RUN {label}", flush=True)
        log = logs / f"{label}.log"
        with log.open("wb") as stream:
            result = subprocess.run(list(map(str, command)), cwd=consumer, env=variables, stdout=stream, stderr=subprocess.STDOUT)
        text = log.read_text(encoding="utf-8", errors="replace")
        report["checks"].append({"name": label, "exit_code": result.returncode, "expected_failure": failure, "log": str(log)})
        write_json(output / "report.json", report)
        if (failure is None and result.returncode != 0) or (failure is not None and (result.returncode == 0 or failure not in text)):
            raise RuntimeError(f"{label} failed; see {log}\n{text[-8000:]}")
        print(f"PASS {label}", flush=True)
        return text

    def build(label, variables, failure=None):
        text = run(label, ["cargo", "build", "--offline", "--locked", "--message-format=json"], variables, failure)
        if failure:
            return None
        messages = [json.loads(line) for line in text.splitlines() if line.startswith("{")]
        scripts = [item for item in messages if item.get("reason") == "build-script-executed" and "llama-cpp-sys-2" in item["package_id"]]
        assert len(scripts) == 1, scripts
        binary = next(Path(item["executable"]) for item in messages if item.get("reason") == "compiler-artifact" and item.get("executable"))
        return Path(scripts[0]["out_dir"]), binary

    def assert_no_source():
        assert not source_cache.exists()
        checkouts = list((cargo_home / "git/checkouts").glob("*/*/llama-cpp-sys-2"))
        assert len(checkouts) == 1, checkouts
        assert not (checkouts[0] / "llama.cpp").exists()
        assert not (checkouts[0].parent / ".gitmodules").exists()
        assert len(list((cargo_home / "git/db").iterdir())) == 1
        assert not curl_trace.exists() or "https://codeload.github.com/ggml-org/llama.cpp" not in curl_trace.read_text()

    def assert_sdk(out, only_bindings=True):
        text = (out.parent / "output").read_text(encoding="utf-8")
        assert "no bindgen/CMake/C++/CUDA compilation" in text
        assert "cargo:source_dir=" not in text
        assert str(sdk_cache / sdk_lock["sha256"] / "bundle/lib") in text
        if only_bindings:
            assert [path.name for path in out.iterdir()] == ["bindings.rs"]

    try:
        run("fetch-empty-cargo-home", ["cargo", "fetch", "--target", host], env)
        assert_no_source()
        report["git_dependency_fetch_without_submodule"] = True
        sdk = env | {"PATH": os.pathsep.join([str(sdk_guards), str(guards), original_path]),
                     "LIBCLANG_PATH": "/must-not-use-bindgen", "LLAMA_CPP_OFFLINE": "1",
                     "LLAMA_CPP_RUNTIME_CACHE": str(sdk_cache), "LLAMA_CPP_RUNTIME_ARCHIVE": str(args.sdk_archive.resolve()),
                     "LLAMA_CPP_SOURCE_CACHE": str(source_cache), "LLAMA_CPP_SOURCE_DIR": "/must-not-resolve-source",
                     "LLAMA_CPP_SOURCE_ARCHIVE": "/must-not-read-source-archive"}
        out, binary = build("prebuilt-cold-sdk", sdk | {"LLAMA_CPP_BUILD_MODE": "prebuilt"})
        assert_sdk(out)
        assert_no_source()
        sdk.pop("LLAMA_CPP_RUNTIME_ARCHIVE")
        out, binary = build("auto-warm-sdk", sdk | {"LLAMA_CPP_BUILD_MODE": "auto"})
        assert_sdk(out)
        assert_no_source()
        build("prebuilt-mismatch", sdk | {"LLAMA_CPP_BUILD_MODE": "prebuilt", "GGML_CPU_REPACK": "OFF"}, "prebuilt mode is incompatible")
        assert_no_source()
        build("auto-sdk-miss-is-not-source", sdk | {"LLAMA_CPP_BUILD_MODE": "auto", "LLAMA_CPP_RUNTIME_CACHE": str(output / "missing-sdk")}, "SDK is not cached")
        assert_no_source()
        bad_sdk = output / "bad-sdk.tar.gz"
        bad_sdk.write_bytes(b"not the pinned SDK")
        build("auto-sdk-digest-failure-is-not-source", sdk | {"LLAMA_CPP_BUILD_MODE": "auto", "LLAMA_CPP_RUNTIME_CACHE": str(output / "missing-sdk"), "LLAMA_CPP_RUNTIME_ARCHIVE": str(bad_sdk)}, "SDK archive SHA-256 mismatch")
        assert_no_source()
        build("auto-sdk-download-failure-is-not-source", sdk | {"LLAMA_CPP_BUILD_MODE": "auto", "LLAMA_CPP_RUNTIME_CACHE": str(output / "missing-sdk"), "LLAMA_CPP_OFFLINE": "0"}, "SDK download failed; no implicit source fallback")
        assert_no_source()
        bundle = sdk_cache / sdk_lock["sha256"] / "bundle"
        external = sdk | {"LLAMA_CPP_BUILD_MODE": "external", "LLAMA_CPP_RUNTIME_DIR": str(bundle), "LLAMA_CPP_RUNTIME_MANIFEST_SHA256": sdk_lock["manifest_sha256"]}
        out, binary = build("external-no-source", external)
        assert_sdk(out)
        assert_no_source()
        run("sdk-real-cpu", [binary, bundle, args.model.resolve(), "cpu"], env)
        run("sdk-real-metal", [binary, bundle, args.model.resolve(), "metal"], env)
        source = env | {"LLAMA_CPP_BUILD_MODE": "source", "LLAMA_CPP_SOURCE_CACHE": str(source_cache), "LLAMA_CPP_OFFLINE": "1", "CMAKE_BUILD_PARALLEL_LEVEL": "4"}
        build("source-offline-miss", source, "downloads are disabled")
        source["LLAMA_CPP_OFFLINE"] = "0"
        build("source-download-failure", source, "download of pinned llama.cpp source")
        assert not list(source_cache.glob("*/source"))
        source.update(LLAMA_CPP_BUILD_MODE="auto", GGML_CPU_REPACK="OFF", LAZY_SOURCE_ALLOW_FETCH="1")
        if args.source_archive:
            source["LLAMA_CPP_SOURCE_ARCHIVE"] = str(args.source_archive.resolve())
        out, binary = build("auto-source-cold", source)
        from runtime import source_from_build
        root = source_from_build(out)
        assert root.is_relative_to(source_cache)
        report["resolved_source"] = str(root)
        report["source_cache_tree_verified"] = True
        run("auto-source-real-cpu", [binary, bundle, args.model.resolve(), "cpu"], env)
        before = curl_trace.read_bytes()
        source.update(LLAMA_CPP_BUILD_MODE="source", LLAMA_CPP_OFFLINE="1", LAZY_SOURCE_ALLOW_FETCH="0")
        source.pop("GGML_CPU_REPACK")
        source.pop("LLAMA_CPP_SOURCE_ARCHIVE", None)
        out, binary = build("explicit-source-offline-warm", source)
        assert source_from_build(out) == root
        assert "GGML_CPU_REPACK:BOOL=ON" in (out / "build/CMakeCache.txt").read_text()
        assert curl_trace.read_bytes() == before
        run("source-real-cpu", [binary, bundle, args.model.resolve(), "cpu"], env)
        run("source-real-metal", [binary, bundle, args.model.resolve(), "metal"], env)
        tampered = root / "vendor/lazy-source-integrity-probe.h"
        tampered.write_bytes(b"unexpected source file")
        try:
            build("source-directory-membership-tamper", source, "source tree SHA-256 mismatch")
        finally:
            tampered.unlink()
        build("source-directory-restored", source)
        injected_git = root / ".git"
        injected_git.write_bytes(b"gitdir: unexpected")
        try:
            build("source-cache-git-metadata-tamper", source, "Git metadata is not allowed in a source cache")
        finally:
            injected_git.unlink()
        build("source-cache-restored", source)
        out, _ = build("auto-return-to-sdk", sdk | {"LLAMA_CPP_BUILD_MODE": "auto"})
        assert_sdk(out, only_bindings=False)
        assert curl_trace.read_bytes() == before
        verify_source(root)
        report.update(status="passed", native_source_git_fetches=0, source_download_attempts=sum(b"https://codeload.github.com/ggml-org/llama.cpp" in line for line in before.splitlines()),
                      sdk_modes_source_free=True, source_warm_cache_network_free=True,
                      untested_platforms=["Intel macOS", "Linux", "Windows", "CUDA"])
    except BaseException:
        report["status"] = "failed"
        raise
    finally:
        write_json(output / "report.json", report)
    print(f"PASS all checks; report: {output / 'report.json'}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-archive", type=Path, required=True)
    parser.add_argument("--source-archive", type=Path)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    verify(parser.parse_args())
