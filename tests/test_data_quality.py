"""
Phase 11 测试：数据质量 —— data_gaps 模型、Monitoring Coverage、
midnight 归集规则、时区/时间跳变、/api/data/quality 与 /api/health。

全部临时目录 + FakeClock；不触碰真实数据目录。
运行：
    python -m unittest discover -s tests
"""

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from clock import FakeClock
from collector import MetricsCollector
from db import Database, local_date
from fastapi.testclient import TestClient
from metrics_parser import parse_metrics
from server import build_app
from configutil import make_config, make_loaded, loopback_app

from test_reliability import metrics_text

# 固定午夜边界：2026-09-15 23:59:55（本机时间）
MIDNIGHT_BEFORE = datetime(2026, 9, 15, 23, 59, 55).timestamp()
DAY_BEFORE = "2026-09-15"
DAY_AFTER = "2026-09-16"


class Driver:
    def __init__(self, tmp: str, poll: float = 5.0, start_wall: float = MIDNIGHT_BEFORE - 120.0):
        self.cfg = make_config(poll_interval=poll)
        self.clock = FakeClock(start_wall=start_wall)
        self.db = Database(Path(tmp) / "dq.db", wal=False, retention_seconds=48 * 3600)
        self.collector = MetricsCollector(self.cfg, self.db, clock=self.clock)

    def round(self, text: str | None, portal=None) -> dict:
        """portal（TestClient.portal）提供时在应用的 event-loop 线程内执行
        （SQLite 连接线程约束）；否则独立 asyncio.run（纯 collector 测试）。"""
        def make_fetch(txt):
            async def fetch():
                return parse_metrics(txt) if txt is not None else None
            return fetch
        self.collector._fetch_parsed = make_fetch(text)
        if portal is not None:
            return portal.call(self.collector.collect_once)
        return asyncio.run(self.collector.collect_once())

    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds)

    def go_offline(self) -> None:
        """把 _fetch_parsed 切为离线：API 测试里 lifespan 的周期采集（若触发）
        只会产生离线轮（不写库），避免污染测试数据。"""
        async def fetch():
            return None
        self.collector._fetch_parsed = fetch

    def close(self) -> None:
        self.collector.shutdown()
        self.db.close()


class TestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def make_driver(self, **kw) -> Driver:
        return Driver(self._tmp.name, **kw)


class MidnightBucketTests(TestBase):
    def test_delta_attributed_to_later_sample_date(self):
        """
        午夜规则：跨午夜的一轮，delta 归属**样本时间戳（较晚样本 B）**的日期——
        "Token usage is bucketed by the timestamp of the successful collection sample"。
        """
        d = self.make_driver(start_wall=MIDNIGHT_BEFORE)
        try:
            # 23:59:55 的样本（前一日，只建 baseline）
            d.round(metrics_text(prompt=100, output=100))
            # +10s -> 次日 00:00:05：delta 全部归属次日
            d.advance(10)
            d.round(metrics_text(prompt=160, output=120))
            daily = {r["date"]: r for r in d.db.get_daily_usage()}
            self.assertIn(DAY_BEFORE, daily)
            self.assertIn(DAY_AFTER, daily)
            self.assertEqual(daily[DAY_BEFORE]["prompt_tokens"], 0)   # baseline 当日无增量
            self.assertEqual(daily[DAY_AFTER]["prompt_tokens"], 60)   # 60 全部归次日
            self.assertEqual(daily[DAY_AFTER]["output_tokens"], 20)
        finally:
            d.close()

    def test_no_negative_interval_on_wall_backward(self):
        """wall 回拨：不产生负间隔缺口（monotonic 检测），不崩溃。"""
        d = self.make_driver()
        try:
            d.round(metrics_text(prompt=100, output=100))
            d.advance(10)                       # mono +10
            d.clock.set_wall(d.clock.now() - 3600)  # wall 回拨 1 小时
            d.round(metrics_text(prompt=110, output=100))
            # monotonic 10s < 15s 阈值 -> 无缺口
            self.assertEqual(len(d.db.get_gaps(limit=1000)), 0)
            total = sum(r["prompt_tokens"] for r in d.db.get_daily_usage())
            self.assertEqual(total, 10)
        finally:
            d.close()

    def test_timezone_change_no_crash_no_rebucket(self):
        """时区修改（wall 平移 8 小时、monotonic 正常）：不崩溃、不重排历史、
        增量归属新时区下的日期。"""
        d = self.make_driver()
        try:
            before_date = local_date(d.clock.now())
            d.round(metrics_text(prompt=100, output=100))
            d.advance(10)
            d.clock.set_wall(d.clock.now() + 8 * 3600)  # 模拟时区前移 8h
            after_date = local_date(d.clock.now())
            self.assertNotEqual(before_date, after_date)  # 确实跨了日期
            d.round(metrics_text(prompt=130, output=100))
            daily = {r["date"]: r for r in d.db.get_daily_usage()}
            # 历史不重排：前一日行保持 baseline 0 增量；30 归新日期
            self.assertEqual(daily[after_date]["prompt_tokens"], 30)
        finally:
            d.close()


