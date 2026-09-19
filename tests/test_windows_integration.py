"""
Phase 10 测试：windows_integration（单实例 Mutex / 唤醒 Event / 文件夹映射 / autostart 命令）。

Windows-only 的部分用 unittest.skipUnless(sys.platform == "win32")（项目用 stdlib
unittest 而非 pytest，等价 pytest.mark.skipif）。测试用独立 Mutex/Event 名，
不与正在运行的 LlamaMonitor 实例串扰。

运行：python -m unittest discover -s tests
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from windows_integration import (  # noqa: E402
    IS_WINDOWS,
    SHOW_WINDOW_EVENT_NAME,
    SHUTDOWN_EVENT_NAME,
    FOLDER_TARGETS,
    AutostartManager,
    ShowWindowListener,
    ShutdownListener,
    SingleInstance,
    autostart_command,
    folder_path,
    notify_shutdown,
    notify_show_window,
    request_shutdown,
)

IS_WIN = unittest.skipIf(not IS_WINDOWS, "Windows-only")


class IsFrozenTests(unittest.TestCase):
    def test_frozen_flag(self):
        # 开发模式（unittest 直接跑）：sys.frozen 不存在 -> False
        import config

        self.assertFalse(config.is_frozen())


class FolderMappingTests(unittest.TestCase):
    """LOCALAPPDATA 指向临时目录：验证固定映射 + 目录自动创建。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = self._tmp.name

    def tearDown(self):
        if self._old is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = self._old
        self._tmp.cleanup()

    def test_mapping_keys(self):
        self.assertEqual(set(FOLDER_TARGETS), {"data", "logs", "backups", "updates"})

    def test_folder_paths_created_under_data_dir(self):
        p_logs = folder_path("logs")
        self.assertEqual(p_logs.parent.name, "LlamaMonitor")
        self.assertTrue(p_logs.is_dir())  # 不存在则创建
        self.assertEqual(folder_path("data").name, "LlamaMonitor")
        self.assertEqual(folder_path("backups").name, "backups")

    def test_invalid_target_rejected(self):
        with self.assertRaises(ValueError):
            folder_path("C:\\Windows")
        with self.assertRaises(ValueError):
            folder_path("data\\..\\..")


class AutostartCommandTests(unittest.TestCase):
    def test_command_quoted_with_background(self):
        exe = r"C:\Program Files\LlamaMonitor\LlamaMonitor.exe"
        cmd = autostart_command(exe)
        self.assertIn('"', cmd)
        self.assertTrue(cmd.endswith("--background"))
        self.assertIn(exe.replace("\\", "\\\\") if False else exe, cmd)

    def test_command_always_quoted(self):
        cmd = autostart_command(r"C:\no space\app.exe")
        self.assertTrue(cmd.startswith('"'))
        self.assertTrue(cmd.endswith('" --background'))

    def test_manager_supported_only_when_frozen_and_win(self):
        # dev 模式（非 frozen）：即使传了 exe 也不支持写注册表
        if IS_WINDOWS:
            m = AutostartManager(r"C:\x\LlamaMonitor.exe")
            self.assertFalse(m.supported())  # 测试进程非 frozen


class _FakeKey:
    def __init__(self, values):
        self.values = values

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeRegistry:
    """winreg 替身：dict 值存储 + FileNotFoundError 语义（与真实 winreg 一致）。"""

    def __init__(self, values):
        self.values = values  # {name: str}
        self.HKEY_CURRENT_USER = "HKCU"
        self.KEY_READ = 1
        self.KEY_SET_VALUE = 2
        self.REG_SZ = 1
        self.deleted = []

    def OpenKey(self, root, path, reserved, sam):
        return _FakeKey(self.values)

    def QueryValueEx(self, key, name):
        if name not in key.values:
            raise FileNotFoundError()
        return key.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, type_, value):
        key.values[name] = value

    def DeleteValue(self, key, name):
        if name not in key.values:
            raise FileNotFoundError()
        del key.values[name]
        self.deleted.append(name)

    def CloseKey(self, key):
        # 真实 winreg：关闭 OpenKey 返回的句柄（fake 中为 no-op 记账）
        self.closed = getattr(self, "closed", 0) + 1


class AutostartManagerMockedRegistryTests(unittest.TestCase):
    """注册表访问封装成可注入 registry（mock winreg），验证 enable/disable/stale。"""

    def setUp(self):
        # 强制 supported：把 is_frozen 置为 True（整个测试期间生效）
        import windows_integration as wi

        self._wi = wi
        self._old_frozen = wi.is_frozen
        wi.is_frozen = lambda: True

    def tearDown(self):
        self._wi.is_frozen = self._old_frozen

    def _mgr(self, values, exe=r"C:\app\LlamaMonitor.exe"):
        reg = _FakeRegistry(values)
        m = AutostartManager(exe, registry=reg)
        return m, reg

    def test_enable_writes_expected_command(self):
        m, reg = self._mgr({})
        m.enable()
        self.assertIn("LlamaMonitor", reg.values)
        self.assertEqual(reg.values["LlamaMonitor"], '"C:\\app\\LlamaMonitor.exe" --background')

    def test_disable_removes_value_and_is_idempotent(self):
        m, reg = self._mgr({"LlamaMonitor": "old"})
        self.assertTrue(m.disable())
        self.assertNotIn("LlamaMonitor", reg.values)
        self.assertTrue(m.disable())  # 再删一次（不存在）也成功

    def test_stale_detection(self):
        m, reg = self._mgr({"LlamaMonitor": '"C:\\old\\LlamaMonitor.exe" --background'})
        state = m.get_state()
        self.assertTrue(state["enabled"])
        self.assertTrue(state["stale"])
        self.assertEqual(state["command"], '"C:\\old\\LlamaMonitor.exe" --background')
        self.assertEqual(state["expected_command"], '"C:\\app\\LlamaMonitor.exe" --background')

    def test_enabled_not_stale_when_matching(self):
        m, reg = self._mgr({"LlamaMonitor": '"C:\\app\\LlamaMonitor.exe" --background'})
        state = m.get_state()
        self.assertTrue(state["enabled"])
        self.assertFalse(state["stale"])

    def test_not_enabled_when_absent(self):
        m, reg = self._mgr({})
        state = m.get_state()
        self.assertFalse(state["enabled"])
        self.assertFalse(state["stale"])
        self.assertEqual(state["command"], "")

    def test_set_enabled_toggle(self):
        m, reg = self._mgr({})
        st = m.set_enabled(True)
        self.assertTrue(st["enabled"])
        self.assertFalse(st["stale"])
        st = m.set_enabled(False)
        self.assertFalse(st["enabled"])


@IS_WIN
class SingleInstanceRealMutexTests(unittest.TestCase):
    """真实 Named Mutex：acquire -> 第二实例 False -> release -> 再 acquire True。"""

    def _name(self):
        return f"Local\\LlamaMonitor.Test.{os.getpid()}.{id(self)}"

    def test_acquire_release_reacquire(self):
        # 真实场景中第一/第二实例是两个独立进程（独立线程）。mutex 可重入，
        # 所以这里把"实例 A"放到独立线程持有，主线程扮演"实例 B"。
        import threading

        a = SingleInstance(self._name())
        acquired = threading.Event()
        release = threading.Event()

        def instance_a():
            if a.acquire():
                acquired.set()
            release.wait(5)
            a.release()

        t = threading.Thread(target=instance_a)
        t.start()
        self.assertTrue(acquired.wait(3))  # 实例 A 持有 mutex

        b = SingleInstance(self._name())
        self.assertFalse(b.acquire())  # 实例 B 被阻塞（已有实例）
        self.assertFalse(b.acquired)

        release.set()
        t.join()
        self.assertFalse(a.acquired)

        self.assertTrue(b.acquire())  # A 释放后 B 可获取
        self.assertTrue(b.acquired)
        b.release()

    def test_double_acquire_same_instance(self):
        a = SingleInstance(self._name())
        self.assertTrue(a.acquire())
        self.assertTrue(a.acquire())  # 幂等：同一实例重复 acquire
        a.release()


@IS_WIN
class ShowWindowEventRealTests(unittest.TestCase):
    """真实 Named Event：第二实例 SetEvent -> 第一实例监听线程收到。"""

    def _name(self):
        return f"Local\\LlamaMonitor.TestEvt.{os.getpid()}.{id(self)}"

    def test_listener_receives_signal(self):
        import threading

        got = threading.Event()
        listener = ShowWindowListener(on_signal=lambda: got.set(), name=self._name())
        self.assertTrue(listener.start())
        try:
            self.assertTrue(notify_show_window(name=self._name()))
            self.assertTrue(got.wait(timeout=3.0))
        finally:
            listener.stop()

    def test_stop_idempotent(self):
        listener = ShowWindowListener(on_signal=lambda: None, name=self._name())
        listener.start()
        listener.stop()
        listener.stop()  # 再次 stop 不报错


