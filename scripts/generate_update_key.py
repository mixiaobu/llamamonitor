r"""
generate_update_key.py — LlamaMonitor 更新签名密钥生成（Phase 13，首次使用 / 密钥轮换）

- 生成 Ed25519 密钥对（cryptography.hazmat.primitives.asymmetric.ed25519，
  **不自实现任何密码学算法**）；
- 私钥默认保存在**项目目录之外**：
      %LOCALAPPDATA%\LlamaMonitor\update-keys\private_key_<key-id>.pem
  输出时明确警告：NEVER COMMIT THE PRIVATE KEY.
- 公钥 Base64 + 需粘贴进 update_keys.py 的代码片段（公钥可以公开）；
- 私钥**不**写入：源码 / EXE / GitHub Repo / release ZIP / Installer / config.json，
  只允许存在于开发者离线环境或 GitHub Actions Secret（docs/UPDATE_SECURITY.md）。

密钥轮换（私钥泄漏 / 定期换代，见 docs/UPDATE_SECURITY.md）：
1. 重新运行本脚本生成新 key_id + 新密钥对；
2. 把新公钥加入 update_keys.py（TRUSTED_UPDATE_KEYS 支持多把受信公钥）；
3. 发布**包含新公钥的客户端**（旧客户端无法信任它不认识的新 key）；
4. 后续 Release 用新私钥签名（LLAMAMONITOR_UPDATE_KEY_ID=新 key_id）。

用法：
    python scripts/generate_update_key.py                        # 默认 key_id = key-<年>-<月>
    python scripts/generate_update_key.py --key-id key-2027-01
    python scripts/generate_update_key.py --output D:\keys       # 指定私钥目录（默认项目外）
"""

from __future__ import annotations

import argparse
import base64
import sys
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

# 私钥默认位置：项目目录之外（%LOCALAPPDATA%\LlamaMonitor\update-keys\）
DEFAULT_KEY_DIR_ENV = "LOCALAPPDATA"


def default_key_dir() -> Path:
    import os

    localappdata = os.environ.get(DEFAULT_KEY_DIR_ENV)
    base = Path(localappdata) / "LlamaMonitor" / "update-keys" if localappdata else Path.home() / "LlamaMonitor" / "update-keys"
    return base


def default_key_id() -> str:
    return "key-" + datetime.now().strftime("%Y-%m")


def generate_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    """生成 Ed25519 密钥对（私钥、公钥）。"""
    private = Ed25519PrivateKey.generate()
    return private, private.public_key()


def public_key_b64(public: Ed25519PublicKey) -> str:
    """公钥 -> Base64（32 字节原始公钥的 Base64；update_keys.py 存储格式）。"""
    raw = public.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def private_key_pem(private: Ed25519PrivateKey) -> bytes:
    """私钥 -> PKCS8 PEM（无加密；密钥文件权限由存放位置保证：项目外）。"""
    return private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def load_private_key_pem(pem: bytes | str):
    """PEM -> Ed25519PrivateKey（构建脚本 / 测试共用）。"""
    if isinstance(pem, str):
        pem = pem.encode("utf-8")
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError("PEM 不是 Ed25519 私钥")
    return key


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Generate an Ed25519 update signing key pair.")
    parser.add_argument("--key-id", default=default_key_id(),
                        help="签名 key_id（写入 .sig 的 key_id；update_keys.py 用它选公钥）。默认 key-<年>-<月>。")
    parser.add_argument("--output", default=None,
                        help="私钥目录（默认项目外 %LOCALAPPDATA%\\LlamaMonitor\\update-keys\\）")
    parser.add_argument("--quiet", action="store_true", help="只输出公钥 Base64（脚本化）")
    args = parser.parse_args(argv)

    key_dir = Path(args.output).expanduser() if args.output else default_key_dir()
    key_dir.mkdir(parents=True, exist_ok=True)
    private_path = key_dir / f"private_key_{args.key_id}.pem"
    if private_path.exists():
        print(f"[FAIL] 私钥文件已存在（不覆盖）: {private_path}", file=sys.stderr)
        print("       如确需重新生成，先删除旧文件或换 --key-id。", file=sys.stderr)
        return 1

    private, public = generate_keypair()
    private_path.write_bytes(private_key_pem(private))
    b64 = public_key_b64(public)

    if args.quiet:
        print(b64)
        return 0

    print("=" * 64)
    print("Ed25519 更新签名密钥已生成（Phase 13）")
    print("=" * 64)
    print(f"key_id    : {args.key_id}")
    print(f"私钥      : {private_path}")
    print()
    print("** NEVER COMMIT THE PRIVATE KEY. **")
    print("私钥只允许存在于：开发者离线环境 / GitHub Actions Secret。")
    print("不要把私钥放进：源码 / EXE / GitHub Repo / release ZIP / Installer / config.json")
    print()
    print("公钥（Base64）：")
    print(f"{b64}")
    print()
    print("请把下面一行加入 update_keys.py 的 TRUSTED_UPDATE_KEYS：")
    print(f'    "{args.key_id}": "{b64}",')
    print()
    print("本地正式发布构建：")
    print(f'    set LLAMAMONITOR_UPDATE_PRIVATE_KEY_FILE={private_path}')
    print(f"    set LLAMAMONITOR_UPDATE_KEY_ID={args.key_id}")
    print("    python scripts\\build_release.py")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