class GapModelTests(TestBase):
    def test_gap_fields_and_retention(self):
        d = self.make_driver()
        try:
            d.db.record_gap(
                start_ts=d.clock.now(), end_ts=d.clock.now() + 60,
                source="llama", reason="server_offline",
                token_recoverable=True, possible_token_loss=False,
            )
            gaps = d.db.get_gaps(limit=10)
            self.assertEqual(len(gaps), 1)
            g = gaps[0]
            self.assertEqual(g["source"], "llama")
            self.assertEqual(g["reason"], "server_offline")
            self.assertEqual(g["duration_seconds"], 60.0)
            self.assertEqual(g["token_recoverable"], 1)
            self.assertEqual(g["possible_token_loss"], 0)
            self.assertEqual(g["resolved"], 1)
            self.assertLess(g["start_timestamp"], g["end_timestamp"])
        finally:
            d.close()

    def test_gap_query_by_date(self):
        d = self.make_driver()
        try:
            now = d.clock.now()
            d.db.record_gap(now, now + 60, "llama", "server_offline")
            # 与当天有交集 -> 能查到
            found = d.db.get_gaps(date=local_date(now))
            self.assertEqual(len(found), 1)
            # 与相隔 30 天的日期无交集 -> 查不到
            other_day = local_date(now - 30 * 86400)
            self.assertEqual(len(d.db.get_gaps(date=other_day)), 0)
        finally:
            d.close()

    def test_gap_stats(self):
        d = self.make_driver()
        try:
            now = d.clock.now()
            d.db.record_gap(now, now + 60, "llama", "server_offline",
                            token_recoverable=True, possible_token_loss=False)
            d.db.record_gap(now + 100, now + 160, "llama", "server_offline",
                            token_recoverable=False, possible_token_loss=True)
            stats = d.db.get_gap_stats(local_date(now))
            self.assertEqual(stats["gap_count"], 2)
            self.assertTrue(stats["possible_token_loss"])
            self.assertAlmostEqual(stats["total_gap_seconds"], 120.0)
            self.assertFalse(stats["token_recoverable"])
        finally:
            d.close()


