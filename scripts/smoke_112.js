// smoke_112.js — 快速结构自检（CDP），先于完整矩阵跑，尽早暴露问题。
const { spawn } = require("node:child_process");
const http = require("node:http");
const path = require("node:path");
const fs = require("node:fs");
const BASE = process.env.LM_BASE || "http://127.0.0.1:8790";
const OUT = process.env.LM_SHOT_OUT || path.resolve(require("node:os").tmpdir(), "lm112_smoke");
fs.mkdirSync(OUT, { recursive: true });
const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";

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
    "--no-default-browser-check", "--user-data-dir=" + path.join(OUT, "p"), "about:blank"], { stdio: "ignore" });
  let url = null;
  for (let i = 0; i < 40 && !url; i++) { await sleep(500); try { url = await getDebuggerUrl(); } catch {} }
  if (!url) { console.error("no cdp"); process.exit(2); }
  const cdp = await CDP.connect(url);
  const errs = [];
  // 合并 handler：保留 pending 解析 + 收集异常/console.error
  cdp.ws.onmessage = (ev) => {
    const m = JSON.parse(typeof ev.data === "string" ? ev.data : ev.data.toString());
    if (m.id && cdp.pending.has(m.id)) { cdp.pending.get(m.id)(m); cdp.pending.delete(m.id); }
    if (m.method === "Runtime.exceptionThrown")
      errs.push("EXC: " + ((m.params.exceptionDetails.exception && m.params.exceptionDetails.exception.description) || m.params.exceptionDetails.text));
    if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error")
      errs.push("ERR: " + m.params.args.map((a) => a.value || a.description).join(" "));
  };
  const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
  const send = (m, p = {}) => cdp.send(m, p, sessionId);
  await send("Runtime.enable"); await send("Page.enable");
  const ev = (e) => send("Runtime.evaluate", { expression: e, returnByValue: true }).then((r) => r.result.value);

  // ---- mobile 390x844 ----
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await send("Page.navigate", { url: BASE + "/" });
  await sleep(7000);
  console.log("MOBILE overview:", await ev(`(function(){
    var d = {};
    d.mnavDisplay = getComputedStyle(document.querySelector('.mobile-nav')).display;
    d.navviewDisplay = getComputedStyle(document.querySelector('.navview')).display;
    d.bottomLabels = Array.from(document.querySelectorAll('.mnav-item .mnav-label')).map(function(x){return x.textContent});
    d.overflow = document.documentElement.scrollWidth + ' vs ' + document.documentElement.clientWidth;
    return JSON.stringify(d); })()`));

  // more sheet
  await ev(`document.getElementById('mnavMore').click(); true`);
  await sleep(400);
  console.log("MORE SHEET:", await ev(`(function(){
    var s = document.getElementById('moreSheet'); var r = s.getBoundingClientRect();
    return JSON.stringify({ display: getComputedStyle(s).display, h: Math.round(r.height), top: Math.round(r.top),
      items: Array.from(document.querySelectorAll('.sheet-item')).map(function(x){return x.textContent.trim()}) }); })()`));
  await ev(`document.getElementById('mnavMore').click(); true`);
  await sleep(400);

  // usage 页 card rows
  await ev(`(function(){ var b = document.querySelector('.mnav-item[data-page="usage"]'); if (b) b.click(); return 'go'; })()`);
  await sleep(4500);
  console.log("MOBILE usage cardrows:", await ev(`(function(){
    var d = {};
    var t = document.getElementById('dailyTbody');
    d.rows = t ? t.querySelectorAll('tr').length : 0;
    var tr = t ? t.querySelector('tr') : null;
    if (tr) {
      d.trDisplay = getComputedStyle(tr).display;
      var tds = tr.querySelectorAll('td');
      if (tds.length > 1) {
        d.firstTdFontWeight = getComputedStyle(tds[0]).fontWeight;
        d.secondLabelBefore = getComputedStyle(tds[1], '::before').content;
        d.secondDisplay = getComputedStyle(tds[1]).display;
      }
    }
    d.overflow = document.documentElement.scrollWidth + ' vs ' + document.documentElement.clientWidth;
    var mn = document.querySelector('.mobile-nav'); d.mnavH = Math.round(mn.getBoundingClientRect().height);
    return JSON.stringify(d); })()`));

  // settings mobile
  await ev(`(function(){ var s = document.querySelector('.sheet-item[data-page="settings"]'); if (s) s.click(); return 'go'; })()`);
  await sleep(3500);
  console.log("MOBILE settings:", await ev(`(function(){
    var d = {};
    var rail = document.querySelector('.settings-rail'); var r = rail.getBoundingClientRect();
    d.railH = Math.round(r.height); d.railScrollW = rail.scrollWidth;
    var f = document.querySelector('.settings-footer'); var fr = f.getBoundingClientRect();
    d.footerPos = getComputedStyle(f).position;
    d.footerBottom = Math.round(fr.bottom);
    d.winH = window.innerHeight;
    d.overflow = document.documentElement.scrollWidth + ' vs ' + document.documentElement.clientWidth;
    d.sheetClosed = getComputedStyle(document.getElementById('moreSheet')).display === 'none';
    return JSON.stringify(d); })()`));

  // history events card rows
  await ev(`(function(){ var s = document.querySelector('.sheet-item[data-page="history"]'); if (s) s.click(); return 'go'; })()`);
  await sleep(3000);
  console.log("MOBILE history:", await ev(`(function(){
    var d = {};
    var t = document.getElementById('eventsTbody');
    d.rows = t ? t.querySelectorAll('tr').length : 0;
    var tr = t ? t.querySelector('tr') : null;
    if (tr) d.trDisplay = getComputedStyle(tr).display;
    d.overflow = document.documentElement.scrollWidth + ' vs ' + document.documentElement.clientWidth;
    return JSON.stringify(d); })()`));

  // system page (long-fold default collapsed on mobile)
  await ev(`(function(){ var s = document.querySelector('.sheet-item[data-page="system"]'); if (s) s.click(); return 'go'; })()`);
  await sleep(3000);
  console.log("MOBILE system:", await ev(`(function(){
    var d = {};
    var folds = document.querySelectorAll('details.long-fold[data-auto-fold]');
    d.folds = Array.from(folds).map(function (f) { return f.id + ':' + (f.open ? 'open' : 'closed'); });
    d.overflow = document.documentElement.scrollWidth + ' vs ' + document.documentElement.clientWidth;
    return JSON.stringify(d); })()`));

  // gpu page (advanced collapsed on mobile)
  await ev(`(function(){ var b = document.querySelector('.mnav-item[data-page="gpu"]'); if (b) b.click(); return 'go'; })()`);
  await sleep(3500);
  console.log("MOBILE gpu:", await ev(`(function(){
    var d = {};
    var adv = document.querySelector('.gpu-adv');
    d.advExists = !!adv; if (adv) d.advOpen = adv.open;
    d.summaryDisplay = adv ? getComputedStyle(adv.querySelector('summary')).display : 'none';
    d.chipOverflow = (function(){ var c = document.getElementById('gpuPick'); if(!c) return 'none'; var r=c.getBoundingClientRect(); return c.scrollWidth+' vs '+Math.round(r.width)+' wrap='+(c.scrollWidth>Math.round(r.width)+2); })();
    d.overflow = document.documentElement.scrollWidth + ' vs ' + document.documentElement.clientWidth;
    return JSON.stringify(d); })()`));

  // ---- desktop 1920x1080 ----
  await send("Emulation.setDeviceMetricsOverride", { width: 1920, height: 1080, deviceScaleFactor: 1, mobile: false });
  await sleep(2500);
  console.log("DESKTOP:", await ev(`(function(){
    var d = {};
    d.mnavDisplay = getComputedStyle(document.querySelector('.mobile-nav')).display;
    d.navW = Math.round(document.querySelector('.navview').getBoundingClientRect().width);
    var adv = document.querySelector('.gpu-adv');
    d.advOpen = adv ? adv.open : null;
    d.summaryDisplay = adv ? getComputedStyle(adv.querySelector('summary')).display : 'n/a';
    d.overflow = document.documentElement.scrollWidth + ' vs ' + document.documentElement.clientWidth;
    var folds = document.querySelectorAll('details.long-fold[data-auto-fold]');
    d.foldsOpen = Array.from(folds).every(function (f) { return f.open; });
    return JSON.stringify(d); })()`));

  console.log("JS ERRORS: " + errs.length);
  errs.slice(0, 15).forEach((e) => console.log("  " + e));
  cdp.close(); chrome.kill(); process.exit(0);
})().catch((e) => { console.error("SMOKE FAIL", e); process.exit(1); });
