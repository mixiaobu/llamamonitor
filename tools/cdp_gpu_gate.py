# GPU 页：控制台 + 轮询节奏门禁（in-page fetch 计数，34s 窗口）。
# 包一层 window.fetch 计数 /api/gpu/* 调用（轮询走全局 fetch，能抓到）。
import sys, socket, struct, base64, os, json, time

WS = sys.argv[1]
def _connect(url):
    rest = url[len("ws://"):]
    host, _, path = rest.partition("/")
    host, _, port = host.partition(":")
    ws = socket.create_connection((host, int(port or 80)))
    key = base64.b64encode(os.urandom(16)).decode()
    ws.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        c = ws.recv(4096)
        if not c: raise RuntimeError("handshake failed")
        resp += c
    return ws

ws = _connect(WS)
_mid=[0]; console=[]
def _recv_exact(ws,n):
    buf=b""
    while len(buf)<n:
        c=ws.recv(n-len(buf))
        if not c: raise RuntimeError("closed")
        buf+=c
    return buf
def read_msg(ws,timeout):
    ws.settimeout(timeout)
    while True:
        b0,b1=_recv_exact(ws,2)
        op=b0&0x0F; masked=b1&0x80; n=b1&0x7F
        if n==126: n=struct.unpack(">H",_recv_exact(ws,2))[0]
        elif n==127: n=struct.unpack(">Q",_recv_exact(ws,8))[0]
        mask=_recv_exact(ws,4) if masked else None
        data=_recv_exact(ws,n) if n else b""
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
            console.append(p.get("type")+" "+(" ".join(str(x) for x in args))[:160])
        elif m=="Runtime.exceptionThrown":
            det=p.get("exceptionDetails",{})
            console.append("EXC "+str((det.get("exception") or {}).get("description",det.get("text")))[:160])
    return {"_timeout":True}
def js(expr,await_p=False,timeout=30):
    r=call("Runtime.evaluate",{"expression":expr,"returnByValue":True,"awaitPromise":await_p},timeout)
    if "exceptionDetails" in r: return {"__exc":str(r["exceptionDetails"])[:300]}
    return r.get("result",{}).get("value",r.get("result"))

call("Page.enable",{},10)
call("Runtime.enable",{},10)
call("Emulation.setDeviceMetricsOverride",{"width":1920,"height":1080,"deviceScaleFactor":1,"mobile":False},15)
call("Page.navigate",{"url":"http://127.0.0.1:8790/"},30)
time.sleep(5)
js("Object.defineProperty(document,'visibilityState',{value:'visible',configurable:true});"
   "if(document.hidden){Object.defineProperty(document,'hidden',{value:false,configurable:true});}"
   "document.dispatchEvent(new Event('visibilitychange'));1")
time.sleep(1)
# 装 in-page fetch 计数器（包 window.fetch；轮询走全局 fetch，能抓到 /api/gpu/*）
js("window.__gpuNet={}; (function(){var orig=window.fetch; window.fetch=function(u,o){ try{var p=String(u); var m=(p.match(/\\/api\\/gpu\\/[a-z]+/)||[])[0]; if(m){window.__gpuNet[m]=(window.__gpuNet[m]||0)+1;}}catch(e){} return orig.apply(this,arguments);}; })();1")
js("LM.nav.showPage('gpu');1")
time.sleep(4)      # 等首次 status/live/daily 完成（计入计数）
js("window.__gpuNet={};1")   # 清零，只统计窗口内的轮询
# 停留 34s：status 15s + gpuLive 15s（各 ~2 次）；daily 60s（0）；mtp 若 gpu 页则不算
time.sleep(34)
net = js("window.__gpuNet")
print("GPU PAGE POLL (34s window, forced visible):")
print(json.dumps(net, ensure_ascii=False, indent=2))
print("CONSOLE lines:", len(console))
for line in console:
    print("  ", line)
ws.close()
