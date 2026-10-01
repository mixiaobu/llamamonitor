(async function(){
  var out = {};
  try {
    var st = window.LM.state;
    out.stateExposed = !!st;
    if (st) {
      out.range = st.historyRange;
      out.gapsGen = st.histGapsGen;
      out.gapsLoading = st.histGapsLoading;
      out.gapsLen = (st.histGaps||[]).length;
      out.gapsHasMore = st.histGapsHasMore;
    }
  } catch(e){ out.err = e.message; }
  out.vis = document.visibilityState;
  out.cnt = (document.getElementById("gapsCountLabel")||{}).textContent;
  out.rows = document.querySelectorAll("#gapsTbody tr.gap-row").length;
  // 直接 fetch 一次页面同款 URL
  var params = "preset=" + ((window.LM.state&&window.LM.state.historyRange)||"7d") + "&limit=20";
  try {
    var t0 = Date.now();
    var r = await fetch("/api/history/gaps?" + params);
    var d = await r.json();
    out.direct = { ok:true, gaps:(d.gaps||[]).length, ms:Date.now()-t0 };
  } catch(e){ out.direct = { ok:false, err:e.message }; }
  return JSON.stringify(out, null, 1);
})()
