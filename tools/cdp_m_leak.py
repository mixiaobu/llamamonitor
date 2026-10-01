#!/usr/bin/env python3
"""Round-8 内存/Timer 泄漏测试：fresh navigate（390 手机）-> 基线 ->
切换 100 次页面 -> 复查。指标：
- ECharts 实例数（LM.charts.instanceCount，应稳定 = 已初始化图表容器数）
- 活跃 setTimeout 数量（monkey-patch 跟踪，自调度 poller 应稳定不增长）
- performance.memory.usedJSHeapSize（MB）
用法: cdp_m_leak.py <ws> <times>
"""
import sys, socket, struct, base64, os, json, time

def send(ws, obj):
    data = json.dumps(obj).encode("utf-8"); h = bytearray([0x81]); n = len(data)
    if n < 126: h.append(0x80 | n)
    elif n < 65536: h.append(0x80 | 126); h += struct.pack(">H", n)
    else: h.append(0x80 | 127); h += struct.pack(">Q", n)
    m = os.urandom(4); h += m
    ws.sendall(bytes(h) + bytes(b ^ m[i % 4] for i, b in enumerate(data)))

def _recv(ws, n):
    buf = b""
    while len(buf) < n:
        c = ws.recv(n - len(buf))
        if not c: raise RuntimeError("ws closed")
        buf += c
    return buf

def read_msg(ws):
    while True:
        b0, b1 = _recv(ws, 2); op = b0 & 0x0F
        if op == 0x8: raise TimeoutError("ws closed")
        masked = b1 & 0x80; n = b1 & 0x7F
        if n == 126: n = struct.unpack(">H", _recv(ws, 2))[0]
        elif n == 127: n = struct.unpack(">Q", _recv(ws, 8))[0]
        mk = _recv(ws, 4) if masked else None
        data = _recv(ws, n) if n else b""
        if mk: data = bytes(b ^ mk[i % 4] for i, b in enumerate(data))
        if op in (0x1, 0x2): return data.decode("utf-8", "replace")

def main():
    ws_url, times = sys.argv[1], int(sys.argv[2] if len(sys.argv) > 2 else 100)
    rest = ws_url[len("ws://"):]; host, _, path = rest.partition("/")
    host, _, port = host.partition(":")
    ws = socket.create_connection((host, int(port or 80)))
    key = base64.b64encode(os.urandom(16)).decode()
    ws.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        c = ws.recv(4096)
        if not c: raise RuntimeError("ws handshake failed")
        resp += c
    mid = [0]
    def call(method, params=None, timeout=60):
        mid[0] += 1
        send(ws, {"id": mid[0], "method": method, "params": params or {}})
        end = time.time() + timeout
        while time.time() < end:
            ws.settimeout(2.0)
            try: raw = read_msg(ws)
            except socket.timeout: continue
            except Exception: return None
            try: j = json.loads(raw)
            except Exception: continue
            if j.get("id") == mid[0]:
                r = j.get("result", {})
                v = r.get("result", r)
                if isinstance(v, dict) and "value" in v: return v["value"]
                return v
        return None
    call("Page.enable")
    call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True})
    call("Page.navigate", {"url": "http://127.0.0.1:8790/"})
    time.sleep(4.0)
    # 安装 timer 跟踪
    call("Runtime.evaluate", {"expression": """
      (function(){
        window.__tset = new Set();
        var _st = window.setTimeout, _ct = window.clearTimeout;
        window.setTimeout = function(fn, ms, a, b){ var id = _st.apply(window, arguments); window.__tset.add(id); return id; };
        window.clearTimeout = function(id){ _ct.call(window, id); window.__tset.delete(id); };
        return true;
      })()
    """, "returnByValue": True})
    time.sleep(2.0)
    def snapshot(tag):
        v = call("Runtime.evaluate", {"expression": """
          (function(){
            var heap = 0;
            if (performance.memory) heap = Math.round(performance.memory.usedJSHeapSize / 1048576);
            return JSON.stringify({
              charts: LM.charts ? LM.charts.instanceCount() : -1,
              timers: window.__tset ? window.__tset.size : -1,
              heapMB: heap,
              page: LM.nav.currentPage()
            });
          })()
        """, "returnByValue": True})
        d = json.loads(v) if v else {}
        d["tag"] = tag
        return d
    base = snapshot("baseline")
    # 切换页面
    pages = ["overview", "usage", "performance", "system", "gpu", "history", "settings", "about"]
    for i in range(times):
        p = pages[i % len(pages)]
        call("Runtime.evaluate", {"expression": "LM.nav.showPage(%s); 1" % json.dumps(p), "returnByValue": True})
    time.sleep(3.0)
    after = snapshot("after-%d-switches" % times)
    # 再切回 overview 让所有图表都初始化过，第二次基线对比
    call("Runtime.evaluate", {"expression": "LM.nav.showPage('overview'); 1", "returnByValue": True})
    time.sleep(3.0)
    stable = snapshot("after-return-stable")
    res = {"baseline": base, "after": after, "stable": stable,
           "chartDelta": stable["charts"] - base["charts"],
           "timerDelta": stable["timers"] - base["timers"]}
    print(json.dumps(res, ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
