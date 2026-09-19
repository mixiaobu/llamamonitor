# UI Test Matrix（Phase 15：Windows 11 Fluent UI Redesign）

验证环境：Windows 11 24H2（Build 26200，ReleaseId 2009），系统主题 Dark，
DPI 100%（96），96 逻辑核，NVIDIA GPU（nvidia-smi 可用）。
验证方式：dev 实例（`python desktop.py`，PyWebView/WebView2/edgechromium）+
同源 iframe harness（`_ui_screenshot.py` / `_ui_*_probe.py` 驱动 7 页 + 主题切换，
Capture 客户区 PNG）。无 Node 测试链；后端 397 pytest 全绿。

## 1. 页面 × 主题（spec §132-138）

| 页面 | Dark | Light | 结果 | 截图 |
|---|---|---|---|---|
| Overview | ✅ | ✅ | 数据正常、无控制台错误 | overview_dark.png / overview_light.png |
| Usage | ✅ | ✅ | 图表+今日/月/总+每日表 | usage_dark.png / usage_light.png |
| Performance | ✅ | — | TPS/MTP/runtime | performance_dark.png |
| GPU | ✅ | — | 设备卡+3 图+能量 | gpu_dark.png |
| History | ✅ | — | 数据质量+gaps+导出 | history_dark.png |
| Settings | ✅ | ✅ | 10 分区全走查 | settings_dark.png / settings_light.png |
| About | ✅ | — | 版本/schema/数据目录 | about_dark.png |

- 7 页全遍历 harness：`uncaught: []`、`consoleErrors: []`（spec §98 无控制台错误）。
- 主题切换：dark→light→dark 全部生效（data-theme 跟随；charts 全量 retheme）。
  修复了一个真实 bug：`settings.init()` 从未被调用（事件未绑定）——已在 app.js init() 补上。

## 2. 主题（spec §26/§27/§99）

| 项 | 结果 | 证据 |
|---|---|---|
| Dark 渲染 | ✅ | 截图 + tokens.css [data-theme=dark] |
| Light 渲染 | ✅ | 截图 + tokens.css [data-theme=light] |
| System 跟随 | ✅ | 解析 prefers-color-scheme；systemMq 监听 |
| 系统主题实时切换（UI-001） | ✅ | app.js bindSystemThemeListener 随模式增删 matchMedia 监听 |
| 切换后图表重绘（UI-014） | ✅ | charts.retheme(chartRenderers()) 全量 |
| Mica 透底 | ✅ | pywebview winforms 已设 DWM 38=2（dark）；bg-base 半透明 |

## 3. DPI（spec §51/§105）

| DPI | 结果 | 说明 |
|---|---|---|
| 100% (96) | ✅ | 全部测试在此 DPI 完成（截图 1424×861 客户区） |
| 125% / 150% / 175% / 200% | 待补 | CSS 全 token/rem 化 + ECharts ResizeObserver，分辨率无关；高 DPI 浏览器按系统 scale 放大，无固定 px 布局风险（导航 220px/compact 60px、卡片 padding 均走 token）。建议在 150% 下抽查 Overview/Settings 各一张（见 VISUAL_TEST.md 清单） |

## 4. 窗口尺寸 / compact（spec §52/§53/§54）

| 项 | 结果 | 说明 |
|---|---|---|
| 默认 1400×900 | ✅ | desktop.py WINDOW_WIDTH/HEIGHT |
| 最小 1000×650（spec §53） | ✅ | desktop.py WINDOW_MIN_SIZE=(1000,650) |
| <1100px compact 导航 | ✅ | navigation.js RO on .app；.app.compact 60px 仅图标 |
| 内容单滚动区（spec §56） | ✅ | body overflow hidden；.content overflow-y auto |

## 5. Server 状态（spec §39/§40/§41/§129/§140）

| 项 | 结果 | 说明 |
|---|---|---|
| Online | ✅ | server_online=true → Online 徽章 + 实时值 |
| Offline InfoBar（UI-015） | ✅（逻辑） | server_online=false → 全局 error InfoBar + 最后成功时间（last_success_ts）；恢复→消失 |
| Stale（spec §41） | ✅（逻辑） | age>2×interval → "stale" + warning 色 |
| Backend 不可达 | ✅（逻辑） | /api/status fetch 失败 → 保留上次 + 提示（不整页红） |
| 各 section 独立（UI-010/spec §42） | ✅ | Promise.allSettled 并行；单 API 失败不拖垮其他 |

## 6. GPU（spec §135/§140）

| 项 | 结果 | 说明 |
|---|---|---|
| 可用（NVIDIA） | ✅ | 设备卡 + 3 图 + 能量 |
| 不可用 | ✅（逻辑） | warning 文案 + reason（UI-015） |
| 多卡显隐（UI-002） | ✅（逻辑） | detected 签名未变不重建 |
| 空数据 EmptyState（UI-011） | ✅ | 0 点隐藏 canvas 显示统一空态 |

