"""临时诊断：重跑 0.13.4（缺 .sig）与 0.13.6（文件名不符）两个场景。
加长稳定时间，避免 GitHub API / 代理对 /releases/latest 的短缓存干扰。"""
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import sqlite3  # noqa: E402
from db import CURRENT_SCHEMA_VERSION  # noqa: E402
from tamper_test import (  # noqa: E402
    GH, H, RELEASE_DIR, api_post, clear_app_etag, create_release, delete_latest,
    make_manifest, sign_for, wait_for_latest,
)

TOKEN = os.environ["GH_TOKEN"]
RELEASE_DIR = ROOT / "release"


def check_until(expect: str, tries: int = 5) -> dict:
    st = {}
    for i in range(1, tries + 1):
        clear_app_etag()
        st = api_post("/api/update/check")
        print("  attempt %d: state=%s error=%r" % (i, st["state"], st["error"]))
        if st["state"] == expect:
            return st
        time.sleep(15)
    return st


def main() -> None:
    orig_exe = (RELEASE_DIR / "LlamaMonitor-Setup-0.13.1-win-x64.exe").read_bytes()
    orig_zip = (RELEASE_DIR / "LlamaMonitor-0.13.1-win-x64.zip").read_bytes()
    c = httpx.Client(trust_env=True, timeout=300)

    print("=== 0.13.4 (重跑): 缺 release-manifest.sig ===")
    m = make_manifest("0.13.4", installer_filename="LlamaMonitor-Setup-0.13.4-win-x64.exe",
                      installer_bytes=orig_exe, portable_bytes=orig_zip)
    _, raw = sign_for(m)
    create_release(c, "v0.13.4", {
        "LlamaMonitor-Setup-0.13.4-win-x64.exe": orig_exe,
        "LlamaMonitor-0.13.4-win-x64.zip": orig_zip,
        "release-manifest.json": raw,
    })
    assert wait_for_latest(c, "v0.13.4")
    time.sleep(20)  # 让代理/CDN 缓存窗口彻底过期
    st = check_until("ERROR")
    print("  RESULT: state=%s error=%r" % (st["state"], st["error"]))
    delete_latest(c)
    time.sleep(20)

    print("=== 0.13.6 (重跑): manifest 文件名与版本不符（签名有效）===")
    m = make_manifest("0.13.6", installer_filename="LlamaMonitor-Setup-0.13.5-win-x64.exe",
                      installer_bytes=orig_exe, portable_bytes=orig_zip)
    sig, raw = sign_for(m)
    create_release(c, "v0.13.6", {
        "LlamaMonitor-Setup-0.13.6-win-x64.exe": orig_exe,
        "LlamaMonitor-0.13.6-win-x64.zip": orig_zip,
        "release-manifest.json": raw,
        "release-manifest.sig": sig,
    })
    assert wait_for_latest(c, "v0.13.6")
    time.sleep(20)
    st = check_until("ERROR")
    print("  RESULT: state=%s error=%r" % (st["state"], st["error"]))
    delete_latest(c)

    print("=== 收尾重查（应 UP_TO_DATE）===")
    deadline = time.time() + 120
    st = {}
    while time.time() < deadline:
        clear_app_etag()
        st = api_post("/api/update/check")
        if st["state"] == "UP_TO_DATE":
            break
        time.sleep(12)
    print("  RESULT: state=%s error=%r" % (st["state"], st["error"]))
    c.close()


if __name__ == "__main__":
    main()
