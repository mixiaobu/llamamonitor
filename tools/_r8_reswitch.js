(function(){
  var out = { st: "pre" };
  try {
    // 诊断：gpu 页 onShow 直接调 refreshGpuStatus()；这里手动重进页触发一次
    LM.nav.showPage("overview");
    setTimeout(function(){ LM.nav.showPage("gpu"); }, 300);
    out.st = "reswitched";
  } catch(e) { out.st = "ERR " + e.message; }
  return out.st;
})()
