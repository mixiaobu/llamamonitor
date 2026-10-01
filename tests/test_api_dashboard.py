"""
Phase 4 测试：/api/daily 的 mtp_accept_rate 字段、/api/mtp/daily、
Dashboard 首页与本地 ECharts 静态文件服务。

在项目根目录运行：
    python -m unittest discover -s tests -v
"""

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from collector import MetricsCollector
from configutil import make_config
from db import Database
from metrics_parser import parse_metrics
from server import build_app

from test_persistence import TEXT_A, sample

TEXT_A_POS = TEXT_A + 'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="0"} 3\n'
TEXT_MTP = (
    sample(prompt=150, prompt_sec=1.5, draft=10, accepted=6)
    + 'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="0"} 8\n'
)


class DashboardApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _start(self, text):
        db = Database(self.tmp / "dash_test.db")
        collector = MetricsCollector(make_config(), db)
        parsed = parse_metrics(text)

        async def _fetch_parsed():
            return parsed

        collector._fetch_parsed = _fetch_parsed
        app = build_app(db, collector)
        client = TestClient(app)
        client.__enter__()
        return client

    def _round(self, client, collector, text):
        parsed = parse_metrics(text)

        async def _fetch_parsed():
            return parsed

        collector._fetch_parsed = _fetch_parsed
        client.portal.call(collector.collect_once)

    def test_daily_month_filter_server_side(self):
        """
        AUDIT-1.1.1 BUG-1111-005 回归：/api/daily?month=true 只在服务端按本机
        自然月（local_date 前缀 YYYY-MM）过滤，与 /api/summary 的 month_key 同源。
        此前前端取 31 天回浏览器按本地前缀过滤——日期来源分离。
        """
        from db import local_date
        db = Database(self.tmp / "t_month.db")
        collector = MetricsCollector(make_config(), db)
        app = build_app(db, collector)
        client = TestClient(app)
        client.__enter__()
        try:
            now_date = local_date(collector.clock.now())
            # 直接造两行：本月一行 + 上个月一行（绕过 collector，纯验证过滤）
            prev_month_date = (now_date[:7] + "-01")
            # 上个月：把本月 01 往前推一个自然月
            import calendar
            y, m = int(now_date[:4]), int(now_date[5:7])
            pm = m - 1 if m > 1 else 12
            py = y if m > 1 else y - 1
            prev_month_date = "%04d-%02d-15" % (py, pm)
            this_month_other = now_date[:7] + "-05"
            def _insert_rows():
                conn = db._connect()
                with conn:
                    for d in (prev_month_date, this_month_other, now_date):
                        conn.execute(
                            "INSERT INTO daily_usage(date, prompt_tokens) VALUES(?, 10) "
                            "ON CONFLICT(date) DO UPDATE SET prompt_tokens = 10", (d,))
            client.portal.call(_insert_rows)
            # month=true：只返回本月（含 now_date 与 this_month_other，不含上月）
            data = client.get("/api/daily", params={"month": True}).json()
            dates = {r["date"] for r in data["days"]}
            self.assertIn(now_date, dates)
            self.assertIn(this_month_other, dates)
            self.assertNotIn(prev_month_date, dates)
            for d in dates:
                self.assertEqual(d[:7], now_date[:7])  # 全是本月
            # all=true：三行都返回（含上月）
            data_all = client.get("/api/daily", params={"all": True}).json()
            self.assertIn(prev_month_date, {r["date"] for r in data_all["days"]})
        finally:
            client.__exit__(None, None, None)

    def test_daily_includes_mtp_accept_rate(self):
        db = Database(self.tmp / "t.db")
        collector = MetricsCollector(make_config(), db)
        app = build_app(db, collector)
        client = TestClient(app)
        client.__enter__()
        try:
            # baseline 轮：draft=0 -> rate null
            self._round(client, collector, TEXT_A_POS)
            rows = client.get("/api/daily", params={"days": 7}).json()["days"]
            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0]["mtp_accept_rate"])

            # 第二轮：draft delta 5、accepted delta 3 -> 60%
            self._round(client, collector, TEXT_MTP)
            rows = client.get("/api/daily", params={"days": 7}).json()["days"]
            self.assertAlmostEqual(rows[0]["mtp_accept_rate"], 60.0)
            # 原有派生字段仍在
            self.assertEqual(rows[0]["compute_tokens"], 50)
            self.assertEqual(rows[0]["logical_tokens"], 50)
        finally:
            client.__exit__(None, None, None)

    def test_index_page_served(self):
        client = self._start(TEXT_A)
        try:
            r = client.get("/")
            self.assertEqual(r.status_code, 200)
            self.assertIn("text/html", r.headers.get("content-type", ""))
            body = r.text
            # 关键结构都在（Phase 15.1：中文文案 + 模块化 JS；status/range 等由 JS 渲染，
            # 故只断言静态骨架里稳定存在的元素）
            for marker in (
                "LlamaMonitor",
                # Phase 16F 术语审计：旧标记（逻辑 Token/缓存率…）已按
                # docs/UI_TERMINOLOGY.md 统一为 新术语
                "Token 总量",
                "缓存复用率",
                "Token 吞吐率",
                "Draft Token 接受率",
                'id="usageRange"',
                "/static/echarts.min.js",
                "/static/js/app.js",
                "data-theme",
            ):
                self.assertIn(marker, body)
        finally:
            client.__exit__(None, None, None)

    def test_echarts_static_served_locally(self):
        client = self._start(TEXT_A)
        try:
            r = client.get("/static/echarts.min.js")
            self.assertEqual(r.status_code, 200)
            self.assertIn("javascript", r.headers.get("content-type", ""))
            # 校验本地文件是真实的 ECharts（>1MB 的压缩 JS，Apache 许可头）
            body = r.text
            self.assertGreater(len(body), 1_000_000)
            self.assertIn("Apache Software Foundation", body[:500])
        finally:
            client.__exit__(None, None, None)

    def test_mtp_daily_endpoint(self):
        # 独立可控流程：两轮采集后检查 /api/mtp/daily
        db = Database(self.tmp / "t2.db")
        collector = MetricsCollector(make_config(), db)
        app = build_app(db, collector)

        parsed = parse_metrics(TEXT_A_POS)

        async def _fetch_parsed():
            return parsed

        collector._fetch_parsed = _fetch_parsed
        client = TestClient(app)
        client.__enter__()
        try:
            # 第二轮：draft 5 / accepted 3
            parsed2 = parse_metrics(TEXT_MTP)

            async def _fetch_parsed2():
                return parsed2

            collector._fetch_parsed = _fetch_parsed2
            client.portal.call(collector.collect_once)

            data = client.get("/api/mtp/daily", params={"days": 3}).json()
            days = data["days"]
            # 完整的 3 个自然日（升序），前两天无数据 -> 0 / null
            self.assertEqual(len(days), 3)
            self.assertEqual(days[0]["draft"], 0)
            self.assertIsNone(days[0]["accept_rate"])
            last = days[-1]
            self.assertEqual(last["draft"], 5)
            self.assertEqual(last["accepted"], 3)
            self.assertAlmostEqual(last["accept_rate"], 60.0)
            # 参数校验
            self.assertEqual(client.get("/api/mtp/daily", params={"days": 0}).status_code, 422)
            self.assertEqual(client.get("/api/mtp/daily", params={"days": 400}).status_code, 422)
        finally:
            client.__exit__(None, None, None)


