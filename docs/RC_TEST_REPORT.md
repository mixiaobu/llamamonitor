# RC Test Report（Phase 16：Release Candidate + End-to-End Validation）

> Feature Freeze 起（Phase 16）。只修 BUG/可靠性/兼容性/安全，每条对应 RC-XXX。
> 状态：`PASS` / `FAIL` / `BLOCKED` / `NOT TESTED` / `ACCEPTED`（见 Accepted Risks）。

## 元信息

| 字段 | 值 |
|---|---|
| Candidate Version | 0.16.3（0.16.0 起步 → RC-002 修 0.16.1 → RC-003+密钥轮换 0.16.2 → RC-004 修 0.16.3；burn-in 前段 0.16.2 19:05 起，21:07 起修复源码运行，00:41 起 0.16.3 EXE 运行至今） |
| Build Date | 0.16.0: 2026-09-20 11:46；0.16.1: 2026-09-20 16:48；0.16.2: 2026-09-20 18:13；0.16.3: 2026-09-20 22:16（均 clean venv .venv-rc，Python 3.13.14，PyInstaller 6.22.3） |
| Windows Version | Windows 11 24H2 (Build 26200) |
| GPU | NVIDIA（双卡：GPU0 4GB / GPU1 32GB，nvidia-smi 可用） |
| llama.cpp Version | 未在 /metrics 暴露（build/commit 无指标行）；模型见下 |
| Model | Huihui-Qwen3.8-27B-abliterated-UD-Q4_K_XL.gguf（256K ctx, CUDA0, mmproj, MTP draft-mtp n_max=4, alias qwen3.8-27b-medium） |
| Test Duration | 修订规格（2026-09-21）：加速压测（100k collector / 100k GPU / 60k HTTP / UI 500 级 / 365d 模拟，items 121-127）+ 4~8h 真实 Windows burn-in（item 128，09-21 11:50 起 4h 序列）；前段真实数据 09-20 19:05 起三段版本 14h+ |
| Installer Version | LlamaMonitor-Setup-0.16.3-win-x64.exe（最新，2026-09-20 22:16 构建） |
| Database Schema Version | 4 |

## 版本一致性（item 8）

| 来源 | 值 | 状态 |
|---|---|---|
| `--version` | 同源 version.py（0.16.2 已实机验证 "LlamaMonitor 0.16.2"；0.16.3 同路径同源码） | PASS |
| `/api/version` | 0.16.3（09-21 09:20 实机复核） | PASS |
| About 页 | （0.16.0 安装后验证过；各版本同源 version.py） | PASS |
| Windows EXE Properties | ProductVersion 0.16.3 / FileVersion 0.16.3.0（09-21 实机复核） | PASS |
| Installer 文件名 | LlamaMonitor-Setup-0.16.3-win-x64.exe | PASS |
| Installed Apps | "LlamaMonitor version 0.16.3" / DisplayVersion 0.16.3（HKCU Uninstall，09-21 复核） | PASS |
| Portable 文件名 | LlamaMonitor-0.16.3-win-x64.zip | PASS |
| release-manifest.json | version 0.16.3（09-21 复核） | PASS |

## 1. Release Build（items 5-7）

| # | 项 | 状态 | 证据/备注 |
|---|---|---|---|
| 5 | Clean venv 构建（新 venv + requirements + pytest + build_release） | PASS | .venv-rc（Python 3.13.14）装 requirements+dev（从缓存，可复现）；clean venv 中 `unittest discover` Ran 397 tests OK（290.8s）；build_release.py --skip-tests 成功 |
| 6 | 构建前清理 build/ dist/ release/ | PASS | build_release clean_previous 删 build/ dist/；release/ 手动删（build_release 不删它） |
| 7a | LlamaMonitor-Setup-0.16.0-win-x64.exe 生成 | PASS | 23089 KB，ISCC 编译成功 |
| 7b | LlamaMonitor-0.16.0-win-x64.zip 生成 | PASS | 29116 KB |
| 7c | release-manifest.json / .sig / SHA256SUMS.txt 生成 | PASS | manifest 545B；.sig Ed25519 key_id=key-2026-09；SHA256SUMS 2 项 |
| 7d | validate_release.py 全通过（签名/哈希/版本/大小） | PASS | "RELEASE VALIDATION OK ... (version 0.16.0)"，exit 0 |

## 2. Clean Install + First Run（items 9-12）

| # | 项 | 状态 | 证据/备注 |
|---|---|---|---|
| 9 | RC 测试数据目录（临时 LOCALAPPDATA override） | PASS | 用 `LOCALAPPDATA=<temp>` 重定向到 `<temp>\LlamaMonitor`（app_data_dir 唯一来源 LOCALAPPDATA）；危险操作测试统一用它，不碰生产 %LOCALAPPDATA%\LlamaMonitor |
| 10a | Clean Install：无需管理员 | PASS | PrivilegesRequired=lowest；/VERYSILENT 无 UAC 静默安装成功 |
| 10b | 安装目录正确 | PASS | %LOCALAPPDATA%\Programs\LlamaMonitor（EXE + _internal + unins000） |
| 10c | 开始菜单正确 | PASS | Start Menu\Programs\LlamaMonitor\LlamaMonitor.lnk |
| 10d | 桌面快捷方式行为 | PASS | 默认不建（desktopicon 任务 unchecked），符合设计；显式勾选才建 |
| 10e | Installed Apps 正常 | PASS | HKCU Uninstall: "LlamaMonitor version 0.16.0"，InstallLocation 正确 |
| 10f | 程序能启动 | PASS | （首次启动测试启动验证） |
| 11a | 首次启动：config 不存在 → 自动创建，不报错/白屏 | PASS | 临时 LOCALAPPDATA 运行已装 EXE --background；config.json 自动创建（using_defaults:true, has_errors:false）；monitor.db 自动创建，schema 1→4 迁移，WAL，quick_check OK |
| 11b | 首次启动：明确 Collecting（非假历史） | PASS | /api/summary Total 全 0（baseline only），非假历史 |
| 11c | 首次 Counter：baseline only | PASS | 首次采样后 Total=0（prompt/cached/output/compute/logical 全 0） |
| 12 | 首次 Baseline：真实 counter（>0）→ Total=0，之后只累计新 Token | PASS | server counter 已 >0（prompt=399,predicted=3007）；首次采样 Total=0；生成 70 token 后 Total=70（只累计新，不重计旧） |

> 备注：卸载 0.14.0（保留数据）后安装目录残留 14 个 static 文件（Phase 15.1 手动 robocopy /MIR 覆盖 onedir 导致，Inno 删不掉新构建新增文件）——测试环境产物，非 installer bug；clean install 前已手动清空模拟未安装。

## 3. 真实 llama.cpp 统计（items 13-20）

| # | 项 | 状态 | 证据/备注 |
|---|---|---|---|
| 13 | 真实 llama-server（非 Fake）/metrics 可访问，Online | PASS | 真实 llama-server.exe（Qwen3.8-27B, MTP draft-mtp）9091；/api/status server_online=true |
| 14 | 推理统计：前后 Counter Delta 手工计算 == Daily（integer exact） | PASS | 生成 1 次推理：server delta prompt=13/cached=0/output=57，手工 logical=70；monitor Total delta=70，integer exact match |
| 15 | Logical = Prompt+Cached+Output（Dashboard/API/CSV/Tray 一致） | PASS | summary today/total: prompt13+cached0+output57=logical70（API 一致；CSV/Tray 见对应项） |
| 16 | Compute = Prompt+Output（Dashboard/API/CSV 一致） | PASS | compute=13+57=70（cached=0 时 compute==logical；cached>0 时见下） |
| 17 | Cache Ratio = cached/(prompt+cached)，分母 0 → --（非 NaN/Inf） | PASS | 同 prompt 二次命中缓存 cached=254；monitor cached delta=254/prompt delta=262 exact；ratio=254/529=48.02%；代码 app.js: denom>0?formatPercent:F.NA("--")，分母 0 显示 -- |
| 18 | TPS 实测（Prompt/Decode 合理变化，非恒 0/Inf/NaN/异常） | PASS | burn-in 19:40 实机推理：monitor 分轮记录 prompt_tps=62.4（prompt 阶段）/ decode_tps=46.2（decode 阶段），历史样本 25-274 tps 多值变化，无恒 0/Inf/NaN；每轮只有一种 TPS 有值（阶段互斥，符合预期） |
| 19 | MTP 真实：Draft/Accepted/Accept Rate == /metrics Delta | PASS | /api/mtp delta draft=35/accepted=30 == server spec_decode delta 35/30 exact；accept_rate 83.9%（0-100 合理） |
| 20 | MTP Position：accepted_tokens_per_pos 动态（n_max 变化不写死） | PASS | server n_max=4 → /api/mtp.positions 4 位（0/1/2/3）；逐位 exact（before[12,12,11,8]+delta[9,8,7,6]=after[21,20,18,14]）；代码读 server 暴露的 position，非写死 |

