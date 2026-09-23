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
    dailyDefaultDays: 7,   // 与服务端默认一致（16D：默认 7 天）——
    theme: "system",
  };

  /* ================= 全局状态 ================= */
  var state = {
    online: null,          // null=尚未取得 / true / false
    lastSuccessTs: null,   // 最近一次 server_online===true 的 last_update（epoch s）
    config: null,          // /api/status 的 config 块
    dailyData: [],         // /api/daily 行（当前范围）
    dailyRange: 30,        // Usage 页当前范围天数（all 模式不使用）
    dailyRangeMode: "7d", // Phase 16B：today | 7d | 30d | month | all（16D：默认 7 天）
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
    events: [],            // /api/events（History 页监控事件）
    lastUpdateTs: null,    // 最近一次采样的 last_update（epoch s）
    lastStatusRefresh: 0,  // 最近一次 /api/status 轮询完成时刻（epoch ms）— 倒计时基准
    statusBackendOk: true, // 后端（非 llama）是否可达
    ovSummary: null,       // 最近一次 /api/summary（Overview 今日卡）
    lossBar: null,         // possible_token_loss InfoBar 实例
    serverUrlText: "",     // Phase 16C §21：当前服务器地址（设置页连接状态复用）
  };

  /* ---- 状态条 1s ticker（Phase 16B spec §7：信息层级调整） ----
     主信息：在线 → "最后更新 X 秒前"（来自 lastUpdateTs）；
             离线 → "最后成功采样: X 分钟前"（lastSuccessTs）。
     次要信息：倒计时 "x 秒后刷新"（text-disabled 色，降级为辅助信息）。
     后端不可达：主信息提示后端不可达。 */
  function updateLastUpdateText() {
    var el = document.getElementById("ovLastUpdate");
    if (!el) return;
    // 16D：常态（在线）不显示"最后更新 X 秒前 / X 秒后刷新"（5s 轮询信息量低）；
    // 仅离线/后端不可达时显示说明。
    if (!state.statusBackendOk) {
      el.textContent = "后端不可达，正在重试...";
      el.className = "stat-hint bad";
      return;
    }
    if (state.online === false) {
      var ref = state.lastSuccessTs || state.lastUpdateTs;
      el.textContent = ref ? "最后成功采样：" + F.formatAgo(Math.floor(Date.now() / 1000) - ref) : "";
      el.className = "stat-hint warn";
    } else {
      el.textContent = "";
      el.className = "stat-hint";
    }
  }
  setInterval(updateLastUpdateText, 1000);

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
      title: "llama.cpp 当前无法连接",
      message: "最近一次成功更新：" + (lastTs ? F.formatTime(lastTs) : "unknown") +
        "。实时值显示 --；历史数据已保留。",
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
        message: "可在 设置 \u2192 更新 中下载并验证更新。",
        actions: [{
          label: "查看更新",
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
      state.online === null ? "启动中" : undefined);

    // Offline InfoBar（spec §39：明确 offline，保留历史）
    if (state.online === false) {
      showOfflineBar(state.lastSuccessTs || data.last_update);
    } else {
      hideOfflineBar();
    }

    // 记录最近采样时刻（离线条/lastSuccessTs 用）；顶部"X 秒后刷新"倒计时
    // 由 1s ticker 依据 refreshStatus 的轮询时机驱动
    state.lastUpdateTs = data.last_update || null;

    // 服务器地址（config 优先，status 兜底）
    var elUrl = $("ovServerUrl");
    if (elUrl) {
      var url = (LM.settings && lastConfigUrl) || data.llama_server_url || "";
      state.serverUrlText = url ? url.replace(/^https?:\/\//, "") : "";
      elUrl.textContent = state.serverUrlText || "--";
    }

    // 配置状态（Config: OK / Default / Error，spec §110 数据库/配置问题明确化）
    var elCfg = $("ovConfigState");
    if (elCfg) {
      var cc = data.config;
      if (cc) {
        var label = cc.has_errors ? "错误" : (cc.using_defaults ? "默认" : "正常");
        elCfg.textContent = "配置：" + label;
        elCfg.className = "stat-hint " + (label === "错误" ? "bad" : label === "默认" ? "warn" : "ok");
        elCfg.title = (cc.path || "") + "（重启后生效）";
      } else {
        elCfg.textContent = "";
      }
    }

    // 当前速率（offline 或字段缺失 -> --）
    var on = state.online === true;
    setStatValue("ovPromptTps", on && data.prompt_tps != null ? F.formatTokenCount(data.prompt_tps) + " t/s" : F.NA);
    setStatValue("ovDecodeTps", on && data.decode_tps != null ? F.formatTokenCount(data.decode_tps) + " t/s" : F.NA);
    setStatValue("ovContext", on ? F.formatTokenCount(data.context_max) : F.NA);
    setStatValue("ovRequests", on ? F.formatInt(data.requests_processing) + " / " + F.formatInt(data.requests_deferred) : F.NA);
    // Performance 页指标条 + 运行卡（spec §25/§26）
    setStatValue("perfPromptTps", on && data.prompt_tps != null ? F.formatTokenCount(data.prompt_tps) + " t/s" : F.NA);
    setStatValue("perfDecodeTps", on && data.decode_tps != null ? F.formatTokenCount(data.decode_tps) + " t/s" : F.NA);
    setStatValue("rtContextMax", data.context_max != null ? F.formatTokenCount(data.context_max) : F.NA);

    if (state.online === true && state.lastUpdateTs) state.lastSuccessTs = state.lastUpdateTs;
  }

  var lastConfigUrl = "";

  /* ================= Summary（Overview 今日卡 + Usage 范围摘要；BUG-A 修复） =================
     /api/summary 现由后端计算 today 与 month（同一 local_date 来源），
     前端不再做浏览器本地月份前缀过滤。 */
  function renderSummaryCards(summary) {
    state.ovSummary = summary;
    var t = summary.today || {};
    // Overview 今日摘要（同一数据，摘要级数字）
    setStatValue("ovTodayLogical", F.formatTokenCount(t.logical_tokens));
    setStatValue("ovTodayCompute", F.formatTokenCount(t.compute_tokens));
    setStatValue("ovTodayPrompt", F.formatTokenCount(t.prompt_tokens));
    setStatValue("ovTodayCached", F.formatTokenCount(t.cached_tokens));
    setStatValue("ovTodayOutput", F.formatTokenCount(t.output_tokens));

    // Usage 范围摘要卡
    renderRangeSummary();
  }

  function setFullTip(id, v) {
    var el = $(id);
    if (el) el.title = F.formatTokenCountFull(v);
  }

  /* Usage 范围摘要：按当前 dailyRangeMode 选择数据源
     today   -> summary.today
     month   -> summary.month（BUG-A 修复：后端计算，month_key 一致）
     all     -> summary.total
     7d/30d  -> 对当前已加载的 daily 行求和 */
  function renderRangeSummary() {
    var labelEl = $("sumRangeLabel");
    if (!labelEl) return;
    var s = state.ovSummary || {}, rows = state.dailyData || [];
    var src = null, label = "";
    var mode = state.dailyRangeMode || "30d";
    if (mode === "today") { src = s.today || {}; label = "今日"; }
    else if (mode === "month") {
      src = s.month || {}; label = "本月";
      if (s.month_key) label = "本月（" + s.month_key + "）";
    }
    else if (mode === "all") { src = s.total || {}; label = "全部（累计）"; }
    else {
      var span = mode === "7d" ? 7 : 30;
      var logical = 0, compute = 0, prompt = 0, cached = 0, output = 0, n = 0;
      for (var i = 0; i < rows.length; i++) {
        var r = rows[i];
        logical += r.logical_tokens || 0; compute += r.compute_tokens || 0;
        prompt += r.prompt_tokens || 0; cached += r.cached_tokens || 0;
        output += r.output_tokens || 0; n++;
      }
      src = { logical_tokens: logical, compute_tokens: compute, prompt_tokens: prompt,
              cached_tokens: cached, output_tokens: output };
      label = (mode === "7d" ? "近 7 天" : "近 30 天") + "（" + n + " 天有数据）";
    }
    labelEl.textContent = label;
    // USAGE-001：标题即范围名，无独立徽标
    setStatValue("sumRangeLogical", F.formatTokenCount(src.logical_tokens));
    setStatValue("sumRangeCompute", F.formatTokenCount(src.compute_tokens));
    setStatValue("sumRangePrompt", F.formatTokenCount(src.prompt_tokens));
    setStatValue("sumRangeCached", F.formatTokenCount(src.cached_tokens));
    setStatValue("sumRangeOutput", F.formatTokenCount(src.output_tokens));
    var denom = (src.prompt_tokens || 0) + (src.cached_tokens || 0);
    setStatValue("sumCacheRatio", denom > 0 ? F.formatPercent((src.cached_tokens || 0) / denom * 100) : F.NA);
    setFullTip("sumRangeLogical", src.logical_tokens);
  }

  /* ================= Runtime（Performance 页 + Overview 摘要） ================= */
  function applyRuntime(d) {
    setStatValue("rtProcessing", F.formatInt(d.requests_processing));
    setStatValue("rtQueued", F.formatInt(d.requests_deferred));
    setStatValue("rtBusySlots", F.formatInt(d.busy_slots));
    // "最大 Token 记录"（n_tokens_max）——不是上下文使用量（spec §26 命名）
    setStatValue("rtTokenMax", F.formatTokenCount(d.n_tokens_max));
    var kv = d.kv_cache_usage_ratio;
    var kvText = kv == null ? F.NA : F.formatPercent(kv * 100);
    var elKv = $("rtKvCache");
    if (elKv) {
      elKv.textContent = kvText;
      elKv.classList.toggle("dim", kv == null);
    }
    setStatValue("ovKvCache", kvText); // Overview 运行状态卡（0.16.12）
    // 指标条（Phase 16C §5：Prompt TPS/Decode TPS/处理中/排队/Busy Slots，
    // 顶部不再重复 MTP 接受率——下方 MTP 卡已有完整 Summary）
    setStatValue("perfProcessing", F.formatInt(d.requests_processing));
    setStatValue("perfQueued", F.formatInt(d.requests_deferred));
    setStatValue("perfBusySlots", F.formatInt(d.busy_slots));
  }

  /* ================= Data Quality（Overview 摘要 + History 页；UI-010 并行） ================= */
  function renderDataQuality() {
    var q = state.quality, h = state.health;
    if (!q && !h) return;
    // 16D：数值元素只叠加语义色 tone，保留字号类（mid 22px），
    // 此前整段覆写 className 会把字号类吞掉退化成 12px hint
    if (h) {
      var dbEl = $("dqDb");
      if (dbEl) {
        var tone = h.database === "healthy" ? "ok" : h.database === "warning" ? "warn" : "bad";
        dbEl.textContent = h.database;
        dbEl.className = "stat-value mid " + tone;
      }
      var hint = $("dqDbHint");
      if (hint) {
        hint.textContent = (h.journal_mode ? h.journal_mode.toUpperCase() + " \u00B7 " : "") + (h.database_detail || "");
        if (h.application === "degraded") hint.textContent += " \u00B7 保护模式（只读）";
      }
    }
    if (q) {
      var t = q.today || {};
      var covEl = $("dqCoverage");
      if (covEl) {
        var cov = t.monitoring_coverage_percent;
        covEl.textContent = cov == null ? F.NA : F.formatPercent(cov);
        covEl.className = "stat-value mid" + (cov == null ? "" : " " + (cov >= 99.9 ? "ok" : cov >= 95 ? "warn" : "bad"));
      }
      var gapsEl = $("dqGapsToday");
      if (gapsEl) {
        var gc = t.gap_count || 0;
        gapsEl.textContent = String(gc);
        gapsEl.className = "stat-value mid " + (gc === 0 ? "ok" : t.possible_token_loss ? "bad" : "warn");
      }
      var lossEl = $("dqLossToday");
      if (lossEl) {
        lossEl.textContent = t.possible_token_loss ? "缺口可能存在 Token 丢失" : "无 Token 丢失缺口";
        lossEl.className = "stat-hint " + (t.possible_token_loss ? "bad" : "");
      }
      var openText = q.open_gap ? "持续缺口，始于 " + F.formatDateTime(q.open_gap.start) +
        (q.open_gap.reason ? "（" + (GAP_REASON_LABELS[q.open_gap.reason] || q.open_gap.reason) + "）" : "") +
        "，进行中" : "";
      // 概览数据质量卡：open gap 提示在卡底部（16D 从"最近有效采样"项移出）
      var dqEl = $("dqOpenGap");
      if (dqEl) dqEl.textContent = openText;
      var wrap = $("dqOpenGapWrap");
      if (wrap) wrap.hidden = !openText;
      // 历史页（独立元素）
      var hqEl = $("hqOpenGap");
      if (hqEl) {
        hqEl.textContent = openText;
        hqEl.style.display = openText ? "" : "none";
      }
      // History 页（同一数据源，独立元素）
      if ($("hqDb")) $("hqDb").textContent = (h && h.database) || "--";
      if ($("hqDbHint")) $("hqDbHint").textContent =
        (h && h.journal_mode ? h.journal_mode.toUpperCase() + " \u00B7 " : "") + (h && h.database_detail || "") +
        (h && h.application === "degraded" ? " \u00B7 保护模式（只读）" : "");
      if ($("hqCoverage")) $("hqCoverage").textContent = t.monitoring_coverage_percent == null ? F.NA : F.formatPercent(t.monitoring_coverage_percent);
      if ($("hqGaps")) $("hqGaps").textContent = (t.gap_count || 0) + " / " + ((q.total && q.total.gap_count) || 0);
      if ($("hqLoss")) $("hqLoss").textContent = (q.total && q.total.possible_token_loss)
        ? "历史中存在 Token 丢失缺口" : "无 Token 丢失缺口";
      if ($("hqLastSample")) $("hqLastSample").textContent = F.formatAgo(t.last_valid_sample_seconds_ago);
      renderGapsTable();
      renderTokenLossBar();
    }
  }

  /* possible_token_loss：Win11 InfoBar（spec §7：提示级，不整页变红） */
  function renderTokenLossBar() {
    var box = $("ovTokenLossBar");
    if (!box) return;
    var loss = state.quality && state.quality.today && state.quality.today.possible_token_loss;
    if (!loss) {
      if (state.lossBar) { state.lossBar.close(); state.lossBar = null; }
      box.innerHTML = "";
      return;
    }
    if (state.lossBar) { state.lossBar.close(); state.lossBar = null; }
    state.lossBar = ui.createInfoBar({
      type: "warning",
      title: "历史 Token 统计可能不完整",
      message: "今日存在监控缺口，期间产生的 Token 可能未被统计。缺口详情见 历史 页。",
      dismissible: true,
    });
    box.appendChild(state.lossBar.el);
  }

  var GAP_REASON_LABELS = {
    server_offline: "服务器离线",
    monitor_restart: "监控重启",
    system_pause_or_sleep: "系统睡眠/暂停",
    invalid_metrics: "无效指标",
    unknown: "未知",
  };
  var GAP_SOURCE_LABELS = { llama: "LLM", application: "应用", gpu: "GPU" };

  function renderGapsTable() {
    var tbody = $("gapsTbody");
    if (!tbody) return;
    var gaps = (state.quality && state.quality.recent_gaps) || [];
    var wrap = $("gapsTableWrap");
    var empty = $("gapsEmpty");
    // HISTORY-001（Phase 16C §13/§16）：空态条件只看 gaps.length===0。
    // 根因：此前仅设 empty.hidden=true，但 author CSS 的 display:flex
    // 压过 UA 的 [hidden]{display:none}，导致有数据时空态仍显示。
    // 现统一走 ui.setEmptyState（force-hide/force-show + !important）。
    ui.setEmptyState(empty, gaps.length === 0);
    if (!gaps.length) {
      // 真空态（spec §45）：✓ 暂无已知监控缺口
      tbody.innerHTML = "";
      if (wrap) wrap.style.display = "none";
      if (empty) {
        var ic = empty.querySelector(".empty-icon");
        if (ic && !ic.innerHTML) ic.innerHTML = LM.icons.get("check");
      }
      return;
    }
    if (wrap) wrap.style.display = "";
    // 紧凑单行时间（今天 HH:MM:SS / 跨天 MM-DD HH:MM），完整值进 tooltip
    tbody.innerHTML = gaps.map(function (g) {
      var clock = F.formatClock || F.formatDateTime; // 16D 兜底：浏览器混装新旧 JS 时不抛错
      var start = clock(g.start);
      var startFull = F.formatDateTime(g.start);
      var end = g.end ? clock(g.end) : "进行中";
      var endFull = g.end ? F.formatDateTime(g.end) : "进行中的缺口（尚未结束）";
      var dur = F.formatDuration(g.duration_seconds == null ? 0 : g.duration_seconds);
      var src = GAP_SOURCE_LABELS[g.source] || g.source || "--";
      var reason = GAP_REASON_LABELS[g.reason] || g.reason || "未知";
      var loss = g.possible_token_loss ? "<td class='cell-bad'>是</td>" : "<td>否</td>";
      return "<tr>" +
        "<td title='" + startFull + "'>" + start + "</td>" +
        "<td title='" + endFull + "'>" + end + "</td>" +
        "<td>" + dur + "</td>" +
        "<td>" + src + "</td>" +
        "<td class='cell-wrap' title='" + reason + "'>" + reason + "</td>" +
        loss + "</tr>";
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
        d.available ? undefined : "不可用");
      var reasonEl = $("gpuUnavailReason");
      if (reasonEl) {
        reasonEl.textContent = d.available ? "" : (d.reason || "nvidia-smi 不可用");
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
      tt.textContent = "无 GPU";
      var dd = document.createElement("div");
      dd.className = "empty-desc";
      dd.textContent = d.available ? "尚未采集到 GPU 样本。" : (d.reason || "GPU 监控不可用。");
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

      // 主指标（spec §36 + Phase 16C §10/GPU-002：显存行内嵌进度条，
      // 进度条视觉上归属于"显存"，不再游离在主指标与温度/功耗之间）
      var vram = (g.memory_used_mb == null || g.memory_total_mb == null) ? F.NA :
        (g.memory_used_mb / 1024).toFixed(1) + " / " + (g.memory_total_mb / 1024).toFixed(1) + " GiB";
      var primary = [
        ["利用率", g.utilization_percent == null ? F.NA : F.formatPercent(g.utilization_percent, 0)],
        ["显存", vram],
        ["温度", F.formatTemp(g.temperature_c)],
        ["功耗", F.formatPower(g.power_draw_w)],
      ];
      primary.forEach(function (r) {
        var row = document.createElement("div");
        row.className = "gd-primary";
        var k = document.createElement("span");
        k.className = "k";
        k.textContent = r[0];
        var v = document.createElement("span");
        v.className = "v" + (r[1] === F.NA ? " dim" : "");
        v.textContent = r[1];
        row.appendChild(k);
        row.appendChild(v);
        card.appendChild(row);
        // GPU-002：VRAM 进度条紧跟在"显存"数值下方（同一行的子元素）
        if (r[0] === "显存" && g.memory_usage_percent != null) {
          var bar = document.createElement("div");
          bar.className = "vram-bar";
          var fill = document.createElement("div");
          fill.className = "fill";
          fill.style.width = Math.max(0, Math.min(100, g.memory_usage_percent)) + "%";
          bar.appendChild(fill);
          row.appendChild(bar);
        }
      });

      // 次要指标（spec §38：风扇/时钟/PCIe 小字一行）
      var secondary = [
        ["风扇", g.fan_percent == null ? F.NA : F.formatPercent(g.fan_percent, 0)],
        ["SM", g.sm_clock_mhz == null ? F.NA : g.sm_clock_mhz + " MHz"],
        ["显存时钟", g.memory_clock_mhz == null ? F.NA : g.memory_clock_mhz + " MHz"],
        ["PCIe", (g.pcie_generation == null || g.pcie_width == null) ? F.NA : "Gen" + g.pcie_generation + " x" + g.pcie_width],
      ];
      var kv = document.createElement("div");
      kv.className = "gpu-kv gd-secondary";
      secondary.forEach(function (r) {
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

  /* Overview 页 GPU 摘要（Phase 16B §11：迷你卡 4 指标 + 详情链接；
     与 GPU 页同一数据源 /api/gpu/status） */
  function renderGpuOverviewSummary(d) {
    var stateEl = $("ovGpuState");
    var lineEl = $("ovGpuLine");
    var grid = $("ovGpuMini");
    if (!stateEl || !lineEl) return;
    if (!d.available) {
      ui.setStatusBadge(stateEl, "offline", "不可用");
      lineEl.textContent = d.reason || "nvidia-smi 不可用";
      if (grid) grid.innerHTML = "";
      return;
    }
    var gpus = d.gpus || [];
    ui.setStatusBadge(stateEl, "online", gpus.length + " 个 GPU");
    if (!gpus.length) {
      lineEl.textContent = "可用，暂无样本。";
      if (grid) grid.innerHTML = "";
      return;
    }
    lineEl.textContent = "";
    if (!grid) return;
    grid.innerHTML = "";
    gpus.forEach(function (g) {
      var card = document.createElement("div");
      card.className = "gpu-mini";
      var head = document.createElement("div");
      head.className = "gpu-mini-head2";
      var idx = document.createElement("span");
      idx.className = "gm-idx";
      idx.textContent = "GPU " + (g.index == null ? "?" : g.index);
      var name = document.createElement("span");
      name.className = "gm-name";
      name.textContent = g.name || "";
      name.title = g.name || "";
      head.appendChild(idx);
      head.appendChild(name);
      card.appendChild(head);

      // BUG-D：N/A 传感器显示 "--"（不画 0）
      var vramText = (g.memory_used_mb == null || g.memory_total_mb == null) ? F.NA :
        (g.memory_used_mb / 1024).toFixed(1) + " / " + (g.memory_total_mb / 1024).toFixed(1) + " GiB";
      var items = [
        ["利用率", g.utilization_percent == null ? F.NA : F.formatPercent(g.utilization_percent, 0)],
        ["显存", vramText],
        ["温度", F.formatTemp(g.temperature_c)],
        ["功耗", F.formatPower(g.power_draw_w)],
      ];
      var metrics = document.createElement("div");
      metrics.className = "gm-metrics";
      items.forEach(function (it) {
        var m = document.createElement("div");
        m.className = "gm-metric";
        var k = document.createElement("div");
        k.className = "k";
        k.textContent = it[0];
        var v = document.createElement("div");
        v.className = "v" + (it[1] === F.NA ? " dim" : "");
        v.textContent = it[1];
        m.appendChild(k);
        m.appendChild(v);
        metrics.appendChild(m);
      });
      card.appendChild(metrics);
      grid.appendChild(card);
    });
  }

  function renderGpuPick(detected) {
    var box = $("gpuPick");
    if (!box) return;
    var sig = JSON.stringify((detected || []).map(function (g) { return g.uuid + "|" + g.index; }));
    if (sig === state.gpuPickSig && box.children.length) return; // UI-002：未变不重建
    state.gpuPickSig = sig;
    box.innerHTML = "";
    if (!detected || !detected.length) {
      box.style.display = "none";
      return;
    }
    box.style.display = "";
    detected.forEach(function (g) {
      if (state.gpuVisible[g.uuid] === undefined) state.gpuVisible[g.uuid] = true;
      // Phase 16C §12：Fluent Check Chip——保留原生 checkbox 语义/键盘访问，
      // 视觉为可点击 chip；完整名称+UUID 走 title tooltip。
      var label = document.createElement("label");
      label.className = "check-chip" + (state.gpuVisible[g.uuid] ? " on" : "");
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = state.gpuVisible[g.uuid];
      cb.addEventListener("change", function () {
        state.gpuVisible[g.uuid] = cb.checked;
        label.classList.toggle("on", cb.checked);
        redrawGpuCharts();
      });
      label.appendChild(cb);
      var tick = document.createElement("span");
      tick.className = "chip-tick";
      tick.textContent = "\u2713"; // 视觉勾选（原生 checkbox 提供语义）
      label.appendChild(tick);
      var txt = document.createElement("span");
      txt.className = "chip-text";
      txt.textContent = "GPU " + (g.index == null ? "?" : g.index) + (g.name ? " \u00B7 " + g.name : "");
      label.appendChild(txt);
      var tipParts = ["GPU " + (g.index == null ? "?" : g.index)];
      if (g.name) tipParts.push(g.name);
      if (g.uuid) tipParts.push("UUID " + g.uuid);
      label.title = tipParts.join("\n");
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
      k.textContent = "GPU " + (g.name ? g.name : "") + " - 今日能耗（估算）";
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
      tt.textContent = "无能耗数据";
      var dd = document.createElement("div");
      dd.className = "empty-desc";
      dd.textContent = "能耗由采样功耗估算；首个样本采集后出现。";
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
    setStatValue("ovMtpRate", F.formatPercent(d.accept_rate));
    // 指标条不再有 MTP（Phase 16C §5）
    charts.renderMtpPosChart("chartMtpPosBox", "chartMtpPos", state.mtpPositions);
  }

  /* ================= Usage 页：图表 + 每日表（Phase 16B §10） ================= */
  function renderDailyTable() {
    var tbody = $("dailyTbody");
    if (!tbody) return;
    var rows = state.dailyData;
    if (!rows.length) {
      tbody.innerHTML = "<tr><td colspan='9' class='na'>该范围内暂无数据。</td></tr>";
      return;
    }
    tbody.innerHTML = rows.map(function (r) {
      var cov = r.monitoring_coverage_percent;
      var covTd = cov == null ? "<td>" + F.NA + "</td>" :
        "<td class='" + (cov >= 99.9 ? "cell-ok" : cov >= 95 ? "cell-warn" : "cell-bad") + "'>" + F.formatPercent(cov) + "</td>";
      var gaps = r.gap_count || 0;
      var loss = r.possible_token_loss;
      var gapsTd = "<td class='" + (gaps === 0 ? "cell-ok" : loss ? "cell-bad" : "cell-warn") + "'>" + gaps + "</td>";
      var p = r.prompt_tokens || 0, c = r.cached_tokens || 0;
      var crDenom = p + c;
      var cacheTd = crDenom > 0
        ? "<td>" + F.formatPercent(c / crDenom * 100) + "</td>" : "<td>" + F.NA + "</td>";
      return "<tr><td>" + r.date + "</td>" +
        "<td>" + F.formatTokenCount(r.prompt_tokens) + "</td>" +
        "<td>" + F.formatTokenCount(r.cached_tokens) + "</td>" +
        "<td>" + F.formatTokenCount(r.output_tokens) + "</td>" +
        "<td>" + F.formatTokenCount(r.compute_tokens) + "</td>" +
        "<td>" + F.formatTokenCount(r.logical_tokens) + "</td>" + cacheTd + covTd + gapsTd + "</tr>";
    }).join("");
  }

  /* Phase 16B 时间范围模式 -> /api/daily 查询参数 */
  function dailyQuery() {
    switch (state.dailyRangeMode) {
      case "today": return "/api/daily?days=1";
      case "7d": return "/api/daily?days=7";
      case "30d": return "/api/daily?days=30";
      case "month": return "/api/daily?days=31"; // 31 天覆盖整月，前端按月份前缀过滤
      case "all":
      default: return "/api/daily?all=true";
    }
  }

  function refreshDaily() {
    return api.get(dailyQuery())
      .then(function (d) {
        var rows = d.days || [];
        if (state.dailyRangeMode === "month") {
          var d2 = new Date();
          var key = d2.getFullYear() + "-" + String(d2.getMonth() + 1).padStart(2, "0");
          rows = rows.filter(function (r) { return String(r.date).indexOf(key) === 0; });
        }
        state.dailyData = rows;
        charts.renderUsageChart("chartUsageBox", "chartUsage", state.dailyData);
        charts.renderMtpChart("chartMtpBox", "chartMtp", state.dailyData);
        renderDailyTable();
        renderRangeSummary();
      })
      .catch(function (e) { console.warn("daily failed:", e.message || e); });
  }

  function setDailyRangeMode(mode) {
    state.dailyRangeMode = mode;
    refreshDaily();
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
    // 倒计时基准：以本次轮询发起时刻为准（1s ticker 据此显示 "x 秒后刷新"）
    state.statusBackendOk = true;
    state.lastStatusRefresh = Date.now();
    return api.get("/api/status")
      .then(applyStatus)
      .catch(function (e) {
        // 后端不可达（区别于 llama 离线）：保留上次数据 + 提示（spec §129）
        console.warn("status failed:", e.message || e);
        state.statusBackendOk = false; // 由 1s ticker 统一渲染"后端不可达"
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

  /* 监控事件（History 页 spec §50：/api/events，最近 30 条） */
  var EVENT_TYPE_LABELS = {
    monitor_start: "监控启动",
    monitor_stop: "监控停止",
    monitor_restart_gap: "监控重启",
    server_online: "服务器上线",
    server_offline: "服务器离线",
    metrics_valid: "指标有效",
    invalid_metrics: "无效指标",
    database_protective_mode: "数据库保护模式",
    database_recovery: "数据库恢复",
    database_write_failure: "数据库写入失败",
    database_integrity_error: "数据库完整性错误",
    counter_reset: "计数器重置",
    backup_created: "备份完成",
    update_check: "更新检查",
    update_check_failed: "更新检查失败",
    update_available: "发现新版本",
    update_download_started: "更新下载开始",
    update_download_complete: "更新下载完成",
    update_download_cancelled: "更新下载取消",
    update_verification_failed: "更新校验失败",
    update_install_started: "更新安装开始",
    update_install_aborted: "更新安装中止",
    update_backup_failed: "更新备份失败",
  };

  function refreshEvents() {
    return api.get("/api/events?limit=30")
      .then(function (d) {
        state.events = d.events || [];
        renderEventsList();
      })
      .catch(function (e) { console.warn("events failed:", e.message || e); });
  }

  /* Phase 16C §19：事件列表默认显示前 N 条，超出部分用"查看更多"展开；
     取消内部嵌套滚动，由页面本身承担纵向滚动。 */
  var EVENTS_PAGE_SIZE = 15;
  var eventsExpanded = false;

  function renderEventsList() {
    var tbody = $("eventsTbody");
    var empty = $("eventsEmpty");
    var wrapEl = $("eventsTableWrap");
    if (!tbody) return;
    var events = state.events || [];
    // HISTORY-002（Phase 16C §14/§15）：events.length>0 时彻底隐藏空态
    // （走 ui.setEmptyState，修复 [hidden] 被 display:flex 压过的问题）。
    ui.setEmptyState(empty, events.length === 0);
    if (wrapEl) wrapEl.style.display = events.length ? "" : "none";
    tbody.innerHTML = "";

    var shown = eventsExpanded ? events : events.slice(0, EVENTS_PAGE_SIZE);
    tbody.innerHTML = shown.map(function (ev) {
      var clock = F.formatClock || F.formatDateTime; // 16D 兜底：浏览器混装新旧 JS 时不抛错
      var time = clock(ev.timestamp); // 紧凑单行：今天 HH:MM:SS / 跨天 MM-DD HH:MM
      var full = F.formatDateTime(ev.timestamp);
      var sev = ev.severity === "warning" ? " cell-warn" : ev.severity === "error" ? " cell-bad" : "";
      var label = EVENT_TYPE_LABELS[ev.event_type] || ev.event_type;
      // Phase 16C §17/§18：展示层 humanize；原 details 保留在 tooltip
      var detail = ui.humanizeEventDetails(ev) || "";
      var rawDetail = "";
      try {
        rawDetail = ev.details && typeof ev.details === "object" ? JSON.stringify(ev.details) : (ev.details || "");
      } catch (e) { rawDetail = String(ev.details || ""); }
      var esc = function (s) { return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;"); };
      return "<tr>" +
        "<td title='" + esc(full) + "'>" + esc(time) + "</td>" +
        "<td class='ev-type" + sev + "' title='" + esc(ev.event_type) + "'>" + esc(label) + "</td>" +
        "<td class='ev-detail' title='" + esc(rawDetail || detail) + "'>" + esc(detail) + "</td>" +
        "</tr>";
    }).join("");
    // "查看更多"（仅当还有未显示的行）—— 表格末行
    if (!eventsExpanded && events.length > EVENTS_PAGE_SIZE) {
      var tr = document.createElement("tr");
      tr.className = "events-more-row";
      var td = document.createElement("td");
      td.colSpan = 3;
      var b = document.createElement("button");
      b.type = "button";
      b.className = "btn small subtle events-more";
      b.textContent = "查看更多（还有 " + (events.length - EVENTS_PAGE_SIZE) + " 条）";
      b.addEventListener("click", function () {
        eventsExpanded = true;
        renderEventsList();
      });
      td.appendChild(b);
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
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
    /* 16E：时间筛选控件先于 /api/config 创建——该端点是 loopback-only，
       手机走局域网 IP 访问得 403，此前筛选被放在 await 之后，403 直接进
       catch，两个 segmented 永远没被创建（手机看不到时间范围筛选）。
       现在：先用内置默认值渲染；config 成功后用服务器配置 set() 同步选中项。 */
    try {
      var modeOptions = [
        { value: "today", label: "今天" },
        { value: "7d", label: "7 天" },
        { value: "30d", label: "30 天" },
        { value: "month", label: "本月" },
        { value: "all", label: "全部" },
      ];
      var rangeEl = $("usageRange");
      var usageSeg = rangeEl && LM.nav
        ? ui.segmented(rangeEl, modeOptions, state.dailyRangeMode, function (v) {
            state.dailyRangeMode = v;
            setDailyRangeMode(v);
          })
        : null;
      var gpuRangeEl = $("gpuRange");
      if (gpuRangeEl) {
        ui.segmented(gpuRangeEl, [
          { value: 15, label: "15 分钟" },
          { value: 60, label: "1 小时" },
          { value: 360, label: "6 小时" },
          { value: 1440, label: "24 小时" },
        ], state.gpuRangeMinutes, function (v) {
          state.gpuRangeMinutes = v;
          refreshGpuLive();
        });
      }
      // 16E：/api/config 是 loopback-only。远程（局域网 IP）客户端不发该请求，
      // 直接用内置默认值——手机 DevTools 网络面板不再出现 403。
      var c = LM.api.isLocal() ? await api.get("/api/config") : null;
      if (c && c.ui) {
        cfgUi.refreshIntervalSeconds = c.ui.refresh_interval_seconds || 5;
        cfgUi.dailyDefaultDays = c.ui.daily_default_days || 7;
        cfgUi.theme = c.ui.theme || "system";
      }
      lastConfigUrl = (c && c.llama_server && c.llama_server.url) || "";
      var _su = $("ovServerUrl"); if (lastConfigUrl && _su) _su.textContent = lastConfigUrl.replace(/^https?:\/\//, "");
      setThemeMode(cfgUi.theme);
      // 服务器配置到达后同步 Usage 默认范围（set() 只更新选中态，不触发 onChange）
      var defDays = cfgUi.dailyDefaultDays || 7;
      var serverDefault = defDays <= 1 ? "today" : defDays <= 7 ? "7d" :
        defDays <= 30 ? "30d" : defDays <= 62 ? "month" : "all";
      if (usageSeg && serverDefault !== state.dailyRangeMode) {
        state.dailyRangeMode = serverDefault;
        usageSeg.set(serverDefault);
        setDailyRangeMode(serverDefault);
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
    // 概览行动链接（Phase 16B §2 data-goto：16C 审计发现点击无响应——
    // 16B 重写 HTML 时丢失了处理器，这里用事件委托统一接管）
    document.querySelectorAll("a.link[data-goto]").forEach(function (a) {
      a.addEventListener("click", function (ev) {
        ev.preventDefault();
        var p = a.getAttribute("data-goto");
        if (p && document.getElementById("page-" + p)) LM.nav.showPage(p);
      });
    });
    LM.nav.initCompact();
    // 16E+：远程（局域网 IP）客户端没有可改的配置（/api/config 等 loopback-only），
    // 直接隐藏「设置」导航入口——比"进去看到只读表单"更干净。本机不变。
    if (!LM.api.isLocal()) {
      document.querySelectorAll('.nav-item[data-page="settings"]').forEach(function (b) {
        b.style.display = "none";
      });
    }
    // Settings 事件绑定（保存/重置/测试连接/dirty 标记/主题切换/自动启动/危险操作/更新）
    if (LM.settings && LM.settings.init) LM.settings.init();

    // 页面钩子（Phase 16B：进入页面时加载该页数据；隐藏页不跑其专属轮询）
    LM.nav.registerPage("overview", function () {
      refreshStatus(); refreshSummary(); refreshRuntime();
      refreshDataQuality(); refreshGpuStatus();
    });
    LM.nav.registerPage("usage", function () {
      charts.ensurePageCharts(["chartUsage"]);
      refreshDaily(); refreshSummary();
    });
    LM.nav.registerPage("performance", function () {
      charts.ensurePageCharts(["chartTps", "chartMtp", "chartMtpPos"]);
      // MTP 趋势图数据来自 /api/daily（BUG-B 修复：单点也显示；无数据时空态）
      charts.renderMtpChart("chartMtpBox", "chartMtp", state.dailyData);
      refreshLive(); refreshMtp(); refreshRuntime();
    });
    LM.nav.registerPage("gpu", function () {
      charts.ensurePageCharts(["chartGpuUtil", "chartGpuPower", "chartGpuTemp"]);
      refreshGpuStatus(); refreshGpuLive(); refreshGpuDaily();
    });
    LM.nav.registerPage("history", function () {
      refreshDataQuality(); refreshEvents();
    });
    LM.nav.registerPage("settings", function () {
      if (!LM.settings.isLoaded()) LM.settings.loadSettings();
      else LM.settings.loadAppIntegration();
    });
    LM.nav.registerPage("about", function () {
      LM.settings.loadAbout();
    });

    // 轮询任务注册（Phase 16B spec §119：页面作用域——
    // 状态类 ~5s（config 间隔）；图表 10-15s；用量/历史 30-60s 且仅在对应页前台时运行）
    var R = Math.max(1, cfgUi.refreshIntervalSeconds) * 1000;
    // 状态类（全局：状态条/离线横幅依赖，任何页可见时都跑）
    LM.poll.register("status", { intervalMs: R, visibleOnly: true, run: refreshStatus });
    LM.poll.register("runtime", { intervalMs: R, visibleOnly: true, run: refreshRuntime });
    LM.poll.register("gpuStatus", { intervalMs: R, visibleOnly: true, run: refreshGpuStatus });
    // 摘要/质量（Overview + Usage/History 共用，30s 基线、前台 10s）
    LM.poll.register("summary", { intervalMs: 30000, visibleIntervalMs: 10000, visibleOnly: false, run: refreshSummary });
    LM.poll.register("dataQuality", { intervalMs: 30000, visibleIntervalMs: 10000, visibleOnly: false, run: refreshDataQuality });
    // 页面专属（hidden page 不轮询，避免跨页重复请求）
    LM.poll.register("live", {
      intervalMs: 60000, visibleIntervalMs: 15000, visibleOnly: true,
      run: function () { if (LM.nav.currentPage() === "performance") return refreshLive(); },
    });
    LM.poll.register("mtp", {
      intervalMs: 60000, visibleIntervalMs: 30000, visibleOnly: false, run: refreshMtp,
    });
    LM.poll.register("daily", {
      intervalMs: 60000, visibleIntervalMs: 30000, visibleOnly: true,
      run: function () {
        var p = LM.nav.currentPage();
        if (p === "usage" || p === "history") return refreshDaily();
      },
    });
    LM.poll.register("gpuLive", {
      intervalMs: 60000, visibleIntervalMs: 15000, visibleOnly: true,
      run: function () { if (LM.nav.currentPage() === "gpu") return refreshGpuLive(); },
    });
    LM.poll.register("gpuDaily", {
      intervalMs: 120000, visibleIntervalMs: 60000, visibleOnly: true,
      run: function () { if (LM.nav.currentPage() === "gpu") return refreshGpuDaily(); },
    });
    LM.poll.register("events", {
      intervalMs: 120000, visibleIntervalMs: 60000, visibleOnly: true,
      run: function () { if (LM.nav.currentPage() === "history") return refreshEvents(); },
    });
    // Updates：30s 全局（驱动横幅）+ 1s 仅在 Updates 分区（下载进度，UI-023 统一进调度器）
    // 16E：/api/update/* 是 loopback-only——手机（局域网 IP）访问得 403。
    // loadUpdateStatus 内部已 catch 并区分 403（静默），这里不变。
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
      // （避免双重 fetch）；这里只预热 Usage 图表数据与更新状态横幅。
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
    refreshDailyNow: function () { refreshDaily(); },
    refreshMtpNow: function () { refreshMtp(); },
    refreshDataQualityNow: function () { refreshDataQuality(); },
    refreshEventsNow: function () { refreshEvents(); },
    // Phase 16C §21：设置页"服务器"卡复用已有 /api/status 轮询状态
    // （不额外高频探测）。返回 {status:'unknown'|'online'|'offline', url}
    serverConnectionState: function () {
      return {
        status: state.online === true ? "online" : state.online === false ? "offline" : "unknown",
        url: state.serverUrlText || "",
      };
    },
  };

  // DOM ready
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
