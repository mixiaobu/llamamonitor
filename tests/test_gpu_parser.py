"""
Phase 9 + 1.1.0 测试：nvidia-smi CSV 解析（1.1：20 列 fast query）+ find_nvidia_smi。

1.1.0 列序（NVSMI_QUERY）：
  0 index | 1 uuid | 2 name | 3 memory.used | 4 memory.total |
  5 utilization.gpu | 6 utilization.memory | 7 temperature.gpu | 8 power.draw |
  9 power.limit | 10 fan.speed | 11 clocks.sm | 12 clocks.mem | 13 pstate |
  14 pcie.link.gen.current | 15 pcie.link.width.current | 16 pcie.link.gen.max |
  17 pcie.link.width.max | 18 driver_version | 19 clocks_event_reasons.active

覆盖：
1. 正常 20 列行 -> 全部字段正确解析（含 1.1 高级字段）
2. N/A / Not Supported / [Not Supported] / 空串 -> None（绝不把 'N/A' 传到前端）
3. pstate 'P0' -> 0；N/A -> None（P0 不等同于 100% 性能——前端语义）
4. throttle reasons 位掩码解码：0x0 -> []；已知位 -> 原因名
5. 多 GPU 多行
6. GPU 名称含逗号（nvidia-smi 输出带引号，csv 模块正确处理）
7. 空输出 -> []
8. 列数不符的行跳过（不影响其他行）
9. uuid 为空行跳过
10. find_nvidia_smi：PATH 命中 / 备用路径 / 都不存在
11. GPU index 乱序/重排：按 UUID 识别身份
12. slow health：ECC CSV 解析（不支持 ECC -> enabled None）+ 进程解析（WDDM N/A）
13. to_row 键 == gpu_samples 列（schema v5）

项目使用标准库 unittest。运行：
    python -m unittest discover -s tests
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gpu_collector import (
    find_nvidia_smi,
    parse_nvidia_smi_csv,
    parse_nvidia_smi_ecc_csv,
    parse_nvidia_smi_processes,
    _NVM_COLUMNS,
    _parse_throttle_reasons,
)

NOW = 1_700_000_000.0

# 正常单 GPU（20 列，1.1 完整列）
ROW_NORMAL = (
    "0, GPU-abc-123, NVIDIA GeForce RTX 4090, 8192, 24564, "
    "94, 68, 350.5, 450.0, 505.0, 71, 2520, 10201, P0, 3, 16, 4, 16, 570.00, 0x0000000000000000"
)


def _make_row(overrides: dict | None = None) -> str:
    """构造 20 列行（1.1 列序）；overrides 按列序（0~19）覆盖任意字段。"""
    cols = [
        "0",                          # index
        "GPU-test-uuid-0001",         # uuid
        "Test GPU Name",              # name
        "1024",                       # memory.used
        "8192",                       # memory.total
        "55",                         # utilization.gpu
        "42",                         # utilization.memory（1.1 新）
        "45",                         # temperature.gpu
        "120.5",                      # power.draw
        "350.0",                      # power.limit（1.1 新）
        "50",                         # fan.speed
        "1800",                       # clocks.sm
        "9501",                       # clocks.mem
        "P8",                         # pstate（1.1 新）
        "3",                          # pcie.link.gen.current
        "16",                         # pcie.link.width.current
        "4",                          # pcie.link.gen.max（1.1 新）
        "16",                         # pcie.link.width.max（1.1 新）
        "570.00",                     # driver_version（1.1 新）
        "0x0000000000000000",         # clocks_event_reasons.active（1.1 新）
    ]
    for i, v in (overrides or {}).items():
        cols[i] = v
    assert len(cols) == _NVM_COLUMNS
    return ",".join(cols)


class ParseNvidiaSmiCsvTests(unittest.TestCase):
    def test_normal_row(self):
        snaps = parse_nvidia_smi_csv(ROW_NORMAL, now=NOW)
        self.assertEqual(len(snaps), 1)
        s = snaps[0]
        self.assertEqual(s.timestamp, NOW)
        self.assertEqual(s.index, 0)
        self.assertEqual(s.uuid, "GPU-abc-123")
        self.assertEqual(s.name, "NVIDIA GeForce RTX 4090")
        self.assertEqual(s.memory_used_mb, 8192.0)
        self.assertEqual(s.memory_total_mb, 24564.0)
        self.assertEqual(s.utilization_percent, 94.0)
        # 1.1：显存控制器利用率 与 GPU 利用率 是两个独立字段
        self.assertEqual(s.memory_controller_percent, 68.0)
        self.assertEqual(s.temperature_c, 350.5)
        self.assertEqual(s.power_draw_w, 450.0)
        # 1.1：power limit（列 9，独立于 power.draw）
        self.assertEqual(s.power_limit_w, 505.0)
        self.assertEqual(s.fan_percent, 71.0)
        self.assertEqual(s.sm_clock_mhz, 2520.0)
        self.assertEqual(s.memory_clock_mhz, 10201.0)
        # 1.1：pstate
        self.assertEqual(s.performance_state, 0)
        self.assertEqual(s.pcie_generation, 3)
        self.assertEqual(s.pcie_width, 16)
        # 1.1：PCIe 上限（当前 vs max 是不同信息）
        self.assertEqual(s.pcie_gen_max, 4)
        self.assertEqual(s.pcie_width_max, 16)
        # 1.1：driver version
        self.assertEqual(s.driver_version, "570.00")
        # 1.1：throttle 0x0 -> 空（无性能限制）
        self.assertEqual(s.throttle_reasons, [])

    def test_na_values_become_none(self):
        row = _make_row({
            6: "Not Supported",      # utilization.memory
            7: "[N/A]",              # temperature
            8: "n/a",                # power.draw
            9: "n/a",                # power.limit
            10: "50",                # fan
            11: "",                  # sm clock 空串
            14: "[Not Supported]",   # pcie gen
            15: "N/A",               # pcie width
            16: "N/A",               # pcie gen max
            18: "[N/A]",             # driver version
        })
        snaps = parse_nvidia_smi_csv(row, now=NOW)
        s = snaps[0]
        self.assertIsNone(s.memory_controller_percent)
        self.assertIsNone(s.temperature_c)
        self.assertIsNone(s.power_draw_w)
        self.assertIsNone(s.power_limit_w)
        self.assertIsNone(s.sm_clock_mhz)
        self.assertIsNone(s.pcie_generation)
        self.assertIsNone(s.pcie_width)
        self.assertIsNone(s.pcie_gen_max)
        self.assertIsNone(s.driver_version)
        # 其他字段不受影响
        self.assertEqual(s.utilization_percent, 55.0)
        self.assertEqual(s.fan_percent, 50.0)

    def test_pstate_na_becomes_none(self):
        row = _make_row({13: "N/A"})
        self.assertIsNone(parse_nvidia_smi_csv(row, now=NOW)[0].performance_state)

    def test_throttle_reason_decoding(self):
        # HW Slowdown (bit 3 = 0x8)
        row = _make_row({19: "0x0000000000000008"})
        snaps = parse_nvidia_smi_csv(row, now=NOW)
        self.assertTrue(snaps[0].throttle_reasons)
        # 0x0 -> []
        self.assertEqual(_parse_throttle_reasons("0x0000000000000000"), [])
        self.assertEqual(_parse_throttle_reasons("Not Supported"), [])

    def test_multi_gpu(self):
        text = (
            "0, GPU-u1, GPU One, 100, 2048, 10, 30, 45.0, 100.0, 200.0, 10, 1000, 2000, P8, 4, 8, 4, 16, 566.36, 0\n"
            "1, GPU-u2, GPU Two, 200, 4096, 20, 35, 55.0, 110.0, 210.0, 20, 1100, 2200, P8, 4, 8, 4, 16, 566.36, 0\n"
            "2, GPU-u3, GPU Three, 300, 8192, 30, 40, 65.0, 120.0, 220.0, 30, 1200, 2400, P0, 5, 16, 5, 16, 566.36, 0\n"
        )
        snaps = parse_nvidia_smi_csv(text, now=NOW)
        self.assertEqual([s.uuid for s in snaps], ["GPU-u1", "GPU-u2", "GPU-u3"])
        self.assertEqual(snaps[1].index, 1)
        self.assertEqual(snaps[2].pcie_generation, 5)
        self.assertEqual(snaps[2].performance_state, 0)

    def test_name_with_comma_quoted(self):
        # nvidia-smi 对含逗号名称加引号
        text = ('0, GPU-q, "Weird, Name GPU", 100, 2048, 10, 30, 45.0, 100.0, 200.0, 10, '
                '1000, 2000, P8, 4, 8, 4, 16, 566.36, 0')
        snaps = parse_nvidia_smi_csv(text, now=NOW)
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0].name, "Weird, Name GPU")

    def test_empty_output(self):
        self.assertEqual(parse_nvidia_smi_csv("", now=NOW), [])
        self.assertEqual(parse_nvidia_smi_csv("\n\n", now=NOW), [])

    def test_malformed_rows_skipped(self):
        text = (
            "garbage line without commas\n"
            + _make_row() + "\n"
            "1, GPU-bad, only, five, cols\n"
            + _make_row({1: "GPU-test-uuid-0002"}) + "\n"
        )
        snaps = parse_nvidia_smi_csv(text, now=NOW)
        self.assertEqual([s.uuid for s in snaps], ["GPU-test-uuid-0001", "GPU-test-uuid-0002"])

    def test_old_13_column_rows_are_skipped_not_crash(self):
        # 1.1 前（13 列）的行：列数不符 -> 跳过（不解析错位、不崩溃）
        old = "0, GPU-old, Old GPU, 100, 2048, 10, 30, 45.0, 10, 1000, 2000, 4, 8"
        self.assertEqual(parse_nvidia_smi_csv(old, now=NOW), [])

    def test_empty_uuid_skipped(self):
        text = _make_row({1: ""})
        self.assertEqual(parse_nvidia_smi_csv(text, now=NOW), [])

    def test_index_reorder_identity_by_uuid(self):
        # index 可能随驱动/插拔变化：解析层按行内容给出 uuid，
        # 调用方（collector/db）一律以 uuid 为身份 —— 交换两行后 uuid 与数据仍一一对应
        a = _make_row({0: "0", 1: "GPU-stable-A"})
        b = _make_row({0: "1", 1: "GPU-stable-B"})
        forward = parse_nvidia_smi_csv(a + "\n" + b, now=NOW)
        swapped = parse_nvidia_smi_csv(b + "\n" + a, now=NOW)
        by_uuid = {s.uuid: s for s in swapped}
        self.assertEqual(set(by_uuid), {"GPU-stable-A", "GPU-stable-B"})
        self.assertEqual(by_uuid["GPU-stable-A"].index, forward[0].index)
        self.assertEqual(by_uuid["GPU-stable-B"].index, forward[1].index)

    def test_bad_number_becomes_none(self):
        row = _make_row({8: "12x"})  # power.draw 非法
        snaps = parse_nvidia_smi_csv(row, now=NOW)
        self.assertIsNone(snaps[0].power_draw_w)

    def test_to_row_matches_db_columns(self):
        from db import Database

        tmp = Path(tempfile.mkdtemp())
        d = Database(tmp / "t.db", wal=False)
        try:
            snaps = parse_nvidia_smi_csv(_make_row(), now=NOW)
            row = snaps[0].to_row()
            # to_row 的键必须是 gpu_samples 的列（id 由数据库自增生成）
            table_cols = {r[1] for r in d._connect().execute("PRAGMA table_info(gpu_samples)")}
            self.assertEqual(set(row) | {"id"}, table_cols)
        finally:
            d.close()


class SlowHealthParserTests(unittest.TestCase):
    """1.1 slow health（60s 周期）：ECC CSV + GPU 进程。"""

    def test_ecc_supported_gpu(self):
        # 10 列：uuid, mode, cv.sram, cv.dram, ca.sram, ca.dram, uv.sram, uv.dram, ua.sram, ua.dram
        text = "GPU-1, Enabled, 1, 2, 3, 4, 0, 1, 0, 5\n"
        out = parse_nvidia_smi_ecc_csv(text)
        e = out["GPU-1"]
        self.assertTrue(e["ecc_enabled"])
        # corrected volatile = sram + dram
        self.assertEqual(e["cv_sram"], 1)
        self.assertEqual(e["cv_dram"], 2)

    def test_ecc_not_supported(self):
        # mode = N/A -> 不支持 ECC（UI 整个 ECC 区隐藏，不显示一排 --）
        text = "GPU-c, N/A, N/A, N/A, N/A, N/A, N/A, N/A, N/A, N/A\n"
        out = parse_nvidia_smi_ecc_csv(text)
        self.assertFalse(out["GPU-c"]["ecc_enabled"])

    def test_ecc_partial_na_counts(self):
        # 单个计数 N/A（消费卡无某 SRAM 计数）-> None（不影响其他计数）
        text = "GPU-p, Enabled, N/A, 7, N/A, 8, 0, N/A, 1, N/A\n"
        e = parse_nvidia_smi_ecc_csv(text)["GPU-p"]
        self.assertTrue(e["ecc_enabled"])
        self.assertIsNone(e["cv_sram"])
        self.assertEqual(e["cv_dram"], 7)

    def test_processes_wddm_na_memory(self):
        # WDDM 下 used_memory 常 N/A -> None（UI 显示 --，不影响其他字段）
        text = "1234, C:\\Windows\\explorer.exe, GPU-1, [N/A]\n5678, python.exe, GPU-1, 2048\n"
        procs = parse_nvidia_smi_processes(text)
        self.assertEqual(len(procs), 2)
        self.assertEqual(procs[0]["pid"], 1234)
        self.assertIsNone(procs[0]["used_memory_mb"])
        self.assertEqual(procs[1]["used_memory_mb"], 2048.0)

    def test_processes_empty(self):
        self.assertEqual(parse_nvidia_smi_processes(""), [])


class FindNvidiaSmiTests(unittest.TestCase):
    def test_which_hit(self):
        with mock.patch("gpu_collector.shutil.which", return_value="/fake/bin/nvidia-smi"):
            self.assertEqual(find_nvidia_smi(), Path("/fake/bin/nvidia-smi"))

    def test_fallback_path(self):
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "nvidia-smi.exe"
            fake.write_bytes(b"x")
            with (
                mock.patch("gpu_collector.shutil.which", return_value=None),
                mock.patch("gpu_collector.NVSMI_FALLBACK_PATH", fake),
            ):
                self.assertEqual(find_nvidia_smi(), fake)

    def test_not_found(self):
        with tempfile.TemporaryDirectory() as td:
            with (
                mock.patch("gpu_collector.shutil.which", return_value=None),
                mock.patch("gpu_collector.NVSMI_FALLBACK_PATH", Path(td) / "nope.exe"),
            ):
                self.assertIsNone(find_nvidia_smi())


if __name__ == "__main__":
    unittest.main()
