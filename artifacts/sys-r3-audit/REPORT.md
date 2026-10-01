# LlamaMonitor Round-3 系统页产品化 — 验收报告（A–BD）

**范围**：系统页 IA（概览/CPU/内存/存储/网络/功耗/硬件传感器/硬件信息）+ 直接依赖：
系统 Telemetry 采集、系统历史数据、硬件传感器、功耗与能耗、Formatter、Charts、
必要的数据聚合、必要低风险 Schema、响应式布局。
**约束遵守**：不改概览/用量/性能/显卡/历史/设置/关于页布局；主导航"系统"（两字）；
无版本变更；无 Commit / Push / Tag / Release。

**验证环境**：2× Intel Xeon Platinum 8168 @ 2.70GHz（48 物理/96 逻辑），255.7 GiB RAM，
Supermicro X11DPi-N，BIOS 4.7，Windows 11 Pro for Workstations Build 26200.9457，x64，
主机 DESKTOP-8NAGU8B。C: 1906.6 GiB（55%，851.9 GiB 可用）+ D: 3726.0 GiB（16%，3116.2 GiB 可用）。
传感器 Provider 状态 available，映射 2 项（cpu|load、memorycontroller|load），0 风扇，
无 CPU Package 温度/功耗/存储（counts {cpu:49, motherboard:2, cooling:0, storage:0}）。

---

## 页面使命 19 问（全部有答案）

- CPU 忙？→ 概览 + CPU 区当前利用率（per-core mean）。
- 长期满载？→ CPU 趋势图 + 当前/平均/峰值摘要。
- 频率？→ 当前频率 + 基准频率（1.20 GHz / 基准 2.70 GHz）。
- 温度？→ Package 温度（能力自适应，本机不可用→隐藏）。
- Package 功耗？→ CPU 区 + 功耗区（本机不可用→隐藏）。
- 逻辑处理器数？→ Heat Grid 96 格（固定 index 顺序）+ "96 个逻辑处理器"。
- 内存使用？→ 内存区（使用率/已用/可用/总计）+ 趋势图。
- 磁盘？→ 存储区磁盘 I/O 图 + 卷容量。
- 哪个卷满？→ 卷容量进度条（<15% 琥珀 / <5% 红）。
- 网络？→ 网络区（接收/发送/接口/链路速度）。
- 哪个接口？→ 接口选择器（自动=默认路由 WLAN/可切换 10 接口）。
- 哪个组件功耗？→ 功耗区分项（CPU Package / GPU 合计）。
- 今日能耗？→ 功耗区"今日监测组件能耗（估算）"。
- 传感器？→ 硬件传感器区（结构化分组 + 查看全部）。
- 风扇 RPM？→ 风扇（真值，0 RPM 显示 0，control % 仅真实 Provider）。
- 风扇 %？→ 同上（不虚算）。
- 硬件？→ 硬件信息（OS/Build/架构/主机/CPU/插槽/核心线程/RAM/主板/BIOS）。
- 数据新鲜度？→ 页面"采集正常"徽章 + 概览最后更新。
- （第 19）整机功耗？→ "未配置"（无外部功率计，非 --，含说明）。

---

## A 页面信息架构顺序
`系统概览 → CPU → 内存 → 存储 → 网络 → 功耗与能耗 → 硬件传感器 → 硬件信息`，
与规格 A–I 一致。✅

## B 页面标题 + 副标题
`<h1>系统</h1>`；副标题"监测 CPU、内存、存储、网络、功耗与硬件传感器。"+ 采集状态徽章。✅

## C 历史范围选择器
15 分钟 / 1 小时 / 6 小时 / 24 小时；默认 1 小时；切换触发 `refreshLive()` 重绘全部趋势图。✅（功能验证：切 24h 后三张趋势 canvas 均在，摘要随范围更新）

## D 系统概览 6 项
CPU 利用率 / 内存使用率 / 磁盘 I/O(↓读↑写) / 网络吞吐(↓收↑发) / 监测组件功耗 / 系统运行时间。
Wide 6 列 → Compact 3×2 → Mobile 2 列。✅（DOM 实测 6 项值均填充）

## E 概览 CPU 利用率 = per-core mean
本机实测 6%（96 逻辑核均值），**非** `psutil.cpu_percent(None)` 的 group-0 2× 偏差。✅

## F 概览内存 used / total 次值
"112.4 GiB / 255.7 GiB"（formatMemory，GiB 自动）。✅

## G 概览磁盘 ↓读 / ↑写 双行
`↓ 7.7 KB/s` / `↑ 197 KB/s`（单侧缺失该侧 --，不清空整卡）。✅

## H 概览网络 ↓收 / ↑发 双行
`↓ 464 KB/s` / `↑ 6.0 KB/s`。✅

## I 概览监测组件功耗 + 次值
"195 W" + "1 个组件可读取 · CPU Package 不可读（总值非整机）"（部分数据提示）。✅

