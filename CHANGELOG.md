# 变更日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。
版本号由 `version.py` 的 `__version__` 单一来源给出（SemVer，无 `v` 前缀）。

> 版本序列说明：Phase 13 采用 **0.13.x** 里程碑系列（安全更新）。0.13.x 与 1.0.0
> 互相视为"不同系列"：安装器降级保护按数值比较（1.0.0 > 0.13.x），从 1.0.0 安装
> 0.13.x 会被识别为降级并拒绝（实测行为，非缺陷）。

## [1.1.3] - 2026-09-27

**Pixel & Interaction Refinement**。不新增后端功能、不新增页面、不改变监控语义、
数据库 schema **保持 5**。重点是：设计 token 唯一视觉源清理、断点体系归并、
Mobile 导航重设计、Heat Grid 真 per-core、图表/表格/交互/状态/对比度/布局的系统性精修。

### 设计体系
- 设计 token 唯一视觉源：content-max 单一定义（+GPU/history 变体）、radius 三档
  别名、**z-index 10 级 token**（content 0 → tooltip 90，全量裸值替换）、
  line-height 3 档、mobile ≤760px 字号体系（caption 13 → hero 32）。
- 断点体系归并为 **1366 / 1099 / 760 / 360** 四档 + 功能查询：旧 900/1100/1200/1400/
  700/640 全部归并（900/1100→1099，1200/1400→1366，700/640→760），消除断点碎片。

### Mobile 导航重设计（≤760px）
- 底部导航 = **5 个核心监控域一键直达**（概览 / 用量 / 性能 / GPU / 系统），
  60px + safe-area；active 态改为 accent icon + `--accent-subtle` pill
  （替代 1.1.2 的 3px 顶线）。
- 辅助页（监控历史 / 设置 / 关于）收进**每页页头右侧 ••• overflow**（44×44）
  打开 More Sheet：焦点陷阱（Tab 循环）+ body 滚动锁 + ESC 关闭 + 焦点归还 +
  aria-expanded/aria-modal。
- 每页 scrollTop 记忆；重击当前页回到顶部；远程只读时设置项禁用并标注。

### Heat Grid 真 per-core（系统页 CPU 负载）
- 1.1.2 用整机聚合值填充 N 个格子（明确标注"非逐核拆分"）；1.1.3 改为
  **真实逐核利用率**：collector 增 `cpu_percent(percpu=True)`（live-only，
  不进 DB），`/api/system/status` 返回 `cpu.per_core_percent`。
- 每格显示真实利用率整数 + 颜色连续映射（accent × opacity）；
  **物理核 / 逻辑核视图切换**（物理核 = 超线程 sibling 取均值，默认）。
- 物理核分组三级回退：Windows GLPIEx（精确）→ Linux cpu_affinity →
  physical/logical 整除 N 倍时确定性配对；全部失败才回退逻辑核平铺。
- 无逐核数据（首条 warmup / 核数过少）→ 只显示聚合口径说明行，**禁止画多格
  假 per-core**。

### 图表
- GPU 温度图 y 轴 nice lower bound（min(data)−10 取整，clamp ≥0）：
  50-70°C 波动不再被 0 基线压扁。
- Token bar 宽度按数据点数分档（≤2→48 / ≤7→32 / ≤31→18 / 更多→12），
  避免稀疏数据 hairline / 密集数据糊块。
- Token 类图表 tooltip 双显：`1.23M (1,234,567)`（紧凑 + 千分位全整数）。

### 表格
- 每日用量表列序按阅读逻辑重排（日期 | Token 总量 | 实际计算 | 输入 |
  缓存复用 | 输出 | 复用率 | 采集覆盖率 | 缺口）；mobile card rows 内
  增加**分组分隔线**（`.td-group-start`）。
- card rows 选择器由 `:has()` 改为显式 `.mobile-card-table` 类（JS 注入），
  规避旧浏览器 `:has` 缺失。
- GPU 进程表包裹 `.table-wrap`：修复 759-1099px 区间长进程名导致的
  页面横向溢出（1059px→容器内横滚）。

