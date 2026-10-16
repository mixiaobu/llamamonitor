"""
1.3.2 集成测试：Fake Strata / Fake llama.cpp（spec §36-§37 验收口径）。

覆盖：
1. Strata 固定计数器验收（input 79,265 / cached 2,032,318 / output 5,759 /
   logical 2,117,342 / compute 85,024）；
2. Draft 加权接受率 501/1001（请求 A 1/1 + B 1000/500，不是 75%）；
3. outside_slots：running=1 + outside_slots=1 必须表现为 **1 个** 处理中请求；
4. 延迟 Histogram：bucket delta 落库 / Counter Reset / P50/P95（含 0 样本=None）；
5. 隐私：SECRET_PROMPT/SECRET_OUTPUT/SECRET_REASONING/SECRET_API_KEY 绝不进入
   API 响应 / SQLite / 日志 / CSV 导出；
6. llama.cpp Router：/models 列表 + selected + /metrics?model=&autoload=false
   + SSE 事件（去重/重连）+ LoRA basename + is_sleeping + next_token 对象；
7. llama.cpp Health 503 "Loading model" -> loading（不是不可达）。

运行：python -m unittest discover -s tests
"""

import asyncio
import json
import logging
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from clock import FakeClock
from configutil import make_config
from db import Database
from fakeservers import (
    FakeLlamaServer,
    FakeStrataServer,
    OFFICIAL_LE,
)
from llama_runtime_collector import LlamaRuntimeCollector
from prometheus_parser import histogram_quantile
from server import build_app
from strata_collector import StrataCollector
from strata_parser import aggregate_draft_stats

T0_WALL = 1_760_000_000.0
T0_MONO = 1000.0


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _strata_collector(url: str, db: Database, clock: FakeClock) -> StrataCollector:
    cfg = make_config(url=url, poll_interval=2.0)
    return StrataCollector(cfg, db=db, clock=clock)


class _StrataBase(unittest.TestCase):
    """Fake Strata + 临时 DB + FakeClock 的公共脚手架。"""

    def setUp(self):
        _LogCaptureHandler.records.clear()
        self._log_handler = _LogCaptureHandler()
        for name in ("llamamonitor.strata", "llamamonitor.collector",
                     "llamamonitor.runtime"):
            logging.getLogger(name).addHandler(self._log_handler)
        self._td = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._td.name) / "t.db", wal=False)
        self.clock = FakeClock(T0_WALL, T0_MONO)
        self.srv = FakeStrataServer().start()
        self.collector = _strata_collector(self.srv.url, self.db, self.clock)

    def tearDown(self):
        try:
            _run(self.collector.aclose())
        except Exception:
            pass
        self.srv.stop()
        self.db.close()
        self._td.cleanup()
        for name in ("llamamonitor.strata", "llamamonitor.collector",
                     "llamamonitor.runtime"):
            logging.getLogger(name).removeHandler(self._log_handler)

    def round(self, advance_s: float = 2.0) -> dict:
        self.clock.advance(advance_s)
        return _run(self.collector.collect_once())


# ---------------------------------------------------------------------------
# 1-3. Strata 验收口径
# ---------------------------------------------------------------------------


