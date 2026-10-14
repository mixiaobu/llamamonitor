# LlamaMonitor 1.3.0 双推理后端 —— 交付报告

## 1. 版本 / 仓库

| 项 | 值 |
| --- | --- |
| 版本 | **1.3.0**（`version.py` 单一来源；tag `v1.3.0`） |
| 提交 | `261fab5` `feat: LlamaMonitor 1.3.0 — 双推理后端（llama.cpp + Strata）统一软件/数据库/页面` |
| 仓库 | https://github.com/mixiaobu/llamamonitor（main 已推送） |
| Release | https://github.com/mixiaobu/llamamonitor/releases/tag/v1.3.0（已发布，5 资产） |
| 变更规模 | 692 files / +16,486 −261（含 5 个新测试文件、23 个 Strata 真实数据 fixture、6 张 UI 截图） |

新增模块：`strata_parser.py`（纯解析）、`strata_collector.py`（Strata 采集器）、
`runtime_provider.py`（后端探测/校验）、`gap_tracker.py`（共享缺口追踪器，
collector.py 同步改为委托实现）。

## 2. Provider 架构

- **一个实例 = 一个后端**。配置 `llama_server.provider`：
  `auto`（默认）/ `llamacpp` / `strata`；修改需重启（PUT 返回 `restart_required`）。
- **auto 识别**（`runtime_provider.detect_backend`）：先 GET `/health`
  （`service == "strata"` → Strata），否则 GET `<metrics_path>` 判断
  `llamacpp:` 指标前缀；均失败 → 回退 llama.cpp 采集链路并记录中文错误。
- **手动选择校验**（`verify_manual_provider`）：识别结果与选择不一致时
  测试连接返回 `mismatch` + 中文 `mismatch_detail`，不盲目切换。
- **切换不丢历史**：Counter Baseline 键按「后端 + 服务地址」命名空间隔离
  （`strata:<server_id>:totals.*` vs llama 裸键）；`daily_usage_by_source`
  （db v7）按 日期 × 后端 × 服务地址 记录每日增量，与既有汇总 `daily_usage`
  并存（UI 只消费汇总，不重复相加）；切换回 llama.cpp 时基线原样保留。
- 探测/测试连接全部只读 GET（`/health` + metrics），3s 超时，支持可选
  Bearer Token。

## 3. llama.cpp 兼容性（原有功能完整保留）

- **真实回归（b10976 构建，Qwen2.5-0.5B Q4_K_M，4 slots / ctx 4096，
  `--metrics`）**：
  - auto 识别 → `llamacpp`；模型/Slot/构建信息全量显示
    （`b10976-987498f45`，4 槽，4.1K 上下文）；
  - **E2E Token 采集**：真实推理 1 次（36 in / 3 out）→ 数据库
    `daily_usage = (36, 0, 3)`、`live_samples` 增量和一致 —— 分毫不差；
  - 前端：10 列 Slot 表（含 MTP/详情）、MTP 段、llama 专属运行时指标
    全部显示；Strata 专属段隐藏；概览显示「llama.cpp 就绪」。
- **单元回归**：collector 8 项 + reliability 24 项 + soak 4 项全绿
  （collector 已重构为委托 `gap_tracker.GapTracker`，行为等价）；
  全量套件 692 项 OK。
- 既有语义不变：counter reset 审计、缺口（server_offline/monitor_restart/
  system_pause_or_sleep/unknown）+ `possible_token_loss`、MTP per-position、
  KV/Slot 详情、CSV 导出、备份/健康检查。

## 4. Strata 兼容性（充分利用真实监控数据）

**真实 Strata 0.1.43.1 @ 127.0.0.1:9091**（qwen3.8-flash-next-unsloth-ud-q4_k_xl，
2 slots，n_ctx 262144，V100-32GB）全端点实测：

