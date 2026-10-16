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

采集链路分层（1.3.3 重构；对齐官方 Strata Monitor ~1s 刷新 /metrics 的节奏）：

A. HOT PATH（1s，deadline 调度，不漂移/不重叠/不追赶）：
   - GET /metrics（独立 1.5s 超时）——实时数据第一来源（live state /
     engine / requests / conversation_cache / hardware 全在 /metrics JSON）；
   - 解析后立即更新内存态（live / engine_info / conversation_cache /
     recent_requests / backend_hardware / server_state / 快照 /
     sample_timestamp / sequence）——**HOT 轮不写数据库**；
   - activity 判定：metrics.live 第一优先（state=reading/generating、
     running>0、outside_slots>0），/status.busy 第二优先，明确 idle 才 idle；
     **绝不使用 /slots[].is_processing**（它只负责 Slot 信息/数量/辅助诊断）；
   - 本轮 /metrics 失败：保留上一轮数据与 activity（不翻 idle），
     连续 OFFLINE_TOLERANCE(5) 次才切离线表现（1.3.1 容忍语义不变）。

B. WARM PATH（2s=既有历史采样策略，独立任务；DB 写入 + 辅助端点）：
   - Token 样本落库（live_samples / daily_usage，与 1.3.2 同节奏——
     1s 实时刷新不会加倍历史写入）；
   - /status 5s 节流、/slots 10s、/health 10s（metrics 失败时立即重探
     health 以区分 unreachable / monitoring_error）；
   - 每个辅助端点独立 3s 超时 + EndpointState（故障局部化）。

C. COLD PATH（single-flight 后台任务，绝不阻塞 HOT）：
   - 延迟 histogram + 主机硬件落库（15s，?format=prometheus）；
   - /v1/status uptime/并发（300s）；
   - /props + /v1/models 模型元数据（600s + 重连）；
   - 每类任务最多一个 in-flight；上一轮未完成则跳过本轮；
     exception 捕获；CancelledError 传播；shutdown 正确取消。

只读：全部 GET；绝不调用控制接口、绝不发送推理请求（§84）。

故障隔离：
- 任一辅助端点 timeout / 5xx / 4xx / 坏 JSON 只局部化（EndpointState），
  /metrics 继续 1s 刷新，实时监控不中断；
- 能力型错误（404/501/401）按 15s 恢复间隔节流，不每秒狂刷；
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
from endpoint_state import (
    EndpointRegistry,
    classify_exception,
    classify_http_status,
)
from gap_tracker import OFFLINE_TOLERANCE, GapTracker
from scheduling import DeadlineScheduler
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

# ---- 1.3.3 分层调度（秒）----
HOT_INTERVAL = 1.0        # HOT PATH：/metrics（官方 Monitor ~1s 节奏）
STATUS_INTERVAL = 5.0     # WARM：/status（辅助；activity 权威在 /metrics.live）
SLOTS_INTERVAL = 10.0     # WARM：/slots（Slot 信息/数量/辅助诊断，不参与 activity）
HEALTH_INTERVAL = 10.0    # WARM：/health（loaded/模型/context/模态）
METADATA_INTERVAL = 600.0 # COLD：/props + /v1/models（模型元数据，~10 分钟）
# 1.3.2 低频遥测（COLD 独立任务；monotonic 节流，绝不占 HOT PATH）
HISTOGRAM_INTERVAL = 15.0    # /metrics?format=prometheus 延迟 Histogram 采样
                             # + Strata 主机硬件落库（spec 建议 10~15s）
