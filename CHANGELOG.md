# 变更日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。
版本号由 `version.py` 的 `__version__` 单一来源给出（SemVer，无 `v` 前缀）。

> 版本序列说明：Phase 13 采用 **0.13.x** 里程碑系列（安全更新）。0.13.x 与 1.0.0
> 互相视为"不同系列"：安装器降级保护按数值比较（1.0.0 > 0.13.x），从 1.0.0 安装
> 0.13.x 会被识别为降级并拒绝（实测行为，非缺陷）。

## [1.0.1] - 2026-09-25

**术语审计与 UI 文案修订版（UI/Text Freeze）**。不改布局、功能、数据库统计逻辑
与 Metrics 采集逻辑，仅术语校准 + llama.cpp metric 语义校准 + 中英文统一 +
tooltip 统一 + 去机器翻译感 + 去开发者内部术语。唯一术语字典：
[`docs/UI_TERMINOLOGY.md`](docs/UI_TERMINOLOGY.md)；回归防护：
`tests/test_ui_terminology.py`（4 例）。

### 术语（详见 UI_TERMINOLOGY.md 完整映射表）
- 「逻辑 Token」→ **Token 总量**、「计算 Token」→ **实际计算 Token**（均标注
  派生指标：输入 + 缓存复用 + 输出 / 输入 + 输出）；「提示」→ **输入 Token**、
  「缓存」→ **缓存复用 Token**、「缓存率」→ **缓存复用率**（= 缓存复用 /（输入 + 缓存复用））。
- MTP 语义校准：「草稿序列」（spec_decode_num_drafts_total 实为验证步骤数）→
  **推测验证轮次**；「接受率」→ **Draft Token 接受率**；「投机解码」→ **推测解码**
  （tooltip Speculative Decoding）；MTP 标题 → **MTP（Multi-Token 预测）**。
- 语义错误修复：「最大 Token 记录」→ **上下文高水位**
  （llamacpp:n_tokens_max = 历史最大序列长度，非上下文上限）；「上下文上限」→
  **上下文窗口上限**（llamacpp:context_max，按真实后端字段核实）；
  「Busy Slots / 忙碌解码槽」→ **平均忙碌 Slot 数**
  （llamacpp:n_busy_slots_per_decode = 每次 llama_decode() 平均，非瞬时状态）；
  「KV 缓存使用率」保留（仅当后端提供 ratio，否则 --）。
- 「排队（延迟）」→ **等待中请求**（requests_deferred 是等待，不是延迟）；
  「处理中」→ **处理中请求**；「服务器运行时」→ **服务器运行状态**；
  「Token 吞吐」→ **Token 吞吐率**；TPS 单位统一 `tok/s`。
- Prompt TPS / Decode TPS 全项目统一英文（卡片、图例、指标条一致）。
- GPU：页标题「GPU」→ **GPU 监控**；「利用率 & 显存」→ **GPU 利用率与显存占用**；
  「硬件趋势」→ **功耗与温度**；指标统一 显存占用/风扇转速/SM 时钟/PCIe 链路；
  能耗说明「根据采样功耗随时间积分估算，仅供参考。」（删 /api/gpu/daily 路径）。
