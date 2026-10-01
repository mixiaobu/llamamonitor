#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""history 缺口表确定性验证：navigate → 单次 showPage(history) 触发 onShow 的
单一 refreshHistoryGaps（gen 只 +1），先耐心等它自然完成（不 re-enter 避免
gen 递增把在途响应判 stale 丢弃）；40s 仍空才 re-enter 一次再等。
用法: cdp_m_gapverify2.py <ws> <w> <h>"""
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
  // 卡头与字段行是否都撑满（宽度应≈行宽）
  var widths = Array.prototype.map.call(rws0.querySelectorAll("td"), function(td){ return Math.round(td.getBoundingClientRect().width); });
  out.minTdWidth = Math.min.apply(null, widths);
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
    # 单次 onShow（gen +1），耐心等待其自然完成（不 re-enter）
    call("Runtime.evaluate", {"expression": "LM.nav.showPage('history'); 1", "returnByValue": True})
    n = 0
    for _ in range(40):
        time.sleep(1.0)
        n = poll()
        if n > 0: break
    if n == 0:
        # 单次 re-enter 后再等（gen +2，之前的在途若完成会被判 stale 丢弃，故给 gen-2 充足时间）
        call("Runtime.evaluate", {"expression": "LM.nav.showPage('overview'); 1", "returnByValue": True})
        time.sleep(0.5)
        call("Runtime.evaluate", {"expression": "LM.nav.showPage('history'); 1", "returnByValue": True})
        for _ in range(30):
            time.sleep(1.0)
            n = poll()
            if n > 0: break
    v = call("Runtime.evaluate", {"expression": MEASURE, "returnByValue": True})
    cnt = call("Runtime.evaluate", {"expression": "(document.getElementById('gapsCountLabel')||{}).textContent || ''", "returnByValue": True})
    print("ROWS:" + str(n))
    print("CNT:" + str(cnt))
    print(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))

if __name__ == "__main__":
    main()