- **7 态状态机**：不可达 / 等待服务响应 / 模型加载中 / 就绪 / 推理中 /
  休眠 / 监测异常。实测：发送真实推理请求期间 `busy=True running=1
  outside_slots=1`，phase `reading the prompt → thinking`，`tok_s=28.4`，
  `generated=34`；请求结束自动回 ready。`strata_state_changed` 事件
  （ready→busy→ready）落库。
- **实时推理状态**：phase（中文映射：Prompt 处理/思考中/生成回答/生成中）、
  当前/平均生成速度、Prompt 处理速度、处理中/等待/排队/槽外、
  当前请求进度（生成/预算）、思考恢复。
- **会话缓存与 Draft**：enabled/复用 Token（实测 12.38M 累计）/
  请求数/复用率（74.8%）——原始整数计算。
- **最近请求**（内存态，不落库）：实测 12 条实时列表（时间/结束原因/
  Prompt/缓存复用率/生成/生成速度/Draft 接受率）。
- **Slot 监控**：2 槽（保留上下文/窗口上限 + 百分比）。
- **后端硬件**：Strata 主机的 GPU 型号（Tesla V100-PCIE-32GB）/利用率/
  显存/温度/功耗 + CPU/内存，与本机「系统」页明确区分（本机/主机标签）。
- **引擎信息**：版本 0.1.43.1、build、max_context、kv、spec、mtp、
  pool_workers、pcie_frac 等白名单字段。
- **分频采集**：metrics+status 每轮（2s）、slots/2、health/5（metrics
  失败立即 health）、props+models 首轮+300s —— 低干扰。
- 测试连接（auto）：真实返回 `Strata / 527ms / 模型名`；手动选
  llama.cpp 对 Strata → 一致性不一致中文提示；不可达 → 「连接超时（2s）」。

## 5. Token 数据（口径统一）

- Strata `totals.prompt_tokens` **含缓存复用**（已按 §20 实测验证：
  2,111,583 = 79,265 新 + 2,032,318 复用）。展示层拆分：
  **输入（Prompt）/ 缓存复用 / 新处理 Prompt / 输出 / 逻辑总量**；
  缓存复用率 = 复用 ÷ 输入总量（原始整数，实测 ≈96.25% 场景口径正确）。
- Counter 单调性防护：reset/倒退 → `counter_reset` 事件 + Baseline 重建；
  `totals.since` 变化 → `strata_instance_changed`（Baseline 重建，防跨
  实例相加）。
- 落库验证（真实 Strata 冒烟实例）：state 8 个命名空间 Counter 键；
  `daily_usage` 与 `daily_usage_by_source` 同值同增（strata 行
  `127.0.0.1:9091`）；llama 真实实例 `(36,0,3)` 独立无串扰。
- 缺口：共享 `GapTracker`，strata 来源缺口实测落库
  （12.5s 轮询停顿 → `system_pause_or_sleep`，token_recoverable=1）。
- 既有历史不回填；汇总/来源两表 UI 永不相加。

## 6. 性能

- 全量套件 692 项：**563.9s OK**（本机，含 soak 84s + settings 51s 长测）。
- 采集负载：Strata 每 2s 轮 metrics+status（两次小 GET），slots 每 4s，
  health 每 10s，props/models 每 300s —— 与 llama.cpp 链路同量级；
  页面隐藏时轮询自动降频（既有机制）。
- 前端零新增依赖（无构建步骤）：Strata 段为 stat-grid/表格，2s 轮询
  仅在性能页可见时运行；最近请求 12 条 DOM 渲染 <1ms 级。
- API：`/api/strata/live` 首次 527ms（冷连接）、后续 <50ms；
  `/api/overview`、`/api/status` 毫秒级。

## 7. UI 逐页（10 视口 × 8 页，Edge 155 CDP 实测）

