"use strict";
const CDP = "http://127.0.0.1:9333";
const BASE = "http://127.0.0.1:8790";
(async () => {
  const r = await fetch(CDP + "/json/new?" + encodeURIComponent(BASE), { method: "PUT" });
  const t = await r.json();
  const ws = new WebSocket(t.webSocketDebuggerUrl);
  await new Promise((res) => { ws.onopen = res; });
  let id = 1;
  const pend = {};
  ws.addEventListener("message", (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pend[m.id]) { pend[m.id](m.result); delete pend[m.id]; }
  });
  const send = (method, params) => new Promise((res) => {
    pend[id] = res;
    ws.send(JSON.stringify({ id: id++, method, params: params || {} }));
  });
  await send("Runtime.enable");
  await new Promise((x) => setTimeout(x, 9000));
  await send("Runtime.evaluate", {
    expression: '(() => { const b = [...document.querySelectorAll("[data-page]")].find((x) => x.dataset && x.dataset.page === "performance"); if (b) b.click(); return 1; })()',
    returnByValue: true,
  });
  await new Promise((x) => setTimeout(x, 5000));
  const o = await send("Runtime.evaluate", {
    expression: '(() => { const t = (i) => { const el = document.getElementById(i); return el ? el.textContent.trim() : "?"; }; return { ttft: t("stLatTtftP50"), ttft95: t("stLatTtftP95"), itl: t("stLatItlP50"), e2e: t("stLatE2eP50"), e2e95: t("stLatE2eP95"), hint: t("stLatSampleHint") }; })()',
    returnByValue: true,
  });
  console.log(JSON.stringify(o.result.value));
  ws.close();
  await fetch(CDP + "/json/close/" + t.id).catch(() => {});
  process.exit(0);
})().catch((e) => { console.error("FAIL", e.message); process.exit(2); });