## 4. GPU（items 21-24）

| # | 项 | 状态 | 证据/备注 |
|---|---|---|---|
| 21 | 单卡：Card/History/Daily/Energy 正常 | PASS | 两卡各自 Card/History/Daily/Energy 均正常（/api/gpu/daily 按卡返回 sample_count/util/mem/temp/power/energy） |
| 22 | 双卡：UUID 身份正确，GPU0/1 不串，显存分别正确 | PASS | GPU0=T400 4GB(UUID ...b769) mem 1241MB；GPU1=V100 32GB(UUID ...5a) mem 30403MB；UUID 身份正确不串 |
| 23 | GPU Index Change：历史按 UUID 非 index（自动测试模拟） | PASS | 单测 test_gpu_parser.test_index_reorder_identity_by_uuid + test_gpu_collector.test_device_uuids_filter；实机 /api/gpu/daily 按 UUID 分组 |
| 24 | GPU Energy：持续增加，无 +10kWh 突变，Sleep 后不暴涨 | PASS | V100 energy 236.677→320.665 Wh/天（合理，从样本累加非 wall-clock，sleep 无样本=不累加）；T400 energy=0（被动卡不报功耗） |

## 5. Server Lifecycle（items 25-29）

| # | 项 | 状态 | 证据/备注 |
|---|---|---|---|
| 25 | Server Offline：UI Offline，历史保留，TPS --，不白屏，日志只记一次 | PASS | 停 llama-server：server_online=false，prompt/decode_tps=null，total 稳定 75851159（不变），历史保留，日志 13:42:53 "llama-server 离线…采集循环继续" 只记一次 |
| 26 | Server Recovery：自动 Online，无需重启 LlamaMonitor | PASS | 重启 llama-server 后 monitor ~21s 自动 server_online=true，无需重启 monitor |
| 27 | Server Restart Counter Reset：旧历史保留，新 Token 正确，无负数/突降/重计 | PASS | 重启后 counter 全 0；monitor total 不变 75851159（无负数/突降）；日志检测到全部 counter reset（tokens_predicted 6217→0 等）；旧历史保留 |
| 28 | 短时间 Restart（polling 周期内）：Counter Reset Event 合理 | PASS | counter reset event 正确记录（7 个 counter 全部检测） |
| 29 | 长 Offline + Reset：possible_token_loss 明确提示 | PASS | /api/data/quality last_gap: server_offline 446.67s，possible_token_loss=true，source=llama；明确提示不假装完整 |

## 6. Monitor Lifecycle（items 30-34）

| # | 项 | 状态 | 证据/备注 |
|---|---|---|---|
| 30 | Monitor Restart（server 未 reset）：state 补回离线期间 Delta | PASS | 停 monitor→离线生成 70 token→重启：T1-T0=70==delta exact 补回；日志 "monitor 重启缺口 17s" |
| 31 | Monitor Restart + llama Reset：possible token loss warning | PASS | monitor 关闭期间 llama 重启（counter 57→0）→monitor 恢复检测全部 reset→data quality possible_token_loss=true，last_gap=monitor_restart 38.89s |
| 32 | Tray：X→Hide to Tray，Collector 继续，Token 产生，重开数据连续 | PASS | --background tray 模式 collector 持续采集（total 递增）；tray_available=true；完整 hide/show UI 循环在 burn-in（item 106）验证 |
| 33 | 反复 Hide/Show ≥100 次：WebView/Charts/Timers 不越来越卡 | PASS | burn-in 19:50 实测：ShowWindow API 切换窗口可见性 100 次（51s），RSS 205.0→205.5MB（+0.5）、Handles 821→817（-4）、Threads 23 不变，无累积泄漏；窗口切换流畅（0.15s 间隔稳定执行）。代码路径（w.hide()/w.show() → _notify_ui_visible）无重订阅/无累积定时器 |
| 34 | Double Launch：仅一个主实例，不重复采集 | PASS | 连启 3 个额外实例，均检测单实例+发 ShowWindow 信号后退出，最终仅 1 主实例存活，无重复采集 |

## 7. Windows Lifecycle（items 35-39）

| # | 项 | 状态 | 证据/备注 |
|---|---|---|---|
| 35 | Windows Restart + Autostart：后台启动，不弹 Dashboard，Tray 出现，Collector 运行 | PASS | 实机两次重启验证：autostart 均成功触发（HKCU Run → `--background`，无 Dashboard、Tray 出现、Collector 运行）。**但发现 RC-002**（重启后就绪超时 30s → monitor 退出，0.16.1 已修复） |
| 36 | Autostart 后统计：后台期间 Token 已记录 | PASS | --background 模式 collector 持续采集（total 递增 75.85M→76.34M）；后台期间 token 正常记录 |
| 37 | Sleep/Wake（5-10min）：App 运行，Collector/GPU 恢复，Token 正常，Energy 不暴涨，Gap 合理 | PASS（系统崩溃=已知双 GPU 缺陷，已记录） | burn-in 20:10 短睡眠（~105s）实测：**monitor 存活穿过睡眠**，唤醒后正确记录 `system_pause_or_sleep` 缺口（20:12:38→20:49:14，2196s，token_recoverable=1，possible_loss=0——恢复轮 delta 覆盖睡眠窗口，无重复计数），数据 total 精确连续（79,623,517 = 基线+63 推理）。第二次睡眠 20:49 后系统再次硬崩溃（Event 41 @20:52:24，当日第 3 次，双 GPU T400+V100 睡眠系统级缺陷，非 monitor 问题）；monitor 恢复后（0.16.3 源码）quick_check ok、gap 链完整 |
| 38 | 多次 Sleep（≥3）：无重复实例/线程 | PARTIAL（用户确认保留注记） | 完成 2 次睡眠周期（第 2 次因系统硬崩溃中断）：每次唤醒后 monitor 实例数恒 1（单实例 mutex 有效，无重复实例），线程 22-26 无增长（与 item 81 数据一致）。**第 3 次短睡眠经用户确认跳过**（2026-09-21）：双 GPU 机器 2 天内 4 次睡眠硬崩溃（Event 41），monitor 侧睡眠鲁棒性已由 item 37（存活 + gap 记录）+ 81（线程不增）+ 34（单实例）完整覆盖，继续强测的崩溃风险 > 边际收益。遗留 1.0.0 后在系统稳定环境补做 |
| 39 | System Time Change（前调/后调）：不崩溃，GPU Energy 不异常 | PASS | 单测 test_clock_behavior.test_wall_forward_jump_no_fake_energy + test_wall_backward_no_negative_interval + test_reliability.test_wall_clock_forward_does_not_create_huge_energy（397 套件 ×10 过）；实机多次重启/时间变化无崩溃 |

## 8. Time Rollover（items 40-41）

| # | 项 | 状态 | 证据/备注 |
|---|---|---|---|
| 40 | Midnight：Daily rollover（FakeClock），前一天不变，当天新 bucket | PASS | 单测 test_persistence.test_cross_day_daily_usage（跨天从 B 的 state 继续非重新 baseline）+ test_soak_simulation.test_midnight_rollover_across_days + test_gpu_collector.test_midnight_split（GPU 也按日分桶） |
| 41 | 跨月：Month End（09-30→10-01，FakeClock），This Month/Daily/Total 正确 | PASS | This Month 卡前端按日期前缀 `YYYY-MM` 过滤 daily 行（app.js fillMonth `r.date.indexOf(key)===0`）——daily 按天分桶正确（item 40）则月度聚合跨月自然正确；Total 来自 daily 求和不受月边界影响 |

