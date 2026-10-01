#!/usr/bin/env python3
"""Round-6 响应式布局验证（连续 drain 模式，沿用已验证的 verify 结构）。
用法: cdp_ovr_gridprobe.py <ws> <w> <h> <dpr>"""
import sys, socket, struct, base64, os, json, time

WS = sys.argv[1]
W = int(sys.argv[2]); H = int(sys.argv[3]); DPR = float(sys.argv[4])

def send(ws, obj):
    data = json.dumps(obj).encode("utf-8")
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
        if op == 0x9:
            pm = os.urandom(4); hh = bytearray([0x8A, 0x80 | n]); hh += pm
            ws.sendall(bytes(hh) + bytes(b ^ pm[i % 4] for i, b in enumerate(_rx(ws, n))))
            continue
        if n == 126: n = struct.unpack(">H", _rx(ws, 2))[0]
        elif n == 127: n = struct.unpack(">Q", _rx(ws, 8))[0]
        mask = _rx(ws, 4) if masked else None
        data = _rx(ws, n) if n else b""
        if mask: data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if op in (0x1, 0x2): return data.decode("utf-8", "replace")
        if op == 0x8: raise RuntimeError("ws close frame")

def main():
    assert WS.startswith("ws://")
    rest = WS[len("ws://"):]; host, _, path = rest.partition("/")
    host, _, port = host.partition(":")
    ws = socket.create_connection((host, int(port or 80)))
    ws.settimeout(3)
    key = base64.b64encode(os.urandom(16)).decode()
    ws.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        c = ws.recv(4096)
        if not c: raise RuntimeError("handshake failed")
        resp += c

    mid = [0]
    state = {"probe": None}
    def call(method, params=None):
        mid[0] += 1; i = mid[0]
        send(ws, {"id": i, "method": method, "params": params or {}})
        return i

    call("Runtime.enable"); call("Page.enable"); call("Emulation.enable")
    call("Emulation.setDeviceMetricsOverride", {"width": W, "height": H, "deviceScaleFactor": DPR, "mobile": False})
    call("Page.navigate", {"url": "http://127.0.0.1:8790/"})

    probe = (
      "(function(){function cols(sel){var e=document.querySelector(sel);if(!e)return null;var v=getComputedStyle(e).gridTemplateColumns;"
      "return v.split(' ').filter(function(x){return x&&parseFloat(x)>0}).length;}"
      "function raw(sel){var e=document.querySelector(sel);if(!e)return null;return getComputedStyle(e).gridTemplateColumns;}"
      "var t=document.getElementById('ovTodayLogical');var tlog=t?t.textContent.trim():null;"
      "var ie=document.querySelector('.ov-integrity-grid');var iwrap=ie?ie.parentElement:null;"
      "return JSON.stringify({width:window.innerWidth,tlog:tlog,"
      "todayBreakdown:cols('.ov-today-breakdown'),inference:cols('.ov-inference-grid'),host:cols('.ov-host-grid'),gpu:cols('.ov-gpu-grid'),integrity:cols('.ov-integrity-grid'),"
      "integrityComputed:ie?getComputedStyle(ie).gridTemplateColumns:null,"
      "integrityClientW:ie?ie.clientWidth:null,"
      "integrityChildren:ie?ie.children.length:0,"
      "integrityFirstSpan:ie&&ie.children[0]?(function(){var r=ie.children[0].getBoundingClientRect();var g=getComputedStyle(ie).gridTemplateColumns;return {x:Math.round(r.left),w:Math.round(r.width)};})():null,"
      "todayRaw:raw('.ov-today-breakdown'),integrityRaw:raw('.ov-integrity-grid'),"
      "gpuCards:document.querySelectorAll('#ovGpuMini .gpu-mini').length,"
      "attItems:document.querySelectorAll('#ovAttentionList .ov-attention-item').length"
      "});})()"
    )

    def drain_until(i, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            try: raw = read_msg(ws)
            except socket.timeout: continue
            except Exception: return None
            try: j = json.loads(raw)
            except Exception: continue
            if j.get("id") == i:
                r = j.get("result", {})
                if "result" in r and isinstance(r["result"], dict) and "value" in r["result"]:
                    return r["result"]["value"]
                return r
        return None

    # keep reading; poll the probe every ~2s via drain
    call("Runtime.evaluate", {"expression": "try{LM.nav.showPage('overview')}catch(e){};1", "returnByValue": True})
    for attempt in range(12):
        i = call("Runtime.evaluate", {"expression": probe, "returnByValue": True})
        v = drain_until(i, timeout=8)
        if v:
            try:
                d = json.loads(v)
                if d.get("tlog") not in (None, "--"):
                    print(json.dumps(d, ensure_ascii=False)); return
            except Exception:
                pass
        time.sleep(1.5)
    print("no-data")

if __name__ == "__main__":
    main()
