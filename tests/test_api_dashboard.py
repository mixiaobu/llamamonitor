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


if __name__ == "__main__":
    unittest.main()
