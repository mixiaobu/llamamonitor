#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""强制渲染采集缺口表后测量布局（确定性）。用法: cdp_m_gapverify.py <ws> <w> <h>"""
import sys, socket, struct, base64, os, json, time
exec(compile(open("tools/cdp_m_waitprobe.py", encoding="utf-8").read().split("def main")[0], "wp", "exec"))

PROBE = 'document.querySelectorAll("#gapsTbody tr.gap-row").length || 0'
MEASURE = r"""
(function(){
  var rows = document.querySelectorAll("#gapsTbody tr.gap-row");
  if (!rows.length) return JSON.stringify({rows:0});
  var out = { rows: rows.length };
  var rws0 = rows[0];
  out.rowRect = (function(){ var r=rws0.getBoundingClientRect(); return Math.round(r.width)+"x"+Math.round(r.height); })();
  out.rowDisplay = getComputedStyle(rws0).display;
  out.tdRects = Array.prototype.map.call(rws0.querySelectorAll("td"), function(td){ var r=td.getBoundingClientRect(); return (td.dataset.label||"?")+"/"+Math.round(r.width); });
  out.headerWidth = Math.round(rws0.querySelector("td").getBoundingClientRect().width);
  out.rowWidth = Math.round(rws0.getBoundingClientRect().width);
  out.cardHeights = Array.prototype.slice.call(rows,0,3).map(function(r){ return Math.round(r.getBoundingClientRect().height); });
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
    call("Runtime.evaluate", {"expression": "LM.nav.showPage('history'); 1", "returnByValue": True})
    end = time.time() + 40
    last = 0
    while time.time() < end:
        n = call("Runtime.evaluate", {"expression": PROBE, "returnByValue": True})
        try: n = int(n)
        except Exception: n = 0
        last = n
        if n > 0:
            break
        call("Runtime.evaluate", {"expression": "LM.nav.showPage('overview'); 1", "returnByValue": True})
        time.sleep(0.4)
        call("Runtime.evaluate", {"expression": "LM.nav.showPage('history'); 1", "returnByValue": True})
        time.sleep(3.0)
    v = call("Runtime.evaluate", {"expression": MEASURE, "returnByValue": True})
    print("LAST_PROBE:" + str(last))
    print(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))

if __name__ == "__main__":
    main()
