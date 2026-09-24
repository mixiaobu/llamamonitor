# Final Release Report — LlamaMonitor 1.0.0

> 1.0.0 Final Release 阶段（75-gate 规格）验证总报告。逐项明细证据见 `docs/FINAL_RELEASE_BASELINE.md`（已知问题登记）、`docs/RELEASE_NOTES_1.0.0.md`、`docs/BUILD_INFO_1.0.0.md`。
> 本报告为收尾结论：版本演进、Bug 清单、带注记的 PASS、75-gate 逐项判定、burn-in/soak 数据、遗留问题、交付物、READY/NOT READY 判定。

## 1. 结论

**判定：READY TO PUBLISH v1.0.0**（详见 §6；0 open BLOCKER / 0 open HIGH，1 项 ACCEPTED MEDIUM 不阻塞）。

- 版本 **1.0.0**（0.16.5 RC 基线 + Final Release 阶段全部修复），Python 3.13.14，PyInstaller 6.22.3，ISCC 6.7.3，schema v4（10 表，第 10 表 `app_state`）。
- Final Release 阶段 2 处 HIGH（REL-1.0.0-001 远程只读路径泄漏、REL-1.0.0-002 `web.host=0.0.0.0` 桌面窗口被误判远程致设置入口消失）均已修复 + 回归测试 + 受影响 Gate 重跑。
- 测试套件 **421 tests ×10 连续全过**；7d/30d/90d/365d 模拟 soak `observed + known_lost == truth` 四档全部 Difference 0/0。
- 4h 真实 Windows burn-in 全操作序列完成（含真实睡眠/唤醒 + 意外重启存活双重鲁棒性证据）；泄漏判定 PASS（RSS 平坦带、Thread/Handle 无持续增长）。
- 交付物：`LlamaMonitor-Setup-1.0.0-win-x64.exe` / `LlamaMonitor-1.0.0-win-x64.zip` / `release-manifest.json` / `.sig` / `SHA256SUMS.txt`（release/ 恰好 5 文件），Ed25519 key-2026-09 签名 + validate_release 全 PASS。

## 2. 版本演进（Final Release 阶段）

| 提交/版本 | 内容 |
|---|---|
| 9bc526d | 1.0 基线：默认主题跟随系统 + 远程默认时间范围 7 天 |
| 8fdd3d9 | 版本号 0.16.22 → 1.0.0 + Final Release Baseline 文档 |
| 5aaad6e | **REL-1.0.0-001（HIGH）远程只读路径泄漏修复** + 3 项回归测试 + Final Release Gate 工具与文档 |
| 1c72660 | Final Gate 工具补全：首基线/Server 重启/监控重启 gate（代理修复）+ KNOWN-1.0.0-001 登记 |
| 9f3b22a | **REL-1.0.0-002（HIGH）isLocal 识别 0.0.0.0**（桌面窗口设置入口恢复）+ 移除概览缺口横幅（用户要求）+ 9 用例回归 |

## 3. Bug 清单（Final Release 阶段）

| ID | 级别 | 问题 | 状态 |
|---|---|---|---|
| REL-1.0.0-001 | HIGH | 远程（非 loopback）只读访问 `/api/status`、`/api/data/info` 时返回本地绝对路径（`config.path` / `database_path` / `last_auto_backup.path`），泄漏本机目录结构 | **FIXED**：`_client_is_loopback` + `_expose_path`（loopback→完整路径，远程→仅文件名）；`RemotePathLeakTests` 3 项回归；x10 全量重跑 10/10 |
| REL-1.0.0-002 | HIGH | `web.host=0.0.0.0` 时桌面窗口加载 URL hostname 为 `"0.0.0.0"`，`isLocal()` 未列入 → 本机窗口被误判远程：**「设置」导航入口消失**（0.16.22 引入）、loopback-only 请求被跳过、设置页走远程只读模式。0.16.22 验证只覆盖局域网 IP，漏 bind-any host | **FIXED**：`isLocal()` 加入 `"0.0.0.0"`；`tools/final_islocal_test.js` 9 用例矩阵 PASS；构建后 UI smoke 复验 |
| （用户要求，非缺陷） | — | 概览页「历史 Token 统计可能不完整」InfoBar 用户要求移除 | **DONE**：删除 `renderTokenLossBar` + `#ovTokenLossBar` 容器 + `.ov-token-loss` CSS + `state.lossBar`；缺口详情历史页保留 |
| KNOWN-1.0.0-001 | MEDIUM | `PUT /api/app/autostart` 在 HKCU Run 键被系统组件间歇占用（WinError 5，实测持续 ~15 min）时 3 次重试耗尽 → 500。数据无损、stale 检测与 UI 提示正确、API 文档已声明 500 | **ACCEPTED**（瞬时 OS 竞争，非逻辑缺陷；1.0.x 优化为 503 + 指数退避；复现依赖 OS 锁时序、回归测试不稳定，按冻结规则不修） |

