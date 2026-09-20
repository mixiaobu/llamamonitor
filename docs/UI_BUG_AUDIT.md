# UI Bug Audit（Phase 15：Windows 11 Fluent UI Redesign）

Sweep 方法（spec §82-84）：先完整通读现有 UI 代码（static/index.html 单文件 2650 行：
内联 CSS ~335 行 + HTML ~500 行 + JS ~1800 行；static/ 下无其他 JS 文件），
逐页检查（Dashboard / Settings×10 节），再在 PyWebView 实机复现。
每个问题：Page / Severity / Reproduction / Expected / Actual / Root Cause / Fix / Regression Test。

Severity（spec §83）：BLOCKER=页面不可用/数据操作错误/危险误触；
HIGH=核心数字错误/保存无效/页面死掉/严重状态不同步；
MEDIUM=布局错/按钮状态错/Loading 问题；LOW=细节视觉。

## 发现列表

### UI-001（MEDIUM）— Page: 全局
- Repro: Settings→Interface 选 System；运行中切换 Windows 深/浅主题。
- Expected（spec §26）：UI 立即跟随系统主题。
- Actual：不跟随。`applyTheme()` 只在启动/设置变更时调用；`matchMedia` 无监听器。
- Root cause：缺少 `prefers-color-scheme` 的 `change` 监听。
- Fix：app.js 主题控制器：system 模式下注册 `matchMedia(...).addEventListener('change')`，
  非 system 模式移除监听（避免残留）。
- Regression：实机验证（系统主题切换后 UI 变色，截图对比）；代码检查监听器随模式增删。

### UI-002（MEDIUM）— Page: GPU（Dashboard GPU 区）
- Repro: 多 GPU 机器，打开 Dashboard 等 10s。
- Expected：GPU 显隐勾选框稳定，焦点不丢。
- Actual：`refreshGpuStatus()` 每 5s 调 `renderGpuPick()` 全量重建勾选框 DOM
  （含 listener 重建）——用户按住焦点时 DOM 被替换、光标/焦点丢失、视觉上闪烁。
- Root cause：无"探测结果未变则不重建"的判断。
- Fix：GPU 页 renderGpuPick 仅在 detected 列表（uuid+index+name 签名）变化时重建。
- Regression：实机多卡环境观察 30s 无重建（DOM 节点 identity 不变，console 计数）。

### UI-003（LOW）— Page: 全局
- Repro: 任意。
- Expected（spec §16）：文案统一。
- Actual：中英混排——"最后更新:"/"加载中…"/"加载失败:"/"输入 RESET 以确认" 与
  "Settings saved."/"Connection successful" 混用；徽章 "ONLINE"（全大写）与正文 "Online" 混用。
- Fix：统一英文文案（与 Win11 一致）；状态词统一 Online/Offline/Warning/Error/Paused/Updating。
- Regression：grep 检查 index.html 无中文 UI 字符串（注释除外）；状态文案单一来源（statusBadge 组件）。

### UI-004（MEDIUM）— Page: 全局（Modal）
- Repro: 打开 Reset Statistics 确认框；按 Tab / Shift+Tab / Escape。
- Expected（spec §78/79）：Escape 关闭；焦点困在 modal 内；关闭后焦点回原按钮。
- Actual：Escape 无反应；Tab 跑到 modal 后面的页面；关闭后焦点留在 body。
- Root cause：confirmModal 只绑了 ok/cancel 的 click。
- Fix：components.js 统一 Modal：焦点陷阱（Tab 循环）、Escape 关闭（非强制）、
  打开时焦点进入、关闭时归还触发元素（`data-trigger` 记录）。
- Regression：实机键盘验证（Tab 循环不出 modal、Esc 关闭、焦点回原按钮）。

### UI-005（MEDIUM）— Page: 全局（Toast）
- Repro: 后端持续报错时连续触发同类 toast。
- Expected（spec §81）：去重/限流，不堆几十个。
- Actual：`toast()` 无去重——相同消息可无上限堆叠（每 4.2s 自动消失但会互相叠加）。
- Fix：components.js toast 以 message+type 为 key 去重：同 key 未消失时重置计时器，不新建节点。
- Regression：代码审查 + 实机（连续失败场景 toast 数量 ≤1）。

