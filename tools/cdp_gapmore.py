# 缺口"显示更多"诊断：点击后每秒采样 rows 8s，判断是追加(→40)、保持还是被轮询重置(→20)。
import sys, socket, struct, base64, os, json, time
WS=open('tools/_ws.txt').read().strip()
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
WS_=_connect(WS); _mid=[0]; console=[]
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
    WS_.sendall(bytes(h)+bytes(b^mask[i%4] for i,b in enumerate(data)))
    deadline=time.time()+timeout
    while time.time()<deadline:
        raw=read_msg(WS_,max(0.5,deadline-time.time()))
        try: j=json.loads(raw)
        except Exception: continue
        if j.get("id")==i: return j.get("error") if "error" in j else j.get("result",{})
        m=j.get("method"); p=j.get("params",{})
        if m=="Runtime.consoleAPICalled":
            args=[a.get("value",a.get("description")) for a in p.get("args",[])]
            console.append((p.get("type"),(" ".join(str(x) for x in args))[:140]))
    return {"_timeout":True}
def js(expr,await_p=False,timeout=30):
    r=call("Runtime.evaluate",{"expression":expr,"returnByValue":True,"awaitPromise":await_p},timeout)
    if "exceptionDetails" in r: return {"__exc":str(r["exceptionDetails"])[:200]}
    return r.get("result",{}).get("value",r.get("result"))
call("Page.enable",{},10); call("Runtime.enable",{},10)
call("Emulation.setDeviceMetricsOverride",{"width":1920,"height":1080,"deviceScaleFactor":1,"mobile":False},15)
call("Page.navigate",{"url":"http://127.0.0.1:8790/"},30)
time.sleep(5)
js("Object.defineProperty(document,'visibilityState',{value:'visible',configurable:true});"
   "if(document.hidden){Object.defineProperty(document,'hidden',{value:false,configurable:true});}"
   "document.dispatchEvent(new Event('visibilitychange'));1")
js("LM.nav.showPage('history');1")
for _ in range(10):
    time.sleep(2)
    if js("document.querySelectorAll('#gapsTbody tr.gap-row').length"): break
# 读更按钮状态 + 捕获 gaps fetch 参数
js("(function(){ if(window.__gf) return 'ok'; window.__gf={}; var o=window.fetch; window.fetch=function(u){try{var p=String(u);if(p.indexOf('/api/history/gaps')>-1){window.__gf[p.slice(0,90)]=(window.__gf[p.slice(0,90)]||0)+1;}}catch(e){}return o.apply(this,arguments);};return 'ok';})()")
print("more btn:", js("(function(){var b=document.getElementById('gapsMoreBtn');var r=document.getElementById('gapsMoreRow');return b?(r.hidden?'HIDDEN':b.textContent):'NO';})()"))
print("before rows:", js("document.querySelectorAll('#gapsTbody tr.gap-row').length"))
# 点击
js("(function(){var b=document.getElementById('gapsMoreBtn');if(b)b.onclick&&b.onclick();})()")
for i in range(8):
    time.sleep(1)
    print("  +%ds rows=%d" % (i+1, js("document.querySelectorAll('#gapsTbody tr.gap-row').length")))
print("gaps fetches:", json.dumps(js("window.__gf"), ensure_ascii=False))
# 再点一次
js("(function(){var b=document.getElementById('gapsMoreBtn');if(b)b.onclick&&b.onclick();})()")
time.sleep(2)
print("after 2nd click rows:", js("document.querySelectorAll('#gapsTbody tr.gap-row').length"))
print("gaps fetches now:", json.dumps(js("window.__gf"), ensure_ascii=False))
WS_.close()
