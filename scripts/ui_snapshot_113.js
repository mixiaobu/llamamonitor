// 1.1.3 视觉审计快照：固定 viewport/page/theme 矩阵，输出 artifacts/ui-113/
// 用途：人工/模型视觉 Review（非脆弱像素相等测试）。
// 用法: node scripts/ui_snapshot_113.js [baseUrl]
// 页面: overview/usage/performance/gpu/system/history/settings/about
// 输出命名: desktop-1920-dark-<page>.png / mobile-390-dark-<page>.png / *-light-*.png
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
      setTimeout(() => { if (this.pending.has(id)) { this.pending.delete(id); rej(new Error(m + " timeout")); } }, 30000);
    });
  }
  close() { try { this.ws.close(); } catch {} }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const chrome = spawn(CHROME, ["--headless=new", "--remote-debugging-port=9333", "--no-first-run",
    "--no-default-browser-check", "--user-data-dir=" + path.join(OUT, "_profile"), "about:blank"], { stdio: "ignore" });
  let url = null;
  for (let i = 0; i < 40 && !url; i++) { await sleep(500); try { url = await getDebuggerUrl(); } catch {} }
  if (!url) { console.error("no cdp"); process.exit(2); }
  const cdp = await CDP.connect(url);
  const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
  const send = (m, p = {}) => cdp.send(m, p, sessionId);
  await send("Runtime.enable"); await send("Page.enable"); await send("Log.enable");
  const ev = (e) => send("Runtime.evaluate", { expression: e, returnByValue: true }).then((r) => r.result.value);

  // console 错误采集（链式包装：保留 CDP 内部的 id 响应处理）
  const consoleErrors = [];
  const origOnMessage = cdp.ws.onmessage;
  cdp.ws.onmessage = (ev2) => {
    try {
      const m = JSON.parse(typeof ev2.data === "string" ? ev2.data : ev2.data.toString());
      if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") {
        const t = (m.params.args || []).map((a) => a.value || a.description || "").join(" ").slice(0, 200);
        consoleErrors.push(t);
      }
      if (m.method === "Runtime.exceptionThrown") {
        const d = m.params.exceptionDetails;
        consoleErrors.push("UNCAUGHT: " + (d.exception && d.exception.description || d.text || "").slice(0, 200));
      }
    } catch {}
    if (origOnMessage) origOnMessage(ev2);
  };

  const shot = async (name) => {
    const { data } = await send("Page.captureScreenshot", { format: "png" });
    fs.writeFileSync(path.join(OUT, name + ".png"), Buffer.from(data, "base64"));
    console.log("shot: " + name);
  };
  // 导航：直接调 LM.nav.showPage(page)（真实 API，不受导航显隐影响），
  // 并读回 currentPage() 校验切换确实发生（防"以为切了其实没切"→ 截图重复）。
  const nav = async (page, ms) => {
    await ev(`(function(){ if (window.LM && LM.nav) LM.nav.showPage(${JSON.stringify(page)}); return 1; })()`);
    await sleep(400); // 等 active 切换 + 首屏数据触发
    const cur = await ev(`(function(){ return (window.LM && LM.nav) ? LM.nav.currentPage() : null; })()`);
    if (cur !== page) console.log("  !! nav MISMATCH: want " + page + " got " + cur);
    await sleep(ms);
  };
  const setTheme = async (t) => {
    await ev(`(function(){ document.documentElement.setAttribute('data-theme', ${JSON.stringify(t)}); try { localStorage.setItem('lm-theme', ${JSON.stringify(t)}); } catch(e){} return 1; })()`);
    await sleep(900);
  };
  const setViewport = async (w, h, mobile) => {
    await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: mobile ? 2 : 1, mobile });
    await send("Page.navigate", { url: BASE + "/" });
    await sleep(6000);
  };

  const PAGES = [
    ["overview", 4000], ["usage", 4000], ["performance", 4500], ["gpu", 5000],
    ["system", 5000], ["history", 4500], ["settings", 4000], ["about", 3000],
  ];

  // ---------- Desktop 1920 dark + light（全部 8 页 dark，4 关键页 light） ----------
  await setViewport(1920, 1080, false);
  await setTheme("dark");
  for (const [p, ms] of PAGES) { await nav(p, ms); await shot("desktop-1920-dark-" + p); }
  await setTheme("light");
  for (const p of ["overview", "gpu", "system", "settings"]) { await nav(p, 4000); await shot("desktop-1920-light-" + p); }

  // ---------- Mobile 390 dark + light ----------
  await setViewport(390, 844, true);
  await setTheme("dark");
  for (const [p, ms] of PAGES) { await nav(p, ms); await shot("mobile-390-dark-" + p); }
  await setTheme("light");
  for (const p of ["overview", "gpu", "system", "settings"]) { await nav(p, 4000); await shot("mobile-390-light-" + p); }

  // ---------- 320 极窄 gate ----------
  await setViewport(320, 568, true);
  await setTheme("dark");
  for (const p of ["overview", "usage", "settings"]) { await nav(p, 4000); await shot("mobile-320-dark-" + p); }

  cdp.close(); chrome.kill();
  fs.writeFileSync(path.join(OUT, "console-errors.json"), JSON.stringify(consoleErrors, null, 2));
  console.log("console errors: " + consoleErrors.length);
  console.log("done -> " + OUT);
  process.exit(0);
})().catch((e) => { console.error(e.message); process.exit(1); });
