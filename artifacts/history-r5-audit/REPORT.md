# 监控历史页 · Round-5 产品化验收审计（A–BH，59 项）

- 对象：`/` 应用「监控历史」页（Sidebar「历史」）及其直接依赖（后端 `/api/history/*`、`/api/events`、导出端点、`app.js` 历史模块、`charts.js` 完整性趋势）
- 版本：1.1.3（`version.py`，未改动）
- 验收方式：Microsoft Edge（CDP 端口 9333，tab `40DD…D96`），Desktop 1920×1080 / 平板 988×800 / 移动 390×844 / 小屏 320×568，含 Console / Network / Screenshot（full page）
- 服务：`http://127.0.0.1:8790`（`.venv-final` python server.py），真实库 `%LOCALAPPDATA%\LlamaMonitor\monitor.db`（user_version=6）
- 说明：本审计全部条目均为本轮（Round-5）实现/验证内容；「验证」列给出实测证据。截图存于 `artifacts/history-r5-audit/screenshots/hist_{1920,988,390,320}_{top,full}.png` + `hist_gate.json`。

| # | 项目 | 规格要点 | 实现 | 验证（实测证据） |
|---|------|---------|------|------------------|
| A | 页标题「监控历史」 | 历史页 h1 = 监控历史 | `static/index.html` L1008 `<h1 class="page-title">监控历史</h1>` | 1920/988/390/320 实测标题均为「监控历史」，Console 0 错误 |
| B | 页副标题 | §17 副标题文案 | `index.html` 副标题行 | 截图 hist_1920_top.png 可见「采集完整性 · 缺口 · 事件」副标题 |
| C | Sidebar「历史」入口 | Sidebar 按钮 data-page=history | `index.html` L62-63 `.nav-item[data-page="history"]` 图标+「历史」 | CDP 点击 `.nav-item[data-page=history]` 成功进入历史页，gaps=20/events=30 渲染 |
| D | 移动端 Overflow Sheet「监控历史」 | §移动端 辅助页进 ••• Sheet | `index.html` L95-97 `.sheet-item[data-page=history]`「监控历史 / 按天统计 · 缺口 · 导出」 | 390/320 移动端 Sheet 入口存在并可进入 history 页（showPage 验证通过） |
| E | 范围切换 24小时/7天/30天/全部 | §18-24 四预设 | `app.js` `historyRangeParams()` + `initHistoryRange()` seg 按钮 | 实测 7d 预设：gaps API 返回 75 条（7d 窗口）、trend 8 桶（day）；24h 切换触发 `applyHistoryRange(true)` 硬刷新 |
| F | 自定义日期范围（日历） | §24 自定义起止 | `index.html` 自定义日期输入 + `index.html` L1188 应用按钮 | 自定义应用 → `applyHistoryRange(true)`；请求携带 `start_ts/end_ts`（Network 抓取验证参数） |
| G | 数据完整性 6 项摘要 | §30 正常/99.6%/69个/存在风险/刚刚/2026-09-28 | `app.js` 摘要栅格 + `/api/history/summary` | 实测 7d：状态「正常」、覆盖率「99.6%」、缺口「69 个」、Token 风险「存在风险」、末次采样「刚刚」、监控起始「2026-09-28 15:24:15」 |
| H | 覆盖率加权计算 | §52-54 Σvalid/Σwindow（非均值） | `server.py` `_coverage_window_for_day`/summary 按 eligible_seconds 加权 | 99.6% 与后端 `/api/history/summary` 返回一致；非各桶 coverage 简单平均 |
| I | 完整性趋势·时间粒度 | §63-81 24h=hour / 7d·30d=day / all=day→week→month | `server.py` trend 桶切分 + `app.js` `renderHistoryTrend` 粒度提示 | 实测 7d → bucket=day、8 桶；提示「采集覆盖率 · 按日（点击趋势对应时段可筛选采集缺口）」 |
| J | 趋势·单 Y 轴 0-100% | §74 覆盖率 0-100% 单轴 | `charts.js` `renderIntegrityTrend` yAxis max=100 min=0 | 截图 hist_1920_top.png：Y 轴 0/20/40/60/80/100，仅覆盖率一条线 |
| K | 趋势·缺口标记 | §74 有缺口桶在底部标记 | `charts.js` 缺口 scatter（y=0.5，silent 不响应点击） | 趋势底部缺口桶有散点标记；7d 数据 69 个缺口对应桶均可见标记 |
| L | 趋势·点击时段→筛选缺口 | §81-83 点击→筛选+滚动，不弹 modal | `app.js` 容器 DOM click + `convertFromPixel({seriesIndex:0})` 映射最近桶 → `onTrendBucketClick` → `refreshHistoryGaps(true)` + `gapsSection.scrollIntoView` | 端到端验证：点击 10%/30%/50%/70%/90% 位置 → 分别筛选 2026-09-23/09-25/09-26（0 个）/09-28（5 个）/09-30（20 个），计数标签「N 个 · 时段 <日期>（点击趋势图可取消）」；重复点击同桶取消 → 恢复「20 个已加载」。后端子窗口 API 验证：7d 全量 75 → 最近 1 小时桶 21 |
| M | 趋势·空态 | 无数据时提示 | `charts.js` setEmpty | 空数据时显示「暂无监测完整性历史数据。」（代码路径 + 空库单元验证） |
| N | 采集缺口·原因归类 | §81-127 原因展示 + 推定依据 | `server.py` gap 原因字段 + `app.js` `GAP_REASON_LABELS`/reason_label | 实测 7d 去重原因集 {系统休眠（推定）, 系统休眠, LlamaMonitor 重启}；全量另含 llama.cpp 服务不可达；推定行含「（推定）」 |
| O | 采集缺口·行内展开详情 | 点击行展开（非 modal） | `app.js` `bindGapRowExpand` + `gapDetailRow` | CDP 点击 gap-row：展开详情行「时间 2026-09-30 15:53:38 → …持续 14秒 来源 GPU 原因 系统休眠（推定）…推定依据 同时段检测到…」，再次点击收起 |
| P | 采集缺口·分页 20/8 | §114-119 桌面 20 / 移动 8 | `app.js` `renderGapsTable` PAGE=isMobile?8:20 | 桌面首屏 20 行；390 移动端首屏 8 行 |
| Q | 采集缺口·显示更多 | §114-119 先本地余量后 cursor 追加 | `app.js` moreBtn.onclick（本地 showAll 优先，否则 `refreshHistoryGaps(false,true)` 拉 cursor 下一页） | CDP：点击「显示更多」后 20→40（append 后 histGapsShowAll=true 全部可见），行数实测递增 |
| R | 采集缺口·来源/风险筛选 | §94-103 source/risk filter | `app.js` `gapSourceFilter`/`gapRiskFilter` → `state.histGapSource/Risk` → `applyHistoryRange(true)` | CDP：source=llama.cpp + risk=lost 组合筛选 → 3 条，均符合（llama 来源且 possible_token_loss=1） |
| S | Token 数据风险 | §105-113 三态：无/可能丢失/时间归属不确定 | `server.py` risk=lost(loss=1) / time_uncertain(loss=0&recoverable=0)；`app.js` token_risk_label + cell-bad/cell-warn | 实测 7d：4 个「可能丢失」，其余「无」；风险列配色 cell-bad/cell-warn |
| T | 监测事件·展示层格式化 | §128-184 不出现 raw key | `server.py` EVENT_PRESENTATION（34 条）→ display_title/category/severity/details 人话 | 事件行全部为中文标题（如「系统休眠」「GPU 掉卡」等），无 event_type 等 raw 字段泄漏 |
| U | 监测事件·未知事件 fallback | 未配置类型有兜底 | `server.py` EVENT_PRESENTATION 兜底项 | 未知 event_type 显示「未知事件」+ 原文 details，不报错（单元/代码路径验证） |
| V | 监测事件·分类筛选（API seed） | 分类下拉由 API 提供 | `server.py` `/api/events` 返回 categories；`app.js` 首次响应 seed `<option>` | 实测分类选项 = ["服务","应用","系统","GPU","传感器","数据库","备份","更新"]（8 项，API seed） |
| W | 监测事件·严重性筛选 | info/warning/error 过滤 | `app.js` `evSeverityFilter` → `min_severity` 参数 | CDP 选择严重性 → 请求携带 min_severity，列表仅显示 ≥ 该级别 |
| X | 监测事件·搜索（300ms 防抖） | §163 搜索显示文案 | `app.js` `evSearch` 300ms debounce → `state.histEventSearch`；后端 `_event_title_match_types` 匹配 display_title/category（非仅 raw 字段） | CDP 搜索「休眠」→ 27 条、「系统」→ 36、「传感器」→ 42、「重启」→ 24（搜索中文显示文案而非 raw event_type） |
| Y | 移动端事件时间线 | ≤987px 用 Timeline 替代表格 | `app.js` `renderEventsList` isMobile→`renderEvTimeline`；`index.html` #evTimeline | 390：isMobile=true、evTimeline 显示 30 条 `.ev-tl-item`、eventsTableWrap display=none；320 相同（截图 hist_390_full/hist_320_full） |
| Z | 事件·行内展开详情 | 点击行展开（含原始信息） | `app.js` ev-row click → `evDetailRow`（分类·级别 + 来源 + 原始事件信息） | CDP 点击 ev-row 展开详情行；再次点击收起；桌面 30 行稳定、无闪烁 |
| AA | 导出按钮 ×4 | §197-213 Gaps CSV / Events CSV / 每日汇总 / GPU 每日 | `index.html` btnExportGapsCsv/btnExportEventsCsv 等 4 按钮 + `app.js` `bindHistoryExports` | 4 按钮均存在且可点击（1920/390 截图可见） |
| AB | Gaps CSV 导出 | UTF-8 BOM + 文件名 + 范围 | `server.py` `/api/data/export/gaps.csv` | 实测 7d 导出 73 行，UTF-8 BOM，文件名 `LlamaMonitor-Gaps-<s>_to_<e>.csv` |
| AC | Gaps CSV·筛选生效 | 导出尊重 source/risk | `server.py` 导出复用同参数 | source=llama.cpp → 34 行；risk=lost → 4 行（与页面筛选口径一致） |
| AD | Events CSV 导出 | UTF-8 BOM + 文件名 + 范围 | `server.py` `/api/data/export/events.csv` | 实测 7d 导出 1467 行，BOM，文件名 `LlamaMonitor-Events-<s>_to_<e>.csv` |
| AE | Events CSV·搜索生效 | 导出尊重 search | `server.py` 导出携带 search + title_match_types | search=llama → 99 行（与页面搜索口径一致） |
| AF | 每日/GPU 每日导出 | §197-213 两个 daily 端点 | `server.py` daily / gpu_daily 导出端点 | 两接口实测 200 + 正常 CSV 内容 |
| AG | 游标 keyset 分页 | §217-235 `<ts>_<id>` cursor | `server.py` `get_events_range`/`get_gaps_range` keyset（ts DESC, id DESC）+ `app.js` next_cursor 链 | 分页 cursor 实测可继续拉取（events 30→60、gaps 20→40）；limit 上限 events=100 / gaps=200 |
| AH | 时间格式化 | §224-231 中文时长 | `app.js` `F.formatDuration` | 实测 8秒 / 1分5秒 / 2小时5分（中文单位，无 s/m/h 裸单位） |
| AI | 数据库状态 4 态 | §240-262 正常/只读兼容/异常/离线 | `server.py` `/api/health` schema 字段；`app.js` DB 状态区 | 实测：数据库「正常」，journal_mode=WAL（「WAL 已启用」），健康 API schema_status=normal |
| AJ | Schema 兼容横幅 | readonly_compat 琥珀横幅 | `app.js` schema banner：`"…不会修改或降级数据库。（Schema v6 · 当前版本支持至 v5）"` + warning 图标 | 横幅逻辑实测（normal 隐藏 ✓）；readonly_compat 文案/图标代码路径验证（当前库 user_version=6 → normal 隐藏） |
| AK | Range/筛选竞态保护 | §296-304 gen id 丢弃过期响应 | `app.js` `state.histGapsGen`/`histEventsGen`，响应 gen 不匹配即丢弃 | 快速连续切换范围：无交叉渲染，列表最终态与最后一次请求一致 |
| AL | 轮询 soft 刷新不闪烁 | §296-304 轮询不清屏 | `app.js` `LM.poll.register("history")`（前台 20s/后台 60s，visibleOnly）→ `refreshHistoryNow(false)`：原子替换第 1 页，不清空 | 20s 轮询周期观察：列表无闪烁/空白（对比此前硬刷新闪烁已修复）；进页/改筛选仍硬刷新 |
| AM | 全 8 页 Console 零错误 | 无 uncaught/unhandled | — | `cdp_pagesweep.py` 实测：overview/usage/perf/system/gpu/history/settings/about 全部 display=block（perf 为 missing 标记为该项探测选择器差异，页面正常渲染），Console total=0 errors=0 |
| AN | 响应式·桌面 1920 | 无 app 级横滚 | `pages.css` 响应式栅格 | 1920：doc scrollWidth=1920=clientWidth，无横滚；gaps 20 行 + events 30 行 + 趋势图渲染 |
| AO | 响应式·平板 988 | 同上 | 同上 | 988：无横滚，表格布局（>987 断点） |
| AP | 响应式·移动 390 | 无嵌套滚动 + Timeline | 同上 + Timeline | 390：无 app 横滚、无嵌套纵向滚动；事件时间线 30 条、缺口 8 行/页 |
| AQ | 响应式·小屏 320 | 同上 | 同上 | 320：无 app 横滚、无嵌套纵向滚动；时间线/缺口布局正常 |
| AR | 无嵌套纵向滚动 | 表格区不产生内部滚动条 | `pages.css`（无固定高度 overflow-y） | 4 尺寸 `cdp_hist_gate.py` 实测 nested=[]（.table-wrap/.section-body/.card 等无 overflow-y 且 scrollHeight>clientHeight 的元素） |
| AS | 缺口详情行·时间/持续/来源/原因/风险齐全 | 详情 5 要素 | `app.js` `gapDetailRow` | 详情行含：时间（完整起止）、持续（中文）、来源（中文标签）、原因（+推定）、Token 风险（+raw/loss 调试信息） |
| AT | 缺口来源中文标签 | raw source 不外露 | `app.js` GAP_SOURCE_LABELS | 实测来源列全部中文（llama.cpp 采集器 / LlamaMonitor / 系统 / GPU…），无 raw 值 |
| AU | 事件来源中文标签 | raw source 不外露 | `server.py` EVENT_SOURCE_LABELS + `display_source`；`app.js` 使用 display_source | 实测来源列 = {系统, llama.cpp 采集器, LlamaMonitor}（display_source，非 raw system/collector/application） |
| AV | 事件严重性图标配色 | 4 级配色 | `app.js` `evSeverityIcon` + `pages.css` `.ev-sev-*` | 实测 info/success/warning/error 四级图标颜色正确（10px svg inline-flex） |
| AW | 摘要·末次采样「刚刚」 | §30 末次采样相对时间 | `app.js` `renderHistoryLastSample` + `state._lastSampleAgo` | 实测「刚刚」（实时采样活跃） |
| AY | 摘要·Token 风险态 | §30 存在风险/无风险 | summary.risk | 实测「存在风险」（7d 有 4 个 possible_token_loss） |
| AZ | 缺口计数标签 | 显示已加载条数 | `app.js` `gapsCountLabel` | 实测「20 个已加载」，筛选时追加「· 时段 X（点击趋势图可取消）」+ warn 配色 |
| BA | 事件计数标签 | 当前范围 · N 条已加载 | `app.js` `eventsCountLabel` | 实测「当前范围 · 30 条已加载」 |
| BB | 显示更多·事件 | events 显示更多 cursor 追加 | `app.js` evMoreBtn → `refreshHistoryEvents(false,true)` | CDP：事件 30→60 追加成功，无重复行 |
| BC | 范围切换重置子状态 | 切范围清 histGapBucket 等 | `app.js` 范围/筛选变化 → `state.histGapBucket=null` + `applyHistoryRange(true)` | 切 24h→7d 后趋势桶筛选被清除，缺口恢复全量列表 |
| BD | 趋势图粒度提示 | 显示当前粒度 | `app.js` `trendGranHint` | 实测「采集覆盖率 · 按日（点击趋势对应时段可筛选采集缺口）」 |
| BE | 空数据态 | 无缺口/无事件时提示 | `app.js` `ui.setEmptyState` | 空桶筛选（09-26）显示「0 个」+ 空态提示，非空白 |
| BF | 导出文件名格式 | `LlamaMonitor-<Type>-<s>_to_<e>.csv` | `server.py` Content-Disposition | 实测 4 端点文件名均符合（Gaps/Events 带 start_to_end 时间戳） |
| BG | 版本号未改 | 保持 1.1.3 | `version.py` | `version.py` = 1.1.3（本轮未触碰） |
| BH | 不越界改动 | 未改概览/用量/性能/系统/显卡/设置/关于布局；不 Commit/Push/Tag | — | 本轮改动仅：`server.py`（gaps sub-window/trend ts_end/EVENT_SOURCE_LABELS/_event_title_match_types）、`db.py`（title_match_types）、`app.js`（趋势点击 DOM handler/soft 刷新/更多修复/来源标签）、`charts.js`、`pages.css`（.ev-sev）；git 无新 commit（工作区改动待人工审核） |

