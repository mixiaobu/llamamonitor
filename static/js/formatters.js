/* ============================================================
   LlamaMonitor — Formatters（Phase 15, spec §64-§66）
   全应用唯一格式化入口。单位规范：
     Tokens: K/M/B（卡片）；tooltip 原始值带千分位
     Memory: MiB / GiB
     Power: W；Energy: Wh / kWh
     Temperature: °C；Duration: ms / s / m / h / d
   所有函数 null/undefined/NaN 安全（返回 "--"，除非说明）。
   ============================================================ */
(function () {
  "use strict";

  var NA = "--";

  function isBad(v) {
    return v === null || v === undefined || (typeof v === "number" && isNaN(v));
  }

  /** Token 紧凑格式：1000 -> 1.00K，1000000 -> 1.00M，1e9 -> 1.00B */
  function formatTokenCount(v) {
    if (isBad(v)) return NA;
    v = Number(v);
    var neg = v < 0 ? "-" : "";
    var a = Math.abs(v);
    if (a >= 1e9) return neg + (a / 1e9).toFixed(2) + "B";
    if (a >= 1e6) return neg + (a / 1e6).toFixed(2) + "M";
    if (a >= 1e3) return neg + (a / 1e3).toFixed(2) + "K";
    return neg + String(Math.round(a));
  }

  /** Token 原始值（tooltip，spec §66：8,324,129） */
  function formatTokenCountFull(v) {
    if (isBad(v)) return NA;
    return Math.round(Number(v)).toLocaleString("en-US");
  }

  /** 字节：B / KB / MB / GB（1024） */
  function formatBytes(n) {
    if (isBad(n)) return NA;
    var units = ["B", "KB", "MB", "GB", "TB"];
    var i = 0;
    var v = Number(n);
    while (v >= 1024 && i < units.length - 1) {
      v /= 1024;
      i++;
    }
    return v.toFixed(v >= 100 || i === 0 ? 0 : 1) + " " + units[i];
  }

  /** 内存：MiB / GiB */
  function formatMemory(bytes) {
    if (isBad(bytes)) return NA;
    var v = Number(bytes) / (1024 * 1024);
    if (v >= 1024) return (v / 1024).toFixed(1) + " GiB";
    return v.toFixed(0) + " MiB";
  }

  /** VRAM（后端给 MB）：x.x / y.y GiB */
  function formatVramMb(usedMb, totalMb) {
    if (isBad(usedMb) || isBad(totalMb)) return NA;
    return (usedMb / 1024).toFixed(1) + " / " + (totalMb / 1024).toFixed(1) + " GiB";
  }

  /** 百分比：1 位小数 + % */
  function formatPercent(v, digits) {
    if (isBad(v)) return NA;
    return Number(v).toFixed(digits === undefined ? 1 : digits) + "%";
  }

  /** 比率 0-1 -> 百分比 */
  function formatRatio(ratio) {
    if (isBad(ratio)) return NA;
    return (Number(ratio) * 100).toFixed(1) + "%";
  }

  /** TPS：1 位小数 */
  function formatTps(v) {
    if (isBad(v)) return NA;
    return Number(v).toFixed(1);
  }

  /** 功率：W（<1000）/ kW */
  function formatPower(w) {
    if (isBad(w)) return NA;
    var v = Number(w);
    if (Math.abs(v) >= 1000) return (v / 1000).toFixed(2) + " kW";
    return v.toFixed(0) + " W";
  }

  /** 能量：Wh / kWh（输入 Wh） */
  function formatEnergy(wh) {
    if (isBad(wh)) return NA;
    var v = Number(wh);
    if (Math.abs(v) >= 1000) return (v / 1000).toFixed(2) + " kWh";
    return v.toFixed(0) + " Wh";
  }

  /** 温度：°C */
  function formatTemp(c) {
    if (isBad(c)) return NA;
    return Number(c).toFixed(0) + " \u00B0C";
  }

  /** 时长（秒输入）：s / m s / h m / d h */
  function formatDuration(sec) {
    sec = Math.max(0, Math.round(Number(sec) || 0));
    if (sec < 60) return sec + "s";
    if (sec < 3600) return Math.floor(sec / 60) + "m " + (sec % 60) + "s";
    if (sec < 86400) return Math.floor(sec / 3600) + "h " + Math.floor((sec % 3600) / 60) + "m";
    return Math.floor(sec / 86400) + "d " + Math.floor((sec % 86400) / 3600) + "h";
  }

  /** 相对时间（中文，如 "2 分钟前"） */
  function formatAgo(sec) {
    if (isBad(sec)) return "暂无数据";
    sec = Number(sec);
    if (sec < 60) return Math.round(sec) + " 秒前";
    if (sec < 3600) return Math.round(sec / 60) + " 分钟前";
    if (sec < 86400) return (sec / 3600).toFixed(1) + " 小时前";
    return (sec / 86400).toFixed(1) + " 天前";
  }

  function pad2(n) {
    return String(n).padStart(2, "0");
  }

  /** epoch 秒 -> 本地 HH:MM:SS */
  function formatTime(epoch) {
    if (!epoch) return NA;
    var d = new Date(Number(epoch) * 1000);
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
  }

  /** epoch 秒 -> 本地 HH:MM（图表横轴） */
  function formatHM(epoch) {
    var d = new Date(Number(epoch) * 1000);
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes());
  }

  /** epoch 秒 -> 本地完整日期时间 */
  function formatDateTime(epoch) {
    if (!epoch) return NA;
    return new Date(Number(epoch) * 1000).toLocaleString();
  }

  /** epoch 秒 -> 紧凑时间（表格/列表用，保证单行）：
      与今天同天只显示 HH:MM:SS，否则 MM-DD HH:MM。完整值可放 title。 */
  function formatClock(epoch) {
    if (!epoch) return NA;
    var d = new Date(Number(epoch) * 1000);
    var now = new Date();
    var sameDay = d.getFullYear() === now.getFullYear() &&
                  d.getMonth() === now.getMonth() &&
                  d.getDate() === now.getDate();
    if (sameDay) return formatTime(epoch);
    return pad2(d.getMonth() + 1) + "-" + pad2(d.getDate()) + " " +
           pad2(d.getHours()) + ":" + pad2(d.getMinutes());
  }

  /** 普通数字（千分位） */
  function formatNumber(v) {
    if (isBad(v)) return NA;
    return Number(v).toLocaleString("en-US");
  }

  /** 整数或 -- */
  function formatInt(v) {
    if (isBad(v)) return NA;
    return String(Math.round(Number(v)));
  }

  window.LM = window.LM || {};
  LM.fmt = {
    NA: NA,
    isBad: isBad,
    formatTokenCount: formatTokenCount,
    formatTokenCountFull: formatTokenCountFull,
    formatBytes: formatBytes,
    formatMemory: formatMemory,
    formatVramMb: formatVramMb,
    formatPercent: formatPercent,
    formatRatio: formatRatio,
    formatTps: formatTps,
    formatPower: formatPower,
    formatEnergy: formatEnergy,
    formatTemp: formatTemp,
    formatDuration: formatDuration,
    formatAgo: formatAgo,
    formatTime: formatTime,
    formatHM: formatHM,
    formatDateTime: formatDateTime,
    formatClock: formatClock,
    formatNumber: formatNumber,
    formatInt: formatInt,
  };
})();
