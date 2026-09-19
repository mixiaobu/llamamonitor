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
    if (hint) hint.textContent = v ? "Current: " + v : "Default: " + (lastPaths ? lastPaths.database : "--");
  }

  async function loadSettings() {
    try {
      await loadGpuDetected(); // 先拿 GPU 探测结果，fillForm 才能渲染勾选框
      var c = await api.get("/api/config");
      fillForm(c);
      initialConfig = readForm();
      settingsDirty = false;
      settingsLoaded = true;
      updateSettingsFooter();
    } catch (e) {
      ui.toast("Failed to load settings: " + (e.message || e), "err");
    }
  }

  async function saveSettings() {
    var btn = $("btnSaveSettings");
    btn.disabled = true;
    var oldLabel = btn.textContent;
    btn.textContent = "Saving...";
    try {
      var data = await api.put("/api/config", readForm());
      if (data && data.success) {
        initialConfig = readForm();
        settingsDirty = false;
        ui.toast(data.restart_required
          ? "Settings saved. Restart LlamaMonitor to apply changes."
          : "Settings saved.", "ok");
      } else {
        ui.toast("Configuration invalid: " + ((data && data.error && data.error.message) || "unknown"), "err");
      }
    } catch (e) {
      ui.toast("Save failed: " + (e.message || e), "err");
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
      ui.toast("Form reset to defaults. Click Save to write config.json.", "warn");
    } catch (e) {
      ui.toast("Failed to load defaults: " + (e.message || e), "err");
    }
  }

  async function testConnection() {
    var btn = $("btnTestConn");
    var out = $("testConnResult");
    btn.disabled = true;
    var oldLabel = btn.textContent;
    btn.textContent = "Testing...";
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
        out.textContent = "Success - " + data.latency_ms + " ms" + (data.metrics_detected ? "" : " (no llamacpp metrics detected)");
        ui.toast("Connection successful: " + data.latency_ms + " ms", "ok");
      } else {
        out.className = "inline-result bad";
        out.textContent = "Failed: " + data.error;
        ui.toast("Connection failed: " + data.error, "err");
      }
    } catch (e) {
      out.className = "inline-result bad";
      out.textContent = "Failed: " + (e.message || e);
      ui.toast("Connection failed: " + (e.message || e), "err");
    }
    btn.textContent = oldLabel;
    btn.disabled = false;
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
      note.textContent = "No GPUs detected (nvidia-smi unavailable).";
      box.appendChild(note);
      return;
    }
    if (!detected || !detected.length) {
      var none = document.createElement("span");
      none.className = "na";
      none.textContent = "No GPUs detected.";
      box.appendChild(none);
      return;
    }
    detected.forEach(function (g) {
      var label = document.createElement("label");
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = g.uuid;
      cb.checked = selectedUuids.indexOf(g.uuid) !== -1;
      cb.addEventListener("change", markDirty);
      label.appendChild(cb);
      label.appendChild(document.createTextNode(" GPU " + (g.index == null ? "?" : g.index) + " " + (g.name || "")));
      var uuid = document.createElement("span");
      uuid.className = "uuid";
      uuid.textContent = " " + g.uuid;
      label.appendChild(uuid);
      box.appendChild(label);
    });
    var hint = document.createElement("div");
    hint.className = "caption";
    hint.textContent = "No boxes checked = monitor all detected GPUs.";
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
      ["Database", "Database Size", "First Recorded Date", "Last Recorded Date",
       "Total Recorded Days", "Live Samples Count", "GPU Samples Count", "Backup Count"]
        .forEach(function (k) {
          var field = {
            "Database": d.database_path,
            "Database Size": F.formatBytes(d.database_size_bytes),
            "First Recorded Date": d.first_recorded_date || "N/A",
            "Last Recorded Date": d.last_recorded_date || "N/A",
            "Total Recorded Days": d.recorded_days,
            "Live Samples Count": F.formatNumber(d.live_samples),
            "GPU Samples Count": F.formatNumber(d.gpu_samples),
            "Backup Count": d.backup_count,
          }[k];
          kvRow(box, k, field);
        });
      loadBackups();
    } catch (e) {
      kvRow(box, "Status", "Failed to load: " + (e.message || e));
    }
  }

  async function loadBackups() {
    var box = $("backupList");
    if (!box) return;
    box.innerHTML = "";
    try {
      var list = await api.get("/api/data/backups");
      if (!list.length) {
        box.textContent = "No backups yet.";
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
          v.textContent = "  \u00B7  \u2713 verified";
          div.appendChild(v);
        } else if (b.verified === false) {
          var v2 = document.createElement("span");
          v2.className = "verified-no";
          v2.textContent = "  \u00B7  \u2717 NOT verified";
          div.appendChild(v2);
        }
        box.appendChild(div);
      });
    } catch (e) {
      box.textContent = "Failed to load backups.";
    }
  }

  async function backupDb() {
    var btn = $("btnBackup");
    btn.disabled = true;
    var old = btn.textContent;
    btn.textContent = "Backing up...";
    try {
      var data = await api.post("/api/data/backup");
      if (data.success) {
        ui.toast("Backup created: " + data.file, "ok");
        refreshDataInfo();
      } else {
        ui.toast("Backup failed: " + ((data.error && data.error.message) || "unknown"), "err");
      }
    } catch (e) {
      ui.toast("Backup failed: " + (e.message || e), "err");
    }
    btn.textContent = old;
    btn.disabled = false;
  }

  async function doClearLive() {
    var btn = $("btnClearLive");
    btn.disabled = true;
    var old = btn.textContent;
    btn.textContent = "Clearing...";
    try {
      var data = await api.post("/api/data/clear-live", { confirm: true });
      if (data.success) {
        ui.toast("Live history cleared (" + data.deleted + " samples).", "ok");
        refreshDataInfo();
        if (LM.app) LM.app.refreshLiveNow();
      } else {
        ui.toast("Clear failed: " + ((data.error && data.error.message) || "unknown"), "err");
      }
    } catch (e) {
      ui.toast("Clear failed: " + (e.message || e), "err");
    }
    btn.textContent = old;
    btn.disabled = false;
  }

  function clearLive() {
    ui.modal({
      title: "Clear Live History",
      text: "This permanently deletes all live samples.\nDaily totals and the current counter baseline are preserved.",
      okLabel: "Clear",
      danger: true,
      onDone: function (ok) { if (ok) doClearLive(); },
    });
  }

  async function doResetStats() {
    var btn = $("btnResetStats");
    btn.disabled = true;
    var old = btn.textContent;
    btn.textContent = "Resetting...";
    try {
      var data = await api.post("/api/data/reset-statistics", { confirm: "RESET" });
      if (data.success) {
        ui.toast("Statistics reset. Counting starts from now.", "ok");
        refreshDataInfo();
        if (LM.app) LM.app.refreshSummaryNow();
      } else {
        ui.toast("Reset failed: " + ((data.error && data.error.message) || "unknown"), "err");
      }
    } catch (e) {
      ui.toast("Reset failed: " + (e.message || e), "err");
    }
    btn.textContent = old;
    btn.disabled = false;
  }

  function resetStats() {
    ui.modal({
      title: "Reset All Statistics",
      text: "This permanently deletes all token usage history and live samples.\n\nConfiguration and the current llama.cpp counter baseline are preserved.",
      okLabel: "Reset",
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
    btn.textContent = "Checking...";
    out.className = "inline-result";
    out.textContent = "";
    try {
      var data = await api.post("/api/data/check-database");
      if (data.success) {
        var db = data.health && data.health.database;
        out.className = "inline-result " + (db === "healthy" ? "ok" : "bad");
        out.textContent = (db === "healthy" ? "Database check passed (healthy)." : "Database check: " + db) +
          (data.health && data.health.database_detail ? " - " + data.health.database_detail : "");
        ui.toast(out.textContent, db === "healthy" ? "ok" : "err");
      } else {
        out.className = "inline-result bad";
        out.textContent = "Check failed: " + ((data.error && data.error.message) || "unknown");
      }
    } catch (e) {
      out.className = "inline-result bad";
      out.textContent = "Check failed: " + (e.message || e);
    }
    btn.textContent = old;
    btn.disabled = false;
  }

  function exportCsv() {
    window.location.href = "/api/data/export/daily.csv";
    ui.toast("CSV exported (see download location).", "ok");
  }
  function exportGpuCsv() {
    window.location.href = "/api/data/export/gpu_daily.csv";
    ui.toast("GPU CSV exported (see download location).", "ok");
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
    try {
      var d = await api.get("/api/app/integration");
      kvRow(box, "Application Mode", d.background ? "Background (tray)" : "Foreground");
      kvRow(box, "System Tray", d.tray_supported ? "Available" : "Unavailable");
      kvRow(box, "Single Instance", d.single_instance ? "Enabled" : "Disabled");
      kvRow(box, "Platform", (d.platform || "unknown") + (d.frozen ? " (EXE)" : " (development)"));
      kvRow(box, "Executable", d.executable || "\u2014");
      kvRow(box, "App Data", d.app_data || "\u2014");
      if (d.uptime_seconds != null) kvRow(box, "Uptime", fmtUptime(d.uptime_seconds));
      renderAutostart(d.autostart || {});
    } catch (e) {
      kvRow(box, "Status", "Failed to load: " + (e.message || e));
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
      status.textContent = "Unavailable in development mode (requires EXE).";
      status.style.color = "";
      cmdBox.textContent = "\u2014";
      repairRow.style.display = "none";
      return;
    }
    cb.checked = !!a.enabled;
    cb.disabled = false;
    if (a.stale) {
      status.textContent = "Stale: the startup entry points to an old EXE location. Click Repair.";
      status.style.color = "var(--warning)";
      repairRow.style.display = "";
    } else {
      status.textContent = a.enabled ? "Enabled" : "Disabled";
      status.style.color = "";
      repairRow.style.display = "none";
    }
    cmdBox.textContent = a.command || a.expected_command || "\u2014";
  }

  async function putAutostart(enabled) {
    try {
      var data = await api.put("/api/app/autostart", { enabled: enabled });
      if (data.success) {
        ui.toast(enabled ? "Start with Windows enabled" : "Start with Windows disabled", "ok");
      } else {
        ui.toast("Failed: " + ((data.error && data.error.message) || "unknown"), "err");
      }
    } catch (e) {
      ui.toast("Failed: " + (e.message || e), "err");
    }
    loadAppIntegration();
  }

  function openFolderTarget(target) {
    api.post("/api/app/open-folder", { target: target })
      .then(function (d) {
        if (d && d.success) ui.toast("Opened: " + (d.path || target), "ok");
        else ui.toast("Failed to open: " + ((d && d.error && d.error.message) || "unknown"), "err");
      })
      .catch(function (e) { ui.toast("Failed to open: " + (e.message || e), "err"); });
  }

  function exitApp() {
    ui.modal({
      title: "Exit LlamaMonitor",
      text: "Exit LlamaMonitor?\nMonitoring stops until the application is started again.",
      okLabel: "Exit",
      onDone: function (ok) {
        if (!ok) return;
        var btn = $("btnExitApp");
        btn.disabled = true;
        btn.textContent = "Exiting\u2026";
        api.post("/api/app/exit").then(function () {
          ui.toast("LlamaMonitor is exiting...", "ok");
        }).catch(function () { /* 应用正在退出 */ });
      },
    });
  }

  /* ================= Updates（Phase 13 语义，UI-023 走中央调度器） ================= */

  function renderUpdateStatus(st) {
    if (!st) return;
    updateStatus = st;
    $("updCurrentVersion").textContent = st.current_version || "--";
    $("updInstallMode").textContent = st.installation_mode || "--";
    $("updLastCheck").textContent = st.last_check || "Never";
    $("updLatestVersion").textContent = st.available_version || "--";
    var stEl = $("updStatus");
    stEl.textContent = (st.state || "--") + (st.error ? " - " + st.error : "");
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
      st.state === "CHECKING" ? "Checking..." :
      st.state === "DOWNLOADING" ? "Downloading..." :
      st.state === "VERIFYING" ? "Verifying..." : "Check for Updates";

    var showProgress = st.state === "DOWNLOADING" && st.total_bytes > 0;
    $("updProgressWrap").hidden = !showProgress;
    if (showProgress) {
      $("updProgressBar").style.width = Math.min(100, st.progress_percent || 0) + "%";
      $("updProgressText").textContent =
        F.formatBytes(st.downloaded_bytes) + " / " + F.formatBytes(st.total_bytes) +
        " - " + Math.floor(st.progress_percent || 0) + "% (SHA-256 verified while downloading)";
    }

    var rel = st.release || null;
    var notes = rel && rel.release_notes;
    $("updReleaseNotesWrap").hidden = !notes;
    if (notes) $("updReleaseNotes").textContent = notes; // textContent only（不 innerHTML）

    $("updSignatureNote").textContent =
      st.state === "READY_TO_INSTALL"
        ? "\u2713 Signature verified - SHA-256 verified - ready to install (a pre-update backup is created first)."
        : (st.state === "UPDATE_AVAILABLE" && rel
            ? "Signature verified. Download to verify the installer (SHA-256) before installing."
            : "");

    var note = "";
    if (mode === "development") {
      note = "Update installation is unavailable in development mode (run the EXE / Portable build to install updates).";
    } else if (mode === "portable") {
      note = "Portable build: LlamaMonitor downloads and verifies the ZIP, but never overwrites itself. Use the buttons below to open the download folder or the GitHub release.";
    }
    $("updModeNote").textContent = note;
    $("updPortableActions").hidden = mode !== "portable";

    // 全局 InfoBar（Overview 顶部横幅由 app.js 处理；这里管状态文本即可）
    if (st.state === "UPDATE_AVAILABLE" && st.available_version && LM.app) {
      LM.app.setUpdateBanner(true, "LlamaMonitor " + st.available_version + " is available (current " +
        st.current_version + ").");
    } else if (LM.app) {
      LM.app.setUpdateBanner(false);
    }
  }

  async function loadUpdateStatus() {
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
            ui.toast("Installing " + (d.available_version || "update") + " - LlamaMonitor is closing, the installer will take over...", "ok");
          }
          return loadUpdateStatus();
        })
        .catch(function (e) { ui.toast("Update " + action + " failed: " + (e.message || e), "err"); });
    }
    if (confirmText) {
      ui.modal({
        title: "Install Update",
        text: confirmText,
        okLabel: "Install",
        onDone: function (ok) { if (ok) doIt(); },
      });
    } else {
      doIt();
    }
  }

  /* ================= About ================= */

  async function loadAbout() {
    try {
      var d = await api.get("/api/version");
      $("aboutName").textContent = d.name || "LlamaMonitor";
      $("aboutVersion").textContent = d.version || "--";
      $("aboutSchema").textContent = String(d.schema_version == null ? "--" : d.schema_version);
    } catch (e) {
      $("aboutVersion").textContent = "--";
      $("aboutSchema").textContent = "--";
    }
    try {
      var c = await api.get("/api/config");
      if (c.paths) $("aboutDataDir").textContent = c.paths.database || "--";
    } catch (e) { /* 保留占位 */ }
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
        out.textContent = "Copied";
        setTimeout(function () { out.textContent = ""; }, 2000);
      } catch (e) {
        out.className = "inline-result bad";
        out.textContent = "Copy failed: " + (e.message || e);
      }
    })();
  }

  /* ================= 分区定位（托盘桥 goToSection） ================= */
  var activeSection = "server";

  function goToSection(name) {
    activeSection = name;
    var sec = $("sec-" + name);
    if (!sec) return;
    var content = document.querySelector(".content");
    if (!content) return;
    var top = sec.getBoundingClientRect().top - content.getBoundingClientRect().top + content.scrollTop - 16;
    content.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
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
    bind("btnUpdateCheck", "click", function () { updateAction("check"); });
    bind("btnUpdateDownload", "click", function () { updateAction("download"); });
    bind("btnUpdateInstall", "click", function () {
      var st = updateStatus || {};
      var to = st.available_version || "the new version";
      updateAction("install",
        "Install LlamaMonitor " + to + "?\n\n" +
        "LlamaMonitor will create a pre-update backup (database + config), then exit gracefully " +
        "and the installer will replace the program files. A verified update is required first.");
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
