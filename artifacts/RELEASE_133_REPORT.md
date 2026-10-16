# LlamaMonitor 1.3.3 交付报告

> 主题：**实时监控链路重构 + Strata / llama.cpp 采集准确性修复 + 防止单点接口异常拖慢/中断整个采集系统**
> 环境：Windows（真实 Strata 0.1.43.1 + 真实 llama-server + Microsoft Edge）
> 基线：1.3.2（748 例自动化 + 交付报告 `artifacts/RELEASE_132_REPORT.md`）
> 版本：`version.py = 1.3.3`（SemVer，MAJOR.MINOR.PATCH）

本报告全部为**真实实测值**，非"测试通过"的概括。所有测量可在 `tools/` 下的验证工具中复现。

---

## 一、全量自动化测试（真实结果）

| 指标 | 1.3.2 基线 | 1.3.3 |
|---|---|---|
| 用例总数 | 748 | **766**（新增 `tests/test_realtime_133.py` 18 例） |
| 结果 | 748/748 OK | **766/766 OK（0 失败 / 0 错误 / 0 跳过）** |
| 耗时 | 585s | **489.1s（8.15 分钟）** |

- 完整运行：`SUITE_DONE cases=766 ok=True fail=0 err=0 skip=0 elapsed=489.1s`（见 `tools/tests_full_133d.log`）。
- 最慢用例（TOP，均 OK）：soak 模拟 16–30s/例（4 例）、桌面隐藏窗口 16–17s/例、`test_17_deadline_scheduler_no_drift_no_overlap` 6.95s。
- `tests/test_realtime_133.py`：**18/18 OK**（活动状态多源联合、TPS 计数器 delta、§23 竞态防护、`/api/realtime` 精确形状、端点错误分类与能力恢复、DeadlineScheduler 无漂移/无重叠/无无限追平）。

### 套件提速（工程，真实前后对比）
1.3.3 重构后 `hot_round` 并行抓取 `/metrics + /slots`，但多数测试驱动只 mock 了 `_fetch_parsed` 未 mock `_fetch_slots`，导致每轮对黑孔 `127.0.0.1:9` 各产生 1.5s `ConnectTimeout`，全量一度膨胀到 ~3 小时（不完整）。根因修复：为**全部**测试驱动 + `tools/soak_test.py` 注入 `_fetch_slots` mock。单文件真实前后：

| 文件 | 修复前 | 修复后 |
|---|---|---|
| `test_reliability.py` | 194s | **22s** |
| `test_local_only_api.py` | 52.8s | **12.5s**（1.3.2 基线 12.8s） |
| `test_soak_simulation.py` | 3h+（挂起） | **79.5s** |

---

## 二、实时链路（1s HOT）——真实刷新节奏与耗时

### 2.1 Microsoft Edge 真实浏览器（headless=new，CDP 实测）
- **轮询节奏**：12s 内 12 次 `/api/realtime` 请求，间隔 `[1006,1019,1007,1027,1010,1018,1008,1009,1009,1020,1006]ms`，**中位 1009ms、最大 1027ms、最小 1006ms** → 稳定 1s 节奏（`polling.js` 固定 1000ms，无自适应、无抖动）。
- **双向 no-store**：12 个响应**全部** `Cache-Control: no-store`，且**全部** `fromDiskCache=false`（CDP `Network.responseReceived` 实测）→ 实时数据绝不走 HTTP 缓存。
- **hidden tab**：`visibilityState=hidden` 期间 4s 内 `/api/realtime` 请求数 = **0**（`visibleOnly` 生效，后台标签不轮询；切回前台恢复）。
- **竞态防护（§23）**：CDP `Fetch` 注入迟到旧响应（`sample_timestamp` 更旧）→ 被拒（`#rtSlots` 保持新值）；注入更新响应 → 被收。`app.js` `refreshRealtime()` 用 `sequence + sample_timestamp` 双条件丢弃乱序到达的旧响应。

### 2.2 HOT 循环耗时（真实 dev server `/api/realtime` 诊断）
llama dev server（8790，真实 llama-server）实时 `/api/realtime` 快照：
```
hot_loop_interval_ms = 1000     # 1s 定周期
last_hot_duration_ms = 4        # 最近一轮 HOT 仅 4ms
hot_overrun            = false
hot_overrun_count      = 25     # 会话累计（诚实计数）
```

### 2.3 Strata（0.1.43.1，9091，真实 qwen 模型）
- **空闲基线 20s**：HOT min **8** / p50 **11** / max **22** / avg **12.9ms**；`overrun_count = 0`；`age_ms` 最大 **2062ms**；`sequence` 20s 内 **+19**（精确 1s 步进，无漂移）。
- **负载下**（200K-token prompt 处理中，n=332）：HOT min **7** / p50 **14** / max **106** / avg **14.7ms**（重载下仍远低于 1s 预算）。
- **分层节奏**：WARM `/slots` ~9s、`/status` ~5s；COLD histogram 15s / `/v1/status` 300s / metadata 600s（`DeadlineScheduler` 各自独立，互不阻塞）。

---

## 三、Strata 采集准确性（§4/§5/§6）——真实值

- **§5 `generated==0` + "reading the prompt" = 活跃（Prompt Processing）**：
  真实 **1,090,984 字符（~272,746 token）** prompt 处理 180s：**332 个采样全部** `state=reading / active=True / generated=0`，**从未翻转 idle**（修复前会误判为空闲）。
