import subprocess, sys, time
ws = open('tools/_ws.txt').read().strip()
def ev(expr, awaitp=False):
    cmd=[sys.executable,'tools/cdp_eval.py',ws,expr]
    if awaitp: cmd.append('--await')
    r=subprocess.run(cmd,capture_output=True)
    return ((r.stdout or b'').decode('utf-8','replace').strip() or (r.stderr or b'').decode('utf-8','replace').strip())
ev("location.reload();1")
time.sleep(6)
ev("LM.nav.showPage('history');1")
for i in range(15):
    time.sleep(2)
    g=ev("document.querySelectorAll('#gapsTbody tr.gap-row').length")
    e=ev("document.querySelectorAll('#eventsTbody tr.ev-row').length")
    gl=ev("(function(){var c=document.getElementById('gapsCountLabel');return c?c.textContent:'?';})()")
    el=ev("(function(){var c=document.getElementById('eventsCountLabel');return c?c.textContent:'?';})()")
    print("%2d gaps=%s events=%s gl=%s el=%s" % (i,g,e,gl[:30],el[:30]))
    if g and e: break
