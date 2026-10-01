import subprocess, time, sys
ws = open('tools/_ws.txt').read().strip()
def ev(expr, awaitp=False):
    cmd = [sys.executable, 'tools/cdp_eval.py', ws, expr]
    if awaitp: cmd.append('--await')
    r = subprocess.run(cmd, capture_output=True)
    return ((r.stdout or b'').decode('utf-8','replace').strip() or (r.stderr or b'').decode('utf-8','replace').strip())
# instrument: log every fetch to /api/events + /api/history/gaps + summary/trend
ev("""(function(){ if(window.__hnet) return 'already'; window.__hnet=[]; var o=window.fetch;
 window.fetch=function(u){ try{ window.__hnet.push(String(u).slice(0,80)); }catch(e){} return o.apply(this,arguments); }; return 'ok'; })()""")
ev("LM.nav.showPage('history');1")
time.sleep(2)
print("fetches after nav:")
for f in ev("JSON.stringify(window.__hnet||[])")[:600]:
    pass
print(ev("JSON.stringify((window.__hnet||[]).filter(function(x){return x.indexOf('events')>-1||x.indexOf('history')>-1;}))"))
time.sleep(15)
print("\nafter +15s:")
print(ev("JSON.stringify((window.__hnet||[]).filter(function(x){return x.indexOf('events')>-1;}))"))
out = ev("""(function(){var tb=document.getElementById('eventsTbody');var c=document.getElementById('eventsCountLabel');var e=document.getElementById('eventsEmpty');var m=document.getElementById('evMoreBtn');return 'rows='+(tb?tb.querySelectorAll('tr.ev-row').length:'?')+' cnt='+(c?c.textContent:'?')+' emptyHidden='+(e?e.hidden:'?')+' more='+(m?m.textContent:'?');})()""")
print("state:", out)
