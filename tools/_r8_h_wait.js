(function(){
  var out = {};
  var page = document.querySelector(".page.active");
  out.page = page ? page.id : null;
  var rows = document.querySelectorAll("#gapsTbody tr.gap-row");
  out.rowCount = rows.length;
  if (rows.length) {
    function wd(el){ var r=el.getBoundingClientRect(); return Math.round(r.width)+"x"+Math.round(r.height); }
    out.firstRow = wd(rows[0]);
    out.tdRects = Array.prototype.map.call(rows[0].querySelectorAll("td"), function(td){ return (td.dataset.label||"?")+"/"+Math.round(td.getBoundingClientRect().width); });
    out.tdDisplays = Array.prototype.map.call(rows[0].querySelectorAll("td"), function(td){ return getComputedStyle(td).display; });
    // 卡头（首列）宽度是否撑满
    out.headerWidth = Math.round(rows[0].querySelector("td").getBoundingClientRect().width);
    out.rowWidth = Math.round(rows[0].getBoundingClientRect().width);
  }
  return JSON.stringify(out, null, 1);
})()
