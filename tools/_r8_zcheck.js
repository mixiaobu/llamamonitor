(function(){
  var out = {};
  var mnav = document.querySelector(".mobile-nav");
  out.navZ = getComputedStyle(mnav).zIndex;
  out.tok = ["--z-sticky","--z-mobile-nav","--z-popover","--z-sheet-scrim","--z-sheet","--z-modal-scrim","--z-tooltip","--z-toast"]
    .map(function(t){ var v=getComputedStyle(document.documentElement).getPropertyValue(t).trim(); return t+"="+v; });
  var bad = [];
  document.querySelectorAll("*").forEach(function(el){
    var cs = getComputedStyle(el);
    if (cs.position !== "static" && cs.zIndex === "auto" && (el.className||"").toString().match(/toast|modal-overlay|sheet|popover|mnav|mobile-nav/)) {
      bad.push((el.className||el.tagName).toString().slice(0,28));
    }
  });
  out.suspectAuto = bad;
  var doc = document.documentElement;
  out.hOverflow = doc.scrollWidth > doc.clientWidth;
  return JSON.stringify(out, null, 1);
})()
