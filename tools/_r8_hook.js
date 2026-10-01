(function(){
  window.__errs = [];
  var ow = console.warn, oe = console.error;
  console.warn = function(){ window.__errs.push("WARN: " + Array.prototype.map.call(arguments, String).join(" ")); ow.apply(console, arguments); };
  console.error = function(){ window.__errs.push("ERR: " + Array.prototype.map.call(arguments, String).join(" ")); oe.apply(console, arguments); };
  // 重新拉一次
  var out = {};
  // GPU: 直接调用 refresh 内部？用 api
  LM.api.get("/api/gpu/status").then(function(d){
    out.gpuOk = !!d; out.gpuProcs = (d.processes||[]).length;
    // 手动触发 GPU 页 onShow 重渲染
    if (LM.nav.currentPage() === "gpu") {
      // 找渲染入口
    }
  }).catch(function(e){ out.gpuErr = e.message; });
  LM.api.get("/api/history/gaps?limit=20").then(function(d){
    out.gapsOk = !!d; out.gapCount = (d.gaps||[]).length;
  }).catch(function(e){ out.gapErr = e.message; });
  return "hooked";
})()
