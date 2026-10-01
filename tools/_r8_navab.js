(function(){
  var mnav = document.querySelector(".mobile-nav");
  function topAt(){
    var r = mnav.getBoundingClientRect();
    var el = document.elementFromPoint(Math.round(r.left + r.width*0.18), Math.round(r.top + r.height*0.5));
    if(!el) return "none";
    var cls = (el.className && el.className.baseVal !== undefined ? el.className.baseVal : (el.className||"")).toString();
    return cls.slice(0,30) || el.tagName;
  }
  function pageOpacity(){ var a=document.querySelector('.page.active'); return a?getComputedStyle(a).opacity:null; }
  var out = {};
  // 记录 token 实际值
  out.zToken = getComputedStyle(document.documentElement).getPropertyValue("--z-mobile-nav").trim() || "(undefined)";
  out.navZComputed = getComputedStyle(mnav).zIndex;

  // Phase A（修复前行为）：强制 z:auto，切页，同步采样 fade 起点
  var cur = LM.nav.currentPage();
  var other = cur === "usage" ? "system" : "usage";
  mnav.style.zIndex = "auto";
  LM.nav.showPage(other);
  out.pre_fix = { top: topAt(), pageOpacity: pageOpacity() };

  // Phase B（修复后）：恢复 z:40，切回，同步采样 fade 起点
  mnav.style.zIndex = "40";
  LM.nav.showPage(cur);
  out.post_fix = { top: topAt(), pageOpacity: pageOpacity() };
  mnav.style.zIndex = ""; // 清理 inline

  // Phase C（修复后稳态，图表在底部）
  var c = document.querySelector(".content");
  c.scrollTop = c.scrollHeight * 0.6;
  out.post_fix_rest = { top: topAt() };
  return JSON.stringify(out, null, 1);
})()
