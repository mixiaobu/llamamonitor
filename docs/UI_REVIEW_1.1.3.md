# UI Review 1.1.3 — Pixel & Interaction Refinement（最终审查记录）

审查方法（规格强制：不能只靠代码判断完成）：

1. **代码改动 → 运行（8790 dev server，真实 llama-server 9091 数据）→ CDP 截图 28 张**
   （`scripts/ui_snapshot_113.js`：8 页 desktop 1920 dark/light + 8 页 mobile 390 dark/light
   + 320×3 页）→ **视觉审查**（逐张检查对齐/间距/裁切/对比/密度/层级）。
2. **DOM 探针**（CDP `Runtime.evaluate`）：Heat Grid 格子数/数字/切换、断点边界、
   横向溢出、stat-grid 轨道宽度、tooltip/导航/sheet 行为。
3. **gate 脚本**：`smoke_113.js`（nav/DOM/console）、`boundary_probe_113.js`（759/760/761/762
   + 1099/1100 + 1365/1366）、zoom 等效宽度 80/100/125/150%（2400/1920/1536/1280 CSS px）、
   `stress_113.js`（500 页切换 / 1000 sheet / 1000 tooltip / 20 旋转 / 100 主题 / 100 导航）、
   `ui_patrol_113.js`（17 viewport × 8 页 overflow + touch target）。
4. 单元测试 530 项（`python -m unittest discover -s tests`）。

## Gate 结果（最终矩阵，2026-09-27）

| Gate | 结果 |
|---|---|
| 320px 硬门（无横向溢出/裁切） | PASS（mobile-320 3 页截图 + overflow 探针） |
| 390×844 主审查 | PASS（8 页 dark/light，card rows/bottom nav/sheet 正常） |
| 边界 759/760/761/762 | PASS（759/760 = mobile nav 可见；761/762 = 桌面 rail 可见；均无溢出） |
| 边界 1099/1100 | PASS（无溢出） |
| 1365/1366 | PASS（无溢出） |
| 1920 @ 80/100/125/150%（等效 2400/1920/1536/1280 CSS px） | PASS（8 页无横向溢出） |
| stress 500/1000/1000/20/100/100 | PASS（0 未捕获 / 0 console error / 0 ResizeObserver） |
| patrol 17 viewport × 8 页 | PASS（overflow 0 / structural 0 / touch 达标，见 patrol_report.json） |
| console 0 uncaught（28 张矩阵 + 所有探针） | PASS |
| 单元测试 530 | PASS（含 test_mobile_ui 24 项同步 1.1.3 架构） |

## Visual Gate（独立视觉审查，2026-09-28）

27 张最终矩阵截图（desktop-1920 dark×8/light×4 + mobile-390 dark×8/light×4 +
mobile-320×3 + heatgrid 验证图）经**两个独立审计 subagent** 逐张审查：

| 审计 | 范围 | 结果 |
|---|---|---|
| Desktop（Pillow 像素审计：边缘/边距/分布/裁切 + WCAG 精确对比度 + Heat Grid 段落分析） | 12 张 desktop | **PASS** — 0 BLOCKER / 0 HIGH / 6 NIT |
| Mobile（Pillow 像素审计：nav 带行直方图/active pill 几何/status-strip 文本范围/Heat Grid 聚类/320 挤压检查） | 15 张 mobile | **PASS** — 0 BLOCKER / 0 HIGH / 3 LOW |

方法说明：本会话模型无 image 输入（`read_image` 报 model capability error），
审计以 PIL 实际解码像素 + 逐项测量值替代目视（每项结论绑定测量数字，可复现）；
与 CDP 客观 gate（patrol 136/136 无溢出、Heat Grid 48→96 格真实值、
28 张 0 console）交叉印证。

关键测量证据：
- 12 张 desktop 左右 2px 边缘列 100% 底色 → 无横向溢出；纯黑（裁切）比例 0.0。
- light 主题白卡与底色区分明确（overview 白卡 55.9% / 底 31.8%；mobile 白卡
  37-47% vs 底 44-55%）——**排除"灰洗无白卡"**。
- WCAG 精确对比度（线性化 sRGB）：全部文本 token×表面 ≥4.47:1（正文 11.4-16.9、
  次级 5.7-7.7、muted 4.5-5.4、accent 5.2-8.0）；仅 disabled/三级文本 2.9-4.2:1
  （AA-large，设计如此）。
- Heat Grid 双主题实测：73.6k/89.3k accent 像素、每行 31-136 个独立 accent 段
  → 逐核真实数据，非聚合平铺。
- mobile bottom nav 60px+safe-area 精确位置、active accent pill 几何正确、
  无内容压带；status-strip 长模型路径 ellipsis（文本止 x=697 < 卡边 x≈763）。
- 320px 无挤压/换行混乱（breakdown 2 列生效）。

非阻塞发现（9 项 NIT/LOW 合计，均不阻断发布）：
- NIT：chart/表格贴近内容区右缘（边距干净无溢出）、GPU 紧凑卡密度（设计如此）、
  light settings 因输入框 tint 偏平、about 页稀疏（内容少）、Heat Grid accent
  填充为颜色非文本（对比度规则不适用）。
