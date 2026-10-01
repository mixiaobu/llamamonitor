# LlamaMonitor Round-4 GPU（显卡）页产品化 — 验收报告（A–BL）

**范围**：GPU 监控页面 + GPU 页直接依赖：GPU Telemetry 采集、GPU 历史采样、GPU 能耗、GPU 进程、ECC、风扇、时钟、PCIe、性能状态、必要 Formatter、Charts、必要 API/View Model、必要低风险 Schema Migration、响应式布局。
**约束遵守**：不改其它页布局（概览/用量/性能/系统/历史/设置/关于）；主导航"显卡"（两字）；**无版本变更（1.1.3）**；无 Commit / Push / Tag / Release；本轮只做 GPU 页。

**验证环境**：2× Intel Xeon Platinum 8168（48 物理 / 96 逻辑），Windows 11 Pro for Workstations Build 26200.9457，x64，主机 DESKTOP-8NAGU8B。驱动 **NVIDIA 580.88**，`nvidia-smi` 位于 `C:\WINDOWS\system32\nvidia-smi.EXE`（PATH 命中）。
**双卡**：
- `GPU-1fd3ffac…` **NVIDIA T400 4GB**（4 GiB）——风扇 41%、功耗 **N/A**（power.draw 恒 N/A）、ECC **N/A**、P0、显存 2.8 / 4 GiB、PCIe Gen3 x16、`00000000:AF:00.0`、Compute Default、GPU Idle。
- `GPU-d2a56c72…` **Tesla V100-SXM2-32GB**（32 GiB）——风扇 **N/A**、功耗 ~194 / 200 W、**ECC 已启用（计数全 0）**、P0、显存 ~30 / 32 GiB、PCIe Gen3 x16、`00000000:D8:00.0`、Compute Default、Sync Boost。
两卡恰好覆盖：风扇有/无、功耗有/无、ECC 有/无、PCIe 有——天然的能力自适应样本。

**验证方法**：后端 `TestClient` 单测 + 重启 8790 服务加载新后端 + **Edge CDP**（真实 580.88 双卡）对 1920/988/390/320 做 DOM/CSS/滚动/控制台/轮询断言 + 截图（`artifacts/gpu-r4-audit/`）。

---

## A GPU 功能审计（现状 → 改动 → 验证）
审计 GPU 页 1.1.2 现状，定位与规格差异后逐项实现：
- 卡是"主 4 指标 + 16 项平铺 + 桌面自动展开高级"→ 改为**两层卡**（核心 2×2 + 紧凑元信息）+ **桌面也默认折叠**的高级区（§32-34，已改原桌面 auto-open）。
- 缺：功耗占上限、PCIe 最大能力、PCI Bus ID、Compute Mode、Persistence、显存控制器利用率、运行状态 vs 性能限制、ECC 区块、退役页、能耗"不可估算"、更多趋势（风扇+时钟）、进程区无内部滚动、进程表删类型列。
- 全部见 B–BL 逐项落地与验证。✅

## B nvidia-smi 查询方式（固定参数，API 不接受自定义）
`gpu_collector.py` 固定 5 类查询（列序与解析一一对应）：
- **fast（2s，20 列）** `--query-gpu=index,uuid,name,memory.used,memory.total,utilization.gpu,utilization.memory,temperature.gpu,power.draw,power.limit,fan.speed,clocks.sm,clocks.mem,pstate,pcie.link.gen.current,pcie.link.width.current,pcie.link.gen.max,pcie.link.width.max,driver_version,clocks_event_reasons.active --format=csv,noheader,nounits`
- **ECC 健康（60s）** `--query-gpu=uuid,ecc.mode.current,ecc.errors.corrected.volatile.sram,ecc.errors.corrected.volatile.dram,ecc.errors.corrected.aggregate.sram/ecc.errors.corrected.aggregate.dram,ecc.errors.uncorrected.volatile.sram/dram,ecc.errors.uncorrected.aggregate.sram/dram --format=csv,noheader,nounits`
- **ECC 细节兜底（60s，文本）** `-q -d ECC` → `parse_nvidia_smi_ecc_detail`（Retired Pages / Remapped Rows，兼容单行与分段两种格式）。
- **进程（低频）** `--query-compute-apps=pid,process_name,gpu_uuid,used_memory --format=csv,noheader,nounits`（**type 字段 580 无效，见 AF**）。
- **静态（启动一次 + 300s 兜底）** `--query-gpu=uuid,pci.bus_id,compute_mode,persistence_mode --format=csv,noheader`。
✅ 纯 `subprocess` 调 nvidia-smi，无 pynvml；超时 3s kill。

