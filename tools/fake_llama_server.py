"""
tools/fake_llama_server.py — 可控的假 llama.cpp /metrics 服务器（Phase 11）

目的：不依赖真实 llama-server 就能测试数据正确性（restart / offline /
malformed / missing / counter reset / ground truth 对比）。

- 默认监听 127.0.0.1:19091（--port 可改）；
- GET /metrics 输出与真实 llama.cpp 相同的 Prometheus 文本格式
  （llamacpp:* counter/gauge，含 MTP per-position 指标）；
- 测试控制接口（只用于 tools / tests，绝不进入正式 LlamaMonitor API）：
    POST /test/add       {"prompt":.., "cached":.., "output":..,
                          "prompt_seconds":.., "predicted_seconds":..,
                          "draft":.., "accepted":.., "drafts":..,
                          "per_position": {"0":..}}   使 counter 增加
    POST /test/reset     模拟 llama-server 重启：所有 counter 归 0（或 --to 指定小值）；
                          若重置前存在"已生成但未被观测"的增长，ground truth
                          仍记录全部真实值（unobservable 部分可查询）；
    POST /test/offline   {"seconds": N} 之后 N 秒内 GET /metrics 返回 503
                          （模拟 metrics 暂时不可访问）；{"seconds": 0} 立即恢复
    POST /test/malformed {"body": "..."} 下一次 GET 返回指定损坏内容（一次）；
    POST /test/missing   {"metrics": ["llamacpp:prompt_tokens_total", ...]}
                          后续 /metrics 中移除这些指标（再发一次同名请求可恢复）；
    POST /test/state     返回当前 counter / ground truth / 控制状态（调试用）；
    GET  /test/ground_truth  返回真实生成总量与观测差值（soak 对比用）。

Ground Truth 语义（soak test 的对照基准）：
- ground_truth.{prompt,cached,output,...}：fake server 生成的全部真实增量累计
  （跨 reset 累计：reset 前的生成也算真实发生过的 token）；
- last_observed.*：monitor 最后一次可能观测到的值（/metrics 被成功读取时更新；
  offline / malformed 期间读取不算观测）；
- unobservable.*：ground_truth - last_observed（"发生了但 monitor 没看到"的量）。
  若之后发生 reset，这些量对 monitor 永久不可恢复（token_loss_possible）。

运行：
    python tools/fake_llama_server.py                # 127.0.0.1:19091
    python tools/fake_llama_server.py --port 19092 --seed 42
"""

from __future__ import annotations

import argparse
import json
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Counter 模型
# ---------------------------------------------------------------------------

COUNTERS = (
    "llamacpp:prompt_tokens_total",
    "llamacpp:prompt_tokens_cached_total",
    "llamacpp:prompt_seconds_total",
    "llamacpp:tokens_predicted_total",
    "llamacpp:tokens_predicted_seconds_total",
    "llamacpp:n_decode_total",
    "llamacpp:spec_decode_num_draft_tokens_total",
    "llamacpp:spec_decode_num_accepted_tokens_total",
    "llamacpp:spec_decode_num_drafts_total",
)

# per-position MTP accepted（position 可增删，模拟不同 llama.cpp 版本）
DEFAULT_POSITIONS = ("0", "1", "2")


