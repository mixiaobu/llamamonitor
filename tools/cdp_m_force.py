#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""navigate → 跑 async 测量 JS（awaitPromise，长超时）→ 打印。
用法: cdp_m_force.py <ws> <w> <h> <page> <jsfile>
"""
import sys, socket, struct, base64, os, json, time
exec(compile(open("tools/cdp_m_waitprobe.py", encoding="utf-8").read().split("def main")[0], "wp", "exec"))

def main():
    ws_url, w, h, page, jsfile = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4], sys.argv[5]
    with open(jsfile, "r", encoding="utf-8") as f: js = f.read()
    rest = ws_url[len("ws://"):]; host, _, path = rest.partition("/")
    host, _, port = host.partition(":")
    ws = socket.create_connection((host, int(port or 80)))
    key = base64.b64encode(os.urandom(16)).decode()
    ws.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        c = ws.recv(4096)
        if not c: raise RuntimeError("ws handshake failed")
        resp += c
    mid = [0]
    def call(method, params=None, timeout=60):
        mid[0] += 1
        send(ws, {"id": mid[0], "method": method, "params": params or {}})
        end = time.time() + timeout
        while time.time() < end:
            ws.settimeout(2.0)
            try: raw = read_msg(ws)
            except socket.timeout: continue
            except Exception: return None
            try: j = json.loads(raw)
            except Exception: continue
            if j.get("id") == mid[0]:
                r = j.get("result", {})
                if "exceptionDetails" in r:
                    det = r["exceptionDetails"]
                    txt = (det.get("exception") or {}).get("description") or det.get("text", "exception")
                    print("JS EXCEPTION: " + str(txt)[:1500], file=sys.stderr)
                v = r.get("result", r)
                if isinstance(v, dict) and "value" in v: return v["value"]
                return v
        return None
    call("Page.enable")
    call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 2 if w < 761 else 1, "mobile": w < 761})
    call("Page.navigate", {"url": "http://127.0.0.1:8790/"})
    time.sleep(4.0)
    call("Runtime.evaluate", {"expression": "window.__lmSetVisible && window.__lmSetVisible(true); 1", "returnByValue": True})
    if page and page != "none":
        call("Runtime.evaluate", {"expression": "LM.nav.showPage(%s); 1" % json.dumps(page), "returnByValue": True})
    time.sleep(2.0)
    v = call("Runtime.evaluate", {"expression": js, "returnByValue": True, "awaitPromise": True}, timeout=50)
    print(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))

if __name__ == "__main__":
    main()
