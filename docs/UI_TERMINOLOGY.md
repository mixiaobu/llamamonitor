# UI 术语字典（唯一术语来源）

> **状态**：Phase 16F（LlamaMonitor 1.0 Final Terminology Audit）已执行并冻结。
> 本文件是 LlamaMonitor 前端（`static/`）所有用户可见文案的**唯一术语来源**。
> 任何新增/修改 UI 文案都必须先对齐本表；被本表「废弃」的术语禁止重新引入。
> 回归防护：`tests/test_ui_terminology.py`（扫描 `static/index.html` + `static/js/*.js`）。

**范围与边界**
- 仅约束前端展示层（label / 标题 / tooltip / toast / 事件名 / 图表图例）。
- **不改变**：内部变量名（`logical_tokens` / `compute_tokens` …）、API JSON 字段、DB 列名、parser keys、config key。前端只做 display label mapping。
- **不改变**：整体布局、功能、采集逻辑、数据库统计逻辑。

**准确性 > 简短 > 中文化**。行业术语保留英文：Prompt / Token / TPS / MTP / KV Cache / GPU / VRAM / PCIe / WAL / Schema / Draft Token。

---

## 1. Token 语义（派生指标 vs 原始指标）

llama.cpp 只上报**增量原始指标**（`prompt_tokens_total` / `prompt_tokens_cached_total` / `tokens_predicted_total`）。下表「派生指标」由 LlamaMonitor 计算，**不是** llama.cpp 官方 metric，tooltip 必须标注「派生指标」。

| 旧术语（废弃） | 新术语（唯一） | 公式 | 类型 | 语义 |
|---|---|---|---|---|
| 逻辑 Token | **Token 总量** | `input + cached + output` | 派生 | 处理的 Token 总规模（含 KV Cache 复用） |
| 计算 Token | **实际计算 Token** | `input + output` | 派生 | 真正消耗算力的 Token（不含缓存复用） |
| 提示 / 新提示 | **输入 Token** | `prompt_tokens` | 原始 | 实际执行 Prompt 处理的 Token（不含缓存复用） |
| 缓存 / 缓存提示 | **缓存复用 Token** | `prompt_tokens_cached` | 原始 | 从 KV Cache 复用、无需重新计算的提示 Token（**不叫**"命中"） |
| 输出 / 生成 / 预测 | **输出 Token** | `tokens_predicted` | 原始 | 模型生成并预测的 Token |
| 缓存率 | **缓存复用率** | `cached / (input + cached)` | 派生 | KV Cache 复用比例 |

> `logical_tokens` / `compute_tokens` 字段名**保留不变**（API/DB），仅展示层改名。

## 2. 速率与吞吐

| 旧术语（废弃） | 新术语（唯一） | 说明 |
|---|---|---|
| 提示处理 / 解码 | **Prompt TPS** / **Decode TPS** | 全项目统一英文（图表图例、卡片、指标条一致） |
| Token 吞吐（标题） | **Token 吞吐率** | 单位统一 `tok/s`（禁止 `t/s`） |
| 最近 60 分钟没有…（空态） | 保留，措辞统一为 "Token 生成活动" | — |

## 3. 服务器 / 请求 / Slot / 上下文

| 旧术语（废弃） | 新术语（唯一） | Raw 来源 | 说明 |
|---|---|---|---|
| 服务器运行时 | **服务器运行状态** | — | 卡标题 |
| 处理中（裸词） | **处理中请求** | `llamacpp:requests_processing` | 正在处理的请求数 |
| 排队 / 排队（延迟） | **等待中请求** | `llamacpp:requests_deferred` | 等待 slot 的请求，**不是延迟** |
| Busy Slots / 忙碌解码槽 | **平均忙碌 Slot 数** | `llamacpp:n_busy_slots_per_decode` | **每次 llama_decode() 调用的平均忙碌 Slot**，历史平均，**非瞬时状态**；指标条顶栏可缩写"忙碌 Slot（平均）" |
| 最大 Token 记录 | **上下文高水位** | `llamacpp:n_tokens_max` | llama.cpp 观测到的最大序列长度（prompt+generation）**历史高水位**，**不是**上下文使用量 |
| 上下文上限（裸词） | **上下文窗口上限** | `llamacpp:context_max`（部分构建 `context_available`） | 服务器报告的上下文长度；无该 metric 时显示 `--` |
| KV 缓存 / KV 缓存使用率 | **KV Cache 使用率** | `llamacpp:kv_cache_usage_ratio` | 仅当后端真的提供 ratio 才显示数值，否则 `--` |

## 4. MTP / 推测解码

> **推测解码（Speculative Decoding）** 是技术名；**MTP = Multi-Token 预测**，是 llama.cpp 推测解码的实现。**MTP 不等于投机解码的同义词**。

