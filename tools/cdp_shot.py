#!/usr/bin/env python3
"""CDP screenshot with exact device metrics.
Usage: cdp_shot.py <ws_url> <width> <height> <dpr> <out_png> [page_name] [theme]
- Emulation.setDeviceMetricsOverride for exact rendering size + DPR
- navigates to the given page via LM.nav.showPage (if page_name)
- sets theme via LM.app.applyTheme (if theme)
- waits for settle, then Page.captureScreenshot -> out_png (PNG)
"""
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
    if len(sys.argv) < 6:
        print("usage: cdp_shot.py <ws> <w> <h> <dpr> <out> [page] [theme]", file=sys.stderr)
        sys.exit(2)
    url, w, h, dpr, out = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]), sys.argv[5]
    page = sys.argv[6] if len(sys.argv) > 6 else None
    theme = sys.argv[7] if len(sys.argv) > 7 else None
    assert url.startswith("ws://")
    rest = url[len("ws://"):]
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
        chunk = ws.recv(4096)
        if not chunk:
            raise RuntimeError("ws handshake failed")
        resp += chunk
    if b"101" not in resp.split(b"\r\n")[0]:
        raise RuntimeError("bad handshake")

    mid = [0]
    def call(method, params=None):
        mid[0] += 1
        send(ws, {"id": mid[0], "method": method, "params": params or {}})
        deadline = time.time() + 30
        while time.time() < deadline:
            ws.settimeout(2.0)
            raw = read_msg(ws)
            try:
                j = json.loads(raw)
            except Exception:
                continue
            if j.get("id") == mid[0]:
                if "error" in j:
                    return j["error"]
                return j.get("result", {})
        return {"_timeout": True}

    call("Emulation.setDeviceMetricsOverride", {
        "width": w, "height": h, "deviceScaleFactor": dpr, "mobile": False,
    })
    # drive page + theme
    js_parts = []
    if theme:
        js_parts.append("LM.app.applyTheme(%s)" % json.dumps(theme))
    if page:
        js_parts.append("LM.nav.showPage(%s)" % json.dumps(page))
    if js_parts:
        call("Runtime.evaluate", {"expression": ";".join(js_parts) + ";1", "returnByValue": True})
    time.sleep(2.5)  # settle (charts draw async)
    r = call("Page.captureScreenshot", {"format": "png"})
    if isinstance(r, dict) and "data" in r:
        with open(out, "wb") as f:
            f.write(base64.b64decode(r["data"]))
        print("saved %s (%d bytes)" % (out, os.path.getsize(out)))
    else:
        print("shot error: %s" % json.dumps(r))

if __name__ == "__main__":
    main()
