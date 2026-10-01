#!/usr/bin/env python3
"""
cdp_perf_audit.py — Round 5 推理性能页 DOM/CSS 视觉审计（Edge CDP）。
程序化"视觉审查"：对每个视口检查横向溢出、Section 顺序、Slot 表 idle 语义、
图表初始化、数据落位、undefined/NaN 残留、mobile card 模式、字号层级。
Usage: cdp_perf_audit.py <ws> [--sizes a,b,c]
"""
import sys, socket, struct, base64, os, json, time

def send(ws, obj):
    data = json.dumps(obj).encode("utf-8")
    header = bytearray([0x81]); n = len(data)
    if n < 126: header.append(0x80 | n)
    elif n < 65536: header.append(0x80 | 126); header += struct.pack(">H", n)
    else: header.append(0x80 | 127); header += struct.pack(">Q", n)
    mask = os.urandom(4); header += mask
    ws.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

def _recv_exact(ws, n):
    buf = b""
    while len(buf) < n:
        c = ws.recv(n - len(buf))
        if not c: raise RuntimeError("ws closed")
        buf += c
    return buf

def read_msg(ws, timeout):
    ws.settimeout(timeout)
    while True:
        b0, b1 = _recv_exact(ws, 2)
        op = b0 & 0x0F; masked = b1 & 0x80; n = b1 & 0x7F
        if n == 126: n = struct.unpack(">H", _recv_exact(ws, 2))[0]
        elif n == 127: n = struct.unpack(">Q", _recv_exact(ws, 8))[0]
        mask = _recv_exact(ws, 4) if masked else None
        data = _recv_exact(ws, n) if n else b""
        if mask: data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if op == 0x9:
            pm = os.urandom(4); h = bytearray([0x8A, 0x80 | len(data)])
            h += pm; ws.sendall(bytes(h) + bytes(b ^ pm[i % 4] for i, b in enumerate(data))); continue
        if op in (0x1, 0x2): return data.decode("utf-8", "replace")

JS = r"""
(function(){
  function t(id){var e=document.getElementById(id);return e?e.textContent.trim():null;}
  function num(id){var v=t(id);return (v==null||v==='')?'NA':v;}
  function rect(id){var e=document.getElementById(id);if(!e)return null;var r=e.getBoundingClientRect();return {x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)};}
  var content=document.querySelector('.content');
  var overflow=(content?content.scrollWidth-content.clientWidth:0);
  // 横向溢出的元素（最右边界超出视口 >2px）
  var viol=[];
  if(content){var cw=content.clientWidth;var all=content.querySelectorAll('*');
    for(var i=0;i<all.length;i++){var r=all[i].getBoundingClientRect();
      if(r.width>0&&r.right>cw+2&&r.width<cw+40){viol.push((all[i].id||all[i].className||all[i].tagName)+' r='+Math.round(r.right));}
      if(viol.length>6)break;}}
  // Section 顺序（performance 页内 sh-title 文本序列）
  var page=document.getElementById('page-performance');
  var shs=page?Array.prototype.map.call(page.querySelectorAll('.sh-title'),function(e){return e.textContent.replace(/\s+/g,' ').trim();}):[];
  // Slot 表
  var slotRows=page?page.querySelectorAll('.slot-row').length:0;
  var slotIdle=page?page.querySelectorAll('.slot-row.idle').length:0;
  var slotActive=page?page.querySelectorAll('.slot-row.active').length:0;
  var slotHead=page?page.querySelectorAll('.slot-table thead th').length:0;
  var slotDisplay=page?(function(){var tb=page.querySelector('.slot-table');return tb?getComputedStyle(tb).display:'?';})():'?';
  // 图表初始化（有 canvas = 已 render，不是空态）
  function chartInit(id){var e=document.getElementById(id);if(!e)return '?';return e.querySelector('canvas')?'init':'empty';}
  // 残留 undefined/NaN
  var junk=[];
  if(page){var txts=page.querySelectorAll('.stat-value,.ps-value,.stat-value small,.ms-value,td');
    for(var j=0;j<txts.length;j++){var s=txts[j].textContent;
      if(/undefined|NaN|Infinity|null/.test(s))junk.push((txts[j].id||txts[j].className)+':'+s.trim().slice(0,20));
      if(junk.length>6)break;}}
  return JSON.stringify({
    width: innerWidth, overflowPx: overflow, viol: viol,
    sectionOrder: shs,
    slot: {rows:slotRows, idle:slotIdle, active:slotActive, headCols:slotHead, display:slotDisplay},
    charts: {tps:chartInit('chartTps'), mtp:chartInit('chartMtp'), mtpPos:chartInit('chartMtpPos')},
    fresh: t('perfFreshness'),
    summary: {promptTps:t('perfPromptTps'), decodeTps:t('perfDecodeTps'), processing:t('perfProcessing'), queued:t('perfQueued'), activeSlots:t('perfActiveSlots')},
    runtime: {slots:t('rtSlots'), busy:t('rtBusySlots'), seq:t('rtCurrentSeq'), tokenMax:t('rtTokenMax'), ctxMax:t('rtContextMax')},
    mtp: {accept:t('mtpAcceptRate'), draft:t('mtpDraft'), accepted:t('mtpAccepted'), seqs:t('mtpSeqs'), avgDraft:t('mtpAvgDraft'), avgAcc:t('mtpAvgAccepted'), state:t('mtpStateNote'), trendSub:t('mtpTrendSub')},
    model: {alias:t('llmAlias'), ftype:t('llmFtype'), params:t('llmParams'), size:t('llmSize'), ctx:t('llmContext'), slots:t('llmSlots'), modal:t('llmModal'), build:t('llmBuild')},
    windowAvg: t('perfWindowAvg'),
    junk: junk
  });
})()
"""

