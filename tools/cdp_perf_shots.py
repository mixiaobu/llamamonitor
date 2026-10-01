#!/usr/bin/env python3
"""
cdp_perf_shots.py — Round 5 推理性能页 AFTER 截图（Edge CDP）。

约定（与 BEFORE 对齐）：
  - top  = 1x viewport（滚动顶部）
  - full = 2x viewport（deviceScaleFactor=2，滚动顶部）——BEFORE 的 "full" 即此
页面 .content 是内部滚动容器（非 document scroll），故 top/full 都只覆盖视口顶部。
  --scroll 模式：对给定尺寸把 .content 逐屏滚动到底，每屏存一片（-s0/-s1/...），
  用于真正审阅折叠层（Slot 表 / 模型与运行环境）。

Usage:
  cdp_perf_shots.py <ws> <prefix> [--zoom 100] [--sizes a,b,c] [--scroll]
"""
import sys, socket, struct, base64, os, json, time

def send(ws, obj):
    data = json.dumps(obj).encode("utf-8")
    header = bytearray([0x81]); n = len(data)
    if n < 126: header.append(0x80 | n)
    elif n < 65536: header.append(0x80 | 126); header += struct.pack(">H", n)
    else: header.append(0x80 | 127); header += struct.pack(">Q", n)
    mask = os.urandom(4); header += mask
    ws.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

def _recv_exact(ws, n):
    buf = b""
    while len(buf) < n:
        c = ws.recv(n - len(buf))
        if not c: raise RuntimeError("ws closed")
        buf += c
    return buf

def read_msg(ws, timeout):
    ws.settimeout(timeout)
    while True:
        b0, b1 = _recv_exact(ws, 2)
        op = b0 & 0x0F; masked = b1 & 0x80; n = b1 & 0x7F
        if n == 126: n = struct.unpack(">H", _recv_exact(ws, 2))[0]
        elif n == 127: n = struct.unpack(">Q", _recv_exact(ws, 8))[0]
        mask = _recv_exact(ws, 4) if masked else None
        data = _recv_exact(ws, n) if n else b""
        if mask: data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if op == 0x9:
            pm = os.urandom(4); h = bytearray([0x8A, 0x80 | len(data)])
            h += pm; ws.sendall(bytes(h) + bytes(b ^ pm[i % 4] for i, b in enumerate(data))); continue
        if op in (0x1, 0x2): return data.decode("utf-8", "replace")

