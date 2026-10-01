(async function(){
  var out = {};
  var page = document.querySelector(".page.active");
  out.page = page ? page.id : null;
  window.__warns = [];
  var ow = console.warn;
  console.warn = function(){ window.__warns.push(Array.prototype.map.call(arguments, String).join(" ").slice(0,120)); ow.apply(console, arguments); };
  var deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    var g = document.getElementById("gpuProc");
    if (g && g.querySelector("table, .gpu-proc-empty")) break;
    var gt = document.getElementById("gapsTbody");
    if (gt && gt.children.length) break;
    await new Promise(function(r){ setTimeout(r, 800); });
  }
  var g = document.getElementById("gpuProc");
  out.gpu = g ? { len: g.innerHTML.length, head: g.innerHTML.slice(0,150), rows: g.querySelectorAll("tbody tr").length } : "missing";
  var gt = document.getElementById("gapsTbody");
  out.gaps = { rows: gt ? gt.children.length : null, wrapDisplay: (document.getElementById("gapsTableWrap")||{}).style ? document.getElementById("gapsTableWrap").style.display : null };
  out.warns = window.__warns.slice(0, 8);
  return JSON.stringify(out, null, 1);
})()
