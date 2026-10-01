#!/usr/bin/env python3
"""Round-7 Settings/About CDP 验证：指定视口 + 设置分区，采集 DOM 状态 + 横向溢出 + 截图。
用法: cdp_r7_verify.py <ws> <w> <h> <dpr> <page> <section> <out_png>
  page: settings | about
  section: server|collector|appearance|gpu|system|data|application|updates（settings 用）
返回 JSON: 关键 DOM 值 + scrollW/innerW + 溢出元素 + 截图路径
"""
import sys, socket, struct, base64, os, json, time

def send(ws, obj):
    data = json.dumps(obj).encode("utf-8")
    header = bytearray([0x81])
    n = len(data)
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

def read_msg(ws):
    while True:
        b0, b1 = _recv_exact(ws, 2)
        op = b0 & 0x0F
        if op == 0x8: raise TimeoutError("ws closed")
        masked = b1 & 0x80; n = b1 & 0x7F
        if n == 126: n = struct.unpack(">H", _recv_exact(ws, 2))[0]
        elif n == 127: n = struct.unpack(">Q", _recv_exact(ws, 8))[0]
        mask = _recv_exact(ws, 4) if masked else None
        data = _recv_exact(ws, n) if n else b""
        if mask: data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if op in (0x1, 0x2): return data.decode("utf-8", "replace")
        if op == 0x9:
            pm = os.urandom(4)
            h = bytearray([0x8A, 0x80 | len(data)]); h += pm
            ws.sendall(bytes(h) + bytes(b ^ pm[i % 4] for i, b in enumerate(data)))

