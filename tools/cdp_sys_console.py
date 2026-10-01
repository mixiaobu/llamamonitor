# cdp_sys_console.py — reload, navigate to SYSTEM page, capture console + page errors + probe system DOM.
# Usage: cdp_sys_console.py <ws_url> [settle_seconds]
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

def main():
    url = sys.argv[1]
    settle = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
    rest = url[len("ws://"):]; host, _, path = rest.partition("/"); host, _, port = host.partition(":")
    ws = socket.create_connection((host, int(port or 80)))
    key = base64.b64encode(os.urandom(16)).decode()
    ws.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        c = ws.recv(4096)
        if not c: raise RuntimeError("handshake failed")
        resp += c
    mid = [0]
    def call(method, params=None, timeout=60):
        mid[0] += 1; i = mid[0]
        send(ws, {"id": i, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = read_msg(ws, max(0.5, deadline - time.time()))
            try: j = json.loads(raw)
            except Exception: continue
            if j.get("id") == i:
                return j.get("error") if "error" in j else j.get("result", {})
        return {"_timeout": True}

    events = []
    def listen_for(duration):
        deadline = time.time() + duration
        while time.time() < deadline:
            try: raw = read_msg(ws, max(0.2, deadline - time.time()))
            except Exception: break
            try: j = json.loads(raw)
            except Exception: continue
            m = j.get("method")
            if m == "Runtime.consoleAPICalled":
                p = j.get("params", {})
                txt = " ".join((a.get("value", a.get("description", "")) for a in p.get("args", [])))
                events.append((p.get("type", "?"), str(txt)[:300]))
            elif m == "Runtime.exceptionThrown":
                e = j.get("params", {}).get("exceptionDetails", {})
                desc = (e.get("exception") or {}).get("description") or e.get("text", "")
                events.append(("exception", str(desc)[:300]))
            elif m == "Log.entryAdded":
                en = j.get("params", {}).get("entry", {})
                events.append((str(en.get("level")), str(en.get("text", ""))[:300]))
            elif m == "Network.loadingFailed":
                events.append(("net-fail", json.dumps(j.get("params", {}))[:160]))

    call("Runtime.enable"); call("Log.enable"); call("Network.enable"); call("Page.enable")
    call("Page.setWebLifecycleState", {"state": "active"}, 10)
    call("Page.reload", {"ignoreCache": True})
    ready = False
    for _ in range(48):
        time.sleep(0.5); listen_for(0.5)
        r = call("Runtime.evaluate", {"expression": "!!(window.LM && LM.app)", "returnByValue": True}, 8)
        if isinstance(r, dict) and r.get("result", {}).get("value"):
            ready = True; break
    print("app ready: %s" % ready)
    call("Runtime.evaluate", {"expression": "LM.nav.showPage('system');1", "returnByValue": True}, 10)
    time.sleep(2.0); listen_for(2.0)
    probe = call("Runtime.evaluate", {
        "expression": """(function(){
          function t(id){var e=document.getElementById(id);return e?e.textContent.trim():'∅'+id;}
          function vis(id){var e=document.getElementById(id);return e?getComputedStyle(e).display!=='none':'∅';}
          return JSON.stringify({
            state:t('systemPageState'),
            ovCpu:t('sysCpuUsage'), ovMem:t('sysMemUsage'), ovMemSub:t('sysMemDetail'),
            diskR:t('sysDiskR'), diskW:t('sysDiskW'),
            netR:t('sysNetR'), netW:t('sysNetW'),
            compPower:t('sysCompPower'), compSub:t('sysCompPowerSub'), uptime:t('sysUptime'),
            cpuPct:t('sysCpuPct'), cpuFreq:t('sysCpuFreq'), cpuFreqBase:t('sysCpuFreqBase'),
            cpuTemp:t('sysCpuTemp'), cpuPower:t('sysCpuPower'),
            cpuChartSummary:t('sysCpuChartSummary'),
            heatCells:document.querySelectorAll('#sysCoreHeat .core-cell').length,
            heatSub:t('coreHeatSub'),
            memPct:t('sysMemPct'), memUsed:t('sysMemUsed'), memAvail:t('sysMemAvail'), memTotal:t('sysMemTotal'),
            memSummary:t('sysMemSummary'), memChartSummary:t('sysMemChartSummary'),
            netIfaceName:t('sysNetIfaceName'), netIfaceKind:t('sysNetIfaceKind'), netSpeed:t('sysNetSpeed'),
            netIfaceOpts:Array.prototype.map.call(document.getElementById('sysNetIface').options,function(o){return o.value;}),
            powerComp:t('sysPowerComp'), powerCpu:t('sysPowerCpu'), powerGpu:t('sysPowerGpu'),
            wallPower:t('sysWallPower'), wallSub:t('sysWallPowerSub'), energyToday:t('sysEnergyToday'),
            sensorState:t('sensorState'), sensorGroups:document.querySelectorAll('#sysSensors .sensor-group-title').length,
            hwOs:t('hwOs'), hwOsBuild:t('hwOsBuild'), hwArch:t('hwArch'), hwHost:t('hwHost'),
            hwCpu:t('hwCpu'), hwCpuFreq:t('hwCpuFreq'), hwSockets:t('hwSockets'), hwCores:t('hwCores'),
            hwRam:t('hwRam'), hwMotherboard:t('hwMotherboard'), hwBios:t('hwBios'),
            diskListRows:document.querySelectorAll('#sysDiskList .disk-cap').length,
            charts:document.querySelectorAll('#page-system .chart-box.has-empty').length,
            pageVisible:vis('page-system')
          });
        })()""",
        "returnByValue": True,
    }, 10)
    val = probe.get("result", {}).get("value", "")
    print("=== SYSTEM DOM ===")
    try: print(json.dumps(json.loads(val), indent=2, ensure_ascii=False))
    except Exception: print(val)
    print("=== CONSOLE/ERRORS ===")
    # categorize
    exc = [e for e in events if e[0] in ("exception",)]
    warn = [e for e in events if e[0] == "warning"]
    err = [e for e in events if e[0] == "error"]
    print("uncaught exceptions: %d" % len(exc))
    for t, m in exc: print("  [EXC] %s" % m)
    print("console.error: %d" % len(err))
    for t, m in err: print("  [ERR] %s" % m)
    print("console.warn: %d" % len(warn))
    for t, m in warn[:8]: print("  [WRN] %s" % m)
    ws.close(); print("DONE")

if __name__ == "__main__":
    main()
