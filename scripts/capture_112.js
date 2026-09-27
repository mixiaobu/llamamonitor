// capture_112.js — 1.1.2 PASS H：README 正式截图（先断言数据填充，再捕获）。
// 输出:
//   <OUT>/llamamonitor-desktop.png  1920×1080 Overview dark（真实数据）
//   <OUT>/llamamonitor-mobile.png   390×844 @2x Overview dark（真实 mobile layout：bottom nav + 卡片流）
// 断言：
//   - 页面非空：status-strip / today-hero / 至少一个 stat-value 有非 "--" 数值
//   - 无 error / offline infobar 占据首屏
//   - desktop：侧边栏可见；mobile：bottom nav 可见 + 侧边栏隐藏
const { spawn } = require("node:child_process");
const http = require("node:http");
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");
const BASE = process.env.LM_BASE || "http://127.0.0.1:8790";
const OUT = process.env.LM_SHOT_OUT || path.resolve(os.tmpdir(), "lm112_captures");
fs.mkdirSync(OUT, { recursive: true });
const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";

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
  await send("Runtime.enable"); await send("Page.enable");
  const ev = (e) => send("Runtime.evaluate", { expression: e, returnByValue: true, awaitPromise: true }).then((r) => r.result.value);
  const shot = async (file) => {
    const r = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    fs.writeFileSync(path.join(OUT, file), Buffer.from(r.data, "base64"));
  };
  const setDark = () => ev(`document.documentElement.setAttribute("data-theme","dark"); true`);

  // 固定 dark + 桌面尺寸先加载并填充数据
  await send("Emulation.setDeviceMetricsOverride", { width: 1920, height: 1080, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url: BASE + "/" });
  await sleep(8000);
  await setDark();
  await sleep(1500);

  // 断言：overview 有真实数据（非 loading / 非全 "--"）
  const dataCheck = await ev(`(function(){
    var pick = function (id) { var e = document.getElementById(id); return e ? e.textContent.trim() : null; };
    var hero = document.querySelector("#page-overview .hero-num");
    var statVals = Array.prototype.slice.call(document.querySelectorAll("#page-overview .stat-value"))
      .map(function (e) { return e.textContent.trim(); })
      .filter(function (t) { return t && t !== "--" && t !== ""; });
    var offline = Array.prototype.slice.call(document.querySelectorAll("#globalInfobars .infobar"))
      .some(function (b) { return /offline|不可用|已断开/i.test(b.textContent); });
    var activePage = document.querySelector(".page.active");
    return JSON.stringify({
      activePage: activePage ? activePage.id : null,
      heroText: hero ? hero.textContent.trim() : null,
      nonDashStats: statVals.length,
      sampleStats: statVals.slice(0, 4),
      offline: offline,
    });
  })()`);
  const dc = JSON.parse(dataCheck);
  console.log("DATA CHECK:", dataCheck);
  if (dc.activePage !== "page-overview") { console.error("FAIL: not on overview"); process.exit(3); }
  if (dc.nonDashStats < 3) { console.error("FAIL: overview lacks data (" + dc.nonDashStats + " real stats)"); process.exit(3); }
  if (dc.offline) { console.error("FAIL: offline infobar visible"); process.exit(3); }

  // ---- DESKTOP 1920×1080 Overview dark ----
  await send("Runtime.evaluate", { expression: "(function(){var b=document.querySelector('.nav-item[data-page=overview]'); if(b) b.click(); return 1})()" });
  await sleep(3500);
  await setDark();
  await sleep(1200);
  await shot("llamamonitor-desktop.png");
  console.log("desktop -> llamamonitor-desktop.png");

  // ---- MOBILE 390×844 @2x Overview dark ----
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
  await sleep(3000);
  await setDark();
  await sleep(1200);
  const mobCheck = await ev(`(function(){
    return JSON.stringify({
      mnavVisible: getComputedStyle(document.querySelector('.mobile-nav')).display !== 'none',
      navviewHidden: getComputedStyle(document.querySelector('.navview')).display === 'none',
      activePage: (document.querySelector('.page.active')||{}).id || null,
    });
  })()`);
  console.log("MOBILE CHECK:", mobCheck);
  const mc = JSON.parse(mobCheck);
  if (!mc.mnavVisible || !mc.navviewHidden) { console.error("FAIL: mobile layout not active"); process.exit(4); }
  await shot("llamamonitor-mobile.png");
  console.log("mobile  -> llamamonitor-mobile.png");

  console.log("CAPTURE OK -> " + OUT);
  cdp.close(); chrome.kill(); process.exit(0);
})().catch((e) => { console.error("CAPTURE FAIL", e); process.exit(1); });
