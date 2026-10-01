#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证导航层级修复：切页 fade 期间逐帧采样 nav 位置的 topmost 元素。
修复后所有帧都应是 mnav-item（导航盖住内容，无闪烁）。
用法: cdp_m_navfade.py <ws>
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

PROBE = """
(async function(){
  var mnav = document.querySelector(".mobile-nav");
  function nr(){ return mnav.getBoundingClientRect(); }
  function topAt(fx){
    var r = nr();
    var el = document.elementFromPoint(Math.round(r.left + r.width*fx), Math.round(r.top + r.height*0.5));
    if(!el) return "none";
    return (el.className||el.tagName).toString().slice(0,30);
  }
  function nextFrame(){ return new Promise(function(res){ requestAnimationFrame(function(){ requestAnimationFrame(res); }); }); }
  var out = { navZ: getComputedStyle(mnav).zIndex, samples: [] };
  var target = LM.nav.currentPage() === "usage" ? "system" : "usage";
  LM.nav.showPage(target);
  for (var i=0; i<8; i++) {
    out.samples.push({ i: i, top: topAt(0.18), overNav: topAt(0.18).indexOf("mnav") === -1 });
    await nextFrame();
  }
  return JSON.stringify(out);
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
    # 带原始响应的 evaluate
    mid[0] += 1
    send(ws, {"id": mid[0], "method": "Runtime.evaluate",
              "params": {"expression": PROBE, "returnByValue": True, "awaitPromise": True}})
    end = time.time() + 30
    raw_out = None
    while time.time() < end:
        ws.settimeout(2.0)
        try: raw = read_msg(ws)
        except socket.timeout: continue
        try: j = json.loads(raw)
        except Exception: continue
        if j.get("id") == mid[0]:
            raw_out = j
            break
    print(json.dumps(raw_out, ensure_ascii=False, indent=1)[:2000])

if __name__ == "__main__":
    main()