### 交互与状态
- 相对时间 ≤5s 显示"刚刚"（6-59s 才 X 秒前）：消除"2秒前/3秒前"逐秒跳动。
- tooltip hover 揭示加 300ms 进入 / 100ms 离开延迟（hover 设备；
  键盘 focus 即时，触屏 tap 路径不受影响）。
- 远程只读 banner 可关闭（sessionStorage 记忆）。
- 布局 bug：概览 status-strip 长 URL/模型路径溢出（mobile 390 下 723px
  内容被 overflow-x:hidden 裁切）→ ellipsis + flex-basis 0 修复。

### 可访问性 / 对比度
- light `--text-muted` #8a8a8a → #6e6e6e（3.39:1 → 4.6:1，正文级 AA）；
  dark muted 0.45 → 0.50（4.49 → ~4.7:1）。
- 输入框 32→34px；**mobile 输入 44px + 16px 字号**（iOS focus 不自动缩放）。
- 暗色主题 success/warning/error/info 对比度实测 5.67-7.46:1（AA 通过）。

### 文档 / 仓库
- README：界面预览置顶、清理 Phase 11/12/13/14 术语、mobile 导航描述更新、
  截图重拍（desktop 57KB / mobile 82KB，真 PNG < 500KB）。
- `docs/UI_AUDIT_1.1.3.md`（27 项编码问题清单）、`docs/UI_REVIEW_1.1.3.md`
  （逐页×主题 PASS/FIXED/KNOWN + gate 结果）。
- 1.1.2 条目日期更正为 2026-09-27。

## [1.1.2] - 2026-09-27

**Mobile & Visual Experience Update**。不新增后端功能、不改变监控语义、
数据库 schema **保持 5**。重点是：≤760px 真 Mobile 模式 + 视觉 token 统一
（Telemetry Fluent 设计语言）+ 触控/可访问性/图表/表格的 Mobile UX 精修。

### 真 Mobile 模式（≤760px）
- 隐藏桌面侧边栏，启用固定 Bottom Navigation（概览 / Token / 性能 / GPU / 更多，
  56px 高 + safe-area-inset-bottom）；"更多"打开底部 Bottom Sheet（系统 / 监控历史 /
  设置 / 关于，顶部圆角 16px，支持点空白 / ESC / 返回关闭，内含版本号）。
- 页头改纵向（标题 + 副标题 + 操作按钮堆叠）；图表 240-280px；stat 网格 2 列；
  正文 15px / 辅助 14px / 指标 24-26px 的 mobile 排版层。
- 触控目标 ≥44×44px（按钮、分段选择 ≥40px、chip/导航 ≥40px）；
  `@media (hover:none)` 收敛装饰性 hover（防点按高亮残留）；允许用户缩放。
- 信息提示（i）按钮支持点按开/关（tap 外部关闭），气泡 `max-width: min(300px, calc(100vw - 32px))` 不超屏。
- Toast 移到 Bottom Navigation 上方；Modal 在手机上改为底部弹出式。
- Windows 高对比（forced-colors）：CanvasText 边框 + Highlight 指示条。

### 表格 → Card Rows（≤760px）
- Token 页"每日明细 / 缺口 / 事件"三张表在小屏变为卡片行：表头隐藏、
  首列作卡片标题、其余字段变"标签: 值"纵向行（`td[data-label]` +
  `content: attr(data-label)`）。GPU 进程表（4 短列）保持表格。

### 视觉统一（Telemetry Fluent）
- `tokens.css` 重写为唯一视觉源：统一 spacing（4-40）、radius（micro→pill 7 级）、
  字号体系、`--touch-target` 44/48、`--bottom-nav-height` 56、逐页 content-max
  （settings 1280 / about 960）、图表高度 token（desktop 320 / mobile 260）。
- 新增 `mobile.css` 承载 ≤760px 全部覆盖（layout 结构在 layout.css 的 760 断点内）。
- 清理 Phase/spec §xx 流水账注释（保留 WHY 说明）。

### 页面 Mobile 精修
- GPU 页：单列布局 + 指标 chips 横滚；"高级遥测 / 健康"（performance_state、
  throttle、ECC）收进可折叠 `<details>`（桌面默认展开观感不变，手机默认收起）。
