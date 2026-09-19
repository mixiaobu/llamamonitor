"""
Phase 11 测试：可靠性 —— counter reset / 重启 / 离线 / 缺口 / sample_invalid /
DB 写失败 / 日志去重。

全部使用临时目录 + FakeClock + 注入的 _fetch_parsed，不触碰真实
%LOCALAPPDATA%\\LlamaMonitor 与 9091。

运行：
    python -m unittest discover -s tests
"""

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from clock import FakeClock
from collector import MetricsCollector
from db import Database
from metrics_parser import parse_metrics
from configutil import make_config

# 固定的 wall 起点：2026-09-15 12:00:00（本地时间无关紧要，测试只用日期一致性）
BASE = 1_789_000_000.0


def metrics_text(
    prompt: int | None = 0,
    output: int | None = 0,
    cached: int | None = 0,
    psec: float | None = 0.0,
    osec: float | None = 0.0,
    draft: int | None = 0,
    accepted: int | None = 0,
    drafts: int | None = 0,
    extra: str = "",
) -> str:
    """构造最小可用的 /metrics 文本（None = 该指标缺失）。"""
    lines = []

    def emit(name, value, fmt="{}"):
        if value is not None:
            lines.append(f"{name} {fmt.format(value)}")

    emit("llamacpp:prompt_tokens_total", prompt)
    emit("llamacpp:tokens_predicted_total", output)
    emit("llamacpp:prompt_tokens_cached_total", cached)
    emit("llamacpp:prompt_seconds_total", psec, "{:g}")
    emit("llamacpp:tokens_predicted_seconds_total", osec, "{:g}")
    emit("llamacpp:n_decode_total", output)
    emit("llamacpp:spec_decode_num_draft_tokens_total", draft)
    emit("llamacpp:spec_decode_num_accepted_tokens_total", accepted)
    emit("llamacpp:spec_decode_num_drafts_total", drafts)
    if extra:
        lines.append(extra)
    return "\n".join(lines) + "\n"


class Driver:
    """collector + db + FakeClock 的测试驱动器。"""

    def __init__(self, tmp: str, poll: float = 5.0, db_path: str = "t.db"):
        self.cfg = make_config(poll_interval=poll)
        self.clock = FakeClock(start_wall=BASE)
        self.db = Database(Path(tmp) / db_path, wal=False, retention_seconds=48 * 3600)
        self.collector = MetricsCollector(self.cfg, self.db, clock=self.clock)
        self._text: str | None = None

    def round(self, text: str | None) -> dict:
        """执行一轮 collect_once：text=None 模拟抓取失败（离线）。"""

        def make_fetch(txt):
            async def fetch():
                return parse_metrics(txt) if txt is not None else None
            return fetch

        self.collector._fetch_parsed = make_fetch(text)
        return asyncio.run(self.collector.collect_once())

    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds)

    def daily_total(self) -> dict:
        return {
            r["date"]: {
                "prompt": r["prompt_tokens"],
                "output": r["output_tokens"],
            }
            for r in self.db.get_daily_usage()
        }

    def gaps(self) -> list[dict]:
        return self.db.get_gaps(limit=100000)

    def events(self, event_type: str | None = None) -> list[dict]:
        rows = self.db.get_events(limit=100000)
        if event_type:
            rows = [r for r in rows if r["event_type"] == event_type]
        return rows

    def close(self) -> None:
        self.collector.shutdown()
        self.db.close()


class TestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def make_driver(self, poll: float = 5.0, db_path: str = "t.db") -> Driver:
        return Driver(self._tmp.name, poll=poll, db_path=db_path)


# ---------------------------------------------------------------------------
# Counter reset
# ---------------------------------------------------------------------------


