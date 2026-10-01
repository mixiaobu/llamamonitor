(function(){
  return (
    (document.querySelectorAll(".slot-table tr.slot-row").length > 0 ? 1 : 0) +
    (document.querySelectorAll("#gpuProc .gpu-proc-table tbody tr").length > 0 ? 1 : 0) +
    (document.querySelectorAll(".core-heat-grid .core-cell").length > 0 ? 1 : 0) +
    (document.querySelectorAll("#gapsTbody tr.gap-row").length > 0 ? 1 : 0)
  );
})()
