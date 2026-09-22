"""
Phase 16B 测试：信息架构重构后的后端数据契约。

1. BUG-A（本月=0）回归（spec §22/§23）：
   /api/summary 的 month 由后端计算，与 today 同一批行、同一个 local_date 前缀。
   固定日期 2026-09-22：
       2026-09-22 = 7730（logical）
       2026-09-01 = 1000
       2026-08-31 = 9999（上月，绝不能计入）
   September total 必须 = 8730。
   同时覆盖边界：月初第 1 天（2026-10-01）上月行必须为 0、今日计入本月。
2. /api/daily?all=true 返回全部历史；days 参数行为不变。
3. /api/events：monitor_events 倒序、字段形状、limit 上限、
   reset-statistics 不清除事件（与 0.16.12 data_gaps 行为对照）。
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from clock import FakeClock
from collector import MetricsCollector
from configutil import loopback_app, make_config
from db import Database
from server import build_app


def _unix(*args) -> float:
    return datetime(*args).timestamp()


class Phase16BSummaryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._dbs: list[Database] = []
        self._patch = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.tmp / "lad")})
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        for db in self._dbs:
            db.close()
        self._tmp.cleanup()

    def _start(self, wall: float):
        cfg = make_config()
        db = Database(self.tmp / "p16b.db")
        self._dbs.append(db)
        clock = FakeClock(start_wall=wall)
        collector = MetricsCollector(cfg, db, clock)

        async def _fetch_offline():
            return None

        collector._fetch_parsed = _fetch_offline
        app = build_app(db, collector)
        client = TestClient(loopback_app(app))
        client.__enter__()
        return client, collector, db

    def _seed_daily(self, date: str, prompt: int, cached: int, output: int) -> None:
        # 独立 sqlite 连接写（WAL 并发安全；db.record_* 走 portal 线程连接，
        # 从主线程直接调 db 方法会跨线程——统一在这里用独立连接灌历史数据）
        conn = sqlite3.connect(self.tmp / "p16b.db")
        try:
            conn.execute(
                "INSERT INTO daily_usage(date, prompt_tokens, cached_tokens, output_tokens, "
                "draft_tokens, accepted_tokens, prompt_seconds, predicted_seconds, draft_sequences) "
                "VALUES(?,?,?,?,0,0,0.0,0.0,0) "
                "ON CONFLICT(date) DO UPDATE SET "
                "prompt_tokens=prompt_tokens+excluded.prompt_tokens, "
                "cached_tokens=cached_tokens+excluded.cached_tokens, "
                "output_tokens=output_tokens+excluded.output_tokens",
                (date, prompt, cached, output),
            )
            conn.commit()
        finally:
            conn.close()

    def _portal_seed(self, client, seed) -> None:
        """经 TestClient portal 在应用线程内执行 seed（用于 db.record_event/record_gap——
        它们用 Database 的长连接，必须与 lifespan 同线程）。"""
        client.portal.call(seed)

    # ---------- 1) BUG-A：month 边界 ----------

    def test_summary_month_excludes_previous_month(self):
        # spec §23：当前日期 2026-09-22
        client, _collector, _db = self._start(wall=_unix(2026, 9, 22, 12, 0, 0))
        try:
            self._seed_daily("2026-09-22", 7730, 0, 0)
            self._seed_daily("2026-09-01", 1000, 0, 0)
            self._seed_daily("2026-08-31", 9999, 0, 0)  # 上月——绝不能计入本月
            data = client.get("/api/summary").json()
            self.assertEqual(data["today"]["logical_tokens"], 7730)
            self.assertEqual(data["month"]["logical_tokens"], 8730)  # 7730 + 1000
            self.assertEqual(data["month"]["prompt_tokens"], 8730)
            self.assertEqual(data["total"]["logical_tokens"], 7730 + 1000 + 9999)
            self.assertEqual(data["month_key"], "2026-09")
        finally:
            client.__exit__(None, None, None)

    def test_summary_month_first_day_of_new_month(self):
        # 边界：月初第 1 天——上月行存在时本月只含今日
        client, _collector, _db = self._start(wall=_unix(2026, 10, 1, 8, 30, 0))
        try:
            self._seed_daily("2026-10-01", 500, 100, 400)   # logical 1000
            self._seed_daily("2026-09-30", 4242, 0, 0)       # 上月
            data = client.get("/api/summary").json()
            self.assertEqual(data["month_key"], "2026-10")
            self.assertEqual(data["month"]["logical_tokens"], 1000)
            self.assertEqual(data["month"]["compute_tokens"], 900)  # prompt+output
            self.assertEqual(data["today"]["logical_tokens"], 1000)
            self.assertEqual(data["total"]["logical_tokens"], 5242)
        finally:
            client.__exit__(None, None, None)

    def test_summary_month_matches_front_prefix_rule(self):
        # 回归语义：month == "date 前缀为当月" 的行之和（前端旧实现的正确版本）
        client, _collector, _db = self._start(wall=_unix(2026, 9, 22, 12, 0, 0))
        try:
            for d, v in [("2026-09-05", 111), ("2026-09-20", 222), ("2026-09-22", 333)]:
                self._seed_daily(d, v, 0, 0)
            data = client.get("/api/summary").json()
            self.assertEqual(data["month"]["logical_tokens"], 111 + 222 + 333)
        finally:
            client.__exit__(None, None, None)

    # ---------- 2) /api/daily all=true ----------

    def test_daily_all_returns_full_history(self):
        client, _collector, _db = self._start(wall=_unix(2026, 9, 22, 12, 0, 0))
        try:
            self._seed_daily("2026-01-05", 10, 0, 0)   # 超出 30 天窗口
            self._seed_daily("2026-09-22", 20, 0, 0)
            limited = client.get("/api/daily?days=30").json()["days"]
            self.assertEqual([r["date"] for r in limited], ["2026-09-22"])
            full = client.get("/api/daily?all=true").json()["days"]
            self.assertEqual([r["date"] for r in full], ["2026-01-05", "2026-09-22"])
        finally:
            client.__exit__(None, None, None)

    # ---------- 3) /api/events ----------

    def test_events_endpoint_shape_and_order(self):
        # 注意：lifespan 启动时会写一条 monitor_start（default_clock 真实时间），
        # 断言按"最近 N 条"相对判定，不假设精确总数。
        client, _collector, db = self._start(wall=_unix(2026, 9, 22, 12, 0, 0))
        try:
            def _seed_events():
                db.record_event("server_offline", "warning", "collector", {}, now=_unix(2026, 9, 22, 9, 0, 0))
                db.record_event("ev_marker_a", "info", "test", {}, now=_unix(2026, 9, 22, 9, 1, 0))
                db.record_event("ev_marker_b", "info", "test", {}, now=_unix(2026, 9, 22, 9, 5, 0))

            self._portal_seed(client, _seed_events)
            data = client.get("/api/events?limit=30").json()
            events = data["events"]
            self.assertGreaterEqual(len(events), 3)
            # 倒序：最新的 marker 在最前
            self.assertEqual(events[0]["event_type"], "ev_marker_b")
            self.assertEqual(events[1]["event_type"], "ev_marker_a")
            self.assertEqual(events[2]["event_type"], "server_offline")
            self.assertEqual(events[2]["severity"], "warning")
            self.assertIsInstance(events[0]["details"], dict)
            self.assertEqual(events[0]["source"], "test")
            # API 契约 = 插入序倒序（id DESC）：事件日志按记录顺序展示，
            # 不做时间戳重排（monitor_start 等系统事件用真实时钟记录，
            # 与 FakeClock 灌入的历史时间戳可能交错，属测试伪影）
            # 但 marker 之间的相对顺序必须保持时间倒序
            self.assertGreater(events[0]["timestamp"], events[1]["timestamp"])
            self.assertGreater(events[1]["timestamp"], events[2]["timestamp"])
        finally:
            client.__exit__(None, None, None)

    def test_events_limit(self):
        client, _collector, db = self._start(wall=_unix(2026, 9, 22, 12, 0, 0))
        try:
            base = _unix(2026, 9, 22, 9, 0, 0)

            def _seed_many():
                for i in range(10):
                    db.record_event("test_event", "info", "test", {}, now=base + i * 60)

            self._portal_seed(client, _seed_many)
            data = client.get("/api/events?limit=4").json()
            self.assertEqual(len(data["events"]), 4)
            # limit=4 取最新 4 条（base+30..base+9 全是 test_event）
            for e in data["events"]:
                self.assertEqual(e["event_type"], "test_event")
            self.assertEqual(data["events"][0]["timestamp"], base + 9 * 60)
        finally:
            client.__exit__(None, None, None)

    def test_reset_keeps_events_clears_gaps(self):
        # 与 0.16.12 语义一致：reset 清 data_gaps、保留 monitor_events
        client, _collector, db = self._start(wall=_unix(2026, 9, 22, 12, 0, 0))
        try:
            self._seed_daily("2026-09-22", 100, 0, 0)

            def _seed_evt_gap():
                db.record_event("counter_reset", "warning", "collector", {}, now=_unix(2026, 9, 22, 10, 0, 0))
                db.record_gap(_unix(2026, 9, 22, 8, 0, 0), _unix(2026, 9, 22, 8, 1, 0),
                              "llama", "server_offline")

            self._portal_seed(client, _seed_evt_gap)
            r = client.post("/api/data/reset-statistics", json={"confirm": "RESET"}).json()
            self.assertTrue(r["success"])
            self.assertGreaterEqual(r.get("gaps_deleted", 0), 1)
            events = client.get("/api/events").json()["events"]
            self.assertEqual(events[0]["event_type"], "counter_reset")  # 事件保留
            self.assertEqual(client.get("/api/daily").json()["days"], [])  # daily 已清
        finally:
            client.__exit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
