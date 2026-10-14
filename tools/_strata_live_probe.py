# 真实 Strata 生成中字段观察：发一个小请求，期间轮询 /status + /metrics.live + /slots
import json, time, urllib.request

BASE = "http://127.0.0.1:9091"

def get(path, timeout=5):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

# 1) 空闲基线
try:
    st = get("/status")
    print("IDLE /status:", json.dumps(st, ensure_ascii=False))
except Exception as e:
    print("IDLE /status FAIL:", e)
m0 = get("/metrics")
print("IDLE live:", json.dumps(m0.get("live"), ensure_ascii=False))

# 2) 发起一个短请求（非流式，~20 token），后台线程
import threading
req_err = []
def do_req():
    body = json.dumps({
        "model": "qwen3.8-flash-next-unsloth-ud-q4_k_xl",
        "messages": [{"role": "user", "content": "用一句话介绍你自己。"}],
        "max_tokens": 60,
    }).encode()
    r = urllib.request.Request(BASE + "/v1/chat/completions", data=body,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            data = json.loads(resp.read().decode())
            print("REQ done, usage:", json.dumps(data.get("usage"), ensure_ascii=False))
    except Exception as e:
        req_err.append(repr(e))

t = threading.Thread(target=do_req); t.start()

# 3) 期间高频轮询 /status 与 /metrics.live（100ms），抓生成中字段
seen = {}
t0 = time.time()
while t.is_alive() and time.time() - t0 < 25:
    try:
        st = get("/status", timeout=2)
        key = (st.get("busy"), st.get("phase"))
        if key not in seen:
            seen[key] = True
            print("BUSY /status:", json.dumps(st, ensure_ascii=False))
        try:
            m = get("/metrics", timeout=2)
            live = m.get("live") or {}
            if live.get("state") not in (None, "idle"):
                lk = (live.get("state"), live.get("phase"))
                if lk not in seen:
                    seen[lk] = True
                    print("BUSY live:", json.dumps(live, ensure_ascii=False)[:1200])
        except Exception:
            pass
    except Exception:
        pass
    time.sleep(0.1)
t.join(30)
print("req errors:", req_err)
m1 = get("/metrics")
print("AFTER totals:", json.dumps(m1.get("totals"), ensure_ascii=False))
print("AFTER conversation_cache:", json.dumps(m1.get("conversation_cache"), ensure_ascii=False))
print("AFTER requests[0]:", json.dumps((m1.get("requests") or [None])[0], ensure_ascii=False))
