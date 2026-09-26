// ui_screenshot_111.js — 1.1.1 PASS I 交付截图（8 页 × dark/light = 16 张）。
// Headless Chrome CDP + Node 24 内置 WebSocket。无依赖。
// 输出到 OUT 目录：shot-<page>-<theme>.png。
const { spawn } = require("node:child_process");
const path = require("node:path");
const os = require("node:os");
const fs = require("node:fs");
const http = require("node:http");

const BASE = process.env.LM_BASE || "http://127.0.0.1:8765";
const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const OUT = process.env.LM_SHOT_OUT || path.resolve(os.tmpdir(), "lm111_shots");
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

function wsUrlFromHeaders(h) { const m = (h["webSocketDebuggerUrl"] || "").match(/ws:\/\/[^\s"]+/); return m ? m[0] : null; }
async function getDebuggerUrl() {
  return new Promise((resolve, reject) => {
    http.get("http://127.0.0.1:9333/json/version", (res) => {
      let b = ""; res.on("data", (c) => (b += c));
      res.on("end", () => { try { resolve(JSON.parse(b).webSocketDebuggerUrl); } catch (e) { reject(e); } });
    }).on("error", reject);
  });
}

class CDP {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); }
  static async connect(url) {
    const ws = new WebSocket(url);
    await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
    const c = new CDP(ws);
    ws.onmessage = (ev) => {
      const msg = JSON.parse(typeof ev.data === "string" ? ev.data : ev.data.toString());
      if (msg.id && c.pending.has(msg.id)) { c.pending.get(msg.id)(msg); c.pending.delete(msg.id); }
    };
    return c;
  }
  send(method, params = {}, sessionId) {
    return new Promise((resolve, reject) => {
      const id = ++this.id;
      this.pending.set(id, (msg) => (msg.error ? reject(new Error(method + ": " + JSON.stringify(msg.error))) : resolve(msg.result)));
      this.ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
      setTimeout(() => { if (this.pending.has(id)) { this.pending.delete(id); reject(new Error(method + " timeout")); } }, 30000);
    });
  }
  close() { try { this.ws.close(); } catch {} }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const chrome = spawn(CHROME, [
    "--headless=new", `--remote-debugging-port=9333`, "--no-first-run",
    "--no-default-browser-check", `--user-data-dir=${OUT}`,
    "--window-size=1600,1000", "about:blank",
  ], { stdio: "ignore" });
  let url = null;
  for (let i = 0; i < 40 && !url; i++) { await sleep(500); try { url = await getDebuggerUrl(); } catch {} }
  if (!url) { console.error("CDP: no debugger url"); process.exit(2); }
  const cdp = await CDP.connect(url);
  const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
  const send = (method, params = {}) => cdp.send(method, params, sessionId);

  await send("Runtime.enable");
  await send("Page.enable");
  // 固定 1600x1000 视口，截图用 captureBeyondViewport:false
  await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false });

  await send("Page.navigate", { url: BASE + "/" });
  await sleep(6000); // 初始加载 + 首轮轮询

  async function gotoPage(name) {
    const r = await send("Runtime.evaluate", {
      expression: `(function(){ var b=document.querySelector('.nav-item[data-page="${name}"]'); if(!b) return 'no-button'; b.click(); return 'clicked'; })()`,
      returnByValue: true,
    });
    return r.result.value;
  }
  async function setTheme(theme) {
    await send("Runtime.evaluate", {
      expression: `document.documentElement.setAttribute("data-theme","${theme}"); true`,
      returnByValue: true,
    });
    await sleep(700); // retheme + charts
  }
  async function shot(file) {
    const r = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    fs.writeFileSync(path.join(OUT, file), Buffer.from(r.data, "base64"));
  }

  const written = [];
  for (const theme of ["dark", "light"]) {
    await setTheme(theme);
    await sleep(1500);
    for (const [name] of PAGES) {
      const res = await gotoPage(name);
      if (res !== "clicked") { console.error("  ", name, "no-button"); continue; }
      await sleep(3200); // 数据 + 图表渲染
      const file = `shot-${name}-${theme}.png`;
      await shot(file);
      written.push(file);
      console.log("  ", theme, name, "->", file);
    }
  }

  console.log("SHOTS OK:", written.length, "->", OUT);
  cdp.close();
  chrome.kill();
  process.exit(0);
})().catch((e) => { console.error("SHOT FAIL:", e); process.exit(1); });
