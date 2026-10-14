"""server.py Strata 端点测试（1.3.0）：/api/status /api/strata/live /api/runtime
/api/config /api/overview 的 provider 感知行为。

在项目根目录运行：
    python -m unittest discover -s tests
"""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from config import LoadedConfig
from configutil import loopback_app, make_config
from db import Database
from fastapi.testclient import TestClient
from server import build_app
from strata_collector import StrataCollector

STRATA_FIX = Path(__file__).resolve().parent / "fixtures" / "strata"


def _strata_doc(name: str) -> str:
    return (STRATA_FIX / name).read_text(encoding="utf-8")


class StrataApiTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.cfg_file = self.tmp_path / "config.json"
        from config import atomic_write_json, get_default_config
        atomic_write_json(self.cfg_file, get_default_config())
        self.dbs: list[Database] = []

    def tearDown(self):
        for db in self.dbs:
            db.close()
        self.tmp.cleanup()

    def _start(self, provider_payloads: dict | None = None,
               strata: bool = True, provider: str = "strata"):
        from collector import MetricsCollector
        cfg = make_config()
        cfg.llama_server.provider = provider
        db = Database(self.tmp_path / f"strata_api_{provider}.db")
        self.dbs.append(db)
        if strata:
            collector = StrataCollector(cfg, db)
            if provider_payloads is None:
                provider_payloads = {
                    "/metrics": "metrics_idle.json",
                    "/status": "status_idle.json",
                    "/slots": "slots.json",
                    "/health": "health_ok.json",
                    "/props": "props.json",
                    "/v1/models": "models.json",
                }
            eps = {p: (lambda n=n: (_strata_doc(n), True)) for p, n in provider_payloads.items()}

            async def ft(p, l):
                f = eps.get(p)
                return f() if f else (None, False)

            async def fj(p, l):
                t, ok = await ft(p, l)
                if not ok or t is None:
                    return None, False
                try:
                    return json.loads(t), True
                except Exception:
                    return None, False

            collector._fetch_text = ft
            collector._fetch_json = fj
            # 首轮采集由 TestClient lifespan 执行（与生产路径一致；
            # DB 连接在 TestClient 线程创建，避免跨线程 SQLite 错误）
        else:
            collector = MetricsCollector(cfg, db)
        loaded = LoadedConfig(config=cfg, path=self.cfg_file, loaded=True,
                              using_defaults=False, has_errors=False)
        app = build_app(db, collector, loaded, runtime=None)
        client = TestClient(loopback_app(app))
        client.__enter__()
        return client, collector


class StatusEndpointTests(StrataApiTestBase):
    def test_status_has_provider_and_strata_block(self):
        client, _ = self._start()
        try:
            data = client.get("/api/status").json()
            self.assertEqual(data["provider"], "strata")
            self.assertEqual(data["provider_display"], "Strata")
            self.assertEqual(data["server_state"], "ready")
            self.assertEqual(data["server_state_label"], "就绪")
            self.assertIsNotNone(data.get("strata"))
            self.assertEqual(data["strata"]["model"], "qwen3.8-flash-next-unsloth-ud-q4_k_xl")
            # llama.cpp 专属字段不存在（runtime=None）
            self.assertNotIn("llama_model", data)
            self.assertNotIn("llama_slots", data)
        finally:
            client.__exit__(None, None, None)

    def test_status_busy_state(self):
        client, _ = self._start(provider_payloads={
            "/metrics": "metrics_generating.json",
            "/status": "status_busy.json",
            "/slots": "slots_busy.json",
            "/health": "health_ok.json",
            "/props": "props.json",
            "/v1/models": "models.json",
        })
        try:
            data = client.get("/api/status").json()
            self.assertEqual(data["server_state"], "busy")
            self.assertTrue(data["strata"]["busy"])
            self.assertEqual(data["strata"]["running"], 1)
            # 隐私边界：tail 文本不进 /api/status
            blob = json.dumps(data, ensure_ascii=False)
            self.assertNotIn("这是模型正在生成的实际文本", blob)
        finally:
            client.__exit__(None, None, None)