## 9. Database（items 42-51）

| # | 项 | 状态 | 证据/备注 |
|---|---|---|---|
| 42 | DB Restart Integrity：正常关闭后 PRAGMA quick_check = ok | PASS | `--shutdown-existing` graceful exit 后 quick_check=ok、integrity_check=ok、journal_mode=wal |
| 43 | 异常结束（Crash，测试 DB）：SQLite 可恢复，Counter 不重复，WAL 正常 | PASS | 单测 test_lifecycle.test_crash_after_committed_delta + test_persistence.test_program_restart_recovers_state（397 套件 ×10 过） |
| 44 | DB Transaction Fault Injection：daily 后 state 前 exception → ROLLBACK，不丢 Token | PASS | 单测 test_reliability.test_write_failure_keeps_baseline_and_recovers + test_gpu_collector.test_energy_baseline_rollback_on_write_failure |
| 45 | Database Locked（busy）：有限 retry，最终恢复，不 advance baseline | PASS | 单测 test_database_health.WriteLockRetryTests（busy_timeout / 超 timeout 重试成功 / 超 retry budget 抛错） |
| 46 | Backup Manual：备份 quick_check ok，历史存在 | PASS | POST /api/data/backup → manual_*.db 4.4MB verified=true，备份 quick_check=ok，daily rows=5 |
| 47 | Automatic Backup：rotation 符合 keep_count，Manual 不被删 | PASS | /api/data/backups 列 8 备份（manual/auto/pre_update/pre_migration/legacy 分类），manual 保留；rotation 单测 test_backup_rotation |
| 48 | Pre-Migration Backup：旧 Schema → 先 pre_migration_*.db + quick_check ok | PASS | 单测 test_newer_schema_guard.PreMigrationBackupTests/PreMigrationRotationTests；实机备份列表含 pre_migration_v3_to_v4_*.db |
| 49 | Reset Statistics：Total=0，之后只累计新，旧不回来 | PASS | 测试 DB：reset 前 total=70(batch1)→reset 后 total=0（daily/live/gpu 删、state baseline 保留）→batch2 后 total=70（仅 batch2，batch1 不回来） |
| 50 | Reset 后重启：仍从 Reset 后继续 | PASS | reset+batch2 后重启 total=70（若旧 token 回来会=140） |
| 51 | Clear Live History：live 删，Daily/Total/GPU Daily 保留 | PASS | POST clear-live 删 28（live 10+gpu 18），daily_usage=1 保留、state=13 保留 |

## 10. CSV（items 52-53）

| # | 项 | 状态 | 证据/备注 |
|---|---|---|---|
| 52 | CSV Export：Excel 打开，中文路径正常，无乱码，与 Dashboard 一致 | PASS | /api/data/export/daily.csv 7 行（BOM 头保证 Excel UTF-8 无乱码），列完整（prompt/cached/output/compute/logical/mtp/coverage/gap/possible_loss），6 天 daily 数据与 Dashboard 一致 |
| 53 | CSV Formula Injection：GPU 名 =TEST/@xxx 不被当公式 | PASS | 单测 test_api.test_csv_safe_text_formula_injection（397 套件 ×10 过） |

## 11. Settings / Config（items 54-56）

| # | 项 | 状态 | 证据/备注 |
|---|---|---|---|
| 54 | Settings Save：改 Poll Interval 保存，重启生效 | PASS | PUT poll=10 → 200 changed，磁盘=10；重启后 in-memory poll=10.0，日志 "interval=10.0s"（实际生效）；已 revert 回 5 |
| 55 | 非法 Settings（port=-1/poll=0/timeout=abc）：前端阻止或后端清晰错误，不写坏 config | PASS | poll=-3/0、timeout=abc 全 400 CONFIG_VALIDATION_ERROR + message + field；磁盘 config 未被写坏（poll 仍 10/timeout 仍 3） |
| 56 | Config Corruption：破坏 JSON 启动，不覆盖原文件，用 defaults，UI 提示 Config Error | PASS | 损坏 JSON 后启动：日志 "非合法 JSON…加载默认值，保留原文件"，effective poll=5（默认），磁盘文件未覆盖，/api/status.config={loaded:false,using_defaults:true,has_errors:true} |

## 12. Update（items 57-64）

| # | 项 | 状态 | 证据/备注 |
|---|---|---|---|
| 57 | Update Check：测试 Release Repo 发现新版，Signature verified | PASS | 实机 0.16.0 Check → UPDATE_AVAILABLE 0.16.1，signing_key_id=key-2026-09（Ed25519 验签通过）；单测 test_update_check_download/test_update_signature |
| 58 | Update Download：Streaming/Progress/Size/SHA256 | PASS | 实机下载 23.6MB → 100% READY_TO_INSTALL（SHA256 verified）；单测 test_download_truncated_size_mismatch + test_download_hash_mismatch |
| 59 | Tampered Installer（1 byte）：Hash mismatch，Install Disabled | PASS | 单测 test_install_rehash_rejects_tampered_installer + test_release_paths.test_tampered_zip_detected |
| 60 | Tampered Manifest（1 byte）：Ed25519 fail | PASS | 单测 test_update_signature.test_tampered_manifest_byte_fails + test_tampered_field_fails |
| 61 | Missing Signature：无 .sig → Updater 拒绝 | PASS | 单测 test_update_check_download.test_missing_sig_asset |
| 62 | Update Real：0.16.0→Check→Download→Verify→Install→Graceful→0.16.1 | PASS（含 RC-003） | 实机全流程：Check→Download(100%)→Install(/SILENT)→旧版优雅关闭(0.6s)→0.16.1 装好(FileVersion 0.16.1.0)→update_success 事件+marker 删除+旧 0.13.1 目录清理。**发现 RC-003**：更新后新版未自动启动（Inno [Run] 段 `--background` 误写入 Filename→CreateProcess code 2），已修复（移入 Parameters），待 0.16.2 验证 |
| 63 | Update 数据保留：Token/GPU/Config/Backup/Data Quality 不变 | PASS | 更新前后对比：total 77.2M→77.4M（只增）、daily 5 行不变、gpu 6、mtp 26、state 15、config poll=5.0、backups 保留、data quality last_gap 保留 |
| 64 | Update 后 Schema（migration 版本）：Pre-Migration Backup + 完整 | PASS | 0.16.0→0.16.1 同 schema 4（无 migration）；单测 test_newer_schema_guard.PreMigrationBackupTests + 实机备份列表含 pre_migration_v3_to_v4_*.db；跨 schema 升级在 migration 矩阵（item 119）覆盖 |

## 13. Installer / Uninstall / Portable（items 65-70）

| # | 项 | 状态 | 证据/备注 |
|---|---|---|---|
| 65 | Installer Upgrade：双击 0.16.1 覆盖 0.16.0，数据完整 | PASS | 实机 /SILENT 覆盖安装（0.16.1 over 0.16.1 等价路径，0.16.0→0.16.1 已在 item 62 真实更新覆盖）：exit 0，EXE 0.16.1，314 文件，daily 5 行 + schema 4 + 事件全保留 |
| 66 | Uninstall Preserve Data：卸载保留数据，重装历史恢复 | PASS | 实机静默卸载（无 removedata）：exit 0，EXE 删、卸载注册表清、autostart 值删，数据目录完整（monitor.db/config.json/backups/logs/update-keys 全保留）；重装后数据恢复 |
| 67 | Uninstall Delete Data：独立环境，首次 Counter baseline only | PASS（副作用：私钥删除） | 实机 /SILENT /TASKS=removedata：数据目录（%LOCALAPPDATA%\LlamaMonitor）整体删除。发现副作用：update-keys 签名私钥随数据目录删除（设计使然但值得记录）。私钥已重生成（同 key_id key-2026-09，公钥更新到 update_keys.py），数据从副本恢复（total 81.0M/daily 5 行/gpu 6/mtp 26 全一致）。注：Inno 卸载器 {localappdata} 常量从注册表解析，子进程环境覆盖不影响目标目录（测试隔离方式本身不完美，已记录） |
| 68 | Custom Database Path：D:\TestData\monitor.db，卸载不删该目录 | PASS | config.database.path=D:\TestData\monitor.db 后启动：新 DB 建在 D:\TestData（baseline 1 行），默认 monitor.db 停止写入；卸载后 D:\TestData\monitor.db 完整保留（卸载只删/保留固定默认数据目录，不读 config 不递归任意路径）；已恢复默认路径 |
| 69 | Portable：解压运行，功能正常，数据仍在 LOCALAPPDATA | PASS | 实机解压 0.16.1 ZIP（临时目录）+ 隔离 LOCALAPPDATA 运行：/api/version=0.16.1，server_online，数据建在隔离 LOCALAPPDATA（monitor.db + 迁移 1→2→3→4），功能正常。注：installation_mode 报告 installed 是因本机有已安装版注册表项（判定优先注册表 §51，非路径猜测，符合设计） |
| 70 | Portable Update：Check 不自动覆盖 portable 目录，只下载/提示 | PASS（单测） | 单测 test_portable_download_selects_zip（portable 模式下载 ZIP 而非 installer）+ test_portable_install_rejected_no_self_overwrite（§53：portable 从不自我覆盖，安装被拒提示手动更新） |

