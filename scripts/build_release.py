r"""
build_release.py — LlamaMonitor Release 构建（Phase 12 + Phase 13 签名）。

流程（§10/§94）：
1. 读取 version.py（唯一版本源）并校验 ^\d+\.\d+\.\d+$（非法即失败）；
2. 环境检查（Windows / Python / PyInstaller / 源文件 / Inno Setup 编译器）；
3. 运行测试（`python -m unittest discover -s tests`；--skip-tests 可跳过）；
4. 清理 build/ 与 dist/；
5. 由 version.py 动态生成 Windows 版本资源 build/version_info.txt
   （PE FileVersion = x.y.z.0；不要手工维护第二份版本）；
6. 调用 PyInstaller（onedir，--version-file 注入版本资源）；
7. 验证 dist\\LlamaMonitor\\LlamaMonitor.exe 存在；
8. smoke test：`LlamaMonitor.exe --version`（临时 LOCALAPPDATA，不碰真实数据）；
9. 生成 Portable ZIP（顶层唯一 LlamaMonitor/ 目录）；
10. 检测到 Inno Setup 6 则构建 Installer（--require-installer 强制）；
11. 生成 SHA256SUMS.txt + release-manifest.json（schema 1，canonical bytes）；
12. **Phase 13：Ed25519 签名** —— 用环境变量提供的私钥对 manifest bytes 签名，
    生成 release-manifest.sig；正式构建缺私钥 -> **失败**（更新器会拒绝无签名
    Release）；--unsigned-development-build 允许开发构建跳过（产物不可作正式更新）；
13. 输出构建结果（下一步 python scripts/validate_release.py 做最终验证）。

私钥只从环境读取（§93）：
    LLAMAMONITOR_UPDATE_PRIVATE_KEY_FILE   PEM 文件路径（推荐；GitHub Actions 用）
    LLAMAMONITOR_UPDATE_PRIVATE_KEY        Base64(PEM)
    LLAMAMONITOR_UPDATE_KEY_ID             key_id（缺省 = update_keys.DEFAULT_KEY_ID）
私钥绝不硬编码进脚本 / 产物 / 日志。

构建不会读写 %LOCALAPPDATA%\\LlamaMonitor（smoke test 用临时 LOCALAPPDATA 覆盖）。

CLI（argparse，无 click/typer）：
    python scripts/build_release.py                  # 完整构建（正式：要求私钥）
    python scripts/build_release.py --skip-tests     # 跳过测试（CI 已跑过时）
    python scripts/build_release.py --portable-only  # 只出 Portable ZIP
    python scripts/build_release.py --require-installer  # 找不到 Inno 则失败
    python scripts/build_release.py --unsigned-development-build  # 开发构建（无签名）
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
INNO_CANDIDATES = [
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
]


def log(msg: str) -> None:
    print(f"[release] {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"[release][FAIL] {msg}", flush=True)
    sys.exit(1)


def read_version() -> str:
    """从 version.py 读版本（唯一版本源；Git tag 不是版本来源）。"""
    spec = importlib.util.spec_from_file_location("llamamonitor_version", ROOT / "version.py")
    if spec is None or spec.loader is None:
        fail("version.py 不可读")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    version = getattr(mod, "__version__", "")
    if not isinstance(version, str) or not VERSION_RE.match(version):
        fail(f"version.py __version__={version!r} 不合法（必须匹配 ^\\d+\\.\\d+\\.\\d+$，如 1.0.0）")
    return version


def check_environment() -> None:
    """构建环境检查（§75）。"""
    if os.name != "nt":
        fail("Release 构建只在 Windows 上执行")
    if sys.version_info < (3, 10):
        fail(f"需要 Python 3.10+，当前 {sys.version.split()[0]}")
    if importlib.util.find_spec("PyInstaller") is None:
        fail("PyInstaller 未安装（pip install -r requirements-dev.txt）")
    for required in ("desktop.py", "static/index.html", "assets/LlamaMonitor.ico",
                     "installer/LlamaMonitor.iss", "version.py"):
        if not (ROOT / required).is_file():
            fail(f"缺少源文件: {required}")
    log(f"环境 OK: Python {sys.version.split()[0]}, PyInstaller {importlib.import_module('PyInstaller').__version__}")


def find_iscc() -> str | None:
    """Inno Setup 6 编译器发现（§76）：INNO_SETUP_COMPILER 环境变量 -> 常见路径。"""
    env = os.environ.get("INNO_SETUP_COMPILER")
    if env and Path(env).is_file():
        return env
    for cand in INNO_CANDIDATES:
        if Path(cand).is_file():
            return cand
    return None


def run_tests() -> None:
    """构建前测试（§71：任何测试失败 -> 不生成正式 Release）。"""
    log("运行测试: python -m unittest discover -s tests")
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    tail = (proc.stderr or "").strip().splitlines()[-3:]
    for line in tail:
        print(f"  {line}")
    if proc.returncode != 0:
        fail("测试未全部通过 —— 不生成正式 Release（详见上方输出）")
    log("测试全部通过")


def clean_previous() -> None:
    for name in ("build", "dist"):
        path = ROOT / name
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            log(f"已清理 {name}/")


def write_version_info(version: str) -> Path:
    """
    由 version.py 动态生成 Windows 版本资源（PyInstaller --version-file 格式）。
    PE FileVersion = x.y.z.0（§9：统一转换，不手工维护第二份版本）。
    """
    major, minor, patch = (int(x) for x in version.split("."))
    file_version = f"{version}.0"
    year = datetime.now().year
    text = f"""# 由 scripts/build_release.py 从 version.py 动态生成（{version}）——不要手工编辑
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({major}, {minor}, {patch}, 0),
    prodvers=({major}, {minor}, {patch}, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [StringTable('040904B0',
        [StringStruct('CompanyName', 'LlamaMonitor Project'),
         StringStruct('FileDescription', 'LlamaMonitor'),
         StringStruct('FileVersion', '{file_version}'),
         StringStruct('InternalName', 'LlamaMonitor'),
         StringStruct('LegalCopyright', '{year} LlamaMonitor Project'),
         StringStruct('OriginalFilename', 'LlamaMonitor.exe'),
         StringStruct('ProductName', 'LlamaMonitor'),
         StringStruct('ProductVersion', '{version}')])
      ]
    ),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    path = ROOT / "build" / "version_info.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    log(f"版本资源已生成: {path.relative_to(ROOT)} (FileVersion={file_version})")
    return path


