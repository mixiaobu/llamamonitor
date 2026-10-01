#!/usr/bin/env python3
"""CDP: reload app, capture console errors / JS exceptions, then exercise
the usage page (range modes, custom range, trend toggle, hourly) and report
state + any console.error / uncaught exceptions.
Usage: cdp_usage_probe.py <ws_url>
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

def read_msg(ws, deadline):
    ws.settimeout(max(0.2, deadline - time.time()))
    while time.time() < deadline:
        b0, b1 = _recv_exact(ws, 2)
        op = b0 & 0x0F; masked = b1 & 0x80; n = b1 & 0x7F
        if n == 126: n = struct.unpack(">H", _recv_exact(ws, 2))[0]
        elif n == 127: n = struct.unpack(">Q", _recv_exact(ws, 8))[0]
        mask = _recv_exact(ws, 4) if masked else None
        data = _recv_exact(ws, n) if n else b""
        if mask: data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if op == 0x9:
            pm = os.urandom(4); h = bytearray([0x8A, 0x80|len(data)])
            h += pm; ws.sendall(bytes(h)+bytes(b^pm[i%4] for i,b in enumerate(data))); continue
        if op in (0x1, 0x2): return data.decode("utf-8", "replace")

def main():
    url = sys.argv[1]
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

    errors = []
    def scan_events(j):
        m = j.get("method")
        if m == "Runtime.consoleAPICalled" and j.get("params", {}).get("type") == "error":
            args = j["params"].get("args", [])
            txt = " ".join(str(a.get("value", a.get("description", ""))) for a in args)
            errors.append("console.error: " + txt[:300])
        elif m == "Runtime.exceptionThrown":
            det = j["params"].get("exceptionDetails", {})
            txt = (det.get("exception") or {}).get("description") or det.get("text", "exception")
            errors.append("uncaught: " + str(txt)[:300])

    def drain(seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                raw = read_msg(ws, deadline)
            except Exception:
                break
            try: j = json.loads(raw)
            except Exception: continue
            scan_events(j)

    # enable listeners
    send(ws, {"id": 1, "method": "Page.enable"})
    send(ws, {"id": 2, "method": "Runtime.enable"})
    send(ws, {"id": 3, "method": "Console.enable"})
    # reload
    send(ws, {"id": 4, "method": "Network.enable"})
    send(ws, {"id": 5, "method": "Network.setCacheDisabled", "params": {"cacheDisabled": True}})
    send(ws, {"id": 6, "method": "Page.reload", "params": {"ignoreCache": True}})
    # wait for load
    deadline = time.time() + 25
    loaded = False
    while time.time() < deadline:
        try:
            raw = read_msg(ws, deadline)
            try: j = json.loads(raw)
            except Exception: continue
            scan_events(j)
            if j.get("method") == "Page.loadEventFired":
                loaded = True; break
        except Exception: break
    drain(3.0)  # app init settle
    if not loaded:
        print("WARN loadEventFired not observed", file=sys.stderr)

    mid = [100]
    def ev(js, await_p=False):
        mid[0] += 1; i = mid[0]
        send(ws, {"id": i, "method": "Runtime.evaluate",
                  "params": {"expression": js, "returnByValue": True, "awaitPromise": await_p}})
        deadline = time.time() + 30
        while time.time() < deadline:
            try: raw = read_msg(ws, deadline)
            except Exception: break
            try: j = json.loads(raw)
            except Exception: continue
            scan_events(j)
            if j.get("id") == i:
                r = j.get("result", {})
                if "exceptionDetails" in r:
                    det = r["exceptionDetails"]
                    txt = (det.get("exception") or {}).get("description") or det.get("text", "exception")
                    return {"__js_exception": str(txt)[:500]}
                res = r.get("result", {})
                return res.get("value", res)
        return {"__timeout": True}

    report = {}
    # exercise usage page
    report["nav"] = ev("LM.nav.showPage('usage'); LM.nav.currentPage();")
    drain(3.5)
    def g(expr): return ev("var e=document.getElementById(%s); e? e.textContent.trim():'(missing)';" % expr)
    report["sumRangeLabel"] = g("'sumRangeLabel'")
    report["sumHeroLogical"] = g("'sumHeroLogical'")
    report["sumCoverage"] = g("'sumCoverage'")
    # 切到 今天 -> 小时档
    report["switchToday"] = ev("(function(){var s=document.getElementById('usageRange').querySelectorAll('button');s[0].click();return 'clicked';})()")
    drain(3.0)
    report["todayLabel"] = g("'sumRangeLabel'")
    # 点按小时档
    report["hourToggle"] = ev("(function(){var t=document.getElementById('usageTrendRange').querySelectorAll('button');t[1].click();return 'clicked';})()")
    drain(2.5)
    report["trendHint"] = g("'usageTrendHint'")
    report["hourlyEmpty"] = ev("document.getElementById('chartUsageBox').classList.contains('has-empty')")
    # 自定义范围弹层
    report["customOpen"] = ev("(function(){document.getElementById('usageRangeCustom').click();var p=document.getElementById('usageRangePopover');return JSON.stringify({open:!p.hidden,start:document.getElementById('usageRangeStart').value,end:document.getElementById('usageRangeEnd').value,max:document.getElementById('usageRangeEnd').max});})()")
    drain(1.0)
    # 应用自定义
    report["customApply"] = ev("(function(){document.getElementById('usageRangeApply').click();return 'applied';})()")
    drain(3.0)
    report["afterCustomLabel"] = g("'sumRangeLabel'")
    report["afterHeroLogical"] = g("'sumHeroLogical'")
    # 控制台错误（去重保序）
    seen = []
    for e in errors:
        if e not in seen: seen.append(e)
    report["consoleErrors"] = seen[:20]
    print(json.dumps(report, ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
