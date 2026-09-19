/* ============================================================
   LlamaMonitor — Navigation（Phase 15, spec §46/§54/§8）
   NavigationView 控制器：
   - showPage(name)：切换 .active + aria-current（spec §75）；
   - 页面生命周期：showPage 时调用注册的 onShow 钩子（懒初始化图表、
     刷新页面级数据）；切走时 onLeave 钩子（当前为 no-op 预留）；
   - Compact 模式：窗口宽度 < 1100px 自动 compact（spec §54，
     ResizeObserver 监听 app 容器，UI-007 单一 RO 机制）。
   ============================================================ */
(function () {
  "use strict";

  var currentPage = null;
  var hooks = {}; // page -> {onShow: fn, onLeave: fn}

  function registerPage(name, onShow, onLeave) {
    hooks[name] = { onShow: onShow, onLeave: onLeave };
  }

  function showPage(name) {
    var next = document.getElementById("page-" + name);
    if (!next) return;
    if (currentPage === name) return;
    if (currentPage && hooks[currentPage] && hooks[currentPage].onLeave) {
      try { hooks[currentPage].onLeave(); } catch (e) { console.warn("page leave failed:", currentPage, e); }
    }
    currentPage = name;
    document.querySelectorAll(".page").forEach(function (p) {
      p.classList.toggle("active", p.id === "page-" + name);
    });
    document.querySelectorAll(".nav-item").forEach(function (b) {
      if (b.getAttribute("data-page") === name) b.setAttribute("aria-current", "page");
      else b.removeAttribute("aria-current");
    });
    // 内容区回顶（页面切换语义）
    var content = document.querySelector(".content");
    if (content) content.scrollTop = 0;
    if (hooks[name] && hooks[name].onShow) {
      try { hooks[name].onShow(); } catch (e) { console.warn("page show failed:", name, e); }
    }
  }

  /** 托盘 "Update Available" 桥接（desktop.py evaluate_js）：进 Settings→Updates。 */
  function showSettingsSection(section) {
    showPage("settings");
    var s = LM.settings;
    if (s && s.goToSection) s.goToSection(section);
  }
  window.__showUpdatesSection = function () { showSettingsSection("updates"); };
  // Phase 15：python 侧窗口可见性桥（UI-024；WebView2 hide 不保证 visibilitychange）
  window.__lmSetVisible = null; // app.js 设置

  /* ---------- Compact 模式（单一 ResizeObserver） ---------- */
  function initCompact() {
    var app = document.querySelector(".app");
    if (!app || typeof ResizeObserver === "undefined") return;
    var ro = new ResizeObserver(function (entries) {
      var w = entries[0].contentRect.width;
      app.classList.toggle("compact", w < 1100);
    });
    ro.observe(app);
  }

  window.LM = window.LM || {};
  LM.nav = {
    showPage: showPage,
    registerPage: registerPage,
    currentPage: function () { return currentPage; },
    initCompact: initCompact,
  };
})();