def main():
    ws_url, w, h, dpr, page, section, out = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]), sys.argv[5], sys.argv[6], sys.argv[7]
    rest = ws_url[len("ws://"):]
    host, _, path = rest.partition("/")
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
    def call(method, params=None, timeout=30):
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
            if j.get("id") == mid[0]:
                if "error" in j: return j["error"]
                r = j.get("result", {})
                if "result" in r and isinstance(r["result"], dict) and "value" in r["result"]:
                    return r["result"]["value"]
                return r
        return None

    call("Runtime.enable")
    call("Page.enable")
    call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": dpr, "mobile": w < 761})
    call("Page.navigate", {"url": "http://127.0.0.1:8790/"})
    time.sleep(2.5)
    # 导航到目标页 + 分区（等 settings 表单加载完成）
    nav_js = "LM.nav.showPage(%s)" % json.dumps(page)
    call("Runtime.evaluate", {"expression": nav_js + ";1", "returnByValue": True})
    time.sleep(1.5)
    if page == "settings":
        call("Runtime.evaluate", {"expression": "LM.settings.goToSection(%s);1" % json.dumps(section), "returnByValue": True})
        time.sleep(2.5)  # 等该分区的异步加载（data/info、system/sensors、app/integration）

    probe = """
    (function(){
      var out = {};
      out.innerW = window.innerWidth;
      out.scrollW = document.documentElement.scrollWidth;
      out.hScroll = out.scrollW > out.innerW + 1;
      var over = [];
      var root = document.getElementById('page-' + (location.hash ? location.hash.slice(1) : '')) || document.body;
      var active = document.querySelector('.page.active') || document.body;
      active.querySelectorAll('*').forEach(function(e){
        var r = e.getBoundingClientRect();
        if (r.right > out.innerW + 1.5 && r.width > 0) {
          var cl = e.className && e.className.toString ? e.className.toString().slice(0,40) : '';
          over.push(cl + '#' + (e.id || '') + ' w=' + Math.round(r.width) + ' right=' + Math.round(r.right));
        }
      });
      out.overCount = over.length;
      out.over = over.slice(0, 10);
      return JSON.stringify(out);
    })()
    """
    # 页面特定 DOM 探针
    if page == "settings":
        dom_probe = """
        (function(){
          var g = function(id){ var e = document.getElementById(id); return e ? (e.textContent||'').trim() : null; };
          var d = {};
          d.rail = Array.prototype.map.call(document.querySelectorAll('.settings-rail .rail-item'), function(b){ return b.textContent.trim(); });
          d.railCurrent = null;
          var cur = document.querySelector('.settings-rail .rail-item[aria-current="true"]');
          if (cur) d.railCurrent = cur.textContent.trim();
          d.visibleCard = null;
          var vc = document.querySelector('.settings-pane .settings-card:not([hidden])');
          if (vc) { d.visibleCard = vc.id; var h2 = vc.querySelector('h2'); if (h2) d.cardTitle = h2.textContent.trim(); }
          d.dirtyHidden = document.getElementById('dirtyBadge') ? document.getElementById('dirtyBadge').hidden : null;
          d.saveDisabled = document.getElementById('btnSaveSettings') ? document.getElementById('btnSaveSettings').disabled : null;
          d.saveLabel = g('btnSaveSettings');
          d.resetLabel = g('btnResetDefaults');
          // 各分区字段
          d.serverUrl = (document.getElementById('setServerUrl')||{}).value;
          d.metricsPath = (document.getElementById('setMetricsPath')||{}).value;
          d.timeout = (document.getElementById('setTimeoutSec')||{}).value;
          d.connText = g('serverConnStatus');
          d.gpuEnabled = (document.getElementById('setGpuEnabled')||{}).checked;
          d.gpuDetectedRows = document.querySelectorAll('#gpuDetected .gpu-detected-row').length;
          d.gpuCount = g('gpuDeviceCount');
          d.gpuPollDisabled = (document.getElementById('setGpuPoll')||{}).disabled;
          d.webScope = (document.getElementById('setWebScope')||{}).value;
          d.webPort = (document.getElementById('setWebPort')||{}).value;
          d.defaultRange = (document.getElementById('setDefaultRange')||{}).value;
          d.theme = (document.getElementById('setTheme')||{}).value;
          d.sysEnabled = (document.getElementById('setSysEnabled')||{}).checked;
          d.sysAdv = (document.getElementById('setSysAdvanced')||{}).checked;
          d.sysMonAdv = g('sysMonAdvState');
          d.dbPath = (document.getElementById('setDbPath')||{}).value;
          d.dbPathHint = g('dbPathHint');
          d.dataInfo = g('dataInfo');
          d.backupLast = g('backupLastInfo');
          d.updVersion = g('updCurrentVersion');
          d.updStatus = g('updStatus');
          d.updInstallMode = g('updInstallMode');
          d.updLastCheck = g('updLastCheck');
          d.updLatest = g('updLatestVersion');
          d.updDownloadDis = (document.getElementById('btnUpdateDownload')||{}).disabled;
          d.updInstallDis = (document.getElementById('btnUpdateInstall')||{}).disabled;
          d.updCancelHidden = (document.getElementById('btnUpdateCancel')||{}).hidden;
          d.updModeNote = (g('updModeNote')||'').slice(0,120);
          // 数据分区详细
          d.dataInfo = (g('dataInfo')||'').replace(/\s+/g,' ').slice(0,200);
          d.dbTone = (function(){var e=document.querySelector('#dataInfo .db-status');return e?e.getAttribute('data-tone'):null;})();
          d.backupLast = g('backupLastInfo');
          d.backupCount = document.getElementById('backupList')?document.querySelectorAll('#backupList > *').length:0;
          d.backupAuto = (document.getElementById('setBackupAuto')||{}).checked;
          d.backupIntDis = (document.getElementById('setBackupInterval')||{}).disabled;
          // 系统分区详细
          d.sysMonAdv = g('sysMonAdvState');
          d.sysMonSub = g('sysMonStateSub');
          d.sysSensorHidden = document.getElementById('sysMonSensorList')?document.getElementById('sysMonSensorList').hidden:'n/a';
          d.sysSensorText = (function(){var e=document.getElementById('sysMonSensorList');return e?e.textContent.replace(/\s+/g,' ').slice(0,80):'';})();
          // 应用分区详细
          d.appInfo = (g('appInfo')||'').replace(/\s+/g,' ').slice(0,200);
          d.autostartCmd = (g('autostartCommand')||'').slice(0,80);
          d.autostartSt = g('autostartStatus');
          d.autostart = (document.getElementById('setAutostart')||{}).checked;
          // 连接状态
          d.connText = (g('serverConnStatus')||'').replace(/\s+/g,' ').slice(0,80);
          d.dangerHidden = document.getElementById('sec-danger') ? document.getElementById('sec-danger').hidden : null;
          d.effBadges = document.querySelectorAll('.settings-pane .eff-badge').length;
          d.inputSuffixes = document.querySelectorAll('.settings-pane .input-suffix').length;
          d.groupTitles = Array.prototype.map.call(document.querySelectorAll('.settings-pane .group-title'), function(e){ return e.textContent.trim(); });
          return JSON.stringify(d);
        })()
        """
    else:
        dom_probe = """
        (function(){
          var g = function(id){ var e = document.getElementById(id); return e ? (e.textContent||'').trim() : null; };
          var d = {};
          d.heroName = g('aboutName') || (document.querySelector('.ah-name')||{}).textContent;
          d.version = g('aboutVersion');
          d.schema = g('aboutSchema');
          d.dataDir = g('aboutDataDir');
          d.platform = g('aboutPlatform');
          d.installMode = g('aboutInstallMode');
          d.sections = Array.prototype.map.call(document.querySelectorAll('#page-about .sh-title'), function(e){ return e.textContent.trim(); });
          d.links = Array.prototype.map.call(document.querySelectorAll('#page-about .about-links a'), function(e){ return e.textContent.trim(); });
          d.linkHrefs = Array.prototype.map.call(document.querySelectorAll('#page-about .about-links a'), function(e){ return e.getAttribute('href'); });
          d.btns = Array.prototype.map.call(document.querySelectorAll('#page-about .btn-row .btn'), function(e){ return e.textContent.trim(); });
          d.privacyItems = document.querySelectorAll('#page-about .about-privacy li').length;
          d.heroH = (document.querySelector('.about-hero-card')||{}).clientHeight;
          return JSON.stringify(d);
        })()
        """
    dom = None
    for _ in range(8):
        dom = call("Runtime.evaluate", {"expression": dom_probe, "returnByValue": True})
        if dom: break
        time.sleep(1.0)
    layout = call("Runtime.evaluate", {"expression": probe, "returnByValue": True})
    time.sleep(0.5)
    r = call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
    if isinstance(r, dict) and "data" in r:
        with open(out, "wb") as f:
            f.write(base64.b64decode(r["data"]))
        shot = "%s (%d bytes)" % (out, os.path.getsize(out))
    else:
        shot = "shot error: %s" % json.dumps(r)
    print(json.dumps({"layout": json.loads(layout) if isinstance(layout, str) else layout,
                      "dom": json.loads(dom) if isinstance(dom, str) else dom,
                      "shot": shot}, ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
