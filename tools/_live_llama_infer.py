# 1.3.3 活体测试（llama.cpp §38）：长 Prompt + decode，0.5s 轮询 /api/realtime
import json
import threading
import time
import urllib.request

BASE = "http://127.0.0.1:8790"
LLAMA = "http://127.0.0.1:9091"


def get(path, timeout=3):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ~24K token 长 Prompt（96K 英文字符）
para = ("The quick brown fox jumps over the lazy dog near the river bank where "
        "the old mill slowly turns and the water flows steadily downstream "
        "through the valley of tall green hills and quiet farming country. ")
prompt_text = (
    "Read the passage below. When I ask, tell me the animal that jumps.\n\n"
    + para * 260 +
    "\nWhat animal jumps over the lazy dog?"
)
print("prompt chars=%d (~%d tokens)" % (len(prompt_text), len(prompt_text) // 4))

result = {}


def infer():
    body = json.dumps({
        "messages": [{"role": "user", "content": prompt_text}],
        "max_tokens": 120, "stream": False,
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
        result["elapsed"] = round(time.time() - t0, 1)


th = threading.Thread(target=infer, daemon=True)
th.start()

prompt_rows = []
decode_rows = []
hot_durs = []
last_key = None
t0 = time.time()
while time.time() - t0 < 240:
    if not th.is_alive() and "ok" in result:
        break
    try:
        d = get("/api/realtime")
    except Exception as e:
        print("t=%6.1f realtime err: %r" % (time.time() - t0, e))
        time.sleep(0.5)
        continue
    a = d["activity"]
    tk = d["tokens"]
    thp = d["throughput"]
    hot_durs.append(d["diagnostics"]["last_hot_duration_ms"])
    key = (a.get("active"), a.get("state"), a.get("phase"))
    if key != last_key:
        print("t=%6.1f STATE -> active=%s state=%s phase=%s src=%s conf=%s" % (
            time.time() - t0, key[0], key[1], key[2],
            a.get("source"), a.get("confidence")))
        last_key = key
    if a.get("state") == "prompt":
        prompt_rows.append((round(time.time() - t0, 1), tk.get("generated"),
                            thp.get("current_prompt_tps")))
    if a.get("state") == "decode":
        decode_rows.append((round(time.time() - t0, 1),
                            thp.get("current_decode_tps"), tk.get("generated")))
    time.sleep(0.5)

th.join(timeout=10)
print("\n=== 推理结果 ===")
print(json.dumps(result, ensure_ascii=False))
print("\n=== Prompt 阶段（state=prompt）：%d 条 ===" % len(prompt_rows))
for s in prompt_rows[:4]:
    print("  t=%.1fs generated=%s prompt_tps=%s" % s)
if len(prompt_rows) > 4:
    print("  ... 末条:", prompt_rows[-1])
print("\n=== Decode 阶段（state=decode）：%d 条 ===" % len(decode_rows))
for s in decode_rows[:8]:
    print("  t=%.1fs decode_tps=%s generated=%s" % s)
if len(decode_rows) > 8:
    print("  ... 末条:", decode_rows[-1])
if hot_durs:
    hs = sorted(hot_durs)
    n = len(hs)
    print("\n=== HOT 负载下 last_duration_ms：min=%d p50=%d max=%d avg=%.1f（n=%d）===" % (
        hs[0], hs[n // 2], hs[-1], sum(hs) / n, n))
    print("hot_overrun_count=%d" % get("/api/realtime")["diagnostics"]["hot_overrun_count"])