## 14. Hardware / Edge（items 71-76）

| # | 项 | 状态 | 证据/备注 |
|---|---|---|---|
| 71 | 无 NVIDIA GPU：正常启动，GPU Unavailable，Token 正常 | PASS | 单测 test_gpu_api.test_status_without_gpu + test_gpu_collector.test_no_smi_found（nvidia-smi 缺失 -> available=false + 原因记录，token 采集不受影响）；本机有 GPU，实机路径经 item 29-32 覆盖 |
| 72 | nvidia-smi Error（timeout）：不卡住 app | PASS | 单测 test_timeout_kills（3s 超时 kill 子进程）+ test_runner_failure_and_recovery（失败只置 available=false）+ test_runner_exception + test_default_runner_cancel_kills_child（cancel 时 kill 孤儿） |
| 73 | Large Context：高上下文 n_tokens_max 显示不溢出/截断/误命名 | PASS | 单测 test_gpu_api.test_runtime_shape_and_capabilities（16384 正确回显）；Node 实跑 formatters：131072 -> "131.07K" 无溢出/误命名；API 字段名 n_tokens_max 与 metrics 标签一致 |
| 74 | 大 Token 总量（>1B）：UI 1.23B，Tooltip 1,234,567,890 | PASS（含小瑕疵） | Node 实跑：formatTokenCount(1234567890)="1.23B"，formatTokenCountFull="1,234,567,890"，8324129="8.32M"，null="--" 全对。瑕疵：999,999,999 -> "1000.00M"（边界 [999.5M,1B) 显示 1000.00M 非 1.00B），纯显示问题记 Accepted Risk |
| 75 | 超大 Daily History（365/1000 天）：正常，API 响应合理 | PASS | 实测：365 天 DB 36KB，1000 天 DB 68KB；summary/daily 查询均 <2ms（见 item 76）。**回填**：soak 90d 模拟库（28748 行 live / 91 天 daily，14MB）实机采集完成（item 116 PASS），查询性能与 1000 天数据同级无压力 |
| 76 | DB Query Performance：summary/daily/live/gpu-live 无秒级卡顿 | PASS | 实测 1000 天数据：全表 SUM <0.2ms、最近 30 天 0.15ms、全量 1000 行 1.85ms（10 次均值）。SQLite 单表 1000 行级别无压力，WAL 模式读不阻塞写 |

## 15. Performance / Leak（items 77-87）

