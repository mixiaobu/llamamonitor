"""
collector.py — llama.cpp 纯旁路 Metrics 采集器（Phase 1/2/7 + Phase 11 可靠性）

行为：
- 定期 GET llama-server 的 /metrics（地址/间隔/超时全部来自统一配置，
  见 config.py：config.json 的 llama_server / collector 段），解析后打印结构化 JSON 快照；
- 只读取 /metrics 端点：不代理 OpenAI API、不修改请求、不启停/重启 llama-server、
  不接管 9091 端口。llama-server 独立运行，本进程崩溃也不影响它；
- llama-server 离线 / 超时 / 解析异常：本轮快照 online=false、全部字段 None，
  采集循环继续运行，程序不会因单次失败退出；服务器恢复后自动继续采集；
- 每次采集成功后，在一个 SQLite 事务内完成
  1) 更新 state（最后一次 Counter）
  2) 累加 daily_usage（按成功采集样本的时间戳归属本机日期——
     "Token usage is bucketed by the timestamp of the successful collection sample"）
  3) 插入 live_samples
  4) 删除超过保留时长的 live_samples（config.collector.live_retention_hours）
  数据库写入失败（重试后仍失败）只记录日志 + 状态不变（baseline 不更新，
  下一轮从旧 baseline 重新计算完整 delta），不破坏采集循环（stdout 保持纯 JSON）。

Counter delta 规则（llama-server 重启会让 Counter 从 0 重新开始）：
- 程序首次读到该 Counter：只建 baseline，delta = 0；
- current >= previous：delta = current - previous；
- current < previous：判定重启，delta = current，并记录 counter_reset 事件；
- 每个 Counter 独立做 reset detection（缺失的 Counter 保持旧 baseline，
  绝不把 None 当 0）；程序重启后从 state 表恢复，避免重复/漏计。

Phase 11 可靠性 / 数据正确性：
- 时间：wall clock 只用于日期/时间戳/日志；间隔与 gap 检测用 monotonic
  （clock.Clock 注入；FakeClock 用于测试/soak）；
- 样本有效性：HTTP 200 但两个核心 Counter（prompt / output）都缺失/不可解析
  -> sample_invalid：不更新 state、不产生 delta（保持最后有效 baseline）；
- 单指标 NaN/Inf/负值 -> 视为本轮缺失（保持旧 baseline）+ invalid_metric_value 事件；
- 已知缺口（data_gaps）：server_offline / monitor_restart / system_pause_or_sleep /
  invalid_metrics；缺口结束（下一个有效样本）时才写库并判定 possible_token_loss：
  核心 Counter 在恢复样本上发生 reset 且缺口 > poll*3 -> 可能有不可恢复 token 丢失
  （绝不"造数"补全丢失量）；
- 监控中（在线、无断档）的 counter reset 最多丢一个采集间隔的数据（采样粒度，
  不记 gap，README 已声明）；
- 日志：只记录状态转换 / 异常（不逐轮刷 ERROR）。

运行方式（项目根目录）：
    python collector.py                 # 常驻采集 + 落库（间隔见 config.json）
    python collector.py --once          # 只抓取一次后退出
    python collector.py --file f.txt    # 解析本地 metrics 文件（仅显示，不落库）
    python collector.py --no-db         # 不落库（纯显示）
    python collector.py --db PATH       # 覆盖数据库路径
    python collector.py --url URL --interval 10   # 覆盖 llama-server 地址 / 采集间隔
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

import httpx

from clock import Clock, default_clock
from config import AppConfig, apply_overrides, load_config, setup_logging
from db import Database, local_date
from metrics_parser import get_metric_by_label, get_metric_value, parse_metrics
from stats import TRACKED_COUNTERS, COUNTER_SHORT_NAMES, compute_deltas, counter_delta, mtp_accept_rate, tps

logger = logging.getLogger("llamamonitor.collector")

# Phase 11：核心 Counter（prompt / output）。两个都缺失/不可解析 -> sample_invalid。
# （cached / MTP 等辅助 Counter 缺失时只保持各自 baseline，不影响样本有效性。）
CORE_COUNTERS = (
    "llamacpp:prompt_tokens_total",
    "llamacpp:tokens_predicted_total",
)

# 缺口判定阈值倍数：相邻有效样本（monotonic）间隔 > poll_interval * 该值 -> 已知缺口
GAP_THRESHOLD_FACTOR = 3.0

# AUDIT-DATA-003：/metrics 响应大小上限（16 MB）。正常 llama-server 的 /metrics
# 只有几 KB；超过即视为异常响应（防御性上限），按离线处理，保护 LlamaMonitor 内存。
MAX_METRICS_BYTES = 16 * 1024 * 1024

# llama.cpp 指标全名 -> 快照键名
# 键名与后续阶段 UI / 数据库使用的字段保持一致；缺失的指标一律为 None。
_METRIC_KEYS: dict[str, str] = {
    "llamacpp:prompt_tokens_total": "prompt_tokens_total",
    "llamacpp:prompt_tokens_cached_total": "cached_tokens_total",
    "llamacpp:prompt_seconds_total": "prompt_seconds_total",
    "llamacpp:tokens_predicted_total": "output_tokens_total",
    "llamacpp:tokens_predicted_seconds_total": "output_seconds_total",
    "llamacpp:n_decode_total": "n_decode_total",
    "llamacpp:n_tokens_max": "n_tokens_max",
    "llamacpp:spec_decode_num_draft_tokens_total": "draft_tokens_total",
    "llamacpp:spec_decode_num_accepted_tokens_total": "accepted_tokens_total",
    "llamacpp:spec_decode_num_drafts_total": "spec_drafts_total",
    "llamacpp:requests_processing": "requests_processing",
    "llamacpp:requests_deferred": "requests_deferred",
    "llamacpp:n_busy_slots_per_decode": "n_busy_slots_per_decode",
    "llamacpp:kv_cache_usage_ratio": "kv_cache_usage_ratio",  # Phase 9：KV Cache 使用率（0~1 gauge，存在才显示）
}

# Capability Detection（Phase 9）：llama.cpp 版本之间指标集合不完全一致，
# 每次成功解析后记录哪些功能可用（只记布尔值，不存原始 Prometheus 文本）
_CAPABILITY_SOURCES = {
    "kv_cache": "llamacpp:kv_cache_usage_ratio",
    "mtp": "llamacpp:spec_decode_num_draft_tokens_total",
    "requests": "llamacpp:requests_processing",
    "busy_slots": "llamacpp:n_busy_slots_per_decode",
    "n_tokens_max": "llamacpp:n_tokens_max",
    "drafts": "llamacpp:spec_decode_num_drafts_total",
}


def detect_capabilities(parsed: dict) -> dict:
    """
    由一次成功解析的结果计算能力位图（功能是否存在）。

    - 指标存在 -> True；不存在 -> False（缺失不报错、不崩溃）；
    - mtp_positions：仅当 per-position accepted 指标存在时为 True。
    """
    caps = {key: get_metric_value(parsed, name) is not None for key, name in _CAPABILITY_SOURCES.items()}
    caps["mtp_positions"] = parsed.get(_SPEC_PER_POS_METRIC) is not None
    return caps

# 特殊带标签指标：spec-decode 按位置的接受 token 数
_SPEC_PER_POS_METRIC = "llamacpp:spec_decode_num_accepted_tokens_per_pos_total"
_SPEC_PER_POS_KEY = "spec_accepted_per_position"


def _spec_pos_state_key(position: str) -> str:
    """
    per-position accepted 计数器在 state 表中的键。

    形如 'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position=0}'，
    与无标签指标的全名天然区分，互不冲突。
    """
    return f"{_SPEC_PER_POS_METRIC}{{position={position}}}"

# context_max 的候选指标：优先 llamacpp:context_max，
# 部分构建只提供 llamacpp:context_available（可用上下文），都没有则 None（UI 显示 N/A）
_CONTEXT_CANDIDATES = ("llamacpp:context_max", "llamacpp:context_available")


def _to_json_number(value: float | None):
    """
    把 Prometheus float 转成便于 JSON 展示的数字：

    - None / NaN -> None（NaN 不是合法 JSON 数值，按不可用处理）；
    - +-Inf -> None（标准 JSON 无法表达，按不可用处理）；
    - 整数值 float -> int（168507.0 -> 168507，保持输出干净）；
    - 其余保持 float。
    """
    if value is None or value != value:  # NaN
        return None
    if value in (float("inf"), float("-inf")):
        return None
    return int(value) if float(value).is_integer() else float(value)


def _context_max(parsed: dict) -> float | None:
    """读取 context_max gauge；候选指标都缺失时返回 None。"""
    for name in _CONTEXT_CANDIDATES:
        value = get_metric_value(parsed, name)
        if value is not None:
            return value
    return None


def build_snapshot(parsed: dict) -> dict:
    """由 parse_metrics 结果构建结构化快照；缺失字段一律为 None。"""
    snapshot: dict = {"online": True}
    for metric_name, key in _METRIC_KEYS.items():
        snapshot[key] = _to_json_number(get_metric_value(parsed, metric_name))
    per_position = get_metric_by_label(parsed, _SPEC_PER_POS_METRIC, "position")
    if per_position is None:
        snapshot[_SPEC_PER_POS_KEY] = None
    else:
        # position 为数字时按数值排序，非数字排在后面
        ordered = sorted(
            per_position.items(),
            key=lambda kv: (0, int(kv[0])) if kv[0].isdigit() else (1, kv[0]),
        )
        snapshot[_SPEC_PER_POS_KEY] = {pos: _to_json_number(val) for pos, val in ordered}
    # context_max gauge（候选指标都缺失时为 None）
    snapshot["context_max"] = _to_json_number(_context_max(parsed))
    return snapshot


def offline_snapshot() -> dict:
    """离线快照：键集与 build_snapshot 完全一致，字段全 None，online=false。"""
    snapshot: dict = {"online": False}
    for key in _METRIC_KEYS.values():
        snapshot[key] = None
    snapshot[_SPEC_PER_POS_KEY] = None
    snapshot["context_max"] = None
    return snapshot


class MetricsCollector:
    """
    常驻异步采集器。

    - 单次抓取的任何异常都视为离线，绝不向外抛出；
    - 配置 db（Database）后，每次采集成功会在单事务内落库（见模块 docstring）。
    """

    def __init__(self, config: AppConfig, db: Database | None = None, clock: Clock | None = None) -> None:
        """
        显式接收统一配置（config.load_config() 的结果），不在 import 时创建全局单例。
        metrics_url / interval / timeout 全部由 config 派生：
        config.llama_server.{url,metrics_path,timeout_seconds} /
        config.collector.poll_interval_seconds

        clock：Phase 11 时间抽象（None -> SystemClock）。测试/soak 注入 FakeClock。
        """
        self.config = config
        self.metrics_url = config.metrics_url
        self.interval = config.collector.poll_interval_seconds
        self.timeout = config.llama_server.timeout_seconds
        self.db = db
        self.clock = clock or default_clock
        self.last_db_error: Exception | None = None
        self.last_snapshot: dict | None = None  # 最近一次快照（含离线轮）
        self.last_update: float | None = None   # 最近一次采集完成时刻（Unix 秒）
        self._last_online: bool | None = None   # 上一轮在线状态（用于状态变化日志）
        # Phase 9：最近一次成功解析的指标能力位图（/api/runtime 的 capabilities 用）
        self.capabilities: dict = {}
        # 跨轮复用的 HTTP 客户端（keep-alive 连接池）：部分 Windows 机器上"每次
        # 新建连接的首个请求"有 ~1s 级开销，复用连接后单轮抓取从 ~1.5s 降到 ~10ms。
        self._http: httpx.AsyncClient | None = None
        self._http_loop = None
        # ---- Phase 11：缺口 / 数据质量追踪（全部内存态，写库才持久化） ----
        # 最近一次"有效样本"的 (wall, mono)；用于 monotonic 间隔判定 gap/sleep
        self._last_valid: tuple[float, float] | None = None
        # 当前未结束的已知缺口：dict(start_wall, reason) 或 None。
        # 缺口在"下一个有效样本"到来时才写 data_gaps（end/duration/possible_token_loss 才能确定）
        self._open_gap: dict | None = None
        # 上一轮是否"有效样本"（用于 sample_invalid 状态转换日志/事件）
        self._last_sample_valid: bool | None = None
        # monitor_restart 缺口：启动时若发现 state 有旧 baseline（上一进程留下的），
        # 首个有效样本到来时记一条 monitor_restart 缺口（上一进程最后写库 -> 本次）。
        # 由 server/desktop 在启动时调用 maybe_note_restart_gap() 置位。
        self._restart_pending_since: float | None = None
        # 上一轮检测到 reset 的核心 Counter（用于"恢复样本"判定 possible_token_loss）
        self._core_reset_on_last_sample: bool = False
        # 本轮值不可用（NaN/Inf/负值）的指标名集合（invalid_metric_value 状态转换去重用）
        self._invalid_metrics: set[str] = set()

    # ---- Phase 11：缺口 / 重启 钩子 ------------------------------------------------

    def gap_threshold(self) -> float:
        """相邻有效样本（monotonic）间隔超过该秒数视为已知缺口。"""
        return self.interval * GAP_THRESHOLD_FACTOR

    def maybe_note_restart_gap(self) -> None:
        """
        启动时调用一次：若 state 表已有 baseline（说明本进程不是第一次运行 monitor），
        记录"上一进程最后写库时刻"，首个有效样本到来时补记 monitor_restart 缺口。
        全新库（无任何 baseline）不记（没有可对比的上一段）。
        """
        if self.db is None or self._restart_pending_since is not None:
            return
        try:
            _, last = self.db.get_first_and_last_sample()
        except Exception:
            last = None
        if last is not None:
            self._restart_pending_since = float(last)

    def open_gap_property(self) -> dict | None:
        """供 API 读取当前未结束缺口（{start_timestamp, reason, duration_seconds}）。"""
        if self._open_gap is None:
            return None
        elapsed = self.clock.now() - self._open_gap["start_wall"]
        return {
            "start_timestamp": self._open_gap["start_wall"],
            "reason": self._open_gap["reason"],
            "duration_seconds": max(0.0, elapsed),
        }

    def _maybe_open_gap(self, now_wall: float, now_mono: float, reason: str) -> None:
        """在 now 时刻开启一个缺口（若已有未结束缺口则不重复开启）。"""
        if self._open_gap is not None:
            return
        self._open_gap = {"start_wall": now_wall, "start_mono": now_mono, "reason": reason}

    def _close_open_gap(self, now_wall: float, core_reset: bool) -> None:
        """
        在 now 时刻结束未结束缺口并写库。
        possible_token_loss = 缺口期间核心 Counter 发生过 reset 且间隔超过阈值
        （llama-server 在断档中重启：断档中生成的 token 无法从 Counter 恢复）。
        """
        gap = self._open_gap
        if gap is None:
            return
        self._open_gap = None
        if self.db is None:
            return
        duration = max(0.0, now_wall - gap["start_wall"])
        if duration <= 0:
            return
        # 间隔未超过阈值：只是正常抖动，不落库（避免 data_gaps 被 1s 级小缺口刷屏）
        # （monitor_restart / sleep 等长缺口天然远超阈值）
        if duration < self.gap_threshold() and gap["reason"] not in ("monitor_restart",):
            return
        loss = bool(core_reset) and duration >= self.gap_threshold()
        try:
            self.db.record_gap(
                start_ts=gap["start_wall"],
                end_ts=now_wall,
                source="llama",
                reason=gap["reason"],
                token_recoverable=not loss,
                possible_token_loss=loss,
                now=now_wall,
            )
            if loss:
                logger.warning(
                    "已知缺口结束（%s, %.0fs）：期间核心 Counter 发生 reset，"
                    "可能有不可恢复 token 丢失", gap["reason"], duration,
                )
            else:
                logger.info("已知缺口结束（%s, %.0fs）", gap["reason"], duration)
        except Exception as exc:
            logger.warning("写入缺口记录失败（不影响采集）: %r", exc)

    def _get_client(self) -> httpx.AsyncClient:
        """懒创建并复用 AsyncClient；事件循环变化（测试多 asyncio.run 场景）时重建。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if self._http is None or self._http_loop is not loop:
            # AUDIT-ASYNC-004：keepalive 过期时间必须显著大于采集间隔——默认 5s 与
            # 常见 5s 间隔相等，导致每轮 TCP 全重连（稳态 ~48 个 TIME_WAIT/小时）。
            # 下限 30s，并覆盖 interval 的 4 倍（interval 大时 keep-alive 仍然有效）。
            keepalive = max(30.0, self.interval * 4)
            self._http = httpx.AsyncClient(
                timeout=self.timeout,
                limits=httpx.Limits(keepalive_expiry=keepalive),
            )
            self._http_loop = loop
        return self._http

    async def aclose(self) -> None:
        """关闭 HTTP 客户端（幂等；由 lifespan 关闭路径调用）。"""
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None
            self._http_loop = None

    def shutdown(self) -> None:
        """
        优雅停止（幂等；lifespan/desktop 关闭路径调用）：
        1) 落盘当前未结束缺口（进程退出前最后已知状态）；
        2) 记 monitor_stop 事件。
        与 aclose() 分工：shutdown 管 DB 侧收尾，aclose 管 HTTP 收尾。
        """
        if getattr(self, "_shutdown_done", False):
            return
        self._shutdown_done = True
        if self.db is None:
            return
        now = self.clock.now()
        if self._open_gap is not None:
            gap = self._open_gap
            self._open_gap = None
            duration = max(0.0, now - gap["start_wall"])
            if duration >= self.gap_threshold():
                try:
                    self.db.record_gap(
                        start_ts=gap["start_wall"], end_ts=now, source="llama",
                        reason=gap["reason"], token_recoverable=True,
                        possible_token_loss=False, now=now,
                    )
                except Exception as exc:
                    logger.warning("退出时写入未结束缺口失败: %r", exc)
        if self._restart_pending_since is not None:
            # 进程在首个有效样本前退出：补记 monitor_restart 缺口
            since = self._restart_pending_since
            self._restart_pending_since = None
            duration = max(0.0, now - since)
            if duration >= self.gap_threshold():
                try:
                    self.db.record_gap(
                        start_ts=since, end_ts=now, source="application",
                        reason="monitor_restart", token_recoverable=True,
                        possible_token_loss=False, now=now,
                    )
                except Exception as exc:
                    logger.warning("退出时写入 monitor_restart 缺口失败: %r", exc)
        try:
            self.db.record_event("monitor_stop", "info", "collector", {}, now=now)
        except Exception as exc:
            logger.warning("写入 monitor_stop 事件失败: %r", exc)

    async def _fetch_parsed(self) -> dict | None:
        """
        抓取并解析 /metrics。

        成功返回 parse_metrics 结果；任何失败（网络错误/超时/非 200）返回 None（按离线处理）。
        测试中可用同名属性覆盖此方法来注入固定文本。

        AUDIT-DATA-003：流式读取 + MAX_METRICS_BYTES 上限——原 `response.text` 无大小
        限制，异常 metrics server 一次返回数 GB 会把 LlamaMonitor 内存打爆。
        """
        try:
            client = self._get_client()
            async with client.stream("GET", self.metrics_url) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_METRICS_BYTES:
                        logger.warning(
                            "/metrics 响应超过 %.0f MB 上限，按离线处理: %s",
                            MAX_METRICS_BYTES / 1048576, self.metrics_url,
                        )
                        return None
                    chunks.append(chunk)
            text = b"".join(chunks).decode("utf-8", "replace")
            return parse_metrics(text)
        except Exception:
            return None

    async def collect_once(self) -> dict:
        """
        抓取并解析一次，返回快照。任何失败返回离线快照，不抛异常。

        Phase 11 每轮流程（时钟：wall 用于时间戳/日期，monotonic 用于间隔判定）：
        1. 抓取失败 -> 离线快照；若无未结束缺口则开启 server_offline 缺口
        2. HTTP 200 但核心 Counter（prompt/output）全缺失 -> sample_invalid：
           不更新 state、不产生 delta；开启 invalid_metrics 缺口（状态转换才记事件）
        3. 有效样本：
           - 先结束未结束缺口（用本轮是否检测到核心 reset 判定 possible_token_loss）
           - monotonic 间隔 > 阈值（本轮前一轮都有效、中间无离线轮）-> 补记
             system_pause_or_sleep 缺口（睡眠/系统暂停：进程被挂起，无离线轮可记）
           - persist_sample（单事务：state + daily + live；失败 -> 保持旧 baseline）
           - 恢复 monitor_restart 缺口（启动时置位的上一进程断档）
        4. 状态转换（online/offline、valid/invalid）才写日志 + 事件，不逐轮刷屏
        """
        now_wall = self.clock.now()
        now_mono = self.clock.monotonic()
        parsed = await self._fetch_parsed()

        if parsed is None:
            snapshot = offline_snapshot()
            # 离线轮：开启 server_offline 缺口（起点 = 上一个有效样本的 wall+mono）
            start_wall, start_mono = (
                (self._last_valid[0], self._last_valid[1]) if self._last_valid else (now_wall, now_mono)
            )
            self._maybe_open_gap(start_wall, start_mono, "server_offline")
        else:
            snapshot = build_snapshot(parsed)
            self.capabilities = detect_capabilities(parsed)
            # sample_invalid 判定：HTTP 200 但**没有任何可用 Counter**（metrics 为空 /
            # 全是不可解析值）。个别 Counter 缺失（含核心 prompt/output）不算 invalid——
            # 按"缺失保持旧 baseline"的 per-metric 规则处理。
            counter_values = [get_metric_value(parsed, name) for name in TRACKED_COUNTERS]
            per_pos = get_metric_by_label(parsed, _SPEC_PER_POS_METRIC, "position") or {}
            counter_values.extend(per_pos.values())
            valid = any(self._is_usable_number(v) for v in counter_values)
            if not valid:
                # 不更新 state、不产生 delta（保持最后有效 baseline）
                start_wall, start_mono = (
                    (self._last_valid[0], self._last_valid[1])
                    if self._last_valid else (now_wall, now_mono)
                )
                self._maybe_open_gap(start_wall, start_mono, "invalid_metrics")
            else:
                snapshot = self._handle_valid_sample(parsed, snapshot, now_wall, now_mono)

        # 在线/离线 + 有效/无效 状态转换才写日志与事件（正常每轮采集不打日志）
        online = snapshot["online"]
        sample_valid = bool(parsed is not None and snapshot.get("_valid", False))
        if self._last_online is not None and online != self._last_online:
            if online:
                logger.info("llama-server 恢复在线: %s", self.metrics_url)
                if self.db is not None:
                    self.db.record_event("server_online", "info", "collector",
                                         {"url": self.metrics_url}, now=now_wall)
            else:
                logger.warning("llama-server 离线（%s 抓取失败），采集循环继续", self.metrics_url)
                if self.db is not None:
                    self.db.record_event("server_offline", "warning", "collector",
                                         {"url": self.metrics_url}, now=now_wall)
        self._last_online = online
        # valid/invalid 转换只在 metrics 实际可读（parsed 非 None）时判定：
        # 离线轮（抓取失败）的"无效"由 server_offline 状态/缺口表达，
        # 不在此处重复记 invalid_metrics 事件（避免一次离线开始产生两条事件）
        if parsed is not None:
            if self._last_sample_valid is not None and sample_valid != self._last_sample_valid:
                if sample_valid:
                    logger.info("metrics 恢复有效（核心 Counter 可读）")
                    if self.db is not None:
                        self.db.record_event("metrics_valid", "info", "collector", {}, now=now_wall)
                else:
                    logger.warning("sample_invalid：metrics 可读但核心 Counter（prompt/output）缺失或不可解析，本轮不落盘")
                    if self.db is not None:
                        self.db.record_event("invalid_metrics", "warning", "collector", {}, now=now_wall)
            self._last_sample_valid = sample_valid
        # 记录最新快照与完成时刻（供 FastAPI /api/status 读取）
        self.last_snapshot = snapshot
        self.last_update = self.clock.now()
        return snapshot

    def _is_usable_number(self, value) -> bool:
        """Counter 值是否可用：非 None、非 NaN/Inf、非负（负值视为不可解析）。"""
        if value is None:
            return False
        try:
            f = float(value)
        except (TypeError, ValueError):
            return False
        return f == f and f not in (float("inf"), float("-inf")) and f >= 0

    def _handle_valid_sample(self, parsed: dict, snapshot: dict, now_wall: float, now_mono: float) -> dict:
        """有效样本的完整处理：结束缺口 / 睡眠检测 / 持久化 / 重启缺口。"""
        # 1) 本轮是否检测到核心 Counter reset（persist 前预判，供缺口判定 possible_token_loss）
        previous_state = self.db.get_state() if self.db is not None else {}
        core_reset = False
        for name in CORE_COUNTERS:
            current = get_metric_value(parsed, name)
            previous = previous_state.get(name)
            if self._is_usable_number(current) and previous is not None and float(current) < float(previous):
                core_reset = True

        # 2) 结束未结束缺口（server_offline / invalid_metrics）。
        #    保持开缺口时的原因：期间有轮询轮次发生（缺口由离线/无效轮开启）说明
        #    进程一直在运行，不能仅凭"monotonic 跨度大"改判为睡眠——5 分钟的
        #    真实 server 离线同样有 5 分钟的 monotonic 跨度。真正的睡眠/暂停
        #    （期间**没有**任何轮询轮次）由下面第 3 步的 mono 跳变检测记录。
        gap_was_open = self._open_gap is not None
        if self._open_gap is not None:
            self._close_open_gap(now_wall, core_reset)

        # 3) 睡眠/系统暂停：上一轮与本轮都有效（期间无离线/无效轮，即本样本前
        #    没有开启过缺口），但 monotonic 间隔超阈值（进程被系统挂起：
        #    offline 轮根本没发生，只能靠 monotonic 跳变发现）
        if (
            not gap_was_open
            and self._last_valid is not None
            and (now_mono - self._last_valid[1]) > self.gap_threshold()
        ):
            sleep_start_wall = self._last_valid[0]
            loss = core_reset  # 暂停期间 server 是否重启：以恢复样本的 reset 判定
            if self.db is not None:
                duration = max(0.0, now_wall - sleep_start_wall)
                self.db.record_gap(
                    start_ts=sleep_start_wall, end_ts=now_wall, source="llama",
                    reason="system_pause_or_sleep",
                    token_recoverable=not (loss and duration >= self.gap_threshold()),
                    possible_token_loss=bool(loss and duration >= self.gap_threshold()),
                    now=now_wall,
                )
                logger.warning("检测到系统暂停/睡眠（monotonic 间隔 %.0fs > 阈值 %.0fs），已记缺口",
                               now_mono - self._last_valid[1], self.gap_threshold())
                self.db.record_event(
                    "sleep_gap", "warning", "collector",
                    {"monotonic_gap_seconds": now_mono - self._last_valid[1]}, now=now_wall,
                )

        # 4) 持久化（单事务；失败 -> 保持旧 baseline，下轮重算完整 delta）
        # AUDIT-SEC-004：unavailable 若由**本进程写失败**触发（last_db_error 非空），
        # 必须继续尝试写——否则 protective mode 会跳过 persist，恢复分支永远执行不到，
        # 状态永久卡死。unavailable 来自启动 quick_check（last_db_error 为空）时保持
        # 原来的只读保护。
        write_blocked = (
            self.db is not None
            and self.db.health in ("corrupt", "unavailable", "incompatible")
            and self.last_db_error is None
        )
        if write_blocked:
            # protective mode：数据库不健康 -> 只读展示，停止修改类写（状态转换才记日志）
            if not getattr(self, "_protective_logged", False):
                logger.error("数据库处于 %s 状态（protective mode）：暂停写入，只读继续",
                             self.db.health)
                self._protective_logged = True
                self.db.record_event("database_protective_mode", "warning", "database",
                                     {"health": self.db.health}, now=now_wall)
        elif self.db is not None:
            if getattr(self, "_protective_logged", False):
                self._protective_logged = False
                logger.info("数据库恢复健康，写入继续")
            try:
                detail = self.persist_sample(parsed, now=now_wall, core_reset_seen=core_reset)
                # 数据库从错误中恢复：上一轮写失败、本轮成功
                if self.last_db_error is not None:
                    logger.info("数据库写入恢复（上轮失败: %r）", self.last_db_error)
                    # AUDIT-SEC-004：配对恢复——上轮因只读置为 unavailable 的 health
                    # 在本轮写成功后回到 healthy（protective mode 自动解除）
                    if self.db.health == "unavailable":
                        self.db.set_health("healthy", None)
                    self.db.record_event("database_recovery", "info", "collector",
                                         {"previous_error": repr(self.last_db_error)}, now=now_wall)
                    self.last_db_error = None
            except Exception as exc:
                self.last_db_error = exc
                logger.warning("数据库写入失败（重试后仍失败），本轮跳过落盘、保持旧 baseline: %r", exc)
                # AUDIT-SEC-004："read-only database" 是持续状态（如整个数据目录被
                # 设为只读 / 磁盘满），原实现 health 永远保持 "healthy"，/api/health
                # 一直报 healthy 而数据实际全部丢失——现在更新健康状态。
                # （事件写入在只读库上也会失败，静默忽略：health 状态本身就是信号）
                if "readonly" in str(exc).lower() and self.db is not None and self.db.health == "healthy":
                    self.db.set_health("unavailable", f"database write failed: {exc!r}")
                    logger.error("数据库进入持续只读状态：health 更新为 unavailable（/api/health 同步反映）")
                    try:
                        self.db.record_event("database_write_failure", "error", "collector",
                                             {"error": repr(exc)}, now=now_wall)
                    except Exception:
                        pass

        # 5) monitor_restart 缺口：启动时置位 -> 首个有效样本时补记（上一进程断档）
        if self._restart_pending_since is not None:
            since_wall = self._restart_pending_since
            self._restart_pending_since = None
            duration = max(0.0, now_wall - since_wall)
            if duration >= self.gap_threshold() and self.db is not None:
                loss = bool(core_reset)
                self.db.record_gap(
                    start_ts=since_wall, end_ts=now_wall, source="application",
                    reason="monitor_restart",
                    token_recoverable=not loss, possible_token_loss=loss, now=now_wall,
                )
                self.db.record_event("monitor_restart_gap", "warning", "application",
                                     {"duration_seconds": duration}, now=now_wall)
                logger.warning("monitor 重启缺口 %.0fs（%s -> 现在）", duration,
                               local_date(since_wall))

        # 记录有效样本时刻（wall + mono）
        self._last_valid = (now_wall, now_mono)
        snapshot["_valid"] = True
        return snapshot

    # 非 seconds 的 Counter 都是"整数计数"（token / 次数），以 int 存储；
    # *_seconds_total 是真实小数（秒），以 float 存储。
    @staticmethod
    def _is_seconds_counter(name: str) -> bool:
        return name.endswith("_seconds_total")

    def _sanitize_counters(self, parsed: dict, previous_state: dict, now: float) -> dict:
        """
        读取所有被跟踪 Counter 的当前值并做 Phase 11 净化：
        - NaN / +Inf / -Inf / 负值 -> 视为本轮缺失（返回 None），保持旧 baseline，
          且（状态转换时）记一条 invalid_metric_value 事件——绝不把坏值当 0 或当 reset；
        - 合法值原样返回（float）。
        返回 {指标全名: float | None}。
        """
        raw = {name: get_metric_value(parsed, name) for name in TRACKED_COUNTERS}
        # per-position 也纳入净化（独立 Counter）
        per_pos = get_metric_by_label(parsed, _SPEC_PER_POS_METRIC, "position") or {}
        for pos, value in per_pos.items():
            raw[_spec_pos_state_key(pos)] = value

        new_invalid: set[str] = set()
        cleaned: dict[str, float | None] = {}
        for name, value in raw.items():
            if value is None:
                cleaned[name] = None
                continue
            if self._is_usable_number(value):
                cleaned[name] = float(value)
            else:
                cleaned[name] = None
                new_invalid.add(name)

        # invalid_metric_value 事件：只在"上轮可用 -> 本轮坏值"的状态转换时记（不逐轮刷屏）
        if self.db is not None:
            newly_bad = new_invalid - getattr(self, "_invalid_metrics", set())
            for name in sorted(newly_bad):
                self.db.record_event(
                    "invalid_metric_value", "warning", "collector",
                    {"metric": name, "raw": raw[name]}, now=now,
                )
        self._invalid_metrics = new_invalid
        return cleaned

    def persist_sample(
        self,
        parsed: dict,
        now: float | None = None,
        daily_date: str | None = None,
        core_reset_seen: bool = False,
    ) -> dict:
        """
        由解析结果计算 delta / TPS / MTP，并在单个事务内写库（只在采集成功后调用）。

        - now: Unix 秒（默认当前时刻），同时用于 daily 归属日期与 48h 清理；
        - daily_date: 显式指定 daily 归属日期（测试用），默认取 now 对应的本机日期；
        - 缺失/坏值的 Counter 字段：delta=None，state 保持最后一次保存的值不更新
          （绝不把 None 当 0，也绝不把 None 当 reset）；
        - Counter reset（current < previous）：记 counter_reset 事件 + 日志；
        - core_reset_seen: 调用方（collect_once）在本轮已判定的核心 reset，
          用于缺口 possible_token_loss（persist 内独立重算用于事件，二者同源一致）。
        返回本轮计算明细（deltas / prompt_tps / decode_tps / mtp_accept_rate / daily_date）。
        """
        now = self.clock.now() if now is None else now
        daily = daily_date if daily_date is not None else local_date(now)

        previous_state = self.db.get_state()
        # Phase 11：先净化（NaN/Inf/负值 -> None + invalid_metric_value 事件）
        current_values = self._sanitize_counters(parsed, previous_state, now)
        deltas = compute_deltas(current_values, previous_state)

        # Counter reset 检测 -> 日志 + counter_reset 事件（llama-server 重启；每个 Counter 独立）
        for name in TRACKED_COUNTERS:
            current = current_values.get(name)
            previous = previous_state.get(name)
            if current is not None and previous is not None and current < previous:
                logger.warning("检测到 Counter 重置: %s (%.0f -> %.0f)", name, previous, current)
                if self.db is not None:
                    self.db.record_event(
                        "counter_reset", "warning", "collector",
                        {"metric": name, "previous": previous, "current": current},
                        now=now,
                    )

        # 本轮要保存的 Counter：缺失/坏值字段不出现（state 保持旧值）；
        # token 计数以 int 存储，seconds 以 float 存储
        state_values: dict[str, float | int] = {}
        for name, value in current_values.items():
            if value is None:
                continue
            state_values[name] = value if self._is_seconds_counter(name) else int(value)

        # per-position accepted 计数器：每个 position 都是独立 Counter，
        # 同样的 reset detection；首次出现的 position 只建 baseline（delta=0）
        per_position = {
            k.split("position=")[-1].rstrip("}"): v
            for k, v in current_values.items()
            if k.startswith(_SPEC_PER_POS_METRIC) and v is not None
        }
        position_deltas: dict[str, float] = {}
        for pos, value in per_position.items():
            previous = previous_state.get(_spec_pos_state_key(pos))
            if previous is not None and value < previous:
                logger.warning("检测到 Counter 重置: %s (position=%s)", _SPEC_PER_POS_METRIC, pos)
                if self.db is not None:
                    self.db.record_event(
                        "counter_reset", "warning", "collector",
                        {"metric": f"{_SPEC_PER_POS_METRIC}{{position={pos}}}",
                         "previous": previous, "current": value},
                        now=now,
                    )
            delta = counter_delta(value, previous)
            state_values[_spec_pos_state_key(pos)] = int(value)
            if delta is not None:
                position_deltas[pos] = delta

        # TPS / MTP（分母 <=0 或分子缺失时为 None）
        prompt_tps = tps(deltas["prompt_tokens"], deltas["prompt_seconds"])
        decode_tps = tps(deltas["output_tokens"], deltas["predicted_seconds"])
        mtp = mtp_accept_rate(deltas["accepted_tokens"], deltas["draft_tokens"])

        # live_samples 行（gauge 直接取本轮值，缺失为 None）
        live_row = {
            "timestamp": int(now),
            "prompt_delta": deltas["prompt_tokens"],
            "cached_delta": deltas["cached_tokens"],
            "output_delta": deltas["output_tokens"],
            "prompt_tps": prompt_tps,
            "decode_tps": decode_tps,
            "requests_processing": get_metric_value(parsed, "llamacpp:requests_processing"),
            "requests_deferred": get_metric_value(parsed, "llamacpp:requests_deferred"),
            "context_max": _context_max(parsed),
            "mtp_accept_rate": mtp,
            # Phase 9：KV Cache 使用率 / 忙碌槽位（服务器无该指标时 NULL）
            "kv_cache_usage_ratio": get_metric_value(parsed, "llamacpp:kv_cache_usage_ratio"),
            "busy_slots": get_metric_value(parsed, "llamacpp:n_busy_slots_per_decode"),
        }

        # daily 累加增量（缺失字段按 0 计）
        daily_increments = {
            "prompt_tokens": deltas["prompt_tokens"] or 0,
            "cached_tokens": deltas["cached_tokens"] or 0,
            "output_tokens": deltas["output_tokens"] or 0,
            "draft_tokens": deltas["draft_tokens"] or 0,
            "accepted_tokens": deltas["accepted_tokens"] or 0,
            "prompt_seconds": deltas["prompt_seconds"] or 0,
            "predicted_seconds": deltas["predicted_seconds"] or 0,
            "draft_sequences": int(deltas["draft_sequences"] or 0),  # Phase 9
        }

        self.db.apply_sample(
            state_values,
            live_row,
            daily,
            daily_increments,
            now=now,
            position_increments={pos: delta for pos, delta in position_deltas.items() if delta > 0},
        )
        return {
            "deltas": deltas,
            "position_deltas": position_deltas,
            "prompt_tps": prompt_tps,
            "decode_tps": decode_tps,
            "mtp_accept_rate": mtp,
            "daily_date": daily,
        }

    async def run(self, out=None) -> None:
        """常驻采集循环：每个间隔打印一条 JSON 快照，自身永不退出。

        注意：默认参数不能写成 `out=sys.stdout.write`——默认值在**类定义（import）
        时求值**，而 PyInstaller --windowed 下 sys.stdout 是 None，会把整个 EXE
        启动直接崩掉（'NoneType' object has no attribute 'write'）。
        """
        if out is None:
            out = sys.stdout.write if sys.stdout is not None else (lambda *_a, **_k: None)
        while True:
            try:
                snapshot = await self.collect_once()
            except Exception as exc:  # 双保险，理论上 collect_once 已捕获一切
                snapshot = offline_snapshot()
                snapshot["error"] = repr(exc)
            # _valid 是内部标记（Phase 11 样本有效性），不进 JSON 输出
            out_json = {k: v for k, v in snapshot.items() if not k.startswith("_")}
            out(json.dumps(out_json, ensure_ascii=False, indent=2) + "\n")
            await asyncio.sleep(self.interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="llama.cpp 纯旁路 metrics 采集器（参数来自 config.json）")
    parser.add_argument("--url", default=None, help="llama-server 基础地址（覆盖 config.json）")
    parser.add_argument("--interval", type=float, default=None, help="采集间隔秒数（覆盖 config.json）")
    parser.add_argument("--once", action="store_true", help="只抓取一次后退出（验证用）")
    parser.add_argument("--file", default=None, help="解析本地 metrics 文本文件一次后退出（仅显示，不落库）")
    parser.add_argument("--db", default=None, help="SQLite 路径（覆盖 config.json）")
    parser.add_argument("--no-db", action="store_true", help="不写数据库（纯显示）")
    args = parser.parse_args()

    if args.file is not None:
        # 离线模式：直接解析本地文件，不依赖 llama-server，也不落库
        text = Path(args.file).read_text(encoding="utf-8")
        print(json.dumps(build_snapshot(parse_metrics(text)), ensure_ascii=False, indent=2))
        return

    # 先挂载日志 handler（默认级别），保证 load_config 的警告/错误能写入 monitor.log
    setup_logging()
    loaded = load_config()
    cfg = loaded.config
    apply_overrides(cfg, url=args.url, db_path=args.db, interval=args.interval)
    setup_logging(cfg.logging)  # 幂等：按配置调整级别

    db = None
    if not args.no_db:
        db = Database(
            cfg.database_path,
            wal=cfg.database.wal,
            retention_seconds=cfg.collector.live_retention_hours * 3600,
        )

    collector = MetricsCollector(cfg, db)
    # Phase 11：monitor 启动事件 + monitor_restart 缺口检测（上一进程断档）
    if db is not None:
        collector.maybe_note_restart_gap()
        try:
            db.record_event("monitor_start", "info", "collector",
                            {"poll_interval_seconds": cfg.collector.poll_interval_seconds},
                            now=collector.clock.now())
        except Exception:
            logger.warning("写入 monitor_start 事件失败", exc_info=True)
    try:
        if args.once:
            async def _once():
                try:
                    return await collector.collect_once()
                finally:
                    await collector.aclose()

            print(json.dumps(asyncio.run(_once()), ensure_ascii=False, indent=2))
        else:
            async def _run_loop():
                try:
                    await collector.run()
                finally:
                    await collector.aclose()  # 同一事件循环内关闭

            asyncio.run(_run_loop())
    except KeyboardInterrupt:
        pass
    finally:
        collector.shutdown()  # Phase 11：落未结束缺口 + monitor_stop 事件
        if db is not None:
            db.close()


if __name__ == "__main__":
    main()
