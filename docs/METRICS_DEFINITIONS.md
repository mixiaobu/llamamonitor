# LlamaMonitor 指标定义（Phase 14，1.1 增补）

> 本文档定义 Dashboard / API / 数据库里每个字段的**精确计算规则**。
> 源：`stats.py`（纯函数）+ `collector.py`（组装）+ `gpu_collector.py`（能耗）+
> `system_collector.py` / `llama_runtime_collector.py` / `hardware_sensor_provider.py`
> （1.1 系统 / Runtime / 硬件遥测，见 §8）。
> 规则与代码一一对应，任何行为以本文档 + 单元测试为准。

## 1. 采样与时间

- **轮询**：默认每 2s 抓取一次 `GET {llama_server}/metrics`（Prometheus 文本）
  （1.2 起全链路默认 2s；1.2 前默认 5s）。
- **时间戳**：`now = clock.now()`（Unix 秒，本机墙钟）。生产用系统时钟；测试可注入
  `FakeClock`。所有 daily 归属、48h 清理、覆盖率窗口都以此 `now` 为唯一时间源
  （AUDIT-ASYNC-005：`/api/daily` 的 cutoff 与 collector 同源，不再错位）。
- **daily 归属日期**：`local_date(now)`（本机 `YYYY-MM-DD`）。跨午夜的一轮，
  delta 归属**该样本时间戳**的日期（不是轮询发起时间）。

## 2. Counter delta 与 reset 检测（stats.counter_delta）

llama-server 重启会让 Prometheus Counter 归零重计。每个 Counter **独立**检测：

| 条件 | delta |
|---|---|
| `current is None`（本轮字段缺失） | `None`（展示 N/A；state 保持旧值） |
| `previous is None`（首次见到） | `0.0`（仅建 baseline，不当历史数据） |
| `current >= previous` | `current - previous` |
| `current < previous`（**判定为 llama-server 重启**） | `current`（整段当作本周期新增） |

- reset 触发 `counter_reset` 事件（每个 Counter 独立记录）。
- 缺失字段**绝不**当 0、**绝不**当 reset。

跟踪的 9 个 Counter（全名 → 短名）：
`prompt_tokens / cached_tokens / prompt_seconds / output_tokens /
predicted_seconds / n_decode / draft_tokens / accepted_tokens / draft_sequences`。

## 3. 派生速率

| 字段 | 公式 | 返回 None 的条件 |
|---|---|---|
| `prompt_tps` | `prompt_tokens_delta / prompt_seconds_delta` | 任一为 None 或秒 delta ≤ 0 |
| `decode_tps` | `output_tokens_delta / predicted_seconds_delta` | 同上 |
| `mtp_accept_rate` | `accepted_delta / draft_delta × 100` | `accepted` 为 None 或 `draft_delta` 为 0/None |

- **MTP 接受率是"接受 token / 草稿 token"的百分比**，不是"接受序列 / 草稿序列"。
- per-position 接受率（`spec_decode_num_accepted_tokens_per_pos_total`）每个 position
  是独立 Counter，同样独立 reset 检测；首次出现的 position 只建 baseline。

## 4. GPU 指标（gpu_collector）

- **能耗 `energy_wh`**：相邻采样功率的**梯形积分**
  `∑ (P_i + P_{i+1}) / 2 × Δt`（Δt 为两样本间隔秒 / 3600）。
  要求 `power_draw_w` 与 `prev_power` **都非 None**（AUDIT-WIN-002：
  上一轮 power 为 None 时不产生能耗段，不再用 0 填充）。
- **利用率 / 显存 / 温度 / 频率**：`N/A` / `Not Supported` 一律解析为 NULL（不猜 0）。
- **GPU 身份**：用 `nvidia-smi` 的 `gpu_uuid`（稳定）；`gpu_index` 仅显示
  （驱动/插拔会变）。

## 5. 数据质量（/api/data/quality）

- **monitoring_coverage_percent（监控覆盖率）**：
  当天**首有效样本 → 末有效样本**（若仍在监控则到 now）时间窗内，
  **无已知缺口**的秒数占比 × 100。
  ```
  coverage = 100 × (1 − 窗口内缺口秒 / 窗口总秒)   （窗口 ≤ 0 时 = 100）
  ```
