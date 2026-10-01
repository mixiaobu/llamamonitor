# Round-4 GPU 页 CDP 多尺寸验收 + 截图（Edge DevTools）。
# 连接已打开的 LlamaMonitor 标签页，对 1920/988/390/320 做：
#   - 导航到 gpu 页（长等待，确保 daily 异步到达）
#   - DOM/CSS/滚动/能耗/运行状态 断言
#   - 截图（top）
# 用法：python cdp_gpu_sizes.py <ws_url> <artifacts_dir>
import sys, socket, struct, base64, os, json, time

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

def screenshot(name):
    r = call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False}, 90)
    if isinstance(r, dict) and "data" in r:
        path = os.path.join(OUT, name)
        with open(path, "wb") as f:
            f.write(base64.b64decode(r["data"]))
        print("shot %s (%d KB)" % (name, os.path.getsize(path) // 1024))
    else:
        print("shot ERR %s: %s" % (name, json.dumps(r)[:150]))

call("Page.enable", {}, 10)
call("Runtime.enable", {}, 10)

# reload 一次拉取新 JS（含 运行状态 恒显示修复）
call("Emulation.setDeviceMetricsOverride",
     {"width": 1920, "height": 1080, "deviceScaleFactor": 1, "mobile": False}, 15)
call("Page.navigate", {"url": "http://127.0.0.1:8790/"}, 30)
time.sleep(5)

def probe_gpu():
    return js("""(function(){
      var out={};
      out.gpuCards = document.querySelectorAll('#gpuCards .gpu-device').length;
      out.coreCells = document.querySelectorAll('#gpuCards .gd-core .gd-cell').length;
      out.advClosed = Array.from(document.querySelectorAll('#gpuCards .gpu-adv')).map(function(d){return !d.open;});
      out.energyRows = document.querySelectorAll('#gpuEnergy .energy-row').length;
      out.energyTexts = Array.from(document.querySelectorAll('#gpuEnergy .energy-row')).map(function(r){
        var k=r.querySelector('.k'),v=r.querySelector('.v'); return (k?k.textContent:'')+' = '+(v?v.textContent:'');
      });
      // 展开每张卡的高级信息，取每张卡的 运行状态 值
      var runStates=[];
      document.querySelectorAll('#gpuCards .gpu-device').forEach(function(card){
        var adv=card.querySelector('.gpu-adv'); if(adv) adv.open=true;
        var row=Array.from(card.querySelectorAll('.adv-row')).filter(function(r){
          return r.querySelector('.k')&&r.querySelector('.k').textContent==='运行状态';
        })[0];
        runStates.push(row?row.querySelector('.v').textContent:'(none)');
      });
      out.runStates=runStates;
      // 进程区内部滚动检查
      var p=document.querySelector('#gpuProc');
      if(p){var cs=getComputedStyle(p);
        out.procScroll={overflowY:cs.overflowY,maxHeight:cs.maxHeight,
          clientH:p.clientHeight,scrollH:p.scrollHeight,
          hOverflow:p.scrollWidth>p.clientWidth+1};}
      out.procRows=document.querySelectorAll('#gpuProc .gpu-proc-table tbody tr').length;
      // 页面横向溢出
      out.pageHOverflow=document.documentElement.scrollWidth>document.documentElement.clientWidth+1;
      out.scrollW=document.documentElement.scrollWidth; out.clientW=document.documentElement.clientWidth;
      // 更多趋势存在
      out.moreTrends=!!document.getElementById('gpuMoreTrends');
      return out;
    })()""")

def set_metrics(w, h, mobile):
    call("Emulation.setDeviceMetricsOverride",
         {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": mobile}, 15)

results = {}
# 先 1920 验证核心（运行状态/能耗/高级折叠）
js("LM.nav.showPage('gpu');1")
time.sleep(6)
results["1920"] = probe_gpu()
screenshot("gpu-1920.png")

# 其余尺寸
for (w, h, mob, tag) in [(988, 800, False, "988"), (390, 844, True, "390"), (320, 640, True, "320")]:
    set_metrics(w, h, mob)
    js("LM.nav.showPage('gpu');1")
    time.sleep(5)
    results[tag] = probe_gpu()
    screenshot("gpu-%s.png" % tag)

with open(os.path.join(OUT, "gpu_sizes.json"), "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print("SIZES-REPORT")
print(json.dumps(results, ensure_ascii=False, indent=2))
ws.close()
print("DONE")
