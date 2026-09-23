# -*- coding: utf-8 -*-
"""为 v0.16.17 补签 release-manifest：本地 release/ 已有 0.16.17 产物，
按 build_release 同一逻辑（canonical bytes + Ed25519 签名）生成
release/release-manifest-0.16.17.json 与 .sig，供上传到 0.16.17 release。"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\mixiaobu\Desktop\ai\LlamaMonitor")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from update_manifest import canonical_manifest_bytes, sign_manifest  # noqa: E402
from scripts.generate_checksums import (  # noqa: E402
    build_update_manifest,
    load_env_private_key,
)
from update_keys import DEFAULT_KEY_ID  # noqa: E402

ver = "0.16.17"
installer = ROOT / "release" / f"LlamaMonitor-Setup-{ver}-win-x64.exe"
portable = ROOT / "release" / f"LlamaMonitor-{ver}-win-x64.zip"
assert installer.is_file(), installer
assert portable.is_file(), portable

# 取 0.16.18 manifest 的 application_schema 保持兼容域一致
m18 = json.loads((ROOT / "release" / "release-manifest.json").read_text(encoding="utf-8"))
app_schema = None
if "schema_compatibility" in m18:
    app_schema = m18["schema_compatibility"]["application_schema"]

manifest = build_update_manifest(
    ver,
    installer=str(installer),
    portable=str(portable),
    application_schema=app_schema,
    signing_key_id=os.environ.get("LLAMAMONITOR_UPDATE_KEY_ID") or DEFAULT_KEY_ID,
)
raw = canonical_manifest_bytes(manifest)
key, key_id = load_env_private_key()
assert key is not None, "need LLAMAMONITOR_UPDATE_PRIVATE_KEY_FILE"
sig = sign_manifest(raw, key, key_id)

out_json = ROOT / "release" / "release-manifest-0.16.17.json"
out_sig = ROOT / "release" / "release-manifest-0.16.17.sig"
out_json.write_bytes(raw)
out_sig.write_bytes(sig)
print("wrote", out_json, len(raw), "bytes")
print("wrote", out_sig, len(sig), "bytes")

# 自验证
from update_manifest import verify_manifest_signature  # noqa: E402
from update_manifest import validate_manifest_fields  # noqa: E402

ok, kid, err = verify_manifest_signature(raw, sig)
print("sig ok:", ok, kid, err)
fields, errors = validate_manifest_fields(json.loads(raw.decode("utf-8")))
print("fields ok:", not errors, errors)
