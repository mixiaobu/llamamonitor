#!/usr/bin/env python3
"""Round-7 恢复本页默认验证：fresh 导航 -> 等表单 -> 弄脏 gpu+theme -> 重置 gpu 分类 -> 读回。
用法: cdp_r7_reset.py <ws>
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
        if op == 0x9:
            pm = os.urandom(4); hh = bytearray([0x8A, 0x80 | len(data)]); hh += pm
            ws.sendall(bytes(hh) + bytes(b ^ pm[i % 4] for i, b in enumerate(data)))

JS_DIRTY_AND_RESET = r"""
(function(){
  if (!LM.settings.isLoaded()) return JSON.stringify({err:'not-loaded'});
  function val(id){var e=document.getElementById(id);return e?e.value:null;}
  function chk(id){var e=document.getElementById(id);return e?e.checked:null;}
  var r={};
  r.before={gpuPoll:val('setGpuPoll'),gpuOn:chk('setGpuEnabled'),theme:val('setTheme'),poll:val('setPollInterval')};
  LM.settings.goToSection('gpu');
  var gp=document.getElementById('setGpuPoll');gp.value='99';gp.dispatchEvent(new Event('input',{bubbles:true}));
  var ge=document.getElementById('setGpuEnabled');ge.checked=false;ge.dispatchEvent(new Event('change',{bubbles:true}));
  var th=document.getElementById('setTheme');th.value='dark';th.dispatchEvent(new Event('change',{bubbles:true}));
  r.dirty={gpuPoll:val('setGpuPoll'),gpuOn:chk('setGpuEnabled'),theme:val('setTheme'),dirty:LM.settings.isDirty()};
  window.__rst={s:'pending'};
  LM.settings.resetToDefaults().then(function(){window.__rst={s:'ok'};},function(e){window.__rst={s:'rej',m:String((e&&e.message)||e)};});
  return JSON.stringify(r);
})()
"""
JS_READBACK = r"""
(function(){
  function val(id){var e=document.getElementById(id);return e?e.value:null;}
  function chk(id){var e=document.getElementById(id);return e?e.checked:null;}
  return JSON.stringify({
    rst: window.__rst,
    gpuPoll: val('setGpuPoll'),   // 应回到默认 5
    gpuOn: chk('setGpuEnabled'),   // 应回到默认 true
    theme: val('setTheme'),       // 其他分类（appearance）应保持 dirty 的 dark
    poll: val('setPollInterval'), // 其他分类（collector）应保持 saved
    dirty: LM.settings.isDirty()
  });
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
    def call(method, params=None, timeout=25):
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
                return r.get("value", r)
        return None
    call("Runtime.enable"); call("Page.enable")
    call("Emulation.setDeviceMetricsOverride", {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})
    call("Page.navigate", {"url": "http://127.0.0.1:8790/"})
    time.sleep(3.0)
    call("Runtime.evaluate", {"expression": "LM.nav.showPage('settings');1", "returnByValue": True})
    loaded = False
    for _ in range(15):
        time.sleep(1.0)
        v = call("Runtime.evaluate", {"expression": "JSON.stringify({loaded:LM.settings.isLoaded()})", "returnByValue": True})
        if v and '"loaded":true' in str(v):
            loaded = True
            break
    if not loaded:
        print(json.dumps({"loaded": False}))
        return
    # dirty + reset
    r1 = call("Runtime.evaluate", {"expression": JS_DIRTY_AND_RESET, "returnByValue": True})
    time.sleep(2.5)
    r2 = call("Runtime.evaluate", {"expression": JS_READBACK, "returnByValue": True})
    print(json.dumps({"loaded": loaded, "dirty": r1, "readback": r2}, ensure_ascii=False))

if __name__ == "__main__":
    main()