| 页面 | 桌面（1920~1024） | Compact（900/768） | 手机（390×844） |
| --- | --- | --- | --- |
| 概览 | 「Strata 就绪」+ 模型/上下文 262.14K/Slot 2/模态；llama 模式「llama.cpp 就绪」 | 同（meta 2 列换行） | 同（左文右 meta 堆叠），0 溢出 |
| 用量 | 日聚合（双后端同表）/趋势 | `.table-wrap` 横向滚动（既有模式） | 2 列，同 |
| 性能-Strata | 4 段（实时推理/会话缓存与 Draft/最近请求 12 行/后端硬件）+ Slot 卡 + 模型与引擎（「Strata 构建 0.1.43.1」） | 同（grid 自适应 2 列） | stat-grid 2 列；**请求/Slot 表转卡片**（复用 .slot-table 卡片 CSS，实测 display:block 生效） |
| 性能-llama | 10 列 Slot 表 + MTP 段 + llama 专属指标，Strata 段隐藏 | 同 | 卡片化（既有） |
| 系统/显卡 | 与后端无关，不变 | 同 | 同（既有 #gpuRange 横滚为既有模式） |
| 历史 | 缺口/事件双表；**来源筛选新增 Strata**；事件详情中文（状态映射/Counter 名映射） | 同 | 同 |
| 设置 | 「推理服务器」卡：推理后端（自动/llama.cpp/Strata）+ API Key（密码框，占位「留空表示不发送/已设置」）；测试连接双后端文案 | 同 | rail 单行横滚（既有） |
| 关于 | 版本 1.3.0 / Schema 7 | 同 | 同 |

**console 门**：10 视口 × 8 页 = 80 次页面访问 + 冒烟/回归脚本，
**0 未捕获错误**（`Runtime.exceptionThrown` + `console.error` 双钩子）。
与已安装 1.2.2 基线同法扫描对比：无新增嵌套滚动/横向溢出模式
（仅剩 6 项均为 1.2.2 既有设计：.table-wrap / .settings-rail / #gpuRange / #gpuPick）。
截图 6 张（`docs/images/130_*.png`，已入 README）。

## 8. 安全

- **只读定位保持**：两后端均只发 GET；测试连接仅 `/health` + metrics；
  无代理、无控制接口、无 POST/写端点。
- **隐私白名单**（解析层强制剥离，测试守护）：
  - `status.tail`（模型生成的实时文本）不进快照/API/DB/日志 —— 实测
    `/api/status` + `/api/strata/live` 全量 JSON 无 tail/Jinja 模板；
  - `chat_template` 不存储；API Key 值只进 Authorization 头
    （GET 仅回显 `api_key_set` 布尔；PUT 空串 = 保持不变；
    清空走配置文件）；模型完整本地路径（`C:\Users\...`）不出现在
    API 响应（alias 取文件名/模型名）。
- **API Key 语义**：test-connection 传 key 不落地（不写配置/日志）；
    保存后 GET 不泄露值。
- **更新链**：manifest Ed25519 验签（`key-2026-09`）用项目自带
  `verify_manifest_signature` 验证 **通过**；SHA256SUMS 与实际
  下载 zip 哈希一致。
- **数据库**：WAL + quick_check 健康检查 + protective mode + 迁移前
  自动备份（v6→v7 迁移测试：回滚 user_version 重放迁移，数据无损，
  迁移事件落库 ×2 幂等）。
- 单实例 Mutex、回环只读（/api/config 仅 loopback）、日志不含配置内容
  （只记变更字段名）—— 既有机制不变。

## 9. 测试（PASS / FAIL / BLOCKED / NOT RUN）

**最终全量：692 项 OK（0 FAIL / 0 ERROR），563.9s。**