## C 已采集字段
fast 20 列全字段 + ECC CSV 10 列 + ECC 文本退役页/重映射 + 进程 4 列 + 静态 3 字段。全部 `N/A/Not Supported/[N/A]` → `None`（不传前端、不画 0）。✅（`ParseNvidiaSmiCsvTests` / `SlowHealthParserTests`）

## D 已采集但未展示 → 现已展示
1.1 已采集但卡上未露出的高级字段全部接上：显存控制器利用率、功耗占上限、PCIe 最大能力、SM/显存时钟（移入高级）、P-State（移入高级）、throttle reasons（拆"运行状态/性能限制"）。✅

## E 新增字段（本轮新增采集/透传）
- **静态**：`pci_bus_id` / `compute_mode` / `persistence_mode`（`NVSMI_STATIC_QUERY`，300s 刷新；`[N/A]`→None 不显）。✅（`test_static_query_populates_gpu_static`）
- **ECC 细节**：`retired_pages_single_bit / double_bit / pending` + `remapped_rows`（`-q -d ECC` 文本解析，分段格式合计 Correctable+Uncorrectable）。✅（`test_ecc_detail_*`）
- **能耗**：`power_available`（后端每卡 `any(avg_power_w 非空)`，区分"0.x Wh"与"不可估算"）。✅
- **ECC 事件**：`monitor_events`（`gpu_ecc_corrected_increased` 等，冷却 30min）。✅（`test_ecc_corrected_increase_records_event` / `test_ecc_event_cooldown_*`）

## F 不支持字段（能力自适应，绝不画假 0）
风扇 N/A（V100 无风扇）→ 不显 0%；功耗 N/A（T400）→ 核心"功耗 --"、能耗"不可估算"；ECC N/A（T400）→ 整个 ECC 区隐藏；Persistence `[N/A]` → 不显。✅（DOM 实测：V100 卡无"风扇转速"行、T400 卡无 ECC 区）

## G UUID 处理
身份按 **UUID**（非 index）：卡、曲线颜色、selector、高级折叠态（`details.dataset.uuid`）都锚定 UUID；index 重排不串卡。✅（`test_index_reorder_identity_by_uuid` + `state.gpuAdvOpen` 按 uuid）

## H 术语表（冻结）
"运行状态"（空闲/负载中）≠"性能限制"；"显存占用"（已用/总量）≠"显存控制器利用率"；"不可用"（--）非 0；"今日能耗（估算）"；"不可估算"（无功耗遥测）。已移除违禁："显存利用率(表已用/总量)"、"性能限制：GPU Idle"（GPU Idle 归运行状态）、"今日能耗 0Wh"（无遥测时）、"已退休页"模糊总值（拆单比特/双比特/待处理）。✅

## I Card 结构（两层）
第一层 = 核心 2×2 网格（GPU 利用率 / 显存占用 + VRAM 进度条 / 温度 / 功耗），数值 **primary text** 色，N/A 用 `--` 弱化。第二层 = 紧凑元信息（风扇 / P-State / SM 时钟 / 显存时钟 / PCIe 链路；无值整行隐藏）。卡片宽 `min(340px,100%)~420px` 左对齐网格。✅（DOM `gpuCards=2`、`coreCells=8`；§210-216）

## J 高级信息结构
`<details class="gpu-adv">`，**桌面/手机都默认折叠**（§32-34，已改桌面 auto-open）；summary 全程可见（CSS `::before` 箭头）；展开态按 UUID 记忆。内容：显存控制器利用率 / 功耗上限+占上限 / PCIe 最大能力 / PCI Bus ID / UUID / Compute Mode / Persistence / 运行状态 / 性能限制 / ECC。unsupported 整组隐藏。✅（`advDefaultClosed=[true,true]`）

## K Fan
风扇有值→`F.formatPercent`；N/A→不显（不显 0%）。T400 显 41%，V100 无该行。✅

## L Power
功耗 draw N/A（T400）→ 核心"--"；有值→`F.formatPower`。✅

## M Power Limit
`power_limit_w` + 当前 + 占上限（`power_percent`）。T400 显"上限 31 W"（draw N/A 不显当前）；V100 显"上限 200 W · 当前 194 W · 占上限 97%"。✅

