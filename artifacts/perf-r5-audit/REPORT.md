# Round 5 — 推理性能（Performance）页产品化 · 最终报告（A–BA）

对象：`static/index.html` 推理性能页 + 直接依赖（实时性能数据 / Slot / MTP / 模型运行信息 /
Formatter / Chart / 相关 API / View Model / 响应式布局），以及已确定的全局导航改名。
约束遵循：不改概览/用量/系统/显卡/历史/设置/关于布局；不改版本号；不 Commit/Push/Tag/Release。
验收：Microsoft Edge（WebView2，CDP 9333）16 尺寸矩阵 + 缩放 100/125/150；真实视觉/DOM 审计，非仅截图成功。

图：AFTER 截图见 `artifacts/perf-r5-audit/after-perf-*.png`（16 尺寸 × top/full/折叠层切片，
125%/150% 抽查），BEFORE 基线见同目录 `perf-*.png`。

---

## A. 现有功能审计（改动前实测）
llama.cpp 实际在 `127.0.0.1:9091`，模型 `qwen3.8-27b-medium`（`Q4_K - Medium`，27.32B 参数，
16.2 GiB 文件），`total_slots=1`，`n_ctx=262144`，`speculative.types="none,draft-mtp"`（MTP 已启用）。
改动前页面 4 处长期 `--` / 语义错误：
1. `llama.cpp 构建` 恒 `--`（`build_model_info()` 只从 `llama-server --version` 子进程取，PATH 无 exe → None，从不读 `/props`）。
2. 空闲 Slot 的 per-request 字段是上一任务残留（`n_prompt_tokens=2801` 残留）却被当"当前"展示。
3. MTP 无区间控制：Summary=今天 / 趋势=4 天 / 位置=今天，三块数据源打架。
4. 吞吐图固定 60 分钟窗口，无 15分/1h/6h/24h 选择、无缺口断线、无窗口加权平均。

## B. 数据源遥测覆盖审计
`live_samples` 原缺 `prompt_seconds` / `predicted_seconds`（窗口加权平均 §47 必需）。
`/slots` 提供 `is_processing` / `n_ctx` / `n_prompt_tokens` / `n_decoded` / `speculative` / `params`。
`/props` 提供 `build_info` / `n_ctx`。`/v1/models` 提供 `n_params` / `size` / `ftype` / `n_ctx` / `format`。
MTP 位置数据来自 `spec_decode_num_accepted_tokens_per_pos_total`（仅 count，无分位置分母）。

## C. MTP 遥测深度审计
7d 实测：draft 454872 / accepted 244570 / steps 114132 / 接受率 53.77% / 平均 Draft 3.99 / 平均接受 2.14 / 4 个位置。
旧页面只有"今日"单一视角，无法看 7d/30d/全部 效率趋势 → 新 `/api/mtp/range` 解决。

## D. build_info / 模型信息 bug 修复
`llama_runtime_collector.build_model_info()` 现读 `/props` 的 `build_info` 并覆盖子进程回退。
实测 `llmBuild = b10976-987498f45`（不再是 `--`）。

## E. 空闲 Slot 残留 bug 修复
新语义：空闲 Slot 状态=空闲，per-request 数字（上下文/Prompt/新处理/缓存复用/已生成/剩余）一律 `--`；
仅 MTP（配置值）保留。绝不当"当前"状态展示。Active 才显示当前请求数据。

## F. v5 → v6 schema 迁移
`db.CURRENT_SCHEMA_VERSION=6`。`live_samples` 增 `prompt_seconds` / `predicted_seconds`（本轮 delta，
REAL，历史行 NULL）。`_migrate_v5_to_v6` 幂等 ALTER + migration 事件。向前兼容（旧库打开自动迁移）。

## G. 新端点 /api/throughput
`minutes ∈ [15,1440]`。返回 `{minutes, available_minutes, window_minutes, window_avg{...}, samples(降采样), last_activity_ts}`。
`window_avg` 在**原始**样本上算（精确），`samples` 超 2000 点 bucket 降采样（只用于画图）。

