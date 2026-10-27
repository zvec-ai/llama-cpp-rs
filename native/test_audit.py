import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit import audit, check_versions, required_versions


NEEDS = """Version needs section '.gnu.version_r' contains 1 entry:
  000000: Version: 1  File: libc.so.6  Cnt: 2
  0x0010:   Name: GLIBC_2.31  Flags: none  Version: 2
  0x0020:   Name: GLIBC_2.17  Flags: none  Version: 3
"""


class LinuxAbiTests(unittest.TestCase):
    def test_only_imports_constrain_compatibility(self):
        text = """Version definition section '.gnu.version_d' contains 1 entry:
  0x0010: Name: GLIBC_2.39
""" + NEEDS + """Version symbols section '.gnu.version' contains 1 entry:
  000: Name: GLIBC_2.38
"""
        self.assertEqual(required_versions(text), ["GLIBC_2.17", "GLIBC_2.31"])

    def test_numeric_limits_and_private_versions(self):
        check_versions(["GLIBC_2.9", "GLIBC_2.31", "GLIBCXX_3.4.28"], "fixture")
        for requirement in ["GLIBC_2.32", "GLIBC_2.34", "GLIBCXX_3.4.29", "GLIBC_PRIVATE", "GLIBC_ABI_DT_RELR"]:
            with self.subTest(requirement=requirement), self.assertRaisesRegex(RuntimeError, requirement):
                check_versions([requirement], "fixture")

    def test_static_sdk_requires_and_audits_linked_consumer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "bundle"
            (bundle / "lib").mkdir(parents=True)
            (bundle / "lib/libllama.a").write_bytes(b"archive fixture")
            manifest = {"target": "x86_64-unknown-linux-gnu"}
            with patch("audit.verify", return_value=manifest):
                with self.assertRaisesRegex(RuntimeError, "external-mode consumer"):
                    audit(bundle, root / "logs")
                probe = root / "probe/native-smoke"
                probe.parent.mkdir()
                probe.write_bytes(b"ELF fixture")
                with patch("audit.readelf", side_effect=lambda path, option: NEEDS if option == "--version-info" else ""):
                    audit(bundle, root / "logs")
                report = json.loads((root / "logs/linux-abi.json").read_text())
                self.assertEqual(report["requirements"]["probe/native-smoke"], ["GLIBC_2.17", "GLIBC_2.31"])
                with patch("audit.readelf", return_value=NEEDS.replace("2.31", "2.34")):
                    with self.assertRaisesRegex(RuntimeError, "probe/native-smoke needs GLIBC_2.34"):
                        audit(bundle, root / "logs")

    def test_cuda_fallback_is_audited_too(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "bundle"
            plugin = bundle / "lib/cuda12/libggml-cuda.so"
            plugin.parent.mkdir(parents=True)
            plugin.write_bytes(b"ELF fixture")
            probe = root / "probe/native-smoke"
            probe.parent.mkdir()
            probe.write_bytes(b"ELF fixture")
            dynamic = "(NEEDED) Shared library: [libcublas.so.12]\n(RUNPATH) Library runpath: [$ORIGIN/..]"
            def elf(path, option):
                return NEEDS.replace("2.31", "2.34") if option == "--version-info" else dynamic
            with patch("audit.verify", return_value={"target": "x86_64-unknown-linux-gnu"}), patch("audit.readelf", side_effect=elf):
                with self.assertRaisesRegex(RuntimeError, "lib/cuda12/libggml-cuda.so needs GLIBC_2.34"):
                    audit(bundle, root / "logs")
