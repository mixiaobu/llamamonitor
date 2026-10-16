# 1.3.3 活体（llama）：长生成捕捉 busy slot 字段 + 实时 TPS
import json
import threading
import time
import urllib.request

BASE = "http://127.0.0.1:8790"
LLAMA = "http://127.0.0.1:9091"


def get(path, timeout=3):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def raw_slots():
    with urllib.request.urlopen(LLAMA + "/slots", timeout=3) as r:
        return json.loads(r.read().decode("utf-8"))


result = {}


def infer():
    body = json.dumps({
        "messages": [{"role": "user",
                      "content": "List 400 different colors of the rainbow, one per line, numbered."}],
        "max_tokens": 2000, "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        LLAMA + "/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            d = json.loads(r.read().decode("utf-8"))
        result["ok"] = True
        result["elapsed"] = round(time.time() - t0, 1)
        result["usage"] = d.get("usage")
    except Exception as e:
        result["ok"] = False
        result["error"] = repr(e)


th = threading.Thread(target=infer, daemon=True)
th.start()

busy_keys_seen = None
rows = []
t0 = time.time()
while time.time() - t0 < 120:
    if not th.is_alive() and "ok" in result:
        break
    try:
        d = get("/api/realtime")
    except Exception:
        time.sleep(0.2)
        continue
    a = d["activity"]
    thp = d["throughput"]
    tk = d["tokens"]
    rows.append((round(time.time() - t0, 1), a.get("state"),
                 thp.get("current_decode_tps"), thp.get("current_prompt_tps"),
                 tk.get("generated"), d["slots"].get("active")))
    # 捕捉一次 busy slot 的原始字段
    if a.get("active") and busy_keys_seen is None:
        try:
            for s in raw_slots():
                if s.get("is_processing"):
                    busy_keys_seen = sorted(s.keys())
                    print("BUSY SLOT keys:", busy_keys_seen)
        except Exception:
            pass
    time.sleep(0.2)

th.join(timeout=10)
print("\n=== 推理结果 ===")
print(json.dumps(result, ensure_ascii=False))
print("\n=== 观察行（t, state, decode_tps, prompt_tps, generated, slots_active）===")
for r in rows:
    print("  %s" % (r,))
# 统计有效 TPS 行数
dec_vals = [r[2] for r in rows if r[2] is not None]
prp_vals = [r[3] for r in rows if r[3] is not None]
gen_vals = [r[4] for r in rows if r[4] is not None]
print("\n有效 decode_tps 行数: %d (min=%s max=%s)" % (
    len(dec_vals), min(dec_vals) if dec_vals else None, max(dec_vals) if dec_vals else None))
print("有效 prompt_tps 行数: %d" % len(prp_vals))
print("有效 generated 行数: %d (末=%s)" % (
    len(gen_vals), gen_vals[-1] if gen_vals else None))
