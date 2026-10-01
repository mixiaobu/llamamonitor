import subprocess, sys, time
ws = open('tools/_ws.txt').read().strip()
def ev(expr, awaitp=False):
    cmd=[sys.executable,'tools/cdp_eval.py',ws,expr]
    if awaitp: cmd.append('--await')
    r=subprocess.run(cmd,capture_output=True)
    return ((r.stdout or b'').decode('utf-8','replace').strip() or (r.stderr or b'').decode('utf-8','replace').strip())
ev("LM.nav.showPage('history');1")
for _ in range(8):
    time.sleep(2)
    if ev("document.querySelectorAll('#eventsTbody tr.ev-row').length"): break
print("event 来源 distinct (now humanized):")
print(ev("""(function(){var s={};document.querySelectorAll('#eventsTbody tr.ev-row').forEach(function(tr){var td=tr.querySelectorAll('td')[2];if(td){s[td.textContent.trim()]=1;}});return JSON.stringify(Object.keys(s));})()"""))
print("sample rows (时间|事件|来源):")
print(ev("""(function(){var o=[];document.querySelectorAll('#eventsTbody tr.ev-row').forEach(function(tr){var t=tr.querySelectorAll('td');if(t.length>=3){o.push(t[0].textContent.trim()+' | '+t[1].textContent.trim().slice(0,24)+' | '+t[2].textContent.trim());}});return JSON.stringify(o.slice(0,6),null,0);})()"""))
