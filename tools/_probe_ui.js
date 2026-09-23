(function(){
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function yieldLoop(){ return new Promise(function(r){ setTimeout(r, 0); }); }
  var out = {};

  // ---- 1) 主题解析（同步，立即）----
  out.dataTheme = document.documentElement.getAttribute("data-theme");
  out.matchDark = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)").matches : null;
  out.matchLight = window.matchMedia ? window.matchMedia("(prefers-color-scheme: light)").matches : null;
  if (LM.app && LM.app.applyTheme) {
    LM.app.applyTheme("dark");  out.applyDark = document.documentElement.getAttribute("data-theme");
    LM.app.applyTheme("light"); out.applyLight = document.documentElement.getAttribute("data-theme");
    LM.app.applyTheme("system");out.applySystem = document.documentElement.getAttribute("data-theme");
  }

  // ---- 2) 轮询：in-flight 不重叠 + 任务数 ----
  if (LM.poll && LM.poll.audit) {
    var a = LM.poll.audit();
    out.pollTasks = Object.keys(a).length;
    var inflight = [];
    Object.keys(a).forEach(function(k){ if (a[k].inFlight) inflight.push(k); });
    out.pollInFlightCount = inflight.length;
    out.pollInFlight = inflight.slice(0,8);
  }

  // ---- 3) 布局：无横向溢出 ----
  out.overflowX = document.documentElement.scrollWidth > document.documentElement.clientWidth + 1;

  // ---- 4) 页面切换压力（~150 次，异步分段 yield 事件循环）+ 图表实例/内存无泄漏 ----
  var pages = ["overview","usage","performance","gpu","history","settings","about"];
  var instBefore = (LM.charts && LM.charts.instanceCount) ? LM.charts.instanceCount() : null;
  var memBefore  = (performance && performance.memory) ? performance.memory.usedJSHeapSize : null;
  var ok = true;
  var t0 = Date.now();
  function switchBatch(n){
    return new Promise(function(resolve){
      setTimeout(function(){
        for (var i = 0; i < n; i++) {
          var name = pages[(i) % pages.length];
          LM.nav.showPage(name);
          if (LM.nav.currentPage() !== name) { ok = false; }
        }
        resolve();
      }, 0);
    });
  }
  var P = Promise.resolve();
  for (var b = 0; b < 15; b++) { P = P.then(function(){ return switchBatch(10); }); }
  return P.then(function(){
    var switchMs = Date.now() - t0;
    LM.nav.showPage("overview");
    out.switchAllOk = ok;
    out.switchMs = switchMs;
    var instAfter = (LM.charts && LM.charts.instanceCount) ? LM.charts.instanceCount() : null;
    var memAfter  = (performance && performance.memory) ? performance.memory.usedJSHeapSize : null;
    out.instBefore = instBefore; out.instAfter = instAfter;
    out.instDelta = (instBefore!=null && instAfter!=null) ? (instAfter-instBefore) : null;
    // 再切一遍（含 settings/history 懒初始化图表），观察是否有未回收实例
    var P2 = Promise.resolve();
    for (var c = 0; c < 4; c++) { P2 = P2.then(function(){ return switchBatch(10); }); }
    return P2;
  }).then(function(){
    LM.nav.showPage("overview");
    var instFinal = (LM.charts && LM.charts.instanceCount) ? LM.charts.instanceCount() : null;
    out.instFinal = instFinal;
    out.instFinalDeltaVsBefore = (instBefore!=null && instFinal!=null) ? (instFinal-instBefore) : null;
    out.memBeforeKB = memBefore ? Math.round(memBefore/1024) : null;
    out.memAfterKB  = memAfter  ? Math.round(memAfter/1024)  : null;
    out.navItemCount = document.querySelectorAll(".nav-item").length;
    out.pageCount = document.querySelectorAll(".page").length;
    return JSON.stringify(out);
  });
})()
