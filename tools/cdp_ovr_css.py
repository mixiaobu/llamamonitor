#!/usr/bin/env python3
"""CDP CSS.getMatchedStylesForNode：打印某元素的真实匹配规则（含来源 url + 关键值）。
用法: cdp_ovr_css.py <ws> <w> <h> <dpr> <selector>"""
import sys, socket, struct, base64, os, json, time
WS = sys.argv[1]; W=int(sys.argv[2]); H=int(sys.argv[3]); DPR=float(sys.argv[4]); SEL = sys.argv[5]

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
            if j.get("id")==i: return j.get("result",{})
        return None
    call("Runtime.enable"); call("Page.enable"); call("Emulation.enable"); call("DOM.enable"); call("CSS.enable")
    call("Emulation.setDeviceMetricsOverride",{"width":W,"height":H,"deviceScaleFactor":DPR,"mobile":False})
    call("Page.navigate",{"url":"http://127.0.0.1:8790/"})
    time.sleep(2.5)
    call("Runtime.evaluate",{"expression":"try{LM.nav.showPage('overview')}catch(e){};1","returnByValue":True})
    time.sleep(1.0)
    doc = call("DOM.getDocument",{"depth":-1})
    root = (doc or {}).get("root",{}).get("nodeId")
    q = call("DOM.querySelector",{"nodeId":root,"selector":SEL})
    nid = (q or {}).get("nodeId")
    if not nid:
        print("element not found:", SEL); return
    m = call("CSS.getMatchedStylesForNode",{"nodeId":nid},timeout=20) or {}
    # print matched rules with grid-template-columns
    for kind in ("matched_css_rules","implicit_matched_css_rules","inherited_pseudo_matches"):
        pass
    print("== matched rules setting grid-template-columns ==")
    for r in (m.get("matchedCSSRules") or []):
        rule = r.get("rule",{})
        text = rule.get("selectorList",{}).get("text","")
        decls = [d.get("text","") for d in (rule.get("style",{}) or {}).get("shorthandEntries",[])]
        decls2 = [str(d.get("property") or "")+": "+str(d.get("value")) for d in (rule.get("style",{}) or {}).get("cssProperties",[])]
        joined = " | ".join(decls) + " || " + " ; ".join(d for d in decls2 if d)
        if "grid-template" in joined or "grid_template" in joined:
            origin = rule.get("origin","")
            print(f"[{origin}] {text}  =>  {joined[:400]}")
    # also inline style
    inline = m.get("inlineStyle",{})
    if inline:
        print("[inline]", " ; ".join(d.get("property")+": "+str(d.get("value")) for d in (inline.get("cssProperties",[]))))

if __name__=="__main__": main()
