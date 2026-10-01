import subprocess, time, sys
ws = open('tools/_ws.txt').read().strip()
expr = ("(function(){var tb=document.getElementById('eventsTbody');var c=document.getElementById('eventsCountLabel');"
        "var more=document.getElementById('evMoreBtn');return JSON.stringify("
        "{rows: tb?tb.querySelectorAll('tr.ev-row').length:'?', count: c?c.textContent:'?', "
        "more: more?more.textContent:'?'});})()")
for i in range(int(sys.argv[1]) if len(sys.argv) > 1 else 8):
    r = subprocess.run([sys.executable, 'tools/cdp_eval.py', ws, expr], capture_output=True, text=True)
    print(i, (r.stdout.strip()[:140] or r.stderr.strip()[-100:]))
    time.sleep(2)
