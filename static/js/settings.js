/* ============================================================
   LlamaMonitor — Settings（Phase 15, spec §23-§25/§90/§91/§92）
   Settings 页逻辑：
   - 表单加载一次（不轮询，避免覆盖未保存编辑——沿用 Phase 语义）；
   - dirty 跟踪 + Save（PUT /api/config 后端校验 + 原子写）+
     Reset to Defaults（只重置表单，Save 才写盘）+ 切页未保存确认；
   - Test Connection（后端只 GET，避免前端 CORS）；
   - Data Management（info/backup/clear-live/reset/CSV/db check）；
   - Application（integration/autostart/exit）；
   - Updates（状态轮询走中央调度器 UI-023；check/download/install/cancel）；
   - About（版本/schema/数据目录，唯一来源 /api/version）。
   危险操作：confirmModal 统一确认（spec §91）；连续点击靠按钮 disabled
   防重入（沿用 Phase 语义）。
   ============================================================ */
(function () {
  "use strict";

  var F = LM.fmt, api = LM.api, ui = LM.ui;
  var $ = function (id) { return document.getElementById(id); };

  var settingsLoaded = false;
  var settingsDirty = false;
  var initialConfig = null;
  var lastPaths = null;
  var gpuPickSignature = null;   // UI-002：detected 列表签名，未变不重建
  var updateStatus = null;
  var lastTestConn = null;       // Phase 16C §21：最近一次"测试连接"结果

  /* ================= 表单 ================= */

  function num(id) {
    var v = $(id).value.trim();
    return v === "" ? 0 : Number(v);
  }

  function readGpuUuids() {
    var box = $("gpuDetected");
    if (!box) return [];
    var out = [];
    box.querySelectorAll("input[type=checkbox]:checked").forEach(function (cb) {
      out.push(cb.value);
    });
    return out;
  }

  function readForm() {
    return {
      llama_server: {
        url: $("setServerUrl").value.trim(),
        metrics_path: $("setMetricsPath").value.trim(),
        timeout_seconds: num("setTimeoutSec"),
      },
      collector: {
        poll_interval_seconds: num("setPollInterval"),
        live_retention_hours: num("setLiveRetention"),
      },
      gpu: {
        enabled: $("setGpuEnabled").checked,
        poll_interval_seconds: num("setGpuPoll"),
        history_retention_hours: num("setGpuRetention"),
        device_uuids: readGpuUuids(),
      },
      web: {
        host: $("setWebHost").value.trim(),
        port: num("setWebPort"),
      },
      database: {
        path: $("setDbPath").value.trim(),
        wal: $("setWal").value === "true",
      },
      ui: {
        refresh_interval_seconds: num("setRefreshInterval"),
        daily_default_days: num("setDefaultDays"),
        theme: $("setTheme").value,
      },
      logging: {
        level: $("setLogLevel").value,
        max_size_mb: num("setMaxLogSize"),
        backup_count: num("setLogBackupCount"),
      },
      backup: {
        automatic: $("setBackupAuto").checked,
        interval_hours: num("setBackupInterval"),
        keep_count: num("setBackupKeep"),
      },
      updates: {
        check_enabled: $("setUpdatesEnabled").checked,
        check_interval_hours: num("setUpdateInterval"),
        auto_download: $("setUpdateAutoDownload").checked,
      },
    };
  }

  function fillForm(c) {
    $("setServerUrl").value = (c.llama_server && c.llama_server.url) || "";
    $("setMetricsPath").value = (c.llama_server && c.llama_server.metrics_path) || "";
    $("setTimeoutSec").value = (c.llama_server && c.llama_server.timeout_seconds) || 3;
    $("setPollInterval").value = (c.collector && c.collector.poll_interval_seconds) || 5;
    $("setLiveRetention").value = (c.collector && c.collector.live_retention_hours) || 48;
    $("setGpuEnabled").checked = !!(c.gpu && c.gpu.enabled);
    $("setGpuPoll").value = (c.gpu && c.gpu.poll_interval_seconds) || 10;
    $("setGpuRetention").value = (c.gpu && c.gpu.history_retention_hours) || 48;
    $("setRefreshInterval").value = (c.ui && c.ui.refresh_interval_seconds) || 5;
    $("setDefaultDays").value = (c.ui && c.ui.daily_default_days) || 30;
    $("setTheme").value = (c.ui && c.ui.theme) || "system";
    $("setWebHost").value = (c.web && c.web.host) || "127.0.0.1";
    $("setWebPort").value = (c.web && c.web.port) || 8765;
    $("setDbPath").value = (c.database && c.database.path) || "";
    $("setWal").value = String(!!(c.database && c.database.wal));
    $("setLogLevel").value = (c.logging && c.logging.level) || "INFO";
    $("setMaxLogSize").value = (c.logging && c.logging.max_size_mb) || 10;
    $("setLogBackupCount").value = (c.logging && c.logging.backup_count) || 5;
    $("setBackupAuto").checked = !!(c.backup && c.backup.automatic);
    $("setBackupInterval").value = (c.backup && c.backup.interval_hours) || 24;
    $("setBackupKeep").value = (c.backup && c.backup.keep_count) || 14;
    $("setUpdatesEnabled").checked = !!(c.updates && c.updates.check_enabled);
    $("setUpdateInterval").value = (c.updates && c.updates.check_interval_hours) || 24;
    $("setUpdateAutoDownload").checked = !!(c.updates && c.updates.auto_download);
    if (c.paths) lastPaths = c.paths;
    updateDbPathHint();
  }

  function markDirty() {
    if (!settingsDirty) {
      settingsDirty = true;
      updateSettingsFooter();
    }
  }

  function updateSettingsFooter() {
    var badge = $("dirtyBadge");
    var saveBtn = $("btnSaveSettings");
    if (badge) badge.hidden = !settingsDirty;
    if (saveBtn) saveBtn.disabled = !settingsDirty;
  }

  function updateDbPathHint() {
    var v = $("setDbPath").value.trim();
    var hint = $("dbPathHint");
    if (hint) hint.textContent = v ? "当前：" + v : "默认：" + (lastPaths ? lastPaths.database : "--");
  }

  async function loadSettings() {
    // 16E：/api/config 读写都 loopback-only。远程（局域网 IP）客户端是只读端
    // ——不发配置请求（避免 403），表单显示占位并给出提示。
    if (!LM.api.isLocal()) {
      settingsLoaded = true;
      settingsDirty = false;
      updateSettingsFooter();
      var note = document.createElement("p");
      note.className = "settings-desc";
      note.id = "remoteReadOnlyNote";
      note.textContent = "远程只读模式：设置仅能在运行 LlamaMonitor 的电脑上修改（本机 127.0.0.1 访问）。";
      var first = document.querySelector(".settings-pane .settings-card");
      if (first) {
        var oldNote = document.getElementById("remoteReadOnlyNote");
        if (oldNote) oldNote.remove();
        first.insertBefore(note, first.firstChild);
      }
      await loadGpuDetected();
      // 禁用表单控件（含刚渲染的 GPU 勾选框）：远程改了也不会保存，
      // 禁用比"改完没反应"更直观
      var pane = document.querySelector(".settings-pane");
      if (pane) {
        Array.prototype.forEach.call(
          pane.querySelectorAll("input, select"),
          function (el) { el.disabled = true; }
        );
      }
      return;
    }
    try {
      await loadGpuDetected(); // 先拿 GPU 探测结果，fillForm 才能渲染勾选框
      var c = await api.get("/api/config");
      fillForm(c);
      initialConfig = readForm();
      settingsDirty = false;
      settingsLoaded = true;
      updateSettingsFooter();
    } catch (e) {
      ui.toast("加载设置失败：" + (e.message || e), "err");
    }
  }

  async function saveSettings() {
    var btn = $("btnSaveSettings");
    btn.disabled = true;
    var oldLabel = btn.textContent;
    btn.textContent = "保存中...";
    try {
      var data = await api.put("/api/config", readForm());
      if (data && data.success) {
        initialConfig = readForm();
        settingsDirty = false;
        ui.toast(data.restart_required
          ? "设置已保存。请重启 LlamaMonitor 以应用更改。"
          : "设置已保存。", "ok");
      } else {
        ui.toast("配置无效：" + ((data && data.error && data.error.message) || "未知"), "err");
      }
    } catch (e) {
      ui.toast("保存失败：" + (e.message || e), "err");
    }
    btn.textContent = oldLabel;
    updateSettingsFooter();
  }

  async function resetToDefaults() {
    try {
      var d = await api.get("/api/config/defaults");
      fillForm(d);
      if (window.LM && LM.app && LM.app.applyTheme) LM.app.applyTheme(d.ui.theme); // 主题即时预览
      markDirty();
      ui.toast("表单已恢复默认值。点击“保存”写入 config.json。", "warn");
    } catch (e) {
      ui.toast("加载默认值失败：" + (e.message || e), "err");
    }
  }

  async function testConnection() {
    var btn = $("btnTestConn");
    var out = $("testConnResult");
    btn.disabled = true;
    var oldLabel = btn.textContent;
    btn.textContent = "测试中...";
    out.className = "inline-result";
    out.textContent = "";
    try {
      var data = await api.post("/api/config/test-connection", {
        url: $("setServerUrl").value.trim(),
        metrics_path: $("setMetricsPath").value.trim(),
        timeout_seconds: Number($("setTimeoutSec").value) || 3,
      }, 15000);
      if (data.success) {
        out.className = "inline-result ok";
        out.textContent = "成功 - " + data.latency_ms + " ms" + (data.metrics_detected ? "" : "（未检测到 llamacpp 指标）");
        ui.toast("连接成功：" + data.latency_ms + " ms", "ok");
        lastTestConn = { at: Date.now(), ok: true, latencyMs: data.latency_ms };
      } else {
        out.className = "inline-result bad";
        out.textContent = "失败：" + data.error;
        ui.toast("连接失败：" + data.error, "err");
        lastTestConn = { at: Date.now(), ok: false, error: data.error };
      }
    } catch (e) {
      out.className = "inline-result bad";
      out.textContent = "失败：" + (e.message || e);
      ui.toast("连接失败：" + (e.message || e), "err");
      lastTestConn = { at: Date.now(), ok: false, error: e.message || "网络错误" };
    }
    btn.textContent = oldLabel;
    btn.disabled = false;
    updateServerConnStatus();
  }

  /* ================= GPU 探测（Settings 勾选用） ================= */

  async function loadGpuDetected() {
    var box = $("gpuDetected");
    if (!box) return;
    var available = true;
    var detected = [];
    try {
      var d = await api.get("/api/gpu/status", 10000);
      available = !!d.available;
      detected = d.detected || [];
    } catch (e) {
      available = false;
    }
    renderGpuDetected(detected, readGpuUuids(), available);
  }

  function renderGpuDetected(detected, selectedUuids, available) {
    var box = $("gpuDetected");
    // UI-002：签名未变不重建（保留焦点与勾选状态）
    var sig = JSON.stringify((detected || []).map(function (g) { return g.uuid + "|" + g.index + "|" + (g.name || ""); }));
    if (gpuPickSignature === sig && box.children.length) return;
    gpuPickSignature = sig;
    box.innerHTML = "";
    if (!available) {
      var note = document.createElement("span");
      note.className = "na";
      note.textContent = "未检测到 GPU（nvidia-smi 不可用）。";
      box.appendChild(note);
      return;
    }
    if (!detected || !detected.length) {
      var none = document.createElement("span");
      none.className = "na";
      none.textContent = "未检测到 GPU。";
      box.appendChild(none);
      return;
    }
    // BUG-G 修复（spec §61）：每卡一行——checkbox + 型号第一行，UUID 缩进第二行。
    // 不再把 checkbox/名称/长 UUID 挤在同一行。
    detected.forEach(function (g) {
      var row = document.createElement("div");
      row.className = "gpu-detected-row";
      var label = document.createElement("label");
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = g.uuid;
      cb.checked = selectedUuids.indexOf(g.uuid) !== -1;
      cb.addEventListener("change", markDirty);
      label.appendChild(cb);
      label.appendChild(document.createTextNode(
        " GPU " + (g.index == null ? "?" : g.index) + " · " + (g.name || "未知型号")));
      row.appendChild(label);
      if (g.uuid) {
        var uuid = document.createElement("div");
        uuid.className = "gpu-row-uuid";
        uuid.textContent = g.uuid;
        uuid.title = g.uuid;
        row.appendChild(uuid);
      }
      box.appendChild(row);
    });
    var hint = document.createElement("div");
    hint.className = "caption";
    hint.style.marginTop = "var(--spacing-sm)";
    hint.textContent = "不勾选 = 监控所有检测到的 GPU。";
    box.appendChild(hint);
  }

  /* ================= Data Management（spec §90/§91） ================= */

  function kvRow(box, k, v) {
    var rowK = document.createElement("span");
    rowK.className = "k";
    rowK.textContent = k;
    var rowV = document.createElement("span");
    rowV.className = "v";
    rowV.textContent = String(v);
    box.appendChild(rowK);
    box.appendChild(rowV);
  }

  async function refreshDataInfo() {
    var box = $("dataInfo");
    box.innerHTML = "";
    try {
      var d = await api.get("/api/data/info");
      [
        ["数据库", d.database_path],
        ["数据库大小", F.formatBytes(d.database_size_bytes)],
        ["首次记录日期", d.first_recorded_date || "无"],
        ["最近记录日期", d.last_recorded_date || "无"],
        ["累计记录天数", d.recorded_days],
        ["实时样本数", F.formatNumber(d.live_samples)],
        ["GPU 样本数", F.formatNumber(d.gpu_samples)],
        ["备份数量", d.backup_count],
      ].forEach(function (r) { kvRow(box, r[0], r[1]); });
      loadBackups();
    } catch (e) {
      kvRow(box, "状态", "加载失败：" + (e.message || e));
    }
  }

  async function loadBackups() {
    var box = $("backupList");
    if (!box) return;
    box.innerHTML = "";
    try {
      var list = await api.get("/api/data/backups");
      if (!list.length) {
        box.textContent = "暂无备份。";
        return;
      }
      list.forEach(function (b) {
        var div = document.createElement("div");
        var main = document.createElement("span");
        main.textContent = b.filename + "  \u00B7  " + (b.kind || "") + "  \u00B7  " +
          F.formatBytes(b.size_bytes) + "  \u00B7  " + (b.created_at || "");
        div.appendChild(main);
        if (b.verified === true) {
          var v = document.createElement("span");
          v.className = "verified-yes";
          v.textContent = "  ·  ✓ 已验证";
          div.appendChild(v);
        } else if (b.verified === false) {
          var v2 = document.createElement("span");
          v2.className = "verified-no";
          v2.textContent = "  ·  ✗ 未验证";
          div.appendChild(v2);
        }
        box.appendChild(div);
      });
    } catch (e) {
      box.textContent = "加载备份失败。";
    }
  }

  async function backupDb() {
    var btn = $("btnBackup");
    btn.disabled = true;
    var old = btn.textContent;
    btn.textContent = "备份中...";
    try {
      var data = await api.post("/api/data/backup");
      if (data.success) {
        ui.toast("备份已创建：" + data.file, "ok");
        refreshDataInfo();
      } else {
        ui.toast("备份失败：" + ((data.error && data.error.message) || "未知"), "err");
      }
    } catch (e) {
      ui.toast("备份失败：" + (e.message || e), "err");
    }
    btn.textContent = old;
    btn.disabled = false;
  }

  async function doClearLive() {
    var btn = $("btnClearLive");
    btn.disabled = true;
    var old = btn.textContent;
    btn.textContent = "清空中...";
    try {
      var data = await api.post("/api/data/clear-live", { confirm: true });
      if (data.success) {
        ui.toast("实时历史已清空（" + data.deleted + " 条样本）。", "ok");
        refreshDataInfo();
        if (LM.app) {
          LM.app.refreshLiveNow();     // 性能页吞吐图
          LM.app.refreshDailyNow();    // 用量页每日图表/表格（含今日行）
          LM.app.refreshSummaryNow();  // 今日/范围摘要卡片
          LM.app.refreshDataQualityNow(); // 历史页数据质量/缺口（今日覆盖率基于实时样本，立即失效重取）
          LM.app.refreshEventsNow();   // 历史页监控事件
        }
      } else {
        ui.toast("清空失败：" + ((data.error && data.error.message) || "未知"), "err");
      }
    } catch (e) {
      ui.toast("清空失败：" + (e.message || e), "err");
    }
    btn.textContent = old;
    btn.disabled = false;
  }

  function clearLive() {
    ui.modal({
      title: "清空实时历史",
      text: "这将永久删除所有实时样本。\n每日汇总与当前计数器基线会保留。",
      okLabel: "清空",
      danger: true,
      onDone: function (ok) { if (ok) doClearLive(); },
    });
  }

  async function doResetStats() {
    var btn = $("btnResetStats");
    btn.disabled = true;
    var old = btn.textContent;
    btn.textContent = "重置中...";
    try {
      var data = await api.post("/api/data/reset-statistics", { confirm: "RESET" });
      if (data.success) {
        ui.toast("统计已重置。计数从现在开始。", "ok");
        refreshDataInfo();
        if (LM.app) {
          LM.app.refreshLiveNow();     // 性能页吞吐图
          LM.app.refreshDailyNow();    // 用量页每日图表/表格
          LM.app.refreshSummaryNow();  // 今日/范围摘要卡片（BUG-A：month 由后端重算）
          LM.app.refreshMtpNow();      // MTP 统计
          LM.app.refreshDataQualityNow(); // 历史页数据质量/最近缺口（缺口已随重置清除）
          LM.app.refreshEventsNow();   // 历史页监控事件（新 counter_reset 事件）
        }
      } else {
        ui.toast("重置失败：" + ((data.error && data.error.message) || "未知"), "err");
      }
    } catch (e) {
      ui.toast("重置失败：" + (e.message || e), "err");
    }
    btn.textContent = old;
    btn.disabled = false;
  }

  function resetStats() {
    ui.modal({
      title: "重置所有统计",
      text: "这将永久删除所有 Token 用量历史、实时样本与已知监控缺口记录。\n\n配置与当前 llama.cpp 计数器基线会保留。",
      okLabel: "重置",
      danger: true,
      needsInput: true,
      inputValue: "RESET",
      onDone: function (ok) { if (ok) doResetStats(); },
    });
  }

  async function runDbCheck() {
    var out = $("dbCheckResult");
    var btn = $("btnRunCheck");
    btn.disabled = true;
    var old = btn.textContent;
    btn.textContent = "检查中...";
    out.className = "inline-result";
    out.textContent = "";
    try {
      var data = await api.post("/api/data/check-database");
      if (data.success) {
        var db = data.health && data.health.database;
        out.className = "inline-result " + (db === "healthy" ? "ok" : "bad");
        out.textContent = (db === "healthy" ? "数据库检查通过（健康）。" : "数据库检查：" + db) +
          (data.health && data.health.database_detail ? " - " + data.health.database_detail : "");
        ui.toast(out.textContent, db === "healthy" ? "ok" : "err");
      } else {
        out.className = "inline-result bad";
        out.textContent = "检查失败：" + ((data.error && data.error.message) || "未知");
      }
    } catch (e) {
      out.className = "inline-result bad";
      out.textContent = "检查失败：" + (e.message || e);
    }
    btn.textContent = old;
    btn.disabled = false;
  }

  function exportCsv() {
    window.location.href = "/api/data/export/daily.csv";
    ui.toast("CSV 已导出（见下载目录）。", "ok");
  }
  function exportGpuCsv() {
    window.location.href = "/api/data/export/gpu_daily.csv";
    ui.toast("GPU CSV 已导出（见下载目录）。", "ok");
  }

  /* ================= Application（Phase 10 集成） ================= */

  function fmtUptime(sec) {
    sec = Math.max(0, sec | 0);
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    if (h) return h + "h " + m + "m";
    if (m) return m + "m " + s + "s";
    return s + "s";
  }

  async function loadAppIntegration() {
    var box = $("appInfo");
    box.innerHTML = "";
    // 16E：/api/app/integration 是 loopback-only——远程客户端不发（含可执行文件
    // 路径等本机信息），占位说明代替 403。
    if (!LM.api.isLocal()) {
      kvRow(box, "状态", "远程只读：本机信息仅在本机可见");
      return;
    }
    try {
      var d = await api.get("/api/app/integration");
      kvRow(box, "应用模式", d.background ? "后台（托盘）" : "前台");
      kvRow(box, "系统托盘", d.tray_supported ? "可用" : "不可用");
      kvRow(box, "单实例", d.single_instance ? "启用" : "禁用");
      kvRow(box, "平台", (d.platform || "未知") + (d.frozen ? "（EXE）" : "（开发）"));
      kvRow(box, "可执行文件", d.executable || "\u2014");
      kvRow(box, "应用数据", d.app_data || "\u2014");
      if (d.uptime_seconds != null) kvRow(box, "运行时长", fmtUptime(d.uptime_seconds));
      renderAutostart(d.autostart || {});
    } catch (e) {
      kvRow(box, "状态", "加载失败：" + (e.message || e));
    }
  }

  function renderAutostart(a) {
    var cb = $("setAutostart");
    var status = $("autostartStatus");
    var cmdBox = $("autostartCommand");
    var repairRow = $("autostartRepairRow");
    if (!a.supported) {
      cb.checked = false;
      cb.disabled = true;
      status.textContent = "开发模式不可用（需要 EXE）。";
      status.style.color = "";
      cmdBox.textContent = "\u2014";
      repairRow.style.display = "none";
      return;
    }
    cb.checked = !!a.enabled;
    cb.disabled = false;
    if (a.stale) {
      status.textContent = "已失效：启动项指向旧的 EXE 位置。点击修复。";
      status.style.color = "var(--warning)";
      repairRow.style.display = "";
    } else {
      status.textContent = a.enabled ? "已启用" : "未启用";
      status.style.color = "";
      repairRow.style.display = "none";
    }
    cmdBox.textContent = a.command || a.expected_command || "\u2014";
  }

  async function putAutostart(enabled) {
    try {
      var data = await api.put("/api/app/autostart", { enabled: enabled });
      if (data.success) {
        ui.toast(enabled ? "已启用随 Windows 启动" : "已禁用随 Windows 启动", "ok");
      } else {
        ui.toast("失败：" + ((data.error && data.error.message) || "未知"), "err");
      }
    } catch (e) {
      ui.toast("失败：" + (e.message || e), "err");
    }
    loadAppIntegration();
  }

  function openFolderTarget(target) {
    api.post("/api/app/open-folder", { target: target })
      .then(function (d) {
        if (d && d.success) ui.toast("已打开：" + (d.path || target), "ok");
        else ui.toast("打开失败：" + ((d && d.error && d.error.message) || "未知"), "err");
      })
      .catch(function (e) { ui.toast("打开失败：" + (e.message || e), "err"); });
  }

  function exitApp() {
    ui.modal({
      title: "退出 LlamaMonitor",
      text: "退出 LlamaMonitor？\n监控将停止，直到再次启动应用。",
      okLabel: "退出",
      onDone: function (ok) {
        if (!ok) return;
        var btn = $("btnExitApp");
        btn.disabled = true;
        btn.textContent = "正在退出\u2026";
        api.post("/api/app/exit").then(function () {
          ui.toast("LlamaMonitor 正在退出...", "ok");
        }).catch(function () { /* 应用正在退出 */ });
      },
    });
  }

  /* ================= Updates（Phase 13 语义，UI-023 走中央调度器） ================= */

  function renderUpdateStatus(st) {
    if (!st) return;
    updateStatus = st;
    $("updCurrentVersion").textContent = st.current_version || "--";
    var _modeMap = { "installed": "安装版", "portable": "便携版", "development": "开发模式" };
    $("updInstallMode").textContent = _modeMap[st.installation_mode] || st.installation_mode || "--";
    $("updLastCheck").textContent = st.last_check || "从未";
    $("updLatestVersion").textContent = st.available_version || "--";
    var stEl = $("updStatus");
    var _stMap = { "UP_TO_DATE": "已是最新", "UPDATE_AVAILABLE": "有可用更新", "CHECKING": "检查中", "DOWNLOADING": "下载中", "VERIFYING": "校验中", "READY_TO_INSTALL": "可安装", "INSTALLING": "安装中", "ERROR": "错误", "IDLE": "空闲" };
    stEl.textContent = (_stMap[st.state] || st.state || "--") + (st.error ? " - " + st.error : "");
    stEl.className = "update-state" +
      (st.state === "ERROR" ? " error" : st.state === "UPDATE_AVAILABLE" ? " available" : "");

    var mode = st.installation_mode;
    var busy = ["CHECKING", "DOWNLOADING", "VERIFYING", "INSTALLING"].indexOf(st.state) !== -1;
    $("btnUpdateCheck").disabled = busy;
    $("btnUpdateDownload").disabled = busy || st.state !== "UPDATE_AVAILABLE" || mode === "development";
    $("btnUpdateInstall").disabled = busy || st.state !== "READY_TO_INSTALL" ||
      mode === "development" || mode === "portable";
    $("btnUpdateCancel").hidden = st.state !== "DOWNLOADING";
    $("btnUpdateCheck").textContent =
      st.state === "CHECKING" ? "检查中..." :
      st.state === "DOWNLOADING" ? "下载中..." :
      st.state === "VERIFYING" ? "校验中..." : "检查更新";

    var showProgress = st.state === "DOWNLOADING" && st.total_bytes > 0;
    $("updProgressWrap").hidden = !showProgress;
    if (showProgress) {
      $("updProgressBar").style.width = Math.min(100, st.progress_percent || 0) + "%";
      $("updProgressText").textContent =
        F.formatBytes(st.downloaded_bytes) + " / " + F.formatBytes(st.total_bytes) +
        " - " + Math.floor(st.progress_percent || 0) + "%（下载时已做 SHA-256 校验）";
    }

    var rel = st.release || null;
    var notes = rel && rel.release_notes;
    $("updReleaseNotesWrap").hidden = !notes;
    if (notes) $("updReleaseNotes").textContent = notes; // textContent only（不 innerHTML）

    $("updSignatureNote").textContent =
      st.state === "READY_TO_INSTALL"
        ? "✓ 签名已验证 - SHA-256 已验证 - 可安装（会先创建更新前备份）。"
        : (st.state === "UPDATE_AVAILABLE" && rel
            ? "签名已验证。下载后、安装前将再次校验安装包（SHA-256）。"
            : "");

    var note = "";
    if (mode === "development") {
      note = "开发模式下无法安装更新（请运行 EXE / 便携版构建来安装更新）。";
    } else if (mode === "portable") {
      note = "便携版：LlamaMonitor 会下载并校验 ZIP，但绝不覆盖自身。用下方按钮打开下载文件夹或 GitHub Release。";
    }
    $("updModeNote").textContent = note;
    $("updPortableActions").hidden = mode !== "portable";

    // 全局 InfoBar（Overview 顶部横幅由 app.js 处理；这里管状态文本即可）
    if (st.state === "UPDATE_AVAILABLE" && st.available_version && LM.app) {
      LM.app.setUpdateBanner(true, "LlamaMonitor " + st.available_version + " 可用（当前 " +
        st.current_version + "）");
    } else if (LM.app) {
      LM.app.setUpdateBanner(false);
    }
  }

  async function loadUpdateStatus() {
    // 16E：/api/update/* 是 loopback-only。远程（局域网 IP）客户端不发该请求
    // ——此前每 30s 全局轮询在手机上刷 403；本机行为不变。
    if (!LM.api.isLocal()) return;
    try {
      var st = await api.get("/api/update/status");
      renderUpdateStatus(st);
    } catch (e) {
      // loopback 后端不可达：保留上次状态（不 toast 轰炸）
      console.warn("update status failed:", e.message || e);
    }
  }

  function updateAction(action, confirmText) {
    function doIt() {
      api.post("/api/update/" + action)
        .then(function (d) {
          if (action === "install" && d && d.state === "INSTALLING") {
            ui.toast("正在安装 " + (d.available_version || "更新") + " - LlamaMonitor 即将关闭，安装程序将接管...", "ok");
          }
          return loadUpdateStatus();
        })
        .catch(function (e) {
          var actName = { check: "检查", download: "下载", install: "安装", cancel: "取消" }[action] || action;
          ui.toast("更新" + actName + "失败：" + (e.message || e), "err");
        });
    }
    if (confirmText) {
      ui.modal({
        title: "安装更新",
        text: confirmText,
        okLabel: "安装",
        onDone: function (ok) { if (ok) doIt(); },
      });
    } else {
      doIt();
    }
  }

  /* ================= About ================= */

  async function loadAbout() {
    // 品牌图标（Phase 16C §23；注意 brand 是函数，必须调用取 SVG 字符串）
    try { var lg = $("aboutLogo"); if (lg && LM.icons && !lg.innerHTML) lg.innerHTML = LM.icons.brand(); } catch (e) {}
    var ver = "--";
    try {
      var d = await api.get("/api/version");
      $("aboutName").textContent = d.name || "LlamaMonitor";
      ver = d.version || "--";
      $("aboutSchema").textContent = String(d.schema_version == null ? "--" : d.schema_version);
    } catch (e) {
      ver = "--";
      $("aboutSchema").textContent = "--";
    }
    $("aboutVersion").textContent = ver;
    var row = $("aboutVersionRow"); if (row) row.textContent = ver;
    // 16E：/api/config loopback-only——远程不发（数据目录是本地路径，不外露）
    if (LM.api.isLocal()) {
      try {
        var c = await api.get("/api/config");
        if (c.paths) $("aboutDataDir").textContent = c.paths.database || "--";
      } catch (e) { /* 保留占位 */ }
    }
    // 平台（静态）
    var pf = $("aboutPlatform"); if (pf) pf.textContent = "Windows x64";
  }

  function copyVersionInfo() {
    var out = $("aboutCopyResult");
    var text = ($("aboutName").textContent || "") + " " + ($("aboutVersion").textContent || "") +
      " (schema " + ($("aboutSchema").textContent || "--") + ")";
    (async function () {
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(text);
        } else {
          var ta = document.createElement("textarea");
          ta.value = text;
          ta.style.position = "fixed";
          ta.style.opacity = "0";
          document.body.appendChild(ta);
          ta.select();
          document.execCommand("copy");
          document.body.removeChild(ta);
        }
        out.className = "inline-result ok";
        out.textContent = "已复制";
        setTimeout(function () { out.textContent = ""; }, 2000);
      } catch (e) {
        out.className = "inline-result bad";
        out.textContent = "复制失败：" + (e.message || e);
      }
    })();
  }

  /* ================= 分区定位（Phase 16B spec §55：内部二级导航 rail） =================
     托盘桥与更新横幅通过 goToSection 定位到分类；rail 按钮同步高亮。
     data-sec 分组：data 分类 = 存储+备份+日志+数据管理+危险区。 */
  var activeSection = "server";

  /* Phase 16C §21：服务器连接状态（轻量；复用 LM.app.serverConnectionState()
     —— 它来自 /api/status 既有轮询与用户点击"测试连接"的结果，
     不额外高频探测）。测试连接成功/失败后也刷新此块。 */
  function updateServerConnStatus() {
    var box = $("serverConnStatus");
    if (!box) return;
    var conn = null;
    if (window.LM && LM.app && LM.app.serverConnectionState) conn = LM.app.serverConnectionState();
    var textEl = box.querySelector(".cs-text");
    var subEl = box.querySelector(".cs-sub");
    box.classList.remove("online", "offline");
    var tested = lastTestConn && lastTestConn.at; // 用户点过"测试连接"
    if (tested) {
      if (lastTestConn.ok) {
        box.classList.add("online");
        textEl.textContent = "已连接";
        subEl.textContent = (conn && conn.url ? conn.url + " · " : "") + "延迟 " + lastTestConn.latencyMs + " ms（测试）";
      } else {
        box.classList.add("offline");
        textEl.textContent = "连接失败";
        subEl.textContent = (conn && conn.url ? conn.url + " · " : "") + (lastTestConn.error || "最近一次测试失败");
      }
      return;
    }
    if (conn) {
      if (conn.status === "online") {
        box.classList.add("online");
        textEl.textContent = "已连接";
        subEl.textContent = conn.url || "";
      } else if (conn.status === "offline") {
        box.classList.add("offline");
        textEl.textContent = "连接失败";
        subEl.textContent = conn.url || "服务器当前不可达";
      } else {
        textEl.textContent = "未测试";
        subEl.textContent = conn.url || "";
      }
    } else {
      textEl.textContent = "未测试";
      subEl.textContent = "";
    }
  }

  function showSection(name) {
    activeSection = name;
    var pane = document.querySelector(".settings-pane");
    if (pane) {
      pane.querySelectorAll(".settings-card").forEach(function (c) {
        c.hidden = c.getAttribute("data-sec") !== name;
      });
    }
    document.querySelectorAll(".settings-rail .rail-item").forEach(function (b) {
      b.setAttribute("aria-current", b.getAttribute("data-sec") === name ? "true" : "false");
    });
    if (name === "server") updateServerConnStatus();
  }

  function goToSection(name) {
    if (!name || !document.querySelector('.rail-item[data-sec="' + name + '"]')) name = "server";
    showSection(name);
  }

  /* ================= 初始化 ================= */

  function init() {
    // 事件绑定
    var bind = function (id, ev, fn) {
      var el = $(id);
      if (el) el.addEventListener(ev, fn);
    };
    bind("btnSaveSettings", "click", saveSettings);
    bind("btnResetDefaults", "click", resetToDefaults);
    bind("btnTestConn", "click", testConnection);
    bind("btnRefreshData", "click", refreshDataInfo);
    bind("btnRunCheck", "click", runDbCheck);
    bind("btnExportCsv", "click", exportCsv);
    bind("btnExportGpuCsv", "click", exportGpuCsv);
    bind("btnBackup", "click", backupDb);
    bind("btnClearLive", "click", clearLive);
    bind("btnResetStats", "click", resetStats);
    bind("btnOpenData", "click", function () { openFolderTarget("data"); });
    bind("btnOpenLogs", "click", function () { openFolderTarget("logs"); });
    bind("btnOpenBackups", "click", function () { openFolderTarget("backups"); });
    bind("btnExitApp", "click", exitApp);
    bind("btnCopyVersion", "click", copyVersionInfo);
    // Phase 16C 审计：About 页"打开数据目录"按钮此前无任何处理器（死按钮）
    bind("btnAboutOpenData", "click", function () { openFolderTarget("data"); });
    bind("btnUpdateCheck", "click", function () { updateAction("check"); });
    bind("btnUpdateDownload", "click", function () { updateAction("download"); });
    bind("btnUpdateInstall", "click", function () {
      var st = updateStatus || {};
      var to = st.available_version || "新版本";
      updateAction("install",
        "安装 LlamaMonitor " + to + "？\n\n" +
        "LlamaMonitor 将先创建更新前备份（数据库 + 配置），然后正常退出，" +
        "由安装程序替换程序文件。需要先完成一次已验证的更新下载。");
    });
    bind("btnUpdateCancel", "click", function () { updateAction("cancel"); });
    bind("btnUpdateOpenFolder", "click", function () { openFolderTarget("updates"); });
    bind("btnUpdateOpenRelease", "click", function () {
      var rel = updateStatus && updateStatus.release;
      // AUDIT-WEB-006 保留：只放行 http(s) URL
      if (rel && rel.release_url && /^https?:\/\//i.test(rel.release_url)) {
        window.open(rel.release_url, "_blank", "noopener");
      }
    });
    bind("setAutostart", "change", function (e) { putAutostart(e.target.checked); });
    bind("btnAutostartRepair", "click", function () { putAutostart(true); });
    bind("setTheme", "change", function () {
      if (LM.app && LM.app.applyTheme) LM.app.applyTheme($("setTheme").value);
    });
    bind("setDbPath", "input", updateDbPathHint);

    // 表单输入 -> dirty 标记
    [
      "setServerUrl", "setMetricsPath", "setTimeoutSec", "setPollInterval", "setLiveRetention",
      "setGpuEnabled", "setGpuPoll", "setGpuRetention",
      "setRefreshInterval", "setDefaultDays", "setTheme", "setWebHost", "setWebPort",
      "setDbPath", "setWal", "setLogLevel", "setMaxLogSize", "setLogBackupCount",
      "setBackupAuto", "setBackupInterval", "setBackupKeep",
      "setUpdatesEnabled", "setUpdateInterval", "setUpdateAutoDownload",
    ].forEach(function (id) {
      bind(id, "input", markDirty);
      bind(id, "change", markDirty);
    });
    var gpuBox = $("gpuDetected");
    if (gpuBox) gpuBox.addEventListener("change", markDirty);

    // 内部二级导航 rail（Phase 16B spec §55）：点击切换分类 pane
    document.querySelectorAll(".settings-rail .rail-item").forEach(function (b) {
      b.addEventListener("click", function () {
        goToSection(b.getAttribute("data-sec"));
      });
    });
    showSection("server");
  }

  window.LM = window.LM || {};
  LM.settings = {
    init: init,
    loadSettings: loadSettings,
    isLoaded: function () { return settingsLoaded; },
    isDirty: function () { return settingsDirty; },
    goToSection: goToSection,
    activeSection: function () { return activeSection; },
    loadAppIntegration: loadAppIntegration,
    loadAbout: loadAbout,
    loadUpdateStatus: loadUpdateStatus,
    refreshDataInfo: refreshDataInfo,
    resetToDefaults: resetToDefaults,
  };
})();