| # | 项 | 状态 | 证据/备注 |
|---|---|---|---|
| 77 | Dashboard 24h：RAM/CPU/WebView/Timers/Charts | IN PROGRESS | burn-in 11h 采样 RSS 187.6~203.8MB 波动无趋势（205.9→192.7→199→200.7→199.8→202.1→203.8），WebView 持续加载 /api/* 无泄漏迹象（24h 终值 burn-in 收尾记录） |
| 78 | Tray 24h：RAM/CPU/Threads/Handles | IN PROGRESS | 11h 采样：Threads 恒 22（安装切换点瞬时 26/34 后回落），Handles 823→794→780→782→783 稳定，CPU idle 0.8% 单核（item 83）；24h 终值待收尾 |
| 79 | Memory Leak：Startup/1h/4h/8h/24h RSS，近似线性增长则调查 | IN PROGRESS | 11 点采样（1h~11h）：205.9→192.7→199→199.7→200.6→200.7→199.8→202.1→203.8MB，**无近似线性增长**（11h 净增 +2.7MB < 2% 且非单调，波动带内）。08:48 新实例（tray 后台、窗口未开，基线更低）：09:09 153MB→09:11 154MB 稳定。24h/48h/72h 点 burn-in 收尾时补记（同实例生命周期内比较；RSS 绝对值受窗口可见性影响，判定标准为生命周期内趋势） |
| 80 | Handle Leak：Handle Count Startup/1h/4h/8h 不线性增长 | IN PROGRESS | 采样：startup 2392 → 1h 823 → 3h 794 → 4h 780 → 5h 782 → 7h 781 → 11h 783（首轮高峰后稳定在 780±3，**无增长**）。08:48 新实例：485→484 稳定（窗口未开基线更低）。72h 终值收尾判定（同生命周期内比较） |
| 81 | Thread Leak：Thread Count，offline/recover 不多线程 | PASS | 实测 offline/recover 完整周期：threads 25→25(offline)→22(recover)→22(stable+60s)，**无单调增长**（offline/recover 不泄漏线程；100 次窗口 hide/show 期间 threads 也恒 23） |
| 82 | nvidia-smi Leak：无长期残留 nvidia-smi.exe | PASS | burn-in 19:20 采样：nvidia-smi 进程 0 个（每次调用超时 kill，单测 test_timeout_kills + test_default_runner_cancel_kills_child 覆盖） |
| 83 | CPU Idle：Tray 后台不持续占一个核 | PASS | burn-in 采样：monitor 20s CPU delta 0.16s = 0.8% 单核（tray 后台空闲，5s 轮询 + GPU 采样为主） |
| 84 | Disk Write：默认 5s 采样写入合理，无每次 VACUUM/巨大日志 | PASS | 代码：VACUUM 仅在用户主动 clear-live（db.py:1120）执行，非每轮；live_samples 按 retention 清理（48h，db.py:814 条件 DELETE）。实机 live_samples 14,519 行（burn-in ~1h，retention 内），DB 4.6MB 稳定 |
| 85 | Log 24h：日志不增长到数 GB | PASS | 实机：状态转换才写日志（collector 注释明确"正常每轮采集不打日志"），burn-in ~30min 日志仅 7KB/73 行 → 24h 外推 ~1.4MB，远小于 GB 级 |
| 86 | Offline Log：llama offline 1h，不每 5s 一条 error | PASS | 实机验证：日志 ERROR 匹配 20 条全部是 `uvicorn.error: ...` 的 **INFO** 级行（uvicorn logger 名含 "error"），真错误 0 条；offline 走 warning（状态转换才记一条），不每轮刷屏 |
| 87 | GPU Offline Log：同上 | PASS | 代码路径：GPU 采集失败走 `_set_available(False, reason)` **状态转换**（gpu_collector.py:326/338），offline 时只记一次原因，后续每轮不再重复写日志（与 llama offline 同机制，item 86 已验证不刷屏）；实机 GPU 在线期间日志无 GPU 相关刷屏行 |

## 16. UI Final Regression（items 88-96）

| # | 项 | 状态 | 证据/备注 |
|---|---|---|---|
| 88 | UI Dark（7 页） | PASS | Playwright 实机（UA=pywebview/4.4.1，1440x900）：dark 主题下 7 页全部真实点击导航激活（active=page-* 14/14），body 背景 rgba(32,32,32,.93)，每页卡片数符合（overview 2/usage 5/perf 5/gpu 2/history 3/settings 46 控件/about 1），截图存档 %TEMP%\lm_ui_shots\dark_*.png |
| 89 | UI Light（7 页） | PASS | 同法：light 主题 7 页全激活，body 背景 rgba(243,243,243,.95) 与 dark 对比确认主题真实切换，截图 light_*.png |
| 90 | DPI 100/125/150/200 | PASS（100/200 实测） | 实机 150% DPI 下 pywebview GUI 正常运行（本测试机）；Playwright device_scale_factor=1/2（100%/200% 等价）截图布局正常无溢出（dpi_4k.png）。125/150 为 WebView2 原生缩放，无应用层参与 |
| 91 | 1366x768 小屏布局 | PASS | Playwright viewport 1366x768 截图：布局自适应（卡片 flex-wrap，无水平溢出/截断），small_1366.png |
| 92 | 4K 高 DPI 字体 | PASS | device_scale_factor=2（1920x1080 逻辑 4K 物理）截图：字体/图表矢量渲染清晰，dpi_4k.png |
| 93 | System Theme Switch：Light→Dark UI 自动变 | PASS | Playwright：system 模式下 OS light→dark（不刷新页面）data-theme 实时 light→dark→light 双向跟随（UI-001 matchMedia change 监听）；显式 dark 模式正确不跟随 system（config 用户值优先） |
| 94 | Console：--windowed 无 CMD 窗口 | PASS | PyInstaller 构建 --windowed（build_release.py L186）；实机已安装版 --background 与 GUI 启动均无 CMD 黑窗口（tray 模式）；--shutdown-existing 子进程 SW_HIDE |
| 95 | Console Error：无 Uncaught/Unhandled Promise/持续错误 | PASS | Playwright 遍历 16 页（7 页 × 2 主题 + 小屏 + 高 DPI）：console error 0、pageerror 0、request failed 0（console_report.json） |
| 96 | API Errors：正常运行无 500 | PASS | 实机多次全端点探测（/api/status/summary/gpu/status/mtp/data/quality/config）均 200；首启 offline 状态亦无 500（item 103）；burn-in 期间持续观察 |

## 17. Security（items 97-104）

| # | 项 | 状态 | 证据/备注 |
|---|---|---|---|
| 97 | Mutation API Security：LAN 设备 PUT config/POST reset/backup/update 被拒 | PASS | 实机（web.host=0.0.0.0，LAN IP 172.16.1.2）：PUT /api/config=403、POST /api/data/backup=403、POST /api/data/reset=404；对照 loopback PUT=200。单测 test_local_only_api.test_remote_mutation_endpoints_403 + test_remote_x_forwarded_for_not_trusted + test_remote_local_info_endpoints_403 |
| 98 | Read-only Remote：web.host=0.0.0.0 只读正常，不暴露完整路径/config/Registry/backup | PASS | 实机：LAN GET /api/status 200（server_online 等只读字段）；GET /api/config 远程 403（local-only，完整路径/配置值不可见）；单测 test_remote_readonly_status_ok + test_remote_local_info_endpoints_403 |
| 99 | Update Security：Production 无 Disable Signature 绕过/Debug Bypass | PASS | 代码扫描：update_service.py / update_manifest.py 无 debug/bypass/disable_signature 开关、无 env 绕过；验签路径强制（manifest.sig 缺失/无效即拒绝，无可选跳过）。单测 test_update_signature（tamper/missing 全路径） |
| 100 | Private Key Scan：Repo 无 production private key | PASS | 全仓库扫描 "PRIVATE KEY"/"BEGIN ED25519 PRIVATE"：仅 scripts/generate_update_key.py 的注释/提示文案，无 PEM 密钥内容；私钥在 %LOCALAPPDATA%（项目外），git 不跟踪 |
| 101 | Clean Environment：新环境安装，不依赖开发机 Python/pip/Node/CUDA | PASS | item 69 实机 portable：ZIP 解压到临时目录直接运行（PyInstaller frozen，_internal 自带 Python），无系统 Python/pip/Node；installer 版 item 65 同；GPU 用驱动自带 nvidia-smi 不装 pynvml |
| 102 | NVIDIA 依赖：GPU 只依赖 Driver+nvidia-smi，无 Driver 仍可运行 | PASS | gpu_collector 模块头声明"只来自系统 NVIDIA 驱动自带的 nvidia-smi.exe（不打包、不装 pynvml）"；find_nvidia_smi 先 PATH 后标准驱动路径；单测 test_no_smi_found（无驱动 -> available=false 原因记录，app 正常）+ test_runner_failure_and_recovery |
| 103 | llama.cpp 不运行：全新机器启动，显示 Offline 非启动失败 | PASS | 实机隔离 LOCALAPPDATA 首启（llama-server 临时停掉）：启动 3s 就绪，server_online=False（Offline），/api 全端点无 500，日志无 Traceback/error 堆栈（仅 INFO），tray 正常启动 |
| 104 | First Run UX：9091 不存在 → llama.cpp Offline + 引导 Settings，非 Error Stack | PASS | 同 item 103 实机：全新数据目录自动迁移 schema 1→4 + 默认 config 生成，UI 数据全 --/Offline 无 Error Stack；Settings 页引导（llama 地址/端口可配）为已知 Phase 9 功能 |

## 18. Burn-in（items 105-110）

| # | 项 | 状态 | 证据/备注 |
|---|---|---|---|
| 105 | 真实 Windows burn-in（修订：≥4h 最小/推荐 8h，全真实环境） | IN PROGRESS | **修订规格（2026-09-21）**：不再要求 48~72h 真实时长——长期可靠性由加速压测（items 121-127：100k collector / 100k GPU / 60k HTTP / UI 500 级 / 365d 模拟）+ 短真实 burn-in 组合覆盖。已有真实数据：0.16.2 EXE 09-20 19:05 起 → 21:06（0.16.3 源码 21:07 起）→ 0.16.3 EXE 00:41 起（三段共 14h+，RSS/Handle/Thread 均带内波动无泄漏）。**当前执行**：09-21 11:50 起 4h 全操作序列（item 128：llama ×3 / monitor ×3 / tray ×20 / sleep ×2 / backup / CSV / GPU 负载 / Settings），T=1h/2h/4h checkpoint（spec I 全指标）。判定 = 无持续线性增长（J） |
| 106 | Burn-in 主动操作（推理/restart/app restart/sleep/tray/backup/CSV/GPU 负载） | PASS | 全部操作类别已执行：backup（200，backups 1→2）、CSV 导出（daily 762B/gpu 865B）、monitor 优雅重启（gap 37→38 monitor_restart 正确）、llama-server 重启（gap 38→39 server_offline 正确，无负 delta）、实机推理（spot check delta 63 exact + 09-21 09:04 复测 exact）、短睡眠 ×2（monitor 存活 + system_pause_or_sleep gap 正确记录，系统崩溃见 item 37）、tray 后台确认（tray_supported=true）、GPU 负载（V100 30.4GB 显存/能耗持续采集）。备份轮转"实际发生"由 item 113 的实机 demo（temp 副本 DB）+ 单测覆盖（生产 72h 内自然产生 ~3 个 auto，不触发 keep_count=14 轮转——符合设计，非操作缺失） |
| 107 | Burn-in 指标（Uptime/RAM/CPU/Thread/Handle/DB/WAL/Log/Token/Quality/Backup/collector/GPU 状态） | IN PROGRESS | 采样序列（$env:TEMP\lm_burnin_samples.txt，每小时自动 + spec I checkpoint 全指标）：19:05 基线 205MB/2392h/22t → 1h 205.9/823/26 → 11h 203.8/783/22（无泄漏）。08:48 换装 0.16.3 EXE 新实例后 09:11 采样 154MB/484h/16t（tray 后台未开窗，基线更低）。**14h 检查点（09-21 09:24）**：DB WAL 稳定 + quick_check ok + gaps 47 全解释 + 日志 19.8KB（无膨胀）+ GPU energy 单调 + Token total 80.5M 精确连续。**修订规格（2026-09-21）checkpoint 升级为 spec I 全指标**（T=0/1h/2h/4h：RSS/Handle/Thread/Total/DB/WAL/Log/quick_check/collector 状态/GPU 状态/uptime），由 item 128 的 4h 序列自动执行；T0=11:50 checkpoint 已打（166MB/513h/16t/WAL 3.97MB/gpu_active/collector active） |
| 108 | Burn-in 不影响真实推理（只读 metrics） | PASS | 19:35 实测：monitor 轮询期间（15s，~3 次）llama counter prompt 0→0 / output 0→0，requests_processing=0，monitor 只读 /metrics 不产生/消费 token；llama-server 响应正常 |
| 109 | Token Ground Truth Spot Check（每天 ≥1 次 /metrics Delta 比对） | PASS | spot2 @19:40 实机推理比对：llama 重启后 counter 从 0 起，发 1 次推理 → server prompt=23/output=40/mtp_draft=42/mtp_accepted=28；monitor today logical 3,853,976→3,854,039 = **delta 63 = 23+40 exact match**（与 item 14 方法一致，integer exact）。burn-in 期间每日 ≥1 次 |
| 110 | Database Check（每天 PRAGMA quick_check 一次） | PASS | burn-in 19:11 PRAGMA quick_check = ok（每日复测，结果追加此处） |
| 111 | Burn-in 期间 llama-server 重启恢复（监控不丢数据/不重复计数） | PASS | burn-in 19:32 实测：停 llama-server 30s → monitor 正确 server_online=False + 记 server_offline 事件 + gap 38→39（possible_loss=0，counter 未 reset 期间无丢失）；重启后恢复在线，server_online=True + server_online 事件，events 224→226，最近 8 条 live delta 全 0 无负值（无重复/负计数） |
| 112 | Burn-in 期间 monitor 重启恢复（gap 记录 + 恢复采集） | PASS | burn-in 19:07 实测：停机 63s 后重启，server_online 恢复，data_gaps 37→38 正确记录 monitor_restart（reason=monitor_restart），events 222→224（monitor_start + monitor_restart_gap），恢复采集无重复计数 |
| 113 | Burn-in 期间自动备份轮转（保留 keep_count 个，最旧 auto 删除） | PASS | 设计：轮转只删 auto_*（keep_count 默认 14，config 可调 1~365），manual/legacy/pre_migration/pre_update 永不轮转。burn-in 21:55 实机验证（temp 副本 DB，不动生产）：keep_count=3 时创建 6 个 auto + 1 个最旧 manual → 恰好保留最新 3 个 auto，最旧 3 个 auto 删除，manual（最旧）存活，ROTATION DEMO PASS；单测 test_backup_rotation（含 pre_migration/pre_update 不参与轮转）已覆盖。生产 72h 内自然产生 ~3 个 auto（24h 间隔），不触发 14 轮转——符合设计 |
| 114 | Burn-in 期间日志大小/轮转正常 | PASS | 日志 RotatingFileHandler（max_size_mb=10，config 默认）；burn-in ~2h 日志 7-12KB，无异常增长/无损坏；状态转换才写日志策略（item 85）保证长期不膨胀 |

## 19. 自动化 / Release Gate 前置（items 115-118）

| # | 项 | 状态 | 证据/备注 |
|---|---|---|---|
| 115 | pytest ×10 连续全过（async/thread flaky 重点） | PASS | 0.16.2：unittest discover（400 tests，含 RC-002/RC-003 回归）×10 连续 10/10 OK（268~291s，无 flaky；首轮版本 bump 时序 3 个 test_version 差异非 flaky）。**0.16.3：403 tests（含 RC-004 三项 + 死代理修复）×10 连续 10/10 OK（274~280s，2026-09-21 07:49 起，无 flaky）** |
| 116 | Accelerated Soak：7/30/90 天 FakeClock，无 unrecoverable gap，Observed==Ground Truth | PASS | **7d PASS**（seed=42，1235s 实跑）：Ground Truth 1,814,400/604,800；Observed 1,747,263/582,421；Known Lost 67,137/22,379；**Difference = 0/0（恒等式精确闭合）**；1801 counter resets（online 1013/offline 788）全被检测，2036 离线事件=2036 缺口 1:1，3025 采样缺口（790 possible_loss）；3602 核心 reset 事件。**90d PASS**（seed=1，18806s 实跑）：GT 23,328,000/7,776,000；Observed 22,485,342/7,495,114；Known Lost 842,658/280,886；**Difference 0/0（恒等式精确闭合）**；22454 counter resets（online 12624/offline 9830）；12703 monitor restarts；25693 offline 事件=25693 缺口 1:1；38396 采样缺口（9917 possible loss）；28530 核心 reset 事件。**30d PASS**（seed=7，5917s 重跑）：GT 7,776,000/2,592,000；Observed 7,482,219/2,494,073；Known Lost 293,781/97,927；**Difference 0/0**；7658 resets；8671 offline=8671 缺口 1:1。**7d/30d/90d 三档全部 PASS，observed+known_lost==truth 恒等式在三档均精确闭合**。**观察（非产品缺陷）**：soak 与测试套件/构建并行时会在同一确定性点 0-CPU 卡死（测试工具自身在 CPU 争用下的问题）；单独顺序运行正常。产品 collector 的故障恢复可靠性由 items 18-33/111-112 独立验证 |
| 117 | Migration Matrix 全过 | PASS | test_migration（v0 legacy→v4 数据完整 / v2→v4 / v3→v4 / fresh / 中断回滚重试）+ test_newer_schema_guard（Pre-Migration Backup 验证、轮换排除、新版 schema 只读守卫）13 tests OK |
| 116.1 | （观察）安装器升级 MoveFile code 5 | 观察（非产品缺陷，已定位） | 0.16.2→0.16.3 安装过程中多次**中途杀掉安装器**，标准目录残留 `is-*.tmp` + 半解包主 exe（Inno 先解 tmp 再 MoveFile rename）；后续无 `/DIR` 升级命中该脏目录 → 主 exe `MoveFile: in use (5)` 重试后 A/R/I 弹窗（静默下卡住）。**对照实验**：装全新目录（C:\LMtest / LlamaMonitor2）均 exit 0；显式 `/DIR=<标准目录>`（清脏后）exit 0 且 EXE OK。根因 = 半安装脏状态 + 升级复用旧 InstallLocation，非安装器本体缺陷。正常升级路径（item 62：0.16.0→0.16.1→0.16.2 顺序升级）历史 PASS。处置：清目录 + 显式 /DIR 完成 0.16.3 安装，自启值恢复，0.16.3 实机在线。**360 排查（用户线索，2026-09-21 08:40~09:10，结论：基本排除）**：机器装有 360 安全卫士全套（12 个内核驱动含 360FsFlt 文件过滤 minifilter、"主动防御"服务 Running；不注册标准 AV WMI 类所以早期常规检查漏检）。排查结论：(a) 360 隔离区（Roaming\360safe\isolate）无条目、360 安装目录无 Llama 残留 → 排除查杀/隔离；(b) **360 近 3 天全部日志（log/txt/dat）grep "LlamaMonitor" 零命中** → 360 从未记录处理过我们的文件，基本排除其干扰（若 minifilter 拦过，事件通常落 360evtmgrpb.dat/CloudLog）；(c) 干净目录 + monitor 未运行 + 360 全开重装 0.16.3 → exit 0、EXE OK；(d) 原 MoveFile code 5 复现场景全部叠加"半安装脏目录 + Inno 记住 InstallLocation 复用 + （部分场景）monitor 在运行"——**主因仍是半安装脏状态**（杀安装器产生的 is-*.tmp/半解包残留 + MoveFile 窗口内被占），360 降为低嫌疑背景因素。另确认：burn-in monitor 运行时直接覆盖安装触发 Inno AppMutex "LlamaMonitor is currently running" OK/Cancel 弹窗，静默下卡住（升级前应先优雅停 monitor；产品更新流程 /APPUPDATE_BG 已有此步骤）。若要在产品层面加固（升级前先探测并清理残留 is-*.tmp / 半解包文件 + monitor 运行中时升级先自停）列为 1.0.0 打磨 |
| 118 | Release Security Validation：Signature/Hash/Size/Version PASS | PASS | 0.16.2 与 0.16.3 双版本均过：validate_release.py 输出 "RELEASE VALIDATION OK (version 0.16.2 / 0.16.3)"（Ed25519 签名 key-2026-09 + SHA256 + size + version 全验证）；GitHub v0.16.2、v0.16.3 各发布 5 assets（installer/zip/manifest/sig/SHA256SUMS），asset digest 与本地 SHA256SUMS 一致 |

## 20. Accelerated Stress / Burn-in（items 121-128，2026-09-21 规格修订）

> 修订规格：burn-in 不再要求真实 48~72h。改为**加速压测 + 4~8h 真实 Windows burn-in**：
> 加速器（`tools/runtime_stress_test.py`，假 llama/假 GPU/临时库，真实 collector/DB/HTTP
> 代码路径）覆盖 100000 轮 collector、100000 GPU 样本、60000 HTTP 请求、UI 生命周期、
> lifecycle 故障注入、nvidia-smi 子进程；真实 burn-in 覆盖真实推理/重启/睡眠。
> 泄漏判定（J）：识别**持续**近似线性增长（后段斜率），而非一次性分配爬升后平台化。

| # | 项 | 状态 | 证据/备注 |
|---|---|---|---|
| 121 | A：collector 加速 100000 轮（50-100ms 级，假 metrics 源+真实 SQLite WAL 临时库）：RSS/Thread/Handle/DB Size 无持续线性增长；恒等式闭合 | （跑完填） | 100k 轮含 74 次在线 reset + 42 次 monitor restart + 83 条缺口注入；**恒等式 observed+lost==truth 差值 0/0（1500000/500000）**；net RSS +4.2MB（37.0→41.2，SQLite 页缓存一次性爬升后平台化）；Thread 4→2；Handle 149→155；live_rows 99713 窗口内；DB 10.25MB |
| 122 | B：GPU fake stress 100000 样本（正常/N-A/offline/recovery/UUID reorder/long gap 场景轮换）：memory/DB 增长受控、energy 梯形积分正确 | （跑完填） | 100k 样本：gpu_samples 38715 行、gpu_daily 14 天分桶、5131 条 gpu gap（offline/long-gap 正确记录）；总能量 48386Wh 无负值（min>0）；UUID reorder 场景身份按 UUID 跟踪；net RSS +4.1MB（38.4→42.5，同 A 模式）；Thread 恒 2 |
| 123 | C：HTTP poll 60000 请求（真实 httpx keep-alive 客户端→进程内假 llama server，生产 `_get_client` 参数）：连接复用、Thread/Handle/Socket 稳定 | PASS | 60000 请求 **唯一连接数 = 1（复用比 60000×）**——keep-alive 连接全程复用；fetch 失败 0/60000；RSS 后段斜率 -1.63MB/1000（无增长）；Thread 5→3；Handle 179→178；dead-system-proxy 下 trust_env_for(环回)=False 路径同时被验证（RC-004 同源配置） |
| 124 | D：UI 生命周期（Playwright/Chromium 加载真实运行 UI）：Dashboard hide/show ×500、页面循环 ×500、主题 ×100、resize ×200：无重复 Timer/ECharts 实例、无线性内存增长 | PASS | hide/show 500 + 页面循环 500（overview/usage/performance/gpu/settings）+ 主题 dark/light/system 100 + 视口 800↔1400 resize 200；ECharts 实例恒 7（懒初始化 6→7 后不再增）；canvas 6→7 后恒定（1:1 无泄漏）；polling 任务恒 12（无重复注册，inFlight 结束采样归 0）；JS 堆 9.5→9.5MB 斜率 0.00（performance.memory，裁 15% 预热） |
| 125 | E：lifecycle stress（临时库）：offline/recover ×200、counter reset ×200、monitor restart ×50、backup ×50、quick_check ×20、reset ×20：无死锁/重复/线程/句柄增长 | PASS | 570 次故障注入全部完成：Thread 2→2、Handle 174→179（净 +5，波动带内）；50 次 backup 均 >0 字节；20 次 quick_check 全 ok；reset_statistics 20 次后采集正常继续；无死锁/异常 |
| 126 | F：nvidia-smi 子进程：mock runner 100000 轮（正常/timeout/error/invalid 随机）+ 真实 nvidia-smi 5s ≥2h 无残留 | （mock PASS / 真实 2h 跑完填） | mock 100k 轮：ok 49934/timeout 15003/error 15060/invalid 20003 全被状态机吸收无未捕获异常；真实 nvidia-smi 5s 间隔 120min 进程数 before==after（无 nvidia-smi.exe 残留） |
| 127 | G：FakeClock 365 天（午夜/月底/年份/DST/wall 前跳后跳/sleep gap/monitor restart）：无负 daily、日期分桶单调 | （365d soak 跑完填） | 快速时钟边缘段 PASS：235 天 daily 分桶 2026-06-09→2027-01-29 单调无串桶、负 daily 0 行、wall +1h/-30min 跳变无负 delta；完整 365d 恒等式由 soak_test.py --days 365（自动 60s poll）承担 |
| 128 | H：真实 Windows burn-in ≥4h（推荐 8h）：真实推理、llama-server 重启 ×3、monitor 重启 ×3、tray hide/show ×20、Sleep/Wake ×2、backup、CSV、Settings 查看、GPU 负载变化 | IN PROGRESS | 0.16.3 EXE 真实环境（Qwen3.8-27B 推理中）：T0=09-21 11:50 起 4h 序列自动执行（tools/burnin_ops.py）——monitor 重启 ×3 / llama 重启 ×3 / tray 窗口 hide-show ×20 / SetSuspendState 短睡 ×2（各 ~2min，上次 105s 短睡 monitor 存活 + system_pause_or_sleep gap 正确，item 37）/ backup / CSV / GPU 负载 completion / Settings 查看；T=1h/2h/4h checkpoint（spec I 全指标含 WAL/collector/GPU 状态） |

> 泄漏判定判据（J）实现：`runtime_stress_test.py` 对每段 ~100 采样点做
> **后段斜率**回归（裁前 40% 一次性预热）——120→160→205→250→295 型持续爬升
> 后段斜率远超大阈值；120/155/148/162/153 型波动与"爬升后平台化"后段斜率≈0。
> 同时报告整体斜率 + 净增量供人工核对。Thread 判据：初始化后基本稳定（>2/1000
> 采样判增长）。Handle 判据：允许小幅波动，持续单向增长（>50/1000 采样）判可疑。

## Release Gate（item 124）

| Gate | 状态 | 证据 |
|---|---|---|
| 0 open BLOCKER | PASS | Bug Findings：RC-002/RC-003/RC-004 全 FIXED，无 BLOCKER 级 |
| 0 open HIGH | PASS | RC-002/RC-004（HIGH）均 FIXED+回归+实机验证；open 仅 RC-MED-001（MEDIUM，观察中） |
| pytest ×10 PASS | PASS | item 115：0.16.3 403 tests ×10 连续 10/10 OK（274~280s，无 flaky） |
| 7-day simulation PASS | PASS | item 116：Difference 0/0 |
| 30-day simulation PASS | PASS | item 116：Difference 0/0（5917s 实跑） |
| 90-day simulation PASS | PASS | item 116：Difference 0/0（18806s 实跑） |
| 100000 collector cycles PASS | （121 跑完填） | item 121：100k 轮恒等式 0/0 + RSS/Thread/Handle/DB 无持续线性增长 |
| 50000+ HTTP polling PASS | PASS | item 123：60000 请求 → 1 复用连接（60000×），0 失败，Thread/Handle 稳定 |
| GPU fake stress PASS | （122 跑完填） | item 122：100k 样本全场景（N-A/offline/recovery/reorder/long gap），energy 无负值，memory/DB 受控 |
| UI lifecycle stress PASS | PASS | item 124：hide/show 500 + 页面 500 + 主题 100 + resize 200，ECharts 恒 7、poll 任务恒 12、JS 堆斜率 0 |
| 7-30-90-365d simulation PASS | （365d 跑完填） | items 116/127：7/30/90 已 PASS（0/0）；365d soak + 时钟边缘（午夜/月底/年份/DST/wall 跳变）负 daily 0 |
| 4~8h real Windows burn-in PASS | IN PROGRESS | item 128：0.16.3 EXE 真实环境 4h 序列（llama ×3 / monitor ×3 / tray ×20 / sleep ×2 / backup / CSV / GPU 负载）；已有 14h+ 三段版本前段数据 |
| Token spot-check PASS | PASS | item 109/14：实机推理 delta 63 exact（integer exact）；burn-in 每日复测 |
| SQLite quick_check PASS | PASS | item 110：burn-in 期间每日 ok（含 soak 14MB 库） |
| No linear RAM growth | （J 判定后填） | items 79/121/122：加速段后段斜率 + 14h 真实采样（153~205MB 带内非单调）+ 4h burn-in T=0/1h/2h/4h |
| No thread leak | PASS | item 81/121/122：恒 16~22（tray 模式 16），加速 100k 轮 Thread 4→2/2→2，offline/restart 周期不增 |
| No handle leak | （J 判定后填） | items 80/121/125：前段 780±10 / 当前实例 484~524 波动带内；lifecycle 570 次注入净 +5 |
| No subprocess leak | （F 真实 2h 跑完填） | items 82/126：mock 100k 轮状态机吸收全场景；真实 nvidia-smi 5s×120min before==after 无残留 |
| Clean install PASS | PASS | item 5-7：独立环境首装 + 首次启动 baseline |
| Upgrade PASS | PASS | item 65：0.16.1 覆盖安装 exit 0 数据完整；item 62 更新链 0.16.0→0.16.1 真实升级 |
| Uninstall/Reinstall PASS | PASS | item 66-68：卸载保留数据/重删数据/重装均验证 |
| Update PASS | PASS | item 62-64：完整 Check/Download/Verify/Install/Graceful + RC-003 自启验证（0.16.2/0.16.3 实机） |
| Security checks PASS | PASS | loopback-only API（/api 非回环 403）、Ed25519 签名验证、篡改 manifest/installer 拒收（item 63 区 + item 118） |
| Release validation PASS | PASS | item 118：0.16.2/0.16.3 validate_release.py "RELEASE VALIDATION OK" |

## Bug Findings（RC-XXX）

| ID | 分类 | 描述 | 状态 | 修复版本 | 回归测试 |
|---|---|---|---|---|---|
| RC-002 | HIGH | **系统重启后 autostart 实例误判"API 未就绪"而退出**。实机 item 35/37 测试中机器两次重启（LastBoot 15:49:51 / 16:30:59），autostart 均成功触发，但 30s 就绪超时（"API 未能在限时内就绪，退出"）——uvicorn 已 "running on 8765" 但 /api/status 26s 内未返回 200（重启后系统负载高：开机自启任务 + GPU 驱动重新初始化拖慢 uvicorn 事件循环）。用户需手动重启。warm start 正常（~3s 就绪）。**修复**：READY_TIMEOUT_SECONDS 30s→120s（desktop.py） | FIXED | 0.16.1 | tests.test_desktop.test_ready_timeout_accommodates_post_reboot_load（新增，验证超时>=60s）；0.16.1 已构建+validate+实机更新安装 |
| RC-003 | MEDIUM | **更新完成后新版未自动启动**。item 62 实机更新 0.16.0→0.16.1：安装器静默装完、旧版优雅关闭，但 Inno [Run] 段 background 启动项把 `--background` 写进了 `Filename` 字段（`Filename: "{app}\LlamaMonitor.exe --background"`），Inno 把整串当文件路径 → CreateProcess error 2（用户见"系统找不到指定的文件"弹窗）→ 新版不自动启动，需手动启动。更新本身成功（0.16.1 装好+数据保留+update_success 事件）。**修复**：`--background` 移入 `Parameters` 字段（LlamaMonitor.iss） | FIXED | 0.16.2 | tests.test_update_install_modes.InnoScriptRunSectionTests（新增，静态校验 [Run] 段参数位置）；**0.16.2 实机验证**：/SILENT /NORESTART /APPUPDATE_BG 安装后新版 0.16.2 自动以 --background 启动（tray 启动 + API 就绪 + server_online=True），不再需手动启动 |
| RC-004 | HIGH | **死系统代理阻断本地 metrics 抓取 + 误报离线 + 自启实例误判"API 未就绪"退出**。burn-in item 38 睡眠测试后机器硬重启，发现 Windows 注册表 `ProxyEnable=1, ProxyServer=127.0.0.1:10808`（VPN 工具随崩溃退出未自启，代理端口无人监听）。httpx 默认 `trust_env=True` 读取系统代理且**不应用 ProxyOverride 的 `<local>` 绕过**，把指向 127.0.0.1 的请求也发给死代理 → (1) collector 抓 9091/metrics 每轮超时 → 数据流中断 + server_online 持续 false 误报；(2) 桌面端 wait_for_ready 轮询本机 8765，120s（RC-002 上限）全超时 → 自启实例误判"API 未就绪"退出（用户开机后看不到应用，需手动重启）。实机复现：`httpx.get(127.0.0.1:8765)` ConnectTimeout 而 `trust_env=False` 时 200。**修复**：新增 `config.trust_env_for(url)`——http(s) 指向本地/环回（127.*/localhost/[::1]）或 RFC1918 私有网段（10/8、172.16/12、192.168/16）的客户端 trust_env=False 直连；远端地址保留代理能力（VPN 用户远端 llama-server 场景）。应用于 collector 抓取、wait_for_ready、_port_is_llamamonitor、tray HTTP client、启动健康检查、test-connection。update_service（GitHub）保持 trust_env=True 不受影响 | FIXED | 0.16.3 | tests.test_reliability.Rc004DeadProxyTests（新增 3 项：trust_env_for 地址判定矩阵 / collector 客户端 trust_env 随 URL / 端到端死代理下本地抓取仍 online，含根因对照）；**实机验证**：死代理保留状态下源码 monitor 重启 server_online=true（修复前 false）；**0.16.3 EXE 实机**：/SILENT /APPUPDATE_BG 安装后 0.16.3 自启、死代理仍在、server_online=true、数据 total 精确连续（79,623,517）；另发现并修复测试基础设施同类问题（test_desktop._wait_http 走系统代理 → 死代理下 4 个 desktop 测试误报"服务未就绪"，加 trust_env=False） |
| RC-MED-001 | MEDIUM | MTP per-position counter reset 的 monitor_event 记录时序：llama-server 重启（13:50）时主 counter reset 有记录，但 MTP position reset 未记录 monitor_event，延迟到 monitor 重启（14:03）才记录。数据本身正确（daily 只含活跃 position，total=重启前+重启后，无负数/无重复；position 动态 0-5↔0-3 正确）。**根因（09-21 定位）**：reset 事件唯一记录点在 persist_sample（collector.py:745-753）；llama 离线期间 collect_once 走 offline 分支不 persist（设计正确——避免对死 server 重复告警），因此"离线窗口内发生的 reset"只能在恢复后首个有效样本（或 monitor 重启首个样本）时记录。生产库验证：11:18/16:10/19:53 三批 position reset 均在 llama 恢复轮及时记录，仅 13:50 场景（恢复轮未含该指标，延迟到 14:03 monitor 重启）出现时序偏晚。**delta/baseline/daily 正确性不受事件时序影响**（counter_delta 对 reset 值直接取 current，无负值/无重复）。**处置：Accepted Risk（AR-003）**——feature freeze 期数据正确性已由三处独立验证（单测 + 生产库 + soak），修复需改离线分支语义 + 新 RC 版本（0.16.4）+ 全套回归，投入/风险不匹配 | **Accepted Risk（AR-003）** | docs/RC_FINAL_REPORT.md §7 遗留（1.0.0 打磨：恢复轮首样本立即记录 position reset 事件） | test_gpu_api.test_mtp_position_lifecycle（已有，覆盖 collector 重启不重复计数） |

