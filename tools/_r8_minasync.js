(async function(){
  await new Promise(function(r){ setTimeout(r, 1500); });
  return "minasync-ok " + (document.querySelector(".page.active")||{}).id;
})()
