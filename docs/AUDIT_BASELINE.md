# LlamaMonitor 1.0 审计基线（Audit Baseline）

> Phase 14 第一步：**在任何代码修改之前**记录当前状态。
> 采集时间：2026-09-19（本地时间）；采集方式：真实运行环境 + `python -m unittest discover -s tests`。
> 本文件是审计起点参照物；所有修复以 `AUDIT-*` Finding ID 关联（见 AUDIT_REPORT.md）。

## 1. 版本与环境

| 项目 | 值 |
|---|---|
| 应用版本 | 0.13.1（`version.py`） |
| 数据库 schema | v4（`db.CURRENT_SCHEMA_VERSION`；支持从 v1 逐级迁移） |
| Python | 3.13.14（CPython，Windows x64） |
| 操作系统 | Windows 11 |
| 打包工具 | PyInstaller 6.22.3（onedir）+ Inno Setup 6.7.3 |
| 测试框架 | 标准库 `unittest`（`python -m unittest discover -s tests`；**无 pytest 依赖**——规格中的 "pytest" 均指本套件） |

## 2. 测试结果（修改前）

```
Ran 370 tests in 305.863s
OK
```

- **370/370 全部通过**，无跳过、无失败。
- 更新子系统 69 例：test_update_version 8 / test_update_signature 19 /
  test_update_check_download 18 / test_update_install_modes 16 / test_update_api 8。
- 其余 301 例覆盖：配置、采集器、counter 语义、持久化、GPU 采集/解析、API、
  本地-only 边界、备份轮换、迁移、新 schema 保护、数据质量、可靠性（缺口/reset/睡眠）、
  soak 模拟（7/30/90 天）、单实例、Windows 集成、版本、发布路径。
- 已知注意点（非失败）：异步/定时测试依赖 `FakeClock`/事件同步；个别测试对
  Windows 句柄时序敏感——Phase 14 第 113 节要求连续 5 次运行验证稳定性。

## 3. 代码规模

| 项目 | 值 |
|---|---|
| 生产 `.py` 文件 | 57（项目根，排除 tests/） |
| 测试 `.py` 文件 | 36（tests/，含辅助 update_util.py / configutil.py / __init__.py） |
| 合计 `.py` | 65，约 15,384 行 |
| 前端 | `static/index.html`（内嵌 CSS）+ `static/app.js` + 本地 `static/echarts.min.js` |
| 构建脚本 | `scripts/build_release.py`、`scripts/validate_release.py`、`scripts/generate_update_key.py` |
| 工具 | `tools/fake_llama_server.py`（开发/测试用）、`tools/tamper_test.py` 等 |

## 4. 依赖（修改前）

### 4.1 生产依赖（requirements.txt，全部固定版本）

| 包 | 版本 | 用途 |
|---|---|---|
| fastapi | 0.141.1 | API 层 |
| uvicorn | 0.53.0 | ASGI 服务器（后台线程内嵌事件循环） |
| httpx | 0.28.1 | 出站 HTTP（llama /metrics、GitHub 更新） |
| pywebview | 6.2.1 | Dashboard 窗口 |
| pystray | 0.19.5 | 系统托盘 |
| Pillow | 12.3.0 | 托盘图标处理 |
| cryptography | 50.0.1 | Ed25519 更新验签（Phase 13） |

### 4.2 开发/构建依赖（requirements-dev.txt）

| 包 | 版本 | 用途 |
|---|---|---|
| pyinstaller | 6.22.3 | 打包（不进 Installer） |

测试框架为 stdlib unittest——无额外测试依赖。
传递依赖（pip 解析）：pydantic、starlette、anyio、h11、httptools/uvloop（按需）、
httpcore、h2、sniffio、certifi、idna、typing 扩展、pystray→Pillow、cffi（cryptography）。
Phase 14 将逐项核对 THIRD_PARTY_NOTICES 与在线漏洞检查（第 49/50 节）。

## 5. 数据库（schema v4）

10 张表、6 个索引：

| 表 | 内容 | 保留策略 |
|---|---|---|
| app_state | key/value 应用状态（更新 ETag/last_check 等） | 永久 |
| state | 最后一次 Counter baseline（metric_name,value） | 永久（进程重启恢复点） |
| daily_usage | 按天 token/秒数聚合 | 永久 |
| mtp_position_daily | per-position accepted token（按天） | 永久 |
| live_samples | 每轮采样明细（delta/TPS/gauge） | 滚动（`live_retention_hours`，默认 48h） |
| gpu_samples | 每轮 GPU 采样 | 滚动（同上） |
| gpu_daily | GPU 按天聚合（含能量积分） | 永久 |
| monitor_events | 事件流 | 滚动（数量上限） |
| data_gaps | 已知缺口（server_offline / monitor_restart / sleep / invalid） | 永久 |
| backup_history | 备份元数据 | 与备份轮换一致 |

