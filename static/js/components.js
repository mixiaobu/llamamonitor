/* ============================================================
   LlamaMonitor — Components（Phase 15, spec §16/§18/§70/§73-81）
   统一组件：StatusBadge / InfoBar / Toast（去重 §81）/
   Modal（focus trap + Escape §78/§79）/ EmptyState（§38）/
   InfoTooltip（§61-63）/ Segmented（§70/§71）。
   全部 DOM API 构建（不拼 HTML 字符串，spec 审计 UI-012）。
   ============================================================ */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  /* ================= StatusBadge（spec §16/§17：小圆点 + 统一文案） =================
     state: online | offline | warning | error | updating | paused
     文案统一（UI-003）：Online / Offline / Warning / Error / Updating / Paused */
  var STATE_TEXT = {
    online: "在线",
    offline: "离线",
    warning: "警告",
    error: "错误",
    updating: "更新中",
    paused: "已暂停",
  };

  function setStatusBadge(el, state, textOverride) {
    if (!el) return;
    el.className = "status-badge " + (state || "");
    var dot = el.querySelector(".dot");
    if (!dot) {
      dot = document.createElement("span");
      dot.className = "dot";
      el.appendChild(dot);
    }
    var label = el.querySelector(".status-text");
    if (!label) {
      label = document.createElement("span");
      label.className = "status-text";
      el.appendChild(label);
    }
    label.textContent = textOverride || STATE_TEXT[state] || "未知";
    el.setAttribute("role", "status");
  }

  /* ================= InfoBar（spec §18） =================
     createInfoBar({type:'info|success|warning|error', title, message,
                    actions:[{label,onClick}], dismissible}) -> {el, close}
     调用方把 el 挂到容器；close() 移除（150ms fade）。 */
  function createInfoBar(opts) {
    var el = document.createElement("div");
    el.className = "infobar " + (opts.type || "info");
    el.setAttribute("role", opts.type === "error" || opts.type === "warning" ? "alert" : "status");

    var icon = document.createElement("span");
    icon.className = "infobar-icon";
    icon.innerHTML = LM.icons.get(opts.type === "success" ? "success" : opts.type || "info");

    var body = document.createElement("div");
    body.className = "infobar-body";
    if (opts.title) {
      var t = document.createElement("div");
      t.className = "infobar-title";
      t.textContent = opts.title;
      body.appendChild(t);
    }
    if (opts.message) {
      var m = document.createElement("div");
      m.className = "infobar-message";
      m.textContent = opts.message;
      body.appendChild(m);
    }
    el.appendChild(icon);
    el.appendChild(body);

    function close() {
      if (el.parentNode && !el.classList.contains("leaving")) {
        el.style.transition = "opacity 150ms ease";
        el.style.opacity = "0";
        setTimeout(function () { el.remove(); }, 160);
      }
    }

    if (opts.actions && opts.actions.length) {
      var actions = document.createElement("div");
      actions.className = "infobar-actions";
      opts.actions.forEach(function (a) {
        var b = document.createElement("button");
        b.className = "btn small subtle";
        b.textContent = a.label;
        b.addEventListener("click", function () { a.onClick(); });
        actions.appendChild(b);
      });
      el.appendChild(actions);
    }
    if (opts.dismissible !== false) {
      var x = document.createElement("button");
      x.className = "infobar-close";
      x.setAttribute("aria-label", "关闭通知");
      x.textContent = "\u00D7";
      x.addEventListener("click", close);
      el.appendChild(x);
    }
    return { el: el, close: close };
  }

  /* ================= Toast（spec §80/§81：右下，3-5s，按消息去重） ================= */
  var toastBox = null;
  var activeToasts = new Map(); // key -> {el, timer}

  function ensureToastBox() {
    if (toastBox) return toastBox;
    toastBox = $("toastBox");
    if (!toastBox) {
      toastBox = document.createElement("div");
      toastBox.id = "toastBox";
      toastBox.className = "toast-box";
      toastBox.setAttribute("role", "status");
      toastBox.setAttribute("aria-live", "polite");
      document.body.appendChild(toastBox);
    }
    return toastBox;
  }

  /** toast(message, type) — type: ok|info|warn|err。同 key 未消失时只重置计时（UI-005）。 */
  function toast(message, type) {
    var box = ensureToastBox();
    var key = (type || "info") + "::" + message;
    var existing = activeToasts.get(key);
    if (existing) {
      clearTimeout(existing.timer);
      existing.timer = scheduleLeave(existing);
      return;
    }
    var el = document.createElement("div");
    el.className = "toast " + (type || "info");
    var icon = document.createElement("span");
    icon.className = "toast-icon";
    var iconName = type === "ok" ? "success" : type === "err" ? "error" : type === "warn" ? "warning" : "info";
    icon.innerHTML = LM.icons.get(iconName);
    var msg = document.createElement("span");
    msg.textContent = message;
    el.appendChild(icon);
    el.appendChild(msg);
    box.appendChild(el);

    var entry = { el: el, timer: 0 };
    function leave() {
      el.classList.add("leaving");
      setTimeout(function () {
        el.remove();
        activeToasts.delete(key);
      }, 170);
    }
    function scheduleLeave() {
      var t = setTimeout(leave, type === "err" ? 6000 : 4200);
      entry.timer = t;
      return t;
    }
    entry.leave = leave;
    entry.timer = scheduleLeave();
    activeToasts.set(key, entry);
    // 限制堆叠数量（防御）
    if (activeToasts.size > 5) {
      var oldest = activeToasts.keys().next().value;
      var o = activeToasts.get(oldest);
      if (o && o !== entry) {
        clearTimeout(o.timer);
        o.leave();
      }
    }
  }

  /* ================= Modal（spec §78/§79：focus trap / Escape / 焦点归还） =================
     modal({title, text, okLabel, danger, needsInput, inputValue, onDone(ok)})
     全局单例（页面只有一个 modal 槽位 #modalOverlay）。 */
  var modalState = { open: false, lastFocus: null, handler: null };

  function modal(opts) {
    var overlay = $("modalOverlay");
    if (!overlay) return;
    var modalEl = overlay.querySelector(".modal");
    var titleEl = overlay.querySelector(".modal-title");
    var textEl = overlay.querySelector(".modal-text");
    var inputEl = overlay.querySelector(".modal-input");
    var okBtn = overlay.querySelector(".modal-ok");
    var cancelBtn = overlay.querySelector(".modal-cancel");

    titleEl.textContent = opts.title || "确认";
    textEl.textContent = opts.text || "";

    var need = opts.needsInput ? opts.inputValue : null;
    inputEl.value = "";
    inputEl.hidden = !need;
    inputEl.placeholder = need ? "输入 " + need + " 以确认" : "";

    okBtn.textContent = opts.okLabel || "确定";
    okBtn.className = "btn " + (opts.danger ? "danger" : "primary");
    okBtn.disabled = !!need;

    modalState.lastFocus = document.activeElement;
    modalState.open = true;
    overlay.hidden = false;

    var check = function () {
      if (need) okBtn.disabled = inputEl.value !== need;
    };
    inputEl.addEventListener("input", check);

    function cleanup() {
      modalState.open = false;
      overlay.hidden = true;
      okBtn.onclick = null;
      cancelBtn.onclick = null;
      inputEl.removeEventListener("input", check);
      document.removeEventListener("keydown", modalState.handler, true);
      if (modalState.lastFocus && modalState.lastFocus.focus) {
        try { modalState.lastFocus.focus(); } catch (e) { /* 元素可能已移除 */ }
      }
    }

    okBtn.onclick = function () { cleanup(); opts.onDone && opts.onDone(true); };
    cancelBtn.onclick = function () { cleanup(); opts.onDone && opts.onDone(false); };

    modalState.handler = function (ev) {
      if (ev.key === "Escape") {
        ev.stopPropagation();
        cleanup();
        opts.onDone && opts.onDone(false);
        return;
      }
      if (ev.key === "Tab") {
        // focus trap：焦点困在 modal 内（spec §79）
        var focusables = modalEl.querySelectorAll(
          "button:not(:disabled), input:not([hidden]):not(:disabled), [tabindex]:not([tabindex='-1'])"
        );
        if (!focusables.length) return;
        var first = focusables[0];
        var last = focusables[focusables.length - 1];
        if (ev.shiftKey && document.activeElement === first) {
          ev.preventDefault();
          last.focus();
        } else if (!ev.shiftKey && document.activeElement === last) {
          ev.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", modalState.handler, true);

    // 打开即焦点进入（spec §79）
    setTimeout(function () {
      (inputEl.hidden ? okBtn : inputEl).focus();
    }, 0);
  }

  /* ================= EmptyState（spec §38/§110） =================
     el.innerHTML 替换为空态；clearEmpty(el) 恢复。
     opts: {icon:'emptyChart|emptyDb|emptyGauge', title, desc} */
  function showEmpty(el, opts) {
    if (!el) return;
    if (el.dataset.empty === "1") return;
    el.dataset.empty = "1";
    el.dataset.emptyHtml = el.innerHTML;
    el.innerHTML = "";
    var box = document.createElement("div");
    box.className = "empty-state";
    var ic = document.createElement("div");
    ic.className = "empty-icon";
    ic.innerHTML = LM.icons.get(opts.icon || "emptyChart");
    var t = document.createElement("div");
    t.className = "empty-title";
    t.textContent = opts.title || "暂无数据";
    var d = document.createElement("div");
    d.className = "empty-desc";
    d.textContent = opts.desc || "";
    box.appendChild(ic);
    box.appendChild(t);
    box.appendChild(d);
    el.appendChild(box);
  }

  function clearEmpty(el) {
    if (!el || el.dataset.empty !== "1") return;
    el.dataset.empty = "0";
    el.innerHTML = el.dataset.emptyHtml || "";
  }

  /* ================= InfoTooltip（spec §61-63） =================
     infoTip(text) -> HTMLElement（? 图标 + 定义文本；hover/focus 显示，
     CSS .info-tip 控制）。alignRight 用于靠近右边缘的 label。 */
  function infoTip(text, alignRight) {
    var wrap = document.createElement("span");
    wrap.className = "info-tip" + (alignRight ? " tip-align-right" : "");
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "tip-btn";
    btn.textContent = "?";
    btn.setAttribute("aria-label", "定义");
    btn.setAttribute("aria-describedby", "");
    var tip = document.createElement("span");
    tip.className = "tip-text";
    tip.id = "tip-" + Math.random().toString(36).slice(2, 9);
    tip.textContent = text;
    btn.setAttribute("aria-describedby", tip.id);
    wrap.appendChild(btn);
    wrap.appendChild(tip);
    return wrap;
  }

  /* ================= Segmented（spec §70/§71：统一分段控件） =================
     segmented(el, options, initial, onChange)
     options: [{value, label}]。返回 {set(value), get()}。 */
  function segmented(el, options, initial, onChange) {
    if (!el) return { set: function () {}, get: function () { return initial; } };
    el.className = "seg";
    el.setAttribute("role", "group");
    el.innerHTML = "";
    var current = null;
    var btns = [];
    options.forEach(function (o) {
      var b = document.createElement("button");
      b.type = "button";
      b.textContent = o.label;
      b.setAttribute("aria-pressed", "false");
      b.addEventListener("click", function () {
        if (current === o.value) return;
        current = o.value;
        sync();
        onChange && onChange(o.value);
      });
      el.appendChild(b);
      btns.push({ value: o.value, el: b });
    });
    function sync() {
      btns.forEach(function (b) {
        b.el.setAttribute("aria-pressed", b.value === current ? "true" : "false");
      });
    }
    if (initial !== undefined) {
      current = initial;
      sync();
    }
    return {
      set: function (v) {
        current = v;
        sync();
      },
      get: function () { return current; },
    };
  }

  window.LM = window.LM || {};
  LM.ui = {
    setStatusBadge: setStatusBadge,
    createInfoBar: createInfoBar,
    toast: toast,
    modal: modal,
    showEmpty: showEmpty,
    clearEmpty: clearEmpty,
    infoTip: infoTip,
    segmented: segmented,
  };
})();
