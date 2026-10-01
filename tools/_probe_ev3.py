import subprocess, time, sys
ws = open('tools/_ws.txt').read().strip()
def ev(expr, awaitp=False):
    cmd = [sys.executable, 'tools/cdp_eval.py', ws, expr]
    if awaitp: cmd.append('--await')
    r = subprocess.run(cmd, capture_output=True)
    return ((r.stdout or b'').decode('utf-8', 'replace').strip() or (r.stderr or b'').decode('utf-8', 'replace').strip())
ev("LM.nav.showPage('history');1")
for i in range(10):
    out = ev("(function(){var tb=document.getElementById('eventsTbody');var w=document.getElementById('eventsTableWrap');var e=document.getElementById('eventsEmpty');var m=document.getElementById('evMoreBtn');return 'rows='+(tb?tb.querySelectorAll('tr.ev-row').length:'?')+' wrap='+(w?getComputedStyle(w).display:'?')+' emptyHidden='+(e?e.hidden:'?')+' more='+(m?m.textContent:'-');})()")
    print(i, out)
    time.sleep(2)