### UI-006（LOW）— Page: Dashboard 统计卡
- Repro: 悬停任意统计数字。
- Expected（spec §61-63）：解释定义（Logical/Compute/Cache Ratio/MTP）。
- Actual：`setStat` 把 `el.title` 设为与显示值相同的文本（无意义的重复 tooltip）。
- Fix：统计项 label 增加 InfoTooltip 组件（hover/focus 弹出定义文本），去掉自引用 title。
- Regression：实机 hover 验证 4 处定义 tooltip 文案（spec §62/63 文本）。

### UI-007（LOW）— Page: 全局（Charts）
- Repro: 任意。
- Expected（spec §50/51）：统一 resize 机制，不重复。
- Actual：`initCharts()` 同时绑 ResizeObserver + `window.resize` listener，双路触发 `chart.resize()`。
- Fix：charts.js 单一 ResizeObserver（覆盖 CSS 布局变化含 compact 导航），移除 window listener。
- Regression：代码审查（grep 仅 1 处 resize 绑定）；实机拖拽窗口图表正常。

### UI-008（LOW）— Page: Dashboard 顶栏
- Repro: 任意。
- Expected（spec §16/17）：小圆点 + 适度文案，不大面积着色。
- Actual：ONLINE/OFFLINE 全大写 + 药丸边框，视觉权重偏高。
- Fix：StatusBadge 组件：小色点（8px）+ "Online"/"Offline" 常规字重文本，无大面积底色。
- Regression：实机截图对比。

### UI-009（LOW）— Page: 全局
- Repro: 查看文档标题。
- Expected（spec §113/114）："LlamaMonitor"。
- Actual：`<title>LlamaMonitor Dashboard</title>`（WebView 窗口标题已由 desktop.py 设为
  "LlamaMonitor"，但文档标题仍是 Web 味重命名）。
- Fix：`<title>LlamaMonitor</title>`。
- Regression：代码检查。

### UI-010（LOW）— Page: Overview（Data Quality 区）
- Repro: 任意（网络慢时明显）。
- Expected（spec §42）：各 section 独立加载，不串行阻塞。
- Actual：`refreshDataQuality()` 串行 await `/api/data/quality` 再 await `/api/health`。
- Fix：`Promise.allSettled` 并行。
- Regression：代码审查。

### UI-011（MEDIUM）— Page: 全部图表
- Repro: 全新安装（无数据）/ 0 点时间段。
- Expected（spec §38/110）：统一 EmptyState（icon+标题+说明），不出现空坐标轴大框。
- Actual：0 点时 ECharts 画空白坐标轴（TPS 图）；表格显示 "No data" 单行；MTP position
  图用 title 文本模拟空态（"N/A（服务器未提供 per-position 数据）" 中英混排）。
- Fix：charts.js EmptyState 组件：0 点时隐藏 canvas 显示统一空态（文案按 spec §38）；
  表格空态同一组件复用。
- Regression：Empty DB 实机检查（spec §140）：首屏不"像坏了"，显示 Collecting/No history yet。

### UI-012（LOW）— Page: GPU cards
- Repro: 代码审查（line 1286）。
- Actual：`innerHTML` 拼接中使用转义引号 hack `class=\"gpu-kv\"`（脆弱、难读）。
- Fix：DOM API 构建（createElement/classList），不再拼 HTML 字符串。
- Regression：实机 GPU 卡片渲染一致（截图）。

### UI-013（MEDIUM）— Page: 架构（页面拆分后）
- Repro: 拆分后导航切换。
- Expected（spec §46）：hidden 页面图表容器 0 宽度时 ECharts 初始化会得 0 尺寸 canvas。
- Fix：charts.js 懒初始化——首次页面可见时 init + resize；切回时 resize()。
- Regression：实机反复切换页面，图表宽度正常、无 0 宽（console 检查 echarts 实例数 = 页面需要数，
  无重复 init——instance leak 审计，spec §140）。