| 项 | 结果 |
| --- | --- |
| 全量 unittest（692） | **PASS**（`artifacts/_full_run_5.log`） |
| 新增 Strata 测试 100 项：parser 48 / collector 20 / provider 15 / 切换 6 / API 11 | **PASS** |
| db v7 迁移 + apply_sample 来源 + reset 双表（test_provider_switch V6ToV7） | **PASS** |
| 真实 Strata 0.1.43.1 @9091：全端点 + 状态机（busy/thinking/28.4 t/s）+ 落库（事件/缺口/daily/state 8 键） | **PASS** |
| 真实 llama.cpp b10976：auto 识别 + 10 列 Slot + E2E Token（36/0/3 精确匹配） | **PASS** |
| Edge CDP：10 视口 × 8 页 0 未捕获错误 + 无新增布局回归（对比 1.2.2 基线） | **PASS** |
| UI 术语冻结 24 项 / 移动端 25 项 / 设置 API 16 项 / 迁移 6 项 | **PASS** |
| GitHub Actions Release（run 38608007672，15min）：构建+签名+发布 | **PASS** |
| Release 资产 5 件 + manifest version=1.3.0 + Ed25519 验签 + SHA256 比对 | **PASS** |
| FAIL / BLOCKED | **无** |
| NOT RUN | 安装器实机安装（Inno Setup 产物由 CI 构建，安装流程与 1.2.2 相同；应用内自助更新链路已验证 manifest/签名）；Strata `sleeping`/`loading` 态实机触发（代码路径由 fixture 单测覆盖；本会话期间 Strata 未加载/休眠新模型，未强触） |

## 10. 自动发布

- 流程（无需人工确认）：`git push main` + `push v1.3.0` →
  GitHub Actions `Release (Windows x64)` 触发（run **38608007672**）→
  **success（15 min）** → Release v1.3.0 自动创建。
- 资产（5 件齐全）：`LlamaMonitor-Setup-1.3.0-win-x64.exe`（23.8 MB）、
  `LlamaMonitor-1.3.0-win-x64.zip`（29.3 MB）、`release-manifest.json`
  （**version=1.3.0**，schema/signing_key_id 齐全）、
  `release-manifest.sig`（**项目自带验签函数验证通过**）、
  `SHA256SUMS.txt`（**与实际下载 zip 哈希一致**）。
- 插曲：首次 push 被 GitHub push protection 拦截
  （旧脚本 `tools/_waitrelease12x.ps1` 硬编码 PAT 命中「Push cannot
  contain secrets」）→ 脚本改为 `$env:GH_PAT` 读取 + amend 后推送通过；
  该 PAT 建议后续轮换（曾出现在被 amend 替换的旧提交中）。
- 本机已安装实例（8765）仍为 1.2.2，可在应用内「软件更新」自助升级到
  1.3.0（manifest/签名链已验证），或安装新 Setup。

## 11. 遗留问题

1. **已安装实例未自动升级**：1.2.2 实例运行中（正是当前会话所用），
   应用内更新或 Setup 重装即可；1.3.0 dev 实例（8790，临时 DB）留作
   演示可随时停。
2. **Strata 休眠/加载态未实机触发**：状态机 7 态中 `sleeping`/
   `loading` 由 fixture 单测覆盖（含 23 个真实数据 fixture），
   本次会话期间 Strata 未发生真实加载/休眠，未强触。
3. **`/api/status` 的 `server_state_label` 在 llama 模式为空串**
   （llama 三态沿旧字段路径，前端优先 `server_state_label`，llama 模式
   回退内置映射 —— 行为正确，非缺陷；如需统一可后续补 label）。
4. **PUT 保存不热重载**（设计决定，所有字段一致）：保存 API Key/
   provider 后 `GET /api/config` 的 `api_key_set` 等回显仍为启动值，
   重启后刷新；前端已用 Toast 提示「重启后生效」。
5. **usage 页聚合跨后端**：`daily_usage` 为两后端合计（统一口径），
   来源级明细存于 `daily_usage_by_source`（已入库、API 可查），
   未做「按来源过滤」UI —— 按「统一页面、低干扰」定位留作后续增强。
6. **旧 PAT 轮换建议**（见 §10）：`ghp_0rK5...` 曾硬编码于已 amend 的
   旧提交（本地历史已替换；GitHub 主分支历史不含该提交）。
7. **Strata 分频端点**（props/models 300s）在极长空闲后首帧为
   「--」至下一轮采集 —— 与 llama.cpp 链路的新鲜度语义一致。
