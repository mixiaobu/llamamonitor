# LlamaMonitor 1.0.0 — Build Information

> 构建信息（可复现）。工件发布在 GitHub Release `v1.0.0`。

## 构建环境

| 项 | 值 |
|---|---|
| OS | Windows 11 专业工作站版（build 10.0.26200，x64） |
| Python（构建 venv） | **3.13.14** |
| PyInstaller | **6.22.3**（onedir，`--windowed`） |
| Inno Setup | **6.7.3**（`C:\Program Files (x86)\Inno Setup 6\ISCC.exe`） |
| 构建 venv | 全新 `.venv-final`，仅从 `requirements.txt` + `requirements-dev.txt` 安装（无其他包） |

## 源代码基线

| 项 | 值 |
|---|---|
| 仓库 | github.com/mixiaobu/llamamonitor |
| 基线 commit（构建前 main 头部） | `8fdd3d9`（1.0.0: 版本号 + Final Release Baseline 文档） |
| 版本来源 | `version.py` `__version__ = "1.0.0"`（单一来源） |
| 版本一致性 | PE FileVersion / ProductVersion = `1.0.0.0`；`LlamaMonitor.exe --version` → `LlamaMonitor 1.0.0`；`/api/version`、`/api/status.version`、manifest.version 同源 |
| 数据库 schema | v4（`db.py CURRENT_SCHEMA_VERSION`；迁移链 v0→v1→v2→v3→v4，迁移前自动备份） |

## 生产依赖（requirements.txt，精确锁定）

| 包 | 版本 |
|---|---|
| fastapi | 0.141.1 |
| uvicorn | 0.53.0 |
| httpx | 0.28.1 |
| pywebview | 6.2.1 |
| pystray | 0.19.5 |
| Pillow | 12.3.0 |
| cryptography | 50.0.1（Ed25519 更新验签，不自实现密码学） |

构建依赖：pyinstaller 6.22.3（+ pyinstaller-hooks-contrib 2026.7 自动伴随）。
测试：标准库 unittest（421 例，含 1.0.0 新增 3 例远程路径泄漏回归）。

## 复现构建

```powershell
# 1. 全新 venv（仅 requirements）
python -3.13 -m venv .venv-final
.\.venv-final\Scripts\pip install -r requirements.txt -r requirements-dev.txt

# 2. 签名私钥（构建机 %LOCALAPPDATA%\LlamaMonitor\update-keys\private_key_key-2026-09.pem；
#    私钥不入库/不入 release/不入日志）
$env:LLAMAMONITOR_UPDATE_PRIVATE_KEY_FILE = "$env:LOCALAPPDATA\LlamaMonitor\update-keys\private_key_key-2026-09.pem"

# 3. 构建（全量测试 → PyInstaller onedir → 便携 zip → ISCC 安装器 →
#    manifest schema 1 + Ed25519 签名 + SHA256SUMS）
.\.venv-final\Scripts\python.exe scripts\build_release.py

# 4. 验证（SHA256 + Ed25519 验签 + manifest 字段 + zip 结构 + PE 版本 + --version CLI）
.\.venv-final\Scripts\python.exe scripts\validate_release.py
```

## 工件清单（release/，共 5 文件）

| 文件 | 说明 |
|---|---|
| `LlamaMonitor-Setup-1.0.0-win-x64.exe` | Inno Setup 安装器（per-user） |
| `LlamaMonitor-1.0.0-win-x64.zip` | 便携版（顶层 `LlamaMonitor/`，含 `LlamaMonitor.exe` + `_internal/`） |
| `release-manifest.json` | 更新 manifest（schema 1：installer/portable 的 filename+size+sha256、application_schema、signing_key_id） |
| `release-manifest.sig` | Ed25519 签名 sidecar（algorithm/key_id/signature，Base64） |
| `SHA256SUMS.txt` | 安装器 + zip 的 SHA-256 |

## 签名信任链

- 公钥：`update_keys.py` → `TRUSTED_UPDATE_KEYS["key-2026-09"]`
  （Base64(32B Ed25519 raw pubkey)，可公开，内置于所有 1.0.0 客户端）。
- 验签顺序（`update_manifest.py`）：读 sig → 公钥选择（key_id 受信表）→
  Ed25519 验 manifest **原始 bytes**（先于 JSON 解析）→ manifest 字段验证 →
  下载后 size + SHA-256 比对。
- 私钥位置（构建机，项目外）：`%LOCALAPPDATA%\LlamaMonitor\update-keys\private_key_key-2026-09.pem`
  （`.gitignore`：`update-keys/`、`*.pem`；repo 全量扫描无 `BEGIN PRIVATE KEY`）。
- 轮换流程：`docs/UPDATE_SECURITY.md`（新公钥须先内置进客户端再启用新 key 签名）。
