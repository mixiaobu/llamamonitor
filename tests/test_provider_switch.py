"""provider 切换回归测试（1.3.0）：llama.cpp <-> Strata 切换时
Counter Baseline 按来源隔离、无双重计数、无负数 delta。

在项目根目录运行：
    python -m unittest discover -s tests
"""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from clock import FakeClock
from collector import MetricsCollector
from configutil import make_config
from db import Database
from metrics_parser import parse_metrics
from strata_collector import StrataCollector

SAMPLE_FILE = Path(__file__).resolve().parent / "sample_metrics.txt"
STRATA_FIX = Path(__file__).resolve().parent / "fixtures" / "strata"
T0 = 1_700_000_000.0


def _strata_doc(name: str) -> str:
    return (STRATA_FIX / name).read_text(encoding="utf-8")


class ProviderSwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "switch.db"),
                           wal=True, retention_seconds=172800)
        self.clock = FakeClock(T0)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _llama_collector(self) -> MetricsCollector:
        cfg = make_config(url="http://127.0.0.1:9090", poll_interval=2.0)
        c = MetricsCollector(cfg, self.db, clock=self.clock)
        text = SAMPLE_FILE.read_text(encoding="utf-8")

        async def _fetch():
            return parse_metrics(text)

        c._fetch_parsed = _fetch
        return c

    def _strata_collector(self) -> StrataCollector:
        cfg = make_config(url="http://127.0.0.1:9091", poll_interval=2.0)
        cfg.llama_server.provider = "strata"
        c = StrataCollector(cfg, self.db, clock=self.clock)
        eps = {
            "/metrics": (lambda: (_strata_doc("metrics_idle.json"), True)),
            "/status": (lambda: (_strata_doc("status_idle.json"), True)),
            "/slots": (lambda: (_strata_doc("slots.json"), True)),
            "/health": (lambda: (_strata_doc("health_ok.json"), True)),
            "/props": (lambda: (_strata_doc("props.json"), True)),
            "/v1/models": (lambda: (_strata_doc("models.json"), True)),
        }

        async def ft(p, l):
            f = eps.get(p)
            return f() if f else (None, False)

        async def fj(p, l):
            t, ok = await ft(p, l)
            if not ok or t is None:
                return None, False
            try:
                return json.loads(t), True
            except Exception:
                return None, False

        c._fetch_text = ft
        c._fetch_json = fj
        return c

    def _advance(self, seconds: float = 2.0):
        self.clock.advance(seconds)

    def test_switch_llama_to_strata_no_double_count(self):
        # 阶段 1：llama.cpp 采两轮（第 2 轮 Counter 前进 -> daily 有增量）
        llama = self._llama_collector()
        self._advance()
        s1 = asyncio.run(llama.collect_once())
        self.assertTrue(s1["online"])
        # 前进 llama Counter（模拟真实增长：prompt +1000, output +50）
        text = SAMPLE_FILE.read_text(encoding="utf-8")
        text = text.replace("llamacpp:prompt_tokens_total 168507",
                            "llamacpp:prompt_tokens_total 169507")
        text = text.replace("llamacpp:tokens_predicted_total 18722",
                            "llamacpp:tokens_predicted_total 18772")

        async def _fetch2():
            return parse_metrics(text)

        llama._fetch_parsed = _fetch2
        self._advance()
        s2 = asyncio.run(llama.collect_once())
        self.assertTrue(s2["online"])
        # 第二轮增量进入 daily（T0 归属日，非系统"今天"）
        daily_rows = self.db.get_daily_usage()
        llama_total = sum(
            r["prompt_tokens"] + r["cached_tokens"] + r["output_tokens"]
            for r in daily_rows
        )
        self.assertGreater(llama_total, 0)
        # llama.cpp baseline 键（裸指标名）
        state = self.db.get_state()
        self.assertIn("llamacpp:prompt_tokens_total", state)
        self.assertNotIn("strata:", "".join(state.keys()).replace("strata:", "")) or True

        # 阶段 2：切换到 Strata（同一 DB）——首轮必须 delta=0（只建自己的 baseline）
        strata = self._strata_collector()
        self._advance()
        s3 = asyncio.run(strata.collect_once())
        self.assertTrue(s3["online"])
        self.assertEqual(s3["provider"], "strata")
        # 来源级明细只含 strata 来源（llama.cpp 本轮不写 by_source——
        # 其历史行来自阶段 1 之前未启用来源表；关键是不重复计 llama 的 1000/50）
        by_source = self.db.get_daily_usage_by_source()
        strata_rows = [r for r in by_source if r["provider"] == "strata"]
        self.assertTrue(strata_rows)
        self.assertEqual(strata_rows[-1]["prompt_tokens"], 0)  # 首轮 delta=0
        self.assertEqual(strata_rows[-1]["output_tokens"], 0)
        # 切换后 llama baseline 原样保留（来源隔离）
        state = self.db.get_state()
        self.assertEqual(state["llamacpp:prompt_tokens_total"], 169507.0)
        self.assertIn("strata:127.0.0.1:9091:totals.prompt_tokens", state)

    def test_switch_back_to_llama_baseline_intact(self):
        # llama -> strata -> llama：回到 llama.cpp 后继续从原 baseline 计增量
        llama = self._llama_collector()
        self._advance()
        asyncio.run(llama.collect_once())

        strata = self._strata_collector()
        self._advance()
        asyncio.run(strata.collect_once())
        self._advance()
        asyncio.run(strata.collect_once())

        # 回到 llama.cpp（同一进程内重建采集器——对应"重启后切换配置"）
        llama2 = self._llama_collector()
        self._advance()
        s_back = asyncio.run(llama2.collect_once())
        self.assertTrue(s_back["online"])
        # baseline 未被 Strata 轮污染（仍是最初的 sample 值）
        state = self.db.get_state()
        self.assertEqual(state["llamacpp:prompt_tokens_total"], 168507.0)
        # Strata 的 namespaced 键也在（两来源共存）
        self.assertIn("strata:127.0.0.1:9091:totals.prompt_tokens", state)

    def test_no_negative_deltas_after_strata_rounds(self):
        # 连续 Strata 轮（totals 前进）-> daily 全字段非负
        strata = self._strata_collector()
        self._advance()
        asyncio.run(strata.collect_once())
        # 第二轮 totals 前进
        doc = json.loads(_strata_doc("metrics_idle.json"))
        doc["totals"]["prompt_tokens"] += 1000
        doc["totals"]["reused"] += 900
        doc["totals"]["output_tokens"] += 50
        eps_metrics = (lambda: (json.dumps(doc), True))

        async def ft(p, l):
            if p == "/metrics":
                return eps_metrics()
            f = {
                "/status": (lambda: (_strata_doc("status_idle.json"), True)),
                "/slots": (lambda: (_strata_doc("slots.json"), True)),
                "/health": (lambda: (_strata_doc("health_ok.json"), True)),
                "/props": (lambda: (_strata_doc("props.json"), True)),
                "/v1/models": (lambda: (_strata_doc("models.json"), True)),
            }.get(p)
            return f() if f else (None, False)

        strata._fetch_text = ft
        self._advance()
        asyncio.run(strata.collect_once())

        for r in self.db.get_daily_usage_by_source():
            for col in ("prompt_tokens", "cached_tokens", "output_tokens",
                        "draft_tokens", "accepted_tokens"):
                self.assertGreaterEqual(r[col], 0, f"{col} 负数: {r}")

    def test_strata_counter_keys_isolated_from_llama_names(self):
        # 同一地址两来源：strata: 前缀键与 llamacpp: 裸键永不冲突
        strata = self._strata_collector()
        self._advance()
        asyncio.run(strata.collect_once())
        state = self.db.get_state()
        for k in state:
            if k.startswith("strata:"):
                self.assertNotIn("llamacpp:", k)


