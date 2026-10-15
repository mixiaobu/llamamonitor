"""
gap_tracker.py — 采集缺口 / 重启断档 / 睡眠检测的共享追踪器（1.3.0 抽取）

从 collector.py（llama.cpp）抽取，供 llama.cpp 与 Strata 两个 Token 采集器
共用。行为与原实现严格一致（原有单测是行为基准）：

- 时间：wall clock 只用于时间戳/日期/日志；间隔判定用 monotonic（Clock 注入）；
- server_offline / invalid_metrics 缺口：由离线/无效轮开启，在**下一个有效
  样本**到来时才写 data_gaps（end/duration/possible_token_loss 才能确定）；
- possible_token_loss = 缺口期间核心 Counter 发生 reset 且间隔超阈值
  （服务在断档中重启：断档中产生的 token 无法从 Counter 恢复）；
- system_pause_or_sleep：上一轮与本轮都有效（期间无离线/无效轮）但
  monotonic 间隔超阈值（进程被系统挂起，无离线轮可记）；
- monitor_restart：启动时若 state 有旧 baseline（上一进程留下的），首个
  有效样本到来时补记；
- 进程退出（shutdown）：落盘未结束缺口（保守：token_recoverable=True）。

单轮异常不影响主循环；所有 DB 写入失败只记日志。
"""

from __future__ import annotations

import logging
from typing import Callable

from clock import Clock, default_clock
from db import local_date

logger = logging.getLogger("llamamonitor.gaps")

GAP_THRESHOLD_FACTOR = 3.0

# 1.3.1 连续失败容忍（1.2.1「<5 次保留上次数据」语义，两个后端采集器共用）：
# 连续 <OFFLINE_TOLERANCE 次抓取失败（llama.cpp：抓取失败/无效 metrics；
# Strata：/metrics 失败）保留最近一次有效数据与状态——不翻转状态、不记事件、
# 字段不置空；连续 >=5 次才切换到失败表现。成功一次立即清零。
# 与前端 LM.poll.streakFail（/api/status 拉取失败容忍）阈值一致。
OFFLINE_TOLERANCE = 5


