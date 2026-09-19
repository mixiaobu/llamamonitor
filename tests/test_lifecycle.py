"""
Phase 10 测试：app_lifecycle（状态机 / UI 命令分发器）+ Token state 退出安全
（spec 74/75：daily_usage 与 state 必须同事务提交；退出/崩溃后不重复不遗漏）。

统计完整性关键测试：
    baseline prompt = 1,000,000
    round 2: prompt = 1,001,000（delta 1000 已提交）
    在此 transaction 附近执行 shutdown（关闭/崩溃模拟）
    round 3: prompt = 1,001,500（delta 500）
    最终新增总量必须 = 1500（不能 500 / 2500 / 1,001,500）

运行：python -m unittest discover -s tests
"""

import asyncio
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app_lifecycle import AppLifecycle, UiCommandDispatcher  # noqa: E402
from collector import MetricsCollector  # noqa: E402
from configutil import make_config  # noqa: E402
from db import Database, local_date  # noqa: E402
from metrics_parser import parse_metrics  # noqa: E402
from test_persistence import sample  # noqa: E402


class AppLifecycleTests(unittest.TestCase):
    def test_initial_running(self):
        lc = AppLifecycle()
        self.assertEqual(lc.state, AppLifecycle.RUNNING)
        self.assertIsNone(lc.stop_reason)

    def test_request_stop_idempotent_only_once_accepted(self):
        lc = AppLifecycle()
        self.assertTrue(lc.request_stop("tray exit"))
        self.assertEqual(lc.state, AppLifecycle.STOPPING)
        self.assertEqual(lc.stop_reason, "tray exit")
        # 重复请求（双击 Exit）：直接忽略
        self.assertFalse(lc.request_stop("api exit"))
        self.assertFalse(lc.request_stop("again"))
        self.assertEqual(lc.state, AppLifecycle.STOPPING)
        lc.mark_stopped()
        self.assertEqual(lc.state, AppLifecycle.STOPPED)
        self.assertFalse(lc.request_stop("late"))

    def test_uptime_monotonic_based(self):
        lc = AppLifecycle()
        t0 = lc.uptime_seconds()
        self.assertGreaterEqual(t0, 0)
        time.sleep(0.05)
        t1 = lc.uptime_seconds()
        self.assertGreaterEqual(t1, t0)

    def test_mark_stopped_from_running(self):
        lc = AppLifecycle()
        lc.mark_stopped()  # 无 request_stop 直接停（兜底路径）
        self.assertEqual(lc.state, AppLifecycle.STOPPED)


