"use strict";
const CDP = "http://127.0.0.1:9333";
const BASE = "http://127.0.0.1:8790";
(async () => {
  const r = await fetch(CDP + "/json/new?" + encodeURIComponent(BASE), { method: "PUT" });
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
  await new Promise((x) => setTimeout(x, 8000));
  const ev = async (expr) => {
    const res = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
    if (res.exceptionDetails) throw new Error(JSON.stringify(res.exceptionDetails).slice(0, 200));
    return res.result.value;
  };
  // 切到性能页
  await ev(`(() => { const b = [...document.querySelectorAll("[data-page]")].find((x) => x.dataset && x.dataset.page === "performance"); if (b) b.click(); return !!b; })()`);
  await new Promise((x) => setTimeout(x, 6000));
  const out = await ev(`(() => {
    const t = (i) => { const el = document.getElementById(i); return el ? (el.textContent || "").trim() : "(missing)"; };
    return {
      latTtftP50: t("stLatTtftP50"),
      latTtftP95: t("stLatTtftP95"),
      latItlP50: t("stLatItlP50"),
      latE2eP50: t("stLatE2eP50"),
      latSampleHint: t("stLatSampleHint"),
      pcieRx: t("stHwPcieRx"),
      pcieTx: t("stHwPcieTx"),
      pcieLink: t("stHwPcieLink"),
      // PCIe 趋势图 canvas 是否渲染
      chartPcieHasCanvas: !!document.querySelector("#chartPcie canvas"),
      // 点击"查看更多"
      moreBefore: t("stReqMoreBtn"),
    };
  })()`);
  // 点击"查看更多"
  await ev(`(() => { const b = document.getElementById("stReqMoreBtn"); if (b) b.click(); return !!b; })()`);
  await new Promise((x) => setTimeout(x, 3000));
  const after = await ev(`(() => {
    const t = (i) => { const el = document.getElementById(i); return el ? (el.textContent || "").trim() : "(missing)"; };
    const rows = (document.querySelector("#stReqList .slot-table tbody") || { children: [] });
    return { moreAfter: t("stReqMoreBtn"), reqSub: t("stReqSub"), rowCount: rows.children.length };
  })()`);
  console.log(JSON.stringify({ before: out, after }, null, 1));
  ws.close();
  await fetch(CDP + "/json/close/" + target.id).catch(() => {});
  process.exit(0);
})().catch((e) => { console.error("FAIL", e.message); process.exit(2); });
