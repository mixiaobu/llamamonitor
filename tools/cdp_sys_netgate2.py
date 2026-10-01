# cdp_sys_netgate2.py — 网络门禁（fetch 拦截器，比 CDP 事件流可靠）。
# 注入 fetch/XHR 拦截器记录 /api/system/* 调用；分别在 system / overview / usage 页停留，
# 读 __lmApiCalls 统计。验证：无 history 重复 / 无 static 重复 / 隐藏页不轮询。
# Usage: cdp_sys_netgate2.py <ws> [window_seconds]
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
    def ev(expr, t=12):
        r = call("Runtime.evaluate", {"expression": expr, "returnByValue": True}, t)
        return r.get("result", {}).get("value")

    call("Page.enable"); call("Runtime.enable")
    call("Page.setWebLifecycleState", {"state": "active"}, 10)
    call("Emulation.setDeviceMetricsOverride", {"width": 1920, "height": 1080, "deviceScaleFactor": 1.0, "mobile": False}, 10)
    call("Page.reload", {"ignoreCache": True}, 20)
    for _ in range(36):
        time.sleep(0.5)
        if ev("!!(window.LM && LM.app)", 8): break
    time.sleep(1.0)

    # 注入 fetch 拦截器（记录 /api/system/* 路径）
    ev("""(function(){ if(window.__lmHooked) return 'already'; window.__lmHooked=true; window.__lmApiCalls=[];
      var of=window.fetch;
      window.fetch=function(input,opts){ var u=(typeof input==='string')?input:(input&&input.url)||'';
        try{ if(u.indexOf('/api/system/')>=0){ window.__lmApiCalls.push(u.split('?')[0]); } }catch(e){}
        return of.apply(this,arguments); };
      var oo=XMLHttpRequest.prototype.open;
      XMLHttpRequest.prototype.open=function(m,u){ try{ if(String(u).indexOf('/api/system/')>=0){window.__lmApiCalls.push(String(u).split('?')[0]);} }catch(e){}
        return oo.apply(this,arguments); };
      return 'hooked'; })()""")
    print("interceptor:", ev("window.__lmHooked ? 'ok' : 'no'"))

    from collections import Counter
    def drain():  # 读并清空
        v = ev("JSON.stringify(window.__lmApiCalls||[])")
        try:
            arr = json.loads(v) if isinstance(v, str) else []
            ev("window.__lmApiCalls.length=0;1")
            return Counter(arr)
        except Exception:
            return Counter()

    def phase(name, page, dur):
        ev("LM.nav.showPage('%s');1" % page)
        time.sleep(2.0); drain()          # 丢弃切换瞬态
        deadline = time.time() + dur
        # 持续读（保持页面活跃 + 收集）
        collected = Counter()
        while time.time() < deadline:
            time.sleep(2.0)
            c = drain()
            for k, v in c.items(): collected[k] += v
        print("=== %s (%ss) system-endpoint counts ===" % (name, int(dur)))
        if not collected: print("  (none)")
        for k, v in sorted(collected.items()): print("  %-38s %d" % (k, v))
        return collected

    a = phase("SYSTEM page", "system", win)
    b = phase("OVERVIEW page", "overview", 8)
    c = phase("USAGE page", "usage", 6)
    ws.close(); print("DONE")

if __name__ == "__main__":
    main()