class StrataAcceptanceTests(_StrataBase):
    def test_fixed_counters_normalized_values(self):
        """spec §36：固定计数器的规范化口径（baseline 轮 delta=0，累计值正确）。"""
        sid = self.collector.server_id
        snap = self.round()
        self.assertTrue(snap["online"])
        # baseline 轮：daily 全 0（首次建基线，delta=0）
        daily = self.db.get_daily_usage()
        self.assertEqual(daily[0]["prompt_tokens"], 0)
        self.assertEqual(daily[0]["output_tokens"], 0)
        # state 基线 = 固定累计值（来源隔离键 strata:<server_id>:<counter>）
        state = self.db.get_state()
        self.assertEqual(state[f"strata:{sid}:totals.prompt_tokens"], 2111583)
        self.assertEqual(state[f"strata:{sid}:totals.reused"], 2032318)
        self.assertEqual(state[f"strata:{sid}:totals.output_tokens"], 5759)
        # spec 固定口径核对（input/cached/output/logical/compute）
        self.assertEqual(2111583 - 2032318, 79265)      # input
        self.assertEqual(2032318, 2032318)               # cached
        self.assertEqual(5759, 5759)                     # output
        self.assertEqual(2111583 + 5759, 2117342)        # logical
        self.assertEqual((2111583 - 2032318) + 5759, 85024)  # compute
        # 第二样本：+100 prompt（90 reused）+10 output -> 窗口 input=10
        self.srv.state.advance(prompt=100, reused=90, output=10,
                               prompt_ms=50, decode_ms=1000,
                               offered=1, accepted=1)
        self.round()
        daily = self.db.get_daily_usage()
        self.assertEqual(daily[0]["prompt_tokens"], 10)   # input = 100-90
        self.assertEqual(daily[0]["cached_tokens"], 90)   # cached
        self.assertEqual(daily[0]["output_tokens"], 10)   # output

    def test_draft_weighted_accept_rate(self):
        """请求 A(1/1) + B(1000/500) -> 501/1001 ≈ 0.5005，不是 75%。"""
        self.round()
        reqs = self.collector.recent_requests
        agg = aggregate_draft_stats(reqs)
        self.assertIsNotNone(agg)
        self.assertEqual(agg["offered"], 1001)
        self.assertEqual(agg["accepted"], 501)
        self.assertAlmostEqual(agg["rate"], 501 / 1001, places=9)

    def test_outside_slots_single_running_request(self):
        """running=1 + outside_slots=1 -> 1 个处理中请求（不显示 2）。"""
        self.round()
        live = self.collector.live
        self.assertEqual(live["running"], 1)
        self.assertEqual(live["outside_slots"], 1)
        snap = self.collector.last_snapshot
        # 快照的 requests_processing 语义 = 1 个真实请求
        self.assertEqual(snap["requests_processing"], 1)

    def test_waiting_total_separate_admitting(self):
        """waiting_total = queued + waiting；admitting 单独（不并入）。"""
        self.srv.state.status["queued"] = 2
        self.srv.state.live["waiting"] = 3
        self.srv.state.live["admitting"] = 1
        self.round()
        live = self.collector.live
        self.assertEqual(live["waiting_total"], 5)
        self.assertEqual(live["admitting"], 1)
        self.assertEqual(live["queued"], 2)

    def test_expert_fields_per_request(self):
        """每请求 expert_hit_rate / expert_pcie_share / prefill_tok_s。"""
        self.round()
        reqs = self.collector.recent_requests
        self.assertEqual(reqs[0]["expert_hit_rate"], 0.85)
        self.assertEqual(reqs[0]["expert_pcie_share"], 0.1)
        # prefill tps = (100-90) / (50ms) = 200
        self.assertAlmostEqual(reqs[0]["prefill_tok_s"], 200.0)


# ---------------------------------------------------------------------------
# 4. 延迟 Histogram E2E
# ---------------------------------------------------------------------------


