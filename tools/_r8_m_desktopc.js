(function(){
  var out = {};
  var doc = document.documentElement;
  out.hOverflow = doc.scrollWidth > doc.clientWidth ? (doc.scrollWidth - doc.clientWidth) + "px" : "none";
  out.w = doc.clientWidth;
  // core-heat: 桌面 cell min-height 应 40px（pages.css），非 mobile 32px
  var d = document.querySelector("details.core-heat");
  if (d && !d.open) d.open = true;
  var grid = document.querySelector(".core-heat-grid");
  out.core = grid ? {
    cols: getComputedStyle(grid).gridTemplateColumns,
    colCount: getComputedStyle(grid).gridTemplateColumns.split(" ").length,
    cellMinH: (function(){ var c=document.querySelector(".core-heat-grid .core-cell"); return c?getComputedStyle(c).minHeight+" / height="+getComputedStyle(c).height : "no-cells"; })()
  } : "grid-absent";
  // gap table: 桌面 table-layout:fixed + nth-child 列宽 190/92/96/auto/120
  var gt = document.querySelector(".table-gap2");
  out.gap = gt ? {
    display: getComputedStyle(gt).display,
    layout: getComputedStyle(gt).tableLayout,
    thWidths: Array.prototype.map.call(gt.querySelectorAll("thead th"), function(th){ return getComputedStyle(th).width; })
  } : "gap-table-absent";
  // slot: 桌面 display:table
  var st = document.querySelector(".slot-table");
  out.slot = st ? { display: getComputedStyle(st).display } : "absent";
  // gpu proc: 桌面 display:table
  var gp = document.querySelector("#gpuProc .gpu-proc-table");
  out.gpu = gp ? { display: getComputedStyle(gp).display, tableLayout: getComputedStyle(gp).tableLayout } : "absent";
  return JSON.stringify(out, null, 1);
})()
