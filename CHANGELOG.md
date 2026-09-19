# 变更日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。
版本号由 `version.py` 的 `__version__` 单一来源给出（SemVer，无 `v` 前缀）。

## [1.0.0] - 2026-07-11

首个正式版本。汇总 Phase 1–12 的全部功能。

### 新增
- **Windows 安装器 + 版本管理 + 升级 + 发布打包（Phase 12）**
  - 统一版本号：`version.py` 单一来源（`__version__ = "1.0.0"`，`^\d+\.\d+\.\d+$`）；
    所有制品（PE 元数据 / `/api/version` / About / 安装器文件名 / Portable ZIP /
    `SHA256SUMS.txt` / `release-manifest.json` / 本文件标题）使用同一版本号，
    唯一转换点为 Windows PE `FileVersion` = `x.y.z.0`（由 `scripts/build_release.py` 生成）。
  - `GET /api/version`：`{name, version, app_version, schema_version}`；
    `schema_version` 取自数据库（`PRAGMA user_version`），**不硬编码**；
    `/api/status` 增加 `version`。
  - 设置页 → **About**：Version / Platform / Data Dir / Database Schema（均来自 API），
    **Copy Version Info** 一键复制，Python Runtime（开发模式显示完整运行时信息）。
  - `LlamaMonitor.exe --version`：打印 `LlamaMonitor 1.0.0`，退出码 0；
    不启动 Collector / FastAPI / Tray / DB。
  - `LlamaMonitor.exe --shutdown-existing`：向运行中的第一实例发送 Shutdown
    Named Event（`Local\LlamaMonitor.Shutdown`），等待单实例 Mutex 释放（≤10s）；
    优雅退出成功 = 0，超时仍运行 = 1；不启动任何组件。供安装器升级/卸载前调用。
  - PE 文件元数据（`version.py` 生成，`PyInstaller --version-file`）：
    FileDescription/ProductName = `LlamaMonitor`，ProductVersion = `1.0.0`，
    FileVersion = `1.0.0.0`，Company = `LlamaMonitor Project`（无虚构公司）。
  - Inno Setup 6 安装器（`installer/LlamaMonitor.iss`）：
    - 固定 AppId `{7E811DED-4947-495D-8F9C-1725CF459D43}`（永不更改）；
    - per-user（`{localappdata}\Programs\LlamaMonitor`）、无 UAC、仅 x64、英文；
    - 开始菜单 + 可选桌面快捷方式（默认不勾选）；Finish 可选 Launch（`skipifsilent`）。
    - 运行中升级：先优雅 `--shutdown-existing`，失败再 Retry/Cancel（不 taskkill、不 HTTP exit）。
    - **阻止降级**（DisplayVersion 比较）；autostart 仅当注册表值已存在时更新（不自动启用）；
    - 卸载默认**保留** `%LOCALAPPDATA%\LlamaMonitor`；可选 "Remove data" 任务
      （仅删固定目录，**从不**读取 `config.database.path`）；自定义 DB 路径永不删除。
    - WebView2 运行时检测（检测到即提示，不阻断——已有浏览器回退）。
  - 发布脚本 `scripts/build_release.py`（argparse：`--skip-tests` / `--portable-only` /
    `--require-installer`）：先跑完整测试，再 PyInstaller `--onedir`、
    Portable ZIP（`make_portable.py`）、Inno 安装器、`SHA256SUMS.txt`
    （`generate_checksums.py`，hashlib）、`release-manifest.json`（无 download_url）、
    以及 `validate_release.py` 校验。`build_release.bat` 一键入口。
  - 发布产物（`release/`）：`LlamaMonitor-1.0.0-win-x64.zip`、
    `LlamaMonitor-Setup-1.0.0-win-x64.exe`、`SHA256SUMS.txt`、`release-manifest.json`。
  - 生产依赖锁定（`requirements.txt` 全 `==`）、`requirements-dev.txt`（PyInstaller）。
  - `THIRD_PARTY_NOTICES.txt`（由实际锁定依赖生成，含本地 `static/echarts.min.js`）、
    `docs/RELEASE.md`、`docs/INSTALLER_TEST.md`、可选 GitHub Actions
    （`.github/workflows/release.yml`：Windows runner，tag/`workflow_dispatch`，
    仅 upload-artifact，无发布页面）。
- **数据库 schema 降级保护 + pre-migration backup（Phase 12）**
  - `PRAGMA user_version > CURRENT_SCHEMA_VERSION`：进入**只读 incompatible** 模式
    （不建表、不迁移、不降级、不写入），`/api/health` 报告 `incompatible`，
    UI 提示 "This database was created by a newer version of LlamaMonitor.
    Please upgrade the application."，修改类 API 返回 409。
  - **pre-migration backup**：既有库（打开前文件非空）在迁移前用 SQLite Backup API
    生成 `backups/pre_migration_vOLD_to_vNEW_TIMESTAMP.db` 并 `quick_check` 验证；
    验证失败则**放弃迁移**（库保持旧版本原样，incompatible 保护）。
    全新空库不生成。`pre_migration_*.db` 前缀隔离，不参与自动备份轮转。
- **数据可靠性与质量（Phase 11）**：counter reset 事件审计、监控缺口
  （server 离线 / 程序重启 / 系统睡眠 / 指标无效）与 `possible_token_loss` 标记、
  Monitoring Coverage、`PRAGMA quick_check` 健康检查与 protective mode、
  数据库自动/手动备份（创建后验证 + 轮转）。
- **Windows 集成（Phase 10）**：系统托盘、单实例（Named Mutex）、开机自启
  （HKCU Run）、优雅关闭、本地管理 API（loopback 限定）。
- **核心监控（Phase 1–9）**：llama-server 旁路指标、NVIDIA GPU（nvidia-smi 只读）、
  MTP 深度统计、按天 Token 历史、本地 SQLite（WAL + 版本化迁移）、
  本地 Dashboard（原生 HTML/CSS/JS + ECharts，无框架/无 CDN）、CSV 导出、
  九分区设置页、深色/浅色/跟随系统主题。

### 变更
- 生产依赖全部固定到确切版本（`requirements.txt`）。
- PyInstaller 保持 `--onedir`（`--version-file` 注入版本元数据）。

### 说明
- 许可证：本项目**尚无** LICENSE 文件（许可证选型待定）；第三方许可证见
  `THIRD_PARTY_NOTICES.txt`（标注 VERIFY 的条目发布前需人工确认）。
- 不自动更新、不在线查版本、不 Windows 服务、不 MSIX/MSI/NSIS/WiX（仅 Inno Setup 6）、
  不 winget manifest、不代码签名自动化、不遥测。
