#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""强制渲染 GPU 进程表后测量布局（确定性）：
navigate → showPage(gpu) → 循环 { 重进页面触发 onShow fetch; 每 1.5s 查表 } 直到 rows>0 或超时。
用法: cdp_m_gpuverify.py <ws> <w> <h>
"""
import sys, socket, struct, base64, os, json, time
exec(compile(open("tools/cdp_m_waitprobe.py", encoding="utf-8").read().split("def main")[0], "wp", "exec"))

PROBE = 'document.querySelectorAll("#gpuProc .gpu-proc-table tbody tr").length || 0'
MEASURE = r"""
(function(){
  var rws = document.querySelectorAll("#gpuProc .gpu-proc-table tbody tr");
  if (!rws.length) return JSON.stringify({rows:0});
  var out = { rows: rws.length };
  var tds = rws[0].querySelectorAll("td");
  out.rowRect = (function(){ var r=rws[0].getBoundingClientRect(); return Math.round(r.width)+"x"+Math.round(r.height); })();
  out.rowDisplay = getComputedStyle(rws[0]).display;
  out.tdRects = Array.prototype.map.call(tds, function(td){ var r=td.getBoundingClientRect(); return (td.dataset.label||"app")+"/"+Math.round(r.width); });
  if (tds.length >= 3) {
    var a = tds[1].getBoundingClientRect(), b = tds[2].getBoundingClientRect();
    out.overlap23 = a.right > b.left ? Math.round(a.right-b.left)+"px" : "none";
  }
  out.cardHeights = Array.prototype.slice.call(rws,0,3).map(function(r){ return Math.round(r.getBoundingClientRect().height); });
  return JSON.stringify(out);
})()
"""

def main():
    ws_url, w, h = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
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
    def call(method, params=None, timeout=30):
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
                r = j.get("result", {}); v = r.get("result", r)
                if isinstance(v, dict) and "value" in v: return v["value"]
                return v
        return None
    call("Page.enable")
    call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 2, "mobile": True})
    call("Page.navigate", {"url": "http://127.0.0.1:8790/"})
    time.sleep(4.0)
    call("Runtime.evaluate", {"expression": "window.__lmSetVisible && window.__lmSetVisible(true); 1", "returnByValue": True})
    call("Runtime.evaluate", {"expression": "LM.nav.showPage('gpu'); 1", "returnByValue": True})
    end = time.time() + 40
    last = 0
    while time.time() < end:
        n = call("Runtime.evaluate", {"expression": PROBE, "returnByValue": True})
        try: n = int(n)
        except Exception: n = 0
        last = n
        if n > 0:
            break
        # 每 6s 重进一次页面（重触发 onShow fetch）
        call("Runtime.evaluate", {"expression": "LM.nav.showPage('overview'); 1", "returnByValue": True})
        time.sleep(0.4)
        call("Runtime.evaluate", {"expression": "LM.nav.showPage('gpu'); 1", "returnByValue": True})
        time.sleep(3.0)
    v = call("Runtime.evaluate", {"expression": MEASURE, "returnByValue": True})
    print("LAST_PROBE:" + str(last))
    print(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))

if __name__ == "__main__":
    main()
