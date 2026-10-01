# 验证 History 页移动端 Timeline vs 表格切换（988 断点），并截图。
import sys, socket, struct, base64, os, json, time

WS = sys.argv[1]
OUT = sys.argv[2] if len(sys.argv) > 2 else "tools/artifacts"
os.makedirs(OUT, exist_ok=True)

def _connect(url):
    rest = url[len("ws://"):]
    host, _, path = rest.partition("/")
    host, _, port = host.partition(":")
    ws = socket.create_connection((host, int(port or 80)))
    key = base64.b64encode(os.urandom(16)).decode()
    ws.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        c = ws.recv(4096)
        if not c: raise RuntimeError("handshake failed")
        resp += c
    return ws

ws = _connect(WS)
_mid = [0]
def _recv_exact(ws, n):
    buf = b""
    while len(buf) < n:
        c = ws.recv(n - len(buf))
        if not c: raise RuntimeError("closed")
        buf += c
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
            pm = os.urandom(4); h = bytearray([0x8A, 0x80 | len(data)]); h += pm
            ws.sendall(bytes(h) + bytes(b ^ pm[i % 4] for i, b in enumerate(data))); continue
        if op in (0x1, 0x2): return data.decode("utf8", "replace")
def call(method, params=None, timeout=30):
    _mid[0] += 1; i = _mid[0]
    data = json.dumps({"id": i, "method": method, "params": params or {}}).encode()
    h = bytearray([0x81]); n = len(data)
    if n < 126: h.append(0x80 | n)
    elif n < 65536: h.append(0x80 | 126); h += struct.pack(">H", n)
    else: h.append(0x80 | 127); h += struct.pack(">Q", n)
    mask = os.urandom(4); h += mask
    ws.sendall(bytes(h) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = read_msg(ws, max(0.5, deadline - time.time()))
        try: j = json.loads(raw)
        except Exception: continue
        if j.get("id") == i: return j.get("error") if "error" in j else j.get("result", {})
    return {"_timeout": True}
def js(expr, await_p=False, timeout=30):
    r = call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": await_p}, timeout)
    if "exceptionDetails" in r: return {"__exc": str(r["exceptionDetails"])[:300]}
    return r.get("result", {}).get("value", r.get("result"))
def shot(name, full=False):
    r = call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": full}, 30)
    if "data" in r:
        p = os.path.join(OUT, name)
        with open(p, "wb") as f: f.write(base64.b64decode(r["data"]))
        return p
    return None

call("Page.enable", {}, 10)
call("Runtime.enable", {}, 10)
call("Page.navigate", {"url": "http://127.0.0.1:8790/"}, 30)
time.sleep(4)
js("Object.defineProperty(document,'visibilityState',{value:'visible',configurable:true});"
   "if(document.hidden){Object.defineProperty(document,'hidden',{value:false,configurable:true});}"
   "document.dispatchEvent(new Event('visibilitychange'));1")
time.sleep(1)

for w, h, name in [(390, 844, "390"), (320, 568, "320")]:
    call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 2, "mobile": True}, 15)
    time.sleep(1)
    js("LM.nav.showPage('history');1")
    for _ in range(12):
        time.sleep(2)
        ev = js("document.querySelectorAll('#eventsTbody tr.ev-row').length")
        gp = js("document.querySelectorAll('#gapsTbody tr.gap-row').length")
        if ev and gp: break
    js("window.scrollTo(0,0);1")
    time.sleep(1)
    out = js("""(function(){
      var tb=document.getElementById('eventsTbody');var tl=document.getElementById('evTimeline');
      var w=document.getElementById('eventsTableWrap');var gt=document.getElementById('gapsTableWrap');
      var gt2=document.querySelector('table.table-gap2');
      return JSON.stringify({
        innerW: window.innerWidth,
        match987: window.matchMedia('(max-width: 987px)').matches,
        evTableRows: tb?tb.querySelectorAll('tr.ev-row').length:-1,
        evWrapDisplay: w?getComputedStyle(w).display:'?',
        tlHidden: tl?tl.hidden:'?', tlItems: tl?tl.querySelectorAll('.ev-tl-item').length:-1,
        gapTableWrapDisplay: gt?getComputedStyle(gt).display:'?',
        gapTableDisplay: gt2?getComputedStyle(gt2).display:'?'
      });})()""")
    print("[%s] %s" % (name, out))
    shot("hmmode_%s_top.png" % name)
    # 滚到事件区截图
    js("document.getElementById('eventsSection').scrollIntoView({block:'start'});1")
    time.sleep(0.6)
    shot("hmmode_%s_events.png" % name)

call("Emulation.clearDeviceMetricsOverride", {}, 10)
print("done")
ws.close()
