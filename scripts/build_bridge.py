"""
build_bridge.py — 构建 HardwareSensorBridge（1.1.0 Stage C）

- 依赖：LibreHardwareMonitorLib 0.9.2（net48 构建，BSD-3-Clause；从官方 NuGet
  包下载并缓存到 native/hardware_bridge/vendor/，版本精确固定）；
- 编译器：Windows 自带 .NET Framework 4.8 的 csc.exe
  （C:\\Windows\\Microsoft.NET\\Framework64\\v4.0.30319\\csc.exe）——
  不需要 dotnet SDK / MSBuild（Clean Machine 部署时也不需要 .NET runtime，
  Win10 1903+/Win11 内置 .NET Framework 4.8）；
- 产物：native/hardware_bridge/HardwareSensorBridge.exe（单文件，
  运行时引用同目录 LibreHardwareMonitorLib.dll）。

运行（项目根目录）：
    python scripts\\build_bridge.py
"""

from __future__ import annotations

import io
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

LHM_VERSION = "0.9.2"
NUGET_URL = (
    f"https://api.nuget.org/v3-flatcontainer/librehardwaremonitorlib/"
    f"{LHM_VERSION}/librehardwaremonitorlib.{LHM_VERSION}.nupkg"
)
CSC_CANDIDATES = (
    r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
    r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe",
)


def _log(msg: str) -> None:
    print(f"[bridge] {msg}", flush=True)


def find_csc() -> Path | None:
    for c in CSC_CANDIDATES:
        if Path(c).is_file():
            return Path(c)
    return None


def download_nupkg(dest: Path) -> Path:
    """下载 LibreHardwareMonitorLib nupkg（已缓存则跳过）。"""
    if dest.is_file() and dest.stat().st_size > 100_000:
        _log(f"nupkg 已缓存: {dest}")
        return dest
    _log(f"下载 LibreHardwareMonitorLib {LHM_VERSION} ...")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        import urllib.request
        req = urllib.request.Request(NUGET_URL, headers={"User-Agent": "LlamaMonitor-build"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
        dest.write_bytes(data)
        return dest
    except Exception as exc:
        raise SystemExit(f"下载 LibreHardwareMonitorLib 失败（可手动下载 {NUGET_URL} 到 {dest}）: {exc!r}")


def extract_net48_dll(nupkg: Path, out_dir: Path) -> Path:
    """
    从 nupkg 提取 .NET Framework 构建（net48 优先；0.9.2 实际提供 net472——
    .NET Framework 4.7.2 程序集在 4.8 上完全兼容运行）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(nupkg) as z:
        names = [n for n in z.namelist() if n.lower().endswith("librehardwaremonitorlib.dll")]
        candidates = [n for n in names if n.lower().startswith("lib/net48/")]
        if not candidates:
            candidates = [n for n in names if n.lower().startswith("lib/net472/")]
        if not candidates:
            raise SystemExit(f"nupkg 中未找到 .NET Framework 构建（net48/net472）: {sorted(names)}")
        src = candidates[0]
        target = out_dir / "LibreHardwareMonitorLib.dll"
        with z.open(src) as fsrc, open(target, "wb") as fdst:
            shutil.copyfileobj(fsrc, fdst)
    _log(f"已提取 {target}")
    return target


def build() -> int:
    if platform.system() != "Windows":
        _log("非 Windows 平台：跳过 Bridge 构建（高级传感器在目标机上编译/部署）")
        return 0
    csc = find_csc()
    if csc is None:
        _log("未找到 .NET Framework 4.8 csc.exe（Windows 10 1903+ 应自带）")
        return 1
    here = Path(__file__).resolve().parent.parent
    bridge_dir = here / "native" / "hardware_bridge"
    src = bridge_dir / "HardwareSensorBridge.cs"
    if not src.is_file():
        _log(f"缺少源文件: {src}")
        return 1
    vendor = bridge_dir / "vendor"
    nupkg = vendor / f"librehardwaremonitorlib.{LHM_VERSION}.nupkg"
    download_nupkg(nupkg)
    dll = extract_net48_dll(nupkg, vendor)
    exe = bridge_dir / "HardwareSensorBridge.exe"
    if exe.is_file():
        exe.unlink()
    # 把 dll 放到 exe 同目录（运行时加载）
    (bridge_dir / "LibreHardwareMonitorLib.dll").unlink(missing_ok=True)
    shutil.copy2(dll, bridge_dir / "LibreHardwareMonitorLib.dll")
    cmd = [
        str(csc),
        "/nologo", "/optimize+", "/target:exe",
        f"/out:{exe}",
        f"/reference:{vendor / 'LibreHardwareMonitorLib.dll'}",
        str(src),
    ]
    _log("编译中 ...")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.stdout.strip():
        _log(r.stdout.strip())
    if r.stderr.strip():
        _log(r.stderr.strip())
    if r.returncode != 0:
        _log(f"csc 退出码 {r.returncode}")
        return r.returncode
    if not exe.is_file():
        _log("编译完成但未生成 exe")
        return 1
    _log(f"Bridge 构建完成: {exe} ({exe.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(build())
