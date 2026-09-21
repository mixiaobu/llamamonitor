r"""
update_keys.py — LlamaMonitor 更新签名**公钥**（Phase 13）

本文件只包含**公钥**（公钥可以公开；私钥绝不进入源码 / EXE / Repo / Release）。
支持多把受信公钥（key 轮换，见 docs/UPDATE_SECURITY.md）：

- 签名 sidecar（release-manifest.sig）内含 key_id，验签时用它选择对应公钥；
- 即使攻击者伪造 key_id，没有对应私钥也伪造不出合法签名；
- 旧客户端只能信任它内置的公钥 —— 轮换时新版客户端必须**提前**内置下一把公钥，
  然后才能信任新 key 签名的 Release。

轮换 / 私钥泄漏响应（详见 docs/UPDATE_SECURITY.md）：
1. scripts/generate_update_key.py 生成新 key_id + 新密钥对；
2. 新公钥加入下方 TRUSTED_UPDATE_KEYS；
3. 发布包含新公钥的客户端；
4. 后续 Release 用新私钥签名（构建时设 LLAMAMONITOR_UPDATE_KEY_ID）。
"""

from __future__ import annotations

import base64

# 当前构建使用的默认 key_id（构建脚本 LLAMAMONITOR_UPDATE_KEY_ID 缺省值）
DEFAULT_KEY_ID = "key-2026-09"

# key_id -> Base64(32 字节 Ed25519 原始公钥)
# key-2026-09 第二次生成：RC item 67（/TASKS=removedata）测试删除了
# %LOCALAPPDATA%\LlamaMonitor\update-keys（私钥随数据目录一起删），按
# UPDATE_SECURITY.md 轮换流程重生成同 key_id 的密钥对（2026-09-20）。
# 0.16.0/0.16.1 内置的旧公钥已无法验新签名 -> 更新链在 0.16.1 上手动
# 升级一次到 0.16.2 即可恢复自助更新（见 docs/RC_TEST_REPORT.md item 67）。
TRUSTED_UPDATE_KEYS: dict[str, str] = {
    "key-2026-09": "eLHVkIl+gQgI0QQIKUuwnQl5NHR5m8I16XWRrdfLOqU=",
}


def load_public_key(key_id: str):
    """
    key_id -> Ed25519PublicKey。
    未知 key_id 抛 KeyError（调用方转换为
    "Update signature was created with an unknown signing key."）。
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        b64 = TRUSTED_UPDATE_KEYS[key_id]
    except KeyError:
        raise KeyError(key_id) from None
    return Ed25519PublicKey.from_public_bytes(base64.b64decode(b64, validate=True))
