#!/usr/bin/env python3
"""CDP client: disable cache -> reload -> wait for load -> eval an expression.
Usage: cdp_reload_eval.py <ws_url> <js or @file> [--await]
Sends Network.enable, Network.setCacheDisabled, Page.reload(ignoreCache),
waits for Page.loadEventFired (+ small settle), then Runtime.evaluate.
"""
import sys, socket, struct, base64, os, json, time

def send(ws, obj):
    data = json.dumps(obj).encode("utf-8")
    header = bytearray([0x81])
    n = len(data)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126); header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127); header += struct.pack(">Q", n)
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

def read_msg(ws, deadline):
    while time.time() < deadline:
        b0, b1 = _recv_exact(ws, 2)
        opcode = b0 & 0x0F
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

def main():
    if len(sys.argv) < 3:
        print("usage: cdp_reload_eval.py <ws_url> <js|@file> [--await]", file=sys.stderr); sys.exit(2)
    url, js_arg = sys.argv[1], sys.argv[2]
    await_promise = "--await" in sys.argv[3:]
    js = open(js_arg[1:], encoding="utf-8").read() if js_arg.startswith("@") else js_arg
    assert url.startswith("ws://"), url
    rest = url[len("ws://"):]
    host, _, path = rest.partition("/")
    host, _, port = host.partition(":")
    ws = socket.create_connection((host, int(port or 80)))
    key = base64.b64encode(os.urandom(16)).decode()
    ws.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = ws.recv(4096)
        if not chunk: raise RuntimeError("ws handshake failed")
        resp += chunk
    if b"101" not in resp.split(b"\r\n")[0]:
        raise RuntimeError("bad handshake")

    # 1. enable + disable cache + reload
    send(ws, {"id": 1, "method": "Page.enable"})
    send(ws, {"id": 2, "method": "Network.enable"})
    send(ws, {"id": 3, "method": "Network.setCacheDisabled", "params": {"cacheDisabled": True}})
    send(ws, {"id": 4, "method": "Page.reload", "params": {"ignoreCache": True}})

    # 2. wait for loadEventFired (up to 25s)
    deadline = time.time() + 25
    loaded = False
    while time.time() < deadline:
        try:
            raw = read_msg(ws, deadline)
        except Exception:
            break
        try:
            j = json.loads(raw)
        except Exception:
            continue
        if j.get("method") == "Page.loadEventFired":
            loaded = True
            break
    if not loaded:
        print("WARN: loadEventFired not observed within deadline; proceeding", file=sys.stderr)
    time.sleep(2.5)  # settle for app init / first paint

    # 3. evaluate
    send(ws, {"id": 100, "method": "Runtime.evaluate",
              "params": {"expression": js, "returnByValue": True, "awaitPromise": await_promise}})
    deadline = time.time() + 30
    while time.time() < deadline:
        raw = read_msg(ws, deadline)
        try:
            j = json.loads(raw)
        except Exception:
            continue
        if j.get("id") != 100:
            continue
        r = j.get("result", {})
        if "exceptionDetails" in r:
            det = r["exceptionDetails"]
            txt = (det.get("exception") or {}).get("description") or det.get("text", "exception")
            print("JS EXCEPTION: " + str(txt)[:2000]); sys.exit(3)
        res = r.get("result", {})
        v = res.get("value", res)
        print(json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v))
        return
    print("CDP TIMEOUT", file=sys.stderr); sys.exit(4)

if __name__ == "__main__":
    main()
