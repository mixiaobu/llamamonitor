# 全页 console sweep：逐个 nav 到各页，等 2.5s，汇总 uncaught/exception + 页面渲染标志。
import sys, socket, struct, base64, os, json, time
WS = sys.argv[1]
def _connect(url):
    rest=url[len("ws://"):]; host,_,path=rest.partition("/"); host,_,port=host.partition(":")
    ws=socket.create_connection((host,int(port or 80))); key=base64.b64encode(os.urandom(16)).decode()
    ws.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    resp=b""
    while b"\r\n\r\n" not in resp:
        c=ws.recv(4096)
        if not c: raise RuntimeError("handshake failed")
        resp+=c
    return ws
ws=_connect(WS); _mid=[0]; console=[]
def _rx(ws,n):
    buf=b""
    while len(buf)<n:
        c=ws.recv(n-len(buf))
        if not c: raise RuntimeError("closed")
        buf+=c
    return buf
def read_msg(ws,timeout):
    ws.settimeout(timeout)
    while True:
        b0,b1=_rx(ws,2); op=b0&0x0F; masked=b1&0x80; n=b1&0x7F
        if n==126: n=struct.unpack(">H",_rx(ws,2))[0]
        elif n==127: n=struct.unpack(">Q",_rx(ws,8))[0]
        mask=_rx(ws,4) if masked else None
        data=_rx(ws,n) if n else b""
        if mask: data=bytes(b^mask[i%4] for i,b in enumerate(data))
        if op==0x9:
            pm=os.urandom(4); h=bytearray([0x8A,0x80|len(data)]); h+=pm
            ws.sendall(bytes(h)+bytes(b^pm[i%4] for i,b in enumerate(data))); continue
        if op in (0x1,0x2): return data.decode("utf8","replace")
def call(method,params=None,timeout=30):
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
        m=j.get("method"); p=j.get("params",{})
        if m=="Runtime.consoleAPICalled":
            args=[a.get("value",a.get("description")) for a in p.get("args",[])]
            console.append((p.get("type"),(" ".join(str(x) for x in args))[:150]))
        elif m=="Runtime.exceptionThrown":
            det=p.get("exceptionDetails",{})
            console.append(("EXC",str((det.get("exception") or {}).get("description",det.get("text")))[:150]))
    return {"_timeout":True}
def js(expr,await_p=False,timeout=30):
    r=call("Runtime.evaluate",{"expression":expr,"returnByValue":True,"awaitPromise":await_p},timeout)
    if "exceptionDetails" in r: return {"__exc":str(r["exceptionDetails"])[:200]}
    return r.get("result",{}).get("value",r.get("result"))

call("Page.enable",{},10); call("Runtime.enable",{},10)
call("Emulation.setDeviceMetricsOverride",{"width":1600,"height":1000,"deviceScaleFactor":1,"mobile":False},15)
call("Page.navigate",{"url":"http://127.0.0.1:8790/"},30)
time.sleep(4)
js("Object.defineProperty(document,'visibilityState',{value:'visible',configurable:true});"
   "if(document.hidden){Object.defineProperty(document,'hidden',{value:false,configurable:true});}"
   "document.dispatchEvent(new Event('visibilitychange'));1")
time.sleep(1)
for page in ["overview","usage","perf","system","gpu","history","settings","about"]:
    js("LM.nav.showPage('%s');1" % page)
    time.sleep(2.5)
    # 渲染标志：当前激活页的 section 是否显示
    vis = js("(function(){var s=document.getElementById('page-%s');return s?getComputedStyle(s).display:'missing';})()" % page)
    print("%-10s display=%s" % (page, vis))
call("Emulation.clearDeviceMetricsOverride",{},10)
errs=[c for c in console if c[0] in ("error","EXC")]
warns=[c for c in console if c[0]=="warning"]
print("\nCONSOLE total=%d errors=%d warnings=%d" % (len(console),len(errs),len(warns)))
for c in errs: print("  ERR  [%s] %s" % c)
for c in warns[:12]: print("  WARN [%s] %s" % c)
ws.close()
