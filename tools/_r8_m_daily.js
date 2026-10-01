(function(){
  var out = {};
  var doc = document.documentElement;
  out.hOverflow = doc.scrollWidth > doc.clientWidth ? (doc.scrollWidth - doc.clientWidth) + "px" : "none";
  var rows = document.querySelectorAll("table.table-daily tbody tr");
  out.rows = rows.length;
  if (rows.length) {
    var tds = rows[0].querySelectorAll("td");
    out.rowW = Math.round(rows[0].getBoundingClientRect().width);
    out.tdRects = Array.prototype.map.call(tds, function(td){ var r=td.getBoundingClientRect(); return Math.round(r.width); });
    out.rowDisplay = getComputedStyle(rows[0]).display;
    out.tdDisplay = getComputedStyle(tds[0]).display;
    // 卡内是否有横向溢出
    out.rowOverflowX = rows[0].scrollWidth > rows[0].clientWidth + 1 ? (rows[0].scrollWidth - rows[0].clientWidth) + "px" : "none";
  }
  return JSON.stringify(out);
})()
