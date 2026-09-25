"""
1.1.0 Stage B 测试：/api/system/* 端点形状与语义（FastAPI TestClient）。

覆盖（spec 绑定约束）：
1. /api/system/status：null = 不可用（前端 --），**绝不** None->0；
   wall_power_w 恒为 null；monitored_components_w = CPU Package + GPU（缺失则 null）。
2. /api/system/live?minutes：1..1440（越界 -> 422）；采样点透传 null。
3. /api/system/daily?days：avg = sum/count（count=0 -> null，不拿 0 冒充）。
4. /api/system/inventory：静态库存 + manual 刷新。
5. /api/system/sensors：provider state + fans（control_percent 仅 provider 提供）。

项目使用标准库 unittest。运行：
    python -X utf8 -m unittest discover -s tests
"""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from collector import MetricsCollector
from clock import FakeClock
from configutil import make_config
from db import Database
from hardware_sensor_provider import HardwareSensorProvider
from server import build_app
from system_collector import SystemCollector, SystemSample

NOW = 1_700_000_000.0


class SystemApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._clock = FakeClock(start_wall=NOW, start_mono=NOW)
        self._db = Database(self.tmp / "sysapi.db", wal=False)
        cfg = make_config(url="http://127.0.0.1:9")
        # 系统周期采集拉长：测试期间 _system_periodic 不触发，不覆盖预填的 latest
        cfg.system.poll_interval_seconds = 3600.0
        self._collector = MetricsCollector(cfg, self._db)
        self._system = SystemCollector(cfg, db=self._db, clock=self._clock)
        self._sensors = HardwareSensorProvider(cfg, db=self._db, clock=self._clock)
        # 本测试直接 _ingest 注入数据：禁用真实 Bridge 子进程（lifespan 的 start() 不起）
        self._sensors.enabled = False

        # 预填一条最新采样（含缺失字段，验证 null 透传）
        self._system.latest = SystemSample(
            timestamp=NOW,
            cpu_usage_percent=42.0,
            cpu_frequency_mhz=3500.0,
            cpu_temperature_c=None,       # 高级传感器不可用 -> null
            cpu_package_power_w=90.0,
            memory_used_bytes=8 * 1024 ** 3,
            memory_total_bytes=32 * 1024 ** 3,
            memory_usage_percent=25.0,
            disk_read_bps=None,
            disk_write_bps=1024.0,
            network_rx_bps=None,
            network_tx_bps=None,
            monitored_component_power_w=None,  # GPU 缺失 -> null
        )
        self._system.available = True
        self._system.last_update = NOW
        self._system.inventory = {"os": "Windows 11", "cpu_model": "Test CPU",
                                  "logical_cpus": 16, "installed_ram_bytes": 32 * 1024 ** 3}

        # 固定静态库存（避免 lifespan 的 read_inventory 跑真实 CIM/PowerShell —— 慢且值不确定）
        self._fixed_inv = {"os": "Windows 11", "cpu_model": "Test CPU",
                           "logical_cpus": 16, "installed_ram_bytes": 32 * 1024 ** 3}
        self._system.inventory = self._fixed_inv
        self._inv_patch = mock.patch.object(self._system, "read_inventory",
                                            return_value=self._fixed_inv)
        self._inv_patch.start()

        self._app = build_app(self._db, self._collector, system=self._system, sensors=self._sensors)
        self._client = TestClient(self._app)
        self._client.__enter__()

    def tearDown(self):
        self._client.__exit__(None, None, None)
        self._inv_patch.stop()
        self._db.close()
        self._tmp.cleanup()

    def _save_sample(self, row: dict, increments: dict | None = None,
                     now: float | None = None) -> None:
        """经 portal 线程写库（Database 连接绑定创建线程）。"""
        self._client.portal.call(
            self._db.save_system_sample, row, increments or {},
            time.time() if now is None else now, 48 * 3600)

    def test_status_nulls_preserved_and_wall_power_none(self):
        data = self._client.get("/api/system/status").json()
        self.assertTrue(data["available"])
        # CPU 有值
        self.assertEqual(data["cpu"]["usage_percent"], 42.0)
        # 缺失字段保持 null（绝不变 0）
        self.assertIsNone(data["cpu"]["temperature_c"])
        self.assertIsNone(data["disk"]["read_bps"])
        self.assertIsNone(data["network"]["rx_bps"])
        # power：CPU package 有值、GPU 缺失 -> 组件合计 null
        self.assertEqual(data["power"]["cpu_package_w"], 90.0)
        self.assertIsNone(data["power"]["gpu_total_w"])
        self.assertIsNone(data["power"]["monitored_components_w"])
        # wall_power_w 恒 null
        self.assertIsNone(data["power"]["wall_power_w"])
        # memory 有值
        self.assertEqual(data["memory"]["usage_percent"], 25.0)

    def test_status_component_power_when_gpu_present(self):
        self._system.latest.monitored_component_power_w = 240.0
        self._system.gpu_power_total_w = 150.0
        data = self._client.get("/api/system/status").json()
        self.assertEqual(data["power"]["monitored_components_w"], 240.0)
        self.assertEqual(data["power"]["gpu_total_w"], 150.0)

    def test_live_returns_points_and_null_passthrough(self):
        # 写入一条**真实 now** 附近的采样（live 查询窗口以真实 time.time() 为基准；
        # 固定 FakeClock 的 NOW 落在窗口外）
        row = self._system.latest.to_row()
        row["timestamp"] = time.time()
        self._save_sample(row, {"cpu_energy_wh": 0.01})
        data = self._client.get("/api/system/live", params={"minutes": 60}).json()
        self.assertIn("points", data)
        self.assertGreaterEqual(len(data["points"]), 1)
        pt = data["points"][-1]
        # null 透传
        self.assertIsNone(pt["cpu_temperature_c"])
        self.assertIsNone(pt["network_rx_bps"])

    def test_live_minutes_bounds(self):
        # minutes < 1 或 > 1440 -> 422
        self.assertEqual(self._client.get("/api/system/live", params={"minutes": 0}).status_code, 422)
        self.assertEqual(self._client.get("/api/system/live", params={"minutes": 1441}).status_code, 422)
        # 边界合法
        self.assertEqual(self._client.get("/api/system/live", params={"minutes": 1}).status_code, 200)
        self.assertEqual(self._client.get("/api/system/live", params={"minutes": 1440}).status_code, 200)

    def test_daily_avg_null_when_no_samples(self):
        # 本采样 cpu_temperature_c=None（高级传感器不可用）：
        # 该天 cpu_temp_count=0 -> cpu_temp_avg 必须 null（不拿 0 冒充不可用值）；
        # cpu_usage 有值 -> avg 有效。
        self._save_sample(self._system.latest.to_row(), {})
        data = self._client.get("/api/system/daily", params={"days": 7}).json()
        self.assertIn("days", data)
        today = next(d for d in data["days"]
                     if d["date"] == time.strftime("%Y-%m-%d"))
        self.assertEqual(today["cpu_usage_avg"], 42.0)
        self.assertIsNone(today["cpu_temp_avg"])
        # 流量累计列存在（可能为 None）
        self.assertIn("disk_read_bytes", today)
        self.assertIn("cpu_energy_wh", today)

    def test_daily_component_energy_includes_gpu(self):
        """系统每日"已监测组件能耗" = CPU（system_daily 列）+ GPU（gpu_daily 同日求和）。

        回归（1.1 审计发现）：此前 API 直接透传 system_daily 列（只含 CPU 部分），
        本机 GPU 当日 2059Wh 而卡片显示 0 Wh，与 label/tooltip"CPU + GPU"矛盾。
        gpu_daily 只存被监控卡（采集器按 device_uuids 过滤后落库），口径与 GPU 页一致。
        """
        from gpu_collector import GpuSnapshot
        now = time.time()
        today = time.strftime("%Y-%m-%d")
        # GPU 侧：当日两段能量 3.25 + 1.5 = 4.75 Wh（gpu_daily 累计）
        gpu_snap = GpuSnapshot(
            timestamp=now, index=0, uuid="GPU-E1", name="Test GPU",
            utilization_percent=70.0, memory_used_mb=1024.0, memory_total_mb=4096.0,
            temperature_c=50.0, power_draw_w=150.0, fan_percent=40.0,
            sm_clock_mhz=1000.0, memory_clock_mhz=5000.0, pcie_generation=3, pcie_width=16,
        )
        self._client.portal.call(self._db.save_gpu_samples, [gpu_snap],
                                 {"GPU-E1": {today: 3.25}})
        self._client.portal.call(self._db.save_gpu_samples, [gpu_snap],
                                 {"GPU-E1": {today: 1.5}})
        # 系统侧：CPU 能耗 0.75 Wh（collector 对两列传同一 CPU 值，见 system_collector）
        self._save_sample(self._system.latest.to_row(),
                          {"cpu_energy_wh": 0.75, "monitored_component_energy_wh": 0.75})

        data = self._client.get("/api/system/daily", params={"days": 7}).json()
        today_row = next(d for d in data["days"] if d["date"] == today)
        self.assertAlmostEqual(today_row["cpu_energy_wh"], 0.75, places=3)
        # 组件合计 = CPU 0.75 + GPU 4.75 = 5.5
        self.assertAlmostEqual(today_row["monitored_component_energy_wh"], 5.5, places=3)

    def test_daily_component_energy_without_gpu(self):
        """无 GPU 能量时组件合计 = CPU 部分（不引入 0 填充误差；0 + 0 = 0 合法）。"""
        self._save_sample(self._system.latest.to_row(),
                          {"cpu_energy_wh": 0.5, "monitored_component_energy_wh": 0.5})
        data = self._client.get("/api/system/daily", params={"days": 7}).json()
        today = time.strftime("%Y-%m-%d")
        today_row = next(d for d in data["days"] if d["date"] == today)
        self.assertAlmostEqual(today_row["monitored_component_energy_wh"], 0.5, places=3)

    def test_daily_days_bounds(self):
        self.assertEqual(self._client.get("/api/system/daily", params={"days": 0}).status_code, 422)
        self.assertEqual(self._client.get("/api/system/daily", params={"days": 366}).status_code, 422)
        self.assertEqual(self._client.get("/api/system/daily", params={"days": 365}).status_code, 200)

    def test_inventory_static_and_manual(self):
        data = self._client.get("/api/system/inventory").json()
        self.assertTrue(data["available"])
        self.assertEqual(data["inventory"]["os"], "Windows 11")
        self.assertEqual(data["inventory"]["cpu_model"], "Test CPU")
        self.assertIn("refreshed_at", data)

    def test_sensors_state_and_fan_control(self):
        # 注入一组传感器（含一个有 Control 的风扇、一个无 Control 的风扇）
        class _Proc:
            pass
        self._sensors._proc = _Proc()
        self._sensors._ingest({"kind": "sensors", "sensors": [
            {"hardware_type": "cpu", "sensor_type": "temperature",
             "sensor_name": "Package", "hardware_name": "CPU", "index": 0,
             "value": 62.5, "unit": "C"},
            {"hardware_type": "cooling", "sensor_type": "fan",
             "sensor_name": "CPU Fan", "hardware_name": "CPU Fan", "index": 0,
             "value": 1200.0, "unit": "RPM"},
            {"hardware_type": "cooling", "sensor_type": "control",
             "sensor_name": "CPU Fan Speed", "hardware_name": "CPU Fan", "index": 0,
             "value": 55.0, "unit": "%"},
        ]})
        data = self._client.get("/api/system/sensors").json()
        self.assertTrue(data["available"])
        self.assertEqual(data["state"], "available")
        self.assertAlmostEqual(data["cpu_temperature_c"], 62.5, places=4)
        fans = {f["name"]: f for f in data["fans"]}
        self.assertEqual(fans["CPU Fan"]["control_percent"], 55.0)
        self.assertEqual(fans["CPU Fan"]["rpm"], 1200.0)
        self.assertIn("counts", data)

    def test_sensors_unavailable_shape(self):
        # 不注入任何传感器 -> snapshot 返回 unavailable 形状
        data = self._client.get("/api/system/sensors").json()
        self.assertTrue(data["available"])  # provider 对象存在（sensors 非 None）
        self.assertEqual(data["state"], "unavailable")
        self.assertEqual(data["fans"], [])


if __name__ == "__main__":
    unittest.main()
