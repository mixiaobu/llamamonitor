# cdp_sys_mobile.py — 移动端溢出审计（320/360/390）：.content 水平溢出 + 关键 grid 是否破列。
# Usage: cdp_sys_mobile.py <ws>
import sys, socket, struct, base64, os, json, time
def send(ws, obj):
    data = json.dumps(obj).encode("utf-8")
    h = bytearray([0x81]); n = len(data)
    if n < 126: h.append(0x80 | n)
    elif n < 65536: h.append(0x80 | 126); h += struct.pack(">H", n)
    else: h += b"\x80\xfe" + struct.pack(">Q", n)
    m = os.urandom(4); h += m
    ws.sendall(bytes(h) + bytes(b ^ m[i % 4] for i, b in enumerate(data)))
def _rx(ws, n):
    b = b""
    while len(b) < n:
        c = ws.recv(n - len(b)); b += c
    return b
def read_msg(ws, t):
    ws.settimeout(t)
    while True:
        b0, b1 = _rx(ws, 2); op = b0 & 0xF; masked = b1 & 0x80; n = b1 & 0x7F
        if n == 126: n = struct.unpack(">H", _rx(ws, 2))[0]
        elif n == 127: n = struct.unpack(">Q", _rx(ws, 8))[0]
        mk = _rx(ws, 4) if masked else None; d = _rx(ws, n) if n else b""
        if mk: d = bytes(x ^ mk[i % 4] for i, x in enumerate(d))
        if op == 0x9:
            pm = os.urandom(4); hh = bytearray([0x8A, 0x80 | len(d)]); hh += pm
            ws.sendall(bytes(hh) + bytes(x ^ pm[i % 4] for i, x in enumerate(d))); continue
        if op in (0x1, 0x2): return d.decode("utf-8", "replace")
def main():
    url = sys.argv[1]
    rest = url[len("ws://"):]; host, _, path = rest.partition("/"); host, _, port = host.partition(":")
    ws = socket.create_connection((host, int(port or 80)))
    key = base64.b64encode(os.urandom(16)).decode()
    ws.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        c = ws.recv(4096)
        if not c: raise RuntimeError("hs")
        resp += c
    mid = [0]
    def call(method, params=None, timeout=20):
        mid[0] += 1; i = mid[0]; send(ws, {"id": i, "method": method, "params": params or {}})
        dl = time.time() + timeout
        while time.time() < dl:
            try: raw = read_msg(ws, max(0.3, dl - time.time()))
            except Exception: return {"_t": 1}
            try: j = json.loads(raw)
            except Exception: continue
            if j.get("id") == i: return j.get("error") if "error" in j else j.get("result", {})
        return {"_t": 1}
    def ev(x, t=12):
        r = call("Runtime.evaluate", {"expression": x, "returnByValue": True}, t)
        return r.get("result", {}).get("value")
    call("Runtime.enable"); call("Page.enable"); call("Page.setWebLifecycleState", {"state": "active"}, 10)
    probe = """(function(){
      var docW = document.documentElement.clientWidth;
      var content = document.querySelector('.content');
      var overflow = content ? (content.scrollWidth - content.clientWidth) : (document.documentElement.scrollWidth - docW);
      // 找出真正造成水平溢出的最宽元素（相对 content）
      var wide = [];
      var all = document.querySelectorAll('#page-system *');
      for (var i=0;i<all.length;i++){
        var e = all[i]; var r = e.getBoundingClientRect();
        if (r.right > docW + 1 && r.width > 40){ wide.push(e.className && e.className.baseVal!==undefined ? e.className.baseVal : String(e.className).slice(0,40)); }
      }
      // 关键 grid 列数
      function cols(sel){ var e=document.querySelector(sel); if(!e) return null; var s=getComputedStyle(e); return s.gridTemplateColumns.split(' ').length; }
      return JSON.stringify({
        width:docW, contentOverflowPx:overflow,
        wideElCount:wide.length, wideEls:wide.slice(0,6),
        overviewCols:cols('.sys-overview-grid'), cpuCols:cols('.sys-cpu-summary'),
        memCols:cols('.sys-mem-summary'), netCols:cols('.sys-net-summary'),
        powerCols:cols('.sys-power-grid'), kvCols:cols('.kv-grid'),
        heatFoldOpen: (document.getElementById('coreHeatFold')||{}).open
      });
    })()"""
    for (w, h) in [(390, 844), (360, 800), (320, 568)]:
        call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 1.0, "mobile": w <= 760}, 10)
        call("Page.reload", {"ignoreCache": True}, 20)
        for _ in range(36):
            time.sleep(0.5)
            if ev("!!(window.LM && LM.app)"): break
        time.sleep(0.5)
        ev("LM.nav.showPage('system');1")
        time.sleep(2.5)
        print("%dx%d:" % (w, h))
        print("  ", ev(probe, 15))
    ws.close(); print("DONE")
if __name__ == "__main__":
    main()
