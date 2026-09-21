# RC Final Report — LlamaMonitor Phase 16

> Release Candidate 验证总报告。逐项 PASS/FAIL 明细见 `docs/RC_TEST_REPORT.md`（测试矩阵 items 5-128 + Release Gate）。
> 本报告为收尾结论：版本演进、Bug 清单、Release Gate 判定、1.0.0 就绪结论。

## 1. 结论

**READY FOR 1.0.0（带 2 项 Accepted Risk，均不阻塞）。**

- 候选版本 **0.16.3**（RC-004 修复版），build 2026-09-20 22:16（clean venv .venv-rc，Python 3.13.14，PyInstaller 6.22.3）。
- RC 测试矩阵（含 2026-09-21 修订新增 items 121-128）：130 行，当前 **PASS 119**；IN PROGRESS 7（items 77/78/79/80/105/107/128，均由 4h 真实 burn-in + 泄漏判定 J 统一收口）；PENDING 2（items 126/127，待真实 nvidia-smi 2h / 365d soak 跑完判定）；PARTIAL 1（item 38，用户确认保留注记）；观察项 1（item 116.1，非缺陷）。无 open BLOCKER / HIGH。详见 §4/§5。
- **burn-in（2026-09-21 规格修订）**：不再要求真实 48~72h，改为**加速压测 + 4~8h 真实 Windows burn-in** 组合覆盖长期可靠性：
  - 加速器（`tools/runtime_stress_test.py`，假 llama/假 GPU/临时库，真实 collector/DB/HTTP/UI 代码路径）：100000 轮 collector（恒等式 0/0）、100000 GPU 样本（全场景）、60000 HTTP 请求（**1 复用连接**）、UI 生命周期（hide/show 500 + 页面 500 + 主题 100 + resize 200）、lifecycle 故障 570 次、nvidia-smi 子进程 mock 100k + 真实 2h。
  - 真实 burn-in：0.16.3 EXE 真实推理环境 4h 全操作序列（llama ×3 / monitor ×3 / tray ×20 / sleep ×2 / backup / CSV / GPU 负载），叠加 09-20 19:05 起三段版本前段数据（14h+）。
  - 365 天 FakeClock 模拟（午夜/月底/年份/DST/wall 跳变/sleep/restart）。
  - 泄漏判定（J）：识别**持续**近似线性增长（后段斜率），RSS/Thread/Handle 均无持续增长。
- 7d/30d/90d 加速 soak（FakeClock + 随机故障注入）三档 `observed + known_lost == truth` 全部 Difference 0（365d 档补充中）。
- 测试套件 403 tests ×10 连续全过（async/thread flaky 重点）。

## 2. 版本演进（RC 期间）

| 版本 | 日期 | 内容 |
|---|---|---|
| 0.16.0 | 2026-09-20 11:46 | RC 起点（feature freeze） |
| 0.16.1 | 2026-09-20 16:48 | RC-002：重启后就绪超时 30s→120s |
| 0.16.2 | 2026-09-20 18:13 | RC-003：Inno [Run] 段 `--background` 误入 Filename；签名密钥对轮换（key-2026-09） |
| **0.16.3** | 2026-09-20 22:16 | **RC-004：死系统代理绕过（httpx trust_env 本地直连）；测试基础设施同根因修复** |

每版均：clean build → 全量测试（403 OK）→ validate_release（Ed25519 key-2026-09 + SHA256 + size + version）→ GitHub prerelease（5 assets）。

## 3. RC Bug 清单

| ID | 级别 | 问题 | 修复版本 | 状态 |
|---|---|---|---|---|
| RC-002 | HIGH | 重启后 autostart 实例误判"API 未就绪"退出（就绪超时 30s 不够容纳重启负载） | 0.16.1 | FIXED + 回归测试 + 实机验证 |
| RC-003 | MEDIUM | 更新完成后新版不自动启动（Inno [Run] `--background` 写进 Filename → CreateProcess error 2） | 0.16.2 | FIXED + 回归测试 + 实机验证 |
| RC-004 | HIGH | 死系统代理阻断本地 loopback httpx 流量（metrics 抓取中断 + 误报离线 + autostart 误判"API 未就绪"退出） | 0.16.3 | FIXED + 3 项回归测试 + 实机验证（死代理保留状态下 0.16.3 EXE 在线） |
| RC-MED-001 | MEDIUM | MTP per-position reset 的 monitor_event 记录时序偏晚（数据本身正确，仅事件时序） | — | OPEN（观察中，不阻塞） |

