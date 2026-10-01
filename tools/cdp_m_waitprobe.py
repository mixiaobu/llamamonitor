#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""等待数据出现后测量布局（awaitPromise 版）。
用法: cdp_m_waitprobe.py <ws> <w> <h> <page> <jsfile>
"""
import sys, socket, struct, base64, os, json, time

def send(ws, obj):
    data = json.dumps(obj).encode("utf-8"); h = bytearray([0x81]); n = len(data)
    if n < 126: h.append(0x80 | n)
    elif n < 65536: h.append(0x80 | 126); h += struct.pack(">H", n)
    else: h.append(0x80 | 127); h += struct.pack(">Q", n)
    m = os.urandom(4); h += m
    ws.sendall(bytes(h) + bytes(b ^ m[i % 4] for i, b in enumerate(data)))

def _recv(ws, n):
    buf = b""
    while len(buf) < n:
        c = ws.recv(n - len(buf))
        if not c: raise RuntimeError("ws closed")
        buf += c
    return buf

def read_msg(ws):
    while True:
        b0, b1 = _recv(ws, 2)
        if b0 & 0x8 == 0x8: raise TimeoutError("ws closed")
        op = b0 & 0x0F; n = b1 & 0x7F
        if n == 126: n = struct.unpack(">H", _recv(ws, 2))[0]
        elif n == 127: n = struct.unpack(">Q", _recv(ws, 8))[0]
        mk = _recv(ws, 4) if b1 & 0x80 else None
        data = _recv(ws, n) if n else b""
        if mk: data = bytes(b ^ mk[i % 4] for i, b in enumerate(data))
        if op in (1, 2): return data.decode("utf-8", "replace")

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
    def call(method, params=None, timeout=90):
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
                    print("JS EXCEPTION: " + str(txt)[:2000], file=sys.stderr); sys.exit(3)
                v = r.get("result", r)
                if isinstance(v, dict) and "value" in v: return v["value"]
                return v
        return None
    call("Page.enable")
    call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 2 if w < 761 else 1, "mobile": w < 761})
    call("Page.navigate", {"url": "http://127.0.0.1:8790/"})
    time.sleep(3.0)
    if page and page != "none":
        call("Runtime.evaluate", {"expression": "LM.nav.showPage(%s); 1" % json.dumps(page), "returnByValue": True})
    time.sleep(1.5)
    v = call("Runtime.evaluate", {"expression": js, "returnByValue": True, "awaitPromise": True})
    print(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))

if __name__ == "__main__":
    main()
