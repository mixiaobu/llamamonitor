(function(){
  window.__warns = window.__warns || [];
  var ow = console.warn, oe = console.error;
  console.warn = function(){ window.__warns.push("W:" + Array.prototype.map.call(arguments, String).join(" ").slice(0,150)); ow.apply(console, arguments); };
  console.error = function(){ window.__warns.push("E:" + Array.prototype.map.call(arguments, String).join(" ").slice(0,150)); oe.apply(console, arguments); };
  // 重进 gpu 页触发 onShow 刷新
  var cur = LM.nav.currentPage();
  if (cur === "gpu") { LM.nav.showPage("overview"); LM.nav.showPage("gpu"); }
  else LM.nav.showPage("gpu");
  return "hooked";
})()