class GapTracker:
    """缺口/断档追踪（内存态；写库才持久化）。db 为 None 时全部空转。"""

    def __init__(
        self,
        interval: float,
        clock: Clock | None = None,
        gap_threshold_factor: float = GAP_THRESHOLD_FACTOR,
    ) -> None:
        self.interval = interval
        self.clock = clock or default_clock
        self._threshold_factor = gap_threshold_factor
        self._last_valid: tuple[float, float] | None = None
        self._open_gap: dict | None = None
        self._restart_pending_since: float | None = None
        self._gap_source = "llama"  # 开缺口时的 source 标注（shutdown 落盘用）

    # ---- 阈值 / 启动钩子 ----

    def gap_threshold(self) -> float:
        return self.interval * self._threshold_factor

    def maybe_note_restart_gap(self, get_last_sample: Callable[[], float | None]) -> None:
        """
        启动时调用一次：若 state 有旧 baseline（上一进程留下），记录其最后
        写库时刻；首个有效样本到来时补记 monitor_restart 缺口。
        全新库（无任何 baseline）不记。get_last_sample 抛异常按 None 处理。
        """
        if self._restart_pending_since is not None:
            return
        try:
            last = get_last_sample()
        except Exception:
            last = None
        if last is not None:
            self._restart_pending_since = float(last)

    def open_gap_property(self) -> dict | None:
        """供 API 读取当前未结束缺口。"""
        if self._open_gap is None:
            return None
        elapsed = self.clock.now() - self._open_gap["start_wall"]
        return {
            "start_timestamp": self._open_gap["start_wall"],
            "reason": self._open_gap["reason"],
            "duration_seconds": max(0.0, elapsed),
        }

    # ---- 开 / 关缺口 ----

    def open_gap(
        self, reason: str, now_wall: float, now_mono: float,
        source: str | None = None,
    ) -> None:
        """在 now 开启缺口（已有未结束缺口则不重复开启）。"""
        if self._open_gap is not None:
            return
        self._open_gap = {
            "start_wall": now_wall, "start_mono": now_mono, "reason": reason,
            "source": source or self._gap_source,
        }

    def _gap_start(self, now_wall: float, now_mono: float) -> tuple[float, float]:
        """缺口起点 = 上一个有效样本的 wall+mono（无则现在）。"""
        if self._last_valid is not None:
            return self._last_valid
        return now_wall, now_mono

    def on_offline(self, now_wall: float, now_mono: float) -> None:
        start_wall, start_mono = self._gap_start(now_wall, now_mono)
        self.open_gap("server_offline", start_wall, start_mono)

    def on_invalid(self, now_wall: float, now_mono: float) -> None:
        start_wall, start_mono = self._gap_start(now_wall, now_mono)
        self.open_gap("invalid_metrics", start_wall, start_mono)

    @property
    def open_gap_was_open(self) -> bool:
        return self._open_gap is not None

    def close_open_gap(
        self, db, now_wall: float, core_reset: bool, source: str = "llama",
    ) -> bool:
        """
        在 now 结束未结束缺口并写库。返回是否曾有缺口。
        possible_token_loss = 核心 Counter reset 且间隔超阈值。
        间隔未超阈值且非 monitor_restart：正常抖动，不落库。
        """
        gap = self._open_gap
        was_open = gap is not None
        if gap is None or db is None:
            return was_open
        self._open_gap = None
        duration = max(0.0, now_wall - gap["start_wall"])
        if duration <= 0:
            return was_open
        if duration < self.gap_threshold() and gap["reason"] != "monitor_restart":
            return was_open
        loss = bool(core_reset) and duration >= self.gap_threshold()
        try:
            db.record_gap(
                start_ts=gap["start_wall"], end_ts=now_wall, source=source,
                reason=gap["reason"], token_recoverable=not loss,
                possible_token_loss=loss, now=now_wall,
            )
            if loss:
                logger.warning(
                    "已知缺口结束（%s, %.0fs）：期间核心 Counter 发生 reset，"
                    "可能有不可恢复 token 丢失", gap["reason"], duration,
                )
            else:
                logger.info("已知缺口结束（%s, %.0fs)", gap["reason"], duration)
        except Exception as exc:
            logger.warning("写入缺口记录失败（不影响采集）: %r", exc)
        return was_open

    def on_sleep_gap(
        self, db, now_wall: float, now_mono: float, core_reset: bool,
        source: str = "llama",
    ) -> bool:
        """
        睡眠/系统暂停检测：上一轮与本轮都有效（调用方保证未开缺口）但
        monotonic 间隔超阈值。返回是否记了缺口。
        """
        if self._last_valid is None:
            return False
        if (now_mono - self._last_valid[1]) <= self.gap_threshold():
            return False
        sleep_start_wall = self._last_valid[0]
        duration = max(0.0, now_wall - sleep_start_wall)
        loss = bool(core_reset) and duration >= self.gap_threshold()
        if db is not None:
            try:
                db.record_gap(
                    start_ts=sleep_start_wall, end_ts=now_wall, source=source,
                    reason="system_pause_or_sleep",
                    token_recoverable=not loss,
                    possible_token_loss=loss, now=now_wall,
                )
                db.record_event(
                    "sleep_gap", "warning", "collector",
                    {"monotonic_gap_seconds": now_mono - self._last_valid[1]},
                    now=now_wall,
                )
            except Exception as exc:
                logger.warning("写入睡眠缺口失败（不影响采集）: %r", exc)
        logger.warning(
            "检测到系统暂停/睡眠（monotonic 间隔 %.0fs > 阈值 %.0fs），已记缺口",
            now_mono - self._last_valid[1], self.gap_threshold(),
        )
        return True

    def on_valid_sample(self, now_wall: float, now_mono: float) -> None:
        self._last_valid = (now_wall, now_mono)

    def finalize_restart_gap(
        self, db, now_wall: float, core_reset: bool, source: str = "application",
    ) -> None:
        """monitor_restart 缺口：启动时置位 -> 首个有效样本时补记。"""
        since = self._restart_pending_since
        if since is None:
            return
        self._restart_pending_since = None
        duration = max(0.0, now_wall - since)
        if duration < self.gap_threshold() or db is None:
            return
        loss = bool(core_reset)
        try:
            db.record_gap(
                start_ts=since, end_ts=now_wall, source=source,
                reason="monitor_restart", token_recoverable=not loss,
                possible_token_loss=loss, now=now_wall,
            )
            db.record_event(
                "monitor_restart_gap", "warning", "application",
                {"duration_seconds": duration}, now=now_wall,
            )
        except Exception as exc:
            logger.warning("写入 monitor_restart 缺口失败: %r", exc)
        logger.warning("monitor 重启缺口 %.0fs（%s -> 现在）", duration, local_date(since))

    # ---- 退出收尾 ----

    def finalize_on_shutdown(self, db, now: float | None = None) -> None:
        """
        进程退出：落盘未结束缺口（保守：token_recoverable=True）+
        重启断档补记。
        """
        if db is None:
            return
        now = self.clock.now() if now is None else now
        if self._open_gap is not None:
            gap = self._open_gap
            self._open_gap = None
            duration = max(0.0, now - gap["start_wall"])
            if duration >= self.gap_threshold():
                try:
                    db.record_gap(
                        start_ts=gap["start_wall"], end_ts=now,
                        source=gap.get("source", "llama"),
                        reason=gap["reason"], token_recoverable=True,
                        possible_token_loss=False, now=now,
                    )
                except Exception as exc:
                    logger.warning("退出时写入未结束缺口失败: %r", exc)
        if self._restart_pending_since is not None:
            since = self._restart_pending_since
            self._restart_pending_since = None
            duration = max(0.0, now - since)
            if duration >= self.gap_threshold():
                try:
                    db.record_gap(
                        start_ts=since, end_ts=now, source="application",
                        reason="monitor_restart", token_recoverable=True,
                        possible_token_loss=False, now=now,
                    )
                except Exception as exc:
                    logger.warning("退出时写入 monitor_restart 缺口失败: %r", exc)
