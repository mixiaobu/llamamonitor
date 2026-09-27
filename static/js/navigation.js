/* ============================================================
 LlamaMonitor — Navigation
 NavigationView 控制器：
 - showPage(name)：切换 .active + aria-current；
 - 页面生命周期：showPage 时调用注册的 onShow 钩子（懒初始化图表、
 刷新页面级数据）；切走时 onLeave 钩子（no-op 预留）；
 - 每页滚动位置记忆（spec §101）：切走前记录 scrollTop，切回恢复；
 首次进入 = 0；重复点击"当前页"的导航项 = 回顶；
 - 1.1.3 Mobile：核心 5 页进 Bottom Nav；辅助页（历史/设置/关于）
 进每页页头右侧 ••• Overflow Sheet（focus trap + body scroll lock +
 关闭回焦点）；
 - Compact 模式：窗口宽度 < 1100px 自动 compact（ResizeObserver）。
 ============================================================ */
(function () {
  "use strict";

  var currentPage = null;
  var hooks = {};
  var scrollMem = {}; // page -> scrollTop（每页滚动位置记忆）
  // 离开守卫：返回 false 则中止切换；抛错视为放行。
  var guards = {};

  function registerPage(name, onShow, onLeave) {
    hooks[name] = { onShow: onShow, onLeave: onLeave };
  }

  function registerNavGuard(name, fn) {
    guards[name] = fn;
  }

  function contentEl() { return document.querySelector(".content"); }

  function showPage(name) {
    var next = document.getElementById("page-" + name);
    if (!next) return;
    var content = contentEl();
    // spec §101：重复点击"当前页" → 回顶（而不是无操作）。
    if (currentPage === name) {
      if (content) content.scrollTop = 0;
      return;
    }
    // 离开守卫（设置页未保存修改确认等）
    if (currentPage && guards[currentPage]) {
      var ok = true;
      try { ok = guards[currentPage](name) !== false; } catch (e) { console.warn("nav guard failed:", currentPage, e); }
      if (!ok) return;
    }
    if (currentPage && hooks[currentPage] && hooks[currentPage].onLeave) {
      try { hooks[currentPage].onLeave(); } catch (e) { console.warn("page leave failed:", currentPage, e); }
    }
    // 切走前记录当前页滚动位置（spec §101）
    if (currentPage && content) scrollMem[currentPage] = content.scrollTop;
    currentPage = name;
    document.querySelectorAll(".page").forEach(function (p) {
      p.classList.toggle("active", p.id === "page-" + name);
    });
    // 底栏 .mnav-item 高亮（1.1.3：5 核心页都走 data-page 直接匹配）。
    document.querySelectorAll(".mnav-item").forEach(function (b) {
      if (b.getAttribute("data-page") === name) b.setAttribute("aria-current", "page");
      else b.removeAttribute("aria-current");
    });
    document.querySelectorAll(".nav-item").forEach(function (b) {
      if (b.getAttribute("data-page") === name) b.setAttribute("aria-current", "page");
      else b.removeAttribute("aria-current");
    });
    // 恢复目标页滚动位置（首次进入 = 0）
    if (content) content.scrollTop = scrollMem[name] || 0;
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
  window.__lmSetVisible = null; // app.js 设置（python 侧窗口可见性桥）

  /* ---------- Compact 模式（单一 ResizeObserver + 手动折叠） ---------- */
  var manualCompact = null;

  function initCompact() {
    var app = document.querySelector(".app");
    if (!app) return;
    try {
      var saved = localStorage.getItem("lm_nav_manual");
      if (saved === "1") manualCompact = true;
      else if (saved === "0") manualCompact = false;
    } catch (e) { /* 存储不可用时忽略 */ }
    var brand = document.getElementById("brandIcon");
    var lastWidth = app.clientWidth;

    function apply() {
      var effective = manualCompact === null ? lastWidth < 1100 : manualCompact;
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
        try { localStorage.setItem("lm_nav_manual", manualCompact ? "1" : "0"); } catch (e) {}
        apply();
      });
    }
    if (typeof ResizeObserver !== "undefined") {
      var ro = new ResizeObserver(function (entries) {
        if (entries[0] && entries[0].contentRect) lastWidth = entries[0].contentRect.width;
        apply();
      });
      ro.observe(app);
    } else {
      window.addEventListener("resize", function () { lastWidth = app.clientWidth; apply(); });
    }
    apply();
  }

  /* ================================================================
   1.1.3 Mobile Overflow Sheet（辅助页：历史 / 设置 / 关于）
   由每页页头右侧 ••• 按钮触发（.page-overflow，44×44 hit）。
   - focus trap（Tab/Shift+Tab 循环，spec §103）
   - body scroll lock（打开锁背景滚动，spec §104）
   - ESC / 点 scrim / 选择项关闭，焦点回触发按钮（spec §105）
   ================================================================ */
  var sheetOpen = false;
  var sheetTrigger = null; // 打开 sheet 的 ••• 按钮（关闭时回焦点）
  var lastFocused = null;

  function sheetNodes() {
    return {
      scrim: document.getElementById("moreSheetScrim"),
      sheet: document.getElementById("moreSheet"),
    };
  }

  function openSheet(trigger) {
    var n = sheetNodes();
    if (!n.scrim || !n.sheet || sheetOpen) return;
    sheetTrigger = trigger || null;
    lastFocused = document.activeElement;
    n.scrim.hidden = false;
    n.sheet.hidden = false;
    sheetOpen = true;
    lockBodyScroll(true);
    if (trigger) { trigger.setAttribute("aria-expanded", "true"); }
    var first = n.sheet.querySelector(".sheet-item:not([disabled])");
    if (first) first.focus();
  }

  function closeSheet() {
    var n = sheetNodes();
    if (n.scrim) n.scrim.hidden = true;
    if (n.sheet) n.sheet.hidden = true;
    if (!sheetOpen) return;
    sheetOpen = false;
    lockBodyScroll(false);
    if (sheetTrigger) sheetTrigger.setAttribute("aria-expanded", "false");
    // 焦点回触发按钮（无则回 lastFocused）
    var target = sheetTrigger || lastFocused;
    if (target && document.contains(target)) target.focus();
    sheetTrigger = null;
    lastFocused = null;
  }

  /* body scroll lock：给 html 加 overflow:hidden + 固定宽度防滚动条跳动。 */
  var scrollLocked = false;
  function lockBodyScroll(on) {
    var doc = document.documentElement;
    if (on && !scrollLocked) {
      doc.style.overflow = "hidden";
      doc.style.width = "100%";
      scrollLocked = true;
    } else if (!on && scrollLocked) {
      doc.style.overflow = "";
      doc.style.width = "";
      scrollLocked = false;
    }
  }

  /* focus trap：Tab 在 sheet 内可聚焦元素间循环。 */
  function trapFocus(e) {
    if (!sheetOpen || e.key !== "Tab") return;
    var n = sheetNodes();
    if (!n.sheet) return;
    var focusables = Array.prototype.filter.call(
      n.sheet.querySelectorAll("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])"),
      function (el) { return el.offsetParent !== null && !el.disabled; }
    );
    if (!focusables.length) return;
    var first = focusables[0], last = focusables[focusables.length - 1];
    var active = document.activeElement;
    if (e.shiftKey) {
      if (active === first || !n.sheet.contains(active)) { e.preventDefault(); last.focus(); }
    } else {
      if (active === last || !n.sheet.contains(active)) { e.preventDefault(); first.focus(); }
    }
  }

  function initMobileNav() {
    // 1) 每页页头标题行右侧注入 ••• Overflow 按钮（仅 ≤760px 显示，CSS 控制；
    //    桌面 display:none 不影响布局）。注入 .ph-head（absolute 定位参照）。
    document.querySelectorAll(".page-header").forEach(function (hdr) {
      var head = hdr.querySelector(".ph-head");
      if (!head) head = hdr;
      if (head.querySelector(".page-overflow")) return; // 幂等
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "page-overflow";
      btn.setAttribute("aria-haspopup", "dialog");
      btn.setAttribute("aria-controls", "moreSheet");
      btn.setAttribute("aria-expanded", "false");
      btn.setAttribute("aria-label", "更多页面");
      btn.innerHTML = '<span class="page-overflow-icon" aria-hidden="true"></span>';
      if (LM.icons && LM.icons.get) btn.querySelector(".page-overflow-icon").innerHTML = LM.icons.get("more");
      head.appendChild(btn);
      btn.addEventListener("click", function () { openSheet(btn); });
    });

    // 2) sheet 项点击：切换页面才关闭（被守卫拦住时保持打开）。
    document.querySelectorAll(".sheet-item").forEach(function (b) {
      b.addEventListener("click", function () {
        var dp = b.getAttribute("data-page");
        if (!dp) return;
        var before = currentPage;
        showPage(dp);
        if (currentPage !== before) closeSheet();
      });
    });

    // 3) scrim 点击 + ESC + Tab trap。
    var scrim = document.getElementById("moreSheetScrim");
    if (scrim) scrim.addEventListener("click", closeSheet);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && sheetOpen) { e.preventDefault(); closeSheet(); }
      else if (sheetOpen) trapFocus(e);
    });

    // 4) sheet 版本号。
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
    sheetVisible: function () { return sheetOpen; },
  };
})();
