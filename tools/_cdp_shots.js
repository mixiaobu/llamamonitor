/* 1.3.0 报告截图：Strata 概览/性能/设置（桌面 1440x900 + 移动 390x844）。
   用法: node tools/_cdp_shots.js [baseUrl] */
"use strict";
const fs = require("fs");
const CDP = "http://127.0.0.1:9333";
const BASE = process.argv[2] || "http://127.0.0.1:8790";
const OUT = "C:\\Users\\mixiaobu\\Desktop\\ai\\LlamaMonitor\\docs\\images";
async function http(p, m) { const r = await fetch(CDP + p, { method: m }); return r.json(); }

(async () => {
  const target = await http("/json/new?" + encodeURIComponent(BASE), "PUT");
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let nextId = 1;
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
  await send("Page.enable");
  await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
  await new Promise((r) => setTimeout(r, 9000));
  const evalJs = async (e) => (await send("Runtime.evaluate", {
    expression: e, returnByValue: true, awaitPromise: true,
  })).result.value;
  const shot = async (name) => {
    const r = await send("Page.captureScreenshot", { format: "png" });
    fs.writeFileSync(OUT + "\\130_" + name + ".png", Buffer.from(r.data, "base64"));
    console.log("saved " + name);
  };

  // 概览（桌面）
  await shot("overview_desktop");
  // 性能（桌面，滚动到 Strata 段）
  await evalJs(`document.querySelector('.nav-item[data-page="performance"]').click()`);
  await new Promise((r) => setTimeout(r, 3500));
  await shot("performance_desktop");
  await evalJs(`document.getElementById("strataOnly").scrollIntoView({block:"start"})`);
  await new Promise((r) => setTimeout(r, 1500));
  await shot("performance_strata_desktop");
  // 设置（桌面，连接卡）
  await evalJs(`document.querySelector('.nav-item[data-page="settings"]').click()`);
  await new Promise((r) => setTimeout(r, 2000));
  await evalJs(`(document.getElementById('setProvider')||document.body).scrollIntoView({block:"center"})`);
  await new Promise((r) => setTimeout(r, 1000));
  await shot("settings_desktop");
  // 移动端
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
  await new Promise((r) => setTimeout(r, 1500));
  await evalJs(`(document.querySelector('.mnav-item[data-page="overview"]')||document.body).click()`);
  await new Promise((r) => setTimeout(r, 2000));
  await shot("overview_mobile");
  await evalJs(`(document.querySelector('.mnav-item[data-page="performance"]')||document.body).click()`);
  await new Promise((r) => setTimeout(r, 3500));
  await evalJs(`document.getElementById("strataOnly").scrollIntoView({block:"start"})`);
  await new Promise((r) => setTimeout(r, 1000));
  await shot("performance_strata_mobile");
  console.log("DONE");
  ws.close();
  process.exit(0);
})().catch((e) => { console.error("SHOTS FAIL:", e.message); process.exit(2); });
