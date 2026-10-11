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
from datetime import datetime, timedelta, timedelta
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
from hardware_sensor_provider import HardwareSensorProvider
from llama_runtime_collector import LlamaRuntimeCollector
from system_collector import SystemCollector
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
    # 1.1 高级遥测（schema v5 列；旧行 -> None）
    "memory_controller_percent",
    "power_limit_w",
    "pcie_gen_max",
    "pcie_width_max",
    "performance_state",
)

# Round-3 系统页：/api/system/live 返回的历史字段（降采样时按此聚合，勿漏）。
_SYS_LIVE_FIELDS = (
    "cpu_usage_percent",
    "cpu_frequency_mhz",
    "cpu_temperature_c",
    "cpu_package_power_w",
    "memory_usage_percent",
    "memory_used_bytes",
    "memory_total_bytes",
    "disk_read_bps",
    "disk_write_bps",
    "network_rx_bps",
    "network_tx_bps",
    "monitored_component_power_w",
)


def _downsample(points: list[dict], max_points: int = 2000, fields: tuple | list | None = None) -> list[dict]:
    """
    简单 bucket 聚合降采样（纯 Python，无 numpy/pandas）：

    - 点数 <= max_points：原样返回；
    - 否则按等宽分桶，每桶取各数值字段的非空均值（时间戳取桶内第一个）。
    前端不需要一次画几万个点。

    fields：要聚合的数值字段。None（缺省）= 从第一个点自动检测所有数值字段
    （排除 timestamp）。GPU 传 _GPU_LIVE_FIELDS、System 传 _SYS_LIVE_FIELDS——
    不显式指定会漏字段（曾发生 system 24h 因字段列表是 GPU-only 导致全 None 的 bug）。
    """
    if len(points) <= max_points:
        return points
    if fields is None:
        ref = points[0]
        fields = [k for k in ref.keys()
                  if k != "timestamp" and isinstance(ref.get(k), (int, float)) and ref.get(k) is not None]
    bucket = (len(points) + max_points - 1) // max_points
    out: list[dict] = []
    for start in range(0, len(points), bucket):
        chunk = points[start:start + bucket]
        merged: dict = {"timestamp": chunk[0]["timestamp"]}
        for field in fields:
            values = [p[field] for p in chunk if p.get(field) is not None]
            merged[field] = round(sum(values) / len(values), 2) if values else None
        out.append(merged)
    return out


def _sys_stats(values: list) -> dict:
    """一组（可能含 None 的）浮点 -> {current(末个非空), avg, max}；全空 -> 全 None。
    用于 /api/system/live summary（chart header 当前/平均/峰值，§46-§48）。"""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return {"current": None, "avg": None, "max": None}
    return {"current": vals[-1], "avg": sum(vals) / len(vals), "max": max(vals)}


def _system_live_summary(points: list[dict]) -> dict:
    """所选范围系统历史摘要（chart header 分析摘要；在原始样本上算，准确）。"""
    return {
        "cpu": _sys_stats([p.get("cpu_usage_percent") for p in points]),
        "memory": _sys_stats([p.get("memory_usage_percent") for p in points]),
        "disk_read": _sys_stats([p.get("disk_read_bps") for p in points]),
        "disk_write": _sys_stats([p.get("disk_write_bps") for p in points]),
        "network_rx": _sys_stats([p.get("network_rx_bps") for p in points]),
        "network_tx": _sys_stats([p.get("network_tx_bps") for p in points]),
        "power": _sys_stats([p.get("monitored_component_power_w") for p in points]),
    }


def _system_adapters_public(system) -> dict[str, dict]:
    """每接口当前速率 + 元数据（接口选择器 / §107-108 errors-drops）。
    合并 system.adapter_rates()（rx/tx）与 system.network_interfaces()（kind/speed/err/...）。"""
    try:
        rates = system.adapter_rates() or {}
    except Exception:
        rates = {}
    try:
        meta = {itf["name"]: itf for itf in (system.network_interfaces() or [])}
    except Exception:
        meta = {}
    out: dict[str, dict] = {}
    for name in set(list(rates.keys()) + list(meta.keys())):
        r = rates.get(name) or {}
        m = meta.get(name) or {}
        out[name] = {
            "rx_bps": r.get("rx_bps"),
            "tx_bps": r.get("tx_bps"),
            "kind": m.get("kind"),
            "is_default": m.get("is_default"),
            "is_virtual": m.get("is_virtual"),
            "speed_mbps": m.get("speed_mbps"),
            "errin": m.get("errin"),
            "errout": m.get("errout"),
            "dropin": m.get("dropin"),
            "dropout": m.get("dropout"),
        }
    return out


def _model_summary_public(runtime, request: Request) -> dict | None:
    """
    模型摘要（/api/status 用；完整信息见 /api/llama/info）。
    远程只读客户端不泄漏完整模型路径（只返回文件名）。
    """
    info = runtime.model_info
    if info is None:
        return None
    return {
        "model_alias": info.get("model_alias"),
        "model_file_name": info.get("model_file_name"),
        "model_ftype": info.get("model_ftype"),
        "parameter_count": info.get("parameter_count"),
        "model_size_bytes": info.get("model_size_bytes"),
        "context_size": info.get("context_size"),
        "total_slots": info.get("total_slots"),
        "vision_supported": info.get("vision_supported"),
        "video_supported": info.get("video_supported"),
        "audio_supported": info.get("audio_supported"),
        "build_info": info.get("build_info"),
        "is_sleeping": info.get("is_sleeping"),
    }


def _slots_public(runtime) -> list[dict]:
    """Slot 摘要（/api/status 用；完整白名单字段见 /api/llama/slots）。"""
    out = []
    for s in runtime.slots:
        out.append({
            "id": s.get("id"),
            "is_processing": bool(s.get("is_processing")),
            "n_ctx": s.get("n_ctx"),
            "n_prompt_tokens": s.get("n_prompt_tokens"),
            "n_prompt_tokens_cache": s.get("n_prompt_tokens_cache"),
            "n_prompt_tokens_processed": s.get("n_prompt_tokens_processed"),
            "next_token": s.get("next_token"),
        })
    return out


def _power_percent(draw_w: float | None, limit_w: float | None) -> float | None:
    """GPU 功耗占比 = draw / limit * 100。**只有两个值都存在且 limit > 0 才计算**
    （1.1 语义：缺任一 -> None，绝不显示假数值；UI 显示 --）。"""
    if draw_w is None or limit_w is None or limit_w <= 0:
        return None
    return round(draw_w / limit_w * 100.0, 1)


def _pstate_label(state: int | None) -> str | None:
    """P-state int -> 'P0' 标签（None -> None）。P0 只表示最高性能状态，
    **不**等于 100% 性能（UI tooltip 说明）。"""
    return f"P{state}" if state is not None else None


def _gpu_ecc_payload(gpu, uuid: str) -> dict | None:
    """
    GPU ECC 健康 payload（slow health，60s 周期）。

    - 不支持 ECC 的 GPU（ecc_enabled 未查到 / None）-> None（UI 整个 ECC 区隐藏，
      不显示一排 --）；
    - 支持时返回 {enabled, corrected/uncorrected volatile/aggregate,
      retired_pages_single_bit/double_bit/pending, remapped_rows}；
      细项缺失（如消费卡无 SRAM / 驱动未提供退役页细分）-> 该计数 None。
    """
    if gpu is None or not gpu.available:
        return None
    health = gpu.slow_health.get(uuid)
    if not health or health.get("ecc_enabled") is None:
        return None
    return {
        "enabled": bool(health.get("ecc_enabled")),
        "corrected_volatile": health.get("ecc_corrected_volatile"),
        "corrected_aggregate": health.get("ecc_corrected_aggregate"),
        "uncorrected_volatile": health.get("ecc_uncorrected_volatile"),
        "uncorrected_aggregate": health.get("ecc_uncorrected_aggregate"),
        # Round-4：退役页细分（单比特/双比特/待处理）；None = 该驱动未提供
        "retired_pages_single_bit": health.get("retired_pages_single_bit"),
        "retired_pages_double_bit": health.get("retired_pages_double_bit"),
        "retired_pages_pending": health.get("retired_pages_pending"),
        "remapped_rows": health.get("remapped_rows"),
    }


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


def _throughput_window_avg(samples: list[dict]) -> dict:
    """
    选定窗口的**加权平均吞吐**（Round 5 §46-§48）。

    不是对逐样本 prompt_tps/decode_tps 做简单平均（那样每个 5s 样本等权，
    短促 burst 会被稀释/夸大），而是：
      prompt_tps_avg = Δprompt_tokens / Δprompt_seconds
      decode_tps_avg = Δgenerated_tokens / Δpredicted_seconds
    其中 Δ 取窗口内所有样本的 token delta 与 seconds delta 之和。

    - 分母 <= 0（含无秒数推进、全 idle）或分子缺失 -> 该平均为 None（§48，
      绝不返回 0 或 Infinity，也不把"无吞吐"当 0 tok/s 误报）。
    - 仅统计 seconds delta 非 None 的样本参与分母，token delta 取对应样本。
    """
    prompt_tok = 0.0
    prompt_sec = 0.0
    decode_tok = 0.0
    decode_sec = 0.0
    has_prompt_tok = False
    has_decode_tok = False
    for s in samples or []:
        dt = s.get("prompt_delta")
        if dt is not None:
            prompt_tok += dt
            has_prompt_tok = True
        ds = s.get("prompt_seconds")
        if ds is not None:
            prompt_sec += ds
        ot = s.get("output_delta")
        if ot is not None:
            decode_tok += ot
            has_decode_tok = True
        os_ = s.get("predicted_seconds")
        if os_ is not None:
            decode_sec += os_
    prompt_avg = (prompt_tok / prompt_sec) if (has_prompt_tok and prompt_sec > 0) else None
    decode_avg = (decode_tok / decode_sec) if (has_decode_tok and decode_sec > 0) else None
    return {
        "prompt_tps_avg": prompt_avg,
        "decode_tps_avg": decode_avg,
        "prompt_seconds": prompt_sec if prompt_sec > 0 else None,
        "predicted_seconds": decode_sec if decode_sec > 0 else None,
    }


def _downsample_throughput(samples: list[dict], max_points: int = 2000) -> list[dict]:
    """
    throughput 窗口的 bucket 降采样（纯 Python）。

    24h / 5s 采集 = ~1.7 万点；折线一次画 ~2000 点足够。分桶规则：
    - 时间戳取桶内第一个；
    - prompt_tps / decode_tps / requests_* / busy_slots 取桶内非空均值
      （速率类：均值是桶内该速率的近似，用于画图即可；**加权平均吞吐**
      另由 _throughput_window_avg 在原始样本上计算，不走降采样，保证精确）；
    - prompt_delta / output_delta / prompt_seconds / predicted_seconds 取桶内
      求和（保持窗口加权平均与 tooltip 计数在降采样后仍一致）。
    """
    if len(samples) <= max_points:
        return samples
    bucket = (len(samples) + max_points - 1) // max_points
    avg_fields = ("prompt_tps", "decode_tps", "requests_processing",
                  "requests_deferred", "busy_slots")
    sum_fields = ("prompt_delta", "output_delta", "prompt_seconds", "predicted_seconds")
    out: list[dict] = []
    for start in range(0, len(samples), bucket):
        chunk = samples[start:start + bucket]
        merged: dict = {"timestamp": chunk[0]["timestamp"]}
        for f in avg_fields:
            vals = [c[f] for c in chunk if c.get(f) is not None]
            merged[f] = round(sum(vals) / len(vals), 3) if vals else None
        for f in sum_fields:
            vals = [c[f] for c in chunk if c.get(f) is not None]
            merged[f] = (sum(vals) if vals else None)
        out.append(merged)
    return out


def _summary_shape(prompt: int, cached: int, output: int) -> dict:
    """summary 的统一字段形状（含派生字段）。"""
    return {
        "prompt_tokens": prompt,
        "cached_tokens": cached,
        "output_tokens": output,
        "compute_tokens": prompt + output,
        "logical_tokens": prompt + cached + output,
    }


def _attention_rank(sev: str) -> int:
    """Attention 排序权重：Error(0) > Warning(1) > Info(2)（同级保持输入序）。"""
    return {"error": 0, "warning": 1, "info": 2}.get(sev, 3)


def build_attention_items(items: list[dict]) -> dict:
    """
    「需要关注」聚合（Round-6 §需要关注）：**只消费**各模块已产出的可靠状态
    （service/gpu/system/integrity 域在端点内组装成 item 列表传入），本函数
    只做排序与截断，不重新发明健康判断。

    - 排序：severity Error > Warning > Info；同级保持传入顺序（各域组装顺序）；
    - 截断：最多返回 3 条（items[:3]），total_count 为全量条数，
      remaining = max(0, total - 3)（UI 显示「还有 N 项 查看监控历史 →」）。
    每项形如 {key, severity("error"/"warning"/"info"), title, subtitle}。
    """
    ordered = sorted(items, key=lambda it: _attention_rank(it.get("severity", "info")))
    shown = ordered[:3]
    total = len(ordered)
    return {
        "items": shown,
        "total_count": total,
        "remaining": max(0, total - 3),
        "hidden": total == 0,  # 0 项 -> 前端整块 display:none
    }


def _base_address_public(metrics_url: str) -> str:
    """
    从完整 metrics 地址（如 http://127.0.0.1:9091/metrics）提取公开展示的基础地址
    （127.0.0.1:9091）：去 scheme、去 path。失败时回退为原始串去 scheme。
    Overview 服务卡只展示 host:port（§服务卡地址），/metrics 走 tooltip。
    """
    s = metrics_url or ""
    low = s.lower()
    for pre in ("http://", "https://"):
        if low.startswith(pre):
            s = s[len(pre):]
            break
    slash = s.find("/")
    if slash != -1:
        s = s[:slash]
    return s


def _overview_tps_display(value, online: bool, metric_supported: bool) -> dict:
    """
    TPS 展示语义（Round-6 §推理状态：绝不混淆「0」与「不可用」）：
    - 离线 -> {value: None, display: "unavailable", tip: None}；
    - 在线且指标受支持（collector 已上报该 counter，即使本轮无 delta -> 空闲）
      -> 空闲显示 0：{value: 0.0, display: "idle", tip: "当前无对应推理活动"}；
    - 在线但指标不受支持（旧版 llama.cpp 无该 counter）-> unavailable。
    value 非 None（真实速率）-> display "active"。
    """
    if not online:
        return {"value": None, "display": "unavailable", "tip": None}
    if value is not None:
        return {"value": value, "display": "active", "tip": None}
    if not metric_supported:
        return {"value": None, "display": "unavailable", "tip": None}
    return {"value": 0.0, "display": "idle", "tip": "当前无对应推理活动"}


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


def _client_is_loopback(request: Request) -> bool:
    """客户端是否本机（127.0.0.1 / ::1）。只读端点据此决定泄露多少本地信息。"""
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1")


