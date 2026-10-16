"""1.3.3 实时监控链路重构 —— 场景回归测试（spec §44）。

覆盖（12+ 场景）：
Strata 活动判定
  1.  busy + /slots.is_processing=false  -> active（metrics.live 权威，§4）
  2.  reading + generated==0             -> active（Prompt 处理是活动，§5）
  3.  /metrics 超时（连续失败轮）        -> 保留 stale activity，不翻 idle（§15/§30）
  4.  /status 超时                       -> 不影响实时（metrics 独立，§29）
  5.  live_last_update 仅 HOT 成功推进（§14）
llama.cpp 活动判定（§6）
  6.  metrics requests_processing=1      -> active
  7.  metrics=0 + slot is_processing=true -> active（slots 权威源）
  8.  TPS 无/0 但 processing             -> active（绝不用 TPS 判忙闲）
  9.  两源都失败                         -> 保留 stale activity，不翻 idle
  10. requests_deferred>0                -> queued
llama.cpp 实时 TPS（§7）
  11. delta/monotonic 间隔计算 + id_task 变化重设基线（不跨任务混算）
  12. 计数器倒退 -> 只重设基线（绝不产负 TPS）
端点健康 / 错误分类（§16-§18）
  13. 404/501 -> unsupported，能力错误 15s 恢复节流（不 1s 狂刷）
  14. timeout/5xx -> 瞬时错误，快速恢复（下一轮即重试）
  15. classify_http_status / classify_exception 映射
调度（§11）
  16. DeadlineScheduler：不漂移/不重叠/卡住不无限追赶（resync）
API（§12/§20/§23）
  17. /api/realtime 形态 + Cache-Control: no-store + sequence 单调递增

在项目根目录运行（venv python）：
    python -m unittest discover -s tests
"""

import asyncio
import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from clock import FakeClock
from configutil import make_config
from db import Database
from endpoint_state import (
    CAPABILITY_RETRY_SECONDS,
    classify_exception,
    classify_http_status,
    EndpointRegistry,
)
from scheduling import DeadlineScheduler
from strata_collector import StrataCollector
from collector import MetricsCollector

FIX = Path(__file__).resolve().parent / "fixtures" / "strata"
T0 = 1_700_000_000.0


def _load_strata(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


# ============================================================
# Strata
# ============================================================

class StrataRealtimeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "rt.db"), wal=True,
                           retention_seconds=48 * 3600)
        self.clock = FakeClock(T0)
        self.cfg = make_config(url="http://127.0.0.1:9091", poll_interval=2.0)
        self.cfg.llama_server.provider = "strata"
        self.collector = StrataCollector(self.cfg, self.db, clock=self.clock)
        # 端点脚本表：path -> (text, ok)
        self._endpoints = {}
        self._set("/metrics", _load_strata("metrics_idle.json"))
        self._set("/status", _load_strata("status_idle.json"))
        self._set("/slots", _load_strata("slots.json"))
        self._set("/health", _load_strata("health_ok.json"))
        self._set("/props", _load_strata("props.json"))
        self._set("/v1/models", _load_strata("models.json"))
        self._install_mocks()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _set(self, path: str, payload, ok: bool = True):
        text = json.dumps(payload) if isinstance(payload, (dict, list)) else payload
        self._endpoints[path] = (lambda t=text, o=ok: (t, o))

    def _install_mocks(self):
        c = self.collector
        clk = self.clock

        def _record(endpoint, ok, err):
            if endpoint:
                c._record_endpoint(endpoint, clk.now(), clk.monotonic(),
                                   None if ok else err)

        async def fetch_text(path, limit, headers=None, timeout=None, endpoint=None):
            fn = self._endpoints.get(path)
            if fn is None:
                _record(endpoint, False, "unreachable")
                return None, False, "unreachable"
            text, ok = fn()
            _record(endpoint, ok, "temporarily_unavailable" if not ok else None)
            return text, ok, None

        async def fetch_json(path, limit, timeout=None, endpoint=None):
            text, ok, err = await fetch_text(path, limit, timeout=timeout,
                                             endpoint=endpoint)
            if not ok or text is None:
                return None, False, err
            try:
                return json.loads(text), True, None
            except Exception:
                _record(endpoint, False, "invalid_response")
                return None, False, "invalid_response"

        c._fetch_text = fetch_text
        c._fetch_json = fetch_json

    def set_metrics(self, payload: dict):
        """替换 /metrics 的 live 块（totals 同步前进，保证样本有效）。"""
        doc = _load_strata("metrics_idle.json")
        for k, v in (payload.get("live") or {}).items():
            doc["live"][k] = v
        for k, v in (payload.get("totals") or {}).items():
            doc["totals"][k] = v
        self._endpoints["/metrics"] = (lambda t=json.dumps(doc): (t, True))

    def set_metrics_fail(self, ok: bool = False):
        self._endpoints["/metrics"] = (lambda: (None, ok))

    def set_status_fail(self):
        self._endpoints["/status"] = (lambda: (None, False))

    def round(self, advance: float = 2.0) -> dict:
        self.clock.advance(advance)
        return asyncio.run(self.collector.collect_once())


