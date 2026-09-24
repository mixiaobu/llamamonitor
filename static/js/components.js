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
    online: "已连接",
    offline: "连接中断",
    warning: "警告",
    error: "错误",
    updating: "更新中",
    paused: "检测中",
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
    // 只用 classList 切换样式类，保留 .modal-ok 定位类
    // （整体覆盖 className 会在第一次打开后把 .modal-ok 冲掉，
    //   导致第二次 querySelector(".modal-ok") 返回 null、模态永远打不开）
    okBtn.classList.remove("primary", "danger");
    okBtn.classList.add(opts.danger ? "danger" : "primary");
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

  /* ============ setEmptyState（Phase 16C §15：统一空态开关） ============
     根因修复：author CSS 的 .empty-state{display:flex} 会压过 UA 的
     [hidden]{display:none}，导致 JS 设 el.hidden=true 后空态仍显示
     （HISTORY-001/002）。这里用 !important 锁定：
     isEmpty=true  -> 强制显示空态（.force-show）
     isEmpty=false -> 强制隐藏（.force-hide）
     所有列表型空态（缺口/事件/…）一律走此函数，不再各自拼 .hidden。 */
  function setEmptyState(el, isEmpty) {
    if (!el) return;
    el.classList.toggle("force-show", !!isEmpty);
    el.classList.toggle("force-hide", !isEmpty);
    el.hidden = !isEmpty; // 保留语义（无障碍/序列化）
  }

  /* ============ humanizeEventDetails（Phase 16C §17/§18） ============
     展示层 Humanize：返回面向用户的短文本。
     不删除原 details（调用方可保留到 title 作为技术细节）。
     未知字段走安全 fallback（key: value 列表）。 */
  function humanizeEventDetails(ev) {
    var d = ev && ev.details;
    if (d == null) return "";
    if (typeof d === "string") return d;
    if (typeof d !== "object") return String(d);
    var type = ev.event_type || "";
    function num(n) { try { return Number(n).toLocaleString("zh-CN"); } catch (e) { return String(n); } }
    function hostUrl(u) {
      try { var m = String(u).match(/^https?:\/\/([^/]+)/); return m ? m[1] : String(u); }
      catch (e) { return String(u); }
    }
    // 按事件类型格式化常见字段
    switch (type) {
      case "monitor_restart_gap":
      case "monitor_restart": {
        var sec = d.duration_seconds;
        if (sec == null) return "";
        return "持续 " + fmtDurHm(sec);
      }
      case "monitor_start":
        return d.poll_interval_seconds != null ? "指标采集间隔 " + num(d.poll_interval_seconds) + " 秒" : "";
      case "migration": {
        if (d.from != null && d.to != null) return "Schema " + num(d.from) + " → " + num(d.to);
        return "";
      }
      case "monitor_stop":
        return d.reason ? String(d.reason) : "";
      case "server_online":
      case "server_offline":
        return d.url ? hostUrl(d.url) : "";
      case "counter_reset": {
        // 16F 术语审计：内部 counter 名不直接显示，映射为中文计数名
        var COUNTER_LABELS = {
          "prompt_tokens_total": "输入 Token 计数",
          "prompt_tokens_cached_total": "缓存复用 Token 计数",
          "tokens_predicted_total": "输出 Token 计数",
        };
        var parts = [];
        if (d.counter_name) parts.push((COUNTER_LABELS[d.counter_name] || String(d.counter_name)) + ":");
        if (d.previous != null || d.current != null) {
          parts.push((d.previous != null ? num(d.previous) : "?") + " → " + (d.current != null ? num(d.current) : "?"));
        }
        return parts.join(" ");
      }
      case "backup_created":
        return d.file ? String(d.file) : "备份完成";
      case "database_protective_mode":
      case "database_recovery":
        return d.reason ? String(d.reason) : "";
      case "update_check":
        return d.version ? "最新 " + String(d.version) : "";
      case "update_available":
        return d.version ? "新版本 " + String(d.version) : "";
      case "update_download_started":
      case "update_download_complete":
      case "update_download_cancelled":
        return d.version ? String(d.version) : "";
      default:
        // 安全 fallback：key: value 列表（截断过长值）
        var items = [];
        for (var k in d) {
          if (!Object.prototype.hasOwnProperty.call(d, k)) continue;
          var v = d[k];
          if (v == null) continue;
          if (typeof v === "object") { try { v = JSON.stringify(v); } catch (e) { v = String(v); } }
          else v = String(v);
          if (v.length > 48) v = v.slice(0, 48) + "…";
          items.push(k + ": " + v);
        }
        return items.join("；");
    }
  }
  function fmtDurHm(sec) {
    sec = Math.max(0, Math.round(Number(sec) || 0));
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    if (h) return h + "小时" + (m ? m + "分" : "") + (s ? s + "秒" : "");
    if (m) return m + "分" + s + "秒";
    return s + "秒";
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
    setEmptyState: setEmptyState,
    humanizeEventDetails: humanizeEventDetails,
    infoTip: infoTip,
    segmented: segmented,
  };
})();
