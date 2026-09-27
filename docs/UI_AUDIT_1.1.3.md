# LlamaMonitor 1.1.3 — 1.1.2 Reality Audit（视觉审计问题清单）

> 生成方式：真实读取 1.1.2 代码（tokens/mobile/layout/navigation/index.html/README/CHANGELOG）
> + 启动 Dashboard（8790 仓库代码 + 8765 安装版 + 9091 llama-server 真实数据）
> + `scripts/ui_snapshot_113.js` 生成 27 张截图（artifacts/ui-113/）做像素审查。
> Severity: BLOCKER / HIGH / MEDIUM / LOW / POLISH。
> 每条：Observed（观察）/ Expected（期望）/ Root Cause（根因）/ Fix（修复）/ Verification（验证）。
> 截图引用：artifacts/ui-113/<name>.png（1.1.2 现状；修复后重截同名文件覆盖对比）。

---

## DOC（文档）

### DOC-0001 · CHANGELOG 1.1.2 日期写成未来日期 —— HIGH
- Screenshot: n/a
- Observed: `CHANGELOG.md:10` 写 `## [1.1.2] - 2026-09-28`，而实际发布（GitHub Release
  published_at）= 2026-09-27。规范：依据实际 release 日期，不写未来日期。
- Expected: `[1.1.2] - 2026-09-27`。
- Root Cause: 发布日当天把日期预估成次日。
- Fix: 更正为 2026-09-27。
- Verification: 与 GitHub release API `published_at` 一致。

### DOC-0002 · README 截图在文件最底部（450 行处的第 45 个章节），不在顶部 —— HIGH
- Screenshot: n/a（GitHub README 渲染）
- Observed: `## 界面预览` 位于 README 末尾（L446-450），`## 功能` 在 L9。1.1.2 CHANGELOG
  宣称"README 顶部新增界面预览"，实际渲染顺序是：介绍 → 功能 → 开发模式 → 打包 → … → 手机访问
  → 界面预览。用户打开 GitHub 首屏看不到任何截图。
- Expected: 截图紧跟项目介绍之后、功能列表之前（spec §131/§132）。
- Root Cause: 1.1.2 是"append 到文末"而不是"插入到顶部"。
- Fix: 重组 README（§131 最终顺序：定位 → 截图 → 核心亮点 → 安装 → 手机访问 → 功能 → …）。
- Verification: 打开 GitHub README Preview 人工确认截图位置（截图留档）。

### DOC-0003 · README 残留内部开发术语（Phase 11/12/13/14）—— MEDIUM
- Observed: README 至少 6 处：L25 "可靠性与数据质量（Phase 11）"、L39 "版本管理（Phase 12）"、
  L61 "发布 / 安装器（Phase 12）"、L85 "安全更新（Secure Updates，Phase 13）"、L213 "更新（Phase 13）"、
  L341-344 schema 说明里的 "Phase 13/11/9"、L418 "Phase 14 AUDIT-SEC-001/002"。
- Expected: 公开 README 用产品语言：Data Reliability / Secure Updates / Release Packaging；
  schema 说明写"v3 增加事件审计表"即可。
- Fix: 全 README grep `Phase` 清理。
- Verification: `grep -c "Phase" README.md` = 0（代码注释可保留）。

### DOC-0004 · README 中英文混用（用户文案）—— LOW
- Observed: 功能列表里 "Check / Download / Install / Cancel"、"Test Connection"、"Save"、
  "Reset to Defaults"、"Unsaved changes"、"Data Info"、"Run Integrity Check Now" 等大量英文按钮名。
- Expected: 用户文案中文化（检查更新 / 下载 / 安装 / 取消），代码命令与 API 名保留英文。
- Fix: 逐处替换（按钮名以 UI 实际文案为准，先核 UI 再改 README）。
- Verification: 人工通读。

## TYPE（排版）

