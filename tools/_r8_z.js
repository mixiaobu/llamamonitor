(function(){
  var rs = getComputedStyle(document.documentElement);
  var toks = ["--z-mobile-nav","--z-sticky","--z-sheet","--z-sheet-scrim","--z-toast","--z-modal-scrim","--z-tooltip"];
  var out = {};
  toks.forEach(function(t){ out[t] = rs.getPropertyValue(t).trim() || "(undefined)"; });
  var mnav = document.querySelector(".mobile-nav");
  out.mnav_zindex_computed = getComputedStyle(mnav).zIndex;
  var content = document.querySelector(".content");
  out.content_position = getComputedStyle(content).position;
  out.content_zindex = getComputedStyle(content).zIndex;
  // 内容里所有 z-index 非 auto 的定位元素（会参与堆叠的）
  var hi = [];
  content.querySelectorAll('*').forEach(function(el){
    var cs = getComputedStyle(el);
    if (cs.position !== 'static' && cs.zIndex !== 'auto') {
      hi.push({cls:(el.className||'').toString().slice(0,30), pos:cs.position, z:cs.zIndex});
    }
  });
  out.contentPositioned = hi.slice(0,15);
  // .app 的 grid 子项
  out.appChildren = Array.prototype.map.call(document.querySelector('.app').children, function(c){
    return (c.className||'').toString().slice(0,20)+':'+getComputedStyle(c).zIndex;
  });
  return JSON.stringify(out, null, 1);
})()