## J 系统运行时间格式
`1 天 0 小时`（统一 §29 格式：<1h "分/秒"、<24h "小时/分"、≥24h "天/小时"）。✅

## K CPU 区 summary 4 项
当前利用率 / 当前频率 / Package 温度 / Package 功耗（能力自适应）。✅

## L 当前频率 vs 基准频率 区分
"1.20 GHz" + 次值"基准 2.70 GHz"（current ≠ base，不都叫"CPU 频率"）。✅

## M Package 温度 / 功耗 能力自适应
本机 null → 淡显"不可用"（`.cap-hidden`），不长期占主位 --。✅（DOM 实测两项均"不可用"淡显）

## N CPU 利用率趋势图
细线 1.5px + 极轻 area（opacity 0.06），无"实心蓝墙"；y 0–100。✅

## O CPU 图 header 当前/平均/峰值
`当前 6% · 平均 28% · 峰值 100%`（后端 `summary`，原始样本算，准确）。✅

## P 逻辑处理器利用率 Heat Grid
固定 CPU Index 顺序（`for i in perCore`，不按利用率重排）；本机渲染 **96 格**；
Cell = 序号 + 百分比。✅（DOM `heatCells=96`）

## Q Heat Grid 桌面展开 / 移动折叠
`data-auto-fold`：桌面 open，≤760px 收起。✅（mobile 审计 `heatFoldOpen=false`）

## R Heat Grid 配色
100% 不直接 error red（high=accent / hot=warning）；0 数据=中性。✅

## S 内存 4 项
使用率 / 已用 / 可用 / 总计（44% / 112.4 GiB / 143.3 GiB / 255.7 GiB；Windows Available 语义）。✅

## T 内存趋势图（% + used tooltip）
主图单系列 %；tooltip 给"已用 / 总量"。✅（canvas 渲染，无历史时隐藏）

## U 内存图 header 当前/平均/峰值
`当前 44% · 平均 45% · 峰值 45%`。✅

## V 存储磁盘 I/O 图
读取=蓝 / 写入=紫；tooltip 自动 KB/s~GB/s。✅

## W 卷容量
C: / D: 两行，GiB/TiB 自动；进度条 <15% 琥珀 / <5% 红；无假"磁盘健康"文案。✅（`diskListRows=2`）

## X 网络接口选择器
`自动 · WLAN` + 10 个适配器；`000005`(WireGuard)、`vEthernet`、`Loopback` 标注"虚拟/VPN"。✅

## Y 网络 接收/发送/接口/链路速度
接口"WLAN"、类型"Wi-Fi"、链路"433 Mbps"、接收/发送速率。✅

## Z 网络无盲目全 NIC 求和
速率口径 = 默认路由主接口（WLAN）；回退全接口合计时 `network_interface=None` 并提示"可能含虚拟网卡"。✅

## AA 网络图
接收=蓝 / 发送=绿。✅

## AB 功耗 监测组件功耗 部分求和
"195 W"（= GPU 195W；CPU Package 缺失不参与求和，非 0 填充、非 null）。✅

## AC 功耗 CPU Package / GPU 分项
CPU Package "--"（不可用）/ GPU "195 W"。✅

## AD 整机输入功耗 = 未配置
"未配置"（非 --）+ 次值"需外部功率计 / UPS / 智能插座 / BMC"。✅

## AE 今日监测组件能耗（估算）
"731 Wh"（标注"估算"）。✅

## AF 功耗趋势图
单系列"监测组件功耗"；无历史时显示空态并隐藏。✅（canvas 存在）

## AG 硬件传感器状态
`● 传感器可用`（Provider available；去掉"高级"字样）。✅

## AH 传感器结构化分组
温度 / 风扇转速 / 功耗 / 其它（Load 类归主区不重复；无逗号 raw 串）。✅

## AI 风扇 RPM 真值 / 0 RPM / control %
RPM 只显真值（0 RPM 显示 "0 RPM"）；control % 仅 Provider 真实提供时显示，不从 RPM 推算。✅

## AJ 查看全部传感器
结构化表（硬件 / 传感器 / 类型 / 当前值），`<details>` 折叠。✅

## AK 硬件信息 OS 展示
"Windows 11"（非 "Windows-11"）+ 次值"Build 26200.9457"。✅

## AL 硬件信息架构
"x64"（AMD64→x64）。✅

## AM 硬件信息 CPU
"Intel Xeon Platinum 8168"（清理型号）+ 基准"2.70 GHz" + 插槽"2" + 核心/线程"48 / 96"。✅

## AN 硬件信息 内存 / 主板 / BIOS
"255.7 GiB" / "Supermicro X11DPi-N" / "4.7"。✅

## AO 刷新硬件信息按钮
`btnRefreshInventory`，点击 `?manual=true` 重读库存 + toast。✅