- LOW：usage 末行卡片止于 nav 边界（正常滚动位）、settings @390/@320 左缘 2px
  边框碎片（2x 缩放下 1-2px 亚像素，非裁切）。

## 逐页 × 主题（desktop 1920 / mobile 390，dark + light）

| 页 | desktop | mobile | 备注 |
|---|---|---|---|
| overview | PASS | PASS | status-strip 长 URL/模型行 ellipsis 修复（mobile 不再 723px 溢出）；主机状态 stat-grid 实测 tailBlank=0（1.1.2 报告的"底部 61% 空白"为旧采集伪影，auto-fit 轨道实测满铺） |
| usage | PASS | PASS | daily 表新列序（日期/Token 总量/实际计算/输入/缓存复用/输出/复用率/覆盖率/缺口）对齐；mobile card rows + `.td-group-start` 分组分隔线生效 |
| performance | PASS | PASS | TPS 空态文案；chart bar 宽度分档（48/32/18/12） |
| gpu | PASS | PASS | **GPU 进程表包裹 `.table-wrap`（修复 759-1099px 横向溢出 1059→0）**；温度图 y 轴 nice min（不再 0 基线压扁 50-70°C）；gpu-grid `minmax(min(300px,100%),520px)` |
| system | PASS | PASS | **Heat Grid 真 per-core**（psutil percpu 96 核真实值；物理核 48 格 sibling 均值 / 逻辑核 96 格切换；无数据时聚合口径说明行，禁止画多格假数据） |
| history | PASS | PASS | 缺口/事件表 card rows；数据质量卡 |
| settings | PASS | PASS | 输入框 mobile 44px/16px（iOS 不缩放）；保存按钮非 dirty 禁用；控件宽度类 w-s/w-m/w-l/w-xl |
| about | PASS | PASS | — |

## 1.1.3 本轮修复清单（相对 1.1.2）

- **DESIGN TOKEN**：content-max 唯一来源（+gpu/history 变体）、radius 别名、z-index 10 级
  token、line-height 3 级、mobile ≤760 字号体系（caption 13 → hero 32）、
  `--bottom-nav-height:60px`。
- **断点**：1366 narrow / 1099 compact / 760 mobile / 360 small-mobile 五档，
  旧 900/1100/1200/1400/700/640 全部归并。
- **Mobile 导航**：底部 5 核心页（概览/用量/性能/GPU/系统）；辅助页进每页头部 •••
  overflow（44×44）→ More Sheet（focus trap + body 滚动锁 + ESC + 焦点归还 +
  aria-expanded/aria-modal）；per-page scroll 记忆；重击当前页回顶；active = accent pill
  （替代 3px 顶线）。
- **Heat Grid**：真 per-core（collector `cpu_per_core_percent` + inventory `core_groups`
  三级回退：GLPIEx / cpu_affinity / 整除配对；无数据降级聚合说明）。
- **图表**：bar 宽度分档、token tooltip 双显（K/M/B + 千分位全整数）、GPU 温度轴
  nice lower bound、MTP 0-100%。
- **表格**：`:has()` → 显式 `.mobile-card-table` 类（app.js 注入）；card row 分组分隔线
  （`.td-group-start`）；daily 表列序按阅读逻辑重排。
- **交互**：formatAgo ≤5s "刚刚"（防逐秒跳动）；tooltip hover 300/100ms 延迟
  （.tip-hover-open 类，键盘 focus 即时）；远程 banner 可关闭（sessionStorage）。
- **状态**：stale ≥10s warn 不红；offline InfoBar + 最后成功采样时间；三态徽章
  （就绪/模型加载中/不可用，单一 STATE_TEXT 源）。
- **对比度**：light `--text-muted` #8a8a8a→#6e6e6e（3.39→4.6:1）；dark muted 0.45→0.5；
  暗色 success/warning/error/info 实测 5.67-7.46:1（on #2b2d31，AA 通过）。
- **布局 bug**：GPU 进程表横向溢出（759-1099px）；status-strip 长 URL/模型路径溢出
  （mobile 723px）；两者均包裹/收缩修复并探针验证。
- **输入**：input/select 32→34px；mobile 44px + 16px 字号。
- **README**：界面预览置顶、Phase 11/12/13/14 术语清理、mobile 导航描述更新、
  截图重拍（真 PNG，desktop 57KB / mobile 82KB < 500KB）；CHANGELOG 1.1.2 日期 2026-09-27。

## KNOWN（非阻塞，留档）

- 本 VM（2 socket × 24 核虚拟化）GLPIEx 只报 2 个 core 条目 → 走整除配对回退
  （48 物理组）；标准单 socket HT 机器走 GLPIEx 精确路径（逻辑 0↔物理 0 的 sibling 掩码）。
- GPU 进程表 WDDM 下 VRAM 可能为 `--`（驱动行为，非监控缺陷）。
- 桌面 4K（3840）内容 max-width 1560px 居中，两侧留白为预期（--content-max）。

## 产物

- 28 张矩阵：`artifacts/ui-113/`（不入库，仅本地审查）
- README 截图：`docs/images/llamamonitor-desktop.png`（1440×810 PNG 57KB）、
  `docs/images/llamamonitor-mobile.png`（720×1558 PNG 82KB）
- patrol 报告：`artifacts/ui-113/patrol_report.json`
