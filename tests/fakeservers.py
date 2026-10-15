"""
fakeservers.py — 测试用本地 Fake 推理服务器（1.3.2；stdlib http.server，无新依赖）

FakeStrataServer：
- /metrics（JSON；支持 ?requests=all）、/metrics?format=prometheus（vLLM Histogram）、
  /metrics/prometheus（llamacpp:* 命名交叉验证）、/status、/slots、/health、
  /props、/v1/models、/v1/status；
- 场景驱动：set_metrics(dict) 直接指定 /metrics JSON；advance() 递增计数器；
  401/404/500/bad-JSON 模式；延迟注入；
- 计数器 reset 支持（reset_counters()）。

FakeLlamaServer：
- /health（200 ok / 503 "Loading model" / 401）、/slots（next_token 对象形态）、
  /props（is_sleeping）、/v1/models（n_ctx_train / modalities）、
  /metrics（llamacpp:* Prometheus 文本；Router 模式：无 model 参数 -> 400）、
  /lora-adapters、/models（Router 列表）、/models/sse（SSE；可配置发送若干事件后
  关闭连接以验证重连）。

用法：
    srv = FakeStrataServer()
    srv.start()            # 随机端口（127.0.0.1）
    url = srv.url          # "http://127.0.0.1:PORT"
    ...
    srv.stop()
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# Fake Strata
# ---------------------------------------------------------------------------

# spec §36 固定计数器（验收口径：input 79,265 / cached 2,032,318 / output 5,759 /
# logical 2,117,342 / compute 85,024；draft 501/1001）
SPEC_BASE_TOTALS = {
    "since": 1760000000,
    "prompt_tokens": 2111583,
    "reused": 2032318,
    "output_tokens": 5759,
    "prompt_ms": 125000,
    "decode_ms": 210000,
    "drafts_offered": 6248,
    "drafts_accepted": 3697,
}

SPEC_REQUESTS = [
    # 请求 A：offered=1 accepted=1
    {
        "time": 1760000100.0, "duration_s": 2.0, "finish": "stop",
        "prompt_tokens": 100, "reused": 90, "output_tokens": 10,
        "prompt_ms": 50.0, "decode_ms": 1000.0,
        "drafts_offered": 1, "drafts_accepted": 1,
        "hit_rate": 0.85, "pcie_share": 0.1,
    },
    # 请求 B：offered=1000 accepted=500（总体 501/1001≈0.5005，不是 75%）
    {
        "time": 1760000200.0, "duration_s": 30.0, "finish": "length",
        "prompt_tokens": 2000, "reused": 100, "output_tokens": 500,
        "prompt_ms": 500.0, "decode_ms": 15000.0,
        "drafts_offered": 1000, "drafts_accepted": 500,
        "hit_rate": 0.9, "pcie_share": 0.05,
    },
]

OFFICIAL_LE = ["0.001", "0.005", "0.01", "0.02", "0.04", "0.06", "0.08", "0.1",
               "0.25", "0.5", "0.75", "1", "2.5", "5", "7.5", "10", "20", "40",
               "80", "160", "640", "2560"]


def _hist_family(name: str, counts: list[int], model: str,
                 help_text: str) -> str:
    """构造一个 vLLM histogram family（counts 为**累计**桶计数，每桶增量）。"""
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} histogram"]
    total = 0
    for le, c in zip(OFFICIAL_LE, counts):
        total += c
        lines.append(f'{name}_bucket{{le="{le}",model_name="{model}"}} {total}')
    lines.append(f'{name}_bucket{{le="+Inf",model_name="{model}"}} {total}')
    lines.append(f'{name}_sum{{model_name="{model}"}} {total * 0.4:.3f}')
    lines.append(f'{name}_count{{model_name="{model}"}} {total}')
    return "\n".join(lines)


class FakeStrataState:
    """Fake Strata 的可变状态（测试直接改）。"""

    def __init__(self) -> None:
        self.totals: dict = dict(SPEC_BASE_TOTALS)
        self.requests: list = [dict(r) for r in SPEC_REQUESTS]
        self.live: dict = {
            "state": "generating", "phase": "answering",
            "tok_s": 42.0, "tok_s_mean": 38.0, "prefill_tok_s_mean": 1200.0,
            "prompt_tokens": 2000, "generated": 500, "max_tokens": 1024,
            "elapsed_s": 12.5,
            "running": 1, "outside_slots": 1,   # spec：必须显示为 1 个请求
            "waiting": 0, "queued": 0, "admitting": 0,
            "slots": [{"slot": 0, "state": "busy", "held_tokens": 12345}],
        }
        self.status: dict = {
            "busy": True, "phase": "answering", "queued": 0, "admitting": 0,
            "reasoning_recoveries": 0,
            # 隐私字段：绝不应出现在 LlamaMonitor API 响应
            "tail": "SECRET_OUTPUT", "tool": "SECRET_TOOL",
            "prompt_tokens": 2000, "generated": 500,
        }
        self.slots: list = [{"id": 0, "n_ctx": 262144, "is_processing": True,
                             "n_prompt_tokens": 12345}]
        self.health: dict = {
            "status": "ok", "service": "strata",
            "model": "fake-model", "loaded": True, "images": False,
            "max_context": 262144,
        }
        self.props: dict = {
            "model_alias": "fake-model", "build_info": "0.1.43.1",
            "is_sleeping": False, "total_slots": 2,
            "modalities": {"vision": False},
            "default_generation_settings": {"n_ctx": 262144},
            "chat_template": "SECRET_CHAT_TEMPLATE",
        }
        self.models: dict = {
            "data": [{"id": "fake-model", "object": "model",
                      "meta": {"n_ctx": 262144, "n_ctx_train": 40960,
                               "n_params": 8000000000, "ftype": "Q4_K_XL",
                               "n_vocab": 151936, "n_embd": 4096,
                               "size": 4700000000},
                      "architecture": {
                          "input_modalities": ["text"],
                          "output_modalities": ["text"]}}],
        }
        self.engine: dict = {
            "model": "fake-model", "engine": "0.1.43.1",
            "max_context": 262144, "context": 262144,
            "expert_slots": 8, "expert_slots_primary": 8,
            "kv": 64, "kv_resident": 64,
            "batch_slots": 2, "batch_groups": 1,
            "pool_workers": 4, "pcie_frac": 0.2,
            "spec": "mtp", "mtp_max": 3,
            "arena_mib": 4096, "vram_free_mib": 8192,
            "conversation_cache_mib": 1024, "conversation_cache_slots": 4,
        }
        self.conversation_cache: dict = {
            "enabled": True, "budget_mib": 1024, "slots": 4, "parked": 1,
            "bytes": 123456789, "evictions": 2, "parks": 10, "restores": 8,
            "last_event": "park", "last_tokens": 5000, "last_at": 1760000000,
            "requests": 100, "requests_reused": 50, "reused_tokens": 120000,
        }
        self.hardware: dict = {
            "gpu_util": 55.0, "gpu_mem_used": 20000000000,
            "gpu_mem_total": 32000000000, "gpu_temp": 60.0,
            "gpu_power": 180.0, "gpu_pcie_gen": 4, "gpu_pcie_gen_max": 5,
            "gpu_pcie_width": 16, "gpu_pcie_rx_mb": 12.5,
            "gpu_pcie_tx_mb": 8.25, "cpu": 12.0,
            "ram_used": 100000000000, "ram_total": 200000000000,
        }
        self.hardware_static: dict = {"gpu_name": "FakeGPU", "cpu_name": "FakeCPU"}
        self.v1_status: dict = {
            "service": "strata", "model": "fake-model", "loaded": True,
            "engine": "0.1.43.1", "started": 1759900000, "uptime_s": 100000,
            "concurrency": {"serving": 2, "requested": 3},
            "activity": {"requests": 42, "in_flight": 1,
                         "last_request_at": 1760000200},
            "last_timings": {"prompt_n": 100, "predicted_n": 10, "at": 1760000200},
        }
        # Histogram 累计计数（每桶增量；advance_histogram 递增）
        self.hist_counts: dict[str, list[int]] = {
            "vllm:time_to_first_token_seconds": [0] * len(OFFICIAL_LE),
            "vllm:inter_token_latency_seconds": [0] * len(OFFICIAL_LE),
            "vllm:e2e_request_latency_seconds": [0] * len(OFFICIAL_LE),
        }
        self.model_name = "fake-model"
        # 故障注入
        self.metrics_mode: str = "ok"   # ok / bad_json / 401 / 404 / 500 / slow
        self.latency_s: float = 0.0
        self.requests_all_extra: list = [
            {**SPEC_REQUESTS[1], "time": 1760000300.0, "duration_s": 1.0,
             "finish": "stop", "prompt_tokens": 50, "reused": 0,
             "output_tokens": 5, "prompt_ms": 10.0, "decode_ms": 100.0,
             "drafts_offered": 0, "drafts_accepted": 0,
             "hit_rate": 0.7, "pcie_share": 0.3},
        ]

    def reset_counters(self) -> None:
        """模拟服务重启：totals 归零 + 新 since（histogram Counter 同时重置）。"""
        self.totals = {
            "since": self.totals.get("since", 1760000000) + 1,
            "prompt_tokens": 0, "reused": 0, "output_tokens": 0,
            "prompt_ms": 0, "decode_ms": 0,
            "drafts_offered": 0, "drafts_accepted": 0,
        }
        self.hist_counts = {k: [0] * len(OFFICIAL_LE) for k in self.hist_counts}

    def advance(self, prompt: int = 0, reused: int = 0, output: int = 0,
                prompt_ms: float = 0.0, decode_ms: float = 0.0,
                offered: int = 0, accepted: int = 0) -> None:
        t = self.totals
        t["prompt_tokens"] = t.get("prompt_tokens", 0) + prompt
        t["reused"] = t.get("reused", 0) + reused
        t["output_tokens"] = t.get("output_tokens", 0) + output
        t["prompt_ms"] = t.get("prompt_ms", 0) + prompt_ms
        t["decode_ms"] = t.get("decode_ms", 0) + decode_ms
        t["drafts_offered"] = t.get("drafts_offered", 0) + offered
        t["drafts_accepted"] = t.get("drafts_accepted", 0) + accepted

    def advance_histogram(self, metric_key: str, per_bucket: list[int]) -> None:
        counts = self.hist_counts.setdefault(metric_key, [0] * len(OFFICIAL_LE))
        for i, c in enumerate(per_bucket):
            counts[i] += c

    def metrics_json(self, requests_all: bool = False) -> dict:
        reqs = list(self.requests)
        if requests_all:
            reqs = list(self.requests_all_extra) + reqs
        return {
            "time": 1760000300.0,
            "live": dict(self.live),
            "requests": reqs,
            "requests_kept": 12,
            "conversation_cache": dict(self.conversation_cache),
            "engine": dict(self.engine),
            "totals": dict(self.totals),
            "hardware": dict(self.hardware),
            "hardware_static": dict(self.hardware_static),
        }

    def prometheus_text(self) -> str:
        parts = [
            "# TYPE vllm:num_requests_running gauge",
            f'vllm:num_requests_running{{model_name="{self.model_name}"}} '
            f"{self.live.get('running', 0)}",
            "# TYPE vllm:num_requests_waiting gauge",
            f'vllm:num_requests_waiting{{model_name="{self.model_name}"}} '
            f"{self.live.get('waiting', 0)}",
            f"# TYPE vllm:kv_cache_usage_perc gauge",
            f'vllm:kv_cache_usage_perc{{model_name="{self.model_name}"}} 0.42',
            f"# TYPE strata:requests_total counter",
            f"strata:requests_total {len(self.requests)}",
        ]
        parts.append(_hist_family(
            "vllm:time_to_first_token_seconds",
            self.hist_counts["vllm:time_to_first_token_seconds"],
            self.model_name, "Time to first token in seconds."))
        parts.append(_hist_family(
            "vllm:inter_token_latency_seconds",
            self.hist_counts["vllm:inter_token_latency_seconds"],
            self.model_name, "Inter token latency in seconds (per-request mean estimate)."))
        parts.append(_hist_family(
            "vllm:e2e_request_latency_seconds",
            self.hist_counts["vllm:e2e_request_latency_seconds"],
            self.model_name, "End to end request latency in seconds."))
        return "\n".join(parts) + "\n"


class _StrataHandler(BaseHTTPRequestHandler):
    state: FakeStrataState = None  # type: ignore[assignment]

    def log_message(self, *a):  # 静默
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        st: FakeStrataState = self.state
        if st.latency_s:
            time.sleep(st.latency_s)
        url = urlparse(self.path)
        path = url.path
        qs = parse_qs(url.query)
        try:
            if path == "/metrics":
                if "format" in qs and qs["format"][0] == "prometheus":
                    self._send(200, st.prometheus_text().encode(), "text/plain")
                    return
                mode = st.metrics_mode
                if mode == "401":
                    self._send(401, b'{"error":"unauthorized"}')
                elif mode == "404":
                    self._send(404, b'{"error":"not found"}')
                elif mode == "500":
                    self._send(500, b'{"error":"boom"}')
                elif mode == "bad_json":
                    self._send(200, b'{not json', "application/json")
                else:
                    payload = st.metrics_json(requests_all="requests" in qs)
                    self._send(200, json.dumps(payload).encode())
            elif path == "/metrics/prometheus":
                self._send(200, st.prometheus_text().encode(), "text/plain")
            elif path == "/status":
                self._send(200, json.dumps(st.status).encode())
            elif path == "/slots":
                self._send(200, json.dumps(st.slots).encode())
            elif path == "/health":
                self._send(200, json.dumps(st.health).encode())
            elif path == "/props":
                self._send(200, json.dumps(st.props).encode())
            elif path == "/v1/models":
                self._send(200, json.dumps(st.models).encode())
            elif path == "/v1/status":
                self._send(200, json.dumps(st.v1_status).encode())
            else:
                self._send(404, b'{"error":"unknown path"}')
        except (BrokenPipeError, ConnectionResetError):
            pass


class FakeStrataServer:
    """127.0.0.1 随机端口的 Fake Strata。start()/stop() 管理生命周期。"""

    def __init__(self) -> None:
        self.state = FakeStrataState()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.url: str = ""

    def start(self) -> "FakeStrataServer":
        handler = type("H", (_StrataHandler,), {"state": self.state})
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None


# ---------------------------------------------------------------------------
# Fake llama.cpp（含 Router 模式）
# ---------------------------------------------------------------------------


def _llamacpp_metrics_text(model: str, prompt: int = 1000, output: int = 200,
                           cached: int = 100) -> str:
    lines = [
        f"# TYPE llamacpp:prompt_tokens_total counter",
        f'llamacpp:prompt_tokens_total{{model="{model}"}} {prompt}',
        f"# TYPE llamacpp:prompt_tokens_cached_total counter",
        f'llamacpp:prompt_tokens_cached_total{{model="{model}"}} {cached}',
        "# TYPE llamacpp:tokens_predicted_total counter",
        f'llamacpp:tokens_predicted_total{{model="{model}"}} {output}',
        "# TYPE llamacpp:prompt_seconds_total counter",
        f'llamacpp:prompt_seconds_total{{model="{model}"}} 0.5',
        "# TYPE llamacpp:tokens_predicted_seconds_total counter",
        f'llamacpp:tokens_predicted_seconds_total{{model="{model}"}} 5.0',
        "# TYPE llamacpp:requests_processing gauge",
        f'llamacpp:requests_processing{{model="{model}"}} 1',
        "# TYPE llamacpp:requests_deferred gauge",
        f'llamacpp:requests_deferred{{model="{model}"}} 0',
    ]
    return "\n".join(lines) + "\n"


class FakeLlamaState:
    def __init__(self, router: bool = False) -> None:
        self.router = router
        self.health_mode: str = "ok"  # ok / loading_503 / 401 / 500
        self.slots: list = [{
            "id": 0, "n_ctx": 8192, "speculative": {"draft": False},
            "is_processing": True, "id_task": 7,
            "n_prompt_tokens": 128, "n_prompt_tokens_processed": 128,
            "n_prompt_tokens_cache": 96, "n_decoded": 42, "n_remaining": 512,
            "is_stream": True, "has_next_token": True,
            "next_token": {  # 官方对象形态
                "has_next_token": True, "has_new_line": False,
                "n_remain": 128, "n_decoded": 512,
            },
            "params": {"temperature": 0.7, "top_p": 0.9, "top_k": 40,
                       "min_p": 0.05, "max_tokens": 1024, "seed": 42},
            "prompt": "SECRET_PROMPT_TEXT",  # 隐私：绝不应出现
        }]
        self.props: dict = {
            "model_alias": "llama-model", "model_ftype": "Q4_K_M",
            "model_path": "C:\\models\\llama-model.gguf",
            "total_slots": 8,
            "modalities": {"vision": True, "video": False, "audio": False},
            "is_sleeping": False,
            "default_generation_settings": {"n_ctx": 8192},
            "build_info": "b10976-00000000",
            "chat_template": "SECRET_CHAT_TEMPLATE_LLAMA",
        }
        self.v1_models: dict = {
            "data": [{
                "id": "llama-model", "object": "model",
                "meta": {"n_vocab": 151936, "n_ctx": 8192,
                         "n_ctx_train": 32768, "n_embd": 4096,
                         "n_params": 497000000, "size": 330000000,
                         "ftype": "Q4_K_M"},
                "architecture": {
                    "input_modalities": ["text", "image"],
                    "output_modalities": ["text"],
                },
            }],
        }
        self.lora: list = [
            {"id": "lora-a", "path": "C:\\loras\\lora-a.gguf", "scale": 1.0},
            {"id": "lora-b", "path": "D:\\loras\\sub\\lora-b.gguf", "scale": 0.5},
        ]
        # Router 模式
        self.router_models: list = [
            {"id": "model-a", "name": "Model A",
             "status": {"value": "loaded"}, "path": "C:\\models\\a.gguf"},
            {"id": "model-b", "name": "Model B",
             "status": {"value": "unloaded"}, "path": "C:\\models\\b.gguf"},
        ]
        # 每模型 metrics（Router 模式按 model 参数返回）
        self.model_counters: dict[str, dict] = {
            "model-a": {"prompt": 1000, "output": 200, "cached": 100},
            "model-b": {"prompt": 50, "output": 10, "cached": 0},
        }
        # SSE：要发送的事件列表（每帧 event+data）；发完后 close 连接（验证重连）
        self.sse_events: list[dict] = [
            {"event": "models_reload", "data": {"count": 2}},
            {"event": "model_status",
             "data": {"id": "model-a", "status": "loaded"}},
            {"event": "download_progress",
             "data": {"id": "model-b", "status": "loading",
                      "progress": {"stages": ["download", "load"],
                                   "current": 0, "value": 0.4}}},
        ]
        self.sse_close_after_send: bool = True  # True=发完即断开（重连测试）
        self.sse_stay_open_s: float = 0.2
        self.sse_enabled: bool = True
        self.sse_fail_connect: bool = False  # True -> /models/sse 返回 500


class _LlamaHandler(BaseHTTPRequestHandler):
    state: FakeLlamaState = None  # type: ignore[assignment]

    def log_message(self, *a):
        pass

    def _send(self, code: int, body: bytes,
              ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        st: FakeLlamaState = self.state
        url = urlparse(self.path)
        path = url.path
        qs = parse_qs(url.query)
        try:
            if path == "/health":
                mode = st.health_mode
                if mode == "401":
                    self._send(401, b'{"detail":"Not authenticated"}')
                elif mode == "loading_503":
                    self._send(503, json.dumps({
                        "error": {"code": 503, "message": "Loading model",
                                  "type": "server_error"}}).encode())
                elif mode == "500":
                    self._send(500, b'{"error":"boom"}')
                else:
                    self._send(200, b'{"status":"ok"}')
            elif path == "/slots":
                self._send(200, json.dumps(st.slots).encode())
            elif path == "/props":
                if st.router and "model" not in qs:
                    self._send(400, b'{"error":"model parameter required"}')
                    return
                self._send(200, json.dumps(st.props).encode())
            elif path == "/v1/models":
                if st.router and "model" not in qs:
                    self._send(400, b'{"error":"model parameter required"}')
                    return
                self._send(200, json.dumps(st.v1_models).encode())
            elif path == "/metrics":
                if st.router:
                    if "model" not in qs:
                        self._send(400, b'{"error":"model parameter required"}')
                        return
                    model = qs["model"][0]
                    if qs.get("autoload", ["false"])[0] != "false":
                        # 只读保证：autoload 必须 false（测试断言请求方行为）
                        st._last_autoload = qs.get("autoload", ["?"])[0]
                    else:
                        st._last_autoload = "false"
                    c = st.model_counters.get(model, {"prompt": 0, "output": 0,
                                                      "cached": 0})
                    self._send(200, _llamacpp_metrics_text(
                        model, c["prompt"], c["output"], c["cached"]).encode(),
                        "text/plain")
                else:
                    self._send(200, _llamacpp_metrics_text(
                        "llama-model").encode(), "text/plain")
            elif path == "/lora-adapters":
                self._send(200, json.dumps(st.lora).encode())
            elif path == "/models":
                if st.router:
                    self._send(200, json.dumps(st.router_models).encode())
                else:
                    self._send(404, b'{"error":"not found"}')
            elif path == "/models/sse":
                if not st.sse_enabled:
                    self._send(404, b'{"error":"not found"}')
                    return
                if st.sse_fail_connect:
                    self._send(500, b'{"error":"sse boom"}')
                    return
                self._sse_stream(st)
            else:
                self._send(404, b'{"error":"unknown path"}')
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _sse_stream(self, st: FakeLlamaState) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for ev in st.sse_events:
                frame = f"event: {ev['event']}\ndata: {json.dumps(ev['data'])}\n\n"
                self.wfile.write(frame.encode())
                self.wfile.flush()
                time.sleep(0.05)
            if not st.sse_close_after_send:
                time.sleep(st.sse_stay_open_s)
        except (BrokenPipeError, ConnectionResetError):
            pass


class FakeLlamaServer:
    """127.0.0.1 随机端口的 Fake llama.cpp（可开 Router 模式）。"""

    def __init__(self, router: bool = False) -> None:
        self.state = FakeLlamaState(router=router)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.url: str = ""

    def start(self) -> "FakeLlamaServer":
        handler = type("H", (_LlamaHandler,), {"state": self.state})
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
