/* 1.3.2 前端 CDP 冒烟：Strata 新段（延迟/PCIe/引擎/会话缓存/uptime/查看更多）
   + 状态徽章 + draft 加权 + 新列。用法: node tools/_cdp_smoke132.js [baseUrl] */
"use strict";
const CDP = "http://127.0.0.1:9333";
const BASE = process.argv[2] || "http://127.0.0.1:8790";

async function http(path, method) {
  const r = await fetch(CDP + path, { method });
  return r.json();
}

function cdpSend(ws, id, method, params) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("cdp timeout: " + method)), 30000);
    const onMsg = (ev) => {
      const m = JSON.parse(typeof ev.data === "string" ? ev.data : ev.data.toString());
      if (m.id === id) {
        clearTimeout(timer);
        ws.removeEventListener("message", onMsg);
        if (m.error) reject(new Error(method + ": " + JSON.stringify(m.error)));
        else resolve(m.result);
      }
    };
    ws.addEventListener("message", onMsg);
    ws.send(JSON.stringify({ id, method, params: params || {} }));
  });
}

(async () => {
  const target = await http("/json/new?" + encodeURIComponent(BASE), "PUT");
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });

  const errors = [];
  let nextId = 1;
  const send = (method, params) => cdpSend(ws, nextId++, method, params);
  ws.addEventListener("message", (ev) => {
    const m = JSON.parse(typeof ev.data === "string" ? ev.data : ev.data.toString());
    if (m.method === "Runtime.exceptionThrown") {
      const d = m.params.exceptionDetails;
      const txt = (d.exception && d.exception.description) || d.text || "unknown";
      errors.push("UNCAUGHT: " + String(txt).slice(0, 300));
    } else if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") {
      const txt = m.params.args.map((a) => a.value || a.description || "").join(" ");
      errors.push("console.error: " + String(txt).slice(0, 300));
    }
  });

  await send("Runtime.enable");
  await send("Log.enable");
  await send("Page.enable");
  await new Promise((r) => setTimeout(r, 8000));

  const evalJs = async (expr) => {
    const r = await send("Runtime.evaluate", {
      expression: expr, returnByValue: true, awaitPromise: true,
    });
    if (r.exceptionDetails) throw new Error("eval: " + JSON.stringify(r.exceptionDetails).slice(0, 300));
    return r.result.value;
  };

  // 切到性能页
  await evalJs(`(() => {
    const b = [...document.querySelectorAll("[data-page]")].find((x) => x.dataset && x.dataset.page === "performance");
    if (b) b.click();
    return !!b;
  })()`);
  await new Promise((r) => setTimeout(r, 8000));

  const report = await evalJs(`(() => {
    const t = (id) => { const el = document.getElementById(id); return el ? (el.textContent || "").trim() : "(missing)"; };
    const vis = (id) => { const el = document.getElementById(id); return el ? !el.hidden : null; };
    const so = document.getElementById("strataOnly");
    const latencySec = document.getElementById("stLatencySection");
    const engineSec = document.getElementById("stEngineSection");
    // 延迟段：seg 选项 + P50 值（-- 或具体值）
    const segBtns = latencySec ? [...latencySec.querySelectorAll(".seg button")].map((b) => b.textContent) : [];
    const reqHead = document.querySelector("#stReqList thead");
    return {
      strataOnlyVisible: so ? !so.hidden : null,
      latencySectionPresent: !!latencySec,
      latencySeg: segBtns,
      latTtftP50: t("stLatTtftP50"),
      latTtftP95Hint: t("stLatTtftP95"),
      latItlP50: t("stLatItlP50"),
      latE2eP50: t("stLatE2eP50"),
      latSampleHint: t("stLatSampleHint"),
      pcieRx: t("stHwPcieRx"),
      pcieTx: t("stHwPcieTx"),
      pcieLink: t("stHwPcieLink"),
      chartPciePresent: !!document.getElementById("chartPcie"),
      engineSectionPresent: !!engineSec,
      engExpert: t("stEngExpert"),
      engKv: t("stEngKv"),
      engBatch: t("stEngBatch"),
      engSpec: t("stEngSpec"),
      engPool: t("stEngPool"),
      engVramFree: t("stEngVramFree"),
      engArena: t("stEngArena"),
      uptime: t("stUptime"),
      concurrency: t("stConcurrency"),
      ccBudget: t("stCCBudget"),
      ccSlots: t("stCCSlots"),
      ccBytes: t("stCCBytes"),
      ccParkRestore: t("stCCParkRestore"),
      ccEvictions: t("stCCEvictions"),
      ccLastEvent: t("stCCLastEvent"),
      draftRate: t("stDraftRate"),
      draftHint: t("stDraftHint"),
      running: t("stRunning"),
      waiting: t("stWaiting"),
      moreBtnVisible: vis("stReqMoreBtn"),
      moreBtnText: t("stReqMoreBtn"),
      reqHeadCols: reqHead ? [...reqHead.children[0].children].map((th) => th.textContent) : [],
      ctxTrain: t("llmContextTrain"),
    };
  })()`);

  console.log(JSON.stringify(report, null, 2));
  console.log("CONSOLE_ERRORS(" + errors.length + "):");
  errors.forEach((e) => console.log("  " + e));
  ws.close();
  // 关闭 tab
  await http("/json/close/" + target.id, "GET").catch(() => {});
  process.exit(errors.length ? 1 : 0);
})().catch((e) => {
  console.error("SMOKE FAIL:", e.message);
  process.exit(2);
});
