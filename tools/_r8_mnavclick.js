(function(){
  var out={page0:LM.nav.currentPage()};
  // 逐项点击底部导航
  var seq=[];
  ['usage','performance','gpu','system','overview'].forEach(function(p){
    var b=document.querySelector('.mnav-item[data-page="'+p+'"]');
    b.click();
    seq.push({clicked:p, now:LM.nav.currentPage(),
      aria: b.getAttribute('aria-current')==='page',
      pageVisible: document.getElementById('page-'+p).classList.contains('active')});
  });
  out.seq=seq;
  // 重复点击当前页 = 回顶
  var c=document.querySelector('.content');
  c.scrollTop=300;
  document.querySelector('.mnav-item[data-page="overview"]').click();
  out.reclickScrollTop=c.scrollTop;
  // 从 usage 经底栏回 overview 后再进 usage（滚动记忆）
  document.querySelector('.mnav-item[data-page="usage"]').click();
  c.scrollTop=150;
  document.querySelector('.mnav-item[data-page="overview"]').click();
  document.querySelector('.mnav-item[data-page="usage"]').click();
  out.scrollRestore=c.scrollTop;
  return JSON.stringify(out);
})()
