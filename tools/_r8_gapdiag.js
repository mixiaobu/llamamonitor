(async function(){
  var out = {};
  out.range = window.LM.state ? window.LM.state.historyRange : "(no state)";
  out.gen = window.LM.state ? window.LM.state.histGapsGen : null;
  // 手动切走再切回，触发 onShow 的 refreshHistoryGaps
  LM.nav.showPage("overview");
  await new Promise(function(r){ setTimeout(r, 1200); });
  LM.nav.showPage("history");
  // 直接抓 app 会请求的 gaps URL，确认网络层 OK
  var params = "preset=" + (window.LM.state.historyRange || "7d") + "&limit=20";
  var t0 = Date.now();
  try {
    var r = await fetch("/api/history/gaps?" + params);
    var d = await r.json();
    out.directFetch = { ok: true, gaps: (d.gaps||[]).length, ms: Date.now()-t0 };
  } catch(e) { out.directFetch = { ok: false, err: e.message }; }
  // 等渲染
  var deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    if (document.querySelectorAll("#gapsTbody tr.gap-row").length) break;
    await new Promise(function(r){ setTimeout(r, 1000); });
  }
  out.rows = document.querySelectorAll("#gapsTbody tr.gap-row").length;
  out.cnt = (document.getElementById("gapsCountLabel")||{}).textContent;
  out.genAfter = window.LM.state ? window.LM.state.histGapsGen : null;
  out.histGapsLen = window.LM.state ? (window.LM.state.histGaps||[]).length : null;
  return JSON.stringify(out, null, 1);
})()
