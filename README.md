# LlamaMonitor

llama.cpp 的纯旁路（sidecar）监控程序，Windows 11 桌面应用。

只读取 `http://127.0.0.1:9091/metrics`：不代理、不修改、不启停 llama-server，
不占用 9091 端口。监控程序崩溃不影响 llama-server。

## 功能

- 实时状态：处理中/延迟请求数、Busy Slots、KV Cache 使用率、Token Max（llama-server
  运行时指标，服务器提供时显示）、Prompt/Decode TPS、MTP 接受率（含 per-position）、上下文
- GPU 监控（NVIDIA）：负载/显存/温度/功耗/风扇/频率/PCIe，历史曲线（15m~24h）、
  今日能耗估算、按天 GPU 统计（永久保留）；数据来自系统 NVIDIA 驱动的 nvidia-smi（只读）
- MTP 深度统计：Draft Tokens / Accepted Tokens / Draft Sequences（今日）、
  按草稿位置的接受 Token 数（Accepted Tokens by Draft Position，动态发现位置）
- 历史统计：按天 Token 用量（Prompt/Cached/Output）、累计、Cache Ratio、每日 MTP 趋势
- 数据：本地 SQLite（按天累计 + 48h 实时采样 + 计数器 state + GPU 采样/按天），
  Counter reset 安全；schema 版本化迁移（PRAGMA user_version，旧版本数据无损升级）
- 可靠性与数据质量（Phase 11）：counter reset 事件审计、监控缺口（server 离线 /
  程序重启 / 系统睡眠 / 指标无效）记录与 possible_token_loss 标记、Monitoring Coverage、
  PRAGMA quick_check 健康检查与 protective mode、数据库自动/手动备份（创建后验证 + 轮转）；
  详细规则见下"数据可靠性与质量"与 `REAL_SOAK_TEST.md`
- 界面：本地 Dashboard（原生 HTML/CSS/JS + ECharts，无框架、无 CDN），深色/浅色/跟随系统主题；
  页面隐藏时自动降低轮询频率
- Windows 集成：系统托盘（关闭窗口=隐藏到托盘，监控继续）、单实例（Named Mutex +
  第二实例唤醒第一实例）、开机自启（HKCU Run，当前用户登录时进托盘）、优雅关闭
- 设置页：Server / Collector / GPU / Interface / Web / Storage / Logging / Data /
  Application / Updates 十个分区 + About，Test Connection、保存（校验 + 原子写入）、
  Reset to Defaults；Updates 分区提供应用内安全更新（Check / Download / Install /
  Cancel，见"安全更新"章节）
- 数据管理：CSV 导出（Excel 直接打开，含 GPU 每日 CSV）、SQLite Backup API 备份
  （手动 + 自动 + 验证 + 轮转）、数据库健康检查与 protective mode、清实时历史、重置统计
- 版本管理（Phase 12）：`version.py` 单一版本号来源、`GET /api/version`、设置页 About
  （Version/Platform/Data Dir/Schema + Copy Version Info）、PE 文件版本元数据、
  schema 降级保护（更新版本的库 → 只读 incompatible）、迁移前自动备份
  （`pre_migration_*.db`）；Inno Setup 6 安装器 + 一键发布脚本（见下"发布 / 安装器"）

## 开发模式运行

```powershell
pip install -r requirements.txt
python desktop.py            # 桌面版（pywebview + 内嵌 FastAPI）
python server.py             # 浏览器版：打开 http://127.0.0.1:8765/
python collector.py          # 纯 CLI 采集（JSON 输出 + 落库）
python -m unittest discover -s tests   # 单元测试
```

## 打包运行

双击 `build.bat` → 生成 `dist\LlamaMonitor\LlamaMonitor.exe`（PyInstaller --onedir）。

把整个 `dist\LlamaMonitor` 目录拷到另一台 Windows 11 机器即可运行
（无需安装 Python；WebView2 运行时 Win11 自带）。

### 发布 / 安装器（Phase 12）

一键构建 + 校验（先跑完整测试，再出便携 ZIP + Inno Setup 安装器 + 校验和/清单）：

```powershell
.\build_release.bat                 # 等价于 python scripts\build_release.py
python scripts\validate_release.py  # 独立校验 release/ 产物
```

