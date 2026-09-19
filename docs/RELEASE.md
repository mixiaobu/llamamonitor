# 发布流程（RELEASE）

LlamaMonitor 的发布 = **版本** + **测试** + **PyInstaller 便携版** + **Inno Setup 安装器**
+ **校验**。全程在 Windows 11 / x64 上进行，版本由 `version.py` 单一来源驱动。

> 本文是操作手册 + 检查清单。实际发布记录见 `docs/INSTALLER_TEST.md`。

## 0. 版本来源（Single Source of Truth）

- `version.py` → `__version__ = "1.0.0"`（SemVer，`^\d+\.\d+\.\d+$`，无 `v` 前缀）。
- 所有制品的版本都从它派生，唯一转换点是 Windows PE `FileVersion` = `x.y.z.0`：

  | 制品 | 版本号形式 | 来源 |
  |---|---|---|
  | `LlamaMonitor.exe --version` | `LlamaMonitor 1.0.0` | `version.py` |
  | `GET /api/version` / `/api/status` | `1.0.0` | `version.py` |
  | 设置页 About | `1.0.0` | `/api/version` |
  | PE FileVersion | `1.0.0.0` | `build_release.py` 由 `version.py` 生成 `version_info.txt` |
  | PE ProductVersion | `1.0.0` | 同上 |
  | 安装器 / Portable 文件名 | `...-1.0.0-win-x64` | `ISCC /DAppVersion` / `make_portable.py` |
  | `SHA256SUMS.txt` / `release-manifest.json` | `1.0.0` | `generate_checksums.py` |
  | 已安装程序显示版本（Add/Remove Programs） | `1.0.0` | Inno `#define AppVersion` |

- **不要**在多处硬编码版本。改动版本只改 `version.py`，然后重跑构建。

## 1. 前置检查

```powershell
python --version          # 3.13.x（构建环境）
python -m pip show pyinstaller innosetup 2>$null
Test-Path "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"   # 应为 True
```

- PyInstaller 必须可用（`requirements-dev.txt`）。
- Inno Setup 6 必须安装（`ISCC.exe`）。可用环境变量 `INNO_SETUP_COMPILER` 指向自定义路径。

## 2. 构建 + 校验（一键）

```powershell
.\build_release.bat
# 等价于：python scripts\build_release.py
```

行为（顺序执行，任一步失败即中止并返回非零）：

1. **校验环境**：Windows、Python ≥3.10、PyInstaller 可导入、必需源文件齐全、读取 `version.py`。
2. **跑完整测试**：`python -m unittest discover -s tests`（不通过则不产出任何发布物）。
3. **清理**：删除旧 `build/` 与 `dist/`。
4. **生成版本资源**：`version.py` → `build/version_info.txt`（PE FileVersion = `x.y.z.0`）。
5. **PyInstaller**：`--onedir` + `--version-file build/version_info.txt` → `dist/LlamaMonitor/`。
6. **冒烟测试**：在**临时** `LOCALAPPDATA` 下跑 `LlamaMonitor.exe --version`，
   断言打印 `LlamaMonitor <ver>`、退出码 0、且未泄漏 `monitor.db`/`config.json`
   （**绝不**触碰真实 `%LOCALAPPDATA%\LlamaMonitor`）。
7. **Portable ZIP**：`make_portable.py` → `release/LlamaMonitor-<ver>-win-x64.zip`
   （顶层唯一 `LlamaMonitor/`，过滤用户数据文件）。
8. **安装器**：`ISCC /DAppVersion=<ver> installer\LlamaMonitor.iss`
   → `release/LlamaMonitor-Setup-<ver>-win-x64.exe`。
9. **校验和 + 清单 + 签名**（Phase 13）：`SHA256SUMS.txt`（hashlib，sha256sum 兼容）+
   `release-manifest.json`（schema 1 canonical bytes，写盘字节 == 被签名字节）+
   `release-manifest.sig`（Ed25519，见 [`UPDATE_SECURITY.md`](UPDATE_SECURITY.md)）。
   正式构建**必须**提供签名私钥（`LLAMAMONITOR_UPDATE_PRIVATE_KEY_FILE` 环境变量；
   缺失则构建失败；开发构建用 `--unsigned-development-build` 跳过 .sig）。
10. **validate_release**：校验文件齐全（含 `.sig`）、**先验签名**、manifest 规范化
    格式、哈希一致、ZIP 结构、PE 元数据、`--version` CLI。

### 选项

| 参数 | 作用 |
|---|---|
| `--skip-tests` | 跳过第 2 步（**仅**在已知测试通过、需快速重打包时用） |
| `--portable-only` | 只出 Portable ZIP + 校验和/清单，不编译安装器 |
| `--require-installer` | 找不到 Inno Setup 时**报错退出**（默认是跳过并警告） |

```powershell
python scripts\build_release.py --portable-only
python scripts\build_release.py --require-installer
```

