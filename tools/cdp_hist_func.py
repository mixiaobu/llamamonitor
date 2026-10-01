# History 功能深度验证：趋势图/汇总/banner/缺口原因/事件展示层/时间格式。
import sys, socket, struct, base64, os, json, time
WS = sys.argv[1]
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
ws=_connect(WS); _mid=[0]
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
    return {"_timeout":True}
def js(expr,await_p=False,timeout=30):
    r=call("Runtime.evaluate",{"expression":expr,"returnByValue":True,"awaitPromise":await_p},timeout)
    if "exceptionDetails" in r: return {"__exc":str(r["exceptionDetails"])[:400]}
    return r.get("result",{}).get("value",r.get("result"))

call("Page.enable",{},10); call("Runtime.enable",{},10)
call("Emulation.setDeviceMetricsOverride",{"width":1920,"height":1080,"deviceScaleFactor":1,"mobile":False},15)
call("Page.navigate",{"url":"http://127.0.0.1:8790/"},30)
time.sleep(5)
js("Object.defineProperty(document,'visibilityState',{value:'visible',configurable:true});"
   "if(document.hidden){Object.defineProperty(document,'hidden',{value:false,configurable:true});}"
   "document.dispatchEvent(new Event('visibilitychange'));1")
js("LM.nav.showPage('history');1")
time.sleep(6)

def G(id):
    return js("(function(){var e=document.getElementById('%s');return e?(e.textContent||'').trim():'__MISSING';})()" % id)

print("=== 1. 汇总 6 指标 ===")
for id in ["iqDb","iqDbHint","iqCoverage","iqCoverageHint","iqGaps","iqGapsHint","iqRisk","iqRiskHint","iqLastSample","iqLastSampleHint","iqMonitorStart","iqOpenGap"]:
    print("  %-16s = %s" % (id, G(id)[:60]))

print("\n=== 2. Schema Banner（应隐藏=正常）===")
print("  banner:", G("historySchemaBanner"), "hidden?", js("document.getElementById('historySchemaBanner').hidden"))

print("\n=== 3. 完整性趋势图（ECharts canvas + 数据点）===")
print("  canvas:", js("!!document.querySelector('#chartHistoryTrend canvas')"),
      "svg:", js("!!document.querySelector('#chartHistoryTrend svg')"),
      "emptyOverlay:", js("(function(){var o=document.querySelector('#chartHistoryTrend .chart-empty-overlay');return o?o.style.display:'none';})()"),
      "granHint:", G("trendGranHint")[:50])
# trend points count from state (chart rendered from state.historyTrend)
print("  trend points:", js("(function(){try{return (LM.app.getHistoryTrend&&LM.app.getHistoryTrend())?LM.app.getHistoryTrend().length:'no-getter';}catch(e){return 'err';}})()"))

print("\n=== 4. 缺口原因标签（正式中文 + 休眠推定）===")
print("  sample gap rows (原因列):")
print(" ", js("""(function(){var out=[];document.querySelectorAll('#gapsTbody tr.gap-row').forEach(function(tr){var tds=tr.querySelectorAll('td');if(tds.length>=5){out.push(tds[3].textContent.trim()+' | '+tds[4].textContent.trim());}});return JSON.stringify(out.slice(0,12));})()"""))
# distinct reasons present
print("  distinct reasons:", js("""(function(){var s={};document.querySelectorAll('#gapsTbody tr.gap-row').forEach(function(tr){var td=tr.querySelectorAll('td')[3];if(td){s[td.textContent.trim()]=1;}});return JSON.stringify(Object.keys(s));})()"""))

print("\n=== 5. 事件展示层（display_title/severity icon，无 raw key）===")
print("  sample event rows:")
print(" ", js("""(function(){var out=[];document.querySelectorAll('#eventsTbody tr.ev-row').forEach(function(tr){var tds=tr.querySelectorAll('td');if(tds.length>=4){out.push(tds[1].textContent.trim().slice(0,30)+' | '+tds[2].textContent.trim());}});return JSON.stringify(out.slice(0,10));})()"""))
# any raw key leaked (e.g. event_type in 事件 col)? check for underscore tokens in 事件 col
print("  raw keys in 事件 col:", js("""(function(){var hit=0;document.querySelectorAll('#eventsTbody tr.ev-row td.ev-title').forEach(function(td){var t=td.textContent;if(/[_]/.test(t)) hit++;});return hit;})()"""))

print("\n=== 6. 时间格式（duration 中文 / 日期）===")
print("  gap 时间列 sample:", js("""(function(){var t=document.querySelectorAll('#gapsTbody tr.gap-row td')[0];return t?t.title+' / '+t.textContent.trim():'none';})()"""))
print("  event 时间列 sample:", js("""(function(){var t=document.querySelectorAll('#eventsTbody tr.ev-row td')[0];return t?t.title+' / '+t.textContent.trim():'none';})()"""))
print("  F.formatDuration(8):", js("LM.fmt.formatDuration(8)"))
print("  F.formatDuration(65):", js("LM.fmt.formatDuration(65)"))
print("  F.formatDuration(7518):", js("LM.fmt.formatDuration(7518)"))

call("Emulation.clearDeviceMetricsOverride",{},10)
ws.close()
