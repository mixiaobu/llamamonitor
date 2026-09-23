# LlamaMonitor 0.16.16 — Phase 16D 统一 UI 交付说明

本版本实现 Phase 16D「最终统一 UI 打磨 + 布局/视觉/组件系统统一 + 页面结构清理 + 桌面 UX 就绪」。
技术栈不变（HTML/CSS/vanilla JS/ECharts/PyWebView/FastAPI），信息架构不变（概览/用量/性能/GPU/历史/设置/关于），
仅做视觉与结构统一。全部视觉值收敛到唯一 token 源。

## 0. 设计系统说明（总纲）

四条核心原则落地：

1. **动作归位**：所有可点动作只出现在 页头右侧（`.page-header .ph-actions`）或 分区标题右侧
   （`.section-header .sh-actions`）。页面正文卡片内不再有游离的操作按钮（设置页的 rail/危险操作除外，属导航/分区语义）。
2. **卡片同构**：所有卡片 = 标题区（`.card-title` 15/600 或分区标题替代）+ 内容区 + 可选底部说明区
   （`.card-footer`，仅数据来源/注释用）。不可点击卡片不假装 clickable。
3. **页面骨架统一**：每个页面 = 页头（`.page-header`：左 Title+Subtitle，右可选动作）→ 若干分区
   （`.section`：`.section-header` 左标题右说明/动作）→ 分区内卡片（`.card`）。
4. **唯一 token**：颜色/字体/间距/圆角/布局节奏只写 `tokens.css`，组件 CSS 只引用变量，主题（dark/light）只在 token 层切换。

统一视觉节奏：页头→首分区 24px、分区间 24px、分区标题→内容 12px、卡片内 padding 16/20px、
页面左右 32px（1366 以下 24px）、内容最大宽 1520px（设置 1280、关于 900；2560+ 收敛到 1920/1520/1040）。

---

## 1. 颜色 token（tokens.css）

Dark（Windows 11 深色，App 背景 #202124，Sidebar 更暗，Main #26282B，Card #2B2D31）：

| Token | 值 | 用途 |
|---|---|---|
| `--bg-base` | `rgba(32,33,36,0.96)` | App/主内容背景（≈#202124，透出 Mica） |
| `--bg-layer` | `#1b1c1f` | Sidebar（比主内容略暗） |
| `--bg-card` | `#2b2d31` | 卡片表面 |
| `--bg-card-hover` | `#33363b` | 卡片 hover |
| `--bg-elevated` | `#313439` | 弹层/表头/tooltip（比 card 略亮） |
| `--bg-input` | `#212327` | 输入框 |
| `--bg-subtle/-hover/-active` | `rgba(255,255,255,0.03/0.05/0.08)` | 极弱填充 |
| `--border-subtle` | `rgba(255,255,255,0.08)` | 卡片/分隔 |
| `--border-default` | `rgba(255,255,255,0.13)` | 控件边框 |
| `--border-strong` | `rgba(255,255,255,0.22)` | 强调边框 |
| `--text-primary` | `#f3f4f6` | 一级文本（近白不刺眼） |
| `--text-secondary` | `rgba(255,255,255,0.64)` | 二级 |
| `--text-muted` | `rgba(255,255,255,0.45)` | 三级（副标题/说明，新增） |
| `--text-disabled` | `rgba(255,255,255,0.36)` | 禁用/占位 |
| `--accent` | `#4cc2ff` | 主色（克制 Win 蓝，替代 #60cdff） |
| `--success` / `--warning` / `--error` | `#5fb96a` / `#f2b354` / `#ff8f98` | 语义色 |
| `--error-subtle-border` | `rgba(255,143,152,0.32)` | 危险区描边（新增） |

Light：`--bg-base #f4f4f4`、`--bg-layer #eaeaea`、`--bg-card #fdfdfd`、`--bg-elevated #f0f0f0`、
text `#1b1b1b/#565656/#8a8a8a`、accent `#005fb8`、success `#0f7b0f`、warning `#9d5d00`、error `#c42b1c`。

## 2. 字体 token（统一层级）

| 角色 | 变量 | 值 |
|---|---|---|
| Page Title | `--font-size-title` | 24 / 700，letter-spacing -0.2px，line-height 1.25 |
| Page Subtitle | `--font-size-secondary` | 13 / 400 |
| Section Title | `--font-size-subtitle` | 18 / 700 |
| Card Title | `--font-size-card` | 15 / 600（新增） |
| Metric Big（普通） | `--font-size-display` | 32 / 600 |
| Metric Big（概览主指标） | `--font-size-hero` | 40 / 700 |
| Metric Medium | `--font-size-metric` | 22 / 600 |
| Body / Secondary / Caption | `--font-size-body/-secondary/-caption` | 14 / 13 / 12 |

