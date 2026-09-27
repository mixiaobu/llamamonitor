/* ============================================================
 LlamaMonitor — Navigation, 
 NavigationView 控制器：
 - showPage(name)：切换 .active + aria-current；
 - 页面生命周期：showPage 时调用注册的 onShow 钩子（懒初始化图表、
 刷新页面级数据）；切走时 onLeave 钩子（当前为 no-op 预留）；
 - Compact 模式：窗口宽度 < 1100px 自动 compact
 ResizeObserver 监听 app 容器，UI-007 单一 RO 机制）。
 ============================================================ */
(function () {
  "use strict";

  var currentPage = null;
  var hooks = {}; 
  // 1.1.2：页面切换守卫——离开某页前询问其守卫（如设置页"有未保存修改"确认）。
  // 守卫返回 false 则中止切换；守卫抛错视为放行（不阻塞导航）。
  var guards = {}; // page -> fn(dest) -> boolean|undefined

  function registerPage(name, onShow, onLeave) {
    hooks[name] = { onShow: onShow, onLeave: onLeave };
  }

  /** 注册离开守卫：fn(dest) 返回 false 时阻止切换到 dest。 */
  function registerNavGuard(name, fn) {
    guards[name] = fn;
  }

  function showPage(name) {
    var next = document.getElementById("page-" + name);
    if (!next) return;
    if (currentPage === name) return;
    // 1.1.2：离开守卫（设置页未保存修改确认等）
    if (currentPage && guards[currentPage]) {
      var ok = true;
      try { ok = guards[currentPage](name) !== false; } catch (e) { console.warn("nav guard failed:", currentPage, e); }
      if (!ok) return;
    }
    if (currentPage && hooks[currentPage] && hooks[currentPage].onLeave) {
      try { hooks[currentPage].onLeave(); } catch (e) { console.warn("page leave failed:", currentPage, e); }
    }
    currentPage = name;
    document.querySelectorAll(".page").forEach(function (p) {
      p.classList.toggle("active", p.id === "page-" + name);
    });
    // 1.1.2：底栏 .mnav-item 高亮同步。二级页（MORE_PAGES）归入"更多"按钮
    // （无 data-page，固定 id=mnavMore）；其余按 data-page 匹配。
    var MORE_PAGES = { system: 1, history: 1, settings: 1, about: 1 };
    document.querySelectorAll(".nav-item").forEach(function (b) {
      if (b.getAttribute("data-page") === name) b.setAttribute("aria-current", "page");
      else b.removeAttribute("aria-current");
    });
    // .mnav-more（"更多"按钮）无 data-page：当前页属于二级页时高亮它
    document.querySelectorAll(".mnav-item:not(.mnav-more)").forEach(function (b) {
      if (b.getAttribute("data-page") === name) b.setAttribute("aria-current", "page");
      else b.removeAttribute("aria-current");
    });
    var moreBtn = document.querySelector(".mnav-more");
    if (moreBtn) {
      if (MORE_PAGES[name]) moreBtn.setAttribute("aria-current", "page");
      else moreBtn.removeAttribute("aria-current");
    }
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
  //python 侧窗口可见性桥（UI-024；WebView2 hide 不保证 visibilitychange）
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

  /* ---------- 1.1.2 Mobile Bottom Navigation + More Sheet ----------
 ≤760px 显示底栏（CSS 控制显隐）；"更多"按钮弹出底部 sheet 选二级页。
 桌面下同样可初始化（DOM 存在），只是隐藏。 */
  var sheetOpen = false;

  function openSheet() {
    var scrim = document.getElementById("moreSheetScrim");
    var sheet = document.getElementById("moreSheet");
    if (!scrim || !sheet || sheetOpen) return;
    scrim.hidden = false;
    sheet.hidden = false;
    sheetOpen = true;
    var first = sheet.querySelector(".sheet-item");
    if (first) first.focus();
  }

  function closeSheet() {
    var scrim = document.getElementById("moreSheetScrim");
    var sheet = document.getElementById("moreSheet");
    if (scrim) scrim.hidden = true;
    if (sheet) sheet.hidden = true;
    sheetOpen = false;
    var more = document.getElementById("mnavMore");
    if (more) more.focus();
  }

  function sheetVisible() {
    return sheetOpen;
  }

  function initMobileNav() {
    document.querySelectorAll(".mnav-item").forEach(function (b) {
      b.addEventListener("click", function () {
        var dp = b.getAttribute("data-page");
        if (!dp) { openSheet(); return; }
        showPage(dp);
        closeSheet();
      });
    });
    document.querySelectorAll(".sheet-item").forEach(function (b) {
      b.addEventListener("click", function () {
        var dp = b.getAttribute("data-page");
        if (!dp) return;
        var before = currentPage;
        showPage(dp);
        // 页面确实切换才关 sheet（被设置页未保存守卫拦住时保持打开）
        if (currentPage !== before) closeSheet();
      });
    });
    var scrim = document.getElementById("moreSheetScrim");
    if (scrim) scrim.addEventListener("click", closeSheet);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && sheetOpen) closeSheet();
    });
    var ver = document.getElementById("moreSheetVersion");
    if (ver && LM.settings && LM.settings.loadAboutVersion) {
      LM.settings.loadAboutVersion(ver);
    }
  }

  window.LM = window.LM || {};
  LM.nav = {
    showPage: showPage,
    registerPage: registerPage,
    currentPage: function () { return currentPage; },
    initCompact: initCompact,
    registerNavGuard: registerNavGuard,
    initMobileNav: initMobileNav,
    sheetVisible: sheetVisible,
  };
})();
