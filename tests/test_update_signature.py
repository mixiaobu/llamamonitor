"""
Phase 13 测试：Ed25519 签名验签 + manifest 字段验证（§7/§8/§22/§23/§24/§89）。

场景（全部临时密钥对，绝不用生产私钥）：
- sign -> verify 通过（mock 公钥表）；
- 篡改 manifest 1 字节 / 1 个字段 -> 验签失败；
- 错误密钥签名 -> 验签失败；
- 未知 key_id -> "unknown signing key"；
- 非法 Base64 签名 -> 失败；
- 错误 algorithm -> 失败；
- .sig 非 JSON -> 失败；
- manifest 字段验证：schema/product/platform/arch/version/filename/size/sha256。

运行：python -m unittest discover -s tests
"""

import base64
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import update_manifest as um  # noqa: E402
from generate_update_key import generate_keypair  # noqa: E402
from update_manifest import (  # noqa: E402
    canonical_manifest_bytes,
    sign_manifest,
    validate_manifest_fields,
    verify_manifest_signature,
)


def _trusted(private, public, key_id="test-key"):
    """让 verify 信任临时密钥对的 context manager。"""
    return (
        mock.patch.object(um, "_trusted_keys", return_value={key_id: "x"}),
        mock.patch.object(um, "_load_public_key", return_value=public),
    )


def _sign_and_verify(manifest: dict, private, public, key_id="test-key"):
    raw = canonical_manifest_bytes(manifest)
    sig = sign_manifest(raw, private, key_id)
    t1, t2 = _trusted(private, public, key_id)
    with t1, t2:
        return verify_manifest_signature(raw, sig)


def _base_manifest() -> dict:
    """一个字段全部合法的最小 manifest（version 0.13.1）。"""
    import hashlib

    def entry(name, size):
        return {"filename": name, "size": size,
                "sha256": hashlib.sha256(name.encode()).hexdigest()}

    return {
        "schema": 1,
        "product": "LlamaMonitor",
        "version": "0.13.1",
        "published_at": "2026-09-15T00:00:00Z",
        "platform": "windows",
        "architecture": "x64",
        "minimum_windows": "10",
        "signing_key_id": "test-key",
        "schema_compatibility": {"application_schema": 4},
        "installer": entry("LlamaMonitor-Setup-0.13.1-win-x64.exe", 1234),
        "portable": entry("LlamaMonitor-0.13.1-win-x64.zip", 5678),
    }


class SignatureVerificationTests(unittest.TestCase):
    def test_roundtrip_ok(self):
        private, public = generate_keypair()
        ok, key_id, err = _sign_and_verify(_base_manifest(), private, public)
        self.assertTrue(ok, err)
        self.assertEqual(key_id, "test-key")

    def test_tampered_manifest_byte_fails(self):
        """§89：篡改 manifest 1 字节 -> 验签失败。"""
        private, public = generate_keypair()
        manifest = _base_manifest()
        raw = canonical_manifest_bytes(manifest)
        sig = sign_manifest(raw, private, "test-key")
        tampered = bytearray(raw)
        tampered[-1] ^= 1
        t1, t2 = _trusted(private, public)
        with t1, t2:
            ok, _k, err = verify_manifest_signature(bytes(tampered), sig)
        self.assertFalse(ok)
        self.assertIn("签名校验失败", err)

    def test_tampered_field_fails(self):
        """篡改 1 个字段（version 0.13.1 -> 0.13.2）-> 验签失败。"""
        private, public = generate_keypair()
        manifest = _base_manifest()
        raw = canonical_manifest_bytes(manifest)
        sig = sign_manifest(raw, private, "test-key")
        manifest["version"] = "0.13.2"
        tampered = canonical_manifest_bytes(manifest)
        t1, t2 = _trusted(private, public)
        with t1, t2:
            ok, _k, err = verify_manifest_signature(tampered, sig)
        self.assertFalse(ok)
        self.assertIn("签名校验失败", err)

    def test_wrong_key_fails(self):
        """用**另一对**密钥签名 -> 验签失败（不是 unknown key，是签名不匹配）。"""
        private, public = generate_keypair()
        attacker_private, attacker_public = generate_keypair()
        manifest = _base_manifest()
        raw = canonical_manifest_bytes(manifest)
        sig = sign_manifest(raw, attacker_private, "test-key")
        t1, t2 = _trusted(private, public)  # 信任的是 victim 的公钥
        with t1, t2:
            ok, _k, err = verify_manifest_signature(raw, sig)
        self.assertFalse(ok)
        self.assertIn("签名校验失败", err)

    def test_unknown_key_id_rejected(self):
        """key_id 不在内置公钥表 -> 'unknown signing key'。"""
        private, public = generate_keypair()
        manifest = _base_manifest()
        raw = canonical_manifest_bytes(manifest)
        sig = sign_manifest(raw, private, "unknown-key-xyz")
        ok, key_id, err = verify_manifest_signature(raw, sig)
        self.assertFalse(ok)
        self.assertEqual(key_id, "unknown-key-xyz")
        self.assertIn("未知的签名密钥", err)

    def test_invalid_base64_signature(self):
        private, public = generate_keypair()
        manifest = _base_manifest()
        raw = canonical_manifest_bytes(manifest)
        sig = json.dumps({
            "algorithm": "Ed25519",
            "key_id": "test-key",
            "signature": "!!!not-base64!!!",
        }).encode("ascii")
        t1, t2 = _trusted(private, public)
        with t1, t2:
            ok, _k, err = verify_manifest_signature(raw, sig)
        self.assertFalse(ok)
        self.assertIn("签名校验失败", err)

    def test_wrong_algorithm_rejected(self):
        """algorithm != Ed25519 -> 拒绝（不允许 RSA 等回退）。"""
        private, public = generate_keypair()
        manifest = _base_manifest()
        raw = canonical_manifest_bytes(manifest)
        real_sig = base64.b64encode(private.sign(raw)).decode("ascii")
        sig = json.dumps({
            "algorithm": "RSA-2048",
            "key_id": "test-key",
            "signature": real_sig,
        }).encode("ascii")
        ok, _k, err = verify_manifest_signature(raw, sig)
        self.assertFalse(ok)
        self.assertIn("不支持的签名算法", err)

    def test_sig_not_json(self):
        ok, _k, err = verify_manifest_signature(b"hello", b"this is not json")
        self.assertFalse(ok)
        self.assertIn("签名文件不是有效的 JSON", err)

    def test_sig_missing_fields(self):
        ok, _k, err = verify_manifest_signature(b"hello", b'{"algorithm":"Ed25519"}')
        self.assertFalse(ok)
        self.assertIn("未知的签名密钥", err)  # key_id 缺失


