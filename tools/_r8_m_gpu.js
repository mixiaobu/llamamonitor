(function(){
  var out = {};
  var doc = document.documentElement;
  out.hOverflow = doc.scrollWidth > doc.clientWidth ? (doc.scrollWidth - doc.clientWidth) + "px" : "none";
  var rws = document.querySelectorAll("#gpuProc .gpu-proc-table tbody tr");
  out.rows = rws.length;
  if (rws.length) {
    var tds = rws[0].querySelectorAll("td");
    out.appW = Math.round(tds[0].getBoundingClientRect().width);
    out.pidW = Math.round(tds[1].getBoundingClientRect().width);
    out.gpuW = tds[2] ? Math.round(tds[2].getBoundingClientRect().width) : null;
    out.memW = tds[3] ? Math.round(tds[3].getBoundingClientRect().width) : null;
    var a = tds[1].getBoundingClientRect(), b = tds[2].getBoundingClientRect();
    out.overlap = a.right > b.left + 0.5 ? Math.round(a.right - b.left) + "px" : "none";
    out.rowH = Math.round(rws[0].getBoundingClientRect().height);
  }
  return JSON.stringify(out);
})()
