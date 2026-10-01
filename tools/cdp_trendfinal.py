# 趋势点击 最终验证：等趋势 canvas 就绪 -> 点 idx0（最可靠命中）-> 读 -> 重算再点 idx0 取消 -> 读。
# 每个阶段带 retry（合成点击偶发不触发，重试即可）。
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
    r=call("Runtime.evaluate",{"expression":expr,"returnByValue":True,"awaitPromise":False},timeout)
    if "exceptionDetails" in r: return {"__exc":str(r["exceptionDetails"])[:140]}
    return r.get("result",{}).get("value",r.get("result"))
def cdp_click(x,y):
    call("Input.dispatchMouseEvent",{"type":"mouseMoved","x":x,"y":y},10); time.sleep(0.08)
    call("Input.dispatchMouseEvent",{"type":"mousePressed","x":x,"y":y,"button":"left","clickCount":1},10); time.sleep(0.08)
    call("Input.dispatchMouseEvent",{"type":"mouseReleased","x":x,"y":y,"button":"left","clickCount":1},10)
def cnt(): return js("(function(){var c=document.getElementById('gapsCountLabel');return c?c.textContent:'';})()")
def active(): return '取消' in str(cnt())
def pt0():
    js("document.getElementById('chartHistoryTrend').scrollIntoView({block:'center'});1"); time.sleep(0.8)
    return js("""(function(){try{var c=LM.charts.chart('chartHistoryTrend');var d=c.getOption().series[0].data;var v=d[0];var p=c.convertToPixel({seriesIndex:0},[0,v]);var cv=document.querySelector('#chartHistoryTrend canvas');var r=cv.getBoundingClientRect();return {x:r.left+p[0],y:r.top+p[1]};}catch(e){return null;}})()""")
def click_until(predicate, attempts=5, wait=2.5):
    """点击 idx0 直到 predicate() 为真（合成点击偶发不触发，重试）"""
    for k in range(attempts):
        p=pt0()
        if p and p.get('x') is not None:
            cdp_click(p['x'],p['y'])
        time.sleep(wait)
        if predicate(): return True
    return predicate()
call("Page.enable",{},10); call("Runtime.enable",{},10)
call("Page.navigate",{"url":"http://127.0.0.1:8790/"},30); time.sleep(4)
js("LM.nav.showPage('history');1")
# 等趋势就绪（canvas 有数据）
ready=False
for _ in range(16):
    time.sleep(2)
    n=js("(function(){var c=LM.charts.chart('chartHistoryTrend');try{var d=c.getOption().series[0].data;return d?d.length:0;}catch(e){return 0;}})()")
    if n and js("document.querySelectorAll('#gapsTbody tr.gap-row').length"):
        ready=True; break
print("ready:", ready, "trend pts:", n)
print("baseline:", json.dumps(cnt(),ensure_ascii=False))
# 点击 idx0 直到 active
ok1 = click_until(lambda: active(), attempts=6, wait=3)
print("after click: active=%s count=%s" % (active(), json.dumps(cnt(),ensure_ascii=False)))
# 取消：再点直到非 active
ok2 = click_until(lambda: not active(), attempts=6, wait=3)
print("after cancel: active=%s count=%s" % (active(), json.dumps(cnt(),ensure_ascii=False)))
print("RESULT click_ok=%s cancel_ok=%s" % (ok1, ok2))
WS_.close()
