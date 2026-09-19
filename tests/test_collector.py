"""collector.py 快照构建冒烟测试（Phase 1）。

在项目根目录运行：
    python -m unittest discover -s tests -v
"""

import asyncio
import json
import unittest
from pathlib import Path

from collector import MetricsCollector, build_snapshot, offline_snapshot
from configutil import make_config
from metrics_parser import parse_metrics

SAMPLE_FILE = Path(__file__).resolve().parent / "sample_metrics.txt"


def _parsed_sample() -> dict:
    return parse_metrics(SAMPLE_FILE.read_text(encoding="utf-8"))


class BuildSnapshotTests(unittest.TestCase):
    def test_build_snapshot_matches_expected_values(self):
        snapshot = build_snapshot(_parsed_sample())
        self.assertTrue(snapshot["online"])
        self.assertEqual(snapshot["prompt_tokens_total"], 168507)
        self.assertEqual(snapshot["cached_tokens_total"], 3314650)
        self.assertEqual(snapshot["output_tokens_total"], 18722)
        self.assertEqual(snapshot["draft_tokens_total"], 23912)
        self.assertEqual(snapshot["accepted_tokens_total"], 14739)
        self.assertEqual(snapshot["spec_drafts_total"], 5420)
        self.assertEqual(snapshot["requests_processing"], 1)
        self.assertEqual(snapshot["requests_deferred"], 0)
        self.assertEqual(snapshot["n_decode_total"], 1501)
        self.assertEqual(snapshot["n_tokens_max"], 2)
        self.assertEqual(snapshot["n_busy_slots_per_decode"], 1)
        self.assertAlmostEqual(snapshot["prompt_seconds_total"], 12.345)
        self.assertAlmostEqual(snapshot["output_seconds_total"], 88.12)
        self.assertEqual(
            snapshot["spec_accepted_per_position"],
            {"0": 14039, "1": 652, "2": 44, "3": 4},
        )

    def test_missing_fields_are_none(self):
        # 空 metrics：抓取本身成功（online=true），但所有字段必须为 None
        snapshot = build_snapshot({})
        self.assertTrue(snapshot["online"])
        for key, value in snapshot.items():
            if key != "online":
                self.assertIsNone(value, key)

    def test_offline_snapshot_schema_matches_online(self):
        # 离线与在线快照键集一致，UI/数据库处理逻辑才能统一
        self.assertEqual(set(offline_snapshot().keys()), set(build_snapshot({}).keys()))
        self.assertFalse(offline_snapshot()["online"])

    def test_snapshot_is_json_serializable(self):
        json.dumps(build_snapshot(_parsed_sample()), ensure_ascii=False)


class CollectOnceTests(unittest.TestCase):
    def test_offline_snapshot_when_server_unreachable(self):
        # 本地 1 端口几乎必然被拒绝连接 -> 必须返回离线快照且不抛异常
        cfg = make_config()
        cfg.llama_server.url = "http://127.0.0.1:1"
        cfg.llama_server.timeout_seconds = 2.0
        collector = MetricsCollector(cfg)
        snapshot = asyncio.run(collector.collect_once())
        self.assertFalse(snapshot["online"])


if __name__ == "__main__":
    unittest.main()