### TYPE-0001 · Mobile 字号 token 实际未设置 caption 13 / secondary 14 —— HIGH
- Screenshot: mobile-390-dark-*.png
- Observed: tokens.css ≤760 块只覆盖 body 15 / card 16 / hero 32 / metric 24。
  `--font-size-caption` 手机仍 12px、`--font-size-secondary` 手机仍 13px。
  而 1.1.2 CHANGELOG 写"正文 15px / 辅助 14px"。大量 12px 出现在：表格卡行标签
  （mobile.css L131 `::before{font-size:var(--font-size-caption)}`）、stat-hint、
  bottom-nav label、tooltip 按钮。spec §9/§10：手机不应大量 12px 正文。
- Expected: mobile token 块真实设置 `--font-size-caption:13px; --font-size-secondary:14px;`。
- Root Cause: 1.1.2 只覆盖了 4 个 token，caption/secondary 漏改。
- Fix: tokens.css mobile 块补两行；逐页截图复核无 <13px 正文（版权/极弱 footnote 除外）。
- Verification: 重截 390 全 8 页 + 像素审查字号直方图。

### TYPE-0002 · 行高单一 1.45，无层级 —— MEDIUM
- Observed: tokens.css 只有 `--line-height:1.45` 一个；标题/指标/长说明同 1.45。
- Expected: tight 1.2-1.25（标题）/ 1.15-1.2（metric）/ normal 1.45-1.5（body）/
  relaxed 1.55-1.6（长说明）。
- Fix: 新增 `--line-height-tight/normal/relaxed` 三档并在对应组件使用。
- Verification: 截图对比标题区收紧效果。

### TYPE-0003 · 字重体系缺 650/section 层级 —— LOW
- Observed: 只有 400/600/700；Section Title 18/700 与 Page Title 24/700 同字重，层级靠尺寸。
- Expected: spec §12：Section 650（Segoe 不支持则 600 或 700 统一）、Card 600、Metric 650/700。
- Fix: 定稿 `--font-weight-section`（选 600，Segoe 可渲染且与 body 拉开）。
- Verification: 截图审查 Section 与 Page Title 视觉权重差异。

## TOKEN（设计 token）

### TOKEN-0001 · `--content-max` 重复定义（1520 然后 1560 覆盖）—— HIGH
- Observed: tokens.css L71 `--content-max:1520px` + L76 `--content-max:1560px`（"覆盖上方旧值"）。
  同一名称两个定义，语义靠声明顺序。
- Expected: 一个名称一个定义。
- Fix: 删 L71，保留 1560 并逐页 max-width 重审（spec §21：Overview/Usage/Performance/System
  1480-1560、GPU 1560-1640、History 1440-1520、Settings 1180-1280、About 880-960）。
- Verification: grep `--content-max:` 出现次数 = 每页一个独立 token。

### TOKEN-0002 · radius 双体系（small/medium/large 与 micro/control/card… 并行）—— MEDIUM
- Observed: tokens.css L37-46：7 个新名 + 3 个旧别名（small=4/medium=8/large=12）。
  组件代码里仍在引用旧名（grep `--radius-small|medium|large` 有命中）。
- Expected: Primary tokens 语义明确（spec §7 建议名：xs/control/control-lg/card/card-lg/sheet/pill），
  旧 alias 保留但加 deprecated 注释，新代码不引用旧名。
- Fix: 定名 + 全量替换组件引用 + alias 加注释。
- Verification: grep 组件 css 无旧名引用；tokens 里旧名仅存于 deprecated 块。

### TOKEN-0003 · 无 z-index 体系（40/50/60/61/1500/2000/2100 散落）—— MEDIUM
- Observed: mobile.css 40（settings rail）；layout.css 50（bottom nav）/60（scrim）/61（sheet）；
  components.css 1500（tooltip?）/2000/2100（modal?）/1。数字跨度大且无文档。
- Expected: 统一层级（spec §211）：content 0 / sticky 20 / mobile nav 40 / infobar 50 /
  sheet scrim 60 / sheet 61 / toast 70 / modal scrim 80 / modal 81 / tooltip 90。