class UsageSummaryApiTests(unittest.TestCase):
    """1.1.4 Round 4：/api/usage-summary、/api/daily 自定义范围 + range 元数据、
    /api/today-hourly、/api/data/export/daily.csv 自定义范围 + reuse 列。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _app(self):
        db = Database(self.tmp / "us.db")
        collector = MetricsCollector(make_config(), db)
        app = build_app(db, collector)
        client = TestClient(app)
        client.__enter__()
        return client, db, collector

    def _insert_daily(self, db, client, date, prompt=0, cached=0, output=0):
        def _ins():
            conn = db._connect()
            with conn:
                conn.execute(
                    "INSERT INTO daily_usage(date, prompt_tokens, cached_tokens, output_tokens) "
                    "VALUES(?,?,?,?) "
                    "ON CONFLICT(date) DO UPDATE SET "
                    "prompt_tokens=excluded.prompt_tokens, cached_tokens=excluded.cached_tokens, "
                    "output_tokens=excluded.output_tokens",
                    (date, prompt, cached, output))
        client.portal.call(_ins)

    def test_daily_custom_range_and_meta(self):
        from db import local_date
        client, db, collector = self._app()
        try:
            today = local_date(collector.clock.now())
            d0 = "2026-01-01"
            d1 = "2026-01-02"
            self._insert_daily(db, client, d0, prompt=10, cached=5, output=3)
            self._insert_daily(db, client, d1, prompt=20, cached=4, output=6)
            self._insert_daily(db, client, today, prompt=1, cached=1, output=1)
            # 自定义范围：只返回 [d0, d1]
            data = client.get("/api/daily", params={"start_date": d0, "end_date": d1}).json()
            self.assertEqual([r["date"] for r in data["days"]], [d0, d1])
            self.assertEqual(data["range"]["mode"], "custom")
            self.assertEqual(data["range"]["start_date"], d0)
            self.assertEqual(data["range"]["end_date"], d1)
            self.assertEqual(data["range"]["today"], today)
            self.assertIn("server_now_hhmm", data["range"])
            # 派生字段
            self.assertEqual(data["days"][0]["logical_tokens"], 18)
            self.assertEqual(data["days"][0]["compute_tokens"], 13)
            # 校验 400：只给一个 / 起>止 / 止>今天
            self.assertEqual(client.get("/api/daily", params={"start_date": d0}).status_code, 400)
            self.assertEqual(client.get("/api/daily", params={"start_date": d1, "end_date": d0}).status_code, 400)
            future = "2999-01-01"
            self.assertEqual(client.get("/api/daily", params={"start_date": d0, "end_date": future}).status_code, 400)
            self.assertEqual(client.get("/api/daily", params={"start_date": "bad", "end_date": d1}).status_code, 400)
        finally:
            client.__exit__(None, None, None)

    def test_usage_summary_aggregates(self):
        from db import local_date
        client, db, collector = self._app()
        try:
            today = local_date(collector.clock.now())
            self._insert_daily(db, client, "2026-01-01", prompt=10, cached=5, output=3)
            self._insert_daily(db, client, "2026-01-02", prompt=20, cached=4, output=6)
            data = client.get("/api/usage-summary", params={"start_date": "2026-01-01", "end_date": "2026-01-02"}).json()
            t = data["totals"]
            self.assertEqual(t["prompt_tokens"], 30)
            self.assertEqual(t["cached_tokens"], 9)
            self.assertEqual(t["output_tokens"], 9)
            self.assertEqual(t["logical_tokens"], 48)
            self.assertEqual(t["compute_tokens"], 39)
            # 缓存复用率 = 9 / (30+9) * 100 = 23.08
            self.assertAlmostEqual(data["cache_reuse_rate_percent"], 23.08, places=1)
            # 日均 = 48 / 2 = 24
            self.assertEqual(data["daily_avg_logical"], 24)
            # 峰值日 = 2026-01-02（logical 30 > 18）
            self.assertEqual(data["peak_day"]["date"], "2026-01-02")
            self.assertEqual(data["peak_day"]["logical_tokens"], 30)
            self.assertEqual(data["valid_days"], 2)
            # 无 live 样本 -> 覆盖按 24h 估算 -> 100%（无缺口）
            self.assertAlmostEqual(data["coverage_percent"], 100.0, places=1)
            self.assertEqual(data["gap_count"], 0)
        finally:
            client.__exit__(None, None, None)

    def test_usage_summary_empty_range(self):
        client, db, collector = self._app()
        try:
            data = client.get("/api/usage-summary", params={"start_date": "2020-01-01", "end_date": "2020-01-05"}).json()
            self.assertEqual(data["totals"]["logical_tokens"], 0)
            self.assertIsNone(data["daily_avg_logical"])
            self.assertIsNone(data["peak_day"])
            self.assertIsNone(data["cache_reuse_rate_percent"])
            self.assertEqual(data["valid_days"], 0)
            self.assertEqual(data["calendar_days"], 5)
        finally:
            client.__exit__(None, None, None)

    def test_today_hourly_buckets(self):
        client, db, collector = self._app()
        try:
            # 两轮采集：第一轮建 baseline（delta=0），第二轮产生增量 (12,4,6)
            baseline = parse_metrics(sample(prompt=100, cached=50, output=10))

            async def _f1():
                return baseline
            collector._fetch_parsed = _f1
            client.portal.call(collector.collect_once)

            grown = parse_metrics(sample(prompt=112, cached=54, output=16))

            async def _f2():
                return grown
            collector._fetch_parsed = _f2
            client.portal.call(collector.collect_once)
            data = client.get("/api/today-hourly").json()
            self.assertIn("date", data)
            self.assertIn("hours", data)
            total_logical = sum(h["logical_tokens"] for h in data["hours"])
            self.assertEqual(total_logical, 22)  # 12+4+6
            # 派生：compute = prompt+output
            for h in data["hours"]:
                self.assertEqual(h["compute_tokens"], h["prompt_tokens"] + h["output_tokens"])
        finally:
            client.__exit__(None, None, None)

    def test_csv_range_and_reuse_column(self):
        from db import local_date
        client, db, collector = self._app()
        try:
            today = local_date(collector.clock.now())
            self._insert_daily(db, client, "2026-01-01", prompt=10, cached=5, output=3)
            self._insert_daily(db, client, "2026-01-02", prompt=20, cached=4, output=6)
            self._insert_daily(db, client, today, prompt=1, cached=0, output=0)
            # 自定义范围导出：只含 [01-01, 01-02]
            r = client.get("/api/data/export/daily.csv",
                           params={"start_date": "2026-01-01", "end_date": "2026-01-02"})
            self.assertEqual(r.status_code, 200)
            body = r.content.decode("utf-8-sig")
            lines = [l for l in body.splitlines() if l.strip()]
            header = lines[0]
            # reuse 列存在
            self.assertIn("reuse_rate_percent", header)
            # 数据行 = 2（不含 today 那行）
            data_lines = lines[1:]
            dates = [l.split(",")[0] for l in data_lines]
            self.assertEqual(dates, ["2026-01-01", "2026-01-02"])
            # 校验 400：只给一个
            self.assertEqual(client.get("/api/data/export/daily.csv",
                                        params={"start_date": "2026-01-01"}).status_code, 400)
        finally:
            client.__exit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
