# cdp_sys_func.py — system page 功能截图 + 交互验证（24h 范围 / 网络接口切换）。
# Usage: cdp_sys_func.py <ws> <prefix>
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
    url = sys.argv[1]; prefix = sys.argv[2]
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
            try: raw = read_msg(ws, max(0.5, deadline - time.time()))
            except Exception: return {"_timeout": True}
            try: j = json.loads(raw)
            except Exception: continue
            if j.get("id") == i: return j.get("error") if "error" in j else j.get("result", {})
        return {"_timeout": True}
    def shot(out):
        r = call("Page.captureScreenshot", {"format": "png"}, 60)
        if isinstance(r, dict) and "data" in r:
            open(out, "wb").write(base64.b64decode(r["data"])); return os.path.getsize(out)//1024
        return -1
    def ev(expr, t=10):
        r = call("Runtime.evaluate", {"expression": expr, "returnByValue": True}, t)
        return r.get("result", {}).get("value")

    call("Page.enable"); call("Runtime.enable"); call("Page.setWebLifecycleState", {"state": "active"}, 10)
    call("Emulation.setDeviceMetricsOverride", {"width": 1920, "height": 1080, "deviceScaleFactor": 1.0, "mobile": False}, 10)
    call("Page.reload", {"ignoreCache": True}, 20)
    for _ in range(36):
        time.sleep(0.5)
        if ev("!!(window.LM && LM.app)", 8): break
    ev("LM.nav.showPage('system');1")
    time.sleep(3.0)

    # 1) 默认 1h CPU（顶部）
    ev("(document.querySelector('.content')||document.scrollingElement).scrollTo(0,0);1")
    time.sleep(0.4)
    print("func_cpu_1h:", shot(prefix + "-func-cpu-1h.png"), "KB")

    # 2) 切 24h 范围 -> 重绘 CPU 图（验证 range 切换 + 24h 降采样）
    ev("(function(){var s=document.getElementById('systemRange');if(!s)return 'no-seg';"
       "var b=Array.prototype.find.call(s.querySelectorAll('button'),function(x){return x.textContent.indexOf('24 小时')>=0;});"
       "if(b){b.click();return 'clicked-24h'}return 'no-24h-btn';})();1")
    time.sleep(4.0)
    info = ev("(function(){return JSON.stringify({range:Array.prototype.map.call("
              "document.getElementById('systemRange').querySelectorAll('button'),function(b){return (b.getAttribute('aria-pressed')==='true'?'>':'')+b.textContent;}),"
              "cpuChartSummary:document.getElementById('sysCpuChartSummary').textContent,"
              "cpuChartCanvas:!!document.querySelector('#chartSysCpu canvas'),"
              "memChartCanvas:!!document.querySelector('#chartSysMem canvas'),"
              "powerChartCanvas:!!document.querySelector('#chartSysPower canvas')});})()")
    print("24h info:", info)
    ev("(document.querySelector('.content')||document.scrollingElement).scrollTo(0,0);1")
    time.sleep(0.4)
    print("func_cpu_24h:", shot(prefix + "-func-cpu-24h.png"), "KB")

    # 3) 网络接口选择器：切到某个具体接口，验证 rx/tx 跟随
    sel = ev("(function(){var s=document.getElementById('sysNetIface');if(!s)return 'no-sel';"
             "var opt=Array.prototype.find.call(s.options,function(o){return o.value!=='auto'&&o.value!=='WLAN';});"
             "var picked=opt?opt.value:'none';"
             "s.value=picked;s.dispatchEvent(new Event('change'));return picked;})()")
    time.sleep(1.0)
    net = ev("(function(){return JSON.stringify({picked:'" + str(sel) + "',"
             "ifaceName:document.getElementById('sysNetIfaceName').textContent,"
             "ifaceKind:document.getElementById('sysNetIfaceKind').textContent,"
             "netR:document.getElementById('sysNetRx').textContent,"
             "netT:document.getElementById('sysNetTx').textContent});})()")
    print("net switch:", net)
    ev("(function(){var e=document.getElementById('sysNetIface');e.scrollIntoView({block:'center'});return 1;})()")
    time.sleep(0.4)
    print("func_net_selector:", shot(prefix + "-func-net-selector.png"), "KB")

    # 4) 硬件信息区
    ev("(function(){var e=document.getElementById('hwGrid');if(e)e.scrollIntoView({block:'start'});return 1;})()")
    time.sleep(0.4)
    print("func_hwinfo:", shot(prefix + "-func-hwinfo.png"), "KB")

    # 5) 内存趋势图
    ev("(function(){var e=document.getElementById('chartSysMemBox');if(e)e.scrollIntoView({block:'center'});return 1;})()")
    time.sleep(0.4)
    print("func_mem_trend:", shot(prefix + "-func-mem-trend.png"), "KB")
    ws.close(); print("DONE")

if __name__ == "__main__":
    main()
