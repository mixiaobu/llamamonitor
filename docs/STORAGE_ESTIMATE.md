# LlamaMonitor 存储占用估算（Phase 14）

> 实测环境：Windows 11，Python 3.13，SQLite（WAL，page_size 默认 4096），
> 5 秒轮询（poll_interval=5s），真实 llama.cpp /metrics 负载。
> 方法：`PRAGMA page_count`/`page_size` 逐表统计 + 索引占用，非理论推算。

## 1. 单行成本（实测，含全部 6 个索引摊入）

| 表 | 单行成本（含索引） | 说明 |
|---|---|---|
| live_samples | **143.4 B/行** | 48h 保留，稳态主导项 |
| gpu_samples | ~120 B/行 | GPU 采集开启时；nvidia-smi 30s 轮询 |
| daily_usage | ~200 B/行（含行） | 1 行/天，可忽略 |
| mtp_position_daily | ~80 B/行 | 每 (date, position) 1 行 |
| monitor_events | ~120 B/行 | 状态转换事件，稀疏 |
| data_gaps | ~150 B/行 | 稀疏 |
| backup_history | ~100 B/行 | 有 1000 行硬上限（AUDIT-DB-002） |

## 2. 稳态占用（5s 轮询，48h live 保留）

| 项目 | 量 |
|---|---|
| live_samples 稳态（48h × 17,280 行/天） | **~10 MB**（含索引） |
| gpu_samples（同保留期） | ~0.6 MB |
| daily_usage / mtp_position_daily（2 年） | < 1 MB |
| monitor_events（365d 保留 + 100,000 行硬上限） | 正常 < 1 MB；极端事件风暴 ≤ ~12 MB |
| data_gaps / backup_history | < 0.5 MB |
| **monitor.db 稳态总量** | **~11-12 MB**（WAL 活跃时 +1-3 MB 临时） |

## 3. 增长模型（长期）

- **live_samples / gpu_samples：不增长**（48h 保留滚动清理，稳态 ~10 MB）；
- **长期增长项**：daily_usage（~0.2 KB/天）、mtp_position_daily、
  monitor_events（~几十 B/小时量级，取决于事件频率）、data_gaps（稀疏）；
- **年度增长 ≈ 3-8 MB/年**（典型负载；重度 MTP + 多 position 取上限）；
- 10 年典型负载 ≈ 30-80 MB——无需任何手动维护。

## 4. 保留策略一览（db.py）

| 表 | 保留 | 机制 |
|---|---|---|
| live_samples / gpu_samples | 48h（可配置） | 每次 apply_sample 同事务清理 |
| monitor_events | 365 天 **且** ≤ 100,000 行（AUDIT-DB-003 行数上限） | 同上 |
| daily_usage / mtp_position_daily | 永久 | — |
| data_gaps | 永久（历史可信度数据） | — |
| backup_history | ≤ 1,000 行（AUDIT-DB-002） | record_backup 同事务清理 |

## 5. 备份目录（%LOCALAPPDATA%\LlamaMonitor\backups）

- automatic 备份：keep_count 轮转（默认 10 份 × ~11 MB ≈ 110 MB）；
- manual / legacy：永久保留（用户资产）；
- pre_migration_* / pre_update_*：特殊恢复点，独立 kind，不参与轮转（AUDIT-DB-005）。