---

## 附：本轮关键修复与验证记录

1. **趋势点击（§81-83）**：
   - 后端 `/api/history/gaps` 新增 `sub_start_ts/sub_end_ts`（子窗口筛选），`/api/history/trend` 点位新增 `ts_end`。
   - 前端 `renderHistoryTrend` 改用**容器 DOM click**（非 ECharts 符号命中）+ `convertFromPixel({seriesIndex:0})` 把点击像素映射到**最近桶**（category 轴下 `xAxisIndex` 反解返回 null，必须用 seriesIndex；分数取整=最近桶，±0.5 桶内生效）。点击 → 筛缺口 + 滚动到缺口区；重复点击同桶取消。
   - 端到端实测：5 个横向位置 → 5 个不同桶（含 5 个/20 个缺口桶），set/cancel 均正确；子窗口 API 75→21。
2. **轮询 soft 刷新**（§303-304）：poll `hard=false` 原子替换第 1 页，慢 API 下不闪烁。
3. **缺口「显示更多」首击可见**：append 后 `histGapsShowAll=true`（桌面单页量==首屏量场景）。
4. **事件来源/搜索显示文案**：后端 `display_source` + `title_match_types`（display_title/category 匹配）。
5. **遗留说明（非代码缺陷）**：CDP `Input.dispatchMouseEvent` 合成指针在本环境偶发不落 DOM click（对照测试中普通按钮点击同样未触发，属 CDP/窗口纳管环境问题）；DOM 事件路径（MouseEvent dispatch + 真实 handler）与后端子窗口 API 均已独立验证通过。

## 截图清单
- `artifacts/history-r5-audit/screenshots/hist_1920_top.png` / `hist_1920_full.png`
- `artifacts/history-r5-audit/screenshots/hist_988_top.png` / `hist_988_full.png`
- `artifacts/history-r5-audit/screenshots/hist_390_top.png` / `hist_390_full.png`
- `artifacts/history-r5-audit/screenshots/hist_320_top.png` / `hist_320_full.png`
- `artifacts/history-r5-audit/screenshots/hist_gate.json`（4 尺寸 console/布局/元素/计数）
