(function(){
  var out = {};
  var doc = document.documentElement;
  out.hOverflow = doc.scrollWidth > doc.clientWidth ? (doc.scrollWidth - doc.clientWidth) + "px" : "none";
  var cells = document.querySelectorAll(".core-heat-grid .core-cell");
  out.cells = cells.length;
  if (cells.length) {
    var c0 = cells[0].getBoundingClientRect();
    out.cell0 = Math.round(c0.width) + "x" + Math.round(c0.height);
    // 相邻格重叠检测
    var overlap = 0;
    for (var i = 1; i < Math.min(cells.length, 20); i++) {
      var a = cells[i-1].getBoundingClientRect(), b = cells[i].getBoundingClientRect();
      // 同行相邻：x 轴有交且 y 轴大量重叠才算重叠
      if (a.right > b.left + 0.5 && Math.abs(a.top - b.top) < 5) {
        var o = a.right - b.left;
        if (o > overlap) overlap = o;
      }
    }
    out.overlap = overlap > 0 ? Math.round(overlap) + "px" : "none";
    // 文本是否溢出格子（pct span 的 scrollWidth > clientWidth）
    var pct = cells[0].querySelector("span:last-child");
    if (pct) out.textFits = pct.scrollWidth <= pct.clientWidth + 1 ? "fits" : "overflow " + (pct.scrollWidth - pct.clientWidth) + "px";
    // 网格列宽
    var grid = document.querySelector(".core-heat-grid");
    var cols = getComputedStyle(grid).gridTemplateColumns.split(" ");
    out.trackW = cols.length ? Math.round(parseFloat(cols[0])) + "px (" + cols.length + " cols)" : "?";
  }
  return JSON.stringify(out);
})()