- **这是"监控连续性"，不是"Token 统计准确率"**。覆盖率 100% 不代表 delta 精确，
  只代表窗口内没有已知断档。
- **gap_count**：当天缺口数（含未结束的 open_gap）。
- **possible_token_loss**：缺口期间核心 Counter 发生 reset，可能有不可恢复 token 丢失。
- **last_valid_sample_seconds_ago**：距最近有效样本的秒数（数据新鲜度）。

### 缺口（data_gaps）模型

- `source`：`llama` / `gpu` / `application`。
- `reason`：`server_offline` / `invalid_metrics` / `monitor_restart` /
  `system_pause_or_sleep` / `unknown`。
- 检测：
  - 离线轮（抓取失败）→ 开 `server_offline` 缺口；
  - 抓到 metrics 但核心 Counter 值无效（NaN/Inf/负值）→ `invalid_metrics`
    （样本不可信，按缺口计；恢复后该缺口闭合）；
  - 有效样本间 monotonic 间隔 > 阈值（默认 15s）且无离线轮 → `system_pause_or_sleep`
    （进程被系统挂起，墙钟跳变）；
  - 应用重启后首个有效样本补记 `monitor_restart`（仅当断档 ≥ 阈值）。
- 缺口**永久保留**（是历史数据可信度的一部分，不做 48h 清理）。

## 6. DB 写失败与 baseline（collector）

- 持久化在**单个事务**（state + daily + live + per-position + 48h 清理）。
- 事务失败 → 本轮不落盘、**保持旧 baseline**，下轮从旧 baseline 重算完整 delta
  （不丢不重）；记 `database_write_failure` 事件。
- 写失败恢复后记 `database_recovery` 事件。
- **"readonly database" 持续失败** → DB 健康置 `unavailable`
  （AUDIT-SEC-004，/api/health 同步反映），写入恢复后回到 `healthy`。

## 7. 数据库保留（见 docs/STORAGE_ESTIMATE.md）

| 表 | 保留 |
|---|---|
| live_samples / gpu_samples / system_samples | 48h（可配置） |
| monitor_events | 365 天 且 ≤ 100,000 行 |
| daily_usage / mtp_position_daily / system_daily | 永久 |
| data_gaps | 永久 |
| backup_history | ≤ 1,000 行 |

## 8. 1.1 新增指标（System / llama Runtime / 硬件传感器）

1.1.0 起新增三类遥测，口径如下（源：`system_collector.py` /
`llama_runtime_collector.py` / `hardware_sensor_provider.py`）。

### 8.1 系统监控（psutil，基础，故障隔离于 llama/GPU 采集）

- **CPU 使用率 / 内存 / 磁盘 / 网络**：psutil 直接读；`poll_interval_seconds`
  周期采样，历史 `history_interval_seconds` 落库。
- **CPU 能耗 `cpu_energy_wh`**：`power_w` 对 monotonic Δt 的梯形积分
  `∑ (P_i + P_{i+1}) / 2 × Δt / 3600`。与 GPU 能耗同口径。
  - 长 gap（monotonic Δt 超阈值）不积分（系统睡眠/挂起不累计假能耗）；
  - 跨午夜的能量按 `prev_wall`/`curr_wall` 所在日期**分段归属**
    （AUDIT-1.1.1 DATA-1111-006：与 GPU 对齐，不再整段归当日）。
- **组件功耗合计**：API 层相加 `CPU package power + 全部 GPU power_draw_w`；
  任一侧无数据 -> 合计 `None`（不猜 0）。

### 8.2 llama Runtime 遥测（/health /slots /props /v1/models，分频调度）

- **运行时状态**：`ready` / `unavailable`。**连续 5 次** health 抓取失败才
  判 `unavailable`（AUDIT-1.1.1 REL-1111-005 防抖；1.3.1 起与 Token 采集器 /
  前端 streak 的 OFFLINE_TOLERANCE=5 统一）；任一次成功立即复位。