### UI-014（LOW）— Page: 全局（主题切换）
- Repro: 主题切换时某图表无数据。
- Actual：`applyTheme` 重绘条件带 `lastLiveSamples.length` / `state.dailyData.length` 判断——
  空数据图表保留旧主题配色直到下次数据到达（最多 30-120s 的"配色不一致"窗口）。
- Fix：主题切换后所有已初始化图表统一重绘（空态图表无成本）。
- Regression：实机主题切换后全图表配色一致。

### UI-015（MEDIUM）— Page: 全局（Offline 状态）
- Repro: llama-server 停止。
- Expected（spec §39/129）：顶部 InfoBar "llama.cpp is currently unreachable.
  Last successful update: 12:43:21"；实时区 --；历史保留；不整页变红。
- Actual：只有顶栏徽章变 OFFLINE + 实时值 --；没有"最后成功更新时间"；
  GPU 不可用提示是纯文本行（无 InfoBar 结构）。
- Fix：全局 Offline InfoBar（components.js InfoBar 组件）：server 离线时显示
  error InfoBar + 最后成功时间（记录 last_success_ts）；GPU 不可用 → warning InfoBar。
- Regression：Server Restart 检查（spec §140）：离线→InfoBar 出现，恢复→消失，无需刷新应用。

### UI-016（LOW）— Page: Usage/Performance
- Repro: 代码审查。
- Actual：指标定义无 tooltip（Logical/Compute/Cache Ratio/MTP Acceptance，spec §61-63）。
- Fix：InfoTooltip 组件 + spec §62/63 定义的文案。
- Regression：实机 hover 验证。

### UI-017（LOW）— Page: Usage/GPU
- Repro: 任意。
- Expected（spec §70/71）：segmented control，统一组件。
- Actual：Today/7/30/All 是四个独立按钮（.ranges）；GPU 15m/1h/6h/24h 同理；
  active 按钮配色硬编码（#232833 等）未走 token。
- Fix：components.js Segmented 组件（Win11 分段控件样式，token 化），两处复用。
- Regression：实机切换功能不变 + 截图。

### UI-018（MEDIUM）— Page: Settings
- Repro: 任意。
- Expected（spec §23/24/25）：Setting Row（左 Title+Description，右 Control），
  按 section 分组；窄屏上下布局。
- Actual：左分类列表 + 右传统表单（label 上 input 下），与 Win11 Settings 风格差距大。
- Fix：重构为 setting-row 布局（CSS grid 1fr/auto，窄屏 <900px 上下堆叠）；
  section 保留（Server/Collector/GPU/Appearance/Web/Storage/Logging/Data/Application/Updates）。
- Regression：实机全 section 走查（spec §84/§90：dirty/Save/Reset/invalid/切页未保存提示）。

### UI-019（MEDIUM）— Page: 全局（状态模型）
- Repro: 任意。
- Expected（spec §130/40）：每模块考虑 loading/ready/empty/offline/error/stale。
- Actual：只有 if(data) 二态；"--" 占位无状态语义区分（loading 与 offline 与 empty 同为 "--"）。
- Fix：统一状态模型：首载 = "--"（skeleton 式 dim）；offline = "--" + dim + 全局 InfoBar；
  empty = EmptyState 组件；error = section 内联 error 文本（独立 section 失败不影响其他，spec §42）；
  stale = 相对时间变色（>2× 轮询间隔 warning 色，spec §41）。
- Regression：各 checklist（spec §85-92）实机走查。

### UI-020（LOW）— Page: Dashboard
- Repro: 代码审查（line 291-307, 104-114）。
- Actual：.ranges 与 #updateBanner 硬编码颜色（#232833、rgba(77,163,255,*)）未走 token；
  light 主题下 .ranges.active 需逐条 override。
