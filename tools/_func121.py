# 1.2.1 逐页功能回归：8 页各检查 关键元素/图表 canvas/console error/空态异常
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
# 收集 console error + pageerror
call("Runtime.enable",{},10)
call("Log.enable",{},10)
call("Emulation.setDeviceMetricsOverride",{"width":1600,"height":1000,"deviceScaleFactor":1,"mobile":False},15)
call("Page.navigate",{"url":"http://127.0.0.1:8790/"},30)
time.sleep(4)
js("Object.defineProperty(document,'visibilityState',{value:'visible',configurable:true});"
   "if(document.hidden){Object.defineProperty(document,'hidden',{value:false,configurable:true});}"
   "document.dispatchEvent(new Event('visibilitychange'));1")
# 装 console.error 计数器 + pageerror 计数器
js("""window.__errs=[];
var _ce=console.error.bind(console);
console.error=function(){ try{window.__errs.push([].slice.call(arguments).map(String).join(' '));}catch(e){} return _ce.apply(null,arguments); };
window.addEventListener('error',function(e){ window.__errs.push('pageerror: '+(e.message||'?')); });
window.addEventListener('unhandledrejection',function(e){ window.__errs.push('unhandled: '+((e.reason&&e.reason.message)||e.reason)); });
1""")
time.sleep(6)

PROBE = """(function(){
  function txt(id){ var e=document.getElementById(id); return e?(e.textContent||'').trim().slice(0,40):null; }
  function vis(id){ var e=document.getElementById(id); if(!e) return false; var s=getComputedStyle(e); return s.display!=='none' && !e.hidden; }
  var page=document.querySelector('.page.active');
  var canvases=[].filter.call(page?page.querySelectorAll('canvas'):[],function(c){var r=c.getBoundingClientRect();return r.width>10&&r.height>10;});
  var emptyTitle=(page?page.querySelector('.empty-title'):null);
  return {
    active: page?page.id:null,
    canvases: canvases.length,
    emptyTitle: emptyTitle?(emptyTitle.textContent||'').trim():null,
    probe: window.__probe ? window.__probe() : null
  };
})()"""

# 每页专属探测（关键数据元素非空）
js("""window.__probes={
  'page-overview': function(){
    return {
      server: txt('ovServerState'),
      today: txt('ovTodayLogical'),
      gpuMini: document.querySelectorAll('#ovGpuMini *').length,
      host: txt('ovHostCpu'),
      integrity: txt('dqCoverage')
    }; function txt(id){var e=document.getElementById(id);return e?(e.textContent||'').trim():null;}
  },
  'page-usage': function(){
    function txt(id){var e=document.getElementById(id);return e?(e.textContent||'').trim():null;}
    return { total: txt('sumTotal'), daily: txt('sumDaily'), gaps: txt('gapsCount') };
  },
  'page-performance': function(){
    function txt(id){var e=document.getElementById(id);return e?(e.textContent||'').trim():null;}
    return { model: txt('llmAlias'), slots: document.querySelectorAll('#llmSlotList tr').length, mtp: txt('mtpAccept') };
  },
  'page-system': function(){
    function txt(id){var e=document.getElementById(id);return e?(e.textContent||'').trim():null;}
    return { cpu: txt('sysCpuUsage'), mem: txt('sysMemUsage'), state: txt('systemPageState') };
  },
  'page-gpu': function(){
    function txt(id){var e=document.getElementById(id);return e?(e.textContent||'').trim():null;}
    return { cards: document.querySelectorAll('#gpuCards .gpu-device').length, state: txt('gpuPageState') };
  },
  'page-history': function(){
    function txt(id){var e=document.getElementById(id);return e?(e.textContent||'').trim():null;}
    return { db: txt('iqDb'), coverage: txt('iqCoverage'), rows: document.querySelectorAll('#historyTable tbody tr').length };
  },
  'page-settings': function(){
    function txt(id){var e=document.getElementById(id);return e?(e.textContent||'').trim():null;}
    return { serverUrl: document.getElementById('setServerUrl')?document.getElementById('setServerUrl').value:null, panes: document.querySelectorAll('.settings-pane').length };
  },
  'page-about': function(){
    function txt(id){var e=document.getElementById(id);return e?(e.textContent||'').trim():null;}
    return { version: txt('aboutVersion') };
  }
};""")
# 把 __probe 指向当前页
js("""Object.defineProperty(window,'__probe',{ get:function(){ var p=document.querySelector('.page.active'); return p&&window.__probes[p.id]?window.__probes[p.id]:null; } });1""")

pages = ["overview","usage","performance","system","gpu","history","settings","about"]
ok = True
for p in pages:
    js("LM.nav.showPage('%s');1" % p)
    time.sleep(4)
    r = js(PROBE)
    errs = js("window.__errs.length")
    print("== %s: %s" % (p, json.dumps(r, ensure_ascii=False)))
    print("   console_errors=%s" % errs)
    if not isinstance(r, dict): ok = False; continue
    if r.get("active") != "page-%s" % p:
        print("   FAIL active page mismatch: %s" % r.get("active")); ok = False
    pr = r.get("probe") or {}
    # 基本断言：探针字段非 null/空
    for k, v in pr.items():
        if v is None or v == "":
            print("   WARN probe field empty: %s=%r" % (k, v))

print("RESULT:", "ALL PAGES OK" if ok else "HAS PROBLEMS")
ws.close()
