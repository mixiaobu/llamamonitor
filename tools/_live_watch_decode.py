# 1.3.3 活体观察：decode 阶段实时 TPS（临时脚本）
import json
import time
import urllib.request


def get(p):
    with urllib.request.urlopen("http://127.0.0.1:8790" + p, timeout=3) as r:
        return json.loads(r.read().decode("utf-8"))


t0 = time.time()
while time.time() - t0 < 45:
    d = get("/api/realtime")
    a = d["activity"]
    t = d["throughput"]
    tk = d["tokens"]
    print("t=%5.1f active=%s state=%s decode_tps=%s gen=%s/%s age_ms=%s" % (
        time.time() - t0, a["active"], a["state"], t["current_decode_tps"],
        tk["generated"], tk["max_tokens"], d["freshness"]["age_ms"]))
    time.sleep(2)
