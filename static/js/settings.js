/* ============================================================
 LlamaMonitor — Settings / About（Round-7 产品化）
 三个状态概念严格分离（§225-§229）：
  - Saved State  = 已保存配置（initialConfig，来自 /api/config）；
  - Draft State  = 用户未保存编辑（表单 DOM + markDirty 跟踪）；
  - Runtime State = 连接状态 / GPU 探测 / 传感器 Provider / 更新状态
    （各自低频或按需刷新，**绝不重渲染表单覆盖 Draft**）。
 表单只加载一次（本机 /api/config）；运行时刷新只写只读展示块。
 ============================================================ */
(function () {
  "use strict";

  var F = LM.fmt, api = LM.api, ui = LM.ui;
  var $ = function (id) { return document.getElementById(id); };

  var settingsLoaded = false;
  var settingsDirty = false;
  var initialConfig = null;     // Saved State（保存成功/加载后更新）
  var lastPaths = null;
  var gpuPickSignature = null;  // GPU detected 列表签名，未变不重建
  var updateStatus = null;
  var lastTestConn = null;      // 最近一次"测试连接"结果（Runtime State）
  var activeSection = "server";
  var remoteMode = false;
  var aboutInfo = null;         // About 缓存（版本/schema/数据目录/安装方式）
  var sysProviderState = null;  // 高级传感器 Provider Runtime State
  var sysSensorsCache = null;   // 传感器列表缓存（重新检测用）

  /* ================= 表单（Draft State） ================= */

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

  /* 默认统计范围：Presentation 映射（后端仍存 daily_default_days 数字，
     用户不感知内部 "7" 语义，§94-§97） */
  var DEFAULT_RANGE_PRESETS = [
    { value: "today", days: 1 },
    { value: "7d", days: 7 },
    { value: "30d", days: 30 },
    { value: "month", days: 62 },
    { value: "all", days: 3650 },
  ];
  function defaultRangeToValue(days) {
    for (var i = 0; i < DEFAULT_RANGE_PRESETS.length; i++) {
      if (DEFAULT_RANGE_PRESETS[i].days === Number(days)) return DEFAULT_RANGE_PRESETS[i].value;
    }
    if (days == null) return "7d";
    if (days <= 1) return "today";
    if (days <= 7) return "7d";
    if (days <= 30) return "30d";
    if (days <= 62) return "month";
    return "all";
  }
  function defaultRangeValueToDays(value) {
    for (var i = 0; i < DEFAULT_RANGE_PRESETS.length; i++) {
      if (DEFAULT_RANGE_PRESETS[i].value === value) return DEFAULT_RANGE_PRESETS[i].days;
    }
    return 7;
  }

  /* 访问范围：Presentation 映射（后端存 web.host；0.0.0.0 不解释为访问地址，§178/§182） */
  function hostToScope(host) {
    var h = (host || "").trim().toLowerCase();
    if (h === "0.0.0.0" || h === "::" || h === "") return "lan";
    if (h === "127.0.0.1" || h === "localhost") return "loopback";
    return "custom";
  }
  function scopeToHost(scope, customHost) {
    if (scope === "loopback") return "127.0.0.1";
    if (scope === "lan") return "0.0.0.0";
    return (customHost || "127.0.0.1").trim() || "127.0.0.1";
  }

  function readForm() {
    var scope = hostToScope($("setWebHost").value);
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
        host: scopeToHost(scope, $("setWebHost").value),
        port: num("setWebPort"),
      },
      database: {
        path: $("setDbPath").value.trim(),
        wal: $("setWal").value === "true",
      },
      ui: {
        refresh_interval_seconds: num("setRefreshInterval"),
        daily_default_days: defaultRangeValueToDays($("setDefaultRange").value),
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
      system: {
        enabled: $("setSysEnabled").checked,
        poll_interval_seconds: num("setSysPoll"),
        history_interval_seconds: num("setSysHistory"),
        history_retention_hours: num("setSysRetention"),
        advanced_sensors: $("setSysAdvanced").checked,
        advanced_sensor_interval_seconds: num("setSysAdvPoll"),
      },
    };
  }

  function fillForm(c) {
    $("setServerUrl").value = (c.llama_server && c.llama_server.url) || "";
    $("setMetricsPath").value = (c.llama_server && c.llama_server.metrics_path) || "";
    $("setTimeoutSec").value = (c.llama_server && c.llama_server.timeout_seconds) || 3;
    $("setPollInterval").value = (c.collector && c.collector.poll_interval_seconds) || 2;
    $("setLiveRetention").value = (c.collector && c.collector.live_retention_hours) || 48;
    $("setGpuEnabled").checked = !!(c.gpu && c.gpu.enabled);
    $("setGpuPoll").value = (c.gpu && c.gpu.poll_interval_seconds) || 2;
    $("setGpuRetention").value = (c.gpu && c.gpu.history_retention_hours) || 48;
    $("setRefreshInterval").value = (c.ui && c.ui.refresh_interval_seconds) || 2;
    $("setDefaultRange").value = defaultRangeToValue(c.ui && c.ui.daily_default_days);
    $("setTheme").value = (c.ui && c.ui.theme) || "system";
    // 访问范围（web.host 经 scope 映射）
    var host = (c.web && c.web.host) || "127.0.0.1";
    var scope = hostToScope(host);
    $("setWebScope").value = scope === "custom" ? "__custom__" : scope;
    $("setWebHost").value = scope === "custom" ? host : host;
    $("webCustomHostRow").style.display = scope === "custom" ? "" : "none";
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
    $("setSysEnabled").checked = !!(c.system && c.system.enabled);
    $("setSysPoll").value = (c.system && c.system.poll_interval_seconds) || 2;
    $("setSysHistory").value = (c.system && c.system.history_interval_seconds) || 2;
    $("setSysRetention").value = (c.system && c.system.history_retention_hours) || 48;
    $("setSysAdvanced").checked = !(c.system && c.system.advanced_sensors === false);
    $("setSysAdvPoll").value = (c.system && c.system.advanced_sensor_interval_seconds) || 2;
    if (c.paths) lastPaths = c.paths;
    updateDbPathHint();
    updateWebHostHint();
    applyFieldDependencyStates();
  }

  /* ================= Dirty（Draft vs Saved） ================= */

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
    if (!hint) return;
    hint.textContent = v ? "当前（自定义）：" + v : "当前：" + (lastPaths ? lastPaths.database : "--");
  }

  /* 访问范围提示（局域网模式 = Amber 安全说明，§181） */
  function updateWebHostHint() {
    var hint = $("webHostHint");
    if (!hint) return;
    var scope = $("setWebScope").value;
    if (scope === "lan") {
      hint.className = "setting-desc lan-note";
      hint.textContent = "局域网中的设备可以访问 LlamaMonitor。远程访问保持只读，设置只能在本机修改。";
    } else if (scope === "loopback") {
      hint.className = "setting-desc";
      hint.textContent = "仅本机访问。";
    } else {
      hint.className = "setting-desc";
      hint.textContent = "自定义网络地址（高级）。";
    }
  }

  /* ================= 输入验证（Inline Error，禁 alert，§35-§37） ================= */

  var VALIDATORS = [
    { id: "setServerUrl", check: function (v) {
        if (!v) return "请输入地址";
        if (!/^(https?):\/\/[^\s/]+(:\d+)?$/i.test(v)) return "格式：http://127.0.0.1:9091（不含路径）";
        return "";
      } },
    { id: "setMetricsPath", check: function (v) {
        if (!v) return "请输入路径";
        if (v.charAt(0) !== "/") return "路径以 / 开头，例如 /metrics";
        return "";
      } },
    { id: "setTimeoutSec", check: function (v) { if (v < 1 || v > 30) return "范围 1~30 秒"; return ""; } },
    { id: "setPollInterval", check: function (v) { if (v < 1 || v > 3600) return "范围 1~3600 秒"; return ""; } },
    { id: "setLiveRetention", check: function (v) { if (v < 1 || v > 8760) return "范围 1~8760 小时"; return ""; } },
    { id: "setGpuPoll", check: function (v) { if (v < 1 || v > 3600) return "范围 1~3600 秒"; return ""; } },
    { id: "setGpuRetention", check: function (v) { if (v < 1 || v > 8760) return "范围 1~8760 小时"; return ""; } },
    { id: "setRefreshInterval", check: function (v) { if (v < 1 || v > 3600) return "范围 1~3600 秒"; return ""; } },
    { id: "setSysPoll", check: function (v) { if (v < 1 || v > 3600) return "范围 1~3600 秒"; return ""; } },
    { id: "setSysHistory", check: function (v) { if (v < 1 || v > 3600) return "范围 1~3600 秒"; return ""; } },
    { id: "setSysRetention", check: function (v) { if (v < 1 || v > 8760) return "范围 1~8760 小时"; return ""; } },
    { id: "setSysAdvPoll", check: function (v) { if (v < 1 || v > 3600) return "范围 1~3600 秒"; return ""; } },
    { id: "setBackupInterval", check: function (v) { if (v < 1 || v > 8760) return "范围 1~8760 小时"; return ""; } },
    { id: "setBackupKeep", check: function (v) { if (v < 1 || v > 365) return "范围 1~365 份"; return ""; } },
    { id: "setUpdateInterval", check: function (v) { if (v < 1 || v > 8760) return "范围 1~8760 小时"; return ""; } },
    { id: "setMaxLogSize", check: function (v) { if (v < 1 || v > 1024) return "范围 1~1024 MB"; return ""; } },
    { id: "setLogBackupCount", check: function (v) { if (v < 1 || v > 100) return "范围 1~100 个"; return ""; } },
    { id: "setWebPort", check: function (v) {
        if (!/^\d+$/.test(String(v)) || v < 1024 || v > 65535) return "范围 1024~65535";
        return "";
      } },
    { id: "setWebHost", check: function (v) {
        var scope = $("setWebScope").value;
        if (scope !== "__custom__") return "";
        if (!/^[a-zA-Z0-9\.\*:\[\]]+$/.test(v)) return "地址格式无效";
        if (v.indexOf(".") === -1 && v.indexOf(":") === -1) return "请输入合法地址";
        return "";
      } },
  ];

  function validateField(id) {
    var cfg = null;
    for (var i = 0; i < VALIDATORS.length; i++) if (VALIDATORS[i].id === id) cfg = VALIDATORS[i];
    if (!cfg) return "";
    var el = $(id);
    var raw = el.value.trim();
    var val = raw === "" ? NaN : Number(raw);
    var msg;
    if (cfg.id === "setServerUrl" || cfg.id === "setMetricsPath" || cfg.id === "setWebHost") {
      if (raw === "" && cfg.id !== "setWebHost") return cfg.check(raw);
      msg = cfg.check(raw);
    } else {
      if (isNaN(val)) return "请输入数字";
      msg = cfg.check(val);
    }
    var errEl = $(id + "Err");
    if (errEl) {
      errEl.textContent = msg;
      errEl.hidden = !msg;
    }
    el.classList.toggle("input-invalid", !!msg);
    return msg;
  }

  function validateAll() {
    var bad = 0;
    for (var i = 0; i < VALIDATORS.length; i++) {
      if (validateField(VALIDATORS[i].id)) bad++;
    }
    return bad === 0;
  }

  function revalidate() {
    markDirty();
    var ok = validateAll();
    var saveBtn = $("btnSaveSettings");
    if (saveBtn) saveBtn.disabled = !settingsDirty || !ok;
  }

  /* ================= 依赖项启停（§101/§126-§129/§151/§205/§207） ================= */

  function applyFieldDependencyStates() {
    // GPU：关闭 -> 采样间隔/历史保留/选择 disabled
    var gpuOff = !$("setGpuEnabled").checked;
    ["setGpuPoll", "setGpuRetention"].forEach(function (id) {
      var el = $(id);
      el.disabled = gpuOff || remoteMode;
      var sfx = el.closest(".input-suffix");
      if (sfx) sfx.classList.toggle("suffix-disabled", gpuOff);
    });
    var gpuList = $("gpuDeviceList");
    if (gpuList) gpuList.classList.toggle("control-disabled", gpuOff);

    // 系统：关闭 -> 基础参数 disabled；高级关闭 -> 高级间隔 disabled
    var sysOff = !$("setSysEnabled").checked;
    ["setSysPoll", "setSysHistory", "setSysRetention"].forEach(function (id) {
      var el = $(id);
      el.disabled = sysOff || remoteMode;
      var sfx = el.closest(".input-suffix");
      if (sfx) sfx.classList.toggle("suffix-disabled", sysOff);
    });
    var advOff = !$("setSysAdvanced").checked;
    var advProvider = sysProviderState && sysProviderState === "unavailable";
    $("setSysAdvanced").disabled = remoteMode || advProvider;
    $("setSysAdvPoll").disabled = sysOff || advOff || remoteMode;
    var advSfx = $("setSysAdvPoll").closest(".input-suffix");
    if (advSfx) advSfx.classList.toggle("suffix-disabled", sysOff || advOff);
    if (advProvider) {
      $("setSysAdvanced").title = "硬件传感器提供程序不可用。";
    } else {
      $("setSysAdvanced").title = "";
    }

    // 备份：关闭自动备份 -> 备份间隔 disabled（保留数量仍可编辑，§152）
    var backupOff = !$("setBackupAuto").checked;
    $("setBackupInterval").disabled = backupOff || remoteMode;
    var biSfx = $("setBackupInterval").closest(".input-suffix");
    if (biSfx) biSfx.classList.toggle("suffix-disabled", backupOff);

    // 更新：关闭自动检查 -> 检查间隔/自动下载 disabled（无自动安装）
    var updOff = !$("setUpdatesEnabled").checked;
    $("setUpdateInterval").disabled = updOff || remoteMode;
    $("setUpdateAutoDownload").disabled = updOff || remoteMode;
    var uiSfx = $("setUpdateInterval").closest(".input-suffix");
    if (uiSfx) uiSfx.classList.toggle("suffix-disabled", updOff);
  }

  /* ================= 加载（表单一次，Remote 只读） ================= */

  function setRemoteReadOnly() {
    remoteMode = true;
    var pane = document.querySelector(".settings-pane");
    if (pane) {
      pane.querySelectorAll("input, select").forEach(function (el) { el.disabled = true; });
    }
    // 远程隐藏：保存 / 恢复默认 / 危险操作（§55）
    var saveBtn = $("btnSaveSettings"); if (saveBtn) saveBtn.hidden = true;
    var resetBtn = $("btnResetDefaults"); if (resetBtn) resetBtn.hidden = true;
    var danger = $("sec-danger"); if (danger) danger.hidden = true;
  }

  async function loadSettings() {
    if (settingsLoaded) return;
    // /api/config 读写都 loopback-only。远程（局域网 IP）客户端是只读端
    if (!api.isLocal()) {
      settingsLoaded = true;
      settingsDirty = false;
      remoteMode = true;
      updateSettingsFooter();
      var note = document.createElement("p");
      note.className = "settings-desc remote-note";
      note.id = "remoteReadOnlyNote";
      note.textContent = "当前为远程只读模式：设置仅能在运行 LlamaMonitor 的电脑上修改（本机 127.0.0.1 访问）。";
      var first = document.querySelector(".settings-pane .settings-card");
      if (first) {
        var oldNote = document.getElementById("remoteReadOnlyNote");
        if (oldNote) oldNote.remove();
        first.insertBefore(note, first.firstChild);
      }
      await loadGpuDetected();
      setRemoteReadOnly();
      return;
    }
    try {
      await loadGpuDetected(); // 先拿 GPU 探测结果，fillForm 才能渲染勾选框
      var c = await api.get("/api/config");
      lastConfigUrl = (c.llama_server && c.llama_server.url) || "";
      fillForm(c);
      initialConfig = readForm();
      settingsDirty = false;
      settingsLoaded = true;
      updateSettingsFooter();
      validateAll();
    } catch (e) {
      ui.toast("加载设置失败：" + (e.message || e), "err");
      settingsLoaded = true;
    }
  }

  async function saveSettings() {
    var btn = $("btnSaveSettings");
    if (!validateAll()) {
      ui.toast("请先修正表单中标红的字段。", "err");
      return;
    }
    btn.disabled = true;
    var oldLabel = btn.textContent;
    btn.textContent = "保存中...";
    try {
      var data = await api.put("/api/config", readForm());
      if (data && data.success) {
        initialConfig = readForm();
        settingsDirty = false;
        // §50/§51：Toast 按生效方式区分（主题立即生效；其余需重启）
        ui.toast(data.restart_required
          ? "设置已保存，部分更改将在重启 LlamaMonitor 后生效。"
          : "设置已保存", "ok");
      } else {
        ui.toast("保存失败：" + ((data && data.error && data.error.message) || "配置无效"), "err");
      }
    } catch (e) {
      ui.toast("保存失败：" + (e.message || e), "err");
    }
    btn.textContent = oldLabel;
    updateSettingsFooter();
  }

  /* §44：恢复**本页**默认（不重置整个程序所有分类） */
  async function resetToPageDefaults() {
    var btn = $("btnResetDefaults");
    btn.disabled = true;
    try {
      var d = await api.get("/api/config/defaults");
      var section = activeSection;
      fillForm(d);
      // 只恢复当前分类对应字段（保持 defaults），其他分类从 Saved State 还原
      applySectionDraft(section);
      if (section === "appearance") {
        if (LM.app && LM.app.applyTheme) LM.app.applyTheme(d.ui.theme); // 主题即时预览
      }
      markDirty();
      validateAll();
      updateSettingsFooter();
      ui.toast("已恢复本页默认值。点击「保存更改」生效。", "warn");
    } catch (e) {
      ui.toast("加载默认值失败：" + (e.message || e), "err");
    }
    btn.disabled = false;
  }

  /* 分类 -> 表单字段（id -> {g:后端配置分组, k:组内键}）。
     恢复本页默认（§44）：当前分类字段保持 defaults（fillForm 已写入），
     其余分类字段从 initialConfig（Saved State）还原。
     注意分类名与后端字段组名不同（server/llama_server、appearance/ui、
     data/database、application/web），必须用此映射而非直接比较分类名。
     数值/文本字段 k 为组内键；特殊字段（主题/范围/勾选/地址）用 resolveFieldValue。 */
  var SECTION_FIELDS = {
    server: { setServerUrl: { g: "llama_server", k: "url" }, setMetricsPath: { g: "llama_server", k: "metrics_path" }, setTimeoutSec: { g: "llama_server", k: "timeout_seconds" } },
    collector: { setPollInterval: { g: "collector", k: "poll_interval_seconds" }, setLiveRetention: { g: "collector", k: "live_retention_hours" } },
    appearance: { setRefreshInterval: { g: "ui", k: "refresh_interval_seconds" }, setDefaultRange: { g: "ui", k: "daily_default_days" }, setTheme: { g: "ui", k: "theme" } },
    gpu: { setGpuEnabled: { g: "gpu", k: "enabled" }, setGpuPoll: { g: "gpu", k: "poll_interval_seconds" }, setGpuRetention: { g: "gpu", k: "history_retention_hours" } },
    system: { setSysEnabled: { g: "system", k: "enabled" }, setSysPoll: { g: "system", k: "poll_interval_seconds" }, setSysHistory: { g: "system", k: "history_interval_seconds" }, setSysRetention: { g: "system", k: "history_retention_hours" }, setSysAdvanced: { g: "system", k: "advanced_sensors" }, setSysAdvPoll: { g: "system", k: "advanced_sensor_interval_seconds" } },
    data: { setDbPath: { g: "database", k: "path" }, setWal: { g: "database", k: "wal" }, setLogLevel: { g: "logging", k: "level" }, setMaxLogSize: { g: "logging", k: "max_size_mb" }, setLogBackupCount: { g: "logging", k: "backup_count" }, setBackupAuto: { g: "backup", k: "automatic" }, setBackupInterval: { g: "backup", k: "interval_hours" }, setBackupKeep: { g: "backup", k: "keep_count" } },
    application: { setWebHost: { g: "web", k: "host" }, setWebPort: { g: "web", k: "port" } },
    updates: { setUpdatesEnabled: { g: "updates", k: "check_enabled" }, setUpdateInterval: { g: "updates", k: "check_interval_hours" }, setUpdateAutoDownload: { g: "updates", k: "auto_download" } }
  };

  /* 从 initialConfig（Saved State）读取某表单字段的原始值并写回 DOM */
  function restoreField(id) {
    var el = $(id);
    if (!el) return;
    for (var sec in SECTION_FIELDS) {
      if (SECTION_FIELDS[sec][id]) {
        var spec = SECTION_FIELDS[sec][id];
        var grp = initialConfig[spec.g];
        if (!grp) return;
        var raw = grp[spec.k];
        switch (id) {
          case "setDefaultRange": el.value = defaultRangeToValue(raw); break;
          case "setTheme": el.value = raw; break;
          case "setWal": el.value = String(!!raw); break;
          case "setWebHost": el.value = raw; break;
          case "setGpuEnabled": el.checked = !!raw; break;
          case "setBackupAuto": el.checked = !!raw; break;
          case "setUpdatesEnabled": el.checked = !!raw; break;
          case "setUpdateAutoDownload": el.checked = !!raw; break;
          case "setSysEnabled": el.checked = !!raw; break;
          case "setSysAdvanced": el.checked = !(raw === false); break;
          default: el.value = raw; break;
        }
        return;
      }
    }
  }

  function applySectionDraft(section) {
    if (!initialConfig) return;
    // fillForm(defaults) 已把全部字段写成默认值；当前分类保持默认，
    // 其余分类字段从 initialConfig（Saved State）还原 -> "只恢复本页"。
    Object.keys(SECTION_FIELDS).forEach(function (sec) {
      if (sec === section) return;
      Object.keys(SECTION_FIELDS[sec]).forEach(restoreField);
    });
    // 主题即时预览（仅当外观不是当前分类时还原 saved 主题；当前分类保持默认）
    if (section !== "appearance" && initialConfig.ui) {
      var themeEl = $("setTheme");
      if (themeEl) themeEl.value = initialConfig.ui.theme;
    }
    if (initialConfig.web) syncWebScopeFromHost(initialConfig.web.host);
    updateDbPathHint();
    updateWebHostHint();
    applyFieldDependencyStates();
  }

  function syncWebScopeFromHost(host) {
    var scope = hostToScope(host);
    $("setWebScope").value = scope === "custom" ? "__custom__" : scope;
    $("webCustomHostRow").style.display = scope === "custom" ? "" : "none";
  }

  /* ================= 测试连接（用当前表单值，不保存，§77） ================= */

  async function testConnection() {
    var btn = $("btnTestConn");
    var out = $("testConnResult");
    // 先验证表单字段（不保存）
    var urlOk = !validateField("setServerUrl");
    var pathOk = !validateField("setMetricsPath");
    var toOk = !validateField("setTimeoutSec");
    if (!urlOk || !pathOk || !toOk) {
      out.className = "inline-result bad";
      out.textContent = "请先修正表单中标红的字段。";
      return;
    }
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
        // §73 成功：连接成功 + Metrics 可用（不虚构延迟之外的结论；延迟是真实测量）
        out.className = "inline-result ok";
        out.textContent = "连接成功 · Metrics 可用 · 延迟 " + data.latency_ms + " ms"
          + (data.metrics_detected ? "" : "（未检测到 llama.cpp 指标）");
        ui.toast("连接成功：" + data.latency_ms + " ms", "ok");
        lastTestConn = { at: Date.now(), ok: true, latencyMs: data.latency_ms, metricsOk: true };
      } else {
        // §74-§76 失败分别表达：无法连接 / Metrics 返回 404 / 请求超时
        out.className = "inline-result bad";
        out.textContent = "失败：" + describeTestFailure(data);
        ui.toast("连接失败：" + describeTestFailure(data), "err");
        lastTestConn = { at: Date.now(), ok: false, error: describeTestFailure(data) };
      }
    } catch (e) {
      out.className = "inline-result bad";
      var msg = describeTestFailure({ error: e.message || e });
      out.textContent = "失败：" + msg;
      ui.toast("连接失败：" + msg, "err");
      lastTestConn = { at: Date.now(), ok: false, error: msg };
    }
    btn.textContent = oldLabel;
    btn.disabled = false;
    updateServerConnStatus();
  }

  /* §74-§76：失败原因分别表达（不只显示「连接失败」） */
  function describeTestFailure(data) {
    var http = data && data.http_status;
    if (http === 404) return "无法连接 llama-server：Metrics 返回 404";
    if (http) return "无法连接 llama-server：Metrics 返回 HTTP " + http;
    var err = String((data && data.error) || "");
    if (/timeout|超时/i.test(err)) return "请求超时";
    if (/refused|connect/i.test(err)) return "无法连接 llama-server（连接被拒绝）";
    if (/url/i.test(err)) return "无效的 URL";
    return err || "无法连接 llama-server";
  }

  /* ================= GPU 探测（Runtime State，绝不覆盖 Draft） ================= */

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
    _lastDetected = detected;
    renderGpuDetected(detected, readGpuUuids(), available);
  }

  /* §103-§112：标准 Device List（checkbox + 名称 / UUID Secondary Monospace 可复制）
     按 UUID 保存（§105）；重新检测不改变当前选择（§108）。 */
  function renderGpuDetected(detected, selectedUuids, available) {
    var box = $("gpuDetected");
    var sig = JSON.stringify((detected || []).map(function (g) { return g.uuid + "|" + g.index + "|" + (g.name || ""); }));
    if (gpuPickSignature === sig && box.children.length) return; // 签名未变不重建（保留勾选/焦点）
    gpuPickSignature = sig;
    box.innerHTML = "";
    var countEl = $("gpuDeviceCount");
    var emptyHint = $("gpuEmptyHint");
    if (!available) {
      var note = document.createElement("span");
      note.className = "na";
      note.textContent = "未检测到 GPU（nvidia-smi 不可用）。";
      box.appendChild(note);
      if (countEl) countEl.textContent = "已检测 0 张";
      return;
    }
    if (!detected || !detected.length) {
      var none = document.createElement("span");
      none.className = "na";
      none.textContent = "未检测到 GPU。";
      box.appendChild(none);
      if (countEl) countEl.textContent = "已检测 0 张";
      return;
    }
    detected.forEach(function (g) {
      var row = document.createElement("div");
      row.className = "gpu-detected-row";
      var label = document.createElement("label");
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = g.uuid;
      cb.checked = selectedUuids.indexOf(g.uuid) !== -1;
      cb.addEventListener("change", function () { markDirty(); updateGpuCount(detected); updateGpuEmptyHint(); });
      label.appendChild(cb);
      label.appendChild(document.createTextNode(
        " GPU " + (g.index == null ? "?" : g.index) + " · " + (g.name || "未知型号")));
      row.appendChild(label);
      if (g.uuid) {
        var uuid = document.createElement("div");
        uuid.className = "gpu-row-uuid";
        uuid.textContent = g.uuid;
        uuid.title = g.uuid + "（点击复制）";
        uuid.addEventListener("click", function (e) {
          e.stopPropagation();
          copyText(g.uuid, "UUID 已复制");
        });
        row.appendChild(uuid);
      }
      box.appendChild(row);
    });
    updateGpuCount(detected);
    updateGpuEmptyHint();
  }

  function updateGpuCount(detected) {
    var countEl = $("gpuDeviceCount");
    if (!countEl) return;
    detected = detected || [];
    var box = $("gpuDetected");
    var sel = box ? box.querySelectorAll("input[type=checkbox]:checked").length : 0;
    countEl.textContent = "已检测 " + detected.length + " 张 · 已监控 " + (sel === 0 ? "全部（" + detected.length + "）" : sel) + " 张";
  }

  function updateGpuEmptyHint() {
    var hint = $("gpuEmptyHint");
    var box = $("gpuDetected");
    if (!hint || !box) return;
    var any = box.querySelectorAll("input[type=checkbox]").length;
    var sel = box.querySelectorAll("input[type=checkbox]:checked").length;
    hint.hidden = !(any && sel === 0);
  }

  /* ================= 系统 Provider 状态（Runtime State） ================= */

  function applySysProviderRuntime(state) {
    sysProviderState = state;
    var adv = $("sysMonAdvState");
    var sub = $("sysMonStateSub");
    var box = $("sysMonState");
    if (adv) {
      if (state === "available") adv.textContent = "可用";
      else if (state === "partial") adv.textContent = "部分可用";
      else adv.textContent = "不可用";
    }
    if (box) {
      box.classList.remove("ok", "warn", "bad");
      if (state === "available") box.classList.add("ok");
      else if (state === "partial") box.classList.add("warn");
      else box.classList.add("bad");
    }
    if (sub) sub.textContent = state === "unavailable" ? "硬件传感器提供程序不可用。基础系统监控（CPU/内存/磁盘/网络）继续正常。" : "";
    applyFieldDependencyStates();
  }

  function refreshSysSensorsSettings() {
    // 复用 LM.system.refreshSensors（系统页同源数据，单次 fetch）：
    // 它内部写 sysMonState 新结构（ok/warn/bad + 高级硬件传感器 值）与
    // sysMonSensorList 摘要（已检测 N 个传感器 + 分类 tooltip）。
    // 这里只同步 Provider Runtime State 到依赖开关（不覆盖 Draft）。
    if (LM.system && LM.system.refreshSensors) {
      LM.system.refreshSensors().then(function () {
        applySysProviderRuntime(LM.system.sensorState());
      }).catch(function () {
        applySysProviderRuntime("unavailable");
      });
    }
  }

  /* ================= Data Management ================= */

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

  /* §154：数据库状态中文四态（不出现 raw incompatible） */
  function dbStatusDisplay(health, detail) {
    var map = {
      healthy: ["正常", "ok"],
      incompatible: ["只读兼容模式", "warn"],
      warning: ["只读", "warn"],
      corrupt: ["异常", "bad"],
      unavailable: ["异常", "bad"],
    };
    var m = map[health] || ["--", ""];
    var el;
    return { text: m[0], tone: m[1], detail: detail || "" };
  }

  async function refreshDataInfo() {
    var box = $("dataInfo");
    if (!box) return;
    box.innerHTML = "";
    try {
      var d = await api.get("/api/data/info");
      // 数据库状态（四态中文）
      var st = dbStatusDisplay(d.database_health, d.database_health_detail);
      var statusRow = document.createElement("span");
      statusRow.className = "v db-status";
      statusRow.setAttribute("data-tone", st.tone);
      statusRow.textContent = st.text;
      box.appendChild(document.createElement("span")).className = "k";
      box.lastChild.textContent = "数据库状态";
      box.appendChild(statusRow);
      kvRow(box, "数据库文件", d.database_path);
      kvRow(box, "数据库大小", F.formatBytes(d.database_size_bytes));
      kvRow(box, "累计记录天数", d.recorded_days);
      // 最近备份（可靠获取时显示相对时间，§148；不可靠不伪造）
      if (d.last_auto_backup && d.last_auto_backup.timestamp) {
        var ts = Date.parse(d.last_auto_backup.timestamp);
        if (!isNaN(ts)) {
          var ago = Math.max(0, (Date.now() - ts) / 1000);
          var when = new Date(ts);
          kvRow(box, "最近自动备份",
            (when.getMonth() + 1) + "/" + when.getDate() + " " + F.pad2(when.getHours()) + ":" + F.pad2(when.getMinutes()) +
            "（" + F.formatAgo(ago) + "）");
        }
      }
      // 最近备份（设置页备份 Group 运行时值）
      var bi = $("backupLastInfo");
      if (bi) {
        if (d.last_auto_backup && d.last_auto_backup.timestamp) {
          var ts2 = Date.parse(d.last_auto_backup.timestamp);
          if (!isNaN(ts2)) {
            var ago2 = Math.max(0, (Date.now() - ts2) / 1000);
            var when2 = new Date(ts2);
            bi.textContent = (when2.getMonth() + 1) + "/" + when2.getDate() + " " + F.pad2(when2.getHours()) + ":" + F.pad2(when2.getMinutes()) + "（" + F.formatAgo(ago2) + "）";
          }
        } else {
          bi.textContent = "暂无";
        }
      }
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
        // §156：备份完成 Toast + 打开备份目录
        ui.toast("备份完成：" + data.file, "ok");
        var toastEl = document.querySelector(".toast.ok:last-child");
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

  /* §162：清空短期历史确认 Dialog 说明删除范围（真实 Audit：live_samples + gpu_samples，
     保留 daily_usage/gpu_daily/state 基线） */
  function clearLive() {
    ui.modal({
      title: "清空短期历史",
      text: "将永久删除：\n· llama.cpp 实时采样历史（live_samples）\n· GPU 实时采样历史（gpu_samples）\n\n保留：每日统计汇总、GPU 每日统计、计数器基线（不会重复累计）。",
      okLabel: "清空",
      danger: true,
      onDone: function (ok) { if (ok) doClearLive(); },
    });
  }

  async function doClearLive() {
    var btn = $("btnClearLive");
    btn.disabled = true;
    var old = btn.textContent;
    btn.textContent = "清空中...";
    try {
      var data = await api.post("/api/data/clear-live", { confirm: true });
      if (data.success) {
        ui.toast("短期历史已清空（" + data.deleted + " 条样本）。", "ok");
        refreshDataInfo();
        if (LM.app) {
          LM.app.refreshLiveNow();
          LM.app.refreshDailyNow();
          LM.app.refreshSummaryNow();
          LM.app.refreshDataQualityNow();
          LM.app.refreshEventsNow();
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

  /* §163/§164/§167：重置统计确认 Dialog 说明删除范围 + 保留基线 + 输入 RESET 二次确认 */
  function resetStats() {
    ui.modal({
      title: "重置统计数据",
      text: "将永久删除：\n· Token 用量每日聚合（daily_usage）\n· GPU 每日统计（gpu_daily）\n· MTP 统计（mtp_position_daily）\n· 实时采样（llama.cpp + GPU）\n· 采集缺口记录（data_gaps）\n\n保留：配置、监控事件、计数器基线（计数从当前会话继续，不会重复累计）。",
      okLabel: "重置",
      danger: true,
      needsInput: true,
      inputValue: "RESET",
      onDone: function (ok) { if (ok) doResetStats(); },
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
          LM.app.refreshLiveNow();
          LM.app.refreshDailyNow();
          LM.app.refreshSummaryNow();
          LM.app.refreshMtpNow();
          LM.app.refreshDataQualityNow();
          LM.app.refreshEventsNow();
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

  /* §157-§158：检查数据库仅用户主动点击（不自动 integrity_check） */
  async function runDbCheck() {
    var out = $("dbCheckResult");
    var btn = $("btnRunCheck");
    btn.disabled = true;
    var old = btn.textContent;
    btn.textContent = "检查中…";
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

  /* ================= Application ================= */

  function fmtUptime(sec) {
    sec = Math.max(0, sec | 0);
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    if (h) return h + "h " + m + "m";
    if (m) return m + "m " + s + "s";
    return s + "s";
  }

  async function loadAppIntegration() {
    var box = $("appInfo");
    if (!box) return;
    box.innerHTML = "";
    if (!api.isLocal()) {
      kvRow(box, "状态", "远程只读：本机信息仅在本机可见");
      return;
    }
    try {
      var d = await api.get("/api/app/integration");
      kvRow(box, "运行模式", d.background ? "后台（系统托盘）" : "前台");
      kvRow(box, "系统托盘", d.tray_supported ? "可用" : "不可用");
      kvRow(box, "单实例运行", d.single_instance ? "启用" : "禁用");
      kvRow(box, "运行环境", (d.platform || "未知") + (d.frozen ? "（EXE）" : "（开发）"));
      kvRow(box, "应用数据目录", d.app_data || "\u2014");
      if (d.uptime_seconds != null) kvRow(box, "运行时长", fmtUptime(d.uptime_seconds));
      renderAutostart(d.autostart || {});
    } catch (e) {
      kvRow(box, "状态", "加载失败：" + (e.message || e));
    }
  }

  /* §173-§174：Switch 自身表达状态（不显示「已启用」占行）；异常时「启动项需要修复」 */
  function renderAutostart(a) {
    var cb = $("setAutostart");
    var status = $("autostartStatus");
    var cmdBox = $("autostartCommand");
    var repairRow = $("autostartRepairRow");
    if (!a.supported) {
      cb.checked = false;
      cb.disabled = true;
      status.textContent = "当前环境不支持（需要 EXE 构建）。";
      status.style.color = "";
      cmdBox.textContent = "\u2014";
      repairRow.style.display = "none";
      return;
    }
    cb.checked = !!a.enabled;
    cb.disabled = remoteMode;
    if (a.stale) {
      status.textContent = "启动项需要修复";
      status.style.color = "var(--warning)";
      repairRow.style.display = "";
    } else {
      status.textContent = "";
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

  /* §193-§196：退出确认（正在后台采集时说明会停止监控） */
  function exitApp() {
    ui.modal({
      title: "退出 LlamaMonitor",
      text: "退出后将停止后台监控，直到再次启动应用。确定退出？",
      okLabel: "退出",
      danger: true,
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

  /* ================= Updates（状态机 Runtime State） ================= */

  function renderUpdateStatus(st) {
    if (!st) return;
    updateStatus = st;
    $("updCurrentVersion").textContent = st.current_version || "--";
    // 安装方式：可靠判断才显示（注册表检测，development 不显示给用户）
    var modeMap = { "installed": "安装版", "portable": "便携版", "development": "开发模式" };
    $("updInstallMode").textContent = modeMap[st.installation_mode] || st.installation_mode || "--";
    // 上次检查（从未 -> 从未）
    $("updLastCheck").textContent = st.last_check ? st.last_check : "从未";
    // 最新版本（从未检查 -> —）
    $("updLatestVersion").textContent = st.available_version || "—";
    var stEl = $("updStatus");
    var _stMap = { "UP_TO_DATE": "已是最新版本", "UPDATE_AVAILABLE": "发现新版本", "CHECKING": "正在检查", "DOWNLOADING": "下载中", "VERIFYING": "正在验证", "READY_TO_INSTALL": "等待安装", "INSTALLING": "安装中", "ERROR": "错误", "IDLE": "未检查" };
    stEl.textContent = (_stMap[st.state] || st.state || "--") + (st.error ? " - " + st.error : "");
    stEl.className = "update-state" +
      (st.state === "ERROR" ? " error" : st.state === "UPDATE_AVAILABLE" ? " available" : "");

    var mode = st.installation_mode;
    var busy = ["CHECKING", "DOWNLOADING", "VERIFYING", "INSTALLING"].indexOf(st.state) !== -1;
    // §210-§215 按钮状态机
    $("btnUpdateCheck").disabled = busy;
    $("btnUpdateDownload").disabled = busy || st.state !== "UPDATE_AVAILABLE" || mode === "development";
    $("btnUpdateInstall").disabled = busy || st.state !== "READY_TO_INSTALL" ||
      mode === "development" || mode === "portable";
    $("btnUpdateCancel").hidden = st.state !== "DOWNLOADING";
    $("btnUpdateCheck").textContent =
      st.state === "CHECKING" ? "正在检查…" :
      st.state === "DOWNLOADING" ? "正在下载…" :
      st.state === "VERIFYING" ? "正在验证…" : "检查更新";

    var showProgress = st.state === "DOWNLOADING" && st.total_bytes > 0;
    $("updProgressWrap").hidden = !showProgress;
    if (showProgress) {
      $("updProgressBar").style.width = Math.min(100, st.progress_percent || 0) + "%";
      $("updProgressText").textContent =
        F.formatBytes(st.downloaded_bytes) + " / " + F.formatBytes(st.total_bytes) +
        " - " + Math.floor(st.progress_percent || 0) + "%（下载时已做 SHA-256 校验）";
    }

    var rel = st.release || null;
    // §220-§221：发现新版显示版本号/发布时间/Release Notes（textContent only，安全）
    if (rel && st.state === "UPDATE_AVAILABLE") {
      var notes = [];
      if (rel.version) notes.push("版本 " + rel.version);
      if (rel.published_at) notes.push("发布 " + rel.published_at);
      $("updReleaseNotesWrap").hidden = !rel.release_notes;
      if (rel.release_notes) $("updReleaseNotes").textContent = rel.release_notes;
      if (notes.length) {
        var relNote = document.createElement("div");
        relNote.className = "release-meta";
        relNote.textContent = notes.join(" · ");
        var wrap = $("updReleaseNotesWrap");
        if (rel.release_notes) wrap.insertBefore(relNote, wrap.firstChild);
      }
    } else {
      $("updReleaseNotesWrap").hidden = true;
    }

    $("updSignatureNote").textContent =
      st.state === "READY_TO_INSTALL"
        ? "✓ Ed25519 签名已验证，SHA-256 完整性校验通过，已准备安装（会先创建更新前备份）。"
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
    // GitHub Releases 外链（真实 URL，noopener noreferrer）
    if (rel && rel.release_url) {
      var relBtn = $("btnUpdateOpenRelease");
      if (relBtn) relBtn.dataset.url = rel.release_url;
    }

  }

  async function loadUpdateStatus() {
    if (!api.isLocal()) return;
    try {
      var st = await api.get("/api/update/status");
      renderUpdateStatus(st);
    } catch (e) {
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

  /* ================= About（唯一来源 /api/version） ================= */

  async function loadAbout() {
    try { var lg = $("aboutLogo"); if (lg && LM.icons && !lg.innerHTML) lg.innerHTML = LM.icons.brand(); } catch (e) {}
    var ver = "--";
    try {
      var d = await api.get("/api/version");
      ver = d.version || "--";
      $("aboutSchema").textContent = String(d.schema_version == null ? "--" : d.schema_version);
    } catch (e) {
      $("aboutSchema").textContent = "--";
    }
    $("aboutVersion").textContent = ver;
    var row = $("aboutVersionRow"); if (row) row.textContent = ver;
    // 数据目录（本机路径，远程不发 /api/config）
    var dataDir = "";
    if (api.isLocal()) {
      try {
        var c = await api.get("/api/config");
        dataDir = (c.paths && c.paths.database) || "";
        if (dataDir) {
          // 数据目录 = 数据库文件所在目录（§240）
          var idx = Math.max(dataDir.lastIndexOf("\\"), dataDir.lastIndexOf("/"));
          var dir = idx > 0 ? dataDir.substring(0, idx) : dataDir;
          var fn = idx > 0 ? dataDir.substring(idx + 1) : "";
          $("aboutDataDir").textContent = dir;
          $("aboutDataDir").title = "数据库文件：" + fn;
        }
      } catch (e) { /* 保留占位 */ }
    } else {
      $("aboutDataDir").textContent = "本机路径（远程只读）";
    }
    aboutInfo = { version: ver, dataDir: dataDir };
    // 安装方式（可靠判断才显示，§245-246；development 不显示）
    if (api.isLocal()) {
      try {
        var st = await api.get("/api/update/status");
        if (st && st.installation_mode && st.installation_mode !== "development") {
          var mm = { "installed": "安装版", "portable": "便携版" };
          $("aboutInstallMode").textContent = mm[st.installation_mode] || st.installation_mode;
          $("aboutInstallModeKey").hidden = false;
          $("aboutInstallMode").hidden = false;
        }
      } catch (e) { /* 不显示安装方式 */ }
    }
  }

  function copyText(text, successMsg) {
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
          document.removeEventListener("copy", function () {});
          document.body.removeChild(ta);
        }
        if (successMsg) ui.toast(successMsg, "ok");
        return true;
      } catch (e) {
        ui.toast("复制失败：" + (e.message || e), "err");
        return false;
      }
    })();
  }

  /* §249-§253：复制诊断信息（不复制 Prompt/模型路径/API Key 等敏感信息，§252；
     远程模式只复制 Version/Platform/Schema/Status，不复制本地文件路径，§291） */
  async function copyDiagInfo() {
    var out = $("aboutCopyResult");
    var lines = ["LlamaMonitor " + (aboutInfo ? aboutInfo.version : $("aboutVersion").textContent)];
    lines.push("Platform: Windows x64");
    lines.push("Database Schema: " + $("aboutSchema").textContent);
    var remote = !api.isLocal();
    if (!remote && aboutInfo && aboutInfo.dataDir) {
      lines.push("Data Directory: " + aboutInfo.dataDir);
    }
    if (api.isLocal()) {
      try {
        var di = await api.get("/api/data/info");
        var st = dbStatusDisplay(di.database_health);
        lines.push("Database Status: " + st.text);
        lines.push("llama-server Address: " + (lastConfigUrl || "未配置"));
      } catch (e) { /* 跳过 */ }
      try {
        var gs = await api.get("/api/gpu/status");
        lines.push("GPU Count: " + ((gs.detected || []).length));
      } catch (e) { /* 跳过 */ }
      try {
        var ss = await api.get("/api/system/sensors");
        var pstate = ss.state || (ss.available ? "available" : "unavailable");
        lines.push("System Monitoring Provider: " + (pstate === "available" ? "可用" : pstate === "partial" ? "部分可用" : "不可用"));
      } catch (e) { /* 跳过 */ }
    } else {
      lines.push("Mode: 远程只读");
    }
    var ok = await copyText(lines.join("\n"), "诊断信息已复制");
    if (out) {
      out.className = "inline-result " + (ok ? "ok" : "bad");
      out.textContent = ok ? "已复制" : "复制失败";
      setTimeout(function () { out.textContent = ""; }, 2000);
    }
  }

  var lastConfigUrl = "";

  /* ================= 分区定位（rail） ================= */

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
      // §22：当前项始终可见（scrollIntoView nearest）
      if (b.getAttribute("data-sec") === name) {
        b.scrollIntoView({ block: "nearest", inline: "nearest" });
      }
    });
    if (name === "server") updateServerConnStatus();
    if (name === "system") refreshSysSensorsSettings();
    if (name === "data") refreshDataInfo();
  }

  function goToSection(name) {
    if (!name || !document.querySelector('.rail-item[data-sec="' + name + '"]')) name = "server";
    showSection(name);
  }

  /* 服务器连接状态（Runtime State：复用 /api/status 轮询 + 测试连接结果） */
  function updateServerConnStatus() {
    var box = $("serverConnStatus");
    if (!box) return;
    var conn = null;
    if (window.LM && LM.app && LM.app.serverConnectionState) conn = LM.app.serverConnectionState();
    var textEl = box.querySelector(".cs-text");
    var subEl = box.querySelector(".cs-sub");
    box.classList.remove("online", "offline", "metrics-unavailable");
    var tested = lastTestConn && lastTestConn.at;
    if (tested) {
      if (lastTestConn.ok) {
        box.classList.add("online");
        textEl.textContent = "已连接";
        subEl.textContent = (conn && conn.url ? conn.url + " · " : "") + "Metrics 可用";
      } else {
        box.classList.add("offline");
        textEl.textContent = "不可达";
        subEl.textContent = lastTestConn.error || "最近一次测试失败";
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
        textEl.textContent = "不可达";
        subEl.textContent = conn.url || "llama-server 当前不可达";
      } else {
        textEl.textContent = "尚未测试";
        subEl.textContent = conn.url || "";
      }
    } else {
      textEl.textContent = "尚未测试";
      subEl.textContent = "";
    }
  }

  /* ================= 初始化 ================= */

  function init() {
    var bind = function (id, ev, fn) {
      var el = $(id);
      if (el) el.addEventListener(ev, fn);
    };
    bind("btnSaveSettings", "click", saveSettings);
    bind("btnResetDefaults", "click", resetToPageDefaults);
    bind("btnTestConn", "click", testConnection);
    bind("btnRefreshData", "click", refreshDataInfo);
    bind("btnRunCheck", "click", runDbCheck);
    bind("btnBackup", "click", backupDb);
    bind("btnClearLive", "click", clearLive);
    bind("btnResetStats", "click", resetStats);
    bind("btnOpenData", "click", function () { openFolderTarget("data"); });
    bind("btnOpenLogs", "click", function () { openFolderTarget("logs"); });
    bind("btnOpenBackups", "click", function () { openFolderTarget("backups"); });
    bind("btnExitApp", "click", exitApp);
    // About
    bind("btnCopyDiag", "click", copyDiagInfo);
    bind("btnAboutOpenData", "click", function () { openFolderTarget("data"); });
    bind("btnAboutCopyDataDir", "click", function () {
      var t = $("aboutDataDir").textContent;
      copyText(t, "数据目录路径已复制");
    });
    // Updates
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
      var url = (rel && rel.release_url) || $("btnUpdateOpenRelease").dataset.url;
      if (url && /^https?:\/\//i.test(url)) {
        window.open(url, "_blank", "noopener,noreferrer");
      }
    });
    // Application
    bind("setAutostart", "change", function (e) { putAutostart(e.target.checked); });
    bind("btnAutostartRepair", "click", function () { putAutostart(true); });
    bind("setTheme", "change", function () {
      if (LM.app && LM.app.applyTheme) LM.app.applyTheme($("setTheme").value);
      markDirty();
    });
    bind("setDbPath", "input", function () { updateDbPathHint(); markDirty(); validateField("setDbPath"); });
    bind("setWebScope", "change", function () {
      var scope = $("setWebScope").value;
      if (scope === "__custom__") {
        $("webCustomHostRow").style.display = "";
        updateWebHostHint();
        markDirty();
      } else {
        var host = scopeToHost(scope === "__custom__" ? "custom" : scope, $("setWebHost").value);
        $("setWebHost").value = host;
        $("webCustomHostRow").style.display = "none";
        updateWebHostHint();
        markDirty();
      }
    });
    bind("setWebHost", "input", function () { markDirty(); validateField("setWebHost"); });

    // 表单输入 -> dirty + 验证
    var formIds = [
      "setServerUrl", "setMetricsPath", "setTimeoutSec", "setPollInterval", "setLiveRetention",
      "setGpuEnabled", "setGpuPoll", "setGpuRetention",
      "setRefreshInterval", "setDefaultRange", "setTheme", "setWebPort",
      "setDbPath", "setWal", "setLogLevel", "setMaxLogSize", "setLogBackupCount",
      "setBackupAuto", "setBackupInterval", "setBackupKeep",
      "setUpdatesEnabled", "setUpdateInterval", "setUpdateAutoDownload",
      "setSysEnabled", "setSysPoll", "setSysHistory", "setSysRetention",
      "setSysAdvanced", "setSysAdvPoll",
    ];
    formIds.forEach(function (id) {
      bind(id, "input", function () { markDirty(); revalidate(); });
      bind(id, "change", function () { markDirty(); revalidate(); applyFieldDependencyStates(); });
    });
    // 访问范围 change（含联动 host）
    bind("setWebScope", "change", function () { markDirty(); revalidate(); });
    var gpuBox = $("gpuDetected");
    if (gpuBox) gpuBox.addEventListener("change", function () { markDirty(); });
    // GPU 全选/全不选/重新检测（§107/§111）
    bind("btnGpuSelectAll", "click", function () {
      var box = $("gpuDetected");
      box.querySelectorAll("input[type=checkbox]").forEach(function (cb) { cb.checked = true; });
      updateGpuCount(getLastDetected());
      updateGpuEmptyHint();
      markDirty();
    });
    bind("btnGpuSelectNone", "click", function () {
      var box = $("gpuDetected");
      box.querySelectorAll("input[type=checkbox]").forEach(function (cb) { cb.checked = false; });
      updateGpuCount(getLastDetected());
      updateGpuEmptyHint();
      markDirty();
    });
    bind("btnGpuRescan", "click", function () {
      // §107-§109：重新检测不改变当前选择（按 UUID），新 GPU 按默认策略
      loadGpuDetected();
    });
    // 系统重新检测（§130）
    bind("btnSysRescan", "click", refreshSysSensorsSettings);

    // rail 点击
    document.querySelectorAll(".settings-rail .rail-item").forEach(function (b) {
      b.addEventListener("click", function () {
        goToSection(b.getAttribute("data-sec"));
      });
    });
    showSection("server");
  }

  var _lastDetected = [];
  function getLastDetected() { return _lastDetected; }

  /* 离开设置页守卫：有未保存修改时弹确认（放弃/返回），§48 */
  var guardReentry = false;
  function unsavedGuard(dest) {
    if (!settingsDirty) return;
    if (guardReentry) return;
    ui.modal({
      title: "设置未保存",
      text: "尚有未保存的设置。\n\n放弃更改后，未保存的修改将丢失。",
      okLabel: "放弃更改",
      cancelLabel: "返回设置",
      danger: true,
      onDone: function (ok) {
        if (!ok) return;
        guardReentry = true;
        LM.nav.showPage(dest);
        guardReentry = false;
      },
    });
    return false;
  }

  async function loadAboutVersion(el) {
    if (!el) return;
    try {
      var d = await api.get("/api/version");
      el.textContent = (d.name || "LlamaMonitor") + " " + (d.version || "--");
    } catch (e) {
      el.textContent = "LlamaMonitor --";
    }
  }

  function initSettings() {
    init();
    if (LM.nav && LM.nav.registerNavGuard) LM.nav.registerNavGuard("settings", unsavedGuard);
  }

  window.LM = window.LM || {};
  LM.settings = {
    init: initSettings,
    loadSettings: loadSettings,
    isLoaded: function () { return settingsLoaded; },
    isDirty: function () { return settingsDirty; },
    goToSection: goToSection,
    activeSection: function () { return activeSection; },
    loadAppIntegration: loadAppIntegration,
    loadAbout: loadAbout,
    loadAboutVersion: loadAboutVersion,
    loadUpdateStatus: loadUpdateStatus,
    refreshDataInfo: refreshDataInfo,
    resetToDefaults: resetToPageDefaults,
    setLastConfigUrl: function (u) { lastConfigUrl = u || ""; },
  };
})();
