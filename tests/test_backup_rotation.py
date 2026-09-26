"""
Phase 11 测试：备份 —— 创建/验证/轮转/到期判定/命名/列表。

全部临时目录；不触碰真实 backups 目录。
运行：
    python -m unittest discover -s tests
"""

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from backup import (
    AUTO_PREFIX,
    LEGACY_PREFIX,
    MANUAL_PREFIX,
    BackupManager,
)
from clock import FakeClock
from db import Database

BASE = 1_789_000_000.0


class TestBase(unittest.TestCase):
    wal = False  # 默认非 WAL；WAL 回归测试子类覆盖

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "monitor.db"
        self.backups = self.tmp / "backups"
        self._make_db()

    def tearDown(self):
        self._tmp.cleanup()

    def _make_db(self) -> None:
        d = Database(self.db_path, wal=self.wal)
        try:
            d.apply_sample(
                {"llamacpp:prompt_tokens_total": 1234},
                {"timestamp": BASE, **{c: None for c in ("prompt_delta", "cached_delta", "output_delta",
                                                        "prompt_tps", "decode_tps", "requests_processing",
                                                        "requests_deferred", "context_max", "mtp_accept_rate",
                                                        "kv_cache_usage_ratio", "busy_slots")}},
                "2026-09-15",
                {"prompt_tokens": 1234, "cached_tokens": 0, "output_tokens": 0,
                 "draft_tokens": 0, "accepted_tokens": 0, "prompt_seconds": 0.0,
                 "predicted_seconds": 0.0, "draft_sequences": 0},
                now=BASE,
            )
        finally:
            d.close()

    def mgr(self, keep_count: int = 3, clock=None) -> BackupManager:
        return BackupManager(self.db_path, self.backups, keep_count=keep_count, clock=clock)

    def autos(self) -> list[Path]:
        if not self.backups.is_dir():
            return []
        return sorted(f for f in self.backups.iterdir() if f.name.startswith(AUTO_PREFIX))

    def set_mtime(self, path: Path, ts: float) -> None:
        os.utime(path, (ts, ts))


class CreateBackupTests(TestBase):
    def test_automatic_backup_created_verified_and_copy_is_consistent(self):
        m = self.mgr()
        result = m.create_backup("automatic")
        self.assertTrue(result.success)
        self.assertTrue(result.verified)
        p = Path(result.path)
        self.assertTrue(p.name.startswith(AUTO_PREFIX))
        self.assertTrue(p.suffix == ".db")
        # 备份内容完整一致（可读、含源数据）
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        state_rows = conn.execute("SELECT metric_name, value FROM state").fetchall()
        conn.close()
        self.assertIn(("llamacpp:prompt_tokens_total", 1234.0), state_rows)

    def test_manual_backup_naming_and_no_rotation(self):
        m = self.mgr(keep_count=1)
        r1 = m.create_backup("manual")
        r2 = m.create_backup("manual")
        self.assertTrue(r1.success and r2.success)
        self.assertTrue(Path(r1.path).name.startswith(MANUAL_PREFIX))
        # 手动备份不参与轮转：keep_count=1 仍保留两个
        manuals = [f for f in self.backups.iterdir() if f.name.startswith(MANUAL_PREFIX)]
        self.assertEqual(len(manuals), 2)

    def test_same_second_collision_gets_sequence_suffix(self):
        m = self.mgr()
        r1 = m.create_backup("manual", now=BASE)
        r2 = m.create_backup("manual", now=BASE)
        self.assertTrue(r1.success and r2.success)
        self.assertNotEqual(Path(r1.path).name, Path(r2.path).name)
        self.assertIn("_1", Path(r2.path).name)

    def test_missing_source_db_fails_cleanly(self):
        m = BackupManager(self.tmp / "nope.db", self.backups)
        result = m.create_backup("automatic")
        self.assertFalse(result.success)
        self.assertIsNotNone(result.error)
        # 不留下新文件
        self.assertEqual(len(list(self.backups.iterdir())), 0)

    def test_corrupt_new_backup_deleted_old_kept(self):
        """验证失败：删除新备份、旧备份一个不删。"""
        m = self.mgr()
        old = m.create_backup("automatic")
        self.assertTrue(old.success)
        with mock.patch.object(BackupManager, "verify_backup", return_value=False):
            new = m.create_backup("automatic")
        self.assertFalse(new.success)
        self.assertIn("quick_check", new.error)
        # 新文件被删除，旧备份还在
        self.assertFalse(Path(new.path).exists())
        self.assertTrue(Path(old.path).exists())