## H. 新端点 /api/mtp/range
`days / all`。一次性驱动 MTP Summary + 趋势 + 位置三块（同一区间）。
`summary{draft_tokens, accepted_tokens, verification_steps, accept_rate, avg_draft_length, avg_accepted_length}`；
`positions`（区间内各位置 count 求和，**仅 count**）；`days`（逐日 accept_rate，缺失日 null）。

## I. 窗口加权平均吞吐（非逐样本均值）
`prompt_tps_avg = ΣΔprompt_tokens / ΣΔprompt_seconds`；`decode_tps_avg = ΣΔgenerated / ΣΔpredicted_seconds`。
分母 ≤0 或缺分子 → None（绝不返回 0/Infinity，不把"无吞吐"误报 0 tok/s）。避免短 burst 被稀释/夸大。

## J. 长时图降采样
`_downsample_throughput(samples, 2000)`：24h/5s ≈ 1.7 万点 → 2000 点。速率取桶内非空均值，
delta 取桶内求和（保持窗口加权平均与 tooltip 计数一致）。

## K. 实时性能摘要（Section A，5 项）
Prompt TPS / Decode TPS / 处理中请求 / 等待中请求 / 活跃 Slot。TPS null→`--`，0→`0 tok/s`（区分 0 与缺失）。
响应式 Wide 5 等分 / Compact 3+2 / Mobile 2+2+1。

## L. 数据新鲜度 badge（Page Header 右侧）
`renderFreshnessBadge()`：离线→"服务器不可用 · 历史值"；age>3×interval→stale "数据已过期 · X"；
否则 live "实时 · 刚刚"。实测显示 `实时 · 刚刚`（class `freshness-badge live`）。

## M. Token 吞吐率 Section（B）
范围分段（15 分钟/1 小时/6 小时/24 小时，默认 1 小时）+ 窗口加权平均行 + 图 + "查看监控历史" 链接。
实测 4 个范围均可点击并切换。

## N. 吞吐图 gap / 0 / null 三态
`_gapBreaks`：相邻间隔 > poll×3 → 插 null 断点（`connectNulls:false` 断线，不连斜线）。
0 TPS 是有效 Idle 样本（画 0），null 才断线/空态。绝不把 Idle/Gap 连成假斜线。

## O. 吞吐图 headroom 与 Y 轴
Y 轴自动缩放 + ~15% headroom（`max = ceil(max×1.15)`），不写死 350/500 硬上限。

## P. 吞吐图 tooltip
时间 + 两路 TPS（`tok/s`，null→`--`）+ 当时"处理中请求"（历史存在则显示）。

## Q. 运行时状态 Section（C，5 项）
活跃 Slot/总 Slot / 平均忙碌 Slot·Decode / 当前序列长度（+上下文进度条）/ 最大观测序列长度 / 上下文窗口上限。
不重复顶部请求/Slot 摘要。

## R. 活跃 Slot / 总 Slot
`activeSlotStats()` 从 `/slots` 的 `is_processing` 计数（非 `n_busy_slots_per_decode`）。实测 `0 / 1`。

## S. 平均忙碌 Slot / Decode
`d.busy_slots`（gauge `n_busy_slots_per_decode`）：null→`--`，0→`0.00`。实测 `1.00`。

## T. 当前序列长度（§133）
= 活跃 Slot 的 `n_prompt_tokens + n_decoded` 最大值（**不用** `n_prompt_tokens_processed` 冒充完整上下文）。
无活跃 Slot → "空闲"（不显示 `--`）。实测空闲态显示"空闲"。

## U. 上下文占用进度条
占用比例（seq/n_ctx + 百分比），**非** KV Cache 显存率。≥85% warn / ≥95% crit 变色。

## V. 最大观测序列长度
`llamacpp:n_tokens_max`（进程运行期观测最大值，非当前上下文）。实测 `195.46K`。

