# LlamaMonitor 架构（线程边界 / async 事件循环 / 数据库访问 / 生命周期 ownership）

> Phase 14 产物。目的：明确**谁创建资源、谁负责关闭、谁依赖谁**，供审计对照。
> 与代码逐行一致（v0.13.1 基线）；任何架构变化必须同步本文档。

## 1. 总览

```
LlamaMonitor.exe（或 python desktop.py）
│
└─ 主线程（Windows 线程 #0，pywebview 消息循环宿主）
   │  desktop.main()：
   │   1. 日志/配置（config.setup_logging / load_config）
   │   2. SingleInstance.acquire()（Named Mutex，最先，失败即唤醒已有实例退出）
   │   3. AppLifecycle（shutdown 状态机，幂等 request_stop）
   │   4. UiCommandDispatcher（线程：UI 命令串行队列）
   │   5. ShowWindowListener / ShutdownListener（线程：Named Event 监听）
   │   6. 端口防御（已有 LlamaMonitor 占端口 -> 打开浏览器+对话框退出）
   │   7. Database（懒连接，此时尚未 _connect）
   │   8. build_collector(cfg, db)（MetricsCollector + GpuCollector）
   │   9. build_app(...) + run_uvicorn_in_thread()（线程：uvicorn 事件循环）
   │  10. wait_for_ready()（轮询 /api/status）
   │  11. check_pending_update() + cleanup_stale()（Phase 13）
   │  12. TrayManager.start()（线程 x2：pystray 消息循环 + 30s 状态刷新）
   │  13. webview.create_window() + webview.start()（主线程阻塞到此）
   │  14. webview.start() 返回 -> _perform_shutdown()（有序关闭，见 §6）
   │
   ├─ 线程 T-uvicorn（llamamonitor-uvicorn，daemon）
   │   └─ uvicorn.Server.run()：独立 asyncio 事件循环
   │       ├─ lifespan 启动：
   │       │   1. db.quick_check()（损坏 -> protective mode 只读）
   │       │   2. maybe_note_restart_gap() + monitor_start 事件
   │       │   3. collector.collect_once()（首采，建 baseline）
   │       │   4. gpu.poll_once()（如启用）
   │       │   5. create_task x4：_periodic（llama 采集）/ _gpu_periodic /
   │       │      _backup_periodic / _update_auto_check
   │       ├─ FastAPI 路由（同步 handler 在线程池 / async handler 在事件循环）
   │       └─ lifespan 关闭（should_exit 触发）：
   │           cancel+await 4 个任务 -> collector.shutdown() -> collector.aclose()
   │           -> db.checkpoint(PASSIVE) -> db.close()
   │
   ├─ 线程 T-ui（llamamonitor-ui，daemon）
   │   └─ UiCommandDispatcher._loop：FIFO 执行 show/hide/exit/open_updates
   │       （Win32 监听线程、tray 回调、API exit 只入队，不直接碰窗口）
   │
   ├─ 线程 T-tray（llamamonitor-tray，daemon）
   │   └─ pystray icon.run()（Win32 消息循环）
   │
   ├─ 线程 T-tray-status（llamamonitor-tray-status，daemon）
   │   └─ 每 30s _apply_status()：读 app_state.runtime + /api/update/status
   │       -> tooltip/菜单变化才重建（MenuItem 不可变）
   │
   ├─ 线程 T-show（llamamonitor-show-event，daemon）
   │   └─ WaitForSingleObject(ShowWindow Event, 250ms) -> dispatcher.request("show")
   │
   └─ 线程 T-shutdown（llamamonitor-shutdown-event，daemon）
       └─ WaitForSingleObject(Shutdown Event, 250ms) -> dispatcher.request("exit")
           （安装器 / --shutdown-existing 的优雅退出通道）
```

## 2. 关键 ownership 规则

### 2.1 数据库连接（单写者）

