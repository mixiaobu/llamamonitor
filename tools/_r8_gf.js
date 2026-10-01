(function(){
  var out = {};
  // 手动 fetch + 调用内部渲染（通过 re-trigger onShow 不可行，直接 fetch 看网络层）
  var t0 = Date.now();
  window.__gpuFetch = { pending: true };
  fetch("/api/gpu/status").then(function(r){
    window.__gpuFetch.status = r.status;
    return r.json();
  }).then(function(d){
    window.__gpuFetch.pending = false;
    window.__gpuFetch.procs = (d.processes||[]).length;
    window.__gpuFetch.ok = !!d;
  }).catch(function(e){ window.__gpuFetch.pending = false; window.__gpuFetch.err = e.message; });
  return "fetching";
})()
