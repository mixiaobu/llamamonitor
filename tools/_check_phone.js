(function(){
  var out = { docW: document.documentElement.clientWidth };
  var content = document.querySelector(".content");
  function segInfo(id){
    var el = document.getElementById(id);
    if (!el) return { missing: true };
    var r = el.getBoundingClientRect();
    var cr = content.getBoundingClientRect();
    var btns = el.querySelectorAll("button").length;
    return { buttons: btns, right: Math.round(r.right), inView: r.right <= cr.right + 1 && btns > 0 };
  }
  LM.nav.showPage("gpu");
  out.gpuRange = segInfo("gpuRange");
  LM.nav.showPage("usage");
  out.usageRange = segInfo("usageRange");
  LM.nav.showPage("settings");
  var rail = document.querySelector('.rail-item[data-sec="application"]');
  if (rail) rail.click();
  var btn = document.getElementById("btnOpenData");
  if (btn) {
    var fc = btn.closest(".setting-control");
    var cr = content.getBoundingClientRect();
    var bs = Array.prototype.map.call(fc.querySelectorAll("button"), function(b){ var r = b.getBoundingClientRect(); return { w: Math.round(r.width), right: Math.round(r.right) }; });
    out.settingsFolder = { dir: getComputedStyle(fc).flexDirection, allIn: bs.every(function(b){ return b.right <= cr.right + 1; }) };
  }
  LM.nav.showPage("overview");
  out.theme = document.documentElement.getAttribute("data-theme");
  out.navItems = document.querySelectorAll(".nav-item").length;
  out.gpuCards = document.querySelectorAll("#ovGpuMini .gpu-mini").length;
  LM.nav.showPage("overview");
  return JSON.stringify(out);
})()