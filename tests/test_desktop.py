"""
Phase 5/10 测试：桌面入口层（desktop.py）。

覆盖：
- wait_for_ready（真实 uvicorn 线程启动 / 超时）
- run_uvicorn_in_thread / stop_uvicorn（无残留线程、SQLite 已关）
- main() 端到端（注入假 webview 模块 + 假托盘/单实例/监听，不开真实 GUI）：
  * 正常启动 -> 窗口参数正确 -> 点 X 隐藏到托盘（closing 返回 False）
    -> 托盘 Exit -> 优雅关闭（DB 已提交、无残留线程、端口释放）
  * pywebview 失败 -> 浏览器回退 -> 托盘 Exit 退出
- main() 端口已被另一个 LlamaMonitor 占用：浏览器打开 + 对话框、不接管、已有进程不受影响

在项目根目录运行：
    python -m unittest discover -s tests
"""

import os
import sqlite3
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import httpx
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

import config as cfgmod
import desktop
from collector import MetricsCollector
from configutil import make_config, make_loaded
from db import Database
from metrics_parser import parse_metrics
from server import build_app

from test_persistence import TEXT_A


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# 测试替身：假 webview / 托盘 / 单实例 / ShowWindow 监听（隔离真实 GUI 与 Win32 对象）
# ---------------------------------------------------------------------------

class _ClosingSignal:
    """pywebview window.events.closing 的替身：支持 `closing += handler`。"""

    def __init__(self):
        self._handlers = []

    def __iadd__(self, handler):
        self._handlers.append(handler)
        return self

    def call(self, window):
        for h in self._handlers:
            result = h(window)
            if result is False:
                return False  # 关闭被取消
        return None


class _FakeWindow:
    def __init__(self, title, url, **kwargs):
        self.title = title
        self.url = url
        self.kwargs = kwargs
        self.on_top = False
        self.destroyed = False
        self.hidden = False
        self.shown = False
        self.events = types.SimpleNamespace(closing=_ClosingSignal())

    def destroy(self):
        self.destroyed = True

    def show(self):
        self.shown = True

    def hide(self):
        self.hidden = True

    def restore(self):
        pass

    def minimize(self):
        pass


class _FakeTray:
    def __init__(self, icon_path, commands, status_provider):
        self.available = True
        self.reason = ""
        self.commands = commands
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self, timeout=3.0):
        self.stopped = True

    def notify(self, message):
        pass


class _FakeSingle:
    def __init__(self, name="Local\\LlamaMonitor.SingleInstance"):
        self.acquired = False

    def acquire(self):
        self.acquired = True
        return True

    def release(self):
        self.acquired = False


class _FakeListener:
    def __init__(self, on_signal, name="Local\\LlamaMonitor.ShowWindow"):
        self.on_signal = on_signal

    def start(self):
        return True

    def stop(self, timeout=2.0):
        pass


