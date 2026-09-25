"""
1.1.0 Stage B 测试：SystemCollector（psutil 基础遥测 / 速率 / 能耗 / 组件功耗 / 故障隔离）。

覆盖（spec 绑定约束）：
1. 首条 CPU 采样是 warmup：cpu_usage_percent=None，不写数据库（psutil.cpu_percent
   interval=None 首次无有效 delta）；
2. 磁盘/网络速率用累计 counter delta / monotonic 间隔（不用 wall clock）；
   counter reset（curr < prev）-> 该段速率 None（不产生负值/巨值）；
3. 已监测组件功耗 = CPU Package Power + 全部 GPU Power（任一缺失 -> None，不显示假值）；
4. CPU 能耗梯形积分：Δt 用 monotonic；gap 超限（> 3× history_interval）不积分；
5. provider 不可用时 CPU 温度/功耗保持 None（基础系统监控继续，不崩溃）；
6. disabled -> poll_once 不写库、返回 None；
7. 单指标 psutil 抛异常被兜底（其他指标不受影响）。

项目使用标准库 unittest；psutil 用 unittest.mock 打桩。运行：
    python -X utf8 -m unittest discover -s tests
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from clock import FakeClock
from configutil import make_config
from db import Database
from system_collector import SystemCollector, SystemSample, _counter_rate

T0 = 1_700_000_000.0


def _clock(start: float = T0) -> FakeClock:
    return FakeClock(start_wall=start, start_mono=start)


def _make_collector(tmp: Path, clock: FakeClock, enabled: bool = True) -> SystemCollector:
    cfg = make_config()
    cfg.system.enabled = enabled
    cfg.system.history_interval_seconds = 5.0
    db = Database(tmp / "sys.db", wal=False)
    return SystemCollector(cfg, db=db, clock=clock), db


class CounterRateTests(unittest.TestCase):
    def test_first_sample_is_warmup(self):
        self.assertIsNone(_counter_rate(None, 100.0, 5.0))

    def test_normal_rate(self):
        # 5 秒内读了 5000 bytes -> 1000 B/s
        self.assertAlmostEqual(_counter_rate(0.0, 5000.0, 5.0), 1000.0, places=6)

    def test_counter_reset_gives_none(self):
        # curr < prev（接口消失/睡眠唤醒/溢出）-> None（不产生负值）
        self.assertIsNone(_counter_rate(1000.0, 10.0, 5.0))

    def test_zero_dt(self):
        self.assertIsNone(_counter_rate(0.0, 100.0, 0.0))

    def test_huge_rate_gives_none(self):
        # 超过 25 GB/s（_MAX_RATE_BPS）视为 counter reset/接口变化 -> None
        self.assertIsNone(_counter_rate(0.0, 200 * 1024 ** 3, 5.0))
        # 恰在边界内 -> 正常速率
        self.assertEqual(_counter_rate(0.0, 25 * 1024 ** 3, 5.0), 25 * 1024 ** 3 / 5.0)


class PollOnceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.clock = _clock()

    def tearDown(self):
        self._tmp.cleanup()

    def _psutil_stub(self, cpu=0.0, read=0.0, write=0.0, rx=0.0, tx=0.0,
                     mem_total=16 * 1024 ** 3, mem_available=8 * 1024 ** 3):
        """构造一个 psutil 打桩（返回受控值）。"""
        return mock.Mock(
            cpu_percent=lambda interval=None: cpu,
            cpu_freq=lambda: SimpleNamespace(current=None),
            virtual_memory=lambda: SimpleNamespace(
                total=mem_total, available=mem_available,
                percent=(1 - mem_available / mem_total) * 100),
            disk_io_counters=lambda: SimpleNamespace(read_bytes=read, write_bytes=write),
            net_io_counters=lambda: SimpleNamespace(bytes_recv=rx, bytes_sent=tx),
        )

    def _db_rows(self, db):
        return db._connect().execute(
            "SELECT timestamp, cpu_usage_percent, disk_read_bps FROM system_samples"
        ).fetchall()

    def test_warmup_does_not_write_db(self):
        c, db = _make_collector(self.tmp, self.clock)
        with mock.patch.object(c, "_cpu_warmed", False):
            with mock.patch("system_collector.psutil", self._psutil_stub(cpu=12.0)):
                s = c.poll_once()
                self.assertIsNotNone(s)
                # 首条 warmup：cpu_usage 不作为有效值
                self.assertIsNone(s.cpu_usage_percent)
                self.assertEqual(len(self._db_rows(db)), 0)
            # 第二条（已 warmup）：cpu_usage 有效 + 落库
            self.clock.advance(2.0)
            with mock.patch("system_collector.psutil", self._psutil_stub(cpu=34.0)):
                s2 = c.poll_once()
                self.assertAlmostEqual(s2.cpu_usage_percent, 34.0, places=3)
                self.assertEqual(len(self._db_rows(db)), 1)
        db.close()

    def test_disk_rate_uses_monotonic_delta(self):
        c, db = _make_collector(self.tmp, self.clock)
        # 第一轮 warmup（建立基线，read=1000）
        with mock.patch("system_collector.psutil", self._psutil_stub(read=1000.0)):
            c.poll_once()
        # 第二轮：read 增到 6000，monotonic 前进 5 秒 -> 1000 B/s
        self.clock.advance(5.0)
        with mock.patch("system_collector.psutil", self._psutil_stub(read=6000.0)):
            s = c.poll_once()
            self.assertAlmostEqual(s.disk_read_bps, 1000.0, places=4)
        db.close()

    def test_counter_reset_no_negative(self):
        c, db = _make_collector(self.tmp, self.clock)
        with mock.patch("system_collector.psutil", self._psutil_stub(read=5000.0)):
            c.poll_once()
        self.clock.advance(5.0)
        # counter reset：curr < prev
        with mock.patch("system_collector.psutil", self._psutil_stub(read=10.0)):
            s = c.poll_once()
            self.assertIsNone(s.disk_read_bps)
        db.close()

    def test_monitored_component_power(self):
        c, db = _make_collector(self.tmp, self.clock)
        # provider 注入 CPU 温度/功耗；GpuCollector 注入 GPU 合计
        c.set_advanced_sensor_values(
            {"cpu_temperature_c": 60.0, "cpu_package_power_w": 90.0},
            fans=[{"name": "F1", "rpm": 1200, "control_percent": None, "source": "x"}],
            all_sensors=[], available=True)
        c.set_gpu_power_total(150.0)
        c.poll_once()
        self.assertEqual(c.latest.cpu_package_power_w, 90.0)
        self.assertEqual(c.latest.cpu_temperature_c, 60.0)
        # 组件功耗 = 90 + 150 = 240（非墙插）
        self.assertAlmostEqual(c.latest.monitored_component_power_w, 240.0, places=4)
        db.close()

    def test_component_power_none_when_gpu_missing(self):
        c, db = _make_collector(self.tmp, self.clock)
        c.set_advanced_sensor_values(
            {"cpu_temperature_c": 60.0, "cpu_package_power_w": 90.0},
            fans=[], all_sensors=[], available=True)
        c.set_gpu_power_total(None)  # 无 GPU 数据
        c.poll_once()
        # GPU 缺失 -> 组件合计 None（不显示假值，只 CPU 不算整机）
        self.assertIsNone(c.latest.monitored_component_power_w)
        db.close()

    def test_provider_unavailable_keeps_basic(self):
        c, db = _make_collector(self.tmp, self.clock)
        # provider 不可用：CPU 温度/功耗 None，但基础 CPU/内存仍工作
        c.set_advanced_sensor_values(
            {"cpu_temperature_c": None, "cpu_package_power_w": None},
            fans=[], all_sensors=[], available=False)
        c.poll_once()
        self.assertIsNone(c.latest.cpu_temperature_c)
        self.assertIsNone(c.latest.cpu_package_power_w)
        # 内存（psutil 真实读取）可用
        self.assertIsNotNone(c.latest.memory_total_bytes)
        db.close()

    def test_disabled_poll_once_returns_none(self):
        c, db = _make_collector(self.tmp, self.clock, enabled=False)
        self.assertIsNone(c.poll_once())
        db.close()

    def test_psutil_exception_is_swallowed(self):
        c, db = _make_collector(self.tmp, self.clock)
        c._cpu_warmed = True  # 跳过 warmup，直接测单指标异常兜底
        boom = mock.Mock(
            cpu_percent=lambda interval=None: 20.0,
            cpu_freq=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            virtual_memory=lambda: SimpleNamespace(total=1024, available=512, percent=50.0),
            disk_io_counters=lambda: SimpleNamespace(read_bytes=0, write_bytes=0),
            net_io_counters=lambda: SimpleNamespace(bytes_recv=0, bytes_sent=0),
        )
        with mock.patch("system_collector.psutil", boom):
            s = c.poll_once()  # 不抛异常
            self.assertIsNotNone(s)
            self.assertAlmostEqual(s.cpu_usage_percent, 20.0, places=3)
            self.assertIsNone(s.cpu_frequency_mhz)  # cpu_freq 抛异常 -> None
        db.close()


class EnergyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.clock = _clock()

    def tearDown(self):
        self._tmp.cleanup()

    def test_trapezoid_integration(self):
        c, db = _make_collector(self.tmp, self.clock)
        mono = self.clock.monotonic()
        # 上一轮 power=200；本轮（5s 后）power=300
        c._cpu_power_prev = (mono, 200.0)
        c.advanced_cpu_power_w = 300.0
        # 5s <= 3*history_interval(5s)=15s -> 梯形 (200+300)/2 * 5/3600
        delta = c._cpu_energy_delta(mono + 5.0)
        self.assertAlmostEqual(delta, 250.0 * 5.0 / 3600.0, places=9)

    def test_gap_exceeds_limit_not_integrated(self):
        c, db = _make_collector(self.tmp, self.clock)
        mono = self.clock.monotonic()
        c._cpu_power_prev = (mono, 200.0)
        c.advanced_cpu_power_w = 300.0
        # gap = 20s > 3×history_interval(5s)=15s -> 睡眠/断档不积分
        delta = c._cpu_energy_delta(mono + 20.0)
        self.assertEqual(delta, 0.0)
        # 基线仍更新到本轮（避免之后从旧点补算一大段）
        self.assertEqual(c._cpu_power_prev[0], mono + 20.0)

    def test_no_power_no_integration(self):
        c, db = _make_collector(self.tmp, self.clock)
        mono = self.clock.monotonic()
        c._cpu_power_prev = (mono, 200.0)
        c.advanced_cpu_power_w = None
        # power 缺失 -> 该段不积分，且基线清空
        self.assertEqual(c._cpu_energy_delta(mono + 5.0), 0.0)
        self.assertIsNone(c._cpu_power_prev)


if __name__ == "__main__":
    unittest.main()
