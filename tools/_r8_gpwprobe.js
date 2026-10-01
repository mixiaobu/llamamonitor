(async function(){
  var out = {};
  var page = document.querySelector(".page.active");
  out.page = page ? page.id : null;
  // 等 GPU 进程表出现
  var deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    if (document.querySelector("#gpuProc .gpu-proc-table tbody tr") || document.querySelector(".gpu-proc-empty")) break;
    await new Promise(function(r){ setTimeout(r, 1000); });
  }
  var g = document.getElementById("gpuProc");
  out.gpuProcHTML = g ? g.innerHTML.slice(0, 200) : "missing";
  var rws = document.querySelectorAll("#gpuProc .gpu-proc-table tbody tr");
  out.rowCount = rws.length;
  if (rws.length) {
    function w(el){ var r=el.getBoundingClientRect(); return Math.round(r.width)+"x"+Math.round(r.height); }
    out.firstRow = w(rws[0]);
    out.tdRects = Array.prototype.map.call(rws[0].querySelectorAll("td"), function(td){ return (td.dataset.label||"app")+"/"+Math.round(td.getBoundingClientRect().width); });
    out.rowDisplay = getComputedStyle(rws[0]).display;
    out.rowGridCols = getComputedStyle(rws[0]).gridTemplateColumns;
    // 重叠检测：第2、3字段 td
    var t2 = rws[0].querySelectorAll("td")[1], t3 = rws[0].querySelectorAll("td")[2];
    if (t2 && t3) {
      var a = t2.getBoundingClientRect(), b = t3.getBoundingClientRect();
      out.overlap23 = a.right > b.left ? Math.round(a.right - b.left) + "px" : "none";
      out.t2x = Math.round(a.left) + "-" + Math.round(a.right);
      out.t3x = Math.round(b.left) + "-" + Math.round(b.right);
    }
    out.rowRect = (function(){ var r = rws[0].getBoundingClientRect(); return Math.round(r.width)+"x"+Math.round(r.height); })();
  }
  return JSON.stringify(out, null, 1);
})()
