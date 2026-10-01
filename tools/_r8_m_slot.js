(function(){
  var out = {};
  var doc = document.documentElement;
  out.hOverflow = doc.scrollWidth > doc.clientWidth ? (doc.scrollWidth - doc.clientWidth) + "px" : "none";
  out.w = doc.clientWidth;
  var rows = document.querySelectorAll(".slot-table tr.slot-row");
  out.slotRows = rows.length;
  if (rows.length) {
    var tbl = document.querySelector(".slot-table");
    out.tblW = Math.round(tbl.getBoundingClientRect().width);
    out.rowW = Math.round(rows[0].getBoundingClientRect().width);
    var ws = Array.prototype.map.call(rows[0].querySelectorAll("td"), function(td){ return Math.round(td.getBoundingClientRect().width); });
    out.minTdW = Math.min.apply(null, ws);
    // 第一个 td 是标题行（block），其余是 flex 行
    out.tdDisplay0 = getComputedStyle(rows[0].querySelector("td")).display;
    var t1 = rows[0].querySelectorAll("td")[1];
    out.tdDisplay1 = t1 ? getComputedStyle(t1).display : null;
  }
  return JSON.stringify(out);
})()
