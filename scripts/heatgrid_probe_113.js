// 1.1.3 Heat Grid DOM 探针：验证真 per-core 渲染（格子数/数字/切换按钮/不溢出）
const { spawn } = require("node:child_process");
const http = require("node:http");
const path = require("node:path");
const fs = require("node:fs");
const ROOT = path.join(__dirname, "..");
const OUT = path.join(ROOT, "artifacts", "ui-113");
fs.mkdirSync(OUT, { recursive: true });
const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const BASE = process.argv[2] || "http://127.0.0.1:8790";
function getDebuggerUrl() {
  return new Promise((res, rej) => {
    http.get("http://127.0.0.1:9333/json/version", (r) => {
      let b = ""; r.on("data", (c) => (b += c));
      r.on("end", () => { try { res(JSON.parse(b).webSocketDebuggerUrl); } catch (e) { rej(e); } });
    }).on("error", rej);
  });
}
class CDP {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); }
  static async connect(url) {
    const ws = new WebSocket(url);
    await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
    const c = new CDP(ws);
    ws.onmessage = (ev) => {
      const m = JSON.parse(typeof ev.data === "string" ? ev.data : ev.data.toString());
      if (m.id && c.pending.has(m.id)) { c.pending.get(m.id)(m); c.pending.delete(m.id); }
    };
    return c;
  }
  send(m, p = {}, s) {
    return new Promise((res, rej) => {
      const id = ++this.id;
      this.pending.set(id, (msg) => (msg.error ? rej(new Error(m + ": " + JSON.stringify(msg.error))) : res(msg.result)));
      this.ws.send(JSON.stringify({ id, method: m, params: p, ...(s ? { sessionId: s } : {}) }));
      setTimeout(() => { if (this.pending.has(id)) { this.pending.delete(id); rej(new Error(m + " timeout")); } }, 30000);
    });
  }
  close() { try { this.ws.close(); } catch {} }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const chrome = spawn(CHROME, ["--headless=new", "--remote-debugging-port=9333", "--no-first-run",
    "--no-default-browser-check", "--user-data-dir=" + path.join(OUT, "_hg"), "about:blank"], { stdio: "ignore" });
  let url = null;
  for (let i = 0; i < 40 && !url; i++) { await sleep(500); try { url = await getDebuggerUrl(); } catch {} }
  if (!url) { console.error("no cdp"); process.exit(2); }
  const cdp = await CDP.connect(url);
  const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
  const send = (m, p = {}) => cdp.send(m, p, sessionId);
  await send("Runtime.enable"); await send("Page.enable");
  const ev = (e) => send("Runtime.evaluate", { expression: e, returnByValue: true }).then((r) => r.result.value);
  const evA = (e) => send("Runtime.evaluate", { expression: e, returnByValue: true, awaitPromise: true }).then((r) => r.result.value);
  await send("Emulation.setDeviceMetricsOverride", { width: 1920, height: 1080, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url: BASE + "/" });
  await sleep(7000);
  // 切到 system 页
  await ev(`LM.nav.showPage('system')`);
  await sleep(1500);
  // 展开 Heat Grid details（可能被 auto-fold 折叠）
  await ev(`(function(){ var d=document.getElementById('coreHeatFold'); if(d){ d.open=true; } return 1; })()`);
  await sleep(1200);
  const r = {};
  r.logical = await evA(`(function(){ return fetch('/api/system/inventory').then(function(x){return x.json();}).then(function(d){ return {logical: d.inventory.logical_cpus, physical: d.inventory.physical_cores, groups: d.inventory.core_groups ? d.inventory.core_groups.length : null}; }); })()`);
  // per-core API 直读
  r.apiPerCore = await evA(`(function(){ return fetch('/api/system/status').then(function(x){return x.json();}).then(function(d){ var p=d.cpu.per_core_percent; return {count: p?p.length:null, first6: p?p.slice(0,6):null, agg: d.cpu.usage_percent}; }); })()`);
  // DOM 测量
  r.cells = await ev(`document.querySelectorAll('.core-heat-grid .core-cell').length`);
  r.cellsWithVal = await ev(`Array.prototype.filter.call(document.querySelectorAll('.core-heat-grid .core-cell .core-val'), function(v){ return v.textContent.trim() && v.textContent.trim() !== '–'; }).length`);
  r.valSamples = await ev(`Array.prototype.slice.call(document.querySelectorAll('.core-heat-grid .core-cell .core-val')).slice(0,12).map(function(v){ return v.textContent; })`);
  r.toggleBtn = await ev(`(function(){ var b=document.querySelector('.core-heat-toggle'); return b ? b.textContent : 'none'; })()`);
  r.noteText = await ev(`(function(){ var n=document.querySelector('.core-heat-head .stat-hint'); return n ? n.textContent : 'none'; })()`);
  // 不溢出：grid 宽度 vs 容器
  r.gridWidth = await ev(`(function(){ var g=document.querySelector('.core-heat-grid'); if(!g) return null; var rc=g.getBoundingClientRect(); return Math.round(rc.width); })()`);
  r.cardScrollW = await ev(`(function(){ var g=document.querySelector('.core-heat-grid'); if(!g) return null; var c=g.closest('.card')||g.parentElement; return Math.round(c.clientWidth); })()`);
  r.scrollsX = await ev(`(function(){ var g=document.querySelector('.core-heat-grid'); if(!g) return null; var c=g.closest('.card')||g.parentElement; return c.scrollWidth > c.clientWidth + 1; })()`);
  // 切换逻辑核
  await ev(`(function(){ var b=document.querySelector('.core-heat-toggle'); if(b) b.click(); return 1; })()`);
  await sleep(800);
  r.logicalCells = await ev(`document.querySelectorAll('.core-heat-grid .core-cell').length`);
  r.noteAfterToggle = await ev(`(function(){ var n=document.querySelector('.core-heat-head .stat-hint'); return n ? n.textContent : 'none'; })()`);
  console.log(JSON.stringify(r, null, 2));
  const { data } = await send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync(path.join(OUT, "heatgrid-desktop-verify.png"), Buffer.from(data, "base64"));
  cdp.close(); chrome.kill();
  process.exit(0);
})().catch((e) => { console.error("HG-ERR " + e.message); process.exit(1); });