class DataQualityApiTests(TestBase):
    def _start(self, driver: Driver) -> TestClient:
        loaded = make_loaded(driver.cfg, Path(self._tmp.name))
        app = build_app(driver.db, driver.collector, loaded=loaded, gpu=None)
        return TestClient(loopback_app(app))

    def test_data_quality_shape_and_values(self):
        d = self.make_driver()
        try:
            with self._start(d) as client:
                # 制造一个缺口 + 有效样本
                d.round(metrics_text(prompt=100, output=100), portal=client.portal)
                d.advance(30)  # > 15s：制造一次系统暂停式缺口（无离线轮）
                d.round(metrics_text(prompt=120, output=100), portal=client.portal)
                d.go_offline()
                r = client.get("/api/data/quality")
                self.assertEqual(r.status_code, 200)
                data = r.json()
                today = data["today"]
                self.assertEqual(today["date"], local_date(d.clock.now()))
                self.assertIn("monitoring_coverage_percent", today)
                self.assertGreaterEqual(today["monitoring_coverage_percent"], 0.0)
                self.assertLess(today["monitoring_coverage_percent"], 100.0)  # 有缺口
                self.assertGreaterEqual(today["gap_count"], 1)
                self.assertFalse(today["possible_token_loss"])
                self.assertIsNotNone(today["last_valid_sample_seconds_ago"])
                self.assertGreaterEqual(data["total"]["gap_count"], 1)
                self.assertIsNotNone(data["last_gap"])
                self.assertEqual(data["last_gap"]["reason"], "system_pause_or_sleep")
        finally:
            d.close()

    def test_health_endpoint(self):
        d = self.make_driver()
        try:
            with self._start(d) as client:
                d.round(metrics_text(prompt=100, output=100), portal=client.portal)
                r = client.get("/api/health")
                self.assertEqual(r.status_code, 200)
                data = r.json()
                self.assertEqual(data["application"], "healthy")
                self.assertEqual(data["database"], "healthy")
                self.assertIsNotNone(data["last_valid_sample_seconds_ago"])
                self.assertIn("possible_token_loss", data)
                # 制造未结束缺口（离线）
                d.round(None, portal=client.portal)
                data2 = client.get("/api/health").json()
                self.assertIsNotNone(data2["known_data_gaps"])
                self.assertEqual(data2["known_data_gaps"]["reason"], "server_offline")
        finally:
            d.close()

    def test_quality_endpoint_does_not_scan_all_live_samples(self):
        """AUDIT-DB-003（同类扩展）：/api/data/quality 必须走按日范围查询
        （get_day_sample_bounds），不允许每次轮询拉全量 live_samples。"""
        d = self.make_driver()
        try:
            with self._start(d) as client:
                d.round(metrics_text(prompt=100, output=100), portal=client.portal)
                d.advance(5)
                d.round(metrics_text(prompt=130, output=100), portal=client.portal)

                calls = {"full": 0, "day_bounds": 0}
                real_full = d.db.get_live_samples
                real_bounds = d.db.get_day_sample_bounds

                def counting_full(hours=48.0):
                    if hours is None:
                        calls["full"] += 1
                    return real_full(hours)

                def counting_bounds(date):
                    calls["day_bounds"] += 1
                    return real_bounds(date)

                d.db.get_live_samples = counting_full
                d.db.get_day_sample_bounds = counting_bounds
                try:
                    r = client.get("/api/data/quality")
                    self.assertEqual(r.status_code, 200)
                    self.assertIsNotNone(r.json()["today"]["monitoring_coverage_percent"])
                finally:
                    d.db.get_live_samples = real_full
                    d.db.get_day_sample_bounds = real_bounds
                self.assertEqual(calls["full"], 0, "quality 不得拉全量 live_samples")
                self.assertGreaterEqual(calls["day_bounds"], 1, "必须走按日范围查询")
        finally:
            d.close()

    def test_daily_api_includes_quality_fields(self):
        d = self.make_driver()
        try:
            with self._start(d) as client:
                d.round(metrics_text(prompt=100, output=100), portal=client.portal)
                d.advance(5)
                d.round(metrics_text(prompt=130, output=100), portal=client.portal)
                r = client.get("/api/daily")
                self.assertEqual(r.status_code, 200)
                days = r.json()["days"]
                self.assertGreaterEqual(len(days), 1)
                for row in days:
                    self.assertIn("monitoring_coverage_percent", row)
                    self.assertIn("gap_count", row)
                    self.assertIn("possible_token_loss", row)
        finally:
            d.close()

    def test_check_database_endpoint(self):
        d = self.make_driver()
        try:
            with self._start(d) as client:
                r = client.post("/api/data/check-database")
                self.assertEqual(r.status_code, 200)
                data = r.json()
                self.assertTrue(data["healthy"])
                self.assertEqual(data["status"], "healthy")
                # 模拟磁盘损坏：把 monitor_events 数据页的 B-tree 页面类型标志清零
                def _corrupt_file():
                    root = d.db._connect().execute(
                        "SELECT rootpage FROM sqlite_master WHERE name='monitor_events'"
                    ).fetchone()[0]
                    page_size = d.db._connect().execute("PRAGMA page_size").fetchone()[0]
                    d.db.close()  # 关闭后文件才是权威状态
                    with open(d.db.path, "r+b") as f:
                        f.seek((root - 1) * page_size)
                        f.write(b"\x00")

                client.portal.call(_corrupt_file)
                r2 = client.post("/api/data/check-database")  # db 对象自动重开连接
                data2 = r2.json()
                self.assertFalse(data2["healthy"])
                self.assertEqual(data2["status"], "corrupt")
                self.assertIsNotNone(data2["detail"])
        finally:
            d.close()

    def test_corrupt_db_blocks_mutation_apis(self):
        d = self.make_driver()
        try:
            with self._start(d) as client:
                d.db.set_health("corrupt", "simulated")
                r = client.post("/api/data/clear-live", json={"confirm": True})
                self.assertEqual(r.status_code, 409)
                self.assertEqual(r.json()["error"]["code"], "DB_UNHEALTHY")
                r2 = client.post("/api/data/reset-statistics", json={"confirm": "RESET"})
                self.assertEqual(r2.status_code, 409)
        finally:
            d.close()


if __name__ == "__main__":
    unittest.main()
