#!/usr/bin/env python3
"""Round-8 Mobile 审计：指定视口 + 页面，输出 Top/Middle/Bottom 视口截图 + 溢出报告 + console 错误。
用法: cdp_m_audit.py <ws> <w> <h> <page> <outdir>
- 每次 fresh navigate（真实加载）
- 捕获 console error / exception / unhandledrejection（10s 窗口）
- 测量 App 级横向滚动 + 溢出元素 + Bottom Nav 遮挡 + touch target
- 截图: <page>-top.png / <page>-mid.png / <page>-bottom.png / <page>-full.png
"""
import sys, socket, struct, base64, os, json, time

def send(ws, obj):
    data = json.dumps(obj).encode("utf-8"); h = bytearray([0x81]); n = len(data)
    if n < 126: h.append(0x80 | n)
    elif n < 65536: h.append(0x80 | 126); h += struct.pack(">H", n)
    else: h.append(0x80 | 127); h += struct.pack(">Q", n)
    m = os.urandom(4); h += m
    ws.sendall(bytes(h) + bytes(b ^ m[i % 4] for i, b in enumerate(data)))

def _recv(ws, n):
    buf = b""
    while len(buf) < n:
        c = ws.recv(n - len(buf))
        if not c: raise RuntimeError("ws closed")
        buf += c
    return buf

def read_msg(ws):
    while True:
        b0, b1 = _recv(ws, 2); op = b0 & 0x0F
        if op == 0x8: raise TimeoutError("ws closed")
        masked = b1 & 0x80; n = b1 & 0x7F
        if n == 126: n = struct.unpack(">H", _recv(ws, 2))[0]
        elif n == 127: n = struct.unpack(">Q", _recv(ws, 8))[0]
        mk = _recv(ws, 4) if masked else None
        data = _recv(ws, n) if n else b""
        if mk: data = bytes(b ^ mk[i % 4] for i, b in enumerate(data))
        if op in (0x1, 0x2): return data.decode("utf-8", "replace")
        if op == 0x9:
            pm = os.urandom(4); hh = bytearray([0x8A, 0x80 | len(data)]); hh += pm
            ws.sendall(bytes(hh) + bytes(b ^ pm[i % 4] for i, b in enumerate(data)))

PROBE = r"""
(function(){
  var out = {};
  out.innerW = window.innerWidth; out.innerH = window.innerHeight;
  var doc = document.documentElement;
  out.docScrollW = doc.scrollWidth; out.docHScroll = doc.scrollWidth > window.innerWidth + 1;
  // 滚动容器是 .content（唯一垂直滚动区）
  var content = document.querySelector('.content');
  out.contentScrollH = content ? content.scrollHeight : 0;
  out.contentScrollW = content ? content.scrollWidth : 0;
  out.contentHScroll = content ? content.scrollWidth > content.clientWidth + 1 : false;
  // 溢出元素（右缘超出视口）。忽略：自身可横滚的（设计内 tabs/chips）、
  // 祖先链上有可横滚或 overflow-x:hidden 容器的（会被裁剪，不产生页面横滚）。
  function hasScrollOrClipAncestor(e) {
    var cur = e.parentElement;
    while (cur && cur !== document.body) {
      var s = getComputedStyle(cur);
      if (['auto','scroll','hidden','clip'].indexOf(s.overflowX) > -1) return true;
      cur = cur.parentElement;
    }
    return false;
  }
  var over = [];
  var active = document.querySelector('.page.active') || document.body;
  active.querySelectorAll('*').forEach(function(e){
    var r = e.getBoundingClientRect();
    if (r.right > window.innerWidth + 1.5 && r.width > 0) {
      var st = getComputedStyle(e);
      var selfScroll = (st.overflowX === 'auto' || st.overflowX === 'scroll');
      if (selfScroll) return;
      if (hasScrollOrClipAncestor(e)) return;
      var cl = (e.className && e.className.toString ? e.className.toString() : '').slice(0,44);
      over.push({id: e.id||'', cls: cl, w: Math.round(r.width), r: Math.round(r.right), tag: e.tagName.toLowerCase()});
    }
  });
  // 去重：若父级已滚动容器（scr=1）则子项可容忍
  out.overCount = over.length;
  out.over = over.slice(0, 12);
  // 内层垂直滚动容器（nested scroll 审计）
  var nested = [];
  active.querySelectorAll('*').forEach(function(e){
    if (e === content) return;
    var st = getComputedStyle(e);
    if ((st.overflowY === 'auto' || st.overflowY === 'scroll') && e.scrollHeight > e.clientHeight + 2) {
      nested.push({id: e.id||'', cls: (e.className && e.className.toString ? e.className.toString() : '').slice(0,40), sh: e.scrollHeight, ch: e.clientHeight});
    }
  });
  out.nestedScroll = nested.slice(0, 8);
  // Bottom Nav
  var mnav = document.querySelector('.mobile-nav');
  if (mnav) {
    var mr = mnav.getBoundingClientRect();
    out.mnav = {h: Math.round(mr.height), top: Math.round(mr.top), w: Math.round(mr.width)};
    var items = mnav.querySelectorAll('.mnav-item');
    var hs = [];
    items.forEach(function(b){ hs.push(Math.round(b.getBoundingClientRect().height)); });
    out.mnavItemH = hs;
    // 内容底部 padding（Bottom Nav 遮挡审计）
    out.contentPadBottom = content ? getComputedStyle(content).paddingBottom : null;
  }
  // 图表实例数
  if (window.LM && LM.charts && LM.charts.instanceCount) out.chartInstances = LM.charts.instanceCount();
  // 页面高度（full page 截图参考）
  out.pageH = content ? content.scrollHeight : doc.scrollHeight;
  return JSON.stringify(out);
})()
"""