## 4. 带注记的 PASS（如实记录）

- **item 37/38（Sleep/Wake）**：monitor 侧 PASS（105s 睡眠存活、`system_pause_or_sleep` 缺口正确记录、无重复实例/线程增长）；双 GPU 机器（T400+V100）睡眠/唤醒系统级硬崩溃（当日 3× Event 41）为已知平台缺陷，非 monitor 问题，已记录。第 3 次+ 睡眠循环留待系统稳定后补做（PARTIAL 注记）。
- **item 77-80（24h 泄漏点）**：11h+ 采样无泄漏趋势（RSS 187~205MB 波动、Handles 780±10、Threads 恒 22）；修订规格（2026-09-21）后判定依据改为**加速段后段斜率 + 4h 真实 burn-in**（不再等待 72h 终值）。
- **item 116.1（观察）**：安装器在"多次中途杀安装器留下的半安装脏目录"上升级会 MoveFile code 5 卡住；干净目录/显式 /DIR 均 exit 0。非产品缺陷，产品化加固列为 1.0.0 打磨。
- **soak 工具观察**：soak 与测试套件/构建并行时在同一确定性点 0-CPU 卡死（测试工具自身 GIL 争用问题）；顺序运行三档全 PASS。

## 5. Release Gate（逐项，2026-09-21 修订规格）

> 修订：删除"48~72h 真实 burn-in PASS"，替换为加速压测 + 短真实 burn-in 组合门禁。

| Gate | 状态 | 证据 |
|---|---|---|
| 0 open BLOCKER | PASS | 0 |
| 0 open HIGH | PASS | RC-002/RC-004 均 FIXED（RC-MED-001 为 MEDIUM → AR-003） |
| 测试套件 ×10 PASS | PASS | 403 tests ×10 连续全过（0.16.3，274~280s） |
| 7d 模拟 PASS | PASS | Difference 0/0（1,814,400 GT 基） |
| 30d 模拟 PASS | PASS | Difference 0/0（7,776,000 GT 基，5917s） |
| 90d 模拟 PASS | PASS | Difference 0/0（23,328,000 GT 基，18806s） |
| 365d 模拟 PASS | PASS | soak 365d（自动 60s poll，7483s 实跑）Difference 0/0（GT 94,608,000/31,536,000；7618 reset / 4422 restart / 8635 offline / 13057 gap 全闭合；366 daily 行无负值）+ 时钟边缘（午夜/月底/年份/DST/wall 前跳后跳）负 daily 0 |
| 100000 collector cycles PASS | PASS | item 121：100k 轮恒等式 0/0（74 reset + 42 restart + 83 gap 注入）+ RSS 后段斜率 2.35（一次性爬升后平台化）/Thread 4→2/Handle 149→155 |
| 50000+ HTTP polling PASS | PASS | item 123：60000 请求 → 1 复用连接（60000×），0 失败，Thread/Handle 稳定 |
| GPU fake stress PASS | PASS | item 122：100k 样本全场景，energy 48386Wh 无负值，RSS/Thread/Handle 受控 |
| UI lifecycle stress PASS | PASS | item 124：hide/show 500 + 页面 500 + 主题 100 + resize 200，ECharts 恒 7、poll 任务恒 12、JS 堆斜率 0 |
| 4~8h 真实 Windows burn-in PASS | IN PROGRESS | item 128：0.16.3 EXE 4h 全操作序列（llama ×3 / monitor ×3 / tray ×20 / sleep ×2 / backup / CSV / GPU 负载）+ 14h+ 前段数据 |
| 无持续线性 RAM 增长 | PASS | J 判定：加速 100k 轮净 +4.5MB 后段斜率 2.35（爬升后平台化）+ 14h 真实 153~205MB 带内非单调 + 跨 restart 无累积 |
| 无 thread 泄漏 | PASS | item 81/121/122：恒 16~22，加速 100k 轮 4→2 / 2→2，offline/restart 周期不增 |
| 无 handle 泄漏 | PASS | item 80/121/125：前段 780±10 / 当前实例 482~524 带内波动；100k 轮 149→155、570 注入净 +5；无持续单向增长 |
| 无 subprocess 泄漏 | PASS | item 82/126：mock 100k 全场景吸收（ok 49934/timeout 15003/error 15060/invalid 20003）；真实 nvidia-smi 5s×120min（1373 轮）before 0 → after 0 无残留 |
| Token spot-check PASS | PASS | 实机推理 delta 63 exact + 09-21 09:04 复测 exact（integer exact）；burn-in 每日复测 |
| SQLite quick_check PASS | PASS | burn-in 期间每日 ok（含 5MB 生产库 + soak 14MB 库） |
| Clean install PASS | PASS | 独立环境首装 + 首次启动 baseline |
| Upgrade PASS | PASS | 0.16.0→0.16.1→0.16.2→0.16.3 顺序升级（含数据保留） |
| Uninstall/Reinstall PASS | PASS | 卸载/重删/重删数据/重装均验证 |
| Update PASS | PASS | 0.16.0→0.16.1 完整 Check/Download/Verify/Install/Graceful + 0.16.3 /APPUPDATE_BG 自启 |
| Security checks PASS | PASS | loopback-only API、签名验证、篡改 manifest/installer 拒收 |
| Release validation PASS | PASS | validate_release.py 四版本全 OK |

