r"""
version.py — LlamaMonitor 唯一版本源（Phase 12）

整个项目只在这里维护版本号：
- Python 代码（/api/version、/api/status、About）：from version import __version__
- 构建脚本（PyInstaller version resource、Inno Setup、release 文件名、
  SHA256SUMS、release-manifest）：读取本文件的 __version__
- Git tag 用 `v1.0.0`（前缀 v），内部 version 值不带前缀：`1.0.0`

格式：MAJOR.MINOR.PATCH（SemVer 数字，如 1.0.0 / 1.0.1 / 1.1.0 / 2.0.0）。
构建前由 scripts/build_release.py 校验 ^\d+\.\d+\.\d+$，不合规则构建失败。

Windows PE FileVersion 由构建脚本统一转换为 x.y.z.0（如 1.0.0 -> 1.0.0.0），
不手工维护第二份版本。
"""

__version__ = "0.16.4"
APP_NAME = "LlamaMonitor"
