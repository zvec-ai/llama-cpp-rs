import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from fetch import extract
from runtime import ARCH12, ARCH13, TARGETS, audit_cache, digest, verify, RELEASE, REVISION, recover_incomplete_cmake


class ContractTests(unittest.TestCase):
    def test_only_two_cuda_targets(self):
        self.assertEqual({target for target, features in TARGETS.items() if "cuda" in features},
                         {"x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc"})
        self.assertEqual(ARCH13, "75-real;80-real;86-real;89-real;90-real;90-virtual")
        self.assertEqual(ARCH12, "50-virtual;61-virtual;70-virtual;75-virtual;80-virtual;86-real")

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