产物在 `release/`：`LlamaMonitor-<ver>-win-x64.zip`（便携）、
`LlamaMonitor-Setup-<ver>-win-x64.exe`（Inno Setup 6 安装器，per-user、无 UAC、仅 x64）、
`SHA256SUMS.txt`、`release-manifest.json`。版本号统一来自 `version.py`
（PE FileVersion 为 `x.y.z.0`，是唯一转换点）。

安装器要点：固定数据目录 `%LOCALAPPDATA%\LlamaMonitor`（与安装位置无关）；
升级时先优雅 `--shutdown-existing`、阻止降级、autostart 仅已启用时更新、
卸载默认保留数据（可选 "Remove data" 只删固定目录，自定义 DB 路径从不删除）。
完整流程与检查清单见 [`docs/RELEASE.md`](docs/RELEASE.md)，
安装器手动测试记录见 [`docs/INSTALLER_TEST.md`](docs/INSTALLER_TEST.md)。
第三方许可证见 [`THIRD_PARTY_NOTICES.txt`](THIRD_PARTY_NOTICES.txt)。

CLI：`LlamaMonitor.exe --version`（打印版本并退出）、
`LlamaMonitor.exe --shutdown-existing`（请求运行中实例优雅退出，供安装器调用）。

## 安全更新（Secure Updates，Phase 13）

**安装版**（installer）支持检查、验签、下载并静默执行官方安装器完成升级，
完成后新版本自动启动。**便携版**：可检查/下载，但只提示"Open Download Folder"
手动更新（**从不自我覆盖**）。**开发模式**：Check 可用，下载/安装禁用。

更新流程：

1. **Check**（Settings → Updates 或托盘菜单"Check for Updates"）：查询 GitHub
   （`mixiaobu/llamamonitor`）最新 **stable** Release（draft/prerelease 一律忽略），
   下载 `release-manifest.json` + `release-manifest.sig`，**先用内置 Ed25519 公钥验签，
   再解析 manifest**——签名有效才信任版本/文件名/大小/SHA-256 等字段；
   remote ≤ 当前版本视为 "Up to date"（**绝不自动降级**）。
2. **Download**：流式写入 `%LOCALAPPDATA%\LlamaMonitor\updates\<version>\<file>.part`
   （1MB 分块；下载前检查磁盘空间（预留 500MB）与 2 GiB 上限）；
   完成后 **size + SHA-256 双校验**，双过才原子转正（`.part` 失败即删）；
   可取消（cancel event，不强杀线程）。
3. **Install**：先 Pre-Update Backup（数据库用 SQLite Online Backup API + quick_check，
   config.json 整体安全复制，存 `%LOCALAPPDATA%\LlamaMonitor\backups\pre_update_*`）；
   **DB 本身 corrupt 时拒绝安装**并提示；备份失败不启动安装器。随后写
   `updates\pending_update.json`，启动**已验证的固定路径**安装器
   （`/SILENT /NORESTART /APPUPDATE[_BG]`，列表参数、无 shell），应用优雅退出
   （Mutex 释放 → 安装器替换文件）。
4. **新版启动**：Inno 的 `/APPUPDATE` [Run] 条目自动启动新版本（后台模式带
   `--background`）；新版本启动时读 marker：`to_version` == 当前版本 → 记
   `update_success` 事件并删 marker；不匹配 → `update_failed_mismatch` 警告。
   **没有自动回滚**——出问题用 Pre-Update Backup 手动恢复。

安全模型（详见 [`docs/UPDATE_SECURITY.md`](docs/UPDATE_SECURITY.md)）：

- 信任链：内置 Ed25519 公钥（`update_keys.py`，`key-2026-09`）验 manifest 签名
  → manifest 声明安装器 SHA-256 → 下载内容双重校验。
  `SHA256SUMS.txt` 只是辅助文件，**不在信任链上**（没有 manifest 签名时不用于放行）。
- 验签先于解析：公钥不匹配/签名无效 → 整个 Release 拒绝（不显示版本、不显示 notes）。
- 私钥不在代码仓库：CI 构建从 GitHub secret 解码，本地构建用环境变量
  （`LLAMAMONITOR_UPDATE_PRIVATE_KEY_FILE` / `_PRIVATE_KEY`）。
- Ed25519 ≠ Authenticode 代码签名：更新通道保证"你下载的就是官方发布的那份"；
  首次启动 SmartScreen 的"未知发布者"提示由 Authenticode 决定（我们不做代码签名）。
