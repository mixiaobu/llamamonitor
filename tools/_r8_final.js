(function(){
  var bar = document.querySelector('.save-bar, [class*="save-bar"]');
  if(!bar) return JSON.stringify({err:'no save-bar'});
  var cs = getComputedStyle(bar);
  var r = bar.getBoundingClientRect();
  return JSON.stringify({
    display: cs.display,
    opacity: cs.opacity,
    visible: !bar.hidden && cs.display !== 'none' && r.height > 0,
    bottom: cs.position==='fixed' ? (window.innerHeight - r.bottom) + 'px from viewport bottom' : cs.position,
    height: Math.round(r.height),
    // 距 bottom-nav 顶
    mnav: (function(){ var m=document.querySelector('.mobile-nav'); if(!m) return null;
      return Math.round(m.getBoundingClientRect().top - r.bottom); })(),
    text: bar.textContent.trim().slice(0,40)
  });
})()