- Fix: tokens 增加 `--z-*` 系列，全量替换。
- Verification: grep z-index 全部走 var(--z-*)。

### TOKEN-0004 · 断点体系混乱（640/700/760/900/1100/1200/1366/1400/2560 共 9 个宽度断点）—— HIGH
- Observed: 33 个 @media 宽度断点：pages.css 700/900/1100/1200/1400/640，layout.css 760/900/1366/2560，
  components.css 900/1100/1200/1400。900 出现 12 次（components 4 + pages 7 + layout 1），760 出现
  6 次。同一语义（如"窄屏页头"）在 900 和 760 各写一遍（spec §210 点名审计项）。
- Expected: 5 档（spec §209）：base / ≤1366 narrow / ≤1099 compact（tablet）/ ≤760 mobile /
  ≤360 small-mobile + 特性查询（pointer/hover/forced-colors/reduced-motion）。例外需注释 WHY。
- Fix: 断点迁移审计：每个 @media 归类到 5 档之一；900/1100/1200/1400/700/640 逐条归并
  （能安全合并的合并，风险大的保留并注释 WHY + 归入最接近档）。
- Verification: grep `@media (max-width` 只出现 1366/1099/760/360 四档；回归 patrol 136 矩阵。

### TOKEN-0005 · 无行高/字重/颜色层级 token（见 TYPE-0002/0003）—— MEDIUM
（合并修复）

## MOBILE（手机）

### MOBILE-0001 · 手机 Section Header 强制 flex-direction:column，Action 全部掉行 —— HIGH
- Screenshot: mobile-390-dark-*.png
- Observed: mobile.css L40-45：`.section-header{flex-direction:column}` +
  `.sh-actions{width:100%; justify-content:flex-start}`。每个 section 的"查看详情"类
  action 都独占一行，页面垂直空白大、节奏差（spec §26/§27 点名：短 action 应与标题同行）。
- Expected: 默认仍 `Title … Action` 同行；放不下才换行（`flex-wrap:wrap` 而非强制 column）。
- Fix: 改为 `flex-wrap:wrap` + action `margin-left:auto`；逐页截图确认。
- Verification: 重截 390 全页，section 垂直密度对比。

### MOBILE-0002 · Bottom Nav 是"Web tab"语言（顶部 3px 指示条）+ 56px 略紧 —— MEDIUM
- Screenshot: mobile-390-dark-overview.png
- Observed: layout.css L368-378：active = 顶部 32×3px accent 条 + label 加粗。
  与桌面侧栏左侧指示条同语言，但 spec §97 要求：不要顶部横线（太像 web tab），
  改用 icon accent + label primary + 极轻 pill 背景（如 32×24 subtle accent pill）。
  高度 spec §98 建议测 60px。
- Fix: `--bottom-nav-height:60px`；active 态重设计（pill 背景 + icon accent）；
  label 13px。
- Verification: 截图对比 + 触控目标复核。

### MOBILE-0003 · System 在 More Sheet 而非主导航（信息架构问题）—— HIGH
- Screenshot: n/a
- Observed: Bottom nav = 概览/Token/性能/GPU/更多；系统（System）是核心一级监控页却在
  More Sheet 里（navigation.js L47 MORE_PAGES 含 system）。spec §92-§94：核心监控域
  5 页（Overview/Usage/Performance/GPU/System）全部一键到达；辅助功能
  （History/Settings/About）进 Overflow。
- Expected: Bottom nav = 概览/用量/性能/GPU/系统；每页 header 右上 `•••` overflow（44×44，
  20px more icon）打开 sheet：监控历史 / 设置（仅本机）/ 关于 + 版本号（spec §93-§96）。
- Fix: 重做导航结构（index.html DOM + navigation.js 逻辑 + CSS）。
- Verification: 重截 390 全页 + 交互冒烟（overflow 开合、焦点回位、远程隐藏设置）。

