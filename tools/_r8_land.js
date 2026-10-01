(function(){
  var c=document.querySelector('.content');
  var mnav=document.querySelector('.mobile-nav');
  var sheetBtn=document.querySelector('.page-overflow');
  return JSON.stringify({
    vw:window.innerWidth, vh:window.innerHeight,
    mnav: mnav?{display:getComputedStyle(mnav).display,h:Math.round(mnav.getBoundingClientRect().height)}:null,
    sheetBtn: sheetBtn?{display:getComputedStyle(sheetBtn).display}:null,
    docHS: document.documentElement.scrollWidth>window.innerWidth+1,
    contHS: c? c.scrollWidth>c.clientWidth+1 : null,
    contSW: c?c.scrollWidth:0, contCW: c?c.clientWidth:0
  });
})()
