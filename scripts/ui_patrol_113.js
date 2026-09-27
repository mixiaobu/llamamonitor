// ui_patrol_113.js — 1.1.3 响应式矩阵 + overflow/touch gate + 截图（1.1.2 patrol 的 1.1.3 适配）。
// 变化：gotoPage 优先走 LM.nav.showPage API（1.1.3 导航重设计：5 核心底部导航 + ••• overflow sheet）。
// Headless Chrome CDP + Node 内置 WebSocket，无依赖。
// 用法:
//   LM_BASE=http://127.0.0.1:8790 LM_SHOT_OUT=... node scripts/ui_patrol_113.js
// 检查：
//   1. 每个 viewport × 每个页面：documentElement.scrollWidth <= clientWidth（无横向溢出）
//   2. ≤760px：Bottom Nav 存在且侧边栏隐藏；>760px 反之
//   3. ≤760px：Settings rail 单行（rail 高度 < 2 行）；save bar fixed 在 bottom-nav 上方
//   4. ≤760px：主要 tap target ≥44px 高（segmented/rail ≥40px）
//   5. 每页 console error 计数
//   6. README 截图：desktop 1920×1080 overview dark + mobile 390×844 overview dark
const { spawn } = require("node:child_process");
const path = require("node:path");
const os = require("node:os");
const fs = require("node:fs");
const http = require("node:http");

const BASE = process.env.LM_BASE || "http://127.0.0.1:8790";
const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const OUT = process.env.LM_SHOT_OUT || path.resolve(os.tmpdir(), "lm112_patrol");
fs.mkdirSync(OUT, { recursive: true });

const PAGES = ["overview", "usage", "performance", "system", "gpu", "history", "settings", "about"];

// 响应式矩阵（规格 65-72：6 手机 + 2 tablet + desktop 阶梯 + 2 横屏）
const MATRIX = [
  [320, 568], [360, 800], [375, 812], [390, 844], [412, 915], [430, 932],   // 手机
  [768, 1024], [820, 1180],                                                 // tablet
  [1024, 768], [1280, 800], [1366, 768], [1600, 900], [1920, 1080],
  [2560, 1440], [3840, 2160],                                               // desktop/4K
  [844, 390], [915, 412],                                                   // 横屏手机
];

