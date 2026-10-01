# 对照测试：(A) CDP Input 点击普通按钮 (gaps 行展开) 是否产生 DOM 点击；
# (B) 合成 mousedown+mouseup 到趋势 canvas，是否触发 ECharts click。
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
    call("Input.dispatchMouseEvent",{"type":"mouseMoved","x":x,"y":y},10); time.sleep(0.08)
    call("Input.dispatchMouseEvent",{"type":"mousePressed","x":x,"y":y,"button":"left","clickCount":1},10); time.sleep(0.08)
    call("Input.dispatchMouseEvent",{"type":"mouseReleased","x":x,"y":y,"button":"left","clickCount":1},10)
call("Runtime.enable",{},10)
print("page:", js("LM.nav.currentPage()"), "gaps:", js("document.querySelectorAll('#gapsTbody tr.gap-row').length"))
# (A) 按钮对照：第一个 gap 行，CDP 点击行中间（展开）
js("document.getElementById('gapsSection').scrollIntoView({block:'start'});1"); time.sleep(1)
row = js("""(function(){var r=document.querySelector('#gapsTbody tr.gap-row');var b=r.getBoundingClientRect();return {x:b.left+b.width/2,y:b.top+b.height/2};})()""")
expanded_before = js("document.querySelectorAll('#gapsTbody tr.gap-detail').length")
cdp_click(row['x'], row['y']); time.sleep(1)
expanded_after = js("document.querySelectorAll('#gapsTbody tr.gap-detail').length")
print("(A) CDP button-click: detail rows %s -> %s" % (expanded_before, expanded_after))
# (B) 合成 mousedown+mouseup 到趋势图
print("(B) synthetic chart click:", js("""(function(){
  window.__b={ec:0, zr:0};
  var c=LM.charts.chart('chartHistoryTrend');
  c.off('click'); c.on('click',function(p){window.__b.ec++;window.__b.last=p&&{s:p.seriesIndex,di:p.dataIndex,comp:p.componentType};});
  var d=c.getOption().series[0].data; var p=c.convertToPixel({seriesIndex:0},[0,d[0]]);
  var cv=document.querySelector('#chartHistoryTrend canvas'); var r=cv.getBoundingClientRect();
  var x=r.left+p[0], y=r.top+p[1];
  // zrender 挂在容器/画布上；对最上层 canvas 发 mousedown+mouseup（合成）
  function fire(type){ var e=new MouseEvent(type,{bubbles:true,cancelable:true,view:window,clientX:x,clientY:y,button:0}); cv.dispatchEvent(e); }
  fire('mousedown'); fire('mouseup');
  return 'dispatched at '+Math.round(x)+','+Math.round(y);
})()"""))
time.sleep(0.8)
print("(B) result:", json.dumps(js("window.__b"),ensure_ascii=False))
WS_.close()
