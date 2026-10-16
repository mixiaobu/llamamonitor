# 1.3.3 故障注入 A：Strata 停/复 全链路观察（临时脚本）
# 每 0.5s 记录 /api/realtime 关键字段；状态变化时打印（不刷屏）
import json
import time
import urllib.request

t0 = time.time()
last = None
rows = []
while time.time() - t0 < 75:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8790/api/realtime", timeout=3) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print("t=%5.1f realtime fetch err: %r" % (time.time() - t0, e))
        time.sleep(0.5)
        continue
    a = d["activity"]
    key = (d["server_online"], a.get("active"), a.get("state"),
           a.get("source"), a.get("confidence"))
    row = dict(t=round(time.time() - t0, 1), online=d["server_online"],
               active=a.get("active"), state=a.get("state"),
               source=a.get("source"), conf=a.get("confidence"),
               metrics=d["diagnostics"]["source"]["metrics"],
               age=d["freshness"]["age_ms"])
    rows.append(row)
    if key != last:
        m = row["metrics"]
        print("t=%5.1f online=%s active=%s state=%s src=%s conf=%s | "
              "metrics avail=%s age=%sms fails=%s err=%s | sample_age=%s" % (
                  row["t"], row["online"], row["active"], row["state"],
                  row["source"], row["conf"],
                  m["available"], m["age_ms"], m["consecutive_failures"],
                  m["last_error"], row["age"]))
        last = key
    time.sleep(0.5)

with open("tools/faultA_rows.json", "w", encoding="utf-8") as f:
    json.dump(rows, f, ensure_ascii=False, indent=1)
print("rows saved:", len(rows))
