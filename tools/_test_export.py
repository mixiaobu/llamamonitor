import urllib.request, urllib.parse, time, os
BASE = "http://127.0.0.1:8790"
def get(path):
    r = urllib.request.urlopen(BASE + path, timeout=20)
    return r.headers, r.read()
def check(path, name):
    try:
        h, body = get(path)
        bom = body[:3] == b'\xef\xbb\xbf'
        text = body.decode('utf-8-sig')
        lines = [l for l in text.split('\n') if l.strip()]
        ctype = h.get('Content-Type','')
        ddisp = h.get('Content-Disposition','')
        print("=== %s ===" % name)
        print("  BOM=%s  lines=%d  content-type=%s" % (bom, len(lines), ctype))
        print("  content-disposition=%s" % ddisp[:80])
        print("  header: " + (lines[0] if lines else '(none)')[:120])
        if len(lines) > 1: print("  row1:   " + lines[1][:120])
        if len(lines) > 2: print("  row2:   " + lines[2][:120])
    except Exception as e:
        print("=== %s === ERR %s" % (name, e))

check("/api/data/export/gaps.csv?preset=7d", "gaps (7d)")
check("/api/data/export/gaps.csv?preset=7d&source=llama", "gaps (7d, source=llama)")
check("/api/data/export/gaps.csv?preset=7d&risk=lost", "gaps (7d, risk=lost)")
check("/api/data/export/events.csv?preset=7d", "events (7d)")
check("/api/data/export/events.csv?preset=7d&category=服务", "events (7d, cat=服务)")
check("/api/data/export/events.csv?preset=7d&search=llama", "events (7d, search=llama)")
check("/api/data/export/daily.csv?days=7", "token daily (7d)")
check("/api/data/export/gpu_daily.csv?days=7", "gpu daily (7d)")
