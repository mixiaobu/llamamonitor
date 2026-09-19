# Visual Test（Phase 15：视觉/走查清单）

说明：本清单是"人眼走查"用。自动化已覆盖：无控制台错误、主题切换、7 页渲染、
ID 接线、397 pytest。截图在 `docs/screenshots/`（10 张，客户区 1424×861，100% DPI）。
下列项若截图无法判断（间距/对齐/截断），请在 100% 与 150% DPI 下目视确认。

## A. 全局（所有页面）
- [ ] 左侧 NavigationView：品牌 Logo + 7 项（5 顶 + 2 底），选中项左侧 accent 竖条 + 高亮 pill。
- [ ] 内容区唯一滚动；卡片圆角 8px、间距 16px（token 化）。
- [ ] 字体 Segoe UI（Variable 优先），数字 tabular（对齐不抖）。
- [ ] focus 环为 accent 色（Tab 走查每个可聚焦控件）。
- [ ] 无 CDN/外部字体；离线打开正常。
- [ ] 窗口 <1100px：导航收为 60px 仅图标（compact）。

## B. Overview
- [ ] Server Status 卡：Online/Offline 小色点 + 文案（不大面积底色）；Last update 含相对时间。
- [ ] 离线时顶部出现红色 InfoBar（"llama.cpp is currently unreachable. Last successful update: …"）。
- [ ] GPU 摘要卡、今日用量摘要、Data Quality 摘要。
- [ ] 有数据更新可用时顶部蓝色 InfoBar（"View Update" 跳到 Settings→Updates）。

## C. Usage
- [ ] 分段控件 Today/7/30/All 切换，图表+每日表联动。
- [ ] 堆叠面积图（Prompt/Cached/Output）+ tooltip 含 Logical Total。
- [ ] 今日/本月/总 三卡；Logical 数字 hover 显示精确值（8,324,129 式）。
- [ ] 每日表：Coverage/Gaps 列按阈值着色（ok/warn/bad）；空表统一空态。

## D. Performance
- [ ] TPS 折线（60 分钟）；MTP 接受率 + draft/accepted/seqs 统计。
- [ ] "Accepted Tokens by Draft Position" 柱图；Server Runtime（KV/Token Max 等）。
- [ ] 指标定义 tooltip（MTP Acceptance 等，spec §63）。

## E. GPU
- [ ] 设备卡（Util/VRAM/Temp/Power/Fan/Clock/PCIe）；多卡显隐勾选。
- [ ] Utilization&VRAM / Power / Temperature 三图；分段 15m/1h/6h/24h。
- [ ] GPU 不可用：warning 文案 + reason；能量"estimated"注记。
- [ ] 空数据：统一 EmptyState（不空坐标轴大框）。

## F. History
- [ ] Data Quality（coverage 是监测时长而非 token 精度）+ 数据库状态（WAL/PROTECTIVE MODE）。
- [ ] Recent Gaps 表（Start/End/Duration/Source/Reason/Token Loss）。
- [ ] CSV 导出按钮（Daily / GPU Daily）。

## G. Settings
- [ ] Setting Row 布局（左 Title+Desc，右 Control）；10 分区。
- [ ] 脏态：改动后底部出现 "Unsaved changes" + Save 可点；Save 后消失。
- [ ] Reset to Defaults 只重置表单（Save 才写盘）；Save 成功 toast（restart-required 提示）。
- [ ] Test Connection 成功/失败内联结果。
- [ ] GPU 探测列表（签名未变不重建，焦点不丢）。
- [ ] 危险操作（Clear Live / Reset Stats）确认 modal：焦点陷阱 + Esc + Reset 需输入 RESET。
- [ ] Updates：状态/进度条/release notes/签名注记；development 模式注记。

## H. About
- [ ] 版本/数据库 schema/数据目录；Copy Version Info 到剪贴板。

## I. 主题（Dark/Light 各看一遍）
- [ ] 对比度可读；无硬编码色残留（light 下不出现深色块）。
- [ ] 系统主题实时切换：UI 立即跟随（System 模式）。
- [ ] Mica 透底（标题栏/背景半透明，跟随系统材质）。

## J. 高 DPI 抽查（建议 150%）
- [ ] Overview + Settings 各截一张：无截断/重叠/过窄输入框。
- [ ] compact 导航（<1100px 等效）图标不糊。

## K. 交互回归（spec §140）
- [ ] 键盘：Tab/Shift+Tab/Enter/Space 全控件；Modal 焦点陷阱。
- [ ] 减弱动态效果（prefers-reduced-motion）：过渡/动画几乎无。
- [ ] 刷新应用：首屏不"像坏了"（-- 占位 + Collecting）。
- [ ] Server 重启：离线 InfoBar 出现→消失（无需刷新应用）。
- [ ] Tray hide：CPU 下降；show：立即全量刷新。
- [ ] Sleep/Wake：唤醒后数据刷新。
