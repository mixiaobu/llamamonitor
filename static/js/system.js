/* ============================================================
 LlamaMonitor — System（Round-3 产品化精修）
 系统页 + 概览主机状态 + 性能页模型/Slot 接线。
 职责：
 - refreshStatus：/api/system/status -> 系统概览 6 项 + CPU/内存/磁盘/网络/功耗 + 概览主机状态摘要
 - refreshLive：/api/system/live?minutes=N&max_points=N -> CPU/内存/磁盘/网络/功耗 图表 + chart header 摘要
 - refreshNetworkInterfaces：/api/system/network-interfaces -> 接口选择器（自动=默认路由主接口）
 - refreshDaily：/api/system/daily?days=1 -> 今日监测组件能耗（估算）
 - refreshSensors：/api/system/sensors -> 硬件传感器（温度/风扇/其它 结构化；Provider 状态）
 - refreshInventory：/api/system/inventory -> 硬件信息 + 卷容量 + 运行时长
 - refreshLlamaInfo/refreshLlamaSlots：/api/llama/info + /api/llama/slots -> 性能页
 设计原则：
 - 每个 refresh 独立 catch（一个 API 失败不拖垮其他区）；
 - null = 不可用 -> F.NA（--），绝不把 null 变 0（风扇/功耗/温度）；
 - 不支持的能力**隐藏**该项（不长期占主界面 --）；0 是有效读数（显示 0）；
 - 只读监控：无控制按钮。
 ============================================================ */