class WalModeBackupTests(TestBase):
    """AUDIT-1.1.1 GAP-003：WAL 模式备份回归。

    1.1.0 起数据库默认 WAL（-wal / -shm 旁挂文件）。此前所有备份测试都
    wal=False，从未覆盖"WAL 模式下用 Online Backup API 备份"这条真实路径。
    验证：WAL 源库 -> create_backup 成功 + 验证通过 + 备份可读且含全部数据
    （Online Backup API 产出一致快照，不会拷进半写入的 WAL 内容）。"""

    wal = True

    def test_wal_source_backup_is_consistent_and_complete(self):
        m = self.mgr()
        # 源库现在是 WAL 模式；再追加一批写入，确保 WAL 里有未 checkpoint 内容
        d = Database(self.db_path, wal=True)
        try:
            for i in range(5):
                d.apply_sample(
                    {"llamacpp:prompt_tokens_total": 1000 + i},
                    {"timestamp": BASE + i, **{c: None for c in ("prompt_delta", "cached_delta", "output_delta",
                                                                "prompt_tps", "decode_tps", "requests_processing",
                                                                "requests_deferred", "context_max", "mtp_accept_rate",
                                                                "kv_cache_usage_ratio", "busy_slots")}},
                    "2026-09-15",
                    {"prompt_tokens": 1000 + i, "cached_tokens": 0, "output_tokens": 0,
                     "draft_tokens": 0, "accepted_tokens": 0, "prompt_seconds": 0.0,
                     "predicted_seconds": 0.0, "draft_sequences": 0},
                    now=BASE + i,
                )
            # 确认源库确实是 WAL 模式
            mode = d._connect().execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode.lower(), "wal")
        finally:
            d.close()

        result = m.create_backup("automatic")
        self.assertTrue(result.success)
        self.assertTrue(result.verified)
        p = Path(result.path)
        # 备份可读、quick_check 通过、含源数据
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            # state 表应反映最后写入的 metric（1004）
            row = conn.execute(
                "SELECT value FROM state WHERE metric_name='llamacpp:prompt_tokens_total'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(float(row[0]), 1004.0)
            # live_samples 含 6 条（初始 1 + 追加 5）
            n = conn.execute("SELECT COUNT(*) FROM live_samples").fetchone()[0]
            self.assertEqual(n, 6)
        finally:
            conn.close()

    def test_wal_backup_with_pending_write_then_close(self):
        """源库关闭前未 checkpoint 的 WAL 内容也必须进备份（一致性快照）。"""
        m = self.mgr()
        d = Database(self.db_path, wal=True)
        d.apply_sample(
            {"llamacpp:prompt_tokens_total": 7777},
            {"timestamp": BASE + 99, **{c: None for c in ("prompt_delta", "cached_delta", "output_delta",
                                                          "prompt_tps", "decode_tps", "requests_processing",
                                                          "requests_deferred", "context_max", "mtp_accept_rate",
                                                          "kv_cache_usage_ratio", "busy_slots")}},
            "2026-09-16",
            {"prompt_tokens": 7777, "cached_tokens": 0, "output_tokens": 0,
             "draft_tokens": 0, "accepted_tokens": 0, "prompt_seconds": 0.0,
             "predicted_seconds": 0.0, "draft_sequences": 0},
            now=BASE + 99,
        )
        # 不 close（留 WAL 未 checkpoint），直接对仍打开的库做备份
        result = m.create_backup("manual")
        d.close()
        self.assertTrue(result.success)
        self.assertTrue(result.verified)
        conn = sqlite3.connect(f"file:{result.path}?mode=ro", uri=True)
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            row = conn.execute(
                "SELECT value FROM state WHERE metric_name='llamacpp:prompt_tokens_total'"
            ).fetchone()
            self.assertEqual(float(row[0]), 7777.0)
        finally:
            conn.close()


class VerifyTests(TestBase):
    def test_verify_good_and_corrupt(self):
        m = self.mgr()
        good = m.create_backup("manual")
        self.assertTrue(m.verify_backup(good.path))
        # 制造损坏备份
        bad = self.backups / "manual_monitor_bad.db"
        conn = sqlite3.connect(bad)
        conn.execute("CREATE TABLE t(x)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        conn.close()
        with open(bad, "r+b") as f:
            f.seek((2 - 1) * 4096)
            f.write(b"\x00")
        self.assertFalse(m.verify_backup(bad))
        self.assertFalse(m.verify_backup(self.backups / "ghost.db"))  # 不存在


class RotationTests(TestBase):
    def _create_n_auto(self, m: BackupManager, n: int, base_ts: float = BASE) -> list:
        """创建 n 个自动备份，mtime 依次错开（旧 -> 新）。"""
        results = []
        for i in range(n):
            ts = base_ts + i * 60
            r = m.create_backup("automatic", now=ts)
            self.assertTrue(r.success)
            self.set_mtime(Path(r.path), ts)
            results.append(Path(r.path))
        return results

    def test_rotation_only_deletes_excess_oldest_auto(self):
        m = self.mgr(keep_count=3)
        paths = self._create_n_auto(m, 5)
        # 最新 3 个保留，最旧 2 个删除
        for p in paths[:2]:
            self.assertFalse(p.exists())
        for p in paths[2:]:
            self.assertTrue(p.exists())
        self.assertEqual(len(self.autos()), 3)

    def test_rotation_preserves_manual_and_legacy(self):
        m = self.mgr(keep_count=1)
        paths = self._create_n_auto(m, 4)  # 只留 1 个自动
        manual = m.create_backup("manual")
        legacy = self.backups / f"{LEGACY_PREFIX}20260101_000000.db"
        legacy.write_bytes(b"legacy")
        self.assertTrue(manual.success)
        # 手动与 legacy 永不被轮转删除
        self.assertTrue(Path(manual.path).exists())
        self.assertTrue(legacy.exists())
        self.assertEqual(len(self.autos()), 1)

    def test_last_result_reports_rotated_names(self):
        m = self.mgr(keep_count=2)
        paths = self._create_n_auto(m, 2)
        last = self.mgr(keep_count=2).create_backup("automatic", now=BASE + 600)
        self.assertTrue(last.success)
        self.assertEqual(len(last.rotated), 1)
        self.assertEqual(last.rotated[0], paths[0].name)  # 最旧的被删


class IsDueTests(TestBase):
    def test_due_when_no_auto_backups(self):
        m = self.mgr()
        self.assertTrue(m.is_due(24.0))

    def test_not_due_fresh_auto_backup(self):
        m = self.mgr()
        r = m.create_backup("automatic")  # mtime = now（真实时钟）
        self.assertFalse(m.is_due(24.0))

    def test_due_after_interval_ago_mtime(self):
        m = self.mgr()
        r = m.create_backup("automatic")
        self.set_mtime(Path(r.path), time.time() - 25 * 3600)  # 25 小时前
        self.assertTrue(m.is_due(24.0))
        self.assertFalse(m.is_due(26.0))

    def test_is_due_uses_fake_clock(self):
        fake = FakeClock(start_wall=time.time())
        m = self.mgr(clock=fake)
        r = m.create_backup("automatic", now=fake.now())
        self.set_mtime(Path(r.path), fake.now() - 25 * 3600)
        self.assertTrue(m.is_due(24.0))


class ListBackupsTests(TestBase):
    def test_kinds_and_newest_first(self):
        m = self.mgr()
        r_auto = m.create_backup("automatic")
        self.set_mtime(Path(r_auto.path), BASE)
        r_manual = m.create_backup("manual")
        self.set_mtime(Path(r_manual.path), BASE + 100)
        legacy = self.backups / f"{LEGACY_PREFIX}20260101_000000.db"
        legacy.write_bytes(b"legacy")
        self.set_mtime(legacy, BASE + 200)
        items = m.list_backups()
        self.assertEqual([i["kind"] for i in items], ["legacy", "manual", "automatic"])
        self.assertTrue(items[0]["name"].startswith(LEGACY_PREFIX))
        for i in items:
            self.assertIn("size", i)
            self.assertIn("mtime", i)


class PreUpdateListTests(TestBase):
    """AUDIT-DB-005：pre_migration / pre_update 备份出现在列表里（独立 kind），
    且不受 keep_count 轮转影响。"""

    def test_pre_update_and_pre_migration_kinds_listed(self):
        m = self.mgr(keep_count=1)
        # 先建 3 个自动备份（keep_count=1 -> 会轮转掉 2 个）
        self._create_n_auto(m, 3)
        pre_update = self.backups / "pre_update_v0.13.0_to_v0.14.0_20260919_104745.db"
        pre_update.write_bytes(b"pre-update snapshot")
        self.set_mtime(pre_update, BASE + 500)
        pre_mig = self.backups / "pre_migration_v2_to_v4_20260101_000000.db"
        pre_mig.write_bytes(b"pre-migration snapshot")
        self.set_mtime(pre_mig, BASE + 600)

        # 再触发一次轮转（第 4 个自动备份）
        self._create_n_auto(m, 1, base_ts=BASE + 700)

        items = {i["name"]: i["kind"] for i in m.list_backups()}
        self.assertEqual(items[pre_update.name], "pre_update")
        self.assertEqual(items[pre_mig.name], "pre_migration")
        self.assertTrue(pre_update.exists(), "pre_update 不参与 keep_count 轮转")
        self.assertTrue(pre_mig.exists(), "pre_migration 不参与 keep_count 轮转")
        self.assertEqual(len(self.autos()), 1)

    def _create_n_auto(self, m, n: int, base_ts: float = BASE) -> list:
        results = []
        for i in range(n):
            ts = base_ts + i * 60
            r = m.create_backup("automatic", now=ts)
            self.assertTrue(r.success)
            self.set_mtime(Path(r.path), ts)
            results.append(Path(r.path))
        return results


if __name__ == "__main__":
    unittest.main()