if IS_WINDOWS is False:
    class SingleInstanceNonWinTests(unittest.TestCase):
        def test_acquire_release(self):
            SingleInstance._non_win_taken = False
            a = SingleInstance("x")
            b = SingleInstance("x")
            self.assertTrue(a.acquire())
            self.assertFalse(b.acquire())
            a.release()
            self.assertTrue(b.acquire())
            b.release()


@IS_WIN
class ShutdownEventRealTests(unittest.TestCase):
    """Phase 12：Shutdown Named Event + request_shutdown（--shutdown-existing 核心）。"""

    def _names(self):
        return (
            f"Local\\LlamaMonitor.TestShut.M.{os.getpid()}.{id(self)}",
            f"Local\\LlamaMonitor.TestShut.E.{os.getpid()}.{id(self)}",
        )

    def test_listener_receives_shutdown_signal(self):
        import threading

        mutex_name, event_name = self._names()
        got = threading.Event()
        listener = ShutdownListener(on_signal=lambda: got.set(), name=event_name)
        self.assertTrue(listener.start())
        try:
            self.assertTrue(notify_shutdown(name=event_name))
            self.assertTrue(got.wait(timeout=3.0))
        finally:
            listener.stop()

    def test_request_shutdown_no_instance_returns_zero(self):
        mutex_name, event_name = self._names()
        self.assertEqual(request_shutdown(timeout=1.0, mutex_name=mutex_name, event_name=event_name), 0)

    def test_request_shutdown_success_after_graceful_release(self):
        """
        模拟跨进程场景：实例 A 在**独立线程**持有 Mutex（真实是另一进程），
        并监听 Shutdown -> 收到信号后优雅释放 -> request_shutdown 返回 0。
        （Mutex 所有权按线程：若 A 与调用方同线程会重入误判，故 A 必须独立线程。）
        """
        import threading

        mutex_name, event_name = self._names()
        instance_a = SingleInstance(mutex_name)
        released = threading.Event()
        hold = threading.Event()

        def instance_a_thread():
            self.assertTrue(instance_a.acquire())  # 在独立线程持有
            hold.set()
            # 收到 Shutdown 信号（listener 回调设置 release_now）-> 优雅释放
            release_now.wait(5)
            instance_a.release()

        release_now = threading.Event()

        def instance_a_shutdown():
            release_now.set()
            released.set()

        holder = threading.Thread(target=instance_a_thread)
        holder.start()
        self.assertTrue(hold.wait(3))  # 线程 A 已持有 Mutex

        listener = ShutdownListener(on_signal=instance_a_shutdown, name=event_name)
        self.assertTrue(listener.start())
        try:
            code = request_shutdown(timeout=5.0, mutex_name=mutex_name, event_name=event_name)
        finally:
            listener.stop()
        holder.join(timeout=3)
        self.assertTrue(released.is_set(), "实例 A 应收到 Shutdown 信号")
        self.assertEqual(code, 0, "优雅释放后 request_shutdown 应返回 0")

    def test_request_shutdown_timeout_returns_one(self):
        """实例 A（独立线程）持有 Mutex 但不响应 Shutdown -> 超时返回 1。"""
        import threading

        mutex_name, event_name = self._names()
        instance_a = SingleInstance(mutex_name)
        hold = threading.Event()
        release_now = threading.Event()

        def instance_a_thread():
            self.assertTrue(instance_a.acquire())  # 独立线程持有
            hold.set()
            release_now.wait(5)
            instance_a.release()

        holder = threading.Thread(target=instance_a_thread)
        holder.start()
        self.assertTrue(hold.wait(3))  # 线程 A 已持有 Mutex（不监听 Shutdown）
        try:
            code = request_shutdown(timeout=1.0, mutex_name=mutex_name, event_name=event_name)
        finally:
            release_now.set()
            holder.join(timeout=3)
        self.assertEqual(code, 1, "A 不响应时 request_shutdown 应超时返回 1")


if __name__ == "__main__":
    unittest.main()