索引：`idx_live_samples_timestamp`、`idx_gpu_samples_timestamp`、
`idx_gpu_samples_uuid_ts`、`idx_monitor_events_ts`、`idx_data_gaps_start`、
`idx_backup_history_ts`。（Phase 14 第 87/88 节将 EXPLAIN QUERY PLAN 复核常用查询。）

## 6. API 端点（修改前，29 个）

**Local-only（`_require_loopback` 依赖，真实 socket peer 判定，不信 proxy 头）：**

| 方法 路径 | 用途 |
|---|---|
| POST /api/config/test-connection | 测试 llama 连接 |
| POST /api/data/backup | 手动备份 |
| POST /api/data/check-database | 完整性检查 |
| POST /api/data/clear-live | 清空 live 明细 |
| POST /api/data/reset-statistics | 重置统计 |
| POST /api/app/open-folder | 打开 data/logs/backups 目录 |
| POST /api/app/exit | 请求退出 |
| GET /api/update/status、POST /api/update/check、/download、/install、/cancel | 更新系统（Phase 13） |

**公开（只读，含 host 暴露面）：**

| 方法 路径 | 用途 |
|---|---|
| GET / | 前端入口 |
| GET /api/status、/api/summary、/api/health | 概览 |
| GET /api/version | 版本 |
| GET /api/config | 当前配置（**Phase 14 第 34 节候选：改 local-only**） |
| GET /api/config/defaults | 默认配置 |
| GET /api/daily、/api/live、/api/mtp、/api/mtp/daily | 数据视图 |
| GET /api/gpu/status、/api/gpu/live、/api/gpu/daily | GPU 视图 |
| GET /api/runtime | capabilities 等 |
| GET /api/data/info、/api/data/quality | 数据质量 |
| GET /api/data/export/daily.csv、/gpu_daily.csv | CSV 导出 |
| GET /api/data/backups | 备份列表 |
| GET /api/app/integration | 应用集成信息 |

CORS：**未启用任何 CORS 中间件**（前端同源由 FastAPI 静态文件提供，第 32 节初步通过）。

## 7. 线程清单（修改前）

| # | 线程名 | 创建位置 | daemon | 停止机制 | join 超时 |
|---|---|---|---|---|---|
| 1 | llamamonitor-uvicorn | desktop.py:122 | 是 | `server.should_exit=True`（触发 lifespan 关闭） | 8s（SHUTDOWN_TIMEOUT_SECONDS），超时 WARNING 继续 |
| 2 | llamamonitor-ui | app_lifecycle.py:99 | 是 | 队列哨兵 `None` | 3s |
| 3 | llamamonitor-tray | tray_manager.py:155 | 是 | `icon.stop()` | 3s |
| 4 | llamamonitor-tray-status | tray_manager.py:157 | 是 | `threading.Event`（30s 慢刷新循环） | 1s |
| 5 | llamamonitor-show-event | windows_integration.py:225 | 是 | stop Event + 250ms 切片 WaitFor | 2s |
| 6 | llamamonitor-shutdown-event | 同上（ShutdownListener 子类） | 是 | 同上 | 2s |
| 7 | pywebview 内部线程 | pywebview（主窗口，主线程 `webview.start()` 阻塞） | — | 窗口 `destroy()` 解除阻塞 | 由 pywebview 管理 |
| 8 | pystray 内部（Win32 消息循环） | pystray | — | `icon.stop()` | 由 pystray 管理 |

所有 join 均带超时且超时只记 WARNING 继续——无无限 join（第 8 节初步通过，
Phase 14 将做启动/退出循环句柄验证）。

## 8. asyncio 任务清单（修改前，uvicorn 线程内事件循环）

| # | 任务 | 创建位置 | 保存位置 | 取消方式 |
|---|---|---|---|---|
| 1 | `_periodic()` llama 采集循环 | server.py:293 | `app.state.collector_task` | lifespan 关闭 `cancel()` + await（捕 CancelledError） |
| 2 | `_gpu_periodic()` GPU 循环 | server.py:306 | 局部 `gpu_task`（lifespan 闭包持有） | 同上 |
| 3 | `_backup_periodic()` 自动备份检查 | server.py:336 | 局部 `backup_task` | 同上 |
| 4 | `_update_auto_check()` 启动一次性更新检查 | server.py:350 | 局部 `update_task` | 同上 |
| 5 | 自动下载守卫（UPDATE_AVAILABLE + auto_download） | update_service.py:509 | `self._auto_download_task`（UpdateService 实例属性） | 下载完成/取消/安装时收敛 |

lifespan 关闭顺序：update → backup → collector → gpu 依次 cancel+await →
`collector.shutdown()`（落未结束缺口+monitor_stop）→ `collector.aclose()`（HTTP）→
`db.checkpoint("PASSIVE")` → `db.close()`。（第 4/5 节：任务全部被持有，无裸 create_task。）