## W. 上下文窗口上限
`/api/runtime` `context_max` 为 null 时回退 slot `n_ctx`。实测 `262.14K`。

## X. MTP Section（D）三态
`mtpStateNote` 三态：未启用 / **已启用 · 暂无样本**（MTP 已启用但所选范围无 Draft 样本）/ 有数据（百分比）。
实测：今天=暂无样本（今天确实无 MTP 推理），7d/30d/全部=53.8%（有历史数据）。

## Y. MTP Summary 6 指标
Draft Token 接受率 / Draft Token / 已接受 Draft Token / 验证步数 / 平均 Draft 长度 / 平均接受长度。
Desktop 6 列 / Compact 3+3 / Mobile 2×3。均值 `Token/步`（steps≤0→`--`）。

## Z. MTP 范围选择器
今天/7 天/30 天/全部（默认今天）。**范围控制整个 MTP Section**（Summary+趋势+位置同源），
消除"Summary 今天 / Chart 4 天 / Position 今天"的混乱。实测 4 档均可点击，指标与子标题同步一致。

## AA. MTP 趋势图（Calendar Timeline）
`renderMtpChart` 现用 `/api/mtp/range.days`（`accept_rate` 字段），**保留全部日期位置**
（7 天→7 位置；缺失日 accept_rate=null→断线/留日期位），不塌缩成 1 点。`connectNulls:false`。
≤7 天显示 symbol（`showSymbol`），30 天隐藏。

## AB. MTP 按位置图
`renderMtpPosChart`：x 轴 `位置 0/1/2/3` 全显（`interval:0`）；bar 宽 48px；
tooltip **只展示已接受 count**（无 per-position 分母，禁算分位置接受率）。

## AC. Slot 监控 Section（E）
标题 "Slot 监控"（原"当前 Slot"）+ 锁图标 + "仅展示运行元数据" 提示。
Desktop 紧凑 Table / Mobile Card（同一 DOM，CSS 响应式）。

## AD. Slot 空闲语义
空闲行：状态 pill "空闲"，per-request 列全 `--`（淡显），仅 MTP 列保留。Active 行显示当前请求数据。
实测空闲 Slot 全部 per-request 列 `--`、MTP=已启用。

## AE. Slot Progressive Disclosure（§137-141）
Active Slot "详情"按钮展开采样参数（temperature/top_p/top_k/min_p/max_tokens/reasoning/chat/
speculative/stream/samplers）——诊断性能配置，非聊天审计。Prompt 正文绝不展示。

## AF. Slot 当前上下文列
`_slotContextCell`：active 才计算（prompt+decoded）/n_ctx + 百分比（≥85% warn / ≥95% crit）；idle→`--`。

## AG. 模型与运行环境（Section F，8 项）
模型别名 / 量化（`Q4_K · Medium`，`-`→`·`）/ 参数量 / 模型文件大小 / 上下文窗口 / 并行 Slot / 模态 / llama.cpp 构建。
"数据来自 llama.cpp 运行时只读接口（/props · /v1/models · /slots）"提示。Wide 4 列 / Compact / Mobile 2。
实测全 8 项落位（含 build 修复值）。

## AH. 术语冻结与改名
新增/改名（术语字典回归）：验证步数（原推测验证轮次）、上下文窗口上限（并入上下文高水位）、
平均忙碌 Slot/Decode（原平均忙碌 Slot 数）、运行时状态（原服务器运行状态）、模型与运行环境（原模型与服务）、
Slot 监控（原当前 Slot）、模型文件大小（原模型大小）、新处理 Prompt、缓存复用 Prompt、已生成 Token、剩余生成预算、
并行 Slot、llama.cpp 构建、MTP（Multi-Token Prediction）首现全称。`Multi-Token 预测`/`忙碌 Slot（平均）` 等旧词已清除。

