// 1.1.3 压力审计：500 次页切换 / 1000 次 More Sheet / 1000 次 tooltip / 20 次旋转 /
// 100 次主题切换 / 100 次导航折叠 + 未捕获异常/控制台错误计数。
// 判定：errors == 0 且无异常类错误（ResizeObserver loop 为已知良性，单独计数）。
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
      setTimeout(() => { if (this.pending.has(id)) { this.pending.delete(id); rej(new Error(m + " timeout")); } }, 60000);
    });
  }
  close() { try { this.ws.close(); } catch {} }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const N_PAGES = 500, N_SHEET = 1000, N_TIP = 1000, N_ROT = 20, N_THEME = 100, N_NAV = 100;
(async () => {
  const chrome = spawn(CHROME, ["--headless=new", "--remote-debugging-port=9333", "--no-first-run",
    "--no-default-browser-check", "--user-data-dir=" + path.join(OUT, "_stress"), "about:blank"], { stdio: "ignore" });
  let url = null;
  for (let i = 0; i < 40 && !url; i++) { await sleep(500); try { url = await getDebuggerUrl(); } catch {} }
  if (!url) { console.error("no cdp"); process.exit(2); }
  const cdp = await CDP.connect(url);
  const exceptions = [], consoleErrors = [], resizeObs = [];
  const orig = cdp.ws.onmessage;
  cdp.ws.onmessage = (ev2) => {
    try {
      const m = JSON.parse(typeof ev2.data === "string" ? ev2.data : ev2.data.toString());
      if (m.method === "Runtime.exceptionThrown") {
        const d = m.params.exceptionDetails;
        const t = (d.exception && d.exception.description) || d.text || "";
        if (/ResizeObserver loop/.test(t)) resizeObs.push(t.slice(0, 80));
        else exceptions.push(t.slice(0, 160));
      }
      if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") {
        const t = (m.params.args || []).map((a) => a.value || a.description || "").join(" ");
        if (/Failed to load resource/.test(t)) return;
        if (/ResizeObserver loop/.test(t)) { resizeObs.push(t.slice(0, 80)); return; }
        consoleErrors.push(t.slice(0, 160));
      }
    } catch {}
    if (orig) orig(ev2);
  };
  const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
  const send = (m, p = {}) => cdp.send(m, p, sessionId);
  await send("Runtime.enable"); await send("Page.enable");
  const ev = (e) => send("Runtime.evaluate", { expression: e, returnByValue: true }).then((r) => r.result.value);
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
  await send("Page.navigate", { url: BASE + "/" });
  await sleep(7000);
  const pages = ["overview", "usage", "performance", "gpu", "system", "history", "settings", "about"];
  const t0 = Date.now();

  // 1) 500 次页切换（移动端底部导航 + ••• sheet 路径混合）
  for (let i = 0; i < N_PAGES; i++) {
    await ev(`LM.nav.showPage(${JSON.stringify(pages[i % pages.length])})`);
    if (i % 60 === 0) await sleep(120); // 让渲染排空
  }
  const afterNav = await ev(`LM.nav.currentPage()`);
  console.log(`page-switch ${N_PAGES} done, currentPage=${afterNav}`);

  // 2) 1000 次 More Sheet 开/关（含 focus trap + scroll lock 反复）
  for (let i = 0; i < N_SHEET; i++) {
    await ev(`(function(){ var b=document.querySelector('.page.active .page-overflow')||document.querySelector('.page-overflow'); if(b) b.click(); return 1; })()`);
    if (i % 2 === 0) {
      await ev(`(function(){ var s=document.getElementById('moreSheet'); if(s && !s.hidden){ var e=document.getElementById('moreSheetClose')||document.querySelector('.more-sheet [data-close]'); if(e){ e.click(); return 2; } var it=document.querySelector('.sheet-item[data-page]'); if(it){ it.click(); return 3; } } return 1; })()`);
    } else {
      await ev(`(function(){ document.getElementById('moreSheet').click(); return 1; })()`);
    }
    if (i % 100 === 0) await sleep(60);
  }
  console.log(`sheet ${N_SHEET} done`);

  // 3) 1000 次 tooltip（tap 开/关，走 .tip-open 路径）
  await ev(`LM.nav.showPage('overview')`);
  await sleep(300);
  for (let i = 0; i < N_TIP; i++) {
    await ev(`(function(){ var tips=document.querySelectorAll('.page.active .tip-btn'); if(!tips.length) return 0; var b=tips[i % tips.length]; b.click(); return 1; })()`);
    if (i % 200 === 0) await sleep(50);
  }
  // 确保全部关闭
  await ev(`(function(){ document.body.click(); var o=document.querySelectorAll('.info-tip.tip-open'); o.forEach(function(w){ w.classList.remove('tip-open'); }); return o.length; })()`);
  console.log(`tooltip ${N_TIP} done`);

  // 4) 20 次旋转（390x844 <-> 844x390）
  for (let i = 0; i < N_ROT; i++) {
    const portrait = i % 2 === 0;
    await send("Emulation.setDeviceMetricsOverride", portrait
      ? { width: 390, height: 844, deviceScaleFactor: 2, mobile: true }
      : { width: 844, height: 390, deviceScaleFactor: 2, mobile: true });
    await sleep(80);
  }
  console.log(`rotation ${N_ROT} done`);

  // 5) 100 次主题切换（dark/light 交替，走 LM.app.applyTheme 真实路径）
  for (let i = 0; i < N_THEME; i++) {
    await ev(`(function(){ if (LM.app && LM.app.applyTheme) LM.app.applyTheme(${i % 2 ? '"light"' : '"dark"'}); return 1; })()`);
    if (i % 25 === 0) await sleep(150);
  }
  console.log(`theme ${N_THEME} done`);

  // 6) 100 次桌面导航（反复点击侧边导航按钮，含 aria-current 切换 + 渲染）
  await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
  await sleep(800);
  for (let i = 0; i < N_NAV; i++) {
    await ev(`(function(){ var b=document.querySelector('.navview .nav-item[data-page="' + pages[i % pages.length] + '"]'); if (b) b.click(); return 1; })()`);
    if (i % 30 === 0) await sleep(60);
  }
  console.log(`nav-collapse ${N_NAV} done`);

  await sleep(1500);
  const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
  const result = {
    elapsed_s: elapsed,
    uncaught: exceptions.length,
    uncaught_samples: exceptions.slice(0, 5),
    console_errors: consoleErrors.length,
    console_samples: consoleErrors.slice(0, 5),
    resize_observer_benign: resizeObs.length,
    PASS: exceptions.length === 0 && consoleErrors.length === 0,
  };
  console.log("RESULT " + JSON.stringify(result));
  fs.writeFileSync(path.join(OUT, "stress-result.json"), JSON.stringify(result, null, 2));
  cdp.close(); chrome.kill();
  process.exit(result.PASS ? 0 : 1);
})().catch((e) => { console.error("STRESS-ERR " + e.message); process.exit(1); });
