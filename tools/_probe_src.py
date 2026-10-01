import subprocess, sys, time
ws = open('tools/_ws.txt').read().strip()
def ev(expr, awaitp=False):
    cmd=[sys.executable,'tools/cdp_eval.py',ws,expr]
    if awaitp: cmd.append('--await')
    r=subprocess.run(cmd,capture_output=True)
    return ((r.stdout or b'').decode('utf-8','replace').strip() or (r.stderr or b'').decode('utf-8','replace').strip())
ev("LM.nav.showPage('history');1")
time.sleep(3)
print("EVENT distinct sources:", ev("""(function(){var s={};document.querySelectorAll('#eventsTbody tr.ev-row td').forEach(function(td){if(td.previousElementSibling&&td.previousElementSibling.classList.contains('ev-title')){s[td.textContent.trim()]=1;}});return JSON.stringify(Object.keys(s));})()"""))
print("GAP distinct 来源 (col2):", ev("""(function(){var s={};document.querySelectorAll('#gapsTbody tr.gap-row td').forEach(function(td,i){});var out={};document.querySelectorAll('#gapsTbody tr.gap-row').forEach(function(tr){var t=tr.querySelectorAll('td');if(t.length>=5){out[t[2].textContent.trim()]=1;}});return JSON.stringify(Object.keys(out));})()"""))
print("GAP header cols:", ev("""(function(){var th=document.querySelectorAll('#gapsTableWrap thead th');var o=[];th.forEach(function(x){o.push(x.textContent.trim());});return JSON.stringify(o);})()"""))
print("EVENT header cols:", ev("""(function(){var th=document.querySelectorAll('#eventsTableWrap thead th');var o=[];th.forEach(function(x){o.push(x.textContent.trim());});return JSON.stringify(o);})()"""))