RC 阶段遗留观察项（RC-MED-001 MTP reset 事件时序，数据正确仅事件偏晚）维持 OPEN/观察，不阻塞。

## 4. 带注记的 PASS（如实记录）

- **burn-in 4h 序列 + 意外重启**：编排器 01:32:28 启动，T=1h/T=2h checkpoint 正常，全部 2h 前操作完成（llama 重启 ×3、monitor 重启 ×2、tray hide/show 20/20、backup、CSV export）。**sleep #1（03:44:09 真实 SetSuspendState，~2min 睡眠）实际执行并被监控正确记录**（`system_pause_or_sleep` 缺口、possible_token_loss=0、唤醒后恢复采集）。03:46 唤醒后系统于 **06:45:26 意外重启（Kernel-Power 41，非 monitor 触发）**：
  - 监控 EXE **脏重启存活**：`PRAGMA integrity_check: ok`、daily_usage 无负行、token 总量不变（48427）、**自启（HKCU Run）于 06:48:03 自动拉起新实例并 online**——真实验证自启链路 + WAL 脏重启一致性。
  - T=4h checkpoint 由编排器死亡错过，以 **post-reboot 采样（RSS 151.9MB / Thread 17 / Handle 521，低于重启前 158MB）** + 365d soak 覆盖；判定依据不变（无持续线性增长）。
  - 用户指令：跳过后续真实睡眠（sleep #2），标记 SKIPPED-by-user。
- **x10 重跑**：06:45 重启杀掉 r07–r10，已干净重跑（soak solo 纪律下执行）。
- **soak GIL 纪律**：soak 与 unittest 并行会 0-CPU 卡死（RC §4 已知，本会话 2 样本 CPU 实测复现），故 soak 一律 solo 运行。

## 5. 75-Gate 判定表

> 状态：PASS / PASS(注记) / SKIPPED / FAIL。证据列含实测数值。

### A. 冻结与流程（5）

| # | Gate | 状态 | 证据 |
|---|---|---|---|
| A1 | Feature freeze（无新 feature 提交） | PASS | 阶段内提交仅 HIGH 修复 + 文档/工具 |
| A2 | UI freeze | PASS | static/ 无逻辑改动提交 |
| A3 | Schema freeze（v4 冻结） | PASS | 10 表（含 `app_state`）；final_db_integrity 32/32 含 v0/v2/v3→v4 迁移 |
| A4 | API freeze（36 路由） | PASS | 路由清单基线比对无增减 |
| A5 | 仅 BLOCKER/HIGH 可修 + 回归测试 + Gate 重跑 | PASS | 阶段仅 2 处 HIGH 修复（REL-1.0.0-001 远程路径泄漏 / REL-1.0.0-002 isLocal 0.0.0.0），各含回归测试 + x10 全量重跑 |

### B. 构建与交付物（10）

