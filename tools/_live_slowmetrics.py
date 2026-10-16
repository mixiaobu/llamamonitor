# §40 验证：慢 /metrics（3s）不拖垮 HOT（临时脚本）
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8791"


def get(p):
    with urllib.request.urlopen(BASE + p, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


rows = []
t0 = time.time()
while time.time() - t0 < 18:
    d = get("/api/realtime")
    rows.append(dict(
        t=round(time.time() - t0, 1),
        seq=d["sequence"],
        dur=d["diagnostics"]["last_hot_duration_ms"],
        over=d["diagnostics"]["hot_overrun"],
        over_n=d["diagnostics"]["hot_overrun_count"],
        m=d["diagnostics"]["source"]["metrics"],
        st=d["diagnostics"]["source"]["status"],
        sl=d["diagnostics"]["source"]["slots"],
        src=d["activity"]["source"],
        conf=d["activity"]["confidence"],
        age=d["freshness"]["age_ms"],
    ))
    time.sleep(1)

for r in rows:
    print("t=%5.1f seq=%-3d hot=%4dms over=%s(%d) | m_age=%6s m_fails=%s | st_age=%6s | sl_age=%6s | %s/%s | sample_age=%s" % (
        r["t"], r["seq"], r["dur"], r["over"], r["over_n"],
        r["m"]["age_ms"], r["m"]["consecutive_failures"],
        r["st"]["age_ms"], r["sl"]["age_ms"],
        r["src"][:30], r["conf"], r["age"]))

# 节奏分析
seqs = [r["seq"] for r in rows]
print("\nsequence delta over %ds: %d (expect ~1/1.5s = ~12)" % (
    round(rows[-1]["t"] - rows[0]["t"]), seqs[-1] - seqs[0]))
durs = [r["dur"] for r in rows]
print("hot duration ms: min=%d max=%d (3s 慢端点必须被 1.5s 超时截断，绝不为 3000ms)" % (
    min(durs), max(durs)))
