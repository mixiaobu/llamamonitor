const fs = require("fs"), path = require("path");
const m = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "artifacts", "edge-ovr-r2", "measures.json"), "utf8"));
for (const v of ["1920", "1065", "390", "320"]) {
  const x = m[v];
  console.log("\n==== " + v + " ====");
  console.log("statusStrip(server card): h=" + (x.statusStrip && x.statusStrip.h) + " w=" + (x.statusStrip && x.statusStrip.w));
  console.log("sidebar: w=" + (x.sidebar && x.sidebar.w) + " h=" + (x.sidebar && x.sidebar.h));
  console.log("mobileNav: h=" + (x.mobileNav && x.mobileNav.h) + " w=" + (x.mobileNav && x.mobileNav.w) + " mnavLabels=" + JSON.stringify(x.mnavLabels));
  console.log("navLabels=" + JSON.stringify(x.navLabels));
  console.log("actions: " + (x.actionLinks || []).map((a) => a.t + "[h" + a.h + "," + a.font + "]").join(" | "));
  console.log("server: " + JSON.stringify(x.server));
  console.log("perf: " + JSON.stringify(x.perf));
  console.log("hostEls: " + JSON.stringify(x.hostEls));
  console.log("dq: " + JSON.stringify(x.dq));
  console.log("ellipsis: " + JSON.stringify(x.ellipsisCheck));
  console.log("overflowX=" + x.overflowX + " compact=" + x.compact);
}