## 3. 校验（独立重跑）

```powershell
python scripts\validate_release.py            # 校验默认 release/ 目录 + version.py 的版本
python scripts\validate_release.py --release-dir release --version 1.0.0
```

检查项：**5 个发布文件存在**（含 `release-manifest.sig`）、`release-manifest.sig`
**Ed25519 验签通过**（内置公钥表）、manifest 为 canonical 格式（重新序列化 == 磁盘
字节）、`SHA256SUMS.txt` 哈希一致、manifest 版本/哈希与实际文件一致、
ZIP 可解压且顶层唯一 `LlamaMonitor/` 且含 `LlamaMonitor/LlamaMonitor.exe`
且不含用户数据文件、安装器非空、PE 字符串/数值版本正确、`--version` CLI 输出正确。

## 4. 发布检查清单（人工）

> 勾选前请实际执行/查看，不要凭记忆打勾。

### 版本一致性
- [ ] `version.py` 的 `__version__` 是目标版本。
- [ ] `LlamaMonitor.exe --version` 输出 `LlamaMonitor <ver>`。
- [ ] 运行中 `/api/version` 与 About 页显示同一 `<ver>`。
- [ ] PE 属性：FileDescription/ProductName=`LlamaMonitor`，ProductVersion=`<ver>`，
      FileVersion=`<ver>.0`，Company=`LlamaMonitor Project`（右键 EXE → 属性 → 详细信息）。
- [ ] 安装后 Add/Remove Programs 显示 `<ver>`。
- [ ] 发布文件名、`SHA256SUMS.txt`、`release-manifest.json`、本 CHANGELOG 标题的 `<ver>` 一致。
- [ ] `release-manifest.sig` 存在且 `validate_release.py` 验签通过（key_id 在内置公钥表）。

### 构建与测试
- [ ] `build_release.py` 全绿（含完整测试套件）。
- [ ] `validate_release.py` 全绿。
- [ ] 真实 `%LOCALAPPDATA%\LlamaMonitor` 在构建期间未被修改（构建只写临时目录）。

### 安装器行为（见 `docs/INSTALLER_TEST.md` 实测记录）
- [ ] 全新安装（干净/无旧数据）→ 能启动、托盘、`/api/version` 正确。
- [ ] 升级（0.12.0 → 1.0.0 或 当前→新）→ 数据/配置/备份保留、schema 迁移、历史不变。
- [ ] 运行中升级 → 先优雅 `--shutdown-existing`，失败再 Retry/Cancel。
- [ ] 阻止降级（装旧版本被拒绝）。
- [ ] 卸载默认保留数据；"Remove data" 任务只删固定目录。
- [ ] 自定义 DB 路径（`config.database.path`）卸载后仍在（从不删除）。
- [ ] autostart：仅当注册表值已存在时更新命令，不自动启用。
- [ ] 静默安装 `/VERYSILENT /NORESTART`（不自动启动）；静默卸载保留数据。
- [ ] 自定义安装目录 `/DIR=` 可正常工作。

### 文档与元数据
- [ ] `CHANGELOG.md` 已更新到本版本（含日期、变更点）。
- [ ] `THIRD_PARTY_NOTICES.txt` 与当前锁定依赖一致（VERIFY 条目已人工确认）。
- [ ] `release/` 下 5 个文件齐全（含 `release-manifest.sig`），`SHA256SUMS.txt` 可独立复算。

## 5. 不在本发布范围内的（明确不做）

- Windows 服务 / MSIX / MSI / NSIS / WiX（仅 Inno Setup 6）
- winget manifest / 代码签名自动化 / 遥测
- Authenticode 安装器代码签名（更新用 Ed25519 签名 manifest，见
  [`UPDATE_SECURITY.md`](UPDATE_SECURITY.md)；SmartScreen 提示由 Authenticode 决定）
- 安装 Python / NVIDIA 驱动 / llama.cpp（安装器只装 LlamaMonitor 本体）
- 防火墙规则 / 文件关联

> 应用内更新（Check / Download / Install）**已在 Phase 13 纳入**：安装版从
> GitHub Release 拉取签名 manifest + 安装器，验签后静默升级。流程与信任模型见
> [`UPDATE_SECURITY.md`](UPDATE_SECURITY.md) 与 README 的"安全更新"章节。

## 6. 发布产物

```
release/
├── LlamaMonitor-<ver>-win-x64.zip        # 便携版（解压即用，顶层 LlamaMonitor/）
├── LlamaMonitor-Setup-<ver>-win-x64.exe  # Inno Setup 安装器（per-user，无 UAC）
├── SHA256SUMS.txt                        # sha256sum 兼容
├── release-manifest.json                 # 名称/版本/平台/架构/制品哈希（无下载 URL，schema 1）
└── release-manifest.sig                  # Ed25519 签名（对 manifest canonical bytes 签名）
```
