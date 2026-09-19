"""
Phase 10 测试：本地管理 API 安全加固（spec 46~50）+ /api/app/* 端点。

- require_loopback：修改类 API 只允许 127.0.0.1 / ::1（用实际 socket 地址，
  不信任 X-Forwarded-For）；只读 API 远程可读；
- /api/app/integration、/api/app/autostart、/api/app/open-folder、/api/app/exit；
- 远程客户端用 ASGI 中间件改写 scope["client"] 模拟（TestClient 默认 client 是
  loopback，正好覆盖"本地"路径）。

运行：python -m unittest discover -s tests
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app_lifecycle import AppIntegrationState
from collector import MetricsCollector
from configutil import make_config, make_loaded
from db import Database
from metrics_parser import parse_metrics
from server import build_app
from test_persistence import TEXT_A


def _with_client(app, host):
    """ASGI 包装：显式设置客户端 socket 地址（starlette TestClient 默认是
    ('testclient', 50000)，不是真实地址，所以两种方向都要显式包装）。"""

    async def wrapper(scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope = dict(scope)
            scope["client"] = (host, 12345)
        await app(scope, receive, send)

    return wrapper


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db = Database(self.tmp / "local_only.db")
        self.collector = MetricsCollector(make_config(), self.db)
        parsed = parse_metrics(TEXT_A)

        async def _fetch():
            return parsed

        self.collector._fetch_parsed = _fetch
        self._clients: list[TestClient] = []

    def tearDown(self):
        for c in self._clients:
            try:
                c.__exit__(None, None, None)
            except Exception:
                pass
        try:
            self.db.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def _client(self, app_state=None, host="127.0.0.1"):
        """host = 模拟的客户端 socket 地址（127.0.0.1/::1 = 本地；其余 = 远程）。"""
        app = build_app(
            self.db, self.collector,
            loaded=make_loaded(make_config(), self.tmp),
            app_state=app_state,
        )
        app = _with_client(app, host)
        client = TestClient(app)
        client.__enter__()
        self._clients.append(client)
        return client


class LoopbackEnforcementTests(_Base):
    def test_loopback_can_put_config(self):
        client = self._client()
        # 合法 body：200；非法 body：400（总之不是 403）
        r = client.put("/api/config", json={"collector": {"poll_interval_seconds": 7}})
        self.assertNotEqual(r.status_code, 403)
        self.assertIn(r.status_code, (200, 400))

    def test_remote_put_config_403(self):
        client = self._client(host="192.168.1.50")
        r = client.put("/api/config", json={"collector": {"poll_interval_seconds": 7}})
        self.assertEqual(r.status_code, 403)

    def test_remote_x_forwarded_for_not_trusted(self):
        client = self._client(host="192.168.1.50")
        r = client.put(
            "/api/config",
            json={"collector": {"poll_interval_seconds": 7}},
            headers={"X-Forwarded-For": "127.0.0.1"},
        )
        self.assertEqual(r.status_code, 403)

    def test_remote_readonly_status_ok(self):
        client = self._client(host="192.168.1.50")
        r = client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        r = client.get("/api/summary")
        self.assertEqual(r.status_code, 200)

    def test_remote_mutation_endpoints_403(self):
        client = self._client(host="192.168.1.50")
        cases = [
            ("POST", "/api/data/clear-live", {"confirm": True}),
            ("POST", "/api/data/reset-statistics", {"confirm": "RESET"}),
            ("POST", "/api/data/backup", None),
            ("POST", "/api/config/test-connection", {"url": "http://127.0.0.1:9/metrics"}),
            ("POST", "/api/app/exit", None),
        ]
        for method, path, body in cases:
            r = client.request(method, path, json=body) if body is not None else client.request(method, path)
            self.assertEqual(r.status_code, 403, f"{method} {path}")

    def test_remote_local_info_endpoints_403(self):
        """AUDIT-SEC-001/002：/api/config（本地路径+llama 地址）与
        /api/app/integration（EXE 路径/autostart 注册表命令）是本地信息，
        远程客户端 -> 403（web.host=0.0.0.0 时不泄露给局域网）。"""
        client = self._client(host="192.168.1.50")
        self.assertEqual(client.get("/api/config").status_code, 403)
        self.assertEqual(client.get("/api/app/integration").status_code, 403)

    def test_loopback_ipv6_ok(self):
        client = self._client(host="::1")
        r = client.put("/api/config", json={"collector": {"poll_interval_seconds": 7}})
        self.assertNotEqual(r.status_code, 403)

    def test_loopback_clear_live_works(self):
        client = self._client()
        r = client.post("/api/data/clear-live", json={"confirm": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["success"])


class AppApiTests(_Base):
    def test_integration_degraded_without_app_state(self):
        client = self._client()
        r = client.get("/api/app/integration")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["tray_supported"], False)
        self.assertFalse(data["autostart"]["supported"])
        self.assertIn("platform", data)

    def test_integration_with_provider(self):
        st = AppIntegrationState(
            background=True,
            integration_provider=lambda: {
                "platform": "windows",
                "frozen": True,
                "tray_supported": True,
                "single_instance": True,
                "background": True,
                "executable": "C:\\x\\LlamaMonitor.exe",
                "app_data": "C:\\Users\\x\\AppData\\Local\\LlamaMonitor",
                "uptime_seconds": 42,
                "autostart": {"supported": True, "enabled": True, "stale": False,
                              "command": '"C:\\x\\LlamaMonitor.exe" --background',
                              "expected_command": '"C:\\x\\LlamaMonitor.exe" --background'},
            },
        )
        client = self._client(app_state=st)
        r = client.get("/api/app/integration")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["background"])
        self.assertTrue(r.json()["tray_supported"])

    def test_status_application_section(self):
        st = AppIntegrationState(background=True, tray_available=True)
        client = self._client(app_state=st)
        r = client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        app = r.json()["application"]
        self.assertTrue(app["background"])
        self.assertTrue(app["tray_available"])
        self.assertTrue(app["single_instance"])
        self.assertGreaterEqual(app["uptime_seconds"], 0)
        # 不暴露 Windows 内部句柄
        self.assertNotIn("mutex", str(r.json()).lower())

    def test_autostart_requires_bool(self):
        st = AppIntegrationState(set_autostart=lambda enabled: {"enabled": enabled})
        client = self._client(app_state=st)
        r = client.put("/api/app/autostart", json={"enabled": "yes"})
        self.assertEqual(r.status_code, 400)
        r = client.put("/api/app/autostart", json={})
        self.assertEqual(r.status_code, 400)

    def test_autostart_enable_and_disable(self):
        calls = []
        st = AppIntegrationState(
            set_autostart=lambda enabled: calls.append(enabled) or {"enabled": enabled, "stale": False}
        )
        client = self._client(app_state=st)
        r = client.put("/api/app/autostart", json={"enabled": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["success"])
        r = client.put("/api/app/autostart", json={"enabled": False})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(calls, [True, False])

    def test_autostart_unsupported_raises_400(self):
        def _boom(enabled):
            raise RuntimeError("autostart 不可用（需要 Windows + EXE 模式）")

        st = AppIntegrationState(set_autostart=_boom)
        client = self._client(app_state=st)
        r = client.put("/api/app/autostart", json={"enabled": True})
        self.assertEqual(r.status_code, 400)

    def test_autostart_without_app_state_503(self):
        client = self._client()
        r = client.put("/api/app/autostart", json={"enabled": True})
        self.assertEqual(r.status_code, 503)

    def test_open_folder_valid_targets(self):
        with mock.patch("server.open_folder") as m:
            m.return_value = r"C:\Users\x\AppData\Local\LlamaMonitor\logs"
            client = self._client()
            for target in ("data", "logs", "backups"):
                r = client.post("/api/app/open-folder", json={"target": target})
                self.assertEqual(r.status_code, 200, target)
                self.assertTrue(r.json()["success"])
            self.assertEqual(m.call_count, 3)

    def test_open_folder_invalid_target(self):
        with mock.patch("server.open_folder") as m:
            client = self._client()
            r = client.post("/api/app/open-folder", json={"target": "C:\\Windows"})
            self.assertEqual(r.status_code, 400)
            r = client.post("/api/app/open-folder", json={"target": "data\\..\\.."})
            self.assertEqual(r.status_code, 400)
            self.assertEqual(m.call_count, 0)

    def test_exit_with_app_state(self):
        exited = []
        st = AppIntegrationState(request_exit=lambda: exited.append(1) or True)
        client = self._client(app_state=st)
        r = client.post("/api/app/exit")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["success"])
        self.assertTrue(body["shutting_down"])
        self.assertEqual(exited, [1])  # 请求返回时 shutdown 已被调度

    def test_exit_without_app_state_503(self):
        client = self._client()
        r = client.post("/api/app/exit")
        self.assertEqual(r.status_code, 503)


if __name__ == "__main__":
    unittest.main()