def main():
    if len(sys.argv) < 2:
        print("usage: cdp_perf_audit.py <ws> [--sizes a,b]", file=sys.stderr); sys.exit(2)
    url = sys.argv[1]; sizes = None
    i = 2
    while i < len(sys.argv):
        if sys.argv[i] == "--sizes": sizes = sys.argv[i+1].split(","); i += 2
        else: i += 1
    matrix = sizes or ["1920x1080","1366x768","988x1394","900x900","768x1024","760x900","430x932","390x844","360x800","320x568"]

    rest = url[len("ws://"):]; host,_,path = rest.partition("/"); host,_,port = host.partition(":")
    ws = socket.create_connection((host, int(port or 80)))
    key = base64.b64encode(os.urandom(16)).decode()
    ws.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        c = ws.recv(4096)
        if not c: raise RuntimeError("handshake failed")
        resp += c
    mid=[0]
    def call(method, params=None, timeout=40):
        mid[0]+=1;i=mid[0]
        send(ws, {"id":i,"method":method,"params":params or {}})
        deadline=time.time()+timeout
        while time.time()<deadline:
            try: raw=read_msg(ws,max(0.5,deadline-time.time()))
            except Exception: return {"_timeout":True}
            try: j=json.loads(raw)
            except Exception: continue
            if j.get("id")==i: return j.get("error") if "error" in j else j.get("result",{})
        return {"_timeout":True}
    call("Page.enable",{},10); call("Runtime.enable",{},10)
    call("Page.setWebLifecycleState",{"state":"active"},10)
    call("Page.reload",{"ignoreCache":True},15)
    for _ in range(30):
        time.sleep(0.5)
        r=call("Runtime.evaluate",{"expression":"!!(window.LM&&LM.app)","returnByValue":True},8)
        if isinstance(r,dict) and r.get("result",{}).get("value"): break
    call("Runtime.evaluate",{"expression":"LM.nav.showPage('performance');1","returnByValue":True},10)
    time.sleep(2.5)
    for spec in matrix:
        w,h=(int(x) for x in spec.split("x"))
        # fresh load at each size（真实用户在该宽度首次加载；避免 resize-down 的
        # ECharts canvas 残留桌面宽度造成假溢出）
        call("Emulation.setDeviceMetricsOverride",{"width":w,"height":h,"deviceScaleFactor":1.0,"mobile":w<=760},10)
        call("Page.reload",{"ignoreCache":True},15)
        for _ in range(30):
            time.sleep(0.5)
            r=call("Runtime.evaluate",{"expression":"!!(window.LM&&LM.app)","returnByValue":True},8)
            if isinstance(r,dict) and r.get("result",{}).get("value"):break
        call("Runtime.evaluate",{"expression":"LM.nav.showPage('performance');1","returnByValue":True},10)
        time.sleep(2.4)
        r=call("Runtime.evaluate",{"expression":JS,"returnByValue":True},15)
        val=r.get("result",{}).get("value","{}")
        try: rep=json.loads(val)
        except Exception: rep={"parse_error":val}
        print("="*70)
        print(spec, "mobile=%s"%(w<=760))
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    ws.close()
    print("AUDIT DONE")

if __name__=="__main__":
    main()
