import subprocess, time, sys, json
ws = open('tools/_ws.txt').read().strip()
def ev(expr):
    r = subprocess.run([sys.executable, 'tools/cdp_eval.py', ws, expr], capture_output=True, text=True)
    return (r.stdout.strip() or r.stderr.strip())
# 进 history 页并等
ev("LM.nav.showPage('history');1")
time.sleep(3)
# 直接 fetch events 看 API 在页面里返回什么
out = ev("""(async function(){ try{ var r=await fetch('/api/events?preset=7d&limit=30'); var d=await r.json(); return 'API:'+d.events.length+' cat:'+JSON.stringify(d.categories).slice(0,40);}catch(e){return 'FETCHERR:'+e.message;} })()""", )
# cdp_eval 用 awaitPromise? 看它的实现
print("api-in-page:", out[:160])
# state
print("state.histEvents via window probe not exposed; probe DOM + errors")
print(ev("(function(){var tb=document.getElementById('eventsTbody');var w=document.getElementById('eventsTableWrap');var e=document.getElementById('eventsEmpty');return 'rows='+(tb?tb.querySelectorAll('tr.ev-row').length:'?')+' wrapDisp='+(w?getComputedStyle(w).display:'?')+' emptyHidden='+(e?e.hidden:'?');})()"))
