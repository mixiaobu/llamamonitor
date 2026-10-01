# 390 手机进程卡结构验证（data-label / 卡片化 / 无横滚）
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
_mid=[0]
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
def call(method,params=None,timeout=45):
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
def js(expr):
    r=call("Runtime.evaluate",{"expression":expr,"returnByValue":True},30)
    return r.get("result",{}).get("value",r.get("result"))

call("Page.enable",{},10)
call("Emulation.setDeviceMetricsOverride",{"width":390,"height":844,"deviceScaleFactor":1,"mobile":True},15)
call("Page.navigate",{"url":"http://127.0.0.1:8790/"},30)
time.sleep(4)
js("LM.nav.showPage('gpu');1")
time.sleep(5)

probe = js("""(function(){
  var rows=document.querySelectorAll('#gpuProc .gpu-proc-table tbody tr');
  var out={rowCount:rows.length, cardified:false, labels:[], firstSample:null, hOverflow:false};
  if(rows.length){
    var first=rows[0]; var cs=getComputedStyle(first);
    out.cardified = (cs.display==='block');
    // 每个 td 的 data-label
    var tds=first.querySelectorAll('td');
    out.labels=Array.prototype.map.call(tds,function(td){return td.getAttribute('data-label');});
    // 第一个 td（应用）显示内容
    out.firstSample={app:tds[0].textContent, appTitle:tds[0].getAttribute('title'),
      pid:tds[1].textContent, gpu:tds[2].textContent, mem:tds[3].textContent};
  }
  // 页面横向溢出
  out.pageHOverflow=document.documentElement.scrollWidth>document.documentElement.clientWidth+1;
  out.scrollW=document.documentElement.scrollWidth; out.clientW=document.documentElement.clientWidth;
  // 进程区无内部 vertical scroll
  var p=document.querySelector('#gpuProc'); var cs2=getComputedStyle(p);
  out.procOverflowY=cs2.overflowY; out.procMaxHeight=cs2.maxHeight;
  out.procScrollH=p.scrollHeight; out.procClientH=p.clientHeight;
  return out;
})()""")
print("MOBILE PROC:", json.dumps(probe, ensure_ascii=False, indent=2))
ws.close()
