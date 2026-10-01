# cdp_sys_netgate3.py — 网络门禁（最终版）：验证可见性 + 各页 system 端点频率。
# 门禁要求：无 history 重复（live 不每 5s 全量）、无 static 重复（inventory/net-iface 低频）、
# 隐藏页不轮询（非 system/overview 页不拉 system）。
# Usage: cdp_sys_netgate3.py <ws> [window_seconds]
import sys, socket, struct, base64, os, json, time
def send(ws, obj):
    data = json.dumps(obj).encode("utf-8")
    h = bytearray([0x81]); n = len(data)
    if n < 126: h.append(0x80 | n)
    elif n < 65536: h.append(0x80 | 126); h += struct.pack(">H", n)
    else: h += b"\x80\xfe" + struct.pack(">Q", n)
    m = os.urandom(4); h += m
    ws.sendall(bytes(h) + bytes(b ^ m[i % 4] for i, b in enumerate(data)))
def _rx(ws, n):
    b = b""
    while len(b) < n:
        c = ws.recv(n - len(b)); b += c
    return b
def read_msg(ws, timeout):
    ws.settimeout(timeout)
    while True:
        b0, b1 = _rx(ws, 2)
        op = b0 & 0x0F; masked = b1 & 0x80; n = b1 & 0x7F
        if n == 126: n = struct.unpack(">H", _rx(ws, 2))[0]
        elif n == 127: n = struct.unpack(">Q", _rx(ws, 8))[0]
        mk = _rx(ws, 4) if masked else None
        d = _rx(ws, n) if n else b""
        if mk: d = bytes(x ^ mk[i % 4] for i, x in enumerate(d))
        if op == 0x9:
            pm = os.urandom(4); hh = bytearray([0x8A, 0x80 | len(d)]); hh += pm
            ws.sendall(bytes(hh) + bytes(x ^ pm[i % 4] for i, x in enumerate(d))); continue
        if op in (0x1, 0x2): return d.decode("utf-8", "replace")
def main():
    url = sys.argv[1]
    win = float(sys.argv[2]) if len(sys.argv) > 2 else 26.0
    rest = url[len("ws://"):]; host, _, path = rest.partition("/"); host, _, port = host.partition(":")
    ws = socket.create_connection((host, int(port or 80)))
    key = base64.b64encode(os.urandom(16)).decode()
    ws.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        c = ws.recv(4096)
        if not c: raise RuntimeError("hs fail")
        resp += c
    mid = [0]
    def call(method, params=None, timeout=30):
        mid[0] += 1; i = mid[0]
        send(ws, {"id": i, "method": method, "params": params or {}})
        dl = time.time() + timeout
        while time.time() < dl:
            try: raw = read_msg(ws, max(0.25, dl - time.time()))
            except Exception: return {"_t": 1}
            try: j = json.loads(raw)
            except Exception: continue
            if j.get("id") == i: return j.get("error") if "error" in j else j.get("result", {})
        return {"_t": 1}
    def ev(x, t=12):
        r = call("Runtime.evaluate", {"expression": x, "returnByValue": True, "awaitPromise": True}, t)
        return r.get("result", {}).get("value")
    from collections import Counter
    call("Runtime.enable"); call("Page.enable"); call("Page.setWebLifecycleState", {"state": "active"}, 10)
    call("Emulation.setDeviceMetricsOverride", {"width": 1920, "height": 1080, "deviceScaleFactor": 1.0, "mobile": False}, 10)
    call("Page.reload", {"ignoreCache": True}, 20)
    for _ in range(36):
        time.sleep(0.5)
        if ev("!!(window.LM && LM.app)"): break
    time.sleep(1.0)
    # 安装 hook（覆盖 window.fetch；api.js 用全局 fetch 故可拦截）
    ev("""(function(){ window.__c=[]; var of=window.fetch;
      window.fetch=function(u,o){ var s=(typeof u==='string')?u:(u&&u.url)||'';
        try{ if(s.indexOf('/api/system/')>=0){ window.__c.push(s.split('?')[0]); } }catch(e){}
        return of.apply(this,arguments); };
      return 'ok'; })()""")
    # 强制可见性 = visible（后台 CDP tab 默认 hidden，会跳过 visibleOnly 轮询；
    # 门禁要测的是"可见时的轮询节奏"，故覆盖 visibilityState/hidden）
    ev("""(function(){
      try{ Object.defineProperty(document,'visibilityState',{get:function(){return 'visible';},configurable:true});
           Object.defineProperty(document,'hidden',{get:function(){return false;},configurable:true}); }catch(e){}
      return 'forced-visible'; })()""")
    print("forced visible; now:", ev("document.visibilityState"), "page:", ev("LM.nav.currentPage()"))
    def phase(name, page, dur):
        ev("LM.nav.showPage('%s');1" % page)
        time.sleep(2.0)
        ev("window.__c.length=0;1")
        total = Counter()
        deadline = time.time() + dur
        while time.time() < deadline:
            time.sleep(2.5)
            v = ev("JSON.stringify(window.__c||[])")
            try: arr = json.loads(v)
            except Exception: arr = []
            ev("window.__c.length=0;1")
            for x in arr: total[x] += 1
        print("=== %s (%.0fs) system-endpoint counts ===" % (name, dur))
        if not total: print("  (none)")
        for k, v in sorted(total.items()): print("  %-34s %d" % (k, v))
        print("  page now:", ev("LM.nav.currentPage()"), "hidden:", ev("document.hidden"))
    phase("SYSTEM page", "system", win)
    phase("OVERVIEW page", "overview", 9)
    phase("USAGE page (hidden-ish)", "usage", 7)
    ws.close(); print("DONE")
if __name__ == "__main__":
    main()
