/* ============================================================
 LlamaMonitor — App bootstrap
 应用启动 / 全局状态 / 页面数据接线 / 主题控制器 / 轮询注册。
 职责
 - loadConfig：/api/config（主题、刷新间隔、服务器地址）
 - 主题控制器：dark/light/system + 系统主题实时跟随（UI-001）
 - 状态处理：Online/Offline/Stale +  全局 InfoBar
 - 各页面数据刷新函数 + LM.poll 任务注册（中央调度器）
 - 每 section 独立失败（一个 API 失败不拖垮其他）
 ============================================================ */
(function () {
  "use strict";

  var F = LM.fmt, api = LM.api, ui = LM.ui, charts = LM.charts;
  var $ = function (id) { return document.getElementById(id); };

  var cfgUi = {
    refreshIntervalSeconds: 5,
    dailyDefaultDays: 7,   // 与服务端默认一致（默认 7 天）——
    theme: "system",
  };

  /* ================= 全局状态 ================= */
  var state = {
    online: null,          // null=尚未取得 / true / false
    lastSuccessTs: null,   // 最近一次 server_online===true 的 last_update（epoch s）
    config: null,          // /api/status 的 config 块
    dailyData: [],         // /api/daily 行（当前范围）
    dailyRange: 30,        // Usage 页当前范围天数（all 模式不使用）
    dailyRangeMode: "7d", //today | 7d | 30d | month | all | custom（默认 7 天）
    dailyRangeMeta: null,  // /api/daily 的 range 元数据（mode/start/end/today/server_now_hhmm）
    usageSummary: null,    // /api/usage-summary 区间聚合（使用汇总卡数字来源）
    customRange: null,     // custom 模式的 {start_date, end_date}（服务器本机日历日）
    trendGran: "day",      // 趋势粒度：day（按天）| hour（按小时，今天）
    todayHourly: null,     // /api/today-hourly（今天逐小时桶）
    todayHourlyFetched: false, // 今天范围下小时档是否已拉过一次（避免重复请求）
    liveSamples: [],       // /api/live 60 分钟
    gpuStatus: null,       
    gpuLive: null,         // /api/gpu/live（当前范围）
    gpuRangeMinutes: 60,
    gpuVisible: {},
    gpuAdvOpen: {},        // Round-4：高级信息 details 展开态（uuid -> bool；重建卡片保留）
    gpuProcShowAll: false, // Round-4：GPU 进程 >20 默认折叠，true=显示全部
    gpuPickSig: null,      // UI-002：detected 签名
    mtp: null,             // /api/mtp（今日；兼容保留）
    mtpPositions: [],
    mtpRange: "today",     // MTP 范围：today | 7 | 30 | all（控制整个 MTP Section）
    mtpData: null,         // /api/mtp/range 响应（summary + days + positions）
    throughputRangeMinutes: 60, // Token 吞吐率范围：15/60/360/1440
    throughputData: null,  // /api/throughput 响应（samples + window_avg）
    slotsData: null,       // /api/llama/slots 响应（活跃 Slot 统计来源）
    pollIntervalSec: 5,    // 采集间隔（缺口阈值 = poll*3；新鲜度 > 3*interval）
    quality: null,         
    health: null,          
    events: [],            // /api/events（History 页监控事件）
    lastUpdateTs: null,    // 最近一次采样的 last_update（epoch s）
    lastStatusRefresh: 0,  // 最近一次 /api/status 轮询完成时刻（epoch ms）— 倒计时基准
    statusBackendOk: true, // 后端（非 llama）是否可达
    ovSummary: null,       // 最近一次 /api/summary（Overview 今日卡）
    serverUrlText: "",     // 当前服务器地址（设置页连接状态复用）
    // ===== Round-5 History 页 =====
    historyRange: "7d",        // 24h | 7d | 30d | all | custom
    historyCustom: null,       // custom 模式 {start_date, end_date}
    historySummary: null,      // /api/history/summary（随 Range 变化）
    historyTrend: null,        // /api/history/trend（points + bucket）
    historyGen: 0,             // Summary/Trend Range 切换 race 防护（§296-304）
    histGapsGen: 0,           // 缺口分页独立 gen（与 events 互不干扰）
    histEventsGen: 0,         // 事件分页独立 gen
    histGaps: [], histGapsCursor: null, histGapsHasMore: false, histGapsLoading: false, histGapsShowAll: false,
    histGapSource: "", histGapRisk: "", histGapExpanded: {}, histGapBucket: null,
    histEvents: [], histEventsCursor: null, histEventsHasMore: false, histEventsLoading: false,
    histEventCategory: "", histEventSeverity: "", histEventSearch: "",
  };

  /* 用量页控件句柄（loadConfig 创建后写入；setDailyRangeMode 回写选中态用） */
  var usageRangeSeg = null;  // 时间范围分段（今天/7天/30天/本月/全部）
  var usageTrendSeg = null;  // 趋势粒度分段（按天/按小时）
  var customPopoverOpen = false;
  /* 推理性能页控件句柄（loadConfig 创建后写入） */
  var perfTpsSeg = null;   // Token 吞吐率范围分段（15分钟/1小时/6小时/24小时）
  var perfMtpSeg = null;   // MTP 范围分段（今天/7天/30天/全部）

  /* ---- 状态条 1s ticker（信息层级调整） ----
 主信息：在线 → "最后更新 X 秒前"（来自 lastUpdateTs）；
 离线 → "最后成功采样: X 分钟前"（lastSuccessTs）。
 次要信息：倒计时 "x 秒后刷新"（text-disabled 色，降级为辅助信息）。
 后端不可达：主信息提示后端不可达。 */
  function updateLastUpdateText() {
    var el = document.getElementById("ovLastUpdate");
    if (!el) return;
    // 常态（在线）不显示"最后更新 X 秒前 / X 秒后刷新"（5s 轮询信息量低）；
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
      // 1.1.4 精修 §12：Server 卡右侧常态显示 "最后更新：刚刚 / X 秒前"
      // （数据新鲜度始终可见；5s 轮询下 elapsed 通常 <5s 显示"刚刚"，
      // 不每轮跳字）。
      var elapsed = state.lastUpdateTs ? (Math.floor(Date.now() / 1000) - state.lastUpdateTs) : null;
      if (elapsed == null) {
        el.textContent = "";
        el.className = "stat-hint";
      } else {
        el.textContent = "最后更新：" + F.formatAgo(elapsed);
        el.className = elapsed >= 10 ? "stat-hint warn" : "stat-hint";
      }
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

  /* ================= 主题控制器（；UI-001 修复） ================= */
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
      chartUsage: function () { renderUsageTrend(); },
      chartTps: function () {
        if (state.throughputData) {
          charts.renderTpsChart("chartTpsBox", "chartTps", state.throughputData.samples || [], {
            pollIntervalSec: state.pollIntervalSec,
            lastActivityTs: state.throughputData.last_activity_ts,
            availableMinutes: state.throughputData.available_minutes,
            requestedMinutes: state.throughputData.minutes,
          });
        }
      },
      chartMtp: function () {
        if (state.mtpData) charts.renderMtpChart("chartMtpBox", "chartMtp", state.mtpData.days || []);
      },
      chartMtpPos: function () {
        if (state.mtpData) charts.renderMtpPosChart("chartMtpPosBox", "chartMtpPos", state.mtpData.positions || []);
      },
      chartGpuUtil: function () { charts.renderGpuUtilChart("chartGpuUtilBox", "chartGpuUtil", state.gpuLive, state.gpuVisible); },
      chartGpuPower: function () { charts.renderGpuPowerChart("chartGpuPowerBox", "chartGpuPower", state.gpuLive, state.gpuVisible); },
      chartGpuTemp: function () { charts.renderGpuTempChart("chartGpuTempBox", "chartGpuTemp", state.gpuLive, state.gpuVisible); },
      chartGpuFan: function () { charts.renderGpuFanChart("chartGpuFanBox", "chartGpuFan", state.gpuLive, state.gpuVisible); },
      chartGpuClock: function () { charts.renderGpuClockChart("chartGpuClockBox", "chartGpuClock", state.gpuLive, state.gpuVisible); },
      // 1.1 系统页图表（数据源 LM.system 的 /api/system/live + summary）
      chartSysCpu: function () { if (LM.system) charts.renderSysCpuChart("chartSysCpuBox", "chartSysCpu", LM.system.getLive() || [], LM.system.getLiveSummary() || {}); },
      chartSysMem: function () { if (LM.system) charts.renderSysMemChart("chartSysMemBox", "chartSysMem", LM.system.getLive() || [], LM.system.getLiveSummary() || {}); },
      chartSysDisk: function () { if (LM.system) charts.renderSysDiskChart("chartSysDiskBox", "chartSysDisk", LM.system.getLive() || []); },
      chartSysNet: function () { if (LM.system) charts.renderSysNetChart("chartSysNetBox", "chartSysNet", LM.system.getLive() || []); },
      chartSysPower: function () { if (LM.system) charts.renderSysPowerChart("chartSysPowerBox", "chartSysPower", LM.system.getLive() || [], LM.system.getLiveSummary() || {}); },
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
      title: "llama-server 连接中断",
      message: "最近一次成功采样：" + (lastTs ? F.formatTime(lastTs) : "无记录") +
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

    // 全局状态徽章（Overview 顶部 + 页面内 server 卡）。
    // 1.1：优先用 server_state（ready/loading/unavailable 三态，来自 llama-server 探活）；
    // 无该字段时回退既有 online/offline 逻辑（旧后端兼容）。
    var ss = data.server_state;
    if (ss === "ready" || ss === "loading" || ss === "unavailable") {
      ui.setStatusBadge($("ovServerState"),
        ss === "ready" ? "online" : ss === "loading" ? "warning" : "offline",
        ss === "ready" ? "就绪" : ss === "loading" ? "模型加载中" : "不可用");
    } else {
      ui.setStatusBadge($("ovServerState"),
        state.online === true ? "online" : state.online === false ? "offline" : "paused",
        state.online === null ? "检测中" : undefined);
    }

    // 概览：当前模型行（别名 · 量化 · 规模 · 参数量；来自 /api/status 内嵌摘要）。
    // 1.1.4 精修：model_ftype 原样是 "Q4_K - Medium"（量化 - 规模），前端展示时
    // 拆成 "Q4_K · Medium"（视觉层；后端 model_ftype 不动）。
    var ovModel = $("ovModelLine");
    if (ovModel) {
      var m = data.llama_model;
      if (m) {
        var parts = [];
        if (m.model_alias) parts.push(m.model_alias);
        if (m.model_ftype) {
          var ft = String(m.model_ftype).split(" - ");
          for (var fi = 0; fi < ft.length; fi++) if (ft[fi]) parts.push(ft[fi]);
        }
        if (m.parameter_count != null) parts.push((m.parameter_count / 1e9).toFixed(2) + "B");
        ovModel.textContent = parts.join(" · ");
        ovModel.title = parts.join(" · ");
        ovModel.style.display = parts.length ? "" : "none";
      } else {
        ovModel.textContent = "";
        ovModel.style.display = "none";
      }
    }

    // Offline InfoBar（明确 offline，保留历史）
    if (state.online === false) {
      showOfflineBar(state.lastSuccessTs || data.last_update);
    } else {
      hideOfflineBar();
    }

    // 记录最近采样时刻（离线条/lastSuccessTs 用）；顶部"X 秒后刷新"倒计时
    // 由 1s ticker 依据 refreshStatus 的轮询时机驱动
    state.lastUpdateTs = data.last_update || null;

    // 服务器地址（§13 语义修复：统一显示 Server Base Address，不含 /metrics 路径。
    // 此前远程客户端读不到 /api/config（loopback-only），回退到 status.llama_server_url
    // = 完整 metrics URL，导致 Desktop 显示 127.0.0.1:9091 而 Mobile 显示
    // 127.0.0.1:9091/metrics。Base = URL 去掉 scheme + 末尾的 metrics 路径。）
    var elUrl = $("ovServerUrl");
    if (elUrl) {
      var url = (LM.settings && lastConfigUrl) || data.llama_server_url || "";
      state.serverUrlText = baseAddress(url) || "--";
      elUrl.textContent = state.serverUrlText;
    }

    // 配置状态（Config: OK / Default / Error，数据库/配置问题明确化）
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

    // 推理性能页顶部摘要（Round 5 §23-§27）：TPS 存在且为 0 -> "0 tok/s"，
    // 只有 null/缺失/离线才 "--"。离线时保留最近值并标 stale（见 freshness badge）。
    // （概览页的 TPS / 上下文 / 请求 / Slot / 模态 由 refreshOverview -> /api/overview
    //  统一驱动，此处只管性能页摘要 + 全局状态。）
    setStatValue("perfPromptTps", data.prompt_tps != null ? F.formatTps(data.prompt_tps) + " tok/s" : F.NA);
    setStatValue("perfDecodeTps", data.decode_tps != null ? F.formatTps(data.decode_tps) + " tok/s" : F.NA);
    renderFreshnessBadge();

    if (state.online === true && state.lastUpdateTs) state.lastSuccessTs = state.lastUpdateTs;
  }

  var lastConfigUrl = "";

  /* §13：Server Base Address 提取——去掉 scheme 与末尾的 metrics 路径
   （127.0.0.1:9091/metrics → 127.0.0.1:9091；已是 base 的原样返回）。 */
  function baseAddress(url) {
    var u = String(url || "").replace(/^https?:\/\//i, "").replace(/\/+$/, "");
    // 仅当末尾是已知 metrics 类路径时剥掉（不盲剥任意末段，避免吃掉 /v1 之类）
    u = u.replace(/\/(metrics|metrics.*)$/, "");
    return u.replace(/\/+$/, "");
  }

  /* ================= Summary（Overview 今日卡 + Usage 范围摘要；BUG-A 修复） =================
 /api/summary 现由后端计算 today 与 month（同一 local_date 来源），
 前端不再做浏览器本地月份前缀过滤。 */
  function renderSummaryCards(summary) {
    state.ovSummary = summary;
    var t = summary.today || {};
    // Round-6：概览「今日用量」6 项由 /api/overview 统一驱动（renderOvUsage）。
    // 此处仅在 overview 尚未首载时做 bootstrap 填充（同一 daily_usage 数据），
    // overview 加载后不再覆写（避免 /api/summary 与 /api/overview 互相打架）。
    if (!ovLoaded) {
      setStatValue("ovTodayLogical", F.formatTokenCount(t.logical_tokens));
      setStatValue("ovTodayCompute", F.formatTokenCount(t.compute_tokens));
      setStatValue("ovTodayPrompt", F.formatTokenCount(t.prompt_tokens));
      setStatValue("ovTodayCached", F.formatTokenCount(t.cached_tokens));
      setStatValue("ovTodayOutput", F.formatTokenCount(t.output_tokens));
    }

    // Usage 使用汇总卡（月份 key 来自 /api/summary；数字来自 /api/usage-summary）
    renderUsageSummary();
  }

  function setFullTip(id, v) {
    var el = $(id);
    if (el) el.title = F.formatTokenCountFull(v);
  }

  /* ================= 使用汇总卡（1.1.4 Round 4） =================
  数字全部来自 /api/usage-summary（服务端区间聚合：加权覆盖率/日均/峰值日），
  前端只做格式化与缺失日语义标注，不做浏览器时区窗口计算。 */
  function rangeLabel() {
    var meta = state.dailyRangeMeta, mode = state.dailyRangeMode;
    if (mode === 'custom' && state.customRange) {
      return state.customRange.start_date + ' ~ ' + state.customRange.end_date;
    }
    if (mode === 'today') {
      var hhmm = meta && meta.server_now_hhmm;
      return '今天' + (hhmm ? ' · 截至 ' + hhmm : '');
    }
    if (mode === 'month') {
      var mk = state.ovSummary && state.ovSummary.month_key;
      return mk ? '本月（' + mk + '）' : '本月';
    }
    if (mode === 'all') return '全部';
    if (mode === '7d') return '近 7 天';
    if (mode === '30d') return '近 30 天';
    return '所选范围';
  }

  function renderUsageSummary() {
    var labelEl = sumRangeLabel;
    if (!labelEl) return;
    labelEl.textContent = rangeLabel() +
      (state.dailyData.length ? '（' + state.dailyData.length + ' 天有数据）' : '（无数据）');
    var s = state.usageSummary;
    if (!s) return;
    var t = s.totals || {};
    setStatValue('sumHeroLogical', F.formatTokenCount(t.logical_tokens));
    setStatValue('sumHeroCompute', F.formatTokenCount(t.compute_tokens));
    setFullTip('sumHeroLogical', t.logical_tokens);
    setFullTip('sumHeroCompute', t.compute_tokens);
    setStatValue('sumPrompt', F.formatTokenCount(t.prompt_tokens));
    setStatValue('sumCached', F.formatTokenCount(t.cached_tokens));
    setStatValue('sumOutput', F.formatTokenCount(t.output_tokens));
    setStatValue('sumReuseRate', s.cache_reuse_rate_percent != null
      ? F.formatPercent(s.cache_reuse_rate_percent) : F.NA);
    // Token 构成条（6–8px 堆叠；总长 0 时不渲染分段）
    var track = sumCompTrack;
    if (track) {
      var total = (t.prompt_tokens || 0) + (t.cached_tokens || 0) + (t.output_tokens || 0);
      track.innerHTML = '';
      if (total > 0) {
        var parts = [[t.prompt_tokens || 0, 'prompt'], [t.cached_tokens || 0, 'cached'], [t.output_tokens || 0, 'output']];
        parts.forEach(function (pt) {
          if (!pt[0]) return;
          var i = document.createElement('i');
          i.className = pt[1];
          i.style.width = (pt[0] / total * 100).toFixed(3) + '%';
          track.appendChild(i);
        });
      }
    }
    // 洞察：日均（÷ 有效数据天数）/ 峰值日（MM-DD · value）/ 加权覆盖率 / 缺口
    var avgEl = sumDailyAvg;
    if (avgEl) {
      if (s.daily_avg_logical != null) {
        avgEl.textContent = F.formatTokenCount(s.daily_avg_logical) + ' / 天';
        avgEl.title = 'Token 总量 ' + F.formatTokenCountFull(t.logical_tokens) +
          ' ÷ 有效数据 ' + s.valid_days + ' 天';
      } else { avgEl.textContent = F.NA; avgEl.title = ''; }
    }
    var peakEl = sumPeakDay;
    if (peakEl) {
      if (s.peak_day && s.peak_day.logical_tokens > 0) {
        peakEl.textContent = s.peak_day.date.slice(5) + ' · ' + F.formatTokenCount(s.peak_day.logical_tokens);
        peakEl.title = s.peak_day.date + '（' + F.formatTokenCountFull(s.peak_day.logical_tokens) + '）';
      } else { peakEl.textContent = F.NA; peakEl.title = ''; }
    }
    var covEl = sumCoverage;
    if (covEl) covEl.textContent = s.coverage_percent != null
      ? F.formatPercent(s.coverage_percent) : F.NA;
    var gapEl = sumGap, lossEl = sumGapLoss;
    if (gapEl) {
      gapEl.textContent = s.gap_seconds > 0 ? F.formatDuration(s.gap_seconds) +
        (s.gap_count ? '（' + s.gap_count + ' 段）' : '') : '无';
    }
    if (lossEl) {
      if (s.possible_token_loss) {
        lossEl.textContent = '可能 Token 丢失';
        lossEl.className = 'ui-loss warn';
      } else { lossEl.textContent = ''; lossEl.className = 'ui-loss'; }
    }
  }

  function usageSummaryQuery() {
    var base = '/api/usage-summary';
    switch (state.dailyRangeMode) {
      case 'today': return base + '?days=1';
      case '7d': return base + '?days=7';
      case '30d': return base + '?days=30';
      case 'month': return base + '?month=true';
      case 'custom':
        if (state.customRange) return base + '?start_date=' + state.customRange.start_date +
          '&end_date=' + state.customRange.end_date;
        return base + '?days=7';
      case 'all':
      default: return base + '?all=true';
    }
  }

  function refreshUsageSummary() {
    return api.get(usageSummaryQuery())
      .then(function (d) {
        state.usageSummary = d;
        renderUsageSummary();
      })
      .catch(function (e) { console.warn('usage summary failed:', e.message || e); });
  }

  /* ================= Runtime（Performance 页 + Overview 摘要） =================
   Round 5：运行时状态 Section 重定义——不再重复顶部「处理中/等待请求」，
   展示更技术性指标：活跃/总 Slot、平均忙碌 Slot / Decode、当前序列长度、
   最大观测序列长度、上下文窗口上限。KV Cache 使用率（无真实数据源）从主布局移除。 */
  function applyRuntime(d) {
    // 平均忙碌 Slot / Decode（gauge；null=服务器未提供该指标 -> --；0=真实 0）
    setStatValue("rtBusySlots", d.busy_slots == null ? F.NA :
      Number(d.busy_slots).toFixed(2));
    // 最大观测序列长度（n_tokens_max，历史观测最大值，非当前上下文）
    setStatValue("rtTokenMax", F.formatTokenCount(d.n_tokens_max));
    // 上下文窗口上限（/props n_ctx；runtime 未报告时回退模型配置 context_size）
    var ctxMax = d.context_max != null ? d.context_max
      : (state.slotsData && state.slotsData.slots && state.slotsData.slots.length
         && state.slotsData.slots[0].n_ctx != null ? state.slotsData.slots[0].n_ctx : null);
    setStatValue("rtContextMax", ctxMax != null ? F.formatTokenCount(ctxMax) : F.NA);
    // Overview 运行状态卡 KV Cache（0.16.12；保留，性能页主布局不再显示）
    var kv = d.kv_cache_usage_ratio;
    setStatValue("ovKvCache", kv == null ? F.NA : F.formatPercent(kv * 100));
    // 顶部摘要条：处理中 / 等待中 请求（0 为真实值）
    setStatValue("perfProcessing", d.requests_processing == null ? F.NA : F.formatInt(d.requests_processing));
    setStatValue("perfQueued", d.requests_deferred == null ? F.NA : F.formatInt(d.requests_deferred));
    // 活跃 Slot（来自 /slots is_processing 统计，见 renderActiveSlots；此处兜底）
    renderActiveSlots();
    renderCurrentSequence();
    // 1.1.4：记录 MTP 能力位（/api/runtime capabilities.mtp）——
    // Overview「Draft Token 接受率」三态需要它（未启用 / 启用暂无样本 / 百分比）。
    state.mtpCapable = !!(d.capabilities && d.capabilities.mtp);
    if (state.mtp) renderOvMtpRate();
  }

  /* 活跃 Slot 统计（§17：从 /slots is_processing 计数，不用 n_busy_slots_per_decode）。
   顶部摘要 + 运行时「活跃/总 Slot」共用。返回 {active, total} 或 null（无 slot 数据）。 */
  function activeSlotStats() {
    var slots = state.slotsData && state.slotsData.slots;
    if (!slots || !slots.length) return null;
    var active = 0;
    for (var i = 0; i < slots.length; i++) if (slots[i].is_processing) active++;
    return { active: active, total: slots.length };
  }

  function renderActiveSlots() {
    var st = activeSlotStats();
    var text = st ? st.active + " / " + st.total : F.NA;
    setStatValue("perfActiveSlots", text);
    setStatValue("rtSlots", text);
  }

  /* 当前序列长度（§66/§133：所有活跃 Slot 的 n_prompt_tokens + n_decoded 最大值；
   无活跃 Slot -> "空闲"，不显示 --）。含上下文占用进度条（§77）。 */
  function renderCurrentSequence() {
    var el = $("rtCurrentSeq");
    var bar = $("rtCurrentCtxBar");
    var hint = $("rtCurrentCtxHint");
    if (!el) return;
    var slots = state.slotsData && state.slotsData.slots;
    var ctxMax = null;
    var maxSeq = null;
    if (slots) {
      for (var i = 0; i < slots.length; i++) {
        var s = slots[i];
        if (ctxMax == null && s.n_ctx != null) ctxMax = s.n_ctx;
        if (s.is_processing) {
          var seq = 0;
          if (s.n_prompt_tokens != null) seq += s.n_prompt_tokens;
          if (s.next_token && s.next_token.n_decoded != null) seq += s.next_token.n_decoded;
          if (maxSeq == null || seq > maxSeq) maxSeq = seq;
        }
      }
    }
    if (maxSeq == null) {
      el.textContent = "空闲";
      el.classList.remove("dim");
      if (bar) bar.hidden = true;
      if (hint) hint.textContent = "";
      return;
    }
    el.textContent = F.formatTokenCount(maxSeq);
    if (bar && ctxMax) {
      var pct = Math.min(100, maxSeq / ctxMax * 100);
      bar.hidden = false;
      bar.style.setProperty("--ctx-pct", pct.toFixed(2) + "%");
      bar.classList.toggle("warn", pct >= 85);
      bar.classList.toggle("crit", pct >= 95);
      if (hint) hint.textContent = F.formatTokenCount(maxSeq) + " / " + F.formatTokenCount(ctxMax) +
        " · " + F.formatPercent(pct);
    } else if (hint) {
      hint.textContent = "";
    }
  }

  /* ================= Data Quality（Overview 摘要 + History 页；UI-010 并行） ================= */

  /* 1.1.4 精修（§50-§55）：缺口/损耗/WAL 文案 —— 纯函数，便于单测。
     - 缺口值：0 -> "0"；N -> "N 个"。
     - 损耗：0 个可能丢失 -> "未发现 Token 丢失"；>0 -> "N 个可能存在 Token 丢失"。
     - WAL：已启用 / 未启用（不再出现 "WAL ·" 残缺）。 */
  function dqWording() {
    function gapValueText(gapCount) {
      var n = Number(gapCount) || 0;
      return n === 0 ? "0" : n + " 个";
    }
    function lossHintText(lossCount) {
      var n = Number(lossCount) || 0;
      return n === 0 ? "未发现 Token 丢失" : n + " 个可能存在 Token 丢失";
    }
    // 今日可能丢失 Token 的缺口数（recent_gaps 取起于今天的条目）
    function todayLossGapCount(q) {
      var now = new Date();
      var start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
      var arr = (q && q.recent_gaps) || [];
      var n = 0;
      for (var i = 0; i < arr.length; i++) {
        if (arr[i] && arr[i].possible_token_loss &&
            new Date(Number(arr[i].start) * 1000).getTime() >= start) n++;
      }
      return n;
    }
    function walText(h) {
      if (!h) return "";
      if (h.journal_mode) return h.journal_mode.toLowerCase() === "wal" ? "WAL 已启用" : "WAL 未启用";
      return "WAL 未启用";
    }
    return {
      gapValueText: gapValueText,
      lossHintText: lossHintText,
      todayLossGapCount: todayLossGapCount,
      walText: walText
    };
  }
  var DQ = dqWording();

  function renderDataQuality() {
    var q = state.quality, h = state.health;
    if (!q && !h) return;
    // Round-6：概览「监测完整性」5 项由 /api/overview 统一驱动（renderOvIntegrity）。
    // 此处仅在 overview 尚未首载时做 bootstrap 填充（同一 data-quality 数据）。
    if (!ovLoaded) {
    // 数值元素只叠加语义色 tone，保留字号类（mid 22px），
    // 此前整段覆写 className 会把字号类吞掉退化成 12px hint
    if (h) {
      var dbEl = $("dqDb");
      if (dbEl) {
        var tone = h.database === "healthy" ? "ok" : h.database === "warning" ? "warn" : "bad";
        dbEl.textContent = dbStatusLabel(h.database);  // UX-1111-007：中文化
        dbEl.className = "stat-value mid " + tone;
      }
      var hint = $("dqDbHint");
      if (hint) {
        // 1.1.4 精修 §53/§110：WAL 已启用 / WAL 未启用（不再出现 "WAL ·" 残缺）
        var wal = DQ.walText(h);
        hint.textContent = (h.database_detail && h.database_detail !== "normal"
          ? h.database_detail + " · " : "") + wal;
        if (h.application === "degraded") hint.textContent += " · 保护模式（只读）";
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
        gapsEl.textContent = DQ.gapValueText(gc); // "0" | "N 个"（§50/§52）
        // §55：coverage 正常 + 少量缺口（无 Token 丢失）-> 轻微 warn；
        // 存在可能 Token 丢失 -> bad；0 缺口 -> ok。
        gapsEl.className = "stat-value mid " + (gc === 0 ? "ok" : t.possible_token_loss ? "bad" : "warn");
      }
      var lossEl = $("dqLossToday");
      if (lossEl) {
        var lossCount = DQ.todayLossGapCount(q);
        // §88/§162：hint 挂在「今日采集缺口」项下，先描述缺口——
        // 0 缺口 -> "未发现采集缺口"；有缺口 -> 描述可能 Token 丢失。
        lossEl.textContent = gc === 0 ? "未发现采集缺口" : DQ.lossHintText(lossCount);
        lossEl.className = "stat-hint " + (gc === 0 ? "" : lossCount > 0 ? "bad" : "");
      }
      var openText = q.open_gap ? "持续缺口，始于 " + F.formatDateTime(q.open_gap.start) +
        (q.open_gap.reason ? "（" + (GAP_REASON_LABELS[q.open_gap.reason] || q.open_gap.reason) + "）" : "") +
        "，进行中" : "";
      // 概览数据质量卡：open gap 提示在卡底部（从"最近有效采样"项移出）
      var dqEl = $("dqOpenGap");
      if (dqEl) dqEl.textContent = openText;
      var wrap = $("dqOpenGapWrap");
      if (wrap) wrap.hidden = !openText;
    }
    } // !ovLoaded（概览 dq* 元素的 bootstrap 填充）
    // 历史页 Summary 的"最后采样 / 持续缺口"复用同一 open_gap（Range 无关，§191-196）
    // ——历史页元素（iqOpenGap 等）不受 overview 接管影响，始终更新。
    if (q) {
      var _ot = q.open_gap ? "持续缺口，始于 " + F.formatDateTime(q.open_gap.start) +
        (q.open_gap.reason ? "（" + (GAP_REASON_LABELS[q.open_gap.reason] || q.open_gap.reason) + "）" : "") +
        "，进行中" : "";
      var hqEl = $("iqOpenGap");
      if (hqEl) { hqEl.textContent = _ot; hqEl.style.display = _ot ? "" : "none"; }
      if (h && h.last_valid_sample_seconds_ago != null) {
        state._lastSampleAgo = h.last_valid_sample_seconds_ago;
        renderHistoryLastSample();
      }
    }
  }

  var GAP_REASON_LABELS = {
    server_offline: "llama.cpp 服务不可达",
    monitor_restart: "LlamaMonitor 重启",
    system_pause_or_sleep: "系统休眠",
    invalid_metrics: "采集超时 / 异常",
    unknown: "原因未确定",
  };
  // Round-5：来源 = 哪个数据源采样缺失（llama.cpp/GPU/系统/LlamaMonitor），弃"GPU 采集"式写法（§93-96）
  var GAP_SOURCE_LABELS = { llama: "llama.cpp", gpu: "GPU", system: "系统", application: "LlamaMonitor" };
  // AUDIT-1.1.1 UX-1111-007：数据库状态 raw 值（healthy/warning/unavailable…）
  // 中文化显示；未知值回退 raw，绝不显示空白。
  var DB_STATUS_LABELS = {
    healthy: "健康",
    warning: "警告",
    degraded: "降级（保护模式）",
    unavailable: "不可用",
  };
  function dbStatusLabel(s) { return s ? (DB_STATUS_LABELS[s] || s) : "--"; }

  // AUDIT-1.1.1 BUG-1111-002：HTML 属性转义（用于 title='…' 这类单引号包裹的属性值）。
  // 此前各处只转义 & < "，缺 ' ——details/reason 含单引号时 title 属性提前闭合，
  // 后续属性错位（tooltip 残缺 / class 被吞）。这里统一 5 字符转义并集中定义，
  // 供事件表与缺口表共用（此前事件表在循环内重复定义、缺口表干脆未转义）。
  function escAttr(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }
  function escText(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  /* ============================================================
   Round-5 History 页（监控历史）：全局时间范围 + 6 项 Summary +
   完整性趋势（点击筛缺口）+ 采集缺口（筛选/分页/inline 展开）+
   监控事件（Presentation Layer/筛选/搜索/Timeline）+ 4 项导出。
   ============================================================ */
  // 时间范围 -> 查询参数字符串（custom 用 start_date/end_date，其余用 preset）
  function historyRangeParams() {
    if (state.historyRange === "custom" && state.historyCustom) {
      return "start_date=" + state.historyCustom.start_date +
             "&end_date=" + state.historyCustom.end_date;
    }
    return "preset=" + (state.historyRange || "7d");
  }
  function historyRangeLabel() {
    if (state.historyRange === "custom" && state.historyCustom)
      return state.historyCustom.start_date + " ~ " + state.historyCustom.end_date;
    return { "24h": "近 24 小时", "7d": "近 7 天", "30d": "近 30 天", "all": "全部" }[state.historyRange] || "近 7 天";
  }
  // 当前时间是否跨日（09:29:41 → 09:29:48 同秒级；跨日 09-29 23:59:58 → 09-30 00:00:12）
  function gapTimeCell(g) {
    var s = g.start, e = g.end;
    if (!s) return "--";
    var ds = new Date(s * 1000), de = e ? new Date(e * 1000) : null;
    function hhmmss(d) { return F.pad2(d.getHours()) + ":" + F.pad2(d.getMinutes()) + ":" + F.pad2(d.getSeconds()); }
    function mmdd(d) { return F.pad2(d.getMonth() + 1) + "-" + F.pad2(d.getDate()); }
    var now = new Date();
    function sameDay(a, b) { return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate(); }
    var primary;
    if (de && sameDay(ds, de)) {
      // 同一天：当前日只显示 HH:MM:SS；非当前日显示 MM-DD HH:MM（§224-228）
      primary = sameDay(ds, now)
        ? hhmmss(ds) + " → " + hhmmss(de)
        : mmdd(ds) + " " + F.pad2(ds.getHours()) + ":" + F.pad2(ds.getMinutes()) + " → " + F.pad2(de.getHours()) + ":" + F.pad2(de.getMinutes());
    } else {
      primary = mmdd(ds) + " " + F.pad2(ds.getHours()) + ":" + F.pad2(ds.getMinutes()) + ":" + F.pad2(ds.getSeconds()) +
        " → " + (de ? mmdd(de) + " " + F.pad2(de.getHours()) + ":" + F.pad2(de.getMinutes()) + ":" + F.pad2(de.getSeconds()) : "进行中");
    }
    var full = F.formatDateTime(s) + (de ? " → " + F.formatDateTime(e) : "（进行中）");
    return { primary: primary, full: full, isToday: sameDay(ds, now) };
  }
  function renderHistoryLastSample() {
    var el = $("iqLastSample"), hint = $("iqLastSampleHint");
    if (!el) return;
    var q = state.historySummary;
    var age = (q && q.last_sample_seconds_ago != null) ? q.last_sample_seconds_ago : state._lastSampleAgo;
    var poll = state.pollIntervalSec || 5;
    if (age == null) { el.textContent = "--"; el.className = "stat-value small"; if (hint) hint.textContent = ""; return; }
    var stale = age > poll * 3;
    var nowTs = (q && q.server_now) ? q.server_now : (Date.now() / 1000);
    var abs = F.formatDateTime(nowTs - age);
    if (age < 5) { el.textContent = "刚刚"; el.className = "stat-value small ok"; }
    else if (!stale) { el.textContent = F.formatAgo(age); el.className = "stat-value small ok"; }
    else if (age < 60) { el.textContent = "采集已延迟 " + Math.round(age) + " 秒前"; el.className = "stat-value small warn"; }
    else { el.textContent = "采集已停止"; el.className = "stat-value small bad"; }
    if (hint) hint.textContent = "最后采样 " + abs + (stale ? " · 数据已过期" : "");
  }
  // 6 项 Summary 渲染（§30-35；数据库状态/最后采样不受 Range 影响）
  function renderHistorySummary() {
    var q = state.historySummary;
    // 数据库状态（来自 /api/health 的 schema_status 4 态，§240-262）
    var h = state.health || {};
    var dbEl = $("iqDb"), dbHint = $("iqDbHint");
    if (dbEl) {
      var st = h.schema_status || "normal";
      var dbMap = {
        normal: ["正常", "ok"], readonly_compat: ["只读兼容模式", "warn"],
        readonly: ["只读", "warn"], abnormal: ["异常", "bad"],
      };
      var m = dbMap[st] || ["--", ""];
      dbEl.textContent = m[0]; dbEl.className = "stat-value small " + m[1];
      if (dbHint) {
        if (st === "readonly_compat") {
          dbHint.textContent = "Schema v" + h.db_schema_version + " · 当前版本支持至 v" + h.app_schema_version;
          dbHint.className = "stat-hint warn";
          var b = $("historySchemaBanner");
          if (b) {
            b.hidden = false; b.className = "schema-banner amber";
            b.innerHTML = "<span class='sb-icon'></span><span>数据库由更新版本的 LlamaMonitor 创建。当前版本仅以只读方式打开，不会修改或降级数据库。（Schema v" +
              escText(h.db_schema_version) + " · 当前版本支持至 v" + escText(h.app_schema_version) + "）</span>";
            var ic = b.querySelector(".sb-icon"); if (ic && LM.icons) ic.innerHTML = LM.icons.get("warning");
          }
        } else if (st === "abnormal") {
          dbHint.textContent = h.database_detail || "数据库状态异常"; dbHint.className = "stat-hint bad";
          var b2 = $("historySchemaBanner");
          if (b2) { b2.hidden = false; b2.className = "schema-banner red"; b2.innerHTML = "<span class='sb-icon'></span><span>" +
            escText(h.database_detail || "数据库状态异常") + "。历史数据以只读方式展示。</span>";
            var ic2 = b2.querySelector(".sb-icon"); if (ic2 && LM.icons) ic2.innerHTML = LM.icons.get("error"); }
        } else {
          dbHint.textContent = "WAL " + (h.journal_mode === "wal" ? "已启用" : "未启用"); dbHint.className = "stat-hint";
          var b3 = $("historySchemaBanner"); if (b3) b3.hidden = true;
        }
      }
    }
    if (!q) return;
    // 采集覆盖率（范围内 Σ有效/Σ预期，§52-54）
    var covEl = $("iqCoverage"), covHint = $("iqCoverageHint");
    if (covEl) {
      if (q.coverage_percent == null) { covEl.textContent = F.NA; covEl.className = "stat-value mid"; }
      else { covEl.textContent = F.formatPercent(q.coverage_percent, 1); covEl.className = "stat-value mid " + (q.coverage_percent >= 99.9 ? "ok" : q.coverage_percent >= 95 ? "warn" : "bad"); }
      if (covHint) covHint.textContent = "当前范围 · " + historyRangeLabel();
    }
    // 范围内缺口（累计 + 时间归属不确定）
    var gapEl = $("iqGaps"), gapHint = $("iqGapsHint");
    if (gapEl) {
      gapEl.textContent = (q.gap_count_range || 0) + " 个";
      gapEl.className = "stat-value mid " + (q.gap_count_range === 0 ? "ok" : q.lost_count > 0 ? "bad" : "warn");
      if (gapHint) gapHint.textContent = "累计 " + (q.gap_count_total || 0) + " 个 · 范围缺口 " + F.formatDuration(q.total_gap_seconds_range || 0);
    }
    // Token 数据风险（§105-113；backend 2 bool -> 无已知风险/存在风险）
    var riskEl = $("iqRisk"), riskHint = $("iqRiskHint");
    if (riskEl) {
      if ((q.lost_count || 0) > 0) { riskEl.textContent = "存在风险"; riskEl.className = "stat-value small warn"; }
      else { riskEl.textContent = "无已知风险"; riskEl.className = "stat-value small ok"; }
      if (riskHint) {
        var parts = [];
        if (q.lost_count > 0) parts.push(q.lost_count + " 个缺口可能造成 Token 丢失");
        if (q.time_uncertain_count > 0) parts.push(q.time_uncertain_count + " 个时间归属不确定");
        riskHint.textContent = parts.length ? parts.join(" · ") : "范围内无 Token 数据风险";
        riskHint.className = "stat-hint" + ((q.lost_count || 0) > 0 ? " warn" : "");
      }
    }
    // 最后采样（Range 无关，§191-196）
    renderHistoryLastSample();
    // 监测开始时间
    var msEl = $("iqMonitorStart");
    if (msEl) msEl.textContent = q.monitoring_start_ts ? F.formatDateTime(q.monitoring_start_ts) : F.NA;
  }
  // 完整性趋势
  function renderHistoryTrend() {
    var t = state.historyTrend;
    var pts = (t && t.points) || [];
    charts.renderIntegrityTrend("historyTrendBox", "chartHistoryTrend", pts);
    var hint = $("trendGranHint");
    if (hint) {
      var gran = { hour: "按小时", day: "按日", week: "按周", month: "按月" }[t && t.bucket] || "";
      hint.textContent = "采集覆盖率 · " + gran + "（点击趋势对应时段可筛选采集缺口）";
    }
    // 点击趋势 -> 把缺口 Section 筛到对应桶 + scrollIntoView（§81-83，不弹 modal）。
    // 用容器 DOM click（非 ECharts 符号命中）：点击绘图区**任意位置**都能映射到最近桶，
    // 比 ECharts series 点击（需精确命中 4px 圆点）更稳。每次读最新 state.historyTrend.points，
    // 避免 Range 切换后闭包捕获旧 pts。
    var trendBoxEl = $("chartHistoryTrend");
    if (trendBoxEl && !trendBoxEl.__boundTrend) {
      trendBoxEl.__boundTrend = true;
      trendBoxEl.addEventListener("click", function (ev) {
        try {
          var ch = charts.chart("chartHistoryTrend");
          if (!ch) return;
          var rect = trendBoxEl.getBoundingClientRect();
          var px = [ev.clientX - rect.left, ev.clientY - rect.top];
          // seriesIndex 反解返回 [桶序号(分数, 已扣除网格边距), y值]；xAxisIndex 对 category 轴返回 null
          var val = ch.convertFromPixel({ seriesIndex: 0 }, px);
          var x = Array.isArray(val) ? val[0] : val;
          if (typeof x !== "number" || isNaN(x)) return;
          var all = (state.historyTrend && state.historyTrend.points) || [];
          if (!all.length) return;
          var idx = Math.round(x);
          // 仅当点击落在绘图区内（桶序号在 [-0.5, length-0.5]）
          if (idx >= 0 && idx < all.length && x >= -0.5 && x <= all.length - 0.5 && all[idx]) {
            onTrendBucketClick(all[idx]);
          }
        } catch (e) { /* 坐标换算异常不阻断其它交互 */ }
      });
    }
  }
  function onTrendBucketClick(pt) {
    // 趋势点击 → 把缺口筛选到该桶（§81-83）+ 滚动到缺口 Section。
    // 再次点击同一桶 = 取消筛选（切回全范围）。
    if (pt && pt.ts && pt.ts_end) {
      if (state.histGapBucket && state.histGapBucket.start === pt.ts && state.histGapBucket.end === pt.ts_end) {
        state.histGapBucket = null;
      } else {
        state.histGapBucket = { start: pt.ts, end: pt.ts_end };
      }
    }
    refreshHistoryGaps(true);
    var sec = $("gapsSection");
    if (sec) sec.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ---- 采集缺口：拉取（Range + 来源 + 风险 + cursor 分页，§82-119/217-235） ----
  function refreshHistoryGaps(reset, append) {
    // reset=true 范围/筛选变化清空重拉；append=true 显示更多 cursor 追加；
    // 否则 soft 刷新（轮询）原子替换第 1 页，不清屏（§303-304 避免闪烁）。
    var gen = ++state.histGapsGen;
    if (reset) { state.histGaps = []; state.histGapsCursor = null; state.histGapsHasMore = false; state.histGapExpanded = {}; state.histGapsShowAll = false; }
    if (!append) state.histGapsCursor = null;
    state.histGapsLoading = true;
    var params = historyRangeParams() + "&limit=20";
    if (state.histGapSource) params += "&source=" + state.histGapSource;
    if (state.histGapRisk) params += "&risk=" + state.histGapRisk;
    // 趋势点击 → 筛选到该桶（§81-83）
    if (state.histGapBucket) {
      params += "&sub_start_ts=" + state.histGapBucket.start + "&sub_end_ts=" + state.histGapBucket.end;
    }
    if (append && state.histGapsCursor) params += "&cursor=" + encodeURIComponent(state.histGapsCursor);
    api.get("/api/history/gaps?" + params).then(function (d) {
      if (gen !== state.histGapsGen) return; // 过期响应丢弃（§302 range/filter race）
      state.histGaps = append ? state.histGaps.concat(d.gaps || []) : (d.gaps || []);
      state.histGapsCursor = d.next_cursor;
      state.histGapsHasMore = !!d.has_more;
      state.histGapsLoading = false;
      // 拉完下一页后展开全部已加载项：desktop 单页量 == 首屏量，否则"显示更多"
      // 首击无可见变化（§114-119）
      if (append) state.histGapsShowAll = true;
      renderGapsTable();
    }).catch(function (e) { if (gen === state.histGapsGen) state.histGapsLoading = false; console.warn("history gaps failed:", e.message || e); });
  }
  function renderGapsTable() {
    var tbody = $("gapsTbody");
    if (!tbody) return;
    var gaps = state.histGaps || [];
    var wrap = $("gapsTableWrap"), empty = $("gapsEmpty");
    var cnt = $("gapsCountLabel");
    // 趋势时段筛选：标签提示当前筛选到的桶（再次点击趋势取消）
    if (cnt && state.histGapBucket) {
      var bp = null;
      if (state.historyTrend && state.historyTrend.points) {
        state.historyTrend.points.forEach(function (p) { if (p.ts === state.histGapBucket.start) bp = p; });
      }
      var seg = bp && bp.label ? " · 时段 " + bp.label : "";
      cnt.textContent = (gaps.length ? gaps.length + " 个已加载" : "0 个") + seg + "（点击趋势图可取消）";
      cnt.classList.add("warn");
    } else if (cnt) {
      cnt.textContent = (gaps.length ? gaps.length + " 个已加载" : "0 个");
      cnt.classList.remove("warn");
    }
    ui.setEmptyState(empty, gaps.length === 0);
    if (!gaps.length) { tbody.innerHTML = ""; if (wrap) wrap.style.display = "none"; var row = $("gapsMoreRow"); if (row) row.hidden = true; return; }
    if (wrap) wrap.style.display = "";
    // 分页（§114-119/217-220）：desktop 初始 20，mobile 初始 8；"显示更多"先展示
    // 本地已加载的余量，再用 cursor 拉下一页。无固定高度内部滚动，仅页面外层滚动。
    var isMobile = window.matchMedia("(max-width: 987px)").matches;
    var PAGE = isMobile ? 8 : 20;
    var limit = state.histGapsShowAll ? gaps.length : Math.min(PAGE, gaps.length);
    var shown = gaps.slice(0, limit);
    tbody.innerHTML = shown.map(function (g) {
      var tc = gapTimeCell(g);
      var src = GAP_SOURCE_LABELS[g.source] || g.source || "--";
      var riskCls = g.token_risk === "lost" ? " cell-bad" : g.token_risk === "time_uncertain" ? " cell-warn" : "";
      var reasonTitle = g.reason_inferred ? escAttr(g.reason_inferred_note || "推定") : escAttr(g.reason);
      return "<tr class='gap-row' data-id='" + g.id + "' data-date='" + escAttr(tc.primary) + "'>" +
        "<td data-label='时间' class='cell-time' title='" + escAttr(tc.full) + "'>" + escText(tc.primary) + "</td>" +
        "<td data-label='持续时间'>" + F.formatDuration(g.duration_seconds == null ? 0 : g.duration_seconds) + "</td>" +
        "<td data-label='来源'>" + escText(src) + "</td>" +
        "<td data-label='原因' class='cell-wrap' title='" + escAttr(reasonTitle) + "'>" + escText(g.reason_label || GAP_REASON_LABELS[g.reason] || "原因未确定") + "</td>" +
        "<td data-label='Token 风险' class='" + riskCls + "'>" + escText(g.token_risk_label || "无") + "</td>" +
        "</tr>" +
        (state.histGapExpanded[g.id] ? gapDetailRow(g) : "");
    }).join("");
    // 显示更多（本地余量优先；本地空了且有更多 -> 拉下一页）
    var moreRow = $("gapsMoreRow"), moreBtn = $("gapsMoreBtn");
    if (moreRow && moreBtn) {
      if (limit < gaps.length || state.histGapsHasMore) {
        moreRow.hidden = false;
        var nextLabel = limit < gaps.length
          ? "显示更多（还有 " + (gaps.length - limit) + " 条）"
          : "显示更多（还有更多）";
        moreBtn.textContent = nextLabel;
        moreBtn.onclick = function () {
          if (limit < gaps.length) { state.histGapsShowAll = true; renderGapsTable(); }
          else refreshHistoryGaps(false, true); // append=true：cursor 拉下一页（§217-235）
        };
      } else moreRow.hidden = true;
    }
    bindGapRowExpand();
  }
  function gapDetailRow(g) {
    var lines = ["时间 " + F.formatDateTime(g.start) + (g.end ? " → " + F.formatDateTime(g.end) : "")];
    lines.push("持续 " + F.formatDuration(g.duration_seconds == null ? 0 : g.duration_seconds));
    lines.push("来源 " + (GAP_SOURCE_LABELS[g.source] || g.source || "--"));
    lines.push("原因 " + (g.reason_label || "原因未确定") + (g.reason_inferred ? "（推定）" : ""));
    if (g.reason_inferred_note) lines.push("推定依据 " + g.reason_inferred_note);
    lines.push("Token 风险 " + (g.token_risk_label || "无") + "（raw " + g.reason + " / loss=" + (g.possible_token_loss ? 1 : 0) + "）");
    return "<tr class='gap-detail-row' data-id='" + g.id + "'><td colspan='5' data-label='详情'>" +
      "<div class='gap-detail'>" + lines.map(escText).join("<br>") + "</div></td></tr>";
  }
  function bindGapRowExpand() {
    document.querySelectorAll("#gapsTbody .gap-row").forEach(function (tr) {
      if (tr.dataset.bound) return; tr.dataset.bound = "1";
      tr.style.cursor = "pointer";
      tr.addEventListener("click", function () {
        var id = tr.dataset.id;
        state.histGapExpanded[id] = !state.histGapExpanded[id];
        renderGapsTable();
      });
    });
  }
  function initHistoryGapsFilters() {
    var s = $("gapSourceFilter"); if (s && !s.dataset.bound) { s.dataset.bound = "1"; s.addEventListener("change", function () { state.histGapSource = s.value; applyHistoryRange(true); }); }
    var r = $("gapRiskFilter"); if (r && !r.dataset.bound) { r.dataset.bound = "1"; r.addEventListener("change", function () { state.histGapRisk = r.value; applyHistoryRange(true); }); }
  }

  // ---- 监控事件（Presentation Layer 后端已给 display_*；前端筛选/搜索/Timeline，§128-184） ----
  function refreshHistoryEvents(reset, append) {
    // reset=true：范围/筛选变化 -> 清空重拉；append=true：显示更多 -> cursor 拉下一页
    // reset=false && append=false：轮询 soft 刷新 -> 不清空，拉第 1 页原子替换（避免闪烁，§303-304）
    var gen = ++state.histEventsGen;
    if (reset) { state.histEvents = []; state.histEventsCursor = null; state.histEventsHasMore = false; state._evExpanded = {}; }
    if (!append) state.histEventsCursor = null;
    state.histEventsLoading = true;
    var params = historyRangeParams() + "&limit=30";
    if (state.histEventCategory) params += "&category=" + encodeURIComponent(state.histEventCategory);
    if (state.histEventSeverity) params += "&min_severity=" + state.histEventSeverity;
    if (state.histEventSearch) params += "&search=" + encodeURIComponent(state.histEventSearch);
    if (append && state.histEventsCursor) params += "&cursor=" + encodeURIComponent(state.histEventsCursor);
    api.get("/api/events?" + params).then(function (d) {
      if (gen !== state.histEventsGen) return;
      // append（显示更多）追加；reset / soft 刷新原子替换第 1 页（soft 不清屏，避免闪烁）
      state.histEvents = append ? state.histEvents.concat(d.events || []) : (d.events || []);
      state.histEventsCursor = d.next_cursor;
      state.histEventsHasMore = !!d.has_more;
      state.histEventsLoading = false;
      // 分类 Filter 选项（后端给固定分类）
      if (d.categories && !$("evCategoryFilter").dataset.seeded) {
        var sel = $("evCategoryFilter");
        d.categories.forEach(function (c) { var o = document.createElement("option"); o.value = c; o.textContent = c; sel.appendChild(o); });
        sel.dataset.seeded = "1";
      }
      renderEventsList();
    }).catch(function (e) { if (gen === state.histEventsGen) state.histEventsLoading = false; console.warn("history events failed:", e.message || e); });
  }
  function evSeverityIcon(sev) {
    var map = { info: "info", success: "success", warning: "warning", error: "error" };
    var cls = sev === "error" ? " ev-sev-error" : sev === "warning" ? " ev-sev-warn" : sev === "success" ? " ev-sev-ok" : " ev-sev-info";
    var icon = LM.icons && LM.icons.get(map[sev] || "info");
    return "<span class='ev-sev" + cls + "' title='" + escAttr(sev) + "'>" + (icon || "") + "</span>";
  }
  function renderEventsList() {
    var events = state.histEvents || [];
    var empty = $("eventsEmpty"), wrapEl = $("eventsTableWrap"), tl = $("evTimeline");
    var cnt = $("eventsCountLabel");
    if (cnt) cnt.textContent = "当前范围 · " + events.length + " 条已加载";
    ui.setEmptyState(empty, events.length === 0);
    var isMobile = window.matchMedia("(max-width: 987px)").matches;
    if (isMobile) {
      if (wrapEl) wrapEl.style.display = "none";
      if (tl) { tl.hidden = false; renderEvTimeline(); }
    } else {
      if (tl) tl.hidden = true;
      renderEventsTable();
    }
    // 显示更多
    var moreRow = $("evMoreRow"), moreBtn = $("evMoreBtn");
    if (moreRow && moreBtn) {
      if (state.histEventsHasMore) { moreRow.hidden = false; moreBtn.textContent = "显示更多（还有更多）"; moreBtn.onclick = function () { refreshHistoryEvents(false, true); }; }
      else { moreRow.hidden = true; }
    }
  }
  function renderEventsTable() {
    var tbody = $("eventsTbody"), wrapEl = $("eventsTableWrap");
    if (!tbody) return;
    var events = state.histEvents || [];
    if (!events.length) { tbody.innerHTML = ""; if (wrapEl) wrapEl.style.display = "none"; return; }
    if (wrapEl) wrapEl.style.display = "";
    tbody.innerHTML = events.map(function (ev) {
      var full = F.formatDateTime(ev.timestamp);
      var time = F.formatClock(ev.timestamp);
      var detail = ui.humanizeEventDetails(ev) || "";
      var rawDetail = ""; try { rawDetail = ev.details && typeof ev.details === "object" ? JSON.stringify(ev.details) : (ev.details || ""); } catch (e) { rawDetail = String(ev.details || ""); }
      var src = ev.display_source || ev.source || "—";
      return "<tr class='ev-row' data-id='" + ev.id + "' data-date='" + (ev.timestamp ? String(ev.timestamp) : "") + "'>" +
        "<td data-label='时间' title='" + escAttr(full) + "'>" + escText(time) + "</td>" +
        "<td data-label='事件' class='ev-title'>" + evSeverityIcon(ev.display_severity) + escText(ev.display_title) + "</td>" +
        "<td data-label='来源'>" + escText(src) + "</td>" +
        "<td data-label='详情' class='ev-detail' title='" + escAttr(rawDetail || detail) + "'>" + escText(detail || ev.display_category) + "</td>" +
        "</tr>" +
        (state._evExpanded && state._evExpanded[ev.id] ? evDetailRow(ev, rawDetail) : "");
    }).join("");
    document.querySelectorAll("#eventsTbody .ev-row").forEach(function (tr) {
      if (tr.dataset.bound) return; tr.dataset.bound = "1";
      tr.style.cursor = "pointer";
      tr.addEventListener("click", function () {
        var id = tr.dataset.id;
        state._evExpanded = state._evExpanded || {};
        state._evExpanded[id] = !state._evExpanded[id];
        renderEventsTable();
      });
    });
  }
  function evDetailRow(ev, rawDetail) {
    var lines = [ev.display_category + " · " + ev.display_severity, "来源 " + (ev.display_source || ev.source || "--")];
    if (rawDetail) lines.push("原始事件信息 " + rawDetail);
    return "<tr class='ev-detail-row' data-id='" + ev.id + "'><td colspan='4' data-label='详情'>" +
      "<div class='ev-detailbox'>" + lines.map(escText).join("<br>") + "</div></td></tr>";
  }
  function renderEvTimeline() {
    var tl = $("evTimeline"); if (!tl) return;
    var events = state.histEvents || [];
    tl.innerHTML = events.map(function (ev) {
      var full = F.formatDateTime(ev.timestamp);
      var detail = ui.humanizeEventDetails(ev) || ev.display_category;
      return "<li class='ev-tl-item' data-date='" + (ev.timestamp ? String(ev.timestamp) : "") + "' title='" + escAttr(full) + "'>" +
        "<div class='ev-tl-time'>" + escText(F.formatClock(ev.timestamp)) + "</div>" +
        "<div class='ev-tl-line'><span class='ev-tl-dot'></span>" +
        "<div class='ev-tl-body'><div class='ev-tl-title'>" + escText(ev.display_title) + "</div>" +
        "<div class='ev-tl-detail'>" + escText(detail) + "</div></div></div></div>";
    }).join("");
  }
  function initHistoryEventFilters() {
    var c = $("evCategoryFilter"); if (c && !c.dataset.bound) { c.dataset.bound = "1"; c.addEventListener("change", function () { state.histEventCategory = c.value; applyHistoryRange(true); }); }
    var s = $("evSeverityFilter"); if (s && !s.dataset.bound) { s.dataset.bound = "1"; s.addEventListener("change", function () { state.histEventSeverity = s.value; applyHistoryRange(true); }); }
    var q = $("evSearch"); if (q && !q.dataset.bound) { q.dataset.bound = "1"; var t; q.addEventListener("input", function () { clearTimeout(t); t = setTimeout(function () { state.histEventSearch = q.value.trim(); applyHistoryRange(true); }, 300); }); }
  }

  // ---- Range 控件（segmented + 自定义日历，§18-24） ----
  function initHistoryRange() {
    var el = $("historyRange");
    if (el && !el.dataset.wired) {
      el.dataset.wired = "1";
      ui.segmented(el, [
        { value: "24h", label: "24 小时" },
        { value: "7d", label: "7 天" },
        { value: "30d", label: "30 天" },
        { value: "all", label: "全部" },
      ], state.historyRange, function (v) {
        state.historyRange = v; state.historyCustom = null; state.histGapBucket = null;
        applyHistoryRange(true);
      });
    }
    // 自定义范围弹层（复用 usage 的 range-popover 模式，独立 id）
    var btn = $("historyRangeCustom");
    if (btn && !btn.dataset.wired) {
      btn.dataset.wired = "1";
      var iconEl = $("historyRangeCustomIcon"); if (iconEl && LM.icons) iconEl.innerHTML = LM.icons.get("calendar");
      var pop = $("historyRangePopover"), startIn = $("historyRangeStart"), endIn = $("historyRangeEnd"), hint = $("historyRangeHint");
      if (pop) {
        function todayStr() { var d = new Date(); return d.getFullYear() + "-" + F.pad2(d.getMonth() + 1) + "-" + F.pad2(d.getDate()); }
        function close() { pop.hidden = true; btn.setAttribute("aria-expanded", "false"); }
        function open() {
          var today = todayStr();
          if (startIn) { startIn.max = today; if (!startIn.value) startIn.value = _addDays(today, -6); }
          if (endIn) { endIn.max = today; if (!endIn.value) endIn.value = today; }
          pop.hidden = false; var r = btn.getBoundingClientRect();
          var pw = pop.offsetWidth, ph = pop.offsetHeight;
          var left = Math.min(r.right - pw, window.innerWidth - pw - 8); if (left < 8) left = 8;
          var top = r.bottom + 6; if (top + ph > window.innerHeight - 8) top = Math.max(8, r.top - ph - 6);
          pop.style.left = left + "px"; pop.style.top = top + "px"; btn.setAttribute("aria-expanded", "true");
          if (endIn) endIn.focus();
        }
        function apply() {
          var s = startIn ? startIn.value : "", e = endIn ? endIn.value : "";
          if (!/^\d{4}-\d{2}-\d{2}$/.test(s) || !/^\d{4}-\d{2}-\d{2}$/.test(e)) { if (hint) { hint.textContent = "请选择开始与结束日期。"; hint.classList.add("bad"); } return; }
          if (s > e) { if (hint) { hint.textContent = "开始日期不能晚于结束日期。"; hint.classList.add("bad"); } return; }
          state.historyCustom = { start_date: s, end_date: e }; state.historyRange = "custom"; state.histGapBucket = null;
          close(); applyHistoryRange(true);
        }
        btn.addEventListener("click", function (ev) { ev.stopPropagation(); if (!pop.hidden) close(); else open(); });
        var cancel = $("historyRangeCancel"); if (cancel) cancel.addEventListener("click", close);
        var applyBtn = $("historyRangeApply"); if (applyBtn) applyBtn.addEventListener("click", apply);
        if (endIn) endIn.addEventListener("keydown", function (ev) { if (ev.key === "Enter") apply(); });
        document.addEventListener("click", function (ev) { if (!pop.hidden && !pop.contains(ev.target) && !btn.contains(ev.target)) close(); });
      }
    }
    initHistoryGapsFilters();
    initHistoryEventFilters();
  }
  // Range 变化：一次拉 summary + trend + gaps + events（§19-24 控制四个 Section；DB 状态/最后采样不重拉）
  function applyHistoryRange(hard) {
    // hard=true（进页 / 切 Range / 切 Filter）：缺口+事件清空重拉。
    // hard=false（轮询，§303-304）：soft 刷新——不清屏，第 1 页原子替换（慢 API 下不闪烁）。
    var gen = ++state.historyGen;
    if (!hard) state.histGapsShowAll = false;
    Promise.allSettled([
      api.get("/api/history/summary?" + historyRangeParams()),
      api.get("/api/history/trend?" + historyRangeParams()),
      api.get("/api/health"),
    ]).then(function (rs) {
      if (gen !== state.historyGen) return;
      if (rs[0].status === "fulfilled") state.historySummary = rs[0].value;
      if (rs[1].status === "fulfilled") state.historyTrend = rs[1].value;
      if (rs[2].status === "fulfilled") state.health = rs[2].value; // DB 状态（Range 无关，§240-262）
      renderHistorySummary();
      renderHistoryTrend();
    });
    refreshHistoryGaps(!!hard);
    refreshHistoryEvents(!!hard);
  }
  function refreshHistoryNow(hard) { applyHistoryRange(hard !== false); } // 默认硬刷新（进页/手动）；轮询传 false
  // 导出（遵守当前 Range；UTF-8 BOM，§197-213）
  function exportHistoryCsv(kind) {
    var params = historyRangeParams();
    if (kind === "gaps") {
      if (state.histGapSource) params += "&source=" + state.histGapSource;
      if (state.histGapRisk) params += "&risk=" + state.histGapRisk;
    }
    if (kind === "events") {
      if (state.histEventCategory) params += "&category=" + encodeURIComponent(state.histEventCategory);
      if (state.histEventSearch) params += "&search=" + encodeURIComponent(state.histEventSearch);
    }
    var url = { token: "/api/data/export/daily.csv", gpu: "/api/data/export/gpu_daily.csv",
                gaps: "/api/data/export/gaps.csv", events: "/api/data/export/events.csv" }[kind];
    // token/gpu daily 也遵守范围（daily.csv 支持 start_date/end_date；preset 不支持，custom 才传）
    window.location.href = url + "?" + params;
  }
  function bindHistoryExports() {
    var map = [ ["btnExportCsv", "token"], ["btnExportGpuCsv", "gpu"], ["btnExportGapsCsv", "gaps"], ["btnExportEventsCsv", "events"] ];
    map.forEach(function (pair) {
      var el = $(pair[0]);
      if (el && !el.dataset.bound) { el.dataset.bound = "1"; el.addEventListener("click", function () { exportHistoryCsv(pair[1]); }); }
    });
  }

  /* ================= GPU 页（；UI-002 签名重建） ================= */
  /* Round-4 GPU 页：性能限制原因 -> 展示名/级别（§43-51）。
     GPU Idle 是**运行状态**（空闲），不是性能限制，单独拆出；
     真实限制按 功耗 amber / 温度 amber-red / 硬件降速 warning 着色。 */
  var GPU_THROTTLE_MAP = [
    ["GPU Idle", "空闲", "idle"],
    ["SW Power Cap", "功耗限制", "power"],
    ["SW Power Capping Throttle", "功耗限制", "power"],
    ["HW Power Brake Slowdown", "功耗限制（硬件）", "power"],
    ["SW Thermal Slowdown", "温度限制", "thermal"],
    ["SW Thermal Capping Throttle", "温度限制", "thermal"],
    ["HW Thermal Slowdown", "温度限制（硬件）", "thermal"],
    ["HW Slowdown", "硬件降速", "hw"],
    ["Applications Clocks Setting", "应用时钟限制", "app"],
    ["Sync Boost", "Sync Boost", "other"],
  ];
  function gpuThrottleInfo(reasons) {
    var level = "none";
    var order = { none: 0, other: 1, app: 2, power: 3, thermal: 4, hw: 5 };
    var labels = [];
    (reasons || []).forEach(function (r) {
      var hit = null;
      for (var i = 0; i < GPU_THROTTLE_MAP.length; i++) if (GPU_THROTTLE_MAP[i][0] === r) { hit = GPU_THROTTLE_MAP[i]; break; }
      labels.push(hit ? hit[1] : r);
      var lv = hit ? hit[2] : "other";
      if (lv !== "idle" && order[lv] > order[level]) level = lv;
    });
    return { labels: labels, level: level, hasIdle: (reasons || []).indexOf("GPU Idle") > -1 };
  }

  /* 型号短名（selector chip / 能耗行）：去厂商前缀；UUID 稳定，index 重排不串卡 */
  function gpuShortName(name) {
    if (!name) return "";
    var s = String(name).replace(/^NVIDIA\s+/i, "").replace(/^[A-Z]+\s+Tesla\s+/i, "");
    return s || name;
  }

  /* 高级区"有值才渲染"的一行（key/value/可选 tooltip/可选 dim） */
  function _gpuAdvRow(k, v, tip, dim) {
    var row = document.createElement("div");
    row.className = "adv-row";
    var key = document.createElement("span");
    key.className = "k";
    key.textContent = k;
    if (tip) key.title = tip;
    var val = document.createElement("span");
    val.className = "v" + (dim ? " dim" : "");
    val.textContent = v;
    row.appendChild(key);
    row.appendChild(val);
    return row;
  }

  function _buildGpuCard(g) {
    var card = document.createElement("div");
    card.className = "gpu-device";
    card.dataset.uuid = g.uuid || "";
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

    // ---- 第一层：核心指标 2×2（GPU 利用率 / 显存占用 / 温度 / 功耗）----
    // 数值一律 primary text 色，不随阈值变色（§193-200）；N/A 用 -- 弱化。
    var vram = (g.memory_used_mb == null || g.memory_total_mb == null) ? F.NA :
      (g.memory_used_mb / 1024).toFixed(1) + " / " + (g.memory_total_mb / 1024).toFixed(1) + " GiB";
    var core = [
      ["GPU 利用率", g.utilization_percent == null ? F.NA : F.formatPercent(g.utilization_percent, 0)],
      ["显存占用", vram],
      ["温度", F.formatTemp(g.temperature_c)],
      ["功耗", F.formatPower(g.power_draw_w)],
    ];
    var coreGrid = document.createElement("div");
    coreGrid.className = "gd-core";
    core.forEach(function (r) {
      var cell = document.createElement("div");
      cell.className = "gd-cell";
      var k = document.createElement("div");
      k.className = "gd-k";
      k.textContent = r[0];
      var v = document.createElement("div");
      v.className = "gd-v" + (r[1] === F.NA ? " dim" : "");
      v.textContent = r[1];
      cell.appendChild(k);
      cell.appendChild(v);
      coreGrid.appendChild(cell);
      // VRAM 进度条跟随"显存占用"单元（视觉归属同一指标）
      if (r[0] === "显存占用" && g.memory_usage_percent != null) {
        var bar = document.createElement("div");
        bar.className = "vram-bar";
        var fill = document.createElement("div");
        fill.className = "fill";
        fill.style.width = Math.max(0, Math.min(100, g.memory_usage_percent)) + "%";
        bar.appendChild(fill);
        cell.appendChild(bar);
      }
    });
    card.appendChild(coreGrid);

    // ---- 第二层：紧凑元信息（风扇/P-State/SM 时钟/显存时钟/PCIe；无值整行隐藏）----
    var pcieCur = (g.pcie_generation == null || g.pcie_width == null) ? null : "Gen" + g.pcie_generation + " x" + g.pcie_width;
    var metaRows = [];
    if (g.fan_percent != null) metaRows.push(["风扇转速", F.formatPercent(g.fan_percent, 0)]);
    if (g.performance_state != null) metaRows.push(["P-State", g.performance_state]);
    if (g.sm_clock_mhz != null) metaRows.push(["SM 时钟", Math.round(g.sm_clock_mhz) + " MHz"]);
    if (g.memory_clock_mhz != null) metaRows.push(["显存时钟", Math.round(g.memory_clock_mhz) + " MHz"]);
    if (pcieCur) metaRows.push(["PCIe 链路", pcieCur]);
    if (metaRows.length) {
      var meta = document.createElement("div");
      meta.className = "gd-meta";
      metaRows.forEach(function (r) {
        meta.appendChild(_gpuAdvRow(r[0], r[1]));
      });
      card.appendChild(meta);
    }

    // ---- 第三层：高级信息（details；桌面/手机都默认折叠，§32-34）----
    var adv = document.createElement("details");
    adv.className = "gpu-adv";
    adv.dataset.uuid = g.uuid || "";   // 展开态记录/自动折叠按 uuid 跟踪（UUID 稳定不串卡）
    advSummaryEl(adv);
    var advHas = false;
    function advAdd(node) { adv.appendChild(node); advHas = true; }

    if (g.memory_controller_percent != null) {
      advAdd(_gpuAdvRow("显存控制器利用率", F.formatPercent(g.memory_controller_percent, 0),
        "显存控制器的繁忙程度，与「显存占用」（已用/总量）是不同指标。"));
    }
    if (g.power_limit_w != null) {
      var pct = g.power_percent != null ? " · 占上限 " + F.formatPercent(g.power_percent, 0) : "";
      advAdd(_gpuAdvRow("功耗上限", F.formatPower(g.power_limit_w) + (g.power_draw_w != null ? " · 当前 " + F.formatPower(g.power_draw_w) : "") + pct));
    } else if (g.power_percent != null) {
      advAdd(_gpuAdvRow("功耗占上限", F.formatPercent(g.power_percent, 0)));
    }
    if (g.pcie_gen_max != null && g.pcie_width_max != null) {
      advAdd(_gpuAdvRow("PCIe 最大能力", "Gen" + g.pcie_gen_max + " x" + g.pcie_width_max));
    }
    if (g.pci_bus_id) advAdd(_gpuAdvRow("PCI Bus ID", g.pci_bus_id));
    if (g.uuid) advAdd(_gpuAdvRow("UUID", g.uuid, "GPU 唯一标识（nvidia-smi）；曲线颜色与过滤按此保持稳定。"));
    if (g.compute_mode) advAdd(_gpuAdvRow("Compute Mode", g.compute_mode));
    if (g.persistence_mode) advAdd(_gpuAdvRow("Persistence Mode", g.persistence_mode));

    // 运行状态 vs 性能限制（§43-51）：运行状态是**两值字段**（空闲 / 负载中），
    // 恒显示——GPU Idle 只是"空闲"这一值，不作 Warning（中性）；性能限制只在存在
    // 真实 throttle 原因（非纯 idle）时才显示，按级别着色。
    var thInfo = gpuThrottleInfo(g.throttle_reasons);
    if (thInfo.hasIdle) {
      advAdd(_gpuAdvRow("运行状态", "空闲（GPU Idle）", "GPU 当前无负载；这是运行状态，不是故障或性能限制。"));
    } else {
      advAdd(_gpuAdvRow("运行状态", "负载中", "GPU 当前有负载。"));
    }
    var realReasons = thInfo.labels.filter(function (l) { return l !== "空闲"; });
    if (realReasons.length && thInfo.level !== "none") {
      var lRow = _gpuAdvRow("性能限制", realReasons.join("、"),
        "nvidia-smi clocks_event_reasons：当前生效的性能限制原因（非故障告警）。");
      lRow.classList.add("throttle-row", "level-" + thInfo.level);
      advAdd(lRow);
    }

    // ---- ECC 健康（ecc == null -> 整个区块隐藏；unsupported 组不占位，§53-65）----
    if (g.ecc) {
      var ecc = g.ecc;
      var eccBlock = document.createElement("div");
      eccBlock.className = "gpu-ecc";
      var eccHead = document.createElement("div");
      eccHead.className = "gpu-ecc-head";
      eccHead.textContent = "ECC 模式 " + (ecc.enabled ? "已启用" : "已禁用");
      eccBlock.appendChild(eccHead);
      var eccKv = document.createElement("div");
      eccKv.className = "gpu-kv";
      function eccRow(label, val, tip) {
        var row = _gpuAdvRow(label, val == null ? F.NA : F.formatInt(val), tip, val == null);
        eccKv.appendChild(row);
      }
      eccRow("可纠正 · 易失性计数", ecc.corrected_volatile,
        "可纠正 ECC 错误（Volatile）：自上次重置（开机/驱动重启）以来的计数。");
      eccRow("可纠正 · 累计计数", ecc.corrected_aggregate,
        "可纠正 ECC 错误（Aggregate）：该 GPU 生命周期的累计计数。");
      eccRow("不可纠正 · 易失性计数", ecc.uncorrected_volatile,
        "不可纠正 ECC 错误（Volatile）：自上次重置以来；通常意味着显存数据损坏。");
      eccRow("不可纠正 · 累计计数", ecc.uncorrected_aggregate,
        "不可纠正 ECC 错误（Aggregate）：该 GPU 生命周期的累计计数。");
      if (ecc.retired_pages_single_bit != null || ecc.retired_pages_double_bit != null) {
        eccRow("退役页 · 单比特", ecc.retired_pages_single_bit,
          "因单比特 ECC 错误退役的显存页（硬件已隔离）。");
        eccRow("退役页 · 双比特", ecc.retired_pages_double_bit,
          "因双比特 ECC 错误退役的显存页（硬件已隔离）。");
        if (ecc.retired_pages_pending != null) {
          eccRow("退役页 · 待处理", ecc.retired_pages_pending ? "是" : "否",
            "Pending Page Blacklist：已标记、待重启后正式退役的页。");
        }
      }
      if (ecc.remapped_rows != null) {
        eccRow("重映射行", ecc.remapped_rows,
          "Remapped Rows：显存行重映射计数（部分架构支持）。");
      }
      eccBlock.appendChild(eccKv);
      advAdd(eccBlock);
    }

    // 无高级数据不渲染空壳 details
    if (advHas) {
      adv.open = !!(state.gpuAdvOpen && state.gpuAdvOpen[g.uuid]); // 重建时保留用户展开态
      card.appendChild(adv);
    }
    return card;
  }

  /* 高级区 summary（chevron 由 CSS ::before 提供；桌面手机都可见，§32-34 默认折叠） */
  function advSummaryEl(adv) {
    var advSummary = document.createElement("summary");
    advSummary.textContent = "高级信息";
    adv.appendChild(advSummary);
    adv.addEventListener("toggle", function () {
      if (adv.dataset.uuid) state.gpuAdvOpen[adv.dataset.uuid] = adv.open;
    });
  }

  function applyGpuStatus(d) {
    state.gpuStatus = d;
    var box = $("gpuCards");
    if (!box) return;
    // 可用性状态（§13-14：正常显示"采集正常"，异常显示"不可用"+原因）
    var stateEl = $("gpuPageState");
    if (stateEl) {
      ui.setStatusBadge(stateEl, d.available ? "online" : "offline",
        d.available ? "采集正常" : "不可用");
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
      var card = _buildGpuCard(g);
      // §13-14：数据已过期（stale > 3×poll_interval）时保 last-known 值 + Stale 徽标
      var stale = d.stale_seconds != null && d.poll_interval_seconds != null &&
        d.stale_seconds > 3 * d.poll_interval_seconds;
      if (stale) {
        var badge = document.createElement("span");
        badge.className = "gpu-stale";
        badge.textContent = "数据已过期";
        badge.title = "最近一次成功采样在 " + Math.round(d.stale_seconds) + " 秒前（超过 3 个采样周期）。显示值为最后已知值。";
        card.appendChild(badge);
        card.classList.add("is-stale");
      }
      box.appendChild(card);
    });
    // ---- 驱动版本 + GPU 计数（页头右侧；取首个非空驱动） ----
    var drv = $("gpuDriverVer");
    if (drv) {
      var dv = null;
      gpus.forEach(function (g) { if (g.driver_version && !dv) dv = g.driver_version; });
      if (dv) {
        drv.textContent = "驱动 " + dv;
        drv.style.display = "";
      } else {
        drv.style.display = "none";
      }
    }
    var cnt = $("gpuGpuCount");
    if (cnt) {
      cnt.textContent = gpus.length + " 张 GPU";
      cnt.style.display = "";
    }
    // ---- 1.1 GPU 进程（只读；WDDM 下 used_memory 常 null -> --） ----
    renderGpuProcesses(d.processes || []);
    renderGpuPick(d.detected || [], d.gpu_uuids_monitored || []);
    // Round-6：概览「GPU 状态」改由 /api/overview 统一驱动（renderOvGpu）。
    // overview 尚未首载时由 /api/gpu/status bootstrap 填充概览迷你卡（GPU 页
    // 用独立的 gpuCards/gpuPageState，不受影响）；首载后不再覆写。
    if (!ovLoaded) renderGpuOverviewSummary(d);
  }

  /* Round-4 GPU 进程（§143-176）：
     - 只读（不结束进程）；列 = 应用 / PID / GPU / 显存占用；
     - 应用列 basename 为主，完整路径走 title tooltip（不撑爆表）；
     - 类型列删（nvidia-smi 580 compute-apps 无 type 字段，实测报 "not a valid field"）；
     - 排序 GPU -> 显存降序 -> PID；跟随 GPU selector 过滤（未选中的卡不显示其进程）；
     - >20 进程默认 20 + 「显示全部 N 个」；
     - 显存 N/A -> "不可用"（绝不 0 MiB）；整列 N/A 时弱化 + 表头说明；
     - 无内部 vertical scroll（.table-flat 语义，页面外层滚动）；
     - ≤760px 由 CSS 把每行转 2 列 definition grid（path ellipsis，可点展开）。 */
  var GPU_PROC_LIMIT = 20;
  function _procBasename(name) {
    if (!name) return "";
    var n = String(name).replace(/\\/g, "/");
    var i = n.lastIndexOf("/");
    return i >= 0 ? n.slice(i + 1) : n;
  }
  function renderGpuProcesses(processes) {
    var box = $("gpuProc");
    if (!box) return;
    box.innerHTML = "";
    var empty = document.createElement("div");
    empty.className = "gpu-proc-empty";
    empty.textContent = "当前无 GPU 进程（或 WDDM 下未报告）";

    var gpus = (state.gpuStatus && state.gpuStatus.gpus) || [];
    var detected = (state.gpuStatus && state.gpuStatus.detected) || [];
    var uuidTo = {};
    detected.forEach(function (g) {
      if (g.uuid != null) uuidTo[g.uuid] = { index: g.index, name: g.name };
    });
    gpus.forEach(function (g) {
      if (g.uuid != null && !uuidTo[g.uuid]) uuidTo[g.uuid] = { index: g.index, name: g.name };
    });

    // 跟随 GPU selector：未选中的卡不显示其进程；无选中信息时全显示
    var selOn = {};
    var anySel = false;
    Object.keys(state.gpuVisible || {}).forEach(function (u) {
      anySel = true;
      selOn[u] = state.gpuVisible[u] !== false;
    });
    var rows = (processes || []).filter(function (p) {
      if (!anySel) return true;
      var uu = p.gpu_uuid;
      if (uu == null) return true;
      return selOn[uu] !== false;
    }).map(function (p) {
      var info = (p.gpu_uuid != null && uuidTo[p.gpu_uuid]) || {};
      return {
        pid: p.pid,
        name: p.process_name || "[未知进程]",
        idx: info.index == null ? 999 : info.index,
        gpuName: info.name || "",
        gpuLabel: info.index == null ? "--" : "GPU " + info.index,
        mem: p.used_memory_mb,
      };
    });
    rows.sort(function (a, b) {
      if (a.idx !== b.idx) return a.idx - b.idx;
      var ma = a.mem == null ? -1 : a.mem, mb = b.mem == null ? -1 : b.mem;
      if (ma !== mb) return mb - ma;
      return a.pid - b.pid;
    });
    var allNa = rows.length > 0 && rows.every(function (r) { return r.mem == null; });

    if (!rows.length) { box.appendChild(empty); return; }

    // 桌面表格（≤760px 由 CSS 转卡片；无内部滚动）
    var table = document.createElement("table");
    table.className = "table-full gpu-proc-table";
    table.style.width = "100%";
    var thead = document.createElement("thead");
    var hrow = document.createElement("tr");
    var memTh = "显存占用" + (allNa ? " <span class='stat-hint'>（该模式未报告）</span>" : "");
    hrow.innerHTML =
      "<th>应用</th><th>PID</th><th>GPU</th><th>" + memTh + "</th>";
    thead.appendChild(hrow);
    table.appendChild(thead);
    var tbody = document.createElement("tbody");
    var showAll = !!state.gpuProcShowAll;
    var shown = showAll ? rows : rows.slice(0, GPU_PROC_LIMIT);
    shown.forEach(function (r) {
      var tr = document.createElement("tr");
      var memTxt = r.mem == null ? "不可用" : (r.mem / 1024).toFixed(2) + " GiB";
      var memTip = r.mem == null ? "该 GPU/驱动（WDDM）未报告此进程显存占用" : "";
      // data-label 供 mobile Process Card 的 CSS ::before 显示字段名（§218-227）
      tr.innerHTML =
        "<td class='proc-app' title='" + (r.name).replace(/'/g, "&#39;") + "'>" + _procBasename(r.name) + "</td>" +
        "<td class='tnum' data-label='PID'>" + F.formatInt(r.pid) + "</td>" +
        "<td data-label='GPU'" + (r.gpuName ? " title='" + r.gpuName.replace(/'/g, "&#39;") + "'" : "") + ">" + r.gpuLabel + "</td>" +
        "<td class='tnum" + (r.mem == null ? " dim" : "") + "' data-label='显存占用'" + (memTip ? " title='" + memTip + "'" : "") + ">" + memTxt + "</td>";
      // mobile：卡头（应用 basename）点击展开完整路径（path ellipsis 点击展开，§218-227）
      var appCell = tr.querySelector(".proc-app");
      var base = _procBasename(r.name);
      if (appCell && base !== r.name) {
        appCell.addEventListener("click", function () {
          var expanded = appCell.textContent !== r.name;
          appCell.textContent = expanded ? r.name : base;
          appCell.style.whiteSpace = expanded ? "normal" : "nowrap";
          appCell.style.wordBreak = expanded ? "break-all" : "normal";
        });
      }
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    box.appendChild(table);

    // 底部说明（WDDM 整列不可用时）+ 显示全部
    var foot = document.createElement("div");
    foot.className = "gpu-proc-foot";
    if (allNa) {
      var note = document.createElement("span");
      note.className = "stat-hint";
      note.textContent = "Windows 桌面（WDDM）下，GPU 进程显存占用常不报告；此为正常现象，非 0。";
      foot.appendChild(note);
    }
    if (rows.length > GPU_PROC_LIMIT) {
      var more = document.createElement("button");
      more.type = "button";
      more.className = "btn small subtle gpu-proc-more";
      more.textContent = showAll ? "收起" : ("显示全部 " + rows.length + " 个");
      more.addEventListener("click", function () {
        state.gpuProcShowAll = !state.gpuProcShowAll;
        renderGpuProcesses(state.gpuStatus ? (state.gpuStatus.processes || []) : []);
      });
      foot.appendChild(more);
    }
    if (foot.children.length) box.appendChild(foot);
  }

  /* Overview 页 GPU 摘要（迷你卡 4 指标 + 详情链接；
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
    ui.setStatusBadge(stateEl, "online", gpus.length + " 张 GPU");
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
        ["GPU 利用率", g.utilization_percent == null ? F.NA : F.formatPercent(g.utilization_percent, 0)],
        ["显存占用", vramText],
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

  function renderGpuPick(detected, monitored) {
    var box = $("gpuPick");
    if (!box) return;
    var sig = JSON.stringify((detected || []).map(function (g) { return g.uuid + "|" + g.index; })) +
      "#" + JSON.stringify(monitored || []);
    if (sig === state.gpuPickSig && box.children.length) return; // UI-002：未变不重建
    state.gpuPickSig = sig;
    box.innerHTML = "";
    if (!detected || !detected.length) {
      box.style.display = "none";
      return;
    }
    box.style.display = "";
    // device_uuids 非空 = 只监控指定卡；检测到但未选中的卡打"未监控"标记
    // （chip 变暗 + 名称后缀 + tooltip 说明），仍可见可勾选（Settings 改选后生效）。
    var monSet = {};
    (monitored || []).forEach(function (u) { monSet[u] = true; });
    var monFilterOn = (monitored || []).length > 0;
    detected.forEach(function (g) {
      var unmon = monFilterOn && !monSet[g.uuid];
      // 未监控的卡默认不在曲线中显示（无实时数据，画出来是误导）；
      // 用户可手动勾选查看其历史。监控中的卡保持"默认显示"。
      if (state.gpuVisible[g.uuid] === undefined) state.gpuVisible[g.uuid] = !unmon;
      // Fluent Check Chip——保留原生 checkbox 语义/键盘访问，
      // 视觉为可点击 chip；完整名称 +UUID 走 title tooltip。
      var label = document.createElement("label");
      label.className = "check-chip" + (state.gpuVisible[g.uuid] ? " on" : "") + (unmon ? " unmonitored" : "");
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
      // Round-4 §67-75：chip 用短名（去 NVIDIA/Tesla 前缀），完整名 + UUID 走 tooltip
      var short = gpuShortName(g.name);
      var txt = document.createElement("span");
      txt.className = "chip-text";
      txt.textContent = "GPU " + (g.index == null ? "?" : g.index) +
        (short ? " \u00B7 " + short : "") + (unmon ? " \u00B7 \u672A\u76D1\u63A7" : "");
      label.appendChild(txt);
      var tipParts = ["GPU " + (g.index == null ? "?" : g.index)];
      if (g.name) tipParts.push(g.name);
      if (g.uuid) tipParts.push("UUID " + g.uuid);
      if (unmon) tipParts.push("未选中监控（在设置的 GPU 筛选中取消勾选的历史卡，无实时数据）");
      label.title = tipParts.join("\n");
      box.appendChild(label);
    });
  }

  function redrawGpuCharts() {
    charts.renderGpuUtilChart("chartGpuUtilBox", "chartGpuUtil", state.gpuLive, state.gpuVisible);
    charts.renderGpuPowerChart("chartGpuPowerBox", "chartGpuPower", state.gpuLive, state.gpuVisible);
    charts.renderGpuTempChart("chartGpuTempBox", "chartGpuTemp", state.gpuLive, state.gpuVisible);
    // Round-4 更多趋势（折叠区）：容器 0 高时 chart() 懒初始化自动跳过，展开后 ResizeObserver 补 resize
    charts.renderGpuFanChart("chartGpuFanBox", "chartGpuFan", state.gpuLive, state.gpuVisible);
    charts.renderGpuClockChart("chartGpuClockBox", "chartGpuClock", state.gpuLive, state.gpuVisible);
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
      var shortName = gpuShortName(g.name);
      var label = (g.index != null ? "GPU " + g.index + " " : "") + (shortName ? shortName + " - " : "");
      // Round-4 §122-131：无功耗遥测（power 恒 N/A）的卡绝不显示"今日能耗 0Wh"，
      // 改显"不可估算（无功耗遥测）"；power_available 由后端给出（任一日有功耗采样）。
      if (!g.power_available) {
        any = true;
        var row = document.createElement("div");
        row.className = "energy-row";
        var k = document.createElement("span");
        k.className = "k";
        k.textContent = label + "今日能耗（估算）";
        k.title = "该 GPU 未报告功耗遥测（power.draw 不可用），能耗无法积分估算。";
        var v = document.createElement("span");
        v.className = "v dim";
        v.textContent = "不可估算";
        row.appendChild(k);
        row.appendChild(v);
        box.appendChild(row);
        return;
      }
      if (!day || day.energy_wh == null) return;
      any = true;
      var row2 = document.createElement("div");
      row2.className = "energy-row";
      var k2 = document.createElement("span");
      k2.className = "k";
      k2.textContent = label + "今日能耗（估算）";
      var v2 = document.createElement("span");
      v2.className = "v";
      v2.textContent = F.formatEnergy(day.energy_wh);
      row2.appendChild(k2);
      row2.appendChild(v2);
      box.appendChild(row2);
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
      dd.textContent = "根据采样功耗随时间积分估算，仅供参考。首个样本采集后显示。";
      empty.appendChild(ic);
      empty.appendChild(tt);
      empty.appendChild(dd);
      box.appendChild(empty);
    }
  }

  /* ================= MTP（Performance 页；AUDIT-DATA-002 单一来源） ================= */
  /* Overview「Draft Token 接受率」三态（1.1.4 精修 §35，绝不恒 "--"）：
     MTP 启用 + 有样本 -> "78.5%"；MTP 未启用 -> "未启用"；启用但今日暂无样本 -> "暂无样本"。
     能力位来自 /api/runtime capabilities.mtp（state.mtpCapable）；样本来自 /api/mtp。
     注意：/api/mtp 返回 accept_rate 即代表 MTP 启用且有数据（不依赖 capabilities 是否已加载）。 */
  function renderOvMtpRate() {
    var m = state.mtp;
    if (!m) return;
    // Round-6：概览页 MTP 接受率改由 /api/overview 统一驱动（renderOvInference）。
    // overview 已加载后，legacy /api/mtp 轮询不再覆写（避免两数据源互相打架）。
    if (ovLoaded) return;
    var text;
    if (m.accept_rate != null) {
      // 有样本 = MTP 启用且已产生 draft（capabilities 尚未到达也能正确显示百分比）
      text = F.formatPercent(m.accept_rate);
    } else if (state.mtpCapable) {
      text = "暂无样本";
    } else {
      text = "未启用";
    }
    setStatValue("ovMtpRate", text);
    var el = $("ovMtpRate");
    if (el) el.classList.toggle("dim", text === "未启用" || text === "暂无样本");
  }

  /* MTP 启用判定（§113：综合 slot.speculative / speculative.types / MTP metrics，
     不只 draft_total>0）。*/
  function mtpEnabled() {
    if (state.mtpCapable) return true;
    var slots = state.slotsData && state.slotsData.slots;
    if (slots) {
      for (var i = 0; i < slots.length; i++) {
        var s = slots[i];
        if (s.speculative) return true;
        var st = s.params && s.params["speculative.types"];
        if (st && st !== "none" && st !== "") return true;
      }
    }
    return false;
  }

  /* ================= Round-6 概览页（/api/overview 只读聚合端点驱动） =================
   一次请求返回 service / usage_today / inference / system / gpus / integrity / attention
   各域摘要。前端按域独立渲染与错误隔离（单域不可用只影响该 Section）。
   - 竞态：gen 自增，响应回来时 gen 已变则丢弃（慢响应不覆盖新数据）。
   - loading 语义：首轮为占位 "--"（非 0）；后续失败保留上次值。
   - attention 只消费后端聚合好的 item（不重新发明健康判断）。 */
  var ovData = null;      // 最近一次 /api/overview 完整响应
  var ovGen = 0;          // 竞态代号（每次请求自增）
  var ovLoaded = false;   // 是否已取到首份数据（决定 loading 占位）

  function refreshOverview() {
    ovGen++;
    var gen = ovGen;
    return api.get("/api/overview")
      .then(function (d) {
        if (gen !== ovGen) return; // 已有更新的请求
        ovData = d;
        ovLoaded = true;
        renderOverview(d);
      })
      .catch(function (e) {
        if (gen !== ovGen) return;
        console.warn("overview failed:", e.message || e);
        // 首屏失败：保留占位；后续失败：保留上次值（不清屏）
      });
  }

  /* 服务状态卡（就绪 / 不可达 / 监测异常 + 上下文窗口 / 并发 Slot / 模态 / 最后更新）。
     注意：全局「检测中 / 模型加载中」状态由 /api/status 的 applyStatus 驱动（更细粒度，
     含模型加载中间态）；/api/overview 的服务状态用于「需要关注」与右侧元信息。 */
  function renderOvService(s) {
    if (!s) return;
    // 服务卡右侧元信息（上下文窗口 / 并发 Slot / 模态）
    var ctxEl = $("ovContext");
    if (ctxEl) {
      if (s.context_window != null) {
        ctxEl.textContent = F.formatTokenCount(s.context_window);
        ctxEl.title = String(s.context_window).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
      } else { ctxEl.textContent = F.NA; ctxEl.title = ""; }
    }
    var slotsEl = $("ovSlots");
    if (slotsEl) slotsEl.textContent = (s.model && s.model.total_slots != null) ? String(s.model.total_slots) : F.NA;
    var modalEl = $("ovModal");
    if (modalEl) {
      var m = s.model || {};
      var parts = [];
      if (m.vision_supported) parts.push("视觉");
      if (m.video_supported) parts.push("视频");
      if (m.audio_supported) parts.push("音频");
      modalEl.textContent = parts.length ? parts.join(" · ") : "文本";
    }
    // 「最后更新 N 秒前」由全局 1s ticker（updateLastUpdateText）统一驱动
    // （更及时 + 处理离线/后端不可达），此处不覆写避免冲突。
  }

  /* 需要关注（仅当存在 item 时显示；最多 3 条 + 还有 N 项）。 */
  function renderOvAttention(a) {
    var box = $("ovAttention");
    if (!box) return;
    var list = $("ovAttentionList");
    var more = $("ovAttentionMore");
    if (!a || a.hidden || !a.items || !a.items.length) {
      box.hidden = true;
      if (list) list.innerHTML = "";
      if (more) more.hidden = true;
      return;
    }
    box.hidden = false;
    list.innerHTML = "";
    a.items.forEach(function (it) {
      var row = document.createElement("div");
      row.className = "ov-attention-item";
      row.setAttribute("data-sev", it.severity || "info");
      var dot = document.createElement("span");
      dot.className = "ov-attention-dot";
      var body = document.createElement("div");
      body.className = "ov-attention-body";
      var title = document.createElement("div");
      title.className = "ov-attention-title";
      title.textContent = it.title || "";
      body.appendChild(title);
      if (it.subtitle) {
        var sub = document.createElement("div");
        sub.className = "ov-attention-sub";
        sub.textContent = it.subtitle;
        body.appendChild(sub);
      }
      row.appendChild(dot);
      row.appendChild(body);
      list.appendChild(row);
    });
    if (more) {
      if (a.remaining > 0) {
        $("ovAttentionMoreText").textContent = "还有 " + a.remaining + " 项";
        more.hidden = false;
      } else { more.hidden = true; }
    }
  }

  /* 今日用量（6 指标：总量/实际计算 大数字 + 输入/缓存复用/输出/缓存复用率）。 */
  function renderOvUsage(u) {
    if (!u) return;
    setStatValue("ovTodayLogical", F.formatTokenCount(u.logical_tokens));
    setFullTip("ovTodayLogical", u.logical_tokens);
    setStatValue("ovTodayCompute", F.formatTokenCount(u.compute_tokens));
    setFullTip("ovTodayCompute", u.compute_tokens);
    setStatValue("ovTodayPrompt", F.formatTokenCount(u.prompt_tokens));
    setFullTip("ovTodayPrompt", u.prompt_tokens);
    setStatValue("ovTodayCached", F.formatTokenCount(u.cached_tokens));
    setFullTip("ovTodayCached", u.cached_tokens);
    setStatValue("ovTodayOutput", F.formatTokenCount(u.output_tokens));
    setFullTip("ovTodayOutput", u.output_tokens);
    // 缓存复用率：分母 0 -> None -> "--"（不显示 0%）
    setStatValue("ovTodayCacheRate", u.cache_reuse_rate_percent != null
      ? F.formatPercent(u.cache_reuse_rate_percent) : F.NA);
  }

  /* 推理状态（6 指标：Prompt TPS / Decode TPS / 活动请求 / 等待请求 / MTP 接受率 / 当前上下文）。
     TPS 三态（绝不混淆 0 与不可用）：
     - unavailable（离线 / 指标不受支持）-> "--"
     - idle（指标受支持但本轮无 delta）-> "0 t/s" + 副标「当前无对应推理活动」
     - active -> "N tok/s" */
  function renderOvInference(i) {
    if (!i) return;
    _ovTps("ovPromptTps", "ovPromptTpsSub", i.prompt_tps);
    _ovTps("ovDecodeTps", "ovDecodeTpsSub", i.decode_tps);
    setStatValue("ovRequestsProcessing", i.requests_processing != null ? F.formatInt(i.requests_processing) : F.NA);
    setStatValue("ovRequestsDeferred", i.requests_deferred != null ? F.formatInt(i.requests_deferred) : F.NA);
    // MTP 接受率四态：未启用 / 暂无数据 / 服务不可达 / 百分比
    var mtpEl = $("ovMtpRate");
    if (mtpEl) {
      var mtp = i.mtp || {};
      var text, dim = false;
      if (mtp.state === "value") { text = F.formatPercent(mtp.accept_rate); }
      else if (mtp.state === "disabled") { text = "未启用"; dim = true; }
      else if (mtp.state === "no_data") { text = "暂无数据"; dim = true; }
      else { text = F.NA; dim = true; }
      mtpEl.textContent = text;
      mtpEl.classList.toggle("dim", dim);
    }
    // 当前上下文（§85-§91 优先级：可靠 Slot 占用 > Busy Slot/总Slot > 省略）
    var ccEl = $("ovCurrentContext"), ccSub = $("ovCurrentContextSub");
    if (ccEl) {
      var cc = i.current_context;
      if (cc && cc.display === "usage" && cc.limit) {
        ccEl.textContent = F.formatTokenCount(cc.used) + " / " + F.formatTokenCount(cc.limit);
        var pct = Math.min(100, (cc.used / cc.limit) * 100);
        ccEl.title = F.formatPercent(pct);
        if (ccSub) { ccSub.textContent = "使用率 " + F.formatPercent(pct); ccSub.hidden = false; }
      } else if (cc && cc.display === "busy") {
        ccEl.textContent = (cc.busy_slots == null ? F.NA : String(cc.busy_slots)) +
          " / " + (cc.total_slots != null ? String(cc.total_slots) : F.NA) + " Slot";
        ccEl.title = "忙碌 Slot / 总 Slot";
        if (ccSub) { ccSub.textContent = ""; ccSub.hidden = true; }
      } else {
        ccEl.textContent = F.NA;
        if (ccSub) { ccSub.textContent = ""; ccSub.hidden = true; }
      }
    }
  }
  function _ovTps(valId, subId, d) {
    var el = $(valId), sub = $(subId);
    if (!el) return;
    if (!d) { el.textContent = F.NA; el.classList.add("dim"); if (sub) sub.hidden = true; return; }
    if (d.display === "active") {
      el.textContent = F.formatTps(d.value) + " tok/s";
      el.classList.remove("dim");
      if (sub) sub.hidden = true;
    } else if (d.display === "idle") {
      el.textContent = "0 t/s";
      el.classList.remove("dim");
      if (sub) { sub.textContent = d.tip || "当前无对应推理活动"; sub.hidden = false; }
    } else { // unavailable
      el.textContent = F.NA;
      el.classList.add("dim");
      if (sub) sub.hidden = true;
    }
  }

  /* 主机状态（6 指标：CPU / 内存 / 磁盘 I/O / 网络 / 监测组件功耗 / 系统运行时间）。
     数据来自 system 域（与 /api/system/status 同源）；组件功耗部分数据注明。 */
  function renderOvSystem(s) {
    if (!s || !s.available) {
      ["ovHostCpu","ovHostMem","ovHostUptime"].forEach(function (id) { setStatValue(id, F.NA); });
      var dr = $("ovHostDiskR"), dw = $("ovHostDiskW"), nr = $("ovHostNetR"), nw = $("ovHostNetW");
      if (dr) dr.textContent = "↓ --"; if (dw) dw.textContent = "↑ --";
      if (nr) nr.textContent = "↓ --"; if (nw) nw.textContent = "↑ --";
      setStatValue("ovHostPower", F.NA);
      return;
    }
    if (s.cpu && s.cpu.usage_percent != null) {
      setStatValue("ovHostCpu", F.formatPercent(s.cpu.usage_percent, 0));
      var csub = $("ovHostCpuSub");
      if (csub) { csub.textContent = s.cpu.temperature_c != null ? F.formatTemp(s.cpu.temperature_c) : ""; csub.hidden = !csub.textContent; }
    }
    if (s.memory) {
      if (s.memory.usage_percent != null) setStatValue("ovHostMem", F.formatPercent(s.memory.usage_percent, 0));
      var msub = $("ovHostMemSub");
      if (msub) {
        msub.textContent = (s.memory.used_bytes != null && s.memory.total_bytes != null)
          ? F.formatMemory(s.memory.used_bytes) + " / " + F.formatMemory(s.memory.total_bytes) : "";
        msub.hidden = !msub.textContent;
      }
    }
    if (s.disk) { _ovDual("ovHostDiskR", "ovHostDiskW", s.disk.read_bps, s.disk.write_bps, "读", "写"); }
    if (s.network) { _ovDual("ovHostNetR", "ovHostNetW", s.network.rx_bps, s.network.tx_bps, "收", "发"); }
    // 监测组件功耗（部分组件可读取时注明；全不可用 -> "不可用"，绝不 0W / --）
    var pw = s.power || {};
    var pwEl = $("ovHostPower");
    if (pw.monitored_components_w != null) {
      setStatValue("ovHostPower", F.formatPower(pw.monitored_components_w));
      var psub = $("ovHostPowerSub");
      if (psub) {
        var n = pw.components_present || 0;
        var miss = [];
        if (pw.cpu_package_available === false) miss.push("CPU Package");
        if (pw.gpu_available === false) miss.push("GPU");
        var txt = n ? n + " 个组件可读取" : "";
        if (miss.length && n < 2) txt += (txt ? " · " : "") + miss.join("、") + " 不可读（总值非整机）";
        psub.textContent = txt; psub.hidden = !txt;
      }
    } else {
      pwEl.textContent = "不可用"; pwEl.classList.add("dim");
      var psub2 = $("ovHostPowerSub"); if (psub2) psub2.hidden = true;
    }
    if (s.uptime_seconds != null) setStatValue("ovHostUptime", F.formatDuration(s.uptime_seconds));
  }
  /* 双行速率（↓/↑），null -> "--"。与 system.js 的 _dual 同语义。 */
  function _ovDual(idA, idB, va, vb, la, lb) {
    function one(id, v) {
      var el = $(id); if (!el) return;
      if (v == null) { el.textContent = (idA === id ? "↓ " : "↑ ") + F.NA; el.classList.add("dim"); }
      else { el.textContent = (idA === id ? "↓ " : "↑ ") + F.formatBytes(v) + "/s"; el.classList.remove("dim"); }
    }
    one(idA, va); one(idB, vb);
  }

  /* GPU 状态（每卡仅 利用率/显存占用/温度/功耗 2×2；短名 + 全名 tooltip；
     功耗不支持 -> "不可用"，不显示 0W / --）。 */
  function renderOvGpu(g) {
    var stateEl = $("ovGpuState"), lineEl = $("ovGpuLine");
    var grid = $("ovGpuMini"), unav = $("ovGpuUnavailable");
    if (!stateEl || !grid) return;
    if (!g || !g.available) {
      ui.setStatusBadge(stateEl, "offline", "不可用");
      if (lineEl) lineEl.textContent = (g && g.reason) ? g.reason : "nvidia-smi 不可用";
      grid.innerHTML = "";
      if (unav) { unav.hidden = false; unav.textContent = "GPU 采集异常：" + ((g && g.reason) || "nvidia-smi 不可用"); }
      return;
    }
    var list = g.gpus || [];
    ui.setStatusBadge(stateEl, "online", g.count + " 张 GPU");
    if (lineEl) lineEl.textContent = "";
    if (unav) unav.hidden = true;
    if (!list.length) { grid.innerHTML = ""; if (lineEl) lineEl.textContent = "可用，暂无样本。"; return; }
    grid.innerHTML = "";
    list.forEach(function (gp) {
      var card = document.createElement("div");
      card.className = "gpu-mini";
      var head = document.createElement("div");
      head.className = "gpu-mini-head2";
      var idx = document.createElement("span"); idx.className = "gm-idx";
      idx.textContent = "GPU " + (gp.index == null ? "?" : gp.index);
      var name = document.createElement("span"); name.className = "gm-name";
      var short = gpuShortName(gp.name);
      name.textContent = short || (gp.name || "");
      name.title = gp.name || "";
      head.appendChild(idx); head.appendChild(name);
      card.appendChild(head);
      var vramText = (gp.memory_used_mb == null || gp.memory_total_mb == null) ? F.NA :
        (gp.memory_used_mb / 1024).toFixed(1) + " / " + (gp.memory_total_mb / 1024).toFixed(1) + " GiB";
      // 功耗不支持 -> "不可用"（不显示 0W / --）
      var powerText = gp.power_draw_w == null ? "不可用" : F.formatPower(gp.power_draw_w);
      var items = [
        ["利用率", gp.utilization_percent == null ? F.NA : F.formatPercent(gp.utilization_percent, 0)],
        ["显存占用", vramText],
        ["温度", F.formatTemp(gp.temperature_c)],
        ["功耗", powerText],
      ];
      var metrics = document.createElement("div"); metrics.className = "gm-metrics";
      items.forEach(function (it) {
        var m = document.createElement("div"); m.className = "gm-metric";
        var k = document.createElement("div"); k.className = "k"; k.textContent = it[0];
        var v = document.createElement("div"); v.className = "v" + (it[1] === F.NA ? " dim" : "");
        v.textContent = it[1];
        m.appendChild(k); m.appendChild(v); metrics.appendChild(m);
      });
      card.appendChild(metrics);
      grid.appendChild(card);
    });
  }

  /* 监测完整性（5 指标：覆盖率 / 今日缺口 / Token 数据风险 / 数据库状态 / 最后采样）。 */
  function renderOvIntegrity(it) {
    if (!it) return;
    // 今日采集覆盖率（颜色来自状态模型：≥99.9 ok / ≥95 warn / 否则 bad）
    var covEl = $("dqCoverage");
    if (covEl) {
      var cov = it.coverage_percent;
      covEl.textContent = cov == null ? F.NA : F.formatPercent(cov);
      covEl.className = "stat-value mid" + (cov == null ? "" : " " + (cov >= 99.9 ? "ok" : cov >= 95 ? "warn" : "bad"));
    }
    // 今日缺口（次值：无已知 Token 丢失 / N 个可能存在 Token 丢失）
    var gapsEl = $("dqGapsToday");
    if (gapsEl) {
      var gc = it.gap_count_today || 0;
      gapsEl.textContent = DQ.gapValueText(gc);
      gapsEl.className = "stat-value mid " + (gc === 0 ? "ok" : it.possible_token_loss ? "bad" : "warn");
    }
    var lossEl = $("dqLossToday");
    if (lossEl) lossEl.textContent = gc === 0 ? "无已知 Token 丢失" : "可能存在 Token 丢失";
    // Token 数据风险（无已知风险 / 可能丢失 / 时间归属不确定）
    var riskEl = $("dqTokenRisk");
    if (riskEl) {
      var r = it.token_risk || "none";
      riskEl.textContent = r === "lost" ? "可能丢失" : r === "time_uncertain" ? "时间归属不确定" : "无已知风险";
      riskEl.setAttribute("data-tone", r === "lost" ? "bad" : r === "time_uncertain" ? "warn" : "ok");
    }
    // 数据库状态（正常 / 只读兼容模式 / 只读 / 异常 + 次值；只读兼容给出 Schema 版本对照）
    var dbEl = $("dqDb");
    if (dbEl) {
      var ds = it.db_status || "normal";
      var dbText = { normal: "正常", readonly_compat: "只读兼容模式", readonly: "只读", abnormal: "异常" }[ds] || "正常";
      dbEl.textContent = dbText;
      dbEl.setAttribute("data-tone", ds === "abnormal" ? "bad" : (ds === "readonly" || ds === "readonly_compat") ? "warn" : "ok");
    }
    var dbHint = $("dqDbHint");
    if (dbHint) dbHint.textContent = it.db_secondary || "";
    // 最后采样（刚刚 + 次值 HH:MM:SS）
    var lsEl = $("dqLastSample");
    if (lsEl) {
      if (it.last_sample_seconds_ago != null) lsEl.textContent = F.formatAgo(it.last_sample_seconds_ago);
      else lsEl.textContent = F.NA;
    }
    var lsTs = $("dqLastSampleTs");
    if (lsTs) lsTs.textContent = it.last_sample_ts ? F.formatTime(it.last_sample_ts) : "";
  }

  /* 渲染整份 /api/overview（各域独立；任一域缺失只影响对应 Section）。 */
  function renderOverview(d) {
    renderOvService(d.service);
    renderOvAttention(d.attention);
    renderOvUsage(d.usage_today);
    renderOvInference(d.inference);
    renderOvSystem(d.system);
    renderOvGpu(d.gpus);
    renderOvIntegrity(d.integrity);
  }

  /* MTP 范围视图（Round 5 §84：范围控制整个 Section——Summary + 趋势 + 位置同源）。
     d = /api/mtp/range 响应（summary + days + positions）。 */
  function applyMtpRange(d) {
    state.mtpData = d;
    var s = d.summary || {};
    var hasSample = (s.draft_tokens || 0) > 0;
    var enabled = mtpEnabled();
    // 状态注脚（§110-§112 三态）
    var note = $("mtpStateNote");
    if (note) {
      if (!enabled) {
        note.hidden = false;
        note.textContent = "MTP 未启用 · 当前运行配置未启用 Multi-Token Prediction Draft。";
      } else if (!hasSample) {
        note.hidden = false;
        note.textContent = "MTP 已启用 · 暂无 Draft 样本。";
      } else {
        note.hidden = true;
      }
    }
    // 6 项 Summary（§91）：接受率 / Draft Token / 已接受 / 验证步数 /
    //   平均 Draft 长度 / 平均接受长度。分母 0 -> 暂无样本（不 0%）。
    setStatValue("mtpAcceptRate", s.accept_rate != null ? F.formatPercent(s.accept_rate) : (enabled && !hasSample ? "暂无样本" : F.NA));
    setStatValue("mtpDraft", F.formatTokenCount(s.draft_tokens || 0));
    setStatValue("mtpAccepted", F.formatTokenCount(s.accepted_tokens || 0));
    setStatValue("mtpSeqs", F.formatTokenCount(s.verification_steps || 0));
    setStatValue("mtpAvgDraft", s.avg_draft_length != null ? s.avg_draft_length.toFixed(2) + " Token/步" : F.NA);
    setStatValue("mtpAvgAccepted", s.avg_accepted_length != null ? s.avg_accepted_length.toFixed(2) + " Token/步" : F.NA);
    // 趋势 + 位置（同范围）
    charts.renderMtpChart("chartMtpBox", "chartMtp", d.days || []);
    charts.renderMtpPosChart("chartMtpPosBox", "chartMtpPos", d.positions || []);
    // 子标题随范围
    var sub = $("mtpTrendSub");
    if (sub) sub.textContent = rangeTrendLabel(d.range);
    var psub = $("mtpPosSub");
    if (psub) psub.textContent = rangeTrendLabel(d.range);
  }

  function rangeTrendLabel(range) {
    if (range === "today") return "今天";
    if (range === "7") return "近 7 天";
    if (range === "30") return "近 30 天";
    if (range === "all") return "全部";
    return "按天";
  }

  function refreshMtpRange() {
    var base = "/api/mtp/range";
    var q;
    if (state.mtpRange === "all") q = "?all=true";
    else if (state.mtpRange === "today") q = "";
    else q = "?days=" + state.mtpRange;
    return api.get(base + q)
      .then(applyMtpRange)
      .catch(function (e) { console.warn("mtp range failed:", e.message || e); });
  }

  /* applyMtp：/api/mtp（今日）——仅驱动 Overview 摘要三态（性能页 MTP Section
     已由 /api/mtp/range 独立驱动，避免两套数据打架）。*/
  function applyMtp(d) {
    state.mtp = d;
    state.mtpPositions = d.positions || [];
    renderOvMtpRate(); // Overview 摘要（三态）
  }

  /* ================= 数据新鲜度 badge（Page Header 右侧；§31-§33） =================
     在线且最近采样新鲜 -> "● 实时 · 刚刚"；
     最近采样 age > 3*pollInterval -> "数据已过期 · X秒前"（warn）；
     离线 -> "服务器不可用 · 历史值"（保留最近值但标注）。 */
  function renderFreshnessBadge() {
    var badge = $("perfFreshness");
    if (!badge) return;
    var dot = badge.querySelector(".fb-dot");
    var text = badge.querySelector(".fb-text");
    if (!dot || !text) return;
    var interval = state.pollIntervalSec || 5;
    var now = Date.now() / 1000;
    if (state.online === false) {
      badge.hidden = false;
      badge.className = "freshness-badge offline";
      text.textContent = "服务器不可用 · 历史值";
      return;
    }
    if (state.lastUpdateTs == null) {
      badge.hidden = true;
      return;
    }
    var age = now - state.lastUpdateTs;
    if (age > interval * 3) {
      badge.hidden = false;
      badge.className = "freshness-badge stale";
      text.textContent = "数据已过期 · " + F.formatAgo(age);
    } else {
      badge.hidden = false;
      badge.className = "freshness-badge live";
      text.textContent = "实时 · " + F.formatAgo(age);
    }
  }

  /* ================= Usage 页：图表 + 每日表 ================= */
  /* ================= 每日明细表（1.1.4 Round 4） =================
  缺失日语义（绝不伪造 0）：daily 行=当天采集器运行过；行缺失=范围内该天
  未开始监测或监测中断。行缺失统一标注"未开始监测"（该天无 daily 记录）。 */
  function _pad2(n) { return (n < 10 ? "0" : "") + n; }
  function _addDays(dateStr, n) {
    var p = dateStr.split("-");
    var d = new Date(+p[0], +p[1] - 1, +p[2] + n);
    return d.getFullYear() + "-" + _pad2(d.getMonth() + 1) + "-" + _pad2(d.getDate());
  }
  /* 生成 [start,end] 闭区间的全部日历日（含端点）；行数上限保护 3660 */
  function _dateSeq(start, end) {
    var out = [], cur = start, guard = 0;
    while (cur && cur <= end && guard < 3660) { out.push(cur); cur = _addDays(cur, 1); guard++; }
    return out;
  }
  /* Round-8 §146-155：Daily 明细双呈现。
  桌面 = 完整 Table；Mobile（≤760）= Day Cards：
  卡头（日期 + Token 总量）+ 核心 4 项 2×2 + Footer（缓存复用率 · 覆盖率 · Gap）。
  一次构建两份 DOM，CSS 按视口切换（不双份轮询、不重复请求）。 */
  function _dailyDayCardHtml(date, r, today) {
    var cov = r ? r.monitoring_coverage_percent : null;
    var covCls = cov == null ? "na" : (cov >= 99.9 ? "cell-ok" : cov >= 95 ? "cell-warn" : "cell-bad");
    var covTxt = cov == null ? F.NA : F.formatPercent(cov);
    var gaps = r ? (r.gap_count || 0) : 0;
    var loss = r ? r.possible_token_loss : false;
    var gapCls = gaps === 0 ? "cell-ok" : (loss ? "cell-bad" : "cell-warn");
    var gapTxt = gaps === 0 ? "无缺口" : (gaps + " 个缺口");
    var p = r ? (r.prompt_tokens || 0) : 0, c = r ? (r.cached_tokens || 0) : 0;
    var crDenom = p + c;
    var cacheTxt = (r && crDenom > 0) ? F.formatPercent(c / crDenom * 100) : F.NA;
    if (!r) {
      var label = today === date ? "今天 · 暂无有效数据" : "未开始监测";
      return "<div class='day-card' data-date='" + date + "'>" +
        "<div class='dc-head'><span class='dc-date'>" + date + "</span>" +
        "<span class='dc-total na'>--</span></div>" +
        "<div class='dc-note'>" + label + "</div></div>";
    }
    return "<div class='day-card' data-date='" + r.date + "'>" +
      "<div class='dc-head'><span class='dc-date'>" + r.date + "</span>" +
      "<span class='dc-total' title='Token 总量'>" + F.formatTokenCount(r.logical_tokens) + "</span></div>" +
      "<div class='dc-grid'>" +
        "<div class='dc-item'><span class='dc-k'>实际计算</span><span class='dc-v'>" + F.formatTokenCount(r.compute_tokens) + "</span></div>" +
        "<div class='dc-item'><span class='dc-k'>输入</span><span class='dc-v'>" + F.formatTokenCount(r.prompt_tokens) + "</span></div>" +
        "<div class='dc-item'><span class='dc-k'>缓存复用</span><span class='dc-v'>" + F.formatTokenCount(r.cached_tokens) + "</span></div>" +
        "<div class='dc-item'><span class='dc-k'>输出</span><span class='dc-v'>" + F.formatTokenCount(r.output_tokens) + "</span></div>" +
      "</div>" +
      "<div class='dc-foot'>" +
        "<span class='dc-f'>缓存复用率 <b>" + cacheTxt + "</b></span>" +
        "<span class='dc-f " + covCls + "'>覆盖率 <b>" + covTxt + "</b></span>" +
        "<span class='dc-f " + gapCls + "'>" + gapTxt + "</span>" +
      "</div></div>";
  }

  function renderDailyTable() {
    var tbody = $("dailyTbody");
    if (!tbody) return;
    var rows = state.dailyData || [];
    var meta = state.dailyRangeMeta;
    var byDate = {};
    rows.forEach(function (r) { byDate[r.date] = r; });
    // 无数据且无范围元数据：单行空态
    if (!rows.length && (!meta || !meta.start_date)) {
      tbody.innerHTML = "<tr><td colspan='9' class='na'>该范围内暂无数据。</td></tr>";
      var dc0 = $("dailyDayCards");
      if (dc0) { dc0.hidden = true; dc0.innerHTML = "<div class='day-card'><div class='dc-note'>该范围内暂无数据。</div></div>"; }
      return;
    }
    var dates = (meta && meta.start_date && meta.end_date)
      ? _dateSeq(meta.start_date, meta.end_date)
      : rows.map(function (r) { return r.date; });
    var today = meta ? meta.today : null;
    var html = "";
    var cardHtml = "";
    dates.forEach(function (date) {
      var r = byDate[date];
      if (!r) {
        // 缺失日：无 daily 行 -> 未开始监测 / 当天暂无有效数据（今天）
        var label = today === date ? "今天 · 暂无有效数据" : "未开始监测";
        html += "<tr class='day-missing' data-date='" + date + "'><td>" + date + "</td>" +
          "<td colspan='8' class='day-missing-note'>" + label + "</td></tr>";
        cardHtml += _dailyDayCardHtml(date, null, today);
        return;
      }
      var cov = r.monitoring_coverage_percent;
      var covTd = cov == null
        ? "<td data-label='采集覆盖率' class='td-group-start na' title='该天无有效监测窗口'>" + F.NA + "</td>"
        : "<td data-label='采集覆盖率' class='td-group-start " + (cov >= 99.9 ? "cell-ok" : cov >= 95 ? "cell-warn" : "cell-bad") + "'>" + F.formatPercent(cov) + "</td>";
      var gaps = r.gap_count || 0;
      var loss = r.possible_token_loss;
      var gapsTd = "<td data-label='缺口' class='" + (gaps === 0 ? "cell-ok" : loss ? "cell-bad" : "cell-warn") + "'>" + gaps + "</td>";
      var p = r.prompt_tokens || 0, c = r.cached_tokens || 0;
      var crDenom = p + c;
      var cacheTd = crDenom > 0
        ? "<td data-label='缓存复用率'>" + F.formatPercent(c / crDenom * 100) + "</td>"
        : "<td data-label='缓存复用率' class='na'>" + F.NA + "</td>";
      html += "<tr data-date='" + r.date + "'><td>" + r.date + "</td>" +
        "<td data-label='Token 总量'>" + F.formatTokenCount(r.logical_tokens) + "</td>" +
        "<td data-label='实际计算'>" + F.formatTokenCount(r.compute_tokens) + "</td>" +
        "<td class='td-group-start' data-label='Prompt'>" + F.formatTokenCount(r.prompt_tokens) + "</td>" +
        "<td data-label='缓存复用'>" + F.formatTokenCount(r.cached_tokens) + "</td>" +
        "<td data-label='生成'>" + F.formatTokenCount(r.output_tokens) + "</td>" +
        cacheTd + covTd + gapsTd + "</tr>";
      cardHtml += _dailyDayCardHtml(date, r, today);
    });
    tbody.innerHTML = html;
    var dc = $("dailyDayCards");
    if (dc) { dc.innerHTML = cardHtml; dc.hidden = false; }
  }

  /* 时间范围模式 -> /api/daily 查询参数（custom 用 start_date/end_date） */
  function dailyQuery() {
    var base = "/api/daily";
    switch (state.dailyRangeMode) {
      case "today": return base + "?days=1";
      case "7d": return base + "?days=7";
      case "30d": return base + "?days=30";
      case "month": return base + "?month=true";
      case "custom":
        if (state.customRange) return base + "?start_date=" + state.customRange.start_date +
          "&end_date=" + state.customRange.end_date;
        return base + "?days=7";
      case "all":
      default: return base + "?all=true";
    }
  }

  function refreshDaily() {
    return api.get(dailyQuery())
      .then(function (d) {
        state.dailyData = d.days || [];
        state.dailyRangeMeta = d.range || null;
        renderUsageTrend();
        // Round 5：性能页 MTP 趋势改由 /api/mtp/range 驱动（refreshMtpRange），
        // 不再用 /api/daily 的 mtp_accept_rate 画（避免两套数据源打架 + 跨页覆盖）。
        renderDailyTable();
        renderUsageSummary();
      })
      .catch(function (e) { console.warn("daily failed:", e.message || e); });
  }

  /* 趋势图：日档（按天堆叠）/ 时档（今天逐小时）。
  日档 category 轴不插 0——缺 day 由 x 轴类别自然断开，缺失日语义在每日明细表标注。 */
  function renderUsageTrend() {
    var hint = $("usageTrendHint");
    var useHour = state.trendGran === "hour" && state.dailyRangeMode === "today";
    if (useHour) {
      if (hint) hint.textContent = "今天 · 逐小时（Prompt / 缓存复用 / 生成，堆叠）";
      if (!state.todayHourly) {
        charts.setEmpty("chartUsageBox", true, "暂无逐小时数据", "采集后自动生成今天逐小时曲线。");
      } else {
        charts.renderUsageHourlyChart("chartUsageBox", "chartUsage", state.todayHourly);
      }
      if (!state.todayHourlyFetched) {
        state.todayHourlyFetched = true;
        refreshTodayHourly();
      }
      return;
    }
    // 非小时档（或非今天范围）：按天堆叠
    if (hint) hint.textContent = state.dailyData.length > 31
      ? "按天（Prompt / 缓存复用 / 生成，堆叠；可拖动缩放）" : "按天（Prompt / 缓存复用 / 生成，堆叠）";
    charts.renderUsageChart("chartUsageBox", "chartUsage", state.dailyData);
  }

  function refreshTodayHourly() {
    return api.get("/api/today-hourly")
      .then(function (d) {
        state.todayHourly = d;
        if (state.trendGran === "hour") {
          charts.renderUsageHourlyChart("chartUsageBox", "chartUsage", state.todayHourly);
        }
      })
      .catch(function (e) { console.warn("today-hourly failed:", e.message || e); });
  }

  function setDailyRangeMode(mode) {
    state.dailyRangeMode = mode;
    // 小时档仅"今天"可用：切到其它范围时回退日档、丢弃逐小时数据并同步分段选中态
    if (mode !== "today") {
      if (state.trendGran === "hour") {
        state.trendGran = "day";
        if (usageTrendSeg) usageTrendSeg.set("day");
      }
      state.todayHourly = null;
    }
    state.todayHourlyFetched = false;
    refreshDaily();
    refreshUsageSummary();
  }

  /* Token 吞吐率（Round 5 §35-§58）：范围 15m/1h/6h/24h，默认 1h。
     /api/throughput 返回 samples（含 prompt/decode tps + requests + 计数/秒数）
     + window_avg（窗口加权平均吞吐，Δtoken/Δseconds）。 */
  function refreshThroughput() {
    return api.get("/api/throughput?minutes=" + state.throughputRangeMinutes)
      .then(function (d) {
        state.throughputData = d;
        charts.renderTpsChart("chartTpsBox", "chartTps", d.samples || [], {
          pollIntervalSec: state.pollIntervalSec,
          lastActivityTs: d.last_activity_ts,
          availableMinutes: d.available_minutes,
          requestedMinutes: d.minutes,
        });
        renderThroughputMeta(d);
      })
      .catch(function (e) { console.warn("throughput failed:", e.message || e); });
  }

  /* 窗口加权平均 Summary（§46-§50）：Section Header 下轻量一行，非大卡。 */
  function renderThroughputMeta(d) {
    var el = $("perfWindowAvg");
    if (!el) return;
    var avg = d.window_avg || {};
    var parts = [];
    if (avg.prompt_tps_avg != null) parts.push("Prompt " + F.formatTps(avg.prompt_tps_avg) + " tok/s");
    if (avg.decode_tps_avg != null) parts.push("Decode " + F.formatTps(avg.decode_tps_avg) + " tok/s");
    // 最近一次推理活动（§50）
    if (d.last_activity_ts != null) {
      parts.push("最近活动 " + F.formatHM(d.last_activity_ts));
    }
    if (parts.length) {
      el.innerHTML = '<span class="pwa-key">窗口平均</span><span class="pwa-sep">·</span>' +
        parts.join(' <span class="pwa-sep">·</span> ');
      el.hidden = false;
    } else {
      el.hidden = true;
    }
    // 可用数据不足所选窗口（§169：不假装 24h 完整）
    if (d.window_minutes < d.minutes) {
      el.classList.add("partial");
      el.dataset.note = "可用数据 " + (d.window_minutes >= 60
        ? (d.window_minutes / 60).toFixed(0) + " 小时"
        : d.window_minutes + " 分钟");
    } else {
      el.classList.remove("partial");
      delete el.dataset.note;
    }
    // 轻量入口：发现 stale/gap 才显示（§173）
    var link = $("perfHistoryLink");
    if (link) link.hidden = !(d.window_minutes < d.minutes || (d.samples || []).length < 3);
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
        // 后端不可达（区别于 llama 离线）：保留上次数据 + 提示
        console.warn("status failed:", e.message || e);
        state.statusBackendOk = false; // 由 1s ticker 统一渲染"后端不可达"
      });
  }

  function refreshRuntime() {
    return api.get("/api/runtime").then(applyRuntime)
      .catch(function (e) { console.warn("runtime failed:", e.message || e); });
  }

  /* Slot 监控（Round 5 §114-§145）：一次 /api/llama/slots 拉取驱动
     顶部「活跃 Slot」+ 运行时「活跃/总 Slot」「当前序列长度」+ 下方 Slot 表/卡。
     取代此前 system.refreshLlamaSlots 单独渲染（避免两处重复请求）。 */
  function refreshSlots() {
    return api.get("/api/llama/slots")
      .then(function (d) {
        state.slotsData = d;
        renderActiveSlots();
        renderCurrentSequence();
        // 上下文窗口上限兜底：/api/runtime 的 context_max 为 null 时，
        // 用 slot 的 n_ctx（llama-server 配置的窗口上限）填充。
        var slots = d && d.slots;
        var ctx = slots && slots.length && slots[0].n_ctx != null ? slots[0].n_ctx : null;
        var rtCtx = $("rtContextMax");
        if (rtCtx && ctx != null && rtCtx.textContent === F.NA) {
          rtCtx.textContent = F.formatTokenCount(ctx);
        }
        if (LM.system) LM.system.renderSlotsOnly(d);
      })
      .catch(function (e) { console.warn("slots failed:", e.message || e); });
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
    // Round-4：24h 范围点量大，后端降采样到 ~1000 点/series 控制传输与渲染；
    // 短范围用默认 2000。
    var mp = state.gpuRangeMinutes >= 1440 ? 1000 : 2000;
    return api.get("/api/gpu/live?minutes=" + state.gpuRangeMinutes + "&max_points=" + mp)
      .then(applyGpuLive)
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

  /* ================= 自定义时间范围（日历图标按钮 + 弹层） =================
  起止为服务器本机日历日（'YYYY-MM-DD'）：start ≤ end ≤ today。
  应用后进入 custom 模式（dailyQuery/usageSummaryQuery 用 start_date/end_date）。 */
  function _serverTodayDate() {
    var meta = state.dailyRangeMeta;
    if (meta && meta.today) return meta.today;
    // 兜底：本地日期（meta 尚未到达）——仅作弹层 max 的初值
    var d = new Date();
    return d.getFullYear() + "-" + _pad2(d.getMonth() + 1) + "-" + _pad2(d.getDate());
  }
  function initCustomRange() {
    var btn = $("usageRangeCustom");
    if (!btn || btn.dataset.wired) return;
    btn.dataset.wired = "1";
    var iconEl = $("usageRangeCustomIcon");
    if (iconEl && LM.icons) iconEl.innerHTML = LM.icons.get("calendar");
    var pop = $("usageRangePopover");
    var startIn = $("usageRangeStart");
    var endIn = $("usageRangeEnd");
    var hint = $("usageRangeHint");
    if (!pop) return;

    function close() {
      pop.hidden = true;
      btn.setAttribute("aria-expanded", "false");
      customPopoverOpen = false;
    }
    function open() {
      var today = _serverTodayDate();
      // 默认：最近 7 天（今天-6 ~ 今天），或沿用已选自定义范围
      if (startIn) { startIn.max = today; if (!startIn.value) startIn.value = (state.customRange && state.customRange.start_date) || _addDays(today, -6); }
      if (endIn) { endIn.max = today; if (!endIn.value) endIn.value = (state.customRange && state.customRange.end_date) || today; }
      if (hint) { hint.textContent = "结束日期不能晚于今天（" + today + "）。"; hint.classList.remove("bad"); }
      // 定位到按钮下方
      var r = btn.getBoundingClientRect();
      pop.hidden = false;
      var pw = pop.offsetWidth, ph = pop.offsetHeight;
      var left = Math.min(r.right - pw, window.innerWidth - pw - 8);
      if (left < 8) left = 8;
      var top = r.bottom + 6;
      if (top + ph > window.innerHeight - 8) top = Math.max(8, r.top - ph - 6);
      pop.style.left = left + "px";
      pop.style.top = top + "px";
      btn.setAttribute("aria-expanded", "true");
      customPopoverOpen = true;
      if (endIn) endIn.focus();
    }
    function apply() {
      var s = startIn ? startIn.value : "";
      var e = endIn ? endIn.value : "";
      var today = _serverTodayDate();
      var bad = function (msg) { if (hint) { hint.textContent = msg; hint.classList.add("bad"); } return false; };
      if (!/^\d{4}-\d{2}-\d{2}$/.test(s) || !/^\d{4}-\d{2}-\d{2}$/.test(e)) return bad("请选择开始与结束日期。");
      if (s > e) return bad("开始日期不能晚于结束日期。");
      if (e > today) return bad("结束日期不能晚于今天（" + today + "）。");
      state.customRange = { start_date: s, end_date: e };
      if (usageRangeSeg) usageRangeSeg.set("__custom__"); // 无选中段（自定义由按钮承载）
      close();
      setDailyRangeMode("custom");
      // 按钮文案标注当前自定义范围
      var label = $("usageRangeCustomLabel");
      if (label) label.textContent = "自定义";
    }
    btn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      if (customPopoverOpen) close(); else open();
    });
    var cancel = $("usageRangeCancel");
    if (cancel) cancel.addEventListener("click", close);
    var applyBtn = $("usageRangeApply");
    if (applyBtn) applyBtn.addEventListener("click", apply);
    if (endIn) endIn.addEventListener("keydown", function (ev) { if (ev.key === "Enter") apply(); });
    document.addEventListener("click", function (ev) {
      if (customPopoverOpen && !pop.contains(ev.target) && !btn.contains(ev.target)) close();
    });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape" && customPopoverOpen) close();
    });
  }
  async function loadConfig() {
    /* 时间筛选控件先于 /api/config 创建——该端点是 loopback-only，
 手机走局域网 IP 访问得 403，此前筛选被放在 await 之后，403 直接进
 catch，两个 segmented 永远没被创建（手机看不到时间范围筛选）。
 现在：先用内置默认值渲染；config 成功后用服务器配置 set 同步选中项。 */
    try {
      var modeOptions = [
        { value: "today", label: "今天" },
        { value: "7d", label: "7 天" },
        { value: "30d", label: "30 天" },
        { value: "month", label: "本月" },
        { value: "all", label: "全部" },
      ];
      var rangeEl = $("usageRange");
      usageRangeSeg = rangeEl && LM.nav
        ? ui.segmented(rangeEl, modeOptions, state.dailyRangeMode, function (v) {
            state.dailyRangeMode = v;
            setDailyRangeMode(v);
          })
        : null;
      // 趋势粒度分段（按天 / 按小时）：小时档仅"今天"范围可用
      var trendEl = $("usageTrendRange");
      usageTrendSeg = trendEl
        ? ui.segmented(trendEl, [
            { value: "day", label: "按天" },
            { value: "hour", label: "按小时" },
          ], state.trendGran, function (v) {
            state.trendGran = v;
            renderUsageTrend();
          })
        : null;
      // 自定义时间范围（日历图标按钮 + 弹层）
      initCustomRange();
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
      // 推理性能页：Token 吞吐率范围（15分钟/1小时/6小时/24小时，默认 1 小时）
      var perfTpsEl = $("perfThroughputRange");
      perfTpsSeg = perfTpsEl
        ? ui.segmented(perfTpsEl, [
            { value: 15, label: "15 分钟" },
            { value: 60, label: "1 小时" },
            { value: 360, label: "6 小时" },
            { value: 1440, label: "24 小时" },
          ], state.throughputRangeMinutes, function (v) {
            state.throughputRangeMinutes = v;
            refreshThroughput();
          })
        : null;
      // 推理性能页：MTP 范围（今天/7天/30天/全部，默认今天；控制整个 MTP Section）
      var perfMtpEl = $("perfMtpRange");
      perfMtpSeg = perfMtpEl
        ? ui.segmented(perfMtpEl, [
            { value: "today", label: "今天" },
            { value: "7", label: "7 天" },
            { value: "30", label: "30 天" },
            { value: "all", label: "全部" },
          ], state.mtpRange, function (v) {
            state.mtpRange = v;
            refreshMtpRange();
          })
        : null;
      // /api/config 是 loopback-only。远程（局域网 IP）客户端不发该请求，
      // 直接用内置默认值——手机 DevTools 网络面板不再出现 403。
      var c = LM.api.isLocal() ? await api.get("/api/config") : null;
      if (c && c.ui) {
        cfgUi.refreshIntervalSeconds = c.ui.refresh_interval_seconds || 5;
        cfgUi.dailyDefaultDays = c.ui.daily_default_days || 7;
        cfgUi.theme = c.ui.theme || "system";
        // 推理性能页：采集间隔（缺口阈值 = poll*3，新鲜度阈值 = 3*interval）
        if (c.collector && c.collector.poll_interval_seconds) {
          state.pollIntervalSec = c.collector.poll_interval_seconds;
        } else {
          state.pollIntervalSec = cfgUi.refreshIntervalSeconds;
        }
      }
      lastConfigUrl = (c && c.llama_server && c.llama_server.url) || "";
      var _su = $("ovServerUrl"); if (lastConfigUrl && _su) _su.textContent = lastConfigUrl.replace(/^https?:\/\//, "");
      setThemeMode(cfgUi.theme);
      // 服务器配置到达后同步 Usage 默认范围（set 只更新选中态，不触发 onChange）
      var defDays = cfgUi.dailyDefaultDays || 7;
      var serverDefault = defDays <= 1 ? "today" : defDays <= 7 ? "7d" :
        defDays <= 30 ? "30d" : defDays <= 62 ? "month" : "all";
      if (usageRangeSeg && serverDefault !== state.dailyRangeMode) {
        state.dailyRangeMode = serverDefault;
        usageRangeSeg.set(serverDefault);
        setDailyRangeMode(serverDefault);
      }
    } catch (e) {
      console.warn("config load failed (defaults used):", e.message || e);
      setThemeMode("system");
    }
  }

  function init() {
    // 导航图标（本地 SVG， + 品牌标识
    // 1.1.2：含移动端底栏 .mnav-icon（sheet-icon 在 navigation.js 内惰性处理）
    document.querySelectorAll(".nav-icon[data-icon], .mnav-icon[data-icon], .sheet-icon[data-icon]").forEach(function (el) {
      el.innerHTML = LM.icons.get(el.getAttribute("data-icon"));
    });
    // 1.1.3：Card Rows 用显式 class（mobile-card-table）驱动，不再依赖 :has()
    // （spec §143：显式 class 更健壮，旧 WebView2 也稳）。idempotent——
    // daily 表动态重渲染但 table-wrap 是静态 DOM，一次标记即可。
    document.querySelectorAll(".table-wrap").forEach(function (w) {
      if (w.querySelector("table.table-daily, table.table-gap, table.table-gap2, table.table-events")) {
        w.classList.add("mobile-card-table");
      }
    });
    var brand = $("brandIcon");
    if (brand) brand.innerHTML = LM.icons.brand();
    // 指标定义 InfoTooltip
    document.querySelectorAll(".info-tip-slot").forEach(function (el) {
      var tip = LM.ui.infoTip(el.getAttribute("data-tip"), el.getAttribute("data-align") === "right");
      el.replaceWith(tip);
    });
    // 导航点击
    document.querySelectorAll(".nav-item").forEach(function (b) {
      b.addEventListener("click", function () { LM.nav.showPage(b.getAttribute("data-page")); });
    });
    // 概览行动链接（审计发现点击无响应——
    // 重写 HTML 时丢失了处理器，这里用事件委托统一接管）
    document.querySelectorAll("a.link[data-goto]").forEach(function (a) {
      a.addEventListener("click", function (ev) {
        ev.preventDefault();
        var p = a.getAttribute("data-goto");
        if (p && document.getElementById("page-" + p)) LM.nav.showPage(p);
      });
    });
    LM.nav.initCompact();
    // 1.1.2：移动端底栏 + More Sheet + 长列表自动折叠（≤760px 收起 data-auto-fold）
    LM.nav.initMobileNav();
    (function () {
      // Round-8 §285：手机横屏（宽>760 但高≤480）同样走 Mobile 布局，
      // 两个条件任一命中即视为 mobile（与 CSS 媒体查询、charts.isMobile 一致）。
      var mq = window.matchMedia("(max-width: 760px)");
      var mqLand = window.matchMedia("(orientation: landscape) and (max-height: 480px)");
      function isMobileLayout() { return mq.matches || mqLand.matches; }
      function apply() {
        var folds = document.querySelectorAll("[data-auto-fold]");
        for (var i = 0; i < folds.length; i++) {
          folds[i].open = !isMobileLayout();
        }
        // Round-4：GPU 高级信息桌面/手机都默认折叠（§32-34）；
        // 用户手动展开过的（state.gpuAdvOpen 记录）在视口切换后保留，
        // 未交互过的保持折叠。
        var advs = document.querySelectorAll(".gpu-adv");
        for (var j = 0; j < advs.length; j++) {
          var u = advs[j].getAttribute("data-uuid") || "";
          advs[j].open = (u !== "" && state.gpuAdvOpen[u] === true);
        }
      }
      function bind(m) {
        if (typeof m.addEventListener === "function") m.addEventListener("change", apply);
        else if (typeof m.addListener === "function") m.addListener(apply);
      }
      bind(mq);
      bind(mqLand);
      apply();
    })();
    // 远程（局域网 IP）客户端没有可改的配置（/api/config 等 loopback-only）：
    // 桌面侧边栏「设置」入口隐藏；移动端 Overflow Sheet 里「设置」保留但禁用
    // （+ 远程只读状态行），比直接消失更能解释"为什么不能改"。
    if (!LM.api.isLocal()) {
      document.querySelectorAll('.nav-item[data-page="settings"]').forEach(function (b) {
        b.style.display = "none";
      });
      document.querySelectorAll('.sheet-item[data-page="settings"]').forEach(function (b) {
        b.disabled = true;
        b.classList.add("sheet-item-disabled");
        var t = b.querySelector(".sheet-title");
        if (t) t.textContent = "设置（远程只读）";
      });
      var remoteRow = $("moreSheetRemote");
      if (remoteRow) remoteRow.hidden = false;
      var banner = $("remoteBanner");
      if (banner) {
        var dismissed = false;
        try { dismissed = sessionStorage.getItem("lm_remote_banner_dismissed") === "1"; } catch (e) {}
        if (!dismissed) {
          banner.hidden = false;
          // 1.1.4：主文案可点击展开/收起说明（role=button 的 span 即可，不引入新层级）
          var main = $("remoteBannerMain");
          var detail = $("remoteBannerDetail");
          if (main && detail) {
            var toggle = function () {
              var open = detail.hidden;   // 展开前是收起的
              detail.hidden = !open;
              banner.classList.toggle("open", open); // 展开态放开 max-height
            };
            main.addEventListener("click", toggle);
            main.addEventListener("keydown", function (e) {
              if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
            });
          }
          var close = document.createElement("button");
          close.type = "button";
          close.className = "remote-banner-close";
          close.setAttribute("aria-label", "关闭提示");
          close.textContent = "✕";
          close.addEventListener("click", function () {
            banner.hidden = true;
            try { sessionStorage.setItem("lm_remote_banner_dismissed", "1"); } catch (e) {}
          });
          banner.appendChild(close);
        }
      }
    }
    // Settings 事件绑定（保存/重置/测试连接/dirty 标记/主题切换/自动启动/危险操作/更新）
    if (LM.settings && LM.settings.init) LM.settings.init();

    // 页面钩子（：进入页面时加载该页数据；隐藏页不跑其专属轮询）
    LM.nav.registerPage("overview", function () {
      // Round-6：概览页数据统一走 /api/overview 只读聚合端点（service/usage/
      // inference/system/gpus/integrity/attention 一次取齐，按域隔离渲染）。
      refreshOverview();
      // /api/status 仍驱动全局状态徽章 / 离线 InfoBar / 性能页摘要（与 overview 互补）。
      refreshStatus();
    });
    LM.nav.registerPage("usage", function () {
      charts.ensurePageCharts(["chartUsage"]);
      refreshDaily(); refreshUsageSummary(); refreshSummary();
      if (state.dailyRangeMode === "today" && state.trendGran === "hour") refreshTodayHourly();
    });
    LM.nav.registerPage("performance", function () {
      charts.ensurePageCharts(["chartTps", "chartMtp", "chartMtpPos"]);
      // Round 5 推理性能页：实时摘要 + 运行时 + 活跃Slot（status/runtime/slots 同源刷新）
      refreshStatus(); refreshRuntime();
      // Slot 监控（一次 /api/llama/slots 驱动：顶部活跃 Slot + 运行时活跃/总 Slot +
      // 当前序列长度 + 上下文上限兜底 + 下方 Slot 表）
      refreshSlots();
      // Token 吞吐率（当前范围）
      refreshThroughput();
      // MTP（当前范围，控制整个 Section）
      refreshMtpRange();
      // 模型与运行环境（静态；进页刷新即可，不高频轮询）
      if (LM.system) LM.system.refreshLlamaInfo();
    });
    // 1.1 / Round-3 系统页（含新增 内存趋势 / 功耗趋势 图）
    LM.nav.registerPage("system", function () {
      charts.ensurePageCharts(["chartSysCpu", "chartSysMem", "chartSysDisk", "chartSysNet", "chartSysPower"]);
      if (LM.system) LM.system.init();
    });
    LM.nav.registerPage("gpu", function () {
      charts.ensurePageCharts(["chartGpuUtil", "chartGpuPower", "chartGpuTemp", "chartGpuFan", "chartGpuClock"]);
      refreshGpuStatus(); refreshGpuLive(); refreshGpuDaily();
    });
    LM.nav.registerPage("history", function () {
      charts.ensurePageCharts(["chartHistoryTrend"]);
      if (!$("historyRange").dataset.wired) initHistoryRange();
      bindHistoryExports();
      refreshHistoryNow();
    });
    LM.nav.registerPage("settings", function () {
      if (!LM.settings.isLoaded()) LM.settings.loadSettings();
      else LM.settings.loadAppIntegration();
    });
    LM.nav.registerPage("about", function () {
      LM.settings.loadAbout();
    });

    // 轮询任务注册（页面作用域——
    // 状态类 ~5s（config 间隔）；图表 10-15s；用量/历史 30-60s 且仅在对应页前台时运行）
    var R = Math.max(1, cfgUi.refreshIntervalSeconds) * 1000;
    // 状态类（全局：状态条/离线横幅依赖，任何页可见时都跑）
    LM.poll.register("status", { intervalMs: R, visibleOnly: true, run: refreshStatus });
    LM.poll.register("runtime", { intervalMs: R, visibleOnly: true, run: refreshRuntime });
    LM.poll.register("gpuStatus", { intervalMs: R, visibleOnly: true, run: refreshGpuStatus });
    // Round-6 概览页：/api/overview 只读聚合端点（前台 ~5s，hidden 页不跑；
    // 各域在端点内已有自身新鲜度，前端单节奏取齐）。竞态由 refreshOverview 的 gen 守卫。
    LM.poll.register("overview", {
      intervalMs: R, visibleIntervalMs: R, visibleOnly: true,
      run: function () { if (LM.nav.currentPage() === "overview") return refreshOverview(); },
    });
    // 摘要/质量（Overview + Usage/History 共用，30s 基线、前台 10s）
    LM.poll.register("summary", { intervalMs: 30000, visibleIntervalMs: 10000, visibleOnly: false, run: refreshSummary });
    LM.poll.register("dataQuality", { intervalMs: 30000, visibleIntervalMs: 10000, visibleOnly: false, run: refreshDataQuality });
    // Round-5 History 页：页面作用域轮询（hidden 页不跑，§296-304）。
    // 前台 20s / 后台 60s；soft 刷新（hard=false，不清屏，慢 API 下不闪烁）。
    // 进页 + 切 Range/Filter 时立即硬刷新（applyHistoryRange(true)）。
    LM.poll.register("history", {
      intervalMs: 60000, visibleIntervalMs: 20000, visibleOnly: true,
      run: function () { if (LM.nav.currentPage() === "history") return refreshHistoryNow(false); },
    });
    // 页面专属（hidden page 不轮询，避免跨页重复请求）
    // Round 5 推理性能页：吞吐率图（前台 ~5s，后台 30s；§215）
    LM.poll.register("throughput", {
      intervalMs: 30000, visibleIntervalMs: R, visibleOnly: true,
      run: function () { if (LM.nav.currentPage() === "performance") return refreshThroughput(); },
    });
    // Slot 监控（§217 前台 2~5s；拉取 /api/llama/slots 一次，驱动顶部活跃 Slot +
    // 运行时「活跃/总 Slot」+「当前序列长度」+ 下方 Slot 表/卡，避免多处重复请求）
    LM.poll.register("perfSlots", {
      intervalMs: 30000, visibleIntervalMs: R, visibleOnly: true,
      run: function () { if (LM.nav.currentPage() === "performance") return refreshSlots(); },
    });
    // 模型与运行环境（§216 静态：进页刷新 + 低频轮询兜底 server 重启/换模型）
    LM.poll.register("perfModel", {
      intervalMs: 120000, visibleIntervalMs: 60000, visibleOnly: true,
      run: function () { if (LM.nav.currentPage() === "performance" && LM.system) return LM.system.refreshLlamaInfo(); },
    });
    // MTP 区间（§218-§220：today 快更，历史 60s；仅性能页前台）
    LM.poll.register("mtpRange", {
      intervalMs: 60000, visibleIntervalMs: 60000, visibleOnly: true,
      run: function () {
        if (LM.nav.currentPage() !== "performance") return;
        // today 范围：MTP 数据随推理累积变化快，前台 30s
        if (state.mtpRange === "today") return refreshMtpRange();
        return refreshMtpRange();
      },
    });
    // Overview 摘要的「Draft Token 接受率」三态（/api/mtp 今天；任意页后台低频）
    LM.poll.register("mtp", {
      intervalMs: 60000, visibleIntervalMs: 30000, visibleOnly: false, run: refreshMtp,
    });
    LM.poll.register("daily", {
      intervalMs: 60000, visibleIntervalMs: 30000, visibleOnly: true,
      run: function () {
        var p = LM.nav.currentPage();
        // Round 5：performance 页的吞吐/MTP 改由 /api/throughput + /api/mtp/range
        // 独立驱动（throughput / mtpRange poller），不再依赖 /api/daily——
        // 去掉 performance，避免该页停留时多余的全量 daily 请求（Network 门 §276）。
        if (p === "usage" || p === "history") return refreshDaily();
        // 用量页：使用汇总卡 + 今天逐小时 随 daily 轮询一起刷新（保持前台新鲜）
        if (p === "usage") {
          var extra = [refreshUsageSummary()];
          if (state.dailyRangeMode === "today" && state.trendGran === "hour") extra.push(refreshTodayHourly());
          Promise.all(extra);
        }
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
    // 1.1 系统监控（页面作用域；四个域各自独立轮询、各自 catch）
    LM.poll.register("sysStatus", {
      intervalMs: R, visibleIntervalMs: R, visibleOnly: true,
      run: function () {
        if (!LM.system) return;
        var p = LM.nav.currentPage();
        if (p === "system" || p === "overview") {
          // 系统页：刷新后重绘逻辑处理器 Heat Grid（per-core 是 live-only，须随 status 节奏更新）
          return LM.system.refreshStatus().then(function () {
            if (LM.nav.currentPage() === "system") LM.system.renderCoreHeat();
          });
        }
      },
    });
    LM.poll.register("sysLive", {
      intervalMs: 60000, visibleIntervalMs: 15000, visibleOnly: true,
      run: function () {
        if (!LM.system) return;
        if (LM.nav.currentPage() === "system") return LM.system.refreshLive();
      },
    });
    // Round-3：网络接口列表（低频刷新，保持"自动=默认路由主接口"与选择器同步）
    LM.poll.register("sysNetIface", {
      intervalMs: 120000, visibleIntervalMs: 60000, visibleOnly: true,
      run: function () {
        if (!LM.system) return;
        if (LM.nav.currentPage() === "system") return LM.system.refreshNetworkInterfaces();
      },
    });
    LM.poll.register("sysSensors", {
      intervalMs: 120000, visibleIntervalMs: 60000, visibleOnly: true,
      run: function () {
        if (!LM.system) return;
        var p = LM.nav.currentPage();
        if (p === "system" || (p === "settings" && LM.settings.activeSection() === "system")) {
          return LM.system.refreshSensors();
        }
      },
    });
    LM.poll.register("sysInventory", {
      intervalMs: 300000, visibleIntervalMs: 120000, visibleOnly: true,
      run: function () {
        if (!LM.system) return;
        if (LM.nav.currentPage() === "system") return LM.system.refreshInventory(false);
      },
    });
    // Round 5：模型/Slot 由 perfSlots + perfModel 两个独立 poller 负责
    // （Slot 前台 ~5s 实时；Model 静态低频），不再共用单一 5s 双请求。
    // History 页事件/缺口由 "history" poller 负责（见上），旧的 "events" poller 已移除。
    // Updates：30s 全局（驱动横幅） + 1s 仅在 Updates 分区（下载进度，UI-023 统一进调度器）
    // /api/update/* 是 loopback-only——手机（局域网 IP）访问得 403。
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
    refreshLiveNow: function () { return refreshThroughput(); },
    refreshThroughputNow: function () { return refreshThroughput(); },
    refreshSummaryNow: function () { refreshSummary(); },
    refreshDailyNow: function () { refreshDaily(); },
    refreshMtpNow: function () { refreshMtp(); },
    refreshDataQualityNow: function () { refreshDataQuality(); },
    refreshEventsNow: function () { if (LM.nav.currentPage() === "history") return refreshHistoryNow(false); },
    // 设置页"服务器"卡复用已有 /api/status 轮询状态
    // （不额外高频探测）。返回 {status:'unknown'|'online'|'offline', url}
    serverConnectionState: function () {
      return {
        status: state.online === true ? "online" : state.online === false ? "offline" : "unknown",
        url: state.serverUrlText || "",
      };
    },
  };

  
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
