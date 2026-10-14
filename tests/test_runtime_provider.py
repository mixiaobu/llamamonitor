"""runtime_provider.py 单元测试（1.3.0：后端识别 / 手动选择校验 / 解析规则）。

在项目根目录运行：
    python -m unittest discover -s tests
"""

import asyncio
import json
import unittest
from unittest import mock

import httpx

import runtime_provider as rp
from runtime_provider import (
    PROVIDER_AUTO,
    PROVIDER_LLMACPP,
    PROVIDER_STRATA,
    detect_backend,
    resolve_provider,
    verify_manual_provider,
)

STRATA_HEALTH = json.dumps({
    "status": "ok", "max_context": 262144,
    "model": "qwen3.8-flash-next-unsloth-ud-q4_k_xl",
    "images": True, "api_key": False, "loaded": True, "service": "strata",
})
LLAMA_HEALTH = json.dumps({"status": "ok"})
LLAMA_METRICS = (
    "# HELP llamacpp:prompt_tokens_total Total prompt tokens\n"
    "# TYPE llamacpp:prompt_tokens_total counter\n"
    "llamacpp:prompt_tokens_total 12345\n"
)


class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = ""):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    """httpx.AsyncClient 替身：按 URL 后缀分发响应。"""

    def __init__(self, routes: dict):
        self._routes = routes
        self.urls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, *a, **k):
        self.urls.append(url)
        for suffix, resp in self._routes.items():
            if url.endswith(suffix):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return _FakeResponse(404, "not found")


def _run(routes: dict, url="http://127.0.0.1:9091", **kw):
    client = _FakeClient(routes)
    with mock.patch.object(httpx, "AsyncClient", return_value=client):
        result = asyncio.run(rp.detect_backend(url, **kw))
    return result, client


class DetectBackendTests(unittest.TestCase):
    def test_strata_by_health_service(self):
        result, client = _run({
            "/health": _FakeResponse(200, STRATA_HEALTH),
        })
        self.assertEqual(result["provider"], PROVIDER_STRATA)
        self.assertEqual(result["provider_display"], "Strata")
        self.assertEqual(result["strata"]["model"], "qwen3.8-flash-next-unsloth-ud-q4_k_xl")
        self.assertTrue(result["strata"]["loaded"])
        # Strata 不需要再请求 /metrics（识别成本最小化）
        self.assertEqual(client.urls, ["http://127.0.0.1:9091/health"])
        self.assertEqual(result["server_id"], "127.0.0.1:9091")

    def test_llamacpp_by_metrics_prefix(self):
        result, client = _run({
            "/health": _FakeResponse(200, LLAMA_HEALTH),
            "/metrics": _FakeResponse(200, LLAMA_METRICS),
        })
        self.assertEqual(result["provider"], PROVIDER_LLMACPP)
        self.assertEqual(result["provider_display"], "llama.cpp")
        self.assertTrue(result["llamacpp"]["metrics_detected"])
        self.assertEqual(client.urls[-1], "http://127.0.0.1:9091/metrics")

    def test_llama_style_health_without_service_field(self):
        # §9：不能仅凭 status=="ok" 判为 Strata——llama.cpp 的 /health 也是 ok
        result, _ = _run({
            "/health": _FakeResponse(200, LLAMA_HEALTH),
            "/metrics": _FakeResponse(200, LLAMA_METRICS),
        })
        self.assertEqual(result["provider"], PROVIDER_LLMACPP)

    def test_unknown_when_no_features(self):
        result, _ = _run({
            "/health": _FakeResponse(200, json.dumps({"status": "weird"})),
            "/metrics": _FakeResponse(200, "plain text no metrics"),
        })
        self.assertEqual(result["provider"], "unknown")
        self.assertIn("未识别", result["error"])

    def test_unreachable(self):
        conn_err = httpx.ConnectError("refused")
        result, _ = _run({"/health": conn_err, "/metrics": conn_err})
        self.assertEqual(result["provider"], "unknown")
        self.assertIn("拒绝", result["error"])

    def test_timeout(self):
        result, _ = _run({
            "/health": httpx.TimeoutException("slow"),
            "/metrics": httpx.TimeoutException("slow"),
        })
        self.assertEqual(result["provider"], "unknown")
        self.assertIn("超时", result["error"])

    def test_bad_url(self):
        result = asyncio.run(rp.detect_backend("ftp://nope"))
        self.assertEqual(result["provider"], "unknown")
        self.assertIn("http", result["error"])

    def test_http_status_recorded(self):
        result, _ = _run({
            "/health": _FakeResponse(404, "no"),
            "/metrics": _FakeResponse(404, "no"),
        })
        self.assertEqual(result["http_status"], 404)

    def test_api_key_header(self):
        # 密钥只进 Authorization 头，不进 URL / 不落到识别结果
        client = _FakeClient({"/health": _FakeResponse(200, STRATA_HEALTH)})
        captured = {}

        class _CapClient(_FakeClient):
            def __init__(self, routes):
                super().__init__(routes)

        with mock.patch.object(httpx, "AsyncClient") as m:
            m.return_value = client
            # 验证 headers 参数被传递
            asyncio.run(rp.detect_backend(
                "http://127.0.0.1:9091", api_key="sk-test-123",
            ))
            kwargs = m.call_args.kwargs
            self.assertEqual(kwargs.get("headers", {}).get("Authorization"), "Bearer sk-test-123")
        self.assertNotIn("sk-test-123", json.dumps(client.urls))


