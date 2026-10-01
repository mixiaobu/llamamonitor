# 测量：浏览器视角 /api/gpu/daily 延迟（GPU 轮询活跃时），多次采样
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
def call(method,params=None,timeout=60):
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
def js(expr,await_p=False,timeout=60):
    r=call("Runtime.evaluate",{"expression":expr,"returnByValue":True,"awaitPromise":await_p},timeout)
    if "exceptionDetails" in r: return {"__exc":str(r["exceptionDetails"])[:300]}
    return r.get("result",{}).get("value",r.get("result"))

# 确保在 gpu 页（轮询活跃）
js("LM.nav.showPage('gpu');1")
time.sleep(2)
for k in range(4):
    t = js("""(async function(){
      var t0=performance.now();
      try { var r=await fetch('/api/gpu/daily?days=1'); var t=await r.text();
        return {ok:true, ms:Math.round(performance.now()-t0), status:r.status, len:t.length};
      } catch(e){ return {ok:false, ms:Math.round(performance.now()-t0), err:String(e)}; }
    })()""", await_p=True)
    print("attempt%d:" % k, json.dumps(t, ensure_ascii=False))
    time.sleep(1)
ws.close()
