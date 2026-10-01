(function(){
  var out={vw:window.innerWidth, vh:window.innerHeight};
  var content=document.querySelector('.content'), inner=document.querySelector('.content-inner');
  function g(el){ if(!el) return null; var r=el.getBoundingClientRect(), s=getComputedStyle(el);
    return {id:el.id, cls:(el.className||'').toString().slice(0,46), x:Math.round(r.x), w:Math.round(r.width),
      pl:parseFloat(s.paddingLeft), pr:parseFloat(s.paddingRight), ml:parseFloat(s.marginLeft), mr:parseFloat(s.marginRight),
      ow:s.overflowWrap, ws:s.whiteSpace, dmin:s.display}; }
  out.content=g(content); out.inner=g(inner);
  // 溢出元素（右缘超视口）
  var over=[];
  var active=document.querySelector('.page.active')||document.body;
  active.querySelectorAll('*').forEach(function(e){
    var r=e.getBoundingClientRect();
    if(r.right>window.innerWidth+1.5&&r.width>0){
      var st=getComputedStyle(e);
      over.push({id:e.id||'',cls:(e.className||'').toString().slice(0,44),w:Math.round(r.width),r:Math.round(r.right),
        scr:(st.overflowX==='auto'||st.overflowX==='scroll')?1:0,tag:e.tagName.toLowerCase()});
    }
  });
  out.overCount=over.length; out.over=over.slice(0,14);
  // 特定元素链
  function chain(id){ var e=document.getElementById(id); if(!e) return null; var c=[]; var cur=e;
    for(var i=0;i<7&&cur;i++){ var r=cur.getBoundingClientRect();
      c.push({t:cur.tagName.toLowerCase(),c:(cur.className||'').toString().slice(0,34),x:Math.round(r.x),w:Math.round(r.width),
        ws:getComputedStyle(cur).whiteSpace,ow:getComputedStyle(cur).overflowWrap,minw:getComputedStyle(cur).minWidth,
        pl:parseFloat(getComputedStyle(cur).paddingLeft)}); cur=cur.parentElement; } return c; }
  out.sysNetW=chain('sysNetW');
  out.usageTrendHint=chain('usageTrendHint');
  // gpu sh-actions / section header
  out.gpuSh=(function(){ var h=document.querySelector('#page-gpu .section-header'); if(!h) return null;
    var kids=[]; h.querySelectorAll('*').forEach(function(e){ var r=e.getBoundingClientRect(); if(r.width>0)
      kids.push({t:e.tagName.toLowerCase(),c:(e.className||'').toString().slice(0,30),x:Math.round(r.x),w:Math.round(r.width)});});
    return {parent:g(h), kids:kids.slice(0,12)}; })();
  // gpu check chips
  out.gpuChips=(function(){ var chips=[]; document.querySelectorAll('#page-gpu .check-chip').forEach(function(e){ var r=e.getBoundingClientRect();
    chips.push({x:Math.round(r.x),w:Math.round(r.width),txt:(e.textContent||'').trim().slice(0,24)});});
    var wrap=document.querySelector('#page-gpu .check-chip')?document.querySelector('#page-gpu .check-chip').closest('.sh-actions,.card,.section,div') : null;
    return {count:chips.length, chips:chips.slice(0,8),
      wrap: wrap?{cls:(wrap.className||'').toString().slice(0,40),w:Math.round(wrap.getBoundingClientRect().width),ox:getComputedStyle(wrap).overflowX,fd:getComputedStyle(wrap).flexDirection,fw:getComputedStyle(wrap).flexWrap}:null}; })();
  // settings rail
  out.rail=(function(){ var r=document.querySelector('.settings-rail'); if(!r) return null; var rr=r.getBoundingClientRect();
    return {w:Math.round(rr.width),x:Math.round(rr.x),ox:getComputedStyle(r).overflowX,fw:getComputedStyle(r).flexWrap,h:Math.round(rr.height),
      items:(function(){var b=r.querySelector('.rail-item');return b?{w:Math.round(b.getBoundingClientRect().width),h:Math.round(b.getBoundingClientRect().height)}:null;})()}; })();
  // usage sh-actions
  out.usageActions=(function(){ var a=document.querySelector('#page-usage .sh-actions'); if(!a) return null;
    var s=getComputedStyle(a), r=a.getBoundingClientRect();
    return {x:Math.round(r.x),w:Math.round(r.width),ml:s.marginLeft,dmin:s.display}; })();
  return JSON.stringify(out);
})()