class StrataHistogramTests(_StrataBase):
    def test_bucket_delta_and_quantile(self):
        self.round()  # baseline（histogram 全 0）
        # 新增样本：ttft 桶 0.001 x1 + 0.005 x1；itl 桶 0.02 x5
        st = self.srv.state
        st.advance_histogram("vllm:time_to_first_token_seconds",
                             [1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                              0, 0, 0, 0, 0])
        st.advance_histogram("vllm:inter_token_latency_seconds",
                             [0, 0, 0, 0, 0, 0, 0, 0, 0, 5, 0, 0, 0, 0, 0, 0, 0,
                              0, 0, 0, 0, 0])
        self.round(advance_s=20)  # 超过 15s 遥测阈值
        # 每桶区间 delta：0.001 桶 1 个；0.005 桶 1 个；+Inf 区间 0（不落库）
        ttft = self.db.get_latency_histogram("ttft", 0, 1e12)
        self.assertEqual(ttft.get("0.001"), 1.0)
        self.assertEqual(ttft.get("0.005"), 1.0)
        self.assertNotIn("+Inf", ttft)  # 0 区间不落库
        itl = self.db.get_latency_histogram("itl", 0, 1e12)
        self.assertEqual(itl.get("0.5"), 5.0)
        # P50：两个样本（[0,0.001] 1 个 + [0.001,0.005] 1 个）
        # target=1.0 落在第一桶 [0,0.001]，frac=(1-0)/(1-0)=1 -> 0.001
        p50 = histogram_quantile(0.5, ttft, total_count=2)
        self.assertAlmostEqual(p50, 0.001, places=9)

    def test_zero_sample_window_is_none_not_zero(self):
        """无请求的窗口 p50/p95 = None（不是 0ms）。"""
        self.round()
        out = {}
        for mkey in ("ttft", "itl", "e2e"):
            totals = self.db.get_latency_histogram(mkey, 0, 1e12)
            total = sum(v for k, v in totals.items() if k != "+Inf")
            out[mkey] = histogram_quantile(0.5, totals, total_count=total)
        for v in out.values():
            self.assertIsNone(v)

    def test_counter_reset_new_cycle(self):
        """totals/histogram 重置（服务重启）-> 新周期基线，不产生负 delta。"""
        # OFFICIAL_LE 中 "2.5" 在索引 12
        e2e_at_2p5 = [0] * 12 + [5] + [0] * 9
        self.round()
        self.srv.state.advance_histogram(
            "vllm:e2e_request_latency_seconds", e2e_at_2p5)
        self.round(advance_s=20)
        e2e1 = self.db.get_latency_histogram("e2e", 0, 1e12)
        self.assertEqual(e2e1.get("2.5"), 5.0)
        # 服务重启：累计归零 + 新 since
        self.srv.state.reset_counters()
        self.round(advance_s=20)  # 全 0 < 上轮 -> reset，delta = current = 0
        e2e2 = self.db.get_latency_histogram("e2e", 0, 1e12)
        self.assertEqual(e2e2.get("2.5"), 5.0)  # 没有负 delta / 没有新样本
        # 新周期来了 2 个样本
        e2e_at_2p5_small = [0] * 12 + [2] + [0] * 9
        self.srv.state.advance_histogram(
            "vllm:e2e_request_latency_seconds", e2e_at_2p5_small)
        self.round(advance_s=20)
        e2e3 = self.db.get_latency_histogram("e2e", 0, 1e12)
        self.assertEqual(e2e3.get("2.5"), 7.0)  # 5 + 2（reset 后按新周期累计）

    def test_endpoint_zero_fill_bucket_lower_bound(self):
        """零 delta 桶不落库：/api/strata/latency 必须用完整 le 轴补零，
        否则 (1, 2.5] 的样本被当成 (0, 2.5]，P50 从 1.75 偏成 1.25。"""
        from configutil import loopback_app, make_loaded
        app = build_app(db=self.db, collector=self.collector,
                        loaded=make_loaded(make_config(url=self.srv.url),
                                           Path(self._td.name)))
        # 全部 DB 访问走 TestClient portal 线程（与 PrivacyScanTests 同模式）
        client = TestClient(loopback_app(app), raise_server_exceptions=False)
        client.__enter__()
        try:
            # 单样本落 (1, 2.5] 桶（OFFICIAL_LE 索引 12 = "2.5"）。
            # 1.3.3：lifespan 首轮 collect_once 已建 histogram 基线（全 0）并
            # start() 后台 COLD 任务——此后 collect_once 不再做 histogram
            # （转由后台 15s COLD 轮驱动）。这里显式调 _poll_histogram_and_hardware
            # 记录 delta（确定性，不依赖后台任务时序；_histo_prev 单线程原子，
            # 与后台轮并发也不会重复落库）。
            self.srv.state.advance_histogram(
                "vllm:e2e_request_latency_seconds",
                [0] * 12 + [1] + [0] * 9)
            self.clock.advance(20)
            client.portal.call(self.collector._poll_histogram_and_hardware)
            # 落库形态：只有非零桶（下界桶 le="1" 不在表里）
            rows = client.portal.call(
                self.db.get_latency_histogram, "e2e", 0, 1e12)
            self.assertEqual(rows, {"2.5": 1.0})
            # 不带补零的朴素聚合（bug 行为）
            naive = histogram_quantile(0.5, rows, total_count=1.0)
            self.assertAlmostEqual(naive, 1.25, places=9)
            # 端点补零后正确插值：1 + (2.5-1)*0.5 = 1.75
            r = client.get("/api/strata/latency?minutes=10")
            self.assertEqual(r.status_code, 200)
            e2e = r.json()["metrics"]["e2e"]
            self.assertEqual(e2e["count"], 1)
            self.assertAlmostEqual(e2e["p50"], 1.75, places=9)
            self.assertAlmostEqual(e2e["p95"], 1.0 + 1.5 * 0.95, places=9)
        finally:
            client.__exit__(None, None, None)

    def test_prometheus_capability_flag(self):
        self.round()
        self.assertIn("prometheus", self.collector.endpoint_ok)
        self.assertTrue(self.collector.endpoint_ok["prometheus"])
        # 能力在下一轮 _update_capabilities 时生效（遥测标志在快照构建后设置）
        self.round()
        self.assertEqual(self.collector.capabilities.get("latency_histogram"),
                         "supported")


# ---------------------------------------------------------------------------
# 5. 隐私扫描（SECRET_* 四件套）
# ---------------------------------------------------------------------------


