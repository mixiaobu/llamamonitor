(function(){
  var out = {};
  var page = document.querySelector(".page.active");
  out.page = page ? page.id : null;
  var g = document.getElementById("gpuProc");
  out.gpu = g ? { len: g.innerHTML.length, rows: g.querySelectorAll("tbody tr").length } : "missing";
  var rws = document.querySelectorAll("#gpuProc .gpu-proc-table tbody tr");
  if (rws.length) {
    function wd(el){ var r=el.getBoundingClientRect(); return Math.round(r.width)+"x"+Math.round(r.height); }
    out.firstRow = wd(rws[0]);
    out.tdRects = Array.prototype.map.call(rws[0].querySelectorAll("td"), function(td){ return (td.dataset.label||"app")+"/"+Math.round(td.getBoundingClientRect().width); });
    out.rowDisplay = getComputedStyle(rws[0]).display;
    out.rowGridCols = getComputedStyle(rws[0]).gridTemplateColumns;
    var tds = rws[0].querySelectorAll("td");
    if (tds.length >= 3) {
      var a = tds[1].getBoundingClientRect(), b = tds[2].getBoundingClientRect();
      out.overlap23 = a.right > b.left ? Math.round(a.right - b.left) + "px" : "none";
    }
  }
  return JSON.stringify(out, null, 1);
})()