- 历史：「历史」→ **监控历史**；「覆盖率」→ **采集覆盖率**（时间完整性，非
  Token 精度）；缺口表列 开始时间/结束时间/持续时间/来源/原因/**Token 可能缺失**
  （是/否，不带问号）；缺口原因中文化（llama-server 不可达 / LlamaMonitor 重启 /
  系统休眠 / 采集异常）；来源 llama.cpp / LlamaMonitor / GPU 采集。
- 事件：llama-server **已连接 / 连接中断**（不用上线/离线）；LlamaMonitor
  **启动 / 停止 / 重启**；「备份完成」→ **数据库备份完成**；补 **数据库迁移**
  （detail "Schema 2 → 3"，不再回退显示英文 event name）；counter_reset 细节
  映射中文计数名（不显内部字段）。
- 设置：服务器 desc 重写（仅 HTTP GET 读取指标，不代理/不修改推理请求）；
  「Metrics 路径」→ **指标端点路径**；「采集器」→ **指标采集**；
  「轮询间隔」→ **指标采集间隔 / GPU 采集间隔**；「实时数据保留」→
  **实时采样保留时长**；「面板刷新间隔」→ **界面刷新间隔**；「默认历史范围」→
  **默认统计范围**；GPU desc「通过 NVIDIA nvidia-smi 读取 GPU 状态…不修改
  GPU 配置」；「检测到的 GPU（不勾选=…）」→ **监控的 GPU（未选择时监控所有
  已检测到的 GPU）**；「最大日志大小」→ **单个日志文件上限**；日志「备份数量」
  → **轮转文件保留数**；WAL 说明带 Write-Ahead Logging；数据按钮 刷新状态 /
  立即备份 / 检查数据库完整性；「清空实时历史」→ **清除实时采样历史**、
  「重置所有统计」→ **重置统计数据**（tooltip/确认框说明计数器基线保留）；
  「随 Windows 启动」→ **登录时自动启动**；「API 主机/端口」→ **监听地址/端口**
  （0.0.0.0 显示轻量 Warning：只读接口可能可被局域网访问，管理操作仅本机）；
  运行信息 运行模式/单实例运行/运行环境/应用数据目录；更新 desc 重写
  （GitHub Releases + Ed25519 签名 + SHA-256 完整性校验）；「安装模式」→
  **安装类型**、「最新 Release」→ **最新版本**、「状态」→ **更新状态**；
  更新状态文案全部中文化（未检查/已是最新版本/发现新版本/正在下载/正在验证/
  已准备安装/正在安装/检查失败，禁 IDLE/READY_TO_INSTALL/ERROR 裸显）。
- 关于：副标题「llama.cpp 本地只读监控工具」；「数据库 Schema」→
  **数据库 Schema 版本**；「数据目录」（值为 monitor.db 路径）→ **数据库文件**；
  说明去 /metrics 路径（"仅通过 HTTP GET 读取 llama-server 指标数据…"）。
- 拼写规范：llama.cpp / llama-server / LlamaMonitor / Draft Token（禁 LLama /
  LLM server / llamacpp 裸词；raw metric 名仅允许出现在 tooltip「来源：」行）。

### 测试
- 新增 `tests/test_ui_terminology.py`（4 例：废弃术语消失 / 新术语存在 /
  日志备份数改名 / tok/s 统一）；`test_api_dashboard.py` 页面标记断言同步
  到新术语。全量 `python -m unittest discover -s tests`：**425 例全绿**。

## [1.0.0] - 2026-09-24

**正式首发版本（Final Release）**。在 0.16.x 稳定线基础上完成 75 项 Release Gate
（全量测试 ×10、加速可靠性 7/30/90/365 天仿真 + 10 万+ 采集周期、数据库完整性
矩阵、主题/DPI/分辨率 UI 门、真实 token 精确性、monitor 重启、Windows 4h 燃烧测试、
Ed25519 信任链 + 篡改矩阵、远程只读面安全），Feature / UI / Schema / API 冻结。
完整验收记录：[`docs/FINAL_RELEASE_REPORT_1.0.0.md`](docs/FINAL_RELEASE_REPORT_1.0.0.md)。

### 修复（发布阶段）
- **HIGH（安全）REL-1.0.0-001**：`web.host=0.0.0.0` 时局域网只读客户端可经
  `/api/status`（`config.path`）与 `/api/data/info`（`database_path`、
  `last_auto_backup.path`）读到完整 Windows 路径，泄漏用户名 + 数据目录。
  现只读端点对远程客户端只返回文件名；本机保持完整路径（`server._expose_path`）。
  回归测试：`tests/test_data_management.py::RemotePathLeakTests`（3 例）。
- 默认主题 dark → **system**（跟随系统外观，Win11 Fluent 语义）；
  用量页时间筛选默认 30 天 → **7 天**（前端内置默认与服务端一致）。
- 远程（非 loopback）隐藏设置导航；远程设置页只读；远程不再请求
  local-only 端点（消除 403 噪音）。

### 发布工件（GitHub Release v1.0.0）
- `LlamaMonitor-Setup-1.0.0-win-x64.exe`（Inno Setup 6.7.3，per-user 安装到
  `%LOCALAPPDATA%\Programs\LlamaMonitor`）
- `LlamaMonitor-1.0.0-win-x64.zip`（便携版，顶层 `LlamaMonitor/` 目录）
- `release-manifest.json` + `release-manifest.sig`（Ed25519，key-2026-09）
- `SHA256SUMS.txt`；验签链说明见
  [`docs/UPDATE_SECURITY.md`](docs/UPDATE_SECURITY.md)

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