class CounterResetTests(TestBase):
    def test_reset_records_event_and_delta_is_current_value(self):
        d = self.make_driver()
        try:
            d.round(metrics_text(prompt=1000, output=500))      # baseline
            d.advance(5)
            d.round(metrics_text(prompt=1050, output=520))      # 正常增量
            d.advance(5)
            # llama-server 重启：counter 从 0 重新开始（prompt 到 30，output 到 10）
            snap = d.round(metrics_text(prompt=30, output=10))
            total = d.daily_total()
            # prompt: baseline 0 + 50 + 30（reset 后 delta=current）= 80
            self.assertEqual(sum(v["prompt"] for v in total.values()), 80)
            # output: baseline 0 + 20 + 10（reset）= 30
            self.assertEqual(sum(v["output"] for v in total.values()), 30)
            resets = d.events("counter_reset")
            metrics_ = {e["details"]["metric"]: e["details"] for e in resets}
            # prompt / output / n_decode（与 output 同值）各自独立记录 reset
            self.assertIn("llamacpp:prompt_tokens_total", metrics_)
            self.assertIn("llamacpp:tokens_predicted_total", metrics_)
            self.assertEqual(
                metrics_["llamacpp:prompt_tokens_total"],
                {"metric": "llamacpp:prompt_tokens_total", "previous": 1050.0, "current": 30.0},
            )
            self.assertEqual(
                metrics_["llamacpp:tokens_predicted_total"]["previous"], 520.0,
            )
        finally:
            d.close()

    def test_counters_reset_independently(self):
        d = self.make_driver()
        try:
            d.round(metrics_text(prompt=100, output=100))
            d.advance(5)
            # 只有 prompt 变小（reset），output 正常增长
            d.round(metrics_text(prompt=5, output=120))
            total = d.daily_total()
            # prompt: 0(baseline) + (5 - reset: 5) = 5；output: 0 + 20 = 20
            self.assertEqual(sum(v["prompt"] for v in total.values()), 5)
            self.assertEqual(sum(v["output"] for v in total.values()), 20)
            resets = d.events("counter_reset")
            self.assertEqual([e["details"]["metric"] for e in resets],
                             ["llamacpp:prompt_tokens_total"])
        finally:
            d.close()

    def test_missing_counter_keeps_baseline_no_zero_reset(self):
        """缺失的 Counter 保持旧 baseline，绝不按 0 处理（不产生虚假大负/大正 delta）。"""
        d = self.make_driver()
        try:
            d.round(metrics_text(prompt=100, output=100))
            d.advance(5)
            # 本轮 output 缺失
            d.round(metrics_text(prompt=110, output=None))
            total = d.daily_total()
            self.assertEqual(sum(v["prompt"] for v in total.values()), 10)
            self.assertEqual(sum(v["output"] for v in total.values()), 0)  # 缺失 -> 无增量
            d.advance(5)
            # output 恢复且继续增长（100 -> 130，跨缺失轮不丢不算错）
            d.round(metrics_text(prompt=120, output=130))
            total = d.daily_total()
            self.assertEqual(sum(v["output"] for v in total.values()), 30)
            self.assertEqual(len(d.events("counter_reset")), 0)  # 130 > 100：不是 reset
        finally:
            d.close()


# ---------------------------------------------------------------------------
# 离线 / 缺口 / possible_token_loss
# ---------------------------------------------------------------------------


