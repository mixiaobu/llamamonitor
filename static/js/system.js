/* ============================================================
   LlamaMonitor — System（1.1）
   系统页 + 概览主机状态 + 性能页模型/Slot 接线。
   职责：
   - refreshStatus()：/api/system/status -> 系统概览 + CPU/内存/功耗 + 概览主机状态摘要
   - refreshLive()：/api/system/live?minutes=N -> CPU/磁盘/网络 图表
   - refreshDaily()：/api/system/daily?days=1 -> 今日已监测组件能耗
   - refreshSensors()：/api/system/sensors -> 散热与传感器（风扇/传感器列表/Provider 状态）
   - refreshInventory()：/api/system/inventory -> 硬件信息 + 磁盘容量 + 运行时长
   - refreshLlamaInfo()/refreshLlamaSlots()：/api/llama/info + /api/llama/slots
     -> 性能页「模型与服务」 + 概览服务器模型行
   设计原则（与 app.js 一致）：
   - 每个 refresh 独立 catch（一个 API 失败不拖垮其他区）；
   - null = 不可用 -> F.NA（--），绝不把 null 变 0（风扇/功耗/温度）；
   - 只读监控：无控制按钮。
   ============================================================ */
(function () {
  "use strict";

  var F = LM.fmt, api = LM.api, ui = LM.ui, charts = LM.charts;
  var $ = function (id) { return document.getElementById(id); };

  var systemLive = null;          // /api/system/live points
  var systemRangeMinutes = 60;    // 系统页当前范围（分钟）
  var lastInventory = null;       // /api/system/inventory 缓存（运行时长/磁盘容量复用）
  var lastCpu = null;             // /api/system/status cpu 子对象（每核心聚合视图用）
  var lastModel = null;           // /api/llama/info model
  var lastSlots = null;           // /api/llama/slots

  /* ---------- 小工具 ---------- */

  function setText(id, text) {
    var el = $(id);
    if (!el) return;
    el.textContent = text;
    el.classList.toggle("dim", text === F.NA);
  }

  function setHint(id, text) {
    var el = $(id);
    if (!el) return;
    el.textContent = text;
    el.style.display = text ? "" : "none";
  }

  function freqText(mhz) {
    if (mhz == null) return F.NA;
    var v = Number(mhz);
    return v >= 1000 ? (v / 1000).toFixed(2) + " GHz" : v.toFixed(0) + " MHz";
  }

  function rateText(bps) {
    if (bps == null) return F.NA;
    return F.formatBytes(bps) + "/s";
  }

  function uptimeSecs(bootEpoch) {
    if (!bootEpoch) return null;
    var now = Date.now() / 1000;
    var d = now - Number(bootEpoch);
    return d > 0 ? d : null;
  }

  /* 每核心负载视图：数据源只有聚合值（无逐核拆分），
     因此仅当 logical_cpus > 16（多核大 CPU，聚合值参考意义有限）时显示，
     并明确标注"聚合视图"，不伪造逐核数字。 */
  function renderCoreHeat(logicalCpus) {
    var body = $("sysCoreHeat");
    var details = body ? body.closest("details.core-heat") : null;
    if (!body || !details) return;
    if (!logicalCpus || logicalCpus <= 16) {
      details.hidden = true;
      return;
    }
    details.hidden = false;
    body.innerHTML = "";
    var note = document.createElement("div");
    note.className = "stat-hint";
    note.textContent = "逻辑 CPU " + logicalCpus + " 核：数据源仅含整机聚合值，此处为聚合视图（非逐核拆分）。";
    body.appendChild(note);
    var row = document.createElement("div");
    row.className = "fan-row";
    var k = document.createElement("span");
    k.className = "sensor-name";
    k.textContent = "聚合（全部核心）";
    var v = document.createElement("span");
    v.className = "sensor-val";
    // 聚合值取自最近一次 /api/system/status（refreshStatus 中写入 lastCpu）
    var cpu = lastCpu;
    var parts = [];
    if (cpu) {
      if (cpu.usage_percent != null) parts.push("利用率 " + F.formatPercent(cpu.usage_percent, 0));
      if (cpu.temperature_c != null) parts.push("温度 " + F.formatTemp(cpu.temperature_c));
      if (cpu.package_power_w != null) parts.push("功耗 " + F.formatPower(cpu.package_power_w));
    }
    v.textContent = parts.length ? parts.join(" · ") : "--";
    row.appendChild(k);
    row.appendChild(v);
    body.appendChild(row);
  }

  /* ================= 系统概览 + CPU/内存/功耗 + 概览主机状态 ================= */
  function refreshStatus() {
    return api.get("/api/system/status")
      .then(function (d) {
        var cpu = d.cpu || {}, mem = d.memory || {}, disk = d.disk || {},
            net = d.network || {}, pw = d.power || {};
        lastCpu = cpu; // 每核心聚合视图（renderCoreHeat）复用

        /* --- 系统页：系统概览 --- */
        setText("sysCpuUsage", cpu.usage_percent == null ? F.NA : F.formatPercent(cpu.usage_percent, 0));
        setText("sysCpuFreq", freqText(cpu.frequency_mhz));
        if (mem.usage_percent != null) {
          var used = mem.used_bytes != null ? F.formatMemory(mem.used_bytes) : F.NA;
          var total = mem.total_bytes != null ? F.formatMemory(mem.total_bytes) : F.NA;
          setText("sysMemUsage", F.formatPercent(mem.usage_percent, 0));
          setHint("sysMemDetail", used + " / " + total);
        } else {
          setText("sysMemUsage", F.NA);
          setHint("sysMemDetail", "");
        }
        setText("sysCompPower", pw.monitored_components_w == null ? F.NA : F.formatPower(pw.monitored_components_w));
        var up = uptimeSecs(lastInventory && lastInventory.boot_time);
        setText("sysUptime", up == null ? F.NA : F.formatDuration(up));

        /* --- CPU 负载：次级 stat 行 --- */
        setText("sysCpuFreq2", freqText(cpu.frequency_mhz));
        setText("sysCpuTemp", cpu.temperature_c == null ? F.NA : F.formatTemp(cpu.temperature_c));
        setText("sysCpuPower", cpu.package_power_w == null ? F.NA : F.formatPower(cpu.package_power_w));

        /* --- 内存 --- */
        setText("sysMemUsed", mem.used_bytes != null ? F.formatMemory(mem.used_bytes) : F.NA);
        setHint("sysMemTotal", mem.total_bytes != null ? "总量 " + F.formatMemory(mem.total_bytes) : "");
        setText("sysMemPct", mem.usage_percent == null ? F.NA : F.formatPercent(mem.usage_percent, 0));

        /* --- 功耗与能耗 --- */
        setText("sysPowerComp", pw.monitored_components_w == null ? F.NA : F.formatPower(pw.monitored_components_w));
        setText("sysPowerCpu", pw.cpu_package_w == null ? F.NA : F.formatPower(pw.cpu_package_w));
        setText("sysPowerGpu", pw.gpu_total_w == null ? F.NA : F.formatPower(pw.gpu_total_w));
        setText("sysWallPower", pw.wall_power_w == null ? F.NA : F.formatPower(pw.wall_power_w));

        /* --- 概览：主机状态摘要卡 --- */
        if (cpu.usage_percent != null) {
          var cpuVal = F.formatPercent(cpu.usage_percent, 0);
          if (cpu.temperature_c != null) cpuVal += " · " + F.formatTemp(cpu.temperature_c);
          setText("ovHostCpu", cpuVal);
        } else {
          setText("ovHostCpu", F.NA);
        }
        if (mem.usage_percent != null && mem.used_bytes != null && mem.total_bytes != null) {
          setText("ovHostMem",
            (mem.used_bytes / (1024 * 1024 * 1024)).toFixed(1) + " / " +
            (mem.total_bytes / (1024 * 1024 * 1024)).toFixed(1) + " GiB · " + F.formatPercent(mem.usage_percent, 0));
        } else {
          setText("ovHostMem", F.NA);
        }
        if (disk.read_bps != null && disk.write_bps != null) {
          setText("ovHostDisk", rateText(disk.read_bps) + " · " + rateText(disk.write_bps));
        } else {
          setText("ovHostDisk", F.NA);
        }
        if (net.rx_bps != null && net.tx_bps != null) {
          setText("ovHostNet", rateText(net.rx_bps) + " · " + rateText(net.tx_bps));
        } else {
          setText("ovHostNet", F.NA);
        }
        setText("ovHostPower", pw.monitored_components_w == null ? F.NA : F.formatPower(pw.monitored_components_w));

        /* --- 系统页状态徽章 --- */
        ui.setStatusBadge($("systemPageState"),
          d.available ? "online" : "offline",
          d.available ? "正常" : "不可用");
        /* 每核心聚合视图（依赖 status 聚合值 + inventory 逻辑核数） */
        renderCoreHeat(lastInventory && lastInventory.logical_cpus);
      })
      .catch(function (e) {
        console.warn("system status failed:", e.message || e);
        ui.setStatusBadge($("systemPageState"), "offline", "不可用");
      });
  }

  /* ================= CPU/磁盘/网络 图表（live） ================= */
  function refreshLive() {
    return api.get("/api/system/live?minutes=" + systemRangeMinutes)
      .then(function (d) {
        systemLive = (d && d.points) || [];
        charts.renderSysCpuChart("chartSysCpuBox", "chartSysCpu", systemLive);
        charts.renderSysDiskChart("chartSysDiskBox", "chartSysDisk", systemLive);
        charts.renderSysNetChart("chartSysNetBox", "chartSysNet", systemLive);
      })
      .catch(function (e) { console.warn("system live failed:", e.message || e); });
  }

  /* ================= 今日已监测组件能耗（daily） ================= */
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

  /* ================= 散热与传感器 ================= */
  function sensorGroupLabel(hardwareType) {
    var t = String(hardwareType || "").toLowerCase();
    if (t === "cpu") return "CPU";
    if (t === "motherboard") return "主板";
    if (t === "storage") return "存储";
    return "散热与其他";
  }

  function refreshSensors() {
    return api.get("/api/system/sensors")
      .then(function (d) {
        var state = d.state || (d.available ? "available" : "unavailable");
        var sensors = d.sensors || [];   // 系统页 + Settings 页共用
        var counts = d.counts || {};
        /* Provider 状态徽章 */
        var badge = $("sensorState");
        if (badge) {
          if (state === "available") ui.setStatusBadge(badge, "online", "高级传感器可用");
          else if (state === "partial") ui.setStatusBadge(badge, "warning", "部分可用");
          else ui.setStatusBadge(badge, "offline", "不可用");
        }
        /* Settings 页 Provider 状态（若存在） */
        var mon = $("sysMonState");
        if (mon) {
          var monText = mon.querySelector(".cs-text");
          var monSub = $("sysMonStateSub");
          mon.classList.remove("online", "offline");
          if (state === "available") { mon.classList.add("online"); if (monText) monText.textContent = "高级硬件传感器：可用"; }
          else if (state === "partial") { if (monText) monText.textContent = "高级硬件传感器：部分可用"; }
          else { mon.classList.add("offline"); if (monText) monText.textContent = "高级硬件传感器：不可用"; }
          if (monSub) monSub.textContent = state === "unavailable" ? "基础系统监控（CPU/内存/存储/网络）继续正常。" : "";
        }

        /* 风扇列表 */
        var fansBox = $("sysFans");
        if (fansBox) {
          fansBox.innerHTML = "";
          var fans = d.fans || [];
          if (!fans.length && state === "unavailable") {
            var note = document.createElement("div");
            note.className = "stat-hint";
            note.textContent = "高级传感器不可用（基础系统监控继续）。";
            fansBox.appendChild(note);
          } else if (fans.length) {
            fans.forEach(function (f) {
              var row = document.createElement("div");
              row.className = "fan-row";
              var nm = document.createElement("span");
              nm.className = "fan-name";
              nm.textContent = f.name || "风扇";
              var val = document.createElement("span");
              val.className = "fan-val";
              var parts = [];
              if (f.rpm != null) parts.push(F.formatInt(f.rpm) + " RPM");
              if (f.control_percent != null) parts.push(F.formatPercent(f.control_percent, 0));
              val.textContent = parts.length ? parts.join(" · ") : "--";
              if (f.source) row.title = f.source;
              row.appendChild(nm);
              row.appendChild(val);
              fansBox.appendChild(row);
            });
          }
        }

        /* 传感器列表（按分类分组；只显示真实存在的传感器） */
        var listBox = $("sysSensorList");
        if (listBox) {
          listBox.innerHTML = "";
          if (!sensors.length) {
            var none = document.createElement("div");
            none.className = "stat-hint";
            none.textContent = state === "unavailable" ? "未检测到高级传感器。" : "暂无传感器数据。";
            listBox.appendChild(none);
          } else {
            sensors.forEach(function (s) {
              var row = document.createElement("div");
              row.className = "sensor-row";
              var nm = document.createElement("span");
              nm.className = "sensor-name";
              var names = (s.names || []).join("、");
              nm.textContent = sensorGroupLabel(s.hardware_type) + " · " + (s.sensor_type || "传感器") +
                (names ? "（" + names + "）" : "");
              var val = document.createElement("span");
              val.className = "sensor-val";
              val.textContent = s.latest == null ? "--" : String(s.latest);
              if (s.count && s.count > 1) val.title = "聚合 " + s.count + " 个传感器（均值）";
              row.appendChild(nm);
              row.appendChild(val);
              listBox.appendChild(row);
            });
          }
        }

        /* Settings 页只读传感器列表（分类计数 + 详情） */
        var monList = $("sysMonSensorList");
        if (monList) {
          monList.innerHTML = "";
          var rows = [];
          ["cpu", "motherboard", "cooling", "storage"].forEach(function (k) {
            var label = { cpu: "CPU", motherboard: "主板", cooling: "散热", storage: "存储" }[k];
            if (counts[k] != null && counts[k] > 0) rows.push(label + " " + counts[k] + " 个");
          });
          if (rows.length) {
            var sum = document.createElement("div");
            sum.className = "stat-hint ok";
            sum.textContent = "检测到：" + rows.join(" · ");
            monList.appendChild(sum);
          }
          sensors.forEach(function (s) {
            var row = document.createElement("div");
            row.className = "sensor-row";
            var nm = document.createElement("span");
            nm.className = "sensor-name";
            nm.textContent = sensorGroupLabel(s.hardware_type) + " · " + (s.sensor_type || "传感器");
            var val = document.createElement("span");
            val.className = "sensor-val";
            val.textContent = s.latest == null ? "--" : String(s.latest);
            row.appendChild(nm);
            row.appendChild(val);
            monList.appendChild(row);
          });
          if (!sensors.length) {
            var empty2 = document.createElement("div");
            empty2.className = "stat-hint";
            empty2.textContent = state === "unavailable" ? "高级传感器不可用。" : "未检测到传感器。";
            monList.appendChild(empty2);
          }
        }
      })
      .catch(function (e) { console.warn("system sensors failed:", e.message || e); });
  }

  /* ================= 硬件信息 + 磁盘容量 + 运行时长 ================= */
  function renderDiskCapacity(inventory) {
    var box = $("sysDiskList");
    if (!box) return;
    box.innerHTML = "";
    var disks = (inventory && inventory.disk_list) || [];
    if (!disks.length) {
      var empty = document.createElement("div");
      empty.className = "stat-hint";
      empty.textContent = "未检测到磁盘卷。";
      box.appendChild(empty);
      return;
    }
    disks.forEach(function (d) {
      var total = d.total_bytes, used = d.used_bytes, free = d.free_bytes;
      var pct = (total && total > 0) ? (used / total * 100) : null;
      var row = document.createElement("div");
      row.className = "disk-cap";
      var head = document.createElement("div");
      head.className = "disk-cap-head";
      var label = document.createElement("span");
      label.className = "disk-cap-label";
      label.textContent = (d.mountpoint || d.device || "磁盘") +
        (d.fstype ? " · " + d.fstype : "");
      var val = document.createElement("span");
      val.className = "disk-cap-val";
      val.textContent = total != null
        ? F.formatMemory(used) + " / " + F.formatMemory(total) + (pct != null ? " · " + F.formatPercent(pct, 0) : "")
        : "--";
      head.appendChild(label);
      head.appendChild(val);
      row.appendChild(head);
      if (pct != null) {
        var bar = document.createElement("div");
        bar.className = "vram-bar";
        var fill = document.createElement("div");
        fill.className = "fill";
        fill.style.width = Math.max(0, Math.min(100, pct)) + "%";
        bar.appendChild(fill);
        row.appendChild(bar);
      }
      if (free != null) {
        var hint = document.createElement("div");
        hint.className = "stat-hint";
        hint.textContent = "可用 " + F.formatMemory(free);
        row.appendChild(hint);
      }
      box.appendChild(row);
    });
  }

  function renderHardwareInfo(inventory) {
    if (!inventory) return;
    setText("hwOs", inventory.os || F.NA);
    setText("hwHost", inventory.computer_name || F.NA);
    setText("hwCpu", inventory.cpu_model || F.NA);
    var pc = inventory.physical_cores, lc = inventory.logical_cpus;
    setText("hwCores",
      (pc == null && lc == null) ? F.NA :
      (pc != null ? pc : "--") + " / " + (lc != null ? lc : "--"));
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
          /* 运行时长依赖 boot_time，重算一次 */
          var up = uptimeSecs(lastInventory.boot_time);
          setText("sysUptime", up == null ? F.NA : F.formatDuration(up));
          /* 逻辑核数到位后重算每核心聚合视图 */
          renderCoreHeat(lastInventory.logical_cpus);
        }
      })
      .catch(function (e) { console.warn("system inventory failed:", e.message || e); });
  }

  /* ================= llama.cpp 模型信息（性能页「模型与服务」 + 概览行） ================= */
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
        setText("llmFtype", m.model_ftype || F.NA);
        setText("llmParams", m.parameter_count == null ? F.NA : (m.parameter_count / 1e9).toFixed(2) + "B");
        setText("llmSize", m.model_size_bytes == null ? F.NA : F.formatMemory(m.model_size_bytes));
        setText("llmContext", m.context_size == null ? F.NA : F.formatNumber(m.context_size));
        setText("llmSlots", m.total_slots == null ? F.NA : String(m.total_slots));
        var modal = [];
        if (m.vision_supported) modal.push("视觉");
        if (m.video_supported) modal.push("视频");
        if (m.audio_supported) modal.push("音频");
        setText("llmModal", modal.length ? modal.join(" · ") : F.NA);
        setText("llmBuild", m.build_info || F.NA);
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

  /* ================= llama.cpp Slot 列表（性能页「当前 Slot」；只读） ================= */
  function renderSlotCard(s) {
    var card = document.createElement("div");
    card.className = "slot-card";
    var head = document.createElement("div");
    head.className = "slot-head";
    var idSpan = document.createElement("span");
    idSpan.className = "slot-id";
    idSpan.textContent = "Slot " + (s.id == null ? "?" : s.id);
    var st = document.createElement("span");
    st.className = "slot-state" + (s.is_processing ? " busy" : "");
    st.textContent = s.is_processing ? "处理中" : "空闲";
    head.appendChild(idSpan);
    head.appendChild(st);
    card.appendChild(head);

    var rows = [
      ["上下文容量", s.n_ctx == null ? F.NA : F.formatTokenCount(s.n_ctx)],
      ["Prompt Token", s.n_prompt_tokens == null ? F.NA : F.formatTokenCount(s.n_prompt_tokens)],
      ["缓存复用", s.n_prompt_tokens_cache == null ? F.NA : F.formatTokenCount(s.n_prompt_tokens_cache)],
      ["本次实际处理", s.n_prompt_tokens_processed == null ? F.NA : F.formatTokenCount(s.n_prompt_tokens_processed)],
      ["已生成", s.next_token && s.next_token.n_decoded != null ? F.formatTokenCount(s.next_token.n_decoded) : F.NA],
      ["剩余生成上限", s.next_token && s.next_token.n_remain != null && s.next_token.n_remain >= 0 ? F.formatTokenCount(s.next_token.n_remain) : F.NA],
      ["当前请求缓存复用率", s.cache_reuse_percent == null ? F.NA : F.formatPercent(s.cache_reuse_percent)],
      ["MTP", s.speculative ? "已启用" : "未启用"],
    ];
    var kv = document.createElement("div");
    kv.className = "gpu-kv";
    rows.forEach(function (r) {
      var k = document.createElement("span");
      k.className = "k";
      k.textContent = r[0];
      var v = document.createElement("span");
      v.className = "v" + (r[1] === F.NA ? " dim" : "");
      v.textContent = r[1];
      kv.appendChild(k);
      kv.appendChild(v);
    });
    card.appendChild(kv);
    return card;
  }

  function refreshLlamaSlots() {
    return api.get("/api/llama/slots")
      .then(function (d) {
        lastSlots = d;
        var box = $("llmSlotList");
        if (!box) return;
        var slots = (d && d.slots) || [];
        box.innerHTML = "";
        if (!d || !d.available || !slots.length) {
          var empty = document.createElement("div");
          empty.className = "stat-hint";
          empty.textContent = (d && !d.available) ? "Slot 监控不可用（llama-server 未提供 /slots）。" : "暂无 Slot 数据";
          box.appendChild(empty);
          return;
        }
        slots.forEach(function (s) { box.appendChild(renderSlotCard(s)); });
      })
      .catch(function (e) { console.warn("llama slots failed:", e.message || e); });
  }

  /* ================= 初始化 ================= */
  var initialized = false;

  function init() {
    /* 一次性设置（segmented / 按钮监听）；每次进页都会刷新数据 */
    if (!initialized) {
      initialized = true;
      /* 系统页范围 segmented（与 GPU 页一致的 15m/1h/6h/24h） */
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
      /* 手动刷新硬件信息（低频 CIM，仅点击时） */
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
    /* 每次进页刷新当前数据（切回页面时保证最新） */
    refreshStatus();
    refreshLive();
    refreshDaily();
    refreshSensors();
    refreshInventory(false);
  }

  window.LM = window.LM || {};
  LM.system = {
    init: init,
    refreshStatus: refreshStatus,
    refreshLive: refreshLive,
    refreshDaily: refreshDaily,
    refreshSensors: refreshSensors,
    refreshInventory: refreshInventory,
    refreshLlamaInfo: refreshLlamaInfo,
    refreshLlamaSlots: refreshLlamaSlots,
    /* 图表重绘（主题 retheme）时读取当前 live 数据 */
    getLive: function () { return systemLive; },
  };
})();