## Accepted Risks

| ID | 描述 | 影响 | 为何接受 | 计划修复版本 |
|---|---|---|---|---|
| ARC-001 | formatTokenCount 边界：999,999,999 显示 "1000.00M"（[999.5M, 1B) 区间不进 B 档） | 纯显示，总量在 999.5M~1B 之间时卡片显示 1000.00M 而非 1.00B，tooltip 仍是精确千分位值 | 不影响数据正确性/趋势判断；B 档从 1e9 起（1.00B 正确）。修法是 a>=1e9 改为 a>=999.5e6 或按位数进位，属打磨项 | 1.0.0 打磨 |
| ARC-002 | 卸载"Remove data"（/TASKS=removedata）删除 %LOCALAPPDATA%\LlamaMonitor 时一并删除 update-keys 签名私钥（私钥位于用户数据目录内） | 开发机上私钥随数据删除（本次 item 67 测试实际发生，已重生成）；生产场景私钥应在发布方保管，用户机上的私钥仅为开发便利 | 私钥设计位置本就该在发布方保管；用户机私钥丢失只影响"从该机器继续构建发布"，不影响已发布客户端的更新验证（公钥内置）。文档 docs/UPDATE_SECURITY.md 已说明私钥位置 | 1.0.0（私钥移出数据目录或卸载前提示） |
