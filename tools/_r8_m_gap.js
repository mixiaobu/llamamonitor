(function(){
  var out = {};
  var doc = document.documentElement;
  out.hOverflow = doc.scrollWidth > doc.clientWidth ? (doc.scrollWidth - doc.clientWidth) + "px" : "none";
  var rows = document.querySelectorAll("#gapsTbody tr.gap-row");
  out.rows = rows.length;
  if (rows.length) {
    var tds = rows[0].querySelectorAll("td");
    out.tdRects = Array.prototype.map.call(tds, function(td){ var r=td.getBoundingClientRect(); return (td.dataset.label||"?")+"/"+Math.round(r.width); });
    out.rowDisplay = getComputedStyle(rows[0]).display;
    out.tdDisplay = getComputedStyle(tds[0]).display;
  }
  return JSON.stringify(out);
})()
