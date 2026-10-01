(function(){
  var out = {};
  var doc = document.documentElement;
  out.hOverflow = doc.scrollWidth > doc.clientWidth ? (doc.scrollWidth - doc.clientWidth) + "px" : "none";
  out.w = doc.clientWidth;
  // 各容器是否渲染 + 关键 CSS 驱动布局量（不依赖数据行）
  var slot = document.querySelector(".slot-table");
  out.slot = slot ? { w: Math.round(slot.getBoundingClientRect().width), display: getComputedStyle(slot).display, minW: getComputedStyle(slot).minWidth } : "absent";
  var gp = document.querySelector("#gpuProc .gpu-proc-table");
  out.gpu = gp ? { w: Math.round(gp.getBoundingClientRect().width), display: getComputedStyle(gp).display, minW: getComputedStyle(gp).minWidth } : "absent";
  // core-heat 网格（折叠状态下 .core-heat-grid 可能未渲染；展开后测轨道）
  var d = document.querySelector("details.core-heat");
  if (d && !d.open) d.open = true;
  var grid = document.querySelector(".core-heat-grid");
  out.core = grid ? { cols: getComputedStyle(grid).gridTemplateColumns, trackW: (function(){ var c=getComputedStyle(grid).gridTemplateColumns.split(" "); return c.length?Math.round(parseFloat(c[0])):0; })() } : "absent";
  var gapsWrap = document.getElementById("gapsTableWrap");
  out.gapWrap = gapsWrap ? { display: getComputedStyle(gapsWrap).display, w: Math.round(gapsWrap.getBoundingClientRect().width), minW: getComputedStyle(gapsWrap).minWidth } : "absent";
  var gapTable = document.querySelector(".table-gap2");
  out.gapTable = gapTable ? { minW: getComputedStyle(gapTable).minWidth, layout: getComputedStyle(gapTable).tableLayout } : "absent";
  return JSON.stringify(out, null, 1);
})()
