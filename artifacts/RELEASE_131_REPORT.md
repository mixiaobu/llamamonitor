# LlamaMonitor 1.3.1 交付报告

**发布**：2026-10-16 · tag `v1.3.1` · commit `e772c2e`
**主题**：单次抓取失败不再把数据打 0 / 翻状态——5 次容忍下沉到采集层

---

## 1. 问题

用户反馈：「之前让你 5 次获取不到数据再提示获取失败，那为什么一次获取不到数据
就变成 0 了？」

1.2.1 引入的「连续 <5 次采集失败保留上一次数据与状态」只落在**前端 4 个独立
数据源**（`/api/status` 拉取失败、GPU、系统、传感器，`LM.poll.streakFail`，
FAIL_THRESHOLD=5）。而**被监控的推理服务本身**发生一次抓取失败时，采集器层
立即输出离线/清零表现：

| 位置 | 单次失败后的表现 |
|---|---|
| `collector.py`（llama.cpp `/metrics`） | `offline_snapshot()`：`/api/status` 立即 `server_online=false` + 全部字段 None（性能页数值变 0 / "--"） |
| `strata_collector.py:305`（Strata） | `self.live = build_live_state(None, ...)` 每轮重算 → 归零 dict（`running=0, busy=false`）→ 性能页 Strata 段归零、`/api/strata/live` 返回 0 |
| `strata_collector.py` 状态机 | 第 1 轮失败即翻 `unreachable` / `monitoring_error`（概览徽章直接变红） |
| `llama_runtime_collector.py`（`/health`） | 连续 2 次失败判「不可用」（与 5 次语义不一致） |

**结论：根因在采集器（后端数据源），不在前端**——前端所有轮询的 catch 路径
本来就保留上次值（复查了全部 23 个轮询任务），后端单次失败却把"真值"打成了 0。

## 2. 修复方案（阈值统一 `gap_tracker.OFFLINE_TOLERANCE = 5`）

- **`collector.py`**：新增 `_offline_streak` / `_invalid_streak`。连续 <5 次
  抓取失败（或无效 metrics）→ 保留最近一次有效快照（数值、online 标志不变，
  不记 server_offline / invalid_metrics 事件）；第 5 次才暴露离线/无效快照；
  成功一次立即清零（失败序列被打断不累计）。从未在线过则首轮失败保持离线
  快照（不假装在线）。
- **`strata_collector.py`**：新增 `_offline_streak`。连续 <5 次 `/metrics`
  失败 → 保留**全部内存态**（live 实时值 / 引擎 / 会话缓存 / 最近请求 /
  后端硬件）且状态机不翻转（徽章保持原状态）；第 5 次才按原规则切
  `unreachable` / `monitoring_error`（health 可达与否仍区分两者）；恢复
  一次清零。
- **`llama_runtime_collector.py`**：`/health` 判「不可用」去抖阈值 2 → 5
  （复用同一常量，语义对齐）。
- **缺口语义不变**：首次失败轮即开启 `server_offline` / `invalid_metrics`
  缺口展示（数据质量独立于展示容忍）；低于 3×间隔阈值的抖动不落库；baseline
  在失败轮保持不动，恢复后 delta 覆盖整个断档期（Token 统计无损）。

## 3. 前端复查（全量 23 个轮询任务）

| 数据源 | 单次拉取失败行为 | 结论 |
|---|---|---|
| status / runtime / overview / summary / strataLive / throughput / usage / mtp / history / daily / gpu / system / sensors … | catch → 保留上次渲染值（仅 console.warn） | ✅ 无需改 |
| status / gpu / system / sensors 的**状态徽章** | `streakFail` 5 次才切「不可用/离线」 | ✅ 1.2.1 已有 |
| Strata `stRunning`/`stWaiting` | null → 0 兜底 | 后端修复后 null 只在真正离线（≥5 次）或首屏前出现——0 即期望行为 |
| 概览/运行时/性能页数值 | null → `--`（F.NA） | ✅ 无需改 |

## 4. 测试

- **新增**（700/700 OK，此前 692）：
  - `test_reliability.OfflineToleranceTests`：单次抖动保留上次快照；4 次容忍、
    第 5 次翻转 + 恰 1 条事件；间歇失败永不翻转
  - `test_reliability.InvalidMetricsToleranceTests`：无效 metrics 同语义
  - `test_strata_collector.StrataOfflineToleranceTests`：live 同一对象保留、
    状态机不翻转、第 5 次 unreachable、恢复清零、从未在线保持 waiting
- **更新旧语义断言**：`test_api.test_status_offline`（单次失败保留 → 5 次
  离线 → 恢复）、`test_reliability.test_invalid_metrics_no_state_update`
  （事件延迟到第 5 次）、`test_runtime_privacy` 2→5、`test_strata_collector`
  3 处（gap/事件/状态机）
- **真实 HTTP 路径 E2E**（`tools/_e2e_blip.py`，本地假 Strata，不动真实 9091）：
  ready → 杀服务 4 轮（online 保持 / ready 保持 / 数值保留）→ 第 5 轮
  offline+unreachable → 恢复 1 轮 ready+数据回来，offline/online 事件各恰 1 条
  ——**全部通过**

## 5. 活体验证（真实 Strata 0.1.43.1）

- 1.3.1 dev 实例（`/api/status`）：`version=1.3.1`，真实推理中数据
  （busy、tok_s 42.6、phase/会话缓存/最近请求/硬件 GPU 全在）
- CDP 前端冒烟：概览「Strata 推理中」徽章、性能页全部真实值、0 控制台错误
- 10 秒 5 连查：`online=true state=ready` 稳定无抖动

## 6. 发布验证

- CI run 38669214608（Release Windows x64）：**success**
- Release `v1.3.1`：5 资产齐全（portable zip 30.7MB / installer 24.9MB /
  manifest / sig / SHA256SUMS）
- manifest `version=1.3.1`、`signing_key_id=key-2026-09`
- Ed25519 验签：**OK**（`update_manifest.verify_manifest_signature`）
- SHA256：zip + installer 与 SHA256SUMS.txt 及 manifest 内嵌哈希双重比对
  **全部一致**（`tools/_verify131.py`）

## 7. 已知边界（行为不变，非缺陷）

- 从未连接成功过的服务：首屏即「等待服务响应」+ 离线表现（没有"上次数据"
  可保留）
- 容忍只影响**展示层**；缺口/事件审计与 Token 统计仍按真实数据质量记
- 安装器自更新通道可用（1.3.0 → 1.3.1 正常升级路径）