### MOBILE-0004 · Today Breakdown 手机单列（今日卡过长）—— MEDIUM
- Screenshot: mobile-390-dark-overview.png
- Observed: mobile.css L53 `.today-breakdown{grid-template-columns:1fr}`：输入/缓存/输出
  三项纵向堆叠，今日卡很长。spec §35：390 宽建议仍三列（label 允许两行），320 才 2+1/单列。
- Fix: 390 档三列（label 两行），≤360 才降列。
- Verification: 截图对比今日卡高度。

### MOBILE-0005 · 页面切换 scrollTop 永远清零（无按页记忆）—— MEDIUM
- Observed: navigation.js L63-64：`content.scrollTop = 0`。GPU 滚到温度切走再回 → 回顶部
  （spec §101：每页记忆 scrollTop；点当前页第二次可回顶）。
- Fix: 按页缓存 scrollTop（Map），切回恢复；重复点当前页 → 回顶。
- Verification: CDP 交互测试（滚动→切走→切回，断言 scrollTop 恢复）。

### MOBILE-0006 · Sheet/Modal 无 focus trap、无 body scroll lock —— MEDIUM
- Observed: openSheet() 只 focus 第一个 item；Tab 可跑到背景；背景 content 可继续滚
  （spec §103-§105）。Modal（components.js）同样无 trap。
- Fix: 通用 focus trap 工具（Tab/Shift+Tab 循环、ESC 关闭、关闭回焦点、open 时锁
  body scroll（`overflow:hidden` + scrollbar-gutter 防跳动）。
- Verification: CDP 键盘测试（Tab 循环、ESC、滚动锁）。

### MOBILE-0007 · 远程 banner 常驻占高（手机首屏损耗）—— LOW
- Observed: remoteBanner 显示后常驻在内容顶部（无关闭记忆）。spec §129：首次 compact
  info bar，可关闭 + session 记忆；Overflow sheet 显示"远程只读"状态。
- Fix: banner 加关闭（sessionStorage 记忆）；overflow sheet 增"远程只读"状态行。
- Verification: 远程模拟（tools/cdp_lan_phone.py）截图。

## DESKTOP（桌面）

### DESKTOP-0001 · GPU 卡网格 auto-fill 无 min/max（2 卡时过宽显空）—— MEDIUM
- Screenshot: desktop-1920-dark-gpu.png
- Observed: gpu-grid `repeat(auto-fill, minmax(...))`（需核 pages.css 具体值）：2 张 GPU
  卡各占 50% 宽，卡片过宽显得空；>4 卡未验证（spec §39/§40：min 300 max 520，
  1/2/3/4/6/8 卡 grid 自然）。
- Fix: `repeat(auto-fill, minmax(300px, 520px))` + justify-content 居中/拉伸策略按卡数。
- Verification: 1/2/3/4 卡截图（本机 2 卡 + 临时配置模拟多卡）。

### DESKTOP-0002 · 页头/分区/卡片左边缘对齐线需逐页像素验证 —— MEDIUM
- Screenshot: desktop-1920-dark-*.png
- Observed: 待像素审计确认（spec §177/§178/§179）：card 左缘、section header 左缘、
  page header 左缘、chart plot 缘、table 缘是否形成垂直对齐线；数字基线是否对齐。
- Fix: 按审计结果修（预计：个别 padding 12/14/17 混用 → 统一 16/20）。
- Verification: 对齐线像素脚本（列亮度扫描）+ 人工截图。

### DESKTOP-0003 · 4K（3840）页面 max-width 单一 1560 → 两侧太空 —— MEDIUM
- Screenshot: n/a（需 3840 截图）
- Observed: layout.css L201 `@media (min-width:2560px){.content-inner{max-width:1920px}}`：
  4K 下内容 1920 居中，左右各 ~960px 空。spec §22：4K 应适度放大 + 保持密度，
  不是 800px 也不是 2500px 拉伸。
- Fix: 2560+ 档逐页 max-width 重定（如 Overview 1800-2000、Settings 1400、About 1000）。
- Verification: 3840×2160 截图审查。

## CHART（图表）

