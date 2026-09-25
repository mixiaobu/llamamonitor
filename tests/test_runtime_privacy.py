"""
1.1.0 Stage A 测试：llama.cpp Runtime 采集器 + 隐私边界 + 新 API 形状。

覆盖（spec 绑定约束）：
1. sanitize_slot 白名单：prompt / generation_prompt / chat_template / messages
   等一切文本字段绝不进入快照（隐私边界：Slot 监控只读数字/状态/采样参数）。
2. build_model_info 白名单：chat_template / generation_prompt / media_marker
   绝不进入 LlamaModelInfo；model_path 只取文件名进 model_file_name。
3. Server State 状态机：/health 200 {"status":"ok"} -> ready；
   "loading"/其他 -> loading；网络失败 -> unavailable；转换才记 llama_health_changed。
4. 能力探测：/slots 501 -> capabilities.slots 保持 False（不报错）。
5. /api/llama/info 远程只读客户端：model_path 只返回文件名（不泄漏完整路径）。
6. /api/llama/slots 的 cache_reuse_percent = n_prompt_tokens_cache / n_prompt_tokens
   （当前请求缓存复用率；n_prompt_tokens<=0 时 None）。

项目使用标准库 unittest。运行：
    python -X utf8 -m unittest discover -s tests
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from clock import FakeClock
from collector import MetricsCollector
from configutil import make_config, remote_app, loopback_app
from db import Database
from llama_runtime_collector import (
    LlamaRuntimeCollector,
    build_model_info,
    model_change_signature,
    sanitize_slot,
)
from server import build_app


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _mk_client(responses: dict):
    """构造一个 httpx.AsyncClient 替身：get(url) 按 URL 子串返回固定 JSON。"""
    import json as _json

    class _Resp:
        def __init__(self, status, payload):
            self.status_code = status
            self.content = _json.dumps(payload).encode("utf-8")

    class _Client:
        def __init__(self, responses):
            self._r = responses
            self.closed = False

        async def get(self, url):
            for key, (status, payload) in self._r.items():
                if key in url:
                    return _Resp(status, payload)
            return _Resp(404, {})

        async def aclose(self):
            self.closed = True

    return _Client(responses)


class SlotSanitizeTests(unittest.TestCase):
    def test_text_fields_are_dropped(self):
        raw = {
            "id": 3,
            "n_ctx": 4096,
            "speculative": {"draft": True},
            "is_processing": True,
            "id_task": 7,
            "n_prompt_tokens": 128,
            "n_prompt_tokens_processed": 100,
            "n_prompt_tokens_cache": 96,
            "n_decoded": 42,
            "is_stream": True,
            # 隐私：这些文本字段绝不进入快照
            "prompt": "secret user prompt text",
            "generation_prompt": "template {{ . }}",
            "chat_template": "{% for m in messages %}",
            "messages": [{"role": "user", "content": "hi"}],
            "params": {
                "temperature": 0.7,
                "top_p": 0.9,
                "max_tokens": 512,
                "reasoning_format": "secret",
                "seed": 1,
            },
        }
        snap = sanitize_slot(raw)
        # 白名单数字/状态保留
        self.assertEqual(snap["id"], 3)
        self.assertEqual(snap["n_ctx"], 4096)
        self.assertTrue(snap["is_processing"])
        self.assertEqual(snap["n_prompt_tokens_cache"], 96)
        # 采样参数：常用集合保留，非常用键丢弃
        self.assertEqual(snap["params"]["temperature"], 0.7)
        self.assertEqual(snap["params"]["max_tokens"], 512)
        self.assertIn("reasoning_format", snap["params"])
        # 隐私：任何文本字段都不在快照
        for k in ("prompt", "generation_prompt", "chat_template", "messages"):
            self.assertNotIn(k, snap)
        # 序列化后的快照不含任何 prompt 文本
        import json as _json
        blob = _json.dumps(snap)
        self.assertNotIn("secret user prompt", blob)

    def test_malformed_input_returns_empty(self):
        self.assertEqual(sanitize_slot(None), {})
        self.assertEqual(sanitize_slot("not-a-dict"), {})
        self.assertEqual(sanitize_slot([]), {})


class ModelInfoTests(unittest.TestCase):
    def test_privacy_fields_never_enter_info(self):
        props = {
            "model_alias": "my-model",
            "model_ftype": "Q8_0",
            "model_path": "C:\\Users\\alice\\models\\my-model.gguf",
            "total_slots": 4,
            "modalities": {"vision": True, "video": False, "audio": False},
            "chat_template": "{{ .Prompt }}",
            "generation_prompt": "system prompt",
            "media_marker": "[IMG]",
            "default_generation_settings": {"n_ctx": 8192},
        }
        info = build_model_info(props, None)
        self.assertEqual(info["model_alias"], "my-model")
        self.assertEqual(info["model_ftype"], "Q8_0")
        # 完整路径进 model_path（内存用），只取文件名进 model_file_name
        self.assertEqual(info["model_path"], "C:\\Users\\alice\\models\\my-model.gguf")
        self.assertEqual(info["model_file_name"], "my-model.gguf")
        self.assertEqual(info["context_size"], 8192)
        self.assertTrue(info["vision_supported"])
        self.assertFalse(info["video_supported"])
        # 隐私字段绝不进入结构
        for k in ("chat_template", "generation_prompt", "media_marker"):
            self.assertNotIn(k, info)

    def test_no_source_returns_none(self):
        self.assertIsNone(build_model_info(None, None))

    def test_change_signature_differs_on_context(self):
        a = build_model_info({"model_alias": "m", "default_generation_settings": {"n_ctx": 4096}}, None)
        b = build_model_info({"model_alias": "m", "default_generation_settings": {"n_ctx": 8192}}, None)
        self.assertNotEqual(model_change_signature(a), model_change_signature(b))
        self.assertEqual(model_change_signature(None), "")


class ServerStateTests(unittest.TestCase):
    def _collector(self, responses, db=None):
        cfg = make_config(url="http://127.0.0.1:9")
        return LlamaRuntimeCollector(cfg, db=db, clock=FakeClock(1_700_000_000.0),
                                     http_client=_mk_client(responses))

    def test_health_ok_is_ready(self):
        c = self._collector({"/health": (200, {"status": "ok"})})
        _run(c.fetch_health())
        self.assertEqual(c.server_state, "ready")
        self.assertTrue(c.capabilities["health"])

    def test_health_loading(self):
        c = self._collector({"/health": (200, {"status": "loading model"})})
        _run(c.fetch_health())
        self.assertEqual(c.server_state, "loading")

    def test_health_network_fail_is_unavailable(self):
        c = self._collector({"/health": (404, {})})  # 404 -> 非 200 -> unavailable
        _run(c.fetch_health())
        self.assertEqual(c.server_state, "unavailable")

    def test_slots_501_capability_stays_false(self):
        c = self._collector({"/slots": (501, {})})
        _run(c.fetch_slots())
        self.assertFalse(c.capabilities["slots"])

    def test_health_change_records_event(self):
        with tempfile.TemporaryDirectory() as td:
            db = Database(Path(td) / "t.db", wal=False)
            try:
                c = self._collector({"/health": (200, {"status": "ok"})}, db=db)
                # 初始状态 unavailable -> 第一次 ready 不记（prev 为初始）；
                # 先置一个非初始状态再转，确保记录转换
                c.server_state = "loading"
                c._last_state = "loading"
                _run(c.fetch_health())  # loading -> ready
                types = [
                    r["event_type"]
                    for r in db.get_events(limit=50)
                ]
                self.assertIn("llama_health_changed", types)
            finally:
                db.close()


class LlamaApiShapeTests(unittest.TestCase):
    """/api/llama/info 与 /api/llama/slots 的形状与隐私。

    注意：用 TestClient 但**不进入** with 上下文（不触发 lifespan）——
    本测试直接预填 runtime 状态，不需要 run 循环；若触发 lifespan，
    run() 的 health 抓取会用 mock 的 404 把 server_state 打回 unavailable。
    """

    def _start(self):
        td = tempfile.TemporaryDirectory()
        db = Database(Path(td.name) / "api.db")
        cfg = make_config(url="http://127.0.0.1:9")
        collector = MetricsCollector(cfg, db)
        runtime = LlamaRuntimeCollector(cfg, db=db, clock=FakeClock(1_700_000_000.0),
                                        http_client=_mk_client({}))
        # 预填模型信息 + slots（避免依赖异步 run 循环）
        runtime.model_info = build_model_info(
            {
                "model_alias": "llama-3",
                "model_ftype": "Q4_K_M",
                "model_path": "C:\\Models\\llama-3.gguf",
                "total_slots": 2,
            }, None)
        runtime.slots = [
            {
                "id": 0, "n_ctx": 4096, "is_processing": True,
                "n_prompt_tokens": 200, "n_prompt_tokens_cache": 150,
                "n_prompt_tokens_processed": 180,
            },
        ]
        runtime.last_slots_update = 1_700_000_000.0
        runtime.server_state = "ready"
        app = build_app(db, collector, runtime=runtime)
        return td, db, runtime, app

    def test_info_model_path_filename_only_for_remote(self):
        td, db, runtime, app = self._start()
        try:
            client = TestClient(app)
            # 默认 TestClient 客户端是 ("testclient", ...) -> 非 loopback -> 只文件名
            data = client.get("/api/llama/info")
            self.assertEqual(data.status_code, 200)
            model = data.json()["model"]
            self.assertEqual(model["model_file_name"], "llama-3.gguf")
            self.assertEqual(model["model_path"], "llama-3.gguf")  # 远程只文件名
            # 隐私字段不返回
            self.assertNotIn("chat_template", model)
            self.assertNotIn("generation_prompt", model)
            self.assertNotIn("media_marker", model)
            # server_state 存在
            self.assertEqual(data.json()["server_state"], "ready")
        finally:
            db.close()
            td.cleanup()

    def test_slots_cache_reuse_percent(self):
        td, db, runtime, app = self._start()
        try:
            client = TestClient(app)
            data = client.get("/api/llama/slots")
            self.assertEqual(data.status_code, 200)
            slots = data.json()["slots"]
            self.assertEqual(len(slots), 1)
            # 150 / 200 = 75.0（当前请求缓存复用率 = cache / n_prompt_tokens）
            self.assertAlmostEqual(slots[0]["cache_reuse_percent"], 75.0, places=3)
            # 隐私：prompt 文本不在返回里
            self.assertNotIn("prompt", slots[0])
        finally:
            db.close()
            td.cleanup()

    def test_slots_cache_reuse_none_when_no_tokens(self):
        td, db, runtime, app = self._start()
        runtime.slots = [
            {"id": 1, "n_ctx": 1024, "is_processing": False,
             "n_prompt_tokens": 0, "n_prompt_tokens_cache": None},
        ]
        try:
            client = TestClient(app)
            slots = client.get("/api/llama/slots").json()["slots"]
            self.assertIsNone(slots[0]["cache_reuse_percent"])
        finally:
            db.close()
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