- System 页：每核心负载 >16 核时渲染 Heat Grid（`core-heat-grid`，每格颜色映射
  当前整机聚合利用率，并注明数据源口径）；传感器列表 / 卷容量 / Heat Grid
  在手机上默认折叠（桌面默认展开，随视口宽度自动切换）。
- Settings ≤760px：分区 rail 单行横向滚动（禁 wrap）；保存栏 sticky 钉在
  Bottom Navigation 上方（含 safe-area）。
- 概览副标题改为"集中查看 llama.cpp 服务、Token、推理性能、GPU、主机与数据采集状态。"

### 远程只读体验
- 非本机访问（`web.host: 0.0.0.0` + 局域网 IP）时界面顶部显示轻量只读 banner，
  并在桌面/移动导航中隐藏"设置"。后端边界不变（修改类 API 仍按实际 socket
  地址回环保护，不信任 X-Forwarded-For）。

### 文档
- README 顶部新增"界面预览"（桌面 1920×1080 + 手机 390×844 真实运行截图，
  `docs/images/`）与"手机访问 Dashboard"章节（`web.host: 0.0.0.0` + 局域网 IP
  访问说明、只读边界说明）。
- 新增 CDP 巡检脚本 `scripts/ui_patrol_112.js`（17 视口 × 8 页矩阵：水平溢出 /
  结构 / 触控目标 gate）与 `scripts/capture_112.js`（README 截图 + 数据断言）。

### 回归
- 新增 `tests/test_mobile_ui.py`（Bottom Nav / More Sheet / card rows /
  sticky save bar / touch / token 源等静态断言）。
- 506 项既有测试 + 新增测试全绿；136 viewport×page 巡检 0 水平溢出 /
  0 结构失败 / 0 触控失败（无真机，Chrome Responsive 覆盖，
  Real device not available）。

## [1.1.1] - 2026-09-27

**Quality & UX 维护版（Quality & UX Maintenance Release）**。基于 1.1.0 的全项目
质量审计（0 BLOCKER / 8 HIGH / 全部 MEDIUM 修复），不新增功能、不改变既有 Token /
GPU / System 监控语义。数据库 schema **保持 5**（1.1.0→1.1.1 直接兼容，无迁移；
已用真实已装库验证无损升级）。

### 正确性
- `/api/mtp` 的"今日"此前用 wall `local_date()`，而兄弟端点全用 `collector.clock`
  （跨午夜 00:00:00→首个采集之间显示昨日数据，与 1.1.0 BUG-A 同类、漏网一个端点）。
  现改为与 `/api/summary` 同源。回归测试 `test_mtp_rollover_across_midnight`。
- CPU 能耗跨午夜不分割（GPU 侧有 `_split_energy_across_midnight`，口径不一致）——
  现对齐。回归测试 `test_midnight_split` / `test_midnight_split_db_attribution`。
- 事件表 / 缺口表 `title='…'` 属性裸拼 reason/detail，单引号截断致 tooltip 残缺 /
  class 被吞。现统一过模块级 `escAttr`（转义 `& < > " '` 五字符）。回归测试
  `test_escattr_escapes_quotes_for_title_attr`。
- "本月"图表/表格此前走浏览器时区前端过滤（与服务端 `month_key` 不同步时差 1 天）。
  现改服务端 `?month=true` 过滤。回归测试 `test_daily_month_filter_server_side`。

### 稳定 / 性能
- system 采集器每 2s 同步 psutil 阻塞事件循环（~20-50ms/次）→ 拆独立任务 +
  `to_thread`。`check-database`/`inventory?manual`/clear-live `VACUUM`/CSV 导出
  同样移出事件循环（`*threadsafe` 系列）。
- `/api/data/quality` 每轮 `get_gaps(limit=100000)` 全量物化 → `get_gap_totals`
  聚合查询。回归测试 `test_quality_endpoint_does_not_scan_all_live_samples`。
