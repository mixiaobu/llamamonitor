# 变更日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。
版本号由 `version.py` 的 `__version__` 单一来源给出（SemVer，无 `v` 前缀）。

> 版本序列说明：Phase 13 采用 **0.13.x** 里程碑系列（安全更新）。0.13.x 与 1.0.0
> 互相视为"不同系列"：安装器降级保护按数值比较（1.0.0 > 0.13.x），从 1.0.0 安装
> 0.13.x 会被识别为降级并拒绝（实测行为，非缺陷）。

## [0.14.0] - 2026-09-19

Pre-1.0 全项目审计修复版（Phase 14，Release Candidate——非 1.0.0）。
全部修复带 Finding ID（`AUDIT-*`）与回归测试，详见
[`docs/AUDIT_REPORT.md`](docs/AUDIT_REPORT.md)（1 HIGH + 13 MEDIUM +
16 LOW 修复；测试 370 → 396+ 例）。

### 修复（节选，完整版见 AUDIT_REPORT.md）
- **HIGH**：Settings 视图选择器笔误（`settingsView` → `settings-view`，
  设置页此前从未显示）。
- **更新安全**：GitHub 响应大小上限（release JSON 5MB / manifest 1MB /
  sig 64KB，流式读取）；安装器 Popen 前 SHA-256 复验（TOCTOU 窗口内被替换
  的文件绝不启动）；下载任务 cancel 时清理 `.part` 并恢复状态；
  自动下载任务强引用（防 GC 中途回收）。
- **API 安全**：`GET /api/config`、`GET /api/app/integration` 改回环-only
  （非回环 403）；关闭 `/docs`/`/redoc`/`/openapi.json` 暴露面；
  CSV 导出公式注入缓解；只读 file URI 对空格/中文 percent-encode。
- **数据库**：只读写失败时 health 置 unavailable（/api/health 如实反映）
  且恢复后自动回 healthy；`/api/daily`、`/api/data/quality`、CSV 导出改
  单次取数 + 按日分组（原 O(天×行) 全表扫）；monitor_events 行数硬上限
  100,000（单条范围 DELETE）；backup_history 行数上限 1,000；
  备份失败退避 3600s（原 60s 刷屏）；迁移矩阵补 v2/v3 带数据 fixture；
  pre_migration/pre_update 备份出现在备份列表（独立 kind，不参与轮转）。
- **GPU**：nvidia-smi 子进程 cancel 时 kill+wait（不再孤儿）；
  能耗积分要求两侧功率都非 None（不再把缺失当 0W）；
  写失败回滚能耗 baseline（不再双计/漏计）。
- **前端**：fetch 30s 超时（AbortController）；轮询 in-flight 去重；
  MTP 卡片单一数据源；轮询间隔下限 1s + 窗口隐藏降频；
  表格渲染 escapeHtml + 外链 `noopener` 防护。
- **生命周期**：线程 join 超时 WARNING；周期任务异常 debug 日志；
  httpx keepalive 与轮询间隔联动；托盘轮询单 client 复用；
  按日缺口窗口 DST 安全（timedelta 而非 +86400）。

### 文档
- 新增 `docs/AUDIT_REPORT.md`（审计发现/修复/接受风险/发布门）、
  `docs/API.md`（端点全清单 + 访问控制）、
  `docs/METRICS_DEFINITIONS.md`（指标精确定义）、
  `docs/STORAGE_ESTIMATE.md`（存储占用实测与增长模型）；
  README 同步（schema v4、回环-only 端点清单、文档索引）。

## [0.13.1] - 2026-09-19

安全更新（Phase 13）部署修复版：

### 修复
- **安装器静默阻塞**：`[Code]` 取参改用 `GetCmdTail` 函数——实测 Inno Setup 6.7.3
  中 `{cmdline}`/`{cmdtail}` **不是**有效的 `ExpandConstant` 常量（运行时抛
  "Unknown constant"），导致静默安装卡死/报错对话框。
- **安装模式对话框阻塞静默安装**：移除 `PrivilegesRequiredOverridesAllowed=dialog`
  ——实测 6.7.3 在 `/SILENT` 下仍弹 "Select Setup Install Mode" 模态框并无限阻塞；
  应用设计即 per-user（固定 `%LOCALAPPDATA%` 数据目录、不写 HKLM），强制 per-user。
- **`update_success` 事件丢失**：`check_pending_update` 在 desktop 线程执行，但
  `Database` 长连接由 uvicorn 线程创建（`check_same_thread=True`）→ 跨线程写事件
  抛 ProgrammingError、marker 已删而事件未落库。改为**短命新连接**写事件
  （WAL 下与主连接并发安全）。

## [0.13.0] - 2026-09-19

### 新增
- **安全更新系统（Phase 13）**：安装版从 GitHub Release 应用内更新
  - **Ed25519 签名 manifest**：`release-manifest.json`（schema 1，canonical bytes）
    + `release-manifest.sig`（JSON sidecar：algorithm/key_id/signature）；
    公钥内置于 `update_keys.py`（多 key 表支持轮换）；验签**先于** JSON 解析。
  - **更新状态机**：IDLE/CHECKING/UPDATE_AVAILABLE/UP_TO_DATE/DOWNLOADING/
    VERIFYING/READY_TO_INSTALL/INSTALLING/ERROR；单 asyncio 工作流 + Lock 串行。
  - **流式下载**：只写 `updates/{version}/*.part`（1MB 分块边下边算 SHA-256），
    2GiB 上限 + 500MB 磁盘余量预检，取消/失败自动清理，完成 `os.replace` 转正。
  - **安装交接**：pre-update backup（SQLite Backup API + quick_check + config 复制）
    → `pending_update.json` 标记 → `Popen` 安装器 `/SILENT /NORESTART
    /APPUPDATE[_BG]`（列表参数、无 shell）→ 应用优雅退出 → Inno 替换文件并自动
    启动新版（后台更新带 `--background`）→ 新版启动核对标记记 `update_success`。
  - **设置页 Updates 分区** + loopback-only API（`/api/update/status|check|
    download|install|cancel`；409 UPDATE_BUSY/NOT_DOWNLOADING）。
  - **构建链**：`build_release.py` 正式构建必须提供签名私钥（环境变量注入，
    私钥不落项目）；`validate_release.py` 先验签再校验；`tools/tamper_test.py`
    篡改回归测试。
  - 测试：`tests/test_update_version.py` / `test_update_signature.py` /
    `test_update_check_download.py` / `test_update_install_modes.py` /
    `test_update_api.py`（共 90+ 例，含 FakeGithub MockTransport 全链路）。
  - 文档：`docs/UPDATE_SECURITY.md`（信任模型/密钥管理/轮换/泄漏响应）、
    README 安全更新章节。

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