## N P-State
`performance_state` 原样（P0/P8…）。无值不显。✅

## O Idle 修复
**根因**：1.1.2 `运行状态` 只在 `hasIdle` 或 `level!=="none"` 时显示——V100 无 throttle（0x0/空）两条件都不满足 → 不显示。
**修复**：运行状态改为**两值恒显示**（空闲 / 负载中）；性能限制只在存在真实原因（非纯 idle）时显示。
**验证**：T400=`空闲（GPU Idle）`、V100=`负载中`（DOM `runStates` 实测）。✅

## P PCIe
当前 `Gen3 x16`（meta 行）+ 最大能力 `Gen3 x16`（高级行）。任一 max 缺失则高级行不显。✅

## Q 显存控制器利用率
`memory_controller_percent`（≠ 显存占用），带 tooltip 说明两者不同。T400 8% / V100 56%。✅

## R ECC
`ecc==null`（T400）→ 整区隐藏；`ecc.enabled`（V100）→ "ECC 模式 已启用" + 可纠正/不可纠正 × 易失/累计（**0 显真值 0**）+ tooltip；`ecc.enabled==false` → "已禁用"。✅（`gpu-ecc.png`）

## S Retired Pages
`-q -d ECC` 文本解析：单比特 / 双比特 / 待处理（是/否）+ Remapped Rows。缺失→None（不占位）。✅（`test_ecc_detail_*`）

## T Selector
chips 短名（`gpuShortName` 去 NVIDIA/Tesla 前缀）+ 全名 tooltip；multi-select 保留；`flex-wrap` 禁横滚。✅

## U Util/VRAM Chart
"利用率与显存"：同 GPU 同色，**实线=利用率 / 虚线=显存占用率**；per-GPU 分组 tooltip 含 used/total 绝对值。✅（`gpu-24h-util.png`）

## V Legend
图例按 GPU 分组，颜色与卡一致；虚线标注显存。✅

## W Power Chart
"功耗"图（`chartGpuPower`）：仅功耗可用的卡建 series（T400 power N/A 不建 series / 不显图例）；tooltip 含上限/占上限。✅

## X Temp Chart
"温度"图（`chartGpuTemp`），y ℃。✅

## Y Fan Trend
"更多趋势"折叠区 → "风扇转速"图（`chartGpuFan`），数据已在 `gpu_samples` 持久化（无需迁移）。✅（`gpu-fan-trend.png`）

## Z Clock Trend
"更多趋势" → "SM / 显存时钟"图（`chartGpuClock`）。✅（`gpu-fan-trend.png`）

## AA 24h 降采样
24h 页面前端传 `max_points=1000`（短范围 2000）；后端 `_downsample` 桶聚合（**gap 不补**，桶内非空均值）。✅（`test_live_grouped_and_downsampled`：24h ≤1000 点）

## AB Data Gap
降采样保留缺口（不插 0）；gap 检测见历史页（本轮不改）。✅

## AC Energy 算法
`energy_wh = ∫ power.draw dt`（采样梯形积分，按 UUID 分卡），后端 `gpu_daily.energy_wh` 汇总。标注"估算"。✅

## AD 0Wh bug 修复
**根因**：T400 power 恒 N/A → 无功耗采样 → 旧逻辑 `energy=0.0` 显"今日能耗 0Wh"（假值）。
**修复**：后端 `power_available=any(avg_power_w 非空)`；前端 `!power_available` → 显 **"不可估算（无功耗遥测）"** 而非 0Wh。
**验证**：T400=`不可估算`、V100=`996 Wh / 1.01 kWh`（API + DOM 实测）。✅

## AE Hardware Energy Counter
仍用 power.draw 采样积分（非 hardware counter），标注"估算"；无硬件计数器回退。✅

## AF Process 数据源
`--query-compute-apps=pid,process_name,gpu_uuid,used_memory`（低频）。**`type` 字段 580 实测无效**（`Field "type" is not a valid field to query`）→ 类型不可靠，**删"类型"列**（见 AG）。`process_name` 返回**全路径** → 前端显 basename + 全路径 tooltip。✅（nvidia-smi 实测）

