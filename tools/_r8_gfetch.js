(function(){
  var out = {};
  out.page = (document.querySelector(".page.active")||{}).id;
  out.st = (function(){ try { return JSON.stringify(window.__dbg_st); } catch(e){ return null; } })();
  // 手动拉一次 gpu status 并渲染
  LM.api.get("/api/gpu/status").then(function(d){
    window.__dbg_gpu = { ok: !!d, procs: (d&&d.processes||[]).length };
    // 触发渲染路径（app 内部函数不暴露；用 state + 重进页）
  }).catch(function(e){ window.__dbg_gpu = { err: e.message }; });
  return "fetching";
})()