- **Slot 槽位**（`/slots`，`is_processing` 区分忙/闲）：
  - 术语统一（见 docs/UI_TERMINOLOGY.md）：上下文窗口上限 / 输入 Token /
    缓存复用 Token / 已处理提示 Token / 输出 Token / 剩余输出 Token /
    缓存复用率 / MTP。
  - **空闲槽位的 per-request 字段是上一次请求的残留**（最长 ~10s 后更新），
    UI 淡化 + 注脚提示（AUDIT-1.1.1 BUG-1111-010），不当当前状态。
- **缓存复用率**（Slot 级）：`cached / (prompt + cached)` × 100 ——
  注意与历史"缓存复用率"（跨请求统计）口径不同；代码字段名区分，无歧义。

### 8.3 高级硬件传感器（LibreHardwareMonitor Bridge，可选）

- **严格只读**：Bridge 只 `Read()`，绝不 Set；GPU Provider 在 Bridge 侧禁用
  （GPU 由 nvidia-smi 负责，避免重复采集）。
- **Provider 不可用 -> 全部高级字段 = None**（UI 显示 `--`，绝不显示 0 冒充）。
- **Fan `control_percent`**：只有 LHM 真实提供 Control 传感器时才有值
  （绝不从 RPM / 假定 MaxRPM 推算）；0 RPM 不是错误。
- **传感器新鲜度**：值 > 30s 视为过期 -> 该字段 None（不返回陈旧读数）。
- **生命周期**：LlamaMonitor 启动拉起（CREATE_NO_WINDOW），退出优雅终止
  （关 stdin -> 宽限 -> Terminate）；崩溃指数退避重启（2/4/8/16/30s 上限）；
  **挂死看门狗**：3 × 传感器周期无输出 -> kill 重启（AUDIT-1.1.1 REL-1111-009）；
  启动清理孤儿 Bridge 进程（AUDIT-1.1.1 BUG-1111-007）。

## 9. 1.3.2 新增指标（Strata 延迟直方图 / 硬件趋势 / llama Router）

1.3.2 起新增三类遥测（源：`prometheus_parser.py` / `strata_collector.py` /
`llama_runtime_collector.py`），口径如下。

### 9.1 Strata 延迟分位数（TTFT / ITL / E2E 的 P50/P95）

- **数据源**：`GET /metrics?format=prometheus`（vLLM 延迟 histogram 的
  `_bucket/_sum/_count` 分桶），**每 15s** 采样一次相邻 bucket delta。
- **bucket delta**：按 le 对累计值相减（Counter reset 感知：`curr < prev`
  则 delta = curr；首次采样只建基线不落库）。落库形态是**每桶区间 delta**
  （`bucket_interval_deltas`：`interval[le_i] = cum_delta[le_i] - cum_delta[le_{i-1}]`），
  只有 `delta > 0` 的桶写 `latency_histogram` 表（le 为 TEXT）。
- **分位数**：窗口内按 le 对 delta 求和，**用采集器已知完整 le 轴补零**
  （零 delta 桶不落库，不补零会丢桶下界），再经典 `histogram_quantile`
  线性插值（与 Prometheus 语义一致；负 delta 钳制 0；+Inf 不计入分母）。
- **ITL 是估计分布**：vLLM `inter_token_latency_seconds` 每请求只记一个
  **平均 token 间隙**（`decode_s / (output_tokens - 1)`），不是逐 token 采样——
  UI 必须标注"估计"（API 字段 `itl_is_estimate=true`）。
- **count = 0 的窗口 P50/P95 = null**（不是 0ms）。

### 9.2 Strata 主机硬件趋势（PCIe RX/TX / GPU / CPU / 内存）

- **数据源**：每 15s 把上一轮 `/metrics` 的 `hardware.live` 摘要落库
  `strata_hardware`（复用已抓数据，不发额外请求）；实时保留窗口内清理。
- **`server_local` 标注**：`/api/strata/hardware-trend` 返回 `server_local`
  bool，标注数据来自本机还是远程 Strata 主机（Strata 主机不一定是
  LlamaMonitor 所在机器）。
- **PCIe 带宽**：`gpu_pcie_rx_mb` / `gpu_pcie_tx_mb`（MB/s）。Expert 卸载
  场景 RX 反映 CPU 内存 → GPU 的专家权重加载流量。