- Fix：全部颜色走 tokens.css（--accent/--bg-card-hover 等），组件内不写 @media 颜色分支（spec §3）。
- Regression：grep 检查 css 组件区无硬编码色值（图表 JS 主题除外，集中管理）。

### UI-021（LOW）— Page: 全局
- Repro: Windows"减弱动态效果"开启。
- Expected（spec §76/27）：prefers-reduced-motion 时禁用非必要 transition/animation。
- Actual：无 reduced-motion 处理（toast 动画、progress 动画、主题过渡均播放）。
- Fix：base.css `@media (prefers-reduced-motion: reduce)` 全局压缩 transition/animation 至 ~0.01ms。
- Regression：系统设置开启后实机验证。

### UI-022（MEDIUM）— Page: 全局（ARIA/键盘）
- Repro: 键盘 Tab 走查 / 读屏。
- Expected（spec §73/74/75）：导航按钮语义、Switch 可操作、Modal/Toast 有 ARIA、focus ring 用 accent。
- Actual：导航用 button（尚可）但无 aria-current；switch 是裸 checkbox 无 role=switch 语义标注；
  toast 无 aria-live；modal 无 role=dialog/aria-modal；focus 样式不一致（部分 input focus 只换边框色）。
- Fix：navigation.js 加 aria-current="page"；checkbox+CSS 保持（spec §22 底层仍 checkbox）+ aria-label；
  toastBox role=status aria-live=polite；modal role=dialog aria-modal=true；
  全局 :focus-visible accent ring（base.css）。
- Regression：键盘 Tab/Shift+Tab/Enter/Space 全控件走查（spec §140）。

### UI-023（LOW）— Page: Settings→Updates
- Repro: 代码审查（line 2505-2512）。
- Actual：Updates 1s 轮询用 `setInterval`，与其余任务的 `every()` 自调度是两套 timer 机制；
  每 1s 检查一次 `isVisible()` 条件（虽开销小，机制不统一）。
- Fix：polling.js 中央调度器统一所有周期任务（含 1s update 轮询），任务注册制 + 可见性策略字段。
- Regression：代码审查（全 JS 无裸 setInterval/setTimeout 轮询）；Timer Audit 报告（spec §141.13）。

### UI-024（MEDIUM）— Page: 架构（轮询/生命周期）
- Repro: 代码审查（index.html 1537-1545, 2604-2647）。
- Actual：7 个 `every()` 自调度 setTimeout 在 IIFE 启动时全部创建；页面隐藏仅靠 `isVisible()`
  跳过执行但 timer 本身永不停；若 pywebview 的 window.hide() 不触发 document.visibilitychange
  （spec §49），隐藏后 5s/15s 任务仍全速运行。
- Fix：(a) polling.js 中央调度器（命名任务、统一 in-flight 守卫、统一最小间隔 1s）；
  (b) desktop.py 在 hide/show 时 `evaluate_js("window.__lmSetVisible(bool)")` 桥接真实窗口可见性
  （spec §49"仅在必要时"——此处必要：WebView2 隐藏窗口不保证 visibilitychange）。
- Regression：Tray hide/show 测试（spec §140/106）：隐藏后 CPU 下降（perf 采样对比）、
  显示后立即全量刷新、无重复 timer（performance.getEntries 无法测 JS timer——用调度器内置计数 + 代码审计）。

## 后端相关（UI 暴露，spec §100）

（sweep 过程中如确认后端 bug 以 UI-BACKEND-xxx 追加；目前 API 响应形态与前端契约一致，
null 字段（kv_cache_usage_ratio / GPU 传感器）前端均已按 null-safe 处理——不是 bug。）

## 状态跟踪（Phase 15 完成）

验证方式：PyWebView dev 实例实机 + 同源 iframe harness（7 页遍历 + 主题切换，
uncaught/console.error 均为 0）+ 397 pytest 全绿 + 截图（docs/screenshots/）。
实机中发现并额外修复 1 个 sweep 未预列的真实 bug：`settings.init()` 从未被调用
（所有 Settings 事件未绑定，主题切换/保存/测试连接等失效）——已在 app.js init() 补调用。