def main():
    if len(sys.argv) < 3:
        print("usage: cdp_perf_shots.py <ws> <prefix> [--zoom N] [--sizes a,b] [--scroll]", file=sys.stderr); sys.exit(2)
    url = sys.argv[1]; prefix = sys.argv[2]
    zoom = 100; sizes = None; scroll = False
    i = 3
    while i < len(sys.argv):
        if sys.argv[i] == "--zoom": zoom = int(sys.argv[i+1]); i += 2
        elif sys.argv[i] == "--sizes": sizes = sys.argv[i+1].split(","); i += 2
        elif sys.argv[i] == "--scroll": scroll = True; i += 1
        else: i += 1

    matrix = sizes or ["1920x1080", "1600x900", "1440x900", "1366x768",
                        "1200x900", "1024x1366", "988x1394", "900x900",
                        "820x1180", "768x1024", "760x900", "600x900",
                        "430x932", "390x844", "360x800", "320x568"]

    rest = url[len("ws://"):]; host, _, path = rest.partition("/"); host, _, port = host.partition(":")
    ws = socket.create_connection((host, int(port or 80)))
    key = base64.b64encode(os.urandom(16)).decode()
    ws.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        c = ws.recv(4096)
        if not c: raise RuntimeError("handshake failed")
        resp += c
    mid = [0]
    def call(method, params=None, timeout=60):
        mid[0] += 1; i = mid[0]
        send(ws, {"id": i, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            try: raw = read_msg(ws, max(0.5, deadline - time.time()))
            except Exception: return {"_timeout": True}
            try: j = json.loads(raw)
            except Exception: continue
            if j.get("id") == i:
                return j.get("error") if "error" in j else j.get("result", {})
        return {"_timeout": True}

    def shot(dpr, out):
        call("Emulation.setDeviceMetricsOverride",
             {"width": 0, "height": 0, "deviceScaleFactor": dpr, "mobile": False}, 5) if False else None
        r = call("Page.captureScreenshot", {"format": "png"}, 60)
        if isinstance(r, dict) and "data" in r:
            with open(out, "wb") as f: f.write(base64.b64decode(r["data"]))
            return os.path.getsize(out) // 1024
        return -1

    def set_metrics(w, h, dpr):
        call("Emulation.setDeviceMetricsOverride",
             {"width": w, "height": h, "deviceScaleFactor": dpr, "mobile": w <= 760}, 10)

    def scroll_top():
        call("Runtime.evaluate", {"expression": "(document.querySelector('.content')||document.scrollingElement).scrollTo(0,0);1", "returnByValue": True}, 8)

    def scroll_by(dy):
        call("Runtime.evaluate", {"expression": "var e=document.querySelector('.content')||document.scrollingElement;e.scrollBy(0,%d);1" % dy, "returnByValue": True}, 8)

    def content_scroll_state():
        r = call("Runtime.evaluate", {"expression":
            "(function(){var e=document.querySelector('.content');if(!e)return JSON.stringify({sh:0,ch:0,st:0});"
            "return JSON.stringify({sh:e.scrollHeight,ch:e.clientHeight,st:e.scrollTop})})()", "returnByValue": True}, 8)
        try: return json.loads(r.get("result", {}).get("value", "{}"))
        except Exception: return {"sh": 0, "ch": 0, "st": 0}

    call("Page.enable", {}, 10)
    call("Runtime.enable", {}, 10)
    call("Page.setWebLifecycleState", {"state": "active"}, 10)
    call("Page.reload", {"ignoreCache": True}, 15)
    for _ in range(30):
        time.sleep(0.5)
        r = call("Runtime.evaluate", {"expression": "!!(window.LM && LM.app)", "returnByValue": True}, 8)
        if isinstance(r, dict) and r.get("result", {}).get("value"): break
    call("Runtime.evaluate", {"expression": "LM.nav.showPage('performance');1", "returnByValue": True}, 10)

    def wait_app(timeout=15):
        for _ in range(int(timeout * 2)):
            time.sleep(0.5)
            r = call("Runtime.evaluate", {"expression": "!!(window.LM && LM.app)", "returnByValue": True}, 8)
            if isinstance(r, dict) and r.get("result", {}).get("value"):
                return
    def reload_at(w, h):
        # 每个尺寸独立 fresh load（真实用户在该宽度首次加载，而非从桌面缩放）——
        # 避免 ECharts canvas 在 CDP resize-down 时残留桌面宽度导致的假溢出。
        set_metrics(w, h, 1.0)
        call("Page.reload", {"ignoreCache": True}, 15)
        wait_app(15)
        call("Runtime.evaluate", {"expression": "LM.nav.showPage('performance');1", "returnByValue": True}, 10)
        time.sleep(2.2)

    ztag = "" if zoom == 100 else "-z%d" % zoom
    for spec in matrix:
        w, h = (int(x) for x in spec.split("x"))
        reload_at(w, h)
        scroll_top(); time.sleep(0.3)
        kb = shot(1.0, "%s-perf-%dx%d%s-top.png" % (prefix, w, h, ztag))
        print("top  %-12s %d KB" % (spec, kb))
        # full = 2x viewport（对齐 BEFORE）
        set_metrics(w, h, 2.0)
        time.sleep(0.6)
        scroll_top(); time.sleep(0.2)
        kb = shot(2.0, "%s-perf-%dx%d%s-full.png" % (prefix, w, h, ztag))
        print("full %-12s %d KB (2x)" % (spec, kb))
        # 折叠层切片：top 已覆盖顶部；逐屏下滚后截 s1, s2, ...（真正审阅折叠层）
        if scroll:
            set_metrics(w, h, 1.0)
            time.sleep(0.5)
            idx = 1
            for _ in range(12):
                st = content_scroll_state()
                sh, ch = st["sh"], st["ch"]
                if ch <= 0: break
                scroll_by(ch)
                time.sleep(1.0)
                st2 = content_scroll_state()
                kb = shot(1.0, "%s-perf-%dx%d%s-s%d.png" % (prefix, w, h, ztag, idx))
                print("slice s%d %-11s %d KB (scrollTop=%d/%d)" % (idx, spec, kb, st2["st"], sh))
                if st2["st"] >= sh - ch - 4: break
                idx += 1
    ws.close()
    print("DONE zoom=%d%% sizes=%d scroll=%s" % (zoom, len(matrix), scroll))

if __name__ == "__main__":
    main()
