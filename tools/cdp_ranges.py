import sys, socket, struct, base64, os, json, time
def send(ws,o):
    d=json.dumps(o).encode();h=bytearray([0x81]);n=len(d)
    if n<126:h.append(0x80|n)
    else:
        h.append(0x80|126);h.extend(struct.pack('>H',n))
    m=os.urandom(4);h+=m;ws.sendall(bytes(h)+bytes(b^m[i%4] for i,b in enumerate(d)))
def recv(ws,n):
    b=b''
    while len(b)<n:
        b+=ws.recv(n-len(b))
    return b
def rm(ws,t):
    ws.settimeout(t)
    while True:
        a,b=recv(ws,2);op=a&15;m=b&128;n=b&127
        if n==126:n=struct.unpack('>H',recv(ws,2))[0]
        elif n==127:n=struct.unpack('>Q',recv(ws,8))[0]
        mk=recv(ws,4) if m else None;data=recv(ws,n) if n else b''
        if mk:data=bytes(x^mk[i%4] for i,x in enumerate(data))
        if op==9:
            pm=os.urandom(4);h=bytearray([0x8A,0x80|len(data)]);h+=pm;ws.sendall(bytes(h)+bytes(x^pm[i%4] for i,x in enumerate(data)));continue
        if op in(1,2):return data.decode('utf-8','replace')
url=sys.argv[1];rest=url[5:];host,_,path=rest.partition('/');host,_,port=host.partition(':')
ws=socket.create_connection((host,int(port)))
k=base64.b64encode(os.urandom(16)).decode()
ws.sendall(('GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n'%(path,host,k)).encode())
r=b''
while b'\r\n\r\n' not in r:r+=ws.recv(4096)
mid=[0]
def call(m,p=None,t=25):
    mid[0]+=1;i=mid[0];send(ws,{'id':i,'method':m,'params':p or {}});dl=time.time()+t
    while time.time()<dl:
        try:raw=rm(ws,dl-time.time())
        except: return {}
        try:j=json.loads(raw)
        except:continue
        if j.get('id')==i:return j.get('result',{})
def ev(expr):
    r=call('Runtime.evaluate',{'expression':expr,'returnByValue':True},20)
    return r.get('result',{}).get('value')
call('Page.enable',{},10);call('Runtime.enable',{},10)
call('Page.setWebLifecycleState',{'state':'active'},10)
call('Emulation.setDeviceMetricsOverride',{'width':1440,'height':900,'deviceScaleFactor':1.0,'mobile':False},10)
call('Page.reload',{'ignoreCache':True},15)
for _ in range(30):
    time.sleep(0.5)
    if ev('!!(window.LM&&LM.app)'):break
ev("LM.nav.showPage('performance')")
time.sleep(3)

def click_seg(seg_id, label):
    # find button whose text == label, click it
    expr=("(function(){var s=document.getElementById('"+seg_id+"');if(!s)return 'noseg';"
          "var b=Array.prototype.filter.call(s.querySelectorAll('button'),function(x){return x.textContent.trim()==='"+label+"';})[0];"
          "if(!b)return 'nobtn'; b.click(); return 'clicked';})()")
    return ev(expr)

def seg_state(seg_id):
    return ev("(function(){var s=document.getElementById('"+seg_id+"');if(!s)return '?';"
              "var b=s.querySelector('[aria-pressed=true]');return b?b.textContent.trim():'?';})()")

print("=== Throughput range ===")
for lbl in ["15 分钟","1 小时","6 小时","24 小时"]:
    print(click_seg("perfThroughputRange",lbl), "-> pressed:", seg_state("perfThroughputRange"),
          "| windowAvg:", (ev("document.getElementById('perfWindowAvg')?document.getElementById('perfWindowAvg').textContent.trim():'?'")))
    time.sleep(1.6)

print("=== MTP range ===")
for lbl in ["今天","7 天","30 天","全部"]:
    click_seg("perfMtpRange",lbl)
    time.sleep(3.5)  # 充分等待 fetch 落地（d.range 驱动 sub 与指标同源，必然一致）
    print(lbl, "-> pressed:", seg_state("perfMtpRange"),
          "| accept:", (ev("document.getElementById('mtpAcceptRate')?document.getElementById('mtpAcceptRate').textContent.trim():'?'")),
          "| draft:", (ev("document.getElementById('mtpDraft')?document.getElementById('mtpDraft').textContent.trim():'?'")),
          "| trendSub:", (ev("document.getElementById('mtpTrendSub')?document.getElementById('mtpTrendSub').textContent.trim():'?'")))

print("=== History link + freshness ===")
print("historyLink hidden:", ev("(function(){var e=document.getElementById('perfHistoryLink');return e?String(e.hidden):'?';})()"))
print("freshness text:", ev("(function(){var e=document.getElementById('perfFreshness');return e?e.textContent.trim():'?';})()"))
print("freshness class:", ev("document.getElementById('perfFreshness')?document.getElementById('perfFreshness').className:'?'"))
print("ctxBar width:", ev("(function(){var b=document.getElementById('rtCurrentCtxBar');return b?getComputedStyle(b,'::after').width:'?';})()"))
ws.close()
print("DONE")
