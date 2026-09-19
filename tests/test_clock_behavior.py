"""
Phase 11 测试：时钟行为 —— FakeClock 语义、wall/monotonic 分工、
wall 跳变（改时间/时区）不影响基于 monotonic 的间隔与能耗判定。

全部临时目录；不触碰真实数据。
运行：
    python -m unittest discover -s tests
"""

import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from clock import FakeClock, SystemClock
from configutil import make_config
from db import Database
from gpu_collector import GpuCollector, parse_nvidia_smi_csv

ROW_A = "0, GPU-E1, Test GPU A, 100, 2048, 50, 55, 280.0, 60, 1700, 9501, 3, 16"

T0 = 1_789_000_000.0


def _snap(text: str, now: float):
    snaps = parse_nvidia_smi_csv(text, now=now)
    return snaps


class FakeClockSemanticsTests(unittest.TestCase):
    def test_advance_moves_both_clocks(self):
        c = FakeClock(start_wall=1000.0, start_mono=50.0)
        c.advance(10.0)
        self.assertEqual(c.now(), 1010.0)
        self.assertEqual(c.monotonic(), 60.0)
        c.advance(0.5)
        self.assertEqual(c.now(), 1010.5)
        self.assertEqual(c.monotonic(), 60.5)

    def test_advance_negative_raises(self):
        c = FakeClock(start_wall=0.0, start_mono=0.0)
        with self.assertRaises(ValueError):
            c.advance(-1.0)

    def test_set_wall_does_not_touch_monotonic(self):
        c = FakeClock(start_wall=1000.0, start_mono=50.0)
        c.set_wall(9_999_999.0)  # 前跳 5 小时以上
        self.assertEqual(c.now(), 9_999_999.0)
        self.assertEqual(c.monotonic(), 50.0)
        c.set_wall(1.0)  # 回拨
        self.assertEqual(c.now(), 1.0)
        self.assertEqual(c.monotonic(), 50.0)

    def test_system_clock_sane(self):
        c = SystemClock()
        w0, m0 = c.now(), c.monotonic()
        time.sleep(0.02)
        w1, m1 = c.now(), c.monotonic()
        self.assertGreaterEqual(w1, w0)
        self.assertGreater(m1, m0)


class WallJumpEnergyTests(unittest.TestCase):
    """wall 跳变不影响 monotonic 三角积分（能耗）与缺口判定。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_wall_forward_jump_no_fake_energy(self):
        """wall 前跳 5 小时（monotonic 只走 10 秒）：能耗按 10 秒计，无缺口。"""
        with tempfile.TemporaryDirectory() as td:
            db = Database(Path(td) / "gj.db", wal=False)
            try:
                cfg = make_config()
                cfg.gpu.poll_interval_seconds = 5.0
                clock = FakeClock(start_wall=T0, start_mono=0.0)
                async def runner(args, timeout):
                    return 0, ROW_A
                c = GpuCollector(cfg, db, runner=runner, clock=clock)

                s1 = _snap(ROW_A, T0)
                s1[0].power_draw_w = 300.0
                c.energy_deltas(s1, mono=0.0)

                clock.advance(10.0)              # mono +10s
                clock.set_wall(clock.now() + 5 * 3600)  # wall 再前跳 5h
                s2 = _snap(ROW_A, clock.now())
                s2[0].power_draw_w = 300.0
                e = c.energy_deltas(s2, mono=10.0)
                total = sum(e["GPU-E1"].values())
                # 按 10 秒 × 300W = 0.8333 Wh，而不是 5 小时 500 Wh
                self.assertAlmostEqual(total, 300.0 * 10 / 3600.0, places=9)
                self.assertEqual(len(db.get_gaps(limit=1000)), 0)  # 10s < 15s 阈值
            finally:
                db.close()

    def test_wall_backward_no_negative_interval(self):
        """wall 回拨 1 小时（monotonic 正常 +5 秒）：正常积分、无负间隔、无缺口。"""
        with tempfile.TemporaryDirectory() as td:
            db = Database(Path(td) / "gj2.db", wal=False)
            try:
                cfg = make_config()
                cfg.gpu.poll_interval_seconds = 5.0
                clock = FakeClock(start_wall=T0, start_mono=0.0)
                async def runner(args, timeout):
                    return 0, ROW_A
                c = GpuCollector(cfg, db, runner=runner, clock=clock)

                s1 = _snap(ROW_A, T0)
                s1[0].power_draw_w = 300.0
                c.energy_deltas(s1, mono=0.0)

                clock.advance(5.0)
                clock.set_wall(clock.now() - 3600)  # wall 回拨 1h
                s2 = _snap(ROW_A, clock.now())
                s2[0].power_draw_w = 300.0
                e = c.energy_deltas(s2, mono=5.0)
                total = sum(e["GPU-E1"].values())
                self.assertAlmostEqual(total, 300.0 * 5 / 3600.0, places=9)
                self.assertEqual(len(db.get_gaps(limit=1000)), 0)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
