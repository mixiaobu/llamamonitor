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
call('Page.enable',{},10);call('Runtime.enable',{},10)
call('Page.setWebLifecycleState',{'state':'active'},10)
call('Page.reload',{'ignoreCache':True},15)
for _ in range(30):
    time.sleep(0.5)
    r=call('Runtime.evaluate',{'expression':'!!(window.LM&&LM.app)','returnByValue':True},8)
    if isinstance(r,dict) and r.get('result',{}).get('value'):break
call('Runtime.evaluate',{'expression':"LM.nav.showPage('performance');1",'returnByValue':True},10)
time.sleep(2.5)
for w in [390,320]:
    call('Emulation.setDeviceMetricsOverride',{'width':w,'height':844,'deviceScaleFactor':1.0,'mobile':True},10)
    time.sleep(1.5)
    e=("(function(){var c=document.querySelector('.content');var cw=c.clientWidth;var out=[];"
       "var all=c.querySelectorAll('*');"
       "for(var i=0;i<all.length;i++){var r=all[i].getBoundingClientRect();"
       "if(r.width>0&&r.right>cw+2){var tag=all[i].tagName;var id=all[i].id;var cls=(all[i].className||'').toString().slice(0,30);"
       "out.push(tag+(id?'#'+id:'')+(cls?'.'+cls:'')+' w='+Math.round(r.width)+' r='+Math.round(r.right));}}"
       "return JSON.stringify({w:cw,sw:c.scrollWidth,top:out.slice(0,12)})})()")
    r=call('Runtime.evaluate',{'expression':e,'returnByValue':True},15)
    print(w, json.dumps(r.get('result',{}).get('value','{}'), ensure_ascii=False))
ws.close()
