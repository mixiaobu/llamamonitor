# Round-4 GPU 页专项截图（Edge DevTools）。
# 1920 桌面：能耗复核 / 高级卡 / ECC / 更多趋势(风扇+时钟) / 进程表 / 24h 利用率图。
# 390 手机：进程卡。
# 用法：python cdp_gpu_shots.py <ws_url> <artifacts_dir>
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

def scroll_to(sel):
    js("(function(){var e=document.querySelector(%s); if(e) e.scrollIntoView({block:'center'});})();" % json.dumps(sel))

call("Page.enable", {}, 10)
call("Runtime.enable", {}, 10)
call("Emulation.setDeviceMetricsOverride",
     {"width": 1920, "height": 1080, "deviceScaleFactor": 1, "mobile": False}, 15)
call("Page.navigate", {"url": "http://127.0.0.1:8790/"}, 30)
time.sleep(5)
js("LM.nav.showPage('gpu');1")
time.sleep(8)  # 等 daily 聚合完成（2s 采样 48h，较慢）

# 能耗复核（长等待后）
energy = js("Array.from(document.querySelectorAll('#gpuEnergy .energy-row')).map(function(r){var k=r.querySelector('.k'),v=r.querySelector('.v');return (k?k.textContent:'')+' = '+(v?v.textContent:'');})")
print("ENERGY:", json.dumps(energy, ensure_ascii=False))
js("window.__gpuEnergyCheck=%s;" % json.dumps(energy))

# 1) 高级卡（两卡都展开）+ 更多趋势 + ECC
js("document.querySelectorAll('#gpuCards .gpu-adv').forEach(function(d){d.open=true;});1")
time.sleep(1)
scroll_to("#gpuCards")
time.sleep(0.5)
screenshot("gpu-adv-expanded.png")          # BB Advanced Card
scroll_to("#gpuCards .gpu-device:nth-child(2) .gpu-ecc")
time.sleep(0.4)
screenshot("gpu-ecc.png")                    # BC ECC（V100 有 ECC）
js("(function(){var d=document.getElementById('gpuMoreTrends'); if(d){d.open=true; d.scrollIntoView({block:'center'});}})();1")
time.sleep(3)   # 等风扇/时钟图渲染
screenshot("gpu-fan-trend.png")              # BD Fan Trend（更多趋势区）

# 2) 进程表（desktop，展开高级不影响进程区）
js("document.querySelectorAll('#gpuCards .gpu-adv').forEach(function(d){d.open=false;});1")
scroll_to("#gpuProc")
time.sleep(0.6)
screenshot("gpu-process-desktop.png")        # BE Process Desktop

# 3) 24h 利用率图（切到 24h 范围，等降采样渲染）—— segmented 按钮按文本匹配
js("(function(){var bs=document.querySelectorAll('#gpuRange button');for(var i=0;i<bs.length;i++){if(bs[i].textContent==='24 小时'){bs[i].click();return;}}})()")
time.sleep(5)
scroll_to("#chartGpuUtil")
time.sleep(1)
screenshot("gpu-24h-util.png")               # BG 24h 利用率图

# 4) 390 手机进程卡
call("Emulation.setDeviceMetricsOverride",
     {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True}, 15)
time.sleep(1)
js("LM.nav.showPage('gpu');1")
time.sleep(5)
scroll_to("#gpuProc")
time.sleep(0.6)
screenshot("gpu-process-mobile.png")         # BF Process Mobile

ws.close()
print("DONE")
