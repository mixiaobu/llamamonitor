# LlamaMonitor 1.0.1 — Release Notes

**发布日期：2026-09-25 ｜ 平台：Windows 11 x64（Win10 1903+ 亦支持）｜ 架构：Python 3.13 + FastAPI + 原生 JS/ECharts**

**术语审计与 UI 文案修订版（UI/Text Freeze）**。相对 1.0.0：**不改布局、不加功能、
不改数据库统计逻辑、不改 Metrics 采集逻辑**，只做了术语校准、llama.cpp metric
语义校准、中英文统一、tooltip 统一、去机器翻译感与去开发者内部术语。

> 本版本进入 **UI/Text Freeze**：此后 UI 文案以
> [`docs/UI_TERMINOLOGY.md`](UI_TERMINOLOGY.md)（唯一术语字典）为准。

## 为什么发这一版

1.0.0 首发后做最终术语审计，发现若干 **metric 语义错误**（不只是措辞问题），
例如把「最大 Token 记录」当成上下文上限、把「草稿序列」当成草稿数量、
把「排队」误读为延迟。这些会让用户误判服务器状态，因此单独出一个 1.0.1。

## 主要术语校准（节选，完整表见 UI_TERMINOLOGY.md）

| 之前 | 现在 | 为什么 |
|---|---|---|
| 逻辑 Token | **Token 总量** | = 输入 + 缓存复用 + 输出，**派生指标**（tooltip 已标注） |
| 计算 Token | **实际计算 Token** | = 输入 + 输出（不含缓存复用），派生指标 |
| 提示 / 缓存 | **输入 Token / 缓存复用 Token** | 去掉机器翻译感；「缓存」不再像"命中" |
| 缓存率 | **缓存复用率** | = 缓存复用 /（输入 + 缓存复用） |
| 最大 Token 记录 | **上下文高水位** | `n_tokens_max` 是历史最大序列长度，**不是**上下文上限 |
| 上下文上限 | **上下文窗口上限** | 按真实后端字段 `llamacpp:context_max` 核实命名 |
| Busy Slots / 忙碌解码槽 | **平均忙碌 Slot 数** | `n_busy_slots_per_decode` 是**每次 decode 的平均**，非瞬时状态 |
| 草稿序列 | **推测验证轮次** | `spec_decode_num_drafts_total` 实为**验证步骤数**，原词语义错误 |
| MTP / 投机解码 | **MTP（Multi-Token 预测）** | MTP = Multi-Token 预测，≠ 投机解码同义词；推测解码带 tooltip |
| 接受率 | **Draft Token 接受率** | 全项目 MTP 术语统一 |
| 排队（延迟） | **等待中请求** | `requests_deferred` 是**等待**，不是延迟 |
| 处理中 | **处理中请求** | 与等待中请求对齐 |
| Token 吞吐 | **Token 吞吐率** | 单位统一 `tok/s` |
| 服务器运行时 | **服务器运行状态** | 更准确 |
| 历史 / 覆盖率 | **监控历史 / 采集覆盖率** | 覆盖率是**监控时间完整性**，非 Token 精度 |
| Token 丢失？ | **Token 可能缺失** | 是/否，不带问号 |
| 利用率 & 显存 / 硬件趋势 | **GPU 利用率与显存占用 / 功耗与温度** | GPU 页术语统一 |
| API 主机/端口 | **监听地址/端口** | 更准确；0.0.0.0 时显示轻量局域网访问提示 |
| 随 Windows 启动 | **登录时自动启动** | 更准确 |
| 安装模式 / 最新 Release | **安装类型 / 最新版本** | 去 Release 术语 |

**大小写/拼写规范**：`llama.cpp` / `llama-server` / `LlamaMonitor` / `Draft Token`
（禁 `LLama` / `LLM server` / `llamacpp` 裸词；raw metric 名只进 tooltip「来源：」行）。

## 事件文案中文化

监控事件列表不再回退显示英文 event name：
- llama-server **已连接 / 连接中断**（不用上线/离线）
- LlamaMonitor **启动 / 停止 / 重启**
- **数据库备份完成**、**数据库迁移**（detail 显示 "Schema 2 → 3"）
- 计数器重置细节映射中文计数名（不再显示内部字段）

## 质量门

- **新增回归测试** `tests/test_ui_terminology.py`（4 例：废弃术语消失 /
  新术语存在 / 日志备份数改名 / `tok/s` 统一）；`test_api_dashboard.py`
  页面标记断言同步到新术语。
- 全量 `python -m unittest discover -s tests`：**425 例全绿**。
- 9 页真实数据截图核对（概览/用量/性能/GPU/历史/设置×5 分区/关于），
  渲染 DOM 逐一验证新术语出现、旧术语消失。

## 下载

- 安装版（Inno Setup，per-user 无 UAC）：`LlamaMonitor-Setup-1.0.1-win-x64.exe`
- 便携版（解压即用）：`LlamaMonitor-1.0.1-win-x64.zip`
- 完整性：`SHA256SUMS.txt`；签名更新信任链：`release-manifest.json` + `release-manifest.sig`
  （Ed25519，key-2026-09）。验签说明见 `docs/UPDATE_SECURITY.md`。

> 安装版可通过应用内「更新 → 检查更新」自动升级到 1.0.1（验签后静默安装）。
