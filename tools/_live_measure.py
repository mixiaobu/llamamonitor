# 1.3.3 活体测量（临时脚本）：/api/realtime 诊断采样 N 秒
import json
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8790"
SECS = int(sys.argv[2]) if len(sys.argv) > 2 else 20


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=3) as r:
        return json.loads(r.read().decode("utf-8"))


durs = []
ages = []
seqs = []
t0 = time.time()
while time.time() - t0 < SECS:
    d = get("/api/realtime")
    durs.append(d["diagnostics"]["last_hot_duration_ms"])
    ages.append(d["freshness"]["age_ms"])
    seqs.append(d["sequence"])
    time.sleep(1)

durs_sorted = sorted(durs)
n = len(durs)
last = get("/api/realtime")
print(f"samples={n}")
print(f"HOT last_duration_ms: min={durs_sorted[0]} p50={durs_sorted[n//2]} max={durs_sorted[-1]} avg={sum(durs)/n:.1f}")
print(f"hot_overrun_count(last)={last['diagnostics']['hot_overrun_count']}")
print(f"hot_overrun(last)={last['diagnostics']['hot_overrun']}")
print(f"age_ms: max={max(ages)} min={min(ages)}")
print(f"sequence: first={seqs[0]} last={seqs[-1]} delta={seqs[-1]-seqs[0]}")
print("activity:", json.dumps(last["activity"], ensure_ascii=False))
print("throughput:", json.dumps(last["throughput"], ensure_ascii=False))
for name, st in last["diagnostics"]["source"].items():
    print(f"endpoint {name}: available={st['available']} age_ms={st['age_ms']} "
          f"failures={st['consecutive_failures']} err={st['last_error']}")
