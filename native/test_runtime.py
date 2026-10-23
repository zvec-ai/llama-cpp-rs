import io
import json
from pathlib import Path
import re
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import runpy
from fetch import extract
from runtime import ARCH12, ARCH13, TARGETS, audit_cache, digest, verify, RELEASE, REVISION, recover_incomplete_cmake, verify_source, source_from_build
from runtime import CPU_TARGETS, CPU_RELEASE, ROOT, primary, sdk_features, package, seal, write_json, main
import hashlib


class ContractTests(unittest.TestCase):
    def test_three_cpu_variants_and_no_cuda_tool_probe(self):
        self.assertEqual(set(CPU_TARGETS), {"x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc",
                                          "aarch64-pc-windows-msvc"})
        self.assertEqual(len(TARGETS) + len(CPU_TARGETS), 8)
        with self.assertRaises(RuntimeError):
            sdk_features("aarch64-apple-darwin", "cpu")
        with self.assertRaisesRegex(RuntimeError, "requires --variant cpu"):
            sdk_features("aarch64-pc-windows-msvc")
        for target in CPU_TARGETS:
            self.assertEqual(sdk_features(target, "cpu"), ["common"])
            if target in TARGETS:
                self.assertIn("cuda", sdk_features(target))
            with tempfile.TemporaryDirectory() as temp, \
                    patch("runtime.subprocess.check_output", side_effect=AssertionError("unexpected CUDA probe")), \
                    patch("runtime.cargo_build", side_effect=RuntimeError("stop before compilation")) as build:
                with self.assertRaisesRegex(RuntimeError, "stop before compilation"):
                    primary(target, Path(temp) / "bundle", Path(temp) / "build", "cpu")
                self.assertEqual(build.call_args.kwargs["features"], ["common"])
                if target == "aarch64-pc-windows-msvc":
                    env = build.call_args.args[2]
                    self.assertEqual(env["CC"], "clang-cl")
                    self.assertEqual(env["CXX"], "clang-cl")
                    self.assertEqual(env["CMAKE_GENERATOR"], "Ninja")

    def test_windows_arm64_cpu_cli_and_existing_ci_matrix(self):
        target = "aarch64-pc-windows-msvc"
        with patch("sys.argv", ["runtime.py", "primary", "--target", target, "--variant", "cpu"]), \
                patch("runtime.primary") as build:
            main()
            self.assertEqual(build.call_args.args[0], target)
            self.assertEqual(build.call_args.args[3], "cpu")
        workflow = (ROOT / ".github/workflows/native-release.yml").read_text()
        matrices = [json.loads(value) for value in re.findall(r"'(\[\{.*?\}\])'", workflow, re.S)]
        self.assertEqual(len(matrices), 2)
        self.assertEqual({entry["target"] for entry in matrices[0]}, set(CPU_TARGETS))
        self.assertEqual({entry["target"] for entry in matrices[1]}, set(TARGETS))
        self.assertEqual(next(entry["runner"] for entry in matrices[0] if entry["target"] == target),
                         "windows-11-vs2026-arm")

    def test_cpu_packaging_and_existing_publisher_prepare_only(self):
        # Synthetic archive contracts, not Linux/Windows native execution.
        cache = {key: "OFF" for key in ["GGML_NATIVE", "GGML_OPENMP", "LLAMA_BUILD_TESTS",
            "LLAMA_BUILD_EXAMPLES", "LLAMA_BUILD_TOOLS", "LLAMA_BUILD_APP", "LLAMA_BUILD_SERVER",
            "LLAMA_CURL", "BUILD_SHARED_LIBS", "GGML_CUDA", "GGML_BACKEND_DL", "GGML_CPU_ALL_VARIANTS",
            "GGML_METAL", "GGML_VULKAN", "GGML_OPENCL", "GGML_HIP"]}
        cache.update(CMAKE_BUILD_TYPE="Release", LLAMA_BUILD_COMMON="ON", GGML_CPU="ON")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            assets = root / "assets"
            for target in CPU_TARGETS:
                bundle = root / target
                bundle.mkdir()
                (bundle / "bindings.rs").write_bytes(b"// fixture, not native code")
                (bundle / "lib").mkdir()
                for name in ("llama", "ggml", "ggml-base", "ggml-cpu"):
                    filename = f"{name}.lib" if "windows" in target else f"lib{name}.a"
                    (bundle / "lib" / filename).write_bytes(b"archive fixture")
                manifest = {"schema": 1, "release": CPU_RELEASE, "crate_version": "0.1.154",
                    "llama_cpp_revision": REVISION, "source_commit": "1" * 40, "target": target,
                    "features": ["common"], "linkage": "static", "primary_cmake": cache,
                    "link_libraries": [f"static={name}" for name in ("llama", "ggml", "ggml-base", "ggml-cpu")]}
                write_json(bundle / "manifest.json", manifest)
                seal(bundle)
                package(bundle, assets)
                archive = next(assets.glob(f"*{target}.tar.gz"))
                extract(archive, root / (target + "-relocated"))
                verify(root / (target + "-relocated"))
                write_json(bundle / "manifest.json", {**manifest, "primary_cmake": {**cache, "GGML_OPENMP": "ON"}})
                seal(bundle)
                with self.assertRaisesRegex(RuntimeError, "Build contract mismatch"):
                    verify(bundle)
            env = {"GITHUB_SHA": "1" * 40, "GITHUB_SERVER_URL": "https://github.com",
                   "GITHUB_REPOSITORY": "zvec-ai/llama-cpp-rs", "GITHUB_RUN_ID": "fixture"}
            command = ["publish.py", "--assets", str(assets), "--variant", "cpu", "--prepare-only"]
            with patch("sys.argv", command), patch.dict("os.environ", env), \
                    patch("subprocess.run", side_effect=AssertionError("must not call GitHub")):
                with self.assertRaises(SystemExit) as stopped:
                    runpy.run_path(str(ROOT / "native/publish.py"), run_name="__main__")
                self.assertEqual(stopped.exception.code, 0)
            lock = json.loads((assets / "cpu-runtime-lock.json").read_text())
            self.assertEqual(set(lock["targets"]), set(CPU_TARGETS))
            for target, entry in lock["targets"].items():
                self.assertEqual(entry["features"], ["common"])
                self.assertEqual(entry["cmake"]["GGML_CUDA"], "OFF")
                self.assertEqual(entry["sha256"], digest(assets / entry["archive"]))
            self.assertNotIn("T4 GPU runners", (assets / "release-notes.md").read_text())
            archive.write_bytes(b"corrupted")
            with patch("sys.argv", command), patch.dict("os.environ", env), \
                    patch("subprocess.run", side_effect=AssertionError("must not call GitHub")):
                with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                    runpy.run_path(str(ROOT / "native/publish.py"), run_name="__main__")

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
        lock = json.loads((ROOT / "llama-cpp-sys-2/build_support/runtime-lock.json").read_text())
        self.assertEqual(RELEASE, "zvec-0.1.154-r1")
        self.assertEqual(lock["release"], RELEASE)
        for target in ("x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc"):
            self.assertEqual(lock["targets"][target]["cmake"]["CMAKE_CUDA_ARCHITECTURES"], ARCH13)

    def test_clang_cl_requires_cpp_exceptions_without_changing_other_compilers(self):
        lock = json.loads((ROOT / "llama-cpp-sys-2/build_support/cpu-runtime-lock.json").read_text())
        base = dict(lock["targets"]["x86_64-pc-windows-msvc"]["cmake"])
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
            root = Path(temp)
            payload = root / "bindings.rs"
            payload.write_text("bindings")
            manifest = {"release": RELEASE, "llama_cpp_revision": REVISION, "features": [],
                        "files": {"bindings.rs": digest(payload)}}
            (root / "manifest.json").write_text(json.dumps(manifest))
            verify(root)
            payload.write_text("modified")
            with self.assertRaises(RuntimeError):
                verify(root)
            payload.write_text("bindings")
            (root / "untracked-library.so").write_text("extra")
            with self.assertRaises(RuntimeError):
                verify(root)

    def test_incomplete_cmake_contract_fails(self):
        with self.assertRaises(RuntimeError):
            audit_cache({"CMAKE_BUILD_TYPE": "Debug"}, "aarch64-apple-darwin")

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
