(function(){
  // core-heat 默认折叠（mobile）：展开后再测
  var d = document.querySelector("details.core-heat");
  if (d && !d.open) d.open = true;
  return (document.querySelectorAll(".core-heat-grid .core-cell").length || 0);
})()
