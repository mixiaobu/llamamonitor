"""临时诊断：GitHub Release 篡改测试（Phase 13 验收 §23-§26）。

对已安装的 0.13.1 依次创建 5 个被篡改/异常的 Release，各做一次真实
check（+必要时 download），记录 state/error，然后删除该 Release。

场景：
  0.13.2  installer 内容被改 1 字节（manifest 仍签原始 hash -> 验签过、下载 hash 不符）
  0.13.3  manifest 文件被改 1 字符（.sig 对应原 bytes -> 验签失败）
  0.13.4  缺 release-manifest.sig
  0.13.5  缺 release-manifest.json
  0.13.6  manifest 声明的 installer 文件名与版本不符（签名有效）
"""
import base64
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from db import CURRENT_SCHEMA_VERSION  # noqa: E402
from generate_update_key import load_private_key_pem  # noqa: E402
from update_manifest import build_manifest, canonical_manifest_bytes, sign_manifest  # noqa: E402

TOKEN = os.environ["GH_TOKEN"]
GH = "https://api.github.com/repos/mixiaobu/llamamonitor"
UP = "https://uploads.github.com/repos/mixiaobu/llamamonitor/releases/{rid}/assets?name={name}"
API = "http://127.0.0.1:8765"
RELEASE_DIR = ROOT / "release"

PRIVATE_KEY_PEM = Path(
    os.environ["LOCALAPPDATA"]
) / "LlamaMonitor" / "update-keys" / "private_key_key-2026-09.pem"
KEY_ID = "key-2026-09"

H = {"Authorization": f"Bearer {TOKEN}", "User-Agent": "LlamaMonitor-tamper-test",
     "Accept": "application/vnd.github+json"}


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sign_for(manifest: dict) -> bytes:
    raw = canonical_manifest_bytes(manifest)
    pk = load_private_key_pem(PRIVATE_KEY_PEM.read_bytes())
    return sign_manifest(raw, pk, KEY_ID), raw


def make_manifest(version: str, *, installer_filename: str, installer_bytes: bytes,
                  portable_bytes: bytes) -> dict:
    return build_manifest(
        version,
        installer={"filename": installer_filename, "size": len(installer_bytes),
                   "sha256": sha256_bytes(installer_bytes)},
        portable={"filename": f"LlamaMonitor-{version}-win-x64.zip",
                  "size": len(portable_bytes), "sha256": sha256_bytes(portable_bytes)},
        application_schema=CURRENT_SCHEMA_VERSION,
        signing_key_id=KEY_ID,
    )


def api_post(path: str, to: int = 120):
    req = urllib.request.Request(API + path, method="POST", data=b"")
    with urllib.request.urlopen(req, timeout=to) as r:
        return json.loads(r.read())


def api_get(path: str):
    with urllib.request.urlopen(API + path, timeout=30) as r:
        return json.loads(r.read())


import time  # noqa: E402
import sqlite3  # noqa: E402


def clear_app_etag() -> None:
    """清空应用 app_state 里的 GitHub ETag，避免 If-None-Match 命中 304 旧路径。"""
    db = Path(os.environ["LOCALAPPDATA"]) / "LlamaMonitor" / "monitor.db"
    conn = sqlite3.connect(str(db), timeout=5.0)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        with conn:
            conn.execute("UPDATE app_state SET value = '' WHERE key = 'app.update.github_etag'")
    finally:
        conn.close()


