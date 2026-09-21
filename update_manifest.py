r"""
update_manifest.py — Ed25519 签名 Release Manifest（Phase 13）。

本模块是"构建签名 / 构建验证 / 运行时验签"的**单一事实来源**，保证三处逻辑一致：
- build_release.py：构建 manifest + 用私钥签名，生成 release-manifest.json + .sig；
- validate_release.py：发布后离线验证（公钥验签 + 字段 + 产物 hash/size）；
- update_service.py：运行时对 GitHub Release 里的 manifest 验签 + 字段验证。

信任模型（详见 docs/UPDATE_SECURITY.md）：
    LlamaMonitor.exe 内置 Ed25519 公钥（update_keys.py）
      -> 验证 release-manifest.json 的原始 bytes（.sig sidecar 携带 key_id + Base64 签名）
        -> manifest 内含 installer/portable 的 filename + size + sha256
          -> 下载自受信任 GitHub 仓库的 Installer/ZIP 按 manifest 逐字节校验

canonicalization（§9，构建结果稳定、写盘与签名同一 bytes）：
    json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
签名内容**必须是这些原始 bytes**——不解析 JSON、不重新序列化（避免 whitespace 差异）。

私钥只存在于开发者离线环境 / GitHub Actions Secret，绝不进入源码/EXE/Repo/Release。
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone

MANIFEST_SCHEMA = 1
PRODUCT = "LlamaMonitor"
PLATFORM = "windows"
ARCHITECTURE = "x64"
ALGORITHM = "Ed25519"

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")


# ---------------------------------------------------------------------------
# 版本（严格 MAJOR.MINOR.PATCH；不引入完整 SemVer）
# ---------------------------------------------------------------------------

def parse_version(text: str) -> tuple[int, int, int]:
    """
    严格解析 "MAJOR.MINOR.PATCH"。
    拒绝：v1.2.3 / 1.2 / 1.2.3-beta / abc / 空 / 非 str —— 抛 ValueError。
    """
    if not isinstance(text, str) or not _VERSION_RE.fullmatch(text):
        raise ValueError(f"无效的版本号：{text!r}（应为 MAJOR.MINOR.PATCH）")
    return tuple(int(p) for p in text.split("."))  # type: ignore[return-value]


def compare_versions(a: str, b: str) -> int:
    """返回 -1 / 0 / 1（a 相对 b 的升/等/降）。二者必须都是合法 MAJOR.MINOR.PATCH。"""
    pa, pb = parse_version(a), parse_version(b)
    return (pa > pb) - (pa < pb)


def expected_installer_filename(version: str) -> str:
    return f"LlamaMonitor-Setup-{version}-win-x64.exe"


def expected_portable_filename(version: str) -> str:
    return f"LlamaMonitor-{version}-win-x64.zip"


def is_safe_basename(name: str) -> bool:
    """
    filename 只允许 basename：禁止 / \\ : 和任何 ".." 段（防 path traversal）。
    例如 ../../../evil.exe 必须拒绝。
    """
    if not isinstance(name, str) or not name:
        return False
    if any(ch in name for ch in ("/", "\\", ":", "\x00")):
        return False
    if name in (".", ".."):
        return False
    for part in name.split("."):
        if part == "..":
            return False
    return True


# ---------------------------------------------------------------------------
# Manifest 构建 + canonicalization
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _artifact_meta(path, sha256: str, size: int) -> dict:
    from pathlib import Path

    return {
        "filename": Path(path).name,
        "size": int(size),
        "sha256": sha256,
    }


def build_manifest(
    version: str,
    *,
    installer: dict | None,
    portable: dict | None,
    published_at: str | None = None,
    application_schema: int | None = None,
    signing_key_id: str | None = None,
) -> dict:
    """
    构建 manifest dict（§6）。
    installer / portable 传 {filename, size, sha256}（至少一个非 None）；
    manifest 只是 metadata：**不含**任何可执行命令 / 安装参数 / URL。
    """
    parse_version(version)  # 非法版本立即失败
    manifest: dict = {
        "schema": MANIFEST_SCHEMA,
        "product": PRODUCT,
        "version": version,
        "published_at": published_at or _now_iso(),
        "platform": PLATFORM,
        "architecture": ARCHITECTURE,
        "minimum_windows": "10",
    }
    if installer is not None:
        manifest["installer"] = installer
    if portable is not None:
        manifest["portable"] = portable
    if application_schema is not None:
        manifest["schema_compatibility"] = {"application_schema": int(application_schema)}
    if signing_key_id is not None:
        manifest["signing_key_id"] = signing_key_id
    return manifest


def canonical_manifest_bytes(manifest: dict) -> bytes:
    """canonical bytes（§9）：ensure_ascii=False, sort_keys=True, 紧凑分隔符, UTF-8。"""
    return json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------
# 签名 / 验签（cryptography Ed25519，不自实现密码学）
# ---------------------------------------------------------------------------

def sign_manifest(manifest_bytes: bytes, private_key, key_id: str) -> bytes:
    """
    对 canonical manifest bytes 签名，返回 .sig sidecar 的字节（JSON ASCII）。
    sidecar 固定格式：{"algorithm": "Ed25519", "key_id": ..., "signature": <Base64>}
    """
    signature = private_key.sign(manifest_bytes)
    sidecar = {
        "algorithm": ALGORITHM,
        "key_id": key_id,
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    return (json.dumps(sidecar, indent=2, ensure_ascii=False) + "\n").encode("ascii")


def verify_manifest_signature(manifest_bytes: bytes, sig_bytes: bytes) -> tuple[bool, str, str | None]:
    """
    验证 manifest 的 Ed25519 签名（§7/§89）。
    返回 (ok, key_id, error)。

    顺序：
    1. 解析 .sig JSON（损坏 -> 失败）；
    2. algorithm 必须 == "Ed25519"；
    3. key_id 必须存在于内置 TRUSTED_UPDATE_KEYS（未知 key -> 拒绝）；
    4. Base64 解码 signature（非法 -> 失败）；
    5. 用对应公钥对**原始 manifest bytes** 验签（不重新序列化 JSON）。
    """
    try:
        sidecar = json.loads(sig_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False, "", "签名文件不是有效的 JSON"
    if not isinstance(sidecar, dict):
        return False, "", "签名文件不是 JSON 对象"

    algorithm = sidecar.get("algorithm")
    if algorithm != ALGORITHM:
        return False, "", f"不支持的签名算法：{algorithm!r}（仅支持 {ALGORITHM}）"

    key_id = sidecar.get("key_id")
    if not isinstance(key_id, str) or key_id not in _trusted_keys():
        return False, str(key_id or ""), "更新签名由未知的签名密钥创建。"

    raw_sig = sidecar.get("signature")
    if not isinstance(raw_sig, str):
        return False, key_id, "签名字段缺失或不是字符串"
    try:
        signature = base64.b64decode(raw_sig, validate=True)
        public_key = _load_public_key(key_id)
        public_key.verify(signature, manifest_bytes)
    except Exception as exc:  # 无效 Base64 / 长度不符 / 签名不匹配都归为验签失败
        return False, key_id, f"签名校验失败（{type(exc).__name__}）"
    return True, key_id, None


def _trusted_keys() -> dict[str, str]:
    from update_keys import TRUSTED_UPDATE_KEYS

    return TRUSTED_UPDATE_KEYS


def _load_public_key(key_id: str):
    from update_keys import load_public_key

    return load_public_key(key_id)


# ---------------------------------------------------------------------------
# Manifest 字段验证（**验签成功之后**才做）
# ---------------------------------------------------------------------------

def validate_manifest_fields(manifest: dict) -> tuple[dict, list[str]]:
    """
    字段级验证（§22/§23/§24）。返回 (提取结果, 错误列表)；错误列表非空 = 无效。
    提取结果含 version 与 installer/portable 的 {filename, size, sha256}。
    """
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return {}, ["manifest 不是 JSON 对象"]

    if manifest.get("schema") != MANIFEST_SCHEMA:
        errors.append(f"schema 应为 {MANIFEST_SCHEMA}，实际为 {manifest.get('schema')!r}")
    if manifest.get("product") != PRODUCT:
        errors.append(f"product 应为 {PRODUCT!r}，实际为 {manifest.get('product')!r}")
    if manifest.get("platform") != PLATFORM:
        errors.append(f"platform 应为 {PLATFORM!r}，实际为 {manifest.get('platform')!r}")
    if manifest.get("architecture") != ARCHITECTURE:
        errors.append(f"architecture 应为 {ARCHITECTURE!r}，实际为 {manifest.get('architecture')!r}")

    version = manifest.get("version")
    try:
        parse_version(version)
    except ValueError as exc:
        errors.append(str(exc))
        return {}, errors  # 版本非法时 filename 校验无从谈起

    out: dict = {"version": version}
    for section in ("installer", "portable"):
        entry = manifest.get(section)
        if entry is None:
            continue  # 该 section 可选（portable-only 构建可缺 installer）
        if not isinstance(entry, dict):
            _sec_zh = "installer（安装程序）" if section == "installer" else "portable（便携版）"
            errors.append(f"{_sec_zh} 条目必须是对象")
            continue
        out[section] = _validate_artifact(section, entry, version, errors)
    return out, errors


def _validate_artifact(section: str, entry: dict, version: str, errors: list[str]) -> dict | None:
    label = "安装程序" if section == "installer" else "便携版"
    filename = entry.get("filename")
    size = entry.get("size")
    sha = entry.get("sha256")

    if not is_safe_basename(filename):
        errors.append(f"{label}.filename {filename!r} 不是安全文件名（不能含 / \\ : ..）")
    else:
        expected = (
            expected_installer_filename(version)
            if section == "installer"
            else expected_portable_filename(version)
        )
        if filename != expected:
            errors.append(f"{label}.filename {filename!r} 与预期 {expected!r} 不符（须与版本匹配）")

    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        errors.append(f"{label}.size 必须为正整数，实际为 {size!r}")
    if not isinstance(sha, str) or not _SHA_RE.match(sha):
        errors.append(f"{label}.sha256 必须为 64 位十六进制字符，实际为 {sha!r}")

    if any(not ok for ok in (
        is_safe_basename(filename),
        isinstance(size, int) and not isinstance(size, bool) and size > 0,
        isinstance(sha, str) and bool(_SHA_RE.match(sha)),
    )):
        return None
    return {"filename": filename, "size": size, "sha256": sha.lower()}
