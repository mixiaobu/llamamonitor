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

  /* ---------- Compact 模式（单一 ResizeObserver + 手动折叠） ----------
     手动状态优先级：null=跟随窗口宽度自动；true/false=用户手动选择。
     手动选择记忆到 localStorage，刷新后保持。 */
  var manualCompact = null;

  function initCompact() {
    var app = document.querySelector(".app");
    if (!app) return;
    try {
      var saved = localStorage.getItem("lm_nav_manual");
      if (saved === "1") manualCompact = true;
      else if (saved === "0") manualCompact = false;
    } catch (e) { /* 存储不可用时忽略 */ }

    // 折叠入口 = 品牌图标（hover 高亮提示可点；点击切换收起/展开）
    var brand = document.getElementById("brandIcon");
    var lastWidth = app.clientWidth;

    function apply() {
      var effective = manualCompact === null
        ? lastWidth < 1100
        : manualCompact;
      app.classList.toggle("compact", effective);
      if (brand) {
        var label = effective ? "展开侧边栏" : "收起侧边栏";
        brand.title = label;
        brand.setAttribute("aria-label", label);
      }
    }

    if (brand) {
      brand.addEventListener("click", function () {
        var isCompact = app.classList.contains("compact");
        manualCompact = !isCompact;
        try {
          localStorage.setItem("lm_nav_manual", manualCompact ? "1" : "0");
        } catch (e) { /* 忽略 */ }
        apply();
      });
    }

    if (typeof ResizeObserver !== "undefined") {
      var ro = new ResizeObserver(function (entries) {
        if (entries[0] && entries[0].contentRect) {
          lastWidth = entries[0].contentRect.width;
        }
        apply();
      });
      ro.observe(app);
    } else {
      window.addEventListener("resize", function () {
        lastWidth = app.clientWidth;
        apply();
      });
    }
    apply();
  }

  window.LM = window.LM || {};
  LM.nav = {
    showPage: showPage,
    registerPage: registerPage,
    currentPage: function () { return currentPage; },
    initCompact: initCompact,
  };
})();
