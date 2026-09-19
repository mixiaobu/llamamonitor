"""
Phase 9 测试：nvidia-smi CSV 解析 + find_nvidia_smi 发现逻辑。

覆盖：
1. 正常 13 列行 -> 各字段正确解析
2. N/A / Not Supported / [Not Supported] / 空串 -> None（绝不把 'N/A' 传到前端）
3. 多 GPU 多行
4. GPU 名称含逗号（nvidia-smi 输出带引号，csv 模块正确处理）
5. 空输出 -> []
6. 列数不符的行跳过（不影响其他行）
7. uuid 为空行跳过
8. find_nvidia_smi：PATH 命中 / 备用路径 / 都不存在
9. GPU index 乱序/重排：按 UUID 识别身份（解析层不含身份逻辑，
   用两行交换顺序验证解析结果与行序无关）

项目使用标准库 unittest。运行：
    python -m unittest discover -s tests
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gpu_collector import find_nvidia_smi, parse_nvidia_smi_csv, _NVM_COLUMNS

NOW = 1_700_000_000.0

# 正常单 GPU（13 列）
ROW_NORMAL = (
    "0, GPU-abc-123, NVIDIA GeForce RTX 4090, 8192, 24564, "
    "94, 68, 350.5, 71, 2520, 10201, 3, 16"
)
def _make_row(overrides: dict | None = None) -> str:
    """构造 13 列行；overrides 按列序（0~12）覆盖任意字段。"""
    cols = [
        "0",                          # index
        "GPU-test-uuid-0001",         # uuid
        "Test GPU Name",              # name
        "1024",                       # memory.used
        "8192",                       # memory.total
        "55",                         # utilization.gpu
        "45",                         # temperature.gpu
        "120.5",                      # power.draw
        "50",                         # fan.speed
        "1800",                       # clocks.sm
        "9501",                       # clocks.mem
        "3",                          # pcie.link.gen.current
        "16",                         # pcie.link.width.current
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
        self.assertEqual(s.temperature_c, 68.0)
        self.assertEqual(s.power_draw_w, 350.5)
        self.assertEqual(s.fan_percent, 71.0)
        self.assertEqual(s.sm_clock_mhz, 2520.0)
        self.assertEqual(s.memory_clock_mhz, 10201.0)
        self.assertEqual(s.pcie_generation, 3)
        self.assertEqual(s.pcie_width, 16)

    def test_na_values_become_none(self):
        row = _make_row({
            6: "Not Supported",      # temperature
            7: "[N/A]",              # power
            8: "n/a",                # fan
            9: "",                   # sm clock 空串
            11: "[Not Supported]",   # pcie gen
            12: "N/A",               # pcie width
        })
        snaps = parse_nvidia_smi_csv(row, now=NOW)
        s = snaps[0]
        self.assertIsNone(s.temperature_c)
        self.assertIsNone(s.power_draw_w)
        self.assertIsNone(s.fan_percent)
        self.assertIsNone(s.sm_clock_mhz)
        self.assertIsNone(s.pcie_generation)
        self.assertIsNone(s.pcie_width)
        # 其他字段不受影响
        self.assertEqual(s.utilization_percent, 55.0)

    def test_multi_gpu(self):
        text = (
            "0, GPU-u1, GPU One, 100, 2048, 10, 30, 45.0, 10, 1000, 2000, 4, 8\n"
            "1, GPU-u2, GPU Two, 200, 4096, 20, 35, 55.0, 20, 1100, 2200, 4, 8\n"
            "2, GPU-u3, GPU Three, 300, 8192, 30, 40, 65.0, 30, 1200, 2400, 5, 16\n"
        )
        snaps = parse_nvidia_smi_csv(text, now=NOW)
        self.assertEqual([s.uuid for s in snaps], ["GPU-u1", "GPU-u2", "GPU-u3"])
        self.assertEqual(snaps[1].index, 1)
        self.assertEqual(snaps[2].pcie_generation, 5)

    def test_name_with_comma_quoted(self):
        # nvidia-smi 对含逗号名称加引号
        text = '0, GPU-q, "Weird, Name GPU", 100, 2048, 10, 30, 45.0, 10, 1000, 2000, 4, 8'
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
        row = _make_row({7: "12x"})  # power 非法
        snaps = parse_nvidia_smi_csv(row, now=NOW)
        self.assertIsNone(snaps[0].power_draw_w)

    def test_to_row_matches_db_columns(self):
        from db import Database
        import tempfile
        from pathlib import Path

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
