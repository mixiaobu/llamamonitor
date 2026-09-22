#!/usr/bin/env python3
"""CDP console capture: enable Runtime+Log, reload the page, collect events.
Usage: cdp_console.py <ws_url> [seconds] [expr_after]
- prints all console messages (error/warning/log) and page exceptions
- optionally evaluates a JS expression after the wait and prints it
"""
import sys, socket, struct, base64, os, json, time

def send(ws, obj) -> None:
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
        if opcode == 0x8:  # close
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
        # ping -> pong; continue
        if opcode == 0x9:
            pm = os.urandom(4)
            h = bytearray([0x8A, 0x80 | len(data)])
            h += pm
            ws.sendall(bytes(h) + bytes(b ^ pm[i % 4] for i, b in enumerate(data)))

def main():
    if len(sys.argv) < 2:
        print("usage: cdp_console.py <ws_url> [seconds] [expr]", file=sys.stderr)
        sys.exit(2)
    url = sys.argv[1]
    wait_s = float(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].replace(".", "").isdigit() else 8.0
    expr = sys.argv[3] if len(sys.argv) > 3 else None
    if expr and expr.startswith("@"):
        with open(expr[1:], "r", encoding="utf-8") as f:
            expr = f.read()
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

    msg_id = [0]
    def cmd(method, params=None):
        msg_id[0] += 1
        send(ws, {"id": msg_id[0], "method": method, "params": params or {}})
        return msg_id[0]

    cmd("Runtime.enable")
    cmd("Log.enable")
    cmd("Page.enable")
    time.sleep(0.2)
    cmd("Page.reload", {"ignoreCache": True})

    out = []
    deadline = time.time() + wait_s
    while time.time() < deadline:
        ws.settimeout(max(1.0, deadline - time.time()))
        try:
            raw = read_msg(ws)
        except (socket.timeout, TimeoutError):
            break
        except RuntimeError:
            break
        try:
            j = json.loads(raw)
        except Exception:
            continue
        m = j.get("method")
        if m == "Runtime.consoleAPICalled":
            p = j.get("params", {})
            txts = [a.get("value") or a.get("description") or "" for a in p.get("args", [])]
            out.append("console[%s]: %s" % (p.get("type", "?"), " ".join(str(t) for t in txts)[:400]))
        elif m == "Runtime.exceptionThrown":
            d = j.get("params", {}).get("exceptionDetails", {})
            txt = (d.get("exception") or {}).get("description") or d.get("text", "exception")
            out.append("EXCEPTION: " + str(txt)[:400])
        elif m == "Log.entryAdded":
            e = j.get("params", {}).get("entry", {})
            out.append("log[%s]: %s %s" % (e.get("level"), e.get("source"), (e.get("text") or "")[:300]))
    # final eval if requested
    if expr:
        eval_id = cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        d2 = time.time() + 10
        while time.time() < d2:
            ws.settimeout(2.0)
            try:
                raw = read_msg(ws)
            except (socket.timeout, TimeoutError, RuntimeError):
                break
            try:
                j = json.loads(raw)
            except Exception:
                continue
            if j.get("id") == eval_id:
                r = j.get("result", {})
                if "exceptionDetails" in r:
                    out.append("EVAL EXCEPTION: " + str((r["exceptionDetails"].get("exception") or {}).get("description"))[:400])
                else:
                    v = (r.get("result") or {}).get("value")
                    out.append("EVAL: " + (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)))
                break
    for line in out:
        print(line)
    if not out:
        print("(no console output captured)")

if __name__ == "__main__":
    main()