| 旧术语（废弃） | 新术语（唯一） | Raw 来源 | 说明 |
|---|---|---|---|
| MTP / 投机解码（标题） | **MTP（Multi-Token 预测）** | — | 卡片标题，tooltip 带 Speculative Decoding |
| 接受率 / MTP 接受率 | **Draft Token 接受率** | `accepted/draft` | 主模型批量验证后的接受比例 |
| 草稿 Token | **Draft Token** | `spec_decode_num_draft_tokens_total` | 草稿 Token 数 |
| 已接受 Token | **已接受 Draft Token** | `spec_decode_num_accepted_tokens_total` | 被接受的草稿 Token |
| 草稿序列（语义错误） | **推测验证轮次** | `spec_decode_num_drafts_total` | = 验证步骤数（verification steps），**不是草稿数量** |
| 接受率趋势 | **Draft Token 接受率趋势** | — | 图表标题 |
| 各草稿位置的已接受 Token | **各 Draft 位置已接受 Token** | `spec_decode_num_accepted_tokens_per_pos_total{position}` | 图表标题/系列名 |

## 5. GPU

| 旧术语（废弃） | 新术语（唯一） | 说明 |
|---|---|---|
| GPU（页标题） | **GPU 监控** | 页标题 + 副标题"通过 nvidia-smi 监控…" |
| 利用率 & 显存 | **GPU 利用率与显存占用** | 图表卡标题 |
| 硬件趋势 | **功耗与温度** | 图表卡标题 |
| 利用率 / 显存 / 风扇 / SM / PCIe（裸词） | **GPU 利用率 / 显存占用 / 温度 / 功耗 / 风扇转速 / SM 时钟 / 显存时钟 / PCIe 链路** | 卡片与图表统一 |
| 由采样功耗积分估算（/api/gpu/daily） | **根据采样功耗随时间积分估算，仅供参考。** | 去开发者路径；单位 Wh / kWh |

## 6. 历史 / 数据质量

| 旧术语（废弃） | 新术语（唯一） | 说明 |
|---|---|---|
| 历史（页/导航） | **监控历史** | 页标题 + 导航项 |
| 覆盖率（裸词） | **采集覆盖率** | tooltip：衡量监控数据**时间完整性**，**不代表 Token 统计精度** |
| Token 丢失？（带问号） | **Token 可能缺失** | 是/否，不带问号 |
| 开始/结束/时长（表头） | **开始时间 / 结束时间 / 持续时间** | 缺口表 |
| 缺口来源 LLM/应用/GPU | **llama.cpp / LlamaMonitor / GPU 采集** | 来源映射 |
| 缺口原因 服务器离线/监控重启/系统睡眠/无效指标 | **llama-server 不可达 / LlamaMonitor 重启 / 系统休眠 / 采集异常** | 原因映射（禁内部枚举） |

## 7. 事件（监控事件列表）

> 事件文案中文化，**不显示内部 event_type 英文**（如 `migration` → "数据库迁移"）。

| event_type（内部，保留） | 展示文案（唯一） |
|---|---|
| monitor_start | LlamaMonitor 启动 |
| monitor_stop | LlamaMonitor 停止 |
| monitor_restart_gap / monitor_restart | LlamaMonitor 重启 |
| server_online | llama-server 已连接 |
| server_offline | llama-server 连接中断 |
| metrics_valid | 指标有效 |
| invalid_metrics | 采集异常 |
| database_protective_mode | 数据库进入保护模式 |
| database_recovery | 数据库恢复 |
| database_write_failure | 数据库写入失败 |
| database_integrity_error | 数据库完整性错误 |
| counter_reset | 计数器重置（counter 名映射中文，不显内部字段） |
| backup_created | 数据库备份完成 |
| migration | 数据库迁移（detail "Schema 2 → 3"） |
| update_* | 更新检查 / 更新检查失败 / 发现新版本 / 更新下载开始 / 完成 / 取消 / 更新校验失败 / 更新安装开始 / 中止 / 更新前备份失败 |

## 8. 设置

### 8.1 llama-server
| 旧 | 新 | 说明 |
|---|---|---|
| 服务器（卡标题） | **llama-server** | desc：配置 llama-server 监控端点。LlamaMonitor 仅通过 HTTP GET 读取指标数据，不代理或修改推理请求。 |
| 基础地址（…不要包含 metrics 路径） | **llama-server 地址** | desc 改为"…不含指标路径。" |
| Metrics 路径 | **指标端点路径** | 例如 /metrics |
| 连接超时（秒。） | **连接超时** | desc：请求超时时间（秒）。 |

