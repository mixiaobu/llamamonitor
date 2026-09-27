// 临时：浏览器侧性能对比 1.1.1(8765, 已装) vs 1.1.2(8790, repo)
// 指标：navigation timing（load）、DOM 节点数、脚本执行时间、布局/渲染耗时
const { spawn } = require("node:child_process");
const http = require("node:http");
const path = require("node:path");
const fs = require("node:fs");
const OUT = path.join(require("node:os").tmpdir(), "lm112_perf");
fs.mkdirSync(OUT, { recursive: true });
const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const TARGETS = [
  { name: "111", base: "http://127.0.0.1:8765" },
  { name: "112", base: "http://127.0.0.1:8790" },
];
const VIEWS = [
  { label: "desktop", width: 1920, height: 1080, mobile: false },
  { label: "mobile", width: 390, height: 844, mobile: true },
];
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

const PERF_EXPR = `(function(){
  var nav = performance.getEntriesByType("navigation")[0] || {};
  var total = 0;
  performance.getEntriesByType("resource").forEach(function(r){ total += (r.responseEnd||0)-(r.startTime||0); });
  return JSON.stringify({
    domContentLoaded: Math.round(nav.domContentLoadedEventEnd||0),
    load: Math.round(nav.loadEventEnd||0),
    domNodes: document.getElementsByTagName("*").length,
    resTotal: Math.round(total)
  });
})()`;

(async () => {
  const chrome = spawn(CHROME, ["--headless=new", "--remote-debugging-port=9333", "--no-first-run",
    "--no-default-browser-check", "--user-data-dir=" + path.join(OUT, "p"), "about:blank"], { stdio: "ignore" });
  let url = null;
  for (let i = 0; i < 40 && !url; i++) { await sleep(500); try { url = await getDebuggerUrl(); } catch {} }
  if (!url) { console.error("no cdp"); process.exit(2); }
  const cdp = await CDP.connect(url);
  const results = [];
  for (const t of TARGETS) {
    for (const v of VIEWS) {
      const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
      const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
      const send = (m, p = {}) => cdp.send(m, p, sessionId);
      const ev = (e) => send("Runtime.evaluate", { expression: e, returnByValue: true }).then((r) => r.result.value);
      await send("Runtime.enable"); await send("Page.enable");
      await send("Emulation.setDeviceMetricsOverride", { width: v.width, height: v.height, deviceScaleFactor: 1, mobile: v.mobile });
      await send("Page.navigate", { url: t.base + "/" });
      await sleep(8000); // 等首屏 + 一轮轮询数据渲染
      const perf = JSON.parse(await ev(PERF_EXPR));
      results.push(Object.assign({ ver: t.name, view: v.label }, perf));
      await cdp.send("Target.closeTarget", { targetId });
      await sleep(500);
    }
  }
  console.table(results);
  cdp.close(); chrome.kill(); process.exit(0);
})().catch((e) => { console.error(e.message); process.exit(1); });
