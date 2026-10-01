/* ============================================================
 LlamaMonitor — Charts, 
 ECharts 统一管理：
 - 单一主题对象（dark/light，与 tokens 同步）；
 - 语义颜色集中管理（全应用同一映射）；
 - 懒初始化（UI-013：页面首次可见时 init） + 单一 ResizeObserver
 （UI-007：不再 window resize 双路）；
 - 空态：0 点时统一 EmptyState 覆盖（UI-011）；
 - animation 关闭（监控面板不需要 5s 一次动画）；
 - 主题切换后全量重绘（UI-014）。
 ============================================================ */
(function () {
  "use strict";

  var F = LM.fmt;

  /* ---------- 主题（与 css/tokens.css 保持同一来源值） ---------- */
  var THEMES = {
    dark: {
      axis: "rgba(255,255,255,0.70)",
      axisWeak: "rgba(255,255,255,0.45)",
      split: "rgba(255,255,255,0.07)",
      tooltipBg: "#323232",
      tooltipBorder: "rgba(255,255,255,0.14)",
      text: "#ffffff",
      empty: "rgba(255,255,255,0.45)",
    },
    light: {
      axis: "#5d5d5d",
      axisWeak: "#8a8a8a",
      split: "#e5e5e5",
      tooltipBg: "#ffffff",
      tooltipBorder: "#d1d1d1",
      text: "#1b1b1b",
      empty: "#8a8a8a",
    },
  };

  /* ---------- 语义颜色（全应用唯一映射） ----------
 同一指标在任何页面/任何主题下颜色语义一致（主题内深浅调整）。 */
  var COLORS = {
    dark: {
      prompt: "#4da3ff",
      cached: "#7a8194",
      output: "#4cc38a",
      tpsPrompt: "#4da3ff",
      tpsDecode: "#4cc38a",
      mtp: "#c29fff",
      gpu: ["#4da3ff", "#4cc38a", "#c29fff", "#f7b955", "#ff99a4", "#5fd0d0"],
      power: "#f7b955",
      temp: "#ff99a4",
    },
    light: {
      prompt: "#0067c0",
      cached: "#616161",
      output: "#0f7b0f",
      tpsPrompt: "#0067c0",
      tpsDecode: "#0f7b0f",
      mtp: "#8764b8",
      gpu: ["#0067c0", "#0f7b0f", "#8764b8", "#9d5d00", "#c42b1c", "#007a7a"],
      power: "#9d5d00",
      temp: "#c42b1c",
    },
  };

  var currentTheme = "dark";
  function pal() { return THEMES[currentTheme]; }
  function colors() { return COLORS[currentTheme]; }

  /* ---------- 图表注册表 + 懒初始化 + 单一 ResizeObserver ---------- */
  var instances = {};     
  var observers = new Set();
  var ro = null;

  function ensureObserver() {
    if (ro || typeof ResizeObserver === "undefined") return;
    ro = new ResizeObserver(function (entries) {
      // 仅对已有实例的容器 resize（懒初始化未创建的跳过）
      for (var i = 0; i < entries.length; i++) {
        var id = entries[i].target.getAttribute("data-chart");
        if (instances[id] && entries[i].contentRect.width > 0) {
          instances[id].resize();
        }
      }
    });
  }

  /** 懒初始化：页面可见（容器 >0 宽）时创建实例。返回 instance 或 null。 */
  function chart(id) {
    if (instances[id]) return instances[id];
    if (typeof echarts === "undefined") return null;
    var el = document.getElementById(id);
    if (!el) return null;
    var w = el.clientWidth, h = el.clientHeight;
    if (w <= 0 || h <= 0) return null; // 容器未布局（页面隐藏）-> 延迟到可见
    var c = echarts.init(el, null, { devicePixelRatio: window.devicePixelRatio || 1 });
    c.setOption({ animation: false, backgroundColor: "transparent" });
    instances[id] = c;
    ensureObserver();
    if (ro) ro.observe(el);
    return c;
  }

  /** 页面可见时调用：初始化缺失的图表 + 全部 resize（navigation.js 调用）。 */
  function ensurePageCharts(ids) {
    ids.forEach(function (id) {
      chart(id);
      if (instances[id]) instances[id].resize();
    });
  }

  /** 主题切换：全部已初始化实例重绘（UI-014 修复）。renderers: {id: fn} */
  function retheme(renderers) {
    Object.keys(renderers).forEach(function (id) {
      if (instances[id]) renderers[id]();
    });
  }

  /** 已初始化实例数（ECharts instance leak 审计用，。 */
  function instanceCount() {
    return Object.keys(instances).length;
  }

  /* ---------- 空态（UI-011）：0 点时覆盖 EmptyState ---------- */
  function setEmpty(containerId, show, title, desc) {
    var box = document.getElementById(containerId);
    if (!box) return;
    if (show) {
      box.classList.add("has-empty");
      var overlay = box.querySelector(".chart-empty-overlay");
      if (overlay) {
        overlay.innerHTML = "";
        //ui.showEmpty 有 dataset.empty 早退守卫——清空后必须复位，
        // 否则在两个不同空态间切换（如"等待数据"→"暂无吞吐数据"）会留下空白 overlay。
        delete overlay.dataset.empty;
        LM.ui.showEmpty(overlay, { icon: "emptyChart", title: title || "暂无数据", desc: desc || "" });
      }
    } else {
      box.classList.remove("has-empty");
    }
  }

  /* ---------- 单点数据可见性（BUG-E 1 点必须显示点） ----------
 ECharts 默认 showSymbol=false 时，单点折线不可见。
 非空值 <=1 时强制显示 symbol。 */
  function nonNullCount(values) {
    var n = 0;
    for (var i = 0; i < (values || []).length; i++) if (values[i] != null) n++;
    return n;
  }
  function singlePointOpts(n) {
    return n <= 1
      ? { showSymbol: true, symbolSize: 6 }
      : { showSymbol: false };
  }

  /* ---------- Mobile 检测（Round-8：图表 mobile 专用配置） ----------
  与 CSS 断点一致：≤760px 竖屏，或手机横屏（宽 >760 但高 ≤480）。
  每次调用实时读 matchMedia，横竖屏 / 旋转后图表重绘时自动跟随
  （§242/§243 orientation resize）。 */
  function isMobile() {
    try {
      var m = window.matchMedia;
      if (!m) return false;
      return m("(max-width: 760px)").matches ||
        m("(orientation: landscape) and (max-height: 480px)").matches;
    } catch (e) { return false; }
  }

  /* ---------- 通用 tooltip 样式 ----------
  Round-8 §95：confine:true——tooltip 被限制在图表容器内，
  触屏点按靠近右缘/底缘的系列时不会超出屏幕被裁切。
  桌面同样受益（tooltip 不漂出图卡），无副作用。 */
  function baseTooltip() {
    var p = pal();
    return {
      backgroundColor: p.tooltipBg,
      borderColor: p.tooltipBorder,
      borderWidth: 1,
      padding: [8, 12],
      confine: true,
      textStyle: { color: p.text, fontSize: 12, fontFamily: "Segoe UI Variable Text, Segoe UI, sans-serif" },
      extraCssText: "border-radius:6px;box-shadow:0 4px 12px rgba(0,0,0,.25);max-width:280px;",
    };
  }

  /* 1.1.3：bar 宽度按数据点数分档（spec §44-46：1 天 40-64 / 7 天 28-48 /
     30 天 10-24 / 长历史更细）。避免 1 天 bar 占满、30 天 bar 挤成一团。 */
  function barWidthFor(count) {
    if (count <= 2) return 48;
    if (count <= 7) return 32;
    if (count <= 31) return 18;
    return 12;
  }

  /* 1.1.3：token 值 tooltip 双显——compact + 完整千分位（spec §48：
     "1.94M (1,942,381)"），避免大数只有缩写。 */
  function tokenTooltipValue(v) {
    if (v == null) return "--";
    return F.formatTokenCount(v) + " (" + F.formatTokenCountFull(v) + ")";
  }

  function baseAxisLabel(p, extra) {
    var l = { color: p.axisWeak, fontSize: 11 };
    if (extra) for (var k in extra) l[k] = extra[k];
    return l;
  }

  /* 移动端：时间/日期轴防挤压——窄容器自动缩短标签 + 抽稀，
 不再全部挤成一团。time 轴用 minInterval（ECharts 自动按间隔抽稀）。 */
  function _chartWidth(containerId) {
    var dom = typeof document !== "undefined" ? document.getElementById(containerId) : null;
    return dom ? Math.round(dom.getBoundingClientRect().width) : 800;
  }
  function timeAxisLabel(p, containerId, extra) {
    var l = baseAxisLabel(p, extra);
    var w = _chartWidth(containerId);
    if (w < 640) {
      // 窄屏：只留 HH:MM + 抽稀。
      // Round-8 §88-89：抽稀密度按数据实际跨度算——目标 ≤4~5 个时间标签，
      // interval 取"≥ span/4"的 nice 档位（1m/2m/5m/15m/30m/2h/4h/6h/12h/1d/2d），
      // 不再固定 20 分钟：15 分钟窗口给 5 分钟档，24 小时窗口给 6 小时档。
      var interval = 20 * 60 * 1000;
      var span = extra && extra.spanMs;
      if (span > 0) {
        var raw = span / 4;
        var nice = [60e3, 2 * 60e3, 5 * 60e3, 15 * 60e3, 30 * 60e3,
          2 * 3600e3, 4 * 3600e3, 6 * 3600e3, 12 * 3600e3, 864e5, 2 * 864e5];
        interval = nice[nice.length - 1];
        for (var i = 0; i < nice.length; i++) { if (nice[i] >= raw) { interval = nice[i]; break; } }
      }
      l.formatter = function (v) { return F.formatHM(v / 1000); };
      l.minInterval = interval;
      l.hideOverlap = true;
      l.margin = 8;
    }
    return l;
  }
  /* points 时间跨度（ms；乱序安全，取 min/max）。供 timeAxisLabel 抽稀。 */
  function _pointsSpan(points) {
    var min = Infinity, max = -Infinity;
    (points || []).forEach(function (s) {
      var t = s.timestamp;
      if (t == null) return;
      if (t < min) min = t;
      if (t > max) max = t;
    });
    return (isFinite(min) && isFinite(max)) ? (max - min) * 1000 : 0;
  }
  function categoryDateAxisLabel(p, containerId, rowCount) {
    var l = baseAxisLabel(p);
    var w = _chartWidth(containerId);
    // 窄屏：日期 "2026-07-18" → "07-18"（省一半宽度），按容器宽抽稀
    if (w < 640) {
      l.formatter = function (s) {
        s = String(s);
        return s.length > 8 ? s.slice(5) : s;
      };
      var fit = Math.max(1, Math.floor(w / 40)); // 每个 MM-DD 标签约 36px
      l.interval = Math.max(0, Math.ceil(rowCount / fit) - 1);
      l.hideOverlap = true;
      l.margin = 8;
    }
    return l;
  }

  /* Round-8 §86-87/§206-207：legend 策略。
  手机：legend 放顶部、允许换行（不横滚溢出屏幕）、字号 11；
  条目 ≤4 时 plain 自然换行；>4 用 scroll（ECharts 内置横滑，不溢出）。
  桌面保持原样（右上 inline 单行）。返回可直接赋给 option.legend 的对象。 */
  function mobileLegend(p, data, opts) {
    opts = opts || {};
    var base = {
      data: data,
      textStyle: { color: p.axis, fontSize: 11 },
      icon: "rect", itemWidth: 10, itemHeight: 10, itemGap: 10,
    };
    if (opts.show === false) return { show: false };
    if (!isMobile()) {
      base.top = 0;
      base.right = 0;
      base.itemGap = 14;
      base.fontSize = 12;
      return base;
    }
    if (data.length > 4) { base.type = "scroll"; }
    base.top = 0;
    base.left = 0;
    base.align = "left";
    return base;
  }

  /* Round-8 §100-101：dataZoom 策略。
  桌面：长范围（>31 天）inside + slider（可精确拖选）。
  手机：只 inside（双指/单指在图内平移缩放），不出占竖向空间的 slider。
  返回数组（可直接赋给 option.dataZoom）或 null（不需 zoom）。 */
  function longRangeDataZoom(count) {
    if (!count || count <= 31) return null;
    if (isMobile()) return [{ type: "inside", start: 0, end: 100, zoomOnMouseWheel: false, moveOnMouseMove: false }];
    return [
      { type: "inside", start: 0, end: 100 },
      { type: "slider", height: 14, bottom: 2, borderColor: "transparent", backgroundColor: "transparent" },
    ];
  }

  /* ================================================================
 图表 1：Daily Token Usage（Stacked Bar：Prompt / Cached / Output）
 适度圆角、tooltip 含 Logical Total。
 rows: /api/daily 行（date, prompt_tokens, cached_tokens, output_tokens, logical_tokens...）
 ================================================================ */
  function renderUsageChart(containerId, id, rows) {
    var c = chart(id);
    if (!c) return;
    var p = pal(), col = colors();
    var hasData = rows && rows.length > 0;
    setEmpty(containerId, !hasData, "暂无历史",
      "LlamaMonitor 将在采集指标时开始建立用量历史。");
    if (!hasData) return;
    var bw = barWidthFor(rows.length);
    c.setOption({
      animation: false,
      tooltip: Object.assign(baseTooltip(), {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        formatter: function (params) {
          var date = params[0] ? params[0].axisValue : "";
          var lines = "<div style='font-weight:600;margin-bottom:4px'>" + date + "</div>";
          (params || []).forEach(function (it) {
            lines += "<div style='display:flex;justify-content:space-between;gap:16px'>" +
              "<span>" + it.marker + it.seriesName + "</span><span style='font-variant-numeric:tabular-nums'>" +
              tokenTooltipValue(it.value) + "</span></div>";
          });
          // Logical Total（tooltip 完整信息，
          var day = null;
          for (var i = 0; i < rows.length; i++) if (rows[i].date === date) { day = rows[i]; break; }
          var logicalTotal = day ? (day.logical_tokens != null ? day.logical_tokens :
            (day.prompt_tokens || 0) + (day.cached_tokens || 0) + (day.output_tokens || 0)) : null;
          lines += "<div style='display:flex;justify-content:space-between;gap:16px;border-top:1px solid " + p.split +
            ";margin-top:4px;padding-top:4px'><span>Token 总量</span><span style='font-variant-numeric:tabular-nums'>" +
            F.formatTokenCount(logicalTotal) + "</span></div>";
          return lines;
        },
      }),
      legend: mobileLegend(p, ["Prompt Token", "缓存复用 Token", "生成 Token"]),
      // 长范围（>31 天，如"本月/全部"）启用 dataZoom。
      // Round-8 §100-101：手机 only inside（不出占空间的 slider）。
      dataZoom: longRangeDataZoom(rows.length),
      grid: { left: 8, right: 8, top: 32, bottom: isMobile() ? 4 : (rows.length > 31 ? 24 : 4), containLabel: true },
      xAxis: {
        type: "category",
        data: rows.map(function (r) { return r.date; }),
        axisLabel: categoryDateAxisLabel(p, id, rows.length),
        axisLine: { lineStyle: { color: p.split } },
        axisTick: { show: false },
      },
      yAxis: {
        type: "value",
        axisLabel: baseAxisLabel(p, { formatter: function (v) { return F.formatTokenCount(v); } }),
        splitLine: { lineStyle: { color: p.split } },
      },
      series: [
        {
          name: "Prompt Token", type: "bar", stack: "tok", barMaxWidth: bw,
          itemStyle: { color: col.prompt, borderRadius: [0, 0, 0, 0] },
          emphasis: { focus: "series" },
          data: rows.map(function (r) { return r.prompt_tokens; }),
        },
        {
          name: "缓存复用 Token", type: "bar", stack: "tok", barMaxWidth: bw,
          itemStyle: { color: col.cached },
          emphasis: { focus: "series" },
          data: rows.map(function (r) { return r.cached_tokens; }),
        },
        {
          name: "生成 Token", type: "bar", stack: "tok", barMaxWidth: bw,
          itemStyle: { color: col.output, borderRadius: [3, 3, 0, 0] },
          emphasis: { focus: "series" },
          data: rows.map(function (r) { return r.output_tokens; }),
        },
      ],
    }, true);
  }
  /* ================================================================
  图表 1b：今天逐小时 Token 趋势（1.1.4 Round 4）
  stacked bar：Prompt / 缓存复用 / 生成，x 轴 0-23 时。
  只画有样本的小时（后端返回的 hours 数组），缺失小时不插 0。 */
  function renderUsageHourlyChart(containerId, id, payload) {
    var c = chart(id);
    if (!c) return;
    var p = pal(), col = colors();
    var hours = (payload && payload.hours) || [];
    var hasData = hours.length > 0;
    setEmpty(containerId, !hasData, "暂无逐小时数据",
      "采集后自动生成今天逐小时曲线。");
    if (!hasData) return;
    var bw = barWidthFor(hours.length);
    var hourLabel = function (h) { return (h < 10 ? "0" : "") + h + ":00"; };
    c.setOption({
      animation: false,
      tooltip: Object.assign(baseTooltip(), {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        formatter: function (params) {
          var label = params[0] ? params[0].axisValue : "";
          var lines = "<div style='font-weight:600;margin-bottom:4px'>" + label + "</div>";
          var total = 0;
          (params || []).forEach(function (it) {
            if (it.value != null) total += it.value;
            lines += "<div style='display:flex;justify-content:space-between;gap:16px'>" +
              "<span>" + it.marker + it.seriesName + "</span><span style='font-variant-numeric:tabular-nums'>" +
              tokenTooltipValue(it.value) + "</span></div>";
          });
          lines += "<div style='display:flex;justify-content:space-between;gap:16px;border-top:1px solid " + p.split +
            ";margin-top:4px;padding-top:4px'><span>Token 总量</span><span style='font-variant-numeric:tabular-nums'>" +
            F.formatTokenCount(total) + "</span></div>";
          return lines;
        },
      }),
      legend: mobileLegend(p, ["Prompt Token", "缓存复用 Token", "生成 Token"]),
      grid: { left: 8, right: 8, top: 32, bottom: 4, containLabel: true },
      xAxis: {
        type: "category",
        data: hours.map(function (h) { return hourLabel(h.hour); }),
        axisLabel: categoryDateAxisLabel(p, id, hours.length),
        axisLine: { lineStyle: { color: p.split } },
        axisTick: { show: false },
      },
      yAxis: {
        type: "value",
        axisLabel: baseAxisLabel(p, { formatter: function (v) { return F.formatTokenCount(v); } }),
        splitLine: { lineStyle: { color: p.split } },
      },
      series: [
        { name: "Prompt Token", type: "bar", stack: "tok", barMaxWidth: bw,
          itemStyle: { color: col.prompt }, emphasis: { focus: "series" },
          data: hours.map(function (h) { return h.prompt_tokens; }) },
        { name: "缓存复用 Token", type: "bar", stack: "tok", barMaxWidth: bw,
          itemStyle: { color: col.cached }, emphasis: { focus: "series" },
          data: hours.map(function (h) { return h.cached_tokens; }) },
        { name: "生成 Token", type: "bar", stack: "tok", barMaxWidth: bw,
          itemStyle: { color: col.output, borderRadius: [3, 3, 0, 0] }, emphasis: { focus: "series" },
          data: hours.map(function (h) { return h.output_tokens; }) },
      ],
    }, true);
  }


  /* ================================================================
 图表 2：TPS 实时曲线（固定 60 分钟窗口）
 2px 线、默认无 symbol、hover 显示 point。
 samples: /api/live samples（timestamp, prompt_tps, decode_tps）
 ================================================================ */
  /* 采集缺口判定（Round 5 §44-§45）：相邻样本间隔 > pollInterval*3 -> 明显断点，
     必须在图中断线（connectNulls=false），绝不把 Idle/Gap 连成斜线。
     返回 Set（下标 i 处需要在其前插一个 null 断点）。 */
  function _gapBreaks(pts, pollIntervalSec) {
    var breaks = {};
    if (!pts || pts.length < 2) return breaks;
    var thresh = (pollIntervalSec || 5) * 3;
    for (var i = 1; i < pts.length; i++) {
      var gap = pts[i].timestamp - pts[i - 1].timestamp;
      if (gap > thresh) breaks[i] = true;
    }
    return breaks;
  }

  function renderTpsChart(containerId, id, samples, opts) {
    var c = chart(id);
    if (!c) return;
    opts = opts || {};
    var p = pal(), col = colors();
    var pts = samples || [];
    // 空态（§58）：无有效吞吐样本（prompt/decode 均 null）-> 统一空态，不留空网格。
    // 真实 0 TPS（tps===0）是有效 Idle 样本，仍画 0（§42）。
    var actPts = pts.filter(function (s) {
      return s.prompt_tps != null || s.decode_tps != null;
    });
    if (actPts.length === 0) {
      setEmpty(containerId, true, "暂无推理吞吐数据",
        "所选时间范围内没有推理活动。产生推理请求后将在此显示实时吞吐趋势。");
      return;
    }
    setEmpty(containerId, false);
    var breaks = _gapBreaks(pts, opts.pollIntervalSec);
    // 系列数据：缺口处插 [ts,null]（connectNulls=false -> 断线；
    // 同一 ts 的 null→实值段不连线，避免竖直假线）
    function series(name, field, color) {
      var data = [];
      for (var i = 0; i < pts.length; i++) {
        var ts = Math.round(pts[i].timestamp * 1000);
        if (breaks[i]) data.push([ts, null]);
        data.push([ts, pts[i][field] == null ? null : Number(pts[i][field])]);
      }
      var vals = data.map(function (d) { return d[1]; });
      // 单点（含 gap 拆分后的单点段）必须显示 symbol（§59）
      return {
        name: name, type: "line",
        symbol: "circle", symbolSize: 5,
        showSymbol: actPts.length <= 7,
        emphasis: { focus: "series" },
        lineStyle: { width: 2, color: color },
        itemStyle: { color: color },
        connectNulls: false,
        data: data,
      };
    }
    // tooltip（§56）：时间 + 两路 TPS + 当时 requests（历史存在则显示）
    var tsIndex = {};
    pts.forEach(function (s) { tsIndex[s.timestamp] = s; });
    c.setOption({
      animation: false,
      tooltip: Object.assign(baseTooltip(), {
        trigger: "axis",
        formatter: function (params) {
          if (!params || !params.length) return "";
          var ts0 = Math.round(params[0].value[0] / 1000);
          var src = tsIndex[ts0] || {};
          var head = "<div style='font-weight:600;margin-bottom:4px'>" + F.formatHM(ts0) + "</div>";
          var lines = head;
          params.forEach(function (it) {
            lines += "<div style='display:flex;justify-content:space-between;gap:16px'>" +
              "<span>" + it.marker + it.seriesName + "</span><span style='font-variant-numeric:tabular-nums'>" +
              (it.value[1] == null ? "--" : F.formatTps(it.value[1]) + " tok/s") + "</span></div>";
          });
          if (src.requests_processing != null) {
            lines += "<div style='display:flex;justify-content:space-between;gap:16px;border-top:1px solid " + p.split +
              ";margin-top:4px;padding-top:4px'><span>处理中请求</span><span style='font-variant-numeric:tabular-nums'>" +
              src.requests_processing + "</span></div>";
          }
          return lines;
        },
      }),
      legend: mobileLegend(p, ["Prompt TPS", "Decode TPS"]),
      grid: { left: 8, right: 8, top: 32, bottom: 4, containLabel: true },
      xAxis: _gpuTimeAxis(p, id, pts),  // TPS 60 分钟窗口：span 自适应抽稀（~4 标签）
      // Y 轴自动缩放 + ~15% headroom（§53-§54），不固定 350/500 硬上限
      yAxis: {
        type: "value",
        max: function (v) { return v && v.max ? Math.ceil(v.max * 1.15) : undefined; },
        axisLabel: baseAxisLabel(p, { formatter: function (v) { return F.formatTps(v); } }),
        splitLine: { lineStyle: { color: p.split } },
      },
      series: [
        series("Prompt TPS", "prompt_tps", col.tpsPrompt),
        series("Decode TPS", "decode_tps", col.tpsDecode),
      ],
    }, true);
  }

  /* ================================================================
 图表 3：MTP Acceptance Rate 趋势（按天）
 rows: /api/daily 行（date, draft_tokens, accepted_tokens, mtp_accept_rate）
 ================================================================ */
  function renderMtpChart(containerId, id, rows) {
    var c = chart(id);
    if (!c) return;
    var p = pal(), col = colors();
    // rows: /api/mtp/range 的 days（{date, accept_rate}）。
    // 保留**全部日期位置**（7 天 -> 7 个位置；缺失日 accept_rate=null -> 断线/空位），
    // 形成完整 Calendar Timeline（§99-§100），不因为没数据就塌缩成 1 个点。
    var all = (rows || []).map(function (r) {
      return [r.date, r.accept_rate == null ? null : Number(r.accept_rate)];
    });
    var nonNull = all.filter(function (d) { return d[1] != null; });
    if (all.length === 0 || nonNull.length === 0) {
      setEmpty(containerId, true, "暂无 MTP 接受率数据",
        "所选范围内没有 MTP Draft 样本。产生 MTP 推理后显示 Draft Token 接受率趋势。");
      return;
    }
    setEmpty(containerId, false);
    c.setOption({
      animation: false,
      tooltip: Object.assign(baseTooltip(), {
        trigger: "axis",
        valueFormatter: function (v) { return v == null ? "--" : F.formatPercent(v); },
      }),
      legend: { show: false },
      grid: { left: 8, right: 8, top: 24, bottom: 4, containLabel: true },
      xAxis: {
        type: "category",
        data: all.map(function (d) { return d[0]; }),
        axisLabel: categoryDateAxisLabel(p, id, all.length),
        axisLine: { lineStyle: { color: p.split } },
        axisTick: { show: false },
      },
      yAxis: {
        type: "value", min: 0, max: 100,
        axisLabel: baseAxisLabel(p, { formatter: function (v) { return v + "%"; } }),
        splitLine: { lineStyle: { color: p.split } },
      },
      series: [{
        name: "Draft Token 接受率", type: "line",
        symbol: "circle", symbolSize: 5,
        showSymbol: nonNull.length <= 7,  // <=7 天显示点（§100-§102），30 天隐藏
        lineStyle: { width: 2, color: col.mtp },
        itemStyle: { color: col.mtp },
        connectNulls: false,               // 缺失日断线（不插 0 / 不连线）
        data: all.map(function (d) { return d[1]; }),
      }],
    }, true);
  }

  /* ================================================================
 图表 4：MTP Accepted Tokens by Draft Position
 只有 accepted 时标题必须叫 Accepted Tokens，不叫 Acceptance Rate）
 positions: /api/mtp positions（{position, accepted_tokens}）
 ================================================================ */
  function renderMtpPosChart(containerId, id, positions) {
    var c = chart(id);
    if (!c) return;
    var p = pal(), col = colors();
    var pos = positions || [];
    setEmpty(containerId, pos.length === 0, "无位置数据",
      "llama-server 今日尚未报告按 Draft 位置的接受数据。");
    if (!pos.length) return;
    // 只有 4 根柱左右：bar 宽度 40~56px（§249），label 全显（§107-§108 interval=0）。
    // tooltip 只展示**已接受 count**（§104-§106：无 per-position 分母，禁止算接受率）。
    var posBw = 48;
    c.setOption({
      animation: false,
      tooltip: Object.assign(baseTooltip(), {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        formatter: function (params) {
          var it = params[0];
          return "<div style='font-weight:600;margin-bottom:4px'>" + it.name + "</div>" +
            "已接受 Draft Token：<b>" + tokenTooltipValue(it.value) + "</b>";
        },
      }),
      grid: { left: 8, right: 8, top: 24, bottom: 4, containLabel: true },
      xAxis: {
        type: "category",
        data: pos.map(function (x) { return "位置 " + x.position; }),
        axisLabel: Object.assign(baseAxisLabel(p), { interval: 0 }),  // 位置 0~3 全显示
        axisLine: { lineStyle: { color: p.split } },
        axisTick: { show: false },
      },
      yAxis: {
        type: "value",
        axisLabel: baseAxisLabel(p, { formatter: function (v) { return F.formatTokenCount(v); } }),
        splitLine: { lineStyle: { color: p.split } },
      },
      series: [{
        name: "已接受 Draft Token", type: "bar", barMaxWidth: posBw,
        itemStyle: { color: col.mtp, borderRadius: [3, 3, 0, 0] },
        data: pos.map(function (x) { return x.accepted_tokens; }),
      }],
    }, true);
  }

  /* ================================================================
 GPU 图表（不同量级不共用 Y 轴）
 - Utilization + VRAM：0-100%（同一张图，VRAM 虚线区分）
 - Power：W（独立图）
 - Temperature：°C（独立图）
 data: /api/gpu/live {gpus:[{uuid,index,name,points:[...]}]}
 visible: {uuid: bool} 显隐控制
 ================================================================ */
  function _gpuTimeAxis(p, containerId, data) {
    // data: GPU 数组（[{points:[...]}]）或平铺 points 数组（TPS/系统）。
    var span = 0;
    (data || []).forEach(function (g) {
      if (g && g.points) span = Math.max(span, _pointsSpan(g.points));
      else if (g && g.timestamp != null) span = Math.max(span, _pointsSpan([g]));
    });
    return {
      type: "time",
      axisLabel: timeAxisLabel(p, containerId, {
        formatter: function (v) { return F.formatHM(v / 1000); },
        spanMs: span,
      }),
      axisLine: { lineStyle: { color: p.split } },
      axisTick: { show: false },
      splitLine: { show: false },
    };
  }

  function _gpuLegend(p) {
    return { textStyle: { color: p.axis, fontSize: 10 }, top: 0, type: "scroll" };
  }

  function _gpuSeries(gpu, field, col, opts) {
    var idx = gpu.index == null ? "?" : gpu.index;
    var color = (opts && opts.color) || col.gpu[Number(idx) % col.gpu.length];
    var vals = (gpu.points || []).map(function (pt) {
      return pt[field] == null ? null : Number(pt[field]);
    });
    // GPU-001：该 GPU 不支持此传感器（全 null）时
    // 不建 series——否则 ECharts 会在 legend 留一条永远没有数据的项。
    if (nonNullCount(vals) === 0) return null;
    var s = {
      name: "GPU " + idx + (opts && opts.suffix ? " " + opts.suffix : ""),
      type: "line", symbol: "circle", symbolSize: 4,
      lineStyle: { width: 2, color: color },
      itemStyle: { color: color },
      connectNulls: false,
      data: (gpu.points || []).map(function (pt, i) {
        return [Math.round(pt.timestamp * 1000), vals[i]];
      }),
    };
    if (opts && opts.dashed) s.lineStyle.type = "dashed";
    // BUG-E：单点数据必须显示点
    return Object.assign(s, singlePointOpts(nonNullCount(vals)));
  }

  function _hasGpuPoints(data) {
    return (data && data.gpus || []).some(function (g) { return (g.points || []).length > 0; });
  }

  /* BUG-D（字段级空态——某传感器全部为 null（不支持）
 与"有数据"区分，避免把 N/A 画成 0 或留一张看似有数据的空图。 */
  function _hasGpuField(data, field) {
    return (data && data.gpus || []).some(function (g) {
      return (g.points || []).some(function (pt) { return pt[field] != null; });
    });
  }

  /* Round-4 GPU 页：per-GPU 分组 tooltip（同一时刻各 GPU 的利用率 / 显存占用率 /
     显存 used/total 绝对值；按 GPU 名分组，不再 4 条 legend 平铺）。
     series name 形如 "GPU 0" / "GPU 0 显存占用"；gpuMap 由调用方提供 idx->meta，
     meta.pointsByMs 为预建的 ms->point 索引（O(1) 查绝对值，避免每帧扫全量点）。 */
  function _gpuGroupedTooltip(gpuMap, fmtVal) {
    return function (params) {
      if (!params || !params.length) return "";
      var ts = params[0].axisValue;
      var t = ts ? new Date(ts) : null;
      var head = t ? F.formatHM(t.getTime() / 1000) : (ts || "");
      var out = [head];
      var byGpu = {};
      var order = [];
      params.forEach(function (sp) {
        var m = (/^GPU (\d+)[ ]*/).exec(sp.seriesName || "");
        var gidx = m ? m[1] : "?";
        if (!byGpu[gidx]) { byGpu[gidx] = []; order.push(gidx); }
        byGpu[gidx].push(sp);
      });
      order.forEach(function (gidx) {
        var meta = gpuMap[gidx] || {};
        out.push("&#10009; " + (meta.name || ("GPU " + gidx)));
        byGpu[gidx].forEach(function (sp) {
          var isMem = sp.seriesName.indexOf("显存占用") > 0;
          var label = isMem ? "显存占用率" : "利用率";
          var val = sp.value == null ? "--" : fmtVal(sp.value);
          var line = "　" + label + "　" + val;
          if (isMem && meta.pointsByMs) {
            var pt = meta.pointsByMs[ts];
            if (pt && pt.memory_used_mb != null && pt.memory_total_mb != null) {
              line += "（" + F.formatVramMb(pt.memory_used_mb, pt.memory_total_mb) + "）";
            }
          }
          out.push(line);
        });
      });
      return out.join("<br>");
    };
  }

  function renderGpuUtilChart(containerId, id, data, visible) {
    var c = chart(id);
    if (!c) return;
    var p = pal(), col = colors();
    var gpus = (data && data.gpus || []).filter(function (g) { return !visible || visible[g.uuid] !== false; });
    var subset = { gpus: gpus };
    setEmpty(containerId, !_hasGpuField(subset, "utilization_percent"), "无 GPU 样本",
      "GPU 监控采集到样本后显示 GPU 利用率与显存占用历史。");
    if (!gpus.length) return;
    var series = [];
    var gpuMap = {};
    gpus.forEach(function (g) {
      var idx = g.index == null ? "?" : g.index;
      var pts = g.points || [];
      var byMs = {};
      pts.forEach(function (pt) { byMs[Math.round(pt.timestamp * 1000)] = pt; });
      gpuMap[idx] = { name: "GPU " + idx + (g.name ? " · " + g.name : ""), pointsByMs: byMs };
      var u = _gpuSeries(g, "utilization_percent", col, {});
      var m = _gpuSeries(g, "memory_usage_percent", col, { suffix: "显存占用", dashed: true });
      if (u) series.push(u);
      if (m) series.push(m);
    });
    if (!series.length) { setEmpty(containerId, true, "无 GPU 样本", "GPU 监控采集到样本后显示 GPU 利用率与显存占用历史。"); return; }
    c.setOption({
      animation: false,
      tooltip: Object.assign(baseTooltip(), {
        trigger: "axis",
        formatter: _gpuGroupedTooltip(gpuMap, function (v) { return F.formatPercent(v, 0); }),
      }),
      legend: { show: false },
      grid: { left: 8, right: 8, top: 16, bottom: 4, containLabel: true },
      xAxis: _gpuTimeAxis(p, id, gpus),
      yAxis: {
        type: "value", min: 0, max: 100,
        axisLabel: baseAxisLabel(p, { formatter: function (v) { return v + "%"; } }),
        splitLine: { lineStyle: { color: p.split } },
      },
      series: series,
    }, true);
  }

  function renderGpuPowerChart(containerId, id, data, visible) {
    var c = chart(id);
    if (!c) return;
    var p = pal(), col = colors();
    var gpus = (data && data.gpus || []).filter(function (g) { return !visible || visible[g.uuid] !== false; });
    var subset = { gpus: gpus };
    setEmpty(containerId, !_hasGpuField(subset, "power_draw_w"), "无功耗数据",
      "GPU 未报告功耗传感器（不支持）或尚未采集到功耗样本。");
    if (!gpus.length) return;
    var series = [];
    var limitByGpu = {};   // Round-4：tooltip 附带"功耗上限 / 占上限"（取该 GPU 最近一个非空 limit）
    gpus.forEach(function (g) {
      var limit = null;
      var pts = g.points || [];
      for (var i = pts.length - 1; i >= 0; i--) { if (pts[i].power_limit_w != null) { limit = pts[i].power_limit_w; break; } }
      limitByGpu[g.index == null ? "?" : g.index] = limit;
      var s = _gpuSeries(g, "power_draw_w", col, {});
      if (s) series.push(s);
    });
    if (!series.length) { setEmpty(containerId, true, "无功耗数据", "GPU 未报告功耗传感器（不支持）或尚未采集到功耗样本。"); return; }
    c.setOption({
      animation: false,
      tooltip: Object.assign(baseTooltip(), {
        trigger: "axis",
        formatter: function (params) {
          if (!params || !params.length) return "";
          var ts = params[0].axisValue;
          var t = ts ? new Date(ts) : null;
          var out = [t ? F.formatHM(t.getTime() / 1000) : (ts || "")];
          params.forEach(function (sp) {
            var m = (/^GPU (\d+)[ ]*/).exec(sp.seriesName || "");
            var gidx = m ? m[1] : "?";
            var val = sp.value == null ? "--" : F.formatPower(sp.value);
            var line = "　" + (sp.seriesName) + "　" + val;
            var limit = limitByGpu[gidx];
            if (limit != null) {
              var pct = sp.value == null ? null : (sp.value / limit * 100);
              line += "（上限 " + F.formatPower(limit) + (pct != null ? " · 占 " + F.formatPercent(pct, 0) : "") + "）";
            }
            out.push(line);
          });
          return out.join("<br>");
        },
      }),
      legend: _gpuLegend(p),
      grid: { left: 8, right: 8, top: 32, bottom: 4, containLabel: true },
      xAxis: _gpuTimeAxis(p, id, gpus),
      yAxis: {
        type: "value",
        axisLabel: baseAxisLabel(p, { formatter: function (v) { return v >= 1000 ? (v / 1000) + "kW" : v + "W"; } }),
        splitLine: { lineStyle: { color: p.split } },
      },
      series: series,
    }, true);
  }

  function renderGpuTempChart(containerId, id, data, visible) {
    var c = chart(id);
    if (!c) return;
    var p = pal(), col = colors();
    var gpus = (data && data.gpus || []).filter(function (g) { return !visible || visible[g.uuid] !== false; });
    var subset = { gpus: gpus };
    setEmpty(containerId, !_hasGpuField(subset, "temperature_c"), "无温度数据",
      "GPU 未报告温度传感器（不支持）或尚未采集到温度样本。");
    if (!gpus.length) return;
    var series = [];
    gpus.forEach(function (g) { var s = _gpuSeries(g, "temperature_c", col, {}); if (s) series.push(s); });
    if (!series.length) { setEmpty(containerId, true, "无温度数据", "GPU 未报告温度传感器（不支持）或尚未采集到温度样本。"); return; }
    // 1.1.3：温度轴 nice lower bound ≈ min(data)-10（clamp ≥0），
    // 避免 50-70°C 被 0 基线压扁看不出波动。
    var tmin = Infinity;
    gpus.forEach(function (g) { (g.points || []).forEach(function (pt) {
      if (pt.temperature_c != null) tmin = Math.min(tmin, Number(pt.temperature_c));
    }); });
    var tFloor = !isFinite(tmin) ? 0 : Math.max(0, Math.floor((tmin - 10) / 10) * 10);
    c.setOption({
      animation: false,
      tooltip: Object.assign(baseTooltip(), {
        trigger: "axis",
        valueFormatter: function (v) { return v == null ? "--" : F.formatTemp(v); },
      }),
      legend: _gpuLegend(p),
      grid: { left: 8, right: 8, top: 32, bottom: 4, containLabel: true },
      xAxis: _gpuTimeAxis(p, id, gpus),
      yAxis: {
        type: "value", min: tFloor,
        axisLabel: baseAxisLabel(p, { formatter: function (v) { return v + "\u00B0C"; } }),
        splitLine: { lineStyle: { color: p.split } },
      },
      series: series,
    }, true);
  }

  /* Round-4 GPU 页「更多趋势」（折叠区）：风扇转速 + SM/显存时钟历史。
     数据已在 gpu_samples 持久化（fan_percent / sm_clock_mhz / memory_clock_mhz），
     无需迁移。字段全 null 的 GPU 不建 series（GPU-001 同规则）。 */
  function renderGpuFanChart(containerId, id, data, visible) {
    var c = chart(id);
    if (!c) return;
    var p = pal(), col = colors();
    var gpus = (data && data.gpus || []).filter(function (g) { return !visible || visible[g.uuid] !== false; });
    var subset = { gpus: gpus };
    setEmpty(containerId, !_hasGpuField(subset, "fan_percent"), "无风扇数据",
      "GPU 未报告风扇转速（不支持，如被动散热卡）或尚未采集到风扇样本。");
    if (!gpus.length) return;
    var series = [];
    gpus.forEach(function (g) { var s = _gpuSeries(g, "fan_percent", col, {}); if (s) series.push(s); });
    if (!series.length) { setEmpty(containerId, true, "无风扇数据", "GPU 未报告风扇转速（不支持，如被动散热卡）或尚未采集到风扇样本。"); return; }
    c.setOption({
      animation: false,
      tooltip: Object.assign(baseTooltip(), {
        trigger: "axis",
        valueFormatter: function (v) { return v == null ? "--" : F.formatPercent(v, 0); },
      }),
      legend: _gpuLegend(p),
      grid: { left: 8, right: 8, top: 32, bottom: 4, containLabel: true },
      xAxis: _gpuTimeAxis(p, id, gpus),
      yAxis: {
        type: "value", min: 0, max: 100,
        axisLabel: baseAxisLabel(p, { formatter: function (v) { return v + "%"; } }),
        splitLine: { lineStyle: { color: p.split } },
      },
      series: series,
    }, true);
  }

  function renderGpuClockChart(containerId, id, data, visible) {
    var c = chart(id);
    if (!c) return;
    var p = pal(), col = colors();
    var gpus = (data && data.gpus || []).filter(function (g) { return !visible || visible[g.uuid] !== false; });
    var subset = { gpus: gpus };
    var hasSm = _hasGpuField(subset, "sm_clock_mhz");
    var hasMem = _hasGpuField(subset, "memory_clock_mhz");
    setEmpty(containerId, !hasSm && !hasMem, "无时钟数据",
      "GPU 未报告时钟或尚未采集到时钟样本。");
    if (!gpus.length) return;
    var series = [];
    gpus.forEach(function (g) {
      var sm = _gpuSeries(g, "sm_clock_mhz", col, { suffix: "SM" });
      var mem = _gpuSeries(g, "memory_clock_mhz", col, { suffix: "显存", dashed: true });
      if (sm) series.push(sm);
      if (mem) series.push(mem);
    });
    if (!series.length) { setEmpty(containerId, true, "无时钟数据", "GPU 未报告时钟或尚未采集到时钟样本。"); return; }
    c.setOption({
      animation: false,
      tooltip: Object.assign(baseTooltip(), {
        trigger: "axis",
        valueFormatter: function (v) { return v == null ? "--" : Math.round(v) + " MHz"; },
      }),
      legend: _gpuLegend(p),
      grid: { left: 8, right: 8, top: 32, bottom: 4, containLabel: true },
      xAxis: _gpuTimeAxis(p, id, gpus),
      yAxis: {
        type: "value",
        axisLabel: baseAxisLabel(p, { formatter: function (v) { return v >= 1000 ? (v / 1000) + "GHz" : v + "MHz"; } }),
        splitLine: { lineStyle: { color: p.split } },
      },
      series: series,
    }, true);
  }

  /* ================================================================
 系统页图表（1.1）：CPU 利用率 / 磁盘 I/O / 网络
 时间轴与 GPU 图一致（time 轴 + HH:MM），空态/单点行为复用同一套。
 points: /api/system/live {points:[{timestamp, ...}]}
 ================================================================ */
  /* §242-§244：时间精度按 range——短窗口（<30m，含 15m）HH:mm:ss；长窗口 HH:mm。
     用数据实际跨度判定（降采样后仍可靠），窄屏交 timeAxisLabel 抽稀。 */
  function _sysTimeFmt(points) {
    var short = false;
    if (points && points.length > 1) {
      var spanMs = points[points.length - 1].timestamp - points[0].timestamp;
      if (spanMs <= 30 * 60 * 1000) short = true;
    }
    return short ? function (v) { return F.formatTime(v / 1000); }
                 : function (v) { return F.formatHM(v / 1000); };
  }
  function _sysTimeAxis(p, containerId, points) {
    return {
      type: "time",
      axisLabel: timeAxisLabel(p, containerId, { formatter: _sysTimeFmt(points), spanMs: _pointsSpan(points) }),
      axisLine: { lineStyle: { color: p.split } },
      axisTick: { show: false },
      splitLine: { show: false },
    };
  }
  /* §44-§45：系统历史折线——细线（1.5px）+ 极轻 area fill（0.06），
     不要大面积高透明填充（避免 0~100 跳变变成"实心蓝墙"）。 */
  function _sysLineSeries(points, field, color, name) {
    var vals = points.map(function (s) {
      return s[field] == null ? null : Number(s[field]);
    });
    if (nonNullCount(vals) === 0) return null;
    var data = points.map(function (s, i) {
      return [Math.round(s.timestamp * 1000), vals[i]];
    });
    return Object.assign({
      name: name, type: "line",
      lineStyle: { width: 1.5, color: color },
      itemStyle: { color: color },
      areaStyle: { color: color, opacity: 0.06 },
      connectNulls: false,
      data: data,
    }, singlePointOpts(nonNullCount(vals)));
  }

  function _sysHasField(points, field) {
    return (points || []).some(function (s) { return s[field] != null; });
  }
  /* §46/§239：chart header 分析摘要（当前 / 平均 / 峰值）。summary 来自 /api/system/live
     （原始样本上算，准确）。无数据 -> ""（不显示）。 */
  function _sysSummaryText(summary, key, fmt) {
    var st = summary && summary[key];
    if (!st || (st.current == null && st.avg == null && st.max == null)) return "";
    return "当前 " + (st.current != null ? fmt(st.current) : "--") +
      " · 平均 " + (st.avg != null ? fmt(st.avg) : "--") +
      " · 峰值 " + (st.max != null ? fmt(st.max) : "--");
  }
  function _setSysSummary(id, text) {
    var el = document.getElementById(id);
    if (el) { el.textContent = text || ""; el.style.display = text ? "" : "none"; }
  }
  function _sysAxisPointer() {
    // §245：AxisPointer 1px 细线，非大面积灰块
    return { type: "line", lineStyle: { width: 1, color: "rgba(128,128,128,0.5)" } };
  }

  /* CPU 利用率（0-100%，单系列；§43-§48 细线+轻填充 + 当前/平均/峰值） */
  function renderSysCpuChart(containerId, id, points, summary) {
    var c = chart(id);
    if (!c) return;
    var p = pal(), col = colors();
    points = points || [];
    _setSysSummary("sysCpuChartSummary", _sysSummaryText(summary, "cpu", function (v) { return F.formatPercent(v, 0); }));
    _setSysSummary("sysCpuSummary", _sysSummaryText(summary, "cpu", function (v) { return F.formatPercent(v, 0); }));
    setEmpty(containerId, !_sysHasField(points, "cpu_usage_percent"), "暂无 CPU 历史数据",
      "系统监控采集到 CPU 利用率样本后显示最近时段的曲线。");
    if (!points.length) return;
    var s = _sysLineSeries(points, "cpu_usage_percent", col.gpu[0], "CPU 利用率");
    c.setOption({
      animation: false,
      tooltip: Object.assign(baseTooltip(), {
        trigger: "axis",
        axisPointer: _sysAxisPointer(),
        valueFormatter: function (v) { return v == null ? "--" : F.formatPercent(v, 0); },
      }),
      legend: { show: false },
      grid: { left: 8, right: 8, top: 24, bottom: 4, containLabel: true },
      xAxis: _sysTimeAxis(p, id, points),
      yAxis: {
        type: "value", min: 0, max: 100,
        axisLabel: baseAxisLabel(p, { formatter: function (v) { return v + "%"; } }),
        splitLine: { lineStyle: { color: p.split } },
      },
      series: s ? [s] : [],
    }, true);
  }

  /* 内存使用趋势（%；tooltip 给 Used / Total，§66-§72 主图只用 % 不双 Y 轴） */
  function renderSysMemChart(containerId, id, points, summary) {
    var c = chart(id);
    if (!c) return;
    var p = pal(), col = colors();
    points = points || [];
    _setSysSummary("sysMemChartSummary", _sysSummaryText(summary, "memory", function (v) { return F.formatPercent(v, 0); }));
    _setSysSummary("sysMemSummary", _sysSummaryText(summary, "memory", function (v) { return F.formatPercent(v, 0); }));
    setEmpty(containerId, !_sysHasField(points, "memory_usage_percent"), "暂无内存历史数据",
      "系统监控采集到内存使用样本后显示趋势。");
    if (!points.length) return;
    var s = _sysLineSeries(points, "memory_usage_percent", col.gpu[3], "内存使用率");
    c.setOption({
      animation: false,
      tooltip: Object.assign(baseTooltip(), {
        trigger: "axis",
        axisPointer: _sysAxisPointer(),
        formatter: function (params) {
          var line = "";
          (params || []).forEach(function (it) {
            var row = _sysRowAt(points, it.value && it.value[0]);
            var used = row && row.memory_used_bytes != null ? F.formatMemory(row.memory_used_bytes) : "--";
            var total = row && row.memory_total_bytes != null ? F.formatMemory(row.memory_total_bytes) : "--";
            line += "<div style='font-weight:600;margin-bottom:4px'>" + F.formatDateTime(it.value ? it.value[0] : 0) + "</div>" +
              "<div>" + (it.marker || "") + "使用率 " + (it.value != null && it.value[1] != null ? F.formatPercent(it.value[1], 0) : "--") + "</div>" +
              "<div>" + used + " / " + total + "（已用 / 总量）</div>";
          });
          return line;
        },
      }),
      legend: { show: false },
      grid: { left: 8, right: 8, top: 24, bottom: 4, containLabel: true },
      xAxis: _sysTimeAxis(p, id, points),
      yAxis: {
        type: "value", min: 0, max: 100,
        axisLabel: baseAxisLabel(p, { formatter: function (v) { return v + "%"; } }),
        splitLine: { lineStyle: { color: p.split } },
      },
      series: s ? [s] : [],
    }, true);
  }
  function _sysRowAt(points, ts) {
    if (!ts || !points.length) return points[points.length - 1];
    var t = ts;
    for (var i = points.length - 1; i >= 0; i--) {
      if (Math.abs(points[i].timestamp - t) < 1) return points[i];
    }
    return points[points.length - 1];
  }

  /* 磁盘 I/O（读/写 B/s，tooltip 自动 KB/s~GB/s） */
  function renderSysDiskChart(containerId, id, points) {
    var c = chart(id);
    if (!c) return;
    var p = pal(), col = colors();
    points = points || [];
    setEmpty(containerId, !_sysHasField(points, "disk_read_bps") && !_sysHasField(points, "disk_write_bps"), "暂无磁盘 I/O 数据",
      "系统监控采集到磁盘读写速率后显示。");
    if (!points.length) return;
    var series = [];
    var r = _sysLineSeries(points, "disk_read_bps", col.gpu[0], "读");
    var w = _sysLineSeries(points, "disk_write_bps", col.gpu[2], "写");
    if (r) series.push(r);
    if (w) series.push(w);
    if (!series.length) { setEmpty(containerId, true, "暂无磁盘 I/O 数据", "系统监控采集到磁盘读写速率后显示。"); return; }
    c.setOption({
      animation: false,
      tooltip: Object.assign(baseTooltip(), {
        trigger: "axis",
        axisPointer: _sysAxisPointer(),
        valueFormatter: function (v) { return v == null ? "--" : F.formatBytes(v) + "/s"; },
      }),
      legend: {
        data: ["读", "写"],
        textStyle: { color: p.axis, fontSize: 12 }, top: 0, right: 0,
        icon: "rect", itemWidth: 10, itemHeight: 10, itemGap: 14,
      },
      grid: { left: 8, right: 8, top: 32, bottom: 4, containLabel: true },
      xAxis: _sysTimeAxis(p, id, points),
      yAxis: {
        type: "value",
        axisLabel: baseAxisLabel(p, { formatter: function (v) { return F.formatBytes(v); } }),
        splitLine: { lineStyle: { color: p.split } },
      },
      series: series,
    }, true);
  }

  /* 网络（接收/发送 B/s） */
  function renderSysNetChart(containerId, id, points) {
    var c = chart(id);
    if (!c) return;
    var p = pal(), col = colors();
    points = points || [];
    setEmpty(containerId, !_sysHasField(points, "network_rx_bps") && !_sysHasField(points, "network_tx_bps"), "暂无网络数据",
      "系统监控采集到网络收发速率后显示。");
    if (!points.length) return;
    var series = [];
    var rx = _sysLineSeries(points, "network_rx_bps", col.gpu[0], "接收");
    var tx = _sysLineSeries(points, "network_tx_bps", col.gpu[1], "发送");
    if (rx) series.push(rx);
    if (tx) series.push(tx);
    if (!series.length) { setEmpty(containerId, true, "暂无网络数据", "系统监控采集到网络收发速率后显示。"); return; }
    c.setOption({
      animation: false,
      tooltip: Object.assign(baseTooltip(), {
        trigger: "axis",
        axisPointer: _sysAxisPointer(),
        valueFormatter: function (v) { return v == null ? "--" : F.formatBytes(v) + "/s"; },
      }),
      legend: {
        data: ["接收", "发送"],
        textStyle: { color: p.axis, fontSize: 12 }, top: 0, right: 0,
        icon: "rect", itemWidth: 10, itemHeight: 10, itemGap: 14,
      },
      grid: { left: 8, right: 8, top: 32, bottom: 4, containLabel: true },
      xAxis: _sysTimeAxis(p, id, points),
      yAxis: {
        type: "value",
        axisLabel: baseAxisLabel(p, { formatter: function (v) { return F.formatBytes(v); } }),
        splitLine: { lineStyle: { color: p.split } },
      },
      series: series,
    }, true);
  }

  /* 监测组件功耗趋势（W；§129-§132 单系列 总监测功耗，无历史时隐藏） */
  function renderSysPowerChart(containerId, id, points, summary) {
    var c = chart(id);
    if (!c) return;
    var p = pal(), col = colors();
    points = points || [];
    setEmpty(containerId, !_sysHasField(points, "monitored_component_power_w"), "暂无功耗历史数据",
      "有可读取的组件功耗传感器（CPU Package / GPU）且采集到历史后显示趋势。");
    if (!points.length) return;
    var s = _sysLineSeries(points, "monitored_component_power_w", col.gpu[2], "监测组件功耗");
    if (!s) { setEmpty(containerId, true, "暂无功耗历史数据", "有可读取的组件功耗传感器且采集到历史后显示趋势。"); return; }
    c.setOption({
      animation: false,
      tooltip: Object.assign(baseTooltip(), {
        trigger: "axis",
        axisPointer: _sysAxisPointer(),
        valueFormatter: function (v) { return v == null ? "--" : F.formatPower(v); },
      }),
      legend: { show: false },
      grid: { left: 8, right: 8, top: 24, bottom: 4, containLabel: true },
      xAxis: _sysTimeAxis(p, id, points),
      yAxis: {
        type: "value",
        axisLabel: baseAxisLabel(p, { formatter: function (v) { return F.formatPower(v); } }),
        splitLine: { lineStyle: { color: p.split } },
      },
      series: [s],
    }, true);
  }

  /* Round-5 History 页：完整性趋势（采集覆盖率 0-100% 主折线 + X 轴附近缺口小标记，
     不双 Y 轴，§70-71）。points: [{ts,label,coverage,valid_seconds,eligible_seconds,
     gap_count,possible_token_loss,is_today}]。coverage=null 的桶画断线（监测前/无窗口，
     §74/76）。点击由 app.js 绑 events（charts.chart('chartHistoryTrend').on('click')）。 */
  function renderIntegrityTrend(containerId, id, points) {
    var c = chart(id);
    if (!c) return;
    var p = pal(), col = colors();
    points = points || [];
    var anyCov = points.some(function (pt) { return pt.coverage != null; });
    setEmpty(containerId, !anyCov, "暂无监测完整性历史数据", "监测运行并产生采样后，这里显示采集覆盖率趋势。");
    if (!anyCov) return;
    var covCol = col.output, gapCol = col.power;
    var labels = points.map(function (pt) { return pt.label; });
    var covData = points.map(function (pt) { return pt.coverage == null ? null : Number(pt.coverage); });
    // 缺口小标记：有缺口的桶在 X 轴附近（y≈0）画 amber 小点（symbolSize 固定小，不双 Y 轴）
    var gapData = points.map(function (pt) { return (pt.gap_count && pt.gap_count > 0) ? 0.5 : null; });
    c.setOption({
      animation: false,
      tooltip: Object.assign(baseTooltip(), {
        trigger: "axis",
        axisPointer: { type: "line", lineStyle: { color: p.split } },
        formatter: function (params) {
          var it = (params || [])[0];
          if (!it || it.dataIndex == null) return "";
          var pt = points[it.dataIndex];
          if (!pt) return "";
          var head = (pt.is_today ? "今天 · " : "") + pt.label;
          if (pt.coverage == null) {
            return "<div style='font-weight:600;margin-bottom:4px'>" + head + "</div><div>未开始监测 / 无有效采集数据</div>";
          }
          var lines = "<div style='font-weight:600;margin-bottom:4px'>" + head + "</div>" +
            "<div>" + (it.marker || "") + "采集覆盖率 " + F.formatPercent(pt.coverage, 1) + "</div>" +
            "<div>有效采样 " + (pt.valid_seconds != null ? Math.round(pt.valid_seconds).toLocaleString("zh-CN") + " 秒" : "--") + "</div>" +
            "<div>预期采样 " + (pt.eligible_seconds != null ? Math.round(pt.eligible_seconds).toLocaleString("zh-CN") + " 秒" : "--") + "</div>" +
            "<div>采集缺口 " + (pt.gap_count || 0) + "</div>" +
            "<div>可能 Token 丢失 " + (pt.possible_token_loss ? "是" : "0") + "</div>";
          return lines;
        },
      }),
      legend: { show: false },
      grid: { left: 8, right: 8, top: 20, bottom: 4, containLabel: true },
      xAxis: {
        type: "category", data: labels,
        axisLabel: { color: p.axisWeak, fontSize: 11, hideOverlap: true, interval: "auto" },
        axisLine: { lineStyle: { color: p.split } },
        axisTick: { show: false },
      },
      yAxis: {
        type: "value", min: 0, max: 100,
        axisLabel: baseAxisLabel(p, { formatter: function (v) { return v + "%"; } }),
        splitLine: { lineStyle: { color: p.split } },
      },
      series: [
        {
          name: "采集覆盖率", type: "line",
          lineStyle: { width: 1.5, color: covCol },
          itemStyle: { color: covCol },
          areaStyle: { color: covCol, opacity: 0.06 },
          connectNulls: false,
          data: covData,
        },
        {
          name: "采集缺口", type: "scatter",
          symbolSize: 5, itemStyle: { color: gapCol },
          data: gapData, zlevel: 2, silent: true,
        },
      ],
    }, true);
  }

  window.LM = window.LM || {};
  LM.charts = {
    setTheme: function (t) { currentTheme = t === "light" ? "light" : "dark"; },
    theme: function () { return currentTheme; },
    chart: chart,
    ensurePageCharts: ensurePageCharts,
    retheme: retheme,
    instanceCount: instanceCount,
    renderUsageChart: renderUsageChart,
    renderIntegrityTrend: renderIntegrityTrend,
    renderUsageHourlyChart: renderUsageHourlyChart,
    setEmpty: setEmpty,
    renderTpsChart: renderTpsChart,
    renderMtpChart: renderMtpChart,
    renderMtpPosChart: renderMtpPosChart,
    renderGpuUtilChart: renderGpuUtilChart,
    renderGpuPowerChart: renderGpuPowerChart,
    renderGpuTempChart: renderGpuTempChart,
    renderGpuFanChart: renderGpuFanChart,
    renderGpuClockChart: renderGpuClockChart,
    renderSysCpuChart: renderSysCpuChart,
    renderSysMemChart: renderSysMemChart,
    renderSysDiskChart: renderSysDiskChart,
    renderSysNetChart: renderSysNetChart,
    renderSysPowerChart: renderSysPowerChart,
  };
})();
