import subprocess, time, sys, json
ws = open('tools/_ws.txt').read().strip()
def ev(expr, awaitp=False):
    cmd = [sys.executable, 'tools/cdp_eval.py', ws, expr]
    if awaitp: cmd.append('--await')
    r = subprocess.run(cmd, capture_output=True)
    return ((r.stdout or b'').decode('utf-8','replace').strip() or (r.stderr or b'').decode('utf-8','replace').strip())
def shot(name, full=False):
    # emulate via CDP through a tiny inline python ws call is heavy; skip, just report mode
    pass
for w in [390, 320]:
    print("=== width", w, "===")
    out = ev("""(function(){
      var mm = window.matchMedia('(max-width: 987px)').matches;
      var tb = document.getElementById('eventsTbody');
      var tl = document.getElementById('evTimeline');
      var w  = document.getElementById('eventsTableWrap');
      return JSON.stringify({
        innerW: window.innerWidth,
        match987: mm,
        tableRows: tb ? tb.querySelectorAll('tr.ev-row').length : -1,
        tableWrapDisplay: w ? getComputedStyle(w).display : -1,
        tlHidden: tl ? tl.hidden : -1,
        tlItems: tl ? tl.querySelectorAll('.ev-tl-item').length : -1
      });
    })()""")
    print(out)
