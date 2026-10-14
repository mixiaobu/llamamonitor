# 1.2.1 失败容忍模拟：CDP 猴补 window.fetch 让全部 /api/* 先失败 ~2 轮（状态不变、数据保留），
# 再失败到 >=5 轮（状态切换），恢复后回正。轮询 2s/轮。
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
call("Emulation.setDeviceMetricsOverride",{"width":1600,"height":1000,"deviceScaleFactor":1,"mobile":False},15)
call("Page.navigate",{"url":"http://127.0.0.1:8790/"},30)
time.sleep(3)
js("Object.defineProperty(document,'visibilityState',{value:'visible',configurable:true});"
   "if(document.hidden){Object.defineProperty(document,'hidden',{value:false,configurable:true});}"
   "document.dispatchEvent(new Event('visibilitychange'));1")
time.sleep(5)  # 等首屏 + 几轮正常轮询

def badge(el_id):
    return js("(function(){var e=document.getElementById('%s');if(!e)return null;"
              "var t=e.querySelector('.status-text');return (t?t.textContent:e.textContent||'').trim();})()" % el_id)

def snapshot(stage):
    s = {
        "stage": stage,
        "ovServerState": badge("ovServerState"),
        "ovGpuState": badge("ovGpuState"),
        "systemPageState": badge("systemPageState"),
        "gpuPageState": badge("gpuPageState"),
        "gpuCards": js("document.querySelectorAll('#gpuCards .gpu-device').length"),
        "ovGpuMini": js("document.querySelectorAll('#ovGpuMini .ov-gpu-mini, #ovGpuMini [class*=mini]').length"),
        "gpuStatusFailures": js("(window.__faultN&&window.__faultN['/api/gpu/status'])||0"),
        "statusFailures": js("(window.__faultN&&window.__faultN['/api/status'])||0"),
    }
    print(json.dumps(s, ensure_ascii=False))
    return s

# 安装 fetch 猴补（失败模式）
js("""(function(){
  if(window.__faultInstalled) return 'already';
  window.__faultInstalled = true;
  window.__faultMode = false;
  window.__faultN = {};
  var orig = window.fetch;
  window.fetch = function(input){
    var url = (typeof input === 'string') ? input : (input && input.url) || '';
    if(window.__faultMode && url.indexOf('/api/') === 0){
      var p = url.split('?')[0];
      window.__faultN[p] = (window.__faultN[p]||0)+1;
      return Promise.reject(new Error('simulated fault: '+p));
    }
    return orig.apply(window, arguments);
  };
  return 'installed';
})()""")

print("== baseline（正常轮询 3 轮后）==")
time.sleep(6)
baseline = snapshot("baseline")

print("== 故障开始：失败 ~2 轮（<5，状态应不变）==")
js("window.__faultMode=true;1")
time.sleep(5)   # ~2 轮
a = snapshot("fault-2rounds")

print("== 继续失败到 >=5 轮（状态应切换）==")
time.sleep(18)  # 再 ~7-8 轮（确保 >=5 次失败）
b = snapshot("fault-5plus")

print("== 恢复（成功即回正）==")
js("window.__faultMode=false;1")
time.sleep(7)   # 1-2 轮成功
c = snapshot("recovered")

ok = True
def chk(cond, msg):
    global ok
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond: ok = False

chk(baseline["ovGpuState"] and baseline["gpuCards"] >= 1, "基线：GPU 数据正常加载")
chk(a["ovServerState"] == baseline["ovServerState"], "2 轮失败：服务状态不变（%r）" % a["ovServerState"])
chk(a["ovGpuState"] == baseline["ovGpuState"], "2 轮失败：GPU 状态不变（%r）" % a["ovGpuState"])
chk(a["gpuCards"] == baseline["gpuCards"], "2 轮失败：GPU 卡片数据保留（%s 张）" % a["gpuCards"])
chk(a["systemPageState"] == baseline["systemPageState"], "2 轮失败：系统状态不变（%r）" % a["systemPageState"])
chk(b["statusFailures"] >= 5, "前置：/api/status 失败次数确实 >=5（%s）" % b["statusFailures"])
chk(b["gpuStatusFailures"] >= 5, "前置：/api/gpu/status 失败次数确实 >=5（%s）" % b["gpuStatusFailures"])
chk(b["ovServerState"] == "不可达", "≥5 轮失败：服务状态切「不可达」（%r）" % b["ovServerState"])
chk(b["gpuPageState"] == "不可用", "≥5 轮失败：GPU 页切「不可用」（%r）" % b["gpuPageState"])
# 概览 GPU 迷你卡：后端全挂时 /api/overview 持续失败，1.2.1 起 overview 的
# catch 在 GPU 连续 >=5 次失败后兜底切「不可用」（与 GPU 页徽章同步）
chk(b["ovGpuState"] == "不可用", "≥5 轮失败：概览 GPU 切「不可用」（%r）" % b["ovGpuState"])
chk(b["systemPageState"] == "采集暂停", "≥5 轮失败：系统切「采集暂停」（%r）" % b["systemPageState"])
chk(c["ovServerState"] == baseline["ovServerState"], "恢复：服务状态回正（%r）" % c["ovServerState"])
chk(c["ovGpuState"] == baseline["ovGpuState"], "恢复：GPU 状态回正（%r）" % c["ovGpuState"])
chk(c["gpuCards"] == baseline["gpuCards"], "恢复：GPU 卡片数据恢复（%s 张）" % c["gpuCards"])
chk(c["systemPageState"] == baseline["systemPageState"], "恢复：系统状态回正（%r）" % c["systemPageState"])

# 清理猴补
js("""(function(){
  if(!window.__faultInstalled) return 'clean';
  window.__faultMode = false;
  return 'left-installed-ok';
})()""")

print("RESULT:", "ALL PASS" if ok else "HAS FAILURES")
ws.close()
