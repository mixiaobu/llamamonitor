#!/usr/bin/env python3
"""CDP: Emulation.setDeviceMetricsOverride then evaluate an expression.
Usage: cdp_resize_eval.py <ws_url> <width> <height> <dpr> <js|@file>
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

def read_msg(ws, deadline):
    while time.time() < deadline:
        b0, b1 = _recv_exact(ws, 2)
        op = b0 & 0x0F; masked = b1 & 0x80; n = b1 & 0x7F
        if n == 126: n = struct.unpack(">H", _recv_exact(ws,2))[0]
        elif n == 127: n = struct.unpack(">Q", _recv_exact(ws,8))[0]
        mask = _recv_exact(ws,4) if masked else None
        data = _recv_exact(ws,n) if n else b""
        if mask: data = bytes(b ^ mask[i%4] for i,b in enumerate(data))
        if op in (0x1,0x2): return data.decode("utf-8","replace")

def main():
    if len(sys.argv) < 6:
        print("usage: cdp_resize_eval.py <ws> <w> <h> <dpr> <js|@file>", file=sys.stderr); sys.exit(2)
    url, w, h, dpr, js_arg = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]), sys.argv[5]
    js = open(js_arg[1:], encoding="utf-8").read() if js_arg.startswith("@") else js_arg
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
    send(ws, {"id": 1, "method": "Emulation.setDeviceMetricsOverride",
              "params": {"width": w, "height": h, "deviceScaleFactor": dpr, "mobile": False}})
    deadline = time.time() + 10
    while time.time() < deadline:
        raw = read_msg(ws, deadline)
        try: j = json.loads(raw)
        except Exception: continue
        if j.get("id") == 1: break
    time.sleep(1.2)  # settle layout
    send(ws, {"id": 2, "method": "Runtime.evaluate", "params": {"expression": js, "returnByValue": True}})
    deadline = time.time() + 20
    while time.time() < deadline:
        raw = read_msg(ws, deadline)
        try: j = json.loads(raw)
        except Exception: continue
        if j.get("id") == 2:
            r = j.get("result", {})
            if "exceptionDetails" in r:
                d = r["exceptionDetails"]
                print("JS EXCEPTION: " + str((d.get("exception") or {}).get("description") or d.get("text"))[:1500]); sys.exit(3)
            v = (r.get("result") or {}).get("value")
            print(json.dumps(v, ensure_ascii=False) if isinstance(v,(dict,list)) else str(v)); return
    print("TIMEOUT", file=sys.stderr); sys.exit(4)

if __name__ == "__main__":
    main()
