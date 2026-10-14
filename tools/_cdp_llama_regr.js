/* 1.3.0 llama.cpp 模式 CDP 回归：strataOnly 隐藏 / MTP 显示 / 10 列 Slot 表 /
   概览 llama.cpp 名称。用法: node tools/_cdp_llama_regr.js [baseUrl] */
"use strict";
const CDP = "http://127.0.0.1:9333";
const BASE = process.argv[2] || "http://127.0.0.1:8791";
async function http(p, m) { const r = await fetch(CDP + p, { method: m }); return r.json(); }

(async () => {
  const target = await http("/json/new?" + encodeURIComponent(BASE), "PUT");
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let nextId = 1; const errors = [];
  const send = (method, params) => new Promise((resolve, reject) => {
    const id = nextId++;
    const timer = setTimeout(() => reject(new Error("cdp timeout " + method)), 30000);
    const onMsg = (ev) => {
      const m = JSON.parse(typeof ev.data === "string" ? ev.data : ev.data.toString());
      if (m.id === id) {
        clearTimeout(timer); ws.removeEventListener("message", onMsg);
        m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result);
      }
    };
    ws.addEventListener("message", onMsg);
    ws.send(JSON.stringify({ id, method, params: params || {} }));
  });
  ws.addEventListener("message", (ev) => {
    const m = JSON.parse(typeof ev.data === "string" ? ev.data : ev.data.toString());
    if (m.method === "Runtime.exceptionThrown") {
      const d = m.params.exceptionDetails;
      errors.push("UNCAUGHT: " + String((d.exception && d.exception.description) || d.text).slice(0, 200));
    } else if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") {
      errors.push("console.error: " + m.params.args.map((a) => a.value || a.description || "").join(" ").slice(0, 200));
    }
  });
  await send("Runtime.enable"); await send("Page.enable");
  await new Promise((r) => setTimeout(r, 10000));
  const evalJs = async (e) => (await send("Runtime.evaluate", {
    expression: e, returnByValue: true, awaitPromise: true,
  })).result.value;

  await evalJs(`document.querySelector('.nav-item[data-page="performance"]').click()`);
  await new Promise((r) => setTimeout(r, 4000));
  const perf = await evalJs(`(() => {
    const t = (id) => (document.getElementById(id) || {}).textContent || "";
    const so = document.getElementById("strataOnly");
    const mtp = document.getElementById("mtpSection");
    const headers = [...document.querySelectorAll("#llmSlotList .slot-table thead th")].map((x) => x.textContent.trim());
    const rows = (document.querySelector("#llmSlotList .slot-table tbody") || { children: [] }).children.length;
    return {
      strataOnlyHidden: so ? so.hidden : null,
      mtpVisible: mtp ? !mtp.hidden : null,
      slotColumns: headers,
      slotRows: rows,
      llmBuildLabel: t("llmBuildLabel").trim(),
      llmBuild: t("llmBuild").trim(),
      modelEnvTitle: t("modelEnvTitle").trim(),
    };
  })()`);

  const ov = await evalJs(`(() => {
    const t = (id) => (document.getElementById(id) || {}).textContent || "";
    return { serviceName: t("ovServiceName").trim(), slots: t("ovSlots").trim(), ctx: t("ovContext").trim() };
  })()`);

  console.log(JSON.stringify({ perf, ov }, null, 2));
  console.log("CONSOLE_ERRORS(" + errors.length + "):");
  [...new Set(errors)].slice(0, 8).forEach((e) => console.log("  " + e));
  ws.close();
  process.exit(errors.length ? 1 : 0);
})().catch((e) => { console.error("LLAMA REGR FAIL:", e.message); process.exit(2); });
