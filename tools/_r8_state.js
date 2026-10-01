(function(){
  var out = {};
  out.page = (document.querySelector(".page.active")||{}).id;
  out.readyState = document.readyState;
  out.LM = typeof window.LM;
  out.navCur = window.LM && LM.nav ? LM.nav.currentPage() : null;
  // 各页容器状态
  out.slotEmpty = (function(){ var e=document.getElementById("llmSlotEmpty"); return e ? e.textContent : null; })();
  out.slotList = (function(){ var e=document.getElementById("llmSlotList"); return e ? e.innerHTML.slice(0,120) : null; })();
  out.gpuProcHTML = (function(){ var e=document.getElementById("gpuProc"); return e ? e.innerHTML.slice(0,120) : null; })();
  out.coreHeatHTML = (function(){ var e=document.getElementById("sysCoreHeat"); return e ? e.innerHTML.slice(0,120) : null; })();
  out.gapsTbody = (function(){ var e=document.getElementById("gapsTbody"); return e ? e.children.length : null; })();
  out.gapsEmpty = (function(){ var e=document.getElementById("gapsEmpty"); return e ? e.hidden : null; })();
  return JSON.stringify(out, null, 1);
})()
