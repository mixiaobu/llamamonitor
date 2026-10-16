# -*- coding: utf-8 -*-
"""
1.3.3 Edge 浏览器实测（spec §42/§43）——CDP 自动化（Microsoft Edge）。

验证三件事（真实 Microsoft Edge，headless=new，非 mock 服务器）：
  T1  /api/realtime 以 ~1s 节奏轮询 + no-store（响应头 Cache-Control:no-store，
      不从磁盘缓存读，fromDiskCache=false）。
  T2  竞态防护（§23）：迟到的旧响应（sample_timestamp 更旧）不覆盖新数据。
      用 CDP Fetch 拦截注入合成响应，观察 #rtSlots（llama 活跃 Slot，1s 实时
      任务独占写入）：旧值被拒、新值被收。
  T3  hidden tab 跳过 1s 轮询（visibleOnly）：把 document.visibilityState 置
      hidden 后，/api/realtime 请求停止。

运行：python tools\_edge_verify.py
输出：tools\edge_verify_result.txt
"""
import base64
import json
import os
import subprocess
import tempfile
import time
import urllib.request
from collections import Counter

import websocket  # websocket-client

APP_URL = "http://127.0.0.1:8790"
DEBUG_PORT = 9333
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "edge_verify_result.txt")

_lines = []


def log(msg):
    print(msg, flush=True)
    _lines.append(msg)


def flush():
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(_lines) + "\n")


