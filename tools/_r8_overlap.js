(function(){
  var mnav = document.querySelector(".mobile-nav");
  var nr = mnav.getBoundingClientRect();
  // 滚动让图表靠近底部
  var c = document.querySelector(".content");
  c.scrollTop = c.scrollHeight * 0.55;
  // 找与 nav 矩形相交的元素
  var hits = [];
  document.querySelectorAll(".content *").forEach(function(el){
    var r = el.getBoundingClientRect();
    if (r.height > 4 && r.bottom > nr.top + 2 && r.top < nr.bottom && r.right > nr.left && r.left < nr.right) {
      var cs = getComputedStyle(el);
      if (cs.position !== "static") {
        hits.push({cls:(el.className||el.id||el.tagName).toString().slice(0,36), pos:cs.position, z:cs.zIndex, bottom:Math.round(r.bottom)});
      }
    }
  });
  hits.sort(function(a,b){return b.bottom-a.bottom;});
  return JSON.stringify({navTop:Math.round(nr.top), navH:Math.round(nr.height), scrollY:Math.round(c.scrollTop), maxContentBottom:Math.round(c.scrollHeight - c.scrollTop + c.getBoundingClientRect().top), hits: hits.slice(0,10)});
})()