- llama Runtime health 状态机无 debounce，单次抖动即翻 Unavailable + 记事件 →
  2 次连续失败才翻转。回归测试 `test_health_single_transient_fail_does_not_flip`。
- poller 循环 `out()`/`sleep` 异常会静默杀循环（无测试）→ 补异常保护 + 回归测试
  （`test_collect_once_exception_does_not_kill_loop` / `test_out_exception_does_not_kill_loop`
  / `test_cancel_cleans_up_loop`）。
- 主进程硬杀后孤儿 `HardwareSensorBridge.exe` → 启动时 `sweep_orphans()` 清扫同签名
  孤儿进程（Job Object 方案延后 1.2）。

### 可靠 / 易用
- bridge reader 线程 `readline` 无超时，静默卡死不重启 → `_pump_stdout` 心跳超时
  （3× 采样间隔无输出 → kill+退避重启）。回归测试 `test_hang_returns_true_and_kills`。
- 全部备份测试 `wal=False`，WAL 模式备份未测 → 补 `WalModeBackupTests`（一致性 /
  完整性 / 关闭前备份）。回归测试 `test_wal_source_backup_is_consistent_and_complete`。
- 未处理异常 500 契约无测试 → 补 `test_unhandled_exception_500_contract`
  （`INTERNAL_ERROR`，不泄 body / traceback）。
- 在线态"最后更新"被清空、数据新鲜度不可见 → 现显示"最后更新 X 秒前"（每秒刷新）。
  回归测试 `test_online_shows_last_update_age`。
- Performance 页 MTP 趋势图停留期间 / 跨页不更新 → 注册独立 `mtp` poller
  （`visibleOnly:false`）。回归测试 `test_mtp_poller_updates_performance_trend`。
- Token 计数格式化精度不一（卡片 2 位 / 图表 1 位）→ `compact()` 统一。
- 事件"查看更多"被轮询全量重建致焦点丢失 → 焦点记录 + 重建后恢复。
- Slot 任务结束后 per-request 字段陈旧最长 ~10s（`is_processing` 正确但数字 stale）
  → 空闲残留值淡化 + 注脚"上一次请求的残留"，不当当前状态。回归测试
  `test_slot_stale_semantics_present`。
- 数据质量卡 DB 状态显示英文 raw 值 → 中文化（健康/警告/降级/不可用）；设置页
  传感器"加载中"占位不再永久残留（刷新失败写终态）。

### 文档一致性
- README：`/metrics` 单一端点声明、设置分区矛盾、废弃术语修正；`config.example.json`
  从 `config.py DEFAULT_CONFIG` 重新生成（消除漂移 + 补 system 段）；
  `docs/API.md` 补全部 1.1 端点；`ARCHITECTURE.md` / `METRICS_DEFINITIONS.md` /
  `PERFORMANCE.md` / `STORAGE_ESTIMATE.md` 更新到 1.1 基线（补 system 指标 + 1.1 复测节）；
  `UI_TERMINOLOGY.md` 补 1.1 新词节；`RELEASE.md` 版本示例参数化。
- 16 个历史审计/发布报告各加 historical 头横幅（"1.1.1 起标注"，仅作追溯参考）。

### 仓库 / 构建卫生
- `.gitignore` 收编 `.venv*/`；97 张 tracked 开发截图移 `artifacts/`（`git mv`）；
  `REAL_SOAK_TEST.md` 移 `docs/`。
- `build.bat` 补 `--add-data` HardwareSensorBridge.exe + DLL（此前"同一套 flags"
  注释失实，便携版可能缺桥）；`THIRD_PARTY_NOTICES.txt` 移除未使用/未导入的
  pefile / xlrd"残留登记"；`release.yml` 注释举例参数化。

### 测试
- 全量 `python -m unittest discover -s tests`：**506 例全绿**（1.1.0 基线 487，
  +19 回归：bridge stdout 泵送 4 + poller 循环 3 + WAL 备份 2 + 500 契约 1 +
  跨午夜 MTP 1 + runtime health debounce 1 + CPU 跨午夜 DB 归属 1 + slot 残留语义
  1 + 前端 HIGH 回归 3：escAttr / MTP poller / lastUpdate）。

