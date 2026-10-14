# 1.2.0 补充：移动端视口巡检 + 设置保存往返
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

print("===== 1) 移动端 390x844 巡检（5 核心页 + 底部导航）=====")
call("Page.enable",{},10); call("Runtime.enable",{},10)
call("Emulation.setDeviceMetricsOverride",{"width":390,"height":844,"deviceScaleFactor":2,"mobile":True},15)
call("Page.navigate",{"url":"http://127.0.0.1:8790/"},30)
time.sleep(5)
js("Object.defineProperty(document,'visibilityState',{value:'visible',configurable:true});"
   "if(document.hidden){Object.defineProperty(document,'hidden',{value:false,configurable:true});}"
   "document.dispatchEvent(new Event('visibilitychange'));1")
time.sleep(1)
mnav = js("(function(){var b=document.querySelector('.mobile-nav');return b?b.querySelectorAll('.mnav-item').length:-1;})()")
print("mobile-nav items=%s" % mnav)
for page in ["overview","usage","performance","system","gpu"]:
    js("LM.nav.showPage('%s');1" % page)
    time.sleep(3)
    ok = js("(function(){var s=document.getElementById('page-%s');return s&&s.classList.contains('active')&&s.innerText.length>50;})()" % page)
    overflow = js("(function(){return document.documentElement.scrollWidth<=392;})()")
    print("%-12s active+content=%s  no_h_overflow=%s" % (page, ok, overflow))

print("\n===== 2) 设置保存往返（PUT /api/config 当前值 -> 200 -> 读回一致）=====")
call("Emulation.clearDeviceMetricsOverride",{},10)
js("LM.nav.showPage('settings');1"); time.sleep(3)
# 收集当前输入值（设置页已填好），触发保存
save_result = js("(async function(){"
    "try{"
    "  var c=await fetch('/api/config').then(function(r){return r.json();});"
    "  var payload={collector:c.collector,gpu:c.gpu,ui:c.ui,system:c.system};"
    "  var r=await fetch('/api/config',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});"
    "  var d=await r.json();"
    "  var back=await fetch('/api/config').then(function(x){return x.json();});"
    "  return JSON.stringify({status:r.status,changed:(d.changed||[]).length,"
    "    collector:back.collector.poll_interval_seconds,gpu:back.gpu.poll_interval_seconds,"
    "    ui:back.ui.refresh_interval_seconds,sys_hist:back.system.history_interval_seconds,adv:back.system.advanced_sensor_interval_seconds});"
    "}catch(e){return 'ERR '+e.message;}})()\n"
    , await_p=True, timeout=30)
print("save roundtrip:", save_result)

errs=[c for c in console if c[0] in ("error","EXC")]
warns=[c for c in console if c[0]=="warning"]
print("\nCONSOLE total=%d errors=%d warnings=%d" % (len(console),len(errs),len(warns)))
for c in errs: print("  ERR  [%s] %s" % c)
for c in warns[:12]: print("  WARN [%s] %s" % c)
ws.close()
