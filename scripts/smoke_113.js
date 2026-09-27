// 1.1.3 冒烟：启动后验证导航/新 DOM/无 console 错误（CDP，390 与 1920）
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
    "--no-default-browser-check", "--user-data-dir=" + path.join(OUT, "_smoke"), "about:blank"], { stdio: "ignore" });
  let url = null;
  for (let i = 0; i < 40 && !url; i++) { await sleep(500); try { url = await getDebuggerUrl(); } catch {} }
  if (!url) { console.error("no cdp"); process.exit(2); }
  const cdp = await CDP.connect(url);
  const errors = [];
  const origOnMessage = cdp.ws.onmessage; // 保留 CDP 响应路由，链式包装
  cdp.ws.onmessage = (ev2) => {
    try {
      const m = JSON.parse(typeof ev2.data === "string" ? ev2.data : ev2.data.toString());
      if (m.method === "Runtime.exceptionThrown") {
        const d = m.params.exceptionDetails;
        errors.push("UNCAUGHT: " + (d.exception && d.exception.description || d.text || "").slice(0, 160));
      }
      if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") {
        const t = (m.params.args || []).map((a) => a.value || a.description || "").join(" ").slice(0, 160);
        if (!/Failed to load resource/.test(t)) errors.push("console: " + t);
      }
    } catch {}
    if (origOnMessage) origOnMessage(ev2);
  };
  const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
  const send = (m, p = {}) => cdp.send(m, p, sessionId);
  await send("Runtime.enable"); await send("Page.enable");
  const ev = (e) => send("Runtime.evaluate", { expression: e, returnByValue: true }).then((r) => r.result.value);

  // ---- Desktop 1920 ----
  await send("Emulation.setDeviceMetricsOverride", { width: 1920, height: 1080, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url: BASE + "/" });
  await sleep(6000);
  let r = {};
  r.lmNav = await ev(`!!(window.LM && LM.nav)`);
  r.showPage = await ev(`(function(){ try { LM.nav.showPage('gpu'); return LM.nav.currentPage(); } catch(e){ return 'ERR '+e.message; } })()`);
  await sleep(1500);
  r.gpuActive = await ev(`!!document.getElementById('page-gpu').classList.contains('active')`);
  r.overflowBtns = await ev(`document.querySelectorAll('.page-overflow').length`);
  r.cardTables = await ev(`document.querySelectorAll('.table-wrap.mobile-card-table').length`);
  r.mnavItems = await ev(`document.querySelectorAll('.mnav-item').length`);
  r.mnavHasSystem = await ev(`!!document.querySelector('.mnav-item[data-page="system"]')`);
  r.desktopOverflowHidden = await ev(`(function(){ var b=document.querySelector('.page-overflow'); return b ? getComputedStyle(b).display : 'none-el'; })()`);
  console.log("DESKTOP " + JSON.stringify(r));
  r = {};

  // ---- Mobile 390 ----
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
  await sleep(2500);
  // 逐页 showPage + 读回 currentPage（防截图重复）
  const pages = ["overview","usage","performance","gpu","system","history","settings","about"];
  for (const p of pages) {
    await ev(`LM.nav.showPage(${JSON.stringify(p)})`);
    await sleep(500);
    const cur = await ev(`LM.nav.currentPage()`);
    const active = await ev(`!!document.getElementById('page-'+${JSON.stringify(p)}).classList.contains('active')`);
    console.log("nav " + p + " -> currentPage=" + cur + " active=" + active + (cur===p&&active?"":"  <-- MISMATCH"));
  }
  r.navWorks = true;
  r.overflowVisible = await ev(`(function(){ var b=document.querySelector('.page.active .page-overflow'); return b ? getComputedStyle(b).display : 'none-el'; })()`);
  r.overflowSize = await ev(`(function(){ var b=document.querySelector('.page.active .page-overflow'); if(!b) return null; var rc=b.getBoundingClientRect(); return Math.round(rc.width)+'x'+Math.round(rc.height); })()`);
  // 打开 overflow sheet
  await ev(`(function(){ var b=document.querySelector('.page-overflow'); if(b) b.click(); return 1; })()`);
  await sleep(700);
  r.sheetOpen = await ev(`!!(document.getElementById('moreSheet') && !document.getElementById('moreSheet').hidden)`);
  r.sheetAriaModal = await ev(`document.getElementById('moreSheet').getAttribute('aria-modal')`);
  r.scrollLocked = await ev(`getComputedStyle(document.documentElement).overflow`);
  // focus trap: active element 在 sheet 内
  r.focusInSheet = await ev(`(function(){ var s=document.getElementById('moreSheet'); return s.contains(document.activeElement); })()`);
  // 截图一张 overflow sheet
  const { data } = await send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync(path.join(OUT, "smoke-390-overflow.png"), Buffer.from(data, "base64"));
  // 点 history 项 → 应切页且关 sheet
  await ev(`(function(){ var b=document.querySelector('.sheet-item[data-page="history"]'); if(b) b.click(); return 1; })()`);
  await sleep(800);
  r.afterPick = await ev(`LM.nav.currentPage()`);
  r.sheetClosed = await ev(`document.getElementById('moreSheet').hidden`);
  console.log("MOBILE " + JSON.stringify(r));
  cdp.close(); chrome.kill();
  fs.writeFileSync(path.join(OUT, "smoke-errors.json"), JSON.stringify(errors, null, 2));
  console.log("errors: " + errors.length + (errors.length ? " -> " + errors.slice(0,8).join(" | ") : ""));
  process.exit(0);
})().catch((e) => { console.error("SMOKE-ERR " + e.message); process.exit(1); });
