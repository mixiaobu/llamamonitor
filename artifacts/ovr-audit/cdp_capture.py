"""Minimal CDP driver (stdlib only) for Overview screenshot capture.

Launches headless Chrome, drives Emulation.setDeviceMetricsOverride +
Page.captureScreenshot (viewport + full page), and returns DOM measurements.
Usage: python -X utf8 cdp_capture.py   (see CAPTURES below)
"""
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request
import uuid

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CDP_PORT = 9333
BASE = "http://127.0.0.1:8790"
OUT = r"C:\Users\mixiaobu\Desktop\ai\LlamaMonitor\artifacts\ovr-audit"


# ---------- minimal WebSocket (client) over socket ----------
class WS:
    def __init__(self, url):
        # url: ws://host:port/path
        assert url.startswith("ws://")
        rest = url[5:]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        port = int(port or 80)
        self.sock = socket.create_connection((host, port), timeout=15)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n" % (path, hostport, key))
        self.sock.sendall(req.encode())
        # read handshake headers
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        self._recvbuf = buf.split(b"\r\n\r\n", 1)[1]

    def _recv(self, n):
        while len(self._recvbuf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("socket closed")
            self._recvbuf += chunk
        out, self._recvbuf = self._recvbuf[:n], self._recvbuf[n:]
        return out

    def _frame(self, op, payload):
        hdr = bytes([0x80 | op])
        ln = len(payload)
        mask = os.urandom(4)
        if ln < 126:
            hdr += bytes([0x80 | ln])
        elif ln < (1 << 16):
            hdr += bytes([0x80 | 126]) + struct.pack(">H", ln)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", ln)
        hdr += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return hdr + masked

    def send(self, obj):
        self._frame(0x1, json.dumps(obj).encode())

    def _read_frame(self):
        b1 = self._recv(1)[0]
        op = b1 & 0x0F
        ln = self._recv(1)[0] & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._recv(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._recv(8))[0]
        data = self._recv(ln) if ln else b""
        return op, data

    def recv(self):
        while True:
            op, data = self._read_frame()
            if op == 0x1:
                return json.loads(data.decode())
            # ignore ping/pong/close control frames for brevity
            if op == 0x9:  # ping -> pong
                self._frame(0xA, data)

    def close(self):
        try:
            self._frame(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


class CDP:
    def __init__(self, ws_url):
        import queue
        import threading
        import websocket
        self._ws = websocket.create_connection(ws_url, timeout=30)
        self.id = 0
        self.events = []
        self._resps = queue.Queue()
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        while True:
            try:
                raw = self._ws.recv()
            except Exception as e:
                sys.stderr.write("reader loop died: %r\n" % (e,))
                return
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if isinstance(msg, dict) and "id" in msg:
                self._resps.put(msg)
            else:
                self.events.append(msg)

    def cmd(self, method, params=None, timeout=60):
        import queue
        with self._lock:
            self.id += 1
            mid = self.id
            self._ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        try:
            return self._resps.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(method)

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass


def http_get(url):
    return json.load(urllib.request.urlopen(url, timeout=10))


def launch_chrome():
    profile = os.path.join(OUT, "profile-r2")
    cmd = [CHROME,
           "--headless=new",
           "--remote-debugging-port=%d" % CDP_PORT,
           "--remote-debugging-address=127.0.0.1",
           "--remote-allow-origins=*",
           "--user-data-dir=%s" % profile,
           "--no-first-run", "--no-default-browser-check",
           "--disable-gpu", "--hide-scrollbars",
           "--window-size=1280,900",
           "about:blank"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # wait for devtools endpoint
    ws_url = None
    for _ in range(60):
        try:
            targets = http_get("http://127.0.0.1:%d/json" % CDP_PORT)
            for t in targets:
                if t.get("type") == "page":
                    ws_url = t["webSocketDebuggerUrl"]
                    break
            if ws_url:
                break
        except Exception:
            pass
        time.sleep(0.5)
    if not ws_url:
        proc.kill()
        raise RuntimeError("Chrome did not expose CDP")
    return proc, ws_url


JS_MEASURE = r"""
(() => {
  const q = (s) => document.querySelector(s);
  const out = {};
  const doc = document.documentElement;
  out.docScrollWidth = doc.scrollWidth;
  out.docClientWidth = doc.clientWidth;
  out.horizOverflow = doc.scrollWidth > doc.clientWidth + 1;
  out.bodyScrollHeight = document.body.scrollHeight;
  function rect(id) {
    const el = document.getElementById(id);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return {
      w: Math.round(r.width), h: Math.round(r.height), top: Math.round(r.top),
      left: Math.round(r.left),
      overflowX: el.scrollWidth > el.clientWidth + 1,
      scrollW: el.scrollWidth, clientW: el.clientWidth,
      font: cs.fontSize, color: cs.color
    };
  }
  function gridCols(sel) {
    const el = document.querySelector(sel);
    if (!el) return null;
    return getComputedStyle(el).gridTemplateColumns;
  }
  // server card
  out.serverUrl = (q('#ovServerUrl')||{}).textContent;
  out.serverModel = (q('#ovModelLine')||{}).textContent;
  out.serverState = (q('#ovServerState .status-text')||{}).textContent;
  out.lastUpdate = (q('#ovLastUpdate')||{}).textContent;
  // today
  out.heroNums = [q('#ovTodayLogical'),q('#ovTodayCompute')].map(e=>e&&e.textContent);
  out.heroGrid = gridCols('.today-hero');
  out.breakdownGrid = gridCols('.today-breakdown');
  out.breakdownLabels = Array.from(document.querySelectorAll('.today-breakdown .tb-label')).map(e=>e.textContent.trim());
  // perf
  out.ovPerfGrid = gridCols('.ov-perf-grid');
  out.promptTps = (q('#ovPromptTps')||{}).textContent;
  out.decodeTps = (q('#ovDecodeTps')||{}).textContent;
  out.mtpRate = (q('#ovMtpRate')||{}).textContent;
  out.context = (q('#ovContext')||{}).textContent;
  out.requests = (q('#ovRequests')||{}).textContent;
  out.kvCache = (q('#ovKvCache')||{}).textContent;
  // host
  out.hostCpu = (q('#ovHostCpu')||{}).textContent;
  out.hostCpuSub = (q('#ovHostCpuSub')||{}).textContent;
  out.hostMem = (q('#ovHostMem')||{}).textContent;
  out.hostMemSub = (q('#ovHostMemSub')||{}).textContent;
  out.hostDiskR = (q('#ovHostDiskR')||{}).textContent;
  out.hostDiskW = (q('#ovHostDiskW')||{}).textContent;
  out.hostNetR = (q('#ovHostNetR')||{}).textContent;
  out.hostNetW = (q('#ovHostNetW')||{}).textContent;
  out.hostPower = (q('#ovHostPower')||{}).textContent;
  out.hostPowerSub = (q('#ovHostPowerSub')||{}).textContent;
  out.hostGrid = gridCols('.host-grid');
  out.hostCellRect = rect('ovHostMem');
  // gpu
  out.gpuState = (q('#ovGpuState .status-text')||{}).textContent;
  out.gpuCards = Array.from(document.querySelectorAll('.gpu-mini')).length;
  out.gpuGrid = gridCols('.gpu-mini-grid');
  out.gpuCardRect = rect('ovGpuMini') ? null : null;
  const gm = document.querySelector('.gpu-mini');
  if (gm) { const r=gm.getBoundingClientRect(); out.gpuCardW=Math.round(r.width); }
  // data quality
  out.dqGrid = gridCols('.dq-summary .stat-grid');
  out.dqDb = (q('#dqDb')||{}).textContent;
  out.dqDbHint = (q('#dqDbHint')||{}).textContent;
  out.dqGaps = (q('#dqGapsToday')||{}).textContent;
  out.dqLoss = (q('#dqLossToday')||{}).textContent;
  out.dqCov = (q('#dqCoverage')||{}).textContent;
  // sidebar + mobile nav
  out.sidebarLabels = Array.from(document.querySelectorAll('.navview .nav-label')).map(e=>e.textContent.trim());
  out.mnavLabels = Array.from(document.querySelectorAll('.mobile-nav .mnav-label')).map(e=>e.textContent.trim());
  // remote banner (may be hidden when loopback)
  out.banner = (q('#remoteBanner')||{}).textContent;
  out.bannerHidden = q('#remoteBanner') ? q('#remoteBanner').hidden : 'no-el';
  // ellipsis patrol: any .stat-value whose rendered width is clipped
  const clipped = [];
  document.querySelectorAll('.stat-value, .gm-metric .v, .tb-value, .hero-num').forEach(e=>{
    if (e.scrollWidth > e.clientWidth + 1) clipped.push((e.id||e.className)+':'+e.textContent);
  });
  out.clippedValues = clipped;
  // first-screen top of server card
  const sc = q('.status-strip');
  if (sc) { const r=sc.getBoundingClientRect(); out.serverCardTopFromViewportTop=Math.round(r.top); }
  return out;
})()
"""


def set_metrics(cdp, w, h):
    cdp.cmd("Emulation.setDeviceMetricsOverride",
            {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": w < 761})


def shot_viewport(cdp, path):
    r = cdp.cmd("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
    with open(path, "wb") as f:
        f.write(base64.b64decode(r["result"]["data"]))


def full_page_height(cdp):
    m = cdp.cmd("Runtime.evaluate", {"expression": "document.body.scrollHeight",
                                     "returnByValue": True})
    return m["result"]["result"]["value"]


def shot_full(cdp, w, h, path):
    # resize to full height, screenshot, restore
    full = full_page_height(cdp)
    cap_h = min(full + 40, 6000)
    set_metrics(cdp, w, cap_h)
    time.sleep(0.6)
    shot_viewport(cdp, path)
    set_metrics(cdp, w, h)
    time.sleep(0.3)


def main():
    cap_names = sys.argv[1:] or ["all"]
    proc, ws_url = launch_chrome()
    try:
        cdp = CDP(ws_url)
        cdp.cmd("Page.enable")
        cdp.cmd("Runtime.enable")
        cdp.cmd("Emulation.setFocusEmulationEnabled", {"enabled": False})

        def navigate():
            cdp.cmd("Page.navigate", {"url": BASE + "/"}, timeout=30)
            # wait for network idle-ish + pollers to populate
            time.sleep(6.0)

        def ensure_overview():
            cdp.cmd("Runtime.evaluate",
                    {"expression": "window.LM && LM.nav.showPage('overview');", "returnByValue": True})
            time.sleep(1.5)

        measures = {}
        if "all" in cap_names or "1920" in cap_names:
            navigate(); ensure_overview()
            set_metrics(cdp, 1920, 1080); time.sleep(1.0)
            shot_viewport(cdp, os.path.join(OUT, "r2-1920x1080-top.png"))
            shot_full(cdp, 1920, 1080, os.path.join(OUT, "r2-1920x1080-full.png"))
            measures["1920x1080"] = cdp.cmd("Runtime.evaluate",
                {"expression": JS_MEASURE, "returnByValue": True})["result"]["result"]["value"]
            print("1920 OK", file=sys.stderr)

        if "all" in cap_names or "1065" in cap_names:
            navigate(); ensure_overview()
            set_metrics(cdp, 1065, 1394); time.sleep(1.0)
            shot_viewport(cdp, os.path.join(OUT, "r2-1065x1394-top.png"))
            shot_full(cdp, 1065, 1394, os.path.join(OUT, "r2-1065x1394-full.png"))
            measures["1065x1394"] = cdp.cmd("Runtime.evaluate",
                {"expression": JS_MEASURE, "returnByValue": True})["result"]["result"]["value"]
            print("1065 OK", file=sys.stderr)

        if "all" in cap_names or "390" in cap_names:
            navigate(); ensure_overview()
            set_metrics(cdp, 390, 844); time.sleep(1.0)
            shot_viewport(cdp, os.path.join(OUT, "r2-390x844-top.png"))
            shot_full(cdp, 390, 844, os.path.join(OUT, "r2-390x844-full.png"))
            measures["390x844"] = cdp.cmd("Runtime.evaluate",
                {"expression": JS_MEASURE, "returnByValue": True})["result"]["result"]["value"]
            print("390 OK", file=sys.stderr)

        if "all" in cap_names or "320" in cap_names:
            navigate(); ensure_overview()
            set_metrics(cdp, 320, 568); time.sleep(1.0)
            shot_viewport(cdp, os.path.join(OUT, "r2-320x568-top.png"))
            shot_full(cdp, 320, 568, os.path.join(OUT, "r2-320x568-full.png"))
            measures["320x568"] = cdp.cmd("Runtime.evaluate",
                {"expression": JS_MEASURE, "returnByValue": True})["result"]["result"]["value"]
            print("320 OK", file=sys.stderr)

        with open(os.path.join(OUT, "r2-measures.json"), "w", encoding="utf-8") as f:
            json.dump(measures, f, ensure_ascii=False, indent=2)
        print("MEASURES SAVED", file=sys.stderr)
        cdp.close()
    finally:
        try:
            proc.terminate()
            time.sleep(1.0)
            proc.kill()
        except Exception:
            pass


if __name__ == "__main__":
    main()