- 全部 5 个 `/api/update/*` 端点 **loopback-only**（远程 403）。

设置（Settings → Updates，保存后重启生效）：

- `updates.check_enabled`（默认**关**，启动时不自动检查）、
  `updates.check_interval_hours`（默认 24h）、`updates.auto_download`（默认关）。
  开启后，启动时若距上次检查超过 interval 会自动 Check 一次（一次性任务，非常驻轮询）；
  auto_download 只自动**下载**（已验签），不自动安装。
- 状态区：当前版本 / 安装模式（installed|portable|development）/ 可用版本 /
  下载进度条 / 上次检查时间 / 错误信息；release notes 纯文本渲染（textContent，无 HTML 注入）。
- 有可用更新时：Dashboard 顶部显示可关闭的更新横幅；托盘菜单出现
  "Update Available: x.y.z"（点击打开 Updates 分区）。

构建与发布：

```powershell
# 本地正式构建（需要签名私钥，否则构建失败）：
$env:LLAMAMONITOR_UPDATE_PRIVATE_KEY_FILE = "<path>\private_key_key-2026-09.pem"
python scripts\build_release.py        # 产物含 release-manifest.json + release-manifest.sig
python scripts\validate_release.py     # 独立校验（含签名验证 + 规范化格式检查）
python scripts\generate_update_key.py --key-id key-YYYY-MM   # 轮换/新增签名密钥
```

GitHub Actions（`.github/workflows/release.yml`）：tag `v*` 推送触发；
从 secret `LLAMAMONITOR_UPDATE_PRIVATE_KEY_B64`（Base64 PEM）解码到 runner 临时目录签名，
key id 用 repo variable `LLAMAMONITOR_UPDATE_KEY_ID`（缺省 `key-2026-09`），
自动创建 GitHub Release（安装器 / 便携 ZIP / manifest / .sig / SHA256SUMS 共 5 个资产）。

## 配置

**运行时真正读取的配置文件**（首次启动自动生成默认值）：

```
%LOCALAPPDATA%\LlamaMonitor\config.json
```

配置文件定位优先级（高 → 低）：

1. 环境变量 `LLAMAMONITOR_CONFIG` 指定的路径
2. 开发模式：项目根目录存在 `config.json` 时使用它
3. 默认：`%LOCALAPPDATA%\LlamaMonitor\config.json`

项目根目录的 `config.example.json` 只是**参考模板**（包含全部可配置项和默认值），
运行时不会读取它，也不要把 dist 里的它当作唯一配置来源。

> **Configuration changes take effect after restarting LlamaMonitor.**
> （修改 config.json 后需要重启程序才生效，当前版本没有热重载。）

命令行参数可以临时覆盖配置（优先级最高）：
`--url`（llama-server 基础地址）、`--interval`（采集间隔秒）、`--db`（SQLite 路径）、
`--background`（后台模式：完整运行但主窗口隐藏，只进托盘——开机自启固定使用）。

## GPU 监控（NVIDIA）

- 数据只来自系统 NVIDIA 驱动自带的 `nvidia-smi.exe`（PATH 或标准安装路径），
  固定查询参数、只读、3 秒超时；**不需要** CUDA Toolkit / PyTorch / pynvml，
  只要装了 NVIDIA 驱动。不打包 nvidia-smi、不做任何 GPU 控制（功耗/频率/风扇/杀进程）。
- GPU 身份用 **UUID**（驱动升级、插拔后 index 可能变化，UUID 稳定）；index 只用于显示。
- Dashboard：每张 GPU 一张卡（负载/显存/温度/功耗/风扇/SM 频率/显存频率/PCIe），
  曲线（Utilization + VRAM%、功耗、温度；15m/1h/6h/24h 可选，GPU 多时可勾选显示），
  今日能耗（每 GPU + Total，**采样梯形积分估算**，非电表级精度；
  睡眠/断线造成的长间隙不积分，不会把关机时间算成满载功耗）。
- 数据存储：`gpu_samples` 原始采样按 `gpu.history_retention_hours`（默认 48h）自动清理；
  `gpu_daily` 按天汇总（avg/max/energy）**永久保留**，可导出 CSV。
- nvidia-smi 缺失/失败：Dashboard 显示 "GPU Monitoring Unavailable" 横幅并说明原因，
  状态翻转才记日志（不刷屏），llama.cpp 监控完全不受影响；恢复后自动继续。
