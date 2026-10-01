# History 交互验证：趋势点击→筛选+滚动 / 缺口内联展开 / 缺口+事件 显示更多 分页 / 筛选组合。
import sys, socket, struct, base64, os, json, time
WS=open('tools/_ws.txt').read().strip()
def _connect(url):
    rest=url[len("ws://"):]; host,_,path=rest.partition("/"); host,_,port=host.partition(":")
    ws=socket.create_connection((host,int(port or 80))); key=base64.b64encode(os.urandom(16)).decode()
    ws.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    resp=b""
    while b"\r\n\r\n" not in resp:
        c=ws.recv(4096)
        if not c: raise RuntimeError("handshake failed")
        resp+=c
    return ws
ws=_connect(WS); _mid=[0]; console=[]
def _rx(ws,n):
    buf=b""
    while len(buf)<n:
        c=ws.recv(n-len(buf))
        if not c: raise RuntimeError("closed")
        buf+=c
    return buf
def read_msg(ws,timeout):
    ws.settimeout(timeout)
    while True:
        b0,b1=_rx(ws,2); op=b0&0x0F; masked=b1&0x80; n=b1&0x7F
        if n==126: n=struct.unpack(">H",_rx(ws,2))[0]
        elif n==127: n=struct.unpack(">Q",_rx(ws,8))[0]
        mask=_rx(ws,4) if masked else None
        data=_rx(ws,n) if n else b""
        if mask: data=bytes(b^mask[i%4] for i,b in enumerate(data))
        if op==0x9:
            pm=os.urandom(4); h=bytearray([0x8A,0x80|len(data)]); h+=pm
            ws.sendall(bytes(h)+bytes(b^pm[i%4] for i,b in enumerate(data))); continue
        if op in (0x1,0x2): return data.decode("utf8","replace")
def call(method,params=None,timeout=30):
    _mid[0]+=1; i=_mid[0]
    data=json.dumps({"id":i,"method":method,"params":params or {}}).encode()
    h=bytearray([0x81]); n=len(data)
    if n<126: h.append(0x80|n)
    elif n<65536: h.append(0x80|126); h+=struct.pack(">H",n)
    else: h.append(0x80|127); h+=struct.pack(">Q",n)
    mask=os.urandom(4); h+=mask
    ws.sendall(bytes(h)+bytes(b^mask[i%4] for i,b in enumerate(data)))
    deadline=time.time()+timeout
    while time.time()<deadline:
        raw=read_msg(ws,max(0.5,deadline-time.time()))
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

call("Page.enable",{},10); call("Runtime.enable",{},10)
call("Emulation.setDeviceMetricsOverride",{"width":1920,"height":1080,"deviceScaleFactor":1,"mobile":False},15)
call("Page.navigate",{"url":"http://127.0.0.1:8790/"},30)
time.sleep(5)
js("Object.defineProperty(document,'visibilityState',{value:'visible',configurable:true});"
   "if(document.hidden){Object.defineProperty(document,'hidden',{value:false,configurable:true});}"
   "document.dispatchEvent(new Event('visibilitychange'));1")
js("LM.nav.showPage('history');1")
for _ in range(8):
    time.sleep(2)
    if js("document.querySelectorAll('#eventsTbody tr.ev-row').length"): break

print("=== A. 缺口 显示更多 分页 ===")
g0 = js("document.querySelectorAll('#gapsTbody tr.gap-row').length")
moreBtn = js("(function(){var b=document.getElementById('gapsMoreBtn');return b?{hidden:document.getElementById('gapsMoreRow').hidden, txt:b.textContent}:'no-btn';})()")
print("  initial gaps rows=%d  moreBtn=%s" % (g0, json.dumps(moreBtn,ensure_ascii=False)))
if isinstance(moreBtn, dict) and not moreBtn.get('hidden'):
    js("document.getElementById('gapsMoreBtn').click();1")
    time.sleep(2)
    g1 = js("document.querySelectorAll('#gapsTbody tr.gap-row').length")
    moreBtn2 = js("(function(){var b=document.getElementById('gapsMoreBtn');return b?b.textContent:'no-btn';})()")
    print("  after click gaps rows=%d  moreBtn=%s" % (g1, json.dumps(moreBtn2,ensure_ascii=False)))

