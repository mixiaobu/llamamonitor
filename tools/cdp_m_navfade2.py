#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导航层级 A/B 验证：切页 fade 期间（0-150ms，15ms 间隔采样）nav 位置的 topmost 元素。
Phase A：mnav z-index 强制 auto（修复前行为）→ 期望内容盖住 nav（复现"闪一下"）。
Phase B：恢复 token z:40（修复后）→ 期望全程 nav 自身元素在最上。
用法: cdp_m_navfade2.py <ws>
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
        b0, b1 = _recv(ws, 2)
        if b0 & 0x8 == 0x8: raise TimeoutError("ws closed")
        op = b0 & 0x0F; n = b1 & 0x7F
        if n == 126: n = struct.unpack(">H", _recv(ws, 2))[0]
        elif n == 127: n = struct.unpack(">Q", _recv(ws, 8))[0]
        mk = _recv(ws, 4) if b1 & 0x80 else None
        data = _recv(ws, n) if n else b""
        if mk: data = bytes(b ^ mk[i % 4] for i, b in enumerate(data))
        if op in (1, 2): return data.decode("utf-8", "replace")

START = r"""
(function(){
  var mnav = document.querySelector(".mobile-nav");
  function topAt(){
    var r = mnav.getBoundingClientRect();
    var el = document.elementFromPoint(Math.round(r.left + r.width*0.18), Math.round(r.top + r.height*0.5));
    if(!el) return "none";
    var cls = (el.className && el.className.baseVal !== undefined ? el.className.baseVal : (el.className||"")).toString();
    return cls.slice(0,28) || el.tagName;
  }
  function navOnTop(){ var t = topAt(); return t.indexOf("mnav") !== -1 || t === "none"; }
  var t0 = performance.now();
  var t = 0;
  function sample(phase){
    return { t: Math.round(performance.now()-t0), top: topAt(), navOnTop: navOnTop() };
  }
  function runPhase(phaseName, forceZ, target, done){
    if (forceZ !== null) mnav.style.zIndex = forceZ;
    LM.nav.showPage(target);
    var samples = [];
    t = 0;
    (function step(){
      try {
        samples.push(sample(phaseName));
        t++;
        if (t < 11) { setTimeout(step, 15); }
        else { window.__fadeResult[phaseName] = samples; done && done(); }
      } catch (e) {
        window.__fadeResult[phaseName] = samples;
        window.__fadeErr = phaseName + ": " + e.message;
        window.__fadeDone = true;
      }
    })();
  }
  window.__fadeResult = {};
  var cur = LM.nav.currentPage();
  var other = cur === "usage" ? "system" : "usage";
  runPhase("pre_zauto", "auto", other, function(){
    setTimeout(function(){
      runPhase("post_z40", null, cur, function(){
        setTimeout(function(){ window.__fadeDone = true; }, 50);
      });
    }, 150);
  });
  return "started:" + cur + "->" + other;
})()
"""

def main():
    ws_url = sys.argv[1]
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
                r = j.get("result", {})
                v = r.get("result", r)
                if isinstance(v, dict) and "value" in v: return v["value"]
                return v
        return None
    call("Page.enable")
    call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True})
    call("Page.navigate", {"url": "http://127.0.0.1:8790/"})
    time.sleep(4.0)
    call("Page.bringToFront")
    # 先确认 LM.nav.currentPage 可用
    print("probe currentPage:", call("Runtime.evaluate", {"expression": "String(typeof LM.nav.currentPage)", "returnByValue": True}))
    print("visibility:", call("Runtime.evaluate", {"expression": "JSON.stringify({vis: document.visibilityState, hidden: document.hidden})", "returnByValue": True}))
    r1 = call("Runtime.evaluate", {"expression": START, "returnByValue": True})
    print("start:", r1)
    time.sleep(6.0)
    r2 = call("Runtime.evaluate", {"expression": "JSON.stringify({done: window.__fadeDone, err: window.__fadeErr || null, result: window.__fadeResult})", "returnByValue": True})
    print(r2)

if __name__ == "__main__":
    main()