- **§4 `metrics.live` 权威**：活动由 `metrics.live`（running/outside/reading/generating）判定，`source=strata.metrics.live`、`confidence=authoritative`；绝不依赖 `/slots[].is_processing`。
- **decode 实时 TPS**：在途 147K-prompt 请求，`current_decode_tps` 实测 **27–61 tok/s**（live 口径），`generated` 2349→4035，`age_ms` 455–656。
- **固定计数器口径（spec §36）**：input 79,265 / cached 2,032,318 / output 5,759 / logical 2,117,342 / compute 85,024（`StrataAcceptanceTests` 精确断言通过）。
- **延迟直方图补零**：`(1,2.5]` 单样本 → 端点补零后 P50 = **1.75**（非朴素聚合的 1.25）；零 delta 桶不落库，完整 le 轴由 `known_le_axes` 补零。

---

## 四、llama.cpp 采集准确性（§6/§7）——真实值

- **§6 活动判定（`requests_processing>0` OR 任意 `slot.is_processing==true`）**：
  真实 400-colors 生成请求 → `state=decode`（修复后正确识别）、`slots_active=1`。
- **§7 实时 TPS（`/slots` 计数器 delta / 单调时钟）**：
  真实 400-colors（1109 completion token）decode：**184.88–190.29 tok/s**（19 个有效采样），`prompt_tps=0.0`（prompt 被缓存，合法 0，非 null），`generated` 154→892。slot id / id_task 变化时**重定基线**（不误算跨请求 delta）。
- **prompt 阶段识别**：13K-token prompt（52,625 字符）处理，**t=0.6s 即** `active=True / state=prompt`（HOT 负载下 `last_duration_ms` min 3 / p50 6 / max 23 / avg 11.0ms，`hot_overrun_count=0`）。
- **`n_decoded` 兜底**：该构建 busy slot 无顶层 `n_decoded`，从 `next_token.n_decoded` 读取；decode 判别 `n_decoded>0`、`==0`→prompt（修复前 phase 卡在 prompt）。
- **`/metrics` 501 → `unsupported`**：该 llama-server 构建不暴露 prometheus → 正确分类 `unsupported` + **15s 节流重探**（不每秒 hammer）；`/slots` 仍是有效二次源。

---

## 五、故障隔离（§29/§30）——真实值

> 目标：**单点接口异常（慢/挂/重启）不拖慢、不中断整个采集系统**；多源联合判定绝不因单源失败错误翻转。

### 5.1 Strata 故障 A（kill t=3s，重启 t=28s，含 ~25s 模型加载）
- t=5.7s 起 `stale:`（**单次前缀**，修复前会累积成 `stale:stale:stale:…`）；
- 连续 5 次失败（t=10.9s）`online=False`；
- 模型加载完成后**完全恢复**（`online=True`、`authoritative` 恢复、`consecutive_failures=0`）。

### 5.2 llama 故障（kill t=3s，重启 t=15s）
- `stale:` 单次前缀；t=18.3s 恢复（`/slots` 二次源，fresh 500ms）。

### 5.3 §40 慢 `/metrics`（8791 经 3s 延迟代理 9092）
- HOT 被 **1.5s 超时封顶**（duration 实测 **1510–2711ms**，绝不 3000ms）；
- HOT 轮次**继续**（不中断）；`overrun_count` 诚实递增 **31→42**；
- `status` 5s / `slots` 10s 节奏**不受影响**；无样本时 `sequence` 保持（绝不伪造样本）；`sample_age=None`。

### 5.4 实时多源隔离样例（llama dev server 8790，`/metrics` 持续 501，`/slots` 正常）
`/api/realtime` 实时快照：
```
source.metrics : available=false, consecutive_failures=460, last_error="unsupported"
                 （~107 分钟仅 460 次尝试 ≈ 15s 节流，非每秒 hammer）
source.slots   : available=true, age_ms=848（fresh）
activity       : active=false, source="llama.slots", confidence="secondary"
freshness      : age_ms=848（<2s=实时）
```
→ **单源（metrics）失败不拖垮另一源（slots）**：activity 由 slots 二次源驱动，freshness 仍为"实时"。

---

## 六、版本 / 文档 / 数据

- **版本**：`version.py = 1.3.3`；`CHANGELOG.md` / `docs/API.md` / `docs/METRICS_DEFINITIONS.md` §10 / `README.md` 已同步。
- **`/api/realtime` 契约（spec §12）**：`provider / server_online / activity / throughput / requests / slots / tokens / freshness / errors / diagnostics / sequence`，严格 0-vs-null（缺失/不支持 = `null`，绝不假 0）。
- **无 DB schema 变更**：1.3.3 不新增表/列（无 SQLite 迁移）；HOT 路径**无 SQLite**（§24），落库仅在 WARM。
- **日志（§32）**：仅在状态**变化**时记录（如 `STATE -> active=True state=decode`），不逐秒刷日志。

---

## 七、复现命令

```powershell
# 全量自动化（766 例）
.\.venv-final\Scripts\python.exe -m unittest discover -s tests

# Edge CDP 三验（节奏/no-store + 竞态 + hidden tab）
.\.venv-final\Scripts\python.exe tools\_edge_verify.py        # -> tools/edge_verify_result.txt

# 活体测量（需真实 Strata 9091 / llama 9091 + dev monitor 8790）
.\.venv-final\Scripts\python.exe tools\_live_measure.py      # HOT/age/sequence 采样
.\.venv-final\Scripts\python.exe tools\_live_longprompt.py   # 272K-char Strata prompt（§5）
.\.venv-final\Scripts\python.exe tools\_live_llama_decode.py # 400-colors decode（§7 TPS）
.\.venv-final\Scripts\python.exe tools\_live_faultA.py       # Strata 故障 A（§29/§30）
```

原始测量产物：`tools/live_longprompt_result.txt`、`tools/live_llama_decode_result.txt`、`tools/live_llama_result.txt`、`tools/faultA_rows.json`、`tools/edge_verify_result.txt`、`tools/tests_full_133d.log`。