| ID | Sev | 状态 | 落地位置 / 证据 |
|---|---|---|---|
| UI-001 | MEDIUM | ✅ 已修复 | app.js `bindSystemThemeListener`：system 模式增 `matchMedia` change 监听，非 system 移除 |
| UI-002 | MEDIUM | ✅ 已修复 | app.js `renderGpuPick` + settings.js `renderGpuDetected`：detected 签名未变不重建 |
| UI-003 | LOW | ✅ 已修复 | 全英文统一文案；状态词单一来源（setStatusBadge） |
| UI-004 | MEDIUM | ✅ 已修复 | components.js modal：Tab 焦点陷阱 + Escape + 打开进焦点/关闭归还触发元素 |
| UI-005 | MEDIUM | ✅ 已修复 | components.js toast 按 message+type 去重，同 key 重置计时不新建，上限 5 |
| UI-006 | LOW | ✅ 已修复 | 指标 label InfoTooltip（Logical/Compute/Cache Ratio/MTP），去掉自引用 title |
| UI-007 | LOW | ✅ 已修复 | charts.js 单一 ResizeObserver（data-chart 映射），无 window resize 双路 |
| UI-008 | LOW | ✅ 已修复 | StatusBadge：8px 色点 + 常规字重 Online/Offline，无大面积底色 |
| UI-009 | LOW | ✅ 已修复 | `<title>LlamaMonitor</title>` |
| UI-010 | LOW | ✅ 已修复 | app.js `refreshDataQuality` 用 Promise.allSettled 并行 |
| UI-011 | MEDIUM | ✅ 已修复 | charts.js setEmpty + GPU 卡/能量 EmptyState：0 点隐藏 canvas 显统一空态 |
| UI-012 | LOW | ✅ 已修复 | GPU 卡 DOM API 构建（createElement/classList），无 innerHTML 引号 hack |
| UI-013 | MEDIUM | ✅ 已修复 | charts.js 懒 init（ensurePageCharts 首次可见时 init+resize）；instanceCount 可审计 |
| UI-014 | LOW | ✅ 已修复 | applyTheme 后 `charts.retheme(chartRenderers())` 全量重绘（含空态） |
| UI-015 | MEDIUM | ✅ 已修复 | 全局 Offline InfoBar + last_success_ts；GPU 不可用 warning 文案 |
| UI-016 | LOW | ✅ 已修复 | InfoTooltip 组件 + spec §62/63 文案（Logical/Compute/Cache Ratio/MTP） |
| UI-017 | LOW | ✅ 已修复 | components.js Segmented 复用 Usage range + GPU range，token 化 |
| UI-018 | MEDIUM | ✅ 已修复 | setting-row（grid 1fr/auto，<900px 堆叠）+ 10 分区；dirty/Save/Reset/未保存确认 |
| UI-019 | MEDIUM | ✅ 已修复 | 统一状态模型：首载 --（dim）/offline -- + InfoBar/empty EmptyState/error 内联/stale 变色 |
| UI-020 | LOW | ✅ 已修复 | 颜色全走 tokens.css（dark/light 两套）；组件无 @media 颜色分支 |
| UI-021 | LOW | ✅ 已修复 | base.css `@media (prefers-reduced-motion: reduce)` 压缩 transition/animation |
| UI-022 | MEDIUM | ✅ 已修复 | aria-current=page；:focus-visible accent ring；toast aria-live；modal role=dialog/aria-modal；switch aria-label |
| UI-023 | LOW | ✅ 已修复 | polling.js 中央调度器统一所有周期任务（含 1s updates），注册制 + 可见性策略 |
| UI-024 | MEDIUM | ✅ 已修复 | 单一调度器（in-flight 守卫 + 最小 1s）+ desktop.py `window.__lmSetVisible` 可见性桥 |

