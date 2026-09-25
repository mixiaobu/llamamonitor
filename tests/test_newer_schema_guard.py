"""
Phase 12 测试：数据库 schema 降级保护 + pre-migration backup。

- user_version > CURRENT：只读 incompatible 模式（不建表、不迁移、不降级、不写入）；
- 只读模式下读取正常（state / daily / gaps）；
- /api/health 报告 incompatible；修改类 API 409；
- pre-migration backup：既有库迁移前生成 pre_migration_vOLD_to_vNEW_*.db
  （SQLite Backup API + quick_check 验证）；
- 全新空库不生成 pre-migration backup；
- backup 失败 -> 放弃迁移（库保持旧版本，incompatible 保护）；修复后
  ensure_migrated() 重试成功；
- pre_migration_*.db 不参与 automatic backup 轮换（前缀隔离）。

运行：python -m unittest discover -s tests
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import AppConfig, LoadedConfig  # noqa: E402
from db import (  # noqa: E402
    CURRENT_SCHEMA_VERSION,
    DB_HEALTH_INCOMPATIBLE,
    Database,
)
from server import build_app  # noqa: E402


def _make_v2_db(path: Path) -> None:
    """构造一个 v2 既有库（v1+v2 表 + 少量历史数据），用于迁移测试。"""
    db = Database(path, wal=False, retention_seconds=3600)
    db.get_schema_version()  # 触发实际连接（Database 是懒连接）
    db.close()  # 此时文件已是完整 v3 schema
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
        # 回退到 v2：删除 v3 表（模拟 Phase 11 之前的库）
        for table in ("monitor_events", "data_gaps", "backup_history"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("PRAGMA user_version = 2")
        # 放入真实历史数据（pre-migration backup 应包含它）
        conn.execute(
            "INSERT INTO daily_usage(date, prompt_tokens, cached_tokens, output_tokens, "
            "draft_tokens, accepted_tokens, prompt_seconds, predicted_seconds, draft_sequences) "
            "VALUES('2026-01-01', 1000, 0, 500, 0, 0, 1.0, 2.0, 0)"
        )
        conn.execute("INSERT INTO state(metric_name, value) VALUES('llamacpp:prompt_tokens_total', 1000.0)")
        conn.commit()
    finally:
        conn.close()


class NewerSchemaGuardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_newer_user_version_is_readonly_incompatible(self):
        """§41：user_version=999 > 当前 —— 只读 incompatible，不降级、不建表。"""
        path = self.tmp / "newer.db"
        db = Database(path, wal=False, retention_seconds=3600)
        db.get_schema_version()  # 触发连接（建好 v3 schema）
        db.close()
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA user_version = 999")
        conn.commit()
        conn.close()

        db = Database(path, wal=False, retention_seconds=3600)
        try:
            self.assertEqual(db.get_schema_version(), 999)  # 触发连接
            self.assertEqual(db.health, DB_HEALTH_INCOMPATIBLE)
            self.assertIn("read-only", db.health_detail or "")
            # schema 不被降低
            # 读取正常
            self.assertIsInstance(db.get_state(), dict)
            self.assertIsInstance(db.get_daily_usage(), list)
            # 连接是只读的：直接 INSERT 必须失败
            conn = db._connect()
            with self.assertRaises(sqlite3.OperationalError):
                with conn:
                    conn.execute(
                        "INSERT INTO state(metric_name, value) VALUES('__probe__', 1.0)"
                    )
        finally:
            db.close()

    def test_newer_db_api_reports_incompatible_and_blocks_mutations(self):
        path = self.tmp / "newer_api.db"
        db = Database(path, wal=False, retention_seconds=3600)
        db.get_schema_version()  # 触发连接（建好 v3 schema）
        db.close()
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA user_version = 999")
        conn.commit()
        conn.close()

        db = Database(path, wal=False, retention_seconds=3600)
        cfg = AppConfig.default()
        cfg.llama_server.url = "http://127.0.0.1:9"
        loaded = LoadedConfig(cfg, self.tmp / "config.json", True, False, False)
        from collector import MetricsCollector

        app = build_app(db, MetricsCollector(cfg, db), loaded, None)
        from configutil import loopback_app  # 修改类端点要求 loopback 来源

        with TestClient(loopback_app(app)) as client:
            h = client.get("/api/health").json()
            self.assertEqual(h["database"], DB_HEALTH_INCOMPATIBLE)
            self.assertTrue(h["database_detail"])
            # 修改类操作被拒绝（409）
            r = client.post("/api/data/reset-statistics", json={"confirm": "RESET"})
            self.assertEqual(r.status_code, 409)
            r2 = client.post("/api/data/clear-live", json={"confirm": True})
            self.assertEqual(r2.status_code, 409)
            # check-database：quick_check 通过但版本更新 -> 保持 incompatible
            r3 = client.post("/api/data/check-database")
            self.assertEqual(r3.status_code, 200)
            self.assertEqual(r3.json()["status"], DB_HEALTH_INCOMPATIBLE)
        db.close()


class PreMigrationBackupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_v2_to_v3_creates_verified_pre_migration_backup(self):
        """§98：既有库迁移前自动快照（Backup API + quick_check 验证）。"""
        dbpath = self.tmp / "hist.db"
        _make_v2_db(dbpath)
        backup_dir = self.tmp / "backups"
        db = Database(dbpath, wal=False, retention_seconds=3600,
                      pre_migration_backup_dir=backup_dir)
        try:
            self.assertEqual(db.health, "healthy")
            self.assertEqual(db.get_schema_version(), CURRENT_SCHEMA_VERSION)
            # 1.1.0：迁移目标版本 v5 -> 备份文件名 pre_migration_v2_to_v5_*
            files = list(backup_dir.glob("pre_migration_v2_to_v5_*.db"))
            self.assertEqual(len(files), 1, "迁移前必须生成一个 pre-migration backup")
            # 备份文件本身 quick_check 通过，且包含迁移前的历史数据
            conn = sqlite3.connect(str(files[0]))
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            row = conn.execute(
                "SELECT prompt_tokens, output_tokens FROM daily_usage WHERE date='2026-01-01'"
            ).fetchone()
            self.assertEqual(row, (1000, 500))
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            conn.close()
            # 主库历史数据仍在
            self.assertEqual(db.get_daily_usage()[0]["prompt_tokens"], 1000)
        finally:
            db.close()

    def test_fresh_db_does_not_create_backup(self):
        """全新空库（无既有数据）不需要 pre-migration backup。"""
        backup_dir = self.tmp / "backups"
        db = Database(self.tmp / "fresh.db", wal=False, retention_seconds=3600,
                      pre_migration_backup_dir=backup_dir)
        db.close()
        self.assertEqual(list(backup_dir.glob("pre_migration_*.db")), [])

    def test_backup_failure_skips_migration_and_stays_protective(self):
        """§98：backup 失败 -> 不继续迁移（库保持 v2 原样，incompatible 保护）。"""
        dbpath = self.tmp / "hist2.db"
        _make_v2_db(dbpath)
        blocker = self.tmp / "backups"  # 让"目录"是一个文件 -> mkdir 失败
        blocker.write_text("blocker")
        db = Database(dbpath, wal=False, retention_seconds=3600,
                      pre_migration_backup_dir=blocker)
        try:
            self.assertEqual(db.get_schema_version(), 2)  # 触发连接（迁移尝试）
            self.assertEqual(db.health, DB_HEALTH_INCOMPATIBLE)
            self.assertIn("pre-migration backup failed", db.health_detail or "")
            # 迁移被放弃：schema 仍是 2
        finally:
            db.close()
        # 库本身保持 v2 原样（没有半迁移状态）
        conn = sqlite3.connect(str(dbpath))
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertIsNone(conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='monitor_events'"
        ).fetchone())
        conn.close()

    def test_ensure_migrated_retries_after_fix(self):
        """check-database 路径：修复备份目录后 ensure_migrated() 重试成功。"""
        dbpath = self.tmp / "hist3.db"
        _make_v2_db(dbpath)
        blocker = self.tmp / "backups"
        blocker.write_text("blocker")
        db = Database(dbpath, wal=False, retention_seconds=3600,
                      pre_migration_backup_dir=blocker)
        try:
            self.assertEqual(db.get_schema_version(), 2)  # 触发连接（迁移尝试）
            self.assertEqual(db.health, DB_HEALTH_INCOMPATIBLE)
            # 修复：把"文件"换成目录
            blocker.unlink()
            blocker.mkdir()
            db.ensure_migrated()
            self.assertEqual(db.get_schema_version(), CURRENT_SCHEMA_VERSION)
            # 迁移前备份这次成功生成（1.1.0：目标版本升到 v5）
            self.assertEqual(len(list(blocker.glob("pre_migration_v2_to_v5_*.db"))), 1)
        finally:
            db.close()


class PreMigrationRotationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_pre_migration_not_rotated(self):
        """§99：pre_migration_*.db 不参与 automatic backup 轮换。"""
        from backup import BackupManager, AUTO_PREFIX

        backup_dir = self.tmp / "backups"
        backup_dir.mkdir()
        # 20 个自动备份（超过 keep_count=14）+ 1 个 pre-migration
        for i in range(20):
            (backup_dir / f"{AUTO_PREFIX}202609{i % 28:02d}_{i:06d}.db").write_bytes(b"x")
        pre = backup_dir / "pre_migration_v2_to_v3_20260918_120000.db"
        pre.write_bytes(b"pre")
        mgr = BackupManager(self.tmp / "fake.db", backup_dir, keep_count=14)
        # AUDIT-DB-005：pre_migration 现在**出现**在备份列表里（kind=pre_migration），
        # 但轮转仍只删 auto_*（下面的断言保持原有意图：不参与轮转）
        pre_in_list = [b for b in mgr.list_backups() if b["name"].startswith("pre_migration")]
        self.assertEqual(len(pre_in_list), 1)
        self.assertEqual(pre_in_list[0]["kind"], "pre_migration")
        removed = mgr._rotate_auto()
        self.assertEqual(len(removed), 6)
        self.assertTrue(pre.exists(), "pre-migration backup 被轮转删除了（不允许）")
        autos = [f for f in backup_dir.iterdir() if f.name.startswith(AUTO_PREFIX)]
        self.assertEqual(len(autos), 14)


if __name__ == "__main__":
    unittest.main()
