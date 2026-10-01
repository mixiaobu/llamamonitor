(function(){
  // 切到"全部"范围让 >31 天 dataZoom 生效
  var seg = document.querySelector('#usageRange button');
  var btns = document.querySelectorAll('#usageRange button');
  var target = null;
  btns.forEach(function(b){ if((b.textContent||'').indexOf('全部')>-1) target=b; });
  if (target) target.click();
  return 'clicked:'+(target?target.textContent:'none');
})()
