# cdp_custom_check.py <ws_url>
# Active-lifecycle custom range roundtrip: open popover, set 2026-09-23 ~ 2026-09-29,
# apply, verify label + hero. Then restore 7天.
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
    send(ws,{"id":_id,"method":"Runtime.evaluate","params":{"expression":js,"returnByValue":True}})
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
send(ws,{"id":3,"method":"Page.setWebLifecycleState","params":{"state":"active"}})
drain(0.3)
# app should already be ready; ensure usage page
ev("if(!document.getElementById('page-usage').classList.contains('active')){LM.nav.showPage('usage')} 'ok'")
drain(1.0)
hero=ev("document.getElementById('sumHeroLogical').textContent.trim()")
print("before:", ev("document.getElementById('sumRangeLabel').textContent.trim()"), hero)
# open popover
ev("document.getElementById('usageRangeCustom').click(); 'x'")
drain(1.0)
open_state = ev("document.getElementById('usageRangePopover').hidden? 'hidden':'open'")
print("popover:", open_state)
# set dates
ev("document.getElementById('usageRangeStart').value='2026-09-23'; 'x'")
ev("document.getElementById('usageRangeEnd').value='2026-09-29'; 'x'")
ev("document.getElementById('usageRangeApply').click(); 'x'")
for i in range(1, 13):
    drain(1.0)
    lab=ev("document.getElementById('sumRangeLabel').textContent.trim()")
    if "~" in lab or "~" in lab:
        print("t+%ds CUSTOM OK: %s | hero=%s | btn=%s" % (i, lab, ev("document.getElementById('sumHeroLogical').textContent.trim()"), ev("document.getElementById('usageRangeCustomLabel').textContent.trim()")))
        break
else:
    print("custom label after 12s:", lab)
# restore 7天
ev("(function(){var el=document.getElementById('usageRange');var bs=el.querySelectorAll('button');for(var i=0;i<bs.length;i++){if(bs[i].textContent.trim()==='7 天'){bs[i].click();return 'ok';}}return 'nf';})()")
for i in range(1, 8):
    drain(1.0)
    lab=ev("document.getElementById('sumRangeLabel').textContent.trim()")
    if "7" in lab: break
print("restored 7天:", lab)
ws.close()
print("DONE")