def run_pyinstaller(version_info: Path) -> Path:
    """PyInstaller onedir 构建（与 build.bat 同一套 flags + --version-file）。"""
    log("PyInstaller 构建中（约 3~5 分钟）...")
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--name", "LlamaMonitor",
        "--windowed",
        f"--version-file={version_info}",
        "--icon", "assets/LlamaMonitor.ico",
        "--add-data", "static;static",
        "--add-data", "assets;assets",
        "--add-data", "config.example.json;.",
        "--collect-all", "webview",
        "--collect-all", "pythonnet",
        "--collect-all", "clr_loader",
        "--collect-all", "pystray",
        "--collect-submodules", "bottle",
        "--collect-submodules", "uvicorn",
        "desktop.py",
    ]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    if proc.returncode != 0:
        print((proc.stderr or proc.stdout or "")[-3000:])
        fail("PyInstaller 构建失败")
    exe = ROOT / "dist" / "LlamaMonitor" / "LlamaMonitor.exe"
    if not exe.is_file():
        fail("构建完成但缺少 dist/LlamaMonitor/LlamaMonitor.exe")
    log(f"EXE 构建完成: {exe.relative_to(ROOT)} ({exe.stat().st_size // 1024} KB)")
    return exe


def smoke_test(exe: Path, version: str) -> None:
    """
    Release EXE smoke test（§52）：
    `LlamaMonitor.exe --version` 输出 `LlamaMonitor <version>`、退出码 0、
    不启动 Collector/FastAPI/Tray/数据库（临时 LOCALAPPDATA 隔离，不碰真实数据）。
    """
    log("smoke test: LlamaMonitor.exe --version（临时 LOCALAPPDATA 隔离）")
    with tempfile.TemporaryDirectory(prefix="llamamonitor_smoke_") as td:
        env = dict(os.environ)
        env["LOCALAPPDATA"] = td
        env.pop("LLAMAMONITOR_CONFIG", None)
        proc = subprocess.run(
            [str(exe), "--version"],
            capture_output=True, text=True, timeout=120, env=env,
            cwd=str(exe.parent),
        )
    expected = f"LlamaMonitor {version}"
    got = (proc.stdout or "").strip()
    if proc.returncode != 0 or expected not in got:
        fail(f"smoke test 失败: exit={proc.returncode} stdout={got!r} stderr={(proc.stderr or '')[:300]!r}")
    # 验证隔离：临时数据目录里不应出现数据库/配置（--version 不启动任何组件）
    leaked = [p.name for p in Path(td).rglob("*") if p.name in ("monitor.db", "config.json")] if os.path.isdir(td) else []
    if leaked:
        fail(f"smoke test 泄漏用户数据文件: {leaked}")
    log("smoke test 通过")


def build_portable(version: str) -> Path:
    from make_portable import make_portable_zip
    out = ROOT / "release" / f"LlamaMonitor-{version}-win-x64.zip"
    log("生成 Portable ZIP ...")
    make_portable_zip(ROOT / "dist", out)
    log(f"Portable ZIP: {out.relative_to(ROOT)} ({out.stat().st_size // 1024} KB)")
    return out


def build_installer(version: str, iscc: str) -> Path:
    log(f"构建 Installer（ISCC: {iscc}）...")
    cmd = [iscc, f"/DAppVersion={version}", str(ROOT / "installer" / "LlamaMonitor.iss")]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    tail = (proc.stdout or "").strip().splitlines()[-5:]
    for line in tail:
        print(f"  {line}")
    out = ROOT / "release" / f"LlamaMonitor-Setup-{version}-win-x64.exe"
    if proc.returncode != 0 or not out.is_file():
        fail("Inno Setup 构建失败（详见上方输出）")
    log(f"Installer: {out.relative_to(ROOT)} ({out.stat().st_size // 1024} KB)")
    return out


