"""
Phase 3 API 集成测试：lifespan 自动启动/优雅停止 Collector、/api/* 各端点。

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

from test_persistence import TEXT_A, sample  # 与 unittest discover 的顶层导入方式保持一致

# 含 position 数据的 baseline 样本（pos0=3）
TEXT_A_POS = TEXT_A + 'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="0"} 3\n'
# 第二轮：prompt / prompt_sec / draft / accepted 增长；pos0=8，pos1=1（首次出现，应只建 baseline）
TEXT_MTP = (
    sample(prompt=150, prompt_sec=1.5, draft=10, accepted=6)
    + 'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="0"} 8\n'
    + 'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="1"} 1\n'
)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _start(self, text):
        """
        建 db / collector / app 并进入 TestClient（触发 lifespan：
        立即采集一次 + 启动后台定期任务）。返回 (db, collector, app, client)。
        """
        db = Database(self.tmp / "api_test.db")
        # 间隔取很大值（make_config 默认 3600s）：测试期间后台循环不会触发额外采集，轮次由本测试手动驱动
        collector = MetricsCollector(make_config(), db)
        parsed = parse_metrics(text)

        async def _fetch_parsed():
            return parsed

        collector._fetch_parsed = _fetch_parsed
        app = build_app(db, collector)
        client = TestClient(app)
        client.__enter__()
        return db, collector, app, client

    def _round(self, client, collector, text=None, offline=False):
        """在应用的事件循环上（portal）触发一次采集，保持 DB 连接单线程使用。"""
        if offline:
            async def _fetch_offline():
                return None

            collector._fetch_parsed = _fetch_offline
        elif text is not None:
            parsed = parse_metrics(text)

            async def _fetch_parsed():
                return parsed

            collector._fetch_parsed = _fetch_parsed
        client.portal.call(collector.collect_once)

    def test_status_after_baseline_and_round(self):
        db, collector, app, client = self._start(TEXT_A_POS)
        try:
            # 第一轮：lifespan 立即采集已完成，status 立即可用
            data = client.get("/api/status").json()
            self.assertTrue(data["server_online"])
            self.assertEqual(data["llama_server_url"], "http://127.0.0.1:9/metrics")
            self.assertEqual(data["requests_processing"], 1)
            self.assertEqual(data["requests_deferred"], 0)
            self.assertIsNone(data["context_max"])   # 样本无 context 指标
            self.assertIsNone(data["prompt_tps"])    # baseline：分母 0
            self.assertIsNone(data["decode_tps"])
            self.assertIsNone(data["mtp_accept_rate"])
            self.assertIsNotNone(data["last_update"])

            # 第二轮：正常增长
            self._round(client, collector, TEXT_MTP)
            data = client.get("/api/status").json()
            self.assertTrue(data["server_online"])
            self.assertAlmostEqual(data["prompt_tps"], 100.0)        # 50 / 0.5
            self.assertIsNone(data["decode_tps"])                    # output / 秒数未增长
            self.assertAlmostEqual(data["mtp_accept_rate"], 60.0)    # 3 / 5 * 100
        finally:
            client.__exit__(None, None, None)

    def test_status_offline(self):
        db, collector, app, client = self._start(TEXT_A_POS)
        try:
            self._round(client, collector, offline=True)
            data = client.get("/api/status").json()
            self.assertFalse(data["server_online"])
            self.assertIsNone(data["requests_processing"])
            self.assertIsNone(data["context_max"])
            self.assertIsNone(data["prompt_tps"])
            self.assertIsNone(data["decode_tps"])
            self.assertIsNone(data["mtp_accept_rate"])
            self.assertIsNotNone(data["last_update"])
        finally:
            client.__exit__(None, None, None)

    def test_summary(self):
        db, collector, app, client = self._start(TEXT_A_POS)
        try:
            self._round(client, collector, TEXT_MTP)
            data = client.get("/api/summary").json()
            today = data["today"]
            self.assertEqual(today["prompt_tokens"], 50)
            self.assertEqual(today["cached_tokens"], 0)
            self.assertEqual(today["output_tokens"], 0)
            self.assertEqual(today["compute_tokens"], 50)   # prompt + output
            self.assertEqual(today["logical_tokens"], 50)   # prompt + cached + output
            self.assertEqual(data["total"], today)          # 只有一天时两者相同
        finally:
            client.__exit__(None, None, None)

    def test_daily(self):
        db, collector, app, client = self._start(TEXT_A_POS)
        try:
            self._round(client, collector, TEXT_MTP)
            r = client.get("/api/daily", params={"days": 30})
            self.assertEqual(r.status_code, 200)
            days = r.json()["days"]
            self.assertEqual(len(days), 1)
            row = days[0]
            self.assertEqual(row["prompt_tokens"], 50)
            self.assertEqual(row["compute_tokens"], 50)
            self.assertEqual(row["logical_tokens"], 50)
            # 参数校验（16B：days 上限 3650，all=true 返回全历史）
            self.assertEqual(client.get("/api/daily", params={"days": 0}).status_code, 422)
            self.assertEqual(client.get("/api/daily", params={"days": 3651}).status_code, 422)
            r_all = client.get("/api/daily", params={"all": "true"})
            self.assertEqual(r_all.status_code, 200)
            self.assertEqual(client.get("/api/live", params={"minutes": 0}).status_code, 422)
            self.assertEqual(client.get("/api/live", params={"minutes": 2881}).status_code, 422)
        finally:
            client.__exit__(None, None, None)

    def test_live(self):
        db, collector, app, client = self._start(TEXT_A_POS)
        try:
            self._round(client, collector, TEXT_MTP)
            data = client.get("/api/live", params={"minutes": 60}).json()
            self.assertEqual(len(data["samples"]), 2)
            first, last = data["samples"]
            self.assertEqual(first["prompt_delta"], 0)       # baseline 轮
            self.assertEqual(last["prompt_delta"], 50)
            self.assertAlmostEqual(last["prompt_tps"], 100.0)
            self.assertAlmostEqual(last["mtp_accept_rate"], 60.0)
            self.assertEqual(last["requests_processing"], 1)
            self.assertIsNone(last["context_max"])
        finally:
            client.__exit__(None, None, None)

    def test_mtp(self):
        db, collector, app, client = self._start(TEXT_A_POS)
        try:
            self._round(client, collector, TEXT_MTP)
            data = client.get("/api/mtp").json()
            self.assertEqual(data["draft"], 5)
            self.assertEqual(data["accepted"], 3)
            self.assertAlmostEqual(data["accept_rate"], 60.0)
            # Phase 9 新键：draft_tokens/accepted_tokens 别名 + num_drafts（无 drafts counter 时 0）
            self.assertEqual(data["draft_tokens"], 5)
            self.assertEqual(data["accepted_tokens"], 3)
            self.assertEqual(data["num_drafts"], 0)
            # pos1 第二轮才首次出现 -> 只建 baseline（delta=0），不计入当日；
            # pos0 的当日增量为 8-3=5（accepted_tokens 为 Phase 9 新增同值键）
            self.assertEqual(
                data["positions"],
                [{"position": "0", "accepted": 5, "accepted_tokens": 5}],
            )
        finally:
            client.__exit__(None, None, None)

    def test_mtp_no_position_data(self):
        # 服务器不提供 position 数据时：不返回 positions 键
        db, collector, app, client = self._start(TEXT_A)
        try:
            self._round(client, collector, sample(draft=10, accepted=6))
            data = client.get("/api/mtp").json()
            self.assertEqual(data["draft"], 5)
            self.assertEqual(data["accepted"], 3)
            self.assertNotIn("positions", data)
        finally:
            client.__exit__(None, None, None)

    def test_graceful_stop(self):
        db, collector, app, client = self._start(TEXT_A_POS)
        client.__exit__(None, None, None)
        # lifespan 关闭时后台采集任务被优雅取消
        self.assertTrue(app.state.collector_task.cancelled())
        # 数据库连接已关闭
        self.assertIsNone(db._conn)


class ServerHelperRegressionTests(unittest.TestCase):
    """Phase 14 审计回归（AUDIT-ASYNC-005 / AUDIT-SEC-003 CSV 注入）。"""

    def test_recent_dates_uses_injected_now(self):
        """AUDIT-ASYNC-005：_recent_dates 接受 now 参数，FakeClock 下窗口稳定。"""
        import time as _time
        from db import local_date
        from server import _recent_dates

        now = 1_789_000_000.0
        dates = _recent_dates(3, now=now)
        self.assertEqual(len(dates), 3)
        # 最后一天必须是 now 的本机日期（而不是 time.time() 的日期）
        self.assertEqual(dates[-1], local_date(now))
        # 升序且无重复
        self.assertEqual(dates, sorted(set(dates)))

    def test_csv_safe_text_formula_injection(self):
        """AUDIT-SEC-003：= + - @ 开头的外部文本前置单引号；其余原样。"""
        from server import _csv_safe_text

        self.assertEqual(_csv_safe_text("=CMD"), "'=CMD")
        self.assertEqual(_csv_safe_text("+1+2"), "'+1+2")
        self.assertEqual(_csv_safe_text("-5"), "'-5")
        self.assertEqual(_csv_safe_text("@SUM"), "'@SUM")
        self.assertEqual(_csv_safe_text("normal"), "normal")
        self.assertEqual(_csv_safe_text(None), "")


if __name__ == "__main__":
    unittest.main()
