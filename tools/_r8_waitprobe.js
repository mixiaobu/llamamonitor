(async function(){
  function w(el){ if(!el) return null; var r=el.getBoundingClientRect(); return {w:Math.round(r.width),h:Math.round(r.height)}; }
  var out = {};
  var page = document.querySelector(".page.active");
  out.page = page ? page.id : null;
  out.viewport = window.innerWidth + "x" + window.innerHeight;

  // 等数据出现（最多 25s），每 1s 探测一次
  var deadline = Date.now() + 25000;
  var got = { slot:false, gpu:false, core:false, gap:false };
  while (Date.now() < deadline) {
    if (page.id === "page-performance" && document.querySelector(".slot-table tr.slot-row")) got.slot = true;
    if (page.id === "page-gpu" && document.querySelector("#gpuProc .gpu-proc-table tbody tr")) got.gpu = true;
    if (page.id === "page-system" && document.querySelector(".core-heat-grid .core-cell")) got.core = true;
    if (page.id === "page-history" && document.querySelector("#gapsTbody tr.gap-row")) got.gap = true;
    if (got.slot || got.gpu || got.core || got.gap) break;
    await new Promise(function(r){ setTimeout(r, 1000); });
  }
  out.got = got;

  if (page.id === "page-performance") {
    var st = document.querySelector(".slot-table");
    var rows = document.querySelectorAll(".slot-table tr.slot-row");
    out.slot = {
      table: w(st), rowCount: rows.length,
      firstRow: rows.length ? w(rows[0]) : null,
      wrap: w(document.querySelector(".slot-card-wrap")),
      tdWidths: rows.length ? Array.prototype.map.call(rows[0].querySelectorAll("td"), function(td){ return Math.round(td.getBoundingClientRect().width); }) : [],
    };
  }
  if (page.id === "page-gpu") {
    var tbl = document.querySelector("#gpuProc .gpu-proc-table");
    var rws = tbl ? tbl.querySelectorAll("tbody tr") : [];
    out.gpuProc = {
      table: w(tbl), rowCount: rws.length,
      firstRow: rws.length ? w(rws[0]) : null,
      tdWidths: rws.length ? Array.prototype.map.call(rws[0].querySelectorAll("td"), function(td){ return Math.round(td.getBoundingClientRect().width); }) : [],
      computedRowDisplay: rws.length ? getComputedStyle(rws[0]).display : null,
    };
  }
  if (page.id === "page-system") {
    var grid = document.querySelector(".core-heat-grid");
    var cells = document.querySelectorAll(".core-cell");
    out.coreHeat = {
      grid: w(grid), cellCount: cells.length,
      cellRects: Array.prototype.slice.call(cells,0,4).map(function(c){ var r=c.getBoundingClientRect(); return Math.round(r.width)+"x"+Math.round(r.height); }),
      gridCols: grid ? getComputedStyle(grid).gridTemplateColumns : null,
      // 检测相邻 cell 是否重叠
      overlap: (function(){ if (cells.length<2) return null; var a=cells[0].getBoundingClientRect(), b=cells[1].getBoundingClientRect(); return a.right > b.left ? (Math.round(a.right-b.left))+"px" : "none"; })(),
    };
  }
  if (page.id === "page-history") {
    var rows2 = document.querySelectorAll("#gapsTbody tr.gap-row");
    out.gaps = {
      rowCount: rows2.length,
      firstRow: rows2.length ? w(rows2[0]) : null,
      firstTdWidths: rows2.length ? Array.prototype.map.call(rows2[0].querySelectorAll("td"), function(td){ return (td.dataset.label||"?")+"/"+Math.round(td.getBoundingClientRect().width); }) : [],
      firstTdDisplays: rows2.length ? Array.prototype.map.call(rows2[0].querySelectorAll("td"), function(td){ return getComputedStyle(td).display; }) : [],
    };
  }
  return JSON.stringify(out, null, 1);
})()