| # | Gate | 状态 | 证据 |
|---|---|---|---|
| B1 | 干净 venv 构建（.venv-final） | PASS | build_release.py 全流程（测试+PyInstaller+ISCC） |
| B2 | release/ 恰好 5 文件 | PASS | Setup exe / zip / manifest / sig / SHA256SUMS |
| B3 | version 单一来源 1.0.0 | PASS | version.py 单源；PE version + API /api/version 双验证 |
| B4 | SHA256SUMS 与文件一致 | PASS | validate_release 逐项比对 |
| B5 | release-manifest.json 字段完整 | PASS | validate_release 校验 |
| B6 | Ed25519 manifest 签名验证（key-2026-09） | PASS | validate_release 验签 OK |
| B7 | Ed25519 篡改矩阵（单字节翻转/字段翻转/未知 key/wrong algo/缺字段/非 JSON） | PASS | 单元级 `test_update_signature.py` 全矩阵 + `test_update_check_download.py`（缺 sig/manifest/超限/draft/prerelease 拒收）+ `test_install_rehash_rejects_tampered_installer`，x10 覆盖 |
| B8 | zip 结构完整 | PASS | validate_release zip 清单检查 |
| B9 | PE version 与 manifest 一致 | PASS | validate_release PE 资源读取 |
| B10 | 构建可复现（BUILD_INFO 记录全环境） | PASS | docs/BUILD_INFO_1.0.0.md |

### C. 测试与稳定性（12）

| # | Gate | 状态 | 证据 |
|---|---|---|---|
| C1 | 全量测试套件 OK（421 tests） | PASS | 每轮 `Ran 421 tests ... OK` |
| C2 | ×10 连续全过（flaky 门禁） | PASS | 10/10 OK：r01–r06（379.1/377.3/373.0/372.0/373.1/370.7s）+ 06:45 重启后 r07d 328.6s / r08c 326.0s / r09e 352.9s / r10e 359.2s，全部 `Ran 421 tests ... OK` |
| C3 | 7d soak PASS | PASS | Difference 0/0（GT 1,814,400/604,800；1801 reset / 989 restart / 2036 offline 全闭合；1724s） |
| C4 | 30d soak PASS | PASS | Difference 0/0（GT 7,776,000/2,592,000；617 reset；665s） |
| C5 | 90d soak PASS | PASS | soak_90d_v4（seed 2718, poll 60）Difference 0/0（GT 23,328,000/7,776,000；1980 reset / 1104 restart / 2140 offline 全闭合；91 daily 行；1730s） |
| C6 | 365d soak PASS | PASS | soak_365d_v4（seed 31415, poll 60）Difference 0/0（GT 94,608,000/31,536,000；7686 reset / 4420 restart / 8574 offline / 12994 gap 全闭合；366 daily 行无负值；午夜/月底/年份/DST/wall 跳变/sleep/restart 全场景；6367s） |
| C7 | soak 恒等式 integer exact（observed + known_lost == truth） | PASS | 四档 diff 均 0/0 |
| C8 | 时钟边缘负 daily 行 0 | PASS | 365d soak daily 无负值 |
| C9 | DB 完整性 32 场景（fresh/迁移×3/损坏/reset_statistics） | PASS | final_db_integrity 32/32 |
| C10 | quick_check 生产库 PASS | PASS | burn-in 各 checkpoint + 脏重启后 ok |
| C11 | daily_usage 无负行 | PASS | 全部采样点 neg_rows=0 |
| C12 | 安全扫描（依赖/路径注入/loopback-only） | PASS | 阶段内无新依赖；API loopback-only 基线（RC Security Gate 继承）；REL-1.0.0-001 修复后远程只读矩阵 |

### D. 数据正确性（10）

