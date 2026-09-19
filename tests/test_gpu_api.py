"""
Phase 9 测试：GPU / Server Runtime / MTP 增强 API + GPU CSV + Reset/Clear 扩展
+ MTP per-position 计数生命周期。

覆盖：
1. /api/gpu/status：无 GPU 采集器（gpu=None）/ 有采集器（available + 字段齐全 + 显存百分比）
2. /api/gpu/live：按 UUID 分组；>2000 点时后端 bucket 降采样到 <=2000（值 = 桶内均值）
3. /api/gpu/daily：avg = sum/count（count=0 -> null）；跨日行齐全
4. /api/runtime：字段形状 + capabilities（kv_cache/mtp_positions/gpu 等）
5. /api/mtp：num_drafts（draft sequences）+ positions[].accepted_tokens
6. /api/data/export/gpu_daily.csv：BOM + 列齐全 + 平均值
7. Reset Statistics：删 gpu_samples/gpu_daily/mtp_position_daily，保留 state（含 position baseline）
8. Clear Live：删 gpu_samples，保留 gpu_daily / daily_usage / state
9. MTP position 生命周期：baseline -> delta -> counter 重置 -> position 消失 ->
   新 position 出现 -> Collector 重启（共享 db）不重复计数

项目使用标准库 unittest；不访问真实 llama-server / 真实 GPU / 真实 %LOCALAPPDATA%。
运行：
    python -m unittest discover -s tests
"""

import asyncio
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from collector import MetricsCollector, parse_metrics
from configutil import loopback_app, make_config
from db import Database
from gpu_collector import GpuCollector, GpuSnapshot
from server import build_app

GPU_ROW = "0, GPU-API-1, Test GPU, 5120, 8192, 77, 58, 240.5, 62, 1900, 9501, 3, 16"


def _gpu_snap(now: float, uuid: str = "GPU-API-1", util: float = 77.0,
              power: float = 240.5, used: float = 5120.0) -> GpuSnapshot:
    return GpuSnapshot(
        timestamp=now, index=0, uuid=uuid, name="Test GPU",
        utilization_percent=util, memory_used_mb=used, memory_total_mb=8192.0,
        temperature_c=58.0, power_draw_w=power, fan_percent=62.0,
        sm_clock_mhz=1900.0, memory_clock_mhz=9501.0,
        pcie_generation=3, pcie_width=16,
    )


def _gpu_collector(cfg, db, row: str = GPU_ROW) -> GpuCollector:
    async def runner(args, timeout):
        return 0, row

    return GpuCollector(cfg, db, runner=runner, timeout_seconds=3.0)


class GpuApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._dbs: list[Database] = []
        self._clients: list[TestClient] = []
        self._patches: list = []
        self._patch = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.tmp / "lad")})
        self._patch.start()

    def tearDown(self):
        for c in self._clients:
            c.__exit__(None, None, None)
        for p in self._patches:
            p.stop()
        for db in self._dbs:
            try:
                db.close()
            except Exception:
                # 启动失败等场景下连接可能绑在 portal 线程上；尽力关闭
                pass
        self._patch.stop()
        self._tmp.cleanup()

    def _start(self, text: str = "", gpu: GpuCollector | None = "auto",
               offline: bool = False):
        """启动应用（metrics 固定文本；gpu='auto' 时创建真实 GpuCollector+假 runner）。"""
        cfg = make_config()
        cfg.gpu.poll_interval_seconds = 3600.0  # 测试期间周期任务不触发
        db = Database(self.tmp / "gpu.db")
        self._dbs.append(db)
        collector = MetricsCollector(cfg, db)

        async def _fetch():
            return parse_metrics(text) if text else None

        collector._fetch_parsed = _fetch

        gpu_obj = None
        if gpu == "auto":
            gpu_obj = _gpu_collector(cfg, db)
            smi_patch = mock.patch(
                "gpu_collector.find_nvidia_smi", return_value=Path("/fake/nvidia-smi")
            )
            smi_patch.start()
            self._patches.append(smi_patch)

        app = build_app(db, collector, None, gpu_obj)
        client = TestClient(loopback_app(app))  # 修改类 API 要求本地客户端
        client.__enter__()
        self._clients.append(client)
        return client, collector, db, gpu_obj

    def _read_db(self, sql, params=()):
        conn = sqlite3.connect(self.tmp / "gpu.db")
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    # ---------- /api/gpu/status ----------

    def test_status_without_gpu(self):
        client, *_ = self._start(gpu=None)
        r = client.get("/api/gpu/status")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertFalse(d["available"])
        self.assertEqual(d["provider"], "nvidia-smi")
        self.assertEqual(d["gpus"], [])

    def test_status_with_gpu(self):
        client, _, db, gpu = self._start(gpu="auto")
        self.assertTrue(gpu.available)
        r = client.get("/api/gpu/status")
        d = r.json()
        self.assertTrue(d["available"])
        self.assertIsNone(d["reason"])
        self.assertEqual(len(d["gpus"]), 1)
        g = d["gpus"][0]
        self.assertEqual(g["uuid"], "GPU-API-1")
        self.assertEqual(g["index"], 0)
        self.assertEqual(g["name"], "Test GPU")
        self.assertEqual(g["utilization_percent"], 77.0)
        self.assertEqual(g["memory_used_mb"], 5120.0)
        self.assertEqual(g["memory_total_mb"], 8192.0)
        self.assertAlmostEqual(g["memory_usage_percent"], 62.5)
        self.assertEqual(g["power_draw_w"], 240.5)
        self.assertEqual(g["pcie_generation"], 3)
        # detected：nvidia-smi 报告的全集
        self.assertEqual(d["detected"][0]["uuid"], "GPU-API-1")
        self.assertIsNotNone(gpu.last_update)

    # ---------- /api/gpu/live + 降采样 ----------

    def test_live_grouped_and_downsampled(self):
        client, _, db, _ = self._start(gpu=None)
        now = time.time()
        snaps = [_gpu_snap(now - 5000 + i, util=float(i % 100)) for i in range(5000)]
        client.portal.call(db.save_gpu_samples, snaps)
        r = client.get("/api/gpu/live?minutes=1440")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(len(d["gpus"]), 1)
        pts = d["gpus"][0]["points"]
        self.assertLessEqual(len(pts), 2000)
        self.assertGreater(len(pts), 0)
        self.assertEqual(d["gpus"][0]["uuid"], "GPU-API-1")
        # 降采样值 = 桶内非空均值（第一桶：util 0,1,2 平均 1.0）
        bucket = (5000 + 2000 - 1) // 2000  # = 3
        expected_first = round(sum(float(i % 100) for i in range(bucket)) / bucket, 2)
        self.assertEqual(pts[0]["utilization_percent"], expected_first)
        # 每点带 memory_usage_percent（后端计算）
        self.assertAlmostEqual(pts[0]["memory_usage_percent"], 5120 / 8192 * 100, places=2)

    def test_live_small_no_downsample(self):
        client, _, db, _ = self._start(gpu=None)
        now = time.time()
        client.portal.call(db.save_gpu_samples, [_gpu_snap(now - 10), _gpu_snap(now - 5), _gpu_snap(now)])
        d = client.get("/api/gpu/live?minutes=5").json()
        self.assertEqual(len(d["gpus"][0]["points"]), 3)

    # ---------- /api/gpu/daily ----------

    def test_daily_avg_and_none(self):
        client, _, db, _ = self._start(gpu=None)
        today12 = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        yday12 = today12 - timedelta(days=1)
        def save(snaps, **kw):
            # portal.call 不转发 kwargs -> lambda 包装
            return client.portal.call(lambda: db.save_gpu_samples(snaps, **kw))

        # 一次调用 = 一轮采样（每 GPU 一条）：分两轮写入，能量 0.75 + 0.75
        save(
            [_gpu_snap(yday12.timestamp(), util=10.0, power=100.0)],
            energy_wh={"GPU-API-1": 0.75},
            now=today12.timestamp(),
            retention_seconds=None,
        )
        save(
            [_gpu_snap(yday12.timestamp() + 60, util=20.0, power=200.0)],
            energy_wh={"GPU-API-1": 0.75},
            now=today12.timestamp(),
            retention_seconds=None,
        )
        # 今日 GPU-API-1 一条（util 77）
        save([_gpu_snap(today12.timestamp())], now=today12.timestamp() + 1, retention_seconds=None)
        # 今日 GPU-API-2：utilization / power 缺失 -> count=0 -> avg null
        s_none = _gpu_snap(today12.timestamp(), uuid="GPU-API-2")
        s_none.utilization_percent = None
        s_none.power_draw_w = None
        save([s_none], now=today12.timestamp() + 2, retention_seconds=None)

        d = client.get("/api/gpu/daily?days=7").json()
        by_uuid = {g["uuid"]: g for g in d["gpus"]}
        self.assertIn("GPU-API-1", by_uuid)
        # 前日两样本：avg util 15.0 / max 20.0 / avg power 150.0 / energy 1.5
        y_row = next(x for x in by_uuid["GPU-API-1"]["days"] if x["date"] == yday12.strftime("%Y-%m-%d"))
        self.assertEqual(y_row["sample_count"], 2)
        self.assertEqual(y_row["avg_utilization"], 15.0)
        self.assertEqual(y_row["max_utilization"], 20.0)
        self.assertEqual(y_row["avg_power_w"], 150.0)
        self.assertEqual(y_row["max_power_w"], 200.0)
        self.assertAlmostEqual(y_row["energy_wh"], 1.5, places=3)
        # 今日行存在
        t_row = next(x for x in by_uuid["GPU-API-1"]["days"] if x["date"] == today12.strftime("%Y-%m-%d"))
        self.assertEqual(t_row["sample_count"], 1)
        # 全 None GPU：count=0 -> null
        g2 = by_uuid["GPU-API-2"]["days"][0]
        self.assertIsNone(g2["avg_utilization"])
        self.assertIsNone(g2["avg_power_w"])
        self.assertEqual(g2["sample_count"], 1)

    # ---------- /api/runtime ----------

    def test_runtime_shape_and_capabilities(self):
        text = (
            "llamacpp:requests_processing 2\n"
            "llamacpp:requests_deferred 1\n"
            "llamacpp:n_busy_slots_per_decode 2\n"
            "llamacpp:kv_cache_usage_ratio 0.42\n"
            "llamacpp:n_tokens_max 16384\n"
            "llamacpp:n_decode_total 7\n"
            "llamacpp:spec_decode_num_draft_tokens_total 5\n"
            "llamacpp:spec_decode_num_accepted_tokens_total 3\n"
            "llamacpp:spec_decode_num_drafts_total 9\n"
            'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="0"} 3\n'
        )
        client, collector, _, gpu = self._start(text=text, gpu="auto")
        d = client.get("/api/runtime").json()
        self.assertTrue(d["server_online"])
        self.assertEqual(d["requests_processing"], 2)
        self.assertEqual(d["requests_deferred"], 1)
        self.assertEqual(d["busy_slots"], 2)
        self.assertEqual(d["n_tokens_max"], 16384)
        self.assertEqual(d["n_decode_total"], 7)
        self.assertAlmostEqual(d["kv_cache_usage_ratio"], 0.42)
        caps = d["capabilities"]
        self.assertTrue(caps["kv_cache"])
        self.assertTrue(caps["mtp"])
        self.assertTrue(caps["mtp_positions"])
        self.assertTrue(caps["requests"])
        self.assertTrue(caps["busy_slots"])
        self.assertTrue(caps["n_tokens_max"])
        self.assertTrue(caps["drafts"])
        self.assertTrue(caps["gpu"])

    def test_runtime_missing_metrics_are_none(self):
        client, _, _, _ = self._start(text="llamacpp:requests_processing 0\n", gpu=None)
        d = client.get("/api/runtime").json()
        self.assertEqual(d["requests_processing"], 0)
        self.assertIsNone(d["kv_cache_usage_ratio"])
        self.assertIsNone(d["n_tokens_max"])
        self.assertFalse(d["capabilities"]["kv_cache"])
        self.assertFalse(d["capabilities"]["mtp_positions"])
        self.assertFalse(d["capabilities"]["gpu"])

    def test_runtime_offline(self):
        client, _, _, _ = self._start(offline=True, gpu=None)
        d = client.get("/api/runtime").json()
        self.assertFalse(d["server_online"])
        self.assertIsNone(d["requests_processing"])

    # ---------- /api/mtp 增强 ----------

    def test_mtp_num_drafts_and_positions(self):
        a = (
            "llamacpp:spec_decode_num_draft_tokens_total 10\n"
            "llamacpp:spec_decode_num_accepted_tokens_total 6\n"
            "llamacpp:spec_decode_num_drafts_total 4\n"
            'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="0"} 4\n'
            'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="1"} 1\n'
        )
        b = (
            "llamacpp:spec_decode_num_draft_tokens_total 20\n"
            "llamacpp:spec_decode_num_accepted_tokens_total 10\n"
            "llamacpp:spec_decode_num_drafts_total 7\n"
            'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="0"} 8\n'
            'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="1"} 2\n'
        )
        client, collector, db, _ = self._start(text=a, gpu=None)

        async def _fetch_b():
            return parse_metrics(b)

        collector._fetch_parsed = _fetch_b
        client.portal.call(collector.collect_once)

        d = client.get("/api/mtp").json()
        self.assertEqual(d["draft_tokens"], 10)
        self.assertEqual(d["accepted_tokens"], 4)
        self.assertEqual(d["draft"], 10)
        self.assertEqual(d["num_drafts"], 3)
        self.assertEqual(len(d["positions"]), 2)
        self.assertEqual(d["positions"][0]["position"], "0")
        self.assertEqual(d["positions"][0]["accepted_tokens"], 4)
        self.assertEqual(d["positions"][1]["accepted_tokens"], 1)

    # ---------- GPU CSV ----------

    def test_gpu_csv_export(self):
        client, _, db, _ = self._start(gpu=None)
        today12 = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        # 两轮采样，各带能量 1.125
        for offset, util, power in ((0, 50.0, 100.0), (60, 70.0, 150.0)):
            client.portal.call(
                lambda off=offset, u=util, pw=power: db.save_gpu_samples(
                    [_gpu_snap(today12.timestamp() + off, util=u, power=pw)],
                    energy_wh={"GPU-API-1": 1.125},
                    now=today12.timestamp() + off + 120,
                    retention_seconds=None,
                )
            )
        r = client.get("/api/data/export/gpu_daily.csv")
        self.assertEqual(r.status_code, 200)
        body = r.content
        self.assertTrue(body.startswith(b"\xef\xbb\xbf"), "missing UTF-8 BOM")
        text = body.decode("utf-8-sig")
        lines = [ln for ln in text.strip().splitlines() if ln]
        header = lines[0]
        for col in ("date", "gpu_uuid", "gpu_name", "avg_utilization", "max_utilization",
                    "avg_memory_used_mb", "max_memory_used_mb", "avg_temperature",
                    "max_temperature", "avg_power_w", "max_power_w", "energy_wh"):
            self.assertIn(col, header)
        row = lines[1]
        self.assertIn("GPU-API-1", row)
        self.assertIn("60.0", row)      # avg util (50+70)/2
        self.assertIn("70.0", row)      # max util
        self.assertIn("125.0", row)     # avg power (100+150)/2
        self.assertIn("2.25", row)      # energy

    def test_gpu_csv_empty(self):
        client, _, _, _ = self._start(gpu=None)
        r = client.get("/api/data/export/gpu_daily.csv")
        self.assertEqual(r.status_code, 200)
        text = r.content.decode("utf-8-sig")
        self.assertEqual(len(text.strip().splitlines()), 1)  # 只有表头

    # ---------- Reset / Clear 扩展 ----------

    def test_reset_deletes_gpu_tables_keeps_state(self):
        a = (
            "llamacpp:spec_decode_num_draft_tokens_total 10\n"
            "llamacpp:spec_decode_num_accepted_tokens_total 6\n"
            'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="0"} 4\n'
        )
        client, collector, db, _ = self._start(text=a, gpu=None)
        now = time.time()
        client.portal.call(db.save_gpu_samples, [_gpu_snap(now)])
        self.assertEqual(self._read_db("SELECT COUNT(*) FROM gpu_samples")[0][0], 1)
        self.assertEqual(self._read_db("SELECT COUNT(*) FROM mtp_position_daily")[0][0], 0)  # 首轮 baseline

        async def _fetch_a2():
            return parse_metrics(
                "llamacpp:spec_decode_num_draft_tokens_total 20\n"
                "llamacpp:spec_decode_num_accepted_tokens_total 10\n"
                'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="0"} 8\n'
            )

        collector._fetch_parsed = _fetch_a2
        client.portal.call(collector.collect_once)
        self.assertEqual(self._read_db("SELECT COUNT(*) FROM mtp_position_daily")[0][0], 1)
        self.assertEqual(self._read_db("SELECT COUNT(*) FROM gpu_daily")[0][0], 1)
        # state 中有 position baseline
        self.assertTrue(
            self._read_db(
                "SELECT COUNT(*) FROM state WHERE metric_name LIKE 'llamacpp:spec_decode_num_accepted_tokens_per_pos_total%'"
            )[0][0] >= 1
        )

        r = client.post("/api/data/reset-statistics", json={"confirm": "RESET"})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertTrue(d["success"])
        self.assertEqual(d["gpu_samples_deleted"], 1)
        self.assertEqual(d["gpu_daily_deleted"], 1)
        self.assertEqual(d["mtp_position_deleted"], 1)
        self.assertEqual(self._read_db("SELECT COUNT(*) FROM gpu_samples")[0][0], 0)
        self.assertEqual(self._read_db("SELECT COUNT(*) FROM gpu_daily")[0][0], 0)
        self.assertEqual(self._read_db("SELECT COUNT(*) FROM mtp_position_daily")[0][0], 0)
        # state（含 position baseline）保留
        self.assertTrue(
            self._read_db(
                "SELECT COUNT(*) FROM state WHERE metric_name LIKE 'llamacpp:spec_decode_num_accepted_tokens_per_pos_total%'"
            )[0][0] >= 1
        )

    def test_clear_live_deletes_gpu_samples_keeps_gpu_daily(self):
        client, _, db, _ = self._start(gpu=None)
        now = time.time()
        client.portal.call(db.save_gpu_samples, [_gpu_snap(now)])
        r = client.post("/api/data/clear-live", json={"confirm": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._read_db("SELECT COUNT(*) FROM gpu_samples")[0][0], 0)
        self.assertEqual(self._read_db("SELECT COUNT(*) FROM gpu_daily")[0][0], 1)  # daily 保留

    # ---------- MTP position 生命周期 ----------

    def test_mtp_position_lifecycle(self):
        """baseline -> delta -> 重置 -> position 消失 -> 新 position -> 重启不重复计数。"""
        base = (
            "llamacpp:spec_decode_num_draft_tokens_total 100\n"
            "llamacpp:spec_decode_num_accepted_tokens_total 60\n"
        )
        # 注意双花括号：str.format 会把单 {position=...} 当成占位符
        p0 = 'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{{position="0"}} {v}\n'
        p1 = 'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{{position="1"}} {v}\n'
        p2 = 'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{{position="2"}} {v}\n'

        rounds = [
            base + p0.format(v=10),                                   # R1：pos0 baseline
            base + p0.format(v=20) + p1.format(v=3),                  # R2：pos0 +10；pos1 baseline
            base + p0.format(v=5) + p1.format(v=1),                   # R3：pos0 重置(5<20 -> delta 5)；pos1 重置(1<3 -> delta 1)
            base + p0.format(v=6) + p2.format(v=2),                   # R4：pos0 +1；pos1 消失；pos2 baseline
            base + p0.format(v=12) + p2.format(v=5),                  # R5：pos0 +6；pos2 +3
        ]
        client, collector, db, _ = self._start(text=rounds[0], gpu=None)
        for text in rounds[1:]:
            async def _fetch(t=text):
                return parse_metrics(t)

            collector._fetch_parsed = _fetch
            client.portal.call(collector.collect_once)

        def pos_sum(pos):
            rows = self._read_db(
                "SELECT accepted_tokens FROM mtp_position_daily WHERE position = ?", (pos,)
            )
            return rows[0][0] if rows else 0

        # pos0：R2 +10，R3 重置后 +5，R4 +1，R5 +6 = 22（R1 只建 baseline 不计）
        self.assertEqual(pos_sum("0"), 22)
        # pos1：R2 baseline，R3 重置后 +1 = 1（R4 起消失，不再增长）
        self.assertEqual(pos_sum("1"), 1)
        # pos2：R4 baseline，R5 +3 = 3
        self.assertEqual(pos_sum("2"), 3)

        # API 返回顺序：position 数值排序
        d = client.get("/api/mtp").json()
        self.assertEqual([p["position"] for p in d["positions"]], ["0", "1", "2"])

        # Collector 重启（新实例共享同一 db）：从 state 恢复 baseline，不重复计数
        old_client = client
        collector2 = MetricsCollector(collector.config, db)

        async def _fetch_r6():
            return parse_metrics(base + p0.format(v=18) + p2.format(v=9))

        collector2._fetch_parsed = _fetch_r6
        old_client.portal.call(collector2.collect_once)

        # R6：pos0 +6（12->18），pos2 +4（5->9）—— 重启不影响
        self.assertEqual(pos_sum("0"), 28)
        self.assertEqual(pos_sum("2"), 7)


if __name__ == "__main__":
    unittest.main()
