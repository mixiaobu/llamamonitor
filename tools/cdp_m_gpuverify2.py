#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GPU 进程表验证 v2：钩 console.warn/error 抓 "gpu status failed"，
多次重进 + 长 settle。用法: cdp_m_gpuverify2.py <ws> <w> <h>"""
import sys, socket, struct, base64, os, json, time
exec(compile(open("tools/cdp_m_waitprobe.py", encoding="utf-8").read().split("def main")[0], "wp", "exec"))

HOOK = r"""
(function(){
  if (!window.__gw) {
    window.__gw = [];
    var ow = console.warn, oe = console.error;
    console.warn = function(){ window.__gw.push("W:" + Array.prototype.map.call(arguments, String).join(" ").slice(0,140)); ow.apply(console, arguments); };
    console.error = function(){ window.__gw.push("E:" + Array.prototype.map.call(arguments, String).join(" ").slice(0,140)); oe.apply(console, arguments); };
  }
  return "ok";
})()
"""
PROBE = 'document.querySelectorAll("#gpuProc .gpu-proc-table tbody tr").length || 0'
MEASURE = r"""
(function(){
  var rws = document.querySelectorAll("#gpuProc .gpu-proc-table tbody tr");
  if (!rws.length) return JSON.stringify({rows:0, warns:(window.__gw||[]).slice(-6)});
  var out = { rows: rws.length, hOverflow: (document.documentElement.scrollWidth > document.documentElement.clientWidth ? (document.documentElement.scrollWidth - document.documentElement.clientWidth) + "px" : "none") };
  var tds = rws[0].querySelectorAll("td");
  out.appW = Math.round(tds[0].getBoundingClientRect().width);
  out.pidW = Math.round(tds[1].getBoundingClientRect().width);
  out.gpuW = tds[2] ? Math.round(tds[2].getBoundingClientRect().width) : null;
  out.memW = tds[3] ? Math.round(tds[3].getBoundingClientRect().width) : null;
  var a = tds[1].getBoundingClientRect(), b = tds[2].getBoundingClientRect();
  out.overlap = a.right > b.left + 0.5 ? Math.round(a.right - b.left) + "px" : "none";
  out.rowH = Math.round(rws[0].getBoundingClientRect().height);
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
    def poll():
        v = call("Runtime.evaluate", {"expression": PROBE, "returnByValue": True})
        try: return int(v)
        except Exception: return 0
    call("Page.enable")
    call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 2, "mobile": True})
    call("Page.navigate", {"url": "http://127.0.0.1:8790/"})
    time.sleep(5.0)
    call("Runtime.evaluate", {"expression": "window.__lmSetVisible && window.__lmSetVisible(true); 1", "returnByValue": True})
    call("Runtime.evaluate", {"expression": HOOK, "returnByValue": True})
    call("Runtime.evaluate", {"expression": "LM.nav.showPage('gpu'); 1", "returnByValue": True})
    n = 0
    for _ in range(45):
        time.sleep(1.0); n = poll()
        if n > 0: break
    if n == 0:
        # 重进再等
        call("Runtime.evaluate", {"expression": "LM.nav.showPage('overview'); 1", "returnByValue": True})
        time.sleep(0.5)
        call("Runtime.evaluate", {"expression": "LM.nav.showPage('gpu'); 1", "returnByValue": True})
        for _ in range(45):
            time.sleep(1.0); n = poll()
            if n > 0: break
    v = call("Runtime.evaluate", {"expression": MEASURE, "returnByValue": True})
    print("ROWS:" + str(n))
    print(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))

if __name__ == "__main__":
    main()
