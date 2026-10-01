#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys, socket, struct, base64, os, json, time
exec(compile(open("tools/cdp_m_waitprobe.py", encoding="utf-8").read().split("def main")[0], "wp", "exec"))
ws_url = sys.argv[1]
rest = ws_url[len("ws://"):]; host, _, path = rest.partition("/")
host, _, port = host.partition(":")
ws = socket.create_connection((host, int(port or 80)))
key = base64.b64encode(os.urandom(16)).decode()
ws.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
resp = b""
while b"\r\n\r\n" not in resp:
    ws.settimeout(5); resp += ws.recv(4096)
mid = [0]
def sendc(o):
    mid[0] += 1
    send(ws, {"id": mid[0], "method": o[0], "params": o[1] or {}})
def waitraw(timeout=15):
    end = time.time() + timeout
    ws.settimeout(2.0)
    while time.time() < end:
        try: raw = read_msg(ws)
        except socket.timeout: continue
        except Exception as e: return {"_exc": str(e)}
        try: j = json.loads(raw)
        except Exception: continue
        if j.get("id") == mid[0]: return j
    return {"_timeout": True}
sendc(("Page.enable", None)); waitraw(5)
sendc(("Emulation.setDeviceMetricsOverride", {"width":390,"height":844,"deviceScaleFactor":2,"mobile":True})); waitraw(5)
sendc(("Page.navigate", {"url":"http://127.0.0.1:8790/"})); waitraw(10)
time.sleep(4.0)
mid[0] += 1
probe = open(sys.argv[2], encoding="utf-8").read()
send(ws, {"id": mid[0], "method": "Runtime.evaluate",
          "params": {"expression": probe, "returnByValue": True, "awaitPromise": True}})
r = waitraw(45)
print(json.dumps(r, ensure_ascii=False)[:1500])
