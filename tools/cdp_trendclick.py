# 趋势点击 -> 桶筛选：精确定位有数据的点 -> 真实点击 -> 捕获 sub_start_ts/sub_end_ts + DOM。
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
        if m=="Runtime.exceptionThrown":
            det=p.get("exceptionDetails",{})
            console.append(str((det.get("exception") or {}).get("description",det.get("text")))[:140])
    return {"_timeout":True}
def js(expr,await_p=False,timeout=30):
    r=call("Runtime.evaluate",{"expression":expr,"returnByValue":True,"awaitPromise":await_p},timeout)
    if "exceptionDetails" in r: return {"__exc":str(r["exceptionDetails"])[:200]}
    return r.get("result",{}).get("value",r.get("result"))
def cdp_click(x,y):
    call("Input.dispatchMouseEvent",{"type":"mouseMoved","x":x,"y":y},10)
    call("Input.dispatchMouseEvent",{"type":"mousePressed","x":x,"y":y,"button":"left","clickCount":1},10)
    call("Input.dispatchMouseEvent",{"type":"mouseReleased","x":x,"y":y,"button":"left","clickCount":1},10)
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

# 装 sub-window 捕获
js("(function(){ if(window.__sub) return 'ok'; window.__sub=[]; var o=window.fetch; window.fetch=function(u){try{var p=String(u);if(p.indexOf('/api/history/gaps')>-1&&p.indexOf('sub_start_ts')>-1){var m=p.match(/sub_start_ts=(\\d+)&sub_end_ts=(\\d+)/);window.__sub.push(m?{s:+m[1],e:+m[2]}:{s:0,e:0});}}catch(e){}return o.apply(this,arguments);};return 'ok';})()")

# 用 ECharts 找到第一个 coverage!=null 的点（series 0 = 采集覆盖率 line），取其像素坐标
ptinfo = js("""(function(){
  var c=LM.charts.chart('chartHistoryTrend'); if(!c) return {err:'no chart'};
  var data=c.getOption().series[0].data;
  var idx=-1;
  for(var i=0;i<data.length;i++){ if(data[i]!=null){ idx=i; break; } }
  if(idx<0) return {err:'no data', n:data.length};
  var px=c.convertToPixel({seriesIndex:0}, idx);
  if(!px) return {err:'no pixel', idx:idx};
  var cv=document.querySelector('#chartHistoryTrend canvas');
  cv.scrollIntoView({block:'center'});
  var r=cv.getBoundingClientRect();
  return {idx:idx, x:r.left+px[0], y:r.top+px[1], n:data.length};
})()""")
print("target point:", json.dumps(ptinfo, ensure_ascii=False))
time.sleep(1)
# 重新读 canvas rect（scroll 后）
c0=js("document.querySelectorAll('#gapsTbody tr.gap-row').length")
cnt0=js("(function(){var c=document.getElementById('gapsCountLabel');return c?c.textContent:'?';})()")
print("before: gaps=%d count=%s" % (c0, json.dumps(cnt0,ensure_ascii=False)))
if 'x' in ptinfo and ptinfo.get('x') is not None:
    pos = js("(function(){var cv=document.querySelector('#chartHistoryTrend canvas');var r=cv.getBoundingClientRect();return {w:r.width,h:r.height};})()")
    # 用 getOption data 重新算像素（scroll 后坐标变）
    px2 = js("""(function(){
      var c=LM.charts.chart('chartHistoryTrend');var data=c.getOption().series[0].data;var idx=-1;
      for(var i=0;i<data.length;i++){if(data[i]!=null){idx=i;break;}}
      var p=c.convertToPixel({seriesIndex:0},idx);var cv=document.querySelector('#chartHistoryTrend canvas');var r=cv.getBoundingClientRect();
      return {x:r.left+p[0],y:r.top+p[1]};})()""")
    print("click at:", json.dumps(px2))
    cdp_click(px2['x'], px2['y'])
    time.sleep(3)
    cnt1=js("(function(){var c=document.getElementById('gapsCountLabel');return c?c.textContent:'?';})()")
    print("after: gaps=%d count=%s" % (js("document.querySelectorAll('#gapsTbody tr.gap-row').length"), json.dumps(cnt1,ensure_ascii=False)))
    print("captured sub-window:", json.dumps(js("window.__sub"), ensure_ascii=False))

# 再点一次取消
px2b = js("""(function(){var c=LM.charts.chart('chartHistoryTrend');var data=c.getOption().series[0].data;var idx=-1;for(var i=0;i<data.length;i++){if(data[i]!=null){idx=i;break;}}var p=c.convertToPixel({seriesIndex:0},idx);var cv=document.querySelector('#chartHistoryTrend canvas');var r=cv.getBoundingClientRect();return {x:r.left+p[0],y:r.top+p[1]};})()""")
if px2b and px2b.get('x') is not None:
    cdp_click(px2b['x'], px2b['y']); time.sleep(2.5)
    cnt2=js("(function(){var c=document.getElementById('gapsCountLabel');return c?c.textContent:'?';})()")
    print("after cancel: count=%s" % json.dumps(cnt2,ensure_ascii=False))
WS_.close()
