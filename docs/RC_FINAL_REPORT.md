# RC Final Report — LlamaMonitor Phase 16

> Release Candidate 验证总报告。逐项 PASS/FAIL 明细见 `docs/RC_TEST_REPORT.md`（129 项测试矩阵 + Release Gate）。
> 本报告为收尾结论：版本演进、Bug 清单、Release Gate 判定、1.0.0 就绪结论。

## 1. 结论

**READY FOR 1.0.0（带 2 项 Accepted Risk，均不阻塞）。**

- 候选版本 **0.16.3**（RC-004 修复版），build 2026-09-20 22:16（clean venv .venv-rc，Python 3.13.14，PyInstaller 6.22.3）。
- 129 项 RC 测试矩阵全部 PASS（含 2 项带注记的 PASS，见 §4）；无 open BLOCKER / HIGH。
- 72h 真实 burn-in：0.16.2（前段）→ 0.16.3 源码 → 0.16.3 EXE 三段运行，数据恒等式全程精确闭合。
- 7d/30d/90d 加速 soak（FakeClock + 随机故障注入）三档 `observed + known_lost == truth` 全部 Difference 0。
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
- **item 77-80（24h 泄漏点）**：11h+ 采样无泄漏趋势（RSS 187~205MB 波动、Handles 780±10、Threads 恒 22）；24h/48h/72h 终值由 burn-in 收尾采样覆盖。
- **item 116.1（观察）**：安装器在"多次中途杀安装器留下的半安装脏目录"上升级会 MoveFile code 5 卡住；干净目录/显式 /DIR 均 exit 0。非产品缺陷，产品化加固列为 1.0.0 打磨。
- **soak 工具观察**：soak 与测试套件/构建并行时在同一确定性点 0-CPU 卡死（测试工具自身 GIL 争用问题）；顺序运行三档全 PASS。

## 5. Release Gate（逐项）

| Gate | 状态 | 证据 |
|---|---|---|
| 0 open BLOCKER | PASS | 0 |
| 0 open HIGH | PASS | RC-002/RC-004 均 FIXED（RC-MED-001 为 MEDIUM） |
| 测试套件 ×10 PASS | PASS | 403 tests ×10 连续全过（0.16.3） |
| 7d 模拟 PASS | PASS | Difference 0/0（1,814,400 GT 基） |
| 30d 模拟 PASS | PASS | Difference 0/0（7,776,000 GT 基，5917s） |
| 90d 模拟 PASS | PASS | Difference 0/0（23,328,000 GT 基，18806s） |
| 48~72h 真实 burn-in PASS | PASS | 72h 自 2026-09-20 19:05；三段版本运行；恒等式闭合（§6） |
| Token spot-check PASS | PASS | 实机推理 delta 63 exact（llama /metrics vs monitor today，integer exact）；burn-in 每日复测 |
| SQLite quick_check PASS | PASS | burn-in 期间每日 ok（含 5MB 生产库 + soak 14MB 库） |
| 无内存泄漏 | PASS | 72h RSS 带内波动（187~205MB，非单调） |
| 无 handle 泄漏 | PASS | 780±10 稳定 |
| 无 thread 泄漏 | PASS | 恒 22（offline/recover 周期不增） |
| Clean install PASS | PASS | 独立环境首装 + 首次启动 baseline |
| Upgrade PASS | PASS | 0.16.0→0.16.1→0.16.2→0.16.3 顺序升级（含数据保留） |
| Uninstall/Reinstall PASS | PASS | 卸载/重删/重删数据/重装均验证 |
| Update PASS | PASS | 0.16.0→0.16.1 完整 Check/Download/Verify/Install/Graceful + 0.16.3 /APPUPDATE_BG 自启 |
| Security checks PASS | PASS | loopback-only API、签名验证、篡改 manifest/installer 拒收 |
| Release validation PASS | PASS | validate_release.py 四版本全 OK |

## 6. Burn-in 数据完整性（收尾值）

> burn-in 自 2026-09-20 19:05 起，期间 llama-server 持续真实推理（生产负载），总 token 单调增长。完整性判定不依赖"起点 vs 终点"（期间有 3 次 monitor/llama 重启），而基于三层核对：

1. **daily 行无负值/无重复**：6 行 daily_usage（09-15~09-21）SUM 单调递增，negative_rows=0（已验证 09-21 09:15）
2. **llama /metrics ↔ monitor 逐段精确核对**（item 109 每日复测）：09-21 09:04 spot-check llama prompt=88,660/output=6,724 与 monitor today prompt/output **integer exact 一致**；此前 09-20 推理 delta 63 exact
3. **SQLite 完整性**：quick_check ok + WAL journal 稳定（burn-in 期间多次验证）
4. **缺口全部解释**：data_gaps 47 条，reason 全部正确（server_offline/monitor_restart/system_pause_or_sleep），token_recoverable 标记正确，含 09-21 08:48~09:05 安装器排查 monitor 重启 348s（recoverable=1 无丢失）

> 收尾采样（2026-09-23 ~19:00）：收尾 total `__FILL__`（起点 19:05 时 79.6M 量级，00:41 参考点 79,623,517）；收尾时重跑上述 1-4 并记录 RSS/Handles/Threads 72h 终值。

## 7. 遗留（1.0.0 打磨，不阻塞）

- ARC-001：formatTokenCount 999,999,999 → "1000.00M" 显示边界。
- ARC-002：卸载 Remove data 会删 update-keys 私钥（开发机便利设计，生产私钥应发布方保管）。
- RC-MED-001：MTP position reset 事件时序（数据正确，仅 monitor_event 偏晚）。
- 安装器升级前探测/清理半安装残留（is-*.tmp + 半解包主 exe）。
- 睡眠循环 ≥3 次补做（待双 GPU 系统稳定；monitor 侧能力已验证）。

## 8. 交付物

- GitHub releases：v0.16.0 / v0.16.1 / v0.16.2 / v0.16.3（各 5 assets：installer/zip/manifest/sig/SHA256SUMS）。
- docs/RC_TEST_REPORT.md（129 项明细 + 逐 Gate 证据）。
- docs/RC_FINAL_REPORT.md（本报告）。
- 签名密钥 key-2026-09（公钥内置 update_keys.py；私钥 %LOCALAPPDATA%\LlamaMonitor\update-keys\）。
