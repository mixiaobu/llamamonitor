/* ============================================================
   LlamaMonitor — Charts（Phase 15, spec §33-§37/§50/§51/§117-119）
   ECharts 统一管理：
   - 单一主题对象（dark/light，与 tokens 同步）；
   - 语义颜色集中管理（spec §34：全应用同一映射）；
   - 懒初始化（UI-013：页面首次可见时 init）+ 单一 ResizeObserver
     （UI-007：不再 window resize 双路）；
   - 空态：0 点时统一 EmptyState 覆盖（UI-011）；
   - animation 关闭（spec §119：监控面板不需要 5s 一次动画）；
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

  /* ---------- 语义颜色（spec §34：全应用唯一映射） ----------
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
  var instances = {};     // id -> echarts instance
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

  /** 已初始化实例数（ECharts instance leak 审计用，spec §140）。 */
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
        // Phase 16C：ui.showEmpty 有 dataset.empty 早退守卫——清空后必须复位，
        // 否则在两个不同空态间切换（如"等待数据"→"暂无吞吐数据"）会留下空白 overlay。
        delete overlay.dataset.empty;
        LM.ui.showEmpty(overlay, { icon: "emptyChart", title: title || "暂无数据", desc: desc || "" });
      }
    } else {
      box.classList.remove("has-empty");
    }
  }

  /* ---------- 单点数据可见性（BUG-E spec §127：1 点必须显示点） ----------
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

  /* ---------- 通用 tooltip 样式 ---------- */
  function baseTooltip() {
    var p = pal();
    return {
      backgroundColor: p.tooltipBg,
      borderColor: p.tooltipBorder,
      borderWidth: 1,
      padding: [8, 12],
      textStyle: { color: p.text, fontSize: 12, fontFamily: "Segoe UI Variable Text, Segoe UI, sans-serif" },
      extraCssText: "border-radius:6px;box-shadow:0 4px 12px rgba(0,0,0,.25);",
    };
  }

  function baseAxisLabel(p, extra) {
    var l = { color: p.axisWeak, fontSize: 11 };
    if (extra) for (var k in extra) l[k] = extra[k];
    return l;
  }

  /* 16D 移动端：时间/日期轴防挤压——窄容器自动缩短标签 + 抽稀，
     不再全部挤成一团。time 轴用 minInterval（ECharts 自动按间隔抽稀）。 */
  function _chartWidth(containerId) {
    var dom = typeof document !== "undefined" ? document.getElementById(containerId) : null;
    return dom ? Math.round(dom.getBoundingClientRect().width) : 800;
  }
  function timeAxisLabel(p, containerId, extra) {
    var l = baseAxisLabel(p, extra);
    var w = _chartWidth(containerId);
    if (w < 640) {
      // 窄屏：只留 HH:MM + 粗抽稀（20 分钟一档），旋转标签尖端间距 ≥ 25px
      l.formatter = function (v) { return F.formatHM(v / 1000); };
      l.minInterval = 20 * 60 * 1000;
      l.hideOverlap = true;
      l.margin = 8;
    }
    return l;
  }
  function categoryDateAxisLabel(p, containerId, rowCount) {
    var l = baseAxisLabel(p);
    var w = _chartWidth(containerId);
    // 窄屏：日期 "2026-07-18" → "07-18"（省一半宽度），按容器宽抽稀
    if (w < 640) {
      l.formatter = function (s) {
        s = String(s);
        return s.length > 8 ? s.slice(5) : s; // MM-DD
      };
      var fit = Math.max(1, Math.floor(w / 40)); // 每个 MM-DD 标签约 36px
      l.interval = Math.max(0, Math.ceil(rowCount / fit) - 1);
      l.hideOverlap = true;
      l.margin = 8;
    }
    return l;
  }

  /* ================================================================
     图表 1：Daily Token Usage（Stacked Bar：Prompt / Cached / Output）
     spec §35：适度圆角、tooltip 含 Logical Total。
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
              F.formatTokenCount(it.value) + "</span></div>";
          });
          // Logical Total（tooltip 完整信息，spec §35/§66）
          var day = null;
          for (var i = 0; i < rows.length; i++) if (rows[i].date === date) { day = rows[i]; break; }
          var logicalTotal = day ? (day.logical_tokens != null ? day.logical_tokens :
            (day.prompt_tokens || 0) + (day.cached_tokens || 0) + (day.output_tokens || 0)) : null;
          lines += "<div style='display:flex;justify-content:space-between;gap:16px;border-top:1px solid " + p.split +
            ";margin-top:4px;padding-top:4px'><span>逻辑合计</span><span style='font-variant-numeric:tabular-nums'>" +
            F.formatTokenCount(logicalTotal) + "</span></div>";
          return lines;
        },
      }),
      legend: {
        data: ["提示", "缓存", "输出"],
        textStyle: { color: p.axis, fontSize: 12 },
        top: 0, right: 0, icon: "rect", itemWidth: 10, itemHeight: 10, itemGap: 14,
      },
      // 长范围（>31 天，如"本月/全部"）启用 dataZoom（spec §113）
      dataZoom: rows.length > 31 ? [
        { type: "inside", start: 0, end: 100 },
        { type: "slider", height: 14, bottom: 2, borderColor: "transparent",
          backgroundColor: "transparent" },
      ] : undefined,
      grid: { left: 8, right: 8, top: 32, bottom: rows.length > 31 ? 24 : 4, containLabel: true },
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
          name: "提示", type: "bar", stack: "tok", barMaxWidth: 28,
          itemStyle: { color: col.prompt, borderRadius: [0, 0, 0, 0] },
          emphasis: { focus: "series" },
          data: rows.map(function (r) { return r.prompt_tokens; }),
        },
        {
          name: "缓存", type: "bar", stack: "tok", barMaxWidth: 28,
          itemStyle: { color: col.cached },
          emphasis: { focus: "series" },
          data: rows.map(function (r) { return r.cached_tokens; }),
        },
        {
          name: "输出", type: "bar", stack: "tok", barMaxWidth: 28,
          itemStyle: { color: col.output, borderRadius: [3, 3, 0, 0] },
          emphasis: { focus: "series" },
          data: rows.map(function (r) { return r.output_tokens; }),
        },
      ],
    }, true);
  }

  /* ================================================================
     图表 2：TPS 实时曲线（固定 60 分钟窗口）
     spec §36：2px 线、默认无 symbol、hover 显示 point。
     samples: /api/live samples（timestamp, prompt_tps, decode_tps）
     ================================================================ */
  function renderTpsChart(containerId, id, samples) {
    var c = chart(id);
    if (!c) return;
    var p = pal(), col = colors();
    var pts = samples || [];
    // PERF-001（Phase 16C §6）：以"有效 TPS 样本"（prompt/decode 至少一个非 null）
    // 作为判定依据。此前守卫是 !pts.length（全部样本数）——只要窗口里有
    // idle 样本（tps=null）就画出一整副空坐标系而空态被 setEmpty(false) 关掉。
    // 真实 0 TPS 样本（tps===0）仍算有效，允许显示 0。
    var actPts = pts.filter(function (s) {
      return s.prompt_tps != null || s.decode_tps != null;
    });
    if (actPts.length === 0) {
      setEmpty(containerId, true, "暂无吞吐数据",
        "最近 60 分钟没有检测到有效的 Token 生成活动。开始一次推理后，这里将显示 Prompt TPS 和 Decode TPS。");
      return;
    }
    setEmpty(containerId, false);
    function series(name, field, color) {
      var vals = pts.map(function (s) {
        return s[field] == null ? null : Number(s[field]);
      });
      return Object.assign({
        name: name, type: "line",
        symbol: "circle",
        emphasis: { focus: "series" },
        lineStyle: { width: 2, color: color },
        itemStyle: { color: color },
        connectNulls: false,
        data: pts.map(function (s, i) {
          return [Math.round(s.timestamp * 1000), vals[i]];
        }),
      }, singlePointOpts(nonNullCount(vals)));
    }
    c.setOption({
      animation: false,
      tooltip: Object.assign(baseTooltip(), {
        trigger: "axis",
        valueFormatter: function (v) { return v == null ? "--" : F.formatTps(v) + " tok/s"; },
      }),
      legend: {
        data: ["提示 TPS", "解码 TPS"],
        textStyle: { color: p.axis, fontSize: 12 }, top: 0, right: 0,
        icon: "rect", itemWidth: 10, itemHeight: 10, itemGap: 14,
      },
      grid: { left: 8, right: 8, top: 32, bottom: 4, containLabel: true },
      xAxis: _gpuTimeAxis(p, id),
      yAxis: {
        type: "value",
        axisLabel: baseAxisLabel(p, { formatter: function (v) { return F.formatTps(v); } }),
        splitLine: { lineStyle: { color: p.split } },
      },
      series: [
        series("提示 TPS", "prompt_tps", col.tpsPrompt),
        series("解码 TPS", "decode_tps", col.tpsDecode),
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
    var data = (rows || []).map(function (r) {
      return [r.date, r.mtp_accept_rate == null ? null : Number(r.mtp_accept_rate)];
    }).filter(function (d) { return d[1] != null; });
    setEmpty(containerId, data.length === 0, "无 MTP 数据",
      "服务器报告投机解码（MTP）指标后显示接受率。");
    if (!data.length) return;
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
        data: data.map(function (d) { return d[0]; }),
        axisLabel: categoryDateAxisLabel(p, id, data.length),
        axisLine: { lineStyle: { color: p.split } },
        axisTick: { show: false },
      },
      yAxis: {
        type: "value", max: 100,
        axisLabel: baseAxisLabel(p, { formatter: function (v) { return v + "%"; } }),
        splitLine: { lineStyle: { color: p.split } },
      },
      series: [Object.assign({
        name: "接受率", type: "line",
        symbol: "circle",
        lineStyle: { width: 2, color: col.mtp },
        itemStyle: { color: col.mtp },
        connectNulls: false,
        data: data.map(function (d) { return d[1]; }),
      }, singlePointOpts(data.length))],
    }, true);
  }

  /* ================================================================
     图表 4：MTP Accepted Tokens by Draft Position（spec §63：
     只有 accepted 时标题必须叫 Accepted Tokens，不叫 Acceptance Rate）
     positions: /api/mtp positions（{position, accepted_tokens}）
     ================================================================ */
  function renderMtpPosChart(containerId, id, positions) {
    var c = chart(id);
    if (!c) return;
    var p = pal(), col = colors();
    var pos = positions || [];
    setEmpty(containerId, pos.length === 0, "无位置数据",
      "服务器今日尚未报告按位置的接受数据。");
    if (!pos.length) return;
    var base = pos[0] && pos[0].accepted_tokens ? pos[0].accepted_tokens : 0;
    c.setOption({
      animation: false,
      tooltip: Object.assign(baseTooltip(), {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        formatter: function (params) {
          var it = params[0];
          var rel = base > 0 && it.value != null ? (it.value / base * 100).toFixed(1) + "%" : "--";
          return "<div style='font-weight:600;margin-bottom:4px'>" + it.name + "</div>" +
            "已接受 Token：<b>" + F.formatTokenCount(it.value) + "</b><br/>" +
            "相对位置 0：" + rel;
        },
      }),
      grid: { left: 8, right: 8, top: 24, bottom: 4, containLabel: true },
      xAxis: {
        type: "category",
        data: pos.map(function (x) { return "位置 " + x.position; }),
        axisLabel: baseAxisLabel(p),
        axisLine: { lineStyle: { color: p.split } },
        axisTick: { show: false },
      },
      yAxis: {
        type: "value",
        axisLabel: baseAxisLabel(p, { formatter: function (v) { return F.formatTokenCount(v); } }),
        splitLine: { lineStyle: { color: p.split } },
      },
      series: [{
        name: "已接受 Token", type: "bar", barMaxWidth: 28,
        itemStyle: { color: col.mtp, borderRadius: [3, 3, 0, 0] },
        data: pos.map(function (x) { return x.accepted_tokens; }),
      }],
    }, true);
  }

  /* ================================================================
     GPU 图表（spec §37：不同量级不共用 Y 轴）
     - Utilization + VRAM：0-100%（同一张图，VRAM 虚线区分）
     - Power：W（独立图）
     - Temperature：°C（独立图）
     data: /api/gpu/live {gpus:[{uuid,index,name,points:[...]}]}
     visible: {uuid: bool} 显隐控制
     ================================================================ */
  function _gpuTimeAxis(p, containerId) {
    return {
      type: "time",
      axisLabel: timeAxisLabel(p, containerId, { formatter: function (v) { return F.formatHM(v / 1000); } }),
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
    // GPU-001（Phase 16C §11）：该 GPU 不支持此传感器（全 null）时
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

  /* BUG-D（spec §38）：字段级空态——某传感器全部为 null（不支持）
     与"有数据"区分，避免把 N/A 画成 0 或留一张看似有数据的空图。 */
  function _hasGpuField(data, field) {
    return (data && data.gpus || []).some(function (g) {
      return (g.points || []).some(function (pt) { return pt[field] != null; });
    });
  }

  function renderGpuUtilChart(containerId, id, data, visible) {
    var c = chart(id);
    if (!c) return;
    var p = pal(), col = colors();
    var gpus = (data && data.gpus || []).filter(function (g) { return !visible || visible[g.uuid] !== false; });
    var subset = { gpus: gpus };
    setEmpty(containerId, !_hasGpuField(subset, "utilization_percent"), "无 GPU 样本",
      "GPU 监控采集到样本后显示利用率与显存历史。");
    if (!gpus.length) return;
    var series = [];
    gpus.forEach(function (g) {
      var u = _gpuSeries(g, "utilization_percent", col, { suffix: "利用率" });
      var m = _gpuSeries(g, "memory_usage_percent", col, { suffix: "显存", dashed: true });
      if (u) series.push(u);
      if (m) series.push(m);
    });
    if (!series.length) { setEmpty(containerId, true, "无 GPU 样本", "GPU 监控采集到样本后显示利用率与显存历史。"); return; }
    c.setOption({
      animation: false,
      tooltip: Object.assign(baseTooltip(), {
        trigger: "axis",
        valueFormatter: function (v) { return v == null ? "--" : F.formatPercent(v, 0); },
      }),
      legend: _gpuLegend(p),
      grid: { left: 8, right: 8, top: 32, bottom: 4, containLabel: true },
      xAxis: _gpuTimeAxis(p, id),
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
    gpus.forEach(function (g) { var s = _gpuSeries(g, "power_draw_w", col, {}); if (s) series.push(s); });
    if (!series.length) { setEmpty(containerId, true, "无功耗数据", "GPU 未报告功耗传感器（不支持）或尚未采集到功耗样本。"); return; }
    c.setOption({
      animation: false,
      tooltip: Object.assign(baseTooltip(), {
        trigger: "axis",
        valueFormatter: function (v) { return v == null ? "--" : F.formatPower(v); },
      }),
      legend: _gpuLegend(p),
      grid: { left: 8, right: 8, top: 32, bottom: 4, containLabel: true },
      xAxis: _gpuTimeAxis(p, id),
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
    c.setOption({
      animation: false,
      tooltip: Object.assign(baseTooltip(), {
        trigger: "axis",
        valueFormatter: function (v) { return v == null ? "--" : F.formatTemp(v); },
      }),
      legend: _gpuLegend(p),
      grid: { left: 8, right: 8, top: 32, bottom: 4, containLabel: true },
      xAxis: _gpuTimeAxis(p, id),
      yAxis: {
        type: "value",
        axisLabel: baseAxisLabel(p, { formatter: function (v) { return v + "\u00B0C"; } }),
        splitLine: { lineStyle: { color: p.split } },
      },
      series: series,
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
    renderTpsChart: renderTpsChart,
    renderMtpChart: renderMtpChart,
    renderMtpPosChart: renderMtpPosChart,
    renderGpuUtilChart: renderGpuUtilChart,
    renderGpuPowerChart: renderGpuPowerChart,
    renderGpuTempChart: renderGpuTempChart,
  };
})();