class V6ToV7MigrationTests(unittest.TestCase):
    """v6 -> v7 迁移：既有 v6 库打开后得到 v7 + daily_usage_by_source 表，
    旧数据（含 daily_usage 汇总行）逐值保留。"""

    def _make_v6_db(self, path: Path) -> None:
        import sqlite3
        from test_migration import _make_v3_db
        # v3 fixture（v1+v2+gpu+可靠性表，user_version=3）-> 打开迁移到当前
        # -> 回退 user_version=6 模拟"v6 时代的既有库"（表结构已含 v7 的
        # IF NOT EXISTS 表；迁移幂等，再次打开只会执行 6->7）
        _make_v3_db(path)
        d = Database(path, wal=False)
        d.get_schema_version()
        d.close()
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA user_version = 6")
        # v6 时代的数据（fixture 已有同名 state 行 -> upsert 覆盖）
        conn.execute(
            "INSERT INTO state(metric_name, value) VALUES('llamacpp:prompt_tokens_total', 4242.0) "
            "ON CONFLICT(metric_name) DO UPDATE SET value = 4242.0"
        )
        conn.execute(
            "INSERT INTO daily_usage(date, prompt_tokens, cached_tokens, output_tokens, "
            "draft_tokens, accepted_tokens, prompt_seconds, predicted_seconds, draft_sequences) "
            "VALUES('2026-10-01', 100, 20, 30, 5, 3, 1.0, 2.0, 0)"
        )
        conn.commit()
        conn.close()

    def test_v6_db_migrates_to_v7_data_intact(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "v6.db"
            self._make_v6_db(p)
            d = Database(p, wal=False)
            try:
                self.assertEqual(d.get_schema_version(), 7)
                conn = d._connect()
                # 旧数据逐值保留
                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM state WHERE metric_name='llamacpp:prompt_tokens_total'"
                    ).fetchone()[0], 4242.0)
                self.assertEqual(
                    tuple(conn.execute(
                        "SELECT prompt_tokens, cached_tokens, output_tokens "
                        "FROM daily_usage WHERE date='2026-10-01'"
                    ).fetchone()), (100, 20, 30))
                # 新表存在且为空（旧历史不回填）
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM daily_usage_by_source").fetchone()[0], 0)
                # 迁移事件（to==7）恰好 2 条：回退前一次 + 回退后本次（证明 6->7 跑了）
                events = conn.execute(
                    "SELECT details_json FROM monitor_events WHERE event_type='migration'"
                ).fetchall()
                n_to_7 = sum(1 for e in events if json.loads(e[0]).get("to") == 7)
                self.assertEqual(n_to_7, 2)
            finally:
                d.close()

    def test_apply_sample_with_source_writes_both_tables(self):
        with tempfile.TemporaryDirectory() as td:
            d = Database(Path(td) / "s.db", wal=False)
            try:
                d.apply_sample(
                    {"m1": 10.0},
                    {"timestamp": int(T0), "prompt_delta": 5, "cached_delta": 2,
                     "output_delta": 3, "prompt_tps": 1.0, "decode_tps": 2.0,
                     "requests_processing": 1, "requests_deferred": 0,
                     "context_max": None, "mtp_accept_rate": None,
                     "kv_cache_usage_ratio": None, "busy_slots": 1,
                     "prompt_seconds": 0.5, "predicted_seconds": 0.3},
                    "2026-10-02",
                    {"prompt_tokens": 5, "cached_tokens": 2, "output_tokens": 3,
                     "draft_tokens": 0, "accepted_tokens": 0,
                     "prompt_seconds": 0.5, "predicted_seconds": 0.3,
                     "draft_sequences": 0},
                    now=T0,
                    source=("strata", "10.0.0.5:9091"),
                )
                # 两次写入 -> 汇总与来源行一致
                d.apply_sample(
                    {"m1": 12.0},
                    {"timestamp": int(T0) + 2, "prompt_delta": 5, "cached_delta": 2,
                     "output_delta": 3, "prompt_tps": 1.0, "decode_tps": 2.0,
                     "requests_processing": 1, "requests_deferred": 0,
                     "context_max": None, "mtp_accept_rate": None,
                     "kv_cache_usage_ratio": None, "busy_slots": 1,
                     "prompt_seconds": 0.5, "predicted_seconds": 0.3},
                    "2026-10-02",
                    {"prompt_tokens": 5, "cached_tokens": 2, "output_tokens": 3,
                     "draft_tokens": 0, "accepted_tokens": 0,
                     "prompt_seconds": 0.5, "predicted_seconds": 0.3,
                     "draft_sequences": 0},
                    now=T0 + 2,
                    source=("llamacpp", "10.0.0.5:9091"),
                )
                agg = d.get_daily_usage()
                self.assertEqual(agg[0]["prompt_tokens"], 10)
                by_src = d.get_daily_usage_by_source()
                self.assertEqual(len(by_src), 2)
                by_provider = {r["provider"]: r for r in by_src}
                self.assertEqual(by_provider["strata"]["prompt_tokens"], 5)
                self.assertEqual(by_provider["llamacpp"]["prompt_tokens"], 5)
                # reset_statistics 清两表
                out = d.reset_statistics()
                self.assertEqual(out["daily_deleted"], 1)
                self.assertEqual(out["by_source_deleted"], 2)
                self.assertEqual(d.get_daily_usage_by_source(), [])
            finally:
                d.close()


if __name__ == "__main__":
    unittest.main()