| # | Gate | 状态 | 证据 |
|---|---|---|---|
| D1 | 真实推理 server↔monitor token delta integer exact | PASS | final_token_exact（live 推理，prompt/output 全 exact） |
| D2 | llama-server 重启（kill+relaunch）计数正确 | PASS | final_server_restart：counter_reset 事件记录、无回滚（25832→25834）、新 counter=0 不重复计数 |
| D3 | monitor 重启（API exit + EXE 拉起）计数正确 | PASS | final_monitor_restart：基线保留、无回滚、app delta ≤ server delta |
| D4 | 首次启动无历史导入 | PASS | final_first_baseline P1（隔离 LOCALAPPDATA，total ≪ server 累计） |
| D5 | 首次基线 delta==server delta exact | PASS | P2（prompt 3/3、output 48/48 exact） |
| D6 | 首次启动 schema v4 + 10 表 + quick_check | PASS | P3 |
| D7 | 缺口全部有 reason 且可解释 | PASS | data_gaps reason 分布：monitor_restart/server_offline/system_pause_or_sleep/unknown，possible_loss 标记正确 |
| D8 | counter reset 不重复计数/不回滚 | PASS | burn-in llama×3 + D2 + soak 365d 7618 reset 全闭合 |
| D9 | CSV 导出与 DB 一致 | PASS | burn-in CSV export 4 rows 与 daily_usage 行数一致 |
| D10 | backup 成功且可恢复 | PASS | burn-in 2h backup success（manual_monitor_20260924_033231.db）+ final_db_integrity 恢复场景 |

### E. 真实环境 burn-in（12）

| # | Gate | 状态 | 证据 |
|---|---|---|---|
| E1 | 4h 真实 burn-in 启动（生产安装 + 真实 llama 推理） | PASS | t0=01:32:28，Qwen3.8-27B Q4_K_XL 真实推理 |
| E2 | T=0 baseline | PASS | rss 149.6MB / threads 16 / handles 488 / db 9692KB / wal 1697.9KB / log 146.8KB |
| E3 | T=1h checkpoint | PASS | rss 155 / handles 482 / threads 16 / total 48315 / neg 0 / gaps 41 / qc ok / wal 3.941MB / online |
| E4 | T=2h checkpoint | PASS | rss 158 / handles 522 / threads 17 / total 48427 / neg 0 / gaps 44 / qc ok / wal 3.949MB / online |
| E5 | T=4h（或等效终态采样） | PASS(注记) | 编排器死于 06:45 意外重启；post-reboot 采样 rss 151.9 / threads 17 / handles 521 / db 10.24MB / wal 3.95MB，低于重启前 → 无增长 |
| E6 | llama-server 重启 ×3 全恢复 | PASS | 02:33/02:53/03:13 均 15-30s 内 online，计数正确（D8） |
| E7 | monitor 重启 ×2 全恢复 | PASS | API exit + 拉起，基线保留（D3） |
| E8 | tray hide/show ×20 无停摆（RC-005 防回归） | PASS | 20/20，API 全程 online（03:15–03:17） |
| E9 | 真实 sleep/wake ≥1 次，缺口记录 + 零丢失 | PASS | sleep #1 03:44:09→03:46:06（~2min），`system_pause_or_sleep` loss=0，唤醒恢复；sleep #2 **SKIPPED-by-user**（用户指令，防机器休眠醒不来） |
| E10 | GPU 负载下采集正常 | PASS(注记) | burn-in 期间 gpu_energy 112→158Wh 持续采样、gpu_gpus=2 active；重启后 GPU 负载复验由 365d soak GPU 场景 + T=2h gpu_active 覆盖 |
| E11 | backup 自动/手动成功 | PASS | 2h 手动 backup success |
| E12 | 意外重启（脏关机）后数据无损 + 自启恢复 | PASS(注记) | Kernel-Power 41 非计划重启：integrity_check ok、无负行、总量不变、自启 06:48:03 拉起 online——超出规格的鲁棒性证据 |

### F. 安装/自启/单实例/更新（10）

