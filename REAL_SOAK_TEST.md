# LlamaMonitor Soak Test 设计与结果（Phase 11）

本文档定义并记录 Phase 11 的长周期可靠性验证：**确定性 soak 模拟**（`tools/soak_test.py`）。
它不是"跑 90 天真实机器"，而是把 7/30/90 天的全部事件序列按固定 seed 精确重放一遍，
用 **ground truth 恒等式**验证 Token/MTP/GPU 统计在长期运行下的正确性与可解释性。

---

## 1. 为什么用模拟而不是真实挂 90 天

| 真实挂 90 天 | 确定性模拟 |
|---|---|
| 事件（server 重启/断网/睡眠）靠运气，可能 90 天没遇到一次 server reset | 每类事件按速率精确注入：reset、离线窗口、monitor 重启、跨午夜 |
| 无 ground truth：无法证明"数据库里的数是对的" | server 按固定速率产生 token，**truth 精确已知** |
| 不可复现 | 固定 seed 完全可复现，CI 可回归 |
| 时间成本高 | 90 天 @ poll 60s 约 25 分钟跑完 |

两者结合才完整：模拟验证**算法正确性**（恒等式），`tools/fake_llama_server.py` + 真实
EXE 验证**运行时行为**（见 §6）。

## 2. 模拟架构

```
FakeClock（wall + monotonic 双时钟）
    │
    ├── FakeServer（llama-server 替身）
    │     · 固定速率产生 prompt/output token（默认 3/1 per s）
    │     · advance(s)：推进产生量并记账 truth
    │     · reset()：counter 归零（模拟 llama-server 重启），同时记账 lost
    │     · read()：暴露当前 counter 值给 monitor
    │
    └── MetricsCollector + Database（生产代码，未修改）
          · 真实 metrics 文本解析、reset 检测、baseline 持久化
          · 真实 SQLite（WAL）、daily 归集、data_gaps、monitor_events
          · FakeClock 注入：collect_once 里的时间全部来自 clock
```

模拟循环按 poll 边界推进时间，每轮可注入的事件（互斥，优先级从高到低）：

1. **monitor 重启**（`--restart-rate`）：collector.shutdown() + 新建 collector（同一 DB），
   模拟进程重启；期间 server 继续产生。
2. **server 离线窗口**（`--offline-rate`）：4~12 个 poll 内 fetch 失败（模拟断网/宕机），
   窗口内 50% 概率发生 server reset（产生真实不可恢复丢失）。
3. **在线期 server reset**（`--reset-rate`）：poll 间隔内随机偏移处 reset。
4. **跨午夜**：模拟窗口从某个本地午夜开始，必然跨日（验证 midnight 归集）。

收尾：循环结束后执行一次**收尾读取**，让最后一个采集间隔被观测覆盖，
保证恒等式精确闭合（否则末尾 1 个 poll 的 token 既未被观测也未被计为丢失）。

## 3. Ground Truth 恒等式（PASS 判据）

对 prompt 与 output 分别要求（**整数精确，无容差**）：

```
ground_truth = observed + known_lost
```

- `ground_truth`：server 按速率实际产生的总量；
- `observed`：数据库 daily_usage 全部行求和（monitor 实际记下的）；
- `known_lost`：server 侧精确记账的"被 reset 抹掉且 monitor 未观测到"的 token
  （= 每次 reset 时 `counter - 上次read`）。

同时检查标记一致性：

- 在线期 reset ⇒ `counter_reset` 事件存在；
- 离线期 reset ⇒ 对应缺口 `possible_token_loss=1`（监控盲区 + server 重启 = 可能真丢）;
- 离线窗口数 == `server_offline` 缺口行数（每窗口一条）;
- 有真实丢失 ⇒ 至少一条 `possible_token_loss` 缺口。

## 4. 模拟中的判定语义（与生产一致）