| 资源 | 创建者 | 使用者（线程） | 关闭者 |
|---|---|---|---|
| `Database._conn`（主连接） | `Database._connect()` 首次调用 | **仅 uvicorn 线程**（lifespan + 路由同步 handler 经线程池复用同一连接——SQLite `check_same_thread=True`，连接绑创建线程；见 §4 风险注记） | `db.close()`（lifespan 关闭最后一步） |
| 备份连接 | `BackupManager.create_backup` | 备份工作线程（`to_thread` 内） | 函数内 `finally` 关闭 |
| 验证/短命连接 | backup.py 192 / update_service 817-912 | 各调用线程 | 各调用处 `finally` 关闭 |

- WAL 模式（`config.database.wal`）：读写可并发；写者=主连接；短命连接只做
  `Backup API` 快照、`quick_check` 验证、pending-update 事件补写。
- **protective mode**：`db.health` ∈ {corrupt, unavailable, incompatible} 时
  collector 停止修改类写（只读展示），绝不删库/重建。

### 2.2 HTTP 客户端

| 资源 | 创建者 | 关闭者 |
|---|---|---|
| collector `_http`（httpx.AsyncClient，keep-alive） | `MetricsCollector._get_client()` 懒创建（事件循环绑定） | `collector.aclose()`（lifespan 关闭） |
| update 下载 client | `UpdateService._client()` 同步 httpx（每操作新建） | 各操作上下文管理 |
| 托盘状态查询 client | `httpx.get` 临时（30s 一次，本地） | 请求结束 |

### 2.3 子进程

- `nvidia-smi`：GPU 循环每轮 `create_subprocess_exec`（3s 超时 kill+wait）；
  与 llama 采集循环**相互独立**（异常不跨传播）。
- 安装器：`update_service.install()` Popen 后 fire-and-forget（安装器
  `--shutdown-existing` 等待本进程释放 Mutex）。

## 3. async 事件循环（唯一）

- 全应用**只有一个 asyncio 事件循环**：uvicorn 线程内。
- 长期任务 4 个（§基线 8 节清单），全部被持有（`app.state` / 实例属性 / 闭包局部），
  lifespan 关闭逐个 `cancel()` + `await`（捕 `CancelledError`）。
- GPU/备份/更新检查循环各自 `while True` + 先睡后做；异常在循环内捕获记日志，
  任务不因单轮异常死亡。
- 同步阻塞工作（备份 I/O）走 `asyncio.to_thread`，不占事件循环。

## 4. 线程安全现状与风险注记（审计输入）

| 共享状态 | 写者 | 读者 | 现状 |
|---|---|---|---|
| `collector.last_snapshot`（dict 引用） | 事件循环 | FastAPI 路由（同事件循环）+ 托盘状态（跨线程读引用） | 引用替换是原子的（GIL）；读到的 dict 内容在写者下一轮才会变——**读的是上一轮快照，可接受**（审计第 96 节复核） |
| `app_state.runtime`（dict，`.update()` 原地改） | 事件循环（每轮 `_refresh_app_state`） | 托盘线程（30s）、`/api/status`（事件循环） | **原地 mutate 非原子快照**——托盘可能读到半更新（如 gpu_available 新、today 旧）。候选修复：整体替换为新 dict（引用原子）。AUDIT-ASYNC 域。 |
| `UpdateService.status`（状态机） | 事件循环（async 方法） | FastAPI（同事件循环） | 同事件循环内一致；Popen 子线程无并发读。 |
| `db` 主连接 | uvicorn 线程 | 同上 | `check_same_thread=True` 保证误用即抛错（fail-fast，无静默损坏）。 |
| `lifecycle`（AppLifecycle） | 任意线程（`request_stop`） | 任意线程 | `threading.Lock` 保护，幂等。 |
| `ui["window"]` 窗口对象 | 主线程创建 | T-ui（show/hide/destroy） | 仅 T-ui 线程调用 pywebview API（Win32 线程约束），主线程 `_schedule_shutdown` 的 `w.destroy()` 在 webview 主循环外调用——pywebview 允许（destroy 线程安全）；审计第 74 节复核。 |
| 托盘 `_status` | T-tray-status | pystray 回调（T-tray 消息循环） | `MenuItem` 重建走 `icon.menu` setter（pystray 内部同步）；读 `_status` 的 checked 回调跨线程读 dict——低频、GIL 下可接受（审计复核）。 |