def write_checksums_and_manifest(
    artifacts: list[Path],
    version: str,
    unsigned_development: bool,
) -> None:
    """
    SHA256SUMS.txt + Phase 13 签名 manifest（§94 顺序：hash -> manifest -> sign）。
    - SHA256SUMS.txt 覆盖二进制产物（installer + portable），供人类 / CI 校验；
    - release-manifest.json 为 schema 1 canonical bytes（写盘字节 == 被签名字节）；
    - release-manifest.sig 由 Ed25519 私钥签名（正式构建缺私钥 -> 失败）。
    """
    from db import CURRENT_SCHEMA_VERSION
    from generate_checksums import (
        build_update_manifest,
        load_env_private_key,
        write_sha256sums,
        write_signature,
        write_update_manifest_bytes,
    )

    items = write_sha256sums(artifacts, ROOT / "release" / "SHA256SUMS.txt")
    for item in items:
        log(f"  {item['sha256']}  {item['file']}")

    installer_path = next((a for a in artifacts if a.name.endswith("-Setup-")), None)
    portable_path = next((a for a in artifacts if a.name.endswith(".zip")), None)

    private_key, key_id = load_env_private_key()
    manifest = build_update_manifest(
        version,
        installer=installer_path,
        portable=portable_path,
        application_schema=CURRENT_SCHEMA_VERSION,
        signing_key_id=None if private_key is None else key_id,
    )
    manifest_path = ROOT / "release" / "release-manifest.json"
    manifest_bytes = write_update_manifest_bytes(manifest, manifest_path)
    log(f"release-manifest.json 已写入（schema 1, canonical bytes, {len(manifest_bytes)} B）")

    if private_key is None:
        if unsigned_development:
            log("无签名开发构建（--unsigned-development-build）：未生成 release-manifest.sig"
                "（更新器会拒绝无签名 Release；仅作开发用途）")
            return
        fail("正式 Release 构建缺少更新签名私钥（§93）。请设置 "
             "LLAMAMONITOR_UPDATE_PRIVATE_KEY_FILE（或 LLAMAMONITOR_UPDATE_PRIVATE_KEY）；"
             "开发构建可用 --unsigned-development-build。")
    sig_path = ROOT / "release" / "release-manifest.sig"
    write_signature(manifest_bytes, private_key, key_id, sig_path)
    log(f"release-manifest.sig 已生成（Ed25519, key_id={key_id}）")


def main() -> int:
    parser = argparse.ArgumentParser(description="LlamaMonitor Release 构建（Phase 12 + Phase 13 签名）")
    parser.add_argument("--skip-tests", action="store_true", help="跳过构建前测试（CI 已跑过时）")
    parser.add_argument("--portable-only", action="store_true", help="只生成 Portable ZIP（跳过 Installer）")
    parser.add_argument("--require-installer", action="store_true", help="找不到 Inno Setup 6 时构建失败")
    parser.add_argument("--unsigned-development-build", action="store_true",
                        help="开发构建：允许无签名私钥（不生成 .sig；产物不可作正式更新）")
    args = parser.parse_args()

    version = read_version()
    log(f"版本: {version}（version.py 唯一来源）")
    check_environment()
    iscc = find_iscc()
    if iscc:
        log(f"Inno Setup: {iscc}")
    else:
        log("Inno Setup 6 未找到" + ("（--require-installer：构建失败）" if args.require_installer else "（Installer 将跳过）"))
        if args.require_installer:
            fail("未找到 Inno Setup 6（设置 INNO_SETUP_COMPILER 或安装 Inno Setup 6）")

    if not args.skip_tests:
        run_tests()

    clean_previous()
    version_info = write_version_info(version)
    run_pyinstaller(version_info)
    exe = ROOT / "dist" / "LlamaMonitor" / "LlamaMonitor.exe"
    smoke_test(exe, version)

    (ROOT / "release").mkdir(exist_ok=True)
    artifacts = [build_portable(version)]
    if not args.portable_only:
        if iscc:
            artifacts.append(build_installer(version, iscc))
        else:
            log("Installer skipped: Inno Setup 6 not found.（Portable ZIP 已生成）")

    write_checksums_and_manifest(artifacts, version, unsigned_development=args.unsigned_development_build)

    log("=" * 60)
    log("Release 构建完成:")
    for a in artifacts:
        log(f"  release/{a.name}")
    log("  release/SHA256SUMS.txt")
    log("  release/release-manifest.json")
    if (ROOT / "release" / "release-manifest.sig").is_file():
        log("  release/release-manifest.sig")
    else:
        log("  （无 release-manifest.sig —— 无签名开发构建）")
    log("下一步: python scripts/validate_release.py 验证产物（含签名验证，docs/RELEASE.md）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
