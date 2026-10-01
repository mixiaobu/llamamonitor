(function(){
  var out = {};
  var tr = document.querySelector("#gapsTbody tr.gap-row");
  var tds = tr ? tr.querySelectorAll("td") : [];
  function cs(el, p){ return el ? getComputedStyle(el)[p] : null; }
  out.page = (document.querySelector(".page.active")||{}).id;
  out.viewport = window.innerWidth;
  if (tr) {
    out.trDisplay = cs(tr, "display");
    out.trWidth = cs(tr, "width");
    out.trBox = (function(){ var r = tr.getBoundingClientRect(); return Math.round(r.width); })();
    out.td0 = { display: cs(tds[0],"display"), width: cs(tds[0],"width"), before: getComputedStyle(tds[0],"::before").content };
    out.td3 = { display: cs(tds[3],"display"), width: cs(tds[3],"width") };
    // 看是哪条规则决定 tr display：收集所有匹配 tr 的 display 规则（用 CSSOM）
    var rules = [];
    for (var s of document.styleSheets) {
      var rs; try { rs = s.cssRules; } catch(e){ continue; }
      for (var r of rs) {
        if (r.type === 4) { // media
          var m = r.conditionText || "";
          if (m.indexOf("760") !== -1) {
            for (var rr of r.cssRules) {
              if (rr.selectorText && rr.selectorText.indexOf("gap") !== -1) rules.push(m+" :: "+rr.selectorText+" { display:"+rr.style.display+"; width:"+rr.style.width+" }");
            }
          }
        } else if (r.selectorText && r.selectorText.indexOf("gap") !== -1) {
          rules.push("BASE :: "+r.selectorText+" { display:"+r.style.display+"; width:"+r.style.width+" }");
        }
      }
    }
    out.gapRules = rules;
    // table + wrap display
    out.tableDisplay = cs(tr.closest("table"), "display");
    out.tableLayout = cs(tr.closest("table"), "tableLayout");
    out.tableWidth = cs(tr.closest("table"), "width");
    out.wrapDisplay = cs(document.getElementById("gapsTableWrap"), "display");
  }
  return JSON.stringify(out, null, 1);
})()