字重：400/500/600/700。数字一律 `font-variant-numeric: tabular-nums`。
变量名沿用 Phase 15（`--font-size-title/subtitle/display/hero/metric` 等）以兼容既有引用，值统一到规范。

## 3. 间距 token

4 / 8 / 12 / 16 / 20 / 24 / 32 / 40 → `--spacing-xs/sm/md/lg/xl/2xl/3xl/4xl`。
布局节奏（新增）：`--section-gap 24`、`--section-head-gap 12`、`--card-pad 16`、`--card-pad-lg 20`、`--card-row-gap 12`。

## 4. 圆角 token

| Token | 值 | 用途 |
|---|---|---|
| `--radius-large` | 12px | 大卡片 |
| `--radius-card-sm` | 10px | 小卡片（GPU mini 等，新增） |
| `--radius-control` / `--radius-medium` | 8px | 按钮/输入/胶囊容器 |
| `--radius-small` | 4px | 更小组件 |
| `--radius-pill` | 999px | 胶囊/状态标签 |

## 5. 全局布局

- 导航 212px（`--nav-width`），内容最大 1520px（`--content-max`），设置 1280、关于 900。
- 页面左右 padding 32px（`--page-pad`，1366 以下 24px `--page-pad-narrow`）。
- `.content-inner` 顶 24 / 底 32。2560+：内容 1920、设置 1520、关于 1040。
- 页面切换 120ms fade。

## 6-12. 各页面布局说明

**6. 概览**：页头 → ① 服务器状态卡（`.status-strip.card`，左名称+状态点、右最后更新）→ ② 今日用量单张大卡
（hero 逻辑/计算 40px + 三列 提示/缓存/输出，"查看用量明细"在分区标题右侧）→ ③ 当前性能与运行双卡
（Token 速率 / 运行状态，"查看性能详情"在分区右侧）→ ④ GPU 摘要（状态行+迷你卡 2 等宽，"查看 GPU 详情"在分区右侧）
→ ⑤ 数据质量横向 summary 卡（"查看历史与缺口详情"在分区右侧）。

**7. 用量**：页头右侧 时间范围 segmented（今天/7天/30天/本月/全部）→ ① 所选范围汇总卡（标题即具体范围名，
无"累计"徽标，右侧"所选范围"轻量标签）→ ② 每日 Token 用量堆叠图（标题右为说明）→ ③ 每日明细表
（日期/提示/缓存/输出/计算/逻辑/缓存率/覆盖率/缺口，说明在分区右侧）。

**8. 性能**：页头 → 实时指标条（一行 5 项 Prompt TPS/Decode TPS/处理中/排队/Busy Slots，单卡）→ Token 吞吐图
（"最近 60 分钟"在分区右侧，无数据时统一 Empty State）→ 服务器运行时（6 项 2~3 列）→ MTP/投机解码
（summary 4 项 + 两张等高并排图，"今日"在分区右侧）。

**9. GPU**：页头右侧 范围 segmented（15min/1h/6h/24h）→ GPU 概览卡网格（等宽，主 4 指标 + VRAM 进度条 + 次要指标）
→ GPU 过滤器行（独立一行，chip 风格）→ 利用率&显存全宽图 → 硬件趋势（功耗+温度并排，不支持不建 series）
→ 能耗估算 summary 卡（来源说明在分区右侧）。

**10. 历史**：页头 → 数据质量 summary → 最近缺口表（有数据绝不显示空态，空态仅在无数据时）→ 监控事件
（人类可读化详情，无数据才显示空态）→ 导出（按钮组）。

**11. 设置**：页头 → 左侧 rail（服务器/采集/外观/GPU/数据与备份/应用/更新）+ 右侧 pane（每个分组 = 一张 `.settings-card`
section 卡）。设置行左标题+描述、右控件；宽度 URL 360-420、数字 96-120、select 160-220。
数据与备份顺序 存储/备份/日志/数据/危险操作（危险区视觉隔离，红描边）。底部 sticky 操作栏：左状态、右 恢复默认+保存
（无改动时保存禁用）。

