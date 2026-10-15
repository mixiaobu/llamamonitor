# LlamaMonitor 1.3.2 交付报告

**发布**：2026-10-17 · tag `v1.3.2` · commit `04f6061`
**主题**：Strata 延迟直方图 / PCIe 实时+趋势 / 引擎详情 / 会话缓存完整块；
llama.cpp Router 多实例只读监控 + SSE 事件流 + LoRA；数据准确性与隐私加固

---

## A. 修改总览（35 文件，+4531/−95）

| 类别 | 文件 | 说明 |
|---|---|---|
| 新增模块 | `prometheus_parser.py` | Prometheus 文本解析 + histogram 分桶 delta（reset 感知）+ 区间 delta 转换 + 经典分位数插值 |
| DB | `db.py` | v7→v8 非破坏迁移：新表 `latency_histogram`（每桶 delta）+ `strata_hardware`（15s 采样）；`record/get_latency_histogram`、`record_strata_hardware/get_strata_hardware_range`（>2000 点均匀降采样）；close() 幂等 |
| Strata 采集 | `strata_collector.py` | 15s 分频 `_poll_histogram_and_hardware`（prometheus histogram delta 落库 + 上轮 hardware 落库）；`/v1/status` 5min uptime/并发；`fetch_all_requests` 按需 ?requests=all（30s TTL、上限 200）；`known_le_axes()` 完整 le 轴（补零用） |
| Strata 解析 | `strata_parser.py` | `aggregate_draft_stats`（Σaccepted/Σoffered 加权）；最近请求 `expert_hit_rate`/`expert_pcie_share`（0-1 钳制）+ `prefill_tok_s`；`waiting_total=waiting+queued`（admitting 独立）；engine 白名单扩展（kv_resident/batch/spec/mtp_max/pool_workers/vram_free/arena/pcie_frac…）；capability `latency_histogram` |
| llama 采集 | `llama_runtime_collector.py` | `is_sleeping` 字段名修复；`next_token` 对象形态归一化；Health 状态机（ready/loading/sleeping/auth_error/degraded/unavailable，仅网络层失败计不可达）；Router 检测（/models 带 status 列表）+ 选中首个 loaded + `model=<id>&autoload=false` 只读；`/models/sse` 受管单连接（退避 1→30s、(event,model,status,stage) 去重、deque(100)）；`/lora-adapters` 只读（basename+scale） |
| API | `server.py` | `/api/llama/router`、`/api/llama/lora`、`/api/strata/latency`（窗口聚合+补零+P50/P95）、`/api/strata/requests`（limit>20 触发全量）、`/api/strata/uptime`、`/api/strata/hardware-trend`（server_local 标注）；`/api/strata/live` += uptime/capabilities/draft_aggregate |
| 前端 | `static/index.html` / `pages.css` / `app.js` / `charts.js` / `system.js` | 性能页分组：Strata-only（延迟分布 G2 / PCIe 实时+趋势 / 引擎详情 K / 会话缓存完整块 / 引擎 uptime+并发 / 查看更多）+ llama-only（Router 实例+SSE 事件流 / LoRA / 训练上下文）；`SS_STATE_BADGES` 统一状态徽章（含 sleeping/auth_error/degraded）；`renderPcieTrendChart` 双系列趋势图 |
| 测试 | `tests/fakeservers.py`（新增）、`tests/test_fake_servers_integration.py`（新增 21 例）、`tests/test_prometheus_parser.py`（新增 18 例）、`test_migration.py`（v7→v8 数据完整）、`test_runtime_privacy.py`（SECRET 四件套扫描）、3 处迁移断言更新 | 748/748 OK |
| 文档 | `CHANGELOG.md`、`docs/API.md`（llama lora/router + Strata 5 端点）、`docs/METRICS_DEFINITIONS.md`（§9 1.3.2 口径） | — |

## B. Strata 实现要点

- **延迟直方图链路**：`/metrics?format=prometheus`（Accept: text/plain，
  content negotiation 与 JSON 端点分离）→ 每 15s 对相邻累计 bucket 求 delta
  （`curr<prev` 判定 reset，delta=curr；首采样只建基线不落库）→ **转每桶区间
  delta**（`bucket_interval_deltas`：cum[i]−cum[i−1]，+Inf 保留）→ 仅 d>0 落库。
- **窗口聚合补零（本轮发现的关键 bug）**：零 delta 桶不落库，朴素聚合会把
  (1, 2.5] 的样本当成 (0, 2.5]（P50 1.75→1.25 偏低）。修复：采集器
  `known_le_axes()` 暴露最近解析的完整 le 轴，`/api/strata/latency` 聚合时
  `setdefault(le, 0.0)` 补零后再插值。活体验证：单样本 (1,2.5] → P50=1.75、
  P95=2.425（精确）；回归测试 `test_endpoint_zero_fill_bucket_lower_bound`
  同时断言朴素聚合=1.25（bug 复现）与端点=1.75（修复）。
- **ITL 语义**：vLLM `inter_token_latency_seconds` 是每请求平均 token 间隙的
  估计分布（活体 23 请求窗口内累计 9870 个观测点，长流式请求持续产生观测），
  API `itl_is_estimate=true` + UI 标注「估计」。
- **PCIe**：live RX/TX（MB/s）+ 链路 Gen/width（实际/最大，真实机器 Gen3 x16）；
  趋势图 15s 采样落库（活体 25 分钟 94 点，RX 0.1–0.4 / TX 0–2.2 MB/s 真实值）。
