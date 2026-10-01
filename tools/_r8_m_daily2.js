(function(){
  var out = {};
  var tbl = document.querySelector("table.table-daily");
  out.tbl = tbl ? { display: getComputedStyle(tbl).display, w: Math.round(tbl.getBoundingClientRect().width), visible: tbl.offsetParent !== null } : "absent";
  var wrap = tbl ? tbl.closest(".table-wrap") : null;
  out.wrap = wrap ? { w: Math.round(wrap.getBoundingClientRect().width), display: getComputedStyle(wrap).display, cls: wrap.className } : null;
  var rows = document.querySelectorAll("table.table-daily tbody tr");
  out.rows = [];
  Array.prototype.slice.call(rows, 0, 3).forEach(function(r){
    var tds = r.querySelectorAll("td");
    out.rows.push({
      w: Math.round(r.getBoundingClientRect().width),
      offsetParent: r.offsetParent !== null,
      hidden: r.hidden,
      cls: r.className,
      tdN: tds.length,
      tdW: Array.prototype.map.call(tds, function(td){ return Math.round(td.getBoundingClientRect().width); })
    });
  });
  return JSON.stringify(out, null, 1);
})()
