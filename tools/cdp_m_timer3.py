#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""决定性 Timer 泄漏测试（fire-aware）：timer 触发时也从集合移除 -> 集合大小 = 真实活跃（未触发未清除）timer 数。
预热全部页面 -> 稳态基线 -> 3 轮 x 40 次底栏/页面切换 -> 每轮充分 settle 后对比。
收敛（各轮相当）= 无泄漏；逐轮递增 = 泄漏。
用法: cdp_m_timer3.py <ws>
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
    ws_url = sys.argv[1]
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
    def ev(e):
        return call("Runtime.evaluate", {"expression": e, "returnByValue": True})
    call("Page.enable")
    call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True})
    call("Page.navigate", {"url": "http://127.0.0.1:8790/"})
    time.sleep(4.0)
    ev("""(function(){
      window.__live = new Set();
      var _st = window.setTimeout, _ct = window.clearTimeout;
      window.setTimeout = function(fn, ms){
        var id = _st(function(){ window.__live.delete(id); return fn.apply(this, arguments); }, ms);
        window.__live.add(id); return id;
      };
      window.clearTimeout = function(id){ _ct.call(window, id); window.__live.delete(id); };
      return 1;
    })()""")
    pages = ["usage","performance","gpu","system","history","settings","about","overview"]
    for p in pages:
        ev("LM.nav.showPage(%s); 1" % json.dumps(p))
        time.sleep(0.4)
    time.sleep(5.0)
    res = {"steady_before": int(ev("window.__live.size"))}
    for rnd in range(3):
        for i in range(40):
            ev("LM.nav.showPage(%s); 1" % json.dumps(pages[i % 8]))
        time.sleep(8.0)  # 充分 settle：短 timer 全部触发或被清除
        res["after_round_%d" % (rnd + 1)] = int(ev("window.__live.size"))
    print(json.dumps(res))

if __name__ == "__main__":
    main()
