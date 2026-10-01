const { spawn } = require("node:child_process");
const http = require("node:http");
const path = require("node:path");
const OUT = path.join(__dirname, "..", "artifacts", "edge-ovr-r2");
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const BASE = "http://127.0.0.1:8790";
const PORT = 9336;
function dbg() { return new Promise((res, rej) => { http.get("http://127.0.0.1:" + PORT + "/json/version", (r) => { let b = ""; r.on("data", (c) => (b += c)); r.on("end", () => res(JSON.parse(b).webSocketDebuggerUrl)); }).on("error", rej); }); }
class CDP {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); }
  static async connect(url) { const ws = new WebSocket(url); await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; }); const c = new CDP(ws); ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && c.pending.has(m.id)) { c.pending.get(m.id)(m); c.pending.delete(m.id); } }; return c; }
  send(m, p = {}, s) { return new Promise((res, rej) => { const id = ++this.id; this.pending.set(id, (msg) => (msg.error ? rej(new Error(m + ": " + JSON.stringify(msg.error))) : res(msg.result))); this.ws.send(JSON.stringify({ id, method: m, params: p, ...(s ? { sessionId: s } : {}) })); setTimeout(() => { if (this.pending.has(id)) { this.pending.delete(id); rej(new Error(m + " timeout")); } }, 20000); }); }
  close() { try { this.ws.close(); } catch {} }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const e = spawn(EDGE, ["--headless=new", "--remote-debugging-port=" + PORT, "--no-first-run", "--no-default-browser-check", "--disable-gpu", "--lang=zh-CN", "--user-data-dir=" + path.join(OUT, "_p3"), "about:blank"], { stdio: "ignore" });
  let url = null; for (let i = 0; i < 40 && !url; i++) { await sleep(500); try { url = await dbg(); } catch {} }
  const cdp = await CDP.connect(url);
  const { targetId } = await cdp.send("Target.createTarget", { url: BASE + "/" });
  const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
  const send = (m, p = {}) => cdp.send(m, p, sessionId);
  await send("Runtime.enable"); await send("Page.enable");
  const ev = (x) => send("Runtime.evaluate", { expression: x, returnByValue: true }).then((r) => r.result.value);
  const W = `
  (function(){
    function w(sel){ var el=document.querySelector(sel); if(!el) return null; return Math.round(el.getBoundingClientRect().width); }
    function cards(sel){ return Array.prototype.map.call(document.querySelectorAll(sel),function(k){var r=k.getBoundingClientRect();return Math.round(r.width);}); }
    // top→server distance + section gaps
    function y(sel){ var el=document.querySelector(sel); if(!el) return null; return Math.round(el.getBoundingClientRect().y); }
    return {
      contentW: w('.content-inner'),
      perfCardWidths: cards('.ov-perf-grid .perf-card'),
      gpuCardWidths: cards('.gpu-mini'),
      gpuCardCount: document.querySelectorAll('.gpu-mini').length,
      serverTop: y('.status-strip'), pageHeaderBottom: (function(){var el=document.querySelector('.page-header'); return el?Math.round(el.getBoundingClientRect().bottom):null;})(),
      sectionTitles: Array.prototype.map.call(document.querySelectorAll('#page-overview .sh-title'),function(t){return t.textContent.trim();}),
      gpuState: (document.getElementById('ovGpuState')||{}).textContent,
      gpuLine: (document.getElementById('ovGpuLine')||{}).textContent
    };
  })()`;
  for (const [tag, vw] of [["1920",1920],["1065",1065],["900",900],["1366",1366]]) {
    await send("Emulation.setDeviceMetricsOverride", { width: vw, height: 1000, deviceScaleFactor: 1, mobile: false });
    await send("Page.navigate", { url: BASE + "/" });
    await sleep(6000);
    await ev("localStorage.setItem('lm-theme','dark'); localStorage.setItem('lm_nav_manual','0'); 'ok'");
    await sleep(1500);
    console.log(tag, JSON.stringify(await ev(W)));
  }
  cdp.close(); e.kill(); process.exit(0);
})().catch((x) => { console.error("FATAL", x.message); process.exit(1); });