## 9. SQLite 连接清单（修改前，12 处 `sqlite3.connect`）

| 位置 | 用途 | 线程 | 生命周期 |
|---|---|---|---|
| db.py:410 | **主连接**（Database 单例，懒创建） | 首次 `_connect()` 的线程（生产=uvicorn 线程） | `db.close()`（lifespan 关闭） |
| db.py:432 | 只读探测（`file:...?mode=ro`，健康/损坏判定） | 调用者 | 即用即关 |
| db.py:594/599 | 迁移过程辅助连接 | uvicorn 线程（启动） | 即用即关 |
| db.py:1092 | WAL checkpoint 辅助 | 调用者 | 即用即关 |
| backup.py:148/149 | `sqlite3.connect` 源→`backup` API 目标 | 备份工作线程（`asyncio.to_thread` / 桌面线程） | 即用即关 |
| backup.py:192 | 备份 quick_check 验证 | 同上 | 即用即关 |
| update_service.py:817/819/825 | pre-update backup 验证（quick_check 等） | 事件循环线程（install 流程） | 即用即关 |
| update_service.py:912 | `_record_update_event` 短命连接（Phase 13 修复：desktop 线程跨线程写事件） | 桌面主线程 | 即用即关 |

**单写者模型**：常规写全部走 Database 主连接（uvicorn 线程）；短命连接只用于
备份快照（Backup API）、验证与 pending-update 事件补写（WAL 下并发安全）。
（第 9/10/11 节将逐条复核事务边界。）

## 10. 子进程清单

| 位置 | 目标 | 超时 | 收尾 |
|---|---|---|---|
| gpu_collector.py:380 | `asyncio.create_subprocess_exec(nvidia-smi ...)` | 3s，超时 kill+wait | 每轮独立 |
| update_service.py:779 | `subprocess.Popen(Setup.exe, /SILENT ...)` | 不等待（fire-and-forget，安装器接管） | 安装器自管 |

## 11. 已知问题清单（修改前，Phase 13 会话遗留 + 初扫）

1. **K1（待审计确认）** `app_state.runtime` 是共享可变 dict：uvicorn 线程
   `dict.update(...)` 写入，托盘线程/托盘状态 provider 读取（server.py:212-231、
   desktop.py:307-336）。CPL 下读单键通常安全，但非原子快照——规格第 96 节候选。
2. **K2（待审计确认）** server.py `_periodic`/`_gpu_periodic` 外层
   `except Exception: pass`——collect_once/poll_once 内部已捕获一切，外层是双保险，
   但按第 5 节标准属于"可静默"路径，应降级为 debug 日志。
3. **K3（待审计确认）** 托盘 `_tray_status` 每 30s 用 `httpx.get` 打
   `/api/update/status`——每次新建 client（低频本地，影响小；第 24 节候选）。
4. **K4（待审计确认）** `GET /api/config` 目前公开（非 local-only）——第 34 节
   建议改 loopback-only（远程只需 status/summary/metrics 视图）。
5. **K5（历史，已修复）** Phase 13 会话：Inno 6.7.3 `{cmdtail}` 常量不存在→GetCmdTail；
   `PrivilegesRequiredOverridesAllowed=dialog` 阻塞静默安装→移除；
   pending-update 事件跨线程写丢失→短命连接修复（0.13.1 已含）。
6. **K6（历史，已记录）** 0.13.0→0.13.1 升级中 Inno `[Run]` 自动启动曾遇
   CreateProcess code 2（文件短暂窗口），人工点掉对话框后完成；注册表/文件均正确 0.13.1。
7. **K7（设计决定）** 0.13.x 与 1.0.0 数值比较互为降级关系（CHANGELOG 已注明），
   非缺陷。
8. **K8（设计决定）** `/releases/latest` 受 GitHub/代理秒级缓存影响（Phase 13 实测），
   生产场景无影响；快速替换 Release 的测试需要等待+重试。

## 12. 基线结论

- 功能面：Phase 1~13 全部完成；370 测试全绿；安装版 0.13.1 正在后台运行
  （`%LOCALAPPDATA%\Programs\LlamaMonitor`，注册表 DisplayVersion 0.13.1）。
- 发布面：GitHub 唯一 release = v0.13.1（5 资产，Ed25519 签名）；
  仓库 main 两提交（abd3acc 0.13.0 / 03a8781 0.13.1）。
- 数据面：schema v4；daily_usage 4 行真实历史；`update_success` 事件 1 条；
  pre-update / pre-migration 备份齐备。
- 审计起点：11 个线程/任务/连接清单如上；12 处 sqlite3.connect 全部有主/有归宿
  （待第 9/10/11 节逐条复核）；无裸 create_task、无无限 join（待 soak 验证）。
