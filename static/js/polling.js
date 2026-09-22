/* ============================================================
   LlamaMonitor — Polling（Phase 15, spec §45/§46/§47/§48/§49）
   中央轮询调度器：
   - 所有周期任务统一注册（命名任务 + 间隔 + 可见性策略）；
   - in-flight 守卫（spec §45：上一轮未返回则跳过本轮，不重叠）；
   - 最小间隔 1000ms（AUDIT-WEB-007 保留：config 写 0 不变忙循环）；
   - 可见性策略：
       visibleOnly=false（默认）：窗口隐藏仍按间隔跑（30s/120s 类，
         与 Phase 9 语义一致：隐藏时有效频率约 30s）；
       visibleOnly=true：隐藏时跳过本轮（5s/15s/1s 类）；
   - 应用可见性 = document.visibilityState AND window.__appVisible
     （pywebview 窗口 hide 时 Python 侧 evaluate_js 置 false，UI-024）；
   - 回到前台：立即全量刷新一次（spec §48）。

   任务注册后永不重复创建（UI-023/§140：no duplicate timers）。
   ============================================================ */
(function () {
  "use strict";

  var tasks = new Map();   // name -> task
  var appVisible = true;   // Python 桥（窗口真实可见性）

  function isPageVisible() {
    return document.visibilityState !== "hidden";
  }

  /** 综合可见性：浏览器 tab 可见 AND pywebview 窗口可见 */
  function isAppVisible() {
    return isPageVisible() && appVisible;
  }

  function register(name, opts) {
    if (tasks.has(name)) return tasks.get(name); // 防重复注册（UI-023）
    var t = {
      name: name,
      intervalMs: Math.max(1000, Number(opts.intervalMs) || 10000),
      // 前台自适应：窗口可见时用更短的 visibleIntervalMs（可选），
      // 隐藏时回到 intervalMs——前台数据"实时感"，后台不浪费请求
      visibleIntervalMs: opts.visibleIntervalMs ? Math.max(1000, Number(opts.visibleIntervalMs)) : null,
      run: opts.run,
      visibleOnly: !!opts.visibleOnly,
      timer: 0,
      inFlight: false,
      started: false,
    };
    tasks.set(name, t);
    return t;
  }

  /** 任务当前应使用的间隔：前台（窗口可见）优先短间隔。 */
  function currentInterval(t) {
    if (t.visibleIntervalMs && isAppVisible()) return t.visibleIntervalMs;
    return t.intervalMs;
  }

  function start(name) {
    var t = tasks.get(name);
    if (!t || t.started) return;
    t.started = true;
    schedule(t);
  }

  function startAll() {
    tasks.forEach(function (t, name) { start(name); });
  }

  function schedule(t) {
    clearTimeout(t.timer);
    t.timer = setTimeout(function () {
      tick(t);
      schedule(t); // 自调度（某次失败不影响后续轮次；间隔每次按可见性重算）
    }, currentInterval(t));
  }

  /** 可见性翻转：重排所有未 in-flight 任务的下次 tick（间隔随之切换）。 */
  function rescheduleAll() {
    tasks.forEach(function (t) {
      if (!t.started || t.inFlight) return;
      clearTimeout(t.timer);
      schedule(t);
    });
  }

  function tick(t) {
    if (t.visibleOnly && !isAppVisible()) return; // 隐藏：跳过本轮
    if (t.inFlight) return;                        // 不重叠（spec §45）
    t.inFlight = true;
    // 完成时重排：若可见性在本轮运行期间翻转，rescheduleAll 会跳过 in-flight 任务，
    // 自调度又按"发起时"的间隔续期 => 前台/后台间隔错配，直到下次翻转才纠正。
    // 这里完成后按**当前**可见性重排，立即纠正（幂等：rescheduleAll 已排过的再排一次无副作用）。
    function _done() {
      t.inFlight = false;
      if (t.started) schedule(t);
    }
    try {
      var r = t.run();
      if (r && typeof r.then === "function") {
        r.catch(function (e) {
          // 调用方 run 内部已处理保留上次数据；这里兜底防 unhandled rejection（spec §99）
          console.warn("poll task failed:", t.name, e && e.message ? e.message : e);
        }).then(_done);
      } else {
        _done();
      }
    } catch (e) {
      console.warn("poll task threw:", t.name, e && e.message ? e.message : e);
      _done();
    }
  }

  /** 回到前台/窗口重新可见：立即全量刷新一次（spec §48）。 */
  function refreshAllNow() {
    tasks.forEach(function (t) {
      if (t.inFlight) return;
      t.inFlight = true;
      try {
        var r = t.run();
        if (r && typeof r.then === "function") {
          r.catch(function (e) {
            console.warn("immediate poll failed:", t.name, e && e.message ? e.message : e);
          }).then(function () { t.inFlight = false; });
        } else {
          t.inFlight = false;
        }
      } catch (e) {
        console.warn("immediate poll threw:", t.name, e && e.message ? e.message : e);
        t.inFlight = false;
      }
    });
  }

  /**
   * 可见性事件入口。source: 'browser'（document.visibilitychange）
   * 或 'app'（Python 桥 window.__lmSetVisible）。
   * 只有"变为可见"才触发立即刷新。
   */
  function onVisibilityChanged() {
    if (isAppVisible()) refreshAllNow();
  }

  function setAppVisible(v) {
    var was = isAppVisible();
    appVisible = !!v;
    if (!was && isAppVisible()) onVisibilityChanged();
    // 可见性翻转（双向）都要重排：前台切快间隔、后台切回长间隔
    if (was !== isAppVisible()) rescheduleAll();
  }

  function onBrowserVisibility() {
    onVisibilityChanged();
    rescheduleAll();
  }

  /* 初始化 browser 侧监听（一次） */
  var browserBound = false;
  function bindBrowserVisibility() {
    if (browserBound) return;
    browserBound = true;
    document.addEventListener("visibilitychange", onBrowserVisibility);
    // Python 桥（UI-024）
    window.__lmSetVisible = setAppVisible;
  }

  /* 诊断：所有任务的 in-flight 状态（Timer Audit 用，spec §141.13） */
  function audit() {
    var out = {};
    tasks.forEach(function (t, name) {
      out[name] = { intervalMs: t.intervalMs, visibleOnly: t.visibleOnly, inFlight: t.inFlight, started: t.started };
    });
    return out;
  }

  window.LM = window.LM || {};
  LM.poll = {
    register: register,
    start: start,
    startAll: startAll,
    refreshAllNow: refreshAllNow,
    setAppVisible: setAppVisible,
    isAppVisible: isAppVisible,
    bindBrowserVisibility: bindBrowserVisibility,
    audit: audit,
  };
})();
