"""1.3.1 Release 交付验证：Ed25519 manifest 签名 + SHA256 全资产比对。"""

import hashlib
import json
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE = "https://github.com/mixiaobu/llamamonitor/releases/download/v1.3.1"
ASSETS = [
    "release-manifest.json",
    "release-manifest.sig",
    "SHA256SUMS.txt",
    "LlamaMonitor-1.3.1-win-x64.zip",
    "LlamaMonitor-Setup-1.3.1-win-x64.exe",
]


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="lm_verify_131_"))
    print(f"tmp: {tmp}")
    for name in ASSETS:
        url = f"{BASE}/{name}"
        dest = tmp / name
        print(f"download {name} ...", flush=True)
        urllib.request.urlretrieve(url, dest)
        print(f"  {dest.stat().st_size} bytes")

    manifest = json.loads((tmp / "release-manifest.json").read_text("utf-8"))
    sig = (tmp / "release-manifest.sig").read_bytes()
    print(f"\nmanifest version={manifest['version']} key={manifest['signing_key_id']}")

    # Ed25519 验签（与 update_manifest 同一实现）
    sys.path.insert(0, str(ROOT / "scripts"))
    import update_manifest

    manifest_bytes = (tmp / "release-manifest.json").read_bytes()
    try:
        update_manifest.verify_manifest_signature(manifest_bytes, sig)
        print("SIGNATURE: OK (Ed25519 验签通过)")
    except Exception as exc:  # noqa: BLE001
        print(f"SIGNATURE: FAIL ({exc})")
        raise SystemExit(1)

    # SHA256SUMS 与本地下载文件比对
    sums_text = (tmp / "SHA256SUMS.txt").read_text("utf-8")
    ok = True
    for line in sums_text.strip().splitlines():
        if not line.strip():
            continue
        digest, fname = line.split()
        local = tmp / fname
        if not local.exists():
            print(f"SHA256 MISS: {fname} 未下载")
            ok = False
            continue
        actual = hashlib.sha256(local.read_bytes()).hexdigest()
        status = "OK" if actual == digest else "MISMATCH"
        if actual != digest:
            ok = False
        print(f"SHA256 {status}: {fname}")

    # manifest 内嵌 sha256 再比一次
    for key in ("installer", "portable"):
        entry = manifest[key]
        digest, fname = entry["sha256"], entry["filename"]
        actual = hashlib.sha256((tmp / fname).read_bytes()).hexdigest()
        status = "OK" if actual == digest else "MISMATCH"
        if actual != digest:
            ok = False
        print(f"MANIFEST-SHA256 {status}: {fname} (size={entry['size']})")

    print("\nVERIFY131:", "全部通过" if ok else "FAIL")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
