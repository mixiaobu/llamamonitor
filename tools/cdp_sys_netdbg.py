# cdp_sys_netdbg.py — debug: does the fetch interceptor capture a known api.get?
import sys, socket, struct, base64, os, json, time
def send(ws, obj):
    data = json.dumps(obj).encode("utf-8")
    h = bytearray([0x81]); n = len(data)
    if n < 126: h.append(0x80 | n)
    elif n < 65536: h.append(0x80 | 126); h += struct.pack(">H", n)
    else: h.append(0x80 | 127); h += struct.pack(">Q", n)
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
            try: raw = read_msg(ws, max(0.3, dl - time.time()))
            except Exception: return {"_t": 1}
            try: j = json.loads(raw)
            except Exception: continue
            if j.get("id") == i: return j.get("error") if "error" in j else j.get("result", {})
        return {"_t": 1}
    def ev(x, t=12):
        r = call("Runtime.evaluate", {"expression": x, "returnByValue": True, "awaitPromise": True}, t)
        return r.get("result", {}).get("value")
    call("Runtime.enable"); call("Page.enable"); call("Page.setWebLifecycleState", {"state": "active"}, 10)
    call("Emulation.setDeviceMetricsOverride", {"width": 1920, "height": 1080, "deviceScaleFactor": 1.0, "mobile": False}, 10)
    call("Page.reload", {"ignoreCache": True}, 20)
    for _ in range(36):
        time.sleep(0.5)
        if ev("!!(window.LM && LM.app)"): break
    time.sleep(1.0)
    # hook
    ev("""(function(){ window.__c=[]; var of=window.fetch;
      window.fetch=function(u,o){ var s=(typeof u==='string')?u:(u&&u.url)||'';
        if(s.indexOf('/api/')>=0){ try{ window.__c.push(s.split('?')[0]); }catch(e){} }
        return of.apply(this,arguments); };
      return 'hooked'; })()""")
    print("hook:", ev("typeof window.__c"))
    # manual call
    ev("LM.api.get('/api/system/status').then(function(){return 1;})")
    time.sleep(1.5)
    print("after manual get, __c =", ev("JSON.stringify(window.__c)"))
    # does LM.api exist & what is it?
    print("LM.api type:", ev("typeof LM.api"), " api.get type:", ev("typeof (LM.api&&LM.api.get)"))
    ws.close(); print("DONE")
if __name__ == "__main__":
    main()