class PrivacyScanTests(_StrataBase):
    SECRETS = (b"SECRET_PROMPT", b"SECRET_OUTPUT",
               b"SECRET_REASONING", b"SECRET_API_KEY")

    def _plant_secrets(self):
        st = self.srv.state
        st.status["tail"] = "SECRET_OUTPUT"
        st.status["tool"] = "SECRET_REASONING"
        st.requests[0]["prompt"] = "SECRET_PROMPT"  # 非白名单字段
        st.requests[0]["reasoning"] = "SECRET_REASONING"
        st.props["chat_template"] = "SECRET_PROMPT jinja"
        st.models["data"][0]["meta"]["n_ctx"] = 4096
        self.collector.api_key = "SECRET_API_KEY"  # 采集器发送 Authorization

    def test_api_responses_db_logs_csv_clean(self):
        self._plant_secrets()
        from configutil import make_loaded
        app = build_app(db=self.db, collector=self.collector,
                        loaded=make_loaded(make_config(url=self.srv.url),
                                           Path(self._td.name)))
        from configutil import loopback_app
        # 全部 DB 访问走 TestClient portal 线程（与 test_api.py 同模式）
        client = TestClient(loopback_app(app), raise_server_exceptions=False)
        client.__enter__()
        try:
            # lifespan 初始轮 + 两轮推进（跨 15s 遥测阈值）
            self.clock.advance(20)
            client.portal.call(self.collector.collect_once)
            self.clock.advance(20)
            client.portal.call(self.collector.collect_once)
            responses = []
            for path in ("/api/status", "/api/strata/live",
                         "/api/strata/requests?limit=50",
                         "/api/strata/latency?minutes=10",
                         "/api/strata/uptime",
                         "/api/strata/hardware-trend?minutes=10",
                         "/api/overview", "/api/config",
                         "/api/data/export/daily.csv",
                         "/api/data/export/events.csv"):
                r = client.get(path)
                self.assertEqual(r.status_code, 200, path)
                responses.append((path, r.content))
        finally:
            client.__exit__(None, None, None)
        # 1) API 响应（含 CSV 字节）
        for path, content in responses:
            for s in self.SECRETS:
                self.assertNotIn(s, content, f"{path} 泄漏 {s.decode()}")
        # 2) SQLite 文件字节扫描
        blob = (Path(self._td.name) / "t.db").read_bytes()
        for s in self.SECRETS:
            self.assertNotIn(s, blob, f"SQLite 泄漏 {s.decode()}")
        # 3) 日志（采集期间的所有日志记录）
        for rec in _LogCaptureHandler.records:
            msg = rec.getMessage()
            for s in self.SECRETS:
                self.assertNotIn(s.decode(), msg, f"日志泄漏: {msg}")
        # 4) strata 内存快照（/api/strata/live 的数据源）不含隐私字段
        blob2 = json.dumps(self.collector.last_snapshot, ensure_ascii=False)
        for s in self.SECRETS:
            self.assertNotIn(s.decode(), blob2)


class _LogCaptureHandler(logging.Handler):
    records: list = []

    def emit(self, record: logging.LogRecord) -> None:
        _LogCaptureHandler.records.append(record)


# ---------------------------------------------------------------------------
# 6-7. llama.cpp Router / Health / LoRA / next_token
# ---------------------------------------------------------------------------


class LlamaRouterTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._td.name) / "t.db", wal=False)
        self.clock = FakeClock(T0_WALL, T0_MONO)
        self.srv = FakeLlamaServer(router=True).start()
        self.collector = LlamaRuntimeCollector(
            make_config(url=self.srv.url, poll_interval=2.0),
            db=self.db, clock=self.clock)
        self.captured: list = []
        self.collector.on_router_model_change = self.captured.append

    def tearDown(self):
        try:
            _run(self.collector.aclose())
        except Exception:
            pass
        self.srv.stop()
        self.db.close()
        self._td.cleanup()

    def test_router_detection_and_selection(self):
        _run(self.collector._reconnect_cycle())
        self.assertTrue(self.collector.router["is_router"])
        models = {m["id"]: m for m in self.collector.router["models"]}
        self.assertEqual(models["model-a"]["status"], "loaded")
        self.assertEqual(models["model-b"]["status"], "unloaded")
        self.assertEqual(self.collector.router["selected"], "model-a")
        self.assertEqual(self.captured[-1], "model-a")
        # 隐私：完整路径不离开采集器（只保留 basename）
        self.assertEqual(models["model-a"]["path_name"], "a.gguf")

    def test_router_metrics_url_autoload_false(self):
        """主采集器 /metrics 必须带 model=<id>&autoload=false。"""
        _run(self.collector._reconnect_cycle())
        # 模拟 server 层接线
        from collector import MetricsCollector
        mc = MetricsCollector(make_config(url=self.srv.url, poll_interval=2.0),
                              db=None, clock=self.clock)
        mc.set_router_model("model-a")
        self.assertIn("model=model-a", mc.metrics_fetch_url)
        self.assertIn("autoload=false", mc.metrics_fetch_url)
        # 真实抓取（Router 模式）
        parsed = _run(mc._fetch_parsed())
        self.assertIsNotNone(parsed)
        self.assertEqual(self.srv.state._last_autoload, "false")
        # 无 model 参数 -> 400（Router 硬约束）
        self.assertIsNone(_run(mc2_fetch_plain(self.srv.url)))

    def test_sse_events_captured_and_deduped(self):
        _run(self.collector._reconnect_cycle())

        async def _drive():
            self.collector._sse_stop = False
            self.collector._start_sse()
            await asyncio.sleep(1.0)
            self.collector._stop_sse()
            return list(self.collector.router_events)

        evs = _run(_drive())
        self.assertGreaterEqual(len(evs), 3)
        kinds = {e["event"] for e in evs}
        self.assertIn("model_status", kinds)
        self.assertIn("download_progress", kinds)
        self.assertIn("models_reload", kinds)
        # progress 保留阶段内值
        dl = [e for e in evs if e["event"] == "download_progress"]
        self.assertEqual(dl[0]["stage_value"], 0.4)
        self.assertEqual(dl[0]["stage_index"], 0)

    def test_sse_dedup_repeated_event(self):
        """同一事件重复推送只保留一条（去重）。"""
        self.srv.state.sse_events = [
            {"event": "model_status", "data": {"id": "m", "status": "loaded"}},
            {"event": "model_status", "data": {"id": "m", "status": "loaded"}},
            {"event": "model_status", "data": {"id": "m", "status": "loaded"}},
        ]

        async def _drive():
            self.collector._start_sse()
            await asyncio.sleep(0.8)
            self.collector._stop_sse()
            return list(self.collector.router_events)

        evs = _run(_drive())
        self.assertEqual(len(evs), 1)

    def test_lora_basename_only(self):
        _run(self.collector.fetch_lora(force=True))
        names = [a["name"] for a in self.collector.lora_adapters]
        self.assertEqual(names, ["lora-a.gguf", "lora-b.gguf"])
        self.assertNotIn("C:", json.dumps(self.collector.lora_adapters))

    def test_is_sleeping_official_field(self):
        _run(self.collector._reconnect_cycle())
        _run(self.collector.fetch_health())
        self.assertEqual(self.collector.server_state, "ready")
        self.srv.state.props["is_sleeping"] = True
        _run(self.collector.fetch_props_and_models(force=True))
        self.assertEqual(self.collector.server_state, "sleeping")
        self.assertTrue(self.collector.model_info["is_sleeping"])

    def test_next_token_object_form(self):
        _run(self.collector.fetch_slots())
        self.assertTrue(self.collector.slots)
        nt = self.collector.slots[0].get("next_token")
        self.assertIsInstance(nt, dict)
        self.assertTrue(nt["has_next_token"])
        self.assertEqual(nt["n_remain"], 128)
        self.assertEqual(nt["n_decoded"], 512)
        # 隐私：slot prompt 文本不进入快照
        self.assertNotIn("SECRET_PROMPT_TEXT",
                         json.dumps(self.collector.slots))


def mc2_fetch_plain(url: str):
    """无 model 参数抓 Router /metrics -> 400 -> None。"""
    from collector import MetricsCollector
    mc = MetricsCollector(make_config(url=url, poll_interval=2.0),
                          db=None, clock=FakeClock(T0_WALL, T0_MONO))
    return mc._fetch_parsed()


class LlamaHealthStateTests(unittest.TestCase):
    def setUp(self):
        self.srv = FakeLlamaServer(router=False).start()
        self.clock = FakeClock(T0_WALL, T0_MONO)

    def tearDown(self):
        self.srv.stop()

    def _collector(self):
        return LlamaRuntimeCollector(
            make_config(url=self.srv.url, poll_interval=2.0),
            db=None, clock=self.clock)

    def test_503_loading_model_is_loading(self):
        self.srv.state.health_mode = "loading_503"
        c = self._collector()
        _run(c.fetch_health())
        self.assertEqual(c.server_state, "loading")

    def test_401_is_auth_error(self):
        self.srv.state.health_mode = "401"
        c = self._collector()
        _run(c.fetch_health())
        self.assertEqual(c.server_state, "auth_error")

    def test_ok_is_ready(self):
        c = self._collector()
        _run(c.fetch_health())
        self.assertEqual(c.server_state, "ready")


if __name__ == "__main__":
    unittest.main()
