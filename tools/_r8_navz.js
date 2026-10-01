(function(){
  var out = {};
  var mnav = document.querySelector(".mobile-nav");
  var nr = mnav.getBoundingClientRect();
  // 采样点：nav 条中部 + 左侧第一项中心（点 "用量"）
  function pt(fx){ return [Math.round(nr.left + nr.width*fx), Math.round(nr.top + nr.height*0.5)]; }
  function topAt(fx){
    var p = pt(fx);
    var el = document.elementFromPoint(p[0], p[1]);
    if(!el) return "none";
    var path = [];
    var cur = el;
    for(var i=0;i<4 && cur;i++){ path.push((cur.className||cur.tagName).toString().slice(0,24)); cur = cur.parentElement; }
    return path.join(" > ");
  }
  // 1) 滚到图表/表格接近底部（system 页末段是图表）
  var c = document.querySelector(".content");
  c.scrollTop = c.scrollHeight * 0.6;
  out.rest = { navZ: getComputedStyle(mnav).zIndex, topCenter: topAt(0.5), topLeft: topAt(0.1) };

  // 2) 模拟修复前：nav z-index: auto -> 看谁盖住
  var oldZ = mnav.style.zIndex;
  mnav.style.zIndex = "auto";
  out.before_fix = { topCenter: topAt(0.5) };
  // 3) 切页瞬间（fade 中）：再切一次页，立刻采样
  LM.nav.showPage("usage");
  out.during_fade = { topCenter: topAt(0.5), pageOpacity: (function(){ var a=document.querySelector('.page.active'); return a?getComputedStyle(a).opacity:null; })() };
  mnav.style.zIndex = oldZ;
  return JSON.stringify(out, null, 1);
})()
