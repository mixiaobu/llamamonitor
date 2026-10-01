import sys,socket,struct,base64,os,json,time
WS=open('tools/_ws.txt').read().strip()
def _c(u):
    rest=u[5:];h,_,p=rest.partition('/');h,_,port=h.partition(':')
    w=socket.create_connection((h,int(port or 80)));k=base64.b64encode(os.urandom(16)).decode()
    w.sendall(f"GET /{p} HTTP/1.1\r\nHost: {h}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {k}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
    r=b''
    while b"\r\n\r\n" not in r:
        c=w.recv(4096)
        if not c: raise RuntimeError('hf')
        r+=c
    return w
W=_c(WS);M=[0]
def rx(w,n):
    b=b''
    while len(b)<n:
        c=w.recv(n-len(b))
        if not c: raise RuntimeError('cl')
        b+=c
    return b
def rm(w,t):
    w.settimeout(t)
    while True:
        a,b=rx(w,2);op=a&15;mk=b&128;n=b&127
        if n==126: n=struct.unpack('>H',rx(w,2))[0]
        elif n==127: n=struct.unpack('>Q',rx(w,8))[0]
        m=rx(w,4) if mk else None
        d=rx(w,n) if n else b''
        if m: d=bytes(x^m[i%4] for i,x in enumerate(d))
        if op==0x9:
            pm=os.urandom(4);hh=bytearray([0x8A,0x80|len(d)]);hh+=pm
            w.sendall(bytes(hh)+bytes(x^pm[i%4] for i,x in enumerate(d)));continue
        if op in(1,2): return d.decode('utf8','replace')
def cm(mth,p=None,t=20):
    M[0]+=1;i=M[0]
    d=json.dumps({"id":i,"method":mth,"params":p or {}}).encode()
    h=bytearray([0x81]);n=len(d)
    if n<126: h.append(0x80|n)
    elif n<65536: h.append(0x80|126);h+=struct.pack('>H',n)
    else: h.append(0x80|127);h+=struct.pack('>Q',n)
    mk=os.urandom(4);h+=mk
    W.sendall(bytes(h)+bytes(x^mk[i%4] for i,x in enumerate(d)))
    dl=time.time()+t
    while time.time()<dl:
        raw=rm(W,max(0.3,dl-time.time()))
        try: j=json.loads(raw)
        except: continue
        if j.get('id')==i: return j.get('error') if 'error' in j else j.get('result',{})
    return {'_t':1}
def js(e,t=20):
    r=cm('Runtime.evaluate',{'expression':e,'returnByValue':True,'awaitPromise':False},t)
    if 'exceptionDetails' in r: return {'__exc':str(r['exceptionDetails'])[:160]}
    return r.get('result',{}).get('value',r.get('result'))
cm('Page.enable',{},10);cm('Runtime.enable',{},10)
cm('Emulation.setDeviceMetricsOverride',{'width':1920,'height':1080,'deviceScaleFactor':1,'mobile':False},15)
cm('Page.navigate',{'url':'http://127.0.0.1:8790/'},30);time.sleep(6)
print('click sidebar:', js("(function(){var b=document.querySelector('.nav-item[data-page=\\\"history\\\"]');if(b){b.click();return 'ok';}return 'nf';})()"))
for k in range(16):
    time.sleep(3)
    s=js("(function(){return 'g='+document.querySelectorAll('#gapsTbody tr.gap-row').length+' e='+document.querySelectorAll('#eventsTbody tr.ev-row').length+' c='+document.getElementById('gapsCountLabel').textContent;})()")
    print('t%03d %s'%(k*3,s))
    if s.startswith('g=0'): continue
    break
W.close()