print("\n=== B. 事件 显示更多 分页 ===")
e0 = js("document.querySelectorAll('#eventsTbody tr.ev-row').length")
evMore = js("(function(){var b=document.getElementById('evMoreBtn');return b?{hidden:document.getElementById('evMoreRow').hidden, txt:b.textContent}:'no-btn';})()")
print("  initial events rows=%d  moreBtn=%s" % (e0, json.dumps(evMore,ensure_ascii=False)))
if isinstance(evMore, dict) and not evMore.get('hidden'):
    js("document.getElementById('evMoreBtn').click();1")
    time.sleep(2)
    e1 = js("document.querySelectorAll('#eventsTbody tr.ev-row').length")
    print("  after click events rows=%d" % e1)

print("\n=== C. 缺口内联展开（点击行 -> detail row）===")
js("(function(){var r=document.querySelector('#gapsTbody tr.gap-row');if(r){r.click();}})()")
time.sleep(1)
print("  gap detail rows:", js("document.querySelectorAll('#gapsTbody tr.gap-detail-row').length"),
      "detail text sample:", js("(function(){var d=document.querySelector('#gapsTbody tr.gap-detail-row .gap-detail');return d?d.textContent.trim().slice(0,80):'none';})()"))

print("\n=== D. 筛选组合（来源=llama + 风险=可能丢失）===")
js("var s=document.getElementById('gapSourceFilter');s.value='llama';s.dispatchEvent(new Event('change'));1")
time.sleep(2)
js("var r=document.getElementById('gapRiskFilter');r.value='lost';r.dispatchEvent(new Event('change'));1")
time.sleep(2)
print("  gaps after llama+lost:", js("document.querySelectorAll('#gapsTbody tr.gap-row').length"))
# 重置
js("var s=document.getElementById('gapSourceFilter');s.value='';s.dispatchEvent(new Event('change'));1")
js("var r=document.getElementById('gapRiskFilter');r.value='';r.dispatchEvent(new Event('change'));1")
time.sleep(2)

print("\n=== E. 事件分类筛选 + 搜索 ===")
print("  category options:", js("(function(){var o=[];document.getElementById('evCategoryFilter').options.forEach(function(x){o.push(x.value);});return JSON.stringify(o);})()"))
js("var c=document.getElementById('evCategoryFilter');c.value='应用';c.dispatchEvent(new Event('change'));1")
time.sleep(2)
print("  events after cat=应用:", js("document.querySelectorAll('#eventsTbody tr.ev-row').length"))
js("var q=document.getElementById('evSearch');q.value='休眠';q.dispatchEvent(new Event('input'));1")
time.sleep(1.5)
print("  events after search=休眠:", js("document.querySelectorAll('#eventsTbody tr.ev-row').length"))
js("var q=document.getElementById('evSearch');q.value='';q.dispatchEvent(new Event('input'));1")
js("var c=document.getElementById('evCategoryFilter');c.value='';c.dispatchEvent(new Event('change'));1")
time.sleep(2)

print("\n=== F. 趋势点击 -> 筛选缺口到桶 + 滚动 ===")
g_all = js("document.querySelectorAll('#gapsTbody tr.gap-row').length")
cnt_all = js("(function(){var c=document.getElementById('gapsCountLabel');return c?c.textContent:'?';})()")
print("  before: gaps rows=%d  count=%s" % (g_all, json.dumps(cnt_all,ensure_ascii=False)))
# 通过 echarts 实例分发 click 到第 1 个 series（触发 onTrendBucketClick(pts[0])）
js("(function(){var c=LM.charts.chart('chartHistoryTrend');if(c){c.dispatchAction({type:'showTip',seriesIndex:0,dataIndex:0});}return 'dispatched';})()")
time.sleep(1.2)
# dispatchAction showTip 不触发 click 事件；用 dispatchAction 不行，改为直接点 canvas 中部
# 直接调用内部不可靠 -> 通过模拟 pointer 事件到 canvas
js("(function(){var cv=document.querySelector('#chartHistoryTrend canvas');if(!cv)return 'no-canvas';var r=cv.getBoundingClientRect();var x=r.left+r.width*0.5,y=r.top+r.height*0.5;cv.dispatchEvent(new MouseEvent('click',{bubbles:true,clientX:x,clientY:y,view:window}));return 'clicked at '+Math.round(x)+','+Math.round(y);})()")
time.sleep(1.5)
g_f = js("document.querySelectorAll('#gapsTbody tr.gap-row').length")
cnt_f = js("(function(){var c=document.getElementById('gapsCountLabel');return c?c.textContent:'?';})()")
print("  after click: gaps rows=%d  count=%s" % (g_f, json.dumps(cnt_f,ensure_ascii=False)))

call("Emulation.clearDeviceMetricsOverride",{},10)
errs=[c for c in console if c[0] in ("error","EXC")]
print("\nCONSOLE errors=%d" % len(errs))
for c in errs: print("  [%s] %s" % c)
ws.close()
