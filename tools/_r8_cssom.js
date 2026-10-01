(function(){
  var out={vw:window.innerWidth};
  var dc=document.getElementById('dailyDayCards');
  var cards=dc?dc.querySelectorAll('.day-card'):[];
  // 找第一张有 dc-grid 的卡
  var withGrid=null;
  cards.forEach(function(c){ if(!withGrid && c.querySelector('.dc-grid')) withGrid=c; });
  out.withGrid = withGrid ? {
    head: (withGrid.querySelector('.dc-head')||{}).textContent,
    items: Array.prototype.map.call(withGrid.querySelectorAll('.dc-item'), function(i){return i.textContent;}),
    foot: (withGrid.querySelector('.dc-foot')||{}).textContent,
    // 数字是否无 ellipsis（dc-total / dc-v 不应被裁）
    totalClipped: (function(){ var t=withGrid.querySelector('.dc-total'); if(!t)return null;
      return {scrollW:t.scrollWidth, clientW:t.clientWidth, clipped: t.scrollWidth>t.clientWidth+1}; })()
  } : null;
  // chart：读真实 option
  var c = window.LM && LM.charts && LM.charts.chart ? window.LM.charts.chart('chartUsage') : null;
  if (c) {
    var o = c.getOption();
    out.chart = {
      confine: o.tooltip&&o.tooltip[0]?o.tooltip[0].confine:null,
      dataZoom: (o.dataZoom||[]).map(function(d){return d.type;}),
      legend: o.legend&&o.legend[0]?{top:o.legend[0].top,align:o.legend[0].align,type:o.legend[0].type||'plain',items:(o.legend[0].data||[]).length}:null,
      series: (o.series||[]).length,
      xAxisData: (o.xAxis&&o.xAxis[0]&&o.xAxis[0].data||[]).length,
    };
    // 当前 range label
    var rl = document.querySelector('#usageRange [aria-pressed="true"]');
    out.range = rl?rl.textContent:null;
  } else out.chart={err:'no instance'};
  return JSON.stringify(out);
})()
