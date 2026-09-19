"""
Phase 13 测试：更新 API 端点（§28/§29/§40/§41）。

- 5 个 /api/update/* 端点全部 loopback-only（远程 403）；
- GET /api/update/status 返回 §29 状态超集；
- POST check/download/install 并发 -> 409 UPDATE_BUSY；
- 状态机错误：IDLE 时 download/install -> 400 NOT_READY；
  非 DOWNLOADING 时 cancel -> 409 NOT_DOWNLOADING；
- 完整 API 流程：check -> download -> install（Popen 被 mock，request_exit 被记录）。

运行：python -m unittest discover -s tests
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import MetricsCollector  # noqa: E402
from configutil import make_config, make_loaded  # noqa: E402
from db import Database  # noqa: E402
from server import build_app  # noqa: E402
from test_local_only_api import _with_client  # noqa: E402
from update_util import FakeGithub, make_service  # noqa: E402
from version import __version__  # noqa: E402

UPDATE_ENDPOINTS = [
    ("GET", "/api/update/status", None),
    ("POST", "/api/update/check", None),
    ("POST", "/api/update/download", None),
    ("POST", "/api/update/install", None),
    ("POST", "/api/update/cancel", None),
]


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.updates = self.tmp / "updates"
        self.backups = self.tmp / "backups"
        self.db = Database(self.tmp / "api_test.db")
        self.config_file = self.tmp / "config.json"
        self.config_file.write_text(json.dumps({}), encoding="utf-8")
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

    def _svc(self, **kw) -> "object":
        fake = kw.pop("fake", None) or FakeGithub(
            repo="owner/repo", current_version=__version__)
        return make_service(
            fake, db=self.db,
            config_path=lambda: self.config_file,
            updates_dir=self.updates,
            backups_dir=self.backups, **kw)

    def _client(self, host: str = "127.0.0.1", update_service=None):
        app = build_app(
            self.db, MetricsCollector(make_config(), self.db),
            loaded=make_loaded(make_config(), self.tmp),
            update_service=update_service,
        )
        app = _with_client(app, host)
        client = TestClient(app)
        client.__enter__()
        self._clients.append(client)
        return client


class UpdateApiLoopbackTests(_Base):
    def test_loopback_all_endpoints_reachable(self):
        svc = self._svc()
        client = self._client(update_service=svc)
        r = client.get("/api/update/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        # §29 状态超集字段
        for key in ("state", "current_version", "installation_mode",
                    "available_version", "downloaded_bytes", "total_bytes",
                    "progress_percent", "last_check", "error", "ready_to_install"):
            self.assertIn(key, body, f"status 缺少字段 {key}")
        self.assertEqual(body["current_version"], __version__)

    def test_remote_all_endpoints_403(self):
        """§28：5 个端点远程一律 403。"""
        svc = self._svc()
        client = self._client(host="192.168.1.50", update_service=svc)
        for method, path, _ in UPDATE_ENDPOINTS:
            r = client.request(method, path)
            self.assertEqual(r.status_code, 403, f"{method} {path}")

    def test_check_not_configured(self):
        """repository 未配置 -> 200 + 明确错误（不发网络请求）。"""
        svc = self._svc(repository="")
        client = self._client(update_service=svc)
        r = client.post("/api/update/check")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "IDLE")
        self.assertIn("not configured", r.json()["error"])


class UpdateApiStateMachineTests(_Base):
    def test_download_before_check_400(self):
        svc = self._svc()
        client = self._client(update_service=svc)
        r = client.post("/api/update/download")
        self.assertEqual(r.status_code, 400)
        self.assertIn("NOT_READY", json.dumps(r.json()))

    def test_install_before_download_400(self):
        """只 Check 未 Download -> install 400。"""
        svc = self._svc()
        client = self._client(update_service=svc)
        r = client.post("/api/update/check")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "UPDATE_AVAILABLE")
        r = client.post("/api/update/install")
        self.assertEqual(r.status_code, 400)

    def test_cancel_not_downloading_409(self):
        svc = self._svc()
        client = self._client(update_service=svc)
        r = client.post("/api/update/cancel")
        self.assertEqual(r.status_code, 409)

    def test_busy_state_returns_409(self):
        """§41：服务处于忙状态（CHECKING/DOWNLOADING/...）时，新请求 -> 409 UPDATE_BUSY。

        用直接置 busy 状态的方式触发 409 路径（确定性、无 TestClient 并发死锁）。
        """
        svc = self._svc()
        client = self._client(update_service=svc)
        for busy in ("CHECKING", "DOWNLOADING", "VERIFYING", "INSTALLING"):
            svc._state = busy
            for method, path in (("POST", "/api/update/check"),
                                 ("POST", "/api/update/download"),
                                 ("POST", "/api/update/install")):
                r = client.request(method, path)
                self.assertEqual(r.status_code, 409, f"{path} @ {busy}")
                self.assertIn("UPDATE_BUSY", json.dumps(r.json()))


class UpdateApiFullFlowTests(_Base):
    def test_check_download_install_via_api(self):
        """完整流程：check -> download -> install（Popen mock，request_exit 记录）。"""
        svc = self._svc(mode="installed",
                        request_exit=lambda: exit_calls.append(1) or True)
        client = self._client(update_service=svc)

        r = client.post("/api/update/check")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "UPDATE_AVAILABLE")
        avail = r.json()["available_version"]
        self.assertTrue(avail)

        r = client.post("/api/update/download")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "READY_TO_INSTALL")
        self.assertEqual(r.json()["progress_percent"], 100.0)

        exit_calls.clear()
        with mock.patch("update_service.subprocess.Popen") as popen:
            r = client.post("/api/update/install")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["state"], "INSTALLING")
            self.assertEqual(exit_calls, [1])
        self.assertEqual(popen.call_count, 1)
        args = popen.call_args[0][0]
        self.assertEqual(args[1:], ["/SILENT", "/NORESTART", "/APPUPDATE"])

        r = client.get("/api/update/status")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "INSTALLING")


# 模块级（test 内引用）
exit_calls: list = []


if __name__ == "__main__":
    unittest.main()
