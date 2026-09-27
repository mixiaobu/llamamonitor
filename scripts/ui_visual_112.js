// 临时：1.1.2 视觉巡检截图（CDP），供主会话 read_image 查看
const { spawn } = require("node:child_process");
const http = require("node:http");
const path = require("node:path");
const fs = require("node:fs");
const OUT = path.join(require("node:os").tmpdir(), "lm112_visual");
fs.mkdirSync(OUT, { recursive: true });
const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const BASE = "http://127.0.0.1:8790";
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
async function shot(send, ev, name) {
  const { data } = await send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync(path.join(OUT, name + ".png"), Buffer.from(data, "base64"));
  console.log("shot: " + name);
}
(async () => {
  const chrome = spawn(CHROME, ["--headless=new", "--remote-debugging-port=9333", "--no-first-run",
    "--no-default-browser-check", "--user-data-dir=" + path.join(OUT, "p"), "about:blank"], { stdio: "ignore" });
  let url = null;
  for (let i = 0; i < 40 && !url; i++) { await sleep(500); try { url = await getDebuggerUrl(); } catch {} }
  if (!url) { console.error("no cdp"); process.exit(2); }
  const cdp = await CDP.connect(url);
  const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
  const send = (m, p = {}) => cdp.send(m, p, sessionId);
  await send("Runtime.enable"); await send("Page.enable");
  const ev = (e) => send("Runtime.evaluate", { expression: e, returnByValue: true }).then((r) => r.result.value);
  const go = (sel, ms) => { ev(`(function(){ var b = document.querySelector(${JSON.stringify(sel)}); if (b) b.click(); return 1; })()`); return sleep(ms); };

  // ---------- 桌面 1920 ----------
  await send("Emulation.setDeviceMetricsOverride", { width: 1920, height: 1080, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url: BASE + "/" });
  await sleep(7000);
  await shot(send, ev, "d1_overview");
  await go('.nav-item[data-page="usage"]', 3500);
  await shot(send, ev, "d2_usage");
  await go('.nav-item[data-page="gpu"]', 4500);
  await shot(send, ev, "d3_gpu");
  await go('.nav-item[data-page="system"]', 4500);
  await shot(send, ev, "d4_system");

  // ---------- 手机 390 ----------
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
  await sleep(2500);
  await ev(`(function(){ var b = document.querySelector('.mnav-item[data-page="overview"]'); if (b) b.click(); return 1; })()`);
  await sleep(3000);
  await shot(send, ev, "m1_overview");
  await ev(`(function(){ var b = document.querySelector('.mnav-item[data-page="usage"]'); if (b) b.click(); return 1; })()`);
  await sleep(3500);
  await shot(send, ev, "m2_usage_cardrows");
  await ev(`(function(){ var b = document.querySelector('#mnavMore'); if (b) b.click(); return 1; })()`);
  await sleep(1200);
  await shot(send, ev, "m3_moresheet");
  await ev(`(function(){ var b = document.querySelector('.sheet-item[data-page="settings"]'); if (b) b.click(); return 1; })()`);
  await sleep(3000);
  await shot(send, ev, "m4_settings");
  await ev(`(function(){ var b = document.querySelector('.mnav-item[data-page="gpu"]'); if (b) b.click(); return 1; })()`);
  await sleep(4000);
  await shot(send, ev, "m5_gpu");
  await ev(`(function(){ var b = document.querySelector('.mnav-item[data-page="performance"]'); if (b) b.click(); return 1; })()`);
  await sleep(3500);
  await shot(send, ev, "m6_performance");

  cdp.close(); chrome.kill();
  console.log("done -> " + OUT);
  process.exit(0);
})().catch((e) => { console.error(e.message); process.exit(1); });
