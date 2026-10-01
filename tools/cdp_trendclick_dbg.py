# 趋势点击 debug：在 canvas 上加 DOM click 监听 + ECharts on('click') 监听，CDP 点击后看谁触发。
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
    return {"_timeout":True}
def js(expr,await_p=False,timeout=30):
    r=call("Runtime.evaluate",{"expression":expr,"returnByValue":True,"awaitPromise":await_p},timeout)
    if "exceptionDetails" in r: return {"__exc":str(r["exceptionDetails"])[:200]}
    return r.get("result",{}).get("value",r.get("result"))
def cdp_click(x,y):
    call("Input.dispatchMouseEvent",{"type":"mouseMoved","x":x,"y":y},10); time.sleep(0.06)
    call("Input.dispatchMouseEvent",{"type":"mousePressed","x":x,"y":y,"button":"left","clickCount":1},10); time.sleep(0.06)
    call("Input.dispatchMouseEvent",{"type":"mouseReleased","x":x,"y":y,"button":"left","clickCount":1},10)
call("Page.enable",{},10); call("Runtime.enable",{},10)
call("Emulation.setDeviceMetricsOverride",{"width":1920,"height":1080,"deviceScaleFactor":1,"mobile":False},15)
call("Page.navigate",{"url":"http://127.0.0.1:8790/"},30)
time.sleep(5)
js("LM.nav.showPage('history');1")
for _ in range(10):
    time.sleep(2)
    if js("document.querySelectorAll('#gapsTbody tr.gap-row').length"): break
# 挂监听
js("""(function(){
  window.__dbg={domClick:0, ecClick:0};
  var c=LM.charts.chart('chartHistoryTrend');
  c.off('click'); c.on('click', function(p){ window.__dbg.ecClick++; window.__dbg.lastE=p&&{series:p.seriesIndex,dataIndex:p.dataIndex,comp:p.componentType}; });
  var cv=document.querySelector('#chartHistoryTrend canvas');
  cv.onclick=function(ev){ window.__dbg.domClick++; };
  return 'hooked';
})()""")
js("document.getElementById('chartHistoryTrend').scrollIntoView({block:'center'});1"); time.sleep(1)
def px(idx):
    return js("""(function(){var c=LM.charts.chart('chartHistoryTrend');var d=c.getOption().series[0].data;var v=d[%d];if(v==null)return null;var p;try{p=c.convertToPixel({seriesIndex:0},[%d,v]);}catch(e){return null;}if(!p)return null;var cv=document.querySelector('#chartHistoryTrend canvas');var r=cv.getBoundingClientRect();return {x:r.left+p[0],y:r.top+p[1],w:r.width,h:r.height,top:r.top};})()""" % (idx,idx))
p=px(3)
print("point3:", json.dumps(p))
# canvas 顶部信息
print("canvas count:", js("document.querySelectorAll('#chartHistoryTrend canvas').length"))
cdp_click(p['x'], p['y']); time.sleep(1)
print("after click @point3:", json.dumps(js("window.__dbg")))
# 也在 canvas 正中心 + 顶部多点试
for (fx,fy) in [(0.5,0.5),(0.5,0.15),(0.3,0.3),(0.7,0.2)]:
    cdp_click(p['top']+p['w']*fx, p['top']+p['h']*fy); time.sleep(0.5)
print("after more clicks:", json.dumps(js("window.__dbg")))
WS_.close()
