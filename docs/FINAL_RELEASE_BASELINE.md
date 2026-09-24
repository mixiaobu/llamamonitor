# LlamaMonitor 1.0.0 — Final Release Baseline

> 生成时间：1.0.0 Final Release 阶段开始（Feature / UI / Schema / API Freeze 生效）。
> 本文件是发布前基线快照：任何核心模块修复后必须对照本基线重新执行受影响的 Release Gate。

## 版本

| 项 | 值 |
|---|---|
| App Version | 0.16.22（发布时改为 **1.0.0**） |
| Schema Version | **4**（`db.py CURRENT_SCHEMA_VERSION`；迁移链 v0→v1→v2→v3→v4） |
| 基线 Git Commit | 9bc526d（main） |

## 构建环境

| 项 | 值 |
|---|---|
| Python | 3.13.14（开发/构建 venv `.venv-rc`；发布构建用全新 clean venv） |
| PyInstaller | 6.22.3（requirements-dev.txt 锁定） |
| Inno Setup | 6.7.3（ISCC: `C:\Program Files (x86)\Inno Setup 6\ISCC.exe`） |
| Windows | Windows 11 专业工作站版（build 10.0.26200） |
| 架构 | Windows x64 |

## 生产依赖（requirements.txt，精确锁定）

| 包 | 版本 |
|---|---|
| fastapi | 0.141.1 |
| uvicorn | 0.53.0 |
| httpx | 0.28.1 |
| pywebview | 6.2.1 |
| pystray | 0.19.5 |
| Pillow | 12.3.0 |
| cryptography | 50.0.1 |

## 构建依赖（requirements-dev.txt）

| 包 | 版本 |
|---|---|
| pyinstaller | 6.22.3 |

测试框架：标准库 unittest（`python -m unittest discover -s tests -p "test_*.py"`），无 pytest 依赖。

## 测试

- 测试数：**418**（unittest，全部绿 @ 9bc526d）
- 重点回归域：async / thread / single-instance / database / update / backup / time-based（FakeClock）

## 数据库表（schema v4，共 10 张）

| 表 | 用途 |
|---|---|
| `state` | 计数器基线 / 累计状态（monitor baseline、counter reset 记录） |
| `app_state` | 应用级键值状态 |
| `daily_usage` | 按天 Token 历史（prompt/cached/output） |
| `live_samples` | 实时采样（~48h 保留） |
| `mtp_position_daily` | MTP 逐位置接受率（按天） |
| `gpu_samples` | GPU 实时采样（~48h 保留） |
| `gpu_daily` | GPU 按天聚合（能耗估算） |
| `monitor_events` | 监控事件（上下线 / reset / 数据质量） |
| `data_gaps` | 数据缺口（含 possible_token_loss） |
| `backup_history` | 备份历史（manual/auto/pre_migration/pre_update 轮转） |

## API 清单（36 条路由）

只读（可远程）：`/`、`/api/status`、`/api/version`、`/api/config`*、`/api/config/defaults`、
`/api/gpu/status`、`/api/gpu/live`、`/api/gpu/daily`、`/api/runtime`、`/api/data/info`、
`/api/data/export/daily.csv`、`/api/data/export/gpu_daily.csv`、`/api/data/backups`、`/api/health`、
`/api/data/quality`、`/api/events`、`/api/app/integration`*、`/api/update/status`*、
`/api/summary`、`/api/daily`、`/api/live`、`/api/mtp`、`/api/mtp/daily`

Mutation（loopback-only，`*` 注：GET /api/config、/api/app/integration、/api/update/status 亦为 loopback-only）：
`PUT /api/config`、`POST /api/config/test-connection`、`POST /api/data/backup`、
`POST /api/data/check-database`、`POST /api/data/clear-live`、`POST /api/data/reset-statistics`、
`PUT /api/app/autostart`、`POST /api/app/open-folder`、`POST /api/app/exit`、
`POST /api/update/check`、`POST /api/update/download`、`POST /api/update/install`、`POST /api/update/cancel`

## Metrics 定义（冻结，全项目统一）

- Compute Tokens = Prompt + Output
- Logical Tokens = Prompt + Cached + Output
- Cache Ratio = Cached / (Prompt + Cached)；分母 0 → None / `--`
- MTP Acceptance = Accepted Draft / Draft
- 指标名：Prompt TPS、Decode TPS、MTP Acceptance、Busy Slots、Requests Processing、Requests Deferred
- `llamacpp:n_tokens_max` 使用中性名称（最大 Token 记录）+ Tooltip 说明来源

## 发布过程中的核心修复（BLOCKER / HIGH，含回归测试）

| ID | 级别 | 模块 | 问题 | 修复 | 回归测试 | 受影响 Gate |
|---|---|---|---|---|---|---|
| REL-1.0.0-001 | HIGH（安全） | `server.py` 只读 API | `web.host=0.0.0.0` 时，局域网只读客户端可经 **未做 loopback 限制**的只读端点读到完整 Windows 路径，泄漏用户名 + `%LOCALAPPDATA%` 目录：`/api/status` → `config.path`；`/api/data/info` → `database_path` + `last_auto_backup.path`。本机显示需要完整路径，但远程不需要。 | 新增 `server._expose_path(request, full)`：本机（127.0.0.1/::1）返回完整路径，远程返回 `Path(full).name`（文件名）；仅套用到上述只读字段。修改类 API 仍由 `_require_loopback` 强制 403，未改动。 | `tests/test_data_management.py::RemotePathLeakTests`（3 个：本机看全路径 / 远程只看 basename / 远程 403 修改类 API）+ 新增 `tests/configutil.py::remote_app` 测试工具 | API（只读形状 + local-only 403）、Security（远程只读无路径泄漏 / 无 bypass） |

## Known Issues / Accepted Risks（发布前登记）

| ID | 级别 | 描述 | 状态 |
|---|---|---|---|
| KNOWN-1.0.0-001 | MEDIUM | `PUT /api/app/autostart` 在 HKCU Run 注册表键被系统组件（Shell/资源管理器/计划任务等）持续占用时，3 次重试（450ms 退避）耗尽后 `PermissionError [WinError 5]` 未被 API 层捕获 → 500。2026-09-24 01:42–01:57 真实复现：约 15 分钟内每次 PUT 均 500；同期同测试进程 `winreg` 写**新**值名成功、覆盖**既有** LlamaMonitor 值间歇被拒；01:57 后同 API 成功（`{"success":true,"stale":false}`）。数据无损（失败时值未被改写；stale 检测与 UI 提示仍正确）。API 文档已声明 500（"注册表被占用（3 次重试后仍失败）"）。 | 接受（瞬时 OS 竞争，非逻辑缺陷）。1.0.x 可优化：区分"键不可写/值不可写"返回 503 + 指数退避。修复需动核心 `windows_integration.py`，复现依赖 OS 锁时序、回归测试不稳定——按冻结规则不纳入 1.0.0。 |

> 已知限制（写入 Release Notes）：llama.cpp 版本间 metrics 差异；离线期间 counter reset 的部分
> 活动不可恢复（possible_token_loss 标记）；GPU 依赖 NVIDIA 驱动/nvidia-smi；GPU 能耗为采样功率
> 估算；仅 Windows x64；无 Authenticode 签名时 SmartScreen 可能警告。
