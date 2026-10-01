(function(){
  function cols(sel){var e=document.querySelector(sel);if(!e)return null;return getComputedStyle(e).gridTemplateColumns;}
  var w = window.innerWidth;
  return JSON.stringify({
    width: w,
    todayBreakdown: cols(".ov-today-breakdown"),
    inferenceGrid: cols(".ov-inference-grid"),
    hostGrid: cols(".ov-host-grid"),
    gpuGrid: cols(".ov-gpu-grid"),
    integrityGrid: cols(".ov-integrity-grid"),
    serviceMetaVisible: (function(){var e=document.querySelector(".ov-meta-grid");if(!e)return null;var r=e.getBoundingClientRect();return {w:Math.round(r.width),visible:r.width>0};})(),
    gpuCardCount: document.querySelectorAll("#ovGpuMini .gpu-mini").length,
    attentionItems: document.querySelectorAll("#ovAttentionList .ov-attention-item").length,
    pageLinks: (document.querySelectorAll(".ov-page-links .link").length)
  });
})()