- 设置：Settings → GPU（启用开关、轮询间隔、保留时长、勾选要监控的 GPU；
  不勾选任何项 = 监控所有检测到的 GPU）。

## 设置页（Settings）

Dashboard 顶部导航切换 **Dashboard / Settings**。设置页分区：Server / Collector /
GPU / Interface / Web / Storage / Logging / Data / Application / Updates（+ About 页）。

- **Server**：llama-server 基础地址（http/https）、metrics 路径、连接超时；
  **Test Connection** 由后端只 GET `<url><metrics_path>`（不触碰任何控制接口），
  成功显示延迟毫秒数，失败显示原因。
- **Collector**：采集间隔、实时数据保留时长（小时）。
- **GPU**：启用开关、轮询间隔（1~3600s）、历史保留时长（小时）、Detected GPUs 勾选
  （来自 nvidia-smi 实时探测；见上节）。
- **Interface**：Dashboard 刷新间隔、默认历史天数、主题（Dark/Light/System，即时预览）。
- **Web**：LlamaMonitor 自身 Host/Port（改 host 离开 127.0.0.1 会提示局域网暴露警告）。
- **Storage**：数据库路径（留空 = 默认路径）、WAL 开关。
- **Logging**：日志级别、滚动大小、备份份数。
- **Data**：数据管理（见下节）。
- **Application**（Phase 12）：开机自启开关（HKCU Run，仅当前用户）、退出应用、
  集成信息（frozen/background/单实例/托盘/数据目录/uptime）。
- **Updates**（Phase 13）：安装版应用内安全更新——Check for Update / Download /
  Install / Cancel，状态区显示当前版本与可用版本、release notes（纯文本渲染）、
  签名 key_id；详见"安全更新（Secure Updates）"章节。

行为约定：

- 表单值全部来自 `GET /api/config`（前端不硬编码默认值）；**Reset to Defaults** 只重置
  表单（默认值来自 `GET /api/config/defaults`），显示 “Unsaved changes”，
  点 **Save** 才写 `config.json`。
- **Save** 只在有改动时可用；保存走 `PUT /api/config`：逐字段类型 + 范围校验，
  只更新已知字段，保留文件里的未知字段与未修改字段；写入是原子的
  （临时文件 + `os.replace`），文件永远不会被写成半个 JSON。
- 除主题（纯前端显示）外，**所有设置保存后需重启 LlamaMonitor 生效**
  （Settings saved. Restart LlamaMonitor to apply changes.；无热重载、无自动重启）。
- 有未保存改动时切回 Dashboard 会先确认是否丢弃。
- 设置页表单进入时读取一次，不轮询（避免覆盖未保存的编辑）。

## 数据管理（Settings → Data）

- **Data Info**：数据库路径/大小、首末记录日期、记录天数、实时采样条数、GPU 采样条数、
  备份数；进入 Data 分区自动刷新一次，也可手动 Refresh。
- **Export Daily Usage CSV**：导出 daily_usage，UTF-8 with BOM（Windows Excel
  直接打开不乱码），数字为原始整数，日期 `YYYY-MM-DD`，
  含派生列 compute_tokens（prompt+output）、logical_tokens（prompt+cached+output）、
  mtp_accept_rate（draft=0 时为空）。文件名 `LlamaMonitor_daily_YYYYMMDD_HHMMSS.csv`。
- **Export GPU Daily CSV**：导出 gpu_daily（按天 GPU 统计：avg/max 负载、显存、温度、
  功耗 + 估算能耗），UTF-8 with BOM。文件名 `LlamaMonitor_gpu_daily_YYYYMMDD_HHMMSS.csv`。
