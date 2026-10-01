import subprocess, time, sys
ws = open('tools/_ws.txt').read().strip()
def ev(expr, awaitp=False):
    cmd = [sys.executable, 'tools/cdp_eval.py', ws, expr]
    if awaitp: cmd.append('--await')
    r = subprocess.run(cmd, capture_output=True)
    return ((r.stdout or b'').decode('utf-8','replace').strip() or (r.stderr or b'').decode('utf-8','replace').strip())
ev("LM.nav.showPage('history');1")
time.sleep(1)
# force 1920 to rule out timeline path
# (can't emulate via eval; but read matchMedia result in-page)
print("matchMedia987:", ev("window.matchMedia('(max-width:987px)').matches"))
print("innerWidth:", ev("window.innerWidth"))
# wait for data
for i in range(12):
    out = ev("""(function(){var tb=document.getElementById('eventsTbody');var e=document.getElementById('eventsEmpty');
      var c=document.getElementById('eventsCountLabel');var tl=document.getElementById('evTimeline');var w=document.getElementById('eventsTableWrap');
      return 'rows='+(tb?tb.querySelectorAll('tr.ev-row').length:'?')+' tl='+ (tl?tl.hidden:'?')+' tlN='+(tl?tl.children.length:'?')
        +' emptyHidden='+(e?e.hidden:'?')+' wrap='+(w?getComputedStyle(w).display:'?')+' cnt='+(c?c.textContent:'?');})()""")
    print(i, out)
    time.sleep(2)
