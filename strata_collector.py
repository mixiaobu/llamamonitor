"""
strata_collector.py — Strata 推理后端采集器（1.3.0）

与 collector.py（llama.cpp）同级的第二推理后端采集器：
- 同一个 LlamaMonitor 实例监控一个推理服务（provider 由配置/自动识别决定）；
- 统一落库口径：规范化 Token 写入同一套 daily_usage / live_samples
  （列语义与 llama.cpp 完全一致：prompt_tokens=实际计算 Prompt、
  cached_tokens=缓存复用、output_tokens=生成），Counter Baseline 按
  来源隔离（state 键 `strata:{server_id}:{counter}`，见 strata_parser）；
- Strata 专属数据（live 状态/当前请求/Slot/最近请求/会话缓存/引擎/后端硬件）
  全部**内存态**，经 /api/strata/live 实时服务，不落库（§51：不永久保存
  请求记录；§90：不把整个 metrics JSON 每轮写库）。

采样策略（§89；interval 默认 2s）：
- /metrics   每轮（Token 累计 + live + requests + conversation_cache +
             engine + hardware，JSON ~12KB，单次抓取 + 单次解析）；
- /status    每轮（当前请求进度/phase/queued；小 JSON）；
- /slots     每 2 轮（~4s；Strata Slot 语义与 llama.cpp 不同，低频即可）；
- /health    每 5 轮（~10s；loaded/模型/context/模态）；
- /props + /v1/models  首轮 + 每 300 轮（~10 分钟，节流；模型元数据）。

只读：全部 GET；绝不调用控制接口、绝不发送推理请求（§84）。

故障隔离：
- /metrics 失败 -> offline（与 llama.cpp 同语义：server_offline 缺口）；
- /status 失败 -> 本轮 live 状态保持上一次（"暂不可用"），不影响 Token 落库；
- 单轮任何异常只记日志，循环不退出。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys

import httpx

from clock import Clock, default_clock
from config import AppConfig, trust_env_for
from db import Database, local_date
from gap_tracker import OFFLINE_TOLERANCE, GapTracker
from stats import mtp_accept_rate, tps
from strata_parser import (
    STRATA_COUNTERS,
    build_conversation_cache,
    build_engine_info,
    build_health_info,
    build_live_state,
    build_model_info,
    build_recent_requests,
    counter_state_key,
    normalize_totals_sample,
    parse_metrics_json,
    server_identity,
    strata_capabilities,
)

logger = logging.getLogger("llamamonitor.strata")

# /metrics 响应上限（16MB 防御性；正常 ~12KB）
MAX_METRICS_BYTES = 16 * 1024 * 1024
MAX_SMALL_BYTES = 1 * 1024 * 1024

# 轮次调度（以轮为单位；interval 默认 2s）
SLOTS_EVERY = 2      # /slots 每 2 轮
HEALTH_EVERY = 5     # /health 每 5 轮
PROPS_EVERY = 300    # /props + /v1/models 每 300 轮（~10 分钟）

# 核心 Counter（namespaced）：缺口 possible_token_loss 判定用
_CORE_COUNTERS = ("totals.prompt_tokens", "totals.output_tokens")

# server_state 取值（§32 完整生命周期）
STATE_WAITING = "waiting"          # 等待服务响应（端口不可达——不假装确认"加载中"）
STATE_LOADING = "loading"          # 模型加载中（/health.loaded=false）
STATE_READY = "ready"              # 就绪
STATE_BUSY = "busy"                # 推理中
STATE_SLEEPING = "sleeping"        # 休眠（props.is_sleeping）
STATE_MONITORING_ERROR = "monitoring_error"  # 监测异常（health 可达但 metrics 持续无效）
STATE_UNREACHABLE = "unreachable"  # 不可达（长时间无响应；waiting 的稳态表述）

_STATE_LABELS = {
    STATE_WAITING: "等待服务响应",
    STATE_LOADING: "模型加载中",
    STATE_READY: "就绪",
    STATE_BUSY: "推理中",
    STATE_SLEEPING: "休眠",
    STATE_MONITORING_ERROR: "监测异常",
    STATE_UNREACHABLE: "不可达",
}


def strata_state_label(state: str | None) -> str | None:
    return _STATE_LABELS.get(state) if state else None


class StrataCollector:
    """
    Strata 常驻采集器（duck-type 与 MetricsCollector 兼容：
    last_snapshot / last_update / capabilities / interval / metrics_url /
    collect_once() / aclose() / shutdown()）。
    """

    provider_id = "strata"

    def __init__(
        self, config: AppConfig, db: Database | None = None, clock: Clock | None = None,
    ) -> None:
        self.config = config
        self.interval = config.collector.poll_interval_seconds
        self.timeout = config.llama_server.timeout_seconds
        self.base_url = (config.llama_server.url or "").rstrip("/")
        self.metrics_url = self.base_url + "/metrics"
        self.api_key = getattr(config.llama_server, "api_key", "") or ""
        self.server_id = server_identity(config.llama_server.url)
        self.db = db
        self.clock = clock or default_clock
        self.last_db_error: Exception | None = None
        self.last_snapshot: dict | None = None
        self.last_update: float | None = None
        self._last_online: bool | None = None
        self.capabilities: dict = {}
        self._http: httpx.AsyncClient | None = None
        self._http_loop = None
        self._round = 0
        self._reconnect_pending = True
        self._metrics = None  # 最近一次合法 /metrics dict（内存态）
        self._status = None   # 最近一次 /status dict（隐私字段已剥离）
        self._slots = []
        self._health: dict | None = None
        self._props: dict | None = None
        self._models: dict | None = None
        # 规范化（内存态，供 /api/strata/live 与 /api/status）
        self.server_state: str = STATE_WAITING
        self.live: dict | None = None
        self.model_info: dict | None = None
        self.engine_info: dict | None = None
        self.conversation_cache: dict | None = None
        self.recent_requests: list[dict] = []
        self.backend_hardware: dict | None = None
        self.endpoint_ok: dict[str, bool] = {}
        self._last_state: str | None = None
        self._invalid_streak = 0  # 连续 invalid /metrics 轮数（监测异常判定）
        # 1.3.1：连续 /metrics 抓取失败计数（<OFFLINE_TOLERANCE 保留上次数据与状态）
        self._offline_streak = 0
        self._gaps = GapTracker(self.interval, self.clock)
        self._gaps._gap_source = "strata"
        self._restart_pending_done = False

    # ---- 缺口 API（与 MetricsCollector 同名，server 层统一调用） ----

    @property
    def _last_valid(self) -> tuple | None:
        """最近有效样本 (wall, mono)（server 端点直接读；与 MetricsCollector 同契约）。"""
        return self._gaps._last_valid

    def gap_threshold(self) -> float:
        return self._gaps.gap_threshold()

    def maybe_note_restart_gap(self) -> None:
        if self.db is None or self._restart_pending_done:
            return
        self._restart_pending_done = True
        self._gaps.maybe_note_restart_gap(
            lambda: (self.db.get_first_and_last_sample() or (None, None))[1]
        )

    def open_gap_property(self) -> dict | None:
        return self._gaps.open_gap_property()

    # ---- HTTP ----

    def _get_client(self) -> httpx.AsyncClient:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if self._http is None or self._http_loop is not loop:
            keepalive = max(30.0, self.interval * 4)
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self._http = httpx.AsyncClient(
                timeout=self.timeout,
                limits=httpx.Limits(keepalive_expiry=keepalive),
                trust_env=trust_env_for(self.metrics_url),
                headers=headers,
            )
            self._http_loop = loop
        return self._http

    async def _fetch_text(self, path: str, limit: int) -> tuple:
        """GET base+path -> (text, ok)。任何失败 (None, False)。"""
        try:
            client = self._get_client()
            response = await client.get(self.base_url + path)
            if response.status_code != 200:
                return None, False
            if len(response.content) > limit:
                logger.warning("Strata %s 响应超过 %.0fMB 上限，按不可用处理",
                               path, limit / 1048576)
                return None, False
            return response.content.decode("utf-8", "replace"), True
        except Exception:
            return None, False

    async def _fetch_json(self, path: str, limit: int) -> tuple:
        """GET + JSON 解析 -> (payload, ok)。"""
        text, ok = await self._fetch_text(path, limit)
        if not ok or text is None:
            return None, False
        try:
            return json.loads(text), True
        except Exception:
            return None, False

    async def aclose(self) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None
            self._http_loop = None

    def shutdown(self) -> None:
        if getattr(self, "_shutdown_done", False):
            return
        self._shutdown_done = True
        if self.db is None:
            return
        now = self.clock.now()
        self._gaps.finalize_on_shutdown(self.db, now)
        try:
            self.db.record_event("monitor_stop", "info", "collector", {}, now=now)
        except Exception as exc:
            logger.warning("写入 monitor_stop 事件失败: %r", exc)

    # ---- 采集轮 ----

    async def collect_once(self) -> dict:
        """
        一轮采集（调度：metrics+status 每轮；slots 每 2 轮；health 每 5 轮；
        props+models 首轮+每 300 轮）。返回快照（含 offline 轮）。
        """
        self._round += 1
        now_wall = self.clock.now()
        now_mono = self.clock.monotonic()

        # 1) /metrics（Token 主数据源）
        metrics_text, metrics_ok = await self._fetch_text("/metrics", MAX_METRICS_BYTES)
        metrics = parse_metrics_json(metrics_text) if metrics_ok else None
        self.endpoint_ok["metrics"] = metrics is not None

        # 2) /status（当前请求；失败不影响 Token 落库）
        status_raw, status_ok = await self._fetch_json("/status", MAX_SMALL_BYTES)
        if status_ok and isinstance(status_raw, dict):
            # 隐私边界：tail / tool 剥离（tail 是正在生成的实际文本）
            self._status = {
                k: v for k, v in status_raw.items() if k not in ("tail", "tool")
            }
        else:
            self._status = None
        self.endpoint_ok["status"] = self._status is not None

        # 3) 调度轮：slots / health / props+models
        #    health 在 metrics 失败的同一轮**立即**再探测一次——区分
        #    "服务整体不可达"（health 也挂 -> unreachable）与
        #    "监测异常"（health 正常但 metrics 坏了 -> monitoring_error）；
        #    平时 10s 节流（HEALTH_EVERY 轮）。
        poll_health = (
            self._reconnect_pending
            or self._round % HEALTH_EVERY == 1
            or metrics is None
            or not isinstance((metrics or {}).get("totals"), dict)
        )
        if self._reconnect_pending or self._round % SLOTS_EVERY == 1:
            slots_raw, slots_ok = await self._fetch_json("/slots", MAX_SMALL_BYTES)
            self.endpoint_ok["slots"] = slots_ok and isinstance(slots_raw, list)
            if slots_ok and isinstance(slots_raw, list):
                self._slots = slots_raw
        health_polled = False
        if poll_health:
            health_raw, health_ok = await self._fetch_json("/health", MAX_SMALL_BYTES)
            health_polled = True
            self.endpoint_ok["health"] = health_ok and isinstance(health_raw, dict)
            if health_ok and isinstance(health_raw, dict):
                self._health = build_health_info(health_raw)
                self._note_model_loaded()
            else:
                # 本轮探测失败：旧 health 不可再当权威（否则"整体不可达"
                # 会被误判为"监测异常"）
                self._health = None
        if self._reconnect_pending or self._round % PROPS_EVERY == 1:
            props_raw, props_ok = await self._fetch_json("/props", MAX_SMALL_BYTES)
            models_raw, models_ok = await self._fetch_json("/v1/models", MAX_SMALL_BYTES)
            self.endpoint_ok["props"] = props_ok and isinstance(props_raw, dict)
            self.endpoint_ok["models"] = models_ok and isinstance(models_raw, dict)
            if props_ok and isinstance(props_raw, dict):
                self._props = props_raw
            if models_ok and isinstance(models_raw, dict):
                self._models = models_raw
            self._refresh_static()
            self._reconnect_pending = False

        # 4) 内存态规范化 + 5) Token 落库路径
        # 1.3.1 容忍：连续 <OFFLINE_TOLERANCE(5) 次 /metrics 抓取失败保留上一轮
        # 全部内存态（live/引擎/会话缓存/最近请求/硬件/server_state 不变），
        # 快照按保留数据构建（online 保持 True）；连续 >=5 次才切离线表现。
        has_totals = metrics is not None and isinstance(metrics.get("totals"), dict)
        if metrics is None:
            self._offline_streak += 1
            # 离线轮：开启 server_offline 缺口（GapTracker 幂等，起点=最后有效样本）
            self._gaps.on_offline(now_wall, now_mono)
            if self._offline_streak < OFFLINE_TOLERANCE:
                if self.last_snapshot is not None and self.last_snapshot.get("online"):
                    # 保留最近一次有效数据与状态（状态机不翻转、不记事件）
                    snapshot = self._online_snapshot(None)
                else:
                    # 从未在线过：保持离线表现（waiting 状态机表达"等待服务响应"）
                    snapshot = self._offline_snapshot()
            else:
                self.live = build_live_state(metrics, self._status)
                self.engine_info = build_engine_info(metrics, self._props)
                self.conversation_cache = build_conversation_cache(metrics)
                self.recent_requests = build_recent_requests(metrics, limit=20)
                self._metrics = metrics
                self._update_capabilities()
                self._update_server_state(metrics, health_polled)
                snapshot = self._offline_snapshot()
        else:
            self._offline_streak = 0
            self.live = build_live_state(metrics, self._status)
            self.engine_info = build_engine_info(metrics, self._props)
            self.conversation_cache = build_conversation_cache(metrics)
            self.recent_requests = build_recent_requests(metrics, limit=20)
            self._metrics = metrics
            self.backend_hardware = self._sanitize_hardware(metrics)
            self._update_capabilities()
            self._update_server_state(metrics, health_polled)
            if not has_totals:
                # /metrics 可读但无 totals（结构不符）：在线但样本无效
                snapshot = self._online_snapshot(metrics)
                snapshot["_valid"] = False
                self._invalid_streak += 1
                self._gaps.on_invalid(now_wall, now_mono)
            else:
                snapshot = self._handle_metrics_sample(metrics, now_wall, now_mono)

        # 6) 状态转换日志/事件（online/offline）
        online = bool(snapshot.get("online"))
        if self._last_online is not None and online != self._last_online:
            if online:
                logger.info("Strata 恢复在线: %s", self.metrics_url)
                self._record_event("strata_online", "success",
                                   {"provider": "strata", "url": self.metrics_url}, now_wall)
            else:
                logger.warning("Strata 离线（/metrics 抓取失败），采集循环继续")
                self._record_event("strata_offline", "warning",
                                   {"provider": "strata", "url": self.metrics_url}, now_wall)
        self._last_online = online

        self.last_snapshot = snapshot
        self.last_update = self.clock.now()
        return snapshot

    @staticmethod
    def _sanitize_hardware(metrics: dict) -> dict | None:
        from strata_parser import _HARDWARE_ALLOWED, _HARDWARE_STATIC_ALLOWED
        hw = metrics.get("hardware")
        hw_static = metrics.get("hardware_static")
        if not isinstance(hw, dict) and not isinstance(hw_static, dict):
            return None
        return {
            "live": {k: v for k, v in (hw or {}).items() if k in _HARDWARE_ALLOWED},
            "static": {k: v for k, v in (hw_static or {}).items()
                       if k in _HARDWARE_STATIC_ALLOWED},
        }

    # ---- 静态信息（props/models/health 合并） ----

    def _refresh_static(self) -> None:
        self.model_info = build_model_info(self._health, self._models, self._props)
        self._update_capabilities()

    def _note_model_loaded(self) -> None:
        """/health.loaded False->True 转换 -> strata_model_loaded 事件。"""
        loaded = bool((self._health or {}).get("loaded"))
        prev = getattr(self, "_last_loaded", None)
        if prev is not None and prev is False and loaded:
            logger.info("Strata 模型加载完成: %s", (self._health or {}).get("model"))
            self._record_event("strata_model_loaded", "info",
                               {"model": (self._health or {}).get("model")},
                               self.clock.now())
        self._last_loaded = loaded

    def _update_capabilities(self) -> None:
        self.capabilities = strata_capabilities(self.endpoint_ok, self._metrics)

    def _update_server_state(self, metrics: dict | None, health_polled: bool) -> None:
        """
        服务状态机（§32/§33/§34）：
        - props.is_sleeping -> sleeping（props 权威；唤醒后下次 /props 刷新恢复）；
        - metrics 有效：
            - 本轮 health 刚探测且 loaded=false -> loading（模型切换中）；
            - 否则按 busy/ready（metrics 是运行态权威）；
        - metrics 无效：
            - 本轮 health 可达：loaded=false -> loading；否则 monitoring_error
              （健康正常但指标坏了 = 监测异常）；
            - health 不可达（或未探测到）：本进程从未连上过 -> waiting（服务可能
              尚未启动/端口未就绪，不假装确认）；曾在线 -> unreachable。
        """
        if (self._props or {}).get("is_sleeping") is True:
            self._set_state(STATE_SLEEPING)
            return
        metrics_ok = metrics is not None and isinstance(metrics.get("totals"), dict)
        if metrics_ok:
            if health_polled and self._health and not bool(self._health.get("loaded")):
                state = STATE_LOADING
            else:
                state = STATE_BUSY if (self.live or {}).get("busy") else STATE_READY
        elif health_polled and self._health:
            state = (
                STATE_LOADING if not bool(self._health.get("loaded"))
                else STATE_MONITORING_ERROR
            )
        else:
            state = (
                STATE_UNREACHABLE
                if self._last_state not in (None, STATE_WAITING)
                else STATE_WAITING
            )
        self._set_state(state)

    def _set_state(self, state: str) -> None:
        if state == self._last_state:
            return
        prev = self.server_state
        self.server_state = state
        if self._last_state is not None and state != prev:
            (logger.info if state in (STATE_READY, STATE_BUSY) else logger.warning)(
                "Strata 状态变化: %s -> %s",
                _STATE_LABELS.get(prev, prev), _STATE_LABELS.get(state, state),
            )
            self._record_event(
                "strata_state_changed",
                "info" if state in (STATE_READY, STATE_BUSY) else "warning",
                {"previous": prev, "current": state},
                self.clock.now(),
            )
        self._last_state = state

    # ---- Token 样本处理 ----

    def _namespaced_state(self) -> dict[str, float]:
        prefix = f"strata:{self.server_id}:"
        if self.db is None:
            return {}
        try:
            all_state = self.db.get_state()
        except Exception:
            return {}
        return {
            k[len(prefix):]: v
            for k, v in all_state.items()
            if k.startswith(prefix)
        }

    def _handle_metrics_sample(self, metrics: dict, now_wall: float, now_mono: float) -> dict:
        totals = metrics.get("totals")
        normalized = normalize_totals_sample(totals, self._namespaced_state())
        if normalized is None:
            # 约束违反/核心字段缺失：保持旧 baseline + invalid_metrics 缺口
            self._invalid_streak += 1
            self._gaps.on_invalid(now_wall, now_mono)
            snap = self._online_snapshot(metrics)
            snap["_valid"] = False
            return snap

        self._invalid_streak = 0
        core_reset = any(
            c in normalized["reset_metrics"] for c in _CORE_COUNTERS
        ) or normalized["epoch_changed"]

        gap_was_open = self._gaps.open_gap_was_open
        if self._gaps.open_gap_was_open:
            self._gaps.close_open_gap(self.db, now_wall, core_reset, source="strata")
        if not gap_was_open:
            self._gaps.on_sleep_gap(self.db, now_wall, now_mono, core_reset,
                                    source="strata")

        # 周期/重置事件
        now = now_wall
        if normalized["epoch_changed"] and self.db is not None:
            self._record_event(
                "strata_instance_changed", "info",
                {"since": totals.get("since"),
                 "totals_requests": totals.get("requests")},
                now,
            )
            logger.info("Strata 累计周期变化（totals.since 变化）：重建 Baseline，"
                        "本周期之前不计入 delta")
        for counter in normalized["reset_metrics"]:
            logger.warning("Strata Counter 重置: %s", counter)
            self._record_event(
                "counter_reset", "warning", "collector",
                {"provider": "strata", "metric": f"strata:{self.server_id}:{counter}"},
                now,
            )

        # 持久化（单事务；失败 -> 保持旧 baseline，下轮重算完整 delta）
        snapshot = self._online_snapshot(metrics)
        write_blocked = (
            self.db is not None
            and self.db.health in ("corrupt", "unavailable", "incompatible")
            and self.last_db_error is None
        )
        if write_blocked:
            if not getattr(self, "_protective_logged", False):
                logger.error("数据库处于 %s 状态（protective mode）：暂停写入，只读继续",
                             self.db.health)
                self._protective_logged = True
                self._record_event("database_protective_mode", "warning", "database",
                                   {"health": self.db.health}, now)
        elif self.db is not None:
            if getattr(self, "_protective_logged", False):
                self._protective_logged = False
                logger.info("数据库恢复健康，写入继续")
            try:
                self.persist_sample(normalized, now=now)
                if self.last_db_error is not None:
                    logger.info("数据库写入恢复（上轮失败: %r）", self.last_db_error)
                    if self.db.health == "unavailable":
                        self.db.set_health("healthy", None)
                    self._record_event("database_recovery", "info", "collector",
                                       {"previous_error": repr(self.last_db_error)}, now)
                    self.last_db_error = None
            except Exception as exc:
                self.last_db_error = exc
                logger.warning("数据库写入失败（重试后仍失败），本轮跳过落盘、保持旧 baseline: %r", exc)
                if "readonly" in str(exc).lower() and self.db.health == "healthy":
                    self.db.set_health("unavailable", f"database write failed: {exc!r}")

        self._gaps.finalize_restart_gap(self.db, now_wall, core_reset,
                                        source="application")
        self._gaps.on_valid_sample(now_wall, now_mono)
        snapshot["_valid"] = True
        return snapshot

    def persist_sample(self, normalized: dict, now: float | None = None) -> dict:
        """
        单事务落库（db.apply_sample + 来源级 daily_usage_by_source）：
        state（namespaced baseline）+ daily（统一口径）+ live + by_source。
        """
        now = self.clock.now() if now is None else now
        daily = local_date(now)
        deltas = normalized["deltas"]

        # state：int 存 token 计数，float 存秒/epoch
        state_values: dict[str, float | int] = {}
        for counter, value in normalized["counters"].items():
            if counter == "totals.since" or counter.endswith("_ms"):
                state_values[counter_state_key(self.server_id, counter)] = float(value)
            else:
                state_values[counter_state_key(self.server_id, counter)] = int(value)

        new_prompt = deltas.get("new_prompt")
        reused = deltas.get("reused")
        output = deltas.get("output")
        prompt_seconds = deltas.get("prompt_seconds")
        decode_seconds = deltas.get("decode_seconds")
        drafts = deltas.get("drafts_offered")
        accepted = deltas.get("drafts_accepted")

        live_row = {
            "timestamp": int(now),
            "prompt_delta": int(new_prompt) if new_prompt is not None else None,
            "cached_delta": int(reused) if reused is not None else None,
            "output_delta": int(output) if output is not None else None,
            # 窗口吞吐（服务端计时口径，与 llama.cpp 的 prompt_tps/decode_tps 同义）
            "prompt_tps": tps(new_prompt, prompt_seconds),
            "decode_tps": tps(output, decode_seconds),
            "requests_processing": (self.live or {}).get("running"),
            "requests_deferred": (self.live or {}).get("waiting"),
            "context_max": (self.model_info or {}).get("context_window"),
            "mtp_accept_rate": mtp_accept_rate(accepted, drafts),
            "kv_cache_usage_ratio": None,
            "busy_slots": (self.live or {}).get("running"),
            "prompt_seconds": prompt_seconds,
            "predicted_seconds": decode_seconds,
        }

        daily_increments = {
            # 每日累加：new_prompt 允许轮内小负值（统计延迟），按 0 下限保护
            "prompt_tokens": int(max(0, new_prompt or 0)),
            "cached_tokens": int(max(0, reused or 0)),
            "output_tokens": int(max(0, output or 0)),
            "draft_tokens": int(max(0, drafts or 0)),
            "accepted_tokens": int(max(0, accepted or 0)),
            "prompt_seconds": prompt_seconds or 0.0,
            "predicted_seconds": decode_seconds or 0.0,
            "draft_sequences": 0,
        }

        self.db.apply_sample(
            state_values,
            live_row,
            daily,
            daily_increments,
            now=now,
            source=("strata", self.server_id),
        )
        return {"deltas": deltas, "daily_date": daily}

    # ---- 快照 ----

    def _offline_snapshot(self) -> dict:
        return {
            "online": False,
            "provider": self.provider_id,
            "requests_processing": None,
            "requests_deferred": None,
            "context_max": (self.model_info or {}).get("context_window"),
            "strata": self._strata_block(),
        }

    def _online_snapshot(self, metrics: dict) -> dict:
        return {
            "online": True,
            "provider": self.provider_id,
            "requests_processing": (self.live or {}).get("running"),
            "requests_deferred": (self.live or {}).get("waiting"),
            "context_max": (self.model_info or {}).get("context_window"),
            "strata": self._strata_block(),
        }

    def _strata_block(self) -> dict:
        """Strata 专属状态块（/api/status.strata / 快照内复用）。"""
        live = self.live or {}
        return {
            "state": self.server_state,
            "state_label": strata_state_label(self.server_state),
            "phase": live.get("phase"),
            "busy": live.get("busy"),
            "running": live.get("running"),
            "waiting": live.get("waiting"),
            "queued": live.get("queued"),
            "outside_slots": live.get("outside_slots"),
            "tok_s": live.get("tok_s"),
            "tok_s_mean": live.get("tok_s_mean"),
            "prefill_tok_s_mean": live.get("prefill_tok_s_mean"),
            "prompt_tokens": live.get("prompt_tokens"),
            "generated": live.get("generated"),
            "max_tokens": live.get("max_tokens"),
            "elapsed_s": live.get("elapsed_s"),
            "slots_count": (self._props or {}).get("total_slots")
            or len((self._slots or [])),
            "model": (self.model_info or {}).get("model"),
            "model_loaded": (self.model_info or {}).get("model_loaded"),
            "version": ((self.model_info or {}).get("build_info")
                        or (self.engine_info or {}).get("version")),
            "context_window": (self.model_info or {}).get("context_window"),
            "images_supported": (self.model_info or {}).get("vision_supported"),
        }

    def _record_event(self, event_type: str, severity: str, source: str = "strata",
                      details: dict | None = None, now: float | None = None) -> None:
        if self.db is None:
            return
        # 兼容两种调用序：
        #   4 参 (type, sev, details, now) —— source 位置是 dict，now 落在 details 位
        #   5 参 (type, sev, source, details, now)
        if isinstance(source, dict):
            now, details = details, source
            source = "strata"
        if now is None:
            now = self.clock.now()
        try:
            self.db.record_event(event_type, severity, source, details or {}, now=now)
        except Exception as exc:
            logger.warning("写入事件 %s 失败: %r", event_type, exc)

    async def run(self, out=None) -> None:
        """常驻采集循环（独立进程模式；与 MetricsCollector.run 同契约）。

        默认参数不能写成 `out=sys.stdout.write`——默认值在类定义（import）时求值，
        而 PyInstaller --windowed 下 sys.stdout 是 None（与 collector.py 同注意）。
        """
        if out is None:
            out = sys.stdout.write if sys.stdout is not None else (lambda *_a, **_k: None)
        while True:
            try:
                try:
                    snapshot = await self.collect_once()
                except Exception as exc:
                    snapshot = self._offline_snapshot()
                    snapshot["error"] = repr(exc)
                out_json = {k: v for k, v in snapshot.items() if not k.startswith("_")}
                out(json.dumps(out_json, ensure_ascii=False, indent=2) + "\n")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Strata 采集循环单轮异常（已恢复，继续下一轮）: %r", exc)
            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Strata 采集循环 sleep 异常（已恢复）: %r", exc)