## AP Formatter
formatBytes（B→KB→MB→GB→TB）、formatMemory（MiB/GiB/TiB 自动）、formatPercent、
formatTemp、formatPower、formatEnergy、formatDuration、formatAgo、formatTime。✅

## AQ §250 配色纪律
蓝=CPU/读取/接收，紫=写入，绿=发送/健康，琥珀=预警，红=真实异常；CPU 100% ≠ 红。✅

## AR §5 CPU 当前 vs 历史 last 点一致性（根因 + 修复）
根因：`psutil.cpu_percent(None)` 在双路 Xeon 只读 processor group 0，实测恒为
per-core mean 的 ~2×（8/8 背靠背样本确认：socket0=满值、socket1=0）。
**修复**：整机 `cpu_usage_percent = mean(per_core_percent)`，per-core 缺失回退聚合值。
历史行仍为旧语义（48h 保留内自愈，24h 峰值 100% 即旧数据）。✅（单元 + 集成测试覆盖）

## AS 后端 downsample 字段 bug 修复
旧 `_downsample` 只迭代 `_GPU_LIVE_FIELDS` → 24h 系统字段全 None。
修复：`fields=` 参数 + `_SYS_LIVE_FIELDS`；GPU 端点自动检测（其 clean 行仅含 GPU 字段）。✅

## AT 后端网络 double-count 修复
`psutil.net_io_counters()` 全接口求和 → WireGuard 隧道双重统计。
修复：持久化速率 = 默认路由接口（Get-NetRoute，60s 缓存）；per-adapter 基线供选择器；
回退全接口合计（`interface=None`）。✅（单元验证 per-adapter 速率）

## AU Schema v6 未变（低风险）
本轮**无 v7**：`network_interface`、`cpu_base_frequency_mhz` 为 live-only 字段（不入 `to_row()`）；
内存历史本就在 `system_samples` 持久化。✅

## AV 测试套件
**567 tests OK**（Round-5 基线 553 + 新增 14：collector CPU mean/fallback + 模块 helper、
API status 新字段/network-interfaces/live summary+memory bytes/max_points 边界）。✅

## AW 控制台门禁
**0 uncaught / 0 console.error / 0 console.warn**（reload → 系统页 12s 稳定）。✅

## AX 网络门禁
- 系统页 26s：`status` ×3（≈5s 节奏）；`live/sensors/inventory/network-interfaces` ×0
  （无 history 重复、无 static 重复）。
- 概览页 9s / 用量页 7s：system 端点 ×0（隐藏/非系统页不轮询）。
（`visibleOnly` 轮询在 `document.hidden` 时跳过，符合设计；强制可见后节奏正确。）✅

## AY 响应式尺寸矩阵 + zoom
17+ 尺寸（320×568 … 3840×2160，含 760/761/988/1053/2560/3840 断点）top+full 全捕获；
zoom 100/125/150。每尺寸独立 fresh load（避免 resize-down 假溢出）。✅

## AZ 移动端无横向溢出
320/360/390 内容 `scrollWidth == clientWidth`（**0px 溢出**）；
概览/CPU/内存/网络/功耗 grid → 2 列，kv → 1 列，Heat Grid 折叠。✅（320 溢出已由 `#page-system` 400px 规则修复）

## BA 概览页主机概况卡未回归
`ovHostCpu/Mem/Disk/Net/Power` + 次值 + 模型行仍正确渲染（system.js 改 refreshStatus 后复核）。✅

## BB 其它页布局未动
概览/用量/性能/显卡/历史/设置/关于 页面结构未改（仅共享 `.host-dual`/`stat-grid` 类，选择器已作用域化）。✅

## BC 导航"系统"（两字，顺序）
Desktop 概览→用量→性能→系统→显卡→历史；Mobile 概览→用量→性能→显卡→系统；
主导航标签两字"系统"。✅（terminology 测试断言）

## BD 无版本变更 / 无 Commit / Push / Tag / Release
本轮仅改 `system_collector.py` / `server.py` / `static/index.html` / `static/js/{system,charts,app}.js` /
`static/css/pages.css` / `tests/*` / `tools/*`；版本号未变；无 VCS 操作。✅

---

## 交付物
- 代码：见上"BD"。
- 截图：`artifacts/sys-r3-audit/after-sys-*.png`（20 尺寸矩阵 + 切片，115 张）+
  `artifacts/sys-r3-audit/final-sys-*.png`（必需要求 1920/988/390/320 top+full + 390 zoom 125/150）。
- 测试日志：`artifacts/sys-r3-audit/full_suite.log`（Ran 567 tests ... OK）。
- 辅助工具：`tools/cdp_sys_console.py`、`cdp_sys_shots.py`、`cdp_sys_func.py`、
  `cdp_sys_netgate3.py`、`cdp_sys_mobile.py`、`cdp_overview_check.py`、`sys_smoke_testclient.py`。
