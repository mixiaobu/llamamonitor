"""
Phase 9 测试：GpuCollector（能量梯形积分 / 睡眠间隙保护 / 过滤 / 状态机 / 落库）。

覆盖：
1. 能量梯形积分：280 -> 300W 过 5 秒 = 290 * 5 / 3600 Wh
2. 睡眠间隙：dt = 60s > interval*3 -> 该段不积分
3. 首个采样无 previous -> 0
4. 跨午夜：前日 23:59:5x 与今日 00:00:00 的样本分别归入前日/今日 gpu_daily，
   前日已有能量保留在前日行
5. device_uuids 过滤：只落选中 GPU
6. disabled -> poll_once 不写库
7. runner 返回非 0 / 抛异常 -> available=False、不崩溃、后续恢复
8. N/A 字段 -> 数据库存 NULL
9. 状态转换日志只在翻转时出现（不刷屏）

项目使用标准库 unittest；临时目录用 tempfile.TemporaryDirectory。运行：
    python -m unittest discover -s tests
"""

import asyncio
import logging
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from config import AppConfig
from db import Database
from gpu_collector import GpuCollector, GpuSnapshot, parse_nvidia_smi_csv, find_nvidia_smi
from configutil import make_config

T0 = 1_700_000_000.0

ROW_A = "0, GPU-E1, Test GPU A, 100, 2048, 50, 55, 280.0, 60, 1700, 9501, 3, 16"
ROW_B = "1, GPU-E2, Test GPU B, 200, 4096, 60, 56, 300.0, 61, 1800, 9501, 3, 16"


def _snap(text: str, now: float) -> list[GpuSnapshot]:
    return parse_nvidia_smi_csv(text, now=now)


def _make_collector(cfg: AppConfig, db: Database, text: str = ROW_A, now: float = T0) -> GpuCollector:
    """构造一个用固定 runner 的 GpuCollector。"""

    async def runner(args, timeout):
        return 0, text

    return GpuCollector(cfg, db, runner=runner, timeout_seconds=3.0)


class EnergyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def _cfg(self, interval: float = 5.0) -> AppConfig:
        cfg = make_config()
        cfg.gpu.poll_interval_seconds = interval
        return cfg

    @staticmethod
    def _total_by_gpu(energy_by_gpu: dict) -> dict[str, float]:
        """{uuid: {date: Wh}} -> {uuid: 合计 Wh}（断言用）。"""
        return {uuid: sum(v.values()) for uuid, v in energy_by_gpu.items()}

    def test_trapezoid(self):
        cfg = self._cfg(interval=5.0)
        db = Database(Path(self._tmp.name) / "e.db", wal=False)
        try:
            c = _make_collector(cfg, db)
            snaps0 = _snap(ROW_A, T0)
            snaps0[0].power_draw_w = 280.0
            snaps1 = _snap(ROW_A, T0 + 5)
            snaps1[0].power_draw_w = 300.0
            e0 = c.energy_deltas(snaps0, mono=T0)
            e1 = c.energy_deltas(snaps1, mono=T0 + 5)
            totals = self._total_by_gpu(e1)
            self.assertEqual(self._total_by_gpu(e0)["GPU-E1"], 0.0)  # 首个采样无 previous
            self.assertAlmostEqual(totals["GPU-E1"], 290.0 * 5 / 3600.0, places=9)
        finally:
            db.close()

    def test_sleep_gap_no_integration(self):
        cfg = self._cfg(interval=5.0)
        db = Database(Path(self._tmp.name) / "e2.db", wal=False)
        try:
            c = _make_collector(cfg, db)
            c.energy_deltas(_snap(ROW_A, T0), mono=T0)
            # 60 秒 > 5 * 3 = 15 秒：unknown gap，不积分（+ 记 data_gaps source=gpu）
            e = c.energy_deltas(_snap(ROW_A, T0 + 60), mono=T0 + 60)
            self.assertEqual(self._total_by_gpu(e)["GPU-E1"], 0.0)
            self.assertEqual(len(db.get_gaps(since_ts=T0)), 1)  # GPU 缺口已记录
            # 间隙后的下一轮正常积分（此时 previous 已更新）
            e2 = c.energy_deltas(_snap(ROW_A, T0 + 65), mono=T0 + 65)
            self.assertGreater(self._total_by_gpu(e2)["GPU-E1"], 0.0)
        finally:
            db.close()  # Windows：未关闭连接会锁住临时目录清理

    def test_missing_power_no_integration(self):
        cfg = self._cfg()
        db = Database(Path(self._tmp.name) / "e3.db", wal=False)
        try:
            c = _make_collector(cfg, db)
            c.energy_deltas(_snap(ROW_A, T0), mono=T0)
            s = _snap(ROW_A, T0 + 5)
            s[0].power_draw_w = None  # N/A
            e = c.energy_deltas(s, mono=T0 + 5)
            self.assertEqual(self._total_by_gpu(e)["GPU-E1"], 0.0)
        finally:
            db.close()


class MidnightAndDailyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = make_config()
        self.cfg.gpu.poll_interval_seconds = 5.0
        self.cfg.gpu.history_retention_hours = 48.0
        self.db = Database(Path(self._tmp.name) / "m.db", wal=False)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_midnight_split(self):
        # 构造跨午夜时刻：今天 00:00:00.5 与昨天 23:59:50.5 / 23:59:59.5
        today_mid = datetime.now().replace(hour=0, minute=0, second=0, microsecond=500)
        yesterday_late1 = today_mid - timedelta(seconds=10.0)   # 23:59:50.5
        yesterday_late2 = today_mid - timedelta(seconds=1.0)    # 23:59:59.5
        t1, t2, t3 = (x.timestamp() for x in (yesterday_late1, yesterday_late2, today_mid))
        if t1 == t2 or t2 == t3:
            self.skipTest("极端时钟边界")

        c = _make_collector(self.cfg, self.db)

        def feed(text: str, now: float, power: float):
            snaps = _snap(text, now)
            snaps[0].power_draw_w = power
            c.poll_once_sync(snaps)

        # 三轮：前日 280W -> 前日 300W -> 今日 320W（每段 1~10 秒，均在 3*interval 内）
        feed(ROW_A, t1, 280.0)
        feed(ROW_A, t2, 300.0)
        feed(ROW_A, t3, 320.0)

        rows = self.db.get_gpu_daily()
        dates = {r["date"] for r in rows}
        self.assertEqual(len(rows), 2, rows)
        self.assertEqual(dates, {yesterday_late1.strftime("%Y-%m-%d"), today_mid.strftime("%Y-%m-%d")})
        y_row = next(r for r in rows if r["date"] == yesterday_late1.strftime("%Y-%m-%d"))
        t_row = next(r for r in rows if r["date"] == today_mid.strftime("%Y-%m-%d"))
        # 第一段（昨日 23:59:50 -> 23:59:59，同在前日）：290 * 9 / 3600 全部归前日
        # 第二段（23:59:59.0005 -> 00:00:00.0005，跨午夜）：(300+320)/2 * 1/3600 按
        # 午夜 00:00:00 边界**精确比例**分割到前日 / 今日（Phase 11 精确午夜分割）
        seg2 = 310.0 * 1 / 3600.0
        midnight = today_mid.replace(microsecond=0).timestamp()
        ratio_prev = (midnight - t2) / (t3 - t2)   # 落在前日的时间比例
        self.assertAlmostEqual(
            y_row["energy_wh"], 290.0 * 9 / 3600.0 + seg2 * ratio_prev, places=6)
        self.assertEqual(y_row["sample_count"], 2)
        self.assertAlmostEqual(t_row["energy_wh"], seg2 * (1.0 - ratio_prev), places=6)
        self.assertEqual(t_row["sample_count"], 1)
        # 能量守恒：两天之和 == 两段梯形积分总和（分割不丢不重）
        self.assertAlmostEqual(
            y_row["energy_wh"] + t_row["energy_wh"],
            290.0 * 9 / 3600.0 + seg2, places=9,
        )

    def test_device_uuids_filter(self):
        c = _make_collector(self.cfg, self.db, text=ROW_A + "\n" + ROW_B)
        self.cfg.gpu.device_uuids = ["GPU-E2"]
        snaps = c.poll_once_sync(_snap(ROW_A + "\n" + ROW_B, T0))
        self.assertEqual([s.uuid for s in snaps], ["GPU-E2"])
        self.assertEqual(self.db.get_gpu_sample_count(), 1)
        self.assertEqual(self.db.get_gpu_latest()[0]["gpu_uuid"], "GPU-E2")

    def test_disabled_no_write(self):
        self.cfg.gpu.enabled = False
        c = _make_collector(self.cfg, self.db)
        self.assertEqual(c.poll_once_sync(_snap(ROW_A, T0)), [])
        self.assertEqual(self.db.get_gpu_sample_count(), 0)

    def test_na_fields_stored_null(self):
        row = "0, GPU-N1, X, 100, 2048, 50, Not Supported, [N/A], 60, 1700, 9501, 3, 16"
        c = _make_collector(self.cfg, self.db, text=row)
        c.poll_once_sync(_snap(row, T0))
        latest = self.db.get_gpu_latest()[0]
        self.assertIsNone(latest["temperature_c"])
        self.assertIsNone(latest["power_draw_w"])
        self.assertEqual(latest["utilization_percent"], 50.0)

    def test_retention_prune(self):
        self.cfg.gpu.history_retention_hours = 1.0
        c = _make_collector(self.cfg, self.db)
        c.poll_once_sync(_snap(ROW_A, T0))
        # 写入一条 2 小时前的旧样本（超出 1h 保留）
        c.poll_once_sync(_snap(ROW_A, T0 + 2 * 3600))
        rows = self.db.get_gpu_samples_since(0)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["timestamp"], T0 + 2 * 3600, places=1)


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = make_config()
        self.cfg.gpu.poll_interval_seconds = 5.0
        self.db = Database(Path(self._tmp.name) / "s.db", wal=False)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _run(self, fn):
        return asyncio.run(fn())

    def test_runner_failure_and_recovery(self):
        calls = {"n": 0}

        async def runner(args, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return 1, "error: something"
            return 0, ROW_A

        c = GpuCollector(self.cfg, self.db, runner=runner)
        with mock.patch("gpu_collector.find_nvidia_smi", return_value=Path("/fake/nvidia-smi")):
            r1 = self._run(c.poll_once)
            self.assertEqual(r1, [])
            self.assertFalse(c.available)
            self.assertIn("code 1", c.unavailable_reason)
            r2 = self._run(c.poll_once)
        self.assertEqual([s.uuid for s in r2], ["GPU-E1"])
        self.assertTrue(c.available)
        self.assertIsNone(c.unavailable_reason)

    def test_runner_exception(self):
        async def runner(args, timeout):
            raise RuntimeError("boom")

        c = GpuCollector(self.cfg, self.db, runner=runner)
        with mock.patch("gpu_collector.find_nvidia_smi", return_value=Path("/fake/nvidia-smi")):
            r = self._run(c.poll_once)
        self.assertEqual(r, [])
        self.assertFalse(c.available)

    def test_no_smi_found(self):
        c = GpuCollector(self.cfg, self.db, runner=None)
        with mock.patch("gpu_collector.find_nvidia_smi", return_value=None):
            r = self._run(c.poll_once)
        self.assertEqual(r, [])
        self.assertFalse(c.available)
        self.assertEqual(c.unavailable_reason, "nvidia-smi not found")

    def test_timeout_kills(self):
        async def runner(args, timeout):
            # 模拟 _default_runner 的超时行为：返回 (-1, "")
            return -1, ""

        c = GpuCollector(self.cfg, self.db, runner=runner)
        with mock.patch("gpu_collector.find_nvidia_smi", return_value=Path("/fake/nvidia-smi")):
            r = self._run(c.poll_once)
        self.assertEqual(r, [])
        self.assertFalse(c.available)

    def test_transition_logging_only_on_flip(self):
        """available 翻转各记一条日志；同状态重复轮次不刷屏。"""
        state = {"ok": True}

        async def runner(args, timeout):
            return (0, ROW_A) if state["ok"] else (1, "err")

        c = GpuCollector(self.cfg, self.db, runner=runner)
        with (
            mock.patch("gpu_collector.find_nvidia_smi", return_value=Path("/fake/nvidia-smi")),
            mock.patch.object(
                logging.getLogger("llamamonitor.gpu"), "warning", autospec=True
            ) as warn,
            mock.patch.object(
                logging.getLogger("llamamonitor.gpu"), "info", autospec=True
            ) as info,
        ):
            for ok in (True, True, True, False, False, True, True):
                state["ok"] = ok
                self._run(c.poll_once)
        # 3 次成功（无翻转）-> 0 条日志；1 次失败翻转 -> 1 WARNING；1 次恢复 -> 1 INFO；
        # 之后 1 次成功（无翻转）-> 0
        self.assertEqual(warn.call_count, 1)
        self.assertEqual(info.call_count, 1)


# 为便于同步测试：给 GpuCollector 加一个同步包装（只在测试中使用）
def _poll_once_sync(self, snaps: list[GpuSnapshot]) -> list[GpuSnapshot]:
    """与 poll_once 相同的落库/状态逻辑，但跳过 find_nvidia_smi/runner（快照已给定）。"""
    if not self.config.gpu.enabled:
        return []
    all_snaps = snaps
    self.detected = [
        {"index": s.index, "uuid": s.uuid, "name": s.name, "memory_total_mb": s.memory_total_mb}
        for s in all_snaps
    ]
    filtered = self._filter(all_snaps)
    if filtered:
        # 测试时钟约定：wall == mono（无系统时间跳变）
        energies = self.energy_deltas(filtered, mono=filtered[0].timestamp)
        if self.db is not None:
            self.db.save_gpu_samples(
                filtered,
                energy_wh=energies,
                now=filtered[0].timestamp,
                retention_seconds=self.config.gpu.history_retention_hours * 3600,
            )
        self.last_update = filtered[0].timestamp
    self._set_available(True, None)
    return filtered


GpuCollector.poll_once_sync = _poll_once_sync


if __name__ == "__main__":
    unittest.main()