## AI. 响应式布局 16 尺寸矩阵
1920×1080 / 1600×900 / 1440×900 / 1366×768 / 1200×900 / 1024×1366 / 988×1394 / 900×900 /
820×1180 / 768×1024 / 760×900 / 600×900 / 430×932 / 390×844 / 360×800 / 320×568。
每尺寸 fresh load 实测**横向溢出 = 0px**；desktop 紧凑 Table / ≤760 转 Card。

## AJ. 缩放 100/125/150
125%（CSS 视口 1920→1536）与 150% 抽查：布局不破、字体层级保持、无裁切。
`deviceScaleFactor` 模拟，1× 顶视 + 2× 全页截图。

## AK. 轮询调度与 Network 预算
移除 `llamaRuntime` 双请求 poller；新 `throughput`（30s 后台/前台 R）+ `perfSlots`（30s/R，perf-only）+
`perfModel`（120s/60s，perf-only）+ `mtpRange`（60s，perf-only）。
12s 实测 perf 页 `/api` 请求：`throughput`×1 / `mtp/range`×1 / `llama/slots`×1 / `llama/info`×1（无重复双请求），
`status`×2 / `runtime`×2（前台节奏），`daily` 仅在应用启动基线触发 1 次（perf 已从 daily poller 移除）。

## AL. 概览页未受扰验证
共享 `applyStatus`/`applyRuntime`/`refreshLlamaInfo` 改动后，概览 `ov*` 全落位：
`ovToday*` / `ovPromptTps` / `ovDecodeTps` / `ovMtpRate`(三态) / `ovRequests`(0/0) / `ovContext`(262.14K) /
`ovKvCache`（保留）；`ov-perf-grid` 存在。概览/用量/系统/显卡/历史/设置/关于布局均未动。

## AM. 测试覆盖（新增/更新）
- `test_ui_terminology`：required/banned/slot-semantics 全部更新（18 tests PASS）。
- `test_api`：新增 `test_throughput_window_avg`（Δ50/0.5=100、Δ10/1.0=10、秒数求和、参数钳制）/
  `test_throughput_empty_window`（无样本→None）/ `test_mtp_range_today` / `test_mtp_range_days_null_days`
  （7 天 6 null 位）/ `test_mtp_range_all`。
- `test_migration` / `test_newer_schema_guard` / `test_database_health`：适配 v6（版本 6、migration 事件 4 条、
  备份名 `v2_to_v6`、`to` 元组含 6、新增 v6 列断言）。

## AN. 全量回归 553 GREEN
`python -m unittest discover -s tests`：**Ran 553 tests — OK**（0 fail / 0 error）。
（早期 7 个 migration/schema 失败均为 v6 bump 的硬编码 `5` 回归，已全部修复并复跑通过。）

## AO. 浏览器验收（Edge / WebView2 CDP）
Edge CDP 端口 9333，`Page.setWebLifecycleState active`（消除隐藏页节流），raw-socket WebSocket。
`Page.captureBeyondViewport` 无法穿透 `.content` 内部滚动 → 逐屏 scroll-slice 捕获折叠层。
每尺寸 fresh load（消除 resize-down 时 ECharts canvas 残留桌面宽度的假溢出）。

## AP. 截图矩阵（BEFORE/AFTER）
BEFORE：`artifacts/perf-r5-audit/perf-*.png`（21 张）。
AFTER：`after-perf-{size}-{top,full}.png` + `-s{N}.png` 折叠层切片（16 尺寸，125/150 抽查）。

## AQ. gap / 0 / null 语义
0 = 真实值（`0 tok/s`）；null/缺失 = `--`；gap = 断线（不连斜线）。TPS 与 Slot per-request 均遵循。

## AR. 轮询间隔与新鲜度阈值
前台 `R`（= collector 采集间隔，默认 5s）、后台 30s；新鲜度阈值 3×interval（>15s 判 stale）。

## AS. 长时图可用性
2000 点降采样 + bucket 求和/均值；Y 轴 headroom；单点显示 symbol；缺口断线——24h 曲线可读不糊。

