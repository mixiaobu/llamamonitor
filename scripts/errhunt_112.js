// errhunt_112.js — 复刻 patrol 的错误收集口径，打印每个错误的完整文本。
const { spawn } = require("node:child_process");
const http = require("node:http");
const path = require("node:path");
const fs = require("node:fs");
const BASE = process.env.LM_BASE || "http://127.0.0.1:8790";
const OUT = process.env.LM_SHOT_OUT || path.resolve(require("node:os").tmpdir(), "lm112_err");
fs.mkdirSync(OUT, { recursive: true });
const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const PAGES = ["overview", "usage", "performance", "system", "gpu", "history", "settings", "about"];

function dbg() {
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
    "--user-data-dir=" + path.join(OUT, "p"), "about:blank"], { stdio: "ignore" });
  let url = null;
  for (let i = 0; i < 40 && !url; i++) { await sleep(500); try { url = await dbg(); } catch {} }
  if (!url) { console.error("no cdp"); process.exit(2); }
  const cdp = await CDP.connect(url);
  const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
  const send = (m, p = {}) => cdp.send(m, p, sessionId);
  await send("Runtime.enable"); await send("Page.enable"); await send("Log.enable");

  const errors = [];
  cdp.ws.onmessage = (ev) => {
    const m = JSON.parse(typeof ev.data === "string" ? ev.data : ev.data.toString());
    if (m.id && cdp.pending.has(m.id)) { cdp.pending.get(m.id)(m); cdp.pending.delete(m.id); }
    if (m.method === "Runtime.exceptionThrown")
      errors.push("EXC: " + JSON.stringify(m.params.exceptionDetails));
    else if (m.method === "Log.entryAdded" && m.params.entry.level === "error")
      errors.push("LOG[" + (m.params.entry.url || "") + "]: " + m.params.entry.text);
    else if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error")
      errors.push("CONSOLE: " + m.params.args.map((a) => a.value || a.description || JSON.stringify(a)).join(" "));
  };

  // 与 patrol 完全相同的遍历：mobile 各尺寸 + 所有页
  const MATRIX = [[390, 844], [320, 568], [768, 1024], [1920, 1080]];
  for (const [w, h] of MATRIX) {
    await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile: w <= 760 });
    await sleep(800);
    for (const page of PAGES) {
      await send("Runtime.evaluate", {
        expression: `(function(){ var b=document.querySelector('.nav-item[data-page="${page}"]')||document.querySelector('.mnav-item[data-page="${page}"]'); if(!b){ var s=document.querySelector('.sheet-item[data-page="${page}"]'); if(s) s.click(); } else b.click(); return 'x'; })()`,
        returnByValue: true,
      });
      await sleep(1200);
    }
  }
  await send("Page.navigate", { url: BASE + "/" });
  await sleep(5000);

  console.log("TOTAL ERRORS: " + errors.length);
  errors.slice(0, 30).forEach((e) => console.log("  " + e.slice(0, 400)));
  cdp.close(); chrome.kill(); process.exit(0);
})().catch((e) => { console.error("HUNT FAIL", e); process.exit(1); });