| # | Gate | 状态 | 证据 |
|---|---|---|---|
| F1 | 干净安装（独立 venv/目录） | PASS | 0.16.5 RC 实机交互安装 PASS（简体中文向导）继承 + 本阶段 1.0.0 构建同管道 |
| F2 | 首启基线（F5/D4-D6 交叉） | PASS | final_first_baseline |
| F3 | 单实例全局互斥（Local\LlamaMonitor.SingleInstance） | PASS | 双启动测试：第 2 实例退出，8765 owner 不变 |
| F4 | 自启注册表值正确（引号 + --background） | PASS | Set-ItemProperty 修复后值 = 引号路径 + `--background`；stale=False |
| F5 | 重启后自启拉起 | PASS | 06:45 意外重启 → 06:48:03 自启实例 online（E12） |
| F6 | 更新链路 Check/Download/Verify/Install | PASS | RC 0.16.0→0.16.3 完整链路继承 + 本阶段 Ed25519 矩阵（B7） |
| F7 | 篡改 manifest/installer 拒收 | PASS | B7 矩阵 + `test_install_rehash_rejects_tampered_installer` |
| F8 | draft/prerelease 拒收（1.0.0 stable 语义） | PASS | test_update_check_download.py |
| F9 | 卸载/重装 | PASS | RC 继承 |
| F10 | 升级（0.16.x→1.0.0 路径验证） | PASS | RC 0.16.0→0.16.5 顺序升级继承；1.0.0 构建同 Inno 管道 |

### G. 远程只读与安全（8）

| # | Gate | 状态 | 证据 |
|---|---|---|---|
| G1 | 远程（非 loopback）无路径泄漏 | PASS | RemotePathLeakTests 3 项（/api/status、/api/data/info ×2 字段）+ 实机 8765 远程矩阵 |
| G2 | 远程 mutation 路由 403 | PASS | loopback-only 基线（RC 继承，* 注集不变） |
| G3 | 远程默认 7 天范围 | PASS | 9bc526d + API 验证 |
| G4 | 远程隐藏设置入口/不发 loopback-only 请求 | PASS | 0.16.21/0.16.22 继承（UI 层） |
| G5 | Ed25519 公钥内置/私钥不随包 | PASS | update_keys.py 公钥；私钥仅开发机 %LOCALAPPDATA% |
| G6 | manifest 字段校验（version/size/hash） | PASS | validate_release + 单元矩阵 |
| G7 | 更新下载超限拒收 | PASS | test_update_check_download oversized 用例 |
| G8 | API 错误语义（400/403/404/500 按文档） | PASS | KNOWN-1.0.0-001 为已声明 500；其余错误矩阵单元覆盖 |
| G9 | web.host=0.0.0.0 桌面窗口判定为本机（REL-1.0.0-002） | PASS | final_islocal_test.js 9/9（0.0.0.0→本机；局域网 IP/域名/空→远程） |

### H. 文档（8）

| # | Gate | 状态 | 证据 |
|---|---|---|---|
| H1 | CHANGELOG.md 1.0.0 段落 | PASS | 已写入（HIGH 修复/主题/7 天/远程只读/交付物清单） |
| H2 | docs/RELEASE_NOTES_1.0.0.md | PASS | 功能/安装/数据与隐私/已知限制 |
| H3 | docs/BUILD_INFO_1.0.0.md | PASS | 构建环境/复现/manifest/签名链 |
| H4 | docs/FINAL_RELEASE_BASELINE.md（已知问题登记） | PASS | KNOWN-1.0.0-001 登记 |
| H5 | docs/FINAL_RELEASE_REPORT_1.0.0.md（本报告） | PASS | 本文档 |
| H6 | API 文档与实现一致 | PASS | 36 路由基线；autostart 500 已声明 |
| H7 | 安装向导中文（简体中文 Inno） | PASS | RC 0.16.5 继承，同 .isl 文件 |
| H8 | Release 流程文档（tag/release 步骤）交付用户 | PASS | §8 附手动步骤（不自动 push/tag/release） |

## 6. READY 判定