- **running/outside_slots**：UI 显示真实总数 + 「（Slot 外 N）」标注（不双计）。
- **Draft**：`draft_aggregate = Σaccepted/Σoffered`（活体 2384/3327 = 71.7%，
  UI 显示加权口径提示）。
- **uptime/并发**：`/v1/status` 5min 低频（活体 23h25m uptime、并发 2/2）。

## C. llama.cpp 实现要点

- **is_sleeping**：官方字段名解析（此前读错字段名）；Server State `sleeping`。
- **Health 503**：`Loading model` → `loading`（不是不可达）；401→`auth_error`；
  其他 4xx/5xx→`degraded`；仅网络层失败累计「不可达」（5 次容忍不变）。
- **next_token 对象**：`{has_next_token, has_new_line, n_remain, n_decoded}`
  归一化（兼容旧 list 形态）；slot 表已显示 n_decoded/n_remain。
- **Router 只读**：`/models` 返回带 status 的列表 → 检测 Router；selected=
  首个 `status.value=="loaded"`（变化时重置 props 时间戳强制刷新）；
  `/metrics`、`/props`、`/v1/models` 全部 `model=<id>&autoload=false`；
  绝不 `?reload=1`、绝不 POST。实例列表 basename only（`a.gguf`，无完整路径）。
- **SSE**：受管 asyncio 单任务；断连指数退避 1→30s；事件按
  (event,model,status,stage_index,stage_value) 去重；`deque(maxlen=100)`；
  `progress.value` 为**阶段内** 0~1（UI「阶段 0 40%」）。
- **LoRA**：`/lora-adapters` 只读 GET；basename + scale。
- **训练 vs 运行时上下文**：`n_ctx_train`（32.77K）与 `n_ctx`（8.19K）并列。

## D. 数据准确性

| 项 | 口径 | 验证 |
|---|---|---|
| 固定计数器 | input 79,265 / cached 2,032,318 / output 5,759 / logical 2,117,342 / compute 85,024 | PASS（fake 固定值精确） |
| Draft 加权 | 501/1001（A 1/1 + B 1000/500，不是 75%） | PASS |
| outside_slots | running=1+outside=1 显示 **1** 个处理中 | PASS |
| histogram delta | 区间语义 / reset 感知 / 0 样本=None（不是 0ms） | PASS |
| 分位数插值 | 零桶补零后 (1,2.5] 单样本 P50=1.75 | PASS（活体+回归） |
| 隐私 | SECRET_PROMPT/OUTPUT/REASONING/API_KEY 不进 API/SQLite/日志/CSV | PASS（10 端点+DB 字节扫描+日志记录） |
| 迁移 v7→v8 | 旧 daily_usage/state/live_samples 逐值保留 + 新表可写 | PASS |

## E. 测试矩阵（离线 + 活体）

| 测试 | 结果 |
|---|---|
| 全量 `unittest discover -s tests`（748 例） | **PASS**（748/748 OK，585s） |
| test_prometheus_parser（18） | PASS |
| test_fake_servers_integration（21：验收口径/直方图/隐私/Router/SSE/Health） | PASS |
| test_migration（9，含 v7→v8 数据完整） | PASS |
| test_strata_parser（48）/ test_strata_collector（24） | PASS |
| test_runtime_privacy（22+）/ test_api（17） | PASS |
| CDP Edge 7 视口 × 8 页（320×568 / 360×800 / 390×844 / 430×932 / 988×1394 / 1920×1080 / 2560×1440） | PASS（零 console 错误；性能页无新增横向溢出；9 处 h-scroll 均为既有设计性滚动容器） |
| 活体 Strata 9091（0.1.43.1，qwen3.8-flash-next 2-slot 262K ctx） | PASS（版本 1.3.2 / 延迟 P50/P95 实数 / PCIe 趋势 94 点 / 引擎详情 6563 expert / 会话缓存 / 查看更多 200 条 / 0 console 错误） |
| fake llama router（9092，Router+SSE+LoRA） | PASS（2 实例选中 model-a / SSE 阶段进度 / LoRA basename / 训练上下文 / strata 段隐藏 / 0 console 错误） |
| CI Release Windows x64（tag 触发，run 38710977515） | **PASS**（success，含全量测试 + Ed25519 验签） |

## F. 发布

- 流程：commit `04f6061` → push main → tag `v1.3.2`（远端无既有 v1.3.2，未覆盖）
  → GitHub Actions `Release (Windows x64)` run 38710977515
- **CI 结果：success**（Build release artifacts（含全量测试 + PyInstaller +
  安装器 + 签名 manifest）/ Validate release（含 Ed25519 验签）/
  Create GitHub Release / 3 个 artifact 上传全部 success）
- Release `v1.3.2`：**5 资产齐全**
  - LlamaMonitor-1.3.2-win-x64.zip（30,787,789 B）
  - LlamaMonitor-Setup-1.3.2-win-x64.exe（24,969,422 B）
  - release-manifest.json（542 B）/ release-manifest.sig（163 B）/ SHA256SUMS.txt（202 B）
- **manifest**：`version=1.3.2`、`signing_key_id=key-2026-09`
- **Ed25519 验签：OK**（`update_manifest.verify_manifest_signature`）
- **SHA256 双重比对：全部一致**（zip + installer 与 SHA256SUMS.txt 及
  manifest 内嵌哈希；`tools/_verify132.py`）
- 1.3.1 → 1.3.2 为安装器自更新通道（1.3.1 已装实例应用内检查即可升级）
