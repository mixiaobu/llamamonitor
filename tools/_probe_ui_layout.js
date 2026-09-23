(function(){
  var out = {};
  out.dataTheme = document.documentElement.getAttribute("data-theme");
  out.viewportW = window.innerWidth; out.viewportH = window.innerHeight;
  out.dpr = window.devicePixelRatio;
  out.docScrollW = document.documentElement.scrollWidth;
  out.docClientW = document.documentElement.clientWidth;
  out.overflowX = document.documentElement.scrollWidth > document.documentElement.clientWidth + 1;
  out.navItems = document.querySelectorAll(".nav-item").length;
  var setBtn = document.querySelector('.nav-item[data-page="settings"]');
  out.settingsNavVisible = setBtn ? (getComputedStyle(setBtn).display !== "none") : null;
  out.pageCount = document.querySelectorAll(".page").length;
  out.charts = (LM.charts && LM.charts.instanceCount) ? LM.charts.instanceCount() : null;
  // 每个页面切一遍，确认无报错 + 无横向溢出
  var pages = ["overview","usage","performance","gpu","history","settings","about"];
  var maxOverflow = false;
  pages.forEach(function(p){
    LM.nav.showPage(p);
    if (document.documentElement.scrollWidth > document.documentElement.clientWidth + 1) maxOverflow = true;
  });
  LM.nav.showPage("overview");
  out.anyPageOverflowX = maxOverflow;
  return JSON.stringify(out);
})()
