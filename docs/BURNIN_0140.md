# LlamaMonitor 0.14.0 RC Burn-in 记录（Phase 14）

> 规格要求：≥48h（目标 72h）burn-in，每日记录资源/数据指标；
> 无异常趋势即通过。本机：Windows 11 桌面机，真实 llama-server
> 127.0.0.1:9091 持续产生流量，5s 轮询。

## 0. 启动信息

| 项目 | 值 |
|---|---|
| 版本 | 0.14.0（RC） |
| 启动时间 | **2026-09-19 21:31:37**（日志 `启动: background=True frozen=True`，PID 45116） |
| 模式 | `--background`（托盘） |
| API | 127.0.0.1:8765 |
| 升级方式 | Inno Setup 就地升级 0.13.1 → 0.14.0（`/SILENT`，exit 0；手动安装路径，pre_update 备份属应用内更新流程，Phase 13 已实测） |
| 升级验证 | 注册表 `DisplayVersion=0.14.0` ✅；EXE ProductVersion 0.14.0 ✅；`/api/version` 0.14.0 + schema 4 ✅；`/api/health` application/database 双 healthy ✅；4 天 daily 历史完整保留（total prompt=2,219,756 / output=829,622）✅；升级后 daily 继续累计 ✅；单实例 ShowWindow 唤醒实测 ✅（21:37 第二实例发信号后退出） |
| 数据目录（升级前基线） | monitor.db 1,268 KB / log 100 KB / backups 1,264 KB（WAL 已 checkpoint，干净退出） |
| 已知日志现象 | 21:32:10 asyncio `_call_connection_lost` ConnectionResetError（WinError 10054）——客户端（我的验证请求）中途断开连接的良性 asyncio 日志，非应用故障（9/17 开发模式也有同样记录） |

## 每日记录模板（每天 ~同一时间填一行）

| 日期 | RSS (MB) | handles | threads | CPU%/核 | monitor.db (MB) | WAL (MB) | log (MB) | coverage | gaps | Today tokens | 异常 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| D0 启动日（21:34–21:39 首测，后台→开窗口） | 146.3–147.8 后台 / 150.6–151.7 窗口 | 488–491 后台 / 519–525 窗口 | 16 后台 / 17–20 窗口 | ~1.7% 后台 / ~2.0% 窗口 | 启动时 1.24 | 0（干净退出 checkpoint） | 0.1 | 0.0%* | 5* | prompt 376,930 / output 205,965（截至 21:36） | *今日 monitor 离线 14:07–21:31（0.13.1 已退出、0.14.0 未启动）→ 覆盖率低属预期，非回归 |
| _D1（9/20 ~21:30 采样）_ | | | | | | | | | | | |
| _D2（9/21 ~21:30 采样）_ | | | | | | | | | | | |

采样命令：
```powershell
powershell -File scripts\perf_sample.ps1            # 4 × 30s RSS/handles/threads/CPU
```

## 通过标准（§91/92/144/145）

- RSS/handles/threads **无单调增长趋势**（±5% 波动可接受）；
- CPU 与 0.13.1 before 基线（PERFORMANCE.md §1，~6.9% 单核）相当；
- DB 稳态 ~11-12 MB（STORAGE_ESTIMATE），无异常膨胀；
- 无 unexpected restart / crash（monitor_restart 事件仅允许升级当次）；
- llama 流量下 daily_usage 持续累计、coverage ≥ 99.9%；
- 更新系统自指检查：`/api/update/status` 返回 UP_TO_DATE（GitHub 无新版时）。

## 结论（burn-in 结束填写）

_待填_