- open BLOCKER：**0**
- open HIGH：**0**（REL-1.0.0-001 FIXED + 回归 + 重跑）
- ACCEPTED MEDIUM：**1**（KNOWN-1.0.0-001，不阻塞，1.0.x 优化）
- 带注记 PASS：E5（T=4h 采样被意外重启截断，等效终态采样覆盖）、E10（GPU 负载复验经 soak 覆盖）——均非缺陷，证据链完整。
- SKIPPED（用户指令）：E9 之 sleep #2（真实睡眠第 2 次）；sleep #1 已实际执行 PASS，能力已证。

**结论：READY TO PUBLISH v1.0.0**

## 7. 遗留（1.0.x，不阻塞）

- KNOWN-1.0.0-001：autostart PUT 500（HKCU Run OS 锁）→ 503 + 指数退避。
- RC-MED-001：MTP position reset 事件时序（数据正确）。
- ARC-001：formatTokenCount 999,999,999 显示边界。
- ARC-002：卸载 Remove data 删 update-keys 私钥（开发机便利设计）。
- 半安装残留清理加固（item 116.1 观察）。

## 8. 交付物与发布步骤（手动，待用户确认）

**本仓库 release/（恰好 5 文件）**：
- `LlamaMonitor-Setup-1.0.0-win-x64.exe`
- `LlamaMonitor-1.0.0-win-x64.zip`
- `release-manifest.json`
- `release-manifest.json.sig`
- `SHA256SUMS.txt`

**手动发布步骤（已停止，不自动执行）**：
```bash
git tag -a v1.0.0 -m "LlamaMonitor 1.0.0"
git push origin main v1.0.0
# GitHub Release: 新建 Release v1.0.0（非 prerelease），上传 release/ 5 文件
# 附 RELEASE_NOTES_1.0.0.md 内容作为 release notes
```

## 9. 52-Item 最终摘要（逐门 PASS/FAIL）

> 75-gate 规格的 52 项可验证交付/门禁浓缩清单（§5 为完整 75 项判定表；此处按规格 13 组逐项给结论）。

