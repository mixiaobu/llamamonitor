#!/usr/bin/env python3
"""Minimal CDP Runtime.evaluate client (client-side masked WS frames).
Usage: cdp_eval.py <ws_url> '<js expression>' [--await]
Returns the JS value (returnByValue) as text. Complex objects: JSON.stringify in JS.
"""
import sys, socket, struct, base64, os, json, time

def send(ws, text: str) -> None:
    data = text.encode("utf-8")
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

def _recv_exact(ws, n: int) -> bytes:
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
        masked = b1 & 0x80
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", _recv_exact(ws, 2))[0]
        elif n == 127:
            n = struct.unpack(">Q", _recv_exact(ws, 2))[0]
        mask = _recv_exact(ws, 4) if masked else None
        data = _recv_exact(ws, n) if n else b""
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if opcode in (0x1, 0x2):  # text/binary data frames
            return data.decode("utf-8", "replace")
        # ignore ping/pong/close fragments (rare in this flow)

def main():
    if len(sys.argv) < 3:
        print("usage: cdp_eval.py <ws_url> <js> [--await]", file=sys.stderr)
        sys.exit(2)
    url, js_arg = sys.argv[1], sys.argv[2]
    await_promise = "--await" in sys.argv[3:]
    if js_arg.startswith("@"):
        with open(js_arg[1:], "r", encoding="utf-8") as f:
            js = f.read()
    else:
        js = js_arg
    assert url.startswith("ws://"), url
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
        raise RuntimeError("bad handshake: " + resp[:120].decode("utf8", "replace"))
    params = {"expression": js, "returnByValue": True, "awaitPromise": await_promise}
    send(ws, json.dumps({"id": 1, "method": "Runtime.evaluate", "params": params}))
    deadline = time.time() + 30
    while time.time() < deadline:
        raw = read_msg(ws)
        try:
            j = json.loads(raw)
        except Exception:
            continue
        if j.get("id") != 1:
            continue
        r = j.get("result", {})
        if "exceptionDetails" in r:
            det = r["exceptionDetails"]
            txt = (det.get("exception") or {}).get("description") or det.get("text", "exception")
            print("JS EXCEPTION: " + str(txt)[:2000])
            sys.exit(3)
        res = r.get("result", {})
        v = res.get("value", res)
        print(json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v))
        return
    print("CDP TIMEOUT", file=sys.stderr)
    sys.exit(4)

if __name__ == "__main__":
    main()