class StrataActivityTests(StrataRealtimeBase):
    def test_01_busy_live_authoritative_slots_false(self):
        """§4：metrics.live running>0 权威；/slots.is_processing 不参与。"""
        self.set_metrics({
            "live": {"state": "generating", "running": 1, "tok_s": 40.0,
                     "generated": 100, "max_tokens": 512, "phase": "answering"},
            "totals": {"output_tokens": 25936 + 100,
                       "decode_ms": 712919.0 + 2500.0},
        })
        # /slots 全部 is_processing=false（fixture 默认）
        self.round()
        act = self.collector.activity_state
        self.assertTrue(act["active"])
        self.assertEqual(act["source"], "strata.metrics.live")
        self.assertEqual(act["confidence"], "authoritative")

    def test_02_reading_generated_zero_is_active(self):
        """§5：state=reading + generated==0 = Prompt 处理中（活动），不是空闲。"""
        self.set_metrics({
            "live": {"state": "reading", "running": 1, "generated": 0,
                     "prompt_tokens": 120000, "phase": "reading the prompt"},
        })
        self.round()
        act = self.collector.activity_state
        self.assertTrue(act["active"])
        self.assertEqual(act["state"], "reading")
        self.assertEqual(act["phase"], "reading the prompt")

    def test_03_metrics_timeout_keeps_stale_activity(self):
        """§15/§30：metrics 超时轮保留上一轮 activity（stale 标注），不翻 idle。"""
        self.set_metrics({
            "live": {"state": "generating", "running": 1, "tok_s": 40.0,
                     "generated": 100, "max_tokens": 512, "phase": "answering"},
        })
        self.round()
        self.assertTrue(self.collector.activity_state["active"])
        # /metrics 超时（连续 1 轮 <5：快照保留、状态不翻转）
        self.set_metrics_fail()
        self.round()
        act = self.collector.activity_state
        self.assertTrue(act["active"], "stale active 不得翻 idle")
        self.assertTrue(str(act["source"]).startswith("stale:"))
        self.assertEqual(act["confidence"], "stale")

    def test_04_status_timeout_does_not_stall_realtime(self):
        """§29：/status 失败不影响 /metrics 实时判定（故障隔离）。"""
        self.round()
        first_ts = self.collector.sample_timestamp
        self.set_status_fail()
        self.round()
        # metrics 成功 -> 新鲜度推进；status 端点不可用
        self.assertIsNotNone(self.collector.sample_timestamp)
        self.assertGreater(self.collector.sample_timestamp, first_ts)
        self.assertFalse(self.collector.endpoints.get("status").available)
        act = self.collector.activity_state
        self.assertEqual(act["source"], "strata.metrics.live")

    def test_05_live_last_update_only_on_hot_success(self):
        """§14：live_last_update 只随 HOT PATH 成功推进。"""
        self.round()
        ts = self.collector.live_last_update
        self.assertIsNotNone(ts)
        self.set_metrics_fail()
        self.clock.advance(2.0)
        asyncio.run(self.collector._hot_round())
        self.assertEqual(self.collector.live_last_update, ts,
                         "metrics 失败轮不得推进 live_last_update")
        # 恢复后推进
        self.set_metrics({})
        self.clock.advance(2.0)
        asyncio.run(self.collector._hot_round())
        self.assertGreater(self.collector.live_last_update, ts)


