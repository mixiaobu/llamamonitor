# cdp_sys_netgate.py — 网络门禁：统计 system 页停留期间各 system 端点请求次数，
# 再切到 overview 页统计，验证：无 history 重复 / 无 static 重复 / 隐藏页不轮询。
# Usage: cdp_sys_netgate.py <ws> [window_seconds]
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
        c = ws.recv(n - len(buf)); buf += c
    return buf

def read_msg(ws, timeout):
    ws.settimeout(timeout)
    while True:
        b0, b1 = _recv_exact(ws, 2)
        op = b0 & 0x0F; masked = b1 & 0x80; n = b1 & 0x7F
        if n == 126: n = struct.unpack(">H", _recv_exact(ws, 2))[0]
        elif n == 127: n = struct.unpack(">Q", _recv_exact(ws, 8))[0]
        mask = _recv_exact(ws, 4) if masked else None
        data = _recv_exact(ws, n) if n else b""
        if mask: data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if op == 0x9:
            pm = os.urandom(4); h = bytearray([0x8A, 0x80 | len(data)])
            h += pm; ws.sendall(bytes(h) + bytes(b ^ pm[i % 4] for i, b in enumerate(data))); continue
        if op in (0x1, 0x2): return data.decode("utf-8", "replace")

def main():
    url = sys.argv[1]
    win = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0
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
    mid = [0]
    def call(method, params=None, timeout=60):
        mid[0] += 1; i = mid[0]
        send(ws, {"id": i, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            try: raw = read_msg(ws, max(0.3, deadline - time.time()))
            except Exception: return {"_timeout": True}
            try: j = json.loads(raw)
            except Exception: continue
            if j.get("id") == i: return j.get("error") if "error" in j else j.get("result", {})
        return {"_timeout": True}
    def ev(expr, t=10):
        r = call("Runtime.evaluate", {"expression": expr, "returnByValue": True}, t)
        return r.get("result", {}).get("value")

    call("Page.enable"); call("Runtime.enable"); call("Network.enable")
    call("Page.setWebLifecycleState", {"state": "active"}, 10)
    call("Emulation.setDeviceMetricsOverride", {"width": 1920, "height": 1080, "deviceScaleFactor": 1.0, "mobile": False}, 10)
    call("Page.reload", {"ignoreCache": True}, 20)
    for _ in range(36):
        time.sleep(0.5)
        if ev("!!(window.LM && LM.app)", 8): break
    time.sleep(1.0)

    reqs = []
    def listen(dur):
        deadline = time.time() + dur
        while time.time() < deadline:
            try:
                raw = read_msg(ws, max(0.15, deadline - time.time()))
            except Exception:
                continue  # 读超时不是错误；继续到 deadline（否则首个空窗就退出）
            try: j = json.loads(raw)
            except Exception: continue
            if j.get("method") == "Network.requestWillBeSent":
                u = (j.get("params", {}).get("request", {}) or {}).get("url", "")
                if "/api/" in u: reqs.append(u.split("127.0.0.1:8790")[-1].split("?")[0])

    def norm(u):
        for k in ("/api/system/status","/api/system/live","/api/system/inventory",
                  "/api/system/network-interfaces","/api/system/sensors","/api/system/daily",
                  "/api/llama/slots","/api/llama/info"):
            if u.startswith(k): return k
        return u

    # Phase A: system page
    ev("LM.nav.showPage('system');1")
    time.sleep(2.0); listen(2.0)
    reqs.clear()
    listen(win)
    from collections import Counter
    sysA = Counter(norm(r) for r in reqs if r.startswith("/api/system/"))
    print("=== SYSTEM page (%ds) system-endpoint counts ===" % int(win))
    for k, v in sorted(sysA.items()): print("  %-38s %d" % (k, v))
    # Phase B: overview page (sysStatus also runs here; others should not)
    ev("LM.nav.showPage('overview');1")
    time.sleep(2.0); listen(2.0)
    reqs.clear()
    listen(8.0)
    sysB = Counter(norm(r) for r in reqs if r.startswith("/api/system/"))
    print("=== OVERVIEW page (8s) system-endpoint counts ===")
    for k, v in sorted(sysB.items()): print("  %-38s %d" % (k, v))
    # Phase C: usage page (NO system endpoint should fire)
    ev("LM.nav.showPage('usage');1")
    time.sleep(2.0); listen(2.0)
    reqs.clear()
    listen(6.0)
    sysC = Counter(norm(r) for r in reqs if r.startswith("/api/system/"))
    print("=== USAGE page (6s) system-endpoint counts (expect none) ===")
    if not sysC: print("  (none)")
    for k, v in sorted(sysC.items()): print("  %-38s %d" % (k, v))
    ws.close(); print("DONE")

if __name__ == "__main__":
    main()