### CHART-0001 · Usage 单/少日 Bar 宽度未约束 —— MEDIUM
- Screenshot: desktop-1920-dark-usage.png / mobile-390-dark-usage.png
- Observed: ECharts barMaxWidth 未设（需核 charts.js）：7 天数据时 bar 可能很宽；
  1 天可能占满（spec §44-§46：1 天 40-64px、7 天 28-48px、30 天 10-24px 自动）。
- Fix: 按天数分档设 barMaxWidth/barWidth。
- Verification: 1/7/30 天截图。

### CHART-0002 · Tooltip 只有 compact 数（1.94M）无完整整数 —— MEDIUM
- Screenshot: n/a
- Observed: chart tooltip formatter 只显示 compact（spec §48：1.94M (1,942,381) 双显示）。
- Fix: formatter 加完整千分位行。
- Verification: 触发 tooltip 截图。

### CHART-0003 · TPS 无数据时画空坐标轴 —— LOW
- Screenshot: mobile-390-dark-performance.png（无推理时）
- Observed: 无样本时仍显示坐标轴（spec §54：无样本不画轴，显示 empty state 文案）。
- Fix: 无样本 → hide 轴 + empty state（"暂无吞吐数据 / 开始推理后将在此显示…"）。
- Verification: llama 无推理时截图。

### CHART-0004 · 温度图 Y 轴从 0 起（50-70 差异被压扁）—— LOW
- Observed: 温度系列 yAxis min:0（spec §79：nice lower bound ≈ min(data)-10）。
- Fix: 温度轴自动 min（不低于 0，nice 对齐）。
- Verification: 截图。

### CHART-0005 · MTP Trend Y 轴自动 40-50%（视觉夸大）—— LOW
- Observed: MTP 率图未固定 0-100%（spec §55）。
- Fix: yAxis 0-100 fixed。
- Verification: 截图。

## TABLE（表格）

### TABLE-0001 · 手机 Card Row 每条 9 行同质（过长）—— MEDIUM
- Screenshot: mobile-390-dark-usage.png
- Observed: 每日卡 9 个字段平铺（spec §50：视觉分组——总量/实际计算 一组，
  输入/缓存/输出 一组，复用率/覆盖率/缺口 一组，组间 divider）。
- Fix: 卡内分组（CSS group header 或 divider 行）。
- Verification: 截图。

### TABLE-0002 · `:has()` 作为 Card Rows 唯一依赖 —— MEDIUM
- Observed: mobile.css L65-168 全部 `.table-wrap:has(.table-daily|gap|events)` 结构选择器。
  spec §143：现代浏览器支持良好，但建议 JS 直接加 `mobile-card-table` 显式 class 更健壮。
- Fix: JS 注入显式 class，CSS 双路径（class 优先，:has 兜底）或纯 class。
- Verification: 旧 WebView2 场景模拟（可选）+ 回归。

## INTERACTION（交互）

### INTERACTION-0001 · Tooltip 无 hover 延迟（鼠标经过即闪）—— LOW
- Observed: infoTip 桌面 hover 即时显示（spec §106：enter 250-350ms，leave 100ms）。
- Fix: 延时显示/即时隐藏。
- Verification: 交互测试。

### INTERACTION-0002 · 按钮高度体系（desktop 44? 需核）—— MEDIUM
- Observed: 需核 components.css `.btn` 高度：spec §110 desktop default 32-36 / primary 36 /
  mobile 44；icon button desktop 32×32 mobile 44×44。
- Fix: 按审计统一。
- Verification: 截图 + 像素测量。

## STATE（状态）

### STATE-0001 · Server 状态时间"X 秒前"持续跳动 —— LOW
- Screenshot: desktop-1920-dark-overview.png
- Observed: 最后更新 "2秒前/3秒前/4秒前" 每轮刷新跳数字（spec §33：≤5s 显示"刚刚"，
  6-59s 才 X秒前）。
- Fix: formatter 分段（刚刚 / X秒前 / X分钟前）。
- Verification: 截图对比。

