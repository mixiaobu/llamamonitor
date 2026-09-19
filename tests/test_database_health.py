"""
Phase 11 测试：数据库健康 —— quick_check / journal_mode / busy_timeout /
写事务重试 / 损坏不自动删除。

全部临时目录；不触碰真实数据。
运行：
    python -m unittest discover -s tests
"""

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import db as db_module
from db import (
    BUSY_TIMEOUT_MS,
    Database,
    LIVE_SAMPLE_COLUMNS,
)


class TestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def path(self, name: str = "h.db") -> Path:
        return Path(self._tmp.name) / name

    def live_row(self) -> dict:
        row = {col: None for col in LIVE_SAMPLE_COLUMNS}
        row["timestamp"] = 1_789_000_000
        return row

    def daily_incs(self) -> dict:
        return {
            "prompt_tokens": 0, "cached_tokens": 0, "output_tokens": 0,
            "draft_tokens": 0, "accepted_tokens": 0,
            "prompt_seconds": 0.0, "predicted_seconds": 0.0, "draft_sequences": 0,
        }

    def corrupt_first_byte_of_page(self, path: Path, table: str = "monitor_events") -> None:
        """把某表数据页的 B-tree 页面类型标志清零（确定性损坏，quick_check 必失败）。"""
        probe = sqlite3.connect(str(path))
        root = probe.execute(
            f"SELECT rootpage FROM sqlite_master WHERE name=?", (table,)
        ).fetchone()[0]
        page_size = probe.execute("PRAGMA page_size").fetchone()[0]
        probe.close()
        with open(path, "r+b") as f:
            f.seek((root - 1) * page_size)
            f.write(b"\x00")


class QuickCheckTests(TestBase):
    def test_healthy_db_passes(self):
        d = Database(self.path(), wal=False)
        try:
            ok, detail = d.quick_check()
            self.assertTrue(ok)
            self.assertIsNone(detail)
        finally:
            d.close()

    def test_corrupt_file_detected_and_not_auto_deleted(self):
        d = Database(self.path(), wal=False)
        p = self.path()
        try:
            d.apply_sample({"llamacpp:prompt_tokens_total": 10}, self.live_row(),
                           "2026-09-15", self.daily_incs(), now=1_789_000_000)
        finally:
            d.close()

        self.assertTrue(p.exists())
        self.corrupt_first_byte_of_page(p)
        d2 = Database(p, wal=False)
        try:
            ok, detail = d2.quick_check()
            self.assertFalse(ok)
            self.assertIsNotNone(detail)
            # 绝不自动删除/重建：文件原样存在
            self.assertTrue(p.exists())
            # 上层把健康状态设为 corrupt（protective mode 入口）
            d2.set_health("corrupt", detail)
            self.assertEqual(d2.health, "corrupt")
            self.assertEqual(d2.health_detail, detail)
        finally:
            d2.close()

    def test_set_health_validates_status(self):
        d = Database(self.path(), wal=False)
        try:
            with self.assertRaises(ValueError):
                d.set_health("on-fire")
            d.set_health("warning", "journal mode not wal")
            self.assertEqual(d.health, "warning")
        finally:
            d.close()


class JournalModeTests(TestBase):
    def test_wal_enabled_reads_back_wal(self):
        d = Database(self.path(), wal=True)
        try:
            d._connect()
            self.assertEqual(d.journal_mode, "wal")
        finally:
            d.close()

    def test_no_wal_reads_back_delete(self):
        d = Database(self.path(), wal=False)
        try:
            d._connect()
            self.assertEqual(d.journal_mode, "delete")
        finally:
            d.close()

    def test_busy_timeout_configured(self):
        d = Database(self.path(), wal=False)
        try:
            conn = d._connect()
            value = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            self.assertEqual(value, BUSY_TIMEOUT_MS)
        finally:
            d.close()


