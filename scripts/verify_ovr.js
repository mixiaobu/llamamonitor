const fs = require("fs"), path = require("path");
const m = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "artifacts", "edge-ovr-r2", "measures.json"), "utf8"));
let fail = 0;
for (const v of ["320", "360", "390", "430", "760", "900", "1065", "1366", "1600", "1920", "2560"]) {
  const x = m[v];
  const e = x.ellipsisCheck;
  const clip = Object.entries(e).filter(([, b]) => b);
  const ok = x.overflowX <= 0 && clip.length === 0;
  if (!ok) fail++;
  console.log(
    v.padEnd(5),
    "ovfX=" + x.overflowX,
    "hero2col=" + (x.todayHero.cols === 2),
    "brk=" + x.todayBreakdown.cols,
    "perf=" + x.ovPerfGrid.cols,
    "host=" + x.hostGrid.cols,
    "gpu=" + x.gpuMiniGrid.cols,
    "dq=" + x.dqGrid.cols,
    clip.length ? "CLIPPED=" + clip.map((c) => c[0]).join(",") : "no-clip",
    ok ? "OK" : "ISSUE"
  );
}
// hero same-row check (1065 is the spec-critical one)
console.log("\n1065 hero same-row:", m["1065"].hero1Rect.y === m["1065"].hero2Rect.y, "(y " + m["1065"].hero1Rect.y + ")");
console.log("1065 perf same-row:", m["1065"].perfCards[0].y === m["1065"].perfCards[1].y, "(y " + m["1065"].perfCards[0].y + ")");
console.log("1065 sidebar compact:", m["1065"].compact, "navw=" + m["1065"].sidebar.w);
console.log("\n390 full-page bottom nav present (mobileNav y=" + m["390"].mobileNav.y + " h=" + m["390"].mobileNav.h + ")");
process.exit(fail ? 1 : 0);