## 7. 托盘 / 生命周期（spec §49/§106/§120/§140）

| 项 | 结果 | 说明 |
|---|---|---|
| 窗口可见性桥（UI-024） | ✅ | desktop.py hide/show/WM_CLOSE 时 evaluate_js `window.__lmSetVisible(bool)`；polling.js 注册 `__lmSetVisible` |
| 隐藏后 CPU 下降 | ✅ 实测 | 见 §11 性能：可见 2.34% → 隐藏 1.51%/core（-35%），visibleOnly 任务跳过 |
| 显示后立即全量刷新 | ✅ | showPage 钩子 + setAppVisible(true)→refreshAllNow |
| 无重复 timer（spec §140） | ✅ | 单一 LM.poll 调度器（Timer Audit 见报告 §141.13） |

## 11. UI 性能前后对比（spec §120）

测量方法：进程组（python/EXE + 后代 msedgeweb2）TotalProcessorTime 时差 / 区间 = 占单核比例；
WorkingSet 求和。96 逻辑核。dev 实例（新 UI）与 0.14.0 installed（旧 UI 基线）同机同环境。

| 指标 | 旧 UI 基线（Phase 14 前） | 新 UI（Phase 15） | 说明 |
|---|---|---|---|
| RSS（窗口可见，稳定） | 593–596 MB | ~600 MB | 基本持平（+~4MB；模块化 JS + 更多 DOM 元素） |
| CPU（窗口可见，稳定） | 1.77 %/core | 2.34 %/core | 略升（中央调度器注册 12 任务 vs 旧 7–8 个自调度；仍极低） |
| CPU（窗口隐藏到托盘） | — | 1.51 %/core | 隐藏降频生效（较可见 -35%，visibleOnly 任务跳过） |

说明：新 UI 可见态 CPU 略高于旧 UI，根因是中央调度器把原先 7–8 个 `every()` 自调度循环
拆成 12 个细粒度任务（status/runtime/gpuStatus/gpuLive 5–15s + summary/live/dq 30s +
gpuDaily/mtp 60s + daily 120s + updates 30s/1s），轮询更细但每项都有 in-flight 守卫与
visibleOnly 跳过。绝对值仍很低（占单核 2.34%）。隐藏到托盘时 visibleOnly 任务全部跳过，
CPU 降至 1.51%/core，验证了 UI-024 隐藏降频。

## 12. Server 重启 / 离线恢复（spec §140，UI-015）

实测（真实改 Server URL 指向无效端口 → 重启 → 恢复）：
- 离线：`server_online=false` → 顶部红色 InfoBar "llama.cpp is currently unreachable.
  Last successful update: 00:31:57. Live values show --; history is preserved."；
  徽章 "Offline"；实时区 `--`；历史保留（截图 001_offline_dark.png）。
- 恢复：改回 `http://127.0.0.1:9091` → 重启 → `server_online=true`，InfoBar 消失，
  config 有效（has_errors=false, using_defaults=false）。全程无需刷新应用。

## 8. 空数据库 / 首屏（spec §110/§140）

| 项 | 结果 | 说明 |
|---|---|---|
| Empty DB 首屏 | ✅（逻辑） | 统一 EmptyState（Collecting/No history yet），不"像坏了"；数字 --（dim） |
| 各图表空态（UI-011） | ✅ | setEmpty 统一 |

## 9. 键盘 / 无障碍（spec §73/§74/§75/§140）

| 项 | 结果 | 说明 |
|---|---|---|
| 导航 aria-current（UI-022） | ✅ | navigation.js showPage 设 aria-current="page" |
| :focus-visible accent ring | ✅ | base.css 全局 |
| Modal role=dialog + focus trap（UI-004） | ✅ | components.js modal Tab 循环 + Escape + 焦点归还 |
| Toast aria-live（UI-022） | ✅ | toastBox role=status aria-live=polite |
| Switch 语义 | ✅ | 底层 checkbox + aria-label + CSS toggle |

## 10. 已知限制 / 待补

- 高 DPI（125/150/175/200%）截图抽查：建议补 Overview + Settings 各 1 张（见 VISUAL_TEST.md）。
- 离线/在线实机（停/起 llama-server）：逻辑已覆盖；如需真实网络抖动截图，在 Settings 临时改
  Server URL 指向无效端口 → 重启 dev 实例 → 观察 InfoBar → 改回。
- Sleep/Wake：逻辑由可见性桥覆盖（隐藏=降频，唤醒=刷新）；真实挂起/恢复建议手动复现一次。
