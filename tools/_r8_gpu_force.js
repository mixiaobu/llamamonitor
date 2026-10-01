(function(){
  // 手动拉数据并直接驱动渲染（绕开 onShow/轮询，验证 CSS 本身）
  window.__diag = { step: "fetch" };
  return fetch("/api/gpu/status").then(function (r) { return r.json(); }).then(function (d) {
    window.__diag.fetched = (d.processes || []).length;
    // 用 app 内部路径：切到 overview 再切回 gpu，强制触发 onShow 的 refreshGpuStatus
    LM.nav.showPage("overview");
    LM.nav.showPage("gpu");
    return new Promise(function (res) {
      var n = 0;
      (function poll() {
        n++;
        var rows = document.querySelectorAll("#gpuProc .gpu-proc-table tbody tr");
        if (rows.length || n > 15) { window.__diag.rows = rows.length; res(); return; }
        setTimeout(poll, 1000);
      })();
    });
  }).then(function () {
    var out = { diag: window.__diag };
    var rws = document.querySelectorAll("#gpuProc .gpu-proc-table tbody tr");
    if (rws.length) {
      var tds = rws[0].querySelectorAll("td");
      out.rowRect = (function(){ var r=rws[0].getBoundingClientRect(); return Math.round(r.width)+"x"+Math.round(r.height); })();
      out.rowDisplay = getComputedStyle(rws[0]).display;
      out.tdRects = Array.prototype.map.call(tds, function(td){ var r=td.getBoundingClientRect(); return (td.dataset.label||"app")+"/"+Math.round(r.width); });
      if (tds.length >= 3) {
        var a = tds[1].getBoundingClientRect(), b = tds[2].getBoundingClientRect();
        out.overlap23 = a.right > b.left ? Math.round(a.right - b.left) + "px" : "none";
      }
    }
    return JSON.stringify(out, null, 1);
  });
})()
