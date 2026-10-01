# 最终 1920 验收：reload -> 轮询能耗直至出现(上限 45s) -> 控制台错误 -> 全页截图
import sys, socket, struct, base64, os, json, time

WS = sys.argv[1]
OUT = sys.argv[2]
os.makedirs(OUT, exist_ok=True)

def _connect(url):
    rest = url[len("ws://"):]
    host, _, path = rest.partition("/")
    host, _, port = host.partition(":")
    ws = socket.create_connection((host, int(port or 80)))
    key = base64.b64encode(os.urandom(16)).decode()
    ws.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        c = ws.recv(4096)
        if not c: raise RuntimeError("handshake failed")
        resp += c
    if b"101" not in resp.split(b"\r\n")[0]: raise RuntimeError("bad handshake")
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
def call(method,params=None,timeout=60):
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
        m=j.get("method")
        if m=="Runtime.consoleAPICalled":
            args=[a.get("value",a.get("description")) for a in j.get("params",{}).get("args",[])]
            console.append(str(j.get("params",{}).get("type"))+":"+ " ".join(str(x) for x in args)[:200])
        elif m=="Runtime.exceptionThrown":
            det=j.get("params",{}).get("exceptionDetails",{})
            console.append("EXC "+str((det.get("exception") or {}).get("description",det.get("text")))[:200])
    return {"_timeout":True}
def js(expr):
    r=call("Runtime.evaluate",{"expression":expr,"returnByValue":True},30)
    return r.get("result",{}).get("value",r.get("result"))

call("Page.enable",{},10)
call("Runtime.enable",{},10)
call("Emulation.setDeviceMetricsOverride",{"width":1920,"height":1080,"deviceScaleFactor":1,"mobile":False},15)
call("Page.navigate",{"url":"http://127.0.0.1:8790/"},30)
time.sleep(4)
console.clear()
js("LM.nav.showPage('gpu');1")

# 轮询能耗出现（上限 45s，每 3s 一次）
energy=None; t=0
while t < 45:
    time.sleep(3); t += 3
    energy = js("Array.from(document.querySelectorAll('#gpuEnergy .energy-row')).map(function(r){var k=r.querySelector('.k'),v=r.querySelector('.v');return (k?k.textContent:'')+' = '+(v?v.textContent:'');})")
    if energy:
        print("energy appeared at t=%ds:" % t, json.dumps(energy, ensure_ascii=False)); break
if not energy:
    print("energy STILL empty at 45s")
    energy = js("(document.querySelector('#gpuEnergy')||{}).textContent||'(empty)')")
    print("  gpuEnergy.textContent =", energy)

# 运行状态 + 高级折叠（默认）+ 进程区滚动
final = js("""(function(){var o={};
o.runStates=Array.from(document.querySelectorAll('#gpuCards .gpu-device')).map(function(c){
  var a=c.querySelector('.adv-row .k');var r=Array.from(c.querySelectorAll('.adv-row')).filter(function(x){var k=x.querySelector('.k');return k&&k.textContent==='运行状态';})[0];
  return r?r.querySelector('.v').textContent:'?';});
o.advDefaultClosed=Array.from(document.querySelectorAll('#gpuCards .gpu-adv')).map(function(d){return !d.open;});
var p=document.querySelector('#gpuProc');var cs=getComputedStyle(p);
o.proc={overflowY:cs.overflowY,maxHeight:cs.maxHeight,clientH:p.clientHeight,scrollH:p.scrollHeight};
o.hOverflow=document.documentElement.scrollWidth>document.documentElement.clientWidth+1;
o.moreTrends=!!document.getElementById('gpuMoreTrends');
return o;})()""")
print("FINAL 1920:", json.dumps(final, ensure_ascii=False))

# 截图：能耗 + 全页
scroll = js("(function(){var e=document.querySelector('#gpuEnergy');if(e)e.scrollIntoView({block:'center'});})()")
time.sleep(0.6)
r = call("Page.captureScreenshot",{"format":"png"},90)
if isinstance(r,dict) and "data" in r:
    with open(os.path.join(OUT,"gpu-1920-energy.png"),"wb") as f: f.write(base64.b64decode(r["data"]))
    print("shot gpu-1920-energy.png")
js("(function(){window.scrollTo(0,0);})()")
time.sleep(0.4)
r = call("Page.captureScreenshot",{"format":"png","captureBeyondViewport":False},90)
if isinstance(r,dict) and "data" in r:
    with open(os.path.join(OUT,"gpu-1920-final.png"),"wb") as f: f.write(base64.b64decode(r["data"]))
    print("shot gpu-1920-final.png")

print("=== CONSOLE (last 12) ===")
for line in console[-12:]:
    print("  ", line)
ws.close()
print("DONE")