## [1.1.0] - 2026-09-26

**System & Hardware Telemetry**。新增系统页（8 个区块）、llama.cpp Runtime
只读遥测、GPU 高级遥测与能耗修正；全程只读，不改变既有 Token / GPU
监控语义。数据库 schema 4 → 5（迁移前自动备份 `pre_migration_v4_to_v5_*.db`）。

### 新增
- **系统监控**（`system_collector.py`，psutil 7.2.2 只读）：CPU 使用率/频率、
  内存、磁盘/网络速率、系统启动时间、静态硬件库存（OS/CPU/主板/BIOS/RAM/
  磁盘）、CPU 能耗积分（CPU Package Power 可用时）。
- **高级硬件传感器**（`hardware_sensor_provider.py` + `native/hardware_bridge/`
  C# 只读桥，LibreHardwareMonitorLib 0.9.2）：CPU 温度/功耗、主板温度、
  风扇转速等；provider 不可用时字段保持 **null**（UI 显示 `--`，绝不 None→0）。
- **llama.cpp Runtime 只读遥测**（`llama_runtime_collector.py`）：/health、
  /slots、/props、/v1/models 分频采集（模型信息 / 当前 Slot 状态 /
  capabilities）；只读，不代理推理请求。
- **GPU 高级遥测**（nvidia-smi fast query 扩展列）：显存控制器利用率、
  功耗上限与功耗百分比、PCIe 链路当前 vs 最大、P-State、驱动版本、
  性能限制原因（位掩码解码）、ECC 错误计数（60s 低频慢查询，
  不支持的卡整组 None → UI 隐藏 ECC 区）、GPU 进程列表（只读，
  WDDM 下显存常 null）。
- **设置页「系统监控」分区**：启用/轮询间隔/历史落库间隔/保留时长/
  高级传感器开关与轮询；`config` 新增 `[system]` 段。
- 新增 API：`/api/system/status|live|daily|inventory|sensors`、
  `/api/runtime`、`/api/mtp`（含 per-position）；GPU API 扩展新字段
  （旧字段全部保留）。

### 修复（1.1.0 审计，逐页实测发现）
- `gpu_collector.driver_version` 从未从解析结果赋值 → `/api/gpu/status`
  驱动版本恒 null（nvidia-smi 明明报告 580.88）。现取任一轮非空值并保留
  最后已知值（N/A 不抹掉已有值）。回归测试
  `test_driver_version_populated_and_sticky`。
- 系统每日「已监测组件能耗」只累加 CPU 部分，与 UI 承诺的「CPU + GPU」
  矛盾（本机 GPU 当日 2000+ Wh 却显示 0 Wh）。现按 design comment 在
  API 层（`api_system_daily`）把 `gpu_daily.energy_wh`（只含被监控卡）
  按日相加。回归测试 `test_daily_component_energy_includes_gpu`。
- GPU 页把**未选中监控**的卡（升级前监控过、后来取消勾选）的陈旧
  最新采样当"还在监控"展示（一排 `--`）。现 `/api/gpu/status` 的
  `gpus` 按 `device_uuids` 过滤；`detected` 保留全集，GPU 页筛选条对
  未监控卡打虚线"未监控"标记（默认不勾选，可手动查看历史）。
  回归测试 `test_status_filters_unmonitored_gpus`。

### 语义与约束
- **只读**：psutil 只查询；LibreHardwareMonitor 桥只调 `Read()`；
  llama Runtime 只发 GET；nvidia-smi 只读查询。
- **null = 不可用**：API 字段 null 时 UI 一律 `--`（高级传感器不可用、
  WDDM 下 GPU 进程显存、消费者卡无 ECC 等）；不拿 0 冒充缺失值。
- 系统采样状态 available / partial / unavailable 三态；provider 故障
  隔离（不影响基础 psutil 监控，更不影响 Token/GPU 采集）。

### 测试
- 全量 `python -m unittest discover -s tests`：**487 例全绿**
  （新增系统采集/系统 API/传感器 provider/运行时隐私/GPU 扩展等模块）。

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
