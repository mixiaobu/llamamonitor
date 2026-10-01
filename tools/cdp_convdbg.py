# 调试 convertFromPixel 返回值 + 容器 click 是否触发 handler
import sys, socket, struct, base64, os, json, time
WS=open('tools/_ws.txt').read().strip()
def _connect(url):
    rest=url[len("ws://"):]; host,_,path=rest.partition("/"); host,_,port=host.partition(":")
    w=socket.create_connection((host,int(port or 80))); key=base64.b64encode(os.urandom(16)).decode()
    w.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    resp=b""
    while b"\r\n\r\n" not in resp:
        c=w.recv(4096)
        if not c: raise RuntimeError("hf")
        resp+=c
    return w
WS_=_connect(WS); _mid=[0]
def _rx(w,n):
    buf=b""
    while len(buf)<n:
        c=w.recv(n-len(buf))
        if not c: raise RuntimeError("cl")
        buf+=c
    return buf
def read_msg(w,timeout):
    w.settimeout(timeout)
    while True:
        b0,b1=_rx(w,2); op=b0&0x0F; masked=b1&0x80; n=b1&0x7F
        if n==126: n=struct.unpack(">H",_rx(w,2))[0]
        elif n==127: n=struct.unpack(">Q",_rx(w,8))[0]
        mask=_rx(w,4) if masked else None
        data=_rx(w,n) if n else b""
        if mask: data=bytes(b^mask[i%4] for i,b in enumerate(data))
        if op==0x9:
            pm=os.urandom(4); h=bytearray([0x8A,0x80|len(data)]); h+=pm
            w.sendall(bytes(h)+bytes(b^pm[i%4] for i,b in enumerate(data))); continue
        if op in (0x1,0x2): return data.decode("utf8","replace")
def call(method,params=None,timeout=20):
    _mid[0]+=1; i=_mid[0]
    data=json.dumps({"id":i,"method":method,"params":params or {}}).encode()
    h=bytearray([0x81]); n=len(data)
    if n<126: h.append(0x80|n)
    elif n<65536: h.append(0x80|126); h+=struct.pack(">H",n)
    else: h.append(0x80|127); h+=struct.pack(">Q",n)
    mask=os.urandom(4); h+=mask
    WS_.sendall(bytes(h)+bytes(b^mask[i%4] for i,b in enumerate(data)))
    deadline=time.time()+timeout
    while time.time()<deadline:
        raw=read_msg(WS_,max(0.3,deadline-time.time()))
        try: j=json.loads(raw)
        except Exception: continue
        if j.get("id")==i: return j.get("error") if "error" in j else j.get("result",{})
    return {"_timeout":True}
def js(expr,timeout=20):
    r=call("Runtime.evaluate",{"expression":expr,"returnByValue":True,"awaitPromise":False},timeout)
    if "exceptionDetails" in r: return {"__exc":str(r["exceptionDetails"])[:300]}
    return r.get("result",{}).get("value",r.get("result"))
call("Runtime.enable",{},10)
print("page:", js("LM.nav.currentPage()"), "gaps:", js("document.querySelectorAll('#gapsTbody tr.gap-row').length"))
# 手动复算 convertFromPixel
print("convertFromPixel test:", js("""(function(){
  var box=document.getElementById('chartHistoryTrend');var r=box.getBoundingClientRect();
  var c=LM.charts.chart('chartHistoryTrend');
  var out={};
  // 试几个横向分数
  [0.2,0.5,0.8].forEach(function(f){
    var px=[r.width*f, r.height*0.5];
    var v1=c.convertFromPixel({xAxisIndex:0},px);
    var v2=c.convertFromPixel({seriesIndex:0},px);
    out[f]={xAxis:v1, series:v2};
  });
  // convertToPixel 验证：桶3 应在哪
  out.pt3=c.convertToPixel({xAxisIndex:0},3);
  out.pt0=c.convertToPixel({xAxisIndex:0},0);
  out.pt7=c.convertToPixel({xAxisIndex:0},7);
  return out;
})()"""))
# 手动触发容器 click（模拟我的 handler 逻辑）
print("manual handler:", js("""(function(){
  var box=document.getElementById('chartHistoryTrend');var r=box.getBoundingClientRect();
  var c=LM.charts.chart('chartHistoryTrend');
  var px=[r.width*0.5, r.height*0.5];
  var val=c.convertFromPixel({xAxisIndex:0},px);
  var x=Array.isArray(val)?val[0]:val;
  var all=LM.state.historyTrend?LM.state.historyTrend.points:[];
  var allLen=all.length;
  // LM.state 可能不可见；改用 window 上的？这里直接看 all
  return JSON.stringify({val:val, x:x, allLen:allLen, rounded:Math.round(typeof x==='number'?x:-99), inRange:(typeof x==='number'&&Math.round(x)>=0&&Math.round(x)<allLen)});
})()"""))
WS_.close()
