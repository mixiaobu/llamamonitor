# cdp_final_check.py <ws_url>
# FINAL verification with the tab forced to "active" (visible) lifecycle:
# 1) Page.setWebLifecycleState=active  (makes document.visibilityState -> visible,
#    so visibleOnly polls run like in a real user window)
# 2) fresh reload -> ready -> usage -> wait for hero settle (usage-summary must render)
# 3) click each range mode, poll label until it flips (10s each)
# 4) hour toggle check.
import sys, socket, struct, base64, os, json, time
def send(ws, obj):
    data = json.dumps(obj).encode("utf-8")
    header = bytearray([0x81]); n = len(data)
    if n < 126: header.append(0x80 | n)
    elif n < 65536: header.append(0x80 | 126); header += struct.pack(">H", n)
    else: header.append(0x80 | 127); header += struct.pack(">Q", n)
    mask = os.urandom(4); header += mask
    ws.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
def _recv_exact(ws, n):
    buf=b""
    while len(buf)<n:
        c=ws.recv(n-len(buf))
        if not c: raise RuntimeError("closed")
        buf+=c
    return buf
def read_msg(ws, deadline):
    ws.settimeout(max(0.2, deadline-time.time()))
    while time.time()<deadline:
        b0,b1=_recv_exact(ws,2); op=b0&0x0F; masked=b1&0x80; n=b1&0x7F
        if n==126: n=struct.unpack(">H",_recv_exact(ws,2))[0]
        elif n==127: n=struct.unpack(">Q",_recv_exact(ws,8))[0]
        mask=_recv_exact(ws,4) if masked else None
        data=_recv_exact(ws,n) if n else b""
        if mask: data=bytes(b^mask[i%4] for i,b in enumerate(data))
        if op==0x9:
            pm=os.urandom(4); h=bytearray([0x8A,0x80|len(data)]); h+=pm
            ws.sendall(bytes(h)+bytes(b^pm[i%4] for i,b in enumerate(data))); continue
        if op in (0x1,0x2): return json.loads(data.decode("utf-8","replace"))
url=sys.argv[1]
rest=url[len("ws://"):]; host,_,path=rest.partition("/"); host,_,port=host.partition(":")
ws=socket.create_connection((host,int(port or 80)))
key=base64.b64encode(os.urandom(16)).decode()
ws.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
resp=b""
while b"\r\n\r\n" not in resp:
    c=ws.recv(4096)
    if not c: raise RuntimeError("handshake")
    resp+=c
_id=100
def ev(js, timeout=8):
    global _id; _id+=1
    send(ws,{"id":_id,"method":"Runtime.evaluate","params":{"expression":js,"returnByValue":True,"awaitPromise":True}})
    d=time.time()+timeout
    while time.time()<d:
        try: j=read_msg(ws,d)
        except Exception: break
        if not isinstance(j,dict): continue
        if j.get("id")==_id:
            r=j.get("result",{})
            if "exceptionDetails" in r:
                return "__EXC__"+str((r["exceptionDetails"].get("exception") or {}).get("description") or r["exceptionDetails"].get("text",""))[:160]
            return r.get("result",{}).get("value")
    return "__TIMEOUT__"
def drain(s):
    d=time.time()+s
    while time.time()<d:
        try: read_msg(ws,d)
        except Exception: break
send(ws,{"id":1,"method":"Runtime.enable"})
send(ws,{"id":2,"method":"Page.enable"})
# force visible/active lifecycle (real user window condition)
r=ev("1")  # noop handshake
send(ws,{"id":3,"method":"Page.setWebLifecycleState","params":{"state":"active"}})
drain(0.3)
print("visibilityState after active:", ev("document.visibilityState"))

send(ws,{"id":5,"method":"Page.reload"})
t0=time.time()
while time.time()-t0<30:
    drain(0.5)
    if ev("(window.LM && LM.app)?'ready':'loading'")=="ready": break
print("ready after %.1fs" % (time.time()-t0))
ev("LM.nav.showPage('usage'); 'x'")
hero="--"; secs=0
for i in range(30):
    drain(1.0); secs=i+1
    hero=ev("document.getElementById('sumHeroLogical').textContent.trim()")
    if hero and hero != "--": break
lab=ev("document.getElementById('sumRangeLabel').textContent.trim()")
print("[entry] hero=%s label=%s after %ds" % (hero, lab, secs))

def click_seg(label):
    return ev("(function(){var el=document.getElementById('usageRange');var bs=el.querySelectorAll('button');"
              "for(var i=0;i<bs.length;i++){if(bs[i].textContent.trim()===%s){bs[i].click();return 'ok';}}"
              "return 'nf';})()" % json.dumps(label))

def wait_label(expect, timeout=12.0):
    last=""
    for i in range(int(timeout)):
        drain(1.0)
        last=ev("document.getElementById('sumRangeLabel').textContent.trim()")
        if expect in last:
            return last, i+1
    return last, int(timeout)+1

tests = [("30 天","30 天"), ("本月","本月"), ("全部","全部"), ("今天","今天"), ("7 天","7 天")]
for btn, expect in tests:
    click_seg(btn)
    last, dt = wait_label(expect)
    hero2=ev("document.getElementById('sumHeroLogical').textContent.trim()")
    print("[switch] %s -> %s in %ds  (hero=%s)" % (btn, last, dt, hero2))

# hour toggle on today
click_seg("今天")
wait_label("今天")
ev("(function(){var el=document.getElementById('usageTrendRange');var bs=el.querySelectorAll('button');"
   "for(var i=0;i<bs.length;i++){if(bs[i].textContent.trim()==='按小时'){bs[i].click();return 'ok';}}return 'nf';})()")
drain(4.0)
h=ev("(function(){var el=document.getElementById('usageTrendRange');var act='?';var bs=el.querySelectorAll('button');"
     "for(var i=0;i<bs.length;i++){if(bs[i].getAttribute('aria-pressed')==='true')act=bs[i].textContent.trim();}return act;})()")
print("[toggle] 按小时 selected =", h)
ev("(function(){var el=document.getElementById('usageTrendRange');var bs=el.querySelectorAll('button');"
   "for(var i=0;i<bs.length;i++){if(bs[i].textContent.trim()==='按天'){bs[i].click();return 'ok';}}return 'nf';})()")
drain(2.0)
h=ev("(function(){var el=document.getElementById('usageTrendRange');var act='?';var bs=el.querySelectorAll('button');"
     "for(var i=0;i<bs.length;i++){if(bs[i].getAttribute('aria-pressed')==='true')act=bs[i].textContent.trim();}return act;})()")
print("[toggle] 按天 restored =", h)
ws.close()
print("DONE")
