/* ============================================================
   LlamaMonitor — Icons（Phase 15）
   本地 SVG 线性图标（spec §9：不引入字体图标/CDN）。
   24x24 viewBox，stroke=currentColor，由 CSS 控制尺寸。
   用法：LM.icons.get('gpu') -> SVG 字符串（内联注入）。
   ============================================================ */
(function () {
  "use strict";

  var PATHS = {
    /* ---- 导航 ---- */
    overview:
      '<path d="M5 15.5a7.5 7.5 0 1 1 14 0"/><line x1="12" y1="15.5" x2="15.4" y2="11.6"/><circle cx="12" cy="15.5" r="0.6"/>',
    usage:
      '<circle cx="12" cy="12" r="8.5"/><path d="M12 3.5V12l6 5.2"/>',
    performance:
      '<path d="M3 12.5h3.6l2.6-6.4 4.2 11.8 2.6-5.4H21"/>',
    gpu:
      '<rect x="5.5" y="5.5" width="13" height="13" rx="2"/><rect x="9.5" y="9.5" width="5" height="5" rx="1"/><path d="M9 2.5v3M15 2.5v3M9 18.5v3M15 18.5v3M2.5 9h3M2.5 15h3M18.5 9h3M18.5 15h3"/>',
    history:
      '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3.2 2"/>',
    settings:
      '<circle cx="12" cy="12" r="3.2"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
    about:
      '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5"/><path d="M12 7.6h.01"/>',

    /* ---- InfoBar / Toast ---- */
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 7.8h.01"/>',
    success: '<circle cx="12" cy="12" r="9"/><path d="M8.2 12.4l2.6 2.6 4.8-5.6"/>',
    warning: '<path d="M12 3.6L21.4 19.8H2.6z"/><path d="M12 9.8v4.4"/><path d="M12 17.2h.01"/>',
    error: '<circle cx="12" cy="12" r="9"/><path d="M9.3 9.3l5.4 5.4M14.7 9.3l-5.4 5.4"/>',
    check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',

    /* ---- EmptyState ---- */
    emptyChart: '<path d="M4 20h16"/><path d="M6.5 20v-6.5M11 20V8M15.5 20v-9.5M20 20V12"/>',
    emptyDb:
      '<ellipse cx="12" cy="6" rx="7.5" ry="3"/><path d="M4.5 6v12c0 1.66 3.36 3 7.5 3s7.5-1.34 7.5-3V6"/><path d="M4.5 12c0 1.66 3.36 3 7.5 3s7.5-1.34 7.5-3"/>',
    emptyGauge:
      '<path d="M5 16a7.5 7.5 0 1 1 14 0"/><path d="M12 16l3-4"/>',

    close: '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"',

    /* ---- 其他 ---- */
    download: '<path d="M12 4v11M7.5 11l4.5 4.5L16.5 11"/><path d="M4.5 19.5h15"/>',
    refresh: '<path d="M20 12a8 8 0 1 1-2.34-5.66"/><path d="M20 4v4h-4"/>',
    open: '<path d="M14 5h5v5"/><path d="M19 5l-8 8"/><path d="M18 13.5V19H5V6h5.5"/>',
  };

  var NS = "http://www.w3.org/2000/svg";

  function svg(name) {
    var p = PATHS[name];
    if (!p) return "";
    return '<svg xmlns="' + NS + '" viewBox="0 0 24 24" aria-hidden="true" focusable="false">' + p + "</svg>";
  }

  /** 品牌标识（spec §115：极简 LM 标识，accent 色由 CSS 提供）。 */
  function brand() {
    return (
      '<svg xmlns="' + NS + '" viewBox="0 0 28 28" aria-hidden="true" focusable="false">' +
      '<rect x="1.5" y="1.5" width="25" height="25" rx="7" fill="none" stroke="currentColor" stroke-width="1.5"/>' +
      '<text x="14" y="18.2" text-anchor="middle" font-size="10.5" font-weight="600" fill="currentColor" ' +
      'font-family="Segoe UI Variable Display, Segoe UI, sans-serif">LM</text></svg>'
    );
  }

  window.LM = window.LM || {};
  LM.icons = {
    get: svg,
    brand: brand,
  };
})();
