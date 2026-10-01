(function(){
  var page = document.getElementById('page-overview');
  var big = [];
  Array.prototype.slice.call(page.querySelectorAll('.stat-label')).forEach(function(v){
    if(getComputedStyle(v).fontSize === '15px') big.push({t:v.textContent.trim().slice(0,20), cls:(v.parentElement.className||'').toString().slice(0,30)});
  });
  return JSON.stringify(big);
})()
