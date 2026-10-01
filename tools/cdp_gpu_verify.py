# Round-4 GPU 页 CDP 验收（Edge DevTools）。连接已打开的 LlamaMonitor 标签页，
# reload 拉取新 JS/CSS/后端，导航到 gpu 页，做 DOM/CSS/控制台/滚动断言 + 截图。
# 用法：python cdp_gpu_verify.py <ws_url> <artifacts_dir>
import sys, socket, struct, base64, os, json, time, urllib.request

WS = sys.argv[1]
OUT = sys.argv[2]
os.makedirs(OUT, exist_ok=True)

def _connect(url):
    rest = url[len("ws://"):]
    host, _, path = rest.partition("/")
    host, _, port = host.partition(":")
    ws = socket.create_connection((host, int(port or 80)))
    key = base64.b64encode(os.urandom(16)).decode()
    ws.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        c = ws.recv(4096)
        if not c:
            raise RuntimeError("handshake failed")
        resp += c
    if b"101" not in resp.split(b"\r\n")[0]:
        raise RuntimeError("bad handshake")
    return ws

ws = _connect(WS)
_mid = [0]

def _recv_exact(ws, n):
    buf = b""
    while len(buf) < n:
        c = ws.recv(n - len(buf))
        if not c:
            raise RuntimeError("ws closed")
        buf += c
    return buf

def read_msg(ws, timeout):
    ws.settimeout(timeout)
    while True:
        b0, b1 = _recv_exact(ws, 2)
        op = b0 & 0x0F
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
        if op == 0x9:
            pm = os.urandom(4)
            h = bytearray([0x8A, 0x80 | len(data)])
            h += pm
            ws.sendall(bytes(h) + bytes(b ^ pm[i % 4] for i, b in enumerate(data)))
            continue
        if op in (0x1, 0x2):
            return data.decode("utf8", "replace")

def call(method, params=None, timeout=45):
    _mid[0] += 1
    i = _mid[0]
    data = json.dumps({"id": i, "method": method, "params": params or {}}).encode()
    header = bytearray([0x81])
    n = len(data)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126); header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127); header += struct.pack(">Q", n)
    mask = os.urandom(4); header += mask
    ws.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = read_msg(ws, max(0.5, deadline - time.time()))
        try:
            j = json.loads(raw)
        except Exception:
            continue
        if j.get("id") == i:
            return j.get("error") if "error" in j else j.get("result", {})
    return {"_timeout": True}

def js(expr, await_p=False):
    r = call("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                  "awaitPromise": await_p}, 60)
    if "exceptionDetails" in r:
        det = r["exceptionDetails"]
        txt = (det.get("exception") or {}).get("description") or det.get("text", "exception")
        return {"__exc": str(txt)[:1500]}
    return r.get("result", {}).get("value", r.get("result"))

def screenshot(name, full_page=False):
    r = call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": full_page}, 90)
    if isinstance(r, dict) and "data" in r:
        path = os.path.join(OUT, name)
        with open(path, "wb") as f:
            f.write(base64.b64decode(r["data"]))
        print("shot %s (%d KB)" % (name, os.path.getsize(path) // 1024))
    else:
        print("shot ERR %s: %s" % (name, json.dumps(r)[:150]))

# ---- enable + capture console ----
console = []
call("Page.enable", {}, 10)
call("Runtime.enable", {}, 10)
call("Log.enable", {}, 10)

# 桌面 1920 视口
call("Emulation.setDeviceMetricsOverride",
     {"width": 1920, "height": 1080, "deviceScaleFactor": 1, "mobile": False}, 15)
# reload to pick up new static + backend
call("Page.navigate", {"url": "http://127.0.0.1:8790/"}, 30)
time.sleep(5)
# 强制可见（后台 CDP 标签 visibilityState 常为 hidden）
js("Object.defineProperty(document,'visibilityState',{value:'visible',configurable:true});"
   "window.__appVisible=true;1")
# 导航到 GPU 页
js("LM.nav.showPage('gpu');1")
time.sleep(4)

report = {}
report["pageVisible"] = js("document.visibilityState")
report["gpuCards"] = js("document.querySelectorAll('#gpuCards .gpu-device').length")
report["coreCells"] = js("document.querySelectorAll('#gpuCards .gd-core .gd-cell').length")
report["advDetails"] = js("document.querySelectorAll('#gpuCards .gpu-adv').length")
report["advOpenDesktop"] = js("Array.from(document.querySelectorAll('#gpuCards .gpu-adv')).map(function(d){return d.open;})")
report["pageState"] = js("(document.querySelector('#gpuPageState .status-text')||{}).textContent")
report["gpuCount"] = js("(document.querySelector('#gpuGpuCount')||{}).textContent")
report["driverVer"] = js("(document.querySelector('#gpuDriverVer')||{}).textContent")
report["energyRows"] = js("document.querySelectorAll('#gpuEnergy .energy-row').length")
report["energyTexts"] = js("Array.from(document.querySelectorAll('#gpuEnergy .energy-row .v')).map(function(e){return e.textContent;})")
report["procTable"] = js("!!document.querySelector('#gpuProc .gpu-proc-table')")
report["procRows"] = js("document.querySelectorAll('#gpuProc .gpu-proc-table tbody tr').length")
report["procShowAll"] = js("(document.querySelector('#gpuProc .gpu-proc-more')||{}).textContent")
report["moreTrendsExists"] = js("!!document.getElementById('gpuMoreTrends')")
report["chartUtil"] = js("!!LM.charts && !!LM.charts.renderGpuUtilChart")

# ---- 进程区无内部 vertical scroll（§299-302 硬性 Gate）----
def scroll_probe(sel):
    expr = """(function(){
      var el=document.querySelector(%s); if(!el) return {found:false};
      var cs=getComputedStyle(el);
      var out={found:true,sel:%s,
        overflowY:cs.overflowY, overflowX:cs.overflowX, maxHeight:cs.maxHeight,
        clientH:el.clientHeight, scrollH:el.scrollHeight, scrollW:el.scrollWidth, clientW:el.clientWidth};
      return out;
    })()""" % (json.dumps(sel), json.dumps(sel))
    return js(expr)

report["procScroll"] = scroll_probe("#gpuProc")
report["procTableScroll"] = scroll_probe("#gpuProc .gpu-proc-table")

# 整页是否有横向溢出（body/html scrollWidth > clientWidth）
report["pageHOverflow"] = js("({scrollW:document.documentElement.scrollWidth, clientW:document.documentElement.clientWidth, overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1})")

# 高级信息展开一张卡（点 summary）看内容
js("(function(){var a=document.querySelector('#gpuCards .gpu-adv'); if(a){a.open=true;}})();1")
time.sleep(0.5)
report["advAfterOpen"] = js("Array.from(document.querySelectorAll('#gpuCards .gpu-adv')).map(function(d){return d.open;})")
report["advRows"] = js("Array.from(document.querySelectorAll('#gpuCards .adv-row .k')).map(function(e){return e.textContent;})")

# 展开更多趋势 + 触发 fan/clock 图
js("(function(){var d=document.getElementById('gpuMoreTrends'); if(d){d.open=true;}})();1")
time.sleep(2)

with open(os.path.join(OUT, "gpu_verify.json"), "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=2)
print("REPORT")
print(json.dumps(report, ensure_ascii=False, indent=2))

# 截图：GPU 页（top + full）
screenshot("gpu-1920-top.png")
screenshot("gpu-1920-full.png", full_page=True)
ws.close()
print("DONE")
