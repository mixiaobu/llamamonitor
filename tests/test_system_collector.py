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
            cpu_percent=lambda interval=None, percpu=False: ([cpu, cpu] if percpu else cpu),
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

    def test_component_power_partial_source(self):
        c, db = _make_collector(self.tmp, self.clock)
        c.set_advanced_sensor_values(
            {"cpu_temperature_c": 60.0, "cpu_package_power_w": 90.0},
            fans=[], all_sensors=[], available=True)
        c.set_gpu_power_total(None)  # 无 GPU 数据
        c.poll_once()
        # 1.1.4 精修 §28/§29：GPU 缺失不参与求和，CPU 90W 是真实值 -> 90（不是 None）
        self.assertAlmostEqual(c.latest.monitored_component_power_w, 90.0, places=4)
        db.close()

    def test_component_power_all_null(self):
        c, db = _make_collector(self.tmp, self.clock)
        c.set_advanced_sensor_values(
            {"cpu_temperature_c": None, "cpu_package_power_w": None},
            fans=[], all_sensors=[], available=True)
        c.set_gpu_power_total(None)
        c.poll_once()
        # 全缺失 -> None（UI 显示 --；真实 0 不会变 null）
        self.assertIsNone(c.latest.monitored_component_power_w)
        db.close()

    def test_component_power_real_zero(self):
        c, db = _make_collector(self.tmp, self.clock)
        c.set_advanced_sensor_values(
            {"cpu_temperature_c": None, "cpu_package_power_w": 0.0},
            fans=[], all_sensors=[], available=True)
        c.set_gpu_power_total(None)
        c.poll_once()
        # §29：真实 0 是有效读数，求和 = 0（不是 None）
        self.assertEqual(c.latest.monitored_component_power_w, 0.0)
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

    def test_cpu_usage_is_per_core_mean(self):
        """Round-3 §6/§10/§57-§58：整机 CPU 利用率 = 逐逻辑核均值（双路 Xeon 上
        psutil.cpu_percent(None) 只读 group 0，实测 ~2× per-core mean 的根因）。
        这里 4 核：3 个 50% + 1 个 0% -> mean = 37.5（不是聚合 50*3/4 也不是 group0）。"""
        c, db = _make_collector(self.tmp, self.clock)
        c._cpu_warmed = True
        c.inventory = {"cpu_base_frequency_mhz": 2700.0}
        per_core = [50.0, 50.0, 50.0, 0.0]
        raw = (50.0, per_core,
               SimpleNamespace(current=2700.0),
               SimpleNamespace(total=16 * 1024 ** 3, available=8 * 1024 ** 3, percent=50.0),
               None, SimpleNamespace(bytes_recv=0, bytes_sent=0), None)
        s = SystemSample(timestamp=self.clock.now())
        c._apply_sample(s, raw, self.clock.monotonic())
        self.assertAlmostEqual(s.cpu_usage_percent, 37.5, places=4)
        self.assertEqual(s.cpu_per_core_percent, per_core)
        # 基准频率从库存型号解析值传播
        self.assertEqual(s.cpu_base_frequency_mhz, 2700.0)
        db.close()

    def test_cpu_usage_fallback_to_aggregate_when_no_percore(self):
        """per-core 缺失（单路 / 无 percpu）时回退到聚合值（两者在单路机一致）。"""
        c, db = _make_collector(self.tmp, self.clock)
        c._cpu_warmed = True
        raw = (12.0, None,
               SimpleNamespace(current=None),
               SimpleNamespace(total=1024, available=512, percent=50.0),
               None, SimpleNamespace(bytes_recv=0, bytes_sent=0), None)
        s = SystemSample(timestamp=self.clock.now())
        c._apply_sample(s, raw, self.clock.monotonic())
        self.assertAlmostEqual(s.cpu_usage_percent, 12.0, places=4)
        self.assertIsNone(s.cpu_per_core_percent)
        db.close()

    def test_psutil_exception_is_swallowed(self):
        c, db = _make_collector(self.tmp, self.clock)
        c._cpu_warmed = True  # 跳过 warmup，直接测单指标异常兜底
        boom = mock.Mock(
            cpu_percent=lambda interval=None, percpu=False: ([20.0, 20.0] if percpu else 20.0),
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
        # 上一轮 power=200；本轮（5s 后）power=300（同一天，wall=mono 同步）
        c._cpu_power_prev = (mono, mono, 200.0)
        c.advanced_cpu_power_w = 300.0
        # 5s <= 3*history_interval(5s)=15s -> 梯形 (200+300)/2 * 5/3600
        out = c._cpu_energy_delta(mono + 5.0, mono + 5.0)
        self.assertAlmostEqual(sum(out.values()), 250.0 * 5.0 / 3600.0, places=9)

    def test_gap_exceeds_limit_not_integrated(self):
        c, db = _make_collector(self.tmp, self.clock)
        mono = self.clock.monotonic()
        c._cpu_power_prev = (mono, mono, 200.0)
        c.advanced_cpu_power_w = 300.0
        # gap = 20s > 3×history_interval(5s)=15s -> 睡眠/断档不积分
        out = c._cpu_energy_delta(mono + 20.0, mono + 20.0)
        self.assertEqual(out, {})
        # 基线仍更新到本轮（避免之后从旧点补算一大段）
        self.assertEqual(c._cpu_power_prev[0], mono + 20.0)

    def test_no_power_no_integration(self):
        c, db = _make_collector(self.tmp, self.clock)
        mono = self.clock.monotonic()
        c._cpu_power_prev = (mono, mono, 200.0)
        c.advanced_cpu_power_w = None
        # power 缺失 -> 该段不积分，且基线清空
        self.assertEqual(c._cpu_energy_delta(mono + 5.0, mono + 5.0), {})
        self.assertIsNone(c._cpu_power_prev)

    def test_midnight_split(self):
        """
        AUDIT-1.1.1 DATA-1111-006 回归：跨午夜的 CPU 能耗段必须按本机午夜边界
        分割到两个自然日（前一天少记的 bug），而不是整段归当天。
        """
        from datetime import datetime, timedelta
        from db import local_date
        c, db = _make_collector(self.tmp, self.clock)
        # 构造：本机 23:59:55 -> 次日 00:00:05（10s 段，跨午夜；gap=10s <= 15s 上限）
        prev_wall = (datetime.now().replace(hour=23, minute=59, second=55, microsecond=0)).timestamp()
        curr_wall = (datetime.now().replace(hour=0, minute=0, second=5, microsecond=0)
                     + timedelta(days=1)).timestamp()
        prev_mono = curr_wall - 10.0  # monotonic 间隔 = wall 间隔 = 10s
        c._cpu_power_prev = (prev_mono, prev_wall, 200.0)
        c.advanced_cpu_power_w = 300.0
        out = c._cpu_energy_delta(curr_wall, curr_wall)
        prev_date = local_date(prev_wall)
        curr_date = local_date(curr_wall)
        self.assertNotEqual(prev_date, curr_date)  # 确实跨了日
        # 5s 在前一天（23:59:55->00:00:00）、5s 在当天（00:00:00->00:00:05）
        # 梯形均值 (200+300)/2=250W；按时间比例各半
        expected_total = 250.0 * 10.0 / 3600.0
        self.assertAlmostEqual(sum(out.values()), expected_total, places=9)
        self.assertAlmostEqual(out.get(prev_date, 0.0), expected_total * 0.5, places=9)
        self.assertAlmostEqual(out.get(curr_date, 0.0), expected_total * 0.5, places=9)
        db.close()

    def test_midnight_split_db_attribution(self):
        """
        AUDIT-1.1.1 DATA-1111-006 端到端：跨午夜段落库后，前一天与当天
        system_daily.cpu_energy_wh 各记其半（而不是全部归当天）。
        """
        from datetime import datetime, timedelta
        from db import local_date
        c, db = _make_collector(self.tmp, self.clock)
        prev_wall = (datetime.now().replace(hour=23, minute=59, second=55, microsecond=0)).timestamp()
        curr_wall = (datetime.now().replace(hour=0, minute=0, second=5, microsecond=0)
                     + timedelta(days=1)).timestamp()
        prev_mono = curr_wall - 10.0
        c._cpu_power_prev = (prev_mono, prev_wall, 200.0)
        c.advanced_cpu_power_w = 300.0
        c._cpu_warmed = True
        out = c._cpu_energy_delta(curr_wall, curr_wall)
        # 直接以 per-date 增量落库（模拟 _finish_sample 的落库路径）
        db.save_system_sample(
            SystemSample(timestamp=curr_wall, cpu_usage_percent=10.0).to_row(),
            cpu_energy_wh_by_date=out,
            now=curr_wall,
        )
        prev_date = local_date(prev_wall)
        curr_date = local_date(curr_wall)
        conn = db._connect()
        rows = {r["date"]: r["cpu_energy_wh"] for r in conn.execute(
            "SELECT date, cpu_energy_wh FROM system_daily WHERE date IN (?,?)",
            (prev_date, curr_date)).fetchall()}
        expected_total = 250.0 * 10.0 / 3600.0
        self.assertAlmostEqual(rows.get(prev_date, 0.0), expected_total * 0.5, places=6)
        self.assertAlmostEqual(rows.get(curr_date, 0.0), expected_total * 0.5, places=6)
        db.close()


class ModuleHelperTests(unittest.TestCase):
    """Round-3 系统页 §157-§168 库存 helper（纯函数，无 I/O 依赖时用固定输入）。"""

    def test_clean_cpu_model(self):
        from system_collector import _clean_cpu_model
        self.assertEqual(_clean_cpu_model("Intel(R) Xeon(R) Platinum 8168 CPU @ 2.70GHz"),
                         "Intel Xeon Platinum 8168")
        self.assertIsNone(_clean_cpu_model(None))
        # 无 "@ x.xGHz"/"CPU"/"(R)" 时原样返回（视觉清理不误删）
        self.assertEqual(_clean_cpu_model("AMD Ryzen 9 5950X 16-Core Processor"),
                         "AMD Ryzen 9 5950X 16-Core Processor")
        # 只去 (R) 商标
        self.assertEqual(_clean_cpu_model("Intel(R) Core(TM) i7-12700K"),
                         "Intel Core(TM) i7-12700K")

    def test_parse_base_freq_mhz(self):
        from system_collector import _parse_base_freq_mhz
        self.assertEqual(_parse_base_freq_mhz("Intel(R) Xeon(R) Platinum 8168 CPU @ 2.70GHz"), 2700.0)
        self.assertEqual(_parse_base_freq_mhz("AMD Ryzen 5 3600 @ 3.6 GHz"), 3600.0)
        self.assertEqual(_parse_base_freq_mhz("CPU @ 3500 MHz"), 3500.0)
        self.assertIsNone(_parse_base_freq_mhz("No freq here"))
        self.assertIsNone(_parse_base_freq_mhz(None))

    def test_is_virtual_if(self):
        from system_collector import _is_virtual_if
        self.assertTrue(_is_virtual_if("WireGuard 000005"))
        self.assertTrue(_is_virtual_if("000005"))
        self.assertTrue(_is_virtual_if("vEthernet (Default Switch)"))
        self.assertTrue(_is_virtual_if("Loopback Pseudo-Interface 1"))
        self.assertFalse(_is_virtual_if("WLAN"))
        self.assertFalse(_is_virtual_if("以太网"))

    def test_if_kind(self):
        from system_collector import _if_kind
        self.assertEqual(_if_kind("WLAN", None), "Wi-Fi")
        self.assertEqual(_if_kind("以太网", None), "以太网")
        self.assertEqual(_if_kind("000005", None), "虚拟/VPN")


if __name__ == "__main__":
    unittest.main()