class VerifyManualProviderTests(unittest.TestCase):
    def test_manual_strata_matches(self):
        client = _FakeClient({"/health": _FakeResponse(200, STRATA_HEALTH)})
        with mock.patch.object(httpx, "AsyncClient", return_value=client):
            result = asyncio.run(rp.verify_manual_provider(
                "http://127.0.0.1:9091", PROVIDER_STRATA,
            ))
        self.assertFalse(result["mismatch"])
        self.assertEqual(result["provider"], PROVIDER_STRATA)

    def test_manual_strata_mismatch(self):
        client = _FakeClient({
            "/health": _FakeResponse(200, LLAMA_HEALTH),
            "/metrics": _FakeResponse(200, LLAMA_METRICS),
        })
        with mock.patch.object(httpx, "AsyncClient", return_value=client):
            result = asyncio.run(rp.verify_manual_provider(
                "http://127.0.0.1:9091", PROVIDER_STRATA,
            ))
        self.assertTrue(result["mismatch"])
        self.assertIn("不一致", result["mismatch_detail"])

    def test_manual_llamacpp_unreachable(self):
        conn_err = httpx.ConnectError("refused")
        client = _FakeClient({"/health": conn_err, "/metrics": conn_err})
        with mock.patch.object(httpx, "AsyncClient", return_value=client):
            result = asyncio.run(rp.verify_manual_provider(
                "http://127.0.0.1:9", PROVIDER_LLMACPP,
            ))
        self.assertTrue(result["mismatch"])
        self.assertIsNotNone(result["mismatch_detail"])


class ResolveProviderTests(unittest.TestCase):
    def test_manual_wins(self):
        self.assertEqual(resolve_provider("strata", {"provider": "llamacpp"}), PROVIDER_STRATA)
        self.assertEqual(resolve_provider("llamacpp", {"provider": "strata"}), PROVIDER_LLMACPP)

    def test_auto_uses_detection(self):
        self.assertEqual(resolve_provider(PROVIDER_AUTO, {"provider": "strata"}), PROVIDER_STRATA)
        self.assertEqual(resolve_provider(None, {"provider": "llamacpp"}), PROVIDER_LLMACPP)

    def test_auto_falls_back_to_llamacpp(self):
        self.assertEqual(resolve_provider(PROVIDER_AUTO, {"provider": "unknown"}), PROVIDER_LLMACPP)
        self.assertEqual(resolve_provider(PROVIDER_AUTO, None), PROVIDER_LLMACPP)


if __name__ == "__main__":
    unittest.main()
