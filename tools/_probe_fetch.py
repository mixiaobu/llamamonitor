# 单连接：装 fetch 计数器 -> 进 history -> 30s 采样 fetches + DOM。
import sys, socket, struct, base64, os, json, time
ws=open('tools/_ws.txt').read().strip()
def _connect(url):
    rest=url[len("ws://"):]; host,_,path=rest.partition("/"); host,_,port=host.partition(":")
    w=socket.create_connection((host,int(port or 80))); key=base64.b64encode(os.urandom(16)).decode()
    w.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    resp=b""
    while b"\r\n\r\n" not in resp:
        c=w.recv(4096)
        if not c: raise RuntimeError("handshake failed")
        resp+=c
    return w
WS=_connect(ws); _mid=[0]; console=[]
def _rx(w,n):
    buf=b""
    while len(buf)<n:
        c=w.recv(n-len(buf))
        if not c: raise RuntimeError("closed")
        buf+=c
    return buf
def read_msg(w,timeout):
    w.settimeout(timeout)
    while True:
        b0,b1=_rx(w,2); op=b0&0x0F; masked=b1&0x80; n=b1&0x7F
        if n==126: n=struct.unpack(">H",_rx(w,2))[0]
        elif n==127: n=struct.unpack(">Q",_rx(w,8))[0]
        mask=_rx(w,4) if masked else None
        data=_rx(w,n) if n else b""
        if mask: data=bytes(b^mask[i%4] for i,b in enumerate(data))
        if op==0x9:
            pm=os.urandom(4); h=bytearray([0x8A,0x80|len(data)]); h+=pm
            w.sendall(bytes(h)+bytes(b^pm[i%4] for i,b in enumerate(data))); continue
        if op in (0x1,0x2): return data.decode("utf8","replace")
def call(method,params=None,timeout=30):
    _mid[0]+=1; i=_mid[0]
    data=json.dumps({"id":i,"method":method,"params":params or {}}).encode()
    h=bytearray([0x81]); n=len(data)
    if n<126: h.append(0x80|n)
    elif n<65536: h.append(0x80|126); h+=struct.pack(">H",n)
    else: h.append(0x80|127); h+=struct.pack(">Q",n)
    mask=os.urandom(4); h+=mask
    WS.sendall(bytes(h)+bytes(b^mask[i%4] for i,b in enumerate(data)))
    deadline=time.time()+timeout
    while time.time()<deadline:
        raw=read_msg(WS,max(0.5,deadline-time.time()))
        try: j=json.loads(raw)
        except Exception: continue
        if j.get("id")==i: return j.get("error") if "error" in j else j.get("result",{})
        m=j.get("method"); p=j.get("params",{})
        if m=="Runtime.consoleAPICalled":
            args=[a.get("value",a.get("description")) for a in p.get("args",[])]
            console.append((p.get("type"),(" ".join(str(x) for x in args))[:140]))
        elif m=="Runtime.exceptionThrown":
            det=p.get("exceptionDetails",{})
            console.append(("EXC",str((det.get("exception") or {}).get("description",det.get("text")))[:140]))
    return {"_timeout":True}
def js(expr,await_p=False,timeout=30):
    r=call("Runtime.evaluate",{"expression":expr,"returnByValue":True,"awaitPromise":await_p},timeout)
    if "exceptionDetails" in r: return {"__exc":str(r["exceptionDetails"])[:200]}
    return r.get("result",{}).get("value",r.get("result"))
call("Page.enable",{},10); call("Runtime.enable",{},10)
call("Page.navigate",{"url":"http://127.0.0.1:8790/"},30)
time.sleep(4)
# 装 fetch 计数
js("(function(){ if(window.__hnet) return 'already'; window.__hnet={}; var o=window.fetch; window.fetch=function(u){ try{ var p=String(u); if(p.indexOf('/api/')>-1){ window.__hnet[p.slice(0,70)]=(window.__hnet[p.slice(0,70)]||0)+1; } }catch(e){} return o.apply(this,arguments); }; return 'ok'; })()")
js("LM.nav.showPage('history');1")
time.sleep(3)
print("fetches after nav:")
print(json.dumps(js("JSON.stringify(window.__hnet||{})"), ensure_ascii=False))
time.sleep(12)
print("\n+12s fetches:")
print(json.dumps(js("JSON.stringify(window.__hnet||{})"), ensure_ascii=False))
print("gaps=", js("document.querySelectorAll('#gapsTbody tr.gap-row').length"),
      "events=", js("document.querySelectorAll('#eventsTbody tr.ev-row').length"))
errs=[c for c in console if c[0] in ("error","EXC")]
print("\nCONSOLE err=%d" % len(errs))
for c in errs[:8]: print("  [%s] %s" % c)
WS.close()
