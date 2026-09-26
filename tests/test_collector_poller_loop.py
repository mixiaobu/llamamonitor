"""
AUDIT-1.1.1 GAP-006：Collector 常驻采集循环的健壮性回归。

原实现 run() 的 try 只包 collect_once()——out() 抛异常（stdout 关闭）或 sleep
被非 cancel 异常打断都会**杀掉整个采集循环**（采集永久停摆、任务消失）。
1.1.1 放宽 try 范围覆盖整轮：任何单轮异常记日志后继续下一轮，仅 task 被 cancel
（停机）才退出循环。

本测试直接驱动 collector.run()（不拉起真实网络/DB），验证：
1. collect_once 抛异常 -> 循环存活（offline_snapshot + 继续）；
2. out() 抛异常 -> 循环存活（下一轮仍输出）；
3. sleep 被非 cancel 异常打断 -> 循环存活；
4. cancel -> 干净退出（CancelledError 传播，循环停止）。

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
    def test_collect_once_exception_does_not_kill_loop(self):
        """collect_once 抛异常 -> 用 offline_snapshot 兜底并继续下一轮。"""
        collector = _mk_collector()
        calls = {"n": 0}

        async def _collect_once_boom():
            calls["n"] += 1
            raise RuntimeError("collect boom")

        collector.collect_once = _collect_once_boom  # type: ignore
        outputs = []

        async def _drive():
            task = asyncio.create_task(collector.run(out=outputs.append))
            # 给循环跑 ~5 轮（interval=0.01s）
            await asyncio.sleep(0.06)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(_drive())
        # 多轮都执行了（循环没在第一轮异常后停摆）
        self.assertGreaterEqual(calls["n"], 3)
        # 每轮都有输出（offline_snapshot 兜底）
        self.assertGreaterEqual(len(outputs), 3)

    def test_out_exception_does_not_kill_loop(self):
        """out() 抛异常（如 stdout 关闭）-> 循环存活，下一轮仍调用 collect。"""
        collector = _mk_collector()
        collect_calls = {"n": 0}

        async def _ok_collect():
            collect_calls["n"] += 1
            return offline_snapshot()

        collector.collect_once = _ok_collect  # type: ignore
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
        # out 第一轮抛异常后，循环没死：后续轮仍调用 collect（>=3 轮）
        self.assertGreaterEqual(collect_calls["n"], 3)
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
