#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GPU 页状态探测：gpuCards 内容 / 无GPU empty-state / gpuProc 长度 / available。"""
import sys, socket, struct, base64, os, json, time
exec(compile(open("tools/cdp_m_waitprobe.py", encoding="utf-8").read().split("def main")[0], "wp", "exec"))
STATE = r"""
(function(){
  var out = {};
  out.page = (document.querySelector(".page.active")||{}).id;
  var cards = document.getElementById("gpuCards");
  out.cards = cards ? { len: cards.innerHTML.length, hasEmpty: !!cards.querySelector(".empty-state"), emptyTitle: (cards.querySelector(".empty-title")||{}).textContent, cardCount: cards.querySelectorAll(".gpu-card").length } : "absent";
  var gp = document.getElementById("gpuProc");
  out.gpuProc = gp ? { len: gp.innerHTML.length, rows: gp.querySelectorAll("tbody tr").length, head: gp.innerHTML.slice(0,120) } : "absent";
  out.state = (document.getElementById("gpuPageState")||{}).textContent;
  return JSON.stringify(out, null, 1);
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
    time.sleep(5.0)
    call("Runtime.evaluate", {"expression": "window.__lmSetVisible && window.__lmSetVisible(true); 1", "returnByValue": True})
    call("Runtime.evaluate", {"expression": "LM.nav.showPage('gpu'); 1", "returnByValue": True})
    time.sleep(8.0)
    print(call("Runtime.evaluate", {"expression": STATE, "returnByValue": True}))

if __name__ == "__main__":
    main()