(function () {
  "use strict";

  var F = LM.fmt, api = LM.api, ui = LM.ui, charts = LM.charts;
  var $ = function (id) { return document.getElementById(id); };

  var systemLive = null;
  var systemLiveSummary = {};      // /api/system/live summary（chart header 当前/平均/峰值）
  var systemRangeMinutes = 60;     // 系统页当前范围（分钟）
  var systemRangePoints = 1500;    // 按 range 选择 max_points（§185-§190）
  var lastInventory = null;        // /api/system/inventory 缓存（运行时长/卷容量/硬件信息）
  var lastCpu = null;              // /api/system/status cpu 子对象（Heat Grid 每逻辑核用）
  var lastNet = null;              // /api/system/status network 子对象（adapters）
  var netIface = "auto";           // 接口选择器当前选择（auto=默认路由主接口）
  var netIfaces = [];              // /api/system/network-interfaces 接口列表
  var lastModel = null;
  var lastSlots = null;

  /* ---------- 小工具 ---------- */

  function setText(id, text) {
    var el = $(id);
    if (!el) return;
    el.textContent = text;
    el.classList.toggle("dim", text === F.NA);
  }
  function setSub(id, text, dim) {
    var el = $(id);
    if (!el) return;
    if (text) { el.textContent = text; el.hidden = false; el.classList.toggle("dim", !!dim); }
    else { el.textContent = ""; el.hidden = true; }
  }
  function freqText(mhz) {
    if (mhz == null) return F.NA;
    var v = Number(mhz);
    return v >= 1000 ? (v / 1000).toFixed(2) + " GHz" : v.toFixed(0) + " MHz";
  }
  function rateText(bps) { return bps == null ? F.NA : F.formatBytes(bps) + "/s"; }
  function uptimeSecs(bootEpoch) {
    if (!bootEpoch) return null;
    var now = Date.now() / 1000, d = now - Number(bootEpoch);
    return d > 0 ? d : null;
  }
  /* §29 系统运行时间统一格式："13 小时 56 分"（<1h 则 "X 分 Y 秒"）。 */
  function uptimeText(sec) {
    if (sec == null) return F.NA;
    sec = Math.max(0, Math.round(sec));
    if (sec < 3600) return Math.floor(sec / 60) + " 分 " + (sec % 60) + " 秒";
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
    if (h >= 24) { var d = Math.floor(h / 24); return d + " 天 " + (h % 24) + " 小时"; }
    return h + " 小时 " + m + " 分";
  }

  /* 组件功耗（§110-§114）：后端 monitored_components_w = 非 null 组件之和；
     前端回退求和（null 不参与，真实 0 保留）。 */
  function componentPower(pw) {
    if (pw.monitored_components_w != null) return pw.monitored_components_w;
    var parts = [pw.cpu_package_w, pw.gpu_total_w], sum = null;
    parts.forEach(function (v) { if (v != null) sum = (sum == null ? 0 : sum) + v; });
    return sum;
  }
  /* 组件功耗次值（§28/§113）：仅「N 个组件可读取」（缺失明细不再展开）。 */
  function componentPowerSub(pw) {
    var n = (pw.components_present != null) ? pw.components_present : 0;
    return n ? n + " 个组件可读取" : "";
  }

  /* ================= 系统概览 + CPU/内存/磁盘/网络/功耗 + 概览主机状态 ================= */
  function refreshStatus() {
    return api.get("/api/system/status")
      .then(function (d) {
        var cpu = d.cpu || {}, mem = d.memory || {}, disk = d.disk || {},
            net = d.network || {}, pw = d.power || {};
        lastCpu = cpu; lastNet = net;

        /* --- 系统概览（§22-§33：6 项；CPU 频率已移入 CPU 区） --- */
        setText("sysCpuUsage", cpu.usage_percent == null ? F.NA : F.formatPercent(cpu.usage_percent, 0));
        // CPU 利用率次值：峰值/当前频率（§46 概览不显频率；这里给当前频率小字，属 CPU 区职责，概览保持纯负载）
        if (mem.usage_percent != null) {
          var used = mem.used_bytes != null ? F.formatMemory(mem.used_bytes) : F.NA;
          var total = mem.total_bytes != null ? F.formatMemory(mem.total_bytes) : F.NA;
          setText("sysMemUsage", F.formatPercent(mem.usage_percent, 0));
          setSub("sysMemDetail", used + " / " + total);
        } else { setText("sysMemUsage", F.NA); setSub("sysMemDetail", ""); }
        _dual("sysDiskR", "sysDiskW", disk.read_bps, disk.write_bps, "读", "写");
        _dual("sysNetR", "sysNetW", net.rx_bps, net.tx_bps, "收", "发");
        var compW = componentPower(pw);
        setText("sysCompPower", compW == null ? F.NA : F.formatPower(compW));
        setSub("sysCompPowerSub", componentPowerSub(pw));
        _uptime();

        /* --- CPU 区 Summary（§35：当前利用率 / 当前频率 / Package 温度 / Package 功耗） --- */
        setText("sysCpuPct", cpu.usage_percent == null ? F.NA : F.formatPercent(cpu.usage_percent, 0));
        // 当前频率 + 基准频率次值（§11-§12/§63-§65：区分 current 与 base，不都叫"CPU 频率"）
        setText("sysCpuFreq", freqText(cpu.frequency_mhz));
        setSub("sysCpuFreqBase", cpu.base_frequency_mhz != null ? "基准 " + freqText(cpu.base_frequency_mhz) : "");
        // Package 温度 / 功耗：能力自适应（§36-§37）——不可用隐藏该 stat（不长期 --）
        _capabilityStat("sysCpuTemp", cpu.temperature_c, F.formatTemp);
        _capabilityStat("sysCpuPower", cpu.package_power_w, F.formatPower);

        /* --- 内存区（§67-§70：使用率 / 已用 / 可用 / 总计；Windows Available 语义） --- */
        setText("sysMemPct", mem.usage_percent == null ? F.NA : F.formatPercent(mem.usage_percent, 0));
        setText("sysMemUsed", mem.used_bytes != null ? F.formatMemory(mem.used_bytes) : F.NA);
        setText("sysMemAvail", mem.available_bytes != null ? F.formatMemory(mem.available_bytes) : F.NA);
        setText("sysMemTotal", mem.total_bytes != null ? F.formatMemory(mem.total_bytes) : F.NA);

        /* --- 网络区（§98/§103-§104：接收 / 发送 / 接口 / 链路速度） --- */
        setText("sysNetRx", net.rx_bps == null ? F.NA : F.formatBytes(net.rx_bps) + "/s");
        setText("sysNetTx", net.tx_bps == null ? F.NA : F.formatBytes(net.tx_bps) + "/s");
        _renderNetIface();

        /* --- 功耗与能耗（§109-§124） --- */
        setText("sysPowerComp", compW == null ? F.NA : F.formatPower(compW));
        setSub("sysPowerCompSub", componentPowerSub(pw));
        setText("sysPowerCpu", pw.cpu_package_w == null ? F.NA : F.formatPower(pw.cpu_package_w));
        setText("sysPowerGpu", pw.gpu_total_w == null ? F.NA : F.formatPower(pw.gpu_total_w));
        // 整机输入功耗（§118-§121）：无外部测量源 -> "未配置"（非 --）+ 说明
        setText("sysWallPower", pw.wall_power_w == null ? "未配置" : F.formatPower(pw.wall_power_w));
        setSub("sysWallPowerSub", "");

        /* Round-6：概览「主机状态」6 项改由 /api/overview 统一驱动（renderOvSystem）——
           与今日用量/推理/GPU/完整性同源同节奏，避免 /api/system/status 与 /api/overview
           两处写同一组 ov* 元素造成互相覆写。系统页仍用 sys* 元素（上方已填）。 */

        LM.poll.streakOk("system");
        /* --- 系统页状态徽章（§16-§17："采集正常"，仅表示采集器正常，非硬件健康） --- */
        ui.setStatusBadge($("systemPageState"), d.available ? "online" : "offline",
          d.available ? "采集正常" : "采集暂停");
      })
      .catch(function (e) {
        // 1.2.1：连续 >=5 次失败才切「采集暂停」；之前保留上次数据与状态
        console.warn("system status failed:", e.message || e);
        if (LM.poll.streakFail("system")) {
          ui.setStatusBadge($("systemPageState"), "offline", "采集暂停");
        }
      });
  }
  function _uptime() {
    var up = uptimeSecs(lastInventory && lastInventory.boot_time);
    setText("sysUptime", up == null ? F.NA : uptimeText(up));
  }
  /* 磁盘/网络双行（§26/§27）：↓ 读 / ↑ 写、↓ 收 / ↑ 发；单侧缺失该行 --（不清空整卡）。 */
  function _dual(idR, idW, r, w, lr, lw) {
    var eR = $(idR), eW = $(idW);
    if (eR) { eR.textContent = "↓ " + (r == null ? "--" : F.formatBytes(r) + "/s"); eR.classList.toggle("dim", r == null); }
    if (eW) { eW.textContent = "↑ " + (w == null ? "--" : F.formatBytes(w) + "/s"); eW.classList.toggle("dim", w == null); }
  }
  /* 能力自适应（§36-§37/§1001/§14）：value 非 null 才显示；null 时隐藏 stat 主体（不长期 --）。
     保留 stat 占位（避免布局跳动），数值行淡显"不可用"。 */
  function _capabilityStat(id, value, fmt) {
    var el = $(id); if (!el) return;
    if (value != null) {
      el.textContent = fmt(value);
      el.classList.remove("dim");
      el.parentElement.classList.remove("cap-hidden");
    } else {
      el.textContent = "不可用";
      el.classList.add("dim");
      el.parentElement.classList.add("cap-hidden");
    }
  }

  /* ================= CPU/内存/磁盘/网络/功耗 图表（live + summary） ================= */
  function _rangePoints(minutes) {
    // §185-§190：按 range 选合理 max_points（不以前端像素宽为后端参数）
    if (minutes <= 60) return 900;   // 15m/1h 原始点数少，留足
    if (minutes <= 360) return 1200;
    return 1500;                      // 6h/24h
  }
  function refreshLive() {
    systemRangePoints = _rangePoints(systemRangeMinutes);
    return api.get("/api/system/live?minutes=" + systemRangeMinutes + "&max_points=" + systemRangePoints)
      .then(function (d) {
        systemLive = (d && d.points) || [];
        systemLiveSummary = (d && d.summary) || {};
        charts.renderSysCpuChart("chartSysCpuBox", "chartSysCpu", systemLive, systemLiveSummary);
        charts.renderSysMemChart("chartSysMemBox", "chartSysMem", systemLive, systemLiveSummary);
        charts.renderSysDiskChart("chartSysDiskBox", "chartSysDisk", systemLive);
        charts.renderSysNetChart("chartSysNetBox", "chartSysNet", systemLive);
        charts.renderSysPowerChart("chartSysPowerBox", "chartSysPower", systemLive, systemLiveSummary);
      })
      .catch(function (e) { console.warn("system live failed:", e.message || e); });
  }

  /* ================= 今日监测组件能耗（估算，daily） ================= */
  function refreshDaily() {
    return api.get("/api/system/daily?days=1")
      .then(function (d) {
        var days = (d && d.days) || [];
        var today = days.length ? days[days.length - 1] : null;
        var wh = today ? today.monitored_component_energy_wh : null;
        setText("sysEnergyToday", wh == null ? F.NA : F.formatEnergy(wh));
      })
      .catch(function (e) { console.warn("system daily failed:", e.message || e); });
  }

  /* ================= 网络接口选择器（§97-§108） ================= */
  function refreshNetworkInterfaces() {
    return api.get("/api/system/network-interfaces")
      .then(function (d) {
        netIfaces = (d && d.interfaces) || [];
        _buildNetIfaceSelect(d && d.default_interface);
        _renderNetIface();
      })
      .catch(function (e) { console.warn("network interfaces failed:", e.message || e); });
  }
  function _buildNetIfaceSelect(defaultName) {
    var sel = $("sysNetIface"); if (!sel) return;
    // 保留已选值
    var keep = sel.value || netIface;
    sel.innerHTML = "";
    var opt = document.createElement("option");
    opt.value = "auto"; opt.textContent = "自动" + (defaultName ? " · " + defaultName : "");
    sel.appendChild(opt);
    netIfaces.forEach(function (itf) {
      var o = document.createElement("option");
      o.value = itf.name;
      o.textContent = itf.name + (itf.kind ? "（" + itf.kind + "）" : "") +
        (itf.is_default ? " · 默认" : "") + (itf.is_virtual ? " · 虚拟" : "");
      sel.appendChild(o);
    });
    sel.value = (keep && Array.prototype.some.call(sel.options, function (x) { return x.value === keep; })) ? keep : "auto";
    netIface = sel.value;
    if (!sel._bound) {
      sel._bound = true;
      sel.addEventListener("change", function () { netIface = sel.value; _renderNetIface(); });
    }
  }
  /* 网络区 Summary：按选择显示接收/发送/接口/链路速度。
     auto = 默认路由主接口（后端已算默认接口速率，net.rx/tx 即默认接口）；
     其它 = 从 net.adapters 取该接口速率；"sum"=全接口合计。 */
  function _renderNetIface() {
    var net = lastNet || {};
    var adapters = net.adapters || {};
    var nameEl = $("sysNetIfaceName"), cardEl = $("sysNetIfaceCard"), kindEl = $("sysNetIfaceKind"),
        speedEl = $("sysNetSpeed"), rxEl = $("sysNetRx"), txEl = $("sysNetTx"),
        sel = $("sysNetIface");
    var chosen = (netIface && netIface !== "auto") ? netIface : (net.interface || null);
    var isAuto = (!netIface || netIface === "auto");
    var itf = null;
    netIfaces.forEach(function (x) { if (x.name === chosen) itf = x; });
    var dispName = chosen ? chosen : (isAuto ? "接口合计" : "--");
    if (nameEl) nameEl.textContent = dispName;
    if (cardEl) { cardEl.textContent = dispName; cardEl.classList.toggle("dim", !chosen); }
    // 链路速度 / 类型
    if (itf && itf.speed_mbps) {
      if (speedEl) { speedEl.textContent = itf.speed_mbps >= 1000 ? (itf.speed_mbps / 1000) + " Gbps" : itf.speed_mbps + " Mbps"; }
    } else if (speedEl) { speedEl.textContent = F.NA; speedEl.classList.add("dim"); }
    if (kindEl) { kindEl.textContent = itf ? itf.kind : (chosen ? "接口合计" : ""); }
    // 接收/发送速率
    if (chosen && adapters[chosen] && (adapters[chosen].rx_bps != null || adapters[chosen].tx_bps != null)) {
      var a = adapters[chosen];
      if (rxEl) { rxEl.textContent = a.rx_bps == null ? F.NA : F.formatBytes(a.rx_bps) + "/s"; rxEl.classList.toggle("dim", a.rx_bps == null); }
      if (txEl) { txEl.textContent = a.tx_bps == null ? F.NA : F.formatBytes(a.tx_bps) + "/s"; txEl.classList.toggle("dim", a.tx_bps == null); }
    } else if (isAuto && net.interface != null) {
      // auto 且默认接口 -> 后端 net.rx/tx 已是默认接口速率（refreshStatus 已填），不覆盖
    } else if (netIface === "sum") {
      if (rxEl) rxEl.textContent = net.rx_bps == null ? F.NA : F.formatBytes(net.rx_bps) + "/s";
    }
    // 图表 note（§101：接口合计时提示可能含虚拟网卡）
    var note = $("sysNetChartNote");
    if (note) note.textContent = isAuto ? (net.interface ? "" : "全接口合计（可能含虚拟网卡）") : "";
  }

  /* ================= 硬件传感器（结构化 §134-§153） ================= */
  var lastSensorState = null; // Provider Runtime State（Settings 页高级传感器开关依赖用）

  function refreshSensors() {
    return api.get("/api/system/sensors")
      .then(function (d) {
        LM.poll.streakOk("sensors");
        var state = d.state || (d.available ? "available" : "unavailable");
        lastSensorState = state;
        var sensors = d.sensors || [];
        var counts = d.counts || {};
        var badge = $("sensorState");
        if (badge) {
          // §136："传感器可用"（去掉"高级"）
          if (state === "available") ui.setStatusBadge(badge, "online", "传感器可用");
          else if (state === "partial") ui.setStatusBadge(badge, "warning", "部分可用");
          else ui.setStatusBadge(badge, "offline", "硬件传感器不可用");
        }
        _renderSensors(state, sensors, d, counts);
        /* Settings 页 Provider 状态（若存在；Round-7 结构：基础监控/高级硬件传感器 两行） */
        var mon = $("sysMonState");
        if (mon) {
          var advEl = $("sysMonAdvState");
          var monSub = $("sysMonStateSub");
          mon.classList.remove("online", "offline", "ok", "warn", "bad");
          if (state === "available") { mon.classList.add("ok"); if (advEl) advEl.textContent = "可用"; }
          else if (state === "partial") { mon.classList.add("warn"); if (advEl) advEl.textContent = "部分可用"; }
          else { mon.classList.add("bad"); if (advEl) advEl.textContent = "不可用"; }
          if (monSub) monSub.textContent = state === "unavailable" ? "硬件传感器提供程序不可用。基础系统监控（CPU/内存/磁盘/网络）继续正常。" : "";
        }
        var monList = $("sysMonSensorList");
        if (monList) _renderSettingsSensorList(monList, sensors, counts, state);
      })
      .catch(function (e) {
        // 1.2.1：连续 >=5 次失败才提示；之前保留最后成功的数据
        console.warn("system sensors failed:", e.message || e);
        if (!LM.poll.streakFail("sensors")) return;
        var monList = $("sysMonSensorList");
        if (monList) { monList.innerHTML = ""; var t = document.createElement("div"); t.className = "stat-hint";
          t.textContent = "高级传感器暂不可用"; monList.appendChild(t); }
        var box = $("sysSensors");
        if (box) { box.innerHTML = ""; var t2 = document.createElement("div"); t2.className = "stat-hint";
          t2.textContent = "传感器暂不可用"; box.appendChild(t2); }
      });
  }
  /* 传感器分类（§138/§140-§142/§149）：温度 / 风扇 / 功耗 / 其它。
     Load 类（CPU load / memory load）归各自主区，不在这里重复（§139）。
     GPU 详细温度/风扇留给显卡页（§141）——这里 GPU 温度可展示但不展开。 */
  function _sensorCat(s) {
    var t = String(s.sensor_type || "").toLowerCase();
    var hw = String(s.hardware_type || "").toLowerCase();
    if (t === "fan" || hw === "cooling" || hw === "fan") return "fan";
    if (t === "temperature" || t === "temp") return "temp";
    if (t === "power") return "power";
    if (t === "control") return "control";
    if (t === "load") return "load";
    return "other";
  }
  function _fmtSensor(s) {
    var t = String(s.sensor_type || "").toLowerCase();
    if (t === "fan") return F.formatInt(s.latest) + " RPM";
    if (t === "control") return F.formatPercent(s.latest, 0);
    if (t === "temperature") return F.formatTemp(s.latest);
    if (t === "power") return F.formatPower(s.latest);
    if (t === "load") return F.formatPercent(s.latest, 0);
    return String(s.latest);
  }
  function _renderSensors(state, sensors, raw, counts) {
    var box = $("sysSensors"); if (!box) return;
    box.innerHTML = "";
    if (state === "unavailable") {
      var n = document.createElement("div"); n.className = "stat-hint";
      n.textContent = "硬件传感器不可用（基础系统监控继续）。";
      box.appendChild(n); return;
    }
    var groups = { fan: [], temp: [], power: [], other: [] };
    sensors.forEach(function (s) {
      var c = _sensorCat(s);
      if (c === "load" || c === "control") return;   // load 归主区；control 并入 fan 行
      (groups[c] || groups.other).push(s);
    });
    // 风扇（§143-§148：RPM 真值；control % 只有 Provider 真提供才显示，绝不从 RPM 推算）
    var fans = (raw && raw.fans) || [];
    if (fans.length) {
      groups.fan = fans.map(function (f) {
        var parts = [];
        if (f.rpm != null) parts.push(F.formatInt(f.rpm) + " RPM");
        if (f.control_percent != null) parts.push(F.formatPercent(f.control_percent, 0) + " 控制");
        return { name: f.name, text: parts.length ? parts.join(" · ") : "--" };
      });
    }
    _sensorGroup(box, "温度", groups.temp.map(function (s) { return { name: _sensorName(s), text: _fmtSensor(s) }; }));
    _sensorGroup(box, "风扇转速", groups.fan.map(function (x) { return { name: x.name, text: x.text }; }));
    _sensorGroup(box, "功耗", groups.power.map(function (s) { return { name: _sensorName(s), text: _fmtSensor(s) }; }));
    var others = groups.other.map(function (s) { return { name: _sensorName(s), text: _fmtSensor(s) }; });
    if (others.length) _sensorGroup(box, "其它", others);
    if (!groups.temp.length && !groups.fan.length && !groups.power.length && !groups.other.length) {
      var e = document.createElement("div"); e.className = "stat-hint";
      e.textContent = "无结构化传感器数据";
      box.appendChild(e);
    }
    // 查看全部传感器（折叠；§152-§154 结构化 Table，不用逗号 raw 串）
    var listBox = $("sysSensorList");
    if (listBox) {
      listBox.innerHTML = "";
      if (!sensors.length) {
        var nn = document.createElement("div"); nn.className = "stat-hint";
        nn.textContent = state === "unavailable" ? "未检测到硬件传感器。" : "暂无传感器数据。";
        listBox.appendChild(nn);
      } else {
        _sensorTable(listBox, sensors);
      }
    }
  }
  function _sensorName(s) {
    // 名称：优先 sensor_name（去重后取首个）；避免长逗号 raw 串（§154）
    var names = s.names || [];
    if (names.length === 1) return names[0];
    if (names.length > 1) return (s.sensor_type || "传感器") + " ×" + names.length;
    return s.sensor_type || "传感器";
  }
  function _sensorGroup(box, title, items) {
    if (!items.length) return;
    var g = document.createElement("div"); g.className = "sensor-group";
    var h = document.createElement("div"); h.className = "sensor-group-title";
    h.textContent = title + " " + items.length;
    g.appendChild(h);
    var list = document.createElement("div"); list.className = "sensor-group-list";
    items.forEach(function (it) {
      var row = document.createElement("div"); row.className = "sensor-row";
      var nm = document.createElement("span"); nm.className = "sensor-name"; nm.textContent = it.name;
      var val = document.createElement("span"); val.className = "sensor-val"; val.textContent = it.text;
      row.appendChild(nm); row.appendChild(val);
      list.appendChild(row);
    });
    g.appendChild(list);
    box.appendChild(g);
  }
  function _sensorTable(listBox, sensors) {
    // 结构化 Table（§153）：硬件 / 传感器 / 类型 / 当前值
    var head = '<table class="sensor-table"><thead><tr><th>硬件</th><th>传感器</th><th>类型</th><th class="num">当前值</th></tr></thead><tbody>';
    var body = '';
    sensors.forEach(function (s) {
      body += '<tr><td>' + _esc(s.hardware_type || "--") + '</td>' +
        '<td>' + _esc(_sensorName(s)) + '</td>' +
        '<td>' + _esc(s.sensor_type || "--") + '</td>' +
        '<td class="num">' + (s.latest == null ? "--" : _fmtSensor(s)) + '</td></tr>';
    });
    listBox.innerHTML = head + body + '</tbody></table>';
  }
  function _renderSettingsSensorList(monList, sensors, counts, state) {
    monList.innerHTML = "";
    // Round-7 §117-§119：不罗列「CPU 49个 主板2个」统计——汇总为一行「已检测 N 个传感器」，
    // 分类明细放 tooltip；明细行折叠在 <details>（sms-detail 默认 hidden 由 JS 控制）。
    var total = 0, rows = [];
    ["cpu", "motherboard", "cooling", "storage"].forEach(function (k) {
      var label = { cpu: "CPU", motherboard: "主板", cooling: "散热", storage: "存储" }[k];
      if (counts[k] != null && counts[k] > 0) { total += counts[k]; rows.push(label + " " + counts[k]); }
    });
    if (total > 0) {
      var sum = document.createElement("div"); sum.className = "stat-hint ok";
      sum.textContent = "已检测 " + total + " 个传感器";
      sum.title = rows.join(" · ");
      monList.appendChild(sum);
      monList.hidden = false;
    } else {
      monList.hidden = true;
    }
    sensors.forEach(function (s) {
      var row = document.createElement("div"); row.className = "sensor-row";
      var nm = document.createElement("span"); nm.className = "sensor-name"; nm.textContent = (s.hardware_type || "硬件") + " · " + (s.sensor_type || "传感器");
      var val = document.createElement("span"); val.className = "sensor-val"; val.textContent = s.latest == null ? "--" : _fmtSensor(s);
      row.appendChild(nm); row.appendChild(val); monList.appendChild(row);
    });
    if (!sensors.length && state !== "unavailable") { var e2 = document.createElement("div"); e2.className = "stat-hint";
      e2.textContent = "未检测到传感器。"; monList.appendChild(e2); }
  }

  /* ================= 硬件信息 + 卷容量 + 运行时长 ================= */
  function renderDiskCapacity(inventory) {
    var box = $("sysDiskList");
    if (!box) return;
    box.innerHTML = "";
    var disks = (inventory && inventory.disk_list) || [];
    if (!disks.length) {
      var empty = document.createElement("div"); empty.className = "stat-hint";
      empty.textContent = "未检测到磁盘卷。"; box.appendChild(empty); return;
    }
    disks.forEach(function (d) {
      var total = d.total_bytes, used = d.used_bytes, free = d.free_bytes;
      var pct = (total && total > 0) ? (used / total * 100) : null;
      var freePct = (total && total > 0) ? (free / total * 100) : null;
      var row = document.createElement("div");
      row.className = "disk-cap";
      var head = document.createElement("div"); head.className = "disk-cap-head";
      var label = document.createElement("span"); label.className = "disk-cap-label";
      label.textContent = (d.mountpoint || d.device || "磁盘") + (d.fstype ? " · " + d.fstype : "");
      var val = document.createElement("span"); val.className = "disk-cap-val";
      // §89/§90：容量 >1024GiB 允许 TiB（formatMemory 已自动 GiB->TiB）
      val.textContent = total != null
        ? "已用 " + F.formatMemory(used) + " / " + F.formatMemory(total) + (pct != null ? " · " + F.formatPercent(pct, 0) : "")
        : "--";
      head.appendChild(label); head.appendChild(val); row.appendChild(head);
      if (pct != null) {
        var bar = document.createElement("div"); bar.className = "vram-bar";
        var fill = document.createElement("div");
        // §91：free<15% amber、<5% red（用 fill class）
        fill.className = "fill" + (freePct != null ? (freePct < 5 ? " crit" : (freePct < 15 ? " warn" : "")) : "");
        fill.style.width = Math.max(0, Math.min(100, pct)) + "%";
        bar.appendChild(fill); row.appendChild(bar);
      }
      if (free != null) {
        var hint = document.createElement("div"); hint.className = "stat-hint";
        hint.textContent = "可用 " + F.formatMemory(free);
        row.appendChild(hint);
      }
      box.appendChild(row);
    });
  }
  function renderHardwareInfo(inventory) {
    if (!inventory) return;
    // §157/§159：操作系统 = "Windows 11"（os_display），Build 次值（os_build）
    setText("hwOs", inventory.os_display || inventory.os || F.NA);
    setSub("hwOsBuild", inventory.os_build != null ? "Build " + inventory.os_build : "");
    // §158：架构 x64
    setText("hwArch", inventory.architecture || F.NA);
    setText("hwHost", inventory.computer_name || F.NA);
    // §160：CPU 型号清理（视觉），基准频率次值
    setText("hwCpu", inventory.cpu_model || inventory.cpu_model_raw || F.NA);
    setSub("hwCpuFreq", inventory.cpu_base_frequency_mhz != null ? freqText(inventory.cpu_base_frequency_mhz) : "");
    // §161：CPU 插槽（多路才显示，可靠值）
    if (inventory.cpu_sockets != null && inventory.cpu_sockets > 1) setText("hwSockets", String(inventory.cpu_sockets));
    else setText("hwSockets", inventory.cpu_sockets != null ? "1" : F.NA);
    var pc = inventory.physical_cores, lc = inventory.logical_cpus;
    setText("hwCores", (pc == null && lc == null) ? F.NA : (pc != null ? pc : "--") + " / " + (lc != null ? lc : "--"));
    setText("hwRam", inventory.installed_ram_bytes == null ? F.NA : F.formatMemory(inventory.installed_ram_bytes));
    var mb = (inventory.motherboard_manufacturer || "") + (inventory.motherboard_manufacturer && inventory.motherboard_model ? " " : "") + (inventory.motherboard_model || "");
    setText("hwMotherboard", mb || F.NA);
    setText("hwBios", inventory.bios_version || F.NA);
  }
  function refreshInventory(manual) {
    var url = "/api/system/inventory" + (manual ? "?manual=true" : "");
    return api.get(url)
      .then(function (d) {
        if (d && d.inventory) {
          lastInventory = d.inventory;
          renderHardwareInfo(lastInventory);
          renderDiskCapacity(lastInventory);
          _uptime();
        }
      })
      .catch(function (e) { console.warn("system inventory failed:", e.message || e); });
  }

  /* ================= 逻辑处理器利用率 Heat Grid（§56-§62） =================
     数据源 = /api/system/status cpu.per_core_percent（真逐逻辑核）。
     固定 CPU Index 顺序（§60，不按利用率重排）；Cell 显示 "N"+%；
     颜色 neutral->accent->warning，100% 不直接 error red（§62/§251）。
     桌面默认展开 / 移动默认折叠（data-auto-fold）。 */
  function renderCoreHeat() {
    var body = $("sysCoreHeat");
    var details = body ? body.closest("details.core-heat") : null;
    if (!body || !details) return;
    var cpu = lastCpu;
    var perCore = (cpu && cpu.per_core_percent) || null;
    var hasPerCore = Array.isArray(perCore) && perCore.length > 1;
    var logicalN = (lastInventory && lastInventory.logical_cpus) || (perCore ? perCore.length : 0);
    // 副标题（§58）："N 个逻辑处理器"
    var sub = $("coreHeatSub");
    if (sub) sub.textContent = logicalN ? " · " + logicalN + " 个逻辑处理器" : "";
    details.hidden = false;
    body.innerHTML = "";
    if (!hasPerCore) {
      var agg = document.createElement("div"); agg.className = "stat-hint";
      var aggTxt = (cpu && cpu.usage_percent != null) ? F.formatPercent(cpu.usage_percent, 0) : F.NA;
      agg.textContent = "逻辑处理器 " + (logicalN || "--") + " 个 · 当前整机 " + aggTxt +
        (hasPerCore ? "" : "。逐核数据采集中（首个有效采样后显示网格）。");
      body.appendChild(agg);
      return;
    }
    var grid = document.createElement("div"); grid.className = "core-heat-grid";
    for (var i = 0; i < perCore.length; i++) {
      var v = perCore[i] == null ? null : Number(perCore[i]);
      var cell = document.createElement("div");
      cell.className = "core-cell" + _heatClass(v);
      cell.title = "逻辑处理器 " + i + " · " + (v == null ? "无数据" : F.formatPercent(v, 0));
      var val = document.createElement("span"); val.className = "core-val";
      val.textContent = i;
      var pv = document.createElement("span"); pv.className = "core-pct";
      pv.textContent = v == null ? "–" : String(Math.round(v));
      cell.appendChild(val); cell.appendChild(pv);
      grid.appendChild(cell);
    }
    body.appendChild(grid);
  }
  function _heatClass(v) {
    if (v == null) return " na";
    if (v >= 95) return " hot";        // 高负载（warning 色，非 error red）
    if (v >= 60) return " high";       // 中等偏高（accent）
    if (v >= 25) return " med";
    return "";
  }

  /* ================= llama.cpp 模型信息（性能页「模型与运行环境」 + 概览行） ================= */
  function refreshLlamaInfo() {
    return api.get("/api/llama/info")
      .then(function (d) {
        var m = d && d.model;
        lastModel = m;
        if (!m) {
          ["llmAlias", "llmFtype", "llmParams", "llmSize", "llmContext", "llmSlots", "llmModal", "llmBuild"].forEach(function (id) {
            setText(id, F.NA);
          });
          setOverviewModelLine(null);
          return;
        }
        setText("llmAlias", m.model_alias || m.model_file_name || F.NA);
        setText("llmFtype", m.model_ftype ? String(m.model_ftype).replace(/ - /g, " · ") : F.NA);
        setText("llmParams", m.parameter_count == null ? F.NA : (m.parameter_count / 1e9).toFixed(2) + "B");
        setText("llmSize", m.model_size_bytes == null ? F.NA : F.formatMemory(m.model_size_bytes));
        setText("llmContext", m.context_size == null ? F.NA : F.formatTokenCount(m.context_size));
        setText("llmSlots", m.total_slots == null ? F.NA : String(m.total_slots));
        var modal = [];
        if (m.vision_supported) modal.push("视觉");
        if (m.video_supported) modal.push("视频");
        if (m.audio_supported) modal.push("音频");
        setText("llmModal", modal.length ? modal.join(" · ") : F.NA);
        var buildEl = $("llmBuild");
        if (buildEl) { buildEl.textContent = m.build_info || F.NA; buildEl.title = m.build_info || ""; }
        setOverviewModelLine(m);
      })
      .catch(function (e) { console.warn("llama info failed:", e.message || e); });
  }
  function setOverviewModelLine(m) {
    var el = $("ovModelLine");
    if (!el) return;
    if (!m) { el.textContent = ""; el.style.display = "none"; return; }
    var parts = [];
    if (m.model_alias) parts.push(m.model_alias);
    if (m.model_ftype) parts.push(m.model_ftype);
    if (m.parameter_count != null) parts.push((m.parameter_count / 1e9).toFixed(2) + "B");
    el.textContent = parts.join(" · ");
    el.style.display = parts.length ? "" : "none";
  }

  /* ================= llama.cpp Slot 监控（性能页；只读） ================= */
  function _esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function _slotContextCell(s) {
    if (!s.is_processing) return '<td class="slot-ctx na" data-label="上下文">--</td>';
    var seq = 0;
    if (s.n_prompt_tokens != null) seq += s.n_prompt_tokens;
    if (s.next_token && s.next_token.n_decoded != null) seq += s.next_token.n_decoded;
    var ctx = s.n_ctx;
    if (ctx == null) return '<td class="slot-ctx" data-label="上下文">' + F.formatTokenCount(seq) + '</td>';
    var pct = Math.min(100, seq / ctx * 100);
    var cls = "slot-ctx" + (pct >= 95 ? " crit" : (pct >= 85 ? " warn" : ""));
    return '<td class="' + cls + '" data-label="上下文">' +
      F.formatTokenCount(seq) + ' / ' + F.formatTokenCount(ctx) +
      '<span class="slot-ctx-pct"> · ' + F.formatPercent(pct) + '</span></td>';
  }
  function _slotDetailHTML(s) {
    var p = (s.params || {});
    function kv(label, val) {
      if (val === undefined || val === null || val === "") return "";
      return '<span class="sd-k">' + label + '</span><span class="sd-v">' + _esc(val) + '</span>';
    }
    var chips = [
      kv("Temperature", p.temperature != null ? Number(p.temperature).toFixed(2) : null),
      kv("Top P", p.top_p != null ? Number(p.top_p).toFixed(2) : null),
      kv("Top K", p.top_k),
      kv("Min P", p.min_p != null ? Number(p.min_p).toFixed(3) : null),
      kv("Max Tokens", p.max_tokens),
      kv("Reasoning", p.reasoning_format),
      kv("Chat", p.chat_format),
      kv("Speculative", p["speculative.types"]),
      kv("Stream", s.is_stream != null ? (s.is_stream ? "on" : "off") : null),
    ];
    if (Array.isArray(p.samplers) && p.samplers.length) {
      chips.push('<span class="sd-k">Samplers</span><span class="sd-v">' + _esc(p.samplers.join(", ")) + '</span>');
    }
    return '<div class="slot-detail-grid">' + chips.join("") + '</div>';
  }
  function renderSlotsOnly(d) {
    lastSlots = d;
    var box = $("llmSlotList");
    if (!box) return;
    var slots = (d && d.slots) || [];
    box.innerHTML = "";
    if (!d || !d.available || !slots.length) {
      var empty = document.createElement("div"); empty.className = "stat-hint";
      empty.textContent = (d && !d.available) ? "Slot 监控不可用（llama-server 未提供 /slots）。" : "暂无 Slot 数据";
      box.appendChild(empty); return;
    }
    var head = '<thead><tr>' +
      '<th>Slot</th><th>状态</th><th class="num">上下文</th>' +
      '<th class="num">Prompt 总量</th><th class="num">新处理 Prompt</th>' +
      '<th class="num">缓存复用 Prompt</th><th class="num">已生成 Token</th>' +
      '<th class="num">剩余生成预算</th><th>MTP</th><th class="num">详情</th></tr></thead>';
    var body = '<tbody>';
    slots.forEach(function (s, idx) {
      var active = !!s.is_processing;
      var mtpOn = (s.speculative || (s.params && s.params["speculative.types"] &&
        s.params["speculative.types"] !== "none" && s.params["speculative.types"] !== ""));
      function cell(label, val) {
        return '<td class="' + (val === F.NA ? "na" : "num") + '" data-label="' + label + '">' + val + '</td>';
      }
      var detailId = "slotDetail" + idx;
      var detailBtn = active
        ? '<button type="button" class="slot-detail-btn" data-target="' + detailId + '" aria-expanded="false">详情</button>'
        : '<span class="slot-detail-off" aria-hidden="true">—</span>';
      body += '<tr class="slot-row' + (active ? " active" : " idle") + '">' +
        '<td class="slot-id-cell" data-label="Slot">Slot ' + (s.id == null ? "?" : s.id) + '</td>' +
        '<td data-label="状态"><span class="slot-state' + (active ? " busy" : "") + '">' + (active ? "处理中" : "空闲") + '</span></td>' +
        _slotContextCell(s) +
        cell("Prompt 总量", active && s.n_prompt_tokens != null ? F.formatTokenCount(s.n_prompt_tokens) : F.NA) +
        cell("新处理 Prompt", active && s.n_prompt_tokens_processed != null ? F.formatTokenCount(s.n_prompt_tokens_processed) : F.NA) +
        cell("缓存复用 Prompt", active && s.n_prompt_tokens_cache != null ? F.formatTokenCount(s.n_prompt_tokens_cache) : F.NA) +
        cell("已生成 Token", active && s.next_token && s.next_token.n_decoded != null ? F.formatTokenCount(s.next_token.n_decoded) : F.NA) +
        cell("剩余生成预算", active && s.next_token && s.next_token.n_remain != null && s.next_token.n_remain >= 0 ? F.formatTokenCount(s.next_token.n_remain) : F.NA) +
        '<td data-label="MTP">' + (mtpOn ? "已启用" : "未启用") + '</td>' +
        '<td class="num" data-label="详情">' + detailBtn + '</td></tr>';
      if (active) {
        body += '<tr class="slot-detail-row" id="' + detailId + '" hidden><td colspan="10">' + _slotDetailHTML(s) + '</td></tr>';
      }
    });
    body += '</tbody>';
    var wrap = document.createElement("div");
    wrap.className = "slot-table-wrap";
    wrap.innerHTML = '<table class="slot-table">' + head + body + '</table>';
    box.appendChild(wrap);
    box.querySelectorAll(".slot-detail-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var row = document.getElementById(btn.getAttribute("data-target"));
        if (!row) return;
        var open = row.hidden;
        row.hidden = !open;
        btn.setAttribute("aria-expanded", open ? "true" : "false");
        btn.textContent = open ? "收起" : "详情";
      });
    });
  }
  function refreshLlamaSlots() {
    return api.get("/api/llama/slots")
      .then(function (d) { renderSlotsOnly(d); })
      .catch(function (e) { console.warn("llama slots failed:", e.message || e); });
  }

  /* ================= 初始化 ================= */
  var initialized = false;

  function init() {
    if (!initialized) {
      initialized = true;
      var rangeEl = $("systemRange");
      if (rangeEl) {
        ui.segmented(rangeEl, [
          { value: 15, label: "15 分钟" },
          { value: 60, label: "1 小时" },
          { value: 360, label: "6 小时" },
          { value: 1440, label: "24 小时" },
        ], systemRangeMinutes, function (v) {
          systemRangeMinutes = v;
          refreshLive();
        });
      }
      var btn = $("btnRefreshInventory");
      if (btn) {
        btn.addEventListener("click", function () {
          btn.disabled = true;
          refreshInventory(true).then(function () {
            ui.toast("硬件信息已刷新。", "ok");
            btn.disabled = false;
          }).catch(function () { btn.disabled = false; });
        });
      }
    }
    // 每次进页刷新（切回保证最新）。
    // refreshStatus 完成后重绘 Heat Grid（lastCpu 此时才有 per-core）+ 网络区
    // （_renderNetIface 依赖 lastNet）。
    refreshStatus().then(function () {
      renderCoreHeat();
      _renderNetIface();
    });
    refreshLive();
    refreshDaily();
    refreshSensors();
    refreshNetworkInterfaces().then(function () { _renderNetIface(); });
    refreshInventory(false);
  }

  window.LM = window.LM || {};
  LM.system = {
    init: init,
    refreshStatus: refreshStatus,
    refreshLive: refreshLive,
    refreshDaily: refreshDaily,
    refreshSensors: refreshSensors,
    sensorState: function () { return lastSensorState; },
    refreshNetworkInterfaces: refreshNetworkInterfaces,
    refreshInventory: refreshInventory,
    refreshLlamaInfo: refreshLlamaInfo,
    refreshLlamaSlots: refreshLlamaSlots,
    renderSlotsOnly: renderSlotsOnly,
    renderCoreHeat: renderCoreHeat,
    getLive: function () { return systemLive; },
    getLiveSummary: function () { return systemLiveSummary; },
  };
})();
