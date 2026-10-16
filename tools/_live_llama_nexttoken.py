# 捕捉 busy slot 的 next_token 结构 + 更长生成
import json
import threading
import time
import urllib.request

LLAMA = "http://127.0.0.1:9091"


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
        result["finish"] = d["choices"][0].get("finish_reason")
    except Exception as e:
        result["ok"] = False
        result["error"] = repr(e)


th = threading.Thread(target=infer, daemon=True)
th.start()

captured = None
t0 = time.time()
while time.time() - t0 < 150:
    if not th.is_alive() and "ok" in result:
        break
    try:
        for s in raw_slots():
            if s.get("is_processing") and captured is None:
                nt = s.get("next_token")
                captured = {
                    "slot_n_decoded": s.get("n_decoded"),
                    "n_prompt_tokens": s.get("n_prompt_tokens"),
                    "n_prompt_tokens_processed": s.get("n_prompt_tokens_processed"),
                    "next_token": nt,
                }
                print("CAPTURED busy slot:")
                print(json.dumps(captured, ensure_ascii=False, indent=1))
    except Exception:
        pass
    time.sleep(0.3)

th.join(timeout=10)
print("\n=== 结果 ===")
print(json.dumps(result, ensure_ascii=False))
