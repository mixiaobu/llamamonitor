# LlamaMonitor 0.14.0 RC Burn-in 记录（Phase 14）

> 规格要求：≥48h（目标 72h）burn-in，每日记录资源/数据指标；
> 无异常趋势即通过。本机：Windows 11 桌面机，真实 llama-server
> 127.0.0.1:9091 持续产生流量，5s 轮询。

## 0. 启动信息

| 项目 | 值 |
|---|---|
| 版本 | 0.14.0（RC） |
| 启动时间 | _待填_（0.13.1 → 0.14.0 in-place 升级，升级路径本身是门检查项） |
| 模式 | `--background`（托盘） |
| API | 127.0.0.1:8765 |
| 升级验证 | pre_update 备份 / update_success 事件 / 注册表 DisplayVersion _待填_ |

## 每日记录模板（每天 ~同一时间填一行）

| 日期 | RSS (MB) | handles | threads | CPU%/核 | monitor.db (MB) | WAL (MB) | log (MB) | coverage | gaps | Today tokens | 异常 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| _D0（启动日，24h 后）_ | | | | | | | | | | | |
| _D1_ | | | | | | | | | | | |
| _D2_ | | | | | | | | | | | |

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
