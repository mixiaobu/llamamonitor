(function(){
  var c = document.querySelector('.content');
  if(!c) return JSON.stringify({nocontent:true});
  var cw = c.clientWidth;
  var out = [];
  var all = c.querySelectorAll('*');
  for(var i=0;i<all.length;i++){
    var el = all[i];
    var r = el.getBoundingClientRect();
    if(r.width>0 && r.right > cw+2){
      var chain = [];
      var p = el;
      var k = 0;
      while(p && k<6){
        var name = p.tagName;
        if(p.id) name += '#'+p.id;
        if(p.className && typeof p.className === 'string') name += '.'+p.className.split(/\s+/)[0];
        chain.push(name);
        p = p.parentElement;
        k++;
      }
      out.push(el.tagName + (el.id?'#'+el.id:'') + ' w=' + Math.round(r.width) + ' < ' + chain.join(' < '));
    }
  }
  return JSON.stringify({cw:cw, sw:c.scrollWidth, n:out.length, out:out.slice(0,10)});
})()