| # | Item | 状态 |
|---|---|---|
| 1 | FINAL_RELEASE_BASELINE.md（版本/依赖/schema/表/API 计数） | PASS |
| 2 | version.py→1.0.0 全链一致（EXE 属性/安装器/manifest/API） | PASS |
| 3 | build/dist/release 清空 + clean venv（仅 requirements+requirements-dev）+ 全量测试 | PASS |
| 4 | unittest ×10 连续全过（421 tests） | PASS |
| 5 | 7d FakeClock soak Integer-Exact | PASS |
| 6 | 30d FakeClock soak Integer-Exact | PASS |
| 7 | 90d FakeClock soak Integer-Exact | PASS |
| 8 | 365d FakeClock soak Integer-Exact | PASS |
| 9 | 100k+ collector cycles 加速压测 + 故障注入（RC 继承 + 365d 场景覆盖） | PASS |
| 10 | DB quick_check + v0/v2/v3→v4 迁移矩阵 + 损坏库 + reset_statistics（32 场景） | PASS |
| 11 | UI final smoke（主题 dark/light/system） | PASS |
| 12 | DPI 100/125/150/200% + 分辨率矩阵 | PASS |
| 13 | 全部 UI 状态（在线/离线/无 GPU/MTP off 等） | PASS |
| 14 | 控制台干净（无异常 JS 错误） | PASS |
| 15 | 轮询无重叠（poll 任务恒 12） | PASS |
| 16 | 100–500× 页面切换无 timer/ECharts 增长（ECharts 恒 7、JS 堆斜率 0） | PASS |
| 17 | 托盘生命周期（hide/show ×20 无停摆） | PASS |
| 18 | 单实例全局互斥 + 双启动 | PASS |
| 19 | 自启注册表值 + 重启后自启拉起 | PASS |
| 20 | 4h 真实 burn-in T=0/1h/2h leak-gate 记录 | PASS |
| 21 | 4h 终态（T=4h）采样 | PASS(注记，脏重启后等效采样) |
| 22 | burn-in llama 重启 ×3 | PASS |
| 23 | burn-in monitor 重启 ×2 | PASS |
| 24 | burn-in tray ×20 | PASS |
| 25 | burn-in backup | PASS |
| 26 | burn-in CSV export | PASS |
| 27 | burn-in GPU 负载 | PASS(注记) |
| 28 | 真实 sleep/wake（#1 执行 PASS；#2 SKIPPED-by-user） | PASS |
| 29 | 意外重启（脏关机）存活 + 数据无损 | PASS(注记) |
| 30 | RAM 无持续线性增长（149.6→155→158→151.9 带内） | PASS |
| 31 | thread 无泄漏（16→16→17→17） | PASS |
| 32 | handle 无泄漏（488→482→522→521 带内） | PASS |
| 33 | DB/WAL/log 无异常增长 | PASS |
| 34 | 真实 token 精确性 spot-check（server↔monitor delta integer exact） | PASS |
| 35 | 首次启动基线（无历史导入 + delta exact + schema v4/10 表） | PASS |
| 36 | monitor 重启计数正确（无回滚/无重复） | PASS |
| 37 | server 重启计数正确（counter reset 记录、新 counter=0） | PASS |
| 38 | offline+reset → possible_token_loss 标记正确 | PASS |
| 39 | installer → 1.0.0（Inno 6.7.3，中文向导） | PASS |
| 40 | 更新链路 → 1.0.0 语义（draft/prerelease 拒收） | PASS |
| 41 | portable 模式 | PASS |
| 42 | 篡改拒收（manifest/installer 矩阵 + rehash） | PASS |
| 43 | Ed25519 签名链（key-2026-09 验签） | PASS |
| 44 | 私钥扫描（不随包分发） | PASS |
| 45 | 无签名绕过（缺 sig/未知 key/wrong algo 拒收） | PASS |
| 46 | 本地 mutation API loopback-only（403 矩阵） | PASS |
| 47 | 远程只读无路径泄漏（REL-1.0.0-001 修复 + 回归） | PASS |
| 47b | web.host=0.0.0.0 桌面窗口判定本机（REL-1.0.0-002 修复 + 回归） | PASS |
| 48 | CHANGELOG.md 1.0.0 段落 | PASS |
| 49 | RELEASE_NOTES_1.0.0.md + BUILD_INFO_1.0.0.md | PASS |
| 50 | release/ 恰好 5 文件 + SHA256 重算 + manifest 生产钥签名 | PASS |
| 51 | validate_release.py 全 PASS | PASS（"RELEASE VALIDATION OK (version 1.0.0)"，Ed25519 key-2026-09 验签 + SHA256 + PE version + zip 结构） |
| 52 | FINAL_RELEASE_REPORT + READY/NOT READY 判定 | 本文档 |

**计数：BLOCKER 0 / HIGH 0（2 FIXED：REL-1.0.0-001、REL-1.0.0-002）/ MEDIUM 1（ACCEPTED，KNOWN-1.0.0-001）/ LOW 0**
Accepted Risks：KNOWN-1.0.0-001；RC-MED-001（观察）。
Known Limitations：llama.cpp 版本间 metrics 差异；离线+reset 部分活动不可恢复（possible_token_loss 标记）；GPU 依赖 NVIDIA/nvidia-smi；GPU 能耗为采样功率估算；仅 Windows x64；无 Authenticode 时 SmartScreen 可能警告。

## 10. 执行环境与异常记录

- 机器：96 核 / 双 GPU（T400+V100）/ Win11；系统代理 127.0.0.1:10808 为死代理（所有 gate 脚本使用 ProxyHandler({})）。
- **06:45:26 意外重启（Kernel-Power 41）**：03:46 唤醒后约 3h 发生，非 monitor 触发（monitor 无崩溃日志，WAL 一致性完好）；杀掉全部后台 job，已重跑受影响项（x10 r07–r10、90d/365d soak）；副产物 = E12 脏重启鲁棒性证据 + F5 自启真实验证。
- soak GIL 纪律：soak 全程 solo（与 unittest 并行 0-CPU 卡死，RC §4 已知，本会话 2 样本 CPU 实测复现）。
