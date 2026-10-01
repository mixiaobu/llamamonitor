// 1.1.4 Round 2 — Microsoft Edge 验收：Overview 截图 + DOM 测量 + Console 采集
// 用法: node scripts/edge_ovr_r2.js [baseUrl]
// 输出: artifacts/edge-ovr-r2/  截图 + measures.json + console.json
// 说明: 用 Edge（Chromium WebView2 同源运行时），headless=new + CDP。
const { spawn } = require("node:child_process");
const http = require("node:http");
const path = require("node:path");
const fs = require("node:fs");
const ROOT = path.join(__dirname, "..");
const OUT = path.join(ROOT, "artifacts", "edge-ovr-r2");
fs.mkdirSync(OUT, { recursive: true });
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const BASE = process.argv[2] || "http://127.0.0.1:8790";
const CDP_PORT = 9334;
function getDebuggerUrl() {
  return new Promise((res, rej) => {
    http.get("http://127.0.0.1:" + CDP_PORT + "/json/version", (r) => {
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

// 测量脚本（在页面里跑）：布局关键量 + 溢出 + 关键文案
const MEASURE_JS = `
(function(){
  function cs(sel){ var el = document.querySelector(sel); if(!el) return null;
    var r = el.getBoundingClientRect(); var s = getComputedStyle(el);
    return { w: Math.round(r.width), h: Math.round(r.height), x: Math.round(r.x), y: Math.round(r.y),
             font: s.fontSize, cols: s.gridTemplateColumns.split(' ').length,
             containerType: s.containerType }; }
  function all(sel){ return Array.prototype.map.call(document.querySelectorAll(sel), function(el){
    var r = el.getBoundingClientRect(); return { w: Math.round(r.width), h: Math.round(r.height), x: Math.round(r.x), y: Math.round(r.y) }; }); }
  function txt(sel){ var el=document.querySelector(sel); return el ? el.textContent.trim() : null; }
  function ell(sel){ var el=document.querySelector(sel); if(!el) return null;
    return el.scrollWidth > el.clientWidth + 1; }
  var doc = document.documentElement;
  var out = {
    viewport: { w: window.innerWidth, h: window.innerHeight },
    overflowX: doc.scrollWidth - doc.clientWidth,
    docScrollHeight: doc.scrollHeight,
    compact: document.getElementById('appShell') ? document.getElementById('appShell').classList.contains('compact') : null,
    todayHero: cs('.today-hero'),
    todayBreakdown: cs('.today-breakdown'),
    ovPerfGrid: cs('.ov-perf-grid'),
    hostGrid: cs('.stat-grid.host-grid'),
    gpuMiniGrid: cs('.gpu-mini-grid'),
    gpuMiniCards: all('.gpu-mini'),
    dqGrid: cs('.dq-summary .stat-grid'),
    statusStrip: cs('.status-strip'),
    remoteBanner: cs('.remote-banner'),
    sidebar: cs('.navview'),
    mobileNav: cs('.mobile-nav'),
    heroNums: all('.today-hero .hero-num').map(function(r){return {y:r.y};}),
    tbLabels: all('.today-breakdown .tb-label').map(function(r){return {y:r.y};}),
    perfCards: all('.ov-perf-grid .perf-card').map(function(r){return {y:r.y,w:r.w};}),
    hostVals: all('.host-grid .stat-value').map(function(r){return {y:r.y,w:r.w};}),
    hostEls: {
      cpu: txt('#ovHostCpu'), cpuSub: txt('#ovHostCpuSub'),
      mem: txt('#ovHostMem'), memSub: txt('#ovHostMemSub'),
      diskR: txt('#ovHostDiskR'), diskW: txt('#ovHostDiskW'),
      netR: txt('#ovHostNetR'), netW: txt('#ovHostNetW'),
      power: txt('#ovHostPower'), powerSub: txt('#ovHostPowerSub')
    },
    dq: { cov: txt('#dqCoverage'), gaps: txt('#dqGapsToday'), loss: txt('#dqLossToday'),
           db: txt('#dqDb'), dbHint: txt('#dqDbHint') },
    server: { url: txt('#ovServerUrl'), model: txt('#ovModelLine'), state: txt('#ovServerState .status-text'),
              lastUpdate: txt('#ovLastUpdate') },
    perf: { prompt: txt('#ovPromptTps'), decode: txt('#ovDecodeTps'), mtp: txt('#ovMtpRate'),
            ctx: txt('#ovContext'), req: txt('#ovRequests'), kv: txt('#ovKvCache') },
    gpuLine: txt('#ovGpuLine'),
    navLabels: Array.prototype.map.call(document.querySelectorAll('.navview .nav-label'), function(e){return e.textContent.trim();}),
    mnavLabels: Array.prototype.map.call(document.querySelectorAll('.mnav-item .mnav-label'), function(e){return e.textContent.trim();}),
    actionLinks: Array.prototype.map.call(document.querySelectorAll('#page-overview .section-header a.link'), function(a){
      var r = a.getBoundingClientRect(); var s = getComputedStyle(a);
      return { t: a.textContent.trim(), h: Math.round(r.height), font: s.fontSize, right: Math.round(r.right) }; }),
    ellipsisCheck: {
      serverUrl: ell('#ovServerUrl'), model: ell('#ovModelLine'),
      hero1: ell('#ovTodayLogical'), hero2: ell('#ovTodayCompute'),
      tb1: ell('#ovTodayPrompt'), tb2: ell('#ovTodayCached'), tb3: ell('#ovTodayOutput')
    },
    hero1Rect: (function(){ var el = document.getElementById('ovTodayLogical'); if(!el) return null; var r=el.getBoundingClientRect(); return {y: Math.round(r.y)}; })(),
    hero2Rect: (function(){ var el = document.getElementById('ovTodayCompute'); if(!el) return null; var r=el.getBoundingClientRect(); return {y: Math.round(r.y)}; })()
  };
  return out;
})()`;

(async () => {
  const edge = spawn(EDGE, ["--headless=new", "--remote-debugging-port=" + CDP_PORT, "--no-first-run",
    "--no-default-browser-check", "--disable-gpu", "--lang=zh-CN",
    "--user-data-dir=" + path.join(OUT, "_profile"), "about:blank"], { stdio: "ignore" });
  let url = null;
  for (let i = 0; i < 40 && !url; i++) { await sleep(500); try { url = await getDebuggerUrl(); } catch {} }
  if (!url) { console.error("no cdp on " + CDP_PORT); edge.kill(); process.exit(2); }
  console.log("edge cdp: " + url);
  const cdp = await CDP.connect(url);
  const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
  const send = (m, p = {}) => cdp.send(m, p, sessionId);
  await send("Runtime.enable"); await send("Page.enable"); await send("Log.enable");
  await send("Emulation.setLocaleOverride", { locale: "zh-CN" });

  const consoleLog = [];
  const origOnMessage = cdp.ws.onmessage;
  cdp.ws.onmessage = (ev2) => {
    try {
      const m = JSON.parse(typeof ev2.data === "string" ? ev2.data : ev2.data.toString());
      if (m.method === "Runtime.consoleAPICalled" && (m.params.type === "error" || m.params.type === "warning")) {
        consoleLog.push({ t: m.params.type, x: (m.params.args || []).map((a) => a.value || a.description || "").join(" ").slice(0, 220) });
      }
      if (m.method === "Runtime.exceptionThrown") {
        const d = m.params.exceptionDetails;
        consoleLog.push({ t: "exception", x: (d.exception && d.exception.description || d.text || "").slice(0, 220) });
      }
      if (m.method === "Log.entryAdded" && m.params.entry.level === "error") {
        consoleLog.push({ t: "log", x: (m.params.entry.text || "").slice(0, 220) });
      }
    } catch {}
    if (origOnMessage) origOnMessage(ev2);
  };
  const ev = (e) => send("Runtime.evaluate", { expression: e, returnByValue: true }).then((r) => r.result.value);

  // 强制深色 + 展开侧栏（<1100 默认自动 compact；lm_nav_manual=0 关闭自动）
  const PRE = `localStorage.setItem('lm-theme','dark'); localStorage.setItem('lm_nav_manual','0'); try{document.getElementById('appShell')&&document.getElementById('appShell').classList.remove('compact')}catch(e){} 'ok'`;
  const shot = async (name, full) => {
    let restore = null;
    if (full) {
      // 真正整页：把视口高度临时设为整页高度。App 用 .content 作内部滚动区，
      // 整页高度 = documentElement.scrollHeight 与 .content.scrollHeight 的较大值。
      const docH = await ev(
        "Math.max(document.documentElement.scrollHeight, (document.querySelector('.content')||{}).scrollHeight||0, (document.querySelector('.content-inner')||{}).scrollHeight||0)");
      restore = { w: await ev("window.innerWidth"), h: await ev("window.innerHeight") };
      await send("Emulation.setDeviceMetricsOverride", { width: restore.w, height: Math.min(docH + 80, 12000), deviceScaleFactor: 1, mobile: false });
      await sleep(700);
    }
    const { data } = await send("Page.captureScreenshot", { format: "png" });
    fs.writeFileSync(path.join(OUT, name + ".png"), Buffer.from(data, "base64"));
    if (restore) { await send("Emulation.setDeviceMetricsOverride", { width: restore.w, height: restore.h, deviceScaleFactor: 1, mobile: false }); await sleep(300); }
    console.log("shot: " + name);
  };
  const setView = async (w, h, mobile) => {
    await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: mobile ? 2 : 1, mobile });
    if (mobile) { await send("Emulation.setTouchEmulationEnabled", { enabled: true }); }
    else { await send("Emulation.setTouchEmulationEnabled", { enabled: false }); }
    await send("Page.navigate", { url: BASE + "/" });
    await sleep(7000); // 轮询 2s ×3 + 渲染稳定
    await ev(PRE + " 'ok'");
    await sleep(2500);
  };

  const results = {};
  const VIEWS = [
    ["1920", 1920, 1080, false, true],
    ["2560", 2560, 1440, false, true],
    ["1600", 1600, 900, false, true],
    ["1366", 1366, 768, false, true],
    ["1065", 1065, 1394, false, true],
    ["900", 900, 900, false, true],
    ["761", 761, 900, false, true],
    ["760", 760, 900, true, true],
    ["430", 430, 932, true, true],
    ["390", 390, 844, true, true],
    ["360", 360, 800, true, true],
    ["320", 320, 568, true, true],
  ];
  for (const [tag, w, h, mobile, doShots] of VIEWS) {
    console.log("--- " + tag + "x? " + w + "x" + h + (mobile ? " [mobile]" : ""));
    await setView(w, h, mobile);
    results[tag] = await ev(MEASURE_JS);
    if (results[tag].overflowX > 0) console.log("  !! overflowX = " + results[tag].overflowX);
    if (doShots) {
      await shot("ovr-" + tag + "-top", false);
      if (["1920", "1065", "390", "320", "2560", "1366", "1600", "900", "430", "360"].includes(tag)) {
        await shot("ovr-" + tag + "-full", true);
      }
    }
  }

  fs.writeFileSync(path.join(OUT, "measures.json"), JSON.stringify(results, null, 2));
  fs.writeFileSync(path.join(OUT, "console.json"), JSON.stringify(consoleLog, null, 2));
  const errs = consoleLog.filter((c) => c.t === "error" || c.t === "exception" || c.t === "log");
  console.log("console errors/warnings: " + errs.length + " (warnings included: " + consoleLog.length + ")");
  errs.slice(0, 12).forEach((e) => console.log("  [" + e.t + "] " + e.x));
  cdp.close(); edge.kill();
  console.log("DONE");
})().catch((e) => { console.error("FATAL", e); process.exit(1); });