- **Backup Database**：用 SQLite Backup API 生成一致性快照（运行中/WAL 开启均安全，
  不是文件复制），创建后立即 `PRAGMA quick_check` 验证；保存到
  `%LOCALAPPDATA%\LlamaMonitor\backups\`（手动备份 `manual_monitor_*.db`，自动备份
  `automatic_monitor_*.db`，早期旧备份归为 legacy）；界面列出最近 20 个备份
  （newest first，含类型与 Verified 验证状态）。**备份还原目前不支持**（可在停止程序后
  手工用 SQLite 工具打开备份文件查看/恢复）。
- **Automatic Backup**：`backup.automatic`（默认开）时，距上次成功自动备份
  ≥ `backup.interval_hours`（默认 24h）即在下一检查周期自动备份；
  `backup.keep_count`（默认 14）为自动备份保留份数，超出的旧**自动**备份被删除
  （手动备份永不轮转；备份目录缺失/不可写只告警不崩溃）。
- **Database Health / Run Integrity Check Now**：显示数据库健康状态
  （healthy / warning / corrupt / unavailable，来自 `PRAGMA quick_check`）与
  journal mode；"Run Integrity Check Now" 手动执行一次只读检查（不修复、不删库）：
  通过 ⇒ 恢复写入（若之前 corrupt 记 `database_recovery` 事件）；
  失败 ⇒ 进入 protective mode（见下节）。启动时已自动执行一次。
- **Clear Live History**（警告操作，需确认）：删 `live_samples` + `gpu_samples`
  并 VACUUM 收缩文件；每日累计（daily_usage / gpu_daily）与计数器 baseline 不动。
- **Reset All Statistics**（危险操作，需输入 RESET 确认）：同一事务删除
  `daily_usage` + `live_samples` + `gpu_samples` + `gpu_daily` + `mtp_position_daily`，
  **保留 `state`（当前 Counter baseline，含 per-position MTP baseline）**——
  重置后 Today/Total 归零，llama-server 与采集器都不受影响，下一次只累计新增 delta，
  旧 Token 不会重新计入（GPU 数据不是累计 Counter，重置后从 0 开始新的按天统计）。
  它重置的是 LlamaMonitor 自己的历史统计，**不会**重启 llama.cpp、
  不会重置 llama.cpp 的计数器、不动 GPU。
- live_samples / gpu_samples 的自动清理（按各自保留时长配置）继续生效；
  daily_usage / gpu_daily 永远不自动删除。

## 数据可靠性与质量（Phase 11）

LlamaMonitor 是只读 sidecar：Token 统计全部来自 llama-server 的**累计 Counter delta**，
因此"server 重启 / 断网 / 程序重启 / 睡眠 / 时钟跳变"等事件下的正确性由以下规则保证。
完整验证见 `REAL_SOAK_TEST.md`（确定性 soak 模拟 + ground truth 恒等式）。

### Token 统计规则

- **counter reset 检测**：某 Counter `current < previous` 判定为 server 重启 ——
  delta = current（只计重启后的新增），并记 `counter_reset` 事件
  （monitor_events 表，含 counter 名与前后值）；`current >= previous` ⇒ 正常 delta。
- **per-metric 独立 baseline**：每个 Counter 的 baseline 独立持久化在 `state` 表；
  某个 Counter 本轮缺失 ⇒ 保持其旧 baseline（**绝不**把缺失当 0 或当 reset），
  恢复后继续正常累计。
- **sample_invalid**：HTTP 200 但核心 Counter（prompt/output）**全部**缺失 ⇒
  本轮判为无效样本：不产生 delta、不更新任何 baseline、不落 live_sample；
  单独某个 Counter 缺失只影响该 Counter（上一条）。
- **恢复后不重复计入**：baseline 持久化 ⇒ 程序重启、写失败重试、离线恢复都不会
  把旧 Token 重复计入（写失败时保持旧 baseline，下一轮重算完整 delta）。

### 监控缺口（data_gaps，永久保留）

有效样本间 monotonic 间隔 > `poll_interval * 3` ⇒ 记录一条已知缺口：

| reason | 含义 |
|---|---|
| server_offline | 期间有轮询轮次但 metrics 不可达（server 宕机/断网） |
| invalid_metrics | 期间 metrics 可达但核心 Counter 缺失 |
| monitor_restart | LlamaMonitor 自身重启（上一进程停止 → 本进程首个有效样本） |
| system_pause_or_sleep | 期间**没有任何轮询轮次**且 monotonic 跳变超阈值（系统睡眠/挂起） |
| unknown | GPU 侧无法区分原因的短断档 |

- **possible_token_loss**：缺口期间核心 Counter 发生 reset（盲区 + server 重启）⇒
  该缺口标记"可能有不可恢复 token 丢失"；否则 token_recoverable。
- 缺口判定**只用 monotonic**：wall clock 被修改/时区跳变/跨 DST 不影响缺口检测与时长。
- Dashboard 底部 **Data Quality** 区域显示：数据库状态、当天 Monitoring Coverage、
  当天/历史已知缺口数、token 丢失提示、未结束缺口、最近缺口明细。
  **Coverage = 当天首样本→末样本窗口内无已知缺口的比例**；它衡量监控断档，
  不是 Token 统计准确率（Token 按 Counter delta 精确累计 + 上述 reset/丢失判定）。

### GPU 统计

- **能耗 = monotonic 三角积分**（相邻采样功率均值 × monotonic Δt）：
  wall clock 跳变（改时间/时区）不会伪造出几小时的假能耗；
  进程重启后首个 GPU 采样不积分（误差 ≤ 一个采样周期）。
- **跨日分割**：跨午夜的能耗按本机午夜 00:00:00 用 wall 比例精确拆分到两个自然日，
  总能量不变；GPU 采样缺档 > 阈值 ⇒ 记 GPU 缺口（reason: system_pause_or_sleep / unknown）。

### 数据库保护（不自动修复、不删库）

- 启动顺序：打开 DB → `PRAGMA quick_check` → 通过才继续（失败进 protective mode）；
  所有写走 `_tx_with_retry`（busy/locked 重试 + busy_timeout 30s）。
- **protective mode**：health ∈ {corrupt, unavailable} 时，修改类写入停止
  （daily/live/state/gaps 不再写，保留最后一致状态），采集与只读展示继续；
  健康恢复（下次 quick_check 通过或手动 Run Check 通过）自动恢复写入并记事件。
- **绝不**：自动 VACUUM 修复、自动恢复备份、删除/覆盖原库、静默继续写可能损坏的库。

## 数据文件（升级不受影响）

全部位于 `%LOCALAPPDATA%\LlamaMonitor\`，与程序目录分离：

```
monitor.db          SQLite：daily_usage / live_samples / state / mtp_position_daily /
                    gpu_samples / gpu_daily / monitor_events / data_gaps / backup_history
