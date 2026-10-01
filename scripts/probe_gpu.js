const { spawn } = require("node:child_process");
const http = require("node:http");
const path = require("node:path");
const OUT = path.join(__dirname, "..", "artifacts", "edge-ovr-r2");
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const BASE = "http://127.0.0.1:8790";
const PORT = 9337;
function dbg() { return new Promise((res, rej) => { http.get("http://127.0.0.1:" + PORT + "/json/version", (r) => { let b = ""; r.on("data", (c) => (b += c)); r.on("end", () => res(JSON.parse(b).webSocketDebuggerUrl)); }).on("error", rej); }); }
class CDP {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); }
  static async connect(url) { const ws = new WebSocket(url); await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; }); const c = new CDP(ws); ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && c.pending.has(m.id)) { c.pending.get(m.id)(m); c.pending.delete(m.id); } }; return c; }
  send(m, p = {}, s) { return new Promise((res, rej) => { const id = ++this.id; this.pending.set(id, (msg) => (msg.error ? rej(new Error(m + ": " + JSON.stringify(msg.error))) : res(msg.result))); this.ws.send(JSON.stringify({ id, method: m, params: p, ...(s ? { sessionId: s } : {}) })); setTimeout(() => { if (this.pending.has(id)) { this.pending.delete(id); rej(new Error(m + " timeout")); } }, 20000); }); }
  close() { try { this.ws.close(); } catch {} }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const e = spawn(EDGE, ["--headless=new", "--remote-debugging-port=" + PORT, "--no-first-run", "--no-default-browser-check", "--disable-gpu", "--lang=zh-CN", "--user-data-dir=" + path.join(OUT, "_p4"), "about:blank"], { stdio: "ignore" });
  let url = null; for (let i = 0; i < 40 && !url; i++) { await sleep(500); try { url = await dbg(); } catch {} }
  const cdp = await CDP.connect(url);
  const { targetId } = await cdp.send("Target.createTarget", { url: BASE + "/" });
  const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
  const send = (m, p = {}) => cdp.send(m, p, sessionId);
  await send("Runtime.enable"); await send("Page.enable");
  await send("Emulation.setDeviceMetricsOverride", { width: 1400, height: 1000, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url: BASE + "/" });
  await sleep(7000);
  await send("Runtime.evaluate", { expression: "localStorage.setItem('lm-theme','dark');localStorage.setItem('lm_nav_manual','0');1" });
  await sleep(1200);
  const ev = (x) => send("Runtime.evaluate", { expression: x, returnByValue: true }).then((r) => r.result.value);
  const out = await ev(`(function(){
    var cards = document.querySelectorAll('.gpu-mini');
    var data = [];
    for (var i=0;i<cards.length;i++){
      var c = cards[i];
      var title = (c.querySelector('.gm-title')||{}).textContent||'';
      var metrics = Array.prototype.map.call(c.querySelectorAll('.gm-metric'), function(m){
        var k = (m.querySelector('.gm-k')||m).textContent.trim();
        var v = (m.querySelector('.v')||m).textContent.trim();
        return k+'='+v;
      });
      data.push({ title: title.trim(), metrics: metrics });
    }
    var banner = document.querySelector('.remote-banner');
    var br = banner ? banner.getBoundingClientRect() : null;
    return {
      gpuBadgeText: (document.getElementById('ovGpuState')||{}).textContent,
      gpuCards: data,
      remoteBannerVisible: br ? (br.width>0 && br.height>0) : false,
      remoteBannerText: br && br.width>0 ? (banner.textContent||'').trim().replace(/\\s+/g,' ').slice(0,80) : null
    };
  })()`);
  console.log(JSON.stringify(out, null, 1));
  cdp.close(); e.kill(); process.exit(0);
})().catch((x) => { console.error("FATAL", x.message); process.exit(1); });