class StrataLiveEndpointTests(StrataApiTestBase):
    def test_strata_live_full_shape(self):
        client, _ = self._start()
        try:
            r = client.get("/api/strata/live")
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertEqual(data["provider"], "strata")
            self.assertEqual(data["server_local"], True)  # 127.0.0.1
            for key in ("state", "state_label", "live", "slots", "recent_requests",
                        "conversation_cache", "engine", "model", "capabilities"):
                self.assertIn(key, data, key)
            self.assertEqual(len(data["slots"]), 2)
            self.assertGreater(len(data["recent_requests"]), 0)
            self.assertFalse(data["conversation_cache"]["enabled"])
            self.assertEqual(data["engine"]["version"], "0.1.43.1")
            self.assertEqual(data["model"]["context_window"], 262144.0)
            # 隐私边界
            blob = json.dumps(data, ensure_ascii=False, default=str)
            self.assertNotIn("这是模型正在生成的实际文本", blob)
            self.assertNotIn("Jinja", blob)
            self.assertNotIn("C:\\Users\\mixiaobu\\Models", blob)
        finally:
            client.__exit__(None, None, None)

    def test_strata_live_404_for_llama_provider(self):
        client, _ = self._start(strata=False, provider="llamacpp")
        try:
            r = client.get("/api/strata/live")
            self.assertEqual(r.status_code, 404)
        finally:
            client.__exit__(None, None, None)


class RuntimeEndpointTests(StrataApiTestBase):
    def test_runtime_provider_and_capability_states(self):
        client, _ = self._start()
        try:
            data = client.get("/api/runtime").json()
            self.assertEqual(data["provider"], "strata")
            caps = data["capabilities"]
            self.assertEqual(caps.get("request_history"), "supported")
            self.assertEqual(caps.get("conversation_cache"), "supported")
            self.assertEqual(caps.get("draft_position_statistics"), "unsupported")
            self.assertIn("gpu", caps)
        finally:
            client.__exit__(None, None, None)


class ConfigEndpointTests(StrataApiTestBase):
    def test_config_get_masks_api_key(self):
        client, _ = self._start()
        try:
            data = client.get("/api/config").json()
            ls = data["llama_server"]
            self.assertIn("provider", ls)
            self.assertIn("api_key_set", ls)
            self.assertNotIn("api_key", ls)  # 值不暴露
            self.assertEqual(ls["provider"], "strata")
        finally:
            client.__exit__(None, None, None)

    def test_config_put_provider_requires_restart(self):
        client, _ = self._start()
        try:
            # 默认 provider="auto" -> 改为 llamacpp 属于变化，需重启
            r = client.put("/api/config", json={
                "llama_server": {"provider": "llamacpp"},
            })
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertTrue(data["restart_required"])
            self.assertIn("llama_server.provider", data["changed"])
        finally:
            client.__exit__(None, None, None)

    def test_config_put_invalid_provider_rejected(self):
        client, _ = self._start()
        try:
            r = client.put("/api/config", json={
                "llama_server": {"provider": "mistral"},
            })
            self.assertEqual(r.status_code, 400)
        finally:
            client.__exit__(None, None, None)


class OverviewEndpointTests(StrataApiTestBase):
    def test_overview_service_domain_strata(self):
        client, _ = self._start()
        try:
            data = client.get("/api/overview").json()
            service = data.get("service", {})
            self.assertEqual(service.get("provider"), "strata")
            self.assertEqual(service.get("status"), "ready")
            self.assertEqual(service.get("server_state"), "ready")
            self.assertIsNotNone(service.get("model"))
            self.assertEqual(service["model"]["model_alias"], "qwen3.8-flash-next-unsloth-ud-q4_k_xl")
            self.assertEqual(service.get("context_window"), 262144.0)
        finally:
            client.__exit__(None, None, None)

    def test_overview_busy_status(self):
        client, _ = self._start(provider_payloads={
            "/metrics": "metrics_generating.json",
            "/status": "status_busy.json",
            "/slots": "slots.json",
            "/health": "health_ok.json",
            "/props": "props.json",
            "/v1/models": "models.json",
        })
        try:
            data = client.get("/api/overview").json()
            service = data.get("service", {})
            self.assertEqual(service.get("status"), "busy")
        finally:
            client.__exit__(None, None, None)


class HealthEndpointTests(StrataApiTestBase):
    def test_health_collector_online_with_strata(self):
        client, _ = self._start()
        try:
            data = client.get("/api/health").json()
            self.assertTrue(data["collector"]["llama_online"])
            self.assertIsNone(data["known_data_gaps"])
        finally:
            client.__exit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
