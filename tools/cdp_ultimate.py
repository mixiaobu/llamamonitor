# 终极验证：明确设置 deviceMetrics + 强制图表在可视区内 + 按钮对照 + 趋势点击 set/cancel。
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
    call("Input.dispatchMouseEvent",{"type":"mouseMoved","x":x,"y":y},10); time.sleep(0.1)
    call("Input.dispatchMouseEvent",{"type":"mousePressed","x":x,"y":y,"button":"left","clickCount":1},10); time.sleep(0.1)
    call("Input.dispatchMouseEvent",{"type":"mouseReleased","x":x,"y":y,"button":"left","clickCount":1},10)
call("Page.enable",{},10); call("Runtime.enable",{},10)
# 明确 device metrics
call("Emulation.setDeviceMetricsOverride",{"width":1920,"height":1080,"deviceScaleFactor":1,"mobile":False},15)
call("Page.navigate",{"url":"http://127.0.0.1:8790/"},30); time.sleep(5)
js("(function(){var b=document.querySelector('.nav-item[data-page=\"history\"]');if(b)b.click();return LM.nav.currentPage();})()")
for _ in range(14):
    time.sleep(2)
    if js("document.querySelectorAll('#gapsTbody tr.gap-row').length"): break
print("gaps:", js("document.querySelectorAll('#gapsTbody tr.gap-row').length"), "page:", js("LM.nav.currentPage()"))
def cnt(): return js("(function(){var c=document.getElementById('gapsCountLabel');return c?c.textContent:'';})()")
# 挂 echarts click 监听 + canvas 计数
js("""(function(){
  window.__f={ec:0};
  var c=LM.charts.chart('chartHistoryTrend');
  c.off('click'); c.on('click',function(p){window.__f.ec++;window.__f.last=p&&{s:p.seriesIndex,di:p.dataIndex,comp:p.componentType};});
  return 'hooked';
})()""")
# 把趋势图滚到正中，确保 canvas 在可视区
js("document.getElementById('chartHistoryTrend').scrollIntoView({block:'center'});1"); time.sleep(1.2)
info = js("""(function(){
  var c=LM.charts.chart('chartHistoryTrend');var d=c.getOption().series[0].data;var v=d[0];
  var p=c.convertToPixel({seriesIndex:0},[0,v]);var cv=document.querySelector('#chartHistoryTrend canvas');var r=cv.getBoundingClientRect();
  var x=r.left+p[0],y=r.top+p[1];var el=document.elementFromPoint(x,y);
  return {x:Math.round(x),y:Math.round(y),el:el?el.tagName:'null',inview:(y>=0&&y<=window.innerHeight&&x>=0&&x<=window.innerWidth)};
})()""")
print("chart click target:", json.dumps(info))
# 点 idx0
cdp_click(info['x'], info['y']); time.sleep(3)
print("after click idx0:", json.dumps(js("window.__f"),ensure_ascii=False), "cnt:", json.dumps(cnt(),ensure_ascii=False))
# 若 active，取消（重新定位）
if '取消' in str(cnt()):
    info2 = js("""(function(){
      var c=LM.charts.chart('chartHistoryTrend');var d=c.getOption().series[0].data;var v=d[0];
      var p=c.convertToPixel({seriesIndex:0},[0,v]);var cv=document.querySelector('#chartHistoryTrend canvas');var r=cv.getBoundingClientRect();
      return {x:Math.round(r.left+p[0]),y:Math.round(r.top+p[1])};
    })()""")
    cdp_click(info2['x'], info2['y']); time.sleep(3)
    print("after cancel:", "cnt:", json.dumps(cnt(),ensure_ascii=False), "gaps:", js("document.querySelectorAll('#gapsTbody tr.gap-row').length"))
WS_.close()
