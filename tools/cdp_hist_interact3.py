# History 交互验证 v3：DOM 事件驱动（select.change / row.click / btn.onclick）+
# 趋势桶：捕获真实 fetch 参数验证 sub-window 筛选 + 画布中心真实点击。
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
    if y is None: y=0
    call("Input.dispatchMouseEvent",{"type":"mouseMoved","x":x,"y":y},10)
    call("Input.dispatchMouseEvent",{"type":"mousePressed","x":x,"y":y,"button":"left","clickCount":1},10)
    call("Input.dispatchMouseEvent",{"type":"mouseReleased","x":x,"y":y,"button":"left","clickCount":1},10)
def wait_data():
    for _ in range(10):
        time.sleep(2)
        if js("document.querySelectorAll('#eventsTbody tr.ev-row').length"): break

call("Page.enable",{},10); call("Runtime.enable",{},10)
call("Emulation.setDeviceMetricsOverride",{"width":1920,"height":1080,"deviceScaleFactor":1,"mobile":False},15)
call("Page.navigate",{"url":"http://127.0.0.1:8790/"},30)
time.sleep(5)
js("Object.defineProperty(document,'visibilityState',{value:'visible',configurable:true});"
   "if(document.hidden){Object.defineProperty(document,'hidden',{value:false,configurable:true});}"
   "document.dispatchEvent(new Event('visibilitychange'));1")
js("LM.nav.showPage('history');1")
wait_data()

print("=== A. 缺口 显示更多（onclick 直接触发）===")
g0=js("document.querySelectorAll('#gapsTbody tr.gap-row').length")
# 读 onclick 是否 cursor 追加
js("(function(){var b=document.getElementById('gapsMoreBtn');if(b&&!document.getElementById('gapsMoreRow').hidden)b.onclick&&b.onclick();})()")
time.sleep(2)
g1=js("document.querySelectorAll('#gapsTbody tr.gap-row').length")
print("  before=%d after=%d" % (g0,g1))

print("\n=== B. 事件 显示更多 ===")
e0=js("document.querySelectorAll('#eventsTbody tr.ev-row').length")
js("(function(){var b=document.getElementById('evMoreBtn');if(b&&!document.getElementById('evMoreRow').hidden)b.onclick&&b.onclick();})()")
time.sleep(2)
e1=js("document.querySelectorAll('#eventsTbody tr.ev-row').length")
print("  before=%d after=%d" % (e0,e1))

print("\n=== D. 筛选组合 llama+lost ===")
js("var s=document.getElementById('gapSourceFilter');s.value='llama';s.dispatchEvent(new Event('change'));1"); time.sleep(2)
js("var r=document.getElementById('gapRiskFilter');r.value='lost';r.dispatchEvent(new Event('change'));1"); time.sleep(2)
print("  gaps llama+lost=%d" % js("document.querySelectorAll('#gapsTbody tr.gap-row').length"))
js("var s=document.getElementById('gapSourceFilter');s.value='';s.dispatchEvent(new Event('change'));1")
js("var r=document.getElementById('gapRiskFilter');r.value='';r.dispatchEvent(new Event('change'));1"); time.sleep(2)

print("\n=== E. 事件搜索（显示标题匹配）===")
js("var q=document.getElementById('evSearch');q.value='休眠';q.dispatchEvent(new Event('input'));1"); time.sleep(2)
print("  search=休眠 rows=%d" % js("document.querySelectorAll('#eventsTbody tr.ev-row').length"))
js("var q=document.getElementById('evSearch');q.value='';q.dispatchEvent(new Event('input'));1"); time.sleep(2)

print("\n=== F. 趋势桶筛选：捕获真实 fetch 参数 + DOM ===")
# 装 fetch 捕获
js("(function(){ if(window.__sub) return 'ok'; window.__sub={}; var o=window.fetch; window.fetch=function(u){ try{ var p=String(u); if(p.indexOf('/api/history/gaps')>-1){ var m=p.match(/sub_start_ts=(\\d+)&sub_end_ts=(\\d+)/); window.__sub.last = m?{start:+m[1],end:+m[2]}:{start:null,end:null}; } }catch(e){} return o.apply(this,arguments);}; return 'ok'; })()")
# 读趋势第 1 点并直接触发 onTrendBucketClick（内部逻辑：set bucket -> refreshHistoryGaps(true)）
pt = js("(function(){var t=LM.app.__histTrend;if(!t){ return null;} var p=t.points?t.points[0]:null;return p?{ts:p.ts,end:p.ts_end,label:p.label}:null;})()")
# 没有 __histTrend 暴露 -> 通过 state 读（state 是闭包，读不到）。改用 window 读 trend 数据：
# 改为：直接 dispatch 画布中心真实点击（触发 ECharts -> onTrendBucketClick）
js("document.getElementById('chartHistoryTrend').scrollIntoView({block:'center'});1"); time.sleep(1)
pos = js("(function(){var cv=document.querySelector('#chartHistoryTrend canvas');if(!cv)return null;var r=cv.getBoundingClientRect();return {x:r.left+r.width*0.5, y:r.top+r.height*0.42, w:r.width, h:r.height};})()")
print("  canvas pos:", json.dumps(pos))
c0=js("document.querySelectorAll('#gapsTbody tr.gap-row').length")
cnt0=js("(function(){var c=document.getElementById('gapsCountLabel');return c?c.textContent:'?';})()")
if pos:
    cdp_click(pos['x'], pos['y']); time.sleep(2.5)
print("  after canvas click: gaps=%d count=%s" % (js("document.querySelectorAll('#gapsTbody tr.gap-row').length"),
      js("(function(){var c=document.getElementById('gapsCountLabel');return c?c.textContent:'?';})()")))
print("  captured sub-window:", json.dumps(js("window.__sub.last"), ensure_ascii=False))

call("Emulation.clearDeviceMetricsOverride",{},10)
errs=[c for c in console if c[0] in ("error","EXC")]
print("\nCONSOLE errors=%d" % len(errs))
for c in errs[:8]: print("  [%s] %s" % c)
WS_.close()