- **PCIe 链路**：`gpu_pcie_gen`（代际）/ `gpu_pcie_width`（通道宽度）/
  对应 `_max`（最大能力），UI 显示"实际（最大）"。

### 9.3 Strata 会话缓存 / Draft 加权接受率 / 引擎详情

- **Draft 加权接受率**：`Σ accepted / Σ offered`（跨请求加权），不是各请求
  接受率的算术平均（避免小样本请求扭曲均值）。来源：每请求
  `draft_tokens_offered/accepted` 求和（`aggregate_draft_stats`）。
- **每请求 Expert 字段**：`expert_hit_rate`（Expert VRAM 命中率，0-1）、
  `expert_pcie_share`（PCIe 份额，0-1）、`prefill_tok_s`（prefill 速度）
  ——均为**每请求原始值**，UI 不做跨请求加权平均。
- **`running`/`outside_slots`**：`outside_slots` 是 `running` 的**子集**
  （Slot 外处理），UI 显示真实总数并标注"（Slot 外 N）"，不再相加双计。
- **`waiting_total` = `waiting + queued`**（合并等待），`admitting` 保持
  独立语义不并入。
- **引擎详情**：`/metrics` engine 块白名单（expert_slots / kv_resident /
  batch_slots / batch_groups / spec / mtp_max / pool_workers / vram_free_mib /
  arena_mib / pcie_frac 等）。`kv` 可能是量化类型字符串（如 "int8"）或数字，
  `spec` 可能是字符串（如 "mtp"）或数字代码，UI 分别处理。

### 9.4 llama.cpp Router 多实例 + SSE + LoRA

- **Router 检测**：`GET /models` 返回**列表**且条目带 `status` dict ->
  Router 模式。selected = 第一个 `status.value == "loaded"` 的实例；
  变化时保持之前选择（若仍在列表）否则 None。
- **只读**：Router 模式 `/metrics`、`/props`、`/v1/models` 均带
  `model=<urlencoded id>&autoload=false`；**绝不** `GET /models?reload=1`、
  **绝不** POST /lora-adapters 或 slots（不触发加载/卸载）。
- **SSE 事件流**：`/models/sse` 受管单连接（asyncio 任务），指数退避重连
  1s→30s；事件按 `(event, model, status, stage_index, stage_value)` 去重；
  有界缓冲 `deque(maxlen=100)`。`progress.value` 是**阶段内** 0~1
  （不是整模型加载百分比），UI 显示"阶段 N XX%"。
- **LoRA**：`/lora-adapters` 只读 GET；UI 只显 basename + scale
  （完整路径只在本机 Diagnostics 可见）。
- **Server State 扩展**：`ready` / `loading`（Health 503 "Loading model"，
  不是不可达）/ `sleeping`（`is_sleeping` 字段）/ `auth_error`（401）/
  `degraded`（4xx/5xx）/ `unavailable`（**仅网络层失败**连续 5 次）。
  HTTP 层响应（4xx/5xx）计入服务异常，不累计"不可达"计数。
- **训练 vs 运行时上下文**：`n_ctx_train`（训练上下文）与 `n_ctx`
  （运行时上下文）并列展示（`/v1/models` meta）。

## 10. 1.3.3 实时口径（activity / 实时 TPS / 新鲜度 / 故障隔离）

1.3.3 起采集链路重构为 HOT 1s / WARM 2s / COLD 低频分层，`/api/realtime`
成为前端实时主入口。口径如下。

### 10.1 活动判定（activity）

- **Strata**（优先级，权威在前）：
  1. `/metrics.live`：`state ∈ {reading, generating}` 或 `running > 0` 或
     `outside_slots > 0` -> active（source `strata.metrics.live`，authoritative）；
  2. `/status.busy == true` -> active（source `strata.status`，secondary）；
  3. 明确 `live.state == "idle"` 且 `running == 0` 且 status 非 busy -> idle。
  **`/slots[].is_processing` 绝不参与活动判定**（只负责 Slot 数量/明细/辅助诊断）。