class ManifestFieldValidationTests(unittest.TestCase):
    """字段验证在验签**之后**执行（verify_manifest_fields 只负责字段层）。"""

    def test_valid_manifest(self):
        selected, errors = validate_manifest_fields(_base_manifest())
        self.assertEqual(errors, [])
        self.assertEqual(selected["version"], "0.13.1")
        self.assertEqual(selected["installer"]["filename"],
                         "LlamaMonitor-Setup-0.13.1-win-x64.exe")
        self.assertEqual(selected["portable"]["size"], 5678)

    def test_bad_schema(self):
        m = _base_manifest()
        m["schema"] = 2
        _sel, errors = validate_manifest_fields(m)
        self.assertTrue(any("schema" in e for e in errors))

    def test_bad_product(self):
        m = _base_manifest()
        m["product"] = "EvilApp"
        _sel, errors = validate_manifest_fields(m)
        self.assertTrue(any("product" in e for e in errors))

    def test_bad_platform_arch(self):
        m = _base_manifest()
        m["platform"] = "linux"
        m["architecture"] = "arm64"
        _sel, errors = validate_manifest_fields(m)
        self.assertTrue(any("platform" in e for e in errors))
        self.assertTrue(any("architecture" in e for e in errors))

    def test_bad_version(self):
        m = _base_manifest()
        m["version"] = "v0.13.1"
        _sel, errors = validate_manifest_fields(m)
        self.assertTrue(any("无效的版本号" in e for e in errors))

    def test_filename_must_match_version(self):
        """§24：filename 必须与 version 一致（LlamaMonitor-Setup-{version}-win-x64.exe）。"""
        m = _base_manifest()
        m["installer"]["filename"] = "LlamaMonitor-Setup-9.9.9-win-x64.exe"
        _sel, errors = validate_manifest_fields(m)
        self.assertTrue(any("filename" in e and "须与版本匹配" in e for e in errors))

    def test_filename_path_traversal(self):
        m = _base_manifest()
        m["portable"]["filename"] = "../../evil.zip"
        _sel, errors = validate_manifest_fields(m)
        self.assertTrue(any("不是安全文件名" in e for e in errors))

    def test_bad_size(self):
        m = _base_manifest()
        m["installer"]["size"] = -5
        _sel, errors = validate_manifest_fields(m)
        self.assertTrue(any("size" in e for e in errors))

    def test_bad_sha256(self):
        m = _base_manifest()
        m["portable"]["sha256"] = "xyz"
        _sel, errors = validate_manifest_fields(m)
        self.assertTrue(any("sha256" in e for e in errors))

    def test_portable_only_manifest_allowed(self):
        """portable-only 构建可缺 installer 段（installed 模式下载时才要求）。"""
        m = _base_manifest()
        del m["installer"]
        selected, errors = validate_manifest_fields(m)
        self.assertEqual(errors, [])
        self.assertIsNone(selected.get("installer"))
        self.assertIsNotNone(selected.get("portable"))


if __name__ == "__main__":
    unittest.main()
