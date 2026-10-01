#!/usr/bin/env python3
"""Round-6 Overview CDP 验证（带 socket 超时 + 逐行 flush）。
用法: cdp_ovr_r6_verify.py <ws_url>"""
import sys, socket, struct, base64, os, json, time

WS = sys.argv[1]

def send(ws, text):
    data = text.encode("utf-8")
    h = bytearray([0x81]); n = len(data)
    if n < 126: h.append(0x80 | n)
    elif n < 65536: h.append(0x80 | 126); h += struct.pack(">H", n)
    else: h.append(0x80 | 127); h += struct.pack(">Q", n)
    mask = os.urandom(4); h += mask
    ws.sendall(bytes(h) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

def _rx(ws, n):
    buf = b""
    while len(buf) < n:
        c = ws.recv(n - len(buf))
        if not c: raise RuntimeError("closed")
        buf += c
    return buf

def read_msg(ws):
    while True:
        b0, b1 = _rx(ws, 2)
        op = b0 & 0x0F; masked = b1 & 0x80; n = b1 & 0x7F
        if n == 126: n = struct.unpack(">H", _rx(ws, 2))[0]
        elif n == 127: n = struct.unpack(">Q", _rx(ws, 8))[0]
        mask = _rx(ws, 4) if masked else None
        data = _rx(ws, n) if n else b""
        if mask: data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if op in (0x1, 0x2): return data.decode("utf-8", "replace")

def main():
    assert WS.startswith("ws://")
    rest = WS[len("ws://"):]; host, _, path = rest.partition("/")
    host, _, port = host.partition(":")
    ws = socket.create_connection((host, int(port or 80)))
    ws.settimeout(4)
    key = base64.b64encode(os.urandom(16)).decode()
    ws.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        c = ws.recv(4096)
        if not c: raise RuntimeError("handshake failed")
        resp += c

    state = {"mid": 0, "console": [], "exc": [], "ovr": 0, "other": {}, "pend": {}}
    def call(method, params=None):
        state["mid"] += 1; i = state["mid"]
        send(ws, json.dumps({"id": i, "method": method, "params": params or {}}))
        return i

    call("Runtime.enable"); call("Page.enable"); call("Network.enable")
    call("Page.navigate", {"url": "http://127.0.0.1:8790/"})

    def drain(until_id, timeout=8):
        end = time.time() + timeout
        got = None
        while time.time() < end:
            try: raw = read_msg(ws)
            except socket.timeout: continue
            except Exception: break
            try: j = json.loads(raw)
            except Exception: continue
            m = j.get("method")
            if m == "Runtime.consoleAPICalled":
                a = j.get("params", {}).get("args", [])
                state["console"].append((j.get("params", {}).get("type", "log"),
                                         " ".join((x.get("value") or x.get("description") or "") for x in a)))
            elif m == "Runtime.exceptionThrown":
                d = j.get("params", {}).get("exceptionDetails", {})
                state["exc"].append(str((d.get("exception") or {}).get("description") or d.get("text", ""))[:300])
            elif m == "Network.responseReceived":
                u = j.get("params", {}).get("response", {}).get("url", "")
                if "/api/overview" in u: state["ovr"] += 1
                else:
                    for k in ("/api/status","/api/summary","/api/data/quality","/api/system/status","/api/gpu/status","/api/mtp","/api/runtime"):
                        if k in u: state["other"][k] = state["other"].get(k,0)+1; break
            if j.get("id") == until_id:
                got = (j.get("result") or {}).get("result", {}).get("value")
                break
        return got

    # Phase 1: navigate + let it load ~20s (box may nap; wait for data)
    # Evaluate probe repeatedly until data present.
    probe = (
      "var g=function(i){var e=document.getElementById(i);return e?e.textContent.trim():null};var h=function(i){var e=document.getElementById(i);return e?e.hidden:null};"
      "JSON.stringify({badge:g('ovServerState'),ctx:g('ovContext'),slots:g('ovSlots'),modal:g('ovModal'),"
      "attHidden:h('ovAttention'),attItems:(document.querySelectorAll('#ovAttentionList .ov-attention-item')).length,attMore:g('ovAttentionMoreText'),"
      "tLog:g('ovTodayLogical'),tComp:g('ovTodayCompute'),tRate:g('ovTodayCacheRate'),"
      "pTps:g('ovPromptTps'),dTps:g('ovDecodeTps'),reqP:g('ovRequestsProcessing'),reqD:g('ovRequestsDeferred'),mtp:g('ovMtpRate'),cc:g('ovCurrentContext'),"
      "hCpu:g('ovHostCpu'),hMem:g('ovHostMem'),hPow:g('ovHostPower'),hUptime:g('ovHostUptime'),"
      "gpuState:g('ovGpuState'),gpuCards:(document.querySelectorAll('#ovGpuMini .gpu-mini')).length,"
      "cov:g('dqCoverage'),gaps:g('dqGapsToday'),risk:g('dqTokenRisk'),db:g('dqDb'),dbHint:g('dqDbHint'),lastS:g('dqLastSample')})"
    )
    vals = None
    for attempt in range(12):
        i = call("Runtime.evaluate", {"expression": probe, "returnByValue": True})
        got = drain(i, timeout=8)
        if got:
            try:
                v = json.loads(got)
                vals = v
                if v.get("tLog") not in (None, "--"):
                    break
            except Exception:
                pass
        time.sleep(2)

    print("ovr_requests:", state["ovr"], flush=True)
    print("other_apis:", json.dumps(state["other"], ensure_ascii=False), flush=True)
    print("DOM:", json.dumps(vals, ensure_ascii=False), flush=True)
    print("CONSOLE_WARN_ERR:", flush=True)
    for lvl, txt in state["console"]:
        if lvl in ("error", "warning"):
            print("  [%s] %s" % (lvl, str(txt)[:180]), flush=True)
    print("EXCEPTIONS:", flush=True)
    for e in state["exc"]:
        print("  EXC:", e, flush=True)
    if not state["exc"]: print("  (none)", flush=True)

if __name__ == "__main__":
    main()
