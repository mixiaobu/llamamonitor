"""
make_portable.py — 从 dist\\LlamaMonitor 生成 Portable ZIP（Phase 12）。

规则（docs/RELEASE.md）：
- ZIP 顶层只有一个目录 `LlamaMonitor/`（不嵌套 release\\dist\\LlamaMonitor）；
- 用户解压整个目录、双击 LlamaMonitor.exe 即可运行；
- **Portable 只指"无需安装即可运行"**——用户数据仍然在
  %LOCALAPPDATA%\\LlamaMonitor（不改 Phase 7 数据目录规则）；
- 双保险过滤已知用户数据文件名（dist 理论上不含，防手工目录混入）：
  monitor.db / monitor.db-wal / monitor.db-shm / config.json / logs / backups。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

# 绝不进 ZIP 的用户数据文件名（§18：安装包内禁止包含用户数据库等）
EXCLUDED_FILE_NAMES = {
    "monitor.db",
    "monitor.db-wal",
    "monitor.db-shm",
    "config.json",
    "monitor.log",
}
# 绝不进 ZIP 的目录名（用户数据目录，若意外出现在 dist 里）
EXCLUDED_DIR_NAMES = {"logs", "backups", "__pycache__"}


def make_portable_zip(dist_dir: str | Path, out_path: str | Path) -> Path:
    """
    把 <dist_dir>/LlamaMonitor/ 打成 <out_path>（顶层目录 LlamaMonitor/）。
    返回输出文件路径；失败抛 ValueError。
    """
    dist_dir = Path(dist_dir)
    app_dir = dist_dir / "LlamaMonitor"
    if not app_dir.is_dir():
        raise ValueError(f"dist 应用目录不存在: {app_dir}")
    exe = app_dir / "LlamaMonitor.exe"
    if not exe.is_file():
        raise ValueError(f"缺少可执行文件: {exe}（先运行 PyInstaller 构建）")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    count = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(app_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(app_dir)
            # 过滤：已知用户数据文件名（任意深度）与用户数据目录
            if path.name in EXCLUDED_FILE_NAMES:
                continue
            if any(part in EXCLUDED_DIR_NAMES for part in rel.parts[:-1]):
                continue
            arcname = Path("LlamaMonitor") / rel
            zf.write(path, arcname.as_posix())
            count += 1
    if count == 0:
        raise ValueError("ZIP 为空（dist 目录内容异常）")
    return out_path


if __name__ == "__main__":
    import sys

    root = Path(__file__).resolve().parent.parent
    make_portable_zip(root / "dist", root / "release" / f"LlamaMonitor-portable.zip")
    print("portable zip written")
    sys.exit(0)
