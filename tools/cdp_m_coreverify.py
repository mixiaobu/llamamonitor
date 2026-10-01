#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core-heat 确定性验证：navigate → showPage(system) → 耐心等（单次 fetch 完整窗口，
不重进；空则重进再等）→ 展开 details 测格尺寸/重叠/文本。用法: cdp_m_coreverify.py <ws> <w> <h>"""
import sys, socket, struct, base64, os, json, time
exec(compile(open("tools/cdp_m_waitprobe.py", encoding="utf-8").read().split("def main")[0], "wp", "exec"))

PROBE = 'document.querySelectorAll(".core-heat-grid .core-cell").length || 0'
MEASURE = r"""
(function(){
  var d = document.querySelector("details.core-heat");
  if (d && !d.open) d.open = true;
  var out = {};
  var doc = document.documentElement;
  out.hOverflow = doc.scrollWidth > doc.clientWidth ? (doc.scrollWidth - doc.clientWidth) + "px" : "none";
  var cells = document.querySelectorAll(".core-heat-grid .core-cell");
  out.cells = cells.length;
  if (cells.length) {
    var c0 = cells[0].getBoundingClientRect();
    out.cell0 = Math.round(c0.width) + "x" + Math.round(c0.height);
    var overlap = 0;
    for (var i = 1; i < Math.min(cells.length, 30); i++) {
      var a = cells[i-1].getBoundingClientRect(), b = cells[i].getBoundingClientRect();
      if (a.right > b.left + 0.5 && Math.abs(a.top - b.top) < 5) {
        var o = a.right - b.left; if (o > overlap) overlap = o;
      }
    }
    out.overlap = overlap > 0 ? Math.round(overlap) + "px" : "none";
    var pct = cells[0].querySelector("span:last-child");
    if (pct) out.textFits = pct.scrollWidth <= pct.clientWidth + 1 ? "fits" : "overflow " + (pct.scrollWidth - pct.clientWidth) + "px";
    var cols = getComputedStyle(document.querySelector(".core-heat-grid")).gridTemplateColumns.split(" ");
    out.trackW = cols.length ? Math.round(parseFloat(cols[0])) + "px (" + cols.length + " cols)" : "?";
  }
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
    time.sleep(4.0)
    call("Runtime.evaluate", {"expression": "window.__lmSetVisible && window.__lmSetVisible(true); 1", "returnByValue": True})
    call("Runtime.evaluate", {"expression": "LM.nav.showPage('system'); 1", "returnByValue": True})
    n = 0
    for _ in range(40):
        time.sleep(1.0); n = poll()
        if n > 0: break
    if n == 0:
        call("Runtime.evaluate", {"expression": "LM.nav.showPage('overview'); 1", "returnByValue": True})
        time.sleep(0.5)
        call("Runtime.evaluate", {"expression": "LM.nav.showPage('system'); 1", "returnByValue": True})
        for _ in range(40):
            time.sleep(1.0); n = poll()
            if n > 0: break
    v = call("Runtime.evaluate", {"expression": MEASURE, "returnByValue": True})
    print("CELLS:" + str(n))
    print(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))

if __name__ == "__main__":
    main()
