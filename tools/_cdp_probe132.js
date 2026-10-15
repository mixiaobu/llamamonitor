"use strict";
const CDP = "http://127.0.0.1:9333";
(async () => {
  const r = await fetch(CDP + "/json/new?" + encodeURIComponent("http://127.0.0.1:8790"), { method: "PUT" });
  const target = await r.json();
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res) => { ws.onopen = res; });
  let id = 1;
  const pending = {};
  ws.addEventListener("message", (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending[m.id]) { pending[m.id](m.result); delete pending[m.id]; }
  });
  const send = (method, params) => new Promise((res) => {
    pending[id] = res;
    ws.send(JSON.stringify({ id: id++, method, params: params || {} }));
  });
  await send("Runtime.enable");
  await new Promise((r) => setTimeout(r, 10000));
  const out = await send("Runtime.evaluate", {
    expression: '(() => { const q = (s) => document.querySelectorAll(s).length; const all = [...document.querySelectorAll("[id^=\'stHwPcie\']")].map((e) => e.id); const tx = document.getElementById("stHwPcieTx"); const link = document.getElementById("stHwPcieLink"); return { rxCount: q("[id=\'stHwPcieRx\']"), txCount: q("[id=\'stHwPcieTx\']"), linkCount: q("[id=\'stHwPcieLink\']"), allIds: all, txTag: tx ? tx.tagName : null, txText: tx ? tx.textContent : null, linkText: link ? link.textContent : null, parentOfRx: (document.getElementById("stHwPcieRx") || {}).parentElement && document.getElementById("stHwPcieRx").parentElement.parentElement && document.getElementById("stHwPcieRx").parentElement.parentElement.className }; })()',
    returnByValue: true,
  });
  console.log(JSON.stringify(out.result.value, null, 1));
  ws.close();
  await fetch(CDP + "/json/close/" + target.id);
  process.exit(0);
})().catch((e) => { console.error("FAIL", e.message); process.exit(2); });
