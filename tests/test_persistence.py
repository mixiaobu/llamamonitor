"""
Phase 2 集成测试：baseline / 正常 delta / Counter reset / 程序重启恢复 state /
跨天统计 / 缺失字段 / 48h 清理 / 事务回滚。

在项目根目录运行：
    python -m unittest discover -s tests -v
"""

import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector import MetricsCollector
from configutil import make_config
from db import Database
from metrics_parser import parse_metrics

# 短名 -> llama.cpp 指标全名（用于生成测试样本）
_FULL = {
    "prompt": "llamacpp:prompt_tokens_total",
    "cached": "llamacpp:prompt_tokens_cached_total",
    "prompt_sec": "llamacpp:prompt_seconds_total",
    "output": "llamacpp:tokens_predicted_total",
    "pred_sec": "llamacpp:tokens_predicted_seconds_total",
    "n_decode": "llamacpp:n_decode_total",
    "draft": "llamacpp:spec_decode_num_draft_tokens_total",
    "accepted": "llamacpp:spec_decode_num_accepted_tokens_total",
}


def sample(**values) -> str:
    """生成一份最小 llama.cpp /metrics 文本；某字段值为 None 表示该字段缺失。"""
    base = {
        "prompt": 100,
        "cached": 50,
        "prompt_sec": 1.0,
        "output": 10,
        "pred_sec": 2.0,
        "n_decode": 2,
        "draft": 5,
        "accepted": 3,
    }
    base.update(values)
    lines = [f"{_FULL[k]} {v}" for k, v in base.items() if v is not None]
    lines.append("llamacpp:requests_processing 1")
    lines.append("llamacpp:requests_deferred 0")
    return "\n".join(lines) + "\n"


# 样本 A：服务器已运行一段时间，Counter 已有值（第一次读取只能建 baseline）
TEXT_A = sample()
# 样本 B：在 A 基础上的正常增长
TEXT_B = sample(prompt=150, cached=80, prompt_sec=1.5, output=20, pred_sec=3.0, n_decode=4, draft=10, accepted=6)
# 样本 C：prompt Counter 重启变小（150->30），output 正常增长（20->25）
TEXT_C = sample(prompt=30, prompt_sec=0.2, output=25, pred_sec=3.5)
# 样本 D：程序重启测试用（prompt 150->80 触发 reset，output 20->30）
TEXT_D = sample(prompt=80, output=30)
# 样本 E：跨天测试第二天用（prompt 150->180，output 20->25）
TEXT_E = sample(prompt=180, output=25)


class PersistenceFlowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "test.db"
        self._dbs: list[Database] = []

    def tearDown(self):
        # 无论断言成败都先关连接（Windows 上未关闭的连接会锁住文件）
        for db in self._dbs:
            db.close()
        self._tmp.cleanup()

    def _make_collector(self, text=None):
        """
        创建 collector，用固定 metrics 文本替换 _fetch_parsed（不走真实网络）。
        返回 (collector, db, set_text)；set_text 可切换下一轮“抓取”到的文本。
        """
        db = Database(self.db_path)
        self._dbs.append(db)
        collector = MetricsCollector(make_config(), db)

        def set_text(t):
            parsed = parse_metrics(t)

            async def _fetch_parsed():
                return parsed

            collector._fetch_parsed = _fetch_parsed

        if text is not None:
            set_text(text)
        return collector, db, set_text

    # 1) 第一次读取：只建 baseline，所有 delta=0
    def test_first_read_is_baseline_only(self):
        collector, db, _ = self._make_collector(TEXT_A)
        snapshot = asyncio.run(collector.collect_once())
        self.assertTrue(snapshot["online"])

        # state：保存 baseline 值
        state = db.get_state()
        self.assertEqual(state["llamacpp:prompt_tokens_total"], 100.0)
        self.assertEqual(state["llamacpp:spec_decode_num_accepted_tokens_total"], 3.0)

        # daily：一行，全部为 0（不把已有 Counter 当作历史数据）
        daily = db.get_daily_usage()
        self.assertEqual(len(daily), 1)
        row = daily[0]
        self.assertEqual(
            (row["prompt_tokens"], row["cached_tokens"], row["output_tokens"],
             row["draft_tokens"], row["accepted_tokens"]),
            (0, 0, 0, 0, 0),
        )
        self.assertEqual(row["prompt_seconds"], 0.0)
        self.assertEqual(row["predicted_seconds"], 0.0)

        # live：一行，全部 delta=0；TPS 分母 0 -> None；MTP draft delta 0 -> None
        samples = db.get_live_samples()
        self.assertEqual(len(samples), 1)
        s = samples[0]
        self.assertEqual((s["prompt_delta"], s["cached_delta"], s["output_delta"]), (0, 0, 0))
        self.assertIsNone(s["prompt_tps"])
        self.assertIsNone(s["decode_tps"])
        self.assertIsNone(s["mtp_accept_rate"])
        self.assertEqual(s["requests_processing"], 1)
        self.assertEqual(s["requests_deferred"], 0)
        self.assertIsNone(s["context_max"])  # 样本无 context 指标 -> N/A

    # 2) 正常 delta：state 更新、daily 累加、TPS / MTP 计算
    def test_normal_delta_daily_and_live(self):
        collector, db, set_text = self._make_collector(TEXT_A)
        asyncio.run(collector.collect_once())
        set_text(TEXT_B)
        asyncio.run(collector.collect_once())

        state = db.get_state()
        self.assertEqual(state["llamacpp:prompt_tokens_total"], 150.0)

        # 同一天：仍是同一行，delta 已累加进去
        daily = db.get_daily_usage()
        self.assertEqual(len(daily), 1)
        row = daily[0]
        self.assertEqual(row["prompt_tokens"], 50)
        self.assertEqual(row["cached_tokens"], 30)
        self.assertEqual(row["output_tokens"], 10)
        self.assertEqual(row["draft_tokens"], 5)
        self.assertEqual(row["accepted_tokens"], 3)
        self.assertAlmostEqual(row["prompt_seconds"], 0.5)
        self.assertAlmostEqual(row["predicted_seconds"], 1.0)

        samples = db.get_live_samples()
        self.assertEqual(len(samples), 2)
        s = samples[-1]
        self.assertEqual(s["prompt_delta"], 50)
        self.assertAlmostEqual(s["prompt_tps"], 100.0)      # 50 / 0.5
        self.assertAlmostEqual(s["decode_tps"], 10.0)       # 10 / 1.0
        self.assertAlmostEqual(s["mtp_accept_rate"], 60.0)  # 3 / 5 * 100

    # 3) llama-server Counter reset：每个 Counter 独立检测
    def test_counter_reset_detection(self):
        collector, db, set_text = self._make_collector(TEXT_A)
        asyncio.run(collector.collect_once())
        set_text(TEXT_B)
        asyncio.run(collector.collect_once())
        set_text(TEXT_C)  # prompt 150->30 重启；output 20->25 正常
        asyncio.run(collector.collect_once())

        state = db.get_state()
        self.assertEqual(state["llamacpp:prompt_tokens_total"], 30.0)
        self.assertEqual(state["llamacpp:tokens_predicted_total"], 25.0)

        s = db.get_live_samples()[-1]
        self.assertEqual(s["prompt_delta"], 30)   # 重启：delta = current
        self.assertEqual(s["output_delta"], 5)    # 正常：delta = current - previous
        self.assertAlmostEqual(s["prompt_tps"], 150.0)  # 30 / 0.2
        self.assertAlmostEqual(s["decode_tps"], 10.0)   # 5 / 0.5

    # 4) 程序自身重启：从 state 表恢复，不重新 baseline
    def test_program_restart_recovers_state(self):
        collector, db, set_text = self._make_collector(TEXT_A)
        asyncio.run(collector.collect_once())
        set_text(TEXT_B)
        asyncio.run(collector.collect_once())
        db.close()  # 模拟旧进程退出

        # 模拟新进程：同一 DB 文件，新的 Database + 新的 collector
        collector2, db2, _ = self._make_collector(TEXT_D)
        asyncio.run(collector2.collect_once())

        s = db2.get_live_samples()[-1]
        # 若重启后重新 baseline，prompt_delta 会是 0（错误）；
        # 正确恢复 state：prompt 150->80 变小，判定重启，delta = current = 80；
        # output 20->30 正常增长，delta = 10。
        self.assertEqual(s["prompt_delta"], 80)
        self.assertEqual(s["output_delta"], 10)

    # 5) 跨天统计：daily_usage 按本机系统日期分开累计
    def test_cross_day_daily_usage(self):
        collector, db, _ = self._make_collector()
        # 不走网络：直接调 persist_sample，注入日期与时刻
        collector.persist_sample(parse_metrics(TEXT_A), now=1767225600.0, daily_date="2026-01-01")
        collector.persist_sample(parse_metrics(TEXT_B), now=1767225660.0, daily_date="2026-01-01")
        collector.persist_sample(parse_metrics(TEXT_E), now=1767312000.0, daily_date="2026-01-02")

        daily = db.get_daily_usage()
        self.assertEqual([r["date"] for r in daily], ["2026-01-01", "2026-01-02"])
        d1, d2 = daily
        self.assertEqual(d1["prompt_tokens"], 50)   # A->B
        self.assertEqual(d1["output_tokens"], 10)
        self.assertAlmostEqual(d1["prompt_seconds"], 0.5)
        self.assertEqual(d2["prompt_tokens"], 30)   # B->E（跨天，从 B 的 state 继续，不是重新 baseline）
        self.assertEqual(d2["output_tokens"], 5)

        # 最近 1 天只返回 2026-01-02
        recent = db.get_daily_usage(days=1)
        self.assertEqual([r["date"] for r in recent], ["2026-01-02"])

    # 6) 字段缺失：delta=None、不崩溃、state 保留最后一次值
    def test_missing_fields_no_crash(self):
        collector, db, set_text = self._make_collector(TEXT_A)
        asyncio.run(collector.collect_once())
        # 第二轮 spec-decode 字段缺失（例如不同版本/配置的服务器）
        set_text(sample(draft=None, accepted=None))
        snapshot = asyncio.run(collector.collect_once())
        self.assertTrue(snapshot["online"])
        self.assertIsNone(snapshot["draft_tokens_total"])
        self.assertIsNone(snapshot["accepted_tokens_total"])

        s = db.get_live_samples()[-1]
        self.assertIsNone(s["mtp_accept_rate"])
        self.assertIsNone(s["prompt_tps"])  # prompt 与秒数都没增长，分母 0

        state = db.get_state()
        # 缺失的 Counter 不更新 state，保留最后一次保存的值
        self.assertEqual(state["llamacpp:spec_decode_num_draft_tokens_total"], 5.0)
        self.assertEqual(state["llamacpp:spec_decode_num_accepted_tokens_total"], 3.0)

    # 7) 超过 48 小时的 live_samples 被删除
    def test_old_live_samples_deleted(self):
        collector, db, _ = self._make_collector()
        now = 1767225600.0  # 固定时刻
        conn = db._connect()
        # 超过 48h 的旧行：3天 / 50小时 / 48小时+1分钟 -> 全部应删除
        for offset in (3 * 86400, 50 * 3600, 48 * 3600 + 60):
            conn.execute(
                "INSERT INTO live_samples(timestamp, prompt_delta) VALUES(?, 1)",
                (int(now) - offset,),
            )
        # 恰好 48h 的行不算“超过” -> 保留
        conn.execute(
            "INSERT INTO live_samples(timestamp, prompt_delta) VALUES(?, 1)",
            (int(now) - 48 * 3600,),
        )
        conn.commit()

        collector.persist_sample(parse_metrics(TEXT_A), now=now)

        remaining = [row["timestamp"] for row in db.get_live_samples(hours=None)]
        self.assertEqual(remaining, [int(now) - 48 * 3600, int(now)])

    # 8) 事务：任一步失败整体回滚，不留半写入状态
    def test_apply_sample_rolls_back_on_error(self):
        collector, db, _ = self._make_collector()
        collector.persist_sample(parse_metrics(TEXT_A))
        collector.persist_sample(parse_metrics(TEXT_B))
        state_before = db.get_state()
        daily_before = db.get_daily_usage()
        conn = db._connect()
        live_before = conn.execute("SELECT COUNT(*) FROM live_samples").fetchone()[0]

        # live_row 的 timestamp 绑定一个 list -> sqlite3 在第三步（live 插入）抛错，
        # 前两步（state / daily）必须整体回滚。
        # 注意：不同 Python 版本对“不支持的绑定类型”抛 InterfaceError 或 ProgrammingError，
        # 两者都是 sqlite3.Error。
        bad_row = {
            "timestamp": [1],
            "prompt_delta": 1,
            "cached_delta": 1,
            "output_delta": 1,
            "prompt_tps": 1.0,
            "decode_tps": 1.0,
            "requests_processing": 0,
            "requests_deferred": 0,
            "context_max": None,
            "mtp_accept_rate": 1.0,
        }
        with self.assertRaises(sqlite3.Error):
            db.apply_sample({"x_metric": 1.0}, bad_row, "2026-01-01", {"prompt_tokens": 5}, now=1767225600.0)

        self.assertEqual(db.get_state(), state_before)
        self.assertEqual(db.get_daily_usage(), daily_before)
        live_after = conn.execute("SELECT COUNT(*) FROM live_samples").fetchone()[0]
        self.assertEqual(live_after, live_before)

    # 9) 默认 DB 路径（config database.path 为空）-> %LOCALAPPDATA%\LlamaMonitor
    def test_database_path_defaults_under_localappdata(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.tmp / "lad")}):
            p = make_config().database_path
        self.assertEqual(p.name, "monitor.db")
        self.assertIn("LlamaMonitor", p.parts)
        self.assertEqual(p.parent, Path(self.tmp / "lad" / "LlamaMonitor"))

    def test_database_creates_parent_dirs(self):
        db = Database(self.tmp / "nested" / "deep" / "m.db")
        self._dbs.append(db)
        db.get_state()
        self.assertTrue((self.tmp / "nested" / "deep" / "m.db").exists())


