(function(){
  function dumpActions(sel){
    var a=document.querySelector(sel); if(!a) return null;
    var ar=a.getBoundingClientRect();
    var kids=[];
    a.querySelectorAll(':scope > *').forEach(function(e){
      var r=e.getBoundingClientRect(); var s=getComputedStyle(e);
      kids.push({t:e.tagName.toLowerCase(), c:(e.className||'').toString().slice(0,30),
        x:Math.round(r.x), w:Math.round(r.width), right:Math.round(r.right),
        fd:s.flexDirection, fw:s.flexWrap, ox:s.overflowX, minw:s.minWidth, fl:s.flex});
    });
    return {x:Math.round(ar.x), w:Math.round(ar.width), right:Math.round(ar.right), kids:kids};
  }
  return JSON.stringify({vw:window.innerWidth,
    gpuActions: dumpActions('#page-gpu .sh-actions'),
    usageActions: dumpActions('#page-usage .sh-actions'),
    hostDual: (function(){
      var e=document.querySelector('.host-dual'); if(!e) return null;
      var kids=[]; e.querySelectorAll('*').forEach(function(x){ if(x.children.length===0){
        var r=x.getBoundingClientRect(); kids.push({c:(x.className||'').toString().slice(0,20), w:Math.round(r.width), txt:(x.textContent||'').trim().slice(0,30)});}});
      var s=getComputedStyle(e);
      return {fd:s.flexDirection, fw:s.flexWrap, gap:s.gap, kids:kids.slice(0,8)};
    })()
  });
})()