class OfflineGapTests(TestBase):
    def test_offline_then_recovery_no_loss(self):
        """离线期间 server 正常运行：恢复后 delta 覆盖整个离线期（无丢失，无 gap loss 标记）。"""
        d = self.make_driver(poll=5.0)
        try:
            d.round(metrics_text(prompt=100, output=100))
            d.advance(5)
            # 离线 40 秒（> 3*5=15s 阈值）
            for _ in range(4):
                d.round(None)
                d.advance(10)
            # 恢复：prompt 从 100 涨到 250（离线期真实发生）
            d.round(metrics_text(prompt=250, output=100))
            total = d.daily_total()
            self.assertEqual(sum(v["prompt"] for v in total.values()), 150)
            gaps = d.gaps()
            self.assertEqual(len(gaps), 1)
            g = gaps[0]
            self.assertEqual(g["reason"], "server_offline")
            self.assertEqual(g["source"], "llama")
            self.assertFalse(g["possible_token_loss"])
            self.assertTrue(g["token_recoverable"])
            # 缺口起点 = 最后一个有效样本（BASE），恢复于 BASE+45 -> 45s
            self.assertAlmostEqual(g["duration_seconds"], 45.0, places=3)
        finally:
            d.close()

    def test_offline_with_server_restart_marks_possible_loss(self):
        """离线期间 server 重启（counter 归零）：离线期 token 不可恢复 -> possible_token_loss=1。"""
        d = self.make_driver(poll=5.0)
        try:
            d.round(metrics_text(prompt=100, output=100))
            d.advance(5)
            for _ in range(4):
                d.round(None)
                d.advance(10)
            # 恢复时 counter 变小（server 在离线期间重启）
            d.round(metrics_text(prompt=30, output=30))
            gaps = d.gaps()
            self.assertEqual(len(gaps), 1)
            self.assertTrue(gaps[0]["possible_token_loss"])
            self.assertFalse(gaps[0]["token_recoverable"])
            self.assertEqual(gaps[0]["reason"], "server_offline")
        finally:
            d.close()

    def test_short_offline_below_threshold_not_recorded(self):
        """低于阈值（< 3*interval）的短暂离线不记 gap（避免小抖动刷屏）。"""
        d = self.make_driver(poll=5.0)
        try:
            d.round(metrics_text(prompt=100, output=100))
            d.advance(5)
            d.round(None)          # 离线一轮（5 秒 < 15 秒阈值）
            d.advance(5)
            d.round(metrics_text(prompt=110, output=100))
            self.assertEqual(len(d.gaps()), 0)
            total = d.daily_total()
            self.assertEqual(sum(v["prompt"] for v in total.values()), 10)
        finally:
            d.close()

    def test_server_online_offline_events_dedup(self):
        """连续多轮离线只记一条 server_offline 事件（状态转换去重，不刷屏）。"""
        d = self.make_driver(poll=5.0)
        try:
            d.round(metrics_text(prompt=1, output=1))
            d.advance(5)
            for _ in range(6):
                d.round(None)
                d.advance(5)
            d.advance(5)
            d.round(metrics_text(prompt=2, output=1))
            self.assertEqual(len(d.events("server_offline")), 1)
            self.assertEqual(len(d.events("server_online")), 1)
        finally:
            d.close()

    def test_invalid_metrics_no_state_update(self):
        """sample_invalid：HTTP 200 但无任何 Counter -> 不更新 state、不产生 delta。"""
        d = self.make_driver(poll=5.0)
        try:
            d.round(metrics_text(prompt=100, output=100))
            d.advance(5)
            # 只有 gauge，没有任何 counter
            d.round("llamacpp:requests_processing 3\nllamacpp:requests_deferred 0\n")
            total = d.daily_total()
            self.assertEqual(sum(v["prompt"] for v in total.values()), 0)
            state = d.db.get_state()
            self.assertEqual(state["llamacpp:prompt_tokens_total"], 100.0)  # baseline 未动
            self.assertEqual(len(d.events("invalid_metrics")), 1)
            # 恢复后从旧 baseline 继续
            d.advance(5)
            d.round(metrics_text(prompt=110, output=100))
            total = d.daily_total()
            self.assertEqual(sum(v["prompt"] for v in total.values()), 10)
        finally:
            d.close()

    def test_nan_inf_negative_counter_treated_missing(self):
        """NaN/Inf/负值 counter -> 视为本轮缺失（保持 baseline）+ invalid_metric_value 事件。"""
        d = self.make_driver(poll=5.0)
        try:
            d.round(metrics_text(prompt=100, output=100))
            d.advance(5)
            d.round(metrics_text(prompt="NaN", output=110))
            state = d.db.get_state()
            self.assertEqual(state["llamacpp:prompt_tokens_total"], 100.0)
            self.assertEqual(state["llamacpp:tokens_predicted_total"], 110.0)
            bad = d.events("invalid_metric_value")
            self.assertEqual(len(bad), 1)
            self.assertEqual(bad[0]["details"]["metric"], "llamacpp:prompt_tokens_total")
            d.advance(5)
            # 下一轮又是 NaN：不重复记事件（状态转换去重）
            d.round(metrics_text(prompt="NaN", output=120))
            self.assertEqual(len(d.events("invalid_metric_value")), 1)
            d.advance(5)
            # 负值同样按缺失处理（不是 reset）
            d.round(metrics_text(prompt=-5, output=130))
            state = d.db.get_state()
            self.assertEqual(state["llamacpp:prompt_tokens_total"], 100.0)
            self.assertEqual(len(d.events("counter_reset")), 0)
        finally:
            d.close()


# ---------------------------------------------------------------------------
# 睡眠 / 系统暂停
# ---------------------------------------------------------------------------


