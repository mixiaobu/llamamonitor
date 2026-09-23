# -*- coding: utf-8 -*-
"""通用 CDP UI 探测（1.0.0 Final Gate：主题 / DPI / 分辨率 / 布局 / 轮询 / 泄漏）。

用法：
  python tools/cdp_ui_probe.py <ws_url> <url> <width> <height> <dsf> <colorScheme> <probeJsFile> <settle_s>

colorScheme: dark | light | no（不覆盖 prefers-color-scheme）
在**导航之前**先 Emulation.setDeviceMetricsOverride + setEmulatedMedia，
使首帧即按模拟的色偏好/分辨率/DPI 渲染（规避 CDP 不派发已有 MQ change 的限制）。
采集：JS 探测结果（returnByValue）+ console 消息 + 非 2xx 响应。
"""
import sys, socket, struct, base64, os, json, time

def send(ws, obj):
    data = json.dumps(obj).encode("utf-8")
    header = bytearray([0x81])
    n = len(data)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126); header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127); header += struct.pack(">Q", n)
    mask = os.urandom(4); header += mask
    ws.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

def _recv_exact(ws, n):
    buf = b""
    while len(buf) < n:
        chunk = ws.recv(n - len(buf))
        if not chunk: raise RuntimeError("ws closed")
        buf += chunk
    return buf

def read_msg(ws):
    while True:
        b0, b1 = _recv_exact(ws, 2)
        opcode = b0 & 0x0F
        if opcode == 0x8: raise TimeoutError("ws closed")
        masked = b1 & 0x80
        n = b1 & 0x7F
        if n == 126: n = struct.unpack(">H", _recv_exact(ws, 2))[0]
        elif n == 127: n = struct.unpack(">Q", _recv_exact(ws, 8))[0]
        mask = _recv_exact(ws, 4) if masked else None
        data = _recv_exact(ws, n) if n else b""
        if mask: data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if opcode in (0x1, 0x2): return data.decode("utf-8", "replace")
        if opcode == 0x9:
            pm = os.urandom(4); h = bytearray([0x8A, 0x80 | len(data)]); h += pm
            ws.sendall(bytes(h) + bytes(b ^ pm[i % 4] for i, b in enumerate(data)))

def main():
    (ws_url, url, w, h, dsf, scheme, jsfile, settle) = (
        sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]),
        float(sys.argv[5]), sys.argv[6], sys.argv[7], float(sys.argv[8]) if len(sys.argv) > 8 else 8.0)
    rest = ws_url[len("ws://"):]; host, _, path = rest.partition("/")
    host, _, port = host.partition(":")
    ws = socket.create_connection((host, int(port or 80)))
    key = base64.b64encode(os.urandom(16)).decode()
    ws.sendall((
        f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        resp += ws.recv(4096)

    mid = [0]; events = []
    def call(method, params=None, timeout=30):
        mid[0] += 1
        send(ws, {"id": mid[0], "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            ws.settimeout(2.0); raw = read_msg(ws)
            try: j = json.loads(raw)
            except Exception: continue
            if "method" in j: events.append(j); continue
            if j.get("id") == mid[0]: return j.get("error") or j.get("result", {})
        return {"_timeout": True}

    # 导航前：分辨率 + DPI + 色偏好（首帧即按模拟值渲染）
    call("Emulation.setDeviceMetricsOverride",
         {"width": w, "height": h, "deviceScaleFactor": dsf, "mobile": False})
    if scheme != "no":
        call("Emulation.setEmulatedMedia",
             {"features": [{"name": "prefers-color-scheme", "value": scheme}]})
    call("Page.enable"); call("Runtime.enable"); call("Network.enable")
    call("Page.navigate", {"url": url})
    time.sleep(settle)

    ws.settimeout(2.0)
    try:
        while True:
            raw = read_msg(ws); j = json.loads(raw)
            if "method" in j: events.append(j)
    except Exception:
        pass

    console = []; badhttp = []
    for ev in events:
        m = ev.get("method"); p = ev.get("params", {})
        if m == "Runtime.consoleAPICalled":
            args = p.get("args", [])
            txt = " ".join(str(a.get("value", a.get("description", ""))) for a in args)
            console.append((p.get("type"), txt[:200]))
        elif m == "Network.responseReceived":
            r = p.get("response", {})
            st = r.get("status")
            if st is not None and st >= 400:
                badhttp.append({"status": st, "url": r.get("url")})

    js = open(jsfile, encoding="utf-8").read()
    r = call("Runtime.evaluate", {"expression": js, "returnByValue": True,
                                  "awaitPromise": True}, timeout=90)
    ui = r.get("result", {}).get("value", json.dumps(r))
    if isinstance(ui, str) and ui.startswith("{"):
        try: ui = json.loads(ui)
        except Exception: pass
    out = {"scenario": os.environ.get("LM_UI_SCENARIO", "?"),
           "ui": ui, "bad_http": badhttp, "console": console}
    print(json.dumps(out, ensure_ascii=False))

main()