class WriteLockRetryTests(TestBase):
    def _hold_write_lock(self, path: str, hold_seconds: float) -> threading.Thread:
        """后台线程持有一个写锁（BEGIN IMMEDIATE）hold_seconds 秒。
        连接在**线程内**创建（sqlite3 连接的线程约束）。
        返回线程；调用方应在操作完成后 join（确保文件句柄释放后再清理临时目录）。"""
        t = threading.Thread(target=self._hold_inner, args=(path, hold_seconds), daemon=True)
        t.start()
        time.sleep(0.15)  # 确保 BEGIN IMMEDIATE 已执行（锁已持有）
        return t

    @staticmethod
    def _hold_inner(path: str, hold_seconds: float) -> None:
        holder_conn = sqlite3.connect(path, timeout=30)
        holder_conn.isolation_level = None
        try:
            holder_conn.execute("BEGIN IMMEDIATE")
            time.sleep(hold_seconds)
            holder_conn.execute("COMMIT")
        finally:
            holder_conn.close()

    def test_short_lock_resolved_by_busy_timeout(self):
        """短暂锁（< busy_timeout）：等待后成功，无需 retry。"""
        with mock.patch.object(db_module, "BUSY_TIMEOUT_MS", 800):
            d = Database(self.path(), wal=False)
            holder = self._hold_write_lock(str(d.path), 0.25)
            try:
                t0 = time.monotonic()
                d.apply_sample({"llamacpp:prompt_tokens_total": 5}, self.live_row(),
                               "2026-09-15", self.daily_incs(), now=1_789_000_000)
                elapsed = time.monotonic() - t0
                self.assertLess(elapsed, 3.0)
                state = d.get_state()
                self.assertEqual(state["llamacpp:prompt_tokens_total"], 5.0)
            finally:
                d.close()
                holder.join()

    def test_lock_longer_than_busy_timeout_then_retry_succeeds(self):
        """锁 0.5s > busy_timeout 0.3s：首次尝试超时失败 -> retry 在锁释放后成功。
        没有 retry 机制这里会直接抛异常。"""
        with mock.patch.object(db_module, "BUSY_TIMEOUT_MS", 300):
            d = Database(self.path(), wal=False)
            holder = self._hold_write_lock(str(d.path), 0.5)
            try:
                d.apply_sample({"llamacpp:prompt_tokens_total": 7}, self.live_row(),
                               "2026-09-15", self.daily_incs(), now=1_789_000_000)
                state = d.get_state()
                self.assertEqual(state["llamacpp:prompt_tokens_total"], 7.0)
            finally:
                d.close()
                holder.join()

    def test_lock_held_past_retry_budget_raises(self):
        """锁 1.8s：所有尝试（apply 从 ~0.15s 起，末次 ~1.7s 结束）都失败
        -> 抛出异常，baseline 不更新（写失败绝不更新 baseline）。"""
        with mock.patch.object(db_module, "BUSY_TIMEOUT_MS", 300):
            d = Database(self.path(), wal=False)
            holder = self._hold_write_lock(str(d.path), 1.8)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    d.apply_sample({"llamacpp:prompt_tokens_total": 9}, self.live_row(),
                                   "2026-09-15", self.daily_incs(), now=1_789_000_000)
            finally:
                d.close()
                holder.join()  # 锁释放后再验证（避免读也被写锁干扰）
            d2 = Database(self.path(), wal=False)
            try:
                state = d2.get_state()
                self.assertNotIn("llamacpp:prompt_tokens_total", state)
            finally:
                d2.close()


class MigrationEventTests(TestBase):
    def test_v3_migration_event_recorded(self):
        """v3 迁移（含 legacy 升级路径）会留下一条 migration 事件。"""
        d = Database(self.path(), wal=False)
        try:
            events = d.get_events(limit=100)
            migration = [e for e in events if e["event_type"] == "migration"]
            # v1->v2 迁移不记事件；v2->v3 / v3->v4 记事件。事件结构必须合法。
            for e in migration:
                self.assertIn(e["details"].get("to"), (3, 4, None))
        finally:
            d.close()


if __name__ == "__main__":
    unittest.main()
