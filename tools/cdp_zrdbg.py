# debug zrender click：挂 zrender click 监听，CDP 点击后看是否触发 + offsetX/Y + 坐标换算结果。
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
    if "exceptionDetails" in r: return {"__exc":str(r["exceptionDetails"])[:200]}
    return r.get("result",{}).get("value",r.get("result"))
def cdp_click(x,y):
    call("Input.dispatchMouseEvent",{"type":"mouseMoved","x":x,"y":y},10); time.sleep(0.08)
    call("Input.dispatchMouseEvent",{"type":"mousePressed","x":x,"y":y,"button":"left","clickCount":1},10); time.sleep(0.08)
    call("Input.dispatchMouseEvent",{"type":"mouseReleased","x":x,"y":y,"button":"left","clickCount":1},10)
call("Page.enable",{},10); call("Runtime.enable",{},10)
js("LM.nav.showPage('history');1")
js("document.getElementById('chartHistoryTrend').scrollIntoView({block:'center'});1"); time.sleep(1.5)
# 挂 zrender click + echarts click 监听
js("""(function(){
  window.__z={zrClick:0, ecClick:0};
  var c=LM.charts.chart('chartHistoryTrend');
  var zr=c.getZr();
  zr.off('click'); zr.on('click', function(ev){ window.__z.zrClick++; window.__z.last={ox:ev.offsetX,oy:ev.offsetY,target:ev.target?1:0}; });
  c.off('click'); c.on('click', function(p){ window.__z.ecClick++; window.__z.lastEC=p&&{s:p.seriesIndex,di:p.dataIndex,comp:p.componentType}; });
  return 'hooked zrCount='+c.getZr().handlers? 'yes':'no';
})()""")
# 取点0像素 + 画布中心
info = js("""(function(){var c=LM.charts.chart('chartHistoryTrend');var d=c.getOption().series[0].data;var p=c.convertToPixel({seriesIndex:0},[0,d[0]]);var cv=document.querySelector('#chartHistoryTrend canvas');var r=cv.getBoundingClientRect();return {pt0:{x:r.left+p[0],y:r.top+p[1]}, center:{x:r.left+r.width/2,y:r.top+r.height/2}};})()""")
print("targets:", json.dumps(info))
# 点击点0
cdp_click(info['pt0']['x'], info['pt0']['y']); time.sleep(1)
print("after click pt0:", json.dumps(js("window.__z"),ensure_ascii=False))
# 点击中心
cdp_click(info['center']['x'], info['center']['y']); time.sleep(1)
print("after click center:", json.dumps(js("window.__z"),ensure_ascii=False))
WS_.close()
