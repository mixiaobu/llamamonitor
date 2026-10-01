(function(){
  var c = document.querySelector('.content');
  var cs = getComputedStyle(c);
  var page = document.querySelector('#page-settings');
  var pcs = page ? getComputedStyle(page) : null;
  var rail = document.querySelector('.settings-rail');
  var rcs = getComputedStyle(rail);
  return JSON.stringify({
    contentClientW: c.clientWidth, contentScrollW: c.scrollWidth,
    contentPadL: cs.paddingLeft, contentPadR: cs.paddingRight,
    pagePadL: pcs?pcs.paddingLeft:null, pagePadR: pcs?pcs.paddingRight:null,
    railMarginL: rcs.marginLeft, railMarginR: rcs.marginRight,
    railPadL: rcs.paddingLeft, railPadR: rcs.paddingRight,
    pagePadVar: getComputedStyle(document.documentElement).getPropertyValue('--page-pad').trim(),
    railW: rail.getBoundingClientRect().width,
  });
})()
