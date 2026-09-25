"""
Phase 9 测试：SQLite schema migration 机制（PRAGMA user_version）。

覆盖：
1. 全新空库 -> v2：全部表存在（含 gpu_samples / gpu_daily）+ 新列
2. Phase 8 legacy 库（user_version=0 + v1 表 + 历史数据）-> 打开后 v2，
   旧数据原样保留（state / daily_usage / live_samples 行数与值不变）
3. 已 v2 的库再次打开：no-op（版本不变、数据不变）
4. 中断的 migration：首个迁移函数抛错 -> 整体回滚、版本保持 1、
   旧数据完好；重新打开后重试成功 -> v2（幂等）
5. migration 不使用 DROP/DELETE：legacy 数据行内容逐值比对
6. get_schema_version 正确

项目使用标准库 unittest。运行：
    python -m unittest discover -s tests
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db as db_module
from db import (
    CURRENT_SCHEMA_VERSION,
    Database,
    _SCHEMA_V1,
)

LEGACY_DAILY_ROW = (
    "2026-09-15", 100, 20, 30, 5, 3, 1.0, 2.0,
)


def _make_legacy_db(path: Path) -> None:
    """模拟一个 Phase 8 时代的库：v1 schema + 历史数据 + user_version=0。"""
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA_V1)
    conn.execute("PRAGMA user_version = 0")
    conn.execute("INSERT INTO state VALUES('llamacpp:prompt_tokens_total', 12345.0)")
    conn.execute("INSERT INTO state VALUES('llamacpp:tokens_predicted_total', 678.0)")
    conn.execute(
        "INSERT INTO daily_usage VALUES(?,?,?,?,?,?,?,?)", LEGACY_DAILY_ROW
    )
    conn.execute(
        "INSERT INTO live_samples(timestamp, prompt_delta, output_delta, decode_tps) "
        "VALUES(1700000000, 7, 3, 2.5)"
    )
    conn.execute(
        "INSERT INTO mtp_position_daily VALUES('2026-09-15', '0', 42)"
    )
    conn.commit()
    conn.close()


class FreshDatabaseTests(unittest.TestCase):
    def test_fresh_db_is_current_with_all_tables(self):
        with tempfile.TemporaryDirectory() as td:
            d = Database(Path(td) / "fresh.db", wal=False)
            try:
                self.assertEqual(d.get_schema_version(), CURRENT_SCHEMA_VERSION)
                self.assertEqual(d.get_schema_version(), 5)  # 1.1.0
                tables = {
                    r[0]
                    for r in d._connect().execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                for expected in (
                    "state", "daily_usage", "live_samples", "mtp_position_daily",
                    "gpu_samples", "gpu_daily",
                    # v3 可靠性 / 数据质量表
                    "monitor_events", "data_gaps", "backup_history",
                    # v4 runtime metadata 表
                    "app_state",
                    # v5（1.1.0）系统遥测表
                    "system_samples", "system_daily",
                ):
                    self.assertIn(expected, tables)
                # v5 系统遥测表关键列
                sys_sample_cols = {r[1] for r in d._connect().execute("PRAGMA table_info(system_samples)")}
                for col in ("timestamp", "cpu_usage_percent", "cpu_frequency_mhz",
                            "cpu_temperature_c", "cpu_package_power_w", "memory_used_bytes",
                            "memory_total_bytes", "memory_usage_percent", "disk_read_bps",
                            "disk_write_bps", "network_rx_bps", "network_tx_bps",
                            "monitored_component_power_w"):
                    self.assertIn(col, sys_sample_cols)
                sys_daily_cols = {r[1] for r in d._connect().execute("PRAGMA table_info(system_daily)")}
                for col in ("date", "cpu_usage_count", "cpu_usage_sum", "cpu_usage_max",
                            "cpu_energy_wh", "monitored_component_energy_wh",
                            "disk_read_bytes", "disk_write_bytes",
                            "network_rx_bytes", "network_tx_bytes"):
                    self.assertIn(col, sys_daily_cols)
                # v5 gpu_samples 高级遥测列
                gpu_cols = {r[1] for r in d._connect().execute("PRAGMA table_info(gpu_samples)")}
                for col in ("memory_controller_percent", "power_limit_w", "pcie_gen_max",
                            "pcie_width_max", "performance_state", "ecc_enabled",
                            "ecc_corrected_volatile", "ecc_corrected_aggregate",
                            "ecc_uncorrected_volatile", "ecc_uncorrected_aggregate"):
                    self.assertIn(col, gpu_cols)
                # v2 新增列
                live_cols = {r[1] for r in d._connect().execute("PRAGMA table_info(live_samples)")}
                self.assertIn("kv_cache_usage_ratio", live_cols)
                self.assertIn("busy_slots", live_cols)
                daily_cols = {r[1] for r in d._connect().execute("PRAGMA table_info(daily_usage)")}
                self.assertIn("draft_sequences", daily_cols)
                # v3 表的关键列
                gap_cols = {r[1] for r in d._connect().execute("PRAGMA table_info(data_gaps)")}
                for col in ("start_timestamp", "end_timestamp", "duration_seconds",
                            "source", "reason", "token_recoverable", "possible_token_loss",
                            "resolved"):
                    self.assertIn(col, gap_cols)
                event_cols = {r[1] for r in d._connect().execute("PRAGMA table_info(monitor_events)")}
                for col in ("timestamp", "event_type", "severity", "source", "details_json"):
                    self.assertIn(col, event_cols)
            finally:
                d.close()


class LegacyMigrationTests(unittest.TestCase):
    def test_legacy_v0_migrates_to_current_data_intact(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "legacy.db"
            _make_legacy_db(p)

            d = Database(p, wal=False)
            try:
                self.assertEqual(d.get_schema_version(), 5)  # 1.1.0：v0 -> v5
                conn = d._connect()
                # state 原样保留（baseline 不能丢）
                state = {
                    r[0]: r[1]
                    for r in conn.execute("SELECT metric_name, value FROM state")
                }
                self.assertEqual(
                    state,
                    {
                        "llamacpp:prompt_tokens_total": 12345.0,
                        "llamacpp:tokens_predicted_total": 678.0,
                    },
                )
                # daily_usage 逐值保留（旧 8 列 + 新列默认 0）
                row = conn.execute(
                    "SELECT date, prompt_tokens, cached_tokens, output_tokens, "
                    "draft_tokens, accepted_tokens, prompt_seconds, predicted_seconds, "
                    "draft_sequences FROM daily_usage"
                ).fetchone()
                self.assertEqual(tuple(row[:8]), LEGACY_DAILY_ROW)
                self.assertEqual(row[8], 0)
                # live_samples 保留
                live = conn.execute(
                    "SELECT timestamp, prompt_delta, output_delta, decode_tps, "
                    "kv_cache_usage_ratio, busy_slots FROM live_samples"
                ).fetchone()
                self.assertEqual(tuple(live[:4]), (1700000000, 7, 3, 2.5))
                self.assertIsNone(live[4])
                self.assertIsNone(live[5])
                # mtp_position_daily 保留
                pos = conn.execute(
                    "SELECT date, position, accepted_tokens FROM mtp_position_daily"
                ).fetchone()
                self.assertEqual(tuple(pos), ("2026-09-15", "0", 42))
                # GPU 表存在且为空
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM gpu_samples").fetchone()[0], 0
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM gpu_daily").fetchone()[0], 0
                )
            finally:
                d.close()

    def test_open_v2_db_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "legacy.db"
            _make_legacy_db(p)
            d1 = Database(p, wal=False)
            d1.get_schema_version()  # 触发 _connect -> migration（连接是惰性的）
            d1.close()

            before = _snapshot(p)
            d2 = Database(p, wal=False)
            try:
                self.assertEqual(d2.get_schema_version(), 5)
                # no-op：不再插入额外 migration 事件（v0 -> v5 共 3 条：2->3/3->4/4->5；1->2 不记事件）
                self.assertEqual(
                    d2._connect().execute(
                        "SELECT COUNT(*) FROM monitor_events WHERE event_type='migration'"
                    ).fetchone()[0], 3,
                )
                self.assertEqual(_snapshot(p), before)
            finally:
                d2.close()

    def test_interrupted_migration_rolls_back_and_retries(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "legacy.db"
            _make_legacy_db(p)

            def boom(conn):
                raise RuntimeError("simulated crash mid-migration")

            with mock.patch.dict(db_module._MIGRATIONS, {1: boom}):
                d1 = Database(p, wal=False)
                with self.assertRaises(RuntimeError):
                    d1._connect()
                # 第一次连接已关闭前版本应保持 1（v1 已标记、v2 未完成）
                try:
                    d1.close()
                except Exception:
                    pass
            conn = sqlite3.connect(p)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
            # GPU 表被回滚（executescript 在失败事务内）
            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            self.assertNotIn("gpu_daily", tables)
            # 旧数据完好
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM state").fetchone()[0], 2
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM daily_usage").fetchone()[0], 1
            )
            conn.close()

            # 重新打开：重试成功（v1 -> v2 -> v3 -> v4 -> v5）
            d2 = Database(p, wal=False)
            try:
                self.assertEqual(d2.get_schema_version(), 5)
                self.assertEqual(
                    d2._connect().execute("SELECT COUNT(*) FROM state").fetchone()[0], 2
                )
            finally:
                d2.close()


def _snapshot(p: Path) -> dict:
    """
    抓库内容快照（no-op 比对用）。

    只比较 v1 原有列：migration 会新增列（live_samples 等），
    SELECT * 的元组长度在 v1/v2 间本来就会不同。
    """
    conn = sqlite3.connect(p)
    try:
        out = {
            "state": conn.execute(
                "SELECT metric_name, value FROM state ORDER BY 1"
            ).fetchall(),
            "daily": conn.execute(
                "SELECT date, prompt_tokens, cached_tokens, output_tokens, "
                "draft_tokens, accepted_tokens, prompt_seconds, predicted_seconds "
                "FROM daily_usage ORDER BY 1"
            ).fetchall(),
            "live": conn.execute(
                "SELECT id, timestamp, prompt_delta, cached_delta, output_delta, "
                "prompt_tps, decode_tps, requests_processing, requests_deferred, "
                "context_max, mtp_accept_rate FROM live_samples ORDER BY 1"
            ).fetchall(),
            "mtp": conn.execute(
                "SELECT date, position, accepted_tokens FROM mtp_position_daily ORDER BY 1, 2"
            ).fetchall(),
        }
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "gpu_samples" in tables:
            out["gpu_samples_n"] = conn.execute("SELECT COUNT(*) FROM gpu_samples").fetchone()[0]
        if "gpu_daily" in tables:
            out["gpu_daily_n"] = conn.execute("SELECT COUNT(*) FROM gpu_daily").fetchone()[0]
        return out
    finally:
        conn.close()


def _make_v2_db(path: Path) -> None:
    """AUDIT-DB-008 fixture：v2 库（v1 + gpu 表 + v2 新列）带真实数据，user_version=2。"""
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA_V1)
    conn.execute("ALTER TABLE live_samples ADD COLUMN kv_cache_usage_ratio REAL")
    conn.execute("ALTER TABLE live_samples ADD COLUMN busy_slots INTEGER")
    conn.execute("ALTER TABLE daily_usage ADD COLUMN draft_sequences INTEGER NOT NULL DEFAULT 0")
    conn.executescript(db_module._GPU_TABLES)
    conn.execute("PRAGMA user_version = 2")
    conn.execute("INSERT INTO state VALUES('llamacpp:prompt_tokens_total', 99999.0)")
    conn.execute(
        "INSERT INTO daily_usage(date, prompt_tokens, cached_tokens, output_tokens, "
        "draft_tokens, accepted_tokens, prompt_seconds, predicted_seconds, draft_sequences) "
        "VALUES('2026-08-01', 10, 2, 3, 0, 0, 0.5, 0.2, 0)"
    )
    conn.execute(
        "INSERT INTO gpu_samples(timestamp, gpu_uuid, gpu_index, gpu_name, utilization_percent, "
        "memory_used_mb, temperature_c, power_draw_w) "
        "VALUES(1700000100, 'GPU-TEST-UUID', 0, 'Test GPU', 77.0, 1234.0, 61.0, 210.0)"
    )
    conn.execute(
        "INSERT INTO gpu_daily(date, gpu_uuid, gpu_name, sample_count, "
        "utilization_count, utilization_sum, utilization_max, energy_wh) "
        "VALUES('2026-08-01', 'GPU-TEST-UUID', 'Test GPU', 5, 5, 385.0, 77.0, 0.5)"
    )
    conn.commit()
    conn.close()


def _make_v3_db(path: Path) -> None:
    """AUDIT-DB-008 fixture：v3 库（v2 + 可靠性表）带真实数据，user_version=3。"""
    _make_v2_db(path)
    conn = sqlite3.connect(path)
    conn.executescript(db_module._SCHEMA_V3)
    conn.execute("PRAGMA user_version = 3")
    conn.execute(
        "INSERT INTO monitor_events(timestamp, event_type, severity, source, details_json) "
        "VALUES(1700000000, 'counter_reset', 'warning', 'collector', '{}')"
    )
    conn.execute(
        "INSERT INTO data_gaps(start_timestamp, end_timestamp, duration_seconds, "
        "source, reason, token_recoverable, possible_token_loss, resolved) "
        "VALUES(1700000000, 1700000060, 60.0, 'llama', 'server_offline', 1, 0, 1)"
    )
    conn.execute(
        "INSERT INTO backup_history(timestamp, type, path, size, verified, success) "
        "VALUES(1700000000, 'automatic', 'auto_monitor_x.db', 123, 1, 1)"
    )
    conn.commit()
    conn.close()


class V2V3MigrationFixtureTests(unittest.TestCase):
    """AUDIT-DB-008：迁移矩阵原只有 fresh + legacy v0 fixture；
    v2->v3 / v3->v4 的数据保留之前只由代码审查证明。本测试补上带真实数据的
    逐级迁移回归网（未来在这两步加数据转换逻辑时立即有测试保护）。"""

    def test_v2_db_with_data_migrates_to_v4_intact(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "v2.db"
            _make_v2_db(p)
            d = Database(p, wal=False)
            try:
                self.assertEqual(d.get_schema_version(), CURRENT_SCHEMA_VERSION)
                conn = d._connect()
                # v1 数据原样保留
                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM state WHERE metric_name='llamacpp:prompt_tokens_total'"
                    ).fetchone()[0], 99999.0)
                self.assertEqual(
                    tuple(conn.execute(
                        "SELECT prompt_tokens, cached_tokens, draft_sequences "
                        "FROM daily_usage WHERE date='2026-08-01'").fetchone()),
                    (10, 2, 0))
                # v2 数据原样保留
                self.assertEqual(
                    tuple(conn.execute(
                        "SELECT utilization_percent, memory_used_mb, power_draw_w "
                        "FROM gpu_samples WHERE gpu_uuid='GPU-TEST-UUID'").fetchone()),
                    (77.0, 1234.0, 210.0))
                self.assertEqual(
                    tuple(conn.execute(
                        "SELECT sample_count, utilization_max, energy_wh "
                        "FROM gpu_daily WHERE gpu_uuid='GPU-TEST-UUID'").fetchone()),
                    (5, 77.0, 0.5))
                # v3/v4 结构就位
                self.assertIsNotNone(conn.execute(
                    "SELECT name FROM sqlite_master WHERE name='app_state'").fetchone())
                self.assertGreaterEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM monitor_events WHERE event_type='migration'"
                    ).fetchone()[0], 2)
            finally:
                d.close()
            # 重开 = no-op（版本不变、无新 migration 事件）
            d2 = Database(p, wal=False)
            try:
                self.assertEqual(d2.get_schema_version(), CURRENT_SCHEMA_VERSION)
            finally:
                d2.close()

    def test_v3_db_with_data_migrates_to_v4_intact(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "v3.db"
            _make_v3_db(p)
            d = Database(p, wal=False)
            try:
                self.assertEqual(d.get_schema_version(), CURRENT_SCHEMA_VERSION)
                conn = d._connect()
                # v3 数据原样保留
                self.assertEqual(
                    tuple(conn.execute(
                        "SELECT timestamp, event_type, severity FROM monitor_events"
                    ).fetchone()),
                    (1700000000, "counter_reset", "warning"))
                self.assertEqual(
                    tuple(conn.execute(
                        "SELECT duration_seconds, reason, resolved FROM data_gaps"
                    ).fetchone()),
                    (60.0, "server_offline", 1))
                self.assertEqual(
                    tuple(conn.execute(
                        "SELECT type, size, verified FROM backup_history"
                    ).fetchone()),
                    ("automatic", 123, 1))
                # v2 数据也还在（v3 库由 v2 升级而来）
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM gpu_samples").fetchone()[0], 1)
            finally:
                d.close()


if __name__ == "__main__":
    unittest.main()
