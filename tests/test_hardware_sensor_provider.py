"""
1.1.0 Stage C 测试：HardwareSensorProvider（LibreHardwareMonitor Bridge 生命周期 /
状态机 / 快照新鲜度 / 风扇配对 / 故障隔离）。

覆盖（spec 绑定约束）：
1. 状态机：有新鲜传感器数据 -> available；Bridge 活但无/过期数据 -> partial；
   无 Bridge -> unavailable。状态转换才写事件（provider_unavailable / recovered）。
2. 快照新鲜度：传感器值 > _SENSOR_STALE_SECONDS（30s）视为过期 -> cpu 温度/功耗 None、
   fans/sensors 清空（不返回陈旧读数）。
3. 风扇配对：RPM 与 Control 必须按 hardware_name 配对；**只有** provider 提供
   Control 传感器时才填 control_percent（绝不从 RPM 推算）；0 RPM 不是错误。
4. CPU 温度/功耗：按 (hardware_type=cpu, sensor_type=temperature/power) 取第一个
   新鲜值；找不到 -> None（绝不猜）。
5. 分类计数 cpu/motherboard/cooling/storage 正确累计。
6. 故障隔离：provider 不可用时 SystemCollector 基础遥测仍工作（本模块不抛异常）。

项目使用标准库 unittest；不拉起真实子进程（直接 _ingest 注入 + FakeClock）。运行：
    python -X utf8 -m unittest discover -s tests
"""

import tempfile
import unittest
from pathlib import Path

from clock import FakeClock
from configutil import make_config
from db import Database
from hardware_sensor_provider import HardwareSensorProvider, _SENSOR_STALE_SECONDS

T0 = 1_700_000_000.0


def _sensor(hw_type, sensor_type, name, value, unit=None, hw_name=None):
    return {
        "hardware_type": hw_type,
        "sensor_type": sensor_type,
        "sensor_name": name,
        "hardware_name": hw_name or name,
        "index": 0,
        "value": value,
        "unit": unit,
    }


def _make_provider(tmp: Path, clock: FakeClock, enabled: bool = True,
                   advanced: bool = True) -> tuple[HardwareSensorProvider, Database]:
    cfg = make_config()
    cfg.system.advanced_sensors = advanced
    db = Database(tmp / "hs.db", wal=False)
    p = HardwareSensorProvider(cfg, db=db, clock=clock)
    # 模拟 Bridge 进程在跑（供 snapshot 判 partial/available；非真实 Popen）
    class _Proc:
        pass
    p._proc = _Proc()
    return p, db


class IngestAndSnapshotTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.clock = FakeClock(start_wall=T0, start_mono=T0)

    def tearDown(self):
        # 各测试自行关闭 db
        self._tmp.cleanup()

    def test_available_state_and_cpu_values(self):
        p, db = _make_provider(self.tmp, self.clock)
        try:
            p._ingest({"kind": "sensors", "sensors": [
                _sensor("cpu", "temperature", "Package Temperature", 62.5, "C"),
                _sensor("cpu", "power", "Package Power", 95.0, "W"),
                _sensor("cpu", "load", "Core 0", 12.0, "%"),
            ]})
            snap = p.snapshot()
            self.assertEqual(snap["state"], "available")
            self.assertAlmostEqual(snap["cpu_temperature_c"], 62.5, places=4)
            self.assertAlmostEqual(snap["cpu_package_power_w"], 95.0, places=4)
            self.assertGreaterEqual(snap["counts"]["cpu"], 2)
        finally:
            db.close()

    def test_stale_data_becomes_none(self):
        p, db = _make_provider(self.tmp, self.clock)
        try:
            p._ingest({"kind": "sensors", "sensors": [
                _sensor("cpu", "temperature", "Package Temperature", 62.5, "C"),
            ]})
            self.assertEqual(p.snapshot()["state"], "available")
            # 时钟前进超过新鲜度阈值 -> 数据过期
            self.clock.advance(_SENSOR_STALE_SECONDS + 5)
            snap = p.snapshot()
            self.assertIsNone(snap["cpu_temperature_c"])
            self.assertIsNone(snap["cpu_package_power_w"])
            self.assertEqual(snap["fans"], [])
            self.assertEqual(snap["sensors"], [])
            # Bridge 仍在 -> partial（不是 unavailable）
            self.assertEqual(snap["state"], "partial")
        finally:
            db.close()

    def test_no_data_is_partial_not_available(self):
        p, db = _make_provider(self.tmp, self.clock)
        try:
            # 空传感器集合（Bridge 活着但还没输出）
            p._ingest({"kind": "sensors", "sensors": []})
            snap = p.snapshot()
            self.assertEqual(snap["state"], "partial")
            self.assertIsNone(snap["cpu_temperature_c"])
        finally:
            db.close()

    def test_no_bridge_is_unavailable(self):
        p, db = _make_provider(self.tmp, self.clock)
        try:
            p._proc = None  # Bridge 不在
            p._ingest({"kind": "sensors", "sensors": [
                _sensor("cpu", "temperature", "Package Temperature", 62.5, "C"),
            ]})
            self.clock.advance(_SENSOR_STALE_SECONDS + 5)
            snap = p.snapshot()
            self.assertEqual(snap["state"], "unavailable")
        finally:
            db.close()


class FanPairingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.clock = FakeClock(start_wall=T0, start_mono=T0)

    def tearDown(self):
        self._tmp.cleanup()

    def test_fan_control_only_when_provider_supplies_it(self):
        p, db = _make_provider(self.tmp, self.clock)
        try:
            p._ingest({"kind": "sensors", "sensors": [
                # 有 Control 的风扇
                _sensor("Cooling", "fan", "CPU Fan", 1200.0, "RPM", hw_name="CPU Fan"),
                _sensor("Cooling", "control", "CPU Fan Speed", 55.0, "%", hw_name="CPU Fan"),
                # 只有 RPM 的风扇（无 Control）
                _sensor("Cooling", "fan", "Case Fan", 0.0, "RPM", hw_name="Case Fan"),
            ]})
            fans = {f["name"]: f for f in p.snapshot()["fans"]}
            # 有 Control -> 填 control_percent
            self.assertEqual(fans["CPU Fan"]["rpm"], 1200.0)
            self.assertEqual(fans["CPU Fan"]["control_percent"], 55.0)
            # 无 Control -> control_percent 保持 None（绝不从 RPM 推算）
            self.assertIsNone(fans["Case Fan"]["control_percent"])
            # 0 RPM 不是错误（正常展示）
            self.assertEqual(fans["Case Fan"]["rpm"], 0.0)
        finally:
            db.close()

    def test_counts_by_category(self):
        p, db = _make_provider(self.tmp, self.clock)
        try:
            p._ingest({"kind": "sensors", "sensors": [
                _sensor("CPU", "temperature", "T", 50.0, "°C"),
                _sensor("Motherboard", "voltage", "Vcore", 1.25, "V"),
                _sensor("Storage", "temperature", "SSD Temp", 40.0, "°C"),
                _sensor("Cooling", "fan", "Fan", 900.0, "RPM"),
            ]})
            counts = p.snapshot()["counts"]
            self.assertEqual(counts["cpu"], 1)
            self.assertEqual(counts["motherboard"], 1)
            self.assertEqual(counts["storage"], 1)
            self.assertEqual(counts["cooling"], 1)
        finally:
            db.close()


class StateTransitionEventTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.clock = FakeClock(start_wall=T0, start_mono=T0)

    def tearDown(self):
        self._tmp.cleanup()

    def _events(self, db):
        return [r["event_type"] for r in db.get_events(limit=100)]

    def test_recovery_and_unavailable_events(self):
        p, db = _make_provider(self.tmp, self.clock)
        try:
            # 首次置位（_last_state=None -> unavailable 不记事件）
            p._proc = None
            p.snapshot()
            # Bridge 恢复 + 有新鲜数据 -> available（记 recovered 事件）
            class _Proc:
                pass
            p._proc = _Proc()
            p._ingest({"kind": "sensors", "sensors": [
                _sensor("cpu", "temperature", "T", 50.0, "C"),
            ]})
            p.snapshot()
            self.assertIn("hardware_sensor_provider_recovered", self._events(db))
            # Bridge 消失且数据过期 -> unavailable（记 unavailable 事件）
            p._proc = None
            self.clock.advance(_SENSOR_STALE_SECONDS + 5)
            p.snapshot()
            self.assertIn("hardware_sensor_provider_unavailable", self._events(db))
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
