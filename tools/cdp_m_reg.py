#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用确定性验证驱动：navigate → 可选 showPage → 耐心等（不 re-enter，避免
gen 递增把在途响应判 stale；40s 仍空 re-enter 一次再等）→ 跑 measure JS。
用法: cdp_m_reg.py <ws> <w> <h> <page> <probejs> <measurejs> [timeout_s]"""
import sys, socket, struct, base64, os, json, time
exec(compile(open("tools/cdp_m_waitprobe.py", encoding="utf-8").read().split("def main")[0], "wp", "exec"))

def main():
    ws_url, w, h, page = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
    probejs = open(sys.argv[5], encoding="utf-8").read()
    measurejs = open(sys.argv[6], encoding="utf-8").read()
    timeout_s = int(sys.argv[7]) if len(sys.argv) > 7 else 40
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
    def call(method, params=None, timeout=30):
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
                r = j.get("result", {}); v = r.get("result", r)
                if isinstance(v, dict) and "value" in v: return v["value"]
                return v
        return None
    def poll():
        v = call("Runtime.evaluate", {"expression": probejs, "returnByValue": True})
        try: return int(v)
        except Exception: return 0
    call("Page.enable")
    call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 2, "mobile": w < 761})
    call("Page.navigate", {"url": "http://127.0.0.1:8790/"})
    time.sleep(4.0)
    call("Runtime.evaluate", {"expression": "window.__lmSetVisible && window.__lmSetVisible(true); 1", "returnByValue": True})
    if page and page != "none":
        call("Runtime.evaluate", {"expression": "LM.nav.showPage(%s); 1" % json.dumps(page), "returnByValue": True})
    # 每次尝试给该 fetch 完整的耐心窗口（慢 API 如 gaps 冷启动可达 30s+；
    # 中途重进会让 gen 递增把在途响应判 stale 丢弃）。整窗仍空才重进再试一轮。
    n = 0
    for _attempt in range(2):
        for _ in range(timeout_s):
            time.sleep(1.0)
            n = poll()
            if n > 0: break
        if n > 0: break
        if page and page != "none":
            call("Runtime.evaluate", {"expression": "LM.nav.showPage('overview'); 1", "returnByValue": True})
            time.sleep(0.5)
            call("Runtime.evaluate", {"expression": "LM.nav.showPage(%s); 1" % json.dumps(page), "returnByValue": True})
    v = call("Runtime.evaluate", {"expression": measurejs, "returnByValue": True})
    print("ROWS:" + str(n))
    print(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))

if __name__ == "__main__":
    main()