class SleepGapTests(TestBase):
    def test_monotonic_jump_without_offline_rounds(self):
        """进程被挂起（无离线轮）：monotonic 跳变 -> system_pause_or_sleep 缺口。"""
        d = self.make_driver(poll=5.0)
        try:
            d.round(metrics_text(prompt=100, output=100))
            d.advance(5)
            # 系统暂停 60 秒（wall 与 mono 同时前进；恢复后第一轮就发现）
            d.advance(60)
            d.round(metrics_text(prompt=105, output=100))
            gaps = d.gaps()
            self.assertEqual(len(gaps), 1)
            self.assertEqual(gaps[0]["reason"], "system_pause_or_sleep")
            self.assertFalse(gaps[0]["possible_token_loss"])  # 无 reset
            self.assertEqual(len(d.events("sleep_gap")), 1)
        finally:
            d.close()

    def test_wall_clock_forward_does_not_create_huge_energy(self):
        """wall 前跳 5 小时（monotonic 只走 10 秒）：不产生 5 小时时长缺口
        （gap 检测基于 monotonic），daily 归属按样本时间戳。"""
        d = self.make_driver(poll=5.0)
        try:
            d.round(metrics_text(prompt=100, output=100))
            # 只前进 10 秒 monotonic，但 wall 直接跳 5 小时
            d.clock.advance(10)
            d.clock.set_wall(d.clock.now() + 5 * 3600)
            snap = d.round(metrics_text(prompt=110, output=100))
            self.assertEqual(len(d.gaps()), 0)  # monotonic 10s < 15s 阈值
            total = d.daily_total()
            # delta 全部归属"样本时间戳"对应的日期（跳变后的新日期）
            self.assertEqual(sum(v["prompt"] for v in total.values()), 10)
        finally:
            d.close()


# ---------------------------------------------------------------------------
# monitor 重启
# ---------------------------------------------------------------------------


class MonitorRestartTests(TestBase):
    def _new_process(self, start_wall: float) -> Driver:
        """模拟新进程：同一 db 文件，新 collector + 新时钟。"""
        d = Driver(self._tmp.name, poll=5.0, db_path="t.db")
        d.clock = FakeClock(start_wall=start_wall)
        d.collector = MetricsCollector(d.cfg, d.db, clock=d.clock)
        d.collector.maybe_note_restart_gap()
        return d

    def test_restart_no_double_count(self):
        """monitor 重启不重复/漏计：新进程从 state 表 baseline 继续计算。"""
        d1 = self.make_driver()
        try:
            d1.round(metrics_text(prompt=1000, output=500))
            d1.advance(5)
            d1.round(metrics_text(prompt=1100, output=520))
            last_wall = d1.clock.now()
        finally:
            d1.close()

        d2 = self._new_process(last_wall + 40)  # 停机 40 秒（> 15s 阈值）
        try:
            d2.round(metrics_text(prompt=1150, output=530))
            total = d2.daily_total()
            # prompt: 100（第一进程）+ 50（1150-1100，停机期间）= 150
            self.assertEqual(sum(v["prompt"] for v in total.values()), 150)
            # output: 20（520-500）+ 10（530-520）= 30
            self.assertEqual(sum(v["output"] for v in total.values()), 30)
            restart_gaps = [g for g in d2.gaps() if g["reason"] == "monitor_restart"]
            self.assertEqual(len(restart_gaps), 1)
            self.assertFalse(restart_gaps[0]["possible_token_loss"])
        finally:
            d2.close()

    def test_restart_with_server_reset_marks_loss(self):
        """停机期间 server 也重启了：monitor_restart 缺口标记 possible_token_loss。"""
        d1 = self.make_driver()
        try:
            d1.round(metrics_text(prompt=1000, output=500))
            last_wall = d1.clock.now()
        finally:
            d1.close()

        d2 = self._new_process(last_wall + 40)
        try:
            d2.round(metrics_text(prompt=30, output=30))  # server 重启：counter 归 30
            restart_gaps = [g for g in d2.gaps() if g["reason"] == "monitor_restart"]
            self.assertEqual(len(restart_gaps), 1)
            self.assertTrue(restart_gaps[0]["possible_token_loss"])
        finally:
            d2.close()

    def test_quick_restart_below_threshold_no_gap(self):
        """快速重启（< 阈值）不记 monitor_restart 缺口。"""
        d1 = self.make_driver()
        try:
            d1.round(metrics_text(prompt=100, output=100))
            last_wall = d1.clock.now()
        finally:
            d1.close()

        d2 = self._new_process(last_wall + 3)  # 3 秒 < 15 秒
        try:
            d2.round(metrics_text(prompt=101, output=100))
            self.assertEqual([g for g in d2.gaps() if g["reason"] == "monitor_restart"], [])
            total = d2.daily_total()
            self.assertEqual(sum(v["prompt"] for v in total.values()), 1)
        finally:
            d2.close()


