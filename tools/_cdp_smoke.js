/* 1.3.0 前端 CDP 冒烟：打开 dev server，检查 console 无未捕获错误 +
   概览/性能/设置页 Strata 内容。用法: node tools/_cdp_smoke.js [baseUrl] */
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
  // 新 tab
  const target = await http("/json/new?" + encodeURIComponent(BASE), "PUT");
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });

  const errors = [];
  let nextId = 1;
  const send = (method, params) => cdpSend(ws, nextId++, method, params);
  const events = [];
  ws.addEventListener("message", (ev) => {
    const m = JSON.parse(typeof ev.data === "string" ? ev.data : ev.data.toString());
    if (m.method === "Runtime.exceptionThrown") {
      const d = m.params.exceptionDetails;
      const txt = (d.exception && d.exception.description) || d.text || "unknown";
      errors.push("UNCAUGHT: " + String(txt).slice(0, 300));
    } else if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") {
      const txt = m.params.args.map((a) => a.value || a.description || "").join(" ");
      errors.push("console.error: " + String(txt).slice(0, 300));
    } else if (m.method === "Log.entryAdded" && m.params.entry.level === "error") {
      errors.push("log.error: " + (m.params.entry.text || "").slice(0, 300));
    }
  });

  await send("Runtime.enable");
  await send("Log.enable");
  await send("Page.enable");
  // 等页面加载 + 若干轮轮询
  await new Promise((r) => setTimeout(r, 12000));

  const evalJs = async (expr) => {
    const r = await send("Runtime.evaluate", {
      expression: expr, returnByValue: true, awaitPromise: true,
    });
    if (r.exceptionDetails) throw new Error("eval: " + JSON.stringify(r.exceptionDetails).slice(0, 300));
    return r.result.value;
  };

  const report = {};
  // ---- Overview ----
  report.overview = await evalJs(`(() => {
    const t = (id) => (document.getElementById(id) || {}).textContent || "";
    return {
      serviceName: t("ovServiceName").trim(),
      serverState: t("ovServerState").trim(),
      modelLine: t("ovModelLine").trim(),
      ctx: t("ovContext").trim(),
      slots: t("ovSlots").trim(),
    };
  })()`);

  // ---- 切到性能页 ----
  await evalJs(`(() => {
    const btns = [...document.querySelectorAll("nav button, .rail-item, [data-page]")];
    const b = btns.find((x) => (x.dataset && x.dataset.page === "performance") || (x.textContent || "").includes("性能"));
    if (b) b.click();
    return !!b;
  })()`);
  await new Promise((r) => setTimeout(r, 6000));
  report.performance = await evalJs(`(() => {
    const t = (id) => (document.getElementById(id) || {}).textContent || "";
    const so = document.getElementById("strataOnly");
    const mtp = document.getElementById("mtpSection");
    return {
      strataOnlyVisible: so ? !so.hidden : null,
      mtpHidden: mtp ? mtp.hidden : null,
      phase: t("stPhase").trim(),
      tokSMean: t("stTokSMean").trim(),
      prefill: t("stPrefill").trim(),
      running: t("stRunning").trim(),
      ccEnabled: t("stCCEnabled").trim(),
      ccReused: t("stCCReused").trim(),
      draftRate: t("stDraftRate").trim(),
      reqRows: (document.querySelector("#stReqList .slot-table tbody") || { children: [] }).children.length,
      hwGpuName: t("stHwGpuName").trim(),
      hwGpuUtil: t("stHwGpuUtil").trim(),
      slotRows: (document.querySelector("#llmSlotList .slot-table tbody") || { children: [] }).children.length,
      llmAlias: t("llmAlias").trim(),
      llmBuildLabel: t("llmBuildLabel").trim(),
      llmBuild: t("llmBuild").trim(),
    };
  })()`);

  // ---- 设置页 ----
  await evalJs(`(() => {
    const b = [...document.querySelectorAll("[data-page]")].find((x) => x.dataset.page === "settings")
      || [...document.querySelectorAll("nav button")].find((x) => (x.textContent || "").includes("设置"));
    if (b) b.click();
    return !!b;
  })()`);
  await new Promise((r) => setTimeout(r, 3000));
  report.settings = await evalJs(`(() => {
    const el = (id) => document.getElementById(id);
    return {
      providerValue: el("setProvider") ? el("setProvider").value : null,
      hasApiKey: !!el("setApiKey"),
      apiKeyPlaceholder: el("setApiKey") ? el("setApiKey").placeholder : null,
    };
  })()`);

  console.log(JSON.stringify(report, null, 2));
  console.log("CONSOLE_ERRORS(" + errors.length + "):");
  errors.forEach((e) => console.log("  " + e));
  ws.close();
  process.exit(errors.length ? 1 : 0);
})().catch((e) => {
  console.error("SMOKE FAIL:", e.message);
  process.exit(2);
});