# ============================================================
# llama.cpp
# ============================================================

def _llama_metrics_text(processing: int = 0, deferred: int = 0) -> str:
    return (
        "# HELP x\n# TYPE llamacpp:requests_processing gauge\n"
        f"llamacpp:requests_processing {processing}\n"
        "llamacpp:requests_deferred 0\n"
        f"llamacpp:requests_deferred {deferred}\n"
        "llamacpp:n_tokens_max 4096\n"
        "llamacpp:kv_cache_usage_ratio 0.12\n"
        "llamacpp:prompt_tokens_total 1000\n"
        "llamacpp:output_tokens_total 500\n"
    )


def _slot(id_=0, id_task=1, processing=True, n_decoded=10,
          n_pt=1000, n_pp=1000, params=None):
    return {
        "id": id_, "id_task": id_task, "state": "processing" if processing else "idle",
        "is_processing": processing,
        "n_prompt_tokens": n_pt, "n_prompt_tokens_processed": n_pp,
        "n_decoded": n_decoded,
        "params": params or {},
    }


class LlamaRealtimeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "rt_llama.db"), wal=True,
                           retention_seconds=48 * 3600)
        self.clock = FakeClock(T0)
        self.cfg = make_config(url="http://127.0.0.1:9091", poll_interval=2.0)
        self.cfg.llama_server.provider = "llamacpp"
        self.collector = MetricsCollector(self.cfg, self.db, clock=self.clock)
        # 注入 HOT 数据源（parse_metrics 文本 + slots 白名单 list）
        from metrics_parser import parse_metrics
        from llama_runtime_collector import sanitize_slot
        self._parsed = parse_metrics(_llama_metrics_text())
        self._slots = []
        c = self.collector

        async def fp():
            return self._parsed

        async def fs():
            if getattr(self, "_slots_none", False):
                return None
            if not self._slots:
                return []
            return [sanitize_slot(s) for s in self._slots]

        c._fetch_parsed = fp
        c._fetch_slots = fs

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def set_parsed(self, processing: int, deferred: int = 0):
        from metrics_parser import parse_metrics
        self._parsed = parse_metrics(_llama_metrics_text(processing, deferred))

    def set_metrics_fail(self):
        self._parsed = None

    def set_slots(self, slots: list):
        self._slots = slots
        self._slots_none = False

    def set_slots_fail(self):
        self._slots_none = True

    def hot(self, advance: float = 2.0) -> dict:
        self.clock.advance(advance)
        return asyncio.run(self.collector.hot_round())


