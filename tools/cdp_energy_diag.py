# 诊断：GPU 页首载 daily 能耗为何 0（控制台 + 直接 fetch + 手动 render）
import sys, socket, struct, base64, os, json, time

WS = sys.argv[1]
def _connect(url):
    rest = url[len("ws://"):]
    host, _, path = rest.partition("/")
    host, _, port = host.partition(":")
    ws = socket.create_connection((host, int(port or 80)))
    key = base64.b64encode(os.urandom(16)).decode()
    ws.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        c = ws.recv(4096)
        if not c: raise RuntimeError("handshake failed")
        resp += c
    return ws

ws = _connect(WS)
_mid=[0]; console=[]; lock_ok=True
def _recv_exact(ws,n):
    buf=b""
    while len(buf)<n:
        c=ws.recv(n-len(buf))
        if not c: raise RuntimeError("closed")
        buf+=c
    return buf
def read_msg(ws,timeout):
    ws.settimeout(timeout)
    while True:
        b0,b1=_recv_exact(ws,2)
        op=b0&0x0F; masked=b1&0x80; n=b1&0x7F
        if n==126: n=struct.unpack(">H",_recv_exact(ws,2))[0]
        elif n==127: n=struct.unpack(">Q",_recv_exact(ws,8))[0]
        mask=_recv_exact(ws,4) if masked else None
        data=_recv_exact(ws,n) if n else b""
        if mask: data=bytes(b^mask[i%4] for i,b in enumerate(data))
        if op==0x9:
            pm=os.urandom(4); h=bytearray([0x8A,0x80|len(data)]); h+=pm
            ws.sendall(bytes(h)+bytes(b^pm[i%4] for i,b in enumerate(data))); continue
        if op in (0x1,0x2):
            return data.decode("utf8","replace")
def call(method,params=None,timeout=45):
    _mid[0]+=1; i=_mid[0]
    data=json.dumps({"id":i,"method":method,"params":params or {}}).encode()
    h=bytearray([0x81]); n=len(data)
    if n<126: h.append(0x80|n)
    elif n<65536: h.append(0x80|126); h+=struct.pack(">H",n)
    else: h.append(0x80|127); h+=struct.pack(">Q",n)
    mask=os.urandom(4); h+=mask
    ws.sendall(bytes(h)+bytes(b^mask[i%4] for i,b in enumerate(data)))
    deadline=time.time()+timeout
    while time.time()<deadline:
        raw=read_msg(ws,max(0.5,deadline-time.time()))
        try: j=json.loads(raw)
        except Exception: continue
        if j.get("id")==i: return j.get("error") if "error" in j else j.get("result",{})
        # 事件
        m=j.get("method")
        if m=="Runtime.consoleAPICalled":
            args=[a.get("value",a.get("description")) for a in j.get("params",{}).get("args",[])]
            console.append(" ".join(str(x) for x in args)[:300])
        elif m=="Runtime.exceptionThrown":
            det=j.get("params",{}).get("exceptionDetails",{})
            console.append("EXC "+str((det.get("exception") or {}).get("description",det.get("text")))[:300])
    return {"_timeout":True}
def js(expr,await_p=False):
    r=call("Runtime.evaluate",{"expression":expr,"returnByValue":True,"awaitPromise":await_p},30)
    return r.get("result",{}).get("value",r.get("result"))

call("Page.enable",{},10)
call("Runtime.enable",{},10)
call("Emulation.setDeviceMetricsOverride",{"width":1920,"height":1080,"deviceScaleFactor":1,"mobile":False},15)
call("Page.navigate",{"url":"http://127.0.0.1:8790/"},30)
time.sleep(5)
console.clear()
js("LM.nav.showPage('gpu');1")
time.sleep(5)
# 直接 in-page fetch daily，看返回结构
direct = js("""(async function(){
  try {
    var r = await fetch('/api/gpu/daily?days=1');
    var t = await r.text();
    var j = JSON.parse(t);
    return {status:r.status, gpus:(j.gpus||[]).length,
      names:(j.gpus||[]).map(function(g){return g.name+'|pa='+g.power_available+'|days='+((g.days||[]).length);}),
      bodyLen:t.length};
  } catch(e) { return {err: String(e)}; }
})()""", await_p=True)
print("DIRECT FETCH:", json.dumps(direct, ensure_ascii=False))
energy = js("document.querySelectorAll('#gpuEnergy .energy-row').length")
print("DOM energyRows after 5s:", energy)
print("=== CONSOLE (last 15) ===")
for line in console[-15:]:
    print("  ", line)
ws.close()