| 判定 | 规则 |
|---|---|
| counter reset | `current < previous` ⇒ delta = current，记 `counter_reset` 事件；`current >= previous` ⇒ 正常 delta |
| sample_invalid | HTTP 200 但核心 counter（prompt+output）**全部**缺失 ⇒ 本轮无效，保持旧 baseline，不产生缺口（短抖动） |
| 已知缺口 | 有效样本间 monotonic 间隔 > `poll * 3` ⇒ 记 `data_gaps`；原因 = server_offline / invalid_metrics / monitor_restart / system_pause_or_sleep / unknown |
| system_pause_or_sleep | 仅在**期间没有任何轮询轮次**且 monotonic 跳变超阈值时判定（进程被系统挂起）；有离线轮次的长缺口保持 server_offline（进程一直在运行，不能仅凭时长改判睡眠） |
| possible_token_loss | 缺口期间核心 counter 发生 reset（盲区 + 重启，恢复样本显示 counter 回退） |
| Monitoring Coverage | 当天首样本→末样本时间窗内无已知缺口的比例。**不是 Token 统计准确率**：Token 是 Counter delta 精确累计，缺口内 server 继续产生的 token 在恢复时按 reset 判定计为 observed 或 loss，不会静默蒸发 |
| midnight 归集 | delta 按**样本时间戳**的本地自然日归集；跨日样本只归当日 |

## 5. 标准档与当前结果

标准档（确定性，seed 固定）：

```
python tools/soak_test.py --days 7  --seed 42 --poll 5
python tools/soak_test.py --days 30 --seed 42 --poll 30
python tools/soak_test.py --days 90 --seed 42 --poll 60
```

**2026-09-18 实测结果**（seed=42，`soak_reports.txt` 保存完整输出）：

| 档位 | 轮次 | Ground Truth (p/o) | Observed (p/o) | Known Lost (p/o) | Diff (p/o) | Resets (on/off) | Restarts | Offline (gap) | Gaps (loss) | Daily 行 | 耗时 | PASS |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 7 天 @ 5s | 120960 | 1814400 / 604800 | 1747263 / 582421 | 67137 / 22379 | **0 / 0** | 1801 (1013/788) | 989 | 2036 (2036) | 3025 (790) | 8 | 1621s | ✅ |
| 30 天 @ 30s | 86400 | 7776000 / 2592000 | 7491345 / 2497115 | 284655 / 94885 | **0 / 0** | 1299 (736/563) | 688 | 1435 (1435) | 2123 (566) | 31 | 1105s | ✅ |
| 90 天 @ 60s | 129600 | 23328000 / 7776000 | 22471188 / 7490396 | 856812 / 285604 | **0 / 0** | 1937 (1107/830) | 1077 | 2120 (2120) | 3197 (836) | 91 | 1609s | ✅ |

三档全部满足恒等式 `ground_truth = observed + known_lost`（**整数精确**，无容差），
且：离线窗口数 == server_offline 缺口行数（1:1）、core reset 事件数 == 2 × reset 次数
（prompt+output 各一条）、daily 行数 == 天数 + 1（收尾读取落在次日边界）。
90 天模拟中已知丢失占比 3.7%（prompt）——全部来自"离线窗口内 server 恰好重启"
这一真实会丢失的场景，且每一条都有 `possible_token_loss` 缺口 + `counter_reset` 事件
可解释、可审计。

多次随机 seed 的 2 天混合场景（`tests/test_soak_simulation.py` 使用更小规模）：
seed 42 / 7 / 123 / 2024 全部 `Diff = (0, 0)`（2026-09-18 实测）。

## 6. 运行时验证（真实 EXE + fake server）

`tools/fake_llama_server.py` 是一个最小 llama-server 替身（FastAPI，`/metrics` 返回真实
格式的 llamacpp_* 文本，counter 持续递增，支持 `POST /_reset` 模拟 server 重启）。
配合 EXE 做的人工验证项：