class LlamaActivityTests(LlamaRealtimeBase):
    def test_06_metrics_processing_active(self):
        """§6：requests_processing>0 -> active（slots 不可用仍 active）。"""
        self.set_parsed(processing=1)
        self.set_slots_fail()
        self.hot()
        act = self.collector.activity_state
        self.assertTrue(act["active"])
        self.assertEqual(act["source"], "llama.metrics")
        self.assertEqual(act["confidence"], "secondary")

    def test_07_slots_processing_active_metrics_zero(self):
        """§6：metrics=0 但 slot is_processing=true -> active（slots 权威源）。"""
        self.set_parsed(processing=0)
        self.set_slots([_slot(processing=True)])
        self.hot()
        act = self.collector.activity_state
        self.assertTrue(act["active"])
        self.assertEqual(act["source"], "llama.metrics+slots")
        self.assertEqual(self.collector.requests_state["slots_active"], 1)

    def test_08_tps_zero_still_active(self):
        """§6/§7：TPS 尚无新鲜 delta（None）但 processing=1 -> active。"""
        self.set_parsed(processing=1)
        self.set_slots([_slot(processing=True)])
        self.hot()
        act = self.collector.activity_state
        self.assertTrue(act["active"])
        # 首轮只建基线：无新鲜 delta -> None（绝不假 0）
        self.assertIsNone(self.collector.realtime_decode_tps)

    def test_09_both_sources_fail_keeps_stale(self):
        """§15/§29：两源都失败 -> 保留 stale activity，不翻 idle。"""
        self.set_parsed(processing=1)
        self.set_slots([_slot(processing=True)])
        self.hot()
        self.assertTrue(self.collector.activity_state["active"])
        self.set_metrics_fail()
        self.set_slots_fail()
        self.hot()
        act = self.collector.activity_state
        self.assertTrue(act["active"])
        self.assertTrue(str(act["source"]).startswith("stale:"))
        self.assertEqual(act["confidence"], "stale")

    def test_10_requests_deferred_queued(self):
        """§6：requests_deferred>0 = queued（排队中，计入 requests.queued）。"""
        self.set_parsed(processing=0, deferred=2)
        self.set_slots([])
        self.hot()
        self.assertEqual(self.collector.requests_state["queued"], 2)
        self.assertFalse(self.collector.activity_state["active"])


class LlamaTpsTests(LlamaRealtimeBase):
    def test_11_tps_delta_and_task_change_rebaseline(self):
        """§7：Δn_decoded/Δt 计算；id_task 变化 -> 重设基线（不跨任务混算）。"""
        self.set_parsed(processing=1)
        self.set_slots([_slot(id_task=7, n_decoded=100)])
        self.hot(advance=1.0)          # 建基线（n_decoded=100）
        self.assertIsNone(self.collector.realtime_decode_tps)
        # 2s 后 n_decoded=200 -> 50 tok/s
        self.set_slots([_slot(id_task=7, n_decoded=200)])
        self.hot(advance=2.0)
        self.assertEqual(self.collector.realtime_decode_tps, 50.0)
        # id_task 变化（同 slot 新任务）-> 重设基线，无陈旧 delta
        self.set_slots([_slot(id_task=8, n_decoded=10)])
        self.hot(advance=2.0)
        self.assertIsNone(self.collector.realtime_decode_tps)

    def test_12_counter_regression_no_negative_tps(self):
        """§7：计数器倒退 -> 只重设基线，绝不产出负 TPS。"""
        self.set_parsed(processing=1)
        self.set_slots([_slot(id_task=7, n_decoded=200)])
        self.hot(advance=1.0)
        # 倒退（同任务 n_decoded 变小：如任务内部重置）
        self.set_slots([_slot(id_task=7, n_decoded=50)])
        self.hot(advance=2.0)
        # 倒退不产新鲜 delta -> 保持 None（上一轮也无新鲜 delta）
        self.assertIsNone(self.collector.realtime_decode_tps)
        # 再前进 -> 正常计算（50 -> 150 / 2s = 50）
        self.set_slots([_slot(id_task=7, n_decoded=150)])
        self.hot(advance=2.0)
        self.assertEqual(self.collector.realtime_decode_tps, 50.0)
        self.assertGreaterEqual(self.collector.realtime_decode_tps, 0.0)


# ============================================================
# 端点健康 / 错误分类（§16-§18）
# ============================================================

class EndpointStateTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(T0)
        self.reg = EndpointRegistry()

    def test_13_capability_error_throttled_recovery(self):
        """§18：404 -> unsupported（能力错误）：15s 内不重试，15s 后恢复探测。"""
        st = self.reg.get("prometheus")
        st.record_failure(self.clock.now(), self.clock.monotonic(), "unsupported")
        self.assertFalse(st.available)
        self.assertFalse(st.should_poll(self.clock.monotonic() + 5.0),
                         "能力错误 15s 内不得重复请求")
        self.assertTrue(
            st.should_poll(self.clock.monotonic() + CAPABILITY_RETRY_SECONDS),
            "15s 恢复探测应允许")

    def test_14_transient_error_fast_recovery(self):
        """§18：timeout/5xx 是瞬时故障 -> 下一轮即重试（不节流）。"""
        st = self.reg.get("metrics")
        st.record_failure(self.clock.now(), self.clock.monotonic(), "timeout")
        self.assertTrue(st.should_poll(self.clock.monotonic() + 0.1))
        st2 = self.reg.get("slots")
        st2.record_failure(self.clock.now(), self.clock.monotonic(),
                           "temporarily_unavailable")
        self.assertTrue(st2.should_poll(self.clock.monotonic() + 0.1))

    def test_15_classification_maps(self):
        """§17：HTTP 状态码 / 异常 -> 错误分类映射。"""
        self.assertEqual(classify_http_status(404), "unsupported")
        self.assertEqual(classify_http_status(501), "unsupported")
        self.assertEqual(classify_http_status(401), "authentication_error")
        self.assertEqual(classify_http_status(429), "rate_limited")
        self.assertEqual(classify_http_status(500), "temporarily_unavailable")
        self.assertEqual(classify_http_status(408), "timeout")
        self.assertEqual(classify_http_status(None), "unreachable")
        self.assertEqual(classify_http_status(200), "")

        class ReadTimeout(Exception):
            pass
        self.assertEqual(classify_exception(ReadTimeout()), "timeout")

        class ConnectError(Exception):
            pass
        self.assertEqual(classify_exception(ConnectError()), "unreachable")

        class _Other(Exception):
            pass
        self.assertEqual(classify_exception(_Other()), "temporarily_unavailable")

    def test_16_to_dict_shape(self):
        """§16：每端点 diagnostics 形态（age_ms / 连续失败 / 最近错误）。"""
        st = self.reg.get("metrics")
        st.record_success(self.clock.now(), self.clock.monotonic())
        d = st.to_dict(self.clock.now())
        for k in ("available", "last_success", "last_attempt", "age_ms",
                  "consecutive_failures", "last_error"):
            self.assertIn(k, d)
        self.assertTrue(d["available"])
        self.assertEqual(d["consecutive_failures"], 0)
        self.assertEqual(d["age_ms"], 0)
        st.record_failure(self.clock.now(), self.clock.monotonic(), "timeout")
        d2 = st.to_dict(self.clock.now())
        self.assertFalse(d2["available"])
        self.assertEqual(d2["consecutive_failures"], 1)
        self.assertEqual(d2["last_error"], "timeout")


# ============================================================
# 调度（§11）
# ============================================================

