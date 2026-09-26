// ui_patrol.js — 1.1.1 PASS D runtime UI audit (read-only)
// Headless Chrome CDP + Node 24 built-in WebSocket. No dependencies.
// Navigates every page, collects console errors / page errors / failed network
// requests, and dumps key DOM text per page for verification.
const { spawn } = require("node:child_process");
const path = require("node:path");
const os = require("node:os");
const fs = require("node:fs");
const http = require("node:http");

const BASE = "http://127.0.0.1:8765";
const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const OUT = path.resolve(os.tmpdir(), "lm111_patrol_" + Date.now());
fs.mkdirSync(OUT, { recursive: true });

const PAGES = [
  ["overview", "概览"],
  ["usage", "Token 用量"],
  ["performance", "推理性能"],
  ["system", "系统"],
  ["gpu", "GPU 监控"],
  ["history", "监控历史"],
  ["settings", "设置"],
  ["about", "关于"],
];

function wsUrlFromHeaders(headers) {
  const m = (headers["webSocketDebuggerUrl"] || "").match(/ws:\/\/[^\s"]+/);
  return m ? m[0] : null;
}

async function getDebuggerUrl() {
  return new Promise((resolve, reject) => {
    http
      .get("http://127.0.0.1:9333/json/version", (res) => {
        let b = "";
        res.on("data", (c) => (b += c));
        res.on("end", () => {
          try { resolve(JSON.parse(b).webSocketDebuggerUrl); } catch (e) { reject(e); }
        });
      })
      .on("error", reject);
  });
}

class CDP {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); this.handlers = []; }
  static async connect(url) {
    const ws = new WebSocket(url);
    await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
    const c = new CDP(ws);
    ws.onmessage = (ev) => {
      const msg = JSON.parse(typeof ev.data === "string" ? ev.data : ev.data.toString());
      if (msg.id && c.pending.has(msg.id)) { c.pending.get(msg.id)(msg); c.pending.delete(msg.id); }
      else if (msg.method) { for (const h of c.handlers) h(msg); }
    };
    return c;
  }
  send(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = ++this.id;
      this.pending.set(id, (msg) => (msg.error ? reject(new Error(method + ": " + JSON.stringify(msg.error))) : resolve(msg.result)));
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => { if (this.pending.has(id)) { this.pending.delete(id); reject(new Error(method + " timeout")); } }, 20000);
    });
  }
  on(fn) { this.handlers.push(fn); }
  close() { try { this.ws.close(); } catch {} }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const chrome = spawn(CHROME, [
    "--headless=new",
    `--remote-debugging-port=9333`,
    "--no-first-run",
    "--no-default-browser-check",
    `--user-data-dir=${OUT}`,
    "--window-size=1920,1080",
    "about:blank",
  ], { stdio: "ignore" });
  // wait for devtools port
  let url = null;
  for (let i = 0; i < 40 && !url; i++) { await sleep(500); try { url = await getDebuggerUrl(); } catch {} }
  if (!url) { console.error("CDP: no debugger url"); process.exit(2); }
  const cdp = await CDP.connect(url);
  const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
  // session-scoped send: reuse pending map with sessionId prefix
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++cdp.id;
    cdp.pending.set(id, (msg) => (msg.error ? reject(new Error(method + ": " + JSON.stringify(msg.error))) : resolve(msg.result)));
    cdp.ws.send(JSON.stringify({ id, method, params, sessionId }));
    setTimeout(() => { if (cdp.pending.has(id)) { cdp.pending.delete(id); reject(new Error(method + " timeout")); } }, 30000);
  });
  const report = { pages: {}, consoleErrors: [], pageErrors: [], failedRequests: [] };
  cdp.on((msg) => {
    if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error")
      report.consoleErrors.push((msg.params.args || []).map((a) => a.value || a.description || "").join(" "));
    if (msg.method === "Runtime.exceptionThrown")
      report.pageErrors.push(JSON.stringify(msg.params.exceptionDetails.exception && msg.params.exceptionDetails.exception.description || msg.params.exceptionDetails.text));
    if (msg.method === "Network.loadingFailed")
      report.failedRequests.push(JSON.stringify(msg.params));
  });
  await send("Runtime.enable");
  await send("Page.enable");
  await send("Network.enable");
  await send("Page.navigate", { url: BASE + "/" });
  await sleep(6000); // initial load + first polls

  async function snapshotPageText() {
    const r = await send("Runtime.evaluate", {
      expression: `(function(){
        var out = {};
        // health/status line
        var el = document.getElementById('statusText'); if (el) out.statusText = el.textContent.trim().slice(0,200);
        var ov = document.getElementById('ovLastUpdate'); if (ov) out.ovLastUpdate = JSON.stringify(ov.textContent.trim());
        // page title of active page
        var act = document.querySelector('section.page.active');
        if (act) { out.pageTitle = (act.querySelector('.page-title, h1') || {}).textContent || ''; out.textLen = act.innerText.length; }
        // empty-state elements visible
        out.emptyStates = Array.from(document.querySelectorAll('section.page.active .empty, section.page.active [class*=empty]')).filter(e=>e.offsetParent!==null).map(e=>e.textContent.trim().slice(0,80));
        // visible -- count in active page metrics (rough stale/na signal)
        var mm = (act ? act.innerText : '').match(/--/g); out.dashCount = mm ? mm.length : 0;
        // charts rendered?
        out.canvasCount = document.querySelectorAll('section.page.active canvas').length;
        return JSON.stringify(out);
      })()`,
      returnByValue: true,
    });
    return JSON.parse(r.result.value);
  }

  report.pages.overview = await snapshotPageText();

  for (const [name, label] of PAGES.slice(1)) {
    const clicked = await send("Runtime.evaluate", {
      expression: `(function(){
        var b = document.querySelector('.nav-item[data-page="${name}"]');
        if (!b) return 'no-button';
        b.click(); return 'clicked';
      })()`,
      returnByValue: true,
    });
    if (clicked.result.value !== "clicked") { report.pages[name] = { error: clicked.result.value }; continue; }
    await sleep(3500);
    report.pages[name] = await snapshotPageText();
  }

  // extra: settings sections — click through each rail item and confirm no console errors
  const sections = await send("Runtime.evaluate", {
    expression: `Array.from(document.querySelectorAll('[data-section]')).map(e=>e.dataset.section).join(',')`,
    returnByValue: true,
  });
  const before = report.consoleErrors.length;
  for (const s of (sections.result.value || "").split(",").filter(Boolean)) {
    await send("Runtime.evaluate", {
      expression: `(function(){var b=document.querySelector('[data-section="${s}"]'); if(b) b.click();})()`,
    });
    await sleep(800);
  }
  report.settingsSectionsWalked = sections.result.value;
  report.settingsConsoleErrors = report.consoleErrors.slice(before);

  fs.writeFileSync(path.join(OUT, "patrol.json"), JSON.stringify(report, null, 2), "utf-8");
  console.log("PATROL OK -> " + path.join(OUT, "patrol.json"));
  console.log("consoleErrors:", report.consoleErrors.length, "pageErrors:", report.pageErrors.length, "failedRequests:", report.failedRequests.length);
  for (const [k, v] of Object.entries(report.pages)) console.log(" ", k, JSON.stringify(v).slice(0, 300));
  cdp.close();
  chrome.kill();
  process.exit(0);
})().catch((e) => { console.error("PATROL FAIL:", e); process.exit(1); });
