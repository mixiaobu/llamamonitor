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

from test_persistence import TEXT_A, TEXT_B, sample  # 与 unittest discover 的顶层导入方式保持一致

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

    def _start(self, text, clock=None):
        """
        建 db / collector / app 并进入 TestClient（触发 lifespan：
        立即采集一次 + 启动后台定期任务）。返回 (db, collector, app, client)。
        clock：可选 FakeClock 注入（AUDIT-1.1.1 GAP-001 回归测试用）。
        """
        db = Database(self.tmp / "api_test.db")
        # 间隔取很大值（make_config 默认 3600s）：测试期间后台循环不会触发额外采集，轮次由本测试手动驱动
        collector = MetricsCollector(make_config(), db, clock=clock)
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

    def test_unhandled_exception_500_contract(self):
        """AUDIT-1.1.1 GAP-005：未处理异常的兜底 handler 返回 500 + INTERNAL_ERROR
        统一契约（{success:false, error:{code,message}}），绝不返回 Traceback。

        用 raise_server_exceptions=False 的 TestClient：默认 TestClient 对 500 会
        把原始异常重新抛出（便于调试），看不到 handler 产出的 500 响应体。
        注意：不能与 _start 的 client 同时活跃——第二个 TestClient 的 lifespan
        退出会触发 DB 连接跨线程访问（SQLite 单线程不变量），故串行关闭。"""
        db, collector, app, client = self._start(TEXT_A_POS)
        try:
            @app.get("/api/_test_bang")
            async def _bang():
                raise RuntimeError("boom-500-contract")

            client.__exit__(None, None, None)  # 先关闭第一个，避免两个 lifespan 并存
            boom_client = TestClient(app, raise_server_exceptions=False)
            with boom_client:
                r = boom_client.get("/api/_test_bang")
        finally:
            db.close()
        self.assertEqual(r.status_code, 500)
        body = r.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["error"]["code"], "INTERNAL_ERROR")
        self.assertIsInstance(body["error"]["message"], str)
        # 契约字段：绝不带 Python Traceback / 内部异常 repr
        self.assertNotIn("boom-500-contract", str(body))
        self.assertNotIn("Traceback", str(body))

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

    def test_throughput_window_avg(self):
        """Round 5 §46-§48：/api/throughput 返回窗口加权平均吞吐
        （Δtoken/Δseconds，不是逐样本 TPS 简单平均）。

        确定性场景：TEXT_A（baseline，delta 全 0）+ TEXT_B 一轮
        （Δprompt=50 / Δprompt_sec=0.5 -> 100 tok/s；Δoutput=10 / Δpred_sec=1.0
        -> 10 tok/s）。window_avg 在原始样本上求和（baseline 的 delta=0 不影响结果）。
        """
        db, collector, app, client = self._start(TEXT_A)
        try:
            self._round(client, collector, TEXT_B)
            # 参数钳制：minutes 范围 [15,1440]
            self.assertEqual(client.get("/api/throughput", params={"minutes": 14}).status_code, 422)
            self.assertEqual(client.get("/api/throughput", params={"minutes": 1441}).status_code, 422)

            data = client.get("/api/throughput", params={"minutes": 60}).json()
            self.assertEqual(data["minutes"], 60)
            # 两个样本（baseline + 一轮）
            self.assertEqual(len(data["samples"]), 2)
            # 窗口加权平均：ΣΔprompt / ΣΔprompt_sec = 50/0.5 = 100
            self.assertAlmostEqual(data["window_avg"]["prompt_tps_avg"], 100.0)
            # ΣΔoutput / ΣΔpred_sec = 10/1.0 = 10
            self.assertAlmostEqual(data["window_avg"]["decode_tps_avg"], 10.0)
            # 秒数求和（降采样/前端 tooltip 计数一致性）
            self.assertAlmostEqual(data["window_avg"]["prompt_seconds"], 0.5)
            self.assertAlmostEqual(data["window_avg"]["predicted_seconds"], 1.0)
            # 形状字段齐全（前端 renderThroughputMeta 依赖）
            for key in ("available_minutes", "window_minutes", "last_activity_ts"):
                self.assertIn(key, data)
        finally:
            client.__exit__(None, None, None)

    def test_throughput_empty_window(self):
        """Round 5 §48：窗口内无样本时 window_avg 两个平均均为 None（不当 0 tok/s）。"""
        db, collector, app, client = self._start(TEXT_A_POS)
        try:
            # baseline 之后不采集任何一轮 -> live 只有 1 条 baseline 样本
            # （prompt_seconds=0.0 分母 <=0 -> 两个平均 None）
            data = client.get("/api/throughput", params={"minutes": 15}).json()
            self.assertEqual(data["window_avg"]["prompt_tps_avg"], None)
            self.assertEqual(data["window_avg"]["decode_tps_avg"], None)
        finally:
            client.__exit__(None, None, None)

    def test_mtp_range_today(self):
        """Round 5 §82-§109：/api/mtp/range 今天（默认 days=1）——区间求和驱动
        MTP Summary + 趋势 + 按位置三块。确定性：draft=5 accepted=3 rate=60。"""
        db, collector, app, client = self._start(TEXT_A_POS)
        try:
            self._round(client, collector, TEXT_MTP)
            data = client.get("/api/mtp/range").json()  # 默认今天
            self.assertEqual(data["range"], "today")
            self.assertEqual(data["summary"]["draft_tokens"], 5)
            self.assertEqual(data["summary"]["accepted_tokens"], 3)
            self.assertAlmostEqual(data["summary"]["accept_rate"], 60.0)
            # verification_steps（draft_sequences）样本无 drafts counter -> 0
            self.assertEqual(data["summary"]["verification_steps"], 0)
            # steps<=0 -> 平均长度 None（§87-§89，不当 0）
            self.assertIsNone(data["summary"]["avg_draft_length"])
            self.assertIsNone(data["summary"]["avg_accepted_length"])
            # days：仅今天 1 条，accept_rate=60
            self.assertEqual(len(data["days"]), 1)
            self.assertAlmostEqual(data["days"][0]["accept_rate"], 60.0)
            # positions：pos0 当日增量 5（accepted_tokens 同值键）
            self.assertEqual(
                data["positions"],
                [{"position": "0", "accepted": 5, "accepted_tokens": 5}],
            )
        finally:
            client.__exit__(None, None, None)

    def test_mtp_range_days_null_days(self):
        """Round 5 §99-§100：/api/mtp/range?days=7 保留**全部 7 个日期位置**，
        缺失日 accept_rate=null（前端 null 断线/留日期位，不塌缩成 1 点）。"""
        db, collector, app, client = self._start(TEXT_A_POS)
        try:
            self._round(client, collector, TEXT_MTP)
            data = client.get("/api/mtp/range", params={"days": 7}).json()
            self.assertEqual(data["range"], "7")
            self.assertEqual(len(data["days"]), 7)
            # 只有今天有数据：1 条 accept_rate 非 null，其余 6 条 None
            non_null = [d for d in data["days"] if d["accept_rate"] is not None]
            is_null = [d for d in data["days"] if d["accept_rate"] is None]
            self.assertEqual(len(non_null), 1)
            self.assertEqual(len(is_null), 6)
            # 区间 summary 仍是今天的数据（其余天无数据）
            self.assertEqual(data["summary"]["draft_tokens"], 5)
            self.assertAlmostEqual(data["summary"]["accept_rate"], 60.0)
            # positions 区间求和（只有今天有）
            self.assertEqual(data["positions"], [{"position": "0", "accepted": 5, "accepted_tokens": 5}])
        finally:
            client.__exit__(None, None, None)

    def test_mtp_range_all(self):
        """Round 5：/api/mtp/range?all=true 取全部历史（date <= 今天，升序）。"""
        db, collector, app, client = self._start(TEXT_A_POS)
        try:
            self._round(client, collector, TEXT_MTP)
            data = client.get("/api/mtp/range", params={"all": "true"}).json()
            self.assertEqual(data["range"], "all")
            # 只有今天有 daily_usage 行 -> 全部历史即 1 条
            self.assertEqual(len(data["days"]), 1)
            self.assertAlmostEqual(data["days"][0]["accept_rate"], 60.0)
            self.assertEqual(data["summary"]["accepted_tokens"], 3)
        finally:
            client.__exit__(None, None, None)

    def test_graceful_stop(self):
        db, collector, app, client = self._start(TEXT_A_POS)
        client.__exit__(None, None, None)
        # lifespan 关闭时后台采集任务被优雅取消
        self.assertTrue(app.state.collector_task.cancelled())
        # 数据库连接已关闭
        self.assertIsNone(db._conn)

    def test_mtp_rollover_across_midnight(self):
        """
        AUDIT-1.1.1 GAP-001 回归：/api/mtp 的"今日"必须跟随 collector.clock.now()
        的 local_date，跨午夜后切换到新的一天（读新行/空=0），而不是停留在昨日。

        历史 bug：原实现用 wall `local_date()`（time.time() 的日期）与 collector
        落库用的 collector.clock 日期不同源——00:00:00 之后会短暂读"昨天"的累计
        （"今日 MTP 显示昨日数据"），且破坏 FakeClock 测试纪律。

        用 FakeClock 精确推进到本机午夜 +30s 验证切换。
        """
        from datetime import datetime, timedelta
        from clock import FakeClock
        from db import local_date

        # 起点：本机"今天"的 23:50:00（距次日午夜 600s；本地时区，只跨一次午夜）
        today_midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        start_unix = today_midnight.timestamp() + 23 * 3600 + 50 * 60
        clock = FakeClock(start_wall=start_unix, start_mono=start_unix)
        db, collector, app, client = self._start(TEXT_A_POS, clock=clock)
        try:
            self._round(client, collector, TEXT_MTP)
            today_before = local_date(clock.now())
            data = client.get("/api/mtp").json()
            self.assertEqual(data["draft"], 5)
            self.assertEqual(data["accepted"], 3)

            # 推进到 次日 00:00:30（23:50:00 -> 次日 00:00:30；wall+mono 同步）
            secs_to_next_midnight = (today_midnight + timedelta(days=1)).timestamp() - clock.now()
            clock.advance(secs_to_next_midnight + 30)
            today_after = local_date(clock.now())
            self.assertNotEqual(today_before, today_after)  # 确实跨了日

            # 跨日后 /api/mtp 读"新一天"行（无数据 -> 0 / rate null），而非昨日累计
            data = client.get("/api/mtp").json()
            self.assertEqual(data["draft"], 0)
            self.assertEqual(data["accepted"], 0)
            self.assertIsNone(data["accept_rate"])
            # "今日"与兄弟端点（/api/summary）同源：summary 的 month_key 应跟随新一天
            summary = client.get("/api/summary").json()
            self.assertEqual(summary["month_key"], today_after[:7])
        finally:
            client.__exit__(None, None, None)


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
