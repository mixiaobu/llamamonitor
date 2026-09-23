# -*- coding: utf-8 -*-
"""Real phone simulation: navigate via LAN IP so /api/config &
/api/update/status return GENUINE 403 from the server (no fetch patch).
Collects console messages + 403 responses + UI state."""
import sys, socket, struct, base64, os, json, time

def send(ws, obj):
    data = json.dumps(obj).encode("utf-8")
    header = bytearray([0x81])
    n = len(data)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    mask = os.urandom(4)
    header += mask
    ws.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

def _recv_exact(ws, n):
    buf = b""
    while len(buf) < n:
        chunk = ws.recv(n - len(buf))
        if not chunk:
            raise RuntimeError("ws closed")
        buf += chunk
    return buf

def read_msg(ws):
    while True:
        b0, b1 = _recv_exact(ws, 2)
        opcode = b0 & 0x0F
        if opcode == 0x8:
            raise TimeoutError("ws closed")
        masked = b1 & 0x80
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", _recv_exact(ws, 2))[0]
        elif n == 127:
            n = struct.unpack(">Q", _recv_exact(ws, 8))[0]
        mask = _recv_exact(ws, 4) if masked else None
        data = _recv_exact(ws, n) if n else b""
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if opcode in (0x1, 0x2):
            return data.decode("utf-8", "replace")
        if opcode == 0x9:
            pm = os.urandom(4)
            h = bytearray([0x8A, 0x80 | len(data)])
            h += pm
            ws.sendall(bytes(h) + bytes(b ^ pm[i % 4] for i, b in enumerate(data)))

def main():
    ws_url, target, w, h, jsfile = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
    rest = ws_url[len("ws://"):]
    host, _, path = rest.partition("/")
    host, _, port = host.partition(":")
    ws = socket.create_connection((host, int(port or 80)))
    key = base64.b64encode(os.urandom(16)).decode()
    ws.sendall((
        f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        resp += ws.recv(4096)

    mid = [0]
    events = []
    def call(method, params=None, timeout=30):
        mid[0] += 1
        send(ws, {"id": mid[0], "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            ws.settimeout(2.0)
            raw = read_msg(ws)
            try:
                j = json.loads(raw)
            except Exception:
                continue
            if "method" in j:
                events.append(j)
                continue
            if j.get("id") == mid[0]:
                return j.get("error") or j.get("result", {})
        return {"_timeout": True}

    call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": True})
    call("Page.enable")
    call("Runtime.enable")
    call("Network.enable")
    call("Page.navigate", {"url": target})
    time.sleep(10)

    # drain pending events
    ws.settimeout(2.0)
    try:
        while True:
            raw = read_msg(ws)
            j = json.loads(raw)
            if "method" in j:
                events.append(j)
    except Exception:
        pass

    console = []
    http403 = []
    for ev in events:
        m = ev.get("method")
        p = ev.get("params", {})
        if m == "Runtime.consoleAPICalled":
            args = p.get("args", [])
            txt = " ".join(str(a.get("value", a.get("description", ""))) for a in args)
            console.append((p.get("type"), txt[:160]))
        elif m == "Network.responseReceived":
            resp = p.get("response", {})
            if resp.get("status") == 403:
                http403.append(resp.get("url"))

    js = open(jsfile, encoding="utf-8").read()
    r = call("Runtime.evaluate", {"expression": js, "returnByValue": True})
    ui = r.get("result", {}).get("value", json.dumps(r))
    out = {
        "ui": json.loads(ui) if isinstance(ui, str) and ui.startswith("{") else ui,
        "http403_urls": http403,
        "console": console,
    }
    print(json.dumps(out, ensure_ascii=False))

main()