class SchedulerTests(unittest.TestCase):
    def test_17_deadline_scheduler_no_drift_no_overlap(self):
        """§11：deadline 锚定不漂移；单轮异常不杀循环；卡住不无限追赶。

        驱动方式：DeadlineScheduler 内部是真实 asyncio.sleep(deadline-now)，
        FakeClock 的 mono 不随真实时间走——每轮前先 clock.advance(1.0) 让
        deadline 到期，再真实 sleep(1.05) 让 sched 完成本轮（确定性）。
        """
        clock = FakeClock(start_wall=T0, start_mono=0.0)
        sched = DeadlineScheduler(1.0, clock, name="test")
        calls = []

        async def work():
            calls.append(clock.monotonic())
            if len(calls) == 2:
                raise RuntimeError("boom")  # 单轮异常不得杀循环

        async def drive():
            task = asyncio.ensure_future(sched.run(work))
            # 先让 sched.run 执行首步（deadline 锚定 mono=0 + interval）——
            # ensure_future 不立即运行协程，若不等，我的 advance 会先于锚点
            for _ in range(5):
                await asyncio.sleep(0)
            # 5 个正常周期（每周期 1.15s 真实时间：sched 内部真实 sleep(1.0)
            # + 0.15s 余量吸收 Windows 定时器粒度/负载抖动；第 6 轮需 t>=6s，
            # 窗口 5.75s 结束 -> 恰好 5 轮）
            for _ in range(5):
                clock.advance(1.0)
                await asyncio.sleep(1.15)
            n5 = len(calls)
            # 模拟卡住 10s：只补一轮立即执行 + 重同步（不无限追赶 10 轮）
            clock.advance(10.0)
            await asyncio.sleep(1.15)
            n_after = len(calls)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return n5, n_after

        n5, n_after = asyncio.run(drive())
        self.assertEqual(n5, 5, "5s 恰好 5 轮（deadline 锚定不漂移；第 2 轮异常不杀循环）")
        self.assertGreaterEqual(n_after - n5, 1, "卡住后仍有轮次（循环存活）")
        self.assertLessEqual(n_after - n5, 4,
                             "10s 卡住不得无限追赶（补 1 轮 + 重同步 + 至多 1~2 个新周期）")


# ============================================================
# /api/realtime 端点（§12/§20/§23）
# ============================================================

class RealtimeApiTests(StrataRealtimeBase):
    def _client(self):
        from fastapi.testclient import TestClient
        from config import LoadedConfig, atomic_write_json, get_default_config
        from configutil import loopback_app
        from server import build_app
        cfg_file = Path(self.tmp.name) / "config.json"
        atomic_write_json(cfg_file, get_default_config())
        loaded = LoadedConfig(config=self.cfg, path=cfg_file, loaded=True,
                              using_defaults=False, has_errors=False)
        app = build_app(self.db, self.collector, loaded, runtime=None)
        return TestClient(loopback_app(app))

    def test_18_realtime_shape_nostore_sequence(self):
        """§12/§20/§23：形态齐全 + no-store + sequence 单调递增。"""
        with self._client() as client:
            r = client.get("/api/realtime")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers.get("cache-control"), "no-store")
            data = r.json()
            for k in ("provider", "server_online", "activity", "throughput",
                      "requests", "slots", "tokens", "freshness", "errors",
                      "diagnostics", "sequence"):
                self.assertIn(k, data, f"missing {k}")
            for k in ("active", "state", "phase", "source", "confidence"):
                self.assertIn(k, data["activity"])
            for k in ("current_decode_tps", "current_prompt_tps",
                      "average_decode_tps", "average_prompt_tps"):
                self.assertIn(k, data["throughput"])
            for k in ("processing", "waiting", "queued"):
                self.assertIn(k, data["requests"])
            for k in ("active", "total"):
                self.assertIn(k, data["slots"])
            for k in ("prompt", "generated", "max_tokens"):
                self.assertIn(k, data["tokens"])
            for k in ("sample_timestamp", "age_ms", "source"):
                self.assertIn(k, data["freshness"])
            for k in ("hot_loop_interval_ms", "last_hot_duration_ms",
                      "hot_overrun", "source"):
                self.assertIn(k, data["diagnostics"])
            self.assertEqual(data["provider"], "strata")
            # 其它实时端点 no-store
            self.assertEqual(client.get("/api/status").headers.get("cache-control"),
                             "no-store")
            # sequence 单调递增（§23 竞态防护字段）
            seq1 = data["sequence"]
            self.round()
            r2 = client.get("/api/realtime")
            seq2 = r2.json()["sequence"]
            self.assertGreater(seq2, seq1)
            # 0-vs-null：idle fixture 的当前生成速度 = null（不是 0）
            self.assertIsNone(data["throughput"]["current_decode_tps"])


if __name__ == "__main__":
    unittest.main()
