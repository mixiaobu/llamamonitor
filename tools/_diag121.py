# 诊断移动端 section-header 换行原因
import sys, socket, struct, base64, os, json, time
WS = sys.argv[1].strip()
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
def js(expr,timeout=60):
    r=call("Runtime.evaluate",{"expression":expr,"returnByValue":True},timeout)
    if "exceptionDetails" in r: return {"__exc":str(r["exceptionDetails"])[:300]}
    return r.get("result",{}).get("value",r.get("result"))

call("Page.enable",{},10); call("Runtime.enable",{},10)
call("Emulation.setDeviceMetricsOverride",{"width":390,"height":844,"deviceScaleFactor":1,"mobile":True},15)
call("Page.navigate",{"url":"http://127.0.0.1:8790/"},30)
time.sleep(5)

DIAG = """(function(){
  function txt(el){ return (el.innerText||'').trim().replace(/\\s+/g,' ').slice(0,24); }
  var page=document.querySelector('.page.active'); if(!page) return {err:'no page'};
  var out=[];
  page.querySelectorAll('.section-header').forEach(function(h){
    var t=h.querySelector('.sh-title'), a=h.querySelector('.sh-actions');
    if(!t||!a) return;
    var r1=t.getBoundingClientRect(), r2=a.getBoundingClientRect();
    if(Math.abs((r1.top+r1.bottom)/2-(r2.top+r2.bottom)/2)<=4) return;
    var cs=getComputedStyle(h);
    out.push({
      title: txt(t),
      headerW: Math.round(h.getBoundingClientRect().width),
      titleW: Math.round(r1.width), actionsW: Math.round(r2.width),
      headerFlexWrap: cs.flexWrap, titleFlex: getComputedStyle(t).flex,
      actionsFlex: getComputedStyle(a).flex, actionsJustify: cs.justifyContent,
      actionHTML: a.innerHTML.slice(0,120)
    });
  });
  return out;
})()"""

for page in ["system","performance"]:
    js("LM.nav.showPage('%s');1" % page)
    time.sleep(3)
    print("== %s ==" % page)
    r = js(DIAG)
    print(json.dumps(r, ensure_ascii=False, indent=1))
ws.close()
