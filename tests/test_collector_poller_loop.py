"""
AUDIT-1.1.1 GAP-006：Collector 常驻采集循环的健壮性回归（1.3.3 架构版）。

1.3.3 起 run() = 首轮 collect_once + start()（HOT 1s deadline 循环 + WARM
落库循环）。GAP-006 语义不变：**任何单轮异常（HOT 轮 / 输出）记日志后继续
下一轮，仅 task 被 cancel（停机）才退出循环**；run 被 cancel 时同步停止
后台任务（不泄漏）。

本测试直接驱动 collector.run()（不拉起真实网络/DB），验证：
1. hot_round 抛异常 -> 循环存活（每轮仍用最近快照输出 + 继续）；
2. out() 抛异常 -> 循环存活（下一轮仍调用 HOT 轮）；
3. cancel -> 干净退出（CancelledError 传播，后台任务停止）。

运行：python -X utf8 -m unittest discover -s tests
"""

import asyncio
import unittest

from clock import FakeClock
from collector import MetricsCollector, offline_snapshot
from configutil import make_config


def _mk_collector():
    collector = MetricsCollector(make_config(poll_interval=0.01), clock=FakeClock(start_wall=1e9, start_mono=0.0))
    return collector


class CollectorPollerLoopTests(unittest.TestCase):
    def _mk_fast_collector(self):
        """HOT 间隔压到 0.01s（测试时间尺度；生产 HOT_INTERVAL=1.0）。

        用真实 monotonic 时钟：1.3.3 DeadlineScheduler 的 deadline 锚定依赖
        monotonic 随时间推进；FakeClock 的 monotonic 不随真实时间走，
        不适合这类"真实时间跑若干轮"的循环测试。
        """
        collector = MetricsCollector(make_config(poll_interval=0.01))
        collector.hot_interval = 0.01
        return collector

    def test_hot_round_exception_does_not_kill_loop(self):
        """hot_round 抛异常 -> 循环存活（每轮仍用最近快照输出 + 继续）。"""
        collector = self._mk_fast_collector()
        calls = {"n": 0}

        async def _ok_collect():
            s = offline_snapshot()
            collector.last_snapshot = s
            return s

        async def _hot_boom():
            calls["n"] += 1
            raise RuntimeError("hot boom")

        collector.collect_once = _ok_collect  # type: ignore
        collector.hot_round = _hot_boom  # type: ignore
        outputs = []

        async def _drive():
            task = asyncio.create_task(collector.run(out=outputs.append))
            # 给 HOT 循环跑 ~5 轮（hot_interval=0.01s）
            await asyncio.sleep(0.06)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(_drive())
        # 多轮都执行了（循环没在第一轮异常后停摆）
        self.assertGreaterEqual(calls["n"], 3)
        # 每轮都有输出（最近快照兜底）
        self.assertGreaterEqual(len(outputs), 3)

    def test_out_exception_does_not_kill_loop(self):
        """out() 抛异常（如 stdout 关闭）-> 循环存活，下一轮仍调用 HOT 轮。"""
        collector = self._mk_fast_collector()
        hot_calls = {"n": 0}

        async def _ok_collect():
            s = offline_snapshot()
            collector.last_snapshot = s
            return s

        async def _ok_hot():
            hot_calls["n"] += 1
            return offline_snapshot()

        collector.collect_once = _ok_collect  # type: ignore
        collector.hot_round = _ok_hot  # type: ignore
        out_calls = {"n": 0}

        def _boom_out(text):
            out_calls["n"] += 1
            if out_calls["n"] == 1:
                raise ValueError("stdout closed")  # 第一轮 out 抛异常

        async def _drive():
            task = asyncio.create_task(collector.run(out=_boom_out))
            await asyncio.sleep(0.06)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(_drive())
        # out 第一轮抛异常后，循环没死：后续轮仍调用 HOT 轮（>=3 轮）
        self.assertGreaterEqual(hot_calls["n"], 3)
        self.assertGreaterEqual(out_calls["n"], 3)

    def test_cancel_cleans_up_loop(self):
        """task 被 cancel -> 循环干净退出（CancelledError 传播，不再空转）。"""
        collector = _mk_collector()

        async def _ok_collect():
            return offline_snapshot()

        collector.collect_once = _ok_collect  # type: ignore

        async def _drive():
            task = asyncio.create_task(collector.run(out=lambda *_a, **_k: None))
            await asyncio.sleep(0.03)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            # cancel 后 task 已结束
            self.assertTrue(task.done())

        asyncio.run(_drive())


if __name__ == "__main__":
    unittest.main()
