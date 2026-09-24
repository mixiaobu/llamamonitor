/* ============================================================
   Final Release Gate — isLocal() 回归测试（Node，无需浏览器）

   覆盖：
   - REL-1.0.0-002：web.host=0.0.0.0 → 桌面窗口 URL hostname "0.0.0.0" 必须判为本机
   - 16E 原矩阵：127.0.0.1 / localhost / ::1 / [::1] 为本机
   - 远程：局域网 IP / 域名 / 空 hostname 必须判为远程

   运行：node tools/final_islocal_test.js（exit 0 = PASS）
   ============================================================ */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const src = fs.readFileSync(path.join(__dirname, "..", "static", "js", "api.js"), "utf8");

function isLocalFor(hostname) {
  const sandbox = {
    location: { hostname },
    fetch: function () { return Promise.resolve({}); },
    setTimeout: global.setTimeout,
    clearTimeout: global.clearTimeout,
    AbortController: global.AbortController,
    Error: global.Error,
    JSON: global.JSON,
    console: global.console,
  };
  sandbox.window = sandbox; // api.js 末尾挂 window.LM
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: "api.js" });
  return sandbox.window.LM.api.isLocal();
}

const cases = [
  // [hostname, expectedLocal, label]
  ["127.0.0.1", true, "loopback IPv4"],
  ["localhost", true, "localhost"],
  ["::1", true, "loopback IPv6"],
  ["[::1]", true, "bracketed IPv6"],
  ["0.0.0.0", true, "REL-1.0.0-002: web.host=0.0.0.0 桌面窗口"],
  ["172.16.1.2", false, "局域网 IP（手机）"],
  ["192.168.1.50", false, "局域网 IP"],
  ["llama.local", false, "域名"],
  ["", false, "空 hostname"],
];

let failed = 0;
for (const [h, expected, label] of cases) {
  const got = isLocalFor(h);
  const ok = got === expected;
  console.log((ok ? "PASS" : "FAIL") + "  isLocal('" + h + "') = " + got + " (expect " + expected + ")  [" + label + "]");
  if (!ok) failed++;
}

if (failed > 0) {
  console.error("\nISLOCAL TEST FAIL: " + failed + " case(s)");
  process.exit(1);
}
console.log("\nISLOCAL TEST PASS: " + cases.length + "/" + cases.length);