class AuditDbRegressionTests(unittest.TestCase):
    """Phase 14 审计回归（AUDIT-DB-002 / AUDIT-SEC-016 / AUDIT-ASYNC-006）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._dbs: list[Database] = []

    def tearDown(self):
        for db in self._dbs:
            try:
                db.close()
            except Exception:
                pass
        self._tmp.cleanup()

    def _db(self, name="a.db") -> Database:
        db = Database(self.tmp / name, wal=False)
        self._dbs.append(db)
        return db

    def test_backup_history_row_cap(self):
        """AUDIT-DB-002：backup_history 行数上限 1000（原无保留，备份盘故障时
        每 60s 一行无界增长）。"""
        from db import BACKUP_HISTORY_MAX_ROWS

        db = self._db()
        for i in range(BACKUP_HISTORY_MAX_ROWS + 50):
            db.record_backup("automatic", f"auto_{i}.db", 10, True, success=True, now=1700000000 + i)
        count = db._connect().execute("SELECT COUNT(*) FROM backup_history").fetchone()[0]
        self.assertLessEqual(count, BACKUP_HISTORY_MAX_ROWS)
        # 保留的是**最近**的（按 id 降序取上限内的行）
        newest = db.get_backup_history(limit=1)
        self.assertEqual(newest[0]["path"], f"auto_{BACKUP_HISTORY_MAX_ROWS + 49}.db")

    def test_readonly_file_uri_encodes_path(self):
        """AUDIT-SEC-016：只读 file URI 对空格/中文/特殊字符 percent-encode
        （原裸路径遇空格即解析失败，降级保护路径本身打不开库）。"""
        from db import _readonly_file_uri

        # 空格
        uri = _readonly_file_uri("C:/Users/test user/Llama Monitor/m.db")
        self.assertIn("%20", uri)
        self.assertNotIn(" ", uri)
        self.assertTrue(uri.endswith("?mode=ro"))
        # 中文（非 ASCII 必须编码，sqlite 按 percent-encoding 解析）
        uri2 = _readonly_file_uri("C:/数据/监控/m.db")
        self.assertNotIn("数据", uri2)
        self.assertIn("%E6", uri2)
        # 普通路径保持不变（盘符 + 正斜杠形式）
        uri3 = _readonly_file_uri("C:/plain/path/m.db")
        self.assertEqual(uri3, "file:///C:/plain/path/m.db?mode=ro")

    def test_readonly_uri_opens_real_db(self):
        """URI 构建端到端：用 _readonly_file_uri 真的打开一个 WAL 库能读到数据。"""
        from db import _readonly_file_uri

        db = self._db("uri.db")
        db.record_event("test_event", "info", "test", {"x": 1}, now=1700000000)
        uri = _readonly_file_uri(str(self.tmp / "uri.db"))
        conn = sqlite3.connect(uri, uri=True)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM monitor_events WHERE event_type='test_event'"
            ).fetchone()[0]
            self.assertEqual(n, 1)
        finally:
            conn.close()

    def test_get_gaps_date_window_includes_end_of_day(self):
        """AUDIT-ASYNC-006：按日窗口 end_day = 次日本地 00:00（DST 回拨日 25h 不漏）；
        无 DST 时区下至少保证"当天最后 1 秒结束的缺口"被包含。"""
        db = self._db()
        from db import local_date
        import time as _time

        # 取一个固定日期（今天），构造"当天 23:59:59 结束"的缺口
        today = local_date()
        from datetime import datetime as _dt
        day_start = _dt.strptime(today, "%Y-%m-%d").timestamp()
        # 无 DST 时区：end = day_start + 86399（当天内）；两种实现都应包含
        db.record_gap(day_start + 86000.0, day_start + 86399.0, "llama", "server_offline",
                      now=day_start + 86399.0)
        rows = db.get_gaps(date=today)
        self.assertEqual(len(rows), 1)
        # 次日的缺口不属于今天
        db.record_gap(day_start + 86400.0, day_start + 86460.0, "llama", "server_offline",
                      now=day_start + 86460.0)
        self.assertEqual(len(db.get_gaps(date=today)), 1)


if __name__ == "__main__":
    unittest.main()
