(function(){
  var out = {};
  out.page = (document.querySelector(".page.active")||{}).id;
  // gpuProc box 内容
  var g = document.getElementById("gpuProc");
  out.gpuProc = g ? g.innerHTML.slice(0, 300) : "missing";
  // state
  out.stateKeys = Object.keys(window.LM.state || {});
  // 是否有渲染函数可手动触发
  out.hasLMapi = typeof LM.api.get;
  // 直接 fetch 测试
  return "init";
})()