V1_STATUS_INTERVAL = 300.0   # /v1/status（uptime/concurrency；低频 5 分钟）
REQUESTS_ALL_TTL = 30.0      # 按需"查看更多"请求的内存缓存 TTL
REQUESTS_ALL_MAX = 200       # 按需拉取的最大请求数
# ---- 1.3.3 分端点超时（§19：HOT 短、WARM/COLD 略长；绝不无限）----
HOT_TIMEOUT = 1.5
WARM_TIMEOUT = 3.0
COLD_TIMEOUT = 5.0
REQUESTS_ALL_MAX = 200       # 按需拉取的最大请求数

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
        # ---- 1.3.2：延迟 Histogram / 后端硬件遥测 / /v1/status ----
        self._last_histo_mono: float = 0.0
        self._last_v1status_mono: float = 0.0
        self._v1status_done: bool = False
        # per (metric, model) 的上一次累计 bucket 快照（delta 计算基线）
        self._histo_prev: dict[tuple, dict] = {}
        self._histo_capable: bool | None = None  # prometheus 端点可达性探测
        self._uptime_info: dict | None = None    # /v1/status 摘要（内存态）
        self._requests_all_cache: tuple | None = None  # (mono, [entries])
        # ---- 1.3.3：分层采集状态（HOT/WARM/COLD + 端点健康 + 新鲜度）----
        self.hot_interval = HOT_INTERVAL
        # 默认（未指定 per-endpoint timeout 时的）请求超时
        self._client_timeout: float = WARM_TIMEOUT
        # 端点健康状态（§16 故障隔离：每端点独立 available/age/连续失败/错误分类）
        self.endpoints = EndpointRegistry()
        # 新鲜度（§14：live_last_update = 最近一次 HOT PATH 成功取得实时数据
        # 的时刻——**不是**所有后台任务完成时刻；/v1/status 卡 3s 不影响它）
        self.sample_timestamp: float | None = None   # 最近成功 HOT /metrics（wall）
        self.live_last_update: float | None = None   # 语义同上（API 明确字段）
        self.sequence: int = 0                       # 单调样本序号（竞态防护）
        # activity（§4 判定规则；每 HOT 轮重算，失败轮保留上一轮）
        self.activity_state: dict | None = None
        self._last_activity: dict | None = None
        # HOT 循环诊断（§31：/api/realtime diagnostics）
        self.last_hot_duration_s: float = 0.0
        self.hot_overrun: bool = False
        self.hot_overrun_count: int = 0
        # 分端点节流（monotonic；WARM/COLD 内部节奏）
        self._last_status_mono: float = 0.0
        self._last_slots_mono: float = 0.0
        self._last_health_mono: float = 0.0
        # 后台任务句柄（single-flight：每类任务最多一个 in-flight）
        self._started = False
        self._hot_task: asyncio.Task | None = None
        self._warm_task: asyncio.Task | None = None
        self._cold_tasks: list[asyncio.Task] = []
        self._cold_inflight: set[str] = set()
        # 每 HOT 轮完成后的回调（server 层刷新 app_state / runtime 同步）
        self.on_hot_round = None
        # HOT 预校验通过的待落库样本 (normalized, metrics, now_wall, now_mono)；
        # WARM 轮消费。失败/无效轮置 None——绝不拿旧样本重算 delta（会误关缺口）
        self._pending_sample: tuple | None = None

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

    async def _fetch_text(self, path: str, limit: int,
                          headers: dict | None = None,
                          timeout: float | None = None,
                          endpoint: str | None = None) -> tuple:
        """
        GET base+path -> (text, ok, error)。

        1.3.3：timeout 分端点（HOT 1.5s / WARM 3s / COLD 5s）；
        endpoint 非 None 时同步记录 EndpointState（错误分类 §17）：
        404/501=unsupported、401=authentication_error、429=rate_limited、
        500/502/503=temporarily_unavailable、timeout、unreachable、
        invalid_response（坏 JSON 由 _fetch_json 记录）。
        """
        now_wall = self.clock.now()
        now_mono = self.clock.monotonic()
        try:
            client = self._get_client()
            response = await client.get(
                self.base_url + path, headers=headers or None,
                timeout=timeout if timeout is not None else self._client_timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if endpoint is not None:
                err = classify_exception(exc)
                self._record_endpoint(endpoint, now_wall, now_mono, err)
            return None, False, classify_exception(exc)
        status = response.status_code
        if status != 200:
            err = classify_http_status(status)
            if endpoint is not None:
                self._record_endpoint(endpoint, now_wall, now_mono, err)
            return None, False, err
        if len(response.content) > limit:
            logger.warning("Strata %s 响应超过 %.0fMB 上限，按不可用处理",
                           path, limit / 1048576)
            err = "response_too_large"
            if endpoint is not None:
                self._record_endpoint(endpoint, now_wall, now_mono, err)
            return None, False, err
        text = response.content.decode("utf-8", "replace")
        if endpoint is not None:
            self._record_endpoint(endpoint, now_wall, now_mono, None)
        return text, True, None

    def _record_endpoint(self, name: str, now_wall: float, now_mono: float,
                         error: str | None) -> None:
        """记录端点尝试结果（状态转换才记日志，不逐轮刷屏 §32）。"""
        st = self.endpoints.get(name)
        prev_error = st.last_error
        if error is None:
            was_available = st.available
            st.record_success(now_wall, now_mono)
            if not was_available:
                logger.info("Strata 端点 %s 恢复可用", name)
        else:
            st.record_failure(now_wall, now_mono, error)
            if not st.available and (prev_error != error
                                     or st.consecutive_failures in (1, 5, 10, 30)):
                logger.warning("Strata 端点 %s 不可用（%s，连续 %d 次）",
                               name, error, st.consecutive_failures)

    async def _fetch_json(self, path: str, limit: int,
                          timeout: float | None = None,
                          endpoint: str | None = None) -> tuple:
        """GET + JSON 解析 -> (payload, ok, error)。"""
        text, ok, err = await self._fetch_text(path, limit, timeout=timeout,
                                               endpoint=endpoint)
        if not ok or text is None:
            return None, False, err
        try:
            return json.loads(text), True, None
        except Exception:
            if endpoint is not None:
                self._record_endpoint(endpoint, self.clock.now(),
                                      self.clock.monotonic(), "invalid_response")
            return None, False, "invalid_response"

    async def aclose(self) -> None:
        # 1.3.3：先取消全部后台采集任务（HOT/WARM/COLD），再关 HTTP client
        if self._started:
            await self.stop_tasks()
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

    # ---- 1.3.3 分层采集：HOT / WARM / COLD ----

    async def _hot_round(self) -> dict:
        """
        1.3.3 HOT PATH（~1s，由 _hot_loop 的 deadline 调度驱动）：

        只做一件事——GET /metrics（独立 1.5s 超时）-> 解析 -> **立即**更新内存态
        （live / engine_info / conversation_cache / recent_requests /
        backend_hardware / server_state / 快照 / sample_timestamp / sequence）。
        不写数据库、不请求其它端点（WARM/COLD 任务负责）。

        activity 判定（spec §4，绝不使用 /slots[].is_processing）：
        - 第一优先 /metrics.live：state=reading/generating、running>0、
          outside_slots>0 任一 -> active；
        - 第二优先 /status.busy == true -> active；
        - 明确 live.state=="idle" 且 running==0 且 status.busy!=true 才 idle；
        - generated==0 的 "reading the prompt" 是 Prompt Processing（active），
          绝不因为 tok_s==0 判 idle。

        失败语义（1.3.1 容忍不变）：连续 <5 次保留上一轮数据与 activity
        （不翻 idle、状态机不翻转）；>=5 次才切离线表现。
        """
        now_wall = self.clock.now()
        now_mono = self.clock.monotonic()

        # 1) /metrics（实时数据第一来源）
        # §18：/metrics 能力型错误（unsupported/auth/rate_limited）期间
        # 恢复探测按 15s 节流——不每秒重复请求已知 501 的端点
        if self.endpoints.get("metrics").should_poll(now_mono):
            metrics_text, metrics_ok, _ = await self._fetch_text(
                "/metrics", MAX_METRICS_BYTES, timeout=HOT_TIMEOUT, endpoint="metrics")
        else:
            metrics_text, metrics_ok = None, False
        metrics = parse_metrics_json(metrics_text) if metrics_ok else None
        self.endpoint_ok["metrics"] = metrics is not None
        has_totals = metrics is not None and isinstance(metrics.get("totals"), dict)
        # health 新鲜度（替代旧"本轮是否刚探测 health"布尔：WARM 10s 节奏，
        # metrics 失败时 WARM 立即重探——30s 窗口内视为可用权威）
        h = self.endpoints.get("health")
        health_fresh = (
            h.available and h.last_attempt_mono is not None
            and (now_mono - h.last_attempt_mono) <= HEALTH_INTERVAL * 3
        )

        if metrics is None:
            self._offline_streak += 1
            # 离线轮：开启 server_offline 缺口（GapTracker 幂等，起点=最后有效样本）
            self._gaps.on_offline(now_wall, now_mono)
            self._pending_sample = None  # 失败轮不重放旧样本（会误关缺口/重复落库）
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
                self._update_server_state(metrics, health_fresh)
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
            self._update_server_state(metrics, health_fresh)
            # 预校验（HOT 与 WARM 落库同一口径 normalize_totals_sample）：
            # 避免"快照说 valid、WARM 说 invalid"的不一致
            normalized = normalize_totals_sample(
                metrics.get("totals"), self._namespaced_state())
            if normalized is None:
                # 无 totals / 约束违反 / 核心字段缺失：在线但样本无效
                # （保持旧 baseline + invalid_metrics 缺口）
                snapshot = self._online_snapshot(metrics)
                snapshot["_valid"] = False
                self._invalid_streak += 1
                self._gaps.on_invalid(now_wall, now_mono)
                self._pending_sample = None
            else:
                # 有效样本：快照即刻发布；DB 写入在 WARM 轮（_persist_round）
                snapshot = self._online_snapshot(metrics)
                snapshot["_valid"] = True
                self._pending_sample = (normalized, metrics, now_wall, now_mono)

        # 2) activity（成功轮按最新数据重算；失败轮保留上一轮并标注 stale）
        self._compute_activity()

        # 3) 新鲜度（仅 HOT 成功时推进——live_last_update 绝不代表
        #    "所有后台任务完成时刻"，/v1/status 卡 3s 不影响它，spec §14）
        if metrics is not None:
            self.sample_timestamp = now_wall
            self.live_last_update = now_wall
            self.sequence += 1

        # 4) 状态转换日志/事件（online/offline）
        online = bool(snapshot.get("online"))
        if self._last_online is not None and online != self._last_online:
            if online:
                logger.info("Strata 恢复在线: %s", self.metrics_url)
                self._record_event("strata_online", "success",
                                   {"provider": "strata", "url": self.metrics_url}, now_wall)
            else:
                logger.warning("Strata 离线（/metrics 连续 %d 次抓取失败）",
                               self._offline_streak)
                self._record_event("strata_offline", "warning",
                                   {"provider": "strata", "url": self.metrics_url}, now_wall)
        self._last_online = online

        self.last_snapshot = snapshot
        self.last_update = self.clock.now()
        return snapshot

    def _compute_activity(self) -> None:
        """
        activity 判定（1.3.3 §4 优先级）：
        1) /metrics.live 权威：state=reading/generating 或 running>0 或 outside>0；
        2) /status.busy == true；
        3) 明确 idle：live.state=="idle" 且 running==0 且 status.busy != true。
        /slots[].is_processing 绝不参与（它只负责 Slot 信息/数量/辅助诊断）。
        metrics 失败轮：保留上一轮 activity（source 加 stale: 前缀），
        不能直接变 idle（§15/§30）。
        """
        m = self.endpoints.get("metrics")
        live = self.live or {}
        live_state = live.get("state")
        running = int(live.get("running") or 0)
        outside = int(live.get("outside_slots") or 0)
        status = self._status or {}
        status_ok = self.endpoints.get("status").available
        status_busy = bool(status.get("busy")) if status_ok else False
        phase = live.get("phase")

        if m.available:
            live_active = (live_state in ("reading", "generating")
                           or running > 0 or outside > 0)
            if live_active:
                act = {
                    "active": True,
                    "state": live_state if live_state in ("reading", "generating") else "unknown",
                    "phase": phase,
                    "source": "strata.metrics.live",
                    "confidence": "authoritative",
                }
            elif status_ok and status_busy:
                act = {
                    "active": True, "state": "unknown",
                    "phase": status.get("phase"),
                    "source": "strata.status", "confidence": "secondary",
                }
            elif live_state == "idle" and running == 0 and not (status_ok and status_busy):
                act = {
                    "active": False, "state": "idle", "phase": phase,
                    "source": "strata.metrics.live", "confidence": "authoritative",
                }
            else:
                act = {
                    "active": False, "state": "unknown", "phase": phase,
                    "source": "strata.metrics.live", "confidence": "degraded",
                }
        else:
            # metrics 失败轮：保留上一轮 activity（不翻 idle；§15/§30）。
            # stale: 前缀只加一层（上一轮已是 stale 时不重复累积）
            if self._last_activity is not None:
                act = dict(self._last_activity)
                _src = str(act.get("source") or "none")
                if _src.startswith("stale:"):
                    _src = _src[len("stale:"):]
                act["source"] = "stale:" + _src
                act["confidence"] = "stale"
            elif status_ok and status_busy:
                act = {
                    "active": True, "state": "unknown",
                    "phase": status.get("phase"),
                    "source": "strata.status", "confidence": "secondary",
                }
            else:
                act = {
                    "active": False, "state": "unknown", "phase": None,
                    "source": "none", "confidence": "unknown",
                }
        self._last_activity = act
        self.activity_state = act

    def _persist_round(self) -> None:
        """
        1.3.3 WARM：Token 样本落库（消费 HOT 轮预校验通过的 pending 样本）。

        HOT 轮只更新内存；历史库写入保持既有 2s 节奏（1s 实时刷新不会
        加倍 SQLite 写入，spec §24）。无 pending（离线/无效/上一轮已消费）
        时不写库——绝不重放旧样本（会误关缺口、重复计数 delta）。
        落库时间戳用样本自身的采集时刻（HOT 轮 now），缺口判定以采样
        间隔为准。
        """
        pending = self._pending_sample
        if pending is None:
            return
        self._pending_sample = None
        normalized, metrics, now_wall, now_mono = pending
        self._persist_valid_sample(normalized, metrics, now_wall, now_mono)

    async def _poll_status(self) -> None:
        """WARM：/status（5s 节流；辅助源，失败保留上一轮值——故障隔离）。"""
        if not self.endpoints.get("status").should_poll(self.clock.monotonic()):
            return
        raw, ok, _ = await self._fetch_json(
            "/status", MAX_SMALL_BYTES, timeout=WARM_TIMEOUT, endpoint="status")
        if ok and isinstance(raw, dict):
            # 隐私边界：tail / tool 剥离（tail 是正在生成的实际文本）
            self._status = {k: v for k, v in raw.items() if k not in ("tail", "tool")}
            self.endpoint_ok["status"] = True
        else:
            # 保留上一轮值（stale）；端点健康由 EndpointState 表达
            self.endpoint_ok["status"] = False

    async def _poll_slots(self) -> None:
        """WARM：/slots（10s；Slot 信息/数量/辅助诊断——不参与 activity）。"""
        if not self.endpoints.get("slots").should_poll(self.clock.monotonic()):
            return
        raw, ok, _ = await self._fetch_json(
            "/slots", MAX_SMALL_BYTES, timeout=WARM_TIMEOUT, endpoint="slots")
        if ok and isinstance(raw, list):
            self._slots = raw
            self.endpoint_ok["slots"] = True
        else:
            self.endpoint_ok["slots"] = False

    async def _poll_health(self) -> None:
        """WARM：/health（10s；metrics 失败时立即重探——区分 unreachable/monitoring_error）。"""
        if not self.endpoints.get("health").should_poll(self.clock.monotonic()):
            return
        raw, ok, _ = await self._fetch_json(
            "/health", MAX_SMALL_BYTES, timeout=WARM_TIMEOUT, endpoint="health")
        if ok and isinstance(raw, dict):
            self._health = build_health_info(raw)
            self._note_model_loaded()
            self.endpoint_ok["health"] = True
        else:
            # 探测失败：旧 health 不可再当权威（否则"整体不可达"误判为"监测异常"）
            self._health = None
            self.endpoint_ok["health"] = False

    async def _poll_metadata(self) -> None:
        """COLD：/props + /v1/models（模型元数据；600s + 重连）。"""
        if not self.endpoints.get("props").should_poll(self.clock.monotonic()):
            return
        props_raw, props_ok, _ = await self._fetch_json(
            "/props", MAX_SMALL_BYTES, timeout=COLD_TIMEOUT, endpoint="props")
        models_raw, models_ok, _ = await self._fetch_json(
            "/v1/models", MAX_SMALL_BYTES, timeout=COLD_TIMEOUT, endpoint="models")
        self.endpoint_ok["props"] = props_ok and isinstance(props_raw, dict)
        self.endpoint_ok["models"] = models_ok and isinstance(models_raw, dict)
        if props_ok and isinstance(props_raw, dict):
            self._props = props_raw
        if models_ok and isinstance(models_raw, dict):
            self._models = models_raw
        self._refresh_static()
        self._reconnect_pending = False

    async def collect_once(self) -> dict:
        """
        一轮完整采集（1.3.3 兼容入口：测试 / 独立模式 / lifespan 首轮）。

        顺序与 1.3.2 一致（辅助端点先行，live 状态用最新 status 构建）：
        /status 每轮；/slots 每 2 轮、/health 每 5 轮（metrics 失败立即重探）、
        /props+/v1/models 首轮/重连/每 300 轮 -> HOT /metrics 轮 -> WARM 落库。
        1.3.3 生产路径改用 start() 多任务分层（HOT 1s / WARM 2s / COLD）。
        """
        self._round += 1
        await self._poll_status()
        poll_health = (
            self._reconnect_pending
            or self._round % 5 == 1
            or self.endpoints.get("metrics").consecutive_failures > 0
        )
        if self._reconnect_pending or self._round % 2 == 1:
            await self._poll_slots()
        if poll_health:
            await self._poll_health()
        if self._reconnect_pending or self._round % 300 == 1:
            await self._poll_metadata()
        snapshot = await self._hot_round()
        try:
            self._persist_round()
        except Exception as exc:
            logger.exception("Strata WARM 落库轮错误（不影响 HOT）: %r", exc)
        # 1.3.2 低频遥测兼容：未走 start() 的 COLD 任务时（测试 / 独立模式 /
        # lifespan 首轮），保留 15s histogram + 300s /v1/status 轮次调度
        if not self._started:
            now_mono = self.clock.monotonic()
            if now_mono - self._last_histo_mono >= HISTOGRAM_INTERVAL:
                self._last_histo_mono = now_mono
                try:
                    await self._poll_histogram_and_hardware()
                except Exception as exc:
                    logger.debug("Strata 遥测轮错误（延迟/硬件）: %r", exc)
            if (not self._v1status_done) or \
                    (now_mono - self._last_v1status_mono >= V1_STATUS_INTERVAL):
                self._last_v1status_mono = now_mono
                try:
                    await self._poll_v1_status()
                except Exception as exc:
                    logger.debug("Strata /v1/status 轮错误: %r", exc)
        return snapshot

    def realtime_payload(self) -> dict:
        """
        1.3.3 /api/realtime 数据源（spec §12/§31）：HOT PATH 最新态聚合。

        - activity：_compute_activity（§4 优先级；metrics.live 权威）；
        - throughput：current=live.tok_s（服务端实时口径）；
          average=tok_s_mean（会话平均）；current_prompt_tps 无实时口径 = None
          （prefill_tok_s_mean 是平均 Prefill，归 average 侧）；
        - requests：processing=running；waiting=waiting_total（官方 vLLM 口径
          queued+waiting）；queued=queued 原始值；
        - slots：active=outside_slots；total=/slots 快照长度（WARM 10s 节奏）；
        - tokens：当前请求 prompt/generated/max_tokens（未生成时 None，绝不 0）；
        - freshness：sample_timestamp = 最近 HOT 成功（§14）；
        - diagnostics：HOT 循环诊断 + 各端点健康（§16/§31）。
        严格 0-vs-null：缺失/不支持字段 = None。
        """
        now = self.clock.now()
        act = self.activity_state or {}
        live = self.live or {}
        snap = self.last_snapshot or {}
        total_slots = len(self._slots) if self._slots else None
        age_ms = (
            int(max(0.0, (now - self.sample_timestamp) * 1000))
            if self.sample_timestamp is not None else None
        )
        # errors：仅不可用端点（可用端点不占 payload）
        _errors = {}
        for _name in self.endpoints.names():
            _st = self.endpoints.get(_name)
            if not _st.available:
                _errors[_name] = _st.to_dict(now)
        return {
            "provider": "strata",
            "server_online": bool(snap.get("online")),
            "activity": {
                "active": act.get("active"),
                "state": act.get("state"),
                "phase": act.get("phase"),
                "source": act.get("source"),
                "confidence": act.get("confidence"),
            },
            "throughput": {
                "current_decode_tps": live.get("tok_s"),
                "current_prompt_tps": None,
                "average_decode_tps": live.get("tok_s_mean"),
                "average_prompt_tps": live.get("prefill_tok_s_mean"),
            },
            "requests": {
                "processing": live.get("running"),
                "waiting": live.get("waiting_total"),
                "queued": live.get("queued"),
            },
            "slots": {
                "active": live.get("outside_slots"),
                "total": total_slots,
            },
            "tokens": {
                "prompt": live.get("prompt_tokens"),
                "generated": live.get("generated"),
                "max_tokens": live.get("max_tokens"),
            },
            "freshness": {
                "sample_timestamp": self.sample_timestamp,
                "age_ms": age_ms,
                "source": "strata.metrics",
            },
            "errors": _errors,
            "diagnostics": {
                "hot_loop_interval_ms": int(round(self.hot_interval * 1000)),
                "last_hot_duration_ms": int(round(self.last_hot_duration_s * 1000)),
                "hot_overrun": self.hot_overrun,
                "hot_overrun_count": self.hot_overrun_count,
                "source": {
                    name: self.endpoints.get(name).to_dict(now)
                    for name in ("metrics", "slots", "status")
                },
            },
        }

    # ---- 1.3.3 任务编排（start / 循环 / 取消） ----

    def start(self) -> None:
        """
        启动 1.3.3 分层采集任务（幂等；lifespan 在首轮 collect_once 后调用）。

        - HOT 任务（1s deadline）：_hot_round（只 /metrics + 内存更新）；
        - WARM 任务（poll_interval，默认 2s）：_warm_round（DB 落库 +
          /status 5s + /slots 10s + /health 10s，各端点独立故障隔离）；
        - COLD 任务（single-flight 后台任务）：histogram 15s /
          /v1/status 300s / props+models 600s——上一轮未完成则跳过本轮，
          绝不阻塞 HOT PATH；exception 捕获、CancelledError 传播。
        """
        if self._started:
            return
        self._started = True
        loop = asyncio.get_running_loop()
        self._hot_task = loop.create_task(self._hot_loop(), name="strata-hot")
        self._warm_task = loop.create_task(self._warm_loop(), name="strata-warm")
        self._cold_tasks = [
            loop.create_task(
                self._cold_loop(self._histo_work, HISTOGRAM_INTERVAL, "histo"),
                name="strata-histo"),
            loop.create_task(
                self._cold_loop(self._v1status_work, V1_STATUS_INTERVAL, "v1status"),
                name="strata-v1status"),
            loop.create_task(
                self._cold_loop(self._metadata_work, METADATA_INTERVAL, "metadata"),
                name="strata-metadata"),
        ]

    async def _hot_loop(self) -> None:
        """HOT 循环：monotonic deadline 调度（不漂移/不重叠/不追赶）。

        GAP-006 语义：_hot_round 抛异常（理论上已内部捕获一切）只记日志，
        本轮仍用最近快照输出（每轮都有输出，循环不空转）。
        """
        sched = DeadlineScheduler(self.hot_interval, self.clock, name="strata-hot")

        async def _work() -> None:
            try:
                snap = await self._hot_round()
            except Exception:
                logger.exception("Strata HOT 轮异常（已记录，继续下一轮）")
                snap = self.last_snapshot
            # 诊断镜像（/api/realtime diagnostics）
            self.last_hot_duration_s = sched.last_duration_s
            self.hot_overrun = sched.last_overrun
            self.hot_overrun_count = sched.overrun_count
            cb = self.on_hot_round
            if cb is not None and snap is not None:
                try:
                    cb(snap)
                except Exception:
                    logger.exception("Strata on_hot_round 回调异常（不影响采集）")

        await sched.run(_work)

    async def _warm_loop(self) -> None:
        """WARM 循环：DB 落库 + 辅助端点（各自节流、故障隔离）。"""
        sched = DeadlineScheduler(self.interval, self.clock, name="strata-warm")

        async def _work() -> None:
            try:
                self._persist_round()
            except Exception as exc:
                logger.exception("Strata WARM 落库轮错误: %r", exc)
            now_mono = self.clock.monotonic()
            metrics_dead = self.endpoints.get("metrics").consecutive_failures > 0
            if now_mono - self._last_status_mono >= STATUS_INTERVAL:
                self._last_status_mono = now_mono
                await self._poll_status()
            if now_mono - self._last_slots_mono >= SLOTS_INTERVAL:
                self._last_slots_mono = now_mono
                await self._poll_slots()
            # metrics 失败时立即重探 health（区分 unreachable / monitoring_error）
            if metrics_dead or now_mono - self._last_health_mono >= HEALTH_INTERVAL:
                self._last_health_mono = now_mono
                await self._poll_health()

        await sched.run(_work)

    async def _cold_loop(self, work, interval: float, name: str) -> None:
        """
        COLD single-flight 循环（§10）：每类低频任务最多一个 in-flight；
        上一轮未完成（慢 endpoint 拖延）则跳过本轮——绝不堆积、
        绝不影响 HOT PATH。
        """
        sched = DeadlineScheduler(interval, self.clock, name=f"strata-{name}")

        async def _guarded() -> None:
            if name in self._cold_inflight:
                logger.debug("Strata COLD %s 上一轮未完成，跳过本轮（single-flight）", name)
                return
            self._cold_inflight.add(name)
            try:
                await work()
            finally:
                self._cold_inflight.discard(name)

        await sched.run(_guarded)

    async def _histo_work(self) -> None:
        """COLD：延迟 Histogram + 后端硬件落库（15s；?format=prometheus）。"""
        if not self.endpoints.get("prometheus").should_poll(self.clock.monotonic()):
            return
        try:
            await self._poll_histogram_and_hardware()
        except Exception as exc:
            logger.debug("Strata 遥测轮错误（延迟/硬件，不影响 HOT）: %r", exc)
            self.endpoints.get("prometheus").record_failure(
                self.clock.now(), self.clock.monotonic(), "temporarily_unavailable")

    async def _v1status_work(self) -> None:
        """COLD：/v1/status uptime/并发（300s；重启归零）。"""
        if not self.endpoints.get("v1status").should_poll(self.clock.monotonic()):
            return
        try:
            await self._poll_v1_status()
        except Exception as exc:
            logger.debug("Strata /v1/status 轮错误（不影响 HOT）: %r", exc)
            self.endpoints.get("v1status").record_failure(
                self.clock.now(), self.clock.monotonic(), "temporarily_unavailable")

    async def _metadata_work(self) -> None:
        """COLD：/props + /v1/models 模型元数据（600s + 重连）。"""
        await self._poll_metadata()

    async def stop_tasks(self) -> None:
        """取消全部后台采集任务（shutdown；幂等；CancelledError 不外泄）。"""
        tasks = [t for t in (self._hot_task, self._warm_task, *self._cold_tasks)
                 if t is not None]
        for t in tasks:
            if not t.done():
                t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._hot_task = None
        self._warm_task = None
        self._cold_tasks = []
        self._cold_inflight.clear()
        self._started = False


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

    # ---- 1.3.2：低频遥测（延迟 Histogram + 后端硬件 + /v1/status） ----

    async def _poll_histogram_and_hardware(self) -> None:
        """
        15s 低频遥测（spec §8/§10/§13）：
        1) GET /metrics?format=prometheus -> 解析 3 个 vLLM 延迟 Histogram
           （TTFT / ITL / E2E），按 (metric, model) 对相邻采样求 bucket delta
           （Counter Reset 感知）落库 latency_histogram；
        2) 上一轮 /metrics 的 hardware 摘要落库 strata_hardware（PCIe 趋势用；
           复用已抓数据，不发额外请求）。

        失败只记 debug——延迟/硬件遥测缺失不影响 Token 主流程。
        """
        if self.db is None:
            return
        # 1) 延迟 Histogram（content negotiation：?format=prometheus + Accept text/plain）
        # 1.3.3：COLD 独立任务（_histo_work 驱动），绝不进 HOT PATH；
        # 本方法阻塞多久都不影响 /metrics 的 1s 节奏（single-flight 保护）。
        try:
            text, ok, _ = await self._fetch_text(
                "/metrics?format=prometheus", MAX_METRICS_BYTES,
                headers={"Accept": "text/plain"},
                timeout=COLD_TIMEOUT, endpoint="prometheus")
        except asyncio.CancelledError:
            raise
        except Exception:
            ok, text = False, None
        if ok and text is not None:
            self._histo_capable = True
            self.endpoint_ok["prometheus"] = True
            from prometheus_parser import (
                bucket_interval_deltas, histogram_bucket_delta,
                parse_histogram_series, parse_prometheus,
            )
            fams = parse_prometheus(text)
            _METRIC_MAP = {
                "vllm:time_to_first_token_seconds": "ttft",
                "vllm:inter_token_latency_seconds": "itl",
                "vllm:e2e_request_latency_seconds": "e2e",
            }
            seen: set = set()
            now = self.clock.now()
            for fname, mkey in _METRIC_MAP.items():
                fam = fams.get(fname)
                if fam is None:
                    continue
                for series in parse_histogram_series(fam):
                    model = series.labels.get("model_name") or ""
                    key = (mkey, model)
                    seen.add(key)
                    prev = self._histo_prev.get(key)
                    # 首次采样只建基线（delta 全 0，不落库）
                    if prev is not None:
                        # 每 le 累计差（reset 感知）-> 每桶区间 delta（落库形态）
                        le_deltas = histogram_bucket_delta(
                            prev["buckets"], series.buckets)
                        deltas = bucket_interval_deltas(le_deltas)
                        if any(d > 0 for d in deltas.values()):
                            self.db.record_latency_histogram(
                                self.server_id, mkey, model, deltas, now=now)
                    self._histo_prev[key] = {"buckets": dict(series.buckets)}
            # 周期重置/模型切换：消失的序列基线作废（避免跨周期错误 delta）
            for key in list(self._histo_prev.keys()):
                if key not in seen:
                    del self._histo_prev[key]
        else:
            if self._histo_capable is None:
                self._histo_capable = False
                self.endpoint_ok["prometheus"] = False

        # 2) 后端硬件采样（复用本轮 /metrics 的 hardware，无额外请求）
        hw_live = (self.backend_hardware or {}).get("live") or {}
        from strata_parser import _num
        sample = {
            "gpu_util": _num(hw_live.get("gpu_util")),
            "gpu_mem_used": _num(hw_live.get("gpu_mem_used")),
            "gpu_mem_total": _num(hw_live.get("gpu_mem_total")),
            "gpu_temp": _num(hw_live.get("gpu_temp")),
            "gpu_power": _num(hw_live.get("gpu_power")),
            "pcie_rx_mb": _num(hw_live.get("gpu_pcie_rx_mb")),
            "pcie_tx_mb": _num(hw_live.get("gpu_pcie_tx_mb")),
            "cpu": _num(hw_live.get("cpu")),
            "ram_used": _num(hw_live.get("ram_used")),
            "ram_total": _num(hw_live.get("ram_total")),
        }
        sample = {k: v for k, v in sample.items() if v is not None}
        if sample:
            self.db.record_strata_hardware(self.server_id, sample,
                                           now=self.clock.now())

    def known_le_axes(self) -> dict[str, list[str]]:
        """
        每 metric 已知的完整 le 轴（来自最近一次 /metrics?format=prometheus
        解析的**全部分桶**，含零 delta 桶）。

        用途：latency_histogram 只落 d>0 的桶，窗口聚合会丢失零桶——
        /api/strata/latency 用本轴对聚合结果补零，保证 histogram_quantile
        的桶下界正确（否则 (1, 2.5] 的样本会被当成 (0, 2.5]，P50 偏低）。

        返回 {mkey: [le_raw, ...]}（按 le 数值升序；多模型取并集）。
        从未采样成功时返回 {}。
        """
        from prometheus_parser import le_sort_key
        axes: dict[str, set] = {}
        for (mkey, _model), prev in self._histo_prev.items():
            buckets = prev.get("buckets") if isinstance(prev, dict) else None
            if buckets:
                axes.setdefault(mkey, set()).update(buckets.keys())
        return {
            mkey: sorted(les, key=le_sort_key)
            for mkey, les in axes.items()
        }

    async def _poll_v1_status(self) -> None:
        """
        /v1/status 低频轮询（5 分钟 + 重连）：uptime / concurrency /
        last_request_at（内存态；服务重启后 uptime 归零——不跨重启累加）。
        1.3.3：COLD 独立任务（_v1status_work 驱动）——卡住/失败绝不影响
        实时 activity（它只负责 uptime/并发）。
        """
        payload, ok, _ = await self._fetch_json(
            "/v1/status", MAX_SMALL_BYTES, timeout=COLD_TIMEOUT, endpoint="v1status")
        if not ok or not isinstance(payload, dict):
            if self._v1status_done:
                self.endpoint_ok["v1status"] = False
            return
        self.endpoint_ok["v1status"] = True
        from strata_parser import _num
        conc = payload.get("concurrency")
        act = payload.get("activity")
        info = {
            "uptime_s": _num(payload.get("uptime_s")),
            "started": _num(payload.get("started")),
            "concurrency_serving": _num(conc.get("serving"))
            if isinstance(conc, dict) else None,
            "concurrency_requested": _num(conc.get("requested"))
            if isinstance(conc, dict) else None,
            "last_request_at": _num(act.get("last_request_at"))
            if isinstance(act, dict) else None,
        }
        # 全部为 None（结构不认识）-> 保持上一次，不覆盖
        if any(v is not None for v in info.values()):
            self._uptime_info = info
        self._v1status_done = True

    async def fetch_all_requests(self, limit: int = 60) -> tuple[list, bool]:
        """
        按需"查看更多"最近请求（spec §15）：/metrics?requests=all 一次性拉取
        （**只**在本端点被调用时发请求——不进轮询循环），30s 内存缓存防抖。
        返回 (entries, ok)；ok=False 时 entries=[]。
        """
        mono = self.clock.monotonic()
        cached = self._requests_all_cache
        if cached is not None and (mono - cached[0]) < REQUESTS_ALL_TTL:
            return cached[1], True
        payload, ok, _ = await self._fetch_json(
            "/metrics?requests=all", MAX_METRICS_BYTES, timeout=WARM_TIMEOUT)
        entries: list = []
        if ok and isinstance(payload, dict):
            entries = build_recent_requests(payload, limit=REQUESTS_ALL_MAX)
        if ok:
            self._requests_all_cache = (mono, entries)
        return entries, ok

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
        """完整处理一个 /metrics 样本（1.3.2 兼容入口：校验+落库一次完成）。

        1.3.3 分层路径：HOT 轮预校验（_hot_round）+ WARM 轮
        _persist_valid_sample 落库；本方法保留供独立/测试路径。
        """
        totals = metrics.get("totals")
        normalized = normalize_totals_sample(totals, self._namespaced_state())
        if normalized is None:
            # 约束违反/核心字段缺失：保持旧 baseline + invalid_metrics 缺口
            self._invalid_streak += 1
            self._gaps.on_invalid(now_wall, now_mono)
            snap = self._online_snapshot(metrics)
            snap["_valid"] = False
            return snap

        return self._persist_valid_sample(normalized, metrics, now_wall, now_mono)

    def _persist_valid_sample(
        self, normalized: dict, metrics: dict,
        now_wall: float, now_mono: float,
    ) -> dict:
        """1.3.3 WARM：预校验通过的样本落库（单事务）+ 缺口关闭 + 事件。"""
        totals = metrics.get("totals")
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
        """常驻采集（1.3.3：start() 分层多任务；独立进程模式，与 MetricsCollector.run 同契约）。

        默认参数不能写成 `out=sys.stdout.write`——默认值在类定义（import）时求值，
        而 PyInstaller --windowed 下 sys.stdout 是 None（与 collector.py 同注意）。
        out 每 HOT 轮（1s）收到一份 JSON 快照文本（独立模式调试输出）。
        """
        if out is None:
            out = sys.stdout.write if sys.stdout is not None else (lambda *_a, **_k: None)

        def _emit(snap: dict) -> None:
            out(json.dumps(
                {k: v for k, v in snap.items() if not k.startswith("_")},
                ensure_ascii=False, indent=2) + "\n")

        # 首轮完整采集（与 1.3.3 前一致：辅助端点 + metrics + 落库）
        try:
            snapshot = await self.collect_once()
        except Exception as exc:
            snapshot = self._offline_snapshot()
            snapshot["error"] = repr(exc)
        try:
            _emit(snapshot)
        except Exception as exc:
            logger.exception("Strata 采集首轮输出异常（已记录，后续 HOT 轮继续）: %r", exc)
        self.on_hot_round = _emit
        self.start()
        if self._hot_task is not None:
            try:
                await self._hot_task
            except asyncio.CancelledError:
                # run 被 cancel（停机/Ctrl+C）：同步停止后台任务，不泄漏
                if self._started:
                    await self.stop_tasks()
                raise
