import io
import json
from pathlib import Path
import re
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import runpy
import shutil
import subprocess
from fetch import extract
from runtime import bindings_sha256, lock_cmake, CRATE_VERSION, ARCH12, ARCH13, TARGETS, audit_cache, digest, verify, RELEASE, REVISION, recover_incomplete_cmake, verify_source, source_from_build
from runtime import ROOT, primary, sdk_features, sdk_variant, asset_stem, package, seal, write_json, main, LINUX_ABI
from publish import prepare, publish
from reuse import repack
from e2e import MODELS
import hashlib
import fetch


class ContractTests(unittest.TestCase):
    def test_bindings_fingerprint_survives_windows_git_checkout(self):
        expected = bindings_sha256()
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            shutil.copy2(ROOT / ".gitattributes", repo / ".gitattributes")
            files = list((ROOT / "llama-cpp-sys-2/src").glob("*.rs"))
            files += list((ROOT / "llama-cpp-sys-2").glob("wrapper*"))
            for source in files:
                destination = repo / source.relative_to(ROOT)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            checkout = Path(temp) / "checkout"
            for arguments in (["init", "-q"], ["add", "."],
                              ["-c", "core.autocrlf=true", "checkout-index", "--all",
                               f"--prefix={checkout.as_posix()}/"]):
                subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
            with patch("runtime.ROOT", checkout):
                self.assertEqual(bindings_sha256(), expected)

    def test_lock_configuration_preserves_macos_deployment_minimum(self):
        cache = {"CMAKE_CXX_FLAGS": " -O3 -mmacosx-version-min=14.0 -w",
                 "CMAKE_CXX_COMPILER": "/builder/clang++", "GGML_BACKEND_DIR": "/builder/plugins",
                 "GGML_METAL": "ON"}
        self.assertEqual(lock_cmake(cache), {"CMAKE_CXX_FLAGS": cache["CMAKE_CXX_FLAGS"],
                                           "MACOSX_DEPLOYMENT_TARGET": "14.0", "GGML_METAL": "ON"})
        self.assertEqual(lock_cmake({"GGML_CUDA": "ON"}), {"GGML_CUDA": "ON"})

    def fixture_bundle(self, root, target, variant):
        entry = json.loads((ROOT / "native/contracts/fixtures/runtime-lock.json").read_text())["targets"][target][variant]
        root.mkdir(parents=True)
        lib = root / "lib"
        lib.mkdir()
        if variant == "cuda":
            suffix = ".dll" if "windows" in target else ".so"
            for name in ["ggml-cuda", "ggml-cpu-one", "ggml-cpu-two"]:
                (lib / (name + suffix)).write_bytes(b"native fixture")
            (lib / "cuda12").mkdir()
            (lib / "cuda12" / (("ggml-cuda" if "windows" in target else "libggml-cuda") + suffix)).write_bytes(b"fallback")
        else:
            (lib / "libllama.a").write_bytes(b"static fixture")
        manifest = {"schema": 2, "bindings_sha256": bindings_sha256(), "crate_version": CRATE_VERSION,
            "release": RELEASE, "variant": variant, "llama_cpp_revision": REVISION, "target": target,
            "features": entry["features"], "source_commit": "1" * 40,
            "linkage": "dynamic" if variant == "cuda" else "static", "primary_cmake": entry["cmake"]}
        if "linux" in target:
            manifest["linux_abi"] = LINUX_ABI
        if variant == "cuda":
            manifest["fallback_cmake"] = {**entry["cmake"], "CMAKE_CUDA_ARCHITECTURES": ARCH12,
                                          "LLAMA_BUILD_COMMON": "OFF", "GGML_CPU": "OFF"}
        write_json(root / "manifest.json", manifest)
        seal(root)
        return root

    def test_manual_fetch_selects_explicit_variant_and_pinned_release(self):
        for target, variant in [("x86_64-unknown-linux-gnu", "cpu"),
                                ("x86_64-unknown-linux-gnu", "cuda"), ("aarch64-apple-darwin", "metal")]:
            with self.subTest(target=target, variant=variant), tempfile.TemporaryDirectory() as temp:
                command = ["fetch.py", "--target", target, "--variant", variant, "--sha256", "a" * 64,
                           "--lock", str(ROOT / "native/contracts/fixtures/runtime-lock.json"),
                           "--output", str(Path(temp) / "bundle")]
                with patch("sys.argv", command), patch("fetch.subprocess.run", side_effect=RuntimeError("download intercepted")) as download:
                    with self.assertRaisesRegex(RuntimeError, "download intercepted"):
                        fetch.main()
                self.assertTrue(download.call_args.args[0][-1].endswith(f"/{RELEASE}/{asset_stem(target, variant)}.tar.gz"))

    def test_all_eight_variants_and_no_cuda_tool_probe_for_cpu(self):
        self.assertEqual(sum(map(len, TARGETS.values())), 8)
        with self.assertRaises(RuntimeError):
            sdk_features("aarch64-apple-darwin", "cpu")
        for target, variants in TARGETS.items():
            if "cpu" not in variants:
                continue
            with tempfile.TemporaryDirectory() as temp, \
                    patch("runtime.subprocess.check_output", side_effect=AssertionError("unexpected CUDA probe")), \
                    patch("runtime.cargo_build", side_effect=RuntimeError("stop before compilation")) as build:
                with self.assertRaisesRegex(RuntimeError, "stop before compilation"):
                    primary(target, Path(temp) / "bundle", Path(temp) / "build", "cpu")
                self.assertEqual(build.call_args.kwargs["features"], ["common"])
                if "linux" in target:
                    self.assertEqual(build.call_args.args[2]["CMAKE_PROJECT_TOP_LEVEL_INCLUDES"],
                                     str(ROOT / "native/linux-glibc228.cmake"))
                if target == "aarch64-pc-windows-msvc":
                    self.assertEqual(build.call_args.args[2]["CXX"], "clang-cl")

    def test_windows_arm64_cli(self):
        target = "aarch64-pc-windows-msvc"
        with patch("sys.argv", ["runtime.py", "primary", "--target", target, "--variant", "cpu"]), patch("runtime.primary") as build:
            main()
            self.assertEqual(build.call_args.args[0], target)
            self.assertEqual(build.call_args.args[3], "cpu")

    def test_complete_release_requires_exact_archive_evidence_and_all_variants(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            assets, evidence = root / "assets", root / "evidence"
            for target, variants in TARGETS.items():
                for variant in variants:
                    bundle = self.fixture_bundle(root / (target + variant), target, variant)
                    package(bundle, assets)
                    platform = evidence / f"evidence-{target}-{variant}"
                    platform.mkdir(parents=True)
                    (platform / "native-imports.txt").write_text("fixture audit")
                    if "linux" in target:
                        write_json(platform / "linux-abi.json", {"limits": LINUX_ABI})
                    for kind in MODELS:
                        modes = ["cpu", "cuda12", "cuda13"] if variant == "cuda" else ["cpu", "metal"] if variant == "metal" else ["cpu"]
                        for mode in modes:
                            directory = platform if not mode.startswith("cuda") else evidence / ("gpu-evidence-windows" if "windows" in target else "gpu-evidence-linux")
                            output = directory / f"{kind}-{mode}"
                            output.mkdir(parents=True)
                            write_json(output / "result.json", {"model_kind": kind, "model_sha256": MODELS[kind][1],
                                "mode": mode, "manifest_sha256": digest(bundle / "manifest.json"), "cpu_dimension": 768, "cosine_similarity": 0.9999})
            files, notes = prepare(assets, evidence, "1" * 40, "https://example.test/run")
            self.assertEqual(len(files), 19)
            lock = json.loads((assets / "runtime-lock.json").read_text())
            self.assertEqual(sum(map(len, lock["targets"].values())), 8)
            self.assertEqual(len((assets / "SHA256SUMS").read_text().splitlines()), 18)
            bad_evidence = next(evidence.rglob("result.json"))
            saved = bad_evidence.read_text()
            bad_evidence.write_text(saved.replace('"cpu_dimension": 768', '"cpu_dimension": 1'))
            with self.assertRaisesRegex(RuntimeError, "inference evidence"):
                prepare(assets, evidence, "1" * 40, "fixture")
            bad_evidence.write_text(saved)
            with self.assertRaisesRegex(RuntimeError, "Mixed packaging"):
                prepare(assets, evidence, "2" * 40, "fixture")
            next(assets.glob("*.tar.gz")).write_bytes(b"corruption")
            with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                prepare(assets, evidence, "1" * 40, "fixture")

    def test_repack_preserves_native_provenance_and_archive_is_reproducible(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target, variant = "aarch64-pc-windows-msvc", "cpu"
            bundle = self.fixture_bundle(root / "bundle", target, variant)
            original = verify(bundle)
            package(bundle, root / "first")
            package(bundle, root / "second")
            archive = next((root / "first").glob("*.tar.gz"))
            self.assertEqual(digest(archive), digest(next((root / "second").glob("*.tar.gz"))))
            result = repack(archive, root / "repacked", target, variant, "1" * 40, {"run": "fixture"}, "2" * 40)
            self.assertEqual(result["source_commit"], "1" * 40)
            self.assertEqual(result["packaging_commit"], "2" * 40)
            self.assertEqual(result["files"], original["files"])
            self.assertEqual(result["provenance"]["manifest_sha256"], digest(bundle / "manifest.json"))

    def test_publisher_never_replaces_existing_release_or_publishes_partial_draft(self):
        with patch("publish.subprocess.check_output", return_value=json.dumps([{"tag_name": RELEASE}])), patch("publish.subprocess.run") as run:
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                publish([], Path("notes"), "1" * 40)
            run.assert_not_called()
        with patch("publish.subprocess.check_output", side_effect=["[]", json.dumps({"isDraft": True, "isPrerelease": True, "assets": []})]), patch("publish.subprocess.run") as run:
            with self.assertRaisesRegex(RuntimeError, "inventory mismatch"):
                publish([], Path("notes"), "1" * 40)
            self.assertEqual(run.call_count, 1)
            self.assertIn("--draft", run.call_args.args[0])

    def test_source_metadata_and_tree_are_verified_without_git(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            (source / "CMakeLists.txt").write_bytes(b"fixture")
            encoded = b"CMakeLists.txt"
            expected = hashlib.sha256(len(encoded).to_bytes(8, "big") + encoded + digest(source / "CMakeLists.txt").encode()).hexdigest()
            out = root / "build/out"
            out.mkdir(parents=True)
            (out.parent / "output").write_text(f"cargo:source_dir={source}\ncargo:source_revision={REVISION}\n")
            with patch("runtime.SOURCE_LOCK", {"tree_sha256": expected}):
                self.assertEqual(source_from_build(out), source)
                (source / "CMakeLists.txt").write_bytes(b"modified")
                with self.assertRaisesRegex(RuntimeError, "source tree mismatch"):
                    verify_source(source)
            (out.parent / "output").write_text("cargo:source_revision=wrong\n")
            with self.assertRaisesRegex(RuntimeError, "locked source"):
                source_from_build(out)

    def test_only_two_cuda_targets(self):
        self.assertEqual({target for target, features in TARGETS.items() if "cuda" in features},
                         {"x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc"})
        self.assertEqual(ARCH13, "75-real;80-real;86-real;89-real;90-real;90-virtual")
        self.assertEqual(ARCH12, "50-virtual;61-virtual;70-virtual;75-virtual;80-virtual;86-real")
        lock = json.loads((ROOT / "native/contracts/fixtures/runtime-lock.json").read_text())
        self.assertEqual(RELEASE, "zvec-0.1.162-r1")
        self.assertEqual(lock["release"], RELEASE)
        for target in ("x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc"):
            self.assertEqual(lock["targets"][target]["cuda"]["cmake"]["CMAKE_CUDA_ARCHITECTURES"], ARCH13)

    def test_linux_profile_keeps_backends_and_common_without_subprocess(self):
        lock = json.loads((ROOT / "native/contracts/fixtures/runtime-lock.json").read_text())
        for target, variant, entry in [
            ("x86_64-unknown-linux-gnu", "cuda", lock["targets"]["x86_64-unknown-linux-gnu"]["cuda"]),
            ("aarch64-unknown-linux-gnu", "cpu", lock["targets"]["aarch64-unknown-linux-gnu"]["cpu"]),
            ("x86_64-unknown-linux-gnu", "cpu", lock["targets"]["x86_64-unknown-linux-gnu"]["cpu"]),
        ]:
            with self.subTest(target=target, variant=variant):
                # Existing published SDKs remain inspectable. New Linux builds
                # must explicitly remove the optional subprocess dependency.
                cache = dict(entry["cmake"])
                audit_cache(cache, target, variant=variant)
                cache["LLAMA_SUBPROCESS"] = "ON"
                with self.assertRaisesRegex(RuntimeError, "LLAMA_SUBPROCESS"):
                    audit_cache(cache, target, variant=variant, glibc228=True)
                cache["LLAMA_SUBPROCESS"] = "OFF"
                audited = audit_cache(cache, target, variant=variant, glibc228=True)
                self.assertEqual(audited["LLAMA_BUILD_COMMON"], "ON")
                self.assertEqual(audited["GGML_CUDA"], entry["cmake"]["GGML_CUDA"])

    def test_clang_cl_requires_cpp_exceptions_without_changing_other_compilers(self):
        lock = json.loads((ROOT / "native/contracts/fixtures/runtime-lock.json").read_text())
        base = dict(lock["targets"]["x86_64-pc-windows-msvc"]["cpu"]["cmake"])
        base.update(CMAKE_CXX_FLAGS="/MD /O2", CMAKE_CXX_FLAGS_RELEASE="/DNDEBUG")
        for target in ("aarch64-pc-windows-msvc", "x86_64-pc-windows-msvc"):
            for compiler in (r"C:\LLVM\bin\clang-cl.exe", "/llvm/bin/clang-cl"):
                cache = {**base, "CMAKE_CXX_COMPILER": compiler}
                with self.assertRaisesRegex(RuntimeError, "clang-cl.*requires /EHsc"):
                    audit_cache(cache, target, variant="cpu")
                cache["CMAKE_CXX_FLAGS"] += " /EHsc"
                self.assertEqual(audit_cache(cache, target, variant="cpu")["CMAKE_CXX_FLAGS"],
                                 "/MD /O2 /EHsc")
            for compiler in (r"C:\MSVC\bin\cl.exe", "/usr/bin/clang++", "/usr/bin/g++"):
                cache = {**base, "CMAKE_CXX_COMPILER": compiler}
                self.assertEqual(audit_cache(cache, target, variant="cpu")["CMAKE_CXX_FLAGS"],
                                 "/MD /O2")

    def test_tar_rejects_traversal_links_duplicates(self):
        for name, kind in [("../outside", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE),
                           ("link", tarfile.SYMTYPE), ("hard", tarfile.LNKTYPE),
                           ("C:\\bad", tarfile.REGTYPE), ("repeat", tarfile.REGTYPE)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                archive = Path(temp) / "bundle.tar.gz"
                with tarfile.open(archive, "w:gz") as tar:
                    info = tarfile.TarInfo(name)
                    info.type = kind
                    tar.addfile(info, io.BytesIO())
                    if name == "repeat":
                        tar.addfile(info, io.BytesIO())
                with self.assertRaises(RuntimeError):
                    extract(archive, Path(temp) / "output")

    def test_checksum_detects_tampering_and_extra_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.fixture_bundle(Path(temp) / "bundle", "x86_64-pc-windows-msvc", "cpu")
            payload = root / "lib/libllama.a"
            original = payload.read_bytes()
            verify(root)
            payload.write_text("modified")
            with self.assertRaises(RuntimeError):
                verify(root)
            payload.write_bytes(original)
            (root / "untracked-library.so").write_text("extra")
            with self.assertRaises(RuntimeError):
                verify(root)

    def test_incomplete_cmake_contract_fails(self):
        with self.assertRaises(RuntimeError):
            audit_cache({"CMAKE_BUILD_TYPE": "Debug"}, "aarch64-apple-darwin", variant="metal")

    def test_incomplete_cmake_cache_reconfigures_without_removing_objects(self):
        with tempfile.TemporaryDirectory() as temp:
            target = "x86_64-pc-windows-msvc"
            build = Path(temp) / target / "release/build/llama-cpp-sys-2-test/out/build"
            build.mkdir(parents=True)
            cache = build / "CMakeCache.txt"
            cache.write_text("partial configure")
            obj = build / "preserved.obj"
            obj.write_bytes(b"object")
            recover_incomplete_cmake(temp, target)
            self.assertFalse(cache.exists())
            self.assertEqual(cache.with_suffix(".txt.incomplete").read_text(), "partial configure")
            self.assertEqual(obj.read_bytes(), b"object")
            cache.write_text("complete configure")
            (build / "cmake_install.cmake").write_text("install script")
            recover_incomplete_cmake(temp, target)
            self.assertEqual(cache.read_text(), "complete configure")
