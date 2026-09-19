"""
Phase 10 测试：单实例完整流程（spec 70）。

模拟"第二实例双击 EXE"：
- 实例 A：acquire Mutex 成功 + 启动 ShowWindow 监听 -> 把 'show' 命令入队
  （等价 desktop 里 dispatcher.request('show')，不启动真实 Collector/WebView）；
- 实例 B：acquire 失败 -> notify_show_window -> 正常退出（不启动 Collector）；
- 断言：A 的队列收到 'show'，B 的 acquire 为 False，A 仍然持有 Mutex。

运行：python -m unittest discover -s tests
"""

import os
import queue
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from windows_integration import (  # noqa: E402
    IS_WINDOWS,
    ShowWindowListener,
    SingleInstance,
    notify_show_window,
)

IS_WIN = unittest.skipIf(not IS_WINDOWS, "Windows-only（真实 Named Mutex/Event）")


@IS_WIN
class SecondInstanceFlowTests(unittest.TestCase):
    def _names(self):
        tag = f"{os.getpid()}.{id(self)}.{time.monotonic_ns()}"
        return f"Local\\LlamaMonitor.Test.Mutex.{tag}", f"Local\\LlamaMonitor.Test.Show.{tag}"

    def test_second_instance_wakes_first_and_exits(self):
        import threading

        mutex_name, event_name = self._names()

        # ---- 实例 A（已在运行）---- 独立线程持有 mutex（模拟独立进程；
        # mutex 可重入，若与 B 同线程会误判）。
        a = SingleInstance(mutex_name)
        ui_queue: "queue.Queue[str]" = queue.Queue()
        a_started = threading.Event()
        a_stop = threading.Event()
        listener_holder: dict = {}

        def instance_a():
            if not a.acquire():
                return
            listener = ShowWindowListener(
                on_signal=lambda: ui_queue.put("show"), name=event_name
            )
            listener_holder["l"] = listener
            listener.start()
            a_started.set()
            a_stop.wait(10)
            listener.stop()
            a.release()

        t = threading.Thread(target=instance_a)
        t.start()
        self.assertTrue(a_started.wait(3))  # A 运行中并持有 mutex

        try:
            # ---- 实例 B（用户再次双击 EXE，主线程扮演） ----
            b = SingleInstance(mutex_name)
            self.assertFalse(b.acquire())  # B 被阻塞，不启动 Collector/DB/FastAPI
            self.assertTrue(notify_show_window(name=event_name))  # B 发唤醒信号

            # ---- A 收到 show 命令 ----
            cmd = ui_queue.get(timeout=3.0)
            self.assertEqual(cmd, "show")
            # A 仍唯一持有 Mutex：主线程新探针被阻塞
            c = SingleInstance(mutex_name)
            self.assertFalse(c.acquire())
            self.assertFalse(b.acquired)
        finally:
            a_stop.set()
            t.join()

    def test_first_instance_alone_no_signal_no_command(self):
        mutex_name, event_name = self._names()
        a = SingleInstance(mutex_name)
        self.assertTrue(a.acquire())
        ui_queue: "queue.Queue[str]" = queue.Queue()
        listener = ShowWindowListener(on_signal=lambda: ui_queue.put("show"), name=event_name)
        listener.start()
        try:
            time.sleep(0.6)  # 无第二实例：不应有命令
            self.assertTrue(ui_queue.empty())
        finally:
            listener.stop()
            a.release()

    def test_multiple_signals_multiple_commands(self):
        # 监听线程在多次信号之间持续存活、每轮都能处理。
        # （手动重置 Event 语义：同一等待周期内的多次 SetEvent 合并为一个命令——
        # show 命令幂等，实际无影响；这里逐条等完再发下一条，验证循环不退出。）
        mutex_name, event_name = self._names()
        a = SingleInstance(mutex_name)
        self.assertTrue(a.acquire())
        ui_queue: "queue.Queue[str]" = queue.Queue()
        listener = ShowWindowListener(on_signal=lambda: ui_queue.put("show"), name=event_name)
        listener.start()
        try:
            for _ in range(3):
                self.assertTrue(notify_show_window(name=event_name))
                self.assertEqual(ui_queue.get(timeout=3.0), "show")
        finally:
            listener.stop()
            a.release()

    def test_mutex_released_on_release_immediately_reusable(self):
        mutex_name, _ = self._names()
        a = SingleInstance(mutex_name)
        b = SingleInstance(mutex_name)
        self.assertTrue(a.acquire())
        a.release()
        # 释放后立即可以 acquire（崩溃时 OS 也会自动释放）
        self.assertTrue(b.acquire())
        b.release()


if __name__ == "__main__":
    unittest.main()