class FakeServer:
    """全部状态（thread-safe：控制接口与 metrics 读取可能来自不同线程）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 当前暴露的 counter 值（monitor 能读到的）
        self.counters: dict[str, float] = {name: 0.0 for name in COUNTERS}
        self.per_position: dict[str, float] = {p: 0.0 for p in DEFAULT_POSITIONS}
        # gauge（随机轻微波动，只为逼真）
        self.gauges = {
            "llamacpp:requests_processing": 0,
            "llamacpp:requests_deferred": 0,
            "llamacpp:n_busy_slots_per_decode": 0,
            "llamacpp:kv_cache_usage_ratio": 0.0,
            "llamacpp:n_tokens_max": 4096,
        }
        # ground truth：真实生成总量（跨 reset 累计）
        self.ground_truth = {
            "prompt": 0.0, "cached": 0.0, "output": 0.0,
            "prompt_seconds": 0.0, "predicted_seconds": 0.0,
            "draft": 0.0, "accepted": 0.0, "drafts": 0.0,
        }
        # monitor 最后一次成功观测时各 counter 的值
        self.last_observed: dict[str, float] = {name: 0.0 for name in COUNTERS}
        # 控制状态
        self.offline_until = 0.0          # wall 秒：now < offline_until 时 503
        self.malformed_body: str | None = None  # 一次性
        self.missing: set[str] = set()    # 当前从 /metrics 中移除的指标名
        self.resets = 0                   # 发生的 reset 次数
        self.observation_count = 0

    # -- 控制接口 -------------------------------------------------------------

    def add(self, inc: dict) -> None:
        """使 counter 增加（同时累计 ground truth）。键：prompt/cached/output/
        prompt_seconds/predicted_seconds/draft/accepted/drafts/per_position。"""
        with self._lock:
            self.counters["llamacpp:prompt_tokens_total"] += float(inc.get("prompt", 0))
            self.counters["llamacpp:prompt_tokens_cached_total"] += float(inc.get("cached", 0))
            self.counters["llamacpp:prompt_seconds_total"] += float(inc.get("prompt_seconds", 0))
            self.counters["llamacpp:tokens_predicted_total"] += float(inc.get("output", 0))
            self.counters["llamacpp:tokens_predicted_seconds_total"] += float(inc.get("predicted_seconds", 0))
            self.counters["llamacpp:spec_decode_num_draft_tokens_total"] += float(inc.get("draft", 0))
            self.counters["llamacpp:spec_decode_num_accepted_tokens_total"] += float(inc.get("accepted", 0))
            self.counters["llamacpp:spec_decode_num_drafts_total"] += float(inc.get("drafts", 0))
            # n_decode_total 与 output 同步（真实 llama.cpp 中两者一致）
            self.counters["llamacpp:n_decode_total"] += float(inc.get("output", 0))
            gt = self.ground_truth
            for key in ("prompt", "cached", "output", "prompt_seconds",
                        "predicted_seconds", "draft", "accepted", "drafts"):
                gt[key] += float(inc.get(key, 0))
            for pos, val in (inc.get("per_position") or {}).items():
                self.per_position[str(pos)] = self.per_position.get(str(pos), 0.0) + float(val)
                gt["accepted"] += 0.0  # per-position 是 accepted 的细分，不重复计

    def reset(self, to: float = 0.0) -> None:
        """模拟 llama-server 重启：所有 counter 归 to（默认 0）。

        ground truth 不变（重启前生成的 token 真实发生过）；
        last_observed 保留 monitor 最后读到的值 —— 两者之差即"不可恢复窗口"。
        """
        with self._lock:
            for name in COUNTERS:
                self.counters[name] = float(to)
            self.per_position = {p: float(to) for p in self.per_position}
            self.resets += 1

    def set_offline(self, seconds: float) -> None:
        with self._lock:
            if seconds <= 0:
                self.offline_until = 0.0
            else:
                self.offline_until = time.time() + seconds

    def set_malformed(self, body: str | None) -> None:
        with self._lock:
            self.malformed_body = body

    def set_missing(self, names: list[str] | None) -> None:
        """names=None 恢复全部；否则只暴露这些之外的指标。"""
        with self._lock:
            self.missing = set(names or [])

    # -- 读取 -----------------------------------------------------------------

    def is_offline(self) -> bool:
        with self._lock:
            return time.time() < self.offline_until

    def render_metrics(self) -> str:
        """生成 Prometheus 文本；monitor 成功读取时更新 last_observed。"""
        with self._lock:
            self.observation_count += 1
            for name in COUNTERS:
                self.last_observed[name] = self.counters[name]
            missing = self.missing
            lines: list[str] = []

            def emit(name: str, value: float) -> None:
                if name not in missing:
                    lines.append(f"{name} {value:g}")

            emit("llamacpp:prompt_tokens_total", self.counters["llamacpp:prompt_tokens_total"])
            emit("llamacpp:prompt_tokens_cached_total", self.counters["llamacpp:prompt_tokens_cached_total"])
            emit("llamacpp:prompt_seconds_total", self.counters["llamacpp:prompt_seconds_total"])
            emit("llamacpp:tokens_predicted_total", self.counters["llamacpp:tokens_predicted_total"])
            emit("llamacpp:tokens_predicted_seconds_total", self.counters["llamacpp:tokens_predicted_seconds_total"])
            emit("llamacpp:n_decode_total", self.counters["llamacpp:n_decode_total"])
            emit("llamacpp:spec_decode_num_draft_tokens_total", self.counters["llamacpp:spec_decode_num_draft_tokens_total"])
            emit("llamacpp:spec_decode_num_accepted_tokens_total", self.counters["llamacpp:spec_decode_num_accepted_tokens_total"])
            emit("llamacpp:spec_decode_num_drafts_total", self.counters["llamacpp:spec_decode_num_drafts_total"])
            for pos, val in self.per_position.items():
                if f"{_PER_POS_NAME}{{position={pos}}}" not in missing and _PER_POS_NAME not in missing:
                    lines.append(f'{_PER_POS_NAME}{{position="{pos}"}} {val:g}')
            for name, value in self.gauges.items():
                if name not in missing:
                    lines.append(f"{name} {value:g}")
            return "\n".join(lines) + "\n"

    def take_malformed(self) -> str | None:
        """消费一次性 malformed body（返回并清除）。"""
        with self._lock:
            body, self.malformed_body = self.malformed_body, None
            return body

    def state_snapshot(self) -> dict:
        with self._lock:
            unobservable = {
                name: max(0.0, self.last_observed[name] - self.counters[name])
                for name in COUNTERS
            }
            return {
                "counters": dict(self.counters),
                "per_position": dict(self.per_position),
                "ground_truth": dict(self.ground_truth),
                "last_observed": dict(self.last_observed),
                "unobservable_before_reset": unobservable,
                "offline_until": self.offline_until,
                "missing": sorted(self.missing),
                "resets": self.resets,
                "observation_count": self.observation_count,
            }


_PER_POS_NAME = "llamacpp:spec_decode_num_accepted_tokens_per_pos_total"


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------


def make_handler(server_state: FakeServer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "FakeLlama/1.0"

        def log_message(self, fmt, *args):  # 静默（避免测试噪音）
            pass

        def _json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/metrics":
                if server_state.is_offline():
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                malformed = server_state.take_malformed()
                if malformed is not None:
                    body = malformed.encode("utf-8", "replace")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = server_state.render_metrics().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/test/ground_truth":
                self._json(200, server_state.state_snapshot())
                return
            self._json(404, {"success": False, "error": {"code": "NOT_FOUND", "message": "not found"}})

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except (ValueError, json.JSONDecodeError):
                self._json(400, {"success": False, "error": {"code": "BAD_JSON", "message": "invalid JSON body"}})
                return
            if self.path == "/test/add":
                server_state.add(payload)
                self._json(200, {"success": True, "counters": server_state.counters})
            elif self.path == "/test/reset":
                server_state.reset(to=float(payload.get("to", 0)))
                self._json(200, {"success": True, "resets": server_state.resets})
            elif self.path == "/test/offline":
                server_state.set_offline(float(payload.get("seconds", 0)))
                self._json(200, {"success": True, "offline_until": server_state.offline_until})
            elif self.path == "/test/malformed":
                server_state.set_malformed(str(payload.get("body", "")))
                self._json(200, {"success": True})
            elif self.path == "/test/missing":
                server_state.set_missing(payload.get("metrics"))
                self._json(200, {"success": True, "missing": sorted(server_state.missing)})
            elif self.path == "/test/state":
                self._json(200, server_state.state_snapshot())
            else:
                self._json(404, {"success": False, "error": {"code": "NOT_FOUND", "message": "not found"}})

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="假 llama.cpp metrics 服务器（测试用）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19091)
    parser.add_argument("--seed", type=int, default=0, help="保留参数（gauge 波动种子；第一版固定 gauge）")
    args = parser.parse_args()

    state = FakeServer()
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(state))
    print(f"Fake llama-server listening on http://{args.host}:{args.port}/metrics")
    print("control: POST /test/add | /test/reset | /test/offline | /test/malformed | /test/missing")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
