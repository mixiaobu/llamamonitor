const fs = require("fs"), path = require("path");
const OUT = path.join(__dirname, "..", "artifacts", "edge-ovr-r2");
const m = JSON.parse(fs.readFileSync(path.join(OUT, "measures.json"), "utf8"));
for (const [v, x] of Object.entries(m)) {
  const o = (n) => (x[n] ? x[n].cols : "-");
  console.log(
    v.padEnd(5),
    "ovfX=" + x.overflowX,
    "hero=" + o("todayHero"),
    "brk=" + o("todayBreakdown"),
    "perf=" + o("ovPerfGrid"),
    "host=" + o("hostGrid"),
    "gpu=" + o("gpuMiniGrid"),
    "dq=" + o("dqGrid"),
    "| gpuCards=" + (x.gpuMiniCards || []).length,
    "| heroSameRow=" + (x.hero1Rect && x.hero2Rect ? x.hero1Rect.y === x.hero2Rect.y : "?"),
    "| perfRows=" + (x.perfCards || []).map((c) => c.y).join(","),
  );
}
console.log("\n-- key content (1920) --");
const a = m["1920"];
console.log(JSON.stringify({ nav: a.navLabels, mnav: a.mnavLabels, server: a.server, perf: a.perf, hostEls: a.hostEls, dq: a.dq, gpuLine: a.gpuLine }, null, 1));
