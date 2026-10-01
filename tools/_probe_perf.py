import subprocess, sys, time
ws = open('tools/_ws.txt').read().strip()
def ev(expr):
    r=subprocess.run([sys.executable,'tools/cdp_eval.py',ws,expr],capture_output=True)
    return ((r.stdout or b'').decode('utf-8','replace').strip() or (r.stderr or b'').decode('utf-8','replace').strip())
# 列出所有 registerPage 的 key
print("registered page keys:", ev("(function(){var o=[];LM.nav._pages&&Object.keys(LM.nav._pages).forEach(function(k){o.push(k);});return JSON.stringify(o.length?o:'no _pages');})()"))
# 直接试 performance
ev("LM.nav.showPage('performance');1")
time.sleep(2.5)
print("after showPage('performance'):")
print(" ", ev("(function(){var s=document.getElementById('page-performance');return 'current='+LM.nav.currentPage()+' perfdisplay='+(s?getComputedStyle(s).display:'?');})()"))
