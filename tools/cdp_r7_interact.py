#!/usr/bin/env python3
"""Round-7 交互态验证：fresh 导航到 settings，等表单加载，跑交互探针。
用法: cdp_r7_interact.py <ws>
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
        if op == 0x9:
            pm = os.urandom(4); hh = bytearray([0x8A, 0x80 | len(data)]); hh += pm
            ws.sendall(bytes(hh) + bytes(b ^ pm[i % 4] for i, b in enumerate(data)))

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
    def call(method, params=None, timeout=25):
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
                return r.get("value", r)
        return None
    call("Runtime.enable"); call("Page.enable")
    call("Emulation.setDeviceMetricsOverride", {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})
    call("Page.navigate", {"url": "http://127.0.0.1:8790/"})
    time.sleep(3.0)
    call("Runtime.evaluate", {"expression": "LM.nav.showPage('settings');1", "returnByValue": True})
    # 等表单加载（最多 12s）
    loaded = False
    for _ in range(12):
        time.sleep(1.0)
        v = call("Runtime.evaluate", {"expression": "JSON.stringify({loaded:LM.settings.isLoaded(),poll:(document.getElementById('setPollInterval')||{}).value})", "returnByValue": True})
        if v and '"loaded":true' in str(v):
            loaded = True
            break
    # 跑交互探针
    with open(r"tools/_r7_interact.js", "r", encoding="utf-8") as f:
        probe = f.read()
    r = call("Runtime.evaluate", {"expression": probe, "returnByValue": True})
    print(json.dumps({"loaded": loaded, "interaction": r}, ensure_ascii=False))

if __name__ == "__main__":
    main()
