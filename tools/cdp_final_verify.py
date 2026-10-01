# 真实路径：点 sidebar button[data-page=history] -> 等数据 -> 趋势点击筛选 -> 再点取消。
import sys, socket, struct, base64, os, json, time
WS=open('tools/_ws.txt').read().strip()
def _connect(url):
    rest=url[len("ws://"):]; host,_,path=rest.partition("/"); host,_,port=host.partition(":")
    w=socket.create_connection((host,int(port or 80))); key=base64.b64encode(os.urandom(16)).decode()
    w.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    resp=b""
    while b"\r\n\r\n" not in resp:
        c=w.recv(4096)
        if not c: raise RuntimeError("hf")
        resp+=c
    return w
WS_=_connect(WS); _mid=[0]
def _rx(w,n):
    buf=b""
    while len(buf)<n:
        c=w.recv(n-len(buf))
        if not c: raise RuntimeError("cl")
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
def call(method,params=None,timeout=25):
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
def js(expr,timeout=25,await_p=False):
    r=call("Runtime.evaluate",{"expression":expr,"returnByValue":True,"awaitPromise":await_p},timeout)
    if "exceptionDetails" in r: return {"__exc":str(r["exceptionDetails"])[:300]}
    return r.get("result",{}).get("value",r.get("result"))
def cdp_click(x,y):
    call("Input.dispatchMouseEvent",{"type":"mouseMoved","x":x,"y":y},10); time.sleep(0.08)
    call("Input.dispatchMouseEvent",{"type":"mousePressed","x":x,"y":y,"button":"left","clickCount":1},10); time.sleep(0.08)
    call("Input.dispatchMouseEvent",{"type":"mouseReleased","x":x,"y":y,"button":"left","clickCount":1},10)
def cnt(): return js("(function(){var c=document.getElementById('gapsCountLabel');return c?c.textContent:'';})()")
def active(): return '取消' in str(cnt())
call("Page.enable",{},10); call("Runtime.enable",{},10)
call("Page.navigate",{"url":"http://127.0.0.1:8790/"},30); time.sleep(5)
# 点真实 sidebar 按钮
print("click sidebar:", js("""(function(){var b=document.querySelector('.nav-item[data-page="history"]');if(b){b.click();return 'ok';}return 'not found';})()"""))
# 等 gaps + trend 就绪
ready=False
for _ in range(20):
    time.sleep(2)
    g=js("document.querySelectorAll('#gapsTbody tr.gap-row').length")
    n=js("(function(){var c=LM.charts.chart('chartHistoryTrend');try{var d=c.getOption().series[0].data;return d?d.length:0;}catch(e){return 0;}})()")
    if g and n: ready=True; break
print("ready:", ready, "gaps=", g, "trendPts=", n)
if not ready:
    print("NOT READY"); WS_.close(); sys.exit(0)
print("baseline cnt:", json.dumps(cnt(),ensure_ascii=False))
# 点 idx0
def pt0():
    js("document.getElementById('chartHistoryTrend').scrollIntoView({block:'center'});1"); time.sleep(1)
    return js("""(function(){try{var c=LM.charts.chart('chartHistoryTrend');var d=c.getOption().series[0].data;var v=d[0];var p=c.convertToPixel({seriesIndex:0},[0,v]);var cv=document.querySelector('#chartHistoryTrend canvas');var r=cv.getBoundingClientRect();return {x:r.left+p[0],y:r.top+p[1]};}catch(e){return null;}})()""")
for attempt in range(6):
    p=pt0()
    if p and p.get('x') is not None:
        cdp_click(p['x'],p['y'])
    time.sleep(3)
    if active(): break
print("after click idx0:", "ACTIVE" if active() else "not active", "cnt:", json.dumps(cnt(),ensure_ascii=False))
# 取消
for attempt in range(6):
    p=pt0()
    if p and p.get('x') is not None:
        cdp_click(p['x'],p['y'])
    time.sleep(3)
    if not active(): break
print("after cancel:", "ACTIVE" if active() else "cancelled", "cnt:", json.dumps(cnt(),ensure_ascii=False))
WS_.close()
