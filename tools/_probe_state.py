import subprocess, sys, time, json
ws = open('tools/_ws.txt').read().strip()
def ev(expr, awaitp=False):
    cmd=[sys.executable,'tools/cdp_eval.py',ws,expr]
    if awaitp: cmd.append('--await')
    r=subprocess.run(cmd,capture_output=True)
    return ((r.stdout or b'').decode('utf-8','replace').strip() or (r.stderr or b'').decode('utf-8','replace').strip())
# 1) 页面是否活着 / 当前页
print("nav:", ev("(function(){return LM.nav.currentPage();})()"))
ev("LM.nav.showPage('history');1")
time.sleep(2)
# 2) 手动调 refreshHistoryGaps 看抛错
out = ev("(function(){try{ var s=window.LM.app.__probe; }catch(e){return 'probe err '+e.message;} return 'ok';})()")
print(out)
# 3) 看 state 是否暴露
print("state keys probe:", ev("(function(){try{var k=Object.keys(window.LM.app.__state||{}).slice(0,8);return JSON.stringify(k);}catch(e){return 'err:'+e.message;}})()"))
# 4) DOM
time.sleep(3)
print("gaps rows:", ev("document.querySelectorAll('#gapsTbody tr.gap-row').length"))
print("events rows:", ev("document.querySelectorAll('#eventsTbody tr.ev-row').length"))
print("gapsEmpty text:", ev("(function(){var e=document.getElementById('gapsEmpty');return e?e.textContent.trim().slice(0,40)+'|hidden='+e.hidden:'?';})()"))
print("eventsEmpty text:", ev("(function(){var e=document.getElementById('eventsEmpty');return e?e.textContent.trim().slice(0,40)+'|hidden='+e.hidden:'?';})()"))
# 5) 直接 fetch summary + gaps，看后端
print("api summary:", ev("(async function(){var r=await fetch('/api/history/summary?preset=7d');return r.status+' '+r.headers.get('content-type');})()", True))
print("api gaps:", ev("(async function(){var r=await fetch('/api/history/gaps?preset=7d&limit=20');var d=await r.json();return 'gaps='+d.gaps.length+' has_more='+d.has_more;})()", True))
