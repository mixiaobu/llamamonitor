# LlamaMonitor 指标定义（Phase 14）

> 本文档定义 Dashboard / API / 数据库里每个字段的**精确计算规则**。
> 源：`stats.py`（纯函数）+ `collector.py`（组装）+ `gpu_collector.py`（能耗）。
> 规则与代码一一对应，任何行为以本文档 + 单元测试为准。

## 1. 采样与时间

- **轮询**：默认每 5s 抓取一次 `GET {llama_server}/metrics`（Prometheus 文本）。
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
- `reason`：`server_offline` / `monitor_restart` / `system_pause_or_sleep` / `unknown`。
- 检测：
  - 离线轮（抓取失败）→ 开 `server_offline` 缺口；
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
| live_samples / gpu_samples | 48h（可配置） |
| monitor_events | 365 天 且 ≤ 100,000 行 |
| daily_usage / mtp_position_daily | 永久 |
| data_gaps | 永久 |
| backup_history | ≤ 1,000 行 |