**12. 关于**：页头 → 品牌卡（Logo+名称+版本+副标题）→ 应用信息卡（kv 表 + 复制版本信息/打开数据目录 + /metrics 说明），
max-width 900px。

## 13. 统一组件清单

Card（`.card`/`.card-title`/`.card-footer`/`.card-section-gap`）· Stat（`.stat-grid`/`.stat-value[.mid|.small]`）
· Button（`.btn[.primary|.subtle|.danger]`）· Input/Select（`.input`/`.select`，`.w-s/m/l/xl`）· Switch
· Segmented（`.seg`）· InfoBar（`.infobar`）· StatusBadge（`.status-badge`，小圆点+文本）· Toast · Modal
· Table（`.table-full`）· EmptyState（`.empty-state`，`force-show/force-hide`）· InfoTooltip（`.info-tip`）
· 进度条 · SettingRow · RangeTag（`.range-tag`，新增）· CheckChip（`.check-chip`）。

## 14. Action 位置规范

- 页头右侧：`.page-header > .ph-actions`（用量/GPU 的范围 segmented 在此）。
- 分区右侧：`.section-header > .sh-actions`（行动链接 `.link`、说明文字、轻量标签）。
- 卡片内不再放页面级动作；设置页 rail 项与危险操作属导航/分区语义，例外。
- 图表"最近 N 分钟/天"等时间范围说明一律上移到分区标题右侧，不进卡片标题。

## 15. Empty / Loading / Error 规范

- 图表空态：`setEmpty()` 覆盖 `.chart-empty-overlay`（`has-empty` 隐藏 canvas），标题如"暂无吞吐数据"，不出现空坐标轴。
- 表格/列表空态：`.empty-state`，仅无数据时 `force-show`；有数据绝不与"暂无"并存（gaps/events 已按 16C 修正）。
- 加载态：占位 `--` + `dim`；GPU 概览初始"加载中"空态。
- 错误/警告：语义色（error/warning）文本与 InfoBar，不整页变红；离线走全局 InfoBar。

## 16. 响应式结果（CDP 实测，1920 高 1080）

| 宽度 | content-inner | 横向溢出 | 元素溢出 |
|---|---|---|---|
| 1366 | 1144（24px 窄边距） | 无 | 无 |
| 1920 | 1520（封顶） | 无 | 无 |
| 2560 | 1920（封顶） | 无 | 无 |
| 3840 | 1920（封顶） | 无 | 无 |

7 页 × 4 宽 全部 `overflowX=false`、无超宽元素。窄屏：概览 GPU mini 单列、perf 双卡单列、charts-2col 单列、
grid cols-3/4 收敛、metric-strip 2 列、settings shell 单列（rail 横排）。

## 17. DPI 结果（1920×1080，deviceScaleFactor）

100%（1.0）/ 125%（1.25）/ 150%（1.5）/ 200%（2.0）均无横向溢出、无元素溢出、布局一致。
（布局走 rem/px + CSS 变量，不依赖固定像素，DPI 缩放由系统按 deviceScaleFactor 渲染。）

## 18. Console 结果

CDP 抓包（Runtime+Log）：重载 12s + 全页导航 + settings rail 全点击，**0 error / 0 warning / 0 exception**。
隐藏窗口下 fetch 不发起（预期），无未捕获异常。

## 19. 图表主题集中化（§34）

ECharts 主题集中在 `charts.js` `THEMES`/`COLORS`（dark/light），`pal()`/`colors()` 统一调色，
legend fontSize 12/10、grid 边距 8/8/32/4。所有图表实例（chartUsage/chartTps/chartMtp/chartMtpPos/
chartGpuUtil/chartGpuPower/chartGpuTemp）均从集中主题取色，无散落的硬编码颜色。

## 20. 所有截图

`docs/screenshots-0.16.16/`（1920×1080，1×，CDP 实机）：
`shot-overview-dark.png` · `shot-usage-dark.png` · `shot-performance-dark.png` · `shot-gpu-dark.png`
· `shot-history-dark.png` · `shot-settings-dark.png` · `shot-about-dark.png` · `shot-overview-light.png`。

---

### 验证与发布

- `validate_release.py` → `RELEASE VALIDATION OK: … (version 0.16.16)`。
- 全量测试 `unittest discover` → **Ran 418 tests … OK**。
- 资产：`LlamaMonitor-0.16.16-win-x64.zip`、`LlamaMonitor-Setup-0.16.16-win-x64.exe`、`SHA256SUMS.txt`、
  `release-manifest.json` + `.sig`。