### STATE-0002 · 状态文案不统一（Loading.../检测中/加载中/-- 混用）—— MEDIUM
- Observed: 需 grep 前端文案核（spec §127/§190：统一"正在加载…/暂无数据/不可用/未监控/
  数据已过期/连接中断/不支持"；`--` 只用于字段级无值）。
- Fix: 文案审计表 + 统一。
- Verification: grep + 截图。

### STATE-0003 · llama offline 时 TPS/Slot 全清成 --（丢最后已知值）—— MEDIUM
- Observed: 需核 app.js offline 分支（spec §124：Last known 淡化 + Offline 标记，
  Today 累计保留 DB 值）。
- Fix: offline 保留最后样本淡化 + 状态条 Warning。
- Verification: 停 llama-server 截图。

## A11Y（无障碍）

### A11Y-0001 · Bottom Nav 缺 aria-label / 每 button 无独立 label —— LOW
- Observed: nav 有 `aria-label="主导航（移动端）"`；但 mnav-item 文字是"Token"（应"用量"，
  随导航改名）；overflow 按钮需 aria-expanded（当前 more 按钮有 aria-haspopup/controls，
  缺 aria-expanded 切换）。
- Fix: 随导航重做补全（spec §144/§145）。
- Verification: DOM 断言。

### A11Y-0002 · Sheet 有 role=dialog/aria-modal 但无 focus trap（同 MOBILE-0006）—— MEDIUM
（合并修复）

### A11Y-0003 · Focus visible 2px accent ring 需验证（overflow:hidden 裁切风险）—— LOW
- Observed: 需键盘 Tab 遍历截图（spec §147）。
- Verification: CDP 键盘测试 + 截图。

### A11Y-0004 · 颜色对比度（Dark secondary 0.64α / muted 0.45α 需实测）—— MEDIUM
- Observed: `--text-secondary: rgba(255,255,255,.64)`（dark）≈ #b8bac0 on #2b2d31 ≈ 对比度
  ~6:1（OK）；`--text-muted: .45α` ≈ ~4:1（临界）；light muted #8a8a8a on #fdfdfd ≈ 3.1:1
  （< 4.5，正文级不达标，仅 caption 级勉强）。spec §13/§146：Muted 不能暗到看不清。
- Fix: 提 light muted 到 ~#6f6f6f（4.5:1）；dark muted 保持或略提；跑对比度脚本全表。
- Verification: 对比度计算脚本 + 截图。

## HEAT GRID（per-core）

### MOBILE-0008 / STATE-0004 · Heat Grid 用聚合值填充 96 格（fake per-core）—— HIGH
- Screenshot: desktop-1920-dark-system.png
- Observed: system.js renderCoreHeat：`cpu_percent` 只有整体值，96 个 cell 全部填同一个
  聚合使用率（opacity 相同 = 96 个颜色完全一样的格子）。CHANGELOG 自己注明"每格颜色映射
  当前整机聚合利用率"——但视觉上用户会认为是 per-core。spec §62：只有整体值时禁止画多格
  假 per-core；方案 A：psutil `cpu_percent(percpu=True)` 真采 per-core（后端 + DB 不变，
  只加实时字段）；方案 B：无 per-core 只显总利用率隐藏 grid。
- Fix: 方案 A（server.py 系统采集加 `cpu_per_core: [..]` 实时字段；system.js 真 per-core
  渲染；每格数字 + 颜色连续映射；默认物理核视图/可切 logical；>32 格默认折叠 advanced）。
- Verification: 单核压载测试（任务管理器确认某核高负载）截图；无数据时降级测试。

---

## 审计方法备注
- 像素审查：scripts/ui_snapshot_113.js（CDP，27 视口×页×主题矩阵）+ 程序化像素分析
  （System.Drawing/PIL：字号直方图、card padding 测量、对齐线列扫描、对比度计算）。
- 本模型无图像输入 → 视觉 QA 由 subagent 程序化分析执行，关键结论人工（用户）抽检。
- 1.1.2 patrol 基线：136/136（overflow/结构/触控 gate）；1.1.3 修复后需全量重跑。