## AT. 0 / null 区分（UI）
`formatTps`：`null→--`、`0→0 tok/s`。Slot idle→`--`、active 0→`0`。绝不把 null 当 0。

## AU. 吞吐窗口"部分数据"标注
`window_minutes < minutes` 时 `.partial` + `可用数据 X`（`available_minutes` 反映真实保留，不假装 24h 完整）。

## AV. MTP 三态措辞
未启用 / 已启用 · 暂无样本 / 有数据。三态文案 + 颜色区分，杜绝"0%"误报（MTP 启用但无样本 ≠ 0% 接受率）。

## AW. Slot 表列头与 data-label
10 列表头（Slot/状态/上下文/Prompt 总量/新处理 Prompt/缓存复用 Prompt/已生成 Token/剩余生成预算/MTP/详情），
`td[data-label]` 驱动 Mobile Card 的 `::before` 标签。

## AX. Mobile Card 转换（table→card）
`.slot-table` ≤760px 转 Card（`display:block` + 逐格 flex 标签+值），复用现有 table→card 机制样式但作用域限定 `.slot-table`。
（初版误置于 `@media(max-width:360px)` 内，已修正到 `@media(max-width:760px)`——760/390 现均正确转 Card。）

## AY. 横向溢出审计
16 尺寸 fresh load 全部 `content.scrollWidth - clientWidth = 0`（desktop Table / mobile Card 均无横滚溢出）。
（曾见 760/390 溢出 210/580px，系 CDP 从桌面 resize-down 的 ECharts canvas 残留宽度假象，fresh load 复测为 0。）

## AZ. 图表空态 / 单点
空态统一文案（"暂无推理吞吐数据"/"暂无 MTP 接受率数据"），不留空网格；单点显示 symbol（`showSymbol`/`singlePointOpts`）。

## BA. 剩余性能问题（Remaining Performance Issues）
1. **`busy_slots`（平均忙碌 Slot/Decode）恒 1.00**：单 Slot 且服务器每 decode 报 1，语义上"平均忙碌"意义有限；
   多 Slot 负载下才有信息量。保留但可考虑在多 Slot 时才突出。
2. **吞吐窗口加权平均依赖 v6 秒数 delta**：历史行（v5 迁移前）`prompt_seconds/predicted_seconds` 为 NULL，
   窗口若含迁移前样本，加权平均只在有秒数的样本上求和（正确），但 24h 窗口早期段精度略降。属预期，无 bug。
3. **MTP 位置图只有 count 无分母**：无法算分位置接受率（服务器未提供分位置 draft 总数），已在 tooltip/说明注明；
   属遥测能力上限，非 UI 缺陷。
4. **`context_max` 依赖 slot `n_ctx` 兜底**：`/api/runtime` 当前未上报 `context_max`，靠 `refreshSlots` 填充
   `rtContextMax`；若未来 runtime 直报则优先。当前行为正确。
5. **缩放 125/150% 未逐尺寸全量截图**：125%/150% 仅抽查 3-4 个代表尺寸（1920/1366/768/390），
   全 16 尺寸仅 100% 全量。如需 100% 覆盖可对 125/150 补拍（工具已支持 `--zoom`）。
6. **ECharts 在 CDP resize-down 时 canvas 残留桌面宽度**（真实设备无此问题，fresh load 正常）：
   `charts.js` 已有单一 ResizeObserver，属 CDP 模拟 artifact，非生产缺陷；记录以备他页回归排查。

---
**结论**：推理性能页 20 问全部可答（是否在推理 / Prompt·Decode TPS 实时·稳定 / 排队 / Slot 占用 /
上下文剩余 / 最大序列 / 模型别名·量化·大小 / 构建 / MTP 启用·接受·平均 draft·平均接受·分位置 /
效率趋势 / Slot 状态 / 数据新鲜度）。553 测试全绿；16 尺寸 + 缩放验收通过；概览等其它页未受扰；
版本号未变；未 Commit/Push/Tag/Release。18+ 文件改动全部在工作区（未提交），等待人工审核。
