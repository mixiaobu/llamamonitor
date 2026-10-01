#!/usr/bin/env python3
"""CSSOM 扫描：打印所有匹配给定 class 且设置 grid-template-columns 的规则
（含所在 @media + 来源文件 + 顺序）。用法: cdp_ovr_cssom.py <ws> <w> <h> <dpr>"""
import sys, socket, struct, base64, os, json, time
WS = sys.argv[1]; W=int(sys.argv[2]); H=int(sys.argv[3]); DPR=float(sys.argv[4])

def send(ws, obj):
    data = json.dumps(obj).encode("utf-8")
    h=bytearray([0x81]); n=len(data)
    if n<126: h.append(0x80|n)
    elif n<65536: h.append(0x80|126); h+=struct.pack(">H",n)
    else: h.append(0x80|127); h+=struct.pack(">Q",n)
    m=os.urandom(4); h+=m
    ws.sendall(bytes(h)+bytes(b^m[i%4] for i,b in enumerate(data)))
def _rx(ws,n):
    buf=b""
    while len(buf)<n:
        c=ws.recv(n-len(buf))
        if not c: raise RuntimeError("closed")
        buf+=c
    return buf
def read_msg(ws):
    while True:
        b0,b1=_rx(ws,2); op=b0&0x0F; masked=b1&0x80; n=b1&0x7F
        if op==0x9:
            pm=os.urandom(4); hh=bytearray([0x8A,0x80|n]); hh+=pm
            ws.sendall(bytes(hh)+bytes(b^pm[i%4] for i,b in enumerate(_rx(ws,n)))); continue
        if n==126: n=struct.unpack(">H",_rx(ws,2))[0]
        elif n==127: n=struct.unpack(">Q",_rx(ws,8))[0]
        mask=_rx(ws,4) if masked else None
        data=_rx(ws,n) if n else b""
        if mask: data=bytes(b^mask[i%4] for i,b in enumerate(data))
        if op in (0x1,0x2): return data.decode("utf-8","replace")
        if op==0x8: raise RuntimeError("close")

def main():
    rest=WS[len("ws://"):]; host,_,path=rest.partition("/"); host,_,port=host.partition(":")
    ws=socket.create_connection((host,int(port or 80))); ws.settimeout(3)
    key=base64.b64encode(os.urandom(16)).decode()
    ws.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    resp=b""
    while b"\r\n\r\n" not in resp:
        c=ws.recv(4096)
        if not c: raise RuntimeError("hs")
        resp+=c
    mid=[0]
    def call(method,params=None,timeout=10):
        mid[0]+=1; i=mid[0]; send(ws,{"id":i,"method":method,"params":params or {}})
        end=time.time()+timeout
        while time.time()<end:
            try: raw=read_msg(ws)
            except socket.timeout: continue
            except Exception: return None
            try: j=json.loads(raw)
            except Exception: continue
            if j.get("id")==i:
                r=j.get("result",{})
                if "result" in r and isinstance(r["result"],dict) and "value" in r["result"]: return r["result"]["value"]
                return r
        return None
    call("Runtime.enable"); call("Page.enable")
    call("Emulation.setDeviceMetricsOverride",{"width":W,"height":H,"deviceScaleFactor":DPR,"mobile":False})
    call("Page.navigate",{"url":"http://127.0.0.1:8790/"})
    time.sleep(2.0)
    probe=(
      "(function(){"
      "var CLS=['ov-today-breakdown','ov-integrity-grid','ov-inference-grid','ov-host-grid','gpu-mini-grid','stat-grid','today-breakdown','host-grid'];"
      "var out=[];var idx=0;"
      "function scan(rules,media,src){for(var i=0;i<rules.length;i++){var r=rules[i];"
      "var nm=r.conditionText?(' ['+r.conditionText+']'):'';"
      "if(r.cssRules){scan(r.cssRules,(media||'')+nm,src);}"
      "var st=r.style;var gtc='';"
      "for(var p=0;p<st.length;p++){if(st[p].indexOf('grid-template-columns')===0){gtc=st[p].replace('grid-template-columns:','').trim();}}"
      "if(!gtc)continue;var sel=r.selectorText||'';"
      "if(CLS.some(function(c){return sel.indexOf(c)!==-1;})){"
      "out.push('#'+(idx++)+' '+src+' '+(media||'(top)')+' :: '+sel+'  =>  '+gtc);}}}"
      "for(var s=0;s<document.styleSheets.length;s++){try{scan(document.styleSheets[s].cssRules,'',(document.styleSheets[s].href||'inline').split('/').pop());}catch(e){}}"
      "out.sort(function(a,b){var na=parseInt(a.replace(/^#/,'')),nb=parseInt(b.replace(/^#/,''));return na-nb;});"
      "return out.join('\\n');})()"
    )
    res = call("Runtime.evaluate",{"expression":probe,"returnByValue":True})
    print(res if isinstance(res,str) else json.dumps(res,ensure_ascii=False))

if __name__=="__main__": main()
