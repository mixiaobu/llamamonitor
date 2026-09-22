"""
server.py — LlamaMonitor FastAPI 后端（Phase 3 + Phase 7）

- 监听 web.host:web.port（来自统一配置 config.json，默认 127.0.0.1:8765，
  loopback 绑定，仅本机访问，不对外暴露）；
- 使用 lifespan（不使用已废弃的 on_event("startup"/"shutdown")）：
  * 启动时先立即采集一次（程序首次运行即建立 baseline；llama-server 离线时
    按离线快照返回，不阻塞启动），随后启动 Collector 定期采集的 asyncio
    后台任务（先睡后采，每 interval 一轮）；
  * 关闭时优雅取消采集任务（取消会传播到在途 HTTP 请求）并等待其结束，
    最后关闭数据库连接；
- API 是纯读层：数据全部来自采集器建立的 SQLite，不触碰 9091 端口的任何功能；
- 处理器为 async 并在事件循环线程内直接读 DB（与采集器写入同线程，
  避免 SQLite 连接跨线程使用）；单次读取亚毫秒级，不阻塞事件循环；
- Phase 7：/api/config 返回前端可用的非敏感配置（显式选字段）；
  /api/status 增加 config 加载状态（path/loaded/using_defaults/has_errors）。

运行方式（项目根目录，参数来自 config.json，可用 --url/--db/--interval 覆盖）：
    python server.py                    # 默认 %LOCALAPPDATA%\\LlamaMonitor\\monitor.db
    python server.py --db ./data/monitor.db   # 开发模式
    python server.py --url http://127.0.0.1:9091 --interval 5

前端（Phase 4）：
    浏览器打开 http://127.0.0.1:8765/ 即为 Dashboard（static/index.html，
    原生 HTML/CSS/JS + 本地 static/echarts.min.js，无 CDN / 无前端框架）。
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import platform
import re
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse, JSONResponse  # starlette 1.x 起 fastapi 顶层不再再导出

from app_lifecycle import AppIntegrationState
from backup import BackupManager, CHECK_INTERVAL_SECONDS
from collector import MetricsCollector
from clock import default_clock
from gpu_collector import GpuCollector
from windows_integration import FOLDER_TARGETS, is_frozen, open_folder
from config import (
    LoadedConfig,
    app_data_dir,
    apply_overrides,
    atomic_write_json,
    build_metrics_url,
    get_default_config,
    load_config,
    read_raw_config,
    setup_logging,
    trust_env_for,
    validate_updates,
)
from db import CURRENT_SCHEMA_VERSION, Database, local_date
from update_service import UpdateError, UpdateService
from version import APP_NAME, __version__

logger = logging.getLogger("llamamonitor.server")

# 前端静态文件目录（与 server.py 同级的 static/）
_STATIC_DIR = Path(__file__).resolve().parent / "static"


def _backups_dir() -> Path:
    """备份目录：只扫描/写入 %LOCALAPPDATA%\\LlamaMonitor\\backups（由后端确定，不接受任意路径）。"""
    return app_data_dir() / "backups"


def _err(code: str, message: str, field: str | None = None) -> dict:
    """统一错误响应体（不返回 Traceback，Traceback 只写日志）。"""
    error: dict = {"code": code, "message": message}
    if field:
        error["field"] = field
    return {"success": False, "error": error}


def build_collector(cfg, db) -> tuple[MetricsCollector, GpuCollector]:
    """创建 llama.cpp metrics 采集器 + GPU 采集器（server/desktop 两个入口共用）。"""
    return MetricsCollector(cfg, db), GpuCollector(cfg, db)


def _memory_usage_percent(used, total) -> float | None:
    """GPU 显存使用率（后端计算，前端不重复算）；total<=0/缺失 -> None。"""
    if used is None or total is None or total <= 0:
        return None
    return round(used / total * 100.0, 2)


_GPU_LIVE_FIELDS = (
    "utilization_percent",
    "memory_used_mb",
    "memory_total_mb",
    "temperature_c",
    "power_draw_w",
    "fan_percent",
    "sm_clock_mhz",
    "memory_clock_mhz",
)


def _downsample(points: list[dict], max_points: int = 2000) -> list[dict]:
    """
    简单 bucket 聚合降采样（纯 Python，无 numpy/pandas）：

    - 点数 <= max_points：原样返回；
    - 否则按等宽分桶，每桶取各数值字段的非空均值（时间戳取桶内第一个）。
    前端不需要一次画几万个点。
    """
    if len(points) <= max_points:
        return points
    bucket = (len(points) + max_points - 1) // max_points
    out: list[dict] = []
    for start in range(0, len(points), bucket):
        chunk = points[start:start + bucket]
        merged: dict = {"timestamp": chunk[0]["timestamp"]}
        for field in _GPU_LIVE_FIELDS:
            values = [p[field] for p in chunk if p.get(field) is not None]
            merged[field] = round(sum(values) / len(values), 2) if values else None
        out.append(merged)
    return out


def _with_derived(row: dict) -> dict:
    """为 daily 行附加派生字段。

    compute_tokens = prompt + output
    logical_tokens = prompt + cached + output
    mtp_accept_rate = accepted / draft * 100（draft 为 0 时为 None）
    """
    out = dict(row)
    out["compute_tokens"] = out["prompt_tokens"] + out["output_tokens"]
    out["logical_tokens"] = out["prompt_tokens"] + out["cached_tokens"] + out["output_tokens"]
    draft = out.get("draft_tokens", 0) or 0
    out["mtp_accept_rate"] = (out.get("accepted_tokens", 0) or 0) / draft * 100.0 if draft > 0 else None
    return out


def _recent_dates(days: int, now: float | None = None) -> list[str]:
    """最近 days 个自然日（含今天）的 'YYYY-MM-DD' 列表，升序。

    AUDIT-ASYNC-005：now 可注入（与 /api/daily 的 cutoff 用同一个
    collector.clock.now()，FakeClock 测试下两条路径日期窗口不再错位）。
    """
    if now is None:
        now = time.time()
    return [local_date(now - i * 86400) for i in range(days - 1, -1, -1)]


def _csv_safe_text(value) -> str:
    """AUDIT-SEC-003：CSV 公式注入缓解——外部文本（GPU 名称/UUID 来自 nvidia-smi）
    以 = + - @ 开头时前置单引号（Excel/LibreOffice 按文本处理，不解析公式）。
    纯数字/日期字段不需要（csv.QUOTE_MINIMAL 已处理 , " 换行）。"""
    s = "" if value is None else str(value)
    if s[:1] in ("=", "+", "-", "@"):
        return "'" + s
    return s


def _summary_shape(prompt: int, cached: int, output: int) -> dict:
    """summary 的统一字段形状（含派生字段）。"""
    return {
        "prompt_tokens": prompt,
        "cached_tokens": cached,
        "output_tokens": output,
        "compute_tokens": prompt + output,
        "logical_tokens": prompt + cached + output,
    }


def _require_loopback(request: Request) -> None:
    """
    Phase 10 安全加固：高权限本地操作（改配置/清数据/备份/注册表/退出/开文件夹）
    只允许 loopback（127.0.0.1 / ::1）访问——用户把 web.host 改成 0.0.0.0 后，
    局域网仍可读 dashboard，但不能触发修改类 API。

    判断用实际 socket 地址 request.client.host（不信任 X-Forwarded-For：
    本阶段没有反向代理认证机制）。只读 API 不受限。
    """
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="local-only endpoint")


