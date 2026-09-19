/* ============================================================
   LlamaMonitor — App bootstrap（Phase 15）
   应用启动 / 全局状态 / 页面数据接线 / 主题控制器 / 轮询注册。
   职责（spec §122）：
   - loadConfig：/api/config（主题、刷新间隔、服务器地址）
   - 主题控制器：dark/light/system + 系统主题实时跟随（UI-001）
   - 状态处理：Online/Offline/Stale（spec §39/§41/§129）+ 全局 InfoBar
   - 各页面数据刷新函数 + LM.poll 任务注册（spec §47 中央调度器）
   - 每 section 独立失败（spec §42：一个 API 失败不拖垮其他）
   ============================================================ */
(function () {
  "use strict";

  var F = LM.fmt, api = LM.api, ui = LM.ui, charts = LM.charts;
  var $ = function (id) { return document.getElementById(id); };

  var cfgUi = {
    refreshIntervalSeconds: 5,
    dailyDefaultDays: 30,
    theme: "system",
  };

  /* ================= 全局状态 ================= */
  var state = {
    online: null,          // null=尚未取得 / true / false
    lastSuccessTs: null,   // 最近一次 server_online===true 的 last_update（epoch s）
    config: null,          // /api/status 的 config 块
    dailyData: [],         // /api/daily 行（当前范围）
    dailyRange: 30,        // Usage 页当前范围（1/7/30/365=All）
    liveSamples: [],       // /api/live 60 分钟
    gpuStatus: null,       // /api/gpu/status
    gpuLive: null,         // /api/gpu/live（当前范围）
    gpuRangeMinutes: 60,
    gpuVisible: {},        // uuid -> bool
    gpuPickSig: null,      // UI-002：detected 签名
    mtp: null,             // /api/mtp
    mtpPositions: [],
    quality: null,         // /api/data/quality
    health: null,          // /api/health
    monthRows: null,       // 本月 daily 行（This Month 卡用）
    monthMonth: "",
  };

  function setText(id, text, cls) {
    var el = $(id);
    if (!el) return;
    el.textContent = text;
    if (cls !== undefined) el.className = el.className.split(" ").filter(function (c) {
      return c !== "dim" && c !== "stat-hint";
    }).join(" ") + (cls ? " " + cls : "");
  }

  /* ================= 主题控制器（spec §26/§27；UI-001 修复） ================= */
  var themeMode = cfgUi.theme;      // dark | light | system（配置值）
  var resolvedTheme = "dark";
  var systemMq = null;

  function systemIsLight() {
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches;
  }

  function applyTheme(mode) {
    if (mode !== "dark" && mode !== "light" && mode !== "system") mode = "system";
    themeMode = mode;
    resolvedTheme = mode === "system" ? (systemIsLight() ? "light" : "dark") : mode;
    document.documentElement.setAttribute("data-theme", resolvedTheme);
    charts.setTheme(resolvedTheme);
    // UI-014：主题切换后全量重绘所有已初始化图表（含空态检查）
    charts.retheme(chartRenderers());
  }

  function chartRenderers() {
    return {
      chartUsage: function () { charts.renderUsageChart("chartUsageBox", "chartUsage", state.dailyData); },
      chartTps: function () { charts.renderTpsChart("chartTpsBox", "chartTps", state.liveSamples); },
      chartMtp: function () { charts.renderMtpChart("chartMtpBox", "chartMtp", state.dailyData); },
      chartMtpPos: function () { charts.renderMtpPosChart("chartMtpPosBox", "chartMtpPos", state.mtpPositions); },
      chartGpuUtil: function () { charts.renderGpuUtilChart("chartGpuUtilBox", "chartGpuUtil", state.gpuLive, state.gpuVisible); },
      chartGpuPower: function () { charts.renderGpuPowerChart("chartGpuPowerBox", "chartGpuPower", state.gpuLive, state.gpuVisible); },
      chartGpuTemp: function () { charts.renderGpuTempChart("chartGpuTempBox", "chartGpuTemp", state.gpuLive, state.gpuVisible); },
    };
  }

  function bindSystemThemeListener() {
    if (!window.matchMedia) return;
    if (systemMq) {
      systemMq.removeEventListener("change", onSystemTheme);
      systemMq = null;
    }
    if (themeMode === "system") {
      systemMq = window.matchMedia("(prefers-color-scheme: light)");
      systemMq.addEventListener("change", onSystemTheme); // UI-001：实时跟随
    }
  }

  function onSystemTheme() {
    if (themeMode === "system") applyTheme("system");
  }

  function setThemeMode(mode) {
    applyTheme(mode);
    bindSystemThemeListener();
  }

  /* ================= 全局 InfoBar（Offline / Update，UI-015） ================= */
  var offlineBar = null;
  var updateBar = null;

  function showOfflineBar(lastTs) {
    var box = $("globalInfobars");
    if (!box) return;
    if (offlineBar) { offlineBar.close(); offlineBar = null; }
    offlineBar = ui.createInfoBar({
      type: "error",
      title: "llama.cpp is currently unreachable",
      message: "Last successful update: " + (lastTs ? F.formatTime(lastTs) : "unknown") +
        ". Live values show --; history is preserved.",
      dismissible: false,
    });
    offlineBar.el.id = "offlineInfoBar";
    box.insertBefore(offlineBar.el, box.firstChild);
  }

  function hideOfflineBar() {
    if (offlineBar) { offlineBar.close(); offlineBar = null; }
  }

  var updateBannerVisible = false;
  var updateBannerText = "";
  function setUpdateBanner(show, text) {
    var box = $("globalInfobars");
    if (!box) return;
    if (show && text === updateBannerText && updateBannerVisible) return;
    if (updateBar) { updateBar.close(); updateBar = null; }
    updateBannerVisible = show;
    updateBannerText = text || "";
    if (show) {
      updateBar = ui.createInfoBar({
        type: "info",
        title: text,
        message: "You can download and verify the update in Settings \u2192 Updates.",
        actions: [{
          label: "View Update",
          onClick: function () {
            updateBannerVisible = false;
            if (updateBar) { updateBar.close(); updateBar = null; }
            LM.nav.showPage("settings");
            LM.settings.goToSection("updates");
          },
        }],
        dismissible: true,
      });
      updateBar.el.id = "updateInfoBar";
      updateBar.el.addEventListener("click", function (ev) {
        if (ev.target.closest(".infobar-close")) {
          updateBannerVisible = false;
        }
      }, true);
      box.appendChild(updateBar.el);
    }
  }

  /* ================= Server Status（Overview + 全局） ================= */
  function setStatValue(id, text) {
    var el = $(id);
    if (!el) return;
    el.textContent = text;
    el.classList.toggle("dim", text === F.NA);
  }

  function applyStatus(data) {
    state.online = data.server_online === true ? true : (data.server_online === false ? false : null);
    state.config = data.config || null;

    // 全局状态徽章（Overview 顶部 + 页面内 server 卡）
    ui.setStatusBadge($("ovServerState"),
      state.online === true ? "online" : state.online === false ? "offline" : "paused",
      state.online === null ? "Starting" : undefined);

    // Offline InfoBar（spec §39：明确 offline，保留历史）
    if (state.online === false) {
      showOfflineBar(state.lastSuccessTs || data.last_update);
    } else {
      hideOfflineBar();
    }

    // 最后更新（stale 判断，spec §41）
    var lu = data.last_update;
    var elLast = $("ovLastUpdate");
    if (elLast) {
      if (!lu) {
        elLast.textContent = "Waiting for first sample...";
        elLast.className = "stat-hint";
      } else {
        var age = Math.max(0, Date.now() / 1000 - lu);
        var stale = state.online === true && age > cfgUi.refreshIntervalSeconds * 2;
        elLast.textContent = F.formatTime(lu) + " (" + F.formatAgo(age) + ")" + (stale ? " - stale" : "");
        elLast.className = "stat-hint" + (stale ? " warn" : "");
      }
    }

    // 服务器地址（config 优先，status 兜底）
    var elUrl = $("ovServerUrl");
    if (elUrl) {
      var url = (LM.settings && lastConfigUrl) || data.llama_server_url || "";
      elUrl.textContent = url ? url.replace(/^https?:\/\//, "") : "--";
    }

    // 配置状态（Config: OK / Default / Error，spec §110 数据库/配置问题明确化）
    var elCfg = $("ovConfigState");
    if (elCfg) {
      var cc = data.config;
      if (cc) {
        var label = cc.has_errors ? "Error" : (cc.using_defaults ? "Default" : "OK");
        elCfg.textContent = "Config: " + label;
        elCfg.className = "stat-hint " + (label === "Error" ? "bad" : label === "Default" ? "warn" : "ok");
        elCfg.title = (cc.path || "") + " (restart to apply changes)";
      } else {
        elCfg.textContent = "";
      }
    }

    // 当前速率（offline 或字段缺失 -> --）
    var on = state.online === true;
    setStatValue("ovPromptTps", on ? F.formatTps(data.prompt_tps) : F.NA);
    setStatValue("ovDecodeTps", on ? F.formatTps(data.decode_tps) : F.NA);
    setStatValue("ovContext", on ? F.formatTokenCount(data.context_max) : F.NA);
    setStatValue("ovRequests", on ? F.formatInt(data.requests_processing) : F.NA);
    setStatValue("ovQueued", on ? F.formatInt(data.requests_deferred) : F.NA);

    if (state.online === true && lu) state.lastSuccessTs = lu;
  }

  var lastConfigUrl = "";

  /* ================= Today / Month / Total（Usage 页，spec §12） ================= */
  function renderSummaryCards(summary) {
    var t = summary.today || {}, s = summary.total || {};
    // Overview 今日摘要（同一数据，摘要级数字）
    setStatValue("ovTodayLogical", F.formatTokenCount(t.logical_tokens));
    setStatValue("ovTodayCompute", F.formatTokenCount(t.compute_tokens));
    setStatValue("ovTodayPrompt", F.formatTokenCount(t.prompt_tokens));
    setStatValue("ovTodayCached", F.formatTokenCount(t.cached_tokens));
    setStatValue("ovTodayOutput", F.formatTokenCount(t.output_tokens));
    setStatValue("sumTodayLogical", F.formatTokenCount(t.logical_tokens));
    setStatValue("sumTodayCompute", F.formatTokenCount(t.compute_tokens));
    setStatValue("sumTodayPrompt", F.formatTokenCount(t.prompt_tokens));
    setStatValue("sumTodayCached", F.formatTokenCount(t.cached_tokens));
    setStatValue("sumTodayOutput", F.formatTokenCount(t.output_tokens));

    setStatValue("sumTotalLogical", F.formatTokenCount(s.logical_tokens));
    setStatValue("sumTotalCompute", F.formatTokenCount(s.compute_tokens));
    setStatValue("sumTotalPrompt", F.formatTokenCount(s.prompt_tokens));
    setStatValue("sumTotalCached", F.formatTokenCount(s.cached_tokens));
    setStatValue("sumTotalOutput", F.formatTokenCount(s.output_tokens));
    var denom = (s.prompt_tokens || 0) + (s.cached_tokens || 0);
    setStatValue("sumCacheRatio", denom > 0 ? F.formatPercent((s.cached_tokens || 0) / denom * 100) : F.NA);

    // tooltip 原始值（spec §66：卡片紧凑，hover 看精确值）
    setFullTip("sumTodayLogical", t.logical_tokens);
    setFullTip("sumTotalLogical", s.logical_tokens);
  }

  function setFullTip(id, v) {
    var el = $(id);
    if (el) el.title = F.formatTokenCountFull(v);
  }

  function renderMonthCard() {
    var now = new Date();
    var key = now.getFullYear() + "-" + String(now.getMonth() + 1).padStart(2, "0");
    if (state.monthMonth !== key || !state.monthRows) {
      api.get("/api/daily?days=31")
        .then(function (d) {
          state.monthRows = (d.days || []);
          state.monthMonth = key;
          fillMonth();
        })
        .catch(function (e) { console.warn("month daily failed:", e.message || e); });
      return;
    }
    fillMonth();
  }

  function fillMonth() {
    var key = state.monthMonth;
    var logical = 0, compute = 0, has = false;
    (state.monthRows || []).forEach(function (r) {
      if (String(r.date).indexOf(key) === 0) {
        has = true;
        logical += r.logical_tokens || 0;
        compute += r.compute_tokens || 0;
      }
    });
    setStatValue("sumMonthLogical", has ? F.formatTokenCount(logical) : F.NA);
    setStatValue("sumMonthCompute", has ? F.formatTokenCount(compute) : F.NA);
  }

  /* ================= Runtime（Overview 摘要 + Performance 页） ================= */
  function applyRuntime(d) {
    setStatValue("rtProcessing", F.formatInt(d.requests_processing));
    setStatValue("rtQueued", F.formatInt(d.requests_deferred));
    setStatValue("rtBusySlots", F.formatInt(d.busy_slots));
    setStatValue("rtTokenMax", F.formatTokenCount(d.n_tokens_max));
    var kv = d.kv_cache_usage_ratio;
    var elKv = $("rtKvCache");
    if (elKv) {
      elKv.textContent = kv == null ? F.NA : F.formatPercent(kv * 100);
      elKv.classList.toggle("dim", kv == null);
    }
  }

  /* ================= Data Quality（Overview 摘要 + History 页；UI-010 并行） ================= */
  function renderDataQuality() {
    var q = state.quality, h = state.health;
    if (!q && !h) return;
    if (h) {
      var dbEl = $("dqDb");
      if (dbEl) {
        var tone = h.database === "healthy" ? "ok" : h.database === "warning" ? "warn" : "bad";
        dbEl.textContent = h.database;
        dbEl.className = "stat-hint " + tone;
      }
      var hint = $("dqDbHint");
      if (hint) {
        hint.textContent = (h.journal_mode ? h.journal_mode.toUpperCase() + " \u00B7 " : "") + (h.database_detail || "");
        if (h.application === "degraded") hint.textContent += " \u00B7 PROTECTIVE MODE (read-only)";
      }
    }
    if (q) {
      var t = q.today || {};
      var covEl = $("dqCoverage");
      if (covEl) {
        var cov = t.monitoring_coverage_percent;
        covEl.textContent = cov == null ? F.NA : F.formatPercent(cov);
        covEl.className = "stat-hint " + (cov == null ? "" : cov >= 99.9 ? "ok" : cov >= 95 ? "warn" : "bad");
      }
      var gapsEl = $("dqGapsToday");
      if (gapsEl) {
        var gc = t.gap_count || 0;
        gapsEl.textContent = String(gc);
        gapsEl.className = "stat-hint " + (gc === 0 ? "ok" : t.possible_token_loss ? "bad" : "warn");
      }
      var lossEl = $("dqLossToday");
      if (lossEl) {
        lossEl.textContent = t.possible_token_loss ? "Possible token loss in gaps" : "No token-loss gaps";
        lossEl.className = "stat-hint " + (t.possible_token_loss ? "bad" : "");
      }
      var lastEl = $("dqLastSample");
      if (lastEl) lastEl.textContent = F.formatAgo(t.last_valid_sample_seconds_ago);

      var openText = q.open_gap ? "Open gap since " + F.formatDateTime(q.open_gap.start) +
        (q.open_gap.reason ? " (" + (GAP_REASON_LABELS[q.open_gap.reason] || q.open_gap.reason) + ")" : "") +
        " - in progress" : "";
      [["dqOpenGap"], ["hqOpenGap"]].forEach(function (pair) {
        var el = $(pair[0]);
        if (el) {
          el.textContent = openText;
          el.style.display = openText ? "" : "none";
        }
      });
      // History 页（同一数据源，独立元素）
      if ($("hqDb")) $("hqDb").textContent = (h && h.database) || "--";
      if ($("hqDbHint")) $("hqDbHint").textContent =
        (h && h.journal_mode ? h.journal_mode.toUpperCase() + " \u00B7 " : "") + (h && h.database_detail || "") +
        (h && h.application === "degraded" ? " \u00B7 PROTECTIVE MODE (read-only)" : "");
      if ($("hqCoverage")) $("hqCoverage").textContent = t.monitoring_coverage_percent == null ? F.NA : F.formatPercent(t.monitoring_coverage_percent);
      if ($("hqGaps")) $("hqGaps").textContent = (t.gap_count || 0) + " / " + ((q.total && q.total.gap_count) || 0);
      if ($("hqLoss")) $("hqLoss").textContent = (q.total && q.total.possible_token_loss)
        ? "Possible token loss exists in history" : "No token-loss gaps";
      if ($("hqLastSample")) $("hqLastSample").textContent = F.formatAgo(t.last_valid_sample_seconds_ago);
      renderGapsTable();
    }
  }

  var GAP_REASON_LABELS = {
    server_offline: "Server Offline",
    monitor_restart: "Monitor Restart",
    system_pause_or_sleep: "System Sleep / Pause",
    invalid_metrics: "Invalid Metrics",
    unknown: "Unknown",
  };
  var GAP_SOURCE_LABELS = { llama: "LLM", application: "App", gpu: "GPU" };

  function renderGapsTable() {
    var tbody = $("gapsTbody");
    if (!tbody) return;
    var gaps = (state.quality && state.quality.recent_gaps) || [];
    var wrap = $("gapsTableWrap");
    if (!gaps.length) {
      tbody.innerHTML = "<tr><td colspan='6' class='na'>No gaps recorded.</td></tr>";
      if (wrap) wrap.style.display = "";
      return;
    }
    tbody.innerHTML = gaps.map(function (g) {
      var start = F.formatDateTime(g.start);
      var end = g.end ? F.formatDateTime(g.end) : "in progress";
      var dur = F.formatDuration(g.duration_seconds == null ? 0 : g.duration_seconds);
      var src = GAP_SOURCE_LABELS[g.source] || g.source || "--";
      var reason = GAP_REASON_LABELS[g.reason] || g.reason || "Unknown";
      var loss = g.possible_token_loss ? "<td class='cell-bad'>Yes</td>" : "<td>No</td>";
      return "<tr><td>" + start + "</td><td>" + end + "</td><td>" + dur + "</td>" +
        "<td>" + src + "</td><td>" + reason + "</td>" + loss + "</tr>";
    }).join("");
  }

  /* ================= GPU 页（spec §135；UI-002 签名重建） ================= */
  function applyGpuStatus(d) {
    state.gpuStatus = d;
    var box = $("gpuCards");
    if (!box) return;
    // 可用性状态
    var stateEl = $("gpuPageState");
    if (stateEl) {
      ui.setStatusBadge(stateEl, d.available ? "online" : "offline",
        d.available ? undefined : "Unavailable");
      var reasonEl = $("gpuUnavailReason");
      if (reasonEl) {
        reasonEl.textContent = d.available ? "" : (d.reason || "nvidia-smi unavailable");
        reasonEl.style.display = d.available ? "none" : "";
      }
    }
    var gpus = d.available ? (d.gpus || []) : [];
    box.innerHTML = "";
    if (!gpus.length) {
      var empty = document.createElement("div");
      empty.className = "empty-state";
      empty.style.gridColumn = "1/-1";
      var ic = document.createElement("div");
      ic.className = "empty-icon";
      ic.innerHTML = LM.icons.get("emptyGauge");
      var tt = document.createElement("div");
      tt.className = "empty-title";
      tt.textContent = "No GPUs";
      var dd = document.createElement("div");
      dd.className = "empty-desc";
      dd.textContent = d.available ? "No GPU samples collected yet." : (d.reason || "GPU monitoring is unavailable.");
      empty.appendChild(ic);
      empty.appendChild(tt);
      empty.appendChild(dd);
      box.appendChild(empty);
      return;
    }
    gpus.forEach(function (g) {
      var card = document.createElement("div");
      card.className = "gpu-device";
      var head = document.createElement("div");
      head.className = "gpu-device-head";
      var idx = document.createElement("span");
      idx.className = "gpu-idx";
      idx.textContent = "GPU " + (g.index == null ? "?" : g.index);
      var name = document.createElement("span");
      name.className = "gpu-name";
      name.textContent = g.name || "";
      name.title = g.name || "";
      head.appendChild(idx);
      head.appendChild(name);
      card.appendChild(head);

      var kv = document.createElement("div");
      kv.className = "gpu-kv";
      var vram = (g.memory_used_mb == null || g.memory_total_mb == null) ? F.NA :
        (g.memory_used_mb / 1024).toFixed(1) + " / " + (g.memory_total_mb / 1024).toFixed(1) + " GiB" +
        (g.memory_usage_percent != null ? " (" + Math.round(g.memory_usage_percent) + "%)" : "");
      [
        ["Utilization", g.utilization_percent == null ? F.NA : F.formatPercent(g.utilization_percent, 0)],
        ["VRAM", vram],
        ["Temperature", F.formatTemp(g.temperature_c)],
        ["Power", F.formatPower(g.power_draw_w)],
        ["Fan", g.fan_percent == null ? F.NA : F.formatPercent(g.fan_percent, 0)],
        ["SM Clock", g.sm_clock_mhz == null ? F.NA : g.sm_clock_mhz + " MHz"],
        ["Memory Clock", g.memory_clock_mhz == null ? F.NA : g.memory_clock_mhz + " MHz"],
        ["PCIe", (g.pcie_generation == null || g.pcie_width == null) ? F.NA : "Gen" + g.pcie_generation + " x" + g.pcie_width],
      ].forEach(function (r) {
        var k = document.createElement("span");
        k.className = "k";
        k.textContent = r[0];
        var v = document.createElement("span");
        v.className = "v";
        v.textContent = r[1];
        kv.appendChild(k);
        kv.appendChild(v);
      });
      card.appendChild(kv);
      box.appendChild(card);
    });
    renderGpuPick(d.detected || []);
    renderGpuOverviewSummary(d);
  }

  /* Overview 页 GPU 摘要（与 GPU 页同一数据源） */
  function renderGpuOverviewSummary(d) {
    var stateEl = $("ovGpuState");
    var lineEl = $("ovGpuLine");
    if (!stateEl || !lineEl) return;
    if (!d.available) {
      ui.setStatusBadge(stateEl, "offline", "Unavailable");
      lineEl.textContent = d.reason || "nvidia-smi unavailable";
      return;
    }
    var gpus = d.gpus || [];
    ui.setStatusBadge(stateEl, "online", gpus.length + " GPU" + (gpus.length === 1 ? "" : "s"));
    if (!gpus.length) {
      lineEl.textContent = "Available, no samples yet.";
      return;
    }
    lineEl.textContent = gpus.map(function (g) {
      var u = g.utilization_percent == null ? F.NA : F.formatPercent(g.utilization_percent, 0);
      var t = g.temperature_c == null ? F.NA : F.formatTemp(g.temperature_c);
      var p = g.power_draw_w == null ? F.NA : F.formatPower(g.power_draw_w);
      var v = (g.memory_used_mb == null || g.memory_total_mb == null) ? F.NA :
        (g.memory_used_mb / 1024).toFixed(1) + " / " + (g.memory_total_mb / 1024).toFixed(1) + " GiB";
      return "GPU " + (g.index == null ? "?" : g.index) + ": " + u + " util, " + v + " VRAM, " + t + ", " + p;
    }).join("  \u00B7  ");
  }

  function renderGpuPick(detected) {
    var box = $("gpuPick");
    if (!box) return;
    var sig = JSON.stringify((detected || []).map(function (g) { return g.uuid + "|" + g.index; }));
    if (sig === state.gpuPickSig && box.children.length) return; // UI-002：未变不重建
    state.gpuPickSig = sig;
    box.innerHTML = "";
    if (!detected || detected.length < 2) {
      box.style.display = "none";
      return;
    }
    box.style.display = "";
    detected.forEach(function (g) {
      if (state.gpuVisible[g.uuid] === undefined) state.gpuVisible[g.uuid] = true;
      var label = document.createElement("label");
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = state.gpuVisible[g.uuid];
      cb.addEventListener("change", function () {
        state.gpuVisible[g.uuid] = cb.checked;
        redrawGpuCharts();
      });
      label.appendChild(cb);
      label.appendChild(document.createTextNode(" GPU " + (g.index == null ? "?" : g.index) + " " + (g.name || "")));
      box.appendChild(label);
    });
  }

  function redrawGpuCharts() {
    charts.renderGpuUtilChart("chartGpuUtilBox", "chartGpuUtil", state.gpuLive, state.gpuVisible);
    charts.renderGpuPowerChart("chartGpuPowerBox", "chartGpuPower", state.gpuLive, state.gpuVisible);
    charts.renderGpuTempChart("chartGpuTempBox", "chartGpuTemp", state.gpuLive, state.gpuVisible);
  }

  function applyGpuLive(d) {
    state.gpuLive = d;
    (d.gpus || []).forEach(function (g) {
      if (state.gpuVisible[g.uuid] === undefined) state.gpuVisible[g.uuid] = true;
    });
    redrawGpuCharts();
  }

  function renderGpuEnergy(d) {
    var box = $("gpuEnergy");
    if (!box) return;
    box.innerHTML = "";
    var gpus = (d && d.gpus) || [];
    var today = new Date();
    var key = today.getFullYear() + "-" + String(today.getMonth() + 1).padStart(2, "0") + "-" +
      String(today.getDate()).padStart(2, "0");
    var any = false;
    gpus.forEach(function (g) {
      var day = (g.days || []).filter(function (x) { return x.date === key; })[0];
      if (!day || day.energy_wh == null) return;
      any = true;
      var row = document.createElement("div");
      row.className = "energy-row";
      var k = document.createElement("span");
      k.className = "k";
      k.textContent = "GPU " + (g.name ? g.name : "") + " - Energy Today (estimated)";
      var v = document.createElement("span");
      v.className = "v";
      v.textContent = F.formatEnergy(day.energy_wh);
      row.appendChild(k);
      row.appendChild(v);
      box.appendChild(row);
    });
    if (!any) {
      var empty = document.createElement("div");
      empty.className = "empty-state";
      var ic = document.createElement("div");
      ic.className = "empty-icon";
      ic.innerHTML = LM.icons.get("emptyGauge");
      var tt = document.createElement("div");
      tt.className = "empty-title";
      tt.textContent = "No energy data";
      var dd = document.createElement("div");
      dd.className = "empty-desc";
      dd.textContent = "Energy is estimated from sampled power draw; it appears after the first samples.";
      empty.appendChild(ic);
      empty.appendChild(tt);
      empty.appendChild(dd);
      box.appendChild(empty);
    }
  }

  /* ================= MTP（Performance 页；AUDIT-DATA-002 单一来源） ================= */
  function applyMtp(d) {
    state.mtp = d;
    state.mtpPositions = d.positions || [];
    setStatValue("mtpAcceptRate", F.formatPercent(d.accept_rate));
    setStatValue("mtpDraft", F.formatTokenCount(d.draft_tokens));
    setStatValue("mtpAccepted", F.formatTokenCount(d.accepted_tokens));
    setStatValue("mtpSeqs", F.formatInt(d.num_drafts));
    charts.renderMtpPosChart("chartMtpPosBox", "chartMtpPos", state.mtpPositions);
  }

  /* ================= Usage 页：图表 + 每日表（spec §133/§67） ================= */
  function renderDailyTable() {
    var tbody = $("dailyTbody");
    if (!tbody) return;
    var rows = state.dailyData;
    if (!rows.length) {
      tbody.innerHTML = "<tr><td colspan='8' class='na'>No data yet.</td></tr>";
      return;
    }
    tbody.innerHTML = rows.map(function (r) {
      var cov = r.monitoring_coverage_percent;
      var covTd = cov == null ? "<td>" + F.NA + "</td>" :
        "<td class='" + (cov >= 99.9 ? "cell-ok" : cov >= 95 ? "cell-warn" : "cell-bad") + "'>" + F.formatPercent(cov) + "</td>";
      var gaps = r.gap_count || 0;
      var loss = r.possible_token_loss;
      var gapsTd = "<td class='" + (gaps === 0 ? "cell-ok" : loss ? "cell-bad" : "cell-warn") + "'>" + gaps + "</td>";
      return "<tr><td>" + r.date + "</td>" +
        "<td>" + F.formatTokenCount(r.prompt_tokens) + "</td>" +
        "<td>" + F.formatTokenCount(r.cached_tokens) + "</td>" +
        "<td>" + F.formatTokenCount(r.output_tokens) + "</td>" +
        "<td>" + F.formatTokenCount(r.logical_tokens) + "</td>" +
        "<td>" + F.formatTokenCount(r.compute_tokens) + "</td>" + covTd + gapsTd + "</tr>";
    }).join("");
  }

  function refreshDaily() {
    return api.get("/api/daily?days=" + state.dailyRange)
      .then(function (d) {
        state.dailyData = d.days || [];
        charts.renderUsageChart("chartUsageBox", "chartUsage", state.dailyData);
        charts.renderMtpChart("chartMtpBox", "chartMtp", state.dailyData);
        renderDailyTable();
      })
      .catch(function (e) { console.warn("daily failed:", e.message || e); });
  }

  function refreshLive() {
    return api.get("/api/live?minutes=60")
      .then(function (d) {
        state.liveSamples = d.samples || [];
        charts.renderTpsChart("chartTpsBox", "chartTps", state.liveSamples);
      })
      .catch(function (e) { console.warn("live failed:", e.message || e); });
  }

  function refreshSummary() {
    return api.get("/api/summary")
      .then(renderSummaryCards)
      .catch(function (e) { console.warn("summary failed:", e.message || e); });
  }

  function refreshStatus() {
    return api.get("/api/status")
      .then(applyStatus)
      .catch(function (e) {
        // 后端不可达（区别于 llama 离线）：保留上次数据 + 提示（spec §129）
        console.warn("status failed:", e.message || e);
        var el = $("ovLastUpdate");
        if (el) {
          el.textContent = "Backend unreachable (" + (e.message || "network") + ")";
          el.className = "stat-hint bad";
        }
      });
  }

  function refreshRuntime() {
    return api.get("/api/runtime").then(applyRuntime)
      .catch(function (e) { console.warn("runtime failed:", e.message || e); });
  }

  function refreshDataQuality() {
    // UI-010：并行（原串行两跳）
    return Promise.allSettled([api.get("/api/data/quality"), api.get("/api/health")])
      .then(function (rs) {
        if (rs[0].status === "fulfilled") state.quality = rs[0].value;
        if (rs[1].status === "fulfilled") state.health = rs[1].value;
        renderDataQuality();
      });
  }

  function refreshGpuStatus() {
    return api.get("/api/gpu/status").then(applyGpuStatus)
      .catch(function (e) { console.warn("gpu status failed:", e.message || e); });
  }

  function refreshGpuLive() {
    return api.get("/api/gpu/live?minutes=" + state.gpuRangeMinutes).then(applyGpuLive)
      .catch(function (e) { console.warn("gpu live failed:", e.message || e); });
  }

  function refreshGpuDaily() {
    return api.get("/api/gpu/daily?days=1").then(renderGpuEnergy)
      .catch(function (e) { console.warn("gpu daily failed:", e.message || e); });
  }

  function refreshMtp() {
    return api.get("/api/mtp").then(applyMtp)
      .catch(function (e) { console.warn("mtp failed:", e.message || e); });
  }

  /* ================= 启动 ================= */

  async function loadConfig() {
    try {
      var c = await api.get("/api/config");
      if (c.ui) {
        cfgUi.refreshIntervalSeconds = c.ui.refresh_interval_seconds || 5;
        cfgUi.dailyDefaultDays = c.ui.daily_default_days || 30;
        cfgUi.theme = c.ui.theme || "system";
      }
      lastConfigUrl = (c.llama_server && c.llama_server.url) || "";
      if (lastConfigUrl) $("ovServerUrl").textContent = lastConfigUrl.replace(/^https?:\/\//, "");
      setThemeMode(cfgUi.theme);
      state.dailyRange = cfgUi.dailyDefaultDays;
      // Usage 页 range 控件初始值（1/7/30/365 档位；配置值映射到最近档位）
      var rangeEl = $("usageRange");
      if (rangeEl && LM.nav) {
        var options = [
          { value: 1, label: "Today" },
          { value: 7, label: "7 Days" },
          { value: 30, label: "30 Days" },
          { value: 365, label: "All" },
        ];
        var nearest = options.reduce(function (a, b) {
          return Math.abs(b.value - state.dailyRange) < Math.abs(a.value - state.dailyRange) ? b : a;
        });
        ui.segmented(rangeEl, options, nearest.value, function (v) {
          state.dailyRange = v;
          refreshDaily();
        });
      }
      var gpuRangeEl = $("gpuRange");
      if (gpuRangeEl) {
        ui.segmented(gpuRangeEl, [
          { value: 15, label: "15 min" },
          { value: 60, label: "1 hour" },
          { value: 360, label: "6 hours" },
          { value: 1440, label: "24 hours" },
        ], state.gpuRangeMinutes, function (v) {
          state.gpuRangeMinutes = v;
          refreshGpuLive();
        });
      }
    } catch (e) {
      console.warn("config load failed (defaults used):", e.message || e);
      setThemeMode("system");
    }
  }

  function init() {
    // 导航图标（本地 SVG，spec §9）+ 品牌标识（spec §115）
    document.querySelectorAll(".nav-icon[data-icon]").forEach(function (el) {
      el.innerHTML = LM.icons.get(el.getAttribute("data-icon"));
    });
    var brand = $("brandIcon");
    if (brand) brand.innerHTML = LM.icons.brand();
    // 指标定义 InfoTooltip（spec §61-63）
    document.querySelectorAll(".info-tip-slot").forEach(function (el) {
      var tip = LM.ui.infoTip(el.getAttribute("data-tip"), el.getAttribute("data-align") === "right");
      el.replaceWith(tip);
    });
    // 导航点击
    document.querySelectorAll(".nav-item").forEach(function (b) {
      b.addEventListener("click", function () { LM.nav.showPage(b.getAttribute("data-page")); });
    });
    LM.nav.initCompact();
    // Settings 事件绑定（保存/重置/测试连接/dirty 标记/主题切换/自动启动/危险操作/更新）
    if (LM.settings && LM.settings.init) LM.settings.init();

    // 页面钩子（spec §46：startPage/stopPage 语义）
    LM.nav.registerPage("overview", function () {
      refreshStatus(); refreshSummary(); renderMonthCard(); refreshRuntime(); refreshDataQuality();
    });
    LM.nav.registerPage("usage", function () {
      charts.ensurePageCharts(["chartUsage"]);
      refreshDaily(); refreshSummary(); renderMonthCard();
    });
    LM.nav.registerPage("performance", function () {
      charts.ensurePageCharts(["chartTps", "chartMtp", "chartMtpPos"]);
      refreshLive(); refreshMtp(); refreshRuntime();
    });
    LM.nav.registerPage("gpu", function () {
      charts.ensurePageCharts(["chartGpuUtil", "chartGpuPower", "chartGpuTemp"]);
      refreshGpuStatus(); refreshGpuLive(); refreshGpuDaily();
    });
    LM.nav.registerPage("history", function () {
      refreshDataQuality(); refreshDaily();
    });
    LM.nav.registerPage("settings", function () {
      if (!LM.settings.isLoaded()) LM.settings.loadSettings();
      else LM.settings.loadAppIntegration();
    });
    LM.nav.registerPage("about", function () {
      LM.settings.loadAbout();
    });

    // 轮询任务注册（spec §47 中央调度器；间隔来自 config）
    var R = Math.max(1, cfgUi.refreshIntervalSeconds) * 1000;
    LM.poll.register("status", { intervalMs: R, visibleOnly: true, run: refreshStatus });
    LM.poll.register("runtime", { intervalMs: R, visibleOnly: true, run: refreshRuntime });
    LM.poll.register("gpuStatus", { intervalMs: R, visibleOnly: true, run: refreshGpuStatus });
    LM.poll.register("gpuLive", { intervalMs: 15000, visibleOnly: true, run: refreshGpuLive });
    LM.poll.register("summary", { intervalMs: 30000, visibleOnly: false, run: refreshSummary });
    LM.poll.register("live", { intervalMs: 30000, visibleOnly: false, run: refreshLive });
    LM.poll.register("dataQuality", { intervalMs: 30000, visibleOnly: false, run: refreshDataQuality });
    LM.poll.register("gpuDaily", { intervalMs: 60000, visibleOnly: false, run: refreshGpuDaily });
    LM.poll.register("mtp", { intervalMs: 60000, visibleOnly: false, run: refreshMtp });
    LM.poll.register("daily", { intervalMs: 120000, visibleOnly: false, run: refreshDaily });
    // Updates：30s 全局（驱动横幅）+ 1s 仅在 Updates 分区（下载进度，UI-023 统一进调度器）
    LM.poll.register("updates", { intervalMs: 30000, visibleOnly: false, run: LM.settings.loadUpdateStatus });
    LM.poll.register("updatesProgress", {
      intervalMs: 1000, visibleOnly: true,
      run: function () {
        if (LM.nav.currentPage() === "settings" && LM.settings.activeSection() === "updates") {
          return LM.settings.loadUpdateStatus();
        }
      },
    });

    (async function () {
      await loadConfig();
      LM.poll.bindBrowserVisibility();
      // 初始加载：Overview 是默认页——其数据刷新由 showPage 的 onShow 钩子统一触发
      // （避免双重 fetch）；这里只预热 Usage 图（daily）与更新状态横幅。
      refreshDaily();
      LM.settings.loadUpdateStatus();
      LM.nav.showPage("overview");
      LM.poll.startAll();
    })();
  }

  window.LM = window.LM || {};
  LM.app = {
    init: init,
    applyTheme: function (mode) { setThemeMode(mode); },
    setUpdateBanner: setUpdateBanner,
    refreshLiveNow: function () { refreshLive(); },
    refreshSummaryNow: function () { refreshSummary(); },
  };

  // DOM ready
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
