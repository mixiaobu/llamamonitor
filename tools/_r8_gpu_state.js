JSON.stringify({
  page: (document.querySelector(".page.active") || {}).id,
  gpuProcLen: (document.getElementById("gpuProc") || { innerHTML: "missing" }).innerHTML.length,
  gpuEmpty: document.querySelector(".gpu-proc-empty") ? document.querySelector(".gpu-proc-empty").textContent : null,
  gapRows: document.querySelectorAll("#gapsTbody tr").length,
  gapEmptyHidden: (document.getElementById("gapsEmpty") || {}).hidden,
  gapWrapDisplay: getComputedStyle(document.getElementById("gapsTableWrap")).display
})