def build_app(
    db: Database,
    collector: MetricsCollector,
    loaded: LoadedConfig | None = None,
    gpu: GpuCollector | None = None,
    app_state: AppIntegrationState | None = None,
    update_service: UpdateService | None = None,
) -> FastAPI:
    """
    创建 FastAPI 应用。db / collector / gpu / loaded / app_state 注入，便于测试指向
    临时数据库、注入固定 metrics 文本 / GPU runner / 配置状态（见 tests/）。

    gpu 为 None（测试）时 GPU 相关 API 返回 available=false，不影响其他功能。
    app_state 为 None（浏览器模式 / 测试）时 /api/app/* 返回降级值，不影响其他功能。
    update_service 为 None（测试/浏览器模式）时创建默认 UpdateService；测试可注入
    带 MockTransport 客户端 / 固定 installation_mode 的实例（Phase 13）。
    """
    if update_service is None:
        update_service = UpdateService(
            db=db,
            get_config=(lambda: loaded.config if loaded is not None else None),
            config_path=(lambda: loaded.path if loaded is not None else None),
            request_exit=(
                (lambda: app_state.request_exit())
                if (app_state is not None and app_state.request_exit is not None)
                else None
            ),
            is_background=(
                (lambda: bool(app_state.background)) if app_state is not None else (lambda: False)
            ),
        )

    def _refresh_app_state() -> None:
        """
        每轮采集后在事件循环线程刷新 app_state.runtime（托盘 / /api/status 复用，
        不额外起线程查库；DB 读取与采集同线程，遵守单连接不变量）。
        """
        if app_state is None:
            return
        try:
            snapshot = collector.last_snapshot
            online = bool(snapshot and snapshot.get("online")) if snapshot else None
            gpu_available = bool(gpu is not None and gpu.config.gpu.enabled and gpu.available)
            gpu_count = len(gpu.detected) if gpu_available else 0
            app_state.runtime.update(
                llama_online=online,
                gpu_available=gpu_available,
                gpu_count=gpu_count,
                today_logical_tokens=db.get_today_logical_tokens(),
            )
        except Exception:
            logger.exception("刷新应用状态失败（不影响采集）")

    # ---- Phase 11：备份管理器（备份 I/O 走 to_thread；元数据在本事件循环线程写） ----
    cfg_backup = loaded.config.backup if loaded is not None else None
    backup_mgr = BackupManager(
        db_path=db.path,
        backups_dir=_backups_dir(),
        keep_count=(cfg_backup.keep_count if cfg_backup else 14),
    )
    auto_backup_enabled = bool(cfg_backup.automatic) if cfg_backup else True
    auto_backup_interval_h = cfg_backup.interval_hours if cfg_backup else 24.0

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # ---- 启动（Phase 11 顺序：DB 健康检查 -> 重启缺口检测 -> 首采 -> 服务） ----
        # 1) quick_check（比 integrity_check 快）：损坏 -> protective mode（只读，
        #    禁修改类写、禁采集写循环），绝不自动删库/重建
        try:
            ok, detail = db.quick_check()
            if ok:
                # Phase 12：_connect 的 schema 版本检查可能已置 incompatible
                # （数据库由更新版本创建）——此时 quick_check 仍通过，不得覆盖
                if db.health == "incompatible":
                    logger.info("数据库 quick_check 通过，但 schema 版本更新（只读 incompatible 模式）")
                else:
                    db.set_health("healthy", None)
                    logger.info("数据库 quick_check 通过（schema v%s）", db.journal_mode)
            else:
                db.set_health("corrupt", detail)
                logger.error("数据库 quick_check 失败（protective mode，只读）: %s", detail)
                db.record_event("database_integrity_error", "error", "database",
                                {"detail": detail}, now=default_clock.now())
        except Exception as exc:
            db.set_health("unavailable", repr(exc))
            logger.error("数据库 quick_check 异常: %r", exc)
        # 2) monitor_start 事件 + monitor_restart 缺口检测（上一进程断档）
        try:
            collector.maybe_note_restart_gap()
            db.record_event("monitor_start", "info", "collector",
                            {"poll_interval_seconds": collector.interval},
                            now=default_clock.now())
        except Exception:
            logger.warning("记录 monitor_start 事件失败", exc_info=True)
        # 3) 启动：立即采集一次 —— 程序首次运行在这一刻建立 baseline；
        #    corrupt/unavailable 时仍采集（供 UI 展示状态）但不落盘（见 collect_once 的
        #    db.health 检查）；保证应用就绪时 /api/status 已有数据
        await collector.collect_once()
        if gpu is not None and gpu.config.gpu.enabled:
            await gpu.poll_once()
        _refresh_app_state()

        async def _periodic() -> None:
            # 定期采集：先睡后采，每 interval 一轮；
            # 异常双保险（collect_once 内部已捕获一切），任务自身不会死
            while True:
                await asyncio.sleep(collector.interval)
                try:
                    await collector.collect_once()
                except Exception:
                    # AUDIT-ASYNC-002：不再静默 pass——collect_once 内部若漏捕
                    # （如 DB 层非 sqlite3 异常），每轮至少留一条 debug 日志，
                    # 避免"数据悄悄不更新"却零日志
                    logger.debug("周期采集异常（内部应已处理，双保险）", exc_info=True)
                _refresh_app_state()

        app.state.collector_task = asyncio.create_task(_periodic())

        gpu_task = None
        if gpu is not None and gpu.config.gpu.enabled:
            async def _gpu_periodic() -> None:
                # GPU 独立循环：周期与 llama.cpp metrics 分别配置（config.gpu）
                while True:
                    await asyncio.sleep(gpu.config.gpu.poll_interval_seconds)
                    try:
                        await gpu.poll_once()
                    except Exception:
                        # AUDIT-ASYNC-002：同 _periodic，双保险留日志不静默
                        logger.debug("GPU 周期轮询异常（内部应已处理，双保险）", exc_info=True)

            gpu_task = asyncio.create_task(_gpu_periodic())

        # 4) 自动备份后台任务：每 CHECK_INTERVAL 秒检查一次是否到期（只 stat 文件，
        #    开销可忽略）；到期才真正备份（to_thread 不阻塞事件循环）
        backup_task = None
        if auto_backup_enabled:
            async def _backup_periodic() -> None:
                # AUDIT-DB-002：连续失败后退避到 1h（原实现每 60s 重试，备份盘长期
                # 不可写时 backup_history 每天 1440 行 + 1440 条 error 事件无界累积）
                consecutive_failures = 0
                while True:
                    sleep_s = CHECK_INTERVAL_SECONDS if consecutive_failures == 0 else 3600
                    await asyncio.sleep(sleep_s)
                    try:
                        if await asyncio.to_thread(backup_mgr.is_due, auto_backup_interval_h):
                            result = await asyncio.to_thread(backup_mgr.create_backup, "automatic")
                            if result.success:
                                if consecutive_failures > 0:
                                    logger.info("自动备份恢复（此前连续失败 %d 次）", consecutive_failures)
                                consecutive_failures = 0
                                db.record_backup("automatic", result.path, result.size,
                                                 result.verified, success=True,
                                                 now=default_clock.now())
                                db.record_event("backup_created", "info", "backup",
                                                {"path": result.path, "type": "automatic",
                                                 "rotated": result.rotated},
                                                now=default_clock.now())
                            else:
                                consecutive_failures += 1
                                db.record_backup("automatic", result.path, None, False,
                                                 success=False, now=default_clock.now())
                                db.record_event("backup_created", "error", "backup",
                                                {"path": result.path, "type": "automatic",
                                                 "error": result.error},
                                                now=default_clock.now())
                    except Exception:
                        consecutive_failures += 1
                        logger.warning("自动备份任务异常（不影响采集）", exc_info=True)

            backup_task = asyncio.create_task(_backup_periodic())

        # 5) Phase 13：启动后一次性自动更新检查（§69）——只在 check_enabled 且距上次
        #    检查 >= check_interval_hours 时访问 GitHub；网络错误绝不影响监控。
        async def _update_auto_check() -> None:
            try:
                await asyncio.sleep(5.0)  # 等应用就绪（DB/采集/健康检查完成）
                if update_service.should_auto_check():
                    await update_service.check(manual=False)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("自动更新检查失败（不影响监控）", exc_info=True)

        update_task = asyncio.create_task(_update_auto_check())

        yield
        # ---- 关闭：优雅停止 ----
        if update_task is not None:
            update_task.cancel()
            try:
                await update_task
            except asyncio.CancelledError:
                pass
        if backup_task is not None:
            backup_task.cancel()
            try:
                await backup_task
            except asyncio.CancelledError:
                pass
        app.state.collector_task.cancel()
        try:
            await app.state.collector_task
        except asyncio.CancelledError:
            pass
        if gpu_task is not None:
            gpu_task.cancel()
            try:
                await gpu_task
            except asyncio.CancelledError:
                pass
        collector.shutdown()  # Phase 11：落未结束缺口 + monitor_stop 事件
        await collector.aclose()  # 关闭跨轮复用的 HTTP 客户端（幂等）
        db.checkpoint("PASSIVE")  # Phase 11：graceful shutdown 前 WAL 落盘（不用 TRUNCATE 高频）
        db.close()

    # AUDIT-SEC-005：production 关闭 /docs、/openapi.json、/redoc（完整 API 清单
    # 公开只是信息面暴露；写操作本就 loopback-only，无权限升级，但没必要留着）
    app = FastAPI(title="LlamaMonitor", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    # 前端静态文件（/static/echarts.min.js、/static/index.html 等）
    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        """Dashboard 首页（static/index.html）。"""
        index_file = _STATIC_DIR / "index.html"
        if not index_file.is_file():
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="static/index.html 不存在")
        return FileResponse(index_file, media_type="text/html")

    @app.get("/api/status")
    async def api_status() -> dict:
        """
        当前状态：服务器在线情况 + 最新 gauge + 最近一轮的 TPS / MTP + 配置状态。

        - server_online: true/false；启动后尚未完成首次采集时为 null；
        - 离线时数值字段均为 null（last_update 仍为最近一次检查时刻）；
        - last_update: Unix 秒，最近一次采集完成时刻；
        - config: {path, loaded, using_defaults, has_errors}（配置加载状态）。
        """
        snapshot = collector.last_snapshot
        if snapshot is None:
            result = {
                "server_online": None,
                "llama_server_url": collector.metrics_url,
                "requests_processing": None,
                "requests_deferred": None,
                "context_max": None,
                "prompt_tps": None,
                "decode_tps": None,
                "mtp_accept_rate": None,
                "last_update": None,
            }
        elif not snapshot["online"]:
            result = {
                "server_online": False,
                "llama_server_url": collector.metrics_url,
                "requests_processing": None,
                "requests_deferred": None,
                "context_max": None,
                "prompt_tps": None,
                "decode_tps": None,
                "mtp_accept_rate": None,
                "last_update": collector.last_update,
            }
        else:
            latest = db.get_latest_live_sample()
            result = {
                "server_online": True,
                "llama_server_url": collector.metrics_url,
                "requests_processing": snapshot["requests_processing"],
                "requests_deferred": snapshot["requests_deferred"],
                "context_max": snapshot["context_max"],
                "prompt_tps": latest["prompt_tps"] if latest else None,
                "decode_tps": latest["decode_tps"] if latest else None,
                "mtp_accept_rate": latest["mtp_accept_rate"] if latest else None,
                "last_update": collector.last_update,
            }
        if loaded is not None:
            result["config"] = {
                "path": str(loaded.path),
                "loaded": loaded.loaded,
                "using_defaults": loaded.using_defaults,
                "has_errors": loaded.has_errors,
            }
        # Phase 12：应用版本（与 /api/version、About 同一来源 version.py）
        result["version"] = __version__
        # Phase 10：应用级状态（不暴露 Mutex handle 等 Windows 内部句柄）
        if app_state is not None:
            result["application"] = {
                "background": app_state.background,
                "tray_available": app_state.tray_available,
                "single_instance": app_state.single_instance,
                "uptime_seconds": int(time.monotonic() - app_state.start_monotonic),
            }
        return result

    @app.get("/api/version")
    async def api_version() -> dict:
        """
        应用版本信息（Phase 12，唯一来源 version.py）：
        {name, version, app_version(=version), schema_version(当前数据库实际版本)}
        前端 About 区域从这里取版本与 schema 版本——不要硬编码。
        """
        try:
            schema_version = db.get_schema_version()
        except Exception:
            schema_version = None
        return {
            "name": APP_NAME,
            "version": __version__,
            "app_version": __version__,
            "schema_version": schema_version,
        }

    @app.get("/api/config", dependencies=[Depends(_require_loopback)])
    async def api_config() -> dict:
        """
        当前生效配置（Settings 页面初始化用）。

        显式选择已知字段（经 config.py 校验后的有效值），不直接返回原始文件
        （未知字段不暴露）；paths 为后端确定的实际路径。
        前端不得硬编码默认值——默认值只由 config.py 定义。

        AUDIT-SEC-001：loopback-only——响应含 4 个完整本地路径（C:\\Users\\<用户>...）
        与 llama_server.url；远程只读客户端只需要 status/summary/metrics 视图，
        不该拿到本地路径信息（web.host 改 0.0.0.0 时的暴露面）。
        """
        if loaded is None:
            raise HTTPException(status_code=404, detail="config not loaded")
        cfg = loaded.config
        return {
            "llama_server": {
                "url": cfg.llama_server.url,
                "metrics_path": cfg.llama_server.metrics_path,
                "timeout_seconds": cfg.llama_server.timeout_seconds,
                "metrics_url": cfg.metrics_url,
            },
            "collector": {
                "poll_interval_seconds": cfg.collector.poll_interval_seconds,
                "live_retention_hours": cfg.collector.live_retention_hours,
            },
            "gpu": {
                "enabled": cfg.gpu.enabled,
                "poll_interval_seconds": cfg.gpu.poll_interval_seconds,
                "history_retention_hours": cfg.gpu.history_retention_hours,
                "device_uuids": cfg.gpu.device_uuids,
            },
            "web": {
                "host": cfg.web.host,
                "port": cfg.web.port,
            },
            "database": {
                "path": cfg.database.path,
                "wal": cfg.database.wal,
            },
            "ui": {
                "refresh_interval_seconds": cfg.ui.refresh_interval_seconds,
                "daily_default_days": cfg.ui.daily_default_days,
                "theme": cfg.ui.theme,
            },
            "logging": {
                "level": cfg.logging.level,
                "max_size_mb": cfg.logging.max_size_mb,
                "backup_count": cfg.logging.backup_count,
            },
            "backup": {
                "automatic": cfg.backup.automatic,
                "interval_hours": cfg.backup.interval_hours,
                "keep_count": cfg.backup.keep_count,
            },
            "updates": {
                "check_enabled": cfg.updates.check_enabled,
                "check_interval_hours": cfg.updates.check_interval_hours,
                "auto_download": cfg.updates.auto_download,
            },
            "paths": {
                "config": str(loaded.path),
                "database": str(cfg.database_path),
                "log_directory": str(cfg.log_directory),
                "backups": str(_backups_dir()),
            },
        }

    @app.get("/api/config/defaults")
    async def api_config_defaults() -> dict:
        """完整默认配置（Reset to Defaults 用；默认值只由 config.py 定义）。"""
        return get_default_config()

    @app.put("/api/config", dependencies=[Depends(_require_loopback)])
    async def api_put_config(payload: dict) -> Response:
        """
        保存配置（Settings 页面）：

        - 只更新已知字段（请求中的未知字段被忽略，不写入文件）；
        - 逐字段类型 + 范围校验（复用 config.py 的校验器）；
        - 保留原文件中的未知字段与用户未修改的字段（在原始 dict 上合并）；
        - 原子写入：临时文件 + fsync + os.replace（config.py 实现）。
        保存不会热重载：除 ui.theme（纯前端显示）外都需要重启生效。
        """
        if loaded is None:
            raise HTTPException(status_code=404, detail="config not loaded")
        if not isinstance(payload, dict):
            return JSONResponse(status_code=400, content=_err("CONFIG_VALIDATION_ERROR", "请求体必须是 JSON 对象"))
        existing = read_raw_config(loaded.path)
        candidate, changed, errors = validate_updates(existing, payload)
        if errors:
            field = None
            m = re.search(r"Invalid config value ([\w.]+)=", errors[0])
            if m:
                field = m.group(1)
            logger.warning("配置更新被拒绝: %s", errors[0])
            return JSONResponse(status_code=400, content=_err("CONFIG_VALIDATION_ERROR", errors[0], field))
        atomic_write_json(loaded.path, candidate)
        # 日志不记录配置内容（避免将来敏感配置泄漏），只记录变更字段
        logger.info("Config saved: %d changed field(s): %s", len(changed), ", ".join(changed) if changed else "-")
        restart_required = any(key != "ui.theme" for key in changed)
        return JSONResponse(
            status_code=200,
            content={"success": True, "restart_required": restart_required, "changed": changed},
        )

    @app.post("/api/config/test-connection", dependencies=[Depends(_require_loopback)])
    async def api_test_connection(payload: dict) -> dict:
        """
        测试 llama-server metrics 端点：只 GET <url><metrics_path>，
        绝不触碰任何控制接口。由后端发起请求（避免前端 CORS 问题）。
        """
        if not isinstance(payload, dict):
            return {"success": False, "error": "请求体必须是 JSON 对象"}
        url = payload.get("url")
        path = payload.get("metrics_path", "/metrics")
        timeout = payload.get("timeout_seconds", 3)
        # 与 config.py 相同的校验口径
        if not (isinstance(url, str) and (url.startswith("http://") or url.startswith("https://"))):
            return {"success": False, "error": "无效的 URL"}
        if not (isinstance(path, str) and path.startswith("/")):
            return {"success": False, "error": "无效的 metrics_path"}
        if not (isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and 0.5 <= timeout <= 60):
            return {"success": False, "error": "无效的 timeout_seconds"}
        metrics_url = build_metrics_url(url, path)
        t0 = time.perf_counter()
        try:
            # RC-004：本地/内网测试目标不走系统代理（死代理会让本机端点测试假超时）
            async with httpx.AsyncClient(timeout=float(timeout), follow_redirects=True,
                                         trust_env=trust_env_for(metrics_url)) as client:
                r = await client.get(metrics_url)
            latency_ms = (time.perf_counter() - t0) * 1000
            if r.status_code == 200:
                lines = (line for line in r.text.splitlines() if line)
                detected = any(
                    line.startswith("llamacpp:") or line.startswith("# TYPE") for line in lines
                )
                return {
                    "success": True,
                    "latency_ms": round(latency_ms, 1),
                    "http_status": 200,
                    "metrics_detected": bool(detected),
                }
            logger.warning("Test connection failed: HTTP %s from %s", r.status_code, metrics_url)
            return {"success": False, "http_status": r.status_code, "error": f"HTTP {r.status_code}"}
        except httpx.ConnectError:
            logger.warning("Test connection failed: connection refused: %s", metrics_url)
            return {"success": False, "error": "Connection refused"}
        except httpx.TimeoutException:
            logger.warning("Test connection failed: timeout after %ss: %s", timeout, metrics_url)
            return {"success": False, "error": f"Timeout after {timeout}s"}
        except Exception as exc:
            logger.warning("Test connection failed: %r (%s)", exc, metrics_url)
            return {"success": False, "error": str(exc) or exc.__class__.__name__}

    # ---------- GPU（Phase 9） ----------

    def _gpu_status_payload() -> dict:
        """GPU 状态响应（/api/gpu/status）；gpu 未注入 / 未启用 / 不可用都给出明确原因。"""
        if gpu is None:
            return {
                "available": False,
                "provider": "nvidia-smi",
                "reason": "GPU 采集器未配置",
                "last_update": None,
                "detected": [],
                "gpus": [],
            }
        if not gpu.config.gpu.enabled:
            return {
                "available": False,
                "provider": "nvidia-smi",
                "reason": "gpu monitoring disabled",
                "last_update": gpu.last_update,
                "detected": gpu.detected,
                "gpus": [],
            }
        gpus = []
        if gpu.available:
            for row in db.get_gpu_latest():
                gpus.append({
                    "index": row["gpu_index"],
                    "uuid": row["gpu_uuid"],
                    "name": row["gpu_name"],
                    "utilization_percent": row["utilization_percent"],
                    "memory_used_mb": row["memory_used_mb"],
                    "memory_total_mb": row["memory_total_mb"],
                    "memory_usage_percent": _memory_usage_percent(
                        row["memory_used_mb"], row["memory_total_mb"]
                    ),
                    "temperature_c": row["temperature_c"],
                    "power_draw_w": row["power_draw_w"],
                    "fan_percent": row["fan_percent"],
                    "sm_clock_mhz": row["sm_clock_mhz"],
                    "memory_clock_mhz": row["memory_clock_mhz"],
                    "pcie_generation": row["pcie_generation"],
                    "pcie_width": row["pcie_width"],
                })
        return {
            "available": gpu.available,
            "provider": "nvidia-smi",
            "reason": None if gpu.available else gpu.unavailable_reason,
            "last_update": gpu.last_update,
            "detected": gpu.detected,
            "gpus": gpus,
        }

    @app.get("/api/gpu/status")
    async def api_gpu_status() -> dict:
        """
        GPU 当前状态（每张 GPU 一条，按 UUID 去重取最新采样）。

        - available=false 时 reason 说明原因（nvidia-smi 不存在/调用失败/被禁用）；
          前端显示 "GPU Monitoring Unavailable"，历史曲线不受影响；
        - detected：nvidia-smi 报告过的全部 GPU（Settings 页面勾选用）；
        - 某张 GPU 从系统中消失：不再出现在 gpus 中，但 gpu_samples 历史保留。
        """
        return _gpu_status_payload()

    @app.get("/api/gpu/live")
    async def api_gpu_live(minutes: int = Query(60, ge=1, le=2880)) -> dict:
        """
        最近 N 分钟（1~2880）的 GPU 历史，按 UUID 分组。

        每个 GPU 最多返回 2000 个点（超出时后端 bucket 降采样，纯 Python）。
        字段：utilization_percent / memory_used_mb / memory_total_mb /
        temperature_c / power_draw_w / fan_percent / sm_clock_mhz / memory_clock_mhz。
        """
        since = time.time() - minutes * 60
        rows = db.get_gpu_samples_since(since)
        per_gpu: dict[str, list[dict]] = {}
        for row in rows:
            per_gpu.setdefault(row["gpu_uuid"], []).append(row)
        gpus = []
        for uuid, points in per_gpu.items():
            clean = [
                {"timestamp": p["timestamp"], **{f: p[f] for f in _GPU_LIVE_FIELDS}}
                for p in points
            ]
            down = _downsample(clean)
            # 显存使用率在（可能的）降采样之后计算，保证每点都有
            for p in down:
                p["memory_usage_percent"] = _memory_usage_percent(
                    p["memory_used_mb"], p["memory_total_mb"]
                )
            gpus.append({
                "uuid": uuid,
                "name": points[-1]["gpu_name"],
                "index": points[-1]["gpu_index"],
                "points": down,
            })
        return {"gpus": gpus}

    @app.get("/api/gpu/daily")
    async def api_gpu_daily(days: int = Query(30, ge=1, le=365)) -> dict:
        """
        GPU 每日累计（最近 N 个自然日，按 UUID 分组，日期升序）。

        平均值由后端计算：avg = sum / count（count=0 -> null）；
        energy_wh 为采样梯形积分估算（UI 需注明 "estimated"）。
        """
        rows = db.get_gpu_daily(days)
        per_gpu: dict[str, dict] = {}
        for r in rows:
            entry = per_gpu.setdefault(r["gpu_uuid"], {"uuid": r["gpu_uuid"], "name": r["gpu_name"], "days": []})
            entry["name"] = entry["name"] or r["gpu_name"]
            entry["days"].append({
                "date": r["date"],
                "sample_count": r["sample_count"],
                "avg_utilization": round(r["utilization_sum"] / r["utilization_count"], 2) if r["utilization_count"] else None,
                "max_utilization": r["utilization_max"],
                "avg_memory_used_mb": round(r["memory_used_sum_mb"] / r["memory_used_count"], 1) if r["memory_used_count"] else None,
                "max_memory_used_mb": r["memory_used_max_mb"],
                "avg_temperature": round(r["temperature_sum"] / r["temperature_count"], 1) if r["temperature_count"] else None,
                "max_temperature": r["temperature_max"],
                "avg_power_w": round(r["power_sum_w"] / r["power_count"], 1) if r["power_count"] else None,
                "max_power_w": r["power_max_w"],
                "energy_wh": round(r["energy_wh"], 3),
            })
        return {"gpus": list(per_gpu.values())}

    # ---------- Server Runtime（Phase 9） ----------

    @app.get("/api/runtime")
    async def api_runtime() -> dict:
        """
        llama-server 运行时状态（最新一轮 gauge 值，缺失指标为 null）。

        capabilities：服务器当前提供了哪些功能（Metric Capability Detection；
        只存布尔，不暴露原始 Prometheus 文本）；gpu = GPU 监控当前是否可用。
        """
        snap = collector.last_snapshot
        caps = dict(collector.capabilities)
        caps["gpu"] = bool(gpu is not None and gpu.available)
        return {
            "server_online": snap["online"] if snap is not None else None,
            "requests_processing": (snap or {}).get("requests_processing"),
            "requests_deferred": (snap or {}).get("requests_deferred"),
            "busy_slots": (snap or {}).get("n_busy_slots_per_decode"),
            "n_decode_total": (snap or {}).get("n_decode_total"),
            "n_tokens_max": (snap or {}).get("n_tokens_max"),
            "kv_cache_usage_ratio": (snap or {}).get("kv_cache_usage_ratio"),
            "capabilities": caps,
        }

    # ---------- 数据管理（Phase 8） ----------

    @app.get("/api/data/info")
    async def api_data_info() -> dict:
        """
        存储信息：数据库路径/大小、WAL 大小、记录日期范围、行数、备份数/总大小、
        数据库健康（Phase 11：health / journal_mode / 最近成功自动备份）。

        数据库文件尚不存在（尚未有成功采集）时返回 0 / null，不报 500。
        """
        p = Path(db.path)
        backups = backup_mgr.list_backups()
        backup_count = len(backups)
        backup_total_size = sum(b["size"] for b in backups)
        wal_p = Path(str(p) + "-wal")
        wal_size = wal_p.stat().st_size if wal_p.is_file() else 0
        last_auto = db.get_last_backup("automatic") if p.is_file() else None
        base = {
            "database_path": str(db.path),
            "database_size_bytes": p.stat().st_size if p.is_file() else 0,
            "wal_size_bytes": wal_size,
            "database_health": db.health,
            "database_health_detail": db.health_detail,
            "journal_mode": db.journal_mode,
            "first_recorded_date": None,
            "last_recorded_date": None,
            "recorded_days": 0,
            "daily_rows": 0,
            "live_samples": 0,
            "gpu_samples": 0,
            "backup_count": backup_count,
            "backup_total_size_bytes": backup_total_size,
            "last_auto_backup": (
                {"path": last_auto["path"], "timestamp": last_auto["timestamp"]}
                if last_auto else None
            ),
        }
        if not p.is_file():
            return base
        info = db.get_data_info()
        base.update({
            "first_recorded_date": info["first_recorded_date"],
            "last_recorded_date": info["last_recorded_date"],
            "recorded_days": info["daily_rows"],
            "daily_rows": info["daily_rows"],
            "live_samples": info["live_samples"],
            "gpu_samples": db.get_gpu_sample_count(),
        })
        return base

    @app.get("/api/data/export/daily.csv")
    async def api_export_daily_csv() -> Response:
        """
        导出 daily_usage 为 CSV：UTF-8 with BOM（Windows Excel 直接打开不乱码），
        数字为原始整数（不做 1.2M 缩写），日期 YYYY-MM-DD。
        """
        rows = db.get_daily_usage() if Path(db.path).is_file() else []
        # AUDIT-DB-003：同 /api/daily——live 样本一次取全按日分组（避免逐日全量扫）
        try:
            all_live = db.get_live_samples(hours=None)
        except Exception:
            all_live = []
        live_by_date: dict[str, list] = {}
        for s in all_live:
            live_by_date.setdefault(local_date(s["timestamp"]), []).append(s)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "date", "prompt_tokens", "cached_tokens", "output_tokens",
            "compute_tokens", "logical_tokens", "draft_tokens", "accepted_tokens",
            "mtp_accept_rate", "prompt_seconds", "predicted_seconds",
            # Phase 11：数据质量字段
            "monitoring_coverage_percent", "gap_count", "possible_token_loss",
        ])
        for r in rows:
            prompt = r.get("prompt_tokens") or 0
            cached = r.get("cached_tokens") or 0
            output = r.get("output_tokens") or 0
            draft = r.get("draft_tokens") or 0
            accepted = r.get("accepted_tokens") or 0
            rate = round(accepted / draft * 100.0, 2) if draft > 0 else ""
            q = _day_quality(r.get("date"), live_by_date.get(r.get("date"), [])) if r.get("date") else {
                "monitoring_coverage_percent": None, "gap_count": 0,
                "possible_token_loss": False,
            }
            writer.writerow([
                r.get("date"), prompt, cached, output,
                prompt + output, prompt + cached + output,
                draft, accepted, rate,
                r.get("prompt_seconds") if r.get("prompt_seconds") is not None else 0,
                r.get("predicted_seconds") if r.get("predicted_seconds") is not None else 0,
                q["monitoring_coverage_percent"] if q["monitoring_coverage_percent"] is not None else "",
                q["gap_count"], "yes" if q["possible_token_loss"] else "no",
            ])
        filename = "LlamaMonitor_daily_" + time.strftime("%Y%m%d_%H%M%S") + ".csv"
        body = ("\ufeff" + buf.getvalue()).encode("utf-8")  # utf-8-sig：BOM + UTF-8
        logger.info("CSV exported: %s (%d rows)", filename, len(rows))
        return Response(
            content=body,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/data/export/gpu_daily.csv")
    async def api_export_gpu_daily_csv() -> Response:
        """
        导出 gpu_daily 为 CSV（UTF-8 with BOM，Excel 直接打开）。

        平均值由后端计算（avg = sum/count；count=0 为空）；
        energy_wh 为采样估算值，随原样导出。
        """
        rows = db.get_gpu_daily()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "date", "gpu_uuid", "gpu_name",
            "avg_utilization", "max_utilization",
            "avg_memory_used_mb", "max_memory_used_mb",
            "avg_temperature", "max_temperature",
            "avg_power_w", "max_power_w",
            "energy_wh",
        ])
        for r in rows:
            writer.writerow([
                r["date"],
                _csv_safe_text(r["gpu_uuid"]),   # AUDIT-SEC-003：外部文本（nvidia-smi）
                _csv_safe_text(r["gpu_name"]),
                round(r["utilization_sum"] / r["utilization_count"], 2) if r["utilization_count"] else "",
                r["utilization_max"] if r["utilization_max"] is not None else "",
                round(r["memory_used_sum_mb"] / r["memory_used_count"], 1) if r["memory_used_count"] else "",
                r["memory_used_max_mb"] if r["memory_used_max_mb"] is not None else "",
                round(r["temperature_sum"] / r["temperature_count"], 1) if r["temperature_count"] else "",
                r["temperature_max"] if r["temperature_max"] is not None else "",
                round(r["power_sum_w"] / r["power_count"], 1) if r["power_count"] else "",
                r["power_max_w"] if r["power_max_w"] is not None else "",
                round(r["energy_wh"], 3) if r["energy_wh"] else "",
            ])
        filename = "LlamaMonitor_gpu_daily_" + time.strftime("%Y%m%d_%H%M%S") + ".csv"
        body = ("\ufeff" + buf.getvalue()).encode("utf-8")  # utf-8-sig：BOM + UTF-8
        logger.info("GPU CSV exported: %s (%d rows)", filename, len(rows))
        return Response(
            content=body,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/api/data/backup", dependencies=[Depends(_require_loopback)])
    async def api_backup_db() -> Response:
        """
        手动数据库备份（Phase 11）：SQLite Backup API 一致性快照（WAL 安全，
        非文件复制）-> manual_monitor_YYYYMMDD_HHMMSS.db；备份后 quick_check 验证。
        手动备份永不参与自动轮转。to_thread 执行，不阻塞事件循环。
        corrupt 数据库也允许备份（救援路径）。local-only。
        """
        result = await asyncio.to_thread(backup_mgr.create_backup, "manual")
        now = default_clock.now()
        if result.success:
            db.record_backup("manual", result.path, result.size, result.verified,
                             success=True, now=now)
            db.record_event("backup_created", "info", "backup",
                            {"path": result.path, "type": "manual"}, now=now)
            logger.info("Database backup created: %s (%d bytes, verified)",
                        Path(result.path).name, result.size or 0)
            return JSONResponse(status_code=200, content={
                "success": True,
                "file": result.path,
                "size_bytes": result.size,
                "verified": result.verified,
                "created_at": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
            })
        db.record_backup("manual", result.path, None, False, success=False, now=now)
        db.record_event("backup_created", "error", "backup",
                        {"path": result.path, "type": "manual", "error": result.error}, now=now)
        return JSONResponse(status_code=200, content={
            "success": False,
            "error": {"code": "BACKUP_FAILED", "message": result.error or "备份失败"},
        })

    @app.get("/api/data/backups")
    async def api_list_backups() -> list[dict]:
        """
        备份列表（newest first，最近 20 个）：automatic / manual / legacy 三类。
        verified: backup_history 中最近一次该文件的验证结果
        （true=quick_check 通过 / false=未通过 / null=无记录，如早期 legacy 备份）。
        """
        hist = {}
        if db.health not in ("corrupt", "unavailable"):
            for h in db.get_backup_history(limit=200):
                name = Path(h["path"]).name if h.get("path") else None
                if name and name not in hist:
                    hist[name] = bool(h["verified"])
        out: list[dict] = []
        for item in backup_mgr.list_backups()[:20]:
            out.append({
                "filename": item["name"],
                "kind": item["kind"],
                "created_at": datetime.fromtimestamp(item["mtime"]).isoformat(timespec="seconds"),
                "size_bytes": item["size"],
                "verified": hist.get(item["name"]),
            })
        return out

    # ------------------------------------------------------------------
    # Phase 11：数据健康 / 数据质量 / 数据库检查
    # ------------------------------------------------------------------

    def _db_healthy_guard() -> bool:
        """corrupt / unavailable / incompatible 时返回 True（调用方应拒绝修改类操作）。"""
        return db.health in ("corrupt", "unavailable", "incompatible")

    @app.get("/api/health")
    async def api_health() -> dict:
        """
        系统健康总览（只读）：
        - application: healthy / degraded（protective mode）
        - database: healthy / warning / corrupt / unavailable（quick_check 状态）
        - collector / gpu: 在线状态
        - last_valid_sample_seconds_ago: 最近一次有效样本距今（None = 尚无）
        - known_data_gaps: 当前未结束缺口（含开始时间/原因）
        - possible_token_loss: 今日是否存在"可能 token 丢失"的缺口
        now 取自 collector.clock（与数据时间戳同源；生产=系统时钟，测试可注入 FakeClock）。
        """
        now = collector.clock.now()
        _, last_valid_ts = db.get_first_and_last_sample()
        # 最近有效样本：collector 内存（本进程）优先，其次 DB 最后一条 live_sample
        collector_last = collector._last_valid[0] if collector._last_valid else None
        valid_ts = max(t for t in (collector_last, last_valid_ts) if t is not None) if (collector_last or last_valid_ts) else None
        today = local_date(now)
        gap_stats = db.get_gap_stats(today) if not _db_healthy_guard() else {
            "gap_count": 0, "possible_token_loss": False,
            "total_gap_seconds": 0.0, "token_recoverable": True,
        }
        open_gap = collector.open_gap_property()
        return {
            "application": "degraded" if db.health != "healthy" else "healthy",
            "database": db.health,
            "database_detail": db.health_detail,
            "journal_mode": db.journal_mode,
            "collector": {
                "llama_online": bool(collector.last_snapshot and collector.last_snapshot.get("online"))
                if collector.last_snapshot else None,
            },
            "gpu": {
                "available": bool(gpu is not None and gpu.available),
            } if gpu is not None else {"available": False},
            "last_valid_sample_seconds_ago": (now - valid_ts) if valid_ts is not None else None,
            "known_data_gaps": open_gap,
            "possible_token_loss": gap_stats["possible_token_loss"],
        }

    @app.get("/api/data/quality")
    async def api_data_quality() -> dict:
        """
        数据质量（只读）：
        - today: monitoring_coverage_percent（监控覆盖率 = 当天"首样本->末样本"
          时间窗内无已知缺口的比例）/ gap_count / possible_token_loss /
          last_valid_sample_seconds_ago；
        - total: 历史全部缺口的 gap_count / possible_token_loss 是否存在；
        - last_gap: 最近一个已知缺口详情。
        注意：覆盖率不是"Token 统计准确率"——定义见 README。
        now 取自 collector.clock（与数据时间戳同源；生产=系统时钟，测试可注入 FakeClock）。
        """
        now = collector.clock.now()
        today = local_date(now)
        # 当天首/末有效样本（AUDIT-DB-003 同类扩展：按日范围索引查询；
        # 原实现每次轮询取全部 live_samples 再 Python 过滤——~3.4万行/轮，
        # 且顺带删掉了未使用的 get_first_and_last_sample() 调用）
        today_first, today_last = db.get_day_sample_bounds(today)
        # 覆盖率：窗口 = 当天首样本 -> 当天末样本（或现在，若当天仍在监控）
        coverage = None
        if today_first is not None:
            window_end = today_last if today_last is not None else now
            window = max(0.0, window_end - today_first)
            gap_stats = db.get_gap_stats(today)
            in_window = min(gap_stats["total_gap_seconds"], window) if window > 0 else 0.0
            coverage = 100.0 if window <= 0 else max(0.0, 100.0 * (1.0 - in_window / window))
            coverage = round(coverage, 2)
        today_gap = db.get_gap_stats(today)
        # 未结束缺口（进行中）也要计入"已知缺口"
        open_gap = collector.open_gap_property()
        last_gap = None
        gaps = db.get_gaps(limit=1)
        if gaps:
            g = gaps[0]
            last_gap = {
                "start": g["start_timestamp"], "end": g["end_timestamp"],
                "duration_seconds": g["duration_seconds"], "source": g["source"],
                "reason": g["reason"], "possible_token_loss": bool(g["possible_token_loss"]),
            }
        all_gaps = db.get_gaps(limit=100000)
        # 最近缺口明细（Dashboard 展开用）
        def _gap_view(g: dict) -> dict:
            return {
                "start": g["start_timestamp"], "end": g["end_timestamp"],
                "duration_seconds": g["duration_seconds"], "source": g["source"],
                "reason": g["reason"], "possible_token_loss": bool(g["possible_token_loss"]),
            }
        return {
            "today": {
                "date": today,
                "monitoring_coverage_percent": coverage,
                "gap_count": today_gap["gap_count"] + (1 if open_gap else 0),
                "possible_token_loss": bool(today_gap["possible_token_loss"]),
                "last_valid_sample_seconds_ago": (
                    (now - today_last) if today_last is not None else None
                ),
            },
            "total": {
                "gap_count": len(all_gaps),
                "possible_token_loss": any(g["possible_token_loss"] for g in all_gaps),
            },
            "last_gap": last_gap,
            "open_gap": open_gap,
            "recent_gaps": [_gap_view(g) for g in db.get_gaps(limit=20)],
        }

    @app.post("/api/data/check-database", dependencies=[Depends(_require_loopback)])
    async def api_check_database() -> Response:
        """
        手动数据库检查（local-only）：PRAGMA quick_check（只读检查，不修复、
        不删库）。结果更新 db.health 健康状态机：
        - 通过：healthy（若之前是 corrupt 则恢复写入，记 database_recovery 事件）；
        - 失败：corrupt（protective mode，采集停止写入）。
        """
        try:
            ok, detail = db.quick_check()
        except Exception as exc:
            db.set_health("unavailable", repr(exc))
            return JSONResponse(status_code=200, content={
                "success": True, "healthy": False, "status": "unavailable",
                "detail": repr(exc),
            })
        previously = db.health
        if ok:
            # Phase 12：quick_check 通过后核对 schema 版本
            # （"更新版本只读" 保持 incompatible；"迁移被放弃"（如 pre-migration
            #   backup 上次失败）则在此重试 migration，成功后恢复 healthy）
            try:
                db.ensure_migrated()  # 已是最新/更高版本时为 no-op
            except Exception as exc:
                logger.warning("Schema migration 重试失败: %r", exc)
            version = db.get_schema_version()
            if version > CURRENT_SCHEMA_VERSION:
                db.set_health(
                    "incompatible",
                    f"database schema v{version} is newer than this app "
                    f"(supports v{CURRENT_SCHEMA_VERSION}); opened read-only",
                )
                return JSONResponse(status_code=200, content={
                    "success": True, "healthy": False, "status": "incompatible",
                    "detail": db.health_detail,
                })
            if version < CURRENT_SCHEMA_VERSION:
                return JSONResponse(status_code=200, content={
                    "success": True, "healthy": False, "status": "incompatible",
                    "detail": db.health_detail or f"schema migration incomplete (v{version} -> v{CURRENT_SCHEMA_VERSION})",
                })
            db.set_health("healthy", None)
            if previously in ("corrupt", "incompatible"):
                db.record_event("database_recovery", "info", "database",
                                {"detail": f"quick_check passed after {previously}"},
                                now=default_clock.now())
            return JSONResponse(status_code=200, content={
                "success": True, "healthy": True, "status": "healthy", "detail": None,
            })
        db.set_health("corrupt", detail)
        db.record_event("database_integrity_error", "error", "database",
                        {"detail": detail}, now=default_clock.now())
        return JSONResponse(status_code=200, content={
            "success": True, "healthy": False, "status": "corrupt", "detail": detail,
        })

    @app.post("/api/data/clear-live", dependencies=[Depends(_require_loopback)])
    async def api_clear_live(payload: dict) -> Response:
        """
        清空实时历史：live_samples + gpu_samples（不影响 daily_usage / gpu_daily / state）。
        必须显式 {"confirm": true}，否则 400。删除后 VACUUM 收缩文件。
        Phase 11：数据库 corrupt/unavailable 时拒绝（409 DB_UNHEALTHY）。
        """
        if _db_healthy_guard():
            return JSONResponse(status_code=409, content=_err(
                "DB_UNHEALTHY", f"数据库处于 {db.health} 状态（protective mode），先运行 Database Check 或从备份恢复"))
        if not isinstance(payload, dict) or payload.get("confirm") is not True:
            return JSONResponse(status_code=400, content=_err("CONFIRMATION_REQUIRED", '必须传 {"confirm": true}'))
        deleted = db.clear_live_samples(vacuum=True)
        logger.info("Live history cleared: %d sample(s) deleted", deleted)
        return JSONResponse(status_code=200, content={"success": True, "deleted": deleted})

    @app.post("/api/data/reset-statistics", dependencies=[Depends(_require_loopback)])
    async def api_reset_statistics(payload: dict) -> Response:
        """
        重置历史统计（Phase 9 扩展）：同一事务删除
        daily_usage + live_samples + gpu_samples + gpu_daily + mtp_position_daily
        + data_gaps（已知监控缺口，0.16.12 起随重置清除——历史页随之干净），
        **保留 state（Counter baseline，含 per-position MTP baseline）**——
        重置后只统计新增 delta，旧 Token 不会重新计入；GPU 从 0 开始新的 daily。
        必须显式 {"confirm": "RESET"}，否则 400。
        Phase 11：数据库 corrupt/unavailable 时拒绝（409 DB_UNHEALTHY）；
        monitor_events（应用生命周期审计日志）不受重置影响。
        """
        if _db_healthy_guard():
            return JSONResponse(status_code=409, content=_err(
                "DB_UNHEALTHY", f"数据库处于 {db.health} 状态（protective mode），先运行 Database Check 或从备份恢复"))
        if not isinstance(payload, dict) or payload.get("confirm") != "RESET":
            return JSONResponse(status_code=400, content=_err("CONFIRMATION_REQUIRED", '必须传 {"confirm": "RESET"}'))
        deleted = db.reset_statistics()
        logger.info(
            "Statistics reset: %d daily row(s), %d live sample(s), %d gpu sample(s), %d gpu daily row(s), "
            "%d mtp position row(s), %d gap record(s) deleted (state baseline + monitor_events preserved)",
            deleted["daily_deleted"], deleted["live_deleted"], deleted["gpu_samples_deleted"],
            deleted["gpu_daily_deleted"], deleted["mtp_position_deleted"], deleted.get("gaps_deleted", 0),
        )
        return JSONResponse(status_code=200, content={"success": True, **deleted})

    # ------------------------------------------------------------------
    # Phase 10：应用集成（托盘 / 单实例 / 开机自启 / 打开文件夹 / 退出）
    # ------------------------------------------------------------------

    @app.get("/api/app/integration", dependencies=[Depends(_require_loopback)])
    async def api_app_integration() -> dict:
        """
        Windows 集成状态（Settings -> Application）：platform / frozen /
        tray / single_instance / background + autostart（enabled/stale/command
        来自**真实注册表**，不是 config.json 的假设值）。

        桌面模式由 desktop.py 注入 provider；浏览器模式 / 测试返回降级值。

        AUDIT-SEC-002：loopback-only——暴露 executable 路径、app_data 路径、
        autostart.command（注册表完整命令行，含 EXE 路径）与 Python 版本；
        这些是本地实现细节，不属于"远程只读视图"。
        """
        if app_state is not None and app_state.integration_provider is not None:
            return app_state.integration_provider()
        return {
            "platform": "windows" if sys.platform == "win32" else sys.platform,
            "frozen": is_frozen(),
            "python": platform.python_implementation() + " " + platform.python_version(),
            "tray_supported": False,
            "single_instance": False,
            "background": False,
            "executable": None,
            "app_data": str(app_data_dir()),
            "autostart": {
                "supported": False,
                "enabled": False,
                "stale": False,
                "command": "",
                "expected_command": "",
            },
        }

    @app.put("/api/app/autostart", dependencies=[Depends(_require_loopback)])
    async def api_app_autostart(payload: dict) -> Response:
        """
        启用/禁用开机自启（HKCU Run 注册表，仅 EXE 模式）。Body {"enabled": bool}。
        只在用户明确请求时修改注册表（stale 不自动修复，需用户点 Enable/Repair）。
        删除不存在的值仍返回 success（幂等）。local-only。
        """
        if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
            return JSONResponse(status_code=400, content=_err("INVALID_BODY", '必须传 {"enabled": true|false}'))
        if app_state is None or app_state.set_autostart is None:
            return JSONResponse(status_code=503, content=_err("NOT_DESKTOP", "需要桌面模式（pywebview 入口）"))
        try:
            state = app_state.set_autostart(payload["enabled"])
        except RuntimeError as exc:
            return JSONResponse(status_code=400, content=_err("UNSUPPORTED", str(exc)))
        return JSONResponse(status_code=200, content={"success": True, "autostart": state})

    @app.post("/api/app/open-folder", dependencies=[Depends(_require_loopback)])
    async def api_app_open_folder(payload: dict) -> Response:
        """
        打开固定映射的文件夹：{"target": "data" | "logs" | "backups"}。
        后端固定映射到 %LOCALAPPDATA%\\LlamaMonitor（目录不存在先创建），
        不接受任意路径。local-only。
        """
        if not isinstance(payload, dict) or payload.get("target") not in FOLDER_TARGETS:
            return JSONResponse(status_code=400, content=_err(
                "INVALID_TARGET", 'target 必须是: ' + " | ".join(sorted(FOLDER_TARGETS))))
        try:
            path = open_folder(payload["target"])
        except Exception:
            logger.exception("打开文件夹失败: %r", payload.get("target"))
            return JSONResponse(status_code=500, content=_err("OPEN_FAILED", "打开文件夹失败，详见 monitor.log"))
        return JSONResponse(status_code=200, content={"success": True, "path": path})

    @app.post("/api/app/exit", dependencies=[Depends(_require_loopback)])
    async def api_app_exit() -> Response:
        """
        请求完整退出（Tray -> Exit 的 API 等价物）。先返回响应，
        桌面调度器在请求返回后异步执行优雅 shutdown（不在 handler 内同步拆毁，
        避免 HTTP 响应中断/死锁）。重复请求由 AppLifecycle 状态机幂等忽略。
        local-only。
        """
        if app_state is None or app_state.request_exit is None:
            return JSONResponse(status_code=503, content=_err("NOT_DESKTOP", "需要桌面模式（pywebview 入口）"))
        accepted = app_state.request_exit()
        return JSONResponse(status_code=200, content={"success": True, "shutting_down": accepted})

    # ---------------- Phase 13：安全更新（全部 loopback-only） ----------------
    # 信任链见 update_service.py / docs/UPDATE_SECURITY.md：
    # 内置 Ed25519 公钥 -> 验证 manifest 签名 -> manifest 的 size/sha256 校验下载产物。
    # 并发保护：UpdateService 内部 asyncio.Lock，busy 时 409 UPDATE_BUSY。

    def _update_error_response(exc: UpdateError) -> JSONResponse:
        if exc.code in ("UPDATE_BUSY", "NOT_DOWNLOADING"):
            status = 409
        elif exc.code == "CANCELLED":
            status = 200
        else:
            status = 400
        return JSONResponse(
            status_code=status,
            content={
                "error": exc.code,
                "message": exc.message,
                "state": update_service.state,
            },
        )

    @app.get("/api/update/status", dependencies=[Depends(_require_loopback)])
    async def api_update_status() -> dict:
        """
        更新状态快照（§29）：state / current_version / available_version /
        downloaded_bytes / total_bytes / progress_percent / last_check / error，
        另有 installation_mode、settings、release 详情（前端 Settings→Updates 用）。
        local-only。
        """
        return update_service.status()

    @app.post("/api/update/check", dependencies=[Depends(_require_loopback)])
    async def api_update_check() -> Response:
        """
        检查 GitHub Release（§13-§17）：latest stable release -> manifest + .sig ->
        Ed25519 验签 -> 字段验证 -> 版本比较（remote <= current 一律不更新）。
        失败返回 {error, message, state}；busy 时 409 UPDATE_BUSY。local-only。
        """
        try:
            status = await update_service.check(manual=True)
        except UpdateError as exc:
            return _update_error_response(exc)
        return JSONResponse(status)

    @app.post("/api/update/download", dependencies=[Depends(_require_loopback)])
    async def api_update_download() -> Response:
        """
        流式下载更新（§31-§38）：到 updates\\{version}\\*.part，边下载边算 SHA-256，
        完成后 size + hash 双校验，os.replace 转正；失败删除 .part 并进入 ERROR。
        要求 state=UPDATE_AVAILABLE（先 Check）。busy 时 409 UPDATE_BUSY。local-only。
        """
        try:
            status = await update_service.download()
        except UpdateError as exc:
            return _update_error_response(exc)
        return JSONResponse(status)

    @app.post("/api/update/install", dependencies=[Depends(_require_loopback)])
    async def api_update_install() -> Response:
        """
        安装更新（§47-§58）：Pre-Update Backup（DB + config）-> 写 pending marker ->
        启动已验证 Installer（/SILENT /NORESTART /APPUPDATE[_BG]）-> 本应用优雅退出。
        要求 state=READY_TO_INSTALL。busy 时 409 UPDATE_BUSY。local-only。
        """
        try:
            status = await update_service.install()
        except UpdateError as exc:
            return _update_error_response(exc)
        return JSONResponse(status)

    @app.post("/api/update/cancel", dependencies=[Depends(_require_loopback)])
    async def api_update_cancel() -> Response:
        """
        取消下载（§40）：只允许 state=DOWNLOADING（置 cancel event，不强杀线程），
        下载循环检测到 event 后删除 .part 回到 UPDATE_AVAILABLE。local-only。
        """
        try:
            status = await update_service.cancel()
        except UpdateError as exc:
            return _update_error_response(exc)
        return JSONResponse(status)

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request, exc: Exception) -> JSONResponse:
        """兜底：Traceback 只写日志，前端拿到人类可读错误。"""
        logger.exception("未处理异常 %s: %r", request.url.path, exc)
        return JSONResponse(status_code=500, content=_err("INTERNAL_ERROR", "内部错误，详见 monitor.log"))

    @app.get("/api/summary")
    async def api_summary() -> dict:
        """
        今日与累计总量（daily_usage 按本机系统日期归集）。

        compute_tokens = prompt + output
        logical_tokens = prompt + cached + output
        """
        rows = db.get_daily_usage()
        today = next((r for r in rows if r["date"] == local_date()), None)
        today_sum = _summary_shape(
            (today or {}).get("prompt_tokens", 0) or 0,
            (today or {}).get("cached_tokens", 0) or 0,
            (today or {}).get("output_tokens", 0) or 0,
        )
        total_sum = _summary_shape(
            sum(r["prompt_tokens"] for r in rows),
            sum(r["cached_tokens"] for r in rows),
            sum(r["output_tokens"] for r in rows),
        )
        return {"today": today_sum, "total": total_sum}

    def _day_quality(date: str, day_samples: list | None = None) -> dict:
        """
        某自然日的 Monitoring Coverage / 缺口统计（Phase 11）。

        Monitoring Coverage = 当天监控时间窗内无已知采集缺口的时间占比
        （**不是** Token 统计准确率，定义见 README）。
        时间窗规则：
        - 当天仍有 live_samples（48h 保留内）：窗 = 首个样本 -> 末个样本
          （今天则到"现在"，上限为末样本 + 2 个采集间隔，避免把正常等待算进窗口）；
        - 更早的自然日（live 已清理）但有 daily 行：窗按 24h 估算（该日确实
          运行过监控——daily 行就是证据；已知缺口按实际时长扣减）；
        - 无任何数据：coverage = null。
        now 取自 collector.clock（与数据时间戳同源）。

        AUDIT-DB-003：day_samples 可预传（按日分组好的样本）——原实现在**每次调用**
        都全量加载 live_samples 再过滤，/api/daily 对 30 天循环 => 30 次全量扫描
        （48h 保留下每次 ~1.7 万行）。调用方在循环外取一次并分组后传入。
        """
        now = collector.clock.now()
        today = local_date(now)
        if day_samples is None:
            try:
                all_samples = db.get_live_samples(hours=None)
            except Exception:
                all_samples = []
            day_samples = [s for s in all_samples if local_date(s["timestamp"]) == date]
        gap_stats = db.get_gap_stats(date)
        coverage = None
        if day_samples:
            first = day_samples[0]["timestamp"]
            last = day_samples[-1]["timestamp"]
            if date == today:
                window_end = min(now, last + collector.interval * 2)
            else:
                window_end = last
            window = max(0.0, window_end - first)
            in_window = min(gap_stats["total_gap_seconds"], window) if window > 0 else 0.0
            coverage = 100.0 if window <= 0 else round(max(0.0, 100.0 * (1.0 - in_window / window)), 2)
        elif date < today:
            # 过去某天：daily 行存在 => 当天有监控运行；按 24h 窗估算
            in_day = min(gap_stats["total_gap_seconds"], 86400.0)
            coverage = round(max(0.0, 100.0 * (1.0 - in_day / 86400.0)), 2)
        return {
            "monitoring_coverage_percent": coverage,
            "gap_count": gap_stats["gap_count"],
            "possible_token_loss": bool(gap_stats["possible_token_loss"]),
        }

    @app.get("/api/daily")
    async def api_daily(days: int = Query(30, ge=1, le=365)) -> dict:
        """
        最近 N 个自然日的统计（日期升序）。

        只返回实际有数据的天（无使用量的天没有行，空档由前端补齐）；
        每行含原始字段 + compute_tokens / logical_tokens 派生字段
        + Phase 11 数据质量字段（monitoring_coverage_percent / gap_count /
        possible_token_loss）。
        """
        cutoff = local_date(collector.clock.now() - (days - 1) * 86400)
        rows = [r for r in db.get_daily_usage() if r["date"] >= cutoff]
        # AUDIT-DB-003：live 样本一次取全、按日分组（原实现在循环内每天全量扫一次）
        try:
            all_live = db.get_live_samples(hours=None)
        except Exception:
            all_live = []
        live_by_date: dict[str, list] = {}
        for s in all_live:
            live_by_date.setdefault(local_date(s["timestamp"]), []).append(s)
        out = []
        for r in rows:
            row = _with_derived(r)
            row.update(_day_quality(r["date"], live_by_date.get(r["date"], [])))
            out.append(row)
        return {"days": out}

    @app.get("/api/live")
    async def api_live(minutes: int = Query(60, ge=1, le=2880)) -> dict:
        """
        最近 N 分钟的实时采样（每采集一轮一条）。

        含 prompt/cached/output delta、prompt_tps、decode_tps、
        requests_processing、requests_deferred、context_max、mtp_accept_rate。
        上限 2880（48h，live_samples 保留时长）。
        """
        samples = db.get_live_samples(hours=minutes / 60)
        return {"samples": samples}

    @app.get("/api/mtp")
    async def api_mtp() -> dict:
        """
        今日的 MTP / spec-decode 统计。

        - draft / accepted: 当日累计 delta（reset 安全）；
        - accept_rate: accepted / draft * 100，draft 为 0 时为 null；
        - positions: 仅当服务器提供 position 数据且当日有增量时返回，
          为当日 per-position 接受数（按 position 数值排序）。
        """
        rows = db.get_daily_usage()
        today = next((r for r in rows if r["date"] == local_date()), None) or {}
        draft = today.get("draft_tokens", 0) or 0
        accepted = today.get("accepted_tokens", 0) or 0
        num_drafts = today.get("draft_sequences", 0) or 0  # Phase 9：Draft Sequences
        result: dict = {
            "draft": draft,
            "accepted": accepted,
            "accept_rate": (accepted / draft * 100.0) if draft > 0 else None,
            # Phase 9 命名（与旧键并存，向后兼容）
            "draft_tokens": draft,
            "accepted_tokens": accepted,
            "num_drafts": num_drafts,
        }
        positions = db.get_mtp_position_daily(local_date())
        if positions:
            ordered = sorted(
                positions.items(),
                key=lambda kv: (0, int(kv[0])) if kv[0].isdigit() else (1, kv[0]),
            )
            result["positions"] = [
                {"position": pos, "accepted": val, "accepted_tokens": val} for pos, val in ordered
            ]
        return result

    @app.get("/api/mtp/daily")
    async def api_mtp_daily(days: int = Query(30, ge=1, le=365)) -> dict:
        """
        最近 N 个自然日的 MTP 统计（日期升序，无使用量的天也返回，值为 0 / rate=null）。

        供前端绘制 MTP 接受率每日趋势（与 /api/daily 的 mtp_accept_rate 同源）。
        """
        rows = {r["date"]: r for r in db.get_daily_usage()}
        days_out = []
        for date in _recent_dates(days, now=collector.clock.now()):  # AUDIT-ASYNC-005
            row = rows.get(date) or {}
            draft = row.get("draft_tokens", 0) or 0
            accepted = row.get("accepted_tokens", 0) or 0
            days_out.append({
                "date": date,
                "draft": draft,
                "accepted": accepted,
                "accept_rate": accepted / draft * 100.0 if draft > 0 else None,
            })
        return {"days": days_out}

    return app


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="LlamaMonitor FastAPI 后端（参数来自 config.json）")
    parser.add_argument("--url", default=None, help="llama-server 基础地址（覆盖 config.json）")
    parser.add_argument("--db", default=None, help="SQLite 路径（覆盖 config.json）")
    parser.add_argument("--interval", type=float, default=None, help="采集间隔秒数（覆盖 config.json）")
    args = parser.parse_args()

    # 先挂载日志 handler（默认级别），保证 load_config 的警告/错误能写入 monitor.log
    setup_logging()
    loaded = load_config()
    cfg = loaded.config
    apply_overrides(cfg, url=args.url, db_path=args.db, interval=args.interval)
    log = setup_logging(cfg.logging)  # 幂等：按配置调整级别

    db = Database(
        cfg.database_path,
        wal=cfg.database.wal,
        retention_seconds=cfg.collector.live_retention_hours * 3600,
        pre_migration_backup_dir=app_data_dir() / "backups",
    )
    collector, gpu = build_collector(cfg, db)
    app = build_app(db, collector, loaded, gpu)

    log.info("启动 FastAPI: http://%s:%s（Ctrl+C 停止）", cfg.web.host, cfg.web.port)
    # log_config=None：uvicorn 不覆盖全局 logging 配置，
    # 其日志传播到 root（控制台 + monitor.log）；访问日志在 setup_logging 中降到 WARNING
    server = uvicorn.Server(uvicorn.Config(app, host=cfg.web.host, port=cfg.web.port, log_config=None))
    try:
        server.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
