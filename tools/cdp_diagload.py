# 诊断：fresh navigate -> 装 console error 监听 + 强制 visibility -> 等 -> 报告错误 + 数据状态。
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
WS_=_connect(WS); _mid=[0]
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
def call(method,params=None,timeout=20):
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
        raw=read_msg(WS_,max(0.3,deadline-time.time()))
        try: j=json.loads(raw)
        except Exception: continue
        if j.get("id")==i: return j.get("error") if "error" in j else j.get("result",{})
    return {"_timeout":True}
def js(expr,timeout=20):
    r=call("Runtime.enable",{},10) if False else None
    r=call("Runtime.evaluate",{"expression":expr,"returnByValue":True,"awaitPromise":False},timeout)
    if "exceptionDetails" in r: return {"__exc":str(r["exceptionDetails"])[:200]}
    return r.get("result",{}).get("value",r.get("result"))
call("Page.enable",{},10); call("Runtime.enable",{},10); call("Log.enable",{},10)
# 收集 console
call("Runtime.addBinding",{"name":"__capErr"},10)
call("Page.navigate",{"url":"http://127.0.0.1:8790/"},30)
time.sleep(6)
# 强制 visibility（轮询门需要）
js("Object.defineProperty(document,'visibilityState',{value:'visible',configurable:true});Object.defineProperty(document,'hidden',{value:false,configurable:true});document.dispatchEvent(new Event('visibilitychange'));LM.nav.showPage('history');1")
# 监听后续 console.error
js("window.__ce=[];var oe=console.error;console.error=function(){window.__ce.push(Array.prototype.map.call(arguments,function(a){return String(a).slice(0,120);}).join(' '));oe.apply(console,arguments);};1")
for _ in range(8):
    time.sleep(2)
    g=js("document.querySelectorAll('#gapsTbody tr.gap-row').length")
    n=js("(function(){var c=LM.charts.chart('chartHistoryTrend');try{var d=c.getOption().series[0].data;return d?d.length:0;}catch(e){return 0;}})()")
    print("t+%ds gaps=%s trendPts=%s" % (_*2, g, n))
    if g and n: break
print("console.errors:", json.dumps(js("window.__ce"),ensure_ascii=False))
print("final gaps=", js("document.querySelectorAll('#gapsTbody tr.gap-row').length"), "cnt=", json.dumps(js("(function(){var c=document.getElementById('gapsCountLabel');return c?c.textContent:'';})()"),ensure_ascii=False))
WS_.close()
