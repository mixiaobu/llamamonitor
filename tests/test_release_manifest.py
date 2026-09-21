"""
Phase 12/13 测试：Release 产物 —— SHA256SUMS.txt 与 Ed25519 签名 release-manifest.json。

- sha256_file 与 hashlib 一致性；
- SHA256SUMS 格式（`<hash>  <name>` 两空格、文件名排序、可重新验证）；
- Phase 13 manifest（schema 1）：product/platform/architecture/version +
  installer/portable{filename,size,sha256}；**不含 URL / 命令 / 安装参数**（§6）；
  canonical bytes 写盘（§9）；
- 签名：sign_manifest -> .sig sidecar 格式（algorithm/key_id/signature Base64）；
  verify_manifest_signature 对**原始 bytes** 验签通过；篡改 1 字节即失败。

运行：python -m unittest discover -s tests
"""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from generate_checksums import (  # noqa: E402
    build_artifact_list,
    build_update_manifest,
    sha256_file,
    write_sha256sums,
    write_signature,
    write_update_manifest_bytes,
)
from generate_update_key import generate_keypair  # noqa: E402
from update_manifest import (  # noqa: E402
    canonical_manifest_bytes,
    verify_manifest_signature,
)


class Sha256Tests(unittest.TestCase):
    def test_matches_hashlib_reference(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.bin"
            content = b"llamamonitor" * 1000
            p.write_bytes(content)
            expected = hashlib.sha256(content).hexdigest()
            self.assertEqual(sha256_file(p), expected)

    def test_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(FileNotFoundError):
                build_artifact_list([Path(td) / "nope.bin"])


class Sha256SumsFileTests(unittest.TestCase):
    def test_format_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            a = Path(td) / "LlamaMonitor-1.0.0-win-x64.zip"
            b = Path(td) / "LlamaMonitor-Setup-1.0.0-win-x64.exe"
            a.write_bytes(b"zipdata")
            b.write_bytes(b"setupdata")
            items = write_sha256sums([b, a], Path(td) / "SHA256SUMS.txt")
            # 按文件名排序（"1.0.0..." < "Setup-..."，故 zip 在前）
            self.assertEqual([i["file"] for i in items], sorted([b.name, a.name]))
            text = (Path(td) / "SHA256SUMS.txt").read_text(encoding="ascii")
            lines = [ln for ln in text.splitlines() if ln.strip()]
            self.assertEqual(len(lines), 2)
            for ln in lines:
                digest, sep, name = ln.partition("  ")
                self.assertEqual(sep, "  ", "sha256sum 兼容格式：hash 后两个空格")
                self.assertEqual(len(digest), 64)
                # 可重新验证
                target = Path(td) / name
                self.assertEqual(sha256_file(target), digest)


class ManifestTests(unittest.TestCase):
    """Phase 13 更新 manifest（schema 1）+ Ed25519 签名。"""

    def _artifacts(self, td: str, version: str = "1.0.0") -> tuple[Path, Path]:
        installer = Path(td) / f"LlamaMonitor-Setup-{version}-win-x64.exe"
        portable = Path(td) / f"LlamaMonitor-{version}-win-x64.zip"
        installer.write_bytes(b"setupdata" * 10)
        portable.write_bytes(b"zipdata" * 10)
        return installer, portable

    def test_structure_no_urls(self):
        with tempfile.TemporaryDirectory() as td:
            installer, portable = self._artifacts(td)
            manifest = build_update_manifest(
                "1.0.0", installer=installer, portable=portable,
                application_schema=4, signing_key_id="test-key",
            )
            self.assertEqual(manifest["schema"], 1)
            self.assertEqual(manifest["product"], "LlamaMonitor")
            self.assertEqual(manifest["version"], "1.0.0")
            self.assertEqual(manifest["platform"], "windows")
            self.assertEqual(manifest["architecture"], "x64")
            self.assertEqual(manifest["signing_key_id"], "test-key")
            self.assertEqual(manifest["schema_compatibility"], {"application_schema": 4})
            inst = manifest["installer"]
            self.assertEqual(inst["filename"], "LlamaMonitor-Setup-1.0.0-win-x64.exe")
            self.assertEqual(inst["size"], (Path(td) / inst["filename"]).stat().st_size)
            self.assertEqual(inst["sha256"], sha256_file(Path(td) / inst["filename"]))
            # §6：manifest 不含 URL / 命令 / 安装参数
            text = json.dumps(manifest)
            for forbidden in ("http://", "https://", "url", "command", "install_args"):
                self.assertNotIn(forbidden, text)

    def test_canonical_bytes_written_are_signed_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            installer, portable = self._artifacts(td)
            manifest = build_update_manifest("1.0.0", installer=installer, portable=portable)
            out = Path(td) / "release-manifest.json"
            raw = write_update_manifest_bytes(manifest, out)
            # 写盘 bytes == canonical bytes == 待签名 bytes（§9）
            self.assertEqual(out.read_bytes(), raw)
            self.assertEqual(canonical_manifest_bytes(json.loads(raw.decode("utf-8"))), raw)

    def test_sign_and_verify_roundtrip(self):
        """临时密钥对 + mock 公钥表：sign -> verify 必须通过（验证 verify 路径本身）。"""
        from unittest import mock

        import update_manifest as um

        with tempfile.TemporaryDirectory() as td:
            installer, portable = self._artifacts(td)
            manifest = build_update_manifest("1.0.0", installer=installer, portable=portable,
                                             signing_key_id="test-key")
            raw = write_update_manifest_bytes(manifest, Path(td) / "release-manifest.json")
            private, public = generate_keypair()
            sig_bytes = write_signature(raw, private, "test-key", Path(td) / "release-manifest.sig")
            # sidecar 格式
            sidecar = json.loads(sig_bytes.decode("ascii"))
            self.assertEqual(set(sidecar.keys()), {"algorithm", "key_id", "signature"})
            self.assertEqual(sidecar["algorithm"], "Ed25519")
            self.assertEqual(sidecar["key_id"], "test-key")
            with mock.patch.object(um, "_trusted_keys", return_value={"test-key": "x"}), \
                 mock.patch.object(um, "_load_public_key", return_value=public):
                ok, key_id, err = verify_manifest_signature(raw, sig_bytes)
            self.assertTrue(ok, err)
            self.assertEqual(key_id, "test-key")
            # 篡改 manifest 1 字节 -> 验签失败
            ok2, _k, err2 = self._verify_with_mock(um, raw + b" ", sig_bytes, public)
            self.assertFalse(ok2)
            self.assertIn("签名校验失败", err2)

    @staticmethod
    def _verify_with_mock(um, raw: bytes, sig_bytes: bytes, public):
        from unittest import mock

        with mock.patch.object(um, "_trusted_keys", return_value={"test-key": "x"}), \
             mock.patch.object(um, "_load_public_key", return_value=public):
            return um.verify_manifest_signature(raw, sig_bytes)

    def test_unknown_key_rejected(self):
        """verify 用内置公钥表（update_keys）——临时 key 不在表中必须被拒绝。"""
        with tempfile.TemporaryDirectory() as td:
            installer, _ = self._artifacts(td)
            manifest = build_update_manifest("1.0.0", installer=installer)
            raw = canonical_manifest_bytes(manifest)
            private, _public = generate_keypair()
            sig_bytes = write_signature(raw, private, "not-trusted-key", Path(td) / "release-manifest.sig")
            ok, _key_id, err = verify_manifest_signature(raw, sig_bytes)
            self.assertFalse(ok)
            self.assertIn("未知的签名密钥", err)


if __name__ == "__main__":
    unittest.main()
