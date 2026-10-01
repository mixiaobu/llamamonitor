// Probe: why is .ov-perf-grid single column at 1920?
const { spawn } = require("node:child_process");
const http = require("node:http");
const path = require("node:path");
const OUT = path.join(__dirname, "..", "artifacts", "edge-ovr-r2");
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const BASE = "http://127.0.0.1:8790";
const PORT = 9335;
function dbg() { return new Promise((res, rej) => { http.get("http://127.0.0.1:" + PORT + "/json/version", (r) => { let b = ""; r.on("data", (c) => (b += c)); r.on("end", () => res(JSON.parse(b).webSocketDebuggerUrl)); }).on("error", rej); }); }
class CDP {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); }
  static async connect(url) { const ws = new WebSocket(url); await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; }); const c = new CDP(ws); ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && c.pending.has(m.id)) { c.pending.get(m.id)(m); c.pending.delete(m.id); } }; return c; }
  send(m, p = {}, s) { return new Promise((res, rej) => { const id = ++this.id; this.pending.set(id, (msg) => (msg.error ? rej(new Error(m + ": " + JSON.stringify(msg.error))) : res(msg.result))); this.ws.send(JSON.stringify({ id, method: m, params: p, ...(s ? { sessionId: s } : {}) })); setTimeout(() => { if (this.pending.has(id)) { this.pending.delete(id); rej(new Error(m + " timeout")); } }, 20000); }); }
  close() { try { this.ws.close(); } catch {} }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const e = spawn(EDGE, ["--headless=new", "--remote-debugging-port=" + PORT, "--no-first-run", "--no-default-browser-check", "--disable-gpu", "--lang=zh-CN", "--user-data-dir=" + path.join(OUT, "_p2"), "about:blank"], { stdio: "ignore" });
  let url = null; for (let i = 0; i < 40 && !url; i++) { await sleep(500); try { url = await dbg(); } catch {} }
  const cdp = await CDP.connect(url);
  const { targetId } = await cdp.send("Target.createTarget", { url: BASE + "/" });
  const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
  const send = (m, p = {}) => cdp.send(m, p, sessionId);
  await send("Runtime.enable"); await send("Page.enable");
  await send("Emulation.setDeviceMetricsOverride", { width: 1920, height: 1080, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url: BASE + "/" });
  await sleep(6000);
  const ev = (x) => send("Runtime.evaluate", { expression: x, returnByValue: true }).then((r) => r.result.value);
  const info = await ev(`(function(){
    var el = document.querySelector('.ov-perf-grid');
    var cs = getComputedStyle(el);
    var r = el.getBoundingClientRect();
    var kids = Array.prototype.map.call(el.children, function(k){ var kr=k.getBoundingClientRect(); return { cls:k.className, w:Math.round(kr.width) }; });
    // collect every matched rule that sets grid-template-columns on this element
    var rules = [];
    for (var s of document.styleSheets) {
      try {
        for (var r2 of s.cssRules) {
          var rs = r2;
          if (r2.cssRules) { for (var r3 of r2.cssRules) { if (r3.selectorText && el.matches(r3.selectorText) && r3.style && r3.style.getPropertyValue('grid-template-columns')) { rules.push({ m: r2.conditionText||'', sel: r3.selectorText, val: r3.style.getPropertyValue('grid-template-columns'), href: (s.href||'').split('/').pop() }); } } }
          else if (r2.selectorText && el.matches(r2.selectorText) && r2.style && r2.style.getPropertyValue('grid-template-columns')) { rules.push({ m:'', sel: r2.selectorText, val: r2.style.getPropertyValue('grid-template-columns'), href: (s.href||'').split('/').pop() }); }
        }
      } catch (err) {}
    }
    return { computed: cs.gridTemplateColumns, width: Math.round(r.width), classList: el.className, kids: kids, rules: rules };
  })()`);
  console.log(JSON.stringify(info, null, 2));
  cdp.close(); e.kill(); process.exit(0);
})().catch((x) => { console.error("FATAL", x.message); process.exit(1); });