以下为 2026-09-18 重建 EXE 后的实机验证结果（`tools/fake_llama_server.py` @ 19091，
EXE 监控，poll 5s）：

- [x] EXE 启动 → 监控 fake server，Token 统计持续累计（add 500 prompt → 当日 +500，精确）；
- [x] `POST /test/reset` → 10 个 counter 全部记 `counter_reset` 事件（含 per-position MTP），
      恢复后统计不重复计入 reset 前的 token（add 400 → 恰 +400）；
- [x] fake server 离线 35s → `server_offline` 缺口 40.2s 正确记录，恢复后统计正常；
- [x] 重启 EXE（停机 25s > 15s 阈值）→ `monitor_restart` 缺口 29.3s 正确记录；
      停机 < 阈值的快速重启不记缺口（设计如此）；baseline 从 DB 恢复，无重复计入；
- [x] `GET /api/health`：database=healthy、journal=wal、application=healthy；
- [x] `POST /api/data/check-database` → healthy=PASS；
- [x] `POST /api/data/backup` → `manual_monitor_20260918_222908.db`（180KB，verified=true）；
- [x] `GET /api/data/quality` / `GET /api/data/backups`（含 verified 字段）/
      Dashboard Data Quality 区域与 Settings→Data 的 Database Health / Automatic
      Backup 均已部署并验证端点响应正确。

## 7. 已知数据统计边界（诚实地列出）

1. **连续盲窗 reset 的第二次可能漏判**：monitor 盲区内发生 server reset，且恢复读取值
   ≥ 盲区前最后读取值时（例如连续两次离线窗口各带一次 reset、且第二次恢复后的
   累计量更大），`current < previous` 判据无法发现第二次 reset，该次 reset 抹掉的
   少量 token 既不计入 observed 也不计入 known_lost。真实世界 counter 以百万计、
   server 重启间隔以小时计，此歧义可忽略；模拟中通过事件排程约束
   （`last_read > (window-poll) * rate` 才允许窗口内 reset）保证检测可靠，
   并在本报告文档化该边界。
2. **sample_invalid 轮不产生缺口**：短抖动（< 阈值）的无效轮被静默跳过，
   只有持续无效（形成 > 阈值缺口）才落 `data_gaps`。
3. **GPU energy 是梯形积分估算**：`∫ power dt` 用相邻采样功率均值近似；
   进程重启后首个 GPU 采样不积分（误差 ≤ 一个采样周期）；
   nvidia-smi 不可用时 GPU 统计停止但 LLM 统计不受影响。
4. **monitor 重启缺口的 lost 判定保守**：进程退出时无法得知 server 是否重启，
   shutdown 路径记录的未结束缺口一律 `possible_token_loss=0`（宁可少报不可多报）；
   若重启后首个有效样本检测到核心 reset，monitor_restart 缺口会带 `possible_token_loss=1`。
5. **WAL 崩溃一致性**：进程被 kill（非优雅退出）时，WAL 中未 checkpoint 的数据在
   下次打开时由 SQLite 自动回放；`quick_check` 在启动时验证，失败进入 protective mode
   （只读，不自动修复/不删库/不覆盖）。
6. **备份是元数据 + 文件系统双轨**：`backup_history` 表只存元数据（验证状态等），
   文件系统（`backups/` 目录）是最终备份来源；表损坏不影响找回备份。

## 8. 复现命令

```bash
# 单元测试内的 soak（1 天 @ 60s，约 1-2 分钟，随全量测试一起跑）
python -m unittest discover -s tests

# 标准档
python tools/soak_test.py --days 7  --seed 42 --poll 5
python tools/soak_test.py --days 30 --seed 42 --poll 30
python tools/soak_test.py --days 90 --seed 42 --poll 60

# fake server（另一个终端，默认 http://127.0.0.1:9091）
python tools/fake_llama_server.py
```