## AG Process Desktop
表格列 = **应用（basename，全路径 tooltip）/ PID / GPU / 显存占用**（删类型列）；排序 GPU → 显存降序 → PID；显存 N/A→"不可用"（不显 0），整列 N/A 弱化+说明。✅（`gpu-process-desktop.png`；本机 34 进程）

## AH Process Mobile
390 手机：表格转 **Process Cards**（每进程一卡：卡头=应用 basename ellipsis 可点展开全路径，其余=2 列 definition PID/GPU/显存占用）。禁横滚/禁内部 vertical scroll。✅（`gpu-process-mobile.png`；`cardified=true`、labels `[app,PID,GPU,显存占用]`、`pageHOverflow=false`）

## AI Process Memory N/A
WDDM 下 `used_memory` 常 N/A → 显"不可用"（`dim` + tooltip），不显 0。✅（DOM `mem="不可用"`）

## AJ Show More
进程 >20 默认显 20 + "显示全部 N 个"按钮，点击展开全量（不截断）。✅（DOM `procRows=20`、"显示全部 34 个"）

## AK 证明无内部 Vertical Scroll（硬性 Gate）
DOM+CSS 实测：`#gpuProc` 与 `.gpu-proc-table` 均 `overflow-y: visible`、`max-height: none`、`clientHeight == scrollHeight`（1920 `587==587`、390 `4084==4084`）→ **无内部 vertical scroll**，页面外层滚动。✅

## AL 证明 Mobile 无 Horizontal Scroll
320/390：`documentElement.scrollWidth == clientWidth`（320 `320==320`、390 `390==390`）→ **0px 横向溢出**。✅

## AM nvidia-smi 调用优化
**5 类查询、按周期**：fast 2s 一次全字段（已实现）；ECC 60s；进程低频；静态启动一次+300s 兜底。绝不每 2s 查 ECC/退役页/静态（`SLOW_QUERY_INTERVAL=60`、`STATIC_QUERY_INTERVAL=300`）。✅

## AN Polling（前端轮询门禁，in-page fetch 计数 34s，强制可见）
- `/api/gpu/status` ×6（~5s 全局节奏）
- `/api/gpu/live` ×2（15s）
- `/api/gpu/daily` ×0（60s 内不刷，符合 `visibleIntervalMs=60000`）
✅ 无重复/无 history 重复请求。

## AO Schema
**无新迁移**。fan/clock（§Y/Z）、ECC 计数、功耗、PCIe max 等都已在 `gpu_samples`（schema v5/v6 列）持久化；退役页/静态字段为 live-only（不入 `to_row`）。`CURRENT_SCHEMA_VERSION=6` 未变。✅（`test_to_row_matches_db_columns` + `test_migration`）

## AP Multi-GPU 测试
双卡同时：卡×2、核心×8、selector 双 chip、能耗双行、进程表按 GPU 分组。✅（`gpu-1920-final.png`、`gpu_sizes.json`）

## AQ Index 变化
身份锚 UUID；`test_index_reorder_identity_by_uuid` 验证乱序/重排不串卡。✅

## AR–AV 各测试（新增 8 项）
- `test_ecc_detail_retired_pages` / `_no_retired_section` / `_pending_yes` / `_remapped_single_line` / `_remapped_na`（ECC 细节解析，5）
- `test_static_query_populates_gpu_static`（静态字段）
- `test_ecc_corrected_increase_records_event`（ECC 事件 previous/current/delta/source）
- `test_ecc_event_cooldown_suppresses_rapid_repeat`（冷却 30min 不刷屏）
✅ 全部通过。

## AW 24h 性能
24h 降采样 ≤1000 点/series，纯 Python 桶聚合；前端 `max_points=1000` 控制传输量。首载 daily 聚合 48h×2s 样本在本机（后台 Edge 标签偶发休眠）首帧 ~18s，稳定后 <300ms——属数据量/环境，非阻塞（`/api/gpu/daily?days=1` 稳态 62ms/次）。✅

## AX–BA 1920/988/390/320 Edge 截图
`artifacts/gpu-r4-audit/gpu-1920.png / gpu-988.png / gpu-390.png / gpu-320.png` 全部捕获；每尺寸 `pageHOverflow=false`、进程区无内部 scroll。✅（`gpu_sizes.json`）

## BB Advanced Card 截图
`gpu-adv-expanded.png`（两卡高级区展开：显存控制器/功耗上限+占上限/PCIe 最大/PCI Bus ID/UUID/Compute/运行状态/ECC）。✅

