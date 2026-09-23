# LlamaMonitor 1.0.0 — Release Notes

**发布日期：2026-09-24 ｜ 平台：Windows 11 x64（Win10 1903+ 亦支持）｜ 架构：Python 3.13 + FastAPI + 原生 JS/ECharts**

LlamaMonitor 是一个针对 llama.cpp `llama-server`（`--metrics`）的本地 Token 用量监控工具：
实时/按天统计 Compute/Logical Tokens、Prompt/Decode TPS、MTP 投机解码接受率、
NVIDIA GPU 利用率/显存/功耗/温度与能耗估算，Win11 Fluent 桌面端 + 局域网手机网页端。

## 1.0.0 是什么

0.16.x 稳定线的**正式首发版本**。本版本经过完整 Final Release 流程：
Feature/UI/Schema/API 冻结，75 项 Release Gate 全部执行，
验收记录见 `docs/FINAL_RELEASE_REPORT_1.0.0.md`。

## 本版（相对 0.16.4 首发候选）包含的改进

### 功能与可靠性
- **前台自适应轮询**（0.16.12）：窗口可见用短间隔（数据实时感），隐藏降频；
  切回前台立即全量刷新；轮询 in-flight 去重，不重叠。
- **概览页重设计**（0.16.12/0.16.13）：趋势图、累计区、性能卡重排；
  信息架构重设计 + Win11 Fluent 布局（Phase 16B）。
- **统一 UI 打磨**（0.16.16–0.16.20）：布局/视觉/组件系统统一、页面结构清理、
  间距/圆角 design tokens 化、表格/卡片细节精修。
- **信息完整**（0.16.13）：事件流（history 页）、About 完整填充、
  数据缺口（data gaps）与监控覆盖率（monitoring coverage）可视化。
- **重置统计语义**（0.16.12）：重置一并清除数据缺口；保留 Counter 基线
  （重置后不重复计入旧 Token）。
- **折叠导航**（0.16.11）：品牌图标即折叠入口；侧边栏手动展开/收起记忆。
- **全界面中文化**（0.16.5）。

### 移动端 / 局域网（Phase 16E–16H，0.16.16–0.16.22）
- 手机/局域网访问体验：时间筛选段控适配窄屏、显存/能耗卡折行修复、
  chip 去灰底竖排、轴标签抽稀、100dvh 移动端视口、no-cache 头、
  触摸高亮与 tap 目标尺寸（0.16.18–0.16.22）。
- **远程安全收敛**（0.16.21/0.16.22）：远程（非 loopback）客户端
  - 不再请求 local-only 端点 → 浏览器网络面板**零 403 噪音**；
  - 隐藏「设置」导航入口；设置页远程只读；
  - **1.0.0 新增**：只读端点（`/api/status`、`/api/data/info`）对远程
    只返回文件名，不再泄漏完整 Windows 路径（用户名/数据目录）。

### 默认值
- **默认主题：跟随系统（system）**——Win11 Fluent 语义；
  之前默认 dark，可手动切换。
- **用量页时间筛选默认：7 天**（前端内置默认与服务端一致；之前 30 天）。

### 安全（Ed25519 更新信任链，Phase 13 起）
- 安装版应用内更新：Ed25519 签名 manifest（schema 1，canonical bytes）
  + 公钥内置（`update_keys.py`，key-2026-09）；验签先于 JSON 解析；
  篡改 manifest / installer / 缺 sig 全部拒绝（回归测试覆盖）。
- 修改类 API（改配置/清数据/备份/注册表/退出/开文件夹/更新操作）
  全部 **loopback-only**（`web.host=0.0.0.0` 后局域网只读）。

## 安装

- **安装版**：`LlamaMonitor-Setup-1.0.0-win-x64.exe`（Inno Setup，per-user，
  安装到 `%LOCALAPPDATA%\Programs\LlamaMonitor`，数据在
  `%LOCALAPPDATA%\LlamaMonitor`；无管理员权限、不写注册表 Run 之外的系统区）。
- **便携版**：`LlamaMonitor-1.0.0-win-x64.zip`（解压即用；顶层 `LlamaMonitor/` 目录）。
- 启动后自动监听 `127.0.0.1:8765`（可在设置里改为 `0.0.0.0` 供局域网访问）。
- 需要 llama-server 以 `--metrics` 启动（默认探测 `http://127.0.0.1:8080/metrics`，
  可在设置里改地址与路径）。
- GPU 监控依赖 NVIDIA 驱动 + `nvidia-smi`（缺失时 GPU 卡片显示 N/A，不影响 Token 统计）。

## 数据与隐私

- 数据全部本地：SQLite（WAL，schema v4，10 张表）+ 备份目录，均在
  `%LOCALAPPDATA%\LlamaMonitor`；无遥测、无网络上报（更新检查仅在用户触发时
  访问 GitHub API）。
- Token 口径（冻结定义）：
  - Compute Tokens = Prompt + Output
  - Logical Tokens = Prompt + Cached + Output
  - Cache Ratio = Cached / (Prompt + Cached)；MTP Acceptance = Accepted / Draft
- 离线/重启期间的已知限制：llama-server 重启使 Counter 归零，
  [最后读取, 重启] 之间产生的 Token 不可恢复（`possible_token_loss` 标记，
  历史页"数据质量"可见）；监控器自身重启不丢基线、不重复计入（回归测试）。

## 已知限制（写入 1.0.0）

- llama.cpp 版本间 `/metrics` 指标命名/字段有差异（按 capability 探测降级）。
- GPU 能耗为**采样功率梯形积分估算**，非智能电表精度。
- 仅 Windows x64；无 Authenticode 代码签名时 SmartScreen 可能提示
  （Ed25519 签名保护的是**更新通道完整性**，与 Authenticode 是两回事，
  见 `docs/UPDATE_SECURITY.md`）。
- 浏览器 WebView2（pywebview）依赖 Windows 自带 Evergreen WebView2 运行时
  （Win11 默认具备）。

## 升级路径

- 从 0.13.x/0.16.x：直接安装 1.0.0（安装器按数值比较，1.0.0 > 0.16.x，
  升级路径无降级拦截）；数据库 schema 自动迁移（v0→v4，迁移前自动备份，
  迁移矩阵有回归测试）。
- 1.0.0 之后：安装版可走应用内自助更新（Ed25519 信任链）。

## 校验

- `SHA256SUMS.txt`：全部工件 SHA-256。
- `release-manifest.json` + `release-manifest.sig`：Ed25519 验签
  （公钥内置于客户端，见 `docs/UPDATE_SECURITY.md`）。
- `docs/BUILD_INFO_1.0.0.md`：构建环境、commit、依赖锁定、复现步骤。
