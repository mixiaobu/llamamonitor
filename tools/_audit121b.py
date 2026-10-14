# 1.2.1 回归审计：桌面 1600 + 移动 390×844 双宽度
# 输出说明文字数量 + section-header 标题/操作行对齐（阈值 4px）
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
def js(expr,await_p=False,timeout=60):
    r=call("Runtime.evaluate",{"expression":expr,"returnByValue":True,"awaitPromise":await_p},timeout)
    if "exceptionDetails" in r: return {"__exc":str(r["exceptionDetails"])[:300]}
    return r.get("result",{}).get("value",r.get("result"))

call("Page.enable",{},10); call("Runtime.enable",{},10)
call("Page.navigate",{"url":"http://127.0.0.1:8790/"},30)
time.sleep(6)
js("Object.defineProperty(document,'visibilityState',{value:'visible',configurable:true});"
   "if(document.hidden){Object.defineProperty(document,'hidden',{value:false,configurable:true});}"
   "document.dispatchEvent(new Event('visibilitychange'));1")
time.sleep(2)

# 收集 console error（回归期间）
call("Runtime.enable",{},10)
console_errs = []
def drain_events():
    # 简单：依赖 Runtime.consoleAPICalled 事件（未订阅则跳过）
    pass

AUDIT_JS = """(function(){
  function vis(el){ var s=getComputedStyle(el); return s.display!=='none' && s.visibility!=='hidden' && !el.hidden; }
  function txt(el){ return (el.innerText||'').trim().replace(/\\s+/g,' '); }
  var out={hints:[], misaligned:[]};
  var page=document.querySelector('.page.active'); if(!page) return {err:'no active page'};
  page.querySelectorAll('.page-subtitle,.sh-sub,.stat-hint,.stat-sub,.ss-hint,.empty-title,.empty-desc,.card-sub,.schema-banner,.setting-desc,.sheet-desc,.page-note,.hint,.desc').forEach(function(el){
    var t=txt(el);
    if(!t) return;
    if(!vis(el) && el.tagName!=='SPAN') return;
    var cls=(el.className||'').toString().split(' ')[0];
    out.hints.push(el.tagName.toLowerCase()+'.'+cls+' :: '+t);
  });
  page.querySelectorAll('.section-header').forEach(function(h){
    var t=h.querySelector('.sh-title'), a=h.querySelector('.sh-actions');
    if(!t||!a) return;
    var r1=t.getBoundingClientRect(), r2=a.getBoundingClientRect();
    var c1=(r1.top+r1.bottom)/2, c2=(r2.top+r2.bottom)/2;
    var dy=Math.round(c2-c1);
    if(Math.abs(dy)>4){
      out.misaligned.push({title:txt(t).slice(0,30), dy:dy, tTop:Math.round(r1.top), aTop:Math.round(r2.top)});
    }
  });
  return out;
})()"""

pages = ["overview","usage","performance","system","gpu","history","settings","about"]
result = {}
for width, mobile in [(1600, False), (390, True)]:
    call("Emulation.setDeviceMetricsOverride",
         {"width":width,"height":844 if mobile else 1000,"deviceScaleFactor":1,"mobile":mobile},15)
    time.sleep(2)
    result[str(width)] = {}
    for p in pages:
        js("LM.nav.showPage('%s');1" % p)
        time.sleep(3)
        result[str(width)][p] = js(AUDIT_JS)

# 顶部提示检查（概览页）
call("Emulation.setDeviceMetricsOverride",{"width":1600,"height":1000,"deviceScaleFactor":1,"mobile":False},15)
js("LM.nav.showPage('overview');1")
time.sleep(3)
top = js("""(function(){
  var ids=['globalInfobars','remoteBanner','ovAttention','ovLastUpdate'];
  var found=ids.map(function(i){return document.getElementById(i)?i:null;}).filter(Boolean);
  var links=document.querySelectorAll('.ov-page-links');
  return {removedTopIdsStillPresent: found, ovPageLinks: links.length};
})()""")
result["topCheck"] = top

with open(r"C:\Users\mixiaobu\Desktop\ai\LlamaMonitor\artifacts\audit121b.json","w",encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=1)
print("saved audit121b.json")
print("topCheck:", json.dumps(top, ensure_ascii=False))
for w in ["1600","390"]:
    for p in pages:
        d = result[w][p]
        if not isinstance(d, dict): continue
        print("%s %s: hints=%d misaligned=%d" % (w, p, len(d.get("hints",[])), len(d.get("misaligned",[]))))
        for m in d.get("misaligned", []):
            print("   MISALIGNED: title=%r dy=%s" % (m["title"], m["dy"]))
ws.close()