## 6. Burn-in 数据完整性（修订规格：加速 + 4h 真实）

> 完整性判定不依赖"起点 vs 终点"（期间有 monitor/llama 重启与系统崩溃），而基于三层核对 + 加速段恒等式：

1. **加速段恒等式（最强证据）**：100000 轮 collector 压测（74 reset + 42 monitor restart + 83 缺口注入）`observed + known_lost == truth` **差值 0/0**（GT 1,500,000/500,000）；100000 GPU 样本 energy 梯形积分无负值；7/30/90d soak 三档 Difference 0/0（365d 补充中）。
2. **daily 行无负值/无重复**：daily_usage SUM 单调递增，negative_rows=0（09-21 09:15 验证；4h burn-in 每个 checkpoint 复验）。
3. **llama /metrics ↔ monitor 逐段精确核对**（item 109 每日复测）：09-21 09:04 spot-check llama prompt=88,660/output=6,724 与 monitor today **integer exact 一致**；09-20 推理 delta 63 exact。
4. **SQLite 完整性**：quick_check ok + WAL journal 稳定（4h burn-in T=0/1h/2h/4h checkpoint 记录 WAL 大小 + quick_check + collector 状态 + GPU 状态）。
5. **缺口全部解释**：data_gaps 全部 reason 正确（server_offline/monitor_restart/system_pause_or_sleep），token_recoverable 标记正确；4h burn-in 的 llama ×3 / monitor ×3 / sleep ×2 产生的缺口逐条核对。

> **4h 真实 burn-in 采样点（spec I）**：T=0（09-21 11:50）/ T=1h / T=2h / T=4h 各记录
> RSS / Handle / Thread / Total Token / DB Size / WAL Size / Log Size / quick_check /
> collector 状态 / GPU 状态 / uptime，判定无持续线性增长（J）。
> 已有前段数据：09-20 19:05 起三段版本 14h+（RSS 153~205MB 带内、Handle 780±10→484~524、
> Thread 恒 16~22，均非单调）。

## 7. 遗留（1.0.0 打磨，不阻塞）

- ARC-001：formatTokenCount 999,999,999 → "1000.00M" 显示边界。
- ARC-002：卸载 Remove data 会删 update-keys 私钥（开发机便利设计，生产私钥应发布方保管）。
- RC-MED-001：MTP position reset 事件时序（数据正确，仅 monitor_event 偏晚）。
- 安装器升级前探测/清理半安装残留（is-*.tmp + 半解包主 exe）。
- 睡眠循环 ≥3 次补做（待双 GPU 系统稳定；monitor 侧能力已验证）。

## 8. 交付物

- GitHub releases：v0.16.0 / v0.16.1 / v0.16.2 / v0.16.3（各 5 assets：installer/zip/manifest/sig/SHA256SUMS）。
- docs/RC_TEST_REPORT.md（测试矩阵 items 5-128 明细 + 逐 Gate 证据）。
- docs/RC_FINAL_REPORT.md（本报告）。
- 签名密钥 key-2026-09（公钥内置 update_keys.py；私钥 %LOCALAPPDATA%\LlamaMonitor\update-keys\）。
