# -*- coding: utf-8 -*-
"""Simulate phone (LAN IP) scenario: /api/config + /api/update/status -> 403
"local-only endpoint". Reload page, then verify time-range segmented controls
still render and no unhandled 403 storm."""
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
    url, w, h = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
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
        resp += ws.recv(4096)

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
                return j.get("error") or j.get("result", {})
        return {"_timeout": True}

    call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": True})
    time.sleep(1)
    call("Page.enable", {})
    call("Runtime.enable", {})
    # Inject a fetch interceptor on every new document (survives reload),
    # so /api/config + /api/update/status return 403 "local-only endpoint"
    # exactly like a phone on the LAN IP sees them.
    patch = r"""
    (function(){
      window.__phone403 = { config: 0, update: 0 };
      var orig = window.fetch;
      window.fetch = function(u){
        var p = String(u && u.url ? u.url : u);
        if (p.indexOf("/api/config") !== -1) { window.__phone403.config++; return Promise.reject(Object.assign(new Error("HTTP 403"), {status:403, body:{detail:"local-only endpoint"}, name:"ApiError"})); }
        if (p.indexOf("/api/update/status") !== -1) { window.__phone403.update++; return Promise.reject(Object.assign(new Error("HTTP 403"), {status:403, body:{detail:"local-only endpoint"}, name:"ApiError"})); }
        return orig.apply(this, arguments);
      };
    })()
    """
    call("Page.addScriptToEvaluateOnNewDocument", {"source": patch})
    call("Page.reload", {"ignoreCache": True})
    time.sleep(8)  # app init: loadConfig runs, 403s expected
    check = open(sys.argv[4], encoding="utf-8").read()
    r = call("Runtime.evaluate", {"expression": check, "returnByValue": True})
    if "result" in r and r["result"].get("value") is not None:
        print(r["result"]["value"])
    else:
        print(json.dumps(r))

main()