### 8.2 指标采集（原"采集器"）
| 旧 | 新 |
|---|---|
| 采集器（卡） | **指标采集** |
| 轮询间隔 | **指标采集间隔** |
| 实时数据保留 | **实时采样保留时长** |

### 8.3 外观
| 旧 | 新 |
|---|---|
| 面板刷新间隔 | **界面刷新间隔**（前端实时数据的刷新周期，秒） |
| 默认历史范围 | **默认统计范围**（天数，用作用量页默认显示范围） |

### 8.4 GPU
| 旧 | 新 |
|---|---|
| GPU desc（Windows NVIDIA 只读监控…） | **通过 NVIDIA nvidia-smi 读取 GPU 状态。LlamaMonitor 仅采集监控数据，不修改 GPU 配置。** |
| 轮询间隔（GPU） | **GPU 采集间隔** |
| 历史保留 | **GPU 实时采样保留时长** |
| 检测到的 GPU（不勾选=…） | **监控的 GPU**（desc：未选择时监控所有已检测到的 GPU。） |

### 8.5 存储 / 备份 / 日志
| 旧 | 新 | 说明 |
|---|---|---|
| SQLite WAL（预写日志） | **SQLite WAL** | desc：Write-Ahead Logging，预写日志模式（推荐） |
| 备份 desc（自动一致性快照…） | **定期创建一致性数据库备份。** | |
| 最大日志大小 | **单个日志文件上限** | MB |
| 备份数量（日志语境） | **轮转文件保留数** | 保留的轮转日志文件数量（数据库备份语境保留"备份数量"） |

### 8.6 数据（操作按钮）
| 旧 | 新 |
|---|---|
| 刷新 | **刷新状态** |
| 备份数据库 | **立即备份** |
| 数据库检查 | **检查数据库完整性** |
| 清空实时历史 | **清除实时采样历史** |
| 重置所有统计 | **重置统计数据** |

### 8.7 应用
| 旧 | 新 | 说明 |
|---|---|---|
| 应用 desc（…API 主机/端口…） | **运行状态、自动启动与监听设置。** | 监听地址/端口更改在重启后生效 |
| 随 Windows 启动 | **登录时自动启动** | |
| API 主机 | **监听地址** | 0.0.0.0 显示轻量 Warning；127.0.0.1/localhost 显示"仅本机访问。"（仅展示，不加逻辑） |
| API 端口 | **监听端口** | |
| 应用模式 / 单实例 / 平台 / 应用数据 | **运行模式 / 单实例运行 / 运行环境 / 应用数据目录** | 运行信息行 |

### 8.8 更新
| 旧 | 新 | 说明 |
|---|---|---|
| 更新 desc（来自 GitHub Release…） | **通过 GitHub Releases 检查和安装更新。更新包会经过 Ed25519 签名与 SHA-256 完整性校验。** | |
| 安装模式 | **安装类型** | |
| 最新 Release | **最新版本** | |
| 状态（更新卡） | **更新状态** | 状态文案中文化，禁 IDLE/READY_TO_INSTALL/ERROR 裸显 |

**更新状态文案（唯一）**：未检查 / 已是最新版本 / 发现新版本 / 正在检查 / 正在下载 / 正在验证 / 已准备安装 / 正在安装 / 检查失败 / 验证失败。

## 9. 关于
| 旧 | 新 | 说明 |
|---|---|---|
| llama.cpp 的本地只读监控工具 | **llama.cpp 本地只读监控工具** | 副标题 |
| 数据库 Schema | **数据库 Schema 版本** | |
| 数据目录（值是 monitor.db 路径） | **数据库文件** | 值是 DB 文件路径 |
| （读取 /metrics 端点…） | **仅通过 HTTP GET 读取 llama-server 指标数据进行监控，不代理或修改推理请求。** | 去 API 路径 |

## 10. 全局大小写 / 拼写规范
- 保留英文写法（区分大小写）：**llama.cpp / llama-server / LlamaMonitor / KV Cache / Draft Token / Multi-Token / Speculative Decoding**。
- **禁止**：`LLama` / `LLM server` / `llamacpp`（裸词，raw metric 名 `llamacpp:*` 仅允许出现在 tooltip「来源：」行）。
- 中文用中文标点；数值与中文之间不加多余空格（单位除外，如 `tok/s`）。

## 11. Tooltip 规则
- **派生指标** tooltip 必须标注"派生指标"（Token 总量、实际计算 Token、缓存复用率）。
- **Raw metric** 仅允许进入易误解指标 tooltip 的「来源：」行：**上下文高水位 / 平均忙碌 Slot 数 / 推测验证轮次 / KV Cache 使用率 / 上下文窗口上限 / 输入·缓存复用·输出 Token**。
- 其余指标 tooltip 只写中文语义，不暴露 raw 字段。
