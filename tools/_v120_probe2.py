# 1.2.0 功能回归 v2：fetch 频率实测 + 8 页逐项检查 + 设置项显示值
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
            console.append((p.get("type"),(" ".join(str(x) for x in args))[:160]))
        elif m=="Runtime.exceptionThrown":
            det=p.get("exceptionDetails",{})
            console.append(("EXC",str((det.get("exception") or {}).get("description",det.get("text")))[:160]))
    return {"_timeout":True}
def js(expr,await_p=False,timeout=30):
    r=call("Runtime.evaluate",{"expression":expr,"returnByValue":True,"awaitPromise":await_p},timeout)
    if "exceptionDetails" in r: return {"__exc":str(r["exceptionDetails"])[:200]}
    return r.get("result",{}).get("value",r.get("result"))

call("Page.enable",{},10); call("Runtime.enable",{},10)
call("Emulation.setDeviceMetricsOverride",{"width":1600,"height":1000,"deviceScaleFactor":1,"mobile":False},15)
call("Page.navigate",{"url":"http://127.0.0.1:8790/"},30)
time.sleep(5)
js("Object.defineProperty(document,'visibilityState',{value:'visible',configurable:true});"
   "if(document.hidden){Object.defineProperty(document,'hidden',{value:false,configurable:true});}"
   "document.dispatchEvent(new Event('visibilitychange'));1")
time.sleep(1)

print("===== 1) 轮询任务间隔（R 驱动应为 2000）=====")
audit = js("LM.poll.audit()")
bad = {k: v["intervalMs"] for k, v in audit.items() if v["intervalMs"] != 2000 and k not in ("updates", "updatesProgress")}
print("非 2000ms 任务:", json.dumps(bad, ensure_ascii=False))

print("\n===== 2) fetch 频率实测（overview 页，10s 窗口）=====")
js("window.__cnt={};var _f=window.fetch;window.fetch=function(u,o){try{var p=new URL(u,location.href).pathname;window.__cnt[p]=(window.__cnt[p]||0)+1;}catch(e){}return _f.apply(this,arguments);};1")
time.sleep(10)
cnt = js("window.__cnt")
total = sum(cnt.values())
print("10s 总请求=%d（~%.1f req/s）" % (total, total / 10.0))
for k in sorted(cnt, key=lambda x: -cnt[x]):
    print("  %-28s %3d 次  (~%.1f/s)" % (k, cnt[k], cnt[k] / 10.0))

print("\n===== 3) 逐页功能检查 =====")
def txt(id_):
    return js("(function(){var e=document.getElementById('%s');return e?e.textContent.trim():null;})()" % id_)
def page_text_len(id_):
    return js("(function(){var e=document.getElementById('%s');return e?e.innerText.length:-1;})()" % id_)

# overview
js("LM.nav.showPage('overview');1"); time.sleep(3)
print("overview   server_url=%r last_update=%r" % (txt("ovServerUrl"), txt("ovLastUpdate")))

# usage
js("LM.nav.showPage('usage');1"); time.sleep(3)
print("usage      text_len=%s" % page_text_len("page-usage"))

# performance
js("LM.nav.showPage('performance');1"); time.sleep(3)
print("performance text_len=%s" % page_text_len("page-performance"))

# system
js("LM.nav.showPage('system');1"); time.sleep(3)
print("system     cpu_usage=%r cpu_pct=%r" % (txt("sysCpuUsage"), txt("sysCpuPct")))

# gpu
js("LM.nav.showPage('gpu');1"); time.sleep(3)
print("gpu        text_len=%s" % page_text_len("page-gpu"))

# history
js("LM.nav.showPage('history');1"); time.sleep(4)
hist_rows = js("(function(){var t=document.querySelector('#page-history table');return t?t.rows.length:-1;})()")
print("history    table_rows=%s" % hist_rows)

# settings：各间隔输入框显示值（应全部为 2）
js("LM.nav.showPage('settings');1"); time.sleep(3)
vals = {}
for i in ("setPollInterval", "setGpuPoll", "setRefreshInterval", "setSysPoll", "setSysHistory", "setSysAdvPoll"):
    vals[i] = js("(function(){var e=document.getElementById('%s');return e?e.value:null;})()" % i)
print("settings   间隔显示值:", json.dumps(vals, ensure_ascii=False))

# about
js("LM.nav.showPage('about');1"); time.sleep(2)
print("about      version=%r" % txt("aboutVersion"))

call("Emulation.clearDeviceMetricsOverride",{},10)
errs=[c for c in console if c[0] in ("error","EXC")]
warns=[c for c in console if c[0]=="warning"]
print("\nCONSOLE total=%d errors=%d warnings=%d" % (len(console),len(errs),len(warns)))
for c in errs: print("  ERR  [%s] %s" % c)
for c in warns[:12]: print("  WARN [%s] %s" % c)
ws.close()
