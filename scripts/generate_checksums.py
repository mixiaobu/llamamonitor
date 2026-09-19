r"""
generate_checksums.py — SHA256SUMS.txt + 签名 Release Manifest（Phase 13）。

- SHA256SUMS.txt：`<sha256>  <文件名>`（两空格，与 sha256sum -c 兼容），
  使用 hashlib（不增加依赖）——面向人类 / CI 的校验清单；
- release-manifest.json：**Ed25519 签名更新 manifest（schema 1，Phase 13）**，
  由 update_manifest.build_manifest 构建、以 canonical bytes 写盘
  （ensure_ascii=False, sort_keys=True, separators=(",",":"), UTF-8）——
  写盘的字节就是被签名的字节（§9）；
- release-manifest.sig：`{"algorithm":"Ed25519","key_id":...,"signature":<Base64>}`
  由 build_release.py 用私钥生成（私钥只在环境变量/临时文件里，不落库）。

信任来源是签名，不是 SHA256SUMS：更新器只验证 manifest 签名 + manifest 内的
size/sha256（docs/UPDATE_SECURITY.md）。
"""

from __future__ import annotations

import hashlib
import os
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in __import__("sys").path:
    import sys
    sys.path.insert(0, str(ROOT))

from update_manifest import (  # noqa: E402
    build_manifest,
    canonical_manifest_bytes,
    sign_manifest,
)


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    """计算文件 SHA-256（hex）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def build_artifact_list(artifacts: list[str | Path]) -> list[dict]:
    """
    [{file, sha256, size_bytes}, ...]（按文件名排序）。
    缺文件抛 FileNotFoundError（构建失败要显式暴露）。
    """
    out = []
    for name in sorted(artifacts, key=str):
        p = Path(name)
        if not p.is_file():
            raise FileNotFoundError(f"产物缺失: {p}")
        out.append({
            "file": p.name,
            "sha256": sha256_file(p),
            "size_bytes": p.stat().st_size,
        })
    return out


def write_sha256sums(artifacts: list[str | Path], out_path: str | Path) -> list[dict]:
    """写 SHA256SUMS.txt（sha256sum 兼容格式），返回 artifact 列表。"""
    items = build_artifact_list(artifacts)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{item['sha256']}  {item['file']}" for item in items]
    out_path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return items


def _artifact_entry(path: str | Path) -> dict:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"产物缺失: {p}")
    return {
        "filename": p.name,
        "size": p.stat().st_size,
        "sha256": sha256_file(p),
    }


def build_update_manifest(
    version: str,
    *,
    installer: str | Path | None = None,
    portable: str | Path | None = None,
    published_at: str | None = None,
    application_schema: int | None = None,
    signing_key_id: str | None = None,
) -> dict:
    """
    构建 Phase 13 更新 manifest（schema 1）：
    installer / portable 传产物路径（计算 filename + size + sha256，至少一个非 None）。
    """
    installer_entry = _artifact_entry(installer) if installer is not None else None
    portable_entry = _artifact_entry(portable) if portable is not None else None
    return build_manifest(
        version,
        installer=installer_entry,
        portable=portable_entry,
        published_at=published_at,
        application_schema=application_schema,
        signing_key_id=signing_key_id,
    )


def write_update_manifest_bytes(manifest: dict, out_path: str | Path) -> bytes:
    """
    把 manifest 以 **canonical bytes** 写到 out_path（§9：写盘与签名同一字节序列）。
    返回写出的 bytes（调用方必须用**这些 bytes** 签名）。
    """
    raw = canonical_manifest_bytes(manifest)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(raw)
    return raw


def write_signature(manifest_bytes: bytes, private_key, key_id: str, out_path: str | Path) -> bytes:
    """对 manifest bytes 签名并写 release-manifest.sig（JSON ASCII sidecar）。"""
    sig = sign_manifest(manifest_bytes, private_key, key_id)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(sig)
    return sig


def load_env_private_key():
    """
    从环境读 Ed25519 私钥（供 build_release.py 使用）：
    - LLAMAMONITOR_UPDATE_PRIVATE_KEY_FILE：PEM 文件路径（推荐；GitHub Actions 把
      Secret 解码到 runner 临时文件后设置该变量）；
    - LLAMAMONITOR_UPDATE_PRIVATE_KEY：Base64(PEM)；
    - LLAMAMONITOR_UPDATE_KEY_ID：key_id（缺省 = update_keys.DEFAULT_KEY_ID）。
    返回 (private_key | None, key_id)。
    """
    import base64

    from generate_update_key import load_private_key_pem
    from update_keys import DEFAULT_KEY_ID

    key_id = os.environ.get("LLAMAMONITOR_UPDATE_KEY_ID") or DEFAULT_KEY_ID
    file_env = os.environ.get("LLAMAMONITOR_UPDATE_PRIVATE_KEY_FILE")
    b64_env = os.environ.get("LLAMAMONITOR_UPDATE_PRIVATE_KEY")
    if file_env:
        pem = Path(file_env).read_bytes()
    elif b64_env:
        pem = base64.b64decode(b64_env)
    else:
        return None, key_id
    return load_private_key_pem(pem), key_id