# ---------------------------------------------------------------------------
# DB 写失败 / 健康
# ---------------------------------------------------------------------------


class DbWriteFailureTests(TestBase):
    def test_write_failure_keeps_baseline_and_recovers(self):
        """写事务失败 -> baseline 不更新；下一轮从旧 baseline 重算完整 delta（不丢不重）。"""
        d = self.make_driver(poll=5.0)
        try:
            d.round(metrics_text(prompt=100, output=100))
            d.advance(5)

            real_apply = d.db.apply_sample

            def boom(*a, **k):
                raise sqlite3.OperationalError("database is locked")

            d.db.apply_sample = boom  # 这一轮写失败
            d.round(metrics_text(prompt=200, output=100))  # 100 的增量没落库
            self.assertIsNotNone(d.collector.last_db_error)
            state = d.db.get_state()
            self.assertEqual(state["llamacpp:prompt_tokens_total"], 100.0)  # baseline 未动

            d.db.apply_sample = real_apply  # 恢复
            d.advance(5)
            d.round(metrics_text(prompt=300, output=100))
            # 从旧 baseline 100 重算：300 - 100 = 200（包含失败轮的 100）
            total = d.daily_total()
            self.assertEqual(sum(v["prompt"] for v in total.values()), 200)
            self.assertIsNone(d.collector.last_db_error)
            self.assertGreaterEqual(len(d.events("database_recovery")), 1)
        finally:
            d.close()

    def test_integer_storage_for_token_counters(self):
        """Token 相关列以整数存储（daily_usage / live_samples 的 token 列为 INTEGER）。"""
        d = self.make_driver(poll=5.0)
        try:
            d.round(metrics_text(prompt=1234, output=567))
            d.advance(5)
            d.round(metrics_text(prompt=1300, output=600))
            rows = d.db._connect().execute(
                "SELECT prompt_tokens, output_tokens FROM daily_usage"
            ).fetchall()
            self.assertEqual(rows[0][0], 66)   # int
            self.assertEqual(rows[0][1], 33)   # int
            self.assertIsInstance(rows[0][0], int)
            live = d.db._connect().execute(
                "SELECT prompt_delta, output_delta FROM live_samples ORDER BY id"
            ).fetchall()
            self.assertEqual(live[-1][0], 66)
            self.assertIsInstance(live[-1][0], int)
            # state 中 token counter 存 int 值
            state = d.db._connect().execute(
                "SELECT value FROM state WHERE metric_name='llamacpp:prompt_tokens_total'"
            ).fetchone()
            self.assertEqual(state[0], 1300.0)  # REAL 列存精确整数值
        finally:
            d.close()


# ---------------------------------------------------------------------------
# 事件保留
# ---------------------------------------------------------------------------


class EventRetentionTests(TestBase):
    def test_events_retention_365_days(self):
        d = self.make_driver()
        try:
            old_ts = d.clock.now() - 400 * 86400  # 400 天前
            d.db.record_event("server_online", "info", "collector", {}, now=old_ts)
            recent_ts = d.clock.now() - 10 * 86400
            d.db.record_event("server_online", "info", "collector", {}, now=recent_ts)
            d.db.record_event("counter_reset", "warning", "collector", {}, now=d.clock.now())
            rows = d.db.get_events(limit=100000)
            # 400 天前的被清理；10 天前与当前保留（另有建库时的 migration 事件）
            self.assertEqual(len(d.events("server_online")), 1)
            self.assertGreaterEqual(len(rows), 3)
            self.assertNotIn(old_ts, [r["timestamp"] for r in rows])
        finally:
            d.close()

    def test_data_gaps_not_pruned_by_time(self):
        """data_gaps 永久保留（不受 48h/365d 清理影响）。"""
        d = self.make_driver()
        try:
            old_start = d.clock.now() - 400 * 86400
            d.db.record_gap(old_start, old_start + 60, "llama", "server_offline",
                            token_recoverable=True, possible_token_loss=False)
            self.assertEqual(len(d.gaps()), 1)
            self.assertEqual(d.gaps()[0]["duration_seconds"], 60.0)
        finally:
            d.close()


if __name__ == "__main__":
    unittest.main()
