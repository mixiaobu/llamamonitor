"""
Phase 13 测试：版本解析/比较 + 预期文件名 + basename 安全（§16/§19/§22/§24）。

运行：python -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from update_manifest import (  # noqa: E402
    compare_versions,
    expected_installer_filename,
    expected_portable_filename,
    is_safe_basename,
    parse_version,
)


class ParseVersionTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_version("1.2.3"), (1, 2, 3))
        self.assertEqual(parse_version("0.13.1"), (0, 13, 1))
        self.assertEqual(parse_version("10.0.0"), (10, 0, 0))

    def test_invalid_rejected(self):
        for bad in ("v1.2.3", "1.2", "1", "1.2.3-beta", "abc", "", "1.2.3.4",
                    "1.2.3 ", " 1.2.3", "1..3", "1.2.x", None, 123, "1.2.3\n"):
            with self.assertRaises(ValueError, msg=f"应拒绝 {bad!r}"):
                parse_version(bad)


class CompareVersionsTests(unittest.TestCase):
    def test_ordering(self):
        self.assertEqual(compare_versions("1.0.0", "1.0.0"), 0)
        self.assertEqual(compare_versions("1.0.1", "1.0.0"), 1)
        self.assertEqual(compare_versions("1.0.0", "1.0.1"), -1)
        self.assertEqual(compare_versions("1.1.0", "1.0.9"), 1)
        self.assertEqual(compare_versions("2.0.0", "1.9.9"), 1)
        self.assertEqual(compare_versions("0.13.0", "0.13.1"), -1)

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            compare_versions("1.0", "1.0.0")
        with self.assertRaises(ValueError):
            compare_versions("1.0.0", "v1.0.0")


class ExpectedFilenameTests(unittest.TestCase):
    def test_installer(self):
        self.assertEqual(
            expected_installer_filename("0.13.1"),
            "LlamaMonitor-Setup-0.13.1-win-x64.exe",
        )

    def test_portable(self):
        self.assertEqual(
            expected_portable_filename("0.13.1"),
            "LlamaMonitor-0.13.1-win-x64.zip",
        )


class SafeBasenameTests(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(is_safe_basename("LlamaMonitor-Setup-0.13.1-win-x64.exe"))
        self.assertTrue(is_safe_basename("a.b.c.zip"))

    def test_path_traversal_rejected(self):
        for bad in ("../evil.exe", "../../evil.exe", "a/evil.exe", "a\\evil.exe",
                    "a..b/evil.exe", "evil:evil.exe", "", ".", "..", None, "a\x00b"):
            self.assertFalse(is_safe_basename(bad), f"应拒绝 {bad!r}")


if __name__ == "__main__":
    unittest.main()