function wsUrlFromHeaders(h) { const m = (h["webSocketDebuggerUrl"] || "").match(/ws:\/\/[^\s"]+/); return m ? m[0] : null; }
function getDebuggerUrl() {
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
      if (msg.method === "Runtime.exceptionThrown" || msg.method === "Log.entryAdded") {
        c.onEvent && c.onEvent(msg);
      }
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
    "--headless=new", "--remote-debugging-port=9333", "--no-first-run",
    "--no-default-browser-check", `--user-data-dir=${path.join(OUT, "profile")}`,
    "--window-size=1920,1080", "about:blank",
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
  await send("Log.enable");

  let jsErrors = 0;
  cdp.onEvent = (msg) => {
    if (msg.method === "Runtime.exceptionThrown") jsErrors++;
    else if (msg.method === "Log.entryAdded" && msg.params.entry.level === "error"
             && !/favicon|ERR_NAME_NOT_RESOLVED/.test(msg.params.entry.text)) jsErrors++;
  };

  // 首次加载
  await send("Page.navigate", { url: BASE + "/" });
  await sleep(7000);

  async function gotoPage(name) {
    // 1.1.3：优先 LM.nav.showPage（权威 API，含 scroll 记忆 + aria-current）；
    // DOM 点击仅作回退（.nav-item 桌面 / .mnav-item 移动 / .sheet-item overflow）。
    const r = await send("Runtime.evaluate", {
      expression: `(function(){
        try { if (window.LM && LM.nav && LM.nav.showPage) { LM.nav.showPage(${JSON.stringify(name)}); return 'api:' + LM.nav.currentPage(); } } catch (e) {}
        var b = document.querySelector('.nav-item[data-page="${name}"]') || document.querySelector('.mnav-item[data-page="${name}"]');
        if (b) { b.click(); return 'dom:' + name; }
        var s = document.querySelector('.sheet-item[data-page="${name}"]');
        if (s) { s.click(); return 'sheet:' + name; }
        return 'no-button';
      })()`,
      returnByValue: true,
    });
    return r.result.value;
  }
  async function setTheme(theme) {
    await send("Runtime.evaluate", {
      expression: `document.documentElement.setAttribute("data-theme","${theme}"); true`,
      returnByValue: true,
    });
    await sleep(900);
  }
  async function evalJs(expression) {
    const r = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    return r.result.value;
  }
  async function shot(file) {
    const r = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    fs.writeFileSync(path.join(OUT, file), Buffer.from(r.data, "base64"));
  }

  // ============ 1. 响应式矩阵：overflow + 结构 gate ============
  const report = [];
  let overflowFails = 0, structFails = 0;
  for (const [w, h] of MATRIX) {
    await send("Emulation.setDeviceMetricsOverride", {
      width: w, height: h, deviceScaleFactor: 1,
      mobile: w <= 760,
    });
    await sleep(1200);
    for (const page of PAGES) {
      const res = await gotoPage(page);
      await sleep(2200);
      const r = await evalJs(`(function(){
        var de = document.documentElement;
        var ov = { scrollWidth: de.scrollWidth, clientWidth: de.clientWidth };
        var navVisible = getComputedStyle(document.querySelector('.navview')).display !== 'none';
        var mnavVisible = getComputedStyle(document.querySelector('.mobile-nav')).display !== 'none';
        var rail = document.querySelector('.settings-rail');
        var railH = rail ? rail.getBoundingClientRect().height : 0;
        var footer = document.querySelector('.settings-footer');
        var footerPos = footer ? getComputedStyle(footer).position : 'none';
        return JSON.stringify({ ov: ov, navVisible: navVisible, mnavVisible: mnavVisible, railH: railH, footerPos: footerPos });
      })()`);
      const d = JSON.parse(r);
      const mobile = w <= 760;
      let fail = null;
      // Gate 1：无横向溢出（内部横滚容器自带 overflow-x:auto，不会撑大 document）
      if (d.ov.scrollWidth > d.ov.clientWidth) {
        fail = "overflow " + d.ov.scrollWidth + ">" + d.ov.clientWidth;
        overflowFails++;
      }
      // Gate 2：导航结构
      if (mobile && (d.navVisible || !d.mnavVisible)) { fail = (fail ? fail + "; " : "") + "nav wrong"; structFails++; }
      if (!mobile && d.mnavVisible) { fail = (fail ? fail + "; " : "") + "mnav visible on desktop"; structFails++; }
      // Gate 3：settings rail 单行（高度 < 80px ≈ 1 行 40px + padding）
      if (mobile && page === "settings" && d.railH > 80) { fail = (fail ? fail + "; " : "") + "rail multi-row " + d.railH; structFails++; }
      // Gate 4：mobile settings save bar 必须 fixed
      if (mobile && page === "settings" && d.footerPos !== "fixed") { fail = (fail ? fail + "; " : "") + "savebar " + d.footerPos; structFails++; }
      report.push({ vp: w + "x" + h, page: page, ok: !fail, fail: fail, ov: d.ov });
      if (fail) console.log("  FAIL " + w + "x" + h + " " + page + ": " + fail);
    }
  }

  // ============ 2. Touch target gate（390×844） ============
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await sleep(1500);
  const touchReport = await evalJs(`(function(){
    var out = [];
    var sels = [
      [".mnav-item", 44], [".sheet-item", 44], [".btn", 44], [".check-chip", 40],
      [".rail-item", 40], [".seg button", 40], [".tip-btn", 16],
      [".nav-item", 44], [".switch", 40], [".page-overflow", 44],
    ];
    var vis = function (el) { var r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    sels.forEach(function (s) {
      var minH = s[1], sel = s[0];
      var els = Array.prototype.slice.call(document.querySelectorAll(sel));
      els.forEach(function (el) {
        if (!vis(el)) return;
        var r = el.getBoundingClientRect();
        if (r.height < minH - 0.5) out.push({ sel: sel, h: Math.round(r.height), min: minH, text: (el.textContent || "").slice(0, 12) });
      });
    });
    return JSON.stringify(out);
  })()`);
  const touchFails = JSON.parse(touchReport);
  // 去重（同选择器同高度只报一次）
  const seen = new Set();
  const touchUnique = touchFails.filter((f) => { const k = f.sel + "|" + f.h; if (seen.has(k)) return false; seen.add(k); return true; });

  // ============ 3. README 截图 ============
  await setTheme("dark");
  // desktop 1920×1080 overview
  await send("Emulation.setDeviceMetricsOverride", { width: 1920, height: 1080, deviceScaleFactor: 1, mobile: false });
  await sleep(1500);
  await gotoPage("overview");
  await sleep(4000);
  await shot("llamamonitor-desktop.png");
  // mobile 390×844 overview（真实 mobile layout：bottom nav + 卡片流）
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
  await sleep(2000);
  await shot("llamamonitor-mobile.png");

  // ============ 汇总 ============
  const totalChecks = report.length;
  const okChecks = report.filter((r) => r.ok).length;
  console.log("=== 1.1.3 PATROL RESULT ===");
  console.log("matrix: " + totalChecks + " viewport x page checks, " + okChecks + " ok, " + (totalChecks - okChecks) + " fail");
  console.log("overflow fails: " + overflowFails + ", structural fails: " + structFails);
  console.log("touch target fails (390x844): " + touchUnique.length);
  touchUnique.slice(0, 20).forEach((f) => console.log("  touch: " + f.sel + " h=" + f.h + " < " + f.min + " (" + f.text + ")"));
  console.log("js errors during patrol: " + jsErrors);
  console.log("shots: " + path.join(OUT, "llamamonitor-desktop.png") + ", " + path.join(OUT, "llamamonitor-mobile.png"));
  fs.writeFileSync(path.join(OUT, "patrol_report.json"), JSON.stringify({
    checks: report, touchFails: touchUnique, jsErrors: jsErrors,
  }, null, 2));
  cdp.close();
  chrome.kill();
  process.exit(overflowFails === 0 && structFails === 0 ? 0 : 1);
})().catch((e) => { console.error("PATROL FAIL:", e); process.exit(3); });