# ---------------------------------------------------------------------------
# CDP 客户端（事件负载在 params 下）
# ---------------------------------------------------------------------------
class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=30)
        self._id = 0
        self.events = []
        self.handlers = []

    def send(self, method, params=None):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(method + " -> " + json.dumps(msg["error"], ensure_ascii=False))
                return msg.get("result", {})
            self._dispatch(msg)

    def on(self, method, handler):
        self.handlers.append((method, handler))

    def _dispatch(self, msg):
        if "method" in msg:
            self.events.append(msg)
            for m, h in self.handlers:
                if msg["method"] == m:
                    h(msg)

    def drain(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            remain = end - time.time()
            if remain <= 0:
                break
            self.ws.settimeout(min(remain, 0.5))
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                break
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if "method" in msg:
                self._dispatch(msg)
        self.ws.settimeout(30)

    def req_url(self, m):
        return m.get("params", {}).get("request", {}).get("url", "")

    def evaluate(self, expression, await_promise=False):
        r = self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        })
        if "exceptionDetails" in r:
            return {"__exception__": r["exceptionDetails"].get("exception", {}).get("description", "?")}
        return r.get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def wait_for_page(debug_port, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/json" % debug_port, timeout=2) as r:
                targets = json.loads(r.read().decode("utf-8"))
        except Exception:
            time.sleep(0.3)
            continue
        for t in targets:
            if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                return t["webSocketDebuggerUrl"]
        time.sleep(0.3)
    raise RuntimeError("no page target appeared")


def launch_edge():
    user_data = tempfile.mkdtemp(prefix="lm_edge_")
    args = [
        EDGE,
        "--remote-debugging-port=%d" % DEBUG_PORT,
        "--remote-allow-origins=*",
        "--user-data-dir=" + user_data,
        "--no-first-run", "--no-default-browser-check",
        "--disable-background-networking",
        "--headless=new",
        "--window-size=1280,900",
        APP_URL,
    ]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def rt_requests(cdp):
    """从 cdp.events 提取 /api/realtime 的 requestWillBeSent（按时间戳）。"""
    out = []
    for e in cdp.events:
        if e.get("method") == "Network.requestWillBeSent":
            if "/api/realtime" in cdp.req_url(e):
                out.append(e.get("params", {}).get("timestamp", 0))
    return out


def main():
    edge = launch_edge()
    t1_pass = t2_pass = t3_pass = False
    try:
        ws_url = wait_for_page(DEBUG_PORT)
        cdp = CDP(ws_url)
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        cdp.send("Network.enable")
        cdp.drain(4.0)

        # 切到性能页 + 确保可见（headless 已 visible，此处幂等保险）
        cdp.evaluate("document.querySelector('[data-page=\"performance\"]').click()")
        cdp.evaluate("Object.defineProperty(document,'visibilityState',"
                     "{get:function(){return 'visible'},configurable:true});true")
        cdp.drain(1.0)
        log("页面: %s  当前页: %s  isAppVisible: %s" % (
            cdp.evaluate("location.href"),
            cdp.evaluate("LM.nav.currentPage()"),
            cdp.evaluate("LM.poll.isAppVisible()"),
        ))

        # ============================================================ T1
        log("\n===== T1：/api/realtime 轮询节奏 + no-store（采样 12s）=====")
        cdp.events.clear()
        cdp.drain(12.0)

        req_ts = rt_requests(cdp)
        gaps = [round((req_ts[i + 1] - req_ts[i]) * 1000) for i in range(len(req_ts) - 1)]
        med = 0
        if gaps:
            gs = sorted(gaps)
            med = gs[len(gs) // 2]
            log("请求数: %d（12s）  间隔(ms): %s" % (len(req_ts), gaps))
            log("间隔中位数: %d ms  最大: %d ms  最小: %d ms" % (med, max(gaps), min(gaps)))
        else:
            log("请求数: %d（过少）" % len(req_ts))

        resp = [e for e in cdp.events if e.get("method") == "Network.responseReceived"
                and "/api/realtime" in e.get("params", {}).get("response", {}).get("url", "")]
        if resp:
            def cc(e):
                return (e["params"]["response"].get("headers", {}) or {}).get("cache-control", "")
            no_store = all("no-store" in cc(e) for e in resp)
            no_disk = all(not e["params"]["response"].get("fromDiskCache", False) for e in resp)
            log("响应数: %d  Cache-Control 含 no-store: %s  fromDiskCache 全 false: %s"
                % (len(resp), no_store, no_disk))
            log("示例 Cache-Control: %s" % (cc(resp[0]) or "(无)"))
            t1_pass = (len(req_ts) >= 8 and med <= 1300 and no_store and no_disk)
        else:
            log("未捕获 /api/realtime 响应（异常）")
        log("T1 结论: %s" % ("PASS" if t1_pass else "FAIL"))

        # ============================================================ T2
        log("\n===== T2：竞态防护（§23 迟到旧响应不覆盖新数据）=====")
        now = time.time()

        def synth(ts, seq, active, total):
            return {
                "provider": "llamacpp",
                "server_online": True,
                "activity": {"active": True, "phase": "decode"},
                "throughput": {"current_decode_tps": 12.5, "average_decode_tps": None,
                               "current_prompt_tps": None, "average_prompt_tps": None},
                "requests": {"processing": 1, "waiting": 0},
                "slots": {"active": active, "total": total},
                "tokens": {"generated": 100, "max_tokens": 1000},
                "freshness": {"sample_timestamp": ts, "state": "live", "age_ms": 100},
                "errors": {},
                "diagnostics": {},
                "sequence": seq,
            }

        cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*api/realtime*", "requestStage": "Response"}]})
        queue = {"body": base64.b64encode(json.dumps(synth(now + 10, 1, 1, 2)).encode()).decode()}

        def on_paused(m):
            cdp.send("Fetch.fulfillRequest", {
                "requestId": m["params"]["requestId"],
                "responseCode": 200,
                "responseHeaders": [
                    {"name": "Content-Type", "value": "application/json; charset=utf-8"},
                    {"name": "Cache-Control", "value": "no-store"},
                ],
                "body": queue["body"],
            })

        cdp.on("Fetch.requestPaused", on_paused)

        def rt_text():
            return cdp.evaluate("document.getElementById('rtSlots').textContent")

        log("基线 #rtSlots: %r" % rt_text())
        # A：新（必大于真实值）-> 1/2 应被收
        queue["body"] = base64.b64encode(json.dumps(synth(now + 10, 1, 1, 2)).encode()).decode()
        cdp.drain(2.5)
        a = rt_text()
        log("A 注入 ts=+10 seq=1 slots=1/2 -> #rtSlots=%r" % a)
        # B：更旧 -> 5/9 应被拒（保持 1/2）
        queue["body"] = base64.b64encode(json.dumps(synth(now, 0, 5, 9)).encode()).decode()
        cdp.drain(2.5)
        b = rt_text()
        log("B 注入 ts=now  seq=0 slots=5/9（更旧）-> #rtSlots=%r" % b)
        # C：更新 -> 3/7 应被收
        queue["body"] = base64.b64encode(json.dumps(synth(now + 60, 2, 3, 7)).encode()).decode()
        cdp.drain(2.5)
        c = rt_text()
        log("C 注入 ts=+60 seq=2 slots=3/7（更新）-> #rtSlots=%r" % c)
        cdp.send("Fetch.disable")
        t2_pass = (a == "1 / 2" and b == "1 / 2" and c == "3 / 7")
        log("T2 结论: %s" % ("PASS（旧值被拒、新值被收）" if t2_pass else "FAIL"))

        # ============================================================ T3
        log("\n===== T3：hidden tab 跳过 1s 轮询（visibleOnly）=====")
        cdp.evaluate("Object.defineProperty(document,'visibilityState',"
                     "{get:function(){return 'hidden'},configurable:true});"
                     "document.dispatchEvent(new Event('visibilitychange'));true")
        cdp.drain(0.5)  # 让在途请求落地
        cdp.events.clear()
        log("已置 visibilityState=hidden，采样 4s……")
        cdp.drain(4.0)
        hidden_reqs = rt_requests(cdp)
        log("hidden 期间 /api/realtime 请求数: %d" % len(hidden_reqs))
        t3_pass = len(hidden_reqs) <= 1
        log("T3 结论: %s" % ("PASS（hidden 不轮询）" if t3_pass else "FAIL"))
        cdp.evaluate("delete document.visibilityState;"
                     "document.dispatchEvent(new Event('visibilitychange'));true")
        cdp.close()

        log("\n===== 汇总 =====")
        log("T1(节奏+no-store)=%s  T2(竞态)=%s  T3(hidden)=%s" % (
            "PASS" if t1_pass else "FAIL",
            "PASS" if t2_pass else "FAIL",
            "PASS" if t3_pass else "FAIL"))
        log("全部通过: %s" % ("是" if (t1_pass and t2_pass and t3_pass) else "否"))
    finally:
        try:
            edge.terminate()
            edge.wait(timeout=10)
        except Exception:
            try:
                edge.kill()
            except Exception:
                pass
    flush()


if __name__ == "__main__":
    main()