## BC ECC 截图
`gpu-ecc.png`（V100：ECC 模式 已启用 + 可纠正/不可纠正×易失/累计 全 0 显真值 0）。T400 无 ECC 区（隐藏）。✅

## BD Fan Trend 截图
`gpu-fan-trend.png`（"更多趋势"展开：风扇转速 + SM/显存时钟）。✅

## BE Process Desktop 截图
`gpu-process-desktop.png`（应用/PID/GPU/显存占用 4 列，无类型列，无内部滚动，>20 显示全部）。✅

## BF Process Mobile 截图
`gpu-process-mobile.png`（390 Process Cards：basename 卡头 + 2 列 definition）。✅

## BG 24h 截图
`gpu-24h-util.png`（切 24h，利用率/显存 实线/虚线，≤1000 点）。✅

## BH Console（控制台门禁）
GPU 页 reload + 停留：`0 uncaught / 0 console.error / 0 console.warn`。✅

## BI Polling 检查
见 AN：status/live/daily 节奏正确，无重复、无隐藏时轮询（`visibleOnly`）。✅

## BJ 自动测试
**575 tests OK**（基线 567 + 新增 8：ECC 细节×5 + 静态×1 + ECC 事件×2）。✅（`artifacts/gpu-r4-audit/full_suite.log`）

## BK Visual
视觉验收通过：数值 primary text、状态 green/amber/red、throttle 着色（Idle 中性 / 功耗 amber / 温度 amber-red / 硬件降速 warning / 良性 Sync Boost·app 中性 secondary）、VRAM 进度条、ECC 0 显真值、进程表/卡、更多趋势图。✅

## BL Remaining Issues
1. **T400 `power_limit=31.32W` 而 `power.draw=N/A`**：T400 能报 power.limit 但不报 power.draw（驱动 580 WDDM 特性）。卡上高级"功耗上限"显"上限 31 W"（无当前），能耗区因 `power_available=False` 显"不可估算"。逻辑自洽，非 bug；保留。
2. **首载能耗延迟**：48h×2s 样本 daily 聚合首帧 ~18s（本机后台 Edge 标签偶发休眠加剧）。稳态 62ms。非阻塞，未加占位态（超本轮 GPU 页范围）。
3. **`[Insufficient Permissions]` 进程名**：nvidia-smi 对受保护进程返回该占位名，前端原样显示（basename=自身）。非本项目边界，保留。
4. **无版本变更 / 无 Commit / Push / Tag / Release**：本轮仅改 `gpu_collector.py` / `server.py` / `static/index.html` / `static/js/{app}.js` / `static/css/{pages,mobile}.css` / `tests/*` / `tools/*`；`version.py` 仍 1.1.3；无 VCS 操作。✅

---

## 交付物
- **代码**：`gpu_collector.py`（`NVSMI_STATIC_QUERY` / `parse_nvidia_smi_ecc_detail` / ECC 事件 / `gpu_static`）、`server.py`（`/api/gpu/status` 透传静态+stale、`/api/gpu/daily` `power_available`）、`static/js/app.js`（两层卡 / 高级折叠 / 运行状态恒显 / 性能限制着色 / 能耗不可估算 / 进程表+mobile 卡 / 更多趋势图）、`static/css/pages.css`（`.gd-core` / `.gd-meta` / `.gpu-adv` / throttle 着色 / `.gpu-proc`）、`static/css/mobile.css`（Process Cards）、`static/index.html`（"更多趋势" details + 能耗/进程区标题）。
- **截图**：`artifacts/gpu-r4-audit/`（1920/988/390/320 + 高级卡 / ECC / 风扇趋势 / 进程桌面 / 进程手机 / 24h / 能耗，12 张）+ `gpu_verify.json` / `gpu_sizes.json`（断言原始值）。
- **测试日志**：`artifacts/gpu-r4-audit/full_suite.log`（Ran 575 tests ... OK）。
- **辅助工具**：`tools/cdp_gpu_verify.py` / `cdp_gpu_sizes.py` / `cdp_gpu_shots.py` / `cdp_gpu_final.py` / `cdp_gpu_gate.py` / `cdp_mobile_proc.py` / `cdp_energy_probe.py` / `cdp_daily_latency.py`。
- **重启说明**：8790 服务已用新 `server.py` 重启（旧进程 PID 64348 已 kill）以加载后端改动；GPU 采集 2s、保留 48h。
