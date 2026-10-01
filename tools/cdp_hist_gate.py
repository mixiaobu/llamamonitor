# Round-5 History 页门禁：console（uncaught/unhandled/API 错误）+ 布局
# （无 app 级横滚 / 无嵌套纵向滚动）+ 各尺寸截图 + 元素验证。
# 用法: python cdp_hist_gate.py <ws_url> [outdir]
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
console = []
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
        m = j.get("method"); p = j.get("params", {})
        if m == "Runtime.consoleAPICalled":
            args = [a.get("value", a.get("description")) for a in p.get("args", [])]
            console.append(p.get("type") + " " + (" ".join(str(x) for x in args))[:200])
        elif m == "Runtime.exceptionThrown":
            det = p.get("exceptionDetails", {})
            console.append("EXC " + str((det.get("exception") or {}).get("description", det.get("text")))[:200])
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
time.sleep(5)
js("Object.defineProperty(document,'visibilityState',{value:'visible',configurable:true});"
   "if(document.hidden){Object.defineProperty(document,'hidden',{value:false,configurable:true});}"
   "document.dispatchEvent(new Event('visibilitychange'));1")
time.sleep(1)

# ---- 布局验证 helper（在指定尺寸下跑）----
def layout_probe():
    # 进 history 页
    js("LM.nav.showPage('history');1")
    # 等首屏数据落地（box 休眠时 events API 可能 ~18s；轮询 soft 刷新不闪烁）
    for _ in range(14):
        time.sleep(2)
        ev = js("document.querySelectorAll('#eventsTbody tr.ev-row').length")
        gp = js("document.querySelectorAll('#gapsTbody tr.gap-row').length")
        if ev and gp: break
    js("window.scrollTo(0,0);1")
    time.sleep(1)
    # 1) app 级横滚：documentElement scrollWidth > clientWidth + 1
    hscroll = js("({doc: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1, "
                 "dw: document.documentElement.clientWidth, ds: document.documentElement.scrollWidth, "
                 "body: document.body.scrollWidth})")
    # 2) 嵌套纵向滚动：找 .table-wrap / .section-body / .card 里 overflow-y auto/scroll 且 scrollHeight>clientHeight+4
    nested = js("""(function(){var bad=[];
      document.querySelectorAll('.table-wrap,.section-body,.card,.integrity-grid,.gap-filterbar,.ev-filterbar').forEach(function(el){
        var s=getComputedStyle(el);
        if((s.overflowY==='auto'||s.overflowY==='scroll') && el.scrollHeight>el.clientHeight+4){
          bad.push(el.className.slice(0,60)+' sh='+el.scrollHeight+' ch='+el.clientHeight);
        }
      });return bad;})()""")
    # 3) 关键元素存在
    els = js("""(function(){var g={};
      ['historyRange','integrityGrid','chartHistoryTrend','gapsTbody','eventsTbody','evTimeline',
       'btnExportGapsCsv','btnExportEventsCsv','historySchemaBanner'].forEach(function(id){
        var el=document.getElementById(id); g[id]=el?(el.hidden?'hidden':'ok'):'MISSING';});return g;})()""")
    # 4) 渲染内容计数
    cnt = js("""(function(){return {
      gaps: document.querySelectorAll('#gapsTbody tr.gap-row').length,
      events: document.querySelectorAll('#eventsTbody tr.ev-row').length,
      tl: document.querySelectorAll('#evTimeline .ev-tl-item').length,
      cov: (document.getElementById('iqCoverage')||{}).textContent,
      db: (document.getElementById('iqDb')||{}).textContent};})()""")
    return hscroll, nested, els, cnt

results = {}
for w, h, mobile, name in [(1920, 1080, False, "1920"), (988, 800, False, "988"), (390, 844, True, "390"), (320, 568, True, "320")]:
    call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": mobile}, 15)
    time.sleep(1)
    hs, nested, els, cnt = layout_probe()
    shot("hist_%s_top.png" % name, full=False)
    # full page（scroll 到底再截图）
    js("window.scrollTo(0, document.body.scrollHeight);1")
    time.sleep(0.6)
    shot("hist_%s_full.png" % name, full=True)
    results[name] = {"hscroll": hs, "nested_scroll": nested, "els": els, "cnt": cnt}
    print("[%s] hscroll=%s nested=%s" % (name, hs, nested))
    print("        cnt=%s" % json.dumps(cnt, ensure_ascii=False))

call("Emulation.clearDeviceMetricsOverride", {}, 10)
print("\nCONSOLE lines: %d" % len(console))
# 只报 error / uncaught / exception（warn 也列出但单独计数）
errs = [c for c in console if c.startswith(("error", "EXC"))]
warns = [c for c in console if c.startswith("warning")]
print("errors/uncaught: %d | warnings: %d" % (len(errs), len(warns)))
for c in errs: print("   ERR  " + c)
for c in warns[:10]: print("   WARN " + c)

with open(os.path.join(OUT, "hist_gate.json"), "w", encoding="utf-8") as f:
    json.dump({"console": console, "results": results}, f, ensure_ascii=False, indent=2)
print("\nSaved hist_gate.json to " + OUT)
ws.close()
