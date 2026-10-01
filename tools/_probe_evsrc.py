import subprocess, sys
ws = open('tools/_ws.txt').read().strip()
def ev(expr, awaitp=False):
    cmd=[sys.executable,'tools/cdp_eval.py',ws,expr]
    if awaitp: cmd.append('--await')
    r=subprocess.run(cmd,capture_output=True)
    return ((r.stdout or b'').decode('utf-8','replace').strip() or (r.stderr or b'').decode('utf-8','replace').strip())
# 直接拉后端 events 统计 distinct source
print(ev("""(async function(){ var r=await fetch('/api/events?preset=all&limit=100'); var d=await r.json(); var s={}; d.events.forEach(function(e){s[e.source]=(s[e.source]||0)+1;}); return JSON.stringify(s); })()""", True))
print(ev("""(async function(){ var r=await fetch('/api/events?preset=all&limit=200'); var d=await r.json(); var s={}; d.events.forEach(function(e){s[e.source]=(s[e.source]||0)+1;}); return JSON.stringify(s); })()""", True))
