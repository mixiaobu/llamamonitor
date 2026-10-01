#!/usr/bin/env python3
"""Orientation 测试（§242-243/§285-289）：390x844 竖屏 -> 844x390 横屏 -> 回到竖屏。
验证：
- 图表实例数稳定（不重建）
- 图表 canvas 宽度跟随视口（ResizeObserver resize 生效）
- Bottom Nav 横竖屏都在
- 无横滚
用法: cdp_m_orient.py <ws>
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
    def ev(expr):
        v = call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return json.loads(v) if isinstance(v, str) else v
    PROBE = """
      (function(){
        var c = document.querySelector('.content');
        var mnav = document.querySelector('.mobile-nav');
        var canvases = Array.prototype.slice.call(document.querySelectorAll('canvas')).filter(function(x){return x.offsetParent;});
        return JSON.stringify({
          vw: window.innerWidth, vh: window.innerHeight,
          charts: LM.charts ? LM.charts.instanceCount() : -1,
          maxCanvasW: Math.max.apply(null, canvases.map(function(x){return x.clientWidth;}).concat([0])),
          mnavDisplay: mnav ? getComputedStyle(mnav).display : null,
          docHS: document.documentElement.scrollWidth > window.innerWidth + 1,
          contHS: c ? c.scrollWidth > c.clientWidth + 1 : null
        });
      })()
    """
    call("Page.enable")
    # 竖屏
    call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True})
    call("Page.navigate", {"url": "http://127.0.0.1:8790/"})
    time.sleep(4.0)
    call("Runtime.evaluate", {"expression": "LM.nav.showPage('usage'); 1", "returnByValue": True})
    time.sleep(2.0)
    out = {"portrait": ev(PROBE)}
    # 横屏
    call("Emulation.setDeviceMetricsOverride", {"width": 844, "height": 390, "deviceScaleFactor": 2, "mobile": True})
    time.sleep(2.5)
    out["landscape"] = ev(PROBE)
    # 回竖屏
    call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True})
    time.sleep(2.5)
    out["portrait2"] = ev(PROBE)
    print(json.dumps(out, ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
