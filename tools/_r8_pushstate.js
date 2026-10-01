(function(){
  var out = {};
  out.before = window.history.length;
  try { window.history.pushState({test:1},""); out.pushOk = true; } catch(e) { out.pushOk = false; out.err = String(e); }
  out.after = window.history.length;
  out.state = JSON.stringify(window.history.state);
  return JSON.stringify(out);
})()