def wait_for_latest(client: httpx.Client, tag: str, timeout_s: int = 120) -> bool:
    """轮询 /releases/latest（无 ETag 头）直到 GitHub 端真的指向 tag。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = client.get(GH + "/releases/latest", headers=H)
        if r.status_code == 200 and r.json().get("tag_name") == tag:
            return True
        time.sleep(5)
    return False


def do_check(expect_state: str, max_tries: int = 3) -> dict:
    """check 直到 state 符合预期（GitHub/代理对 releases/latest 有短缓存）。"""
    st = {}
    for attempt in range(1, max_tries + 1):
        clear_app_etag()
        st = api_post("/api/update/check")
        if st["state"] == expect_state:
            return st
        print("  [retry %d] state=%s error=%r" % (attempt, st["state"], st["error"]))
        time.sleep(12)
    return st


def main() -> None:
    orig_exe = (RELEASE_DIR / "LlamaMonitor-Setup-0.13.1-win-x64.exe").read_bytes()
    orig_zip = (RELEASE_DIR / "LlamaMonitor-0.13.1-win-x64.zip").read_bytes()

    # 1 字节翻转（文件中部，保持 size 不变）
    tampered_exe = bytearray(orig_exe)
    tampered_exe[len(tampered_exe) // 2] ^= 0x01
    tampered_exe = bytes(tampered_exe)

    c = httpx.Client(trust_env=True, timeout=300)

    # ---------------------------------------------------------------- 0.13.2
    print("=== 0.13.2: installer 内容改 1 字节（manifest 签原始 hash）===")
    m = make_manifest("0.13.2", installer_filename="LlamaMonitor-Setup-0.13.2-win-x64.exe",
                      installer_bytes=orig_exe, portable_bytes=orig_zip)
    sig, raw = sign_for(m)
    create_release(c, "v0.13.2", {
        "LlamaMonitor-Setup-0.13.2-win-x64.exe": tampered_exe,
        "LlamaMonitor-0.13.2-win-x64.zip": orig_zip,
        "release-manifest.json": raw,
        "release-manifest.sig": sig,
    })
    assert wait_for_latest(c, "v0.13.2"), "v0.13.2 未成为 latest"
    st = do_check("UPDATE_AVAILABLE")
    print("  check: state=%s available=%s error=%r" % (st["state"], st.get("available_version"), st["error"]))
    if st["state"] == "UPDATE_AVAILABLE":
        st2 = api_post("/api/update/download")
        print("  download: state=%s error=%r" % (st2["state"], st2["error"]))
        parts = list((Path(os.environ["LOCALAPPDATA"]) / "LlamaMonitor" / "updates").rglob("*.part"))
        print("  .part 残留: %s" % (parts or "无"))
    delete_latest(c)
    time.sleep(5)

    # ---------------------------------------------------------------- 0.13.3
    print("=== 0.13.3: manifest 文件改 1 字符（验签应失败）===")
    m = make_manifest("0.13.3", installer_filename="LlamaMonitor-Setup-0.13.3-win-x64.exe",
                      installer_bytes=orig_exe, portable_bytes=orig_zip)
    sig, raw = sign_for(m)
    text = raw.decode("utf-8")
    # 改 sha256 串中的一个 hex 字符（保持 JSON 结构）
    idx = text.find('"sha256"')
    char_idx = idx + 12  # 进入引号内第一个字符
    ch = text[char_idx]
    text = text[:char_idx] + ("a" if ch != "a" else "b") + text[char_idx + 1:]
    tampered_manifest = text.encode("utf-8")
    assert tampered_manifest != raw
    create_release(c, "v0.13.3", {
        "LlamaMonitor-Setup-0.13.3-win-x64.exe": orig_exe,
        "LlamaMonitor-0.13.3-win-x64.zip": orig_zip,
        "release-manifest.json": tampered_manifest,
        "release-manifest.sig": sig,
    })
    assert wait_for_latest(c, "v0.13.3"), "v0.13.3 未成为 latest"
    st = do_check("ERROR")
    print("  check: state=%s error=%r" % (st["state"], st["error"]))
    delete_latest(c)
    time.sleep(5)

    # ---------------------------------------------------------------- 0.13.4
    print("=== 0.13.4: 缺 release-manifest.sig ===")
    m = make_manifest("0.13.4", installer_filename="LlamaMonitor-Setup-0.13.4-win-x64.exe",
                      installer_bytes=orig_exe, portable_bytes=orig_zip)
    _, raw = sign_for(m)
    create_release(c, "v0.13.4", {
        "LlamaMonitor-Setup-0.13.4-win-x64.exe": orig_exe,
        "LlamaMonitor-0.13.4-win-x64.zip": orig_zip,
        "release-manifest.json": raw,
    })
    assert wait_for_latest(c, "v0.13.4"), "v0.13.4 未成为 latest"
    st = do_check("ERROR")
    print("  check: state=%s error=%r" % (st["state"], st["error"]))
    delete_latest(c)
    time.sleep(5)

    # ---------------------------------------------------------------- 0.13.5
    print("=== 0.13.5: 缺 release-manifest.json ===")
    m = make_manifest("0.13.5", installer_filename="LlamaMonitor-Setup-0.13.5-win-x64.exe",
                      installer_bytes=orig_exe, portable_bytes=orig_zip)
    sig, _ = sign_for(m)
    create_release(c, "v0.13.5", {
        "LlamaMonitor-Setup-0.13.5-win-x64.exe": orig_exe,
        "LlamaMonitor-0.13.5-win-x64.zip": orig_zip,
        "release-manifest.sig": sig,
    })
    assert wait_for_latest(c, "v0.13.5"), "v0.13.5 未成为 latest"
    st = do_check("ERROR")
    print("  check: state=%s error=%r" % (st["state"], st["error"]))
    delete_latest(c)
    time.sleep(5)

    # ---------------------------------------------------------------- 0.13.6
    print("=== 0.13.6: manifest 文件名与版本不符（签名有效）===")
    m = make_manifest("0.13.6", installer_filename="LlamaMonitor-Setup-0.13.5-win-x64.exe",
                      installer_bytes=orig_exe, portable_bytes=orig_zip)
    sig, raw = sign_for(m)
    create_release(c, "v0.13.6", {
        "LlamaMonitor-Setup-0.13.6-win-x64.exe": orig_exe,
        "LlamaMonitor-0.13.6-win-x64.zip": orig_zip,
        "release-manifest.json": raw,
        "release-manifest.sig": sig,
    })
    assert wait_for_latest(c, "v0.13.6"), "v0.13.6 未成为 latest"
    st = do_check("ERROR")
    print("  check: state=%s error=%r" % (st["state"], st["error"]))
    delete_latest(c)

    # ------------------------------------------------- 收尾：0.13.1 应为 latest
    print("=== 收尾：删除测试 Release 后重查（应回到 UP_TO_DATE）===")
    deadline = time.time() + 120
    st = {}
    while time.time() < deadline:
        clear_app_etag()
        st = api_post("/api/update/check")
        if st["state"] == "UP_TO_DATE":
            break
        time.sleep(12)
    print("  check: state=%s available=%s error=%r" % (st["state"], st.get("available_version"), st["error"]))
    c.close()


def sha256sums_text(assets: dict) -> bytes:
    lines = []
    for name, data in assets.items():
        if name in ("release-manifest.json", "release-manifest.sig", "SHA256SUMS.txt"):
            continue
        lines.append(f"{sha256_bytes(data)}  {name}")
    return ("\n".join(lines) + "\n").encode("ascii")


def create_release(client: httpx.Client, tag: str, assets: dict) -> int:
    # 若同名 tag 已存在（上次中断残留），先删
    pre = client.get(f"{GH}/releases/tags/{tag}", headers=H)
    if pre.status_code == 200:
        old = pre.json()
        for a in old["assets"]:
            client.delete(f"{GH}/releases/assets/{a['id']}", headers=H)
        client.delete(f"{GH}/releases/{old['id']}", headers=H)
        print(f"  cleaned up leftover {tag}")
    sums = sha256sums_text(assets)
    all_assets = dict(assets)
    all_assets["SHA256SUMS.txt"] = sums
    r = client.post(GH + "/releases", headers=H, json={
        "tag_name": tag, "target_commitish": "main", "name": tag,
        "body": "tamper test (temporary)", "draft": False, "prerelease": False,
    })
    assert r.status_code in (200, 201), (r.status_code, r.text[:200])
    rid = r.json()["id"]
    for name, data in all_assets.items():
        ra = client.post(UP.format(rid=rid, name=name),
                         headers={**H, "Content-Type": "application/octet-stream"},
                         content=data)
        assert ra.status_code in (200, 201), (name, ra.status_code, ra.text[:200])
    print("  release %s created (id=%d, %d assets)" % (tag, rid, len(all_assets)))
    return rid


def delete_latest(client: httpx.Client) -> None:
    r = client.get(GH + "/releases/latest", headers=H)
    assert r.status_code == 200, r.text[:200]
    rel = r.json()
    rd = client.delete(f"{GH}/releases/{rel['id']}", headers=H)
    print("  release %s deleted (%d)" % (rel["tag_name"], rd.status_code))


if __name__ == "__main__":
    main()
