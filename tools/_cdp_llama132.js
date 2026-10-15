"use strict";
const CDP = "http://127.0.0.1:9333";
const BASE = "http://127.0.0.1:8791";
(async () => {
  const r = await fetch(CDP + "/json/new?" + encodeURIComponent(BASE), { method: "PUT" });
  const target = await r.json();
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res) => { ws.onopen = res; });
  const errors = [];
  let id = 1;
  const pending = {};
  ws.addEventListener("message", (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending[m.id]) { pending[m.id](m.result); delete pending[m.id]; }
    else if (m.method === "Runtime.exceptionThrown") {
      const d = m.params.exceptionDetails;
      errors.push("UNCAUGHT: " + String((d.exception && d.exception.description) || d.text).slice(0, 200));
    } else if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") {
      errors.push("console.error: " + m.params.args.map((a) => a.value || a.description || "").join(" ").slice(0, 200));
    }
  });
  const send = (method, params) => new Promise((res) => {
    pending[id] = res;
    ws.send(JSON.stringify({ id: id++, method, params: params || {} }));
  });
  await send("Runtime.enable");
  await new Promise((x) => setTimeout(x, 10000));
  const ev = async (expr) => {
    const res = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
    if (res.exceptionDetails) throw new Error(JSON.stringify(res.exceptionDetails).slice(0, 200));
    return res.result.value;
  };
  await ev(`(() => { const b = [...document.querySelectorAll("[data-page]")].find((x) => x.dataset && x.dataset.page === "performance"); if (b) b.click(); return !!b; })()`);
  await new Promise((x) => setTimeout(x, 8000));
  const out = await ev(`(() => {
    const t = (i) => { const el = document.getElementById(i); return el ? (el.textContent || "").trim() : "(missing)"; };
    const el = (i) => document.getElementById(i);
    const routerSec = el("routerSection");
    const loraSec = el("loraSection");
    const strataOnly = el("strataOnly");
    const mtpSec = el("mtpSection");
    return {
      providerBadge: t("ovServerState") || t("serverState"),
      routerSectionVisible: routerSec ? !routerSec.hidden : null,
      routerSub: t("routerSub"),
      routerSelected: t("routerSelected"),
      routerSseState: t("routerSseState"),
      routerModels: [...(document.querySelector("#routerModelList") || document.body).querySelectorAll(".slot-row")].map((r) => r.textContent.trim().replace(/\\s+/g, " ").slice(0, 80)),
      routerEventCount: (document.querySelector("#routerEventFeed") || { children: [] }).children.length,
      routerEventSample: (document.querySelector("#routerEventFeed .re-item") || { textContent: "" }).textContent.trim().replace(/\\s+/g, " ").slice(0, 120),
      loraSectionVisible: loraSec ? !loraSec.hidden : null,
      loraItems: [...(document.querySelector("#loraList") || document.body).querySelectorAll(".lora-item")].map((r) => r.textContent.trim().replace(/\\s+/g, " ")),
      strataOnlyHidden: strataOnly ? strataOnly.hidden : null,
      mtpVisible: mtpSec ? !mtpSec.hidden : null,
      llmAlias: t("llmAlias"),
      llmContext: t("llmContext"),
      llmContextTrain: t("llmContextTrain"),
      llmModal: t("llmModal"),
      slotRows: (document.querySelector("#llmSlotList .slot-table tbody") || { children: [] }).children.length,
    };
  })()`);
  console.log(JSON.stringify(out, null, 1));
  console.log("CONSOLE_ERRORS(" + errors.length + "):");
  [...new Set(errors)].slice(0, 8).forEach((e) => console.log("  " + e));
  ws.close();
  await fetch(CDP + "/json/close/" + target.id).catch(() => {});
  process.exit(errors.length ? 1 : 0);
})().catch((e) => { console.error("FAIL", e.message); process.exit(2); });
