(function(){
  function w(el){ if(!el) return null; var r=el.getBoundingClientRect(); return {w:Math.round(r.width),h:Math.round(r.height)}; }
  var out = {};
  var page = document.querySelector(".page.active");
  out.page = page ? page.id : null;
  if (location.hash.indexOf("perf") !== -1 || page && page.id === "page-performance") {
    var wrap = document.querySelector(".slot-card-wrap");
    var st = document.querySelector(".slot-table");
    var rows = document.querySelectorAll(".slot-table tr.slot-row");
    var tds = rows.length ? rows[0].querySelectorAll("td") : [];
    out.slot = {
      wrap: w(wrap), table: w(st), rowCount: rows.length,
      tdCount: tds.length,
      tdRects: Array.prototype.map.call(tds, function(td){ return (td.dataset.label||td.className).slice(0,12)+":"+Math.round(td.getBoundingClientRect().width); }),
      firstRowRect: rows.length ? w(rows[0]) : null,
      computedTableDisplay: st ? getComputedStyle(st).display : null,
      computedFirstTdDisplay: tds.length ? getComputedStyle(tds[0]).display : null,
    };
  }
  if (page && page.id === "page-gpu") {
    var box = document.getElementById("gpuProc");
    var tbl = box ? box.querySelector(".gpu-proc-table") : null;
    var rws = tbl ? tbl.querySelectorAll("tbody tr") : [];
    out.gpuProc = {
      box: w(box), table: w(tbl), rowCount: rws.length,
      firstRowRect: rws.length ? w(rws[0]) : null,
      tdRects: rws.length ? Array.prototype.map.call(rws[0].querySelectorAll("td"), function(td){ return (td.dataset.label||"head")+"/"+Math.round(td.getBoundingClientRect().width); }) : [],
    };
  }
  if (page && page.id === "page-system") {
    var d = document.querySelector("details.core-heat");
    var grid = document.querySelector(".core-heat-grid");
    var cells = document.querySelectorAll(".core-cell");
    var cellRects = Array.prototype.slice.call(cells, 0, 6).map(function(c){ return Math.round(c.getBoundingClientRect().width)+"x"+Math.round(c.getBoundingClientRect().height); });
    out.coreHeat = {
      detailsOpen: d ? d.open : null, grid: w(grid), cellCount: cells.length,
      cellRects: cellRects,
      gridCols: grid ? getComputedStyle(grid).gridTemplateColumns : null,
    };
  }
  if (page && page.id === "page-history") {
    var wrap2 = document.getElementById("gapsTableWrap");
    var rows2 = document.querySelectorAll("#gapsTbody tr.gap-row");
    out.gaps = {
      wrapVisible: wrap2 ? getComputedStyle(wrap2).display : null,
      wrapCardClass: wrap2 ? wrap2.classList.contains("mobile-card-table") : null,
      wrapRect: w(wrap2), table: w(wrap2 ? wrap2.querySelector("table") : null),
      rowCount: rows2.length,
      firstRowRect: rows2.length ? w(rows2[0]) : null,
      firstTdRects: rows2.length ? Array.prototype.map.call(rows2[0].querySelectorAll("td"), function(td){ return (td.dataset.label||"?")+"/"+Math.round(td.getBoundingClientRect().width); }) : [],
    };
  }
  out.viewport = window.innerWidth + "x" + window.innerHeight;
  return JSON.stringify(out, null, 1);
})()