- **llama.cpp**（多源联合）：`llamacpp:requests_processing > 0` **或**
  任一 `/slots[].is_processing == true` -> active；**仅当两者皆 0 才 idle**；
  `requests_deferred > 0` = 排队中（`requests.queued`，独立于 active）。
  **绝不用** `predicted_*` / `prompt_tokens_seconds == 0` 判忙闲。
- **Prompt 处理是活动**：Strata `state="reading"`（generated==0，
  phase "reading the prompt"）与 llama `n_prompt_tokens_processed <
  n_prompt_tokens`（phase=prompt）都判 active——长 Prompt 处理期间
  UI 不再显示"空闲"。
- **stale 不翻 idle**：数据源失败轮保留上一轮 activity（source 加 `stale:`
  前缀、confidence=stale）；连续 <5 轮失败保留快照（1.3.1 容忍不变），
  >=5 轮才切离线表现。

### 10.2 实时 TPS（llama.cpp，/slots 计数器 delta）

- decode = Δ`n_decoded`/Δt；prompt = Δ`n_prompt_tokens_processed`/Δt
  （Δt 为 **monotonic** 间隔）；`Δt <= 0.05s` 本轮不计算（防除零）。
- **基线键 = (slot id, id_task)**：slot id 或 id_task 变化 -> 重设基线
  （不跨任务混算）；服务端重启（连续 >=5 轮离线）清零基线。
- 计数器倒退（Δ<0）-> 只重设基线，**绝不产出负 TPS**。
- 无新鲜 delta（首轮/任务刚开始）-> `null`（UI 显示 `--`，绝不假 0）；
  有新鲜 delta 时 `0.0` 是合法测量值（如 Prompt 处理期 decode=0）。
- 多 slot 同时活动按 slot 求和（总吞吐口径）。
- **实时 vs 平均**：`current_*_tps` = 上述实时值；`average_*_tps` =
  Strata 官方会话平均（`tok_s_mean`/`prefill_tok_s_mean`）或 llama 最近
  5 分钟 live_samples 窗口加权平均（与 `/api/throughput` 同口径）。

### 10.3 新鲜度（freshness）与四态

- `sample_timestamp` / `live_last_update` = **最近一次 HOT PATH 成功**
  取得实时数据的时刻（Strata=/metrics 成功；llama=/metrics 或 /slots
  任一成功）；COLD 任务（histogram/v1status/metadata）不推进它——
  低频端点卡住不影响"实时"新鲜度。
- 前端新鲜度四态（按 sample 的 age）：**实时**（<=2s）/ **轻微延迟**
  （2~10s）/ **数据延迟**（10~30s）/ **状态未知**（>30s 或尚无数据）。
  数据缺失绝不渲染成"空闲"。
- 竞态防护：`sequence`（单调样本序号）+ `sample_timestamp`；前端迟到旧
  响应（时间戳更旧，或同时间戳但 sequence 更小）不覆盖新数据。

### 10.4 端点健康与故障隔离

- 每端点独立 `{available, last_success, last_attempt, age_ms,
  consecutive_failures, last_error}`（`endpoint_state.py`）；
  一个端点失败绝不影响其它端点与实时链路（§29：单源失败后 active 保持
  active+stale，不翻 idle）。
- 错误分类：404/501→`unsupported`、401→`authentication_error`、
  429→`rate_limited`、5xx→`temporarily_unavailable`、超时→`timeout`、
  连接拒绝→`unreachable`、坏 JSON→`invalid_response`。
- 能力型错误（unsupported/auth/rate_limited）= **明确结果**：恢复探测按
  15s 节流（不 1s 狂刷已知 404 的端点）；瞬时故障（timeout/5xx/
  unreachable）保持正常节奏快速恢复。
- per-endpoint 超时：HOT 1.5s / WARM 3s / COLD 5s；HOT 内 /metrics 与
  /slots **并行**（asyncio.gather，独立超时/独立错误）——慢 /slots
  绝不阻塞 /metrics，反之亦然。
- 日志只在**状态变化**时输出（OK->ERROR / ERROR->OK / 连续失败 1/5/10/30），
  不逐轮刷屏。