### 附：实机额外发现（sweep 未预列）
- **UI-025（MEDIUM）— Settings 事件未绑定**：`settings.js` 的 `init()`（绑定保存/重置/
  测试连接/dirty/主题切换/自动启动/危险操作/更新等全部事件）从未被调用。
  Fix：app.js `init()` 中 `if (LM.settings && LM.settings.init) LM.settings.init();`。
  证据：harness change-probe——修复前 setTheme 派发 change 后 data-theme 不变；修复后 light/dark 均跟随。

---

## Phase 15.1 附录（用户反馈细化，非新 sweep）

用户四项反馈 → 三项 UI 改动 + 一项运行时 bug：

### 1. 运行时终端闪烁（bug，归 AUDIT-WIN-004）
- Repro：GPU 监控开启时运行程序，每 5s（GPU 轮询）"终端框闪一下"。
- Root cause：`gpu_collector._default_runner` 用 `asyncio.create_subprocess_exec`
  调 nvidia-smi（console 程序）未传 `creationflags`；GUI 无控制台父进程每次调用
  新建可见控制台窗口（Win11 默认终端为 WindowsTerminal.exe）。
- Fix：`creationflags=_NO_WINDOW`（`subprocess.CREATE_NO_WINDOW`，仅 win32）。
- 验证：A/B 实机（pythonw windowless 父进程 + EnumWindows 统计运行期新出现的可见
  终端窗口）——旧行为 6 次调用 → 12 个新窗口；修复后 → 0 个（两次一致）。
  详见 AUDIT_REPORT.md §AUDIT-WIN-004。

### 2. 布局/配色对齐 Windows 11（Overview 重做）
- 反馈"不是 Win11 风格，注意布局和配色" → Overview 重做为 Win11 仪表板布局：
  细状态条（llama.cpp 在线 + 最后更新相对时间）+ **今日大数字卡**（逻辑/计算 Token
  两个 hero 大数字 + 分隔线 + 提示/缓存/输出小行）+ **性能双卡**（解码 TPS/请求 |
  提示 TPS/MTP）+ GPU 行 + 数据质量。tokens.css 已为 Fluent 2 真值（accent
  #60cdff/#005fb8、Mica 透底），本次新增 `.status-strip`/`.today-card`/`.today-hero`
  /`.perf-grid`/`.perf-card`/`.section-label` 等组件 + `--font-size-hero:36px`。
- 证据：DOM dump 实时数据正确填充（在线 / 最后更新于 N 秒前 / GPU 0&1 真实利用率
  显存温度功耗 / 覆盖率）；截图 docs/screenshots/010_overview_new_dark.png、
  011_overview_new_light.png。

### 3. 界面中文化（i18n）
- 反馈"很多英文改成中文，专有名词保留" → 全 UI 中文化：
  - index.html `lang="zh-CN"`；导航/页标题/卡片/设置 10 分区/关于 全中文；
    专有名词保留（llama.cpp、GPU、MTP、TPS、Token、KV 缓存、nvidia-smi、
    SQLite WAL、Ed25519、SHA-256、Release、CSV、UUID、PCIe、SM 时钟…）。
  - JS 动态文案中文化（formatters `formatAgo` 中文相对时间；components 状态词
    在线/离线/警告/错误/更新中/已暂停 + modal 确认/取消/确定；charts 空态/图例/
    坐标轴；app 离线 InfoBar/数据质量/缺口/GPU kv/能耗/范围标签；settings 全部
    toast/kv/危险操作 modal/更新状态）。
- 验证：7 页 + 主题切换 harness `uncaught=[] consoleErrors=[]`；node --check 9 个
  JS 全过；DOM dump 各元素中文 + 实时值正确。

### 状态
| 项 | 状态 | 落地/证据 |
|---|---|---|
| 终端闪烁 | ✅ 已修复 | gpu_collector `_NO_WINDOW`；AUDIT-WIN-004；A/B 验证 0 窗口 |
| Win11 Overview | ✅ 已重做 | index.html + pages.css 新组件；DOM dump + 截图 010/011 |
| 界面中文化 | ✅ 已完成 | index.html + 5 个 JS 文件；7 页 0 console error |
