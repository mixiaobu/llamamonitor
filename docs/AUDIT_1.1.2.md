# LlamaMonitor 1.1.2 — Mobile & Visual Experience 审计与改造记录

**基线**：1.1.1（commit b36a209，506 测试全绿，schema 5）
**范围**：PC 视觉精修 + 真 Mobile 响应式 + Typography/Icon/Radius/Spacing 统一 + Touch UX + Chart/Table/Settings Mobile + 远程只读 UX + 可访问性 + README 截图。不改 schema、不加新功能。

## 一、1.1.1 静态层审计发现（改前基线）

### 结构事实（改造依据）

- 静态层：`static/index.html`（1303 行，8 页）+ `css/{tokens,base,layout,components,pages}.css` +
  `js/{icons,formatters,api,components,charts,navigation,polling,settings,system,app}.js`。
  无框架、无 CDN，JS 按模块 IIFE 挂 `window.LM.*`。
- 中央调度器 `LM.poll`（polling.js）：所有周期任务统一注册，防重复、防重叠、
  可见性自适应——**Mobile 改造不新增第二套轮询**（规格 83）。
- 图表：charts.js 单一 ResizeObserver + 懒初始化，`ensurePageCharts` 在切页时调用；
  图表高度 `.chart 320px / ≤1400px 280px / ≤700px 240px`。
- `LM.api.isLocal()`（api.js:71）：loopback 判定（含 0.0.0.0），**远程只读判定的
  基建已存在**；settings.js 已有远程禁控件 + 提示文案，app.js 已有远程隐藏设置导航。
- reduced-motion：base.css:135 已有全局 `prefers-reduced-motion` 规则。
- 100dvh：layout.css 已有（.app）。

### 移动端问题清单（规格三/十一~五十二对照）

| # | 级别 | 发现 |
|---|------|------|
| M-01 | HIGH | ≤1100px 仍是 60px icon rail（compact 模式），手机上占宽且无 Bottom Navigation（规格 11/12 明确禁止） |
| M-02 | HIGH | 无 `<=760px` 真 Mobile 模式：无 Bottom Nav、无 More Sheet、无 safe-area（规格 12/13/14） |
| M-03 | HIGH | 每日明细表 `.table-daily min-width:920px`、缺口表 780px、事件表——手机全靠 .table-wrap 横滚（规格 30/31 要求 Card Rows） |
| M-04 | MEDIUM | Settings rail ≤900px `flex-wrap` 折多行（规格 47 要求单行横向 tabs）；无 sticky save bar（规格 49） |
| M-05 | MEDIUM | InfoTooltip 只有 hover/focus（规格 17 要求 coarse pointer 下 tap 开/关 + 不超屏） |
| M-06 | MEDIUM | 触屏 hit target：`.tip-btn` 14px、`.btn.small` 26px 高、nav-item 36px 高——coarse 指针下不足 44px（规格 16/78） |
| M-07 | MEDIUM | hover 效果未包 `@media (hover:hover)`——手机点按后残留 hover 态（规格 55） |
| M-08 | MEDIUM | 无 `@media (pointer:coarse)` 分支（规格 54） |
| M-09 | MEDIUM | GPU 页 `.charts-2col` ≤1200px 已单列（OK），但 ≤900px GPU pick chips 竖排全宽占高（规格 37 要求横滚）；GPU 卡 20+ 指标一次全展开（规格 40 要求分层折叠） |
| M-10 | MEDIUM | 系统页 per-core 是 details 折叠 + fan-row 列表（规格 42 要求 Heat Grid）；库存/传感器长列表手机无折叠（规格 41） |
| M-11 | LOW | Toast 固定在右下（规格 51 要求 mobile 底部居中、在 Bottom Nav 上方） |
| M-12 | LOW | 远程只读：设置导航被隐藏但没有全局轻量 Banner（规格 64 要求显示只读说明） |
| M-13 | LOW | 无 forced-colors / Windows 高对比支持（规格 86） |
| M-14 | LOW | 表格 hover 行高亮在手机点按后残留（同 M-07 机制） |

