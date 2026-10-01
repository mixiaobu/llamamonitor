#!/usr/bin/env python3
"""CDP: clean reload -> usage -> for each range segment, click it, wait, then
report which button is pressed (aria-pressed) + the sumRangeLabel + hero.
This separates 'click not registering' from 'label not re-rendering'.
Usage: cdp_mode_click.py <ws_url>"""
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
    msgs = []
    def scan(j):
        m = j.get("method")
        if m == "Runtime.consoleAPICalled":
            t = j.get("params", {}).get("type")
            txt = " ".join(str(a.get("value","")) for a in j["params"].get("args", []))
            if t == "log" and txt.startswith("DBG"):
                msgs.append("log: " + txt[:200])
            elif t in ("error", "warning"):
                msgs.append(t + ": " + txt[:200])
        elif m == "Runtime.exceptionThrown":
            det = j["params"].get("exceptionDetails", {})
            msgs.append("uncaught: " + str((det.get("exception") or {}).get("description") or det.get("text",""))[:250])
    def drain(s):
        d = time.time() + s
        while time.time() < d:
            try: raw = read_msg(ws, d)
            except Exception: break
            try: j = json.loads(raw)
            except Exception: continue
            scan(j)
    def ev(js):
        i = 700 + int(time.time()*1000) % 100000
        send(ws, {"id": i, "method": "Runtime.evaluate", "params": {"expression": js, "returnByValue": True}})
        d = time.time() + 15
        while time.time() < d:
            try: raw = read_msg(ws, d)
            except Exception: break
            try: j = json.loads(raw)
            except Exception: continue
            scan(j)
            if j.get("id") == i:
                r = j.get("result", {})
                if "exceptionDetails" in r:
                    det = r["exceptionDetails"]; return {"__exc": str((det.get("exception") or {}).get("description") or det.get("text",""))[:200]}
                return r.get("result", {}).get("value", {})
        return {"__timeout": True}
    def snap():
        return ev("(function(){var b=document.getElementById('usageRange').querySelectorAll('button');var pr=[];for(var i=0;i<b.length;i++){if(b[i].getAttribute('aria-pressed')==='true')pr.push(b[i].textContent);}var g=function(id){var e=document.getElementById(id);return e?e.textContent.trim():'';};return JSON.stringify({pressed:pr.join(',')||'?',label:g('sumRangeLabel'),hero:g('sumHeroLogical')});})()")

    send(ws, {"id": 1, "method": "Runtime.enable"})
    send(ws, {"id": 2, "method": "Page.enable"})
    send(ws, {"id": 5, "method": "Page.reload"})
    drain(6.0)  # full init
    ev("LM.nav.showPage('usage'); 'x'")
    drain(3.0)  # let 7d default populate
    print("initial: " + str(snap()))
    names = ["今天","7 天","30 天","本月","全部"]
    for idx in [0, 2, 4]:  # today, 30d, all
        ev("(function(){var b=document.getElementById('usageRange').querySelectorAll('button');b[%d].click();return 'x';})()" % idx)
        drain(3.5)
        print("click %r -> " % names[idx] + str(snap()))
    print("console:", json.dumps(list(dict.fromkeys(msgs))[:15], ensure_ascii=False))

if __name__ == "__main__":
    main()
