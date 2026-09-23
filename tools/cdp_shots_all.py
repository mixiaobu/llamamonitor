#!/usr/bin/env python3
"""CDP: one websocket session, set device metrics once, capture N pages.
Usage: cdp_shots_all.py <ws> <w> <h> <dpr> <out_prefix> <page1,page2,...> [theme]
Writes <out_prefix>-<page>-<theme>.png for each page.
"""
import sys, socket, struct, base64, os, json, time

def send(ws, obj):
    data = json.dumps(obj).encode("utf-8")
    header = bytearray([0x81]); n = len(data)
    if n < 126: header.append(0x80 | n)
    elif n < 65536: header.append(0x80 | 126); header += struct.pack(">H", n)
    else: header.append(0x80 | 127); header += struct.pack(">Q", n)
    mask = os.urandom(4); header += mask
    ws.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

def _recv_exact(ws, n):
    buf = b""
    while len(buf) < n:
        c = ws.recv(n - len(buf))
        if not c: raise RuntimeError("ws closed")
        buf += c
    return buf

def read_msg(ws, timeout):
    ws.settimeout(timeout)
    while True:
        b0, b1 = _recv_exact(ws, 2)
        op = b0 & 0x0F; masked = b1 & 0x80; n = b1 & 0x7F
        if n == 126: n = struct.unpack(">H", _recv_exact(ws,2))[0]
        elif n == 127: n = struct.unpack(">Q", _recv_exact(ws,8))[0]
        mask = _recv_exact(ws,4) if masked else None
        data = _recv_exact(ws,n) if n else b""
        if mask: data = bytes(b ^ mask[i%4] for i,b in enumerate(data))
        if op == 0x9:
            pm = os.urandom(4); h = bytearray([0x8A, 0x80|len(data)])
            h += pm; ws.sendall(bytes(h)+bytes(b^pm[i%4] for i,b in enumerate(data))); continue
        if op in (0x1,0x2): return data.decode("utf-8","replace")

def main():
    if len(sys.argv) < 7:
        print("usage: cdp_shots_all.py <ws> <w> <h> <dpr> <prefix> <pages> [theme]", file=sys.stderr); sys.exit(2)
    url = sys.argv[1]; w,h = int(sys.argv[2]), int(sys.argv[3]); dpr = float(sys.argv[4])
    prefix = sys.argv[5]; pages = sys.argv[6].split(","); theme = sys.argv[7] if len(sys.argv) > 7 else None
    rest = url[len("ws://"):]; host,_,path = rest.partition("/"); host,_,port = host.partition(":")
    ws = socket.create_connection((host, int(port or 80)))
    key = base64.b64encode(os.urandom(16)).decode()
    ws.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        c = ws.recv(4096)
        if not c: raise RuntimeError("handshake failed")
        resp += c
    mid = [0]
    def call(method, params=None, timeout=45):
        mid[0] += 1; i = mid[0]
        send(ws, {"id": i, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = read_msg(ws, max(0.5, deadline - time.time()))
            try: j = json.loads(raw)
            except Exception: continue
            if j.get("id") == i:
                return j.get("error") if "error" in j else j.get("result", {})
        return {"_timeout": True}

    call("Page.enable", {}, 10)
    call("Emulation.setDeviceMetricsOverride",
         {"width": w, "height": h, "deviceScaleFactor": dpr, "mobile": False}, 10)
    if theme:
        call("Runtime.evaluate", {"expression": "LM.app.applyTheme(%s);1" % json.dumps(theme), "returnByValue": True}, 10)
    time.sleep(1.0)
    for p in pages:
        call("Runtime.evaluate", {"expression": "LM.nav.showPage(%s);1" % json.dumps(p), "returnByValue": True}, 10)
        time.sleep(2.2)
        r = call("Page.captureScreenshot", {"format": "png"}, 60)
        out = "%s-%s%s.png" % (prefix, p, (("-" + theme) if theme else ""))
        if isinstance(r, dict) and "data" in r:
            with open(out, "wb") as f:
                f.write(base64.b64decode(r["data"]))
            print("saved %s (%d KB)" % (out, os.path.getsize(out) // 1024))
        else:
            print("ERROR %s: %s" % (p, json.dumps(r)[:200]))

if __name__ == "__main__":
    main()
