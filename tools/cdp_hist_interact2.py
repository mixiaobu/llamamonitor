# History 交互验证 v2：真实 CDP 鼠标点击（canvas 需要真事件）。
# 趋势点击 -> 筛选+滚动；缺口/事件 显示更多；内联展开；筛选组合。
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
        elif m=="Runtime.exceptionThrown":
            det=p.get("exceptionDetails",{})
            console.append(("EXC",str((det.get("exception") or {}).get("description",det.get("text")))[:140]))
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
    if js("document.querySelectorAll('#eventsTbody tr.ev-row').length"): break

print("=== A. 缺口 显示更多 ===")
print("  before rows=%d" % js("document.querySelectorAll('#gapsTbody tr.gap-row').length"))
pos = js("(function(){var b=document.getElementById('gapsMoreBtn');if(!b||document.getElementById('gapsMoreRow').hidden)return null;b.scrollIntoView({block:'center'});var r=b.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2};})()")
if pos:
    cdp_click(pos['x'],pos['y']); time.sleep(2)
print("  after rows=%d" % js("document.querySelectorAll('#gapsTbody tr.gap-row').length"))

print("\n=== B. 事件 显示更多 ===")
e0=js("document.querySelectorAll('#eventsTbody tr.ev-row').length")
pos = js("(function(){var b=document.getElementById('evMoreBtn');if(!b||document.getElementById('evMoreRow').hidden)return null;b.scrollIntoView({block:'center'});var r=b.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2};})()")
if pos: cdp_click(pos['x'],pos['y']); time.sleep(2)
print("  before=%d after=%d" % (e0, js("document.querySelectorAll('#eventsTbody tr.ev-row').length")))

print("\n=== C. 缺口内联展开 ===")
js("(function(){var r=document.querySelector('#gapsTbody tr.gap-row');if(r)r.click();})()")
time.sleep(1)
print("  detail rows=%d  text=%s" % (js("document.querySelectorAll('#gapsTbody tr.gap-detail-row').length"),
      js("(function(){var d=document.querySelector('#gapsTbody tr.gap-detail-row .gap-detail');return d?d.textContent.trim().slice(0,70):'none';})()")))

print("\n=== D. 筛选组合 llama+lost ===")
js("var s=document.getElementById('gapSourceFilter');s.value='llama';s.dispatchEvent(new Event('change'));1")
time.sleep(2)
js("var r=document.getElementById('gapRiskFilter');r.value='lost';r.dispatchEvent(new Event('change'));1")
time.sleep(2)
print("  gaps llama+lost=%d" % js("document.querySelectorAll('#gapsTbody tr.gap-row').length"))
js("var s=document.getElementById('gapSourceFilter');s.value='';s.dispatchEvent(new Event('change'));1")
js("var r=document.getElementById('gapRiskFilter');r.value='';r.dispatchEvent(new Event('change'));1")
time.sleep(2)

print("\n=== E. 事件分类+搜索 ===")
print("  category options:", js("(function(){var s=document.getElementById('evCategoryFilter');var o=[];Array.prototype.forEach.call(s.options,function(x){o.push(x.value);});return JSON.stringify(o);})()"))
js("var q=document.getElementById('evSearch');q.value='休眠';q.dispatchEvent(new Event('input'));1")
time.sleep(1.5)
print("  search=休眠 rows=%d" % js("document.querySelectorAll('#eventsTbody tr.ev-row').length"))
js("var q=document.getElementById('evSearch');q.value='';q.dispatchEvent(new Event('input'));1")
time.sleep(2)

print("\n=== F. 趋势点击 -> 筛选缺口到桶 + 滚动 ===")
print("  before: gaps=%d count=%s" % (js("document.querySelectorAll('#gapsTbody tr.gap-row').length"),
      js("(function(){var c=document.getElementById('gapsCountLabel');return c?c.textContent:'?';})()")))
# 找趋势 canvas 中部某 series 点的像素坐标
pos = js("(function(){var c=LM.charts.chart('chartHistoryTrend');if(!c)return null;var cv=document.querySelector('#chartHistoryTrend canvas');if(!cv)return null;cv.scrollIntoView({block:'center'});var pt=null;try{pt=c.convertToPixel({seriesIndex:0},[0]);}catch(e){}var r=cv.getBoundingClientRect();if(pt)return {x:r.left+pt[0],y:r.top+pt[1]};return {x:r.left+r.width*0.5,y:r.top+r.height*0.4};})()")
print("  chart click pos:", json.dumps(pos))
if pos:
    cdp_click(pos['x'],pos['y']); time.sleep(2)
print("  after: gaps=%d count=%s" % (js("document.querySelectorAll('#gapsTbody tr.gap-row').length"),
      js("(function(){var c=document.getElementById('gapsCountLabel');return c?c.textContent:'?';})()")))
# 再点一次取消
if pos:
    cdp_click(pos['x'],pos['y']); time.sleep(2)
print("  after 2nd click (cancel): count=%s" % js("(function(){var c=document.getElementById('gapsCountLabel');return c?c.textContent:'?';})()"))

call("Emulation.clearDeviceMetricsOverride",{},10)
errs=[c for c in console if c[0] in ("error","EXC")]
print("\nCONSOLE errors=%d" % len(errs))
for c in errs[:8]: print("  [%s] %s" % c)
WS_.close()
