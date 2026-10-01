# 诊断 2：不重载（用已加载页），检查 energy 为何 0 + 手动触发 render
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
_mid=[0]
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
        if op in (0x1,0x2): return data.decode("utf8","replace")
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
    return {"_timeout":True}
def js(expr,await_p=False):
    r=call("Runtime.evaluate",{"expression":expr,"returnByValue":True,"awaitPromise":await_p},30)
    if "exceptionDetails" in r:
        return {"__exc": str(r["exceptionDetails"])[:500]}
    return r.get("result",{}).get("value",r.get("result"))

# 不重载；确保在 gpu 页
js("LM.nav.showPage('gpu');1")
time.sleep(3)
print("energyRows now:", js("document.querySelectorAll('#gpuEnergy .energy-row').length"))
# in-page 手动 fetch daily 并打印结构（不 await 整个，直接同步轮询不行；用同步 XHR 代替）
sync = js("""(function(){
  try {
    var x = new XMLHttpRequest();
    x.open('GET','/api/gpu/daily?days=1',false); x.send();
    var j = JSON.parse(x.responseText);
    return {status:x.status, gpus:(j.gpus||[]).length,
      names:(j.gpus||[]).map(function(g){return g.name+'|pa='+g.power_available+'|todayDays='+((g.days||[]).filter(function(d){return d.date===new Date().toISOString().slice(0,10);}).length);}),
      today:new Date().toISOString().slice(0,10)};
  } catch(e){ return {err:String(e)}; }
})()""")
print("SYNC daily:", json.dumps(sync, ensure_ascii=False))
ws.close()
