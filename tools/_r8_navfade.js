(async function(){
  var mnav = document.querySelector(".mobile-nav");
  function nr(){ return mnav.getBoundingClientRect(); }
  function topAt(fx){
    var r = nr();
    var el = document.elementFromPoint(Math.round(r.left + r.width*fx), Math.round(r.top + r.height*0.5));
    if(!el) return "none";
    return (el.className||el.tagName).toString().slice(0,30);
  }
  function nextFrame(){ return new Promise(function(res){ requestAnimationFrame(function(){ requestAnimationFrame(res); }); }); }
  var out = { navZ: getComputedStyle(mnav).zIndex, samples: [] };
  var target = LM.nav.currentPage() === "usage" ? "system" : "usage";
  LM.nav.showPage(target);
  for (var i=0; i<8; i++) {
    out.samples.push({ i: i, top: topAt(0.18), overNav: topAt(0.18).indexOf("mnav") === -1 });
    await nextFrame();
  }
  window.__navFadeResult = JSON.stringify(out);
  return "captured";
})()