def _expose_path(request: Request, full: str) -> str:
    """
    本地系统路径的远程泄露防护（Phase 10 补强）：
    - 本机客户端（loopback）-> 返回完整路径（设置页需要显示数据库/配置文件位置）；
    - 远程只读客户端（web.host=0.0.0.0 后局域网可达）-> 只返回文件名 basename，
      不泄漏 Windows 用户名 / %LOCALAPPDATA% 目录结构。
    只读端点（/api/status、/api/data/info）据此收敛，修改类端点仍走 _require_loopback。
    """
    if _client_is_loopback(request):
        return full
    return Path(full).name


def build_app(
    db: Database,
    collector: MetricsCollector,
    loaded: LoadedConfig | None = None,
    gpu: GpuCollector | None = None,
    app_state: AppIntegrationState | None = None,
    update_service: UpdateService | None = None,
    runtime: "LlamaRuntimeCollector | None" = None,
    system: "SystemCollector | None" = None,
    sensors: "HardwareSensorProvider | None" = None,
) -> FastAPI:
    """
    创建 FastAPI 应用。db / collector / gpu / loaded / app_state 注入，便于测试指向
    临时数据库、注入固定 metrics 文本 / GPU runner / 配置状态（见 tests/）。

    gpu 为 None（测试）时 GPU 相关 API 返回 available=false，不影响其他功能。
    app_state 为 None（浏览器模式 / 测试）时 /api/app/* 返回降级值，不影响其他功能。
    update_service 为 None（测试/浏览器模式）时创建默认 UpdateService；测试可注入
    带 MockTransport 客户端 / 固定 installation_mode 的实例（Phase 13）。

    1.1.0：runtime（llama Health/Model/Slot）/ system（CPU/内存/磁盘/网络）/
    sensors（高级硬件传感器）均为可选注入——None（测试/浏览器模式）时相应
    API 返回 available=false / 降级值，**绝不影响** Token 采集与其他功能
    （四个 Collector 故障隔离）。
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
        # 1.1：启动 system / sensors / runtime（故障隔离：任何失败不影响 Token 采集）
        if system is not None:
            try:
                system.inventory = system.read_inventory()  # 静态库存：启动读一次
            except Exception:
                logger.warning("系统静态库存读取失败（不影响基础监控）", exc_info=True)
        if sensors is not None:
            sensors.start()
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
                # 1.1：metrics 在线状态同步给 runtime（metrics 失败时允许立即 health 检查）
                if runtime is not None:
                    try:
                        runtime.set_metrics_online(bool((collector.last_snapshot or {}).get("online")))
                    except Exception:
                        pass
                _refresh_app_state()

        app.state.collector_task = asyncio.create_task(_periodic())

        # 1.1：System 采集任务（独立循环，每 poll_interval_seconds 一轮；故障隔离）
        system_task = None
        if system is not None and system.enabled:
            async def _system_periodic() -> None:
                while True:
                    await asyncio.sleep(system.config.system.poll_interval_seconds)
                    try:
                        # 同步 GPU 功耗（组件功耗合计 = CPU + 全部 GPU；GPU 无数据 -> None）
                        if gpu is not None:
                            powers = [s.power_draw_w for s in gpu.latest if s.power_draw_w is not None]
                            system.set_gpu_power_total(sum(powers) if powers else None)
                        # 同步高级传感器（provider 不可用时字段保持 None -> UI 显示 --）
                        if sensors is not None:
                            snap = sensors.snapshot()
                            system.set_advanced_sensor_values(
                                {"cpu_temperature_c": snap["cpu_temperature_c"],
                                 "cpu_package_power_w": snap["cpu_package_power_w"]},
                                fans=snap["fans"], all_sensors=snap["sensors"],
                                available=(snap["state"] == "available"),
                            )
                        # AUDIT-1.1.1 REL-1111-001：阻塞的 psutil 采样在 to_thread，
                        # 事件循环不再被每 2s 的 10~50ms WMI 调用周期性卡住
                        await system.poll_once_async()
                    except Exception:
                        logger.debug("System 周期采集异常（内部应已处理，双保险）", exc_info=True)

            system_task = asyncio.create_task(_system_periodic())

        # 1.1：llama Runtime 采集任务（/health /slots /props /v1/models 分频调度）
        runtime_task = None
        if runtime is not None:
            runtime.set_metrics_online(bool((collector.last_snapshot or {}).get("online")))
            runtime_task = asyncio.create_task(runtime.run())

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

        if runtime is not None:
            app.state.runtime = runtime
        if system is not None:
            app.state.system = system
        if sensors is not None:
            app.state.sensors = sensors

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
        # 1.1：停止 system / runtime / sensors（优雅终止，不留孤儿进程/线程）
        if runtime_task is not None:
            runtime_task.cancel()
            try:
                await runtime_task
            except asyncio.CancelledError:
                pass
            try:
                await runtime.aclose()
            except Exception:
                pass
        if system_task is not None:
            system_task.cancel()
            try:
                await system_task
            except asyncio.CancelledError:
                pass
            try:
                system.shutdown()
            except Exception:
                pass
        if sensors is not None:
            try:
                sensors.stop()  # 优雅终止 Bridge（关 stdin -> 宽限 -> Terminate）
            except Exception:
                logger.warning("停止硬件传感器 provider 失败", exc_info=True)
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

    # 16D：静态文件强制 revalidate（no-cache + ETag）。
    # FastAPI StaticFiles 不带 Cache-Control，浏览器会启发式缓存旧 JS/CSS ——
    # 原地升级便携版后新 app.js 配旧 formatters.js 会报 "F.formatClock is not a function"。
    # no-cache 不增加流量（ETag 304），只保证导航后必向服务端校验。
    @app.middleware("http")
    async def _static_no_cache(request, call_next):
        response = await call_next(request)
        if (request.url.path == "/" or request.url.path.startswith("/static/")) and \
                "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        """Dashboard 首页（static/index.html）。"""
        index_file = _STATIC_DIR / "index.html"
        if not index_file.is_file():
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="static/index.html 不存在")
        return FileResponse(index_file, media_type="text/html")

    @app.get("/api/status")
    async def api_status(request: Request) -> dict:
        """
        当前状态：服务器在线情况 + 最新 gauge + 最近一轮的 TPS / MTP + 配置状态。

        - server_online: true/false；启动后尚未完成首次采集时为 null；
        - 离线时数值字段均为 null（last_update 仍为最近一次检查时刻）；
        - last_update: Unix 秒，最近一次采集完成时刻；
        - config: {path, loaded, using_defaults, has_errors}（配置加载状态）；
          远程只读客户端的 path 只返回文件名（不泄漏 Windows 用户名/目录）。
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
                "path": _expose_path(request, str(loaded.path)),
                "loaded": loaded.loaded,
                "using_defaults": loaded.using_defaults,
                "has_errors": loaded.has_errors,
            }
        # 1.1：llama.cpp Runtime（Health State / 模型摘要 / 当前 Slot）。
        # 全部内存态（llama_runtime_collector）；runtime collector 未注入时 -> None。
        if runtime is not None:
            result["server_state"] = runtime.server_state  # ready / loading / unavailable
            result["llama_model"] = _model_summary_public(runtime, request)
            result["llama_slots"] = _slots_public(runtime)
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
            # 1.1：系统监控段（放在 gpu 之后，与设置页分区顺序一致）
            "system": {
                "enabled": cfg.system.enabled,
                "poll_interval_seconds": cfg.system.poll_interval_seconds,
                "history_interval_seconds": cfg.system.history_interval_seconds,
                "history_retention_hours": cfg.system.history_retention_hours,
                "advanced_sensors": cfg.system.advanced_sensors,
                "advanced_sensor_interval_seconds": cfg.system.advanced_sensor_interval_seconds,
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
                "gpu_uuids_monitored": [],
                "gpus": [],
            }
        if not gpu.config.gpu.enabled:
            return {
                "available": False,
                "provider": "nvidia-smi",
                "reason": "gpu monitoring disabled",
                "last_update": gpu.last_update,
                "detected": gpu.detected,
                "gpu_uuids_monitored": list(gpu.config.gpu.device_uuids),
                "gpus": [],
            }
        gpus = []
        if gpu.available:
            # device_uuids 非空 = 只监控指定 GPU；gpus 只列被监控的卡。
            # 未选中的卡（如升级前曾监控、后来取消勾选）历史保留在 gpu_samples，
            # 但 /api/gpu/status 不再展示其**陈旧**的"最新采样"——那会误导成"还在监控"。
            # 未选中的卡仍通过 detected + gpu_uuids_monitored 暴露给前端（标记"未监控"）。
            monitored = set(gpu.config.gpu.device_uuids)
            for row in db.get_gpu_latest():
                if monitored and row["gpu_uuid"] not in monitored:
                    continue
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
                    # ---- 1.1 高级遥测（旧字段全部保留；新字段缺失 -> None）----
                    "memory_controller_percent": row.get("memory_controller_percent"),
                    "power_limit_w": row.get("power_limit_w"),
                    "power_percent": _power_percent(row.get("power_draw_w"), row.get("power_limit_w")),
                    "pcie_gen_max": row.get("pcie_gen_max"),
                    "pcie_width_max": row.get("pcie_width_max"),
                    "performance_state": _pstate_label(row.get("performance_state")),
                    "driver_version": (gpu.driver_version or None),
                    # slow health（60s 周期；不支持 -> None -> UI 隐藏 ECC 区）
                    "ecc": _gpu_ecc_payload(gpu, row["gpu_uuid"]),
                    # Round-4：静态字段（PCI Bus ID / Compute Mode / Persistence；None -> UI 不显示）
                    "pci_bus_id": (gpu.gpu_static or {}).get(row["gpu_uuid"], {}).get("pci_bus_id"),
                    "compute_mode": (gpu.gpu_static or {}).get(row["gpu_uuid"], {}).get("compute_mode"),
                    "persistence_mode": (gpu.gpu_static or {}).get(row["gpu_uuid"], {}).get("persistence_mode"),
                    # 性能限制原因（非故障；0x0 时 []）
                    "throttle_reasons": list(gpu.latest_throttle.get(row["gpu_uuid"], []))
                    if hasattr(gpu, "latest_throttle") else [],
                })
        # 1.1：GPU 进程（只读；WDDM 下 used_memory 常 None -> UI 显示 --）
        processes = gpu.processes if gpu.available else []
        # Round-4：数据新鲜度（§13-14）。stale_seconds = now - last_update（>0）；
        # 前端 stale_seconds > 3 × poll_interval 时显示"数据已过期"（Last Known Values）。
        now_ts = time.time()
        stale_seconds = (
            round(now_ts - gpu.last_update, 1)
            if gpu.available and gpu.last_update else None
        )
        return {
            "available": gpu.available,
            "provider": "nvidia-smi",
            "reason": None if gpu.available else gpu.unavailable_reason,
            "last_update": gpu.last_update,
            "stale_seconds": stale_seconds,
            "poll_interval_seconds": gpu.config.gpu.poll_interval_seconds,
            "detected": gpu.detected,
            # 当前被监控的 GPU uuid 集合（device_uuids；空 = 全部）。
            # 前端据此给 detected 里"检测到但未监控"的卡打标记，而不是直接隐藏
            # （检测列表是 Settings 勾选的数据来源，必须可见）。
            "gpu_uuids_monitored": list(gpu.config.gpu.device_uuids),
            "gpus": gpus,
            "processes": processes,
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
    async def api_gpu_live(
        minutes: int = Query(60, ge=1, le=2880),
        max_points: int = Query(2000, ge=100, le=2000),
    ) -> dict:
        """
        最近 N 分钟（1~2880）的 GPU 历史，按 UUID 分组。

        每个 GPU 最多返回 max_points 个点（默认 2000；超出时后端 bucket 降采样，
        纯 Python）。Round-4：max_points 可配（24h 页面用 ~1000 控制传输量；
        现有前端不传时行为与之前完全一致）。
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
            down = _downsample(clean, max_points=max_points)
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
        # Round-4：power_available = 该 GPU 任一自然日是否有真实功耗采样
        # （power_count > 0）。前端据此区分「今日能耗 0.x Wh（真实为 0）」与
        # 「不可估算（无功耗遥测）」——power 恒 N/A 的卡绝不显示 0Wh（§122-131）。
        for e in per_gpu.values():
            e["power_available"] = any((d["avg_power_w"] is not None) for d in e["days"])
        return {"gpus": list(per_gpu.values())}

    # ---------- 1.1 llama.cpp Runtime（Health / Model / Slot） ----------

    @app.get("/api/llama/info")
    async def api_llama_info(request: Request) -> dict:
        """
        llama.cpp 模型与服务信息（1.1）。

        - 只读端点；远程只读客户端的 model_path 只返回文件名（不泄漏完整 Windows 路径，
          与 /api/status 的 config.path 同一策略）；
        - 能力探测：该 llama.cpp 版本无 /props 或 /v1/models 时对应字段为 null
          （前端按"不可用"处理，不报错）；
        - chat_template / generation_prompt / media_marker 绝不返回（隐私边界）。
        """
        if runtime is None:
            return {"available": False, "model": None, "capabilities": {}}
        info = runtime.model_info
        model = None
        if info is not None:
            model = dict(info)
            # 完整路径只允许本机 Diagnostics 查看
            if model.get("model_path") and not _client_is_loopback(request):
                model["model_path"] = Path(model["model_path"]).name
        return {
            "available": info is not None,
            "model": model,
            "capabilities": dict(runtime.capabilities),
            "server_state": runtime.server_state,
            "last_update": runtime.last_health_update,
        }

    @app.get("/api/llama/slots")
    async def api_llama_slots() -> dict:
        """
        当前 Slot 运行时（1.1，只读白名单字段：状态/计数/采样参数）。

        - 隐私边界：prompt / generation_prompt / chat_template / 消息文本绝不返回；
        - 该版本无 /slots（501）时 available=false（前端显示"不可用"，不报错）；
        - cache_reuse_percent 仅当 n_prompt_tokens > 0 时计算
          （n_prompt_tokens_cache / n_prompt_tokens）——当前请求缓存复用率，
          与历史 Token 缓存复用率是不同指标。
        """
        if runtime is None:
            return {"available": False, "slots": []}
        slots = []
        for s in runtime.slots:
            entry = dict(s)
            n_prompt = s.get("n_prompt_tokens")
            n_cache = s.get("n_prompt_tokens_cache")
            entry["cache_reuse_percent"] = (
                round(n_cache / n_prompt * 100.0, 1)
                if n_prompt and n_cache is not None and n_prompt > 0
                else None
            )
            slots.append(entry)
        return {
            "available": bool(runtime.capabilities.get("slots")),
            "slots": slots,
            "last_update": runtime.last_slots_update,
        }

    # ---------- 1.1 System Telemetry（CPU/内存/磁盘/网络/功耗/传感器） ----------

    @app.get("/api/system/status")
    async def api_system_status() -> dict:
        """
        当前系统状态（1.1 实时摘要）。

        - null = 不可用（前端显示 --）；**绝不**把 None 变 0（尤其风扇/功耗/温度）；
        - monitored_components_w = 所有**可读取（非 null）**组件功耗之和（CPU Package +
          被监控 GPU）。1.1.4 精修：null != 0，缺失组件不参与求和；全缺失 -> null。
          **非**墙插整机功耗；
        - wall_power_w 恒为 null（1.1 无外部功率计；数据模型预留）。
        """
        if system is None:
            return {"available": False, "cpu": None, "memory": None, "disk": None,
                    "network": None, "power": None, "last_update": None}
        latest = system.latest
        if latest is None:
            return {"available": system.available, "cpu": None, "memory": None,
                    "disk": None, "network": None, "power": None,
                    "last_update": system.last_update}
        # 组件功耗：按**当前**可读取组件求和（cpu 取自样本，gpu 取 live），
        # 与返回的 cpu_package_w / gpu_total_w 同源一致（1.1.4 精修 §110-§114）。
        _pw_parts = [latest.cpu_package_power_w, system.gpu_power_total_w]
        _pw_present = [p for p in _pw_parts if p is not None]
        _monitored_w = float(sum(_pw_present)) if _pw_present else None
        _cpu_has = latest.cpu_package_power_w is not None
        _gpu_has = system.gpu_power_total_w is not None
        # 内存：Windows Available 语义（§69）：used = total - available（采集器已算），
        # available_bytes = total - used（回推，供 UI"可用"）。
        _mem_avail = None
        if latest.memory_used_bytes is not None and latest.memory_total_bytes is not None:
            _mem_avail = latest.memory_total_bytes - latest.memory_used_bytes
        return {
            "available": system.available,
            "cpu": {
                "usage_percent": latest.cpu_usage_percent,
                # 1.1.3：逐逻辑核利用率（live-only，Heat Grid 真 per-core 数据源）
                "per_core_percent": latest.cpu_per_core_percent,
                # Round-3（§11-§12/§63-§65）：current 与 base 区分（不再都叫"CPU 频率"）
                "frequency_mhz": latest.cpu_frequency_mhz,
                "base_frequency_mhz": latest.cpu_base_frequency_mhz,
                "temperature_c": latest.cpu_temperature_c,
                "package_power_w": latest.cpu_package_power_w,
            },
            "memory": {
                "used_bytes": latest.memory_used_bytes,
                "available_bytes": _mem_avail,
                "total_bytes": latest.memory_total_bytes,
                "usage_percent": latest.memory_usage_percent,
            },
            "disk": {
                "read_bps": latest.disk_read_bps,
                "write_bps": latest.disk_write_bps,
            },
            "network": {
                "rx_bps": latest.network_rx_bps,
                "tx_bps": latest.network_tx_bps,
                # Round-3（§98/§100）：当前速率对应的接口名；None=全接口合计（未识别默认接口）
                "interface": latest.network_interface,
                # 所有接口当前速率（接口选择器即时显示；含 errors/drops，§107-§108）
                "adapters": _system_adapters_public(system),
            },
            "power": {
                "monitored_components_w": _monitored_w,
                "cpu_package_w": latest.cpu_package_power_w,
                "gpu_total_w": system.gpu_power_total_w,
                "wall_power_w": None,  # 无外部测量源：恒 null（UI 显示"未配置"，不显示 --，§121）
                # 已读取组件数 + 缺失标记（§112-§113 部分数据提示 + §28 sub"X 个组件可读取"）
                "components_present": sum((_cpu_has, _gpu_has)),
                "cpu_package_available": _cpu_has,
                "gpu_available": _gpu_has,
            },
            "last_update": system.last_update,
        }

    @app.get("/api/system/live")
    async def api_system_live(minutes: int = Query(60, ge=1, le=1440),
                              max_points: int = Query(1500, ge=50, le=3000)) -> dict:
        """
        最近 N 分钟（1~1440）的系统采样（schema v5 system_samples）。
        超过 max_points 时后端 bucket 降采样（纯 Python；保留缺口不插 0）。

        Round-3 系统页（§46-§55/§185-§190）：
        - max_points 由前端按 range 选择（15m→~450 原始，1h→~720，6h/24h→~1200-1500），
          避免向前端推 24h×5s≈1.7 万点；
        - 附 summary（当前/平均/峰值）用于 chart header 分析摘要（§46-§48）；
        - 降采样按 _SYS_LIVE_FIELDS 聚合（曾漏字段导致 24h 全 None 的 bug 已修）。
        """
        if system is None:
            return {"points": [], "summary": {}}
        since = time.time() - minutes * 60
        rows = db.get_system_samples_since(since)
        points = [
            {k: r.get(k) for k in (
                "timestamp", "cpu_usage_percent", "cpu_frequency_mhz",
                "cpu_temperature_c", "cpu_package_power_w",
                "memory_usage_percent", "memory_used_bytes", "memory_total_bytes",
                "disk_read_bps", "disk_write_bps",
                "network_rx_bps", "network_tx_bps", "monitored_component_power_w",
            )}
            for r in rows
        ]
        return {"points": _downsample(points, max_points, _SYS_LIVE_FIELDS),
                "summary": _system_live_summary(points)}

    @app.get("/api/system/network-interfaces")
    async def api_system_network_interfaces() -> dict:
        """
        Round-3（§97-§102）：网络接口列表（供"接口选择器"）。
        - default_interface = 拥有默认路由的主接口（自动模式的选中项）；
        - interfaces = [{name, kind, is_default, is_virtual, speed_mbps, errin, errout, dropin, dropout}]；
        - 前端"自动" = default_interface；选"接口合计" = 全 NIC 求和（当前历史口径）。
        无 system 时返回空。
        """
        if system is None:
            return {"available": False, "default_interface": None, "interfaces": []}
        try:
            ifs = system.network_interfaces()
        except Exception:
            ifs = []
        default_name = None
        for itf in ifs:
            if itf.get("is_default"):
                default_name = itf.get("name")
                break
        return {
            "available": True,
            "default_interface": default_name,
            "interfaces": ifs,
        }

    @app.get("/api/system/daily")
    async def api_system_daily(days: int = Query(30, ge=1, le=365)) -> dict:
        """
        系统每日聚合（1.1 schema v5 system_daily）。
        avg = sum / count（count=0 -> null：只有有效采样的天才有均值）。
        """
        rows = db.get_system_daily(days)
        # "已监测组件今日能耗" = CPU（system_daily 列）+ GPU（gpu_daily，同窗口按日求和）。
        # system_daily.monitored_component_energy_wh 列只存 CPU 部分——GPU 能量由
        # gpu_daily 维护（采集器按 device_uuids 过滤后才落库，口径与 GPU 页每日能耗
        # 一致），组件合计按 design comment 在此 API 层相加。
        gpu_energy_by_date: dict[str, float] = {}
        for g in db.get_gpu_daily(days):
            if g["energy_wh"]:
                gpu_energy_by_date[g["date"]] = (
                    gpu_energy_by_date.get(g["date"], 0.0) + g["energy_wh"]
                )
        out = []
        for r in rows:
            out.append({
                "date": r["date"],
                "cpu_usage_avg": round(r["cpu_usage_sum"] / r["cpu_usage_count"], 2) if r["cpu_usage_count"] else None,
                "cpu_usage_max": r["cpu_usage_max"],
                "cpu_temp_avg": round(r["cpu_temp_sum"] / r["cpu_temp_count"], 1) if r["cpu_temp_count"] else None,
                "cpu_temp_max": r["cpu_temp_max"],
                "memory_usage_avg": round(r["memory_usage_sum"] / r["memory_usage_count"], 2) if r["memory_usage_count"] else None,
                "memory_usage_max": r["memory_usage_max"],
                "disk_read_bytes": r["disk_read_bytes"],
                "disk_write_bytes": r["disk_write_bytes"],
                "network_rx_bytes": r["network_rx_bytes"],
                "network_tx_bytes": r["network_tx_bytes"],
                "cpu_energy_wh": round(r["cpu_energy_wh"], 3),
                "monitored_component_energy_wh": round(
                    r["monitored_component_energy_wh"] + gpu_energy_by_date.get(r["date"], 0.0), 3
                ),
            })
        return {"days": out}

    @app.get("/api/system/inventory")
    async def api_system_inventory(request: Request) -> dict:
        """
        静态硬件库存（1.1）：OS / CPU / 主板 / BIOS / RAM / 磁盘 / BootTime。
        启动时读取一次；manual=true 时重新读取（手动刷新，低频 CIM 安全）。

        AUDIT-1.1.1 PERF-1111-003：CIM/PowerShell 子进程 10s+ 直接在事件循环里跑
        会卡死所有 HTTP 端点——放到 asyncio.to_thread；加 60s 节流防重复点击。
        """
        if system is None:
            return {"available": False, "inventory": {}}
        manual = request.query_params.get("manual", "").lower() == "true"
        if manual:
            now = time.time()
            if not hasattr(app.state, "_last_inventory_manual") or \
               now - app.state._last_inventory_manual >= 60.0:
                app.state._last_inventory_manual = now
                # CIM 子进程是阻塞的（10s+）；to_thread 不卡事件循环
                inv = await asyncio.to_thread(system.refresh_inventory)
                return {"available": True, "inventory": inv,
                        "refreshed_at": time.time(), "manual_refreshed": True}
            return {"available": True, "inventory": system.inventory,
                    "refreshed_at": time.time(), "manual_refreshed": False,
                    "throttled": True}
        return {"available": True, "inventory": system.inventory,
                "refreshed_at": time.time()}

    @app.get("/api/system/sensors")
    async def api_system_sensors() -> dict:
        """
        高级硬件传感器（1.1）：状态 + 传感器列表 + 风扇。

        - state: available / partial / unavailable（Provider 状态机）；
        - fans：[{name, rpm, control_percent, source}]——control_percent 只有 Provider
          真实提供 Control 传感器时才有值（绝不从 RPM 推算）；
        - counts：分类计数（CPU / 主板 / 散热 / 存储）——Settings 页面显示。
        """
        if sensors is None:
            return {"available": False, "state": "unavailable", "sensors": [],
                    "fans": [], "counts": {}, "last_update": None}
        snap = sensors.snapshot()
        # 传感器列表按 (hardware_type, sensor_type) 去重计数（Settings 分类列表用）
        grouped: dict[str, dict] = {}
        for s in snap["sensors"]:
            key = f"{s['hardware_type']}/{s['sensor_type']}"
            g = grouped.setdefault(key, {
                "hardware_type": s["hardware_type"], "sensor_type": s["sensor_type"],
                "names": set(), "values": [],
            })
            g["names"].add(s["sensor_name"])
            g["values"].append(s["value"])
        groups = [
            {
                "hardware_type": g["hardware_type"], "sensor_type": g["sensor_type"],
                "names": sorted(g["names"])[:8], "count": len(g["values"]),
                "latest": round(sum(g["values"]) / len(g["values"]), 1),
            }
            for g in grouped.values()
        ]
        return {
            "available": True,
            "state": snap["state"],
            "sensors": groups,
            "fans": snap["fans"],
            "counts": snap["counts"],
            "cpu_temperature_c": snap["cpu_temperature_c"],
            "cpu_package_power_w": snap["cpu_package_power_w"],
            "last_update": sensors.last_bridge_update,
        }

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
    async def api_data_info(request: Request) -> dict:
        """
        存储信息：数据库路径/大小、WAL 大小、记录日期范围、行数、备份数/总大小、
        数据库健康（Phase 11：health / journal_mode / 最近成功自动备份）。

        数据库文件尚不存在（尚未有成功采集）时返回 0 / null，不报 500。
        远程只读客户端：database_path / last_auto_backup.path 只返回文件名
        （不泄漏 Windows 用户名 / %LOCALAPPDATA% 目录结构）。
        """
        p = Path(db.path)
        backups = backup_mgr.list_backups()
        backup_count = len(backups)
        backup_total_size = sum(b["size"] for b in backups)
        wal_p = Path(str(p) + "-wal")
        wal_size = wal_p.stat().st_size if wal_p.is_file() else 0
        last_auto = db.get_last_backup("automatic") if p.is_file() else None
        base = {
            "database_path": _expose_path(request, str(db.path)),
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
                {"path": _expose_path(request, last_auto["path"]),
                 "timestamp": last_auto["timestamp"]}
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
    async def api_export_daily_csv(start_date: str | None = None,
                                   end_date: str | None = None) -> Response:
        """
        导出 daily_usage 为 CSV：UTF-8 with BOM（Windows Excel 直接打开不乱码），
        数字为原始整数（不做 1.2M 缩写），日期 YYYY-MM-DD。

        1.1.2：支持 start_date / end_date（'YYYY-MM-DD'，服务器本机日历日）
        按当前查看范围导出（使用页"导出 CSV"按钮传参）；不传 = 全部历史。
        新增 reuse_rate_percent 列 = 缓存复用 Token / (Prompt + 缓存复用) * 100
        （分母为 0 时为空；术语与页面一致——缓存复用率）。
        """
        if (start_date is None) != (end_date is None):
            raise HTTPException(status_code=400, detail="start_date 与 end_date 必须同时提供")
        s_s = _parse_calendar_date(start_date)
        e_s = _parse_calendar_date(end_date)
        if s_s is not None and (e_s is None or s_s > e_s):
            raise HTTPException(status_code=400, detail="start_date / end_date 非法")
        rows = db.get_daily_usage() if Path(db.path).is_file() else []
        if s_s:
            rows = [r for r in rows if s_s <= r["date"] <= e_s]
        # AUDIT-DB-003：同 /api/daily——live 样本一次取全按日分组（避免逐日全量扫）
        try:
            all_live = db.get_live_samples(hours=None)
        except Exception:
            all_live = []
        # AUDIT-1.1.1 PERF-1111-013：DB 读取在事件循环线程（快），CSV 构建
        # （逐行 _day_quality 的 gap_stats 聚合查询，数百~数千行）放 to_thread
        body = await asyncio.to_thread(_build_daily_csv_body, rows, all_live)
        filename = "LlamaMonitor_daily_" + time.strftime("%Y%m%d_%H%M%S") + ".csv"
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

    # ---------- Round-5 History 页：采集缺口 / 监控事件 CSV 导出 ----------
    # Gap 来源/原因/风险的中英文标签（导出用；与前端展示层同一语义）。
    _GAP_SOURCE_CSV = {"llama": "llama.cpp", "gpu": "GPU", "application": "LlamaMonitor",
                       "system": "系统"}
    _GAP_REASON_CSV = {
        "server_offline": "llama.cpp 服务不可达",
        "monitor_restart": "LlamaMonitor 重启",
        "system_pause_or_sleep": "系统休眠",
        "invalid_metrics": "采集超时/异常",
        "unknown": "原因未确定",
    }
    _GAP_RISK_CSV = {
        # token_recoverable + possible_token_loss -> 风险级别
        (0, 1): "可能丢失",
        (0, 0): "时间归属不确定",
        (1, 0): "无",
        (1, 1): "可能丢失",
    }

    def _gap_risk_csv(recov: int, lost: int) -> str:
        return _GAP_RISK_CSV.get((bool(recov), bool(lost)),
                                 "可能丢失" if lost else ("时间归属不确定" if not recov else "无"))

    @app.get("/api/data/export/gaps.csv")
    async def api_export_gaps_csv(
        preset: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        source: str | None = None,
        risk: str | None = None,
    ) -> Response:
        """
        采集缺口 CSV（UTF-8 BOM，Excel 直接打开）。遵守当前时间范围 + 来源/风险筛选
        （§199-205）。分页拉取全部范围内缺口（cursor 循环，避免单次物化整表）。
        文件名 LlamaMonitor-Gaps-<start>_to_<end>.csv（§205）。
        """
        start_ts, end_ts = _history_range_ts(preset, start_date, end_date)
        if source not in (None, "llama", "gpu", "application", "system"):
            source = None
        if risk not in (None, "lost", "time_uncertain"):
            risk = None
        all_rows: list[dict] = []
        cursor = None
        while True:
            rows = db.get_gaps_range(start_ts=start_ts, end_ts=end_ts, source=source,
                                     risk=risk, cursor=cursor, limit=200)
            all_rows.extend(rows)
            if not rows or len(rows) < 200:
                break
            cursor = (rows[-1]["start_timestamp"], rows[-1]["id"])
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["start_time", "end_time", "duration_seconds", "source",
                         "reason", "token_risk", "token_loss_possible"])
        for g in all_rows:
            def _iso(ts):
                try:
                    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    return ""
            writer.writerow([
                _iso(g["start_timestamp"]),
                _iso(g["end_timestamp"]),
                round(float(g["duration_seconds"]), 2),
                _GAP_SOURCE_CSV.get(g["source"], g["source"] or ""),
                _GAP_REASON_CSV.get(g["reason"], g["reason"] or "原因未确定"),
                _gap_risk_csv(int(g["token_recoverable"]), int(g["possible_token_loss"])),
                "yes" if g["possible_token_loss"] else "no",
            ])
        s_iso = datetime.fromtimestamp(int(start_ts)).strftime("%Y-%m-%d") if start_ts else "all"
        e_iso = datetime.fromtimestamp(int(end_ts)).strftime("%Y-%m-%d")
        filename = f"LlamaMonitor-Gaps-{s_iso}_to_{e_iso}.csv"
        body = ("\ufeff" + buf.getvalue()).encode("utf-8")
        logger.info("Gaps CSV exported: %s (%d rows)", filename, len(all_rows))
        return Response(
            content=body,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/data/export/events.csv")
    async def api_export_events_csv(
        preset: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        category: str | None = None,
        search: str | None = None,
    ) -> Response:
        """
        监控事件 CSV（UTF-8 BOM）。遵守当前时间范围 + 分类/搜索筛选（§206-210）。
        分页拉取全部范围内事件。字段 timestamp/category/event_type/display_title/
        source/detail/severity/raw_event_type（§208）。
        """
        start_ts, end_ts = _history_range_ts(preset, start_date, end_date)
        cat_types = _EVENT_CATEGORY_TYPES.get(category) if category else None
        search = (search or "").strip() or None
        title_types = _event_title_match_types(search) if search else None
        all_rows: list[dict] = []
        cursor = None
        while True:
            rows = db.get_events_range(start_ts=start_ts, end_ts=end_ts, category=cat_types,
                                       search=search, cursor=cursor, limit=200,
                                       title_match_types=title_types)
            all_rows.extend(rows)
            if not rows or len(rows) < 200:
                break
            cursor = (rows[-1]["timestamp"], rows[-1]["id"])
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["timestamp", "category", "event_type", "display_title",
                         "source", "detail", "severity", "raw_event_type"])
        for r in all_rows:
            et = r["event_type"]
            pres = EVENT_PRESENTATION.get(et)
            title = pres[0] if pres else "未知事件"
            cat = pres[1] if pres else "其它"
            sev = pres[2] if (pres and pres[2] is not None) else (r.get("severity") or "info")
            try:
                detail = json.dumps(r.get("details") or {}, ensure_ascii=False)
            except Exception:
                detail = str(r.get("details") or "")
            def _iso(ts):
                try:
                    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    return ""
            writer.writerow([
                _iso(r["timestamp"]), cat, title, r.get("source") or "",
                detail, sev, et,
            ])
        s_iso = datetime.fromtimestamp(int(start_ts)).strftime("%Y-%m-%d") if start_ts else "all"
        e_iso = datetime.fromtimestamp(int(end_ts)).strftime("%Y-%m-%d")
        filename = f"LlamaMonitor-Events-{s_iso}_to_{e_iso}.csv"
        body = ("\ufeff" + buf.getvalue()).encode("utf-8")
        logger.info("Events CSV exported: %s (%d rows)", filename, len(all_rows))
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

    def _schema_status_from_health() -> str:
        """
        History 页数据库状态模型（§240-262）：把 db.health 映射为 4 态展示语义。
        - healthy      -> normal          （版本匹配或已迁移，可读写）
        - incompatible -> readonly_compat （DB schema 比 app 新，只读打开，§241-247）
        - warning      -> readonly        （降级/保护态，只读）
        - corrupt/unavailable -> abnormal （损坏/不可用，§249-250）
        WAL 只作 Secondary，不改变该 4 态。
        """
        return {
            "healthy": "normal",
            "incompatible": "readonly_compat",
            "warning": "readonly",
            "corrupt": "abnormal",
            "unavailable": "abnormal",
        }.get(db.health, "normal")

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
            # Round-5：History 页数据库状态模型（§240-262）。
            # schema_status = 数据库健康态 + schema 版本关系的最终展示语义：
            #   normal          正常（版本匹配或已迁移）
            #   readonly_compat 只读兼容模式（DB schema 比当前 app 新，只读打开）
            #   readonly        只读（文件系统只读等保护态）
            #   abnormal        异常（损坏 / 不可用 / 警告）
            "schema_status": _schema_status_from_health(),
            "db_schema_version": db.get_schema_version(),
            "app_schema_version": CURRENT_SCHEMA_VERSION,
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

    # ---------- Round-6 Overview 页：只读聚合端点（§247-§283） ----------
    @app.get("/api/overview")
    async def api_overview(request: Request) -> dict:
        """
        Overview 页只读聚合端点：一次请求返回全部域的**摘要**（不含图表/明细/路径/键）。

        各域独立可用：available / sample_timestamp / stale 逐域给出；前端按域隔离
        渲染与错误处理（单域失败只影响该 Section）。attention 聚合器只消费各模块
        已产出的可靠状态（不重新发明健康判断，§254-§256）。
        """
        now = collector.clock.now()
        today_str = local_date(now)

        # ---- service（llama-server 健康 + 指标采集） ----
        snap = collector.last_snapshot
        online = None
        if snap is not None:
            online = bool(snap.get("online"))
        server_state = runtime.server_state if runtime is not None else None
        if online is True:
            service_status = "ready"  # 就绪
        elif online is False:
            # 健康检查仍正常但指标端点不可达 = 监测异常；健康也失败 = 不可达
            service_status = (
                "monitoring_error" if server_state in ("ready", "loading")
                else "unreachable"
            )
        else:
            service_status = "detecting"  # 首轮采集未完成
        model_info = runtime.model_info if runtime is not None else None
        service = {
            "status": service_status,
            "online": online,
            "server_state": server_state,
            "address": _base_address_public(collector.metrics_url),
            "model": _model_summary_public(runtime, request) if runtime is not None else None,
            "last_update": collector.last_update,
            "available": online is not None,
        }
        # 上下文窗口：运行时 context_max 优先，回退模型配置 n_ctx（/props 或 /v1/models）
        ctx = None
        if online and snap is not None:
            ctx = snap.get("context_max")
        if ctx is None and model_info:
            ctx = model_info.get("context_size")
        service["context_window"] = ctx
        # 静态模型元数据（上下文/Slot/模态）——内存态，无 I/O，无需额外缓存

        # ---- usage_today（daily_usage 按本机日历日；与 /api/summary 同源） ----
        rows = db.get_daily_usage()
        today_row = next((r for r in rows if r["date"] == today_str), None) or {}
        usage = _summary_shape(
            (today_row.get("prompt_tokens", 0) or 0),
            (today_row.get("cached_tokens", 0) or 0),
            (today_row.get("output_tokens", 0) or 0),
        )
        denom = usage["prompt_tokens"] + usage["cached_tokens"]
        usage["cache_reuse_rate_percent"] = (
            round(usage["cached_tokens"] / denom * 100.0, 1) if denom > 0 else None
        )  # 分母 0 -> None（UI 显示 --，不显示 0%）
        schema_status = _schema_status_from_health()
        usage["db_status"] = schema_status
        usage["available"] = True
        usage["sample_timestamp"] = now

        # ---- inference（最新 live 样本 + runtime slots + 当日 MTP） ----
        latest = db.get_latest_live_sample() if online else None
        # TPS 三态（绝不混淆 0 与不可用）：counter 存在但本轮无 delta = 空闲 -> 0 t/s
        prompt_disp = _overview_tps_display(
            (latest or {}).get("prompt_tps"), bool(online),
            (latest or {}).get("prompt_delta") is not None,
        )
        decode_disp = _overview_tps_display(
            (latest or {}).get("decode_tps"), bool(online),
            (latest or {}).get("output_delta") is not None,
        )
        # MTP 三态：未启用 / 暂无数据 / 百分比
        mtp_cap = bool(collector.capabilities.get("mtp"))
        slots_spec = False
        if runtime is not None:
            for s in runtime.slots:
                if s.get("speculative"):
                    slots_spec = True
                    break
        mtp_enabled = mtp_cap or slots_spec
        draft = (today_row.get("draft_tokens", 0) or 0)
        accepted = (today_row.get("accepted_tokens", 0) or 0)
        if not online:
            mtp_state = "unavailable"
        elif not mtp_enabled:
            mtp_state = "disabled"
        elif draft <= 0:
            mtp_state = "no_data"
        else:
            mtp_state = "value"
        mtp = {
            "state": mtp_state,
            "accept_rate": (accepted / draft * 100.0) if (mtp_state == "value" and draft > 0) else None,
        }
        # 当前上下文（§85-§91 优先级：可靠 Slot 占用 > Busy Slot > 省略；
        # 用 n_prompt_tokens + n_decoded，不用 n_prompt_tokens_processed 冒充）
        current_context = None
        if runtime is not None and runtime.capabilities.get("slots") and runtime.slots:
            active = [s for s in runtime.slots if s.get("is_processing")]
            busy = len(active)
            total_slots = (model_info or {}).get("total_slots")
            if len(active) == 1:
                s = active[0]
                np_ = s.get("n_prompt_tokens")
                nd = s.get("n_decoded")
                limit = s.get("n_ctx")
                if np_ is not None and nd is not None and limit:
                    current_context = {
                        "display": "usage", "used": np_ + nd, "limit": limit,
                        "busy_slots": 1, "total_slots": total_slots,
                    }
            if current_context is None:
                current_context = {
                    "display": "busy", "busy_slots": busy, "total_slots": total_slots,
                }
        inference = {
            "online": online,
            "prompt_tps": prompt_disp,
            "decode_tps": decode_disp,
            "requests_processing": (snap or {}).get("requests_processing") if online else None,
            "requests_deferred": (snap or {}).get("requests_deferred") if online else None,
            "mtp": mtp,
            "current_context": current_context,
            "available": online is not None,
            "sample_timestamp": (latest or {}).get("timestamp") if latest else None,
        }

        # ---- system（主机遥测；与 /api/system/status 同源数据） ----
        sys_payload: dict = {"available": False, "sample_timestamp": None}
        if system is not None:
            sys_payload["sample_timestamp"] = system.last_update
            sys_payload["available"] = bool(system.available)
            if system.latest is not None:
                L = system.latest
                _pw_parts = [L.cpu_package_power_w, system.gpu_power_total_w]
                _pw_present = [p for p in _pw_parts if p is not None]
                sys_payload["cpu"] = {
                    "usage_percent": L.cpu_usage_percent,
                    "temperature_c": L.cpu_temperature_c,
                }
                sys_payload["memory"] = {
                    "usage_percent": L.memory_usage_percent,
                    "used_bytes": L.memory_used_bytes,
                    "total_bytes": L.memory_total_bytes,
                }
                sys_payload["disk"] = {"read_bps": L.disk_read_bps, "write_bps": L.disk_write_bps}
                sys_payload["network"] = {"rx_bps": L.network_rx_bps, "tx_bps": L.network_tx_bps}
                sys_payload["power"] = {
                    "monitored_components_w": float(sum(_pw_present)) if _pw_present else None,
                    "components_present": len(_pw_present),
                    "cpu_package_available": L.cpu_package_power_w is not None,
                    "gpu_available": system.gpu_power_total_w is not None,
                }
            boot = (system.inventory or {}).get("boot_time")
            sys_payload["uptime_seconds"] = (
                int(now - boot) if boot and now - boot > 0 else None
            )

        # ---- gpus（与 /api/gpu/status 同源；每卡仅 4 项摘要） ----
        gpu_payload = _gpu_status_payload()
        gpus_out = []
        for g in gpu_payload.get("gpus", []):
            name = g.get("name") or ""
            gpus_out.append({
                "index": g.get("index"),
                "name": name,
                "name_short": name.replace("NVIDIA ", "", 1) if name else None,
                "utilization_percent": g.get("utilization_percent"),
                "memory_used_mb": g.get("memory_used_mb"),
                "memory_total_mb": g.get("memory_total_mb"),
                "temperature_c": g.get("temperature_c"),
                "power_draw_w": g.get("power_draw_w"),
                "power_limit_w": g.get("power_limit_w"),
                "throttle_reasons": g.get("throttle_reasons") or [],
                "ecc_uncorrected_volatile": (g.get("ecc") or {}).get("uncorrected_volatile"),
            })
        gpus = {
            "available": bool(gpu_payload.get("available")),
            "reason": gpu_payload.get("reason"),
            "count": len(gpus_out),
            "gpus": gpus_out,
            "sample_timestamp": gpu_payload.get("last_update"),
            "stale_seconds": gpu_payload.get("stale_seconds"),
        }

        # ---- integrity（今日窗口；与 /api/data/quality + /api/health 同源） ----
        today_first, today_last = db.get_day_sample_bounds(today_str)
        today_gap = db.get_gap_stats(today_str)
        coverage = None
        if today_first is not None:
            window_end = today_last if today_last is not None else now
            window = max(0.0, window_end - today_first)
            in_window = min(today_gap["total_gap_seconds"], window) if window > 0 else 0.0
            coverage = 100.0 if window <= 0 else round(max(0.0, 100.0 * (1.0 - in_window / window)), 2)
        last_valid = db.get_latest_live_sample()
        last_valid_ts = (last_valid or {}).get("timestamp") if last_valid else None
        collector_last = collector._last_valid[0] if collector._last_valid else None
        valid_ts = max(t for t in (collector_last, last_valid_ts, today_last) if t is not None) \
            if any(t is not None for t in (collector_last, last_valid_ts, today_last)) else None
        last_age = (now - valid_ts) if valid_ts is not None else None
        if today_gap["possible_token_loss"]:
            token_risk = "lost"
        elif not today_gap["token_recoverable"]:
            token_risk = "time_uncertain"
        else:
            token_risk = "none"
        db_v = db.get_schema_version()
        if schema_status == "readonly_compat":
            db_secondary = f"Schema v{db_v} · 当前版本支持至 v{CURRENT_SCHEMA_VERSION}"
        elif schema_status == "abnormal":
            db_secondary = db.health_detail or db.health
        elif schema_status == "readonly":
            db_secondary = "保护模式（只读）"
        else:
            # 1.2.1：正常态不再展示「WAL 已启用」等开发者向细节（WAL 状态仍可在
            # 设置页数据库区查看）
            db_secondary = ""
        integrity = {
            "coverage_percent": coverage,
            "gap_count_today": today_gap["gap_count"] + (1 if collector.open_gap_property() else 0),
            "possible_token_loss": bool(today_gap["possible_token_loss"]),
            "token_risk": token_risk,
            "db_status": schema_status,
            "db_secondary": db_secondary,
            "last_sample_ts": valid_ts,
            "last_sample_seconds_ago": round(last_age, 1) if last_age is not None else None,
            "available": True,
        }

        # ---- attention（聚合各域可靠状态；排序 + 截断） ----
        items: list[dict] = []
        if service_status == "unreachable":
            items.append({
                "key": "service_unreachable", "severity": "error",
                "title": "llama.cpp 服务不可达",
                "subtitle": "实时推理与 Token 采集暂停，历史数据已保留",
            })
        elif service_status == "monitoring_error":
            items.append({
                "key": "service_metrics", "severity": "warning",
                "title": "llama.cpp 指标采集异常",
                "subtitle": "服务健康检查正常，但指标端点暂不可达",
            })
        if schema_status == "readonly_compat":
            items.append({
                "key": "db_readonly_compat", "severity": "warning",
                "title": "数据库处于只读兼容模式",
                "subtitle": f"Schema v{db_v} · 当前版本支持至 v{CURRENT_SCHEMA_VERSION}（统计暂停累计，数据不会丢失）",
            })
        elif schema_status == "readonly":
            items.append({
                "key": "db_readonly", "severity": "warning",
                "title": "数据库处于只读模式",
                "subtitle": "保护模式：只读展示，写入暂停",
            })
        elif schema_status == "abnormal":
            items.append({
                "key": "db_abnormal", "severity": "error",
                "title": "数据库状态异常",
                "subtitle": db.health_detail or db.health,
            })
        stale_threshold = max(30.0, collector.interval * 3)
        if last_age is not None and last_age > stale_threshold:
            items.append({
                "key": "stale", "severity": "warning",
                "title": "实时采样已过期",
                "subtitle": "最近有效采样距今超过预期间隔",
            })
        if today_gap["possible_token_loss"]:
            items.append({
                "key": "token_loss", "severity": "warning",
                "title": "今日可能存在 Token 丢失",
                "subtitle": "存在可能导致 Token 丢失的采集缺口",
            })
        if coverage is not None and coverage < 99.9:
            items.append({
                "key": "coverage", "severity": "info",
                "title": "今日采集覆盖率未达 100%",
                "subtitle": f"{integrity['gap_count_today']} 个已知缺口",
            })
        if gpu is not None and not gpu.available:
            items.append({
                "key": "gpu_unavailable", "severity": "warning",
                "title": "GPU 采集异常",
                "subtitle": gpu_payload.get("reason") or "nvidia-smi 不可用",
            })
        if system is not None and not system.available:
            items.append({
                "key": "system_unavailable", "severity": "warning",
                "title": "系统遥测采集异常",
                "subtitle": "CPU / 内存 / 磁盘 / 网络指标暂不可读",
            })
        for g in gpus_out:
            if g["throttle_reasons"]:
                items.append({
                    "key": f"gpu_throttle_{g['index']}", "severity": "info",
                    "title": f"GPU {g['index']} 性能受限",
                    "subtitle": "、".join(g["throttle_reasons"]),
                })
                break
        for g in gpus_out:
            if (g["ecc_uncorrected_volatile"] or 0) > 0:
                items.append({
                    "key": f"gpu_ecc_{g['index']}", "severity": "warning",
                    "title": f"GPU {g['index']} 出现 ECC 不可纠正错误",
                    "subtitle": f"{g['ecc_uncorrected_volatile']} 个（波动值）",
                })
                break
        # 严重磁盘空间（静态库存，取容量最大盘）
        disk_list = (system.inventory or {}).get("disk_list") if system is not None else []
        if disk_list:
            main = max(disk_list, key=lambda d: d.get("total_bytes") or 0)
            if main.get("total_bytes") and main.get("used_bytes") is not None:
                du = main["used_bytes"] / main["total_bytes"] * 100.0
                if du > 90.0:
                    items.append({
                        "key": "disk_severe", "severity": "warning",
                        "title": "磁盘空间紧张",
                        "subtitle": f"{main.get('mountpoint') or main.get('device')} 已用 {du:.0f}%",
                    })

        return {
            "service": service,
            "usage_today": usage,
            "inference": inference,
            "system": sys_payload,
            "gpus": gpus,
            "integrity": integrity,
            "attention": build_attention_items(items),
            "timestamps": {"now": now},
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
        today_gap = db.get_gap_stats(today)
        # 覆盖率：窗口 = 当天首样本 -> 当天末样本（或现在，若当天仍在监控）
        coverage = None
        if today_first is not None:
            window_end = today_last if today_last is not None else now
            window = max(0.0, window_end - today_first)
            in_window = min(today_gap["total_gap_seconds"], window) if window > 0 else 0.0
            coverage = 100.0 if window <= 0 else max(0.0, 100.0 * (1.0 - in_window / window))
            coverage = round(coverage, 2)
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
        # AUDIT-1.1.1 PERF-1111-004：total 统计走 SQL 聚合，不再物化最多 10 万行
        total_gap_count, total_possible_loss = db.get_gap_totals()
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
                "gap_count": total_gap_count,
                "possible_token_loss": total_possible_loss,
            },
            "last_gap": last_gap,
            "open_gap": open_gap,
            "recent_gaps": [_gap_view(g) for g in db.get_gaps(limit=20)],
        }

    # ---------- Round-5 History 页：监测完整性 Summary + 完整性趋势 ----------
    @app.get("/api/history/summary")
    async def api_history_summary(
        preset: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict:
        """
        监测完整性 Summary（随时间范围变化）：
        - 采集覆盖率 = Σ(有效秒) / Σ(预期秒) × 100（范围内加权，**不是**逐日算术平均，
          §52-54；监测开始前 / 未来时段不计入预期，§57-59）。
        - 范围内缺口 / 累计缺口 / Token 数据风险（可能丢失 / 时间归属不确定）。
        - 最后采样（实时）+ 监测开始时间。
        数据库状态 / WAL 不受范围影响（前端另取 /api/health）。
        """
        start_ts, end_ts, _label = _history_range(preset, start_date, end_date)
        now = collector.clock.now()
        today = local_date(now)
        # 一次性取全 live（48h 保留，~数万行）按日分组（AUDIT-DB-003 同法）
        try:
            all_live = db.get_live_samples(hours=None)
        except Exception:
            all_live = []
        live_by_date: dict[str, list] = {}
        for s in all_live:
            live_by_date.setdefault(local_date(s["timestamp"]), []).append(s)
        # 逐自然日累计窗口（与 _day_quality 同源口径），再与范围求交
        total_window = 0.0
        total_valid = 0.0
        d = datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d")
        end_date_s = local_date(end_ts)
        while d <= end_date_s:
            window, valid, _full = _coverage_window_for_day(
                d, live_by_date.get(d, []), today, now, start_ts, end_ts)
            total_window += window
            total_valid += valid
            d = (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        coverage = round(max(0.0, 100.0 * total_valid / total_window), 2) if total_window > 0 else None
        # 范围内缺口聚合 + 全量累计
        range_stats = db.get_gap_range_stats(start_ts=start_ts, end_ts=end_ts)
        total_count, _ = db.get_gap_totals()
        last_ts = db.get_live_bounds()[1]
        last_age = (now - last_ts) if last_ts is not None else None
        return {
            "coverage_percent": coverage,
            "eligible_seconds": round(total_window, 1),
            "valid_seconds": round(total_valid, 1),
            "gap_count_range": range_stats["gap_count"],
            "gap_count_total": total_count,
            "lost_count": range_stats["lost_count"],
            "time_uncertain_count": range_stats["time_uncertain_count"],
            "total_gap_seconds_range": round(range_stats["total_gap_seconds"], 1),
            "last_sample_ts": last_ts,
            "last_sample_seconds_ago": round(last_age, 1) if last_age is not None else None,
            "monitoring_start_ts": db.get_live_bounds()[0],
            "server_now": now,
        }

    @app.get("/api/history/trend")
    async def api_history_trend(
        preset: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict:
        """
        完整性趋势（桶化）：采集覆盖率 0-100% 主序列 + 每桶缺口计数（小标记，非双 Y 轴，§70-71）。
        分桶：24h=按小时；7d/30d=按日；>90d=按周；非常长=按月（§65-68）。
        桶 coverage = 有效秒 / 预期秒；监测前 / 无窗口 -> null（tooltip 显示"未开始监测"，§74）；
        有预期但 0 有效 -> 0%（§76）。tooltip 给 有效/预期秒 + 缺口数 + 可能丢失。
        """
        start_ts, end_ts, _label = _history_range(preset, start_date, end_date)
        now = collector.clock.now()
        today = local_date(now)
        try:
            all_live = db.get_live_samples(hours=None)
        except Exception:
            all_live = []
        live_by_date: dict[str, list] = {}
        for s in all_live:
            live_by_date.setdefault(local_date(s["timestamp"]), []).append(s)
        span = end_ts - start_ts
        # 分桶粒度（§65-68，按 preset 语义而非裸时长）：
        #   24h -> 按小时；7d/30d -> 按日；
        #   all -> ≤90d 按日 / ≤1yr 按周 / 更长按月。
        #   custom -> 按时长（≤2d 按小时 / ≤92d 按日 / ≤370d 按周 / 更长按月）。
        if preset == "24h":
            bucket = "hour"
        elif preset in ("7d", "30d"):
            bucket = "day"
        elif preset == "all":
            if span <= 92 * 86400:
                bucket = "day"
            elif span <= 370 * 86400:
                bucket = "week"
            else:
                bucket = "month"
        else:
            if span <= 2 * 86400:
                bucket = "hour"
            elif span <= 92 * 86400:
                bucket = "day"
            elif span <= 370 * 86400:
                bucket = "week"
            else:
                bucket = "month"
        # 逐日窗口（日粒度），再按桶聚合
        daily: list[dict] = []
        d = datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d")
        end_date_s = local_date(end_ts)
        while d <= end_date_s:
            window, valid, _full = _coverage_window_for_day(
                d, live_by_date.get(d, []), today, now, start_ts, end_ts)
            gs = db.get_gap_stats(d)
            daily.append({"date": d, "window": window, "valid": valid,
                          "gap_count": gs["gap_count"],
                          "lost": 1 if gs["possible_token_loss"] else 0,
                          "time_uncertain": 0})  # 日粒度 time_uncertain 由范围聚合提供
            d = (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        # 聚合到桶
        out: list[dict] = []
        if bucket == "hour":
            # 按小时：只有今天/昨天有 live（48h 保留），其余桶窗口 0 -> null
            cur = datetime.fromtimestamp(start_ts)
            cur = cur.replace(minute=0, second=0, microsecond=0)
            while cur.timestamp() < end_ts:
                b_start = cur.timestamp()
                b_end = b_start + 3600.0
                day = cur.strftime("%Y-%m-%d")
                ds = live_by_date.get(day, [])
                # 该小时窗口 = [b_start, min(b_end, 末样本, now)] ∩ 有样本
                in_hour = [s for s in ds if b_start <= s["timestamp"] < b_end]
                window = 0.0
                valid = 0.0
                hour_gaps = db.get_gaps_range(start_ts=b_start, end_ts=b_end, limit=500)
                gc = len(hour_gaps)
                lost = any(g["possible_token_loss"] for g in hour_gaps)
                if in_hour:
                    first = in_hour[0]["timestamp"]
                    last = in_hour[-1]["timestamp"]
                    w_end = min(b_end, last + collector.interval * 2, now)
                    if w_end > first:
                        window = w_end - first
                        # 本小时内的实际缺口秒（各 gap 与本小时窗口求交，累加）
                        in_w = 0.0
                        for g in hour_gaps:
                            ov = min(float(g["end_timestamp"]), w_end) - max(float(g["start_timestamp"]), first)
                            in_w += max(0.0, ov)
                        valid = max(0.0, window - in_w)
                out.append({
                    "ts": int(b_start),
                    "ts_end": int(b_start + 3600),
                    "label": cur.strftime("%H:%M") if day == today else day + " " + cur.strftime("%H:%M"),
                    "coverage": round(100.0 * valid / window, 2) if window > 0 else None,
                    "valid_seconds": round(valid, 1),
                    "eligible_seconds": round(window, 1),
                    "gap_count": gc,
                    "possible_token_loss": lost,
                    "is_today": day == today,
                })
                cur = (cur + timedelta(hours=1))
        else:
            # 按日/周/月：把 daily 聚合到桶
            def _bucket_key(day: str) -> tuple:
                dt = datetime.strptime(day, "%Y-%m-%d")
                if bucket == "day":
                    return day
                if bucket == "week":
                    # ISO 周起始（周一）
                    monday = dt - timedelta(days=dt.weekday())
                    return monday.strftime("%Y-%m-%d")
                return dt.strftime("%Y-%m")
            buckets: dict[str, dict] = {}
            order: list[str] = []
            for row in daily:
                k = _bucket_key(row["date"])
                if k not in buckets:
                    buckets[k] = {"window": 0.0, "valid": 0.0, "gap_count": 0, "lost": 0}
                    order.append(k)
                buckets[k]["window"] += row["window"]
                buckets[k]["valid"] += row["valid"]
                buckets[k]["gap_count"] += row["gap_count"]
                buckets[k]["lost"] += row["lost"]
            # 桶大小：day=1天, week=7天, month=到下月1号
            def _bucket_end_ts(k: str) -> int:
                dt = datetime.strptime(k, "%Y-%m-%d")
                if bucket == "day":
                    return int((dt + timedelta(days=1)).timestamp())
                if bucket == "week":
                    return int((dt + timedelta(days=7)).timestamp())
                # month
                if dt.month == 12:
                    nxt = dt.replace(year=dt.year + 1, month=1)
                else:
                    nxt = dt.replace(month=dt.month + 1)
                return int(nxt.timestamp())
            for k in order:
                b = buckets[k]
                out.append({
                    "ts": int(datetime.strptime(k, "%Y-%m-%d").timestamp()),
                    "ts_end": _bucket_end_ts(k),
                    "label": k,
                    "coverage": round(100.0 * b["valid"] / b["window"], 2) if b["window"] > 0 else None,
                    "valid_seconds": round(b["valid"], 1),
                    "eligible_seconds": round(b["window"], 1),
                    "gap_count": b["gap_count"],
                    "possible_token_loss": b["lost"] > 0,
                    "is_today": k == today,
                })
        return {"bucket": bucket, "points": out}

    # ---------- Round-5 History 页：采集缺口（范围 + 筛选 + cursor 分页） ----------
    def _history_range_ts(
        preset: str | None, start_date: str | None, end_date: str | None
    ) -> tuple[float | None, float | None]:
        """History 范围 -> (start_ts, end_ts)；preset=all 时 start_ts=None（不限下界）。"""
        now = collector.clock.now()
        start_ts, end_ts, label = _history_range(preset, start_date, end_date)
        if label == "all":
            start_ts = None
        return start_ts, end_ts

    @app.get("/api/history/gaps")
    async def api_history_gaps(
        preset: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        source: str | None = None,
        risk: str | None = None,
        cursor: str | None = None,
        limit: int = Query(20, ge=1, le=200),
        sub_start_ts: int | None = None,
        sub_end_ts: int | None = None,
    ) -> dict:
        """
        采集缺口列表（时间倒序，cursor 分页 + 来源/风险筛选，§82-119）。
        cursor = 'ts_id'（上一页最后一条）。返回 gaps + next_cursor + has_more。
        未结束缺口（open_gap）置顶，标记 ongoing。
        sub_start_ts/sub_end_ts：趋势点击 → 筛选到该桶（缺口与桶窗口有重叠即命中，§81-83）。
        """
        start_ts, end_ts = _history_range_ts(preset, start_date, end_date)
        cur = None
        if cursor:
            try:
                ts_s, id_s = str(cursor).split("_", 1)
                cur = (int(float(ts_s)), int(id_s))
            except ValueError:
                cur = None
        if source not in (None, "llama", "gpu", "application", "system"):
            source = None
        if risk not in (None, "lost", "time_uncertain"):
            risk = None
        q_start, q_end = start_ts, end_ts
        if sub_start_ts is not None and sub_end_ts is not None:
            # 桶窗口（重叠需允许 start 略早于桶开始；缺口最长 ~数分钟，宽 900s 足够）
            q_start = sub_start_ts - 900
            if start_ts is not None:
                q_start = max(q_start, min(start_ts, q_start))
            q_end = sub_end_ts
            if end_ts is not None:
                q_end = min(q_end, end_ts)
        rows = db.get_gaps_range(start_ts=q_start, end_ts=q_end, source=source,
                                 risk=risk, cursor=cur, limit=limit + 1)
        # 趋势桶精确筛选：缺口与桶窗口重叠（start < sub_end 且 end > sub_start）
        if sub_start_ts is not None and sub_end_ts is not None:
            rows = [g for g in rows
                    if g["start_timestamp"] < sub_end_ts and g["end_timestamp"] > sub_start_ts]
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = "%d_%d" % (last["start_timestamp"], last["id"])
        open_gap = collector.open_gap_property()
        return {
            "gaps": _present_gaps(rows, start_ts, end_ts),
            "next_cursor": next_cursor,
            "has_more": has_more,
            "open_gap": open_gap,
        }

    def _present_gaps(rows: list[dict], start_ts: float | None,
                      end_ts: float | None) -> list[dict]:
        """
        缺口展示层（§88-127）：
        - reason 中文化：系统休眠 / LlamaMonitor 重启 / llama.cpp 服务不可达 /
          采集超时 / 采集器异常 / 数据源不可用 / 原因未确定（弃 raw unknown/sleep_gap 上主 UI）。
        - **原因推定**（§113）：reason=unknown 且缺口时段附近有 sleep_gap 事件
          -> reason_label 标"系统休眠（推定）"+ inferred=True + tooltip 说明证据；
          无证据 -> "原因未确定"（不崩溃、不臆测）。
        - token 风险：后端只有 2 bool（token_recoverable / possible_token_loss），
          UI 表达 false=无已知风险、true=存在风险（§107-109），不假造三态：
            lost=1            -> risk=lost        "可能丢失"
            lost=0,recov=0    -> risk=time_uncertain "时间归属不确定"
            其余              -> risk=none         "无"
        """
        # 一次取范围内休眠证据（sleep_gap / system_monitor_stop）
        evidence: list = []
        try:
            evidence = db.get_sleep_events(start_ts, end_ts)
        except Exception:
            evidence = []
        # 休眠证据窗口：事件时间戳 + 其 monotonic_gap_seconds（暂停持续时长）。
        # gpu/unknown 缺口 start≈暂停开始，end≈暂停结束；只要 gap 的 [start,end]
        # 与某个休眠段 [ts-gap_seconds, ts] 重叠（含 60s 容差）即推定。
        def _sleep_windows() -> list[tuple[float, float]]:
            wins = []
            for e in evidence:
                ts = e["timestamp"]
                det = e["details"] or {}
                gap_s = det.get("monotonic_gap_seconds")
                if gap_s is None:
                    # system_monitor_stop 无 duration：用 120s 默认暂停窗
                    gap_s = 120.0
                try:
                    gap_s = float(gap_s)
                except (TypeError, ValueError):
                    gap_s = 120.0
                wins.append((ts - gap_s - 60.0, ts + 60.0))
            return wins
        sleep_wins = _sleep_windows()
        out = []
        for g in rows:
            lost = bool(g["possible_token_loss"])
            recov = bool(g["token_recoverable"])
            if lost:
                risk, risk_label = "lost", "可能丢失"
            elif not recov:
                risk, risk_label = "time_uncertain", "时间归属不确定"
            else:
                risk, risk_label = "none", "无"
            reason = g["reason"] or "unknown"
            label, inferred, inferred_note = _reason_label(reason)
            if reason == "unknown" and not lost:
                s0, e0 = float(g["start_timestamp"]), float(g["end_timestamp"])
                for ws, we in sleep_wins:
                    if s0 <= we and e0 >= ws:  # 区间重叠
                        label = "系统休眠（推定）"
                        inferred = True
                        inferred_note = (
                            "同时段检测到系统休眠/采集暂停事件（monotonic 断档），"
                            "推定为系统休眠；原始原因为 unknown（GPU 侧未达休眠阈值）。")
                        break
            out.append({
                "id": g["id"],
                "start": g["start_timestamp"],
                "end": g["end_timestamp"],
                "duration_seconds": g["duration_seconds"],
                "source": g["source"],
                "reason": reason,              # raw（Developer Detail）
                "reason_label": label,          # 主 UI 中文
                "reason_inferred": inferred,
                "reason_inferred_note": inferred_note,
                "token_recoverable": recov,
                "possible_token_loss": lost,
                "token_risk": risk,
                "token_risk_label": risk_label,
            })
        return out

    def _reason_label(reason: str) -> tuple[str, bool, str]:
        """raw reason -> (中文 label, inferred=False, note="")（§97-104 正式分类）。未知 -> 原因未确定。"""
        return {
            "system_pause_or_sleep": ("系统休眠", False, ""),
            "monitor_restart": ("LlamaMonitor 重启", False, ""),
            "server_offline": ("llama.cpp 服务不可达", False, ""),
            "invalid_metrics": ("采集超时 / 异常", False, ""),
            "unknown": ("原因未确定", False, ""),
        }.get(reason, ("原因未确定", False, ""))

    # ---------- Round-5 History 页：监控事件 Presentation Layer ----------
    # 事件来源中文展示（前端 来源 列不再出现 raw key collector/database/...）。
    # 未知 source 原样保留（Developer Detail 可查）。
    EVENT_SOURCE_LABELS: dict[str, str] = {
        "collector": "llama.cpp 采集器",
        "application": "LlamaMonitor",
        "system": "系统",
        "gpu": "GPU",
        "database": "数据库",
        "backup": "备份",
        "update": "更新",
        "llama.cpp": "llama.cpp",
    }

    def _event_title_match_types(search: str) -> list[str]:
        """展示层标题匹配（§166-168 搜索增强）：用户按**看到的中文标题**搜，
        把 display_title 含关键词的 raw event_type 收集起来，供 SQL IN 匹配。
        未知事件标题"未知事件"不入表，raw LIKE 仍兜底。"""
        s = (search or "").strip().lower()
        if not s:
            return []
        # 匹配用户可见的展示标题 或 展示分类（两者都出现在事件行/详情里）
        return [et for et, pres in EVENT_PRESENTATION.items()
                if s in pres[0].lower() or s in pres[1].lower()]

    # 单一事实源：raw event_type -> (display_title, category, severity_override)。
    # severity_override=None 表示沿用 DB 里存的 severity；否则用此值（§146-148：
    # 休眠=info 不标红、恢复=success、不可达=warning、损坏=error）。
    # category ∈ 服务/应用/系统/GPU/传感器/数据库/备份/更新（§132）。
    EVENT_PRESENTATION: dict[str, tuple[str, str, str | None]] = {
        # 服务
        "server_online": ("llama.cpp 服务已恢复", "服务", "success"),
        "server_offline": ("llama.cpp 服务不可达", "服务", "warning"),
        "llama_health_changed": ("llama.cpp 健康状态变化", "服务", None),
        # 应用（LlamaMonitor 生命周期）
        "monitor_start": ("LlamaMonitor 已启动", "应用", "info"),
        "monitor_stop": ("LlamaMonitor 已停止", "应用", "info"),
        "monitor_restart_gap": ("LlamaMonitor 已重启", "应用", "info"),
        "metrics_valid": ("指标采集有效", "应用", "info"),
        "invalid_metrics": ("采集到无效指标", "应用", "warning"),
        "counter_reset": ("Token 计数器重置", "应用", "warning"),
        "model_changed": ("模型已切换", "应用", "info"),
        # 系统
        "sleep_gap": ("系统休眠 / 采集暂停", "系统", "info"),
        "system_monitor_stop": ("系统监控已停止", "系统", "info"),
        # 传感器
        "hardware_sensor_provider_recovered": ("硬件传感器已连接", "传感器", "success"),
        "hardware_sensor_provider_unavailable": ("硬件传感器不可用", "传感器", "warning"),
        # GPU
        "gpu_unavailable": ("GPU 采集不可用", "GPU", "warning"),
        "gpu_available": ("GPU 采集已恢复", "GPU", "success"),
        # 数据库
        "migration": ("数据库结构已升级", "数据库", "info"),
        "backup_created": ("数据库备份完成", "备份", "info"),
        "database_recovery": ("数据库已恢复", "数据库", "success"),
        "database_integrity_error": ("数据库完整性错误", "数据库", "error"),
        "database_write_failure": ("数据库写入失败", "数据库", "error"),
        "database_protective_mode": ("数据库进入只读兼容模式", "数据库", "warning"),
        # 更新
        "update_check": ("更新检查", "更新", "info"),
        "update_check_failed": ("更新检查失败", "更新", "warning"),
        "update_available": ("发现新版本", "更新", "info"),
        "update_download_started": ("更新下载开始", "更新", "info"),
        "update_download_complete": ("更新下载完成", "更新", "info"),
        "update_download_cancelled": ("更新下载取消", "更新", "info"),
        "update_verification_failed": ("更新校验失败", "更新", "error"),
        "update_install_started": ("更新安装开始", "更新", "info"),
        "update_install_aborted": ("更新安装中止", "更新", "warning"),
        "update_backup_failed": ("更新前备份失败", "更新", "error"),
        "update_success": ("更新完成", "更新", "success"),
    }
    # category -> raw event_type 集合（供后端 category 过滤）
    _EVENT_CATEGORY_TYPES: dict[str, list[str]] = {}
    for _etype, (_t, _cat, _sv) in EVENT_PRESENTATION.items():
        _EVENT_CATEGORY_TYPES.setdefault(_cat, []).append(_etype)

    @app.get("/api/events")
    async def api_events(
        limit: int = Query(30, ge=1, le=100),
        preset: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        category: str | None = None,
        min_severity: str | None = None,
        search: str | None = None,
        cursor: str | None = None,
    ) -> dict:
        """
        监控事件（时间倒序 + 范围/分类/级别/搜索 + cursor 分页）。

        Presentation Layer（§130-145）：后端为每条事件补 display_title /
        display_category / display_severity（前端主 UI 不再出现 sleep_gap 等
        raw key）。raw event_type 仍原样返回（Developer Detail 用）。
        未知 event_type -> display_title="未知事件"，category 兜底"其它"，不崩（§145）。
        """
        start_ts, end_ts = _history_range_ts(preset, start_date, end_date)
        cat_types = _EVENT_CATEGORY_TYPES.get(category) if category else None
        cur = None
        if cursor:
            try:
                ts_s, id_s = str(cursor).split("_", 1)
                cur = (int(float(ts_s)), int(id_s))
            except ValueError:
                cur = None
        if min_severity not in (None, "warning", "error"):
            min_severity = None
        search = (search or "").strip() or None
        title_types = _event_title_match_types(search) if search else None
        rows = db.get_events_range(start_ts=start_ts, end_ts=end_ts, category=cat_types,
                                   min_severity=min_severity, search=search,
                                   cursor=cur, limit=limit + 1,
                                   title_match_types=title_types)
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = "%d_%d" % (last["timestamp"], last["id"])

        def _present(r: dict) -> dict:
            et = r["event_type"]
            pres = EVENT_PRESENTATION.get(et)
            if pres is not None:
                title, category_name, sev_override = pres
            else:
                title, category_name, sev_override = "未知事件", "其它", None
            sev = sev_override if sev_override is not None else r.get("severity") or "info"
            return {
                "id": r.get("id"),
                "timestamp": r["timestamp"],
                "event_type": et,               # raw（Developer Detail）
                "severity": sev,
                "source": r.get("source"),
                "display_source": EVENT_SOURCE_LABELS.get(r.get("source"), r.get("source") or "—"),
                "details": r.get("details") or {},
                "display_title": title,
                "display_category": category_name,
                "display_severity": sev,
            }
        return {
            "events": [_present(r) for r in rows],
            "next_cursor": next_cursor,
            "has_more": has_more,
            # 供前端分类 Filter 的可选项（固定展示顺序；前端按当前数据计数显示）
            "categories": ["服务", "应用", "系统", "GPU", "传感器", "数据库", "备份", "更新"],
        }

    @app.post("/api/data/check-database", dependencies=[Depends(_require_loopback)])
    async def api_check_database() -> Response:
        """
        手动数据库检查（local-only）：PRAGMA quick_check（只读检查，不修复、
        不删库）。结果更新 db.health 健康状态机：
        - 通过：healthy（若之前是 corrupt 则恢复写入，记 database_recovery 事件）；
        - 失败：corrupt（protective mode，采集停止写入）。
        """
        # AUDIT-1.1.1 PERF-1111-002：quick_check 在独立连接的工作线程执行
        # （大库可达秒级），不阻塞事件循环。健康状态/事件更新仍在主连接线程。
        try:
            ok, detail = await asyncio.to_thread(db.quick_check_threadsafe)
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
        # AUDIT-1.1.1 PERF-1111-012：DELETE + VACUUM 在独立连接的工作线程执行
        # （VACUUM 收缩文件可达数百 ms~秒级），不阻塞事件循环与其他 HTTP 端点
        try:
            deleted = await asyncio.to_thread(db.clear_live_samples_threadsafe, True)
        except Exception as exc:
            logger.warning("清空实时历史失败: %r", exc)
            return JSONResponse(status_code=500, content=_err("INTERNAL_ERROR", f"清空实时历史失败: {exc!r}"))
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
        今日 / 本月 / 累计总量（daily_usage 按本机系统日期归集）。

        compute_tokens = prompt + output
        logical_tokens = prompt + cached + output

        Phase 16B（BUG-A 修复）：month 改由后端计算。此前前端用浏览器本地
        'YYYY-MM' 字符串前缀匹配 /api/daily 返回的行，日期来源与 daily_usage
        的归集日期（collector 采集时刻的 local_date）分离，且仅在页面首次加载
        时取一次快照（月内不更新）——跨月/重置/时钟边界等场景下"今日 > 0 但
        本月 = 0"。现在 month 与 today 同源：同一批行、同一个 local_date()
        前缀判定，无时区/双日期源问题。
        """
        now = collector.clock.now()
        today_str = local_date(now)
        month_key = today_str[:7]  # 'YYYY-MM'，与 local_date 完全同源
        rows = db.get_daily_usage()
        today = next((r for r in rows if r["date"] == today_str), None)
        today_sum = _summary_shape(
            (today or {}).get("prompt_tokens", 0) or 0,
            (today or {}).get("cached_tokens", 0) or 0,
            (today or {}).get("output_tokens", 0) or 0,
        )
        month_sum = _summary_shape(
            sum(r["prompt_tokens"] for r in rows if r["date"].startswith(month_key)),
            sum(r["cached_tokens"] for r in rows if r["date"].startswith(month_key)),
            sum(r["output_tokens"] for r in rows if r["date"].startswith(month_key)),
        )
        total_sum = _summary_shape(
            sum(r["prompt_tokens"] for r in rows),
            sum(r["cached_tokens"] for r in rows),
            sum(r["output_tokens"] for r in rows),
        )
        return {"today": today_sum, "month": month_sum, "total": total_sum, "month_key": month_key}

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

    def _build_daily_csv_body(rows: list, all_live: list) -> bytes:
        """
        AUDIT-1.1.1 PERF-1111-013：daily CSV 构建（工作线程执行，经
        asyncio.to_thread 调用）。

        逐行的 Monitoring Coverage 需要 db.gap_stats_threadsafe（独立连接，
        不跨线程复用主连接），数百~数千行在事件循环里跑会周期性卡住所有
        HTTP 端点。DB 读取（get_daily_usage / get_live_samples）已在端点的
        事件循环线程完成，这里只算 + 拼 CSV。
        """
        live_by_date: dict[str, list] = {}
        for s in all_live:
            live_by_date.setdefault(local_date(s["timestamp"]), []).append(s)
        now = collector.clock.now()
        today = local_date(now)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "date", "prompt_tokens", "cached_tokens", "output_tokens",
            "compute_tokens", "logical_tokens", "draft_tokens", "accepted_tokens",
            "mtp_accept_rate", "prompt_seconds", "predicted_seconds",
            # Phase 11：数据质量字段
            "monitoring_coverage_percent", "gap_count", "possible_token_loss",
            # 1.1.2：缓存复用率（缓存复用 / (Prompt + 缓存复用) * 100）
            "reuse_rate_percent",
        ])
        for r in rows:
            prompt = r.get("prompt_tokens") or 0
            cached = r.get("cached_tokens") or 0
            output = r.get("output_tokens") or 0
            draft = r.get("draft_tokens") or 0
            accepted = r.get("accepted_tokens") or 0
            rate = round(accepted / draft * 100.0, 2) if draft > 0 else ""
            date = r.get("date")
            coverage, gap_count, possible_loss = "", 0, False
            if date:
                day_samples = live_by_date.get(date, [])
                gap_stats = db.gap_stats_threadsafe(date)
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
                    in_day = min(gap_stats["total_gap_seconds"], 86400.0)
                    coverage = round(max(0.0, 100.0 * (1.0 - in_day / 86400.0)), 2)
                gap_count = gap_stats["gap_count"]
                possible_loss = bool(gap_stats["possible_token_loss"])
            # 1.1.2：缓存复用率（与页面 /api/daily 口径一致：缓存复用 / (Prompt + 缓存复用)）
            reuse_denom = prompt + cached
            reuse_rate = round(cached / reuse_denom * 100.0, 2) if reuse_denom > 0 else ""
            writer.writerow([
                date, prompt, cached, output,
                prompt + output, prompt + cached + output,
                draft, accepted, rate,
                r.get("prompt_seconds") if r.get("prompt_seconds") is not None else 0,
                r.get("predicted_seconds") if r.get("predicted_seconds") is not None else 0,
                coverage if coverage is not None else "",
                gap_count, "yes" if possible_loss else "no",
                reuse_rate,
            ])
        return ("\ufeff" + buf.getvalue()).encode("utf-8")

    _DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    def _parse_calendar_date(value: str | None) -> str | None:
        """'YYYY-MM-DD' 严格校验（合法日历日期 + 格式），非法返回 None。"""
        if value is None:
            return None
        if not _DATE_RE.match(value):
            return None
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return None
        return value

    # ---------- Round-5 History 页：全局时间范围 ----------
    def _history_range(
        preset: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> tuple[float, float, str]:
        """
        解析 History 页时间范围 -> (start_ts, end_ts, label)。

        preset ∈ {24h, 7d, 30d, all}（now - N -> now）；
        或 start_date/end_date（'YYYY-MM-DD'，服务器本机日历日，§19 自定义范围）。
        自定义优先；两者都不合法 -> 400。end_ts 统一为 now（今天桶只计到当前，§57-58）。
        """
        now = collector.clock.now()
        today = local_date(now)
        if (start_date is not None) or (end_date is not None):
            if _parse_calendar_date(start_date) is None or _parse_calendar_date(end_date) is None:
                raise HTTPException(status_code=400, detail="自定义范围需同时提供合法 start_date / end_date")
            s_s, e_s = start_date, end_date
            if s_s > e_s:
                raise HTTPException(status_code=400, detail="start_date 不能晚于 end_date")
            if e_s > today:
                raise HTTPException(status_code=400, detail="end_date 不能晚于今天")
            start_ts = datetime.strptime(s_s, "%Y-%m-%d").timestamp()
            # end_ts = end_date 当日 24:00（但今天则到 now；§57-58 未来时段不入 expected）
            end_next = (datetime.strptime(e_s, "%Y-%m-%d") + timedelta(days=1)).timestamp()
            end_ts = min(now, end_next)
            return start_ts, end_ts, "custom"
        preset = preset or "7d"
        span = {"24h": 86400.0, "7d": 7 * 86400.0, "30d": 30 * 86400.0}.get(preset)
        if span is None:
            if preset == "all":
                # 全部：监测开始（最早 live 样本）-> now；无数据则 30 天兜底
                lo, hi = db.get_live_bounds()
                start_ts = lo if lo is not None else now - 30 * 86400.0
                return start_ts, now, "all"
            raise HTTPException(status_code=400, detail="未知 preset")
        return now - span, now, preset

    def _coverage_window_for_day(
        date: str, day_samples: list, today: str, now: float,
        start_ts: float, end_ts: float,
    ) -> tuple[float, float, bool]:
        """
        某自然日（或"今天到 now"）的监控时间窗，**与请求范围 [start_ts, end_ts] 求交**。
        返回 (窗口秒, 有效秒=窗口-缺口秒, 是否完整)。监测开始前 / 未来时段不计（§59-58）。
        口径与 _day_quality 同源：窗口 = 首样本->末样本（今天到 min(now, 末+2 间隔)），
        过去无 live 但有 daily 行 -> 24h 估算；再与 [start_ts, end_ts] 取交集。
        """
        win = None  # (window_start, window_end)
        if day_samples:
            first = day_samples[0]["timestamp"]
            last = day_samples[-1]["timestamp"]
            w_end = min(now, last + collector.interval * 2) if date == today else last
            win = (first, w_end)
        elif date < today:
            # 过去自然日（live 已清理）但有 daily 行：整日 24h 估算
            day0 = datetime.strptime(date, "%Y-%m-%d").timestamp()
            win = (day0, day0 + 86400.0)
        if win is None:
            return 0.0, 0.0, False
        # 与请求范围求交（未来时段 / 监测开始前不计）
        ws = max(win[0], start_ts)
        we = min(win[1], end_ts, now)
        if we <= ws:
            return 0.0, 0.0, False
        window = we - ws
        # 缺口秒：用 [ws, 次日00:00) 收窄的当日统计，再按窗口占比粗估（小时桶见 trend）
        gap_stats = db.get_gap_stats(date, start_ts=ws)
        in_window = min(gap_stats["total_gap_seconds"], window) if window > 0 else 0.0
        valid = max(0.0, window - in_window)
        full = (date < today) and (we - ws) >= 86400.0 - 1.0
        return window, valid, full

    @app.get("/api/daily")
    async def api_daily(days: int = Query(30, ge=1, le=3650), all: bool = False,
                        month: bool = False,
                        start_date: str | None = None,
                        end_date: str | None = None) -> dict:
        """
        最近 N 个自然日的统计（日期升序）；all=true 时返回全部历史（无上限）；
        month=true 时只返回**当前自然月**（本机日期前缀 YYYY-MM）；
        start_date/end_date（'YYYY-MM-DD'，**服务器本机日历日**）给出自定义
        闭区间时按区间过滤（优先级最高，其余参数忽略）。

        只返回实际有数据的天（无使用量的天没有行，空档由前端补齐）；
        每行含原始字段 + compute_tokens / logical_tokens 派生字段
        + Phase 11 数据质量字段（monitoring_coverage_percent / gap_count /
        possible_token_loss）。

        响应附 `range` 元数据：{mode, start_date, end_date, today}。
        mode ∈ "recent" | "all" | "month" | "custom"；start/end 为实际返回
        窗口的闭区间（recent 模式 end = 今天，start = 今天-(days-1)），
        供前端补全缺失日、标注"今天"、渲染自定义范围标签。

        AUDIT-1.1.1 BUG-1111-005：month 过滤移到服务端——此前前端取 31 天再按
        浏览器本地 'YYYY-MM' 前缀过滤，日期来源与 daily_usage 归集日期分离
        （跨月/时钟边界下"本月"可能漏行），且仅在页面加载时取一次快照。现在与
        /api/summary 的 month_key 同源（同一 collector.clock、同一 local_date 前缀）。
        自定义范围（1.1.2）：起止与 all 同一 local_date 日历口径——服务器本机
        日期，前端不再用浏览器时区推算窗口；非法格式 / 起>止 / 止>今天 / 跨度
        >3650 天 → 400。
        """
        now = collector.clock.now()
        today = local_date(now)
        start_s = end_s = None
        if start_date is not None or end_date is not None:
            start_s = _parse_calendar_date(start_date)
            end_s = _parse_calendar_date(end_date)
            if start_date is None or end_date is None or start_s is None or end_s is None:
                raise HTTPException(status_code=400, detail="start_date 与 end_date 必须同时提供且为 YYYY-MM-DD 日期")
            if start_s > end_s:
                raise HTTPException(status_code=400, detail="start_date 不能晚于 end_date")
            if end_s > today:
                raise HTTPException(status_code=400, detail="end_date 不能晚于今天")
            span = (datetime.strptime(end_s, "%Y-%m-%d") - datetime.strptime(start_s, "%Y-%m-%d")).days
            if span + 1 > 3650:
                raise HTTPException(status_code=400, detail="时间范围最多 3650 天")
        all_rows = db.get_daily_usage()
        mode = "recent"
        if start_s:
            rows = [r for r in all_rows if start_s <= r["date"] <= end_s]
            mode = "custom"
        elif all:
            rows = all_rows
            mode = "all"
            start_s, end_s = (all_rows[0]["date"], today) if all_rows else (today, today)
        elif month:
            month_key = today[:7]
            rows = [r for r in all_rows if r["date"].startswith(month_key)]
            mode = "month"
            start_s, end_s = today[:7] + "-01", today
        else:
            cutoff = local_date(now - (days - 1) * 86400)
            rows = [r for r in all_rows if r["date"] >= cutoff]
            start_s, end_s = cutoff, today
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
        return {
            "days": out,
            "range": {
                "mode": mode, "start_date": start_s, "end_date": end_s, "today": today,
                # 服务器当前时刻（供前端渲染"今天 · 截至 HH:MM"部分日标注，
                # 与 local_date 同一时钟，浏览器时区不参与"今天"判定）
                "server_now_iso": datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M"),
                "server_now_hhmm": datetime.fromtimestamp(now).strftime("%H:%M"),
            },
        }

    def _range_coverage_window(gap_stats: dict, date: str, today: str,
                               day_samples: list, now: float) -> tuple[float, bool] | None:
        """
        与 _day_quality 同口径的覆盖窗口（秒）：
        - 当日有 live 样本：首样本 → 末样本（今天到 min(now, 末样本+2 间隔)）；
        - 过去自然日（live 已清理）但 daily 行存在：按 24h 估算（监控当天运行过）；
        - 今天但无 live 样本：窗 0（尚未积累可观测时间）。
        返回 (窗口秒, 窗口是否完整一天)；无窗口时返回 None。
        """
        if day_samples:
            first = day_samples[0]["timestamp"]
            last = day_samples[-1]["timestamp"]
            if date == today:
                window_end = min(now, last + collector.interval * 2)
            else:
                window_end = last
            window = max(0.0, window_end - first)
            return window, (date < today)
        if date < today:
            return 86400.0, True
        if date == today:
            return 0.0, False
        return None

    @app.get("/api/range-stats")
    async def api_range_stats(start_date: str | None = None,
                              end_date: str | None = None) -> dict:
        """
        自定义范围的**区间级**数据质量统计（1.1.2，供使用汇总的采集覆盖区）。

        - 采集覆盖率 = Σ(窗口内有效时间) / Σ(窗口总时间) × 100——窗口按日累计
          后**加权**平均（不是逐日覆盖率的算术平均），口径与 /api/daily 的
          monitoring_coverage_percent 完全同源（_day_quality 的窗口规则）。
        - 采集缺口 = 范围内（窗口内）的缺口时长（秒）。
        - 范围缺口条数 / 可能存在 Token 丢失 = 范围内按日缺口统计的聚合。
        - 有效数据天数 = 有 daily 行的天数（日均 Token 的分母口径）。
        - 无数据天数 = 范围日历天数 - 有效数据天数（未开始监测 / 无有效采集）。

        非法参数 400（与 /api/daily 自定义范围同校验）。
        """
        if start_date is None or end_date is None:
            raise HTTPException(status_code=400, detail="start_date 与 end_date 必须同时提供")
        start_s = _parse_calendar_date(start_date)
        end_s = _parse_calendar_date(end_date)
        if start_s is None or end_s is None:
            raise HTTPException(status_code=400, detail="start_date 与 end_date 必须为 YYYY-MM-DD 日期")
        now = collector.clock.now()
        today = local_date(now)
        if start_s > end_s:
            raise HTTPException(status_code=400, detail="start_date 不能晚于 end_date")
        if end_s > today:
            raise HTTPException(status_code=400, detail="end_date 不能晚于今天")
        cal_days = (datetime.strptime(end_s, "%Y-%m-%d") - datetime.strptime(start_s, "%Y-%m-%d")).days + 1
        if cal_days > 3650:
            raise HTTPException(status_code=400, detail="时间范围最多 3650 天")
        all_rows = db.get_daily_usage()
        rows = [r for r in all_rows if start_s <= r["date"] <= end_s]
        try:
            all_live = db.get_live_samples(hours=None)
        except Exception:
            all_live = []
        live_by_date: dict[str, list] = {}
        for s in all_live:
            live_by_date.setdefault(local_date(s["timestamp"]), []).append(s)
        total_window = 0.0
        valid_window = 0.0
        gap_seconds = 0.0
        gap_count = 0
        possible_loss = False
        for r in rows:
            date = r["date"]
            gap_stats = db.get_gap_stats(date)
            win = _range_coverage_window(gap_stats, date, today,
                                         live_by_date.get(date, []), now)
            if win:
                window, _full = win
                total_window += window
                in_window = min(gap_stats["total_gap_seconds"], window) if window > 0 else 0.0
                valid_window += max(0.0, window - in_window)
                gap_seconds += in_window
            gap_count += gap_stats["gap_count"]
            if gap_stats["possible_token_loss"]:
                possible_loss = True
        valid_days = len(rows)
        no_data_days = max(0, cal_days - valid_days)
        coverage = None
        if total_window > 0:
            coverage = round(max(0.0, 100.0 * valid_window / total_window), 2)
        return {
            "coverage_percent": coverage,
            "gap_seconds": round(gap_seconds, 1),
            "gap_count": gap_count,
            "possible_token_loss": possible_loss,
            "valid_days": valid_days,
            "no_data_days": no_data_days,
            "calendar_days": cal_days,
        }

    @app.get("/api/today-hourly")
    async def api_today_hourly() -> dict:
        """
        今日按小时聚合的 Token 用量（1.1.2，供使用趋势的"小时"视图）。

        数据源为 live_samples（48h 保留）；小时桶**服务器本机时间**
        （local_date 同口径）。只返回有样本的小时桶（0-23），前端按缺失
        小时不画值（空 ≠ 0）。含 today（YYYY-MM-DD）供前端标注。
        """
        now = collector.clock.now()
        today = local_date(now)
        try:
            samples = db.get_live_samples(hours=None)
        except Exception:
            samples = []
        # live_samples 存的是本轮 delta（prompt/cached/output）；
        # logical = prompt + cached + output，compute = prompt + output
        buckets: dict[int, dict] = {}
        for s in samples:
            if local_date(s["timestamp"]) != today:
                continue
            hour = datetime.fromtimestamp(s["timestamp"]).hour
            b = buckets.get(hour)
            if b is None:
                b = buckets[hour] = {"hour": hour, "logical_tokens": 0,
                                     "compute_tokens": 0, "cached_tokens": 0,
                                     "prompt_tokens": 0, "output_tokens": 0}
            p = int(s.get("prompt_delta") or 0)
            c = int(s.get("cached_delta") or 0)
            o = int(s.get("output_delta") or 0)
            b["prompt_tokens"] += p
            b["cached_tokens"] += c
            b["output_tokens"] += o
            b["compute_tokens"] += p + o
            b["logical_tokens"] += p + c + o
        out = [buckets[h] for h in sorted(buckets)]
        return {"date": today, "hours": out}

    @app.get("/api/usage-summary")
    async def api_usage_summary(days: int = Query(30, ge=1, le=3650), all: bool = False,
                                month: bool = False,
                                start_date: str | None = None,
                                end_date: str | None = None) -> dict:
        """
        使用汇总块的**区间级**聚合（1.1.2）——/api/daily 同参数、同窗口，
        一次请求给出汇总卡全部数字，前端不再自行求和：

        - totals：Prompt / 缓存复用 / 生成 / 实际计算 / Token 总量（区间求和）；
        - cache_reuse_rate_percent：缓存复用 / (Prompt + 缓存复用) * 100
          （分母 0 -> null；术语=缓存复用率，**不是**缓存命中率）；
        - daily_avg_logical：Token 总量 / 有效数据天数（无有效天 -> null）；
        - peak_day：区间内 Token 总量最大的日子 {date, logical_tokens}（无 -> null）；
        - coverage_percent：采集覆盖率 = Σ窗口内有效时间 / Σ窗口总时间 * 100
          （**加权**，非逐日算术平均；无窗口 -> null）；
        - gap_seconds / gap_count / possible_token_loss：区间内采集缺口聚合；
        - valid_days / calendar_days / no_data_days。

        参数校验与 /api/daily 自定义范围一致（非法 400）。
        """
        now = collector.clock.now()
        today = local_date(now)
        # —— 窗口选择：与 /api/daily 完全同构 ——
        start_s = end_s = None
        if start_date is not None or end_date is not None:
            start_s = _parse_calendar_date(start_date)
            end_s = _parse_calendar_date(end_date)
            if start_date is None or end_date is None or start_s is None or end_s is None:
                raise HTTPException(status_code=400, detail="start_date 与 end_date 必须同时提供且为 YYYY-MM-DD 日期")
            if start_s > end_s:
                raise HTTPException(status_code=400, detail="start_date 不能晚于 end_date")
            if end_s > today:
                raise HTTPException(status_code=400, detail="end_date 不能晚于今天")
            if (datetime.strptime(end_s, "%Y-%m-%d") - datetime.strptime(start_s, "%Y-%m-%d")).days + 1 > 3650:
                raise HTTPException(status_code=400, detail="时间范围最多 3650 天")
        all_rows = db.get_daily_usage()
        if start_s:
            rows = [r for r in all_rows if start_s <= r["date"] <= end_s]
        elif all:
            rows = all_rows
            start_s, end_s = (all_rows[0]["date"], today) if all_rows else (today, today)
        elif month:
            month_key = today[:7]
            rows = [r for r in all_rows if r["date"].startswith(month_key)]
            start_s, end_s = today[:7] + "-01", today
        else:
            cutoff = local_date(now - (days - 1) * 86400)
            rows = [r for r in all_rows if r["date"] >= cutoff]
            start_s, end_s = cutoff, today
        # —— Token 汇总（区间求和）——
        prompt = sum(r["prompt_tokens"] or 0 for r in rows)
        cached = sum(r["cached_tokens"] or 0 for r in rows)
        output = sum(r["output_tokens"] or 0 for r in rows)
        logical = prompt + cached + output
        compute = prompt + output
        reuse_denom = prompt + cached
        reuse_rate = round(cached / reuse_denom * 100.0, 2) if reuse_denom > 0 else None
        valid_days = len(rows)
        daily_avg = round(logical / valid_days) if valid_days > 0 else None
        peak = None
        if rows:
            p = max(rows, key=lambda r: (r["prompt_tokens"] or 0) + (r["cached_tokens"] or 0) + (r["output_tokens"] or 0))
            peak = {"date": p["date"],
                    "logical_tokens": (p["prompt_tokens"] or 0) + (p["cached_tokens"] or 0) + (p["output_tokens"] or 0)}
        # —— 采集覆盖（与 /api/range-stats 同口径的加权窗口）——
        try:
            all_live = db.get_live_samples(hours=None)
        except Exception:
            all_live = []
        live_by_date: dict[str, list] = {}
        for s in all_live:
            live_by_date.setdefault(local_date(s["timestamp"]), []).append(s)
        total_window = 0.0
        valid_window = 0.0
        gap_seconds = 0.0
        gap_count = 0
        possible_loss = False
        for r in rows:
            date = r["date"]
            gap_stats = db.get_gap_stats(date)
            win = _range_coverage_window(gap_stats, date, today,
                                         live_by_date.get(date, []), now)
            if win:
                window, _full = win
                total_window += window
                in_window = min(gap_stats["total_gap_seconds"], window) if window > 0 else 0.0
                valid_window += max(0.0, window - in_window)
                gap_seconds += in_window
            gap_count += gap_stats["gap_count"]
            if gap_stats["possible_token_loss"]:
                possible_loss = True
        cal_days = max(1, (datetime.strptime(end_s, "%Y-%m-%d") - datetime.strptime(start_s, "%Y-%m-%d")).days + 1)
        coverage = None
        if total_window > 0:
            coverage = round(max(0.0, 100.0 * valid_window / total_window), 2)
        return {
            "totals": {"prompt_tokens": prompt, "cached_tokens": cached, "output_tokens": output,
                       "compute_tokens": compute, "logical_tokens": logical},
            "cache_reuse_rate_percent": reuse_rate,
            "daily_avg_logical": daily_avg,
            "peak_day": peak,
            "coverage_percent": coverage,
            "gap_seconds": round(gap_seconds, 1),
            "gap_count": gap_count,
            "possible_token_loss": possible_loss,
            "valid_days": valid_days,
            "calendar_days": cal_days,
            "no_data_days": max(0, cal_days - valid_days),
        }

    @app.get("/api/throughput")
    async def api_throughput(minutes: int = Query(60, ge=15, le=1440)) -> dict:
        """
        Token 吞吐历史窗口（Round 5 §35-§48）。

        - minutes：15 / 60 / 360 / 1440（15分钟/1小时/6小时/24小时），钳到 [15,1440]；
          live_samples 保留时长不足 24h 时，available_minutes 反映真实可用窗口
          （§168-§169：不假装 24h 完整）。
        - samples：窗口内逐样本（时间戳/prompt_tps/decode_tps/requests_processing/
          requests_deferred/prompt_delta/output_delta/prompt_seconds/predicted_seconds/
          busy_slots），超过 2000 点做 bucket 降采样（_downsample_throughput）。
        - window_avg：窗口加权平均吞吐（_throughput_window_avg，Δtoken/Δseconds，
          不是逐样本 TPS 简单平均）。
        - 采集缺口（Monitoring Gap）由前端按 timestamp 间隔 > poll*3 判断线
          （§44-§45），这里原样保留 null（connectNulls=false）。
        """
        capped = max(15, min(1440, minutes))
        samples = db.get_live_samples(hours=capped / 60.0)
        # 真实可用窗口：live 保留上限内、且窗口起点之后是否有数据
        now = time.time()
        oldest = samples[0]["timestamp"] if samples else now
        available_minutes = max(1, int(round((now - oldest) / 60)))
        window = capped if capped <= available_minutes else max(15, available_minutes)
        # 加权平均在**原始**样本上算（精确），降采样只用于画图
        avg = _throughput_window_avg(samples)
        plot = _downsample_throughput(samples)
        return {
            "minutes": capped,
            "available_minutes": available_minutes,
            "window_minutes": window,
            "window_avg": avg,
            "samples": plot,
            "last_activity_ts": (samples[-1]["timestamp"] if samples else None),
        }

    @app.get("/api/live")
    async def api_live(minutes: int = Query(60, ge=1, le=2880)) -> dict:
        """
        最近 N 分钟的实时采样（每采集一轮一条）。

        含 prompt/cached/output delta、prompt_tps、decode_tps、
        requests_processing、requests_deferred、context_max、mtp_accept_rate、
        prompt_seconds/predicted_seconds（v6）。
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
        # AUDIT-1.1.1 GAP-001："今日"必须与兄弟端点（/api/summary、/api/daily、
        # /api/mtp/daily）同源——collector.clock.now() 的 local_date，而非 wall
        # local_date()。wall 版本在 00:00:00 到当天首个采集之间会读昨天的行
        # （"今日 MTP 显示昨日累计"），且破坏 FakeClock 测试纪律。
        now = collector.clock.now()
        today_date = local_date(now)
        rows = db.get_daily_usage()
        today = next((r for r in rows if r["date"] == today_date), None) or {}
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
        positions = db.get_mtp_position_daily(today_date)
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

    @app.get("/api/mtp/range")
    async def api_mtp_range(
        days: int | None = Query(None, ge=1, le=365),
        all_: bool = Query(False, alias="all"),
    ) -> dict:
        """
        MTP 区间聚合（Round 5 §82-§109）：今天/7天/30天/全部。

        与 /api/mtp（仅今日）同源（daily_usage + mtp_position_daily），但按区间
        求和，且一次性驱动 MTP Summary + 趋势 + 按位置三块（§84：范围控制整个
        Section，避免"Summary 今天 / Chart 4 天 / Position 又是今天"的混乱）。

        - days：最近 N 个自然日；all=true：全部历史（date <= 今天，升序）；
          都不传 = 今天（days=1）。
        - summary（区间求和）：
            draft_tokens / accepted_tokens / verification_steps（draft_sequences）
            accept_rate = accepted/draft*100（draft<=0 -> None，§94-§95）
            avg_draft_length = draft_tokens/verification_steps（steps<=0 -> None，§87-§88）
            avg_accepted_length = accepted_tokens/verification_steps（§89）
        - positions：区间内各 Draft 位置的已接受 Token 求和（§109）。**只有 count**，
          无 per-position 分母 -> 前端禁止算分位置接受率（§104-§106）。
        - days_out：逐日 {date, draft, accepted, accept_rate}（供趋势折线，缺失日为
          accept_rate=null -> 前端 null 断线/留日期位）。
        """
        now = collector.clock.now()
        all_rows = db.get_daily_usage()
        if all_:
            date_list = sorted(r["date"] for r in all_rows if r["date"] <= local_date(now))
        else:
            d = 1 if days is None else days
            date_list = _recent_dates(d, now=now)
        rows = {r["date"]: r for r in all_rows}

        draft = 0
        accepted = 0
        steps = 0
        days_out = []
        for date in date_list:
            row = rows.get(date) or {}
            dd = row.get("draft_tokens", 0) or 0
            acc = row.get("accepted_tokens", 0) or 0
            st = row.get("draft_sequences", 0) or 0
            draft += dd
            accepted += acc
            steps += st
            days_out.append({
                "date": date,
                "draft": dd,
                "accepted": acc,
                "accept_rate": acc / dd * 100.0 if dd > 0 else None,
            })

        # 按位置求和（区间内出现过的所有 position；数值排序优先）
        pos_sum: dict[str, int] = {}
        for date in date_list:
            for pos, val in db.get_mtp_position_daily(date).items():
                pos_sum[pos] = pos_sum.get(pos, 0) + val
        ordered = sorted(
            pos_sum.items(),
            key=lambda kv: (0, int(kv[0])) if kv[0].isdigit() else (1, kv[0]),
        )
        positions = [
            {"position": pos, "accepted": val, "accepted_tokens": val}
            for pos, val in ordered
        ]

        return {
            "range": "all" if all_ else ("today" if (days in (None, 1)) else str(days)),
            "summary": {
                "draft_tokens": draft,
                "accepted_tokens": accepted,
                "verification_steps": steps,
                "accept_rate": accepted / draft * 100.0 if draft > 0 else None,
                "avg_draft_length": draft / steps if steps > 0 else None,
                "avg_accepted_length": accepted / steps if steps > 0 else None,
            },
            "days": days_out,
            "positions": positions,
        }

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
    # 1.1：System & Hardware Telemetry collectors（故障隔离：各自独立，
    # 失败绝不影响 Token 采集 / GPU 采集）
    runtime = LlamaRuntimeCollector(cfg, db=db)
    system = SystemCollector(cfg, db=db)
    sensors = HardwareSensorProvider(cfg, db=db)
    app = build_app(db, collector, loaded, gpu,
                    runtime=runtime, system=system, sensors=sensors)

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
