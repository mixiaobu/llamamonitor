"""
Phase 8 测试：数据管理（info / CSV 导出 / 备份 / 清实时 / 重置统计）。

覆盖：
1. Data Info（有数据 / 无数据库文件两种情况，不报 500）
2. CSV 导出（BOM、列齐全、原始整数、compute/logical/接受率计算、文件名）
3. CSV：draft=0 时接受率为空
4. 备份（SQLite Backup API；备份文件可用 sqlite3 重新打开且数据一致）
5. 备份列表（newest first、最多 10 个）
6. Clear Live History（confirm 校验；只删 live_samples，daily/state 不动）
7. Reset Statistics（confirm="RESET" 校验；删 daily+live、保留 state）
8. 重置后继续采集：只累计新 delta，旧 Token 不重新计入
9. 数据库写锁存在（threading.Lock）

测试不访问真实 llama-server / 真实数据库 / 真实 %LOCALAPPDATA%（环境变量被重定向到临时目录）。
项目使用标准库 unittest：临时目录用 tempfile.TemporaryDirectory（等价 pytest tmp_path）。

在项目根目录运行：
    python -m unittest discover -s tests
"""

import csv
import io
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from collector import MetricsCollector
from configutil import loopback_app, make_config
from db import Database, local_date
from server import build_app

TEXT_A = (
    "# HELP llamacpp:prompt_tokens_total Total prompt processing tokens\n"
    "llamacpp:prompt_tokens_total 100\n"
    "llamacpp:prompt_tokens_cached_total 50\n"
    "llamacpp:prompt_seconds_total 1.0\n"
    "llamacpp:tokens_predicted_total 10\n"
    "llamacpp:tokens_predicted_seconds_total 2.0\n"
    "llamacpp:n_decode_total 2\n"
    "llamacpp:spec_decode_num_draft_tokens_total 5\n"
    "llamacpp:spec_decode_num_accepted_tokens_total 3\n"
)
# 第二轮：所有 counter 增长（delta: prompt+50 cached+30 output+10 draft+5 accepted+3）
TEXT_B = (
    "# HELP llamacpp:prompt_tokens_total Total prompt processing tokens\n"
    "llamacpp:prompt_tokens_total 150\n"
    "llamacpp:prompt_tokens_cached_total 80\n"
    "llamacpp:prompt_seconds_total 1.5\n"
    "llamacpp:tokens_predicted_total 20\n"
    "llamacpp:tokens_predicted_seconds_total 3.0\n"
    "llamacpp:n_decode_total 4\n"
    "llamacpp:spec_decode_num_draft_tokens_total 10\n"
    "llamacpp:spec_decode_num_accepted_tokens_total 6\n"
)
# 重置后继续：再增长（相对 TEXT_B 的 delta: prompt+50 cached+20 output+10 draft+10 accepted+3）
TEXT_C = (
    "# HELP llamacpp:prompt_tokens_total Total prompt processing tokens\n"
    "llamacpp:prompt_tokens_total 200\n"
    "llamacpp:prompt_tokens_cached_total 100\n"
    "llamacpp:prompt_seconds_total 2.0\n"
    "llamacpp:tokens_predicted_total 30\n"
    "llamacpp:tokens_predicted_seconds_total 4.0\n"
    "llamacpp:n_decode_total 6\n"
    "llamacpp:spec_decode_num_draft_tokens_total 20\n"
    "llamacpp:spec_decode_num_accepted_tokens_total 9\n"
)


def _parsed(text: str) -> dict:
    from collector import parse_metrics

    return parse_metrics(text)


class DataManagementTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._dbs: list[Database] = []
        # 备份/日志目录重定向到临时 LOCALAPPDATA，不触碰真实 %LOCALAPPDATA%
        self._patch = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.tmp / "lad")})
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        for db in self._dbs:
            db.close()
        self._tmp.cleanup()

    def _backups_dir(self) -> Path:
        return self.tmp / "lad" / "LlamaMonitor" / "backups"

    def _start(self, text: str = TEXT_A, offline: bool = False):
        """启动应用；_fetch_parsed 打补丁返回给定文本（不访问真实服务器）。"""
        cfg = make_config()
        db = Database(self.tmp / "dm.db")
        self._dbs.append(db)
        collector = MetricsCollector(cfg, db)

        if offline:
            async def _fetch_offline():
                return None

            collector._fetch_parsed = _fetch_offline
        else:
            async def _fetch_patched():
                return _parsed(text)

            collector._fetch_parsed = _fetch_patched

        app = build_app(db, collector)
        client = TestClient(loopback_app(app))  # 修改类 API 要求本地客户端
        client.__enter__()
        return client, collector, db

    def _round(self, client, collector, text: str) -> None:
        async def _fetch():
            return _parsed(text)

        collector._fetch_parsed = _fetch
        client.portal.call(collector.collect_once)

    def _read_db(self, sql, params=()):
        """用独立连接读库（跨线程安全；WAL 允许并发读）。"""
        conn = sqlite3.connect(self.tmp / "dm.db")
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    # ---------- 1) Data Info ----------

    def test_data_info_with_data(self):
        client, _collector, _db = self._start()
        try:
            self._round(client, _collector, TEXT_B)  # lifespan 已跑 A，这里补 B
            data = client.get("/api/data/info").json()
            self.assertEqual(data["database_path"], str(self.tmp / "dm.db"))
            self.assertGreater(data["database_size_bytes"], 0)
            today = local_date()
            self.assertEqual(data["first_recorded_date"], today)
            self.assertEqual(data["last_recorded_date"], today)
            self.assertEqual(data["recorded_days"], 1)
            self.assertEqual(data["daily_rows"], 1)
            self.assertEqual(data["live_samples"], 2)
            self.assertEqual(data["backup_count"], 0)
        finally:
            client.__exit__(None, None, None)

    def test_data_info_without_data(self):
        client, _collector, _db = self._start(offline=True)  # 无成功采集（Phase 11 起启动
        # quick_check 会先建空库文件——文件可能存在，但没有任何记录数据）
        try:
            r = client.get("/api/data/info")
            self.assertEqual(r.status_code, 200)
            data = r.json()
            # Phase 11：启动 quick_check 会创建带 schema 的空库文件（size 可 >0），
            # 但没有任何记录数据
            self.assertIsNone(data["first_recorded_date"])
            self.assertIsNone(data["last_recorded_date"])
            self.assertEqual(data["recorded_days"], 0)
            self.assertEqual(data["daily_rows"], 0)
            self.assertEqual(data["live_samples"], 0)
            self.assertEqual(data["gpu_samples"], 0)
        finally:
            client.__exit__(None, None, None)

    # ---------- 2) CSV 导出 ----------

    def _export_csv(self, client) -> tuple[bytes, str]:
        r = client.get("/api/data/export/daily.csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r.headers["content-type"])
        body = r.content
        filename = re.search(r'filename="([^"]+)"', r.headers["content-disposition"]).group(1)
        return body, filename

    def test_csv_export_content_and_bom(self):
        client, collector, _db = self._start()
        try:
            self._round(client, collector, TEXT_B)
            body, filename = self._export_csv(client)
            # BOM（UTF-8 with BOM，Windows Excel 直接打开不乱码）
            self.assertTrue(body.startswith(b"\xef\xbb\xbf"))
            # 文件名格式
            self.assertRegex(filename, r"^LlamaMonitor_daily_\d{8}_\d{6}\.csv$")
            text = body.decode("utf-8-sig")
            rows = list(csv.reader(io.StringIO(text)))
            header = rows[0]
            self.assertEqual(header, [
                "date", "prompt_tokens", "cached_tokens", "output_tokens",
                "compute_tokens", "logical_tokens", "draft_tokens", "accepted_tokens",
                "mtp_accept_rate", "prompt_seconds", "predicted_seconds",
                # Phase 11：数据质量字段
                "monitoring_coverage_percent", "gap_count", "possible_token_loss",
            ])
            self.assertEqual(len(rows), 2)  # 表头 + 当天 1 行
            row = rows[1]
            self.assertEqual(row[0], local_date())
            # 原始整数（不做 1.2M 缩写）
            self.assertEqual(row[1], "50")   # prompt delta
            self.assertEqual(row[2], "30")   # cached delta
            self.assertEqual(row[3], "10")   # output delta
            self.assertEqual(row[4], "60")   # compute = 50 + 10
            self.assertEqual(row[5], "90")   # logical = 50 + 30 + 10
            self.assertEqual(row[6], "5")    # draft delta
            self.assertEqual(row[7], "3")    # accepted delta
            self.assertEqual(float(row[8]), 60.0)  # 3/5*100
            self.assertEqual(float(row[9]), 0.5)   # prompt_seconds delta
            self.assertEqual(float(row[10]), 1.0)  # predicted_seconds delta
            # Phase 11 数据质量列：无缺口 -> 覆盖率 100 / gap 0 / 无可能丢失
            self.assertEqual(float(row[11]), 100.0)
            self.assertEqual(row[12], "0")
            self.assertEqual(row[13], "no")
        finally:
            client.__exit__(None, None, None)

    def test_csv_rate_empty_when_no_draft(self):
        client, _collector, _db = self._start()
        try:
            # 只有基线轮（TEXT_A 是 baseline，daily 行由第二轮产生）——
            # 直接手工写一条 draft=0 的 daily 行验证空接受率
            # （用独立连接写入：Database 的连接绑定在事件循环线程）
            conn = sqlite3.connect(self.tmp / "dm.db")
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO daily_usage(date, prompt_tokens, cached_tokens, output_tokens, "
                        "draft_tokens, accepted_tokens, prompt_seconds, predicted_seconds) "
                        "VALUES(?, 100, 0, 20, 0, 0, 1.0, 2.0)",
                        ("2026-01-01",),
                    )
            finally:
                conn.close()
            body, _fn = self._export_csv(client)
            text = body.decode("utf-8-sig")
            rows = list(csv.reader(io.StringIO(text)))
            row = next(r for r in rows[1:] if r[0] == "2026-01-01")
            self.assertEqual(row[8], "")  # draft=0 -> 接受率空
            self.assertEqual(row[4], "120")  # compute 仍有值
        finally:
            client.__exit__(None, None, None)

    # ---------- 3) 备份 ----------

    def test_backup_creates_consistent_copy(self):
        client, _collector, db = self._start()
        try:
            self._round(client, _collector, TEXT_B)
            r = client.post("/api/data/backup")
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertTrue(data["success"])
            dest = Path(data["file"])
            self.assertTrue(dest.exists())
            self.assertEqual(dest.parent, self._backups_dir())
            # Phase 11：手动备份用 manual_monitor_ 前缀，且经过 quick_check 验证
            self.assertRegex(dest.name, r"^manual_monitor_\d{8}_\d{6}(\_\d+)?\.db$")
            self.assertTrue(data["verified"])
            self.assertGreater(data["size_bytes"], 0)
            # 备份可用 sqlite3 重新打开，数据与源库一致
            conn = sqlite3.connect(dest)
            try:
                daily = conn.execute(
                    "SELECT prompt_tokens, output_tokens FROM daily_usage ORDER BY date"
                ).fetchall()
                state_rows = conn.execute("SELECT COUNT(*) FROM state").fetchone()[0]
                live_rows = conn.execute("SELECT COUNT(*) FROM live_samples").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(daily, [(50, 10)])
            self.assertGreater(state_rows, 0)
            self.assertEqual(live_rows, 2)
        finally:
            client.__exit__(None, None, None)

    def test_backup_list_newest_first_max20(self):
        client, _collector, _db = self._start()
        try:
            bdir = self._backups_dir()
            bdir.mkdir(parents=True, exist_ok=True)
            now = time.time()
            # 造 21 个不同 mtime 的 legacy（monitor_ 前缀）假备份文件
            for i in range(21):
                f = bdir / f"monitor_20260101_{i:06d}.db"
                f.write_bytes(b"")
                os.utime(f, (now - (21 - i) * 60, now - (21 - i) * 60))
            # 再做一个真实手动备份（最新 mtime）
            r = client.post("/api/data/backup")
            self.assertEqual(r.status_code, 200)

            listing = client.get("/api/data/backups")
            self.assertEqual(listing.status_code, 200)
            items = listing.json()
            self.assertEqual(len(items), 20)  # 最多 20 个
            self.assertEqual(Path(items[0]["filename"]).name, Path(r.json()["file"]).name)  # 最新在前
            self.assertEqual(items[0]["kind"], "manual")
            self.assertEqual(items[1]["kind"], "legacy")  # 旧 monitor_* 归类 legacy
            # mtime 非递增（newest first）
            for a, b in zip(items, items[1:]):
                self.assertGreaterEqual(a["created_at"], b["created_at"])
            # 最旧的假文件（21 分钟前的）被挤出列表
            self.assertNotIn("monitor_20260101_000000.db", [i["filename"] for i in items])
        finally:
            client.__exit__(None, None, None)

    # ---------- 4) Clear Live History ----------

    def test_clear_live_requires_confirm(self):
        client, _collector, _db = self._start()
        try:
            self._round(client, _collector, TEXT_B)
            r = client.post("/api/data/clear-live", json={})
            self.assertEqual(r.status_code, 400)
            self.assertEqual(r.json()["error"]["code"], "CONFIRMATION_REQUIRED")
            r = client.post("/api/data/clear-live", json={"confirm": False})
            self.assertEqual(r.status_code, 400)
            # live 行仍在
            self.assertEqual(self._read_db("SELECT COUNT(*) FROM live_samples")[0][0], 2)
        finally:
            client.__exit__(None, None, None)

    def test_clear_live_deletes_only_live(self):
        client, collector, db = self._start()
        try:
            self._round(client, collector, TEXT_B)
            state_before = client.portal.call(db.get_state)
            r = client.post("/api/data/clear-live", json={"confirm": True})
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertTrue(data["success"])
            self.assertEqual(data["deleted"], 2)
            # live 清空，daily / state 不动
            self.assertEqual(self._read_db("SELECT COUNT(*) FROM live_samples")[0][0], 0)
            self.assertEqual(self._read_db("SELECT COUNT(*) FROM daily_usage")[0][0], 1)
            state_after = client.portal.call(db.get_state)
            self.assertEqual(state_before, state_after)
            self.assertGreater(len(state_before), 0)
        finally:
            client.__exit__(None, None, None)

    # ---------- 5) Reset Statistics ----------

    def test_reset_requires_reset_confirm(self):
        client, _collector, _db = self._start()
        try:
            self._round(client, _collector, TEXT_B)
            r = client.post("/api/data/reset-statistics", json={"confirm": "yes"})
            self.assertEqual(r.status_code, 400)
            self.assertEqual(r.json()["error"]["code"], "CONFIRMATION_REQUIRED")
            r = client.post("/api/data/reset-statistics", json={})
            self.assertEqual(r.status_code, 400)
            # 数据未动
            self.assertEqual(self._read_db("SELECT COUNT(*) FROM daily_usage")[0][0], 1)
        finally:
            client.__exit__(None, None, None)

    def test_reset_deletes_daily_and_live_keeps_state(self):
        client, collector, db = self._start()
        try:
            self._round(client, collector, TEXT_B)
            state_before = client.portal.call(db.get_state)
            self.assertGreater(len(state_before), 0)
            r = client.post("/api/data/reset-statistics", json={"confirm": "RESET"})
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertTrue(data["success"])
            self.assertEqual(data["daily_deleted"], 1)
            self.assertEqual(data["live_deleted"], 2)
            # daily / live 清空，state 原样保留（baseline 不动）
            self.assertEqual(self._read_db("SELECT COUNT(*) FROM daily_usage")[0][0], 0)
            self.assertEqual(self._read_db("SELECT COUNT(*) FROM live_samples")[0][0], 0)
            state_after = client.portal.call(db.get_state)
            self.assertEqual(state_before, state_after)
        finally:
            client.__exit__(None, None, None)

    def test_after_reset_new_tokens_accumulate_from_zero(self):
        client, collector, db = self._start()
        try:
            self._round(client, collector, TEXT_B)
            client.post("/api/data/reset-statistics", json={"confirm": "RESET"})

            # 重置后继续采集 TEXT_C：只累计相对 TEXT_B 的新 delta（prompt +50）
            # 而不是从头累计 200 —— 证明 baseline 被保留、旧 Token 不重新计入
            self._round(client, collector, TEXT_C)
            rows = self._read_db(
                "SELECT prompt_tokens, cached_tokens, output_tokens, draft_tokens, accepted_tokens "
                "FROM daily_usage"
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0], (50, 20, 10, 10, 3))

            # 信息接口：今日 Total 只反映新 delta
            summary = client.get("/api/summary").json()
            self.assertEqual(summary["today"]["logical_tokens"], 80)  # 50 + 20 + 10
            self.assertEqual(summary["total"]["logical_tokens"], 80)
        finally:
            client.__exit__(None, None, None)

    # ---------- 6) 写锁 ----------

    def test_database_has_write_lock(self):
        client, _collector, db = self._start()
        try:
            self.assertIsInstance(db._write_lock, type(threading.Lock()))
        finally:
            client.__exit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
