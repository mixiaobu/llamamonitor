#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Timer 泄漏精确复测：稳态（所有页都访问过）后做 N 次切换，前后对比 timer 计数。
若 delta 为个位数 -> 收敛（无泄漏）；若随 N 线性增长 -> 泄漏。
用法: cdp_m_timer2.py <ws> <n>
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
        b0, b1 = _recv(ws, 2); op = b0 & 0x0F
        if op == 0x8: raise TimeoutError("ws closed")
        masked = b1 & 0x80; n = b1 & 0x7F
        if n == 126: n = struct.unpack(">H", _recv(ws, 2))[0]
        elif n == 127: n = struct.unpack(">Q", _recv(ws, 8))[0]
        mk = _recv(ws, 4) if masked else None
        data = _recv(ws, n) if n else b""
        if mk: data = bytes(b ^ mk[i % 4] for i, b in enumerate(data))
        if op in (0x1, 0x2): return data.decode("utf-8", "replace")

def main():
    ws_url, n = sys.argv[1], int(sys.argv[2] if len(sys.argv) > 2 else 40)
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
                v = r.get("result", r)
                if isinstance(v, dict) and "value" in v: return v["value"]
                return v
        return None
    def ev(expr):
        v = call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return v
    call("Page.enable")
    call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True})
    call("Page.navigate", {"url": "http://127.0.0.1:8790/"})
    time.sleep(4.0)
    ev("(function(){ window.__tset=new Set(); var _st=window.setTimeout,_ct=window.clearTimeout; window.setTimeout=function(){var id=_st.apply(window,arguments);window.__tset.add(id);return id;}; window.clearTimeout=function(id){_ct.call(window,id);window.__tset.delete(id);}; return 1;})()")
    # 预热：把 8 页都点一遍（走真实底栏点击）
    pages = ["usage","performance","gpu","system","history","settings","about","overview"]
    for p in pages:
        ev("document.querySelector('.mnav-item[data-page=\"%s\"]') ? document.querySelector('.mnav-item[data-page=\"%s\"]').click() : LM.nav.showPage('%s'); 1" % (p,p,p))
        time.sleep(0.4)
    time.sleep(3.0)
    before = int(ev("window.__tset.size"))
    for i in range(n):
        p = pages[i % len(pages)]
        ev("LM.nav.showPage('%s'); 1" % p)
    time.sleep(3.0)
    after = int(ev("window.__tset.size"))
    # 再等 5s（轮询自调度一轮后）看是否继续涨
    time.sleep(5.0)
    later = int(ev("window.__tset.size"))
    print(json.dumps({"before": before, "after_%d_switches" % n: after,
                      "delta": after - before, "later(+5s)": later,
                      "converged": abs(after - before) <= 8 and abs(later - after) <= 8}))

if __name__ == "__main__":
    main()
