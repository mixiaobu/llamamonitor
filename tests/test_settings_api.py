"""
Phase 8 测试：Settings API（配置读取 / 默认值 / 校验更新 / 原子保存 / Test Connection）。

覆盖：
1. GET /api/config 完整形状（六段 + paths）
2. GET /api/config/defaults 与 config.py 默认值一致
3. 合法配置更新（changed 列表 / restart_required）
4. 非法配置拒绝（统一错误格式，文件不被修改）
5. 保存后原有未知字段保留
6. 请求中的未知字段被忽略（不写入文件）
7. 配置文件原子保存（写后是合法 JSON，无 .tmp 残留）
8. 只改 ui.theme -> restart_required=false
9. Test Connection mock 成功 / 失败 / 超时

测试不访问真实 llama-server、不触碰真实 %LOCALAPPDATA%（配置路径与数据库均指向临时目录）。
项目使用标准库 unittest：临时目录用 tempfile.TemporaryDirectory（等价 pytest tmp_path）。

在项目根目录运行：
    python -m unittest discover -s tests
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from fastapi.testclient import TestClient

import config as cfgmod
from collector import MetricsCollector
from configutil import loopback_app, make_config
from db import Database
from server import build_app


def _write_config(path: Path, data: dict) -> None:
    cfgmod.atomic_write_json(path, data)


class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = ""):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    """httpx.AsyncClient 替身：记录请求 URL，返回固定响应或抛固定异常。"""

    last_url = None

    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        _FakeClient.last_url = url
        if self._exc is not None:
            raise self._exc
        return self._resp


class ConfigApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg_file = self.tmp / "config.json"
        self._dbs: list[Database] = []
        # 默认配置落盘（load_config 语义：文件存在且合法）
        _write_config(self.cfg_file, cfgmod.get_default_config())

    def tearDown(self):
        for db in self._dbs:
            db.close()
        self._tmp.cleanup()

    def _start(self):
        from config import LoadedConfig

        cfg = make_config()
        db = Database(self.tmp / "settings_test.db")
        self._dbs.append(db)
        collector = MetricsCollector(cfg, db)
        loaded = LoadedConfig(
            config=cfg,
            path=self.cfg_file,
            loaded=True,
            using_defaults=False,
            has_errors=False,
        )
        app = build_app(db, collector, loaded)
        client = TestClient(loopback_app(app))  # 修改类 API 要求本地客户端
        client.__enter__()
        return client

    # 1) GET /api/config 完整形状
    def test_get_config_full_shape(self):
        client = self._start()
        try:
            data = client.get("/api/config").json()
            for section in ("llama_server", "collector", "web", "database", "ui", "logging", "paths"):
                self.assertIn(section, data, section)
            self.assertEqual(data["web"], {"host": "127.0.0.1", "port": 8765})
            self.assertEqual(data["database"], {"path": "", "wal": True})
            self.assertEqual(data["llama_server"]["metrics_url"], "http://127.0.0.1:9/metrics")
        finally:
            client.__exit__(None, None, None)

    # 2) defaults 端点与 config.py 一致
    def test_defaults_endpoint_matches_config_module(self):
        client = self._start()
        try:
            data = client.get("/api/config/defaults").json()
            self.assertEqual(data, cfgmod.get_default_config())
            self.assertEqual(data["web"]["port"], 8765)
        finally:
            client.__exit__(None, None, None)

    # 3) 合法更新：changed / restart_required / 文件更新 / 未改字段保留
    def test_put_valid_updates(self):
        client = self._start()
        try:
            r = client.put("/api/config", json={
                "llama_server": {"url": "http://192.168.1.100:9091"},
                "collector": {"poll_interval_seconds": 10},
            })
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertTrue(data["success"])
            self.assertEqual(data["changed"], ["llama_server.url", "collector.poll_interval_seconds"])
            self.assertTrue(data["restart_required"])

            saved = json.loads(self.cfg_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["llama_server"]["url"], "http://192.168.1.100:9091")
            self.assertEqual(saved["collector"]["poll_interval_seconds"], 10)
            # 未修改字段保留
            self.assertEqual(saved["web"]["port"], 8765)
            self.assertEqual(saved["ui"]["theme"], "dark")
        finally:
            client.__exit__(None, None, None)

    # 4) 非法字段：统一错误格式 + 文件不被修改
    def test_put_invalid_field_rejected(self):
        before = self.cfg_file.read_text(encoding="utf-8")
        client = self._start()
        try:
            r = client.put("/api/config", json={"web": {"port": "abc"}})
            self.assertEqual(r.status_code, 400)
            data = r.json()
            self.assertFalse(data["success"])
            self.assertEqual(data["error"]["code"], "CONFIG_VALIDATION_ERROR")
            self.assertEqual(data["error"]["field"], "web.port")
            self.assertIn("web.port", data["error"]["message"])
            self.assertEqual(self.cfg_file.read_text(encoding="utf-8"), before)  # 文件未动
        finally:
            client.__exit__(None, None, None)

    def test_put_invalid_port_out_of_range(self):
        client = self._start()
        try:
            r = client.put("/api/config", json={"web": {"port": 99999}})
            self.assertEqual(r.status_code, 400)
            self.assertEqual(r.json()["error"]["field"], "web.port")
        finally:
            client.__exit__(None, None, None)

    # 5) 原文件未知字段保留
    def test_put_preserves_existing_unknown_fields(self):
        _write_config(self.cfg_file, {
            **cfgmod.get_default_config(),
            "future_feature": 1,
        })
        client = self._start()
        try:
            r = client.put("/api/config", json={"collector": {"poll_interval_seconds": 7}})
            self.assertEqual(r.status_code, 200)
            saved = json.loads(self.cfg_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["future_feature"], 1)  # 保留
            self.assertEqual(saved["collector"]["poll_interval_seconds"], 7)
        finally:
            client.__exit__(None, None, None)

    # 6) 请求中的未知字段被忽略
    def test_put_ignores_unknown_request_fields(self):
        client = self._start()
        try:
            r = client.put("/api/config", json={
                "future_feature": 42,
                "web": {"port": 9000, "future": 2},
            })
            self.assertEqual(r.status_code, 200)
            saved = json.loads(self.cfg_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["web"]["port"], 9000)
            self.assertNotIn("future_feature", saved)
            self.assertNotIn("future", saved["web"])
        finally:
            client.__exit__(None, None, None)

    # 7) 原子保存：写后是合法 JSON，无 .tmp 残留
    def test_atomic_save_leaves_valid_json(self):
        client = self._start()
        try:
            client.put("/api/config", json={"collector": {"poll_interval_seconds": 6}})
            data = json.loads(self.cfg_file.read_text(encoding="utf-8"))  # 可解析
            self.assertEqual(data["collector"]["poll_interval_seconds"], 6)
            self.assertFalse((self.cfg_file.parent / "config.json.tmp").exists())
        finally:
            client.__exit__(None, None, None)

    # 8) 只改 ui.theme -> restart_required=false
    def test_theme_only_change_no_restart_required(self):
        client = self._start()
        try:
            r = client.put("/api/config", json={"ui": {"theme": "light"}})
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertEqual(data["changed"], ["ui.theme"])
            self.assertFalse(data["restart_required"])
        finally:
            client.__exit__(None, None, None)

    # 9) Test Connection（mock httpx，不访问真实网络）
    def test_test_connection_success(self):
        client = self._start()
        try:
            fake = _FakeClient(resp=_FakeResponse(200, "llamacpp:prompt_tokens_total 5\n# TYPE x counter\n"))
            with mock.patch.object(httpx, "AsyncClient", return_value=fake):
                r = client.post("/api/config/test-connection", json={
                    "url": "http://127.0.0.1:9091",
                    "metrics_path": "/metrics",
                    "timeout_seconds": 3,
                })
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertTrue(data["success"])
            self.assertEqual(data["http_status"], 200)
            self.assertTrue(data["metrics_detected"])
            self.assertIsInstance(data["latency_ms"], float)
            self.assertEqual(_FakeClient.last_url, "http://127.0.0.1:9091/metrics")
        finally:
            client.__exit__(None, None, None)

    def test_test_connection_refused(self):
        client = self._start()
        try:
            fake = _FakeClient(exc=httpx.ConnectError("refused"))
            with mock.patch.object(httpx, "AsyncClient", return_value=fake):
                r = client.post("/api/config/test-connection", json={
                    "url": "http://127.0.0.1:9",
                    "metrics_path": "/metrics",
                    "timeout_seconds": 1,
                })
            data = r.json()
            self.assertFalse(data["success"])
            self.assertEqual(data["error"], "Connection refused")
        finally:
            client.__exit__(None, None, None)

    def test_test_connection_timeout(self):
        client = self._start()
        try:
            fake = _FakeClient(exc=httpx.TimeoutException("slow"))
            with mock.patch.object(httpx, "AsyncClient", return_value=fake):
                r = client.post("/api/config/test-connection", json={
                    "url": "http://127.0.0.1:9091",
                    "metrics_path": "/metrics",
                    "timeout_seconds": 2,
                })
            data = r.json()
            self.assertFalse(data["success"])
            self.assertIn("Timeout", data["error"])
        finally:
            client.__exit__(None, None, None)

    def test_test_connection_non_200(self):
        client = self._start()
        try:
            fake = _FakeClient(resp=_FakeResponse(404, "not found"))
            with mock.patch.object(httpx, "AsyncClient", return_value=fake):
                r = client.post("/api/config/test-connection", json={
                    "url": "http://127.0.0.1:9091",
                    "metrics_path": "/nope",
                    "timeout_seconds": 3,
                })
            data = r.json()
            self.assertFalse(data["success"])
            self.assertEqual(data["http_status"], 404)
        finally:
            client.__exit__(None, None, None)

    def test_test_connection_invalid_params(self):
        client = self._start()
        try:
            r = client.post("/api/config/test-connection", json={
                "url": "ftp://bad",
                "metrics_path": "/metrics",
                "timeout_seconds": 3,
            })
            data = r.json()
            self.assertFalse(data["success"])
            self.assertIn("无效的 URL", data["error"])
        finally:
            client.__exit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
