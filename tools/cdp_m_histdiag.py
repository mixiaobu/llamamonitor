#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""history 页诊断：钩住 console.warn/error，多次重进 history，看 fetch/渲染报错。
用法: cdp_m_histdiag.py <ws> <w> <h>"""
import sys, socket, struct, base64, os, json, time
exec(compile(open("tools/cdp_m_waitprobe.py", encoding="utf-8").read().split("def main")[0], "wp", "exec"))

HOOK = r"""
(function(){
  if (!window.__hd) {
    window.__hd = { warns: [] };
    var ow = console.warn, oe = console.error;
    console.warn = function(){ window.__hd.warns.push("W:" + Array.prototype.map.call(arguments, String).join(" ").slice(0,160)); ow.apply(console, arguments); };
    console.error = function(){ window.__hd.warns.push("E:" + Array.prototype.map.call(arguments, String).join(" ").slice(0,160)); oe.apply(console, arguments); };
  }
  LM.nav.showPage("history");
  return "hooked";
})()
"""
READ = r"""
(function(){
  var out = {
    rows: document.querySelectorAll("#gapsTbody tr.gap-row").length,
    wrapDisplay: (function(){ var e=document.getElementById("gapsTableWrap"); return e?getComputedStyle(e).display:null; })(),
    wrapHiddenInline: (function(){ var e=document.getElementById("gapsTableWrap"); return e?e.style.display:null; })(),
    emptyHidden: (function(){ var e=document.getElementById("gapsEmpty"); return e?e.hidden:null; })(),
    countLabel: (function(){ var e=document.getElementById("gapsCountLabel"); return e?e.textContent:null; })(),
    warns: (window.__hd ? window.__hd.warns.slice(-10) : [])
  };
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
    time.sleep(5.0)
    call("Runtime.evaluate", {"expression": "window.__lmSetVisible && window.__lmSetVisible(true); 1", "returnByValue": True})
    for i in range(4):
        call("Runtime.evaluate", {"expression": HOOK, "returnByValue": True})
        time.sleep(4.0)
        v = call("Runtime.evaluate", {"expression": READ, "returnByValue": True})
        print("iter%d: %s" % (i, v))
        try:
            if json.loads(v).get("rows", 0) > 0:
                break
        except Exception:
            pass

if __name__ == "__main__":
    main()