### Desktop 问题清单（规格 56~58/60/62/88/89/90）

| # | 级别 | 发现 |
|---|------|------|
| D-01 | MEDIUM | 所有页面共用 max-width 1520px（≥2560px 1920）——规格 57 要求逐页：数据页 1500-1600 / chart-heavy 1600 / Settings 1200-1350 / About 900-1000 |
| D-02 | LOW | 硬编码值散落：`.stat-value.large 28px`（components.css:108）、`.energy-row max-width:480px`、`.chart 320px` 等页面级裸值应归 token 或就地注释 |
| D-03 | LOW | CSS/HTML 大量 Phase 15/16B/16C/16D/16E 流水账注释（规格 89 要求清理，保留 WHY） |
| D-04 | LOW | 概览副标题"10 秒看懂：…"偏营销（规格 90 要求改为"集中查看 llama.cpp 服务、Token、推理性能、GPU、主机与数据采集状态。"） |
| D-05 | LOW | Overview 手机卡片顺序为 服务→今日→性能→主机→GPU→数据质量，与规格 22 一致（无需改序）；今日卡 hero 双大数 + 3 列 breakdown 在 320px 会挤（规格 24 要求窄屏 1 列/2+1） |
| D-06 | LOW | GPU 进程表（4 列）/ Slot 卡 / 模型信息在 ≤760px 无专门布局（规格 35/36/744） |

### 可复用机制（不重造）

- 单 RO 图表 resize、`LM.poll` 调度、`isLocal()`、reduced-motion、100dvh、
  `escAttr`/`compact()` 格式化、status-badge、info-bar 槽位、modal focus trap。

## 二、Telemetry Fluent 设计语言（1.1.2 正式化）

Quiet / Technical / Dense-but-readable / Data-first / Low-noise / Professional。
保留：Segoe UI Variable、Neutral Dark/Light Surface、Cyan-Blue Accent、Tabular
Numbers、Subtle Borders、Status Dot、Cards。特征：数值优先、Label 弱化、状态用
小型 Signal、大面积颜色避免、Accent 只用于当前导航/关键交互/Chart/状态强调、
卡片靠层次+边框+留白区分、不用重阴影/玻璃/高饱和渐变。Active nav = 左 Accent Rail
（已有）。

## 三、实施计划（按 PASS）

- B：tokens.css 扩 Telemetry Fluent 完整 token 体系（typography 双端 / icon / radius /
  spacing / mobile 布局变量）+ base.css tabular-nums 扩展 + hover:hover 收敛。
- C：layout 层 ≤760px 真 Mobile 模式：Bottom Navigation（5 项：概览/Token/性能/GPU/更多）
  + More Bottom Sheet（系统/监控历史/设置/关于+版本号）+ safe-area + content 占满 +
  mobile 页头纵向。
- D：组件 mobile：coarse 指针 touch target ≥44、tap tooltip、表格→card rows（daily/
  gaps/events/gpu-proc）、settings 横向 tabs + sticky save bar、chart mobile 参数、
  GPU chips 横滚 + 卡分层折叠、系统页 heat grid + 折叠、Toast/Modal mobile。
- E：远程只读 Banner（全局）、forced-colors、逐页 max-width、注释/文案清理。
- F：回归测试（506 全绿 + 新增）。
- G：响应式矩阵（CDP 真机数据：320/360/375/390/412/430/768/820/1024/1280/1366/
  1600/1920/2560/3840 + 横屏 844×390/915×412；scrollWidth≤clientWidth gate；
  touch target gate）。
- H：真实截图 desktop 1920×1080 + mobile 390×844（Overview dark）→ docs/images/ +
  README 界面预览 + 手机访问说明。
- I：性能回归（tray idle / desktop open / mobile connected 的 CPU/RAM/线程/句柄对比
  1.1.1）+ 已装 1.1.1→1.1.2 升级验证 + version/CHANGELOG。
- J：commit + push + tag + CI Release + 信任链 + updater 验证。