def main():
    ws_url, w, h, page, outdir = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4], sys.argv[5]
    os.makedirs(outdir, exist_ok=True)
    rest = ws_url[len("ws://"):]; host, _, path = rest.partition("/")
    host, _, port = host.partition(":")
    ws = socket.create_connection((host, int(port or 80)))
    key = base64.b64encode(os.urandom(16)).decode()
    ws.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        c = ws.recv(4096)
        if not c: raise RuntimeError("ws handshake failed")
        resp += c
    mid = [0]
    events = []
    def drain_events(seconds):
        end = time.time() + seconds
        while time.time() < end:
            ws.settimeout(max(0.5, end - time.time()))
            try: raw = read_msg(ws)
            except (socket.timeout, TimeoutError, RuntimeError): return
            try: j = json.loads(raw)
            except Exception: continue
            m = j.get("method")
            if m == "Runtime.consoleAPICalled":
                p = j.get("params", {})
                txt = " ".join(str(a.get("value") or a.get("description") or "") for a in p.get("args", []))
                if p.get("type") in ("error", "warning"):
                    events.append("console[%s]: %s" % (p.get("type"), txt[:300]))
            elif m == "Runtime.exceptionThrown":
                d = j.get("params", {}).get("exceptionDetails", {})
                txt = (d.get("exception") or {}).get("description") or d.get("text", "exception")
                events.append("EXCEPTION: %s" % str(txt)[:300])
    def call(method, params=None, timeout=25):
        mid[0] += 1
        send(ws, {"id": mid[0], "method": method, "params": params or {}})
        end = time.time() + timeout
        while time.time() < end:
            ws.settimeout(2.0)
            try: raw = read_msg(ws)
            except socket.timeout: continue
            except Exception: return None
            try: j = json.loads(raw)
            except Exception: continue
            m = j.get("method")
            if m == "Runtime.consoleAPICalled":
                p = j.get("params", {})
                txt = " ".join(str(a.get("value") or a.get("description") or "") for a in p.get("args", []))
                if p.get("type") in ("error", "warning"):
                    events.append("console[%s]: %s" % (p.get("type"), txt[:300]))
            elif m == "Runtime.exceptionThrown":
                d = j.get("params", {}).get("exceptionDetails", {})
                txt = (d.get("exception") or {}).get("description") or d.get("text", "exception")
                events.append("EXCEPTION: %s" % str(txt)[:300])
            if j.get("id") == mid[0]:
                r = j.get("result", {})
                if "exceptionDetails" in r:
                    det = r["exceptionDetails"]
                    txt = (det.get("exception") or {}).get("description") or det.get("text", "exception")
                    events.append("EVAL-EXCEPTION: %s" % str(txt)[:300])
                v = r.get("result", r)
                # Runtime.evaluate: value 在 result.result.value；其余命令在 result.*
                if isinstance(v, dict) and "result" in v and "value" in v.get("result", {}):
                    return v["result"]["value"]
                return v
        return None
    def shot(name, full=False, scrollY=None):
        if scrollY is not None:
            call("Runtime.evaluate", {"expression": "document.querySelector('.content').scrollTop = %d; 1" % scrollY, "returnByValue": True})
            time.sleep(0.4)
        r = call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": full,
                                            **({"clip": {"x": 0, "y": 0, "width": w, "height": h, "scale": 1}} if not full else {})})
        if isinstance(r, dict) and "data" in r:
            fn = os.path.join(outdir, name)
            with open(fn, "wb") as f: f.write(base64.b64decode(r["data"]))
            return os.path.getsize(fn)
        return 0

    mobile = (w < 761) or (w > h and h <= 480)  # 横屏手机（宽>760 高≤480）也算 mobile
    call("Runtime.enable"); call("Page.enable")
    call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 2 if mobile else 1, "mobile": mobile})
    call("Page.navigate", {"url": "http://127.0.0.1:8790/"})
    time.sleep(3.0)
    call("Runtime.evaluate", {"expression": "LM.nav.showPage(%s); 1" % json.dumps(page), "returnByValue": True})
    time.sleep(2.5)
    drain_events(2.0)
    probe = None
    for _ in range(6):
        raw = call("Runtime.evaluate", {"expression": PROBE, "returnByValue": True})
        # 兼容多层 CDP 包装
        v = raw
        for _ in range(3):
            if isinstance(v, dict) and "value" in v and isinstance(v.get("value"), str):
                v = v["value"]; break
            if isinstance(v, dict) and "result" in v and isinstance(v.get("result"), dict):
                v = v["result"]; continue
            break
        if v:
            probe = v
            break
        time.sleep(1.0)
    # 截图：top / mid / bottom / full
    sizes = {}
    if isinstance(probe, str):
        probe = json.loads(probe)
    if probe:
        d = probe
        ch = d.get("contentScrollH", 0)
        vh = h
        mid_y = max(0, (ch - vh) // 2) if ch > vh else 0
        bot_y = max(0, ch - vh) if ch > vh else 0
        sizes["top"] = shot("%s-top.png" % page)
        sizes["mid"] = shot("%s-mid.png" % page, scrollY=mid_y)
        sizes["bottom"] = shot("%s-bottom.png" % page, scrollY=bot_y)
        # full：临时解锁 .content 内部滚动让文档变高 -> captureBeyondViewport
        call("Runtime.evaluate", {"expression": """
          (function(){ var a=document.querySelector('.app'), c=document.querySelector('.content');
            if(a){ a.style.height='auto'; }
            if(c){ c.style.overflow='visible'; c.style.position='static'; c.style.height='auto'; }
            window.__fh = document.documentElement.scrollHeight; return window.__fh; })()
        """, "returnByValue": True})
        time.sleep(0.6)
        fh = call("Runtime.evaluate", {"expression": "document.documentElement.scrollHeight", "returnByValue": True})
        fh = int(fh) if isinstance(fh, (int, float)) else min(ch, 8000)
        r = call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True,
                                            "clip": {"x": 0, "y": 0, "width": w, "height": min(fh, 10000), "scale": 1}})
        if isinstance(r, dict) and "data" in r:
            fn = os.path.join(outdir, "%s-full.png" % page)
            with open(fn, "wb") as f: f.write(base64.b64decode(r["data"]))
            sizes["full"] = os.path.getsize(fn)
        # 恢复
        call("Runtime.evaluate", {"expression": """
          (function(){ var a=document.querySelector('.app'), c=document.querySelector('.content');
            if(a){ a.style.height=''; } if(c){ c.style.overflow=''; c.style.position=''; c.style.height=''; }
            return 1; })()
        """, "returnByValue": True})
        # 回到顶部
        call("Runtime.evaluate", {"expression": "document.querySelector('.content').scrollTop = 0; 1", "returnByValue": True})
    drain_events(1.0)
    res = {"w": w, "h": h, "page": page,
           "probe": probe,
           "events": events[:15], "sizes": sizes}
    with open(os.path.join(outdir, "_result-%d-%d-%s.json" % (w, h, page)), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print("OK %dx%d %s -> %s" % (w, h, page, os.path.join(outdir, "_result-%d-%d-%s.json" % (w, h, page))))

if __name__ == "__main__":
    main()
