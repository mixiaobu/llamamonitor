(function(){
  function gs(sel, prop){ var el=document.querySelector(sel); if(!el) return null; var cs=getComputedStyle(el); return cs[prop]; }
  function px(v){ var n=parseFloat(v); return n; }
  var out={};
  // Bottom Nav
  var mnav=document.querySelector('.mobile-nav');
  if(mnav){
    var r=mnav.getBoundingClientRect();
    out.mnav={h:Math.round(r.height), w:Math.round(r.width), bottom:Math.round(window.innerHeight-r.bottom)};
    var item=mnav.querySelector('.mnav-item');
    if(item){ var ir=item.getBoundingClientRect(); out.mnavItem={w:Math.round(ir.width),h:Math.round(ir.height)};
      var ic=item.querySelector('svg,.mnav-icon,span[aria-hidden]');
      if(ic){ var icr=ic.getBoundingClientRect(); out.mnavIcon={w:Math.round(icr.width),h:Math.round(icr.height)}; }
      var tx=item.querySelector('.mnav-label,span');
      if(tx) out.mnavLabelFs=px(getComputedStyle(tx).fontSize);
    }
  }
  // 页头标题
  var pt=document.querySelector('.page-header .ph-title, .ph-title');
  if(pt) out.pageTitle={fs:px(getComputedStyle(pt).fontSize), fw:getComputedStyle(pt).fontWeight};
  // Card
  var card=document.querySelector('.card');
  if(card){ var cs=getComputedStyle(card); out.card={radius:cs.borderRadius, pad:cs.padding}; }
  // 关键数字（overview 主指标）
  var hero=document.querySelector('.stat-hero .ui-value, .overview-hero .ui-value, .ui-value');
  if(hero) out.heroNum={fs:px(getComputedStyle(hero).fontSize), text:hero.textContent.slice(0,20)};
  // 次级指标
  var mv=document.querySelector('.metric .ui-value, .stat .ui-value');
  if(mv) out.metricNum=px(getComputedStyle(mv).fontSize);
  // 底部留白：content 末尾到 nav 顶
  var c=document.querySelector('.content');
  if(c&&mnav){ out.gapBeforeNav=Math.round(mnav.getBoundingClientRect().top - c.getBoundingClientRect().bottom); }
  // chart 高度
  var ch=document.querySelector('.chart');
  if(ch) out.chartH=Math.round(ch.getBoundingClientRect().height);
  // section 间距
  var secs=document.querySelectorAll('.section');
  if(secs.length>1){ var a=secs[0].getBoundingClientRect(), b=secs[1].getBoundingClientRect(); out.sectionGap=Math.round(b.top-a.bottom); }
  // 输入框字号（settings 页）
  var inp=document.querySelector('input[type="text"],input[type="number"],input:not([type])');
  if(inp) out.inputFs=px(getComputedStyle(inp).fontSize);
  // 按钮高度
  var btn=document.querySelector('.btn');
  if(btn) out.btnH=Math.round(btn.getBoundingClientRect().height);
  // tabular-nums 抽查
  if(hero) out.heroNumericFont=getComputedStyle(hero).fontVariantNumeric;
  return JSON.stringify(out);
})()
