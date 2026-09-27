// 1.1.3 断点边界探针：759/760/761/762 + 1099/1100 —— 验证断点切换无重复/无跳变/无横向溢出
const { spawn } = require("node:child_process");
const http = require("node:http");
const path = require("node:path");
const ROOT = path.join(__dirname, "..");
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
    "--no-default-browser-check", "--user-data-dir=" + path.join(ROOT, "artifacts", "ui-113", "_bw"), "about:blank"], { stdio: "ignore" });
  let url = null;
  for (let i = 0; i < 40 && !url; i++) { await sleep(500); try { url = await getDebuggerUrl(); } catch {} }
  if (!url) { console.error("no cdp"); process.exit(2); }
  const cdp = await CDP.connect(url);
  const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
  const send = (m, p = {}) => cdp.send(m, p, sessionId);
  await send("Runtime.enable"); await send("Page.enable");
  const ev = (e) => send("Runtime.evaluate", { expression: e, returnByValue: true }).then((r) => r.result.value);
  await send("Page.navigate", { url: BASE + "/" });
  await sleep(6000);
  const widths = [759, 760, 761, 762, 1099, 1100, 1365, 1366];
  const pages = ["overview", "usage", "gpu", "system"];
  for (const w of widths) {
    await send("Emulation.setDeviceMetricsOverride", { width: w, height: 900, deviceScaleFactor: 1, mobile: w <= 760 });
    await sleep(900);
    const row = { width: w };
    // 关键断点信号：mobile 导航可见性 / 侧边栏可见性 / 横向溢出
    row.mobileNav = await ev(`(function(){ var n=document.querySelector('.mobile-nav'); if(!n) return 'none'; var cs=getComputedStyle(n); return cs.display !== 'none' ? 'visible' : 'hidden'; })()`);
    row.rail = await ev(`(function(){ var r=document.querySelector('.navview'); if(!r) return 'none'; return getComputedStyle(r).display !== 'none' ? 'visible' : 'hidden'; })()`);
    // 每个页面横向溢出检查
    const overflows = [];
    for (const p of pages) {
      await ev(`LM.nav.showPage(${JSON.stringify(p)})`);
      await sleep(400);
      const of = await ev(`(function(){ var de=document.documentElement; var page=document.getElementById('page-'+${JSON.stringify(p)}); var pw=page?page.scrollWidth:0; return { docOverflow: de.scrollWidth > de.clientWidth + 1, pageScroll: pw, clientW: de.clientWidth }; })()`);
      if (of.docOverflow || of.pageScroll > of.clientW + 1) overflows.push(p + ":" + (of.pageScroll > of.clientW + 1 ? "page" : "doc") + "=" + of.pageScroll + "/" + of.clientW);
    }
    row.overflow = overflows.length ? overflows.join(" ") : "none";
    console.log(JSON.stringify(row));
  }
  cdp.close(); chrome.kill();
  process.exit(0);
})().catch((e) => { console.error("BW-ERR " + e.message); process.exit(1); });