class UiCommandDispatcherTests(unittest.TestCase):
    def test_commands_run_serially_in_order(self):
        events = []
        max_concurrency = {"v": 0}
        running = {"v": 0}

        def tracked(name):
            def _fn():
                running["v"] += 1
                max_concurrency["v"] = max(max_concurrency["v"], running["v"])
                events.append(name)
                time.sleep(0.01)
                running["v"] -= 1
            return _fn

        d = UiCommandDispatcher({"show": tracked("show"), "hide": tracked("hide"), "exit": tracked("exit")})
        d.start()
        d.request("show")
        d.request("hide")
        d.request("exit")
        d.stop(timeout=5.0)
        self.assertEqual(events, ["show", "hide", "exit"])
        self.assertEqual(max_concurrency["v"], 1)  # 串行执行，不并发

    def test_concurrent_requests_all_processed(self):
        counter = {"n": 0}
        lock = threading.Lock()

        def inc():
            with lock:
                counter["n"] += 1

        d = UiCommandDispatcher({"show": inc})
        d.start()
        threads = [threading.Thread(target=lambda: d.request("show")) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        time.sleep(0.5)
        d.stop(timeout=5.0)
        self.assertEqual(counter["n"], 50)

    def test_handler_exception_does_not_kill_loop(self):
        events = []

        def boom():
            events.append("boom")
            raise RuntimeError("intentional")

        d = UiCommandDispatcher({"show": boom, "hide": lambda: events.append("hide")})
        d.start()
        d.request("show")
        d.request("hide")
        time.sleep(0.3)
        d.stop(timeout=5.0)
        self.assertEqual(events, ["boom", "hide"])  # 异常后循环继续

    def test_unknown_command_ignored(self):
        d = UiCommandDispatcher({"show": lambda: None})
        d.start()
        d.request("nonsense")
        time.sleep(0.1)
        d.stop(timeout=5.0)  # 不抛异常、不挂起

    def test_stop_then_request_dropped(self):
        d = UiCommandDispatcher({"show": lambda: None})
        d.start()
        d.stop(timeout=5.0)
        d.request("show")  # 之后入队静默丢弃
        self.assertFalse(d.running)


# ---------------------------------------------------------------------------
# Token state 退出安全（spec 74 / 75）
# ---------------------------------------------------------------------------

def _run_round(db_path: Path, text: str) -> Database:
    """模拟一个进程的一次采集轮次：新 Database + 新 Collector，采集后返回 db。"""
    db = Database(db_path)
    collector = MetricsCollector(make_config(), db)
    parsed = parse_metrics(text)

    async def _fetch():
        return parsed

    collector._fetch_parsed = _fetch
    asyncio.run(collector.collect_once())
    return db


def _today_prompt(db: Database) -> int:
    rows = {r["date"]: r for r in db.get_daily_usage()}
    row = rows.get(local_date())
    return int(row["prompt_tokens"] or 0) if row else 0


class StateExitSafetyTests(unittest.TestCase):
    """
    关键性质：每轮采集的 daily_usage 累加与 state baseline 更新在**同一事务**提交，
    因此任何退出（优雅关闭 / 崩溃 / 断电）发生在提交之后时，重启后：
    - 不会重复计入已提交的 delta（state 已推进）；
    - 不会漏计（daily 与 state 同步）。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "state_safety.db"
        self._dbs: list[Database] = []

    def tearDown(self):
        for db in self._dbs:
            try:
                db.close()
            except Exception:
                pass
        self._tmp.cleanup()

    def _text(self, prompt):
        return sample(prompt=prompt, prompt_sec=1.0)

    def test_restart_after_committed_delta_graceful(self):
        # round 1: baseline（不产生 delta）
        db1 = _run_round(self.db_path, self._text(1_000_000))
        self._dbs.append(db1)
        self.assertEqual(_today_prompt(db1), 0)

        # round 2: delta 1000 —— daily_usage 与 state 同事务提交
        db1 = _run_round(self.db_path, self._text(1_001_000))
        self._dbs.append(db1)
        self.assertEqual(_today_prompt(db1), 1000)

        # 优雅 shutdown：关闭数据库（事务早已提交）
        db1.close()
        self._dbs.pop()

        # 重启：round 3 —— delta 500（从持久化 baseline 1,001,000 继续）
        db2 = _run_round(self.db_path, self._text(1_001_500))
        self._dbs.append(db2)
        self.assertEqual(_today_prompt(db2), 1500)  # 不是 500 / 2500 / 1,001,500

    def test_crash_after_committed_delta(self):
        # 与优雅路径相同的两轮
        db1 = _run_round(self.db_path, self._text(1_000_000))
        db2 = _run_round(self.db_path, self._text(1_001_000))
        self._dbs.extend([db1, db2])

        # 崩溃模拟：不执行 db.close() 清理，直接释放底层连接
        # （等价进程死亡：已提交事务在 WAL/主库中，句柄被 OS 回收）
        for db in (db2, db1):
            self._dbs.remove(db)
            db._conn.close()

        # 重启：round 3
        db3 = _run_round(self.db_path, self._text(1_001_500))
        self._dbs.append(db3)
        self.assertEqual(_today_prompt(db3), 1500)

    def test_state_and_daily_committed_together(self):
        """state baseline 与 daily 增量必须同时存在（不存在只提交一半的情况）。"""
        db = _run_round(self.db_path, self._text(1_000_000))
        self._dbs.append(db)
        db = _run_round(self.db_path, self._text(1_001_000))
        self._dbs.append(db)
        state = db.get_state()
        prompt_state = state.get("llamacpp:prompt_tokens_total")
        self.assertEqual(prompt_state, 1_001_000.0)
        self.assertEqual(_today_prompt(db), 1000)
        # 两个值对应同一轮提交：state 推进到 1,001,000 当且仅当 daily 含 1000

    def test_logical_tokens_matches_dashboard_definition(self):
        """get_today_logical_tokens = prompt+cached+output（与 Dashboard 同口径）。"""
        db1 = _run_round(
            self.db_path, sample(prompt=1_000_000, cached=500, prompt_sec=1.0, output=700)
        )
        db2 = _run_round(
            self.db_path, sample(prompt=1_001_000, cached=1000, prompt_sec=1.0, output=1400)
        )
        self._dbs.extend([db1, db2])
        # delta: prompt +1000, cached +500, output +700
        self.assertEqual(db2.get_today_logical_tokens(), 1000 + 500 + 700)


if __name__ == "__main__":
    unittest.main()
