"""strata_collector.py 单元测试（1.3.0）。

在项目根目录运行：
    python -m unittest discover -s tests

模式：monkeypatch _fetch_text/_fetch_json 注入 fixture（无真实网络）；
FakeClock 控制时间；临时 SQLite 数据库。
"""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from clock import FakeClock
from configutil import make_config
from db import Database
from strata_collector import (
    STATE_BUSY,
    STATE_LOADING,
    STATE_READY,
    STATE_WAITING,
    StrataCollector,
)

FIX = Path(__file__).resolve().parent / "fixtures" / "strata"
T0 = 1_700_000_000.0


def loadj(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class StrataCollectorTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(
            os.path.join(self.tmp.name, "strata.db"),
            wal=True,
            retention_seconds=48 * 3600,
        )
        self.clock = FakeClock(T0)
        self.cfg = make_config(url="http://127.0.0.1:9091", poll_interval=2.0)
        self.cfg.llama_server.provider = "strata"
        self.collector = StrataCollector(self.cfg, self.db, clock=self.clock)
        # 端点 -> (text, ok) 的脚本表（默认全可用）
        self._endpoints = {}
        for path, name in (
            ("/metrics", "metrics_idle.json"),
            ("/status", "status_idle.json"),
            ("/slots", "slots.json"),
            ("/health", "health_ok.json"),
            ("/props", "props.json"),
            ("/v1/models", "models.json"),
        ):
            self._set_endpoint_default(path, name)
        self._install_mocks()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _set_endpoint_default(self, path: str, name: str):
        text = json.dumps(loadj(name))
        self._endpoints[path] = (lambda t=text: (t, True))

    def set_endpoint(self, path: str, payload, ok: bool = True):
        if isinstance(payload, dict) or isinstance(payload, list):
            text = json.dumps(payload)
        else:
            text = payload
        self._endpoints[path] = (lambda t=text, o=ok: (t, o))

    def set_endpoint_raw(self, path: str, text: str, ok: bool = True):
        self._endpoints[path] = (lambda t=text, o=ok: (t, o))

    def _install_mocks(self):
        async def fetch_text(path, limit):
            fn = self._endpoints.get(path)
            if fn is None:
                return None, False
            return fn()

        async def fetch_json(path, limit):
            text, ok = await fetch_text(path, limit)
            if not ok or text is None:
                return None, False
            try:
                return json.loads(text), True
            except Exception:
                return None, False

        self.collector._fetch_text = fetch_text
        self.collector._fetch_json = fetch_json

    def run_round(self, advance: float = 2.0) -> dict:
        self.clock.advance(advance)
        return asyncio.run(self.collector.collect_once())


class PersistenceTests(StrataCollectorTestBase):
    def test_baseline_first_round_writes_state(self):
        snap = self.run_round()
        self.assertTrue(snap["online"])
        state = self.db.get_state()
        prefix = "strata:127.0.0.1:9091:"
        keys = [k for k in state if k.startswith(prefix)]
        self.assertIn(prefix + "totals.prompt_tokens", keys)
        self.assertIn(prefix + "totals.since", keys)
        self.assertEqual(state[prefix + "totals.prompt_tokens"], 9080209.0)
        # live 行写入（首轮 delta=0）
        live = self.db.get_latest_live_sample()
        self.assertIsNotNone(live)
        self.assertEqual(live["prompt_delta"], 0)

    def test_daily_source_rows(self):
        self.run_round()
        # 无变化 delta：daily 行应存在（0 增量）——统一汇总与来源明细同事务
        self.run_round()
        by_source = self.db.get_daily_usage_by_source()
        self.assertTrue(any(
            r["provider"] == "strata" and r["server_id"] == "127.0.0.1:9091"
            for r in by_source
        ))
        daily = self.db.get_daily_usage()
        self.assertEqual(len(by_source), len(daily))  # 同事务写入，行数一致

    def test_delta_round_updates_daily(self):
        self.run_round()
        # 第二轮：totals 前进（包进完整 /metrics 文档）
        new_totals = dict(loadj("metrics_epoch_change.json")["totals"])
        new_totals["since"] = loadj("metrics_idle.json")["totals"]["since"]  # 同 epoch
        self.set_endpoint("/metrics", {**loadj("metrics_idle.json"), "totals": new_totals})
        self.run_round()
        daily = self.db.get_daily_usage()
        row = [r for r in daily if r["date"]]
        self.assertTrue(row)
        r = row[-1]
        # Δprompt_total = 2634494, Δreused = 2630882 -> new_prompt = 3612
        self.assertEqual(r["prompt_tokens"], 3612)
        self.assertEqual(r["cached_tokens"], 2630882)
        self.assertEqual(r["output_tokens"], 6907)

    def test_invalid_sample_keeps_baseline(self):
        self.run_round()
        self.set_endpoint("/metrics", loadj("metrics_invalid_reused_gt_prompt.json"))
        snap = self.run_round()
        self.assertFalse(snap.get("_valid", True))
        state = self.db.get_state()
        key = "strata:127.0.0.1:9091:totals.prompt_tokens"
        self.assertEqual(state[key], 9080209.0)  # 保持旧 baseline

    def test_epoch_change_event_and_baselines(self):
        self.run_round()
        self.set_endpoint("/metrics", loadj("metrics_epoch_change.json"))
        self.run_round()
        state = self.db.get_state()
        key = "strata:127.0.0.1:9091:totals.prompt_tokens"
        self.assertEqual(state[key], 11714703.0)  # 新周期 baseline
        events = self.db.get_events_range(limit=50)
        self.assertTrue(any(e["event_type"] == "strata_instance_changed" for e in events))

    def test_offline_round_opens_gap(self):
        self.run_round()
        self.set_endpoint("/metrics", None, ok=False)
        snap = self.run_round()
        self.assertFalse(snap["online"])
        open_gap = self.collector.open_gap_property()
        self.assertIsNotNone(open_gap)
        self.assertEqual(open_gap["reason"], "server_offline")
        # 恢复后缺口关闭并落库
        self.set_endpoint("/metrics", loadj("metrics_idle.json"))
        self.run_round(advance=10)
        gaps = self.db.get_gaps_range(limit=10)
        self.assertTrue(any(g["reason"] == "server_offline" for g in gaps))


class StateMachineTests(StrataCollectorTestBase):
    def test_ready_when_idle(self):
        self.run_round()
        self.assertEqual(self.collector.server_state, STATE_READY)

    def test_busy_when_generating(self):
        self.set_endpoint("/metrics", loadj("metrics_generating.json"))
        self.set_endpoint("/status", loadj("status_busy.json"))
        self.run_round()
        self.assertEqual(self.collector.server_state, STATE_BUSY)
        self.assertTrue(self.collector.live["busy"])

    def test_loading_when_health_loaded_false(self):
        self.set_endpoint("/health", loadj("health_loading.json"))
        self.run_round()
        self.assertEqual(self.collector.server_state, STATE_LOADING)

    def test_waiting_when_unreachable(self):
        for p in ("/metrics", "/status", "/slots", "/health", "/props", "/v1/models"):
            self.set_endpoint(p, None, ok=False)
        self.run_round()
        self.assertEqual(self.collector.server_state, STATE_WAITING)
        self.assertFalse(self.collector.last_snapshot["online"])

    def test_unreachable_after_online(self):
        self.run_round()
        for p in ("/metrics", "/status", "/slots", "/health", "/props", "/v1/models"):
            self.set_endpoint(p, None, ok=False)
        self.run_round()
        self.assertEqual(self.collector.server_state, "unreachable")

    def test_sleeping_from_props(self):
        self.set_endpoint("/props", loadj("props_sleeping.json"))
        self.run_round()
        self.assertEqual(self.collector.server_state, "sleeping")


class SnapshotShapeTests(StrataCollectorTestBase):
    def test_snapshot_has_strata_block(self):
        snap = self.run_round()
        self.assertEqual(snap["provider"], "strata")
        st = snap["strata"]
        self.assertEqual(st["state"], STATE_READY)
        self.assertEqual(st["state_label"], "就绪")
        self.assertEqual(st["model"], "qwen3.8-flash-next-unsloth-ud-q4_k_xl")
        self.assertEqual(st["context_window"], 262144.0)
        self.assertTrue(st["images_supported"])

    def test_tail_never_in_snapshot(self):
        self.set_endpoint("/status", loadj("status_busy.json"))
        self.set_endpoint("/metrics", loadj("metrics_generating.json"))
        snap = self.run_round()
        blob = json.dumps(snap, ensure_ascii=False)
        self.assertNotIn("这是模型正在生成的实际文本", blob)


class EventTests(StrataCollectorTestBase):
    def test_online_offline_events(self):
        self.run_round()
        self.set_endpoint("/metrics", None, ok=False)
        self.run_round()
        self.set_endpoint("/metrics", loadj("metrics_idle.json"))
        self.run_round()
        events = self.db.get_events_range(limit=50)
        types = [e["event_type"] for e in events]
        self.assertIn("strata_offline", types)
        self.assertIn("strata_online", types)
        # 来源标签
        self.assertTrue(all(e["source"] == "strata" for e in events
                            if e["event_type"] in ("strata_offline", "strata_online")))

    def test_model_loaded_event_on_transition(self):
        self.set_endpoint("/health", loadj("health_loading.json"))
        self.run_round()
        self.assertEqual(self.collector.server_state, STATE_LOADING)
        self.set_endpoint("/health", loadj("health_ok.json"))
        # health 每 HEALTH_EVERY(5) 轮探测一次：跑到第 6 轮触发重新探测
        for _ in range(5):
            self.run_round()
        events = self.db.get_events_range(limit=50)
        self.assertTrue(any(e["event_type"] == "strata_model_loaded" for e in events))


class PrivacyTests(StrataCollectorTestBase):
    def test_no_tail_or_chat_template_in_memory_state(self):
        self.set_endpoint("/status", loadj("status_busy.json"))
        self.set_endpoint("/metrics", loadj("metrics_generating.json"))
        self.run_round()
        blob = json.dumps({
            "live": self.collector.live,
            "engine": self.collector.engine_info,
            "model": self.collector.model_info,
            "requests": self.collector.recent_requests,
            "cc": self.collector.conversation_cache,
            "slots": self.collector._slots,
        }, ensure_ascii=False, default=str)
        self.assertNotIn("这是模型正在生成的实际文本", blob)
        self.assertNotIn("Jinja", blob)
        # model_path 完整本地路径不进内存快照
        self.assertNotIn("C:\\Users\\mixiaobu\\Models", blob)
        self.assertNotIn("C:/Users/mixiaobu/Models", blob)


class ApiSurfaceTests(StrataCollectorTestBase):
    def test_last_valid_property(self):
        self.assertIsNone(self.collector._last_valid)
        self.run_round()
        self.assertIsNotNone(self.collector._last_valid)

    def test_capabilities(self):
        self.run_round()
        self.assertEqual(self.collector.capabilities.get("request_history"), "supported")
        self.assertEqual(self.collector.capabilities.get("draft_position_statistics"), "unsupported")

    def test_server_id(self):
        self.assertEqual(self.collector.server_id, "127.0.0.1:9091")
        self.assertEqual(self.collector.metrics_url, "http://127.0.0.1:9091/metrics")


if __name__ == "__main__":
    unittest.main()
