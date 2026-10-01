(function(){
  var out = {};
  out.vis = document.visibilityState;
  out.page = (document.querySelector(".page.active")||{}).id;
  // 手动 fetch /api/gpu/status，2s 后读结果
  out.fetch = (function(){
    return new Promise(function(res){
      var t = setTimeout(function(){ res("timeout"); }, 2500);
      fetch("/api/gpu/status", { cache: "no-store" }).then(function(r){
        clearTimeout(t);
        return r.json().then(function(d){ res("ok:" + (d.processes||[]).length); });
      }).catch(function(e){ clearTimeout(t); res("err:" + e.message); });
    });
  })();
  return out;
})()
