/* 1.3.0 视口扫描：10 个视口 × 性能/概览/设置页——
   1) console 无未捕获错误 2) 无嵌套垂直滚动容器 3) 移动端 Strata 表转卡片。
   用法: node tools/_cdp_viewports.js [baseUrl] */
"use strict";
const CDP = "http://127.0.0.1:9333";
const BASE = process.argv[2] || "http://127.0.0.1:8790";
const SIZES = [
  [1920, 1080], [1680, 1050], [1440, 900], [1366, 768], [1280, 800],
  [1180, 720], [1024, 768], [900, 800], [768, 1024], [390, 844],
];

async function http(path, method) {
  const r = await fetch(CDP + path, { method });
  return r.json();
}

(async () => {
  const target = await http("/json/new?" + encodeURIComponent(BASE), "PUT");
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let nextId = 1;
  const errors = [];
  const send = (method, params) => new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("cdp timeout " + method)), 30000);
    const onMsg = (ev) => {
      const m = JSON.parse(typeof ev.data === "string" ? ev.data : ev.data.toString());
      if (m.id === nextId - 1) {
        clearTimeout(timer);
        ws.removeEventListener("message", onMsg);
        m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result);
      }
    };
    ws.addEventListener("message", onMsg);
    ws.send(JSON.stringify({ id: nextId, method, params: params || {} }));
    nextId++;
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
  await send("Runtime.enable");
  await send("Page.enable");
  await new Promise((r) => setTimeout(r, 8000));

  const evalJs = async (expr) => (await send("Runtime.evaluate", {
    expression: expr, returnByValue: true, awaitPromise: true,
  })).result.value;

  const PAGES = ["overview", "performance", "usage", "history", "settings", "system", "gpu", "about"];
  let problems = [];
  for (const [w, h] of SIZES) {
    await send("Emulation.setDeviceMetricsOverride", {
      width: w, height: h, deviceScaleFactor: 1, mobile: w <= 768,
    });
    await new Promise((r) => setTimeout(r, 1200));
    for (const page of PAGES) {
      await evalJs(`(() => {
        const b = document.querySelector('.nav-item[data-page="${page}"], .mnav-item[data-page="${page}"], .sheet-item[data-page="${page}"]');
        if (b) b.click();
        return !!b;
      })()`);
      await new Promise((r) => setTimeout(r, 900));
      const check = await evalJs(`(() => {
        const out = { nestedScroll: [], overflowH: [] };
        // 嵌套垂直滚动：overflow-y auto/scroll 且内容真实垂直溢出（scrollHeight>clientHeight）。
        // 注意：overflow-x:auto 会按 CSS 规格把 overflow-y 计算为 auto，纯横向滚动器无垂直溢出。
        document.querySelectorAll("main *").forEach((el) => {
          const cs = getComputedStyle(el);
          if (/(auto|scroll)/.test(cs.overflowY) && el.clientHeight > 0
              && el.scrollHeight > el.clientHeight + 2) {
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) {
              out.nestedScroll.push(el.id ? "#" + el.id : el.className ? "." + String(el.className).split(" ")[0] : el.tagName);
            }
          }
          if (cs.overflowX === "auto" && el.scrollWidth > el.clientWidth + 2) {
            out.overflowH.push(el.id ? "#" + el.id : el.className ? "." + String(el.className).split(" ")[0] : el.tagName);
          }
        });
        // 页面横向溢出
        if (document.documentElement.scrollWidth > document.documentElement.clientWidth + 2) {
          out.pageOverflowX = document.documentElement.scrollWidth + ">" + document.documentElement.clientWidth;
        }
        return out;
      })()`);
      const uniq = (a) => [...new Set(a)].slice(0, 4);
      if (check.nestedScroll.length) problems.push(`[${w}x${h} ${page}] nested-scroll: ${uniq(check.nestedScroll).join(",")}`);
      if (check.overflowH.length) problems.push(`[${w}x${h} ${page}] h-scroll: ${uniq(check.overflowH).join(",")}`);
      if (check.pageOverflowX) problems.push(`[${w}x${h} ${page}] page-overflow-x: ${check.pageOverflowX}`);
    }
  }
  // 移动端 Slot/请求表卡片化检查（390x844 + performance）
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await new Promise((r) => setTimeout(r, 800));
  await evalJs(`(() => { const b = document.querySelector('.mnav-item[data-page="performance"]') || document.querySelector('[data-page="performance"]'); if (b) b.click(); })()`);
  await new Promise((r) => setTimeout(r, 3000));
  const mobileCards = await evalJs(`(() => {
    const t = document.querySelector("#stReqList .slot-table");
    const s = document.querySelector("#llmSlotList .slot-table");
    const g = getComputedStyle(t || document.body).display;
    return { reqTableDisplay: g, slotTableDisplay: s ? getComputedStyle(s).display : null,
             strataVisible: !document.getElementById("strataOnly").hidden };
  })()`);
  console.log("MOBILE_CARDS:", JSON.stringify(mobileCards));
  console.log("PROBLEMS(" + problems.length + "):");
  problems.forEach((p) => console.log("  " + p));
  console.log("CONSOLE_ERRORS(" + errors.length + "):");
  [...new Set(errors)].slice(0, 10).forEach((e) => console.log("  " + e));
  ws.close();
  process.exit(problems.length || errors.length ? 1 : 0);
})().catch((e) => { console.error("VIEWPORT SWEEP FAIL:", e.message); process.exit(2); });
