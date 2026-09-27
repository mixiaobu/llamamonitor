#!/usr/bin/env python3
"""verify_release_gh.py — 从 GitHub Release 下载全部资产并在本地完整验证（1.1.2/1.1.3 复用）。

用法：
  python tools/verify_release_gh.py --token ghp_xxx --version 1.1.3

步骤：
1. 读 release 资产列表（期望 5 个：installer EXE / portable ZIP / SHA256SUMS.txt /
   release-manifest.json / release-manifest.sig）；
2. 下载到临时目录；
3. SHA256SUMS.txt 每项与下载文件实际 hash 比对；
4. 调 scripts/validate_release.py（Ed25519 验签 + manifest 字段 + PE 版本 +
   `--version` 冒烟）——复用仓库内置公钥，不碰本地安装。
退出码 0 = 全部通过。
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO = "mixiaobu/llamamonitor"
PROXY = "http://127.0.0.1:10808"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({"https": PROXY, "http": PROXY}))


def expected_assets(version: str):
    return {
        "LlamaMonitor-Setup-",  # installer .exe  (LlamaMonitor-Setup-<v>-win-x64.exe)
        f"LlamaMonitor-{version}-win-x64.zip",  # portable ZIP
        "SHA256SUMS.txt",
        "release-manifest.json",
        "release-manifest.sig",
    }


def get_json(url, token):
    req = urllib.request.Request(url, headers={"Authorization": f"token {token}", "User-Agent": "lm-verify"})
    with OPENER.open(req, timeout=30) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", required=True)
    ap.add_argument("--version", required=True)
    a = ap.parse_args()

    rel = get_json(f"https://api.github.com/repos/{REPO}/releases/tags/v{a.version}", a.token)
    assets = list(rel.get("assets", []))
    exp = expected_assets(a.version)
    names = {x["name"] for x in assets}
    missing = []
    for x in assets:
        ok = any(x["name"] == e or x["name"].startswith(e) for e in exp)
        if not ok:
            missing.append(x["name"])
    have = {e: [x for x in assets if x["name"] == e or x["name"].startswith(e)] for e in exp}
    for e, lst in have.items():
        if not lst:
            missing.append(e)
    if missing:
        print("MISSING ASSETS: " + ", ".join(missing) + f" (got: {sorted(names)})")
        sys.exit(1)
    print(f"release {rel.get('tag_name')}: {len(assets)} assets present")

    tmp = Path(tempfile.mkdtemp(prefix="lm_verify_"))
    for x in assets:
        dest = tmp / x["name"]
        print(f"  dl {x['name']} ({x['size']} B)")
        with OPENER.open(x["browser_download_url"], timeout=300) as r, open(dest, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)

    # SHA256SUMS 校验
    fails = 0
    sums = (tmp / "SHA256SUMS.txt").read_text(encoding="utf-8").strip().splitlines()
    for line in sums:
        parts = line.split()
        if len(parts) != 2:
            continue
        h, name = parts
        f = tmp / name
        if not f.exists():
            print(f"  SUMS: file missing {name}")
            fails += 1
            continue
        actual = hashlib.sha256(f.read_bytes()).hexdigest()
        status = "ok" if actual == h.lower() else "MISMATCH"
        if actual != h.lower():
            fails += 1
        print(f"  SUMS: {name} {status}")

    # validate_release.py（签名 + manifest + PE 版本 + --version 冒烟）
    repo = Path(__file__).resolve().parent.parent
    venv_py = repo / ".venv-final" / "Scripts" / "python.exe"
    py = str(venv_py) if venv_py.exists() else sys.executable
    r = subprocess.run([py, str(repo / "scripts" / "validate_release.py"), str(tmp), a.version],
                       cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.strip().splitlines()[-25:]:
        print("  | " + line)
    if r.returncode != 0:
        fails += 1
        print(f"  validate_release exit={r.returncode}")
    else:
        print("  validate_release: PASS")

    print("VERIFY-RESULT: " + ("PASS" if fails == 0 else f"FAIL ({fails})"))
    sys.exit(0 if fails == 0 else 1)


if __name__ == "__main__":
    main()
