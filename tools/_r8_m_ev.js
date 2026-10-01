(function(){
  var out = {};
  var doc = document.documentElement;
  out.hOverflow = doc.scrollWidth > doc.clientWidth ? (doc.scrollWidth - doc.clientWidth) + "px" : "none";
  var rows = document.querySelectorAll("#eventsTbody tr");
  out.rows = rows.length;
  if (rows.length) {
    var tds = rows[0].querySelectorAll("td");
    out.tdRects = Array.prototype.map.call(tds, function(td){ var r=td.getBoundingClientRect(); return Math.round(r.width); });
    out.rowW = Math.round(rows[0].getBoundingClientRect().width);
    out.tdDisplay = getComputedStyle(tds[0]).display;
    out.rowDisplay = getComputedStyle(rows[0]).display;
  }
  return JSON.stringify(out);
})()