## 5. 依赖方向（模块图）

```
desktop.py ──> config / db / server / tray_manager / windows_integration / app_lifecycle / update_service
server.py  ──> collector / gpu_collector / backup / db / update_service / app_lifecycle
collector.py ──> metrics_parser / stats / clock / db / httpx
gpu_collector.py ──> db / httpx(无) / asyncio.subprocess
backup.py ──> sqlite3 / db(元数据)
update_service.py ──> update_manifest / update_keys / httpx / db(事件)
db.py ──> sqlite3（无其他业务依赖）
metrics_parser.py / stats.py / clock.py：纯函数/纯状态，无 I/O 依赖
config.py：纯配置（stdlib + 路径）
```

反向依赖：无（`db` 不 import 业务模块；`metrics_parser` 不 import collector）。

## 6. 生命周期时序

### 6.1 启动（顺序即所有权建立顺序）

```
Mutex -> Lifecycle/Dispatcher/Listeners -> DB(对象) -> Collector/GPU(对象)
-> build_app -> uvicorn 线程 -> wait_for_ready(lifespan 完成)
-> pending update 检测 -> Tray -> webview 窗口(阻塞)
```

### 6.2 优雅关闭（`_perform_shutdown`，`_shutdown_steps_done` 保证只执行一次，每步异常不中断后续）

```
1. 托盘 stop()（icon.stop + join 3s/1s）
2. stop_uvicorn：should_exit=True -> lifespan 关闭
   （cancel 4 任务 -> collector.shutdown 落缺口 -> aclose HTTP
    -> checkpoint PASSIVE -> db.close）；join 8s 超时 WARNING
3. 窗口 destroy()（解除 webview.start() 阻塞）
4. UiCommandDispatcher.stop()（哨兵 + join 3s）
5. ShowWindowListener.stop()（join 2s + CloseHandle）
6. ShutdownListener.stop()（同上）
7. SingleInstance.release()（CloseHandle）
8. lifecycle.mark_stopped()
```

退出触发源（全部幂等收敛到 `_schedule_shutdown` -> 上述序列）：
Tray->Exit、Settings/API exit（`dispatcher.request("exit")`）、
Shutdown Event（安装器/--shutdown-existing）、Ctrl+C。

### 6.3 崩溃语义

- Mutex/Event 由 OS 持有：进程崩溃自动释放，无 stale 锁。
- SQLite WAL：崩溃后下次启动 quick_check + 迁移检查；WAL 未 checkpoint 部分
  由 SQLite 自身回放。
- 采集 baseline 在 `state` 表：monitor 重启不丢（monitor_restart 缺口检测）。

## 7. 数据流（每轮 5s）

```
llama-server /metrics --(httpx keep-alive)--> parse_metrics
  -> 有效样本？
     ├─ 否：offline/invalid 快照（开缺口）
     └─ 是：_handle_valid_sample
           -> 缺口结束/睡眠检测（monotonic 阈值）
           -> persist_sample（单事务：state + daily_usage + mtp_position_daily
              + live_samples + 保留清理）
           -> monitor_restart 缺口恢复
  -> last_snapshot 更新 -> _refresh_app_state（托盘/API 复用）

GPU 每轮（独立周期）：nvidia-smi（3s 超时）-> parse -> 能量积分（monotonic
间隔 + 跨零点拆分）-> gpu_samples + gpu_daily（单事务）
```

## 8. 配置流

```
config.json（%LOCALAPPDATA%\LlamaMonitor）
  -> load_config()（defaults + 未知字段警告 + 原子写回）
  -> apply_overrides（--url/--db/--interval CLI 覆盖）
  -> AppConfig（不可变语义的 dataclass 树，运行时不被改）
  写回仅发生在 Settings API 保存（temp+flush+fsync+os.replace）。
```
