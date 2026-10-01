(function(){
  var out = {};
  out.page = (document.querySelector(".page.active")||{}).id;
  out.vis = document.visibilityState;
  var a = LM.poll.audit();
  out.tasks = Object.keys(a).map(function(k){
    var t = a[k];
    return k + "{iv:" + t.intervalMs + ",vis:" + t.visibleOnly + ",inFlight:" + t.inFlight + ",started:" + t.started + "}";
  });
  return JSON.stringify(out, null, 1);
})()