config.json         用户配置
backups\            数据库备份（manual_monitor_*.db / automatic_monitor_*.db /
                    早期 monitor_*.db）
logs\monitor.log    滚动日志（默认 10MB x 5；记录启动/关闭/状态变化/保存配置/备份/
                    清理/重置/CSV 导出/Test Connection 失败/缺口与 reset 事件，不记录每次抓取）
```

升级/替换 `dist\LlamaMonitor` 目录不会删除或覆盖上述任何文件。

**数据库 schema 迁移**：monitor.db 用 `PRAGMA user_version` 标记版本
（当前 **v3**：Phase 11 增加 `monitor_events`（counter_reset / sleep_gap /
database_* / backup_* 等事件审计）/ `data_gaps`（已知监控缺口，永久保留）/
`backup_history`（备份元数据与验证状态）；v2 = Phase 9 GPU 表）。
启动时自动检测并逐版本迁移：旧版本（含早期无版本号的库）数据无损升级，
迁移在独立事务内完成，失败自动回滚并在下次启动重试；从不 DROP/DELETE 旧数据。
**Counter 事务审计**：所有修改类写（daily 归集、state baseline、live 清理、
gaps、events、backup_history）都经过统一的 `_tx_with_retry`（单事务 +
busy/locked 重试 + busy_timeout），任何一步失败整体回滚，不存在"写了一半"的状态。

## Windows 集成（托盘 / 单实例 / 开机自启 / 优雅关闭）

### 系统托盘 + 关闭不等于退出

- 关闭窗口（右上角 X）**只隐藏到托盘**：FastAPI、Collector、GPU 采集、SQLite、
  托盘图标全部继续运行，监控不中断；首次关闭时托盘气泡提示一次
  "LlamaMonitor is still running in the system tray."。
- **真正退出**只有三种方式：托盘菜单 → Exit；Settings → Application →
  Exit LlamaMonitor（需确认）；`POST /api/app/exit`。
- 托盘菜单：Open Dashboard（左键单击托盘图标也是打开）、llama.cpp / GPU /
  Today 状态（只读，状态变化才刷新）、Open Data/Log Folder、
  Start with Windows（勾选状态来自真实注册表）、Exit。
- 托盘图标 tooltip 随在线状态变化：`LlamaMonitor - Online` / `LlamaMonitor - llama.cpp Offline`。
- `--background`：完整启动（FastAPI/Collector/托盘都运行）但主窗口隐藏创建
  （不闪窗）——开机自启固定使用此模式。托盘初始化失败时：前台模式 Dashboard
  照常运行；后台模式自动改为显示 Dashboard 并记 WARNING。

### 单实例（Named Mutex，不是锁文件）

- 启动最早期（先于数据库/Collector/FastAPI）获取命名互斥体
  `Local\LlamaMonitor.SingleInstance`；已存在 → 判定已有实例在运行。
- 第二实例**唤醒第一实例**：向命名事件 `Local\LlamaMonitor.ShowWindow` 发
  SetEvent，第一实例的监听线程收到后把 `show` 命令交给单线程 UI 分发器
  （恢复窗口 + 置顶带到最前），第二实例正常退出（不启动任何采集/服务）。
- 用 mutex 而不是锁文件的原因：进程崩溃/断电时 OS 自动释放 mutex，
  永远不会留下"锁还在但程序死了"的 stale 锁文件；`Local\` 前缀保证
  每会话独立（多用户/远程会话互不干扰）。
- 端口防御作为兜底：极端情况（mutex 未拦截但端口已被另一个 LlamaMonitor 占用）
  时用默认浏览器打开已有实例的 Dashboard，并弹对话框说明，本实例退出，
  已有实例不受影响。

### Start with Windows（开机自启）

- Settings → Application 的 **Start with Windows** 开关；写入
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 的值 `LlamaMonitor`，
  内容为 `"<EXE 绝对路径>" --background`（总是加引号、绝对路径、
  登录时进托盘不弹窗口）。
- 只写 **HKCU**（当前用户，登录时启动），不需要管理员、不是 Windows Service、
  不写 HKLM；仅 EXE 模式可用（开发模式显示 Unavailable）。
- 状态永远**读真实注册表**（不在 config.json 里存"自启状态"）：
  Enabled / Disabled / **Stale**（注册表值指向的 EXE 路径与当前不同——
  程序被移动过；不静默修改，显示 Repair 按钮，用户明确点击才修复）。

### 优雅关闭

- 状态机 RUNNING → STOPPING → STOPPED，幂等（连点两次 Exit 只执行一次）。
- 关闭顺序（每阶段有超时预算，总计约 8~12s，超时记 WARNING 后继续，
  不用 os._exit/taskkill 硬杀）：停止托盘 → 停 FastAPI（lifespan 取消
  Collector/GPU 任务并等待在途采样结束，最后关 SQLite）→ 销毁窗口 →
  停 UI 命令分发器 → 停 ShowWindow 监听（CloseHandle 事件句柄）→
  释放单实例 Mutex（CloseHandle）→ 退出。
- Token 统计安全：每轮采集的 daily_usage 累加与 state（计数器 baseline）
  更新在**同一个数据库事务**里提交，因此任何退出/崩溃发生在提交之后时，
  重启后新增量既不会重复计入也不会遗漏（已有单测覆盖：
  baseline 1,000,000 → 1,001,000 后崩溃/退出 → 重启后 1,001,500 = 共 1500）。
- 所有启动失败与关闭过程写入 `%LOCALAPPDATA%\LlamaMonitor\logs\monitor.log`
  （`--windowed` 无控制台，日志是唯一的诊断通道）；致命失败额外弹系统对话框。

### 本地管理 API（loopback-only）

修改类 API 只接受本机连接（按实际 socket 地址 127.0.0.1/::1 判定，
**不信任 X-Forwarded-For**），局域网客户端可读 Dashboard 数据但不能改设置：

- `PUT /api/config`、`POST /api/config/test-connection`
- `POST /api/data/backup` / `clear-live` / `reset-statistics`
- `PUT /api/app/autostart`、`POST /api/app/open-folder`、`POST /api/app/exit`
- 只读 API（/api/status、/api/summary、/api/daily、/api/live、GPU 等）远程可读。

相关端点：`GET /api/app/integration`（托盘/单实例/自启/路径/uptime 集成状态）、
`PUT /api/app/autostart`（{enabled}）、`POST /api/app/open-folder`（data|logs|backups）、
`POST /api/app/exit`（先返回再调度关闭）。`GET /api/status` 新增
`application: {background, tray_available, single_instance, uptime_seconds}`。

## 端口

- 8765：LlamaMonitor 自身（127.0.0.1，仅本机，可用 `web.host`/`web.port` 配置）
- 9091：llama-server 的 /metrics（只读）
