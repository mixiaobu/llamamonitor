(function(){
  var out = {};
  var doc = document.documentElement;
  out.hOverflow = doc.scrollWidth > doc.clientWidth ? (doc.scrollWidth - doc.clientWidth) + "px" : "none";
  out.w = doc.clientWidth;
  // SLOT: 应仍是 table 布局（td table-cell），逐列宽生效
  var slot = document.querySelector(".slot-table");
  if (slot) {
    var sr = slot.querySelector("tr.slot-row");
    out.slot = {
      tableDisplay: getComputedStyle(slot).display,
      tdDisplay: sr ? getComputedStyle(sr.querySelector("td")).display : null,
      widths: sr ? Array.prototype.map.call(sr.querySelectorAll("td"), function(td){ return Math.round(td.getBoundingClientRect().width); }) : [],
      wrapWidth: Math.round(slot.getBoundingClientRect().width)
    };
  }
  // GPU PROC: 桌面 td 仍 ellipsis 技巧（max-width 0 + width 100%? 实际是 tnum 等），表宽
  var gp = document.querySelector("#gpuProc .gpu-proc-table");
  if (gp) {
    var gr = gp.querySelector("tbody tr");
    out.gpu = {
      tableDisplay: getComputedStyle(gp).display,
      tdDisplay: gr ? getComputedStyle(gr.querySelector("td")).display : null,
      tdMaxW: gr ? getComputedStyle(gr.querySelector("td")).maxWidth : null,
      widths: gr ? Array.prototype.map.call(gr.querySelectorAll("td"), function(td){ return Math.round(td.getBoundingClientRect().width); }) : []
    };
  }
  // CORE-HEAT: 桌面 grid，cell 有 min-height
  var cells = document.querySelectorAll(".core-heat-grid .core-cell");
  if (cells.length) {
    var c0 = cells[0].getBoundingClientRect();
    out.core = { cell: Math.round(c0.width)+"x"+Math.round(c0.height), count: cells.length, track: getComputedStyle(document.querySelector(".core-heat-grid")).gridTemplateColumns.split(" ").length };
  }
  // GAP: 桌面 table，nth-child 列宽生效
  var g = document.querySelector("#gapsTbody tr.gap-row");
  if (g) {
    out.gap = {
      tdDisplay: getComputedStyle(g.querySelector("td")).display,
      widths: Array.prototype.map.call(g.querySelectorAll("td"), function(td){ return Math.round(td.getBoundingClientRect().width); }),
      wrapDisplay: getComputedStyle(document.getElementById("gapsTableWrap")).display
    };
  }
  return JSON.stringify(out, null, 1);
})()
