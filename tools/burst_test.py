# burst_test.py — fire the 15 page-load endpoints concurrently (stdlib only).
import time, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "http://127.0.0.1:8790"
URLS = [
 "/api/version", "/api/config", "/api/daily?days=7", "/api/update/status",
 "/api/status", "/api/summary", "/api/runtime", "/api/mtp", "/api/data/quality",
 "/api/health", "/api/gpu/status", "/api/system/status",
 "/api/daily?days=7", "/api/usage-summary?days=7", "/api/summary",
]

def one(path):
    t0 = time.time()
    try:
        with urllib.request.urlopen(BASE + path, timeout=20) as r:
            r.read()
            return path, r.status, int((time.time()-t0)*1000)
    except Exception as e:
        return path, "ERR:"+type(e).__name__, int((time.time()-t0)*1000)

wall0 = time.time()
out = {}
with ThreadPoolExecutor(max_workers=20) as ex:
    futs = {ex.submit(one, u): u for u in URLS}
    for f in as_completed(futs):
        p, c, ms = f.result()
        out[p] = (c, ms)
for p in URLS:
    if p in out:
        c, ms = out[p]
        print(f"{p:42s} {str(c):8s} {ms:6d}ms")
print(f"total burst wall: {int((time.time()-wall0)*1000)}ms")