def _make_fake_webview(calls, on_start=None, fail_create=False):
    mod = types.ModuleType("webview")

    def create_window(title, url, **kwargs):
        if fail_create:
            raise RuntimeError("WebView2 runtime missing")
        w = _FakeWindow(title, url, **kwargs)
        calls["window"] = w
        calls["create"] = (title, url, kwargs)
        return w

    def start():
        calls["start"] = True
        if on_start:
            on_start()

    mod.create_window = create_window
    mod.start = start
    return mod


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._servers = []  # (server, thread)，防止失败用例泄漏
        self.tray_instances = []
        self.webview_calls = {}
        self._patches = []

    def tearDown(self):
        for server, thread in self._servers:
            if not thread.is_alive():
                continue
            server.should_exit = True
            thread.join(timeout=5)
        for p in self._patches:
            p.stop()
        cfgmod.reset_logging()
        self._tmp.cleanup()

    def _patch_fakes(self, webview_module=None):
        """把真实 GUI/Win32 依赖替换为测试替身（记录到 self._patches）。"""
        if webview_module is not None:
            self._patches.append(mock.patch.dict(sys.modules, {"webview": webview_module}))
        self._patches.append(mock.patch.object(desktop, "TrayManager", self._make_tray))
        self._patches.append(mock.patch.object(desktop, "SingleInstance", _FakeSingle))
        self._patches.append(mock.patch.object(desktop, "ShowWindowListener", _FakeListener))
        for p in self._patches:
            p.start()

    def _make_tray(self, icon_path=None, commands=None, status_provider=None) -> _FakeTray:
        t = _FakeTray(icon_path, commands or {}, status_provider or (lambda: {}))
        self.tray_instances.append(t)
        return t

    def _make_app(self, name="t.db", text=TEXT_A):
        db = Database(self.tmp / name)
        collector = MetricsCollector(make_config(), db)
        parsed = parse_metrics(text)

        async def _fetch_parsed():
            return parsed

        collector._fetch_parsed = _fetch_parsed
        return db, collector, build_app(db, collector)

    def _start_server(self, app, name="t.db"):
        port = _free_port()
        server, thread = desktop.run_uvicorn_in_thread(app, "127.0.0.1", port)
        self._servers.append((server, thread))
        self.assertTrue(
            desktop.wait_for_ready(f"http://127.0.0.1:{port}", timeout=15, poll=0.1),
            "uvicorn 线程启动后 API 应在限时内就绪",
        )
        return port

    def _wait_http(self, url: str, timeout: float = 15.0) -> None:
        """轮询任意 URL 直到 200（用于没有 /api/status 的假 metrics 服务）。

        trust_env=False：轮询的是本机回环的假服务，绝不应走开发机的系统代理
        （RC-004：死系统代理会让这里 15s 全超时，误报"服务未就绪"——与生产
        同一根因，测试基础设施也要代理鲁棒）。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if httpx.get(url, timeout=1.0, trust_env=False).status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(0.1)
        self.fail(f"服务未在限时内就绪: {url}")

    # ------------------------------------------------------------------
    # 基础工具（与 Phase 5 相同）
    # ------------------------------------------------------------------

    def test_wait_for_ready_and_shutdown_closes_db(self):
        db, collector, app = self._make_app()
        self._start_server(app)
        # 就绪 = lifespan 首次采集完成 -> baseline 已写库
        conn = sqlite3.connect(str(db.path))
        names = {r[0] for r in conn.execute("SELECT metric_name FROM state")}
        conn.close()
        self.assertIn("llamacpp:prompt_tokens_total", names)
        self.assertIsNotNone(db._conn)
        server, thread = self._servers[-1]
        desktop.stop_uvicorn(server, thread, timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(db._conn)  # lifespan 关闭时 SQLite 已关

    def test_wait_for_ready_timeout(self):
        port = _free_port()  # 无监听
        t0 = time.time()
        self.assertFalse(desktop.wait_for_ready(f"http://127.0.0.1:{port}", timeout=1.0, poll=0.1))
        self.assertLess(time.time() - t0, 3.0)

    def test_ready_timeout_accommodates_post_reboot_load(self):
        # RC-002：系统重启后负载高（开机自启任务、GPU 驱动重新初始化）会拖慢
        # uvicorn 事件循环，/api/status 可能 30s 内不返回 200，导致 autostart
        # 实例误判"API 未就绪"而退出（用户需手动重启）。就绪超时必须足够大
        # （>= 60s）容纳重启后负载。
        self.assertGreaterEqual(desktop.READY_TIMEOUT_SECONDS, 60.0)

    def test_no_leftover_uvicorn_threads(self):
        db, collector, app = self._make_app(name="threads.db")
        self._start_server(app)
        server, thread = self._servers[-1]
        desktop.stop_uvicorn(server, thread, timeout=10)
        leftovers = [t for t in threading.enumerate() if t.name == "llamamonitor-uvicorn" and t.is_alive()]
        self.assertEqual(leftovers, [])

    # ------------------------------------------------------------------
    # Phase 10：main() 端到端（假窗口）
    # ------------------------------------------------------------------

    def _start_fake_metrics(self) -> str:
        metrics_port = _free_port()
        metrics_app = FastAPI()

        @metrics_app.get("/metrics")
        async def _fake_metrics():
            return PlainTextResponse(TEXT_A, media_type="text/plain")

        m_server, m_thread = desktop.run_uvicorn_in_thread(metrics_app, "127.0.0.1", metrics_port)
        self._servers.append((m_server, m_thread))
        self._wait_http(f"http://127.0.0.1:{metrics_port}/metrics")
        return f"http://127.0.0.1:{metrics_port}"

    def _run_main(self, port, metrics_url, db_file, argv=None):
        cfg = make_config(url=metrics_url)
        cfg.web.port = port
        loaded = make_loaded(cfg, self.tmp)
        argv = argv or ["desktop.py", "--db", str(db_file)]
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.tmp / "lad")}), \
             mock.patch.object(sys, "argv", argv):
            return desktop.main(loaded=loaded)

    def test_main_close_window_hides_to_tray_then_exit(self):
        """点 X -> 隐藏到托盘（监控继续）；托盘 Exit -> 优雅关闭。"""
        metrics_url = self._start_fake_metrics()
        port = _free_port()
        db_file = self.tmp / "main.db"

        def on_start():
            w = self.webview_calls["window"]
            # 用户点 X（应用仍在 RUNNING）：closing 应返回 False（取消关闭）
            self.webview_calls["close_result"] = w.events.closing.call(w)
            if self.webview_calls["close_result"] is False:
                # 应用继续运行；随后用户点托盘 Exit
                self.tray_instances[0].commands["exit"]()

        self._patch_fakes(_make_fake_webview(self.webview_calls, on_start=on_start))
        rc = self._run_main(port, metrics_url, db_file)
        self.assertEqual(rc, 0)

        # 窗口参数
        title, url, kwargs = self.webview_calls["create"]
        self.assertEqual(title, desktop.WINDOW_TITLE)
        self.assertEqual(url, f"http://127.0.0.1:{port}/")
        self.assertEqual(kwargs["width"], desktop.WINDOW_WIDTH)
        self.assertEqual(kwargs["height"], desktop.WINDOW_HEIGHT)
        self.assertEqual(kwargs["min_size"], desktop.WINDOW_MIN_SIZE)
        self.assertFalse(kwargs["hidden"])  # 前台模式

        # 点 X：被取消（返回 False）且窗口被隐藏（不是销毁）
        self.assertIs(self.webview_calls["close_result"], False)
        w = self.webview_calls["window"]
        self.assertTrue(w.hidden)
        self.assertTrue(w.destroyed)  # 最终托盘 Exit 时销毁

        # 托盘启动过且已停止
        self.assertTrue(self.tray_instances[0].started)
        self.assertTrue(self.tray_instances[0].stopped)

        # SQLite 已正常提交：baseline state 存在
        conn = sqlite3.connect(db_file)
        try:
            names = {r[0] for r in conn.execute("SELECT metric_name FROM state")}
        finally:
            conn.close()
        self.assertIn("llamacpp:prompt_tokens_total", names)
        self.assertGreaterEqual(len(names), 8)

    def test_tray_commands_accept_pystray_two_arg_signature(self):
        """
        回归：pystray 以 (icon, menu_item) 两个参数调用菜单 action——
        左键默认项（Menu.__call__）与右键菜单（_handler）两条路径都是
        action(icon, item)。零参 lambda 会抛 TypeError 且被 pystray 内部
        吞掉，表现为"菜单点了没反应"。这里按 pystray 的真实调用约定逐个验证。
        """
        metrics_url = self._start_fake_metrics()
        port = _free_port()
        db_file = self.tmp / "tray2arg.db"

        def on_start():
            cmds = self.tray_instances[0].commands
            icon, item = object(), object()  # pystray 调用约定：action(icon, item)
            for key in ("open", "open_data", "open_logs", "autostart_toggle"):
                cmds[key](icon, item)  # 必须不抛异常
            cmds["exit"](icon, item)   # 最后：触发优雅关闭

        # open_folder 走真实 os.startfile 会弹资源管理器：测试中 mock 掉
        # （由 _patch_fakes 统一 start/stop）
        self._patches.append(mock.patch.object(desktop, "open_folder", lambda target: str(self.tmp / target)))
        self._patch_fakes(_make_fake_webview(self.webview_calls, on_start=on_start))
        rc = self._run_main(port, metrics_url, db_file)
        self.assertEqual(rc, 0)

        # "open" 经由 dispatcher 执行 show（dispatcher.stop 保证命令排空后返回）
        self.assertTrue(self.webview_calls["window"].shown)
        self.assertTrue(self.tray_instances[0].stopped)

        # 停掉假 metrics 服务，再检查端口释放与无残留线程
        m_server, m_thread = self._servers.pop()
        desktop.stop_uvicorn(m_server, m_thread, timeout=10)
        self.assertFalse(desktop._port_in_use("127.0.0.1", port))
        leftovers = [t for t in threading.enumerate() if t.name == "llamamonitor-uvicorn" and t.is_alive()]
        self.assertEqual(leftovers, [])

    def test_main_background_mode_creates_hidden_window(self):
        """--background：窗口以 hidden=True 创建（不闪一下），其余流程相同。"""
        metrics_url = self._start_fake_metrics()
        port = _free_port()
        db_file = self.tmp / "bg.db"

        def on_start():
            # 后台模式：直接通过托盘 Exit 退出
            self.tray_instances[0].commands["exit"]()

        self._patch_fakes(_make_fake_webview(self.webview_calls, on_start=on_start))
        rc = self._run_main(port, metrics_url, db_file, argv=["desktop.py", "--background", "--db", str(db_file)])
        self.assertEqual(rc, 0)
        _, _, kwargs = self.webview_calls["create"]
        self.assertTrue(kwargs["hidden"])  # 后台隐藏创建

    def test_main_webview_failure_falls_back_to_browser(self):
        """pywebview 创建窗口失败 -> 浏览器回退 -> 托盘 Exit 退出。"""
        metrics_url = self._start_fake_metrics()
        port = _free_port()
        db_file = self.tmp / "fallback.db"
        browser_urls = []

        def on_open(url):
            browser_urls.append(url)
            # 用户在托盘点 Exit（fallback 路径的退出方式）
            self.tray_instances[0].commands["exit"]()

        # 托盘必须先于 webview 创建好（main 的真实顺序），这里确保实例存在
        def on_start():
            pass

        self._patch_fakes(_make_fake_webview(self.webview_calls, on_start=on_start, fail_create=True))
        with mock.patch.object(desktop.webbrowser, "open", side_effect=on_open):
            rc = self._run_main(port, metrics_url, db_file)
        self.assertEqual(rc, 0)
        self.assertEqual(browser_urls, [f"http://127.0.0.1:{port}/"])

    def test_main_port_occupied_by_existing_llamamonitor(self):
        # 已有 LlamaMonitor 在跑：不接管，浏览器打开，对话框确认后退出，已有进程不受影响
        db, collector, app = self._make_app(name="existing.db")
        port = self._start_server(app)

        def on_start():
            raise AssertionError("端口被占用时不应创建窗口")

        self._patch_fakes(_make_fake_webview(self.webview_calls, on_start=on_start))
        cfg = make_config()
        cfg.web.port = port
        loaded = make_loaded(cfg, self.tmp)
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.tmp / "lad")}), \
             mock.patch.object(desktop.webbrowser, "open") as opened, \
             mock.patch.object(desktop, "_message_box") as mb, \
             mock.patch.object(sys, "argv", ["desktop.py", "--db", str(self.tmp / "x.db")]):
            rc = desktop.main(loaded=loaded)
        self.assertEqual(rc, 0)
        opened.assert_called_once_with(f"http://127.0.0.1:{port}/")
        self.assertTrue(mb.called)  # 阻塞对话框给出退出路径
        # 已有服务仍在运行（未被接管/关闭）
        server, thread = self._servers[-1]
        self.assertFalse(server.should_exit)
        self.assertTrue(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
