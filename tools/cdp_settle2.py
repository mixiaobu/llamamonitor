#!/usr/bin/env python3
"""CDP: realistic usage-entry timing. Reload (normal cache), wait for full app
init, then navigate to usage and sample the summary hero every 500ms for N sec.
Usage: cdp_settle2.py <ws_url> [sec]"""
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
    ws.settimeout(max(0.2, deadline - time.time()))
    while time.time() < deadline:
        b0, b1 = _recv_exact(ws, 2)
        op = b0 & 0x0F; masked = b1 & 0x80; n = b1 & 0x7F
        if n == 126: n = struct.unpack(">H", _recv_exact(ws, 2))[0]
        elif n == 127: n = struct.unpack(">Q", _recv_exact(ws, 8))[0]
        mask = _recv_exact(ws, 4) if masked else None
        data = _recv_exact(ws, n) if n else b""
        if mask: data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if op == 0x9:
            pm = os.urandom(4); h = bytearray([0x8A, 0x80|len(data)])
            h += pm; ws.sendall(bytes(h)+bytes(b^pm[i%4] for i,b in enumerate(data))); continue
        if op in (0x1, 0x2): return data.decode("utf-8", "replace")

def main():
    url = sys.argv[1]; nsec = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    rest = url[len("ws://"):]; host, _, path = rest.partition("/"); host, _, port = host.partition(":")
    ws = socket.create_connection((host, int(port or 80)))
    key = base64.b64encode(os.urandom(16)).decode()
    ws.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        c = ws.recv(4096)
        if not c: raise RuntimeError("handshake failed")
        resp += c
    def drain(s):
        d = time.time() + s
        while time.time() < d:
            try: read_msg(ws, d)
            except Exception: break
    def ev(js):
        i = 700 + int(time.time()*1000) % 100000
        send(ws, {"id": i, "method": "Runtime.evaluate", "params": {"expression": js, "returnByValue": True}})
        d = time.time() + 15
        while time.time() < d:
            try: raw = read_msg(ws, d)
            except Exception: break
            try: j = json.loads(raw)
            except Exception: continue
            if j.get("id") == i:
                r = j.get("result", {})
                if "exceptionDetails" in r:
                    det = r["exceptionDetails"]; return {"__exc": str((det.get("exception") or {}).get("description") or det.get("text",""))[:200]}
                return r.get("result", {}).get("value", {})
        return {"__timeout": True}

    send(ws, {"id": 1, "method": "Runtime.enable"})
    send(ws, {"id": 2, "method": "Page.enable"})
    send(ws, {"id": 5, "method": "Page.reload"})
    # wait for full app init (assets load + first poll round)
    t0 = time.time()
    for _ in range(40):
        drain(0.5)
        ready = ev("(function(){return (window.LM && LM.app) ? 'ready' : 'loading';})()")
        if ready == "ready":
            break
    print("app ready after %.1fs" % (time.time() - t0))
    # navigate IMMEDIATELY (worst case: user clicks 用量 the moment the app is up;
    # the first poll round may still be in flight)
    ev("LM.nav.showPage('usage'); 'x'")
    seq = []
    for k in range(int(nsec * 2) + 1):
        time.sleep(0.5)
        drain(0.1)
        v = ev("(function(){var g=function(id){var e=document.getElementById(id);return e?e.textContent.trim():'';};return g('sumHeroLogical')+'|'+g('sumCoverage');})()")
        seq.append("%d.%ds: %s" % (k * 0.5, int(k*5)%10, v))
    print(json.dumps(seq, ensure_ascii=False))

if __name__ == "__main__":
    main()
