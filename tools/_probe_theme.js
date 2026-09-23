(function(){
  var out = {};
  try {
    // ---- 主题解析（同步）----
    out.dataTheme = document.documentElement.getAttribute("data-theme");
    out.matchDark = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)").matches : null;
    out.matchLight = window.matchMedia ? window.matchMedia("(prefers-color-scheme: light)").matches : null;
    if (LM.app && LM.app.applyTheme) {
      LM.app.applyTheme("dark");  out.applyDark = document.documentElement.getAttribute("data-theme");
      LM.app.applyTheme("light"); out.applyLight = document.documentElement.getAttribute("data-theme");
      LM.app.applyTheme("system");out.applySystem = document.documentElement.getAttribute("data-theme");
    }
    // ---- 轮询：in-flight 不重叠 + 任务数 ----
    if (LM.poll && LM.poll.audit) {
      var a = LM.poll.audit();
      out.pollTasks = Object.keys(a).length;
      var inflight=[]; Object.keys(a).forEach(function(k){ if(a[k].inFlight) inflight.push(k); });
      out.pollInFlight = inflight;
    }
    // ---- 布局：无横向溢出 ----
    out.overflowX = document.documentElement.scrollWidth > document.documentElement.clientWidth + 1;
    // ---- 页面切换压力 200 次（同步）+ 图表实例稳定（无泄漏）----
    var pages = ["overview","usage","performance","gpu","history","settings","about"];
    var ic = function(){ return (LM.charts && LM.charts.instanceCount) ? LM.charts.instanceCount() : null; };
    var mem = function(){ return (performance && performance.memory) ? performance.memory.usedJSHeapSize : null; };
    var instBefore = ic(), memBefore = mem();
    var ok = true, t0 = Date.now();
    for (var i=0;i<60;i++){ var n=pages[i%pages.length]; LM.nav.showPage(n); if(LM.nav.currentPage()!==n) ok=false; }
    var swMs = Date.now()-t0;
    LM.nav.showPage("overview");
    var instMid = ic(), memMid = mem();
    // 再切 60 次，观察实例数是否稳定（不随切换次数增长 = 无泄漏；>100 次总切换）
    for (var j=0;j<60;j++){ var m=pages[j%pages.length]; LM.nav.showPage(m); }
    LM.nav.showPage("overview");
    var instFinal = ic(), memFinal = mem();
    out.switchAllOk = ok; out.switchMs = swMs;
    out.instBefore = instBefore; out.instMid = instMid; out.instFinal = instFinal;
    out.instGrowthFinalVsMid = (instMid!=null&&instFinal!=null)?(instFinal-instMid):null;
    out.memBeforeKB = memBefore?Math.round(memBefore/1024):null;
    out.memFinalKB  = memFinal?Math.round(memFinal/1024):null;
    out.navItems = document.querySelectorAll(".nav-item").length;
    out.pages = document.querySelectorAll(".page").length;
  } catch(e) { out.error = (e && e.message) || String(e); }
  return JSON.stringify(out);
})()
