# 1.3.3 活体测试：100K+ token 长 Prompt（§36/§40）
# 发起长 Prompt 推理请求，同时 0.5s 轮询 /api/realtime 记录：
# - Prompt 处理阶段（state=reading, generated=0 -> active 不翻 idle）
# - Decode 阶段（state=generating, current_decode_tps 真实值）
# - token 进度（prompt/generated/max_tokens 推进）
# - HOT 循环耗时（负载下仍 <1s）
import json
import threading
import time
import urllib.request

BASE = "http://127.0.0.1:8790"
STRATA = "http://127.0.0.1:9091"


def get(path, base=BASE, timeout=3):
    with urllib.request.urlopen(base + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# 构造 100K+ token 长 Prompt（~500K 英文字符 ≈ 120K+ tokens）
para = ("The quick brown fox jumps over the lazy dog near the river bank where "
        "the old mill slowly turns and the water flows steadily downstream "
        "through the valley of tall green hills and quiet farming country. ")
UNIT = para * 6  # ~600 chars
n_units = 900    # ~540K chars
prompt_text = (
    "You are a careful summarizer. Read the long passage below and remember it. "
    "Then answer briefly when asked.\n\n" + UNIT * n_units +
    "\n\nQuestion: What animal jumps over which other animal in the repeated story?"
)
print(f"prompt chars={len(prompt_text)} (~{len(prompt_text)//4} tokens)")

result = {}


def infer():
    body = json.dumps({
        "messages": [{"role": "user", "content": prompt_text}],
        "max_tokens": 200, "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        STRATA + "/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            d = json.loads(r.read().decode("utf-8"))
        result["ok"] = True
        result["elapsed"] = time.time() - t0
        result["usage"] = d.get("usage")
        c = d["choices"][0]["message"]
        result["content_len"] = len((c.get("content") or "") + (c.get("reasoning_content") or ""))
    except Exception as e:
        result["ok"] = False
        result["error"] = repr(e)
        result["elapsed"] = time.time() - t0


th = threading.Thread(target=infer, daemon=True)
th.start()

# 0.5s 轮询 /api/realtime 记录阶段
prompt_phase = []   # (t, prompt_tokens, generated, max_tokens)
decode_phase = []   # (t, decode_tps, generated)
hot_durs = []
t0 = time.time()
deadline = 180
last_state = None

while time.time() - t0 < deadline:
    if not th.is_alive() and "ok" in result:
        break
    try:
        d = get("/api/realtime")
    except Exception as e:
        print(f"[{time.time()-t0:6.1f}s] realtime fetch err: {e!r}")
        time.sleep(0.5)
        continue
    act = d["activity"]
    tok = d["tokens"]
    thp = d["throughput"]
    hot_durs.append(d["diagnostics"]["last_hot_duration_ms"])
    key = (act.get("active"), act.get("state"), act.get("phase"))
    if key != last_state:
        print(f"[{time.time()-t0:6.1f}s] STATE -> active={key[0]} state={key[1]} "
              f"phase={key[2]} src={act.get('source')} conf={act.get('confidence')}")
        last_state = key
    if act.get("state") == "reading":
        prompt_phase.append((round(time.time() - t0, 1),
                             tok.get("prompt"), tok.get("generated"),
                             tok.get("max_tokens")))
    if act.get("state") == "generating":
        decode_phase.append((round(time.time() - t0, 1),
                             thp.get("current_decode_tps"),
                             tok.get("generated")))
    time.sleep(0.5)

th.join(timeout=10)
print("\n=== 推理结果 ===")
print(json.dumps(result, ensure_ascii=False))
print(f"\n=== Prompt 处理阶段样本（state=reading）：{len(prompt_phase)} 条 ===")
for s in prompt_phase[:5]:
    print("  t=%.1fs prompt=%s generated=%s max=%s" % s)
if len(prompt_phase) > 5:
    print("  ... 末条:", prompt_phase[-1])
print(f"\n=== Decode 阶段样本（state=generating）：{len(decode_phase)} 条 ===")
for s in decode_phase[:5]:
    print("  t=%.1fs decode_tps=%s generated=%s" % s)
if len(decode_phase) > 5:
    print("  ... 末条:", decode_phase[-1])
if hot_durs:
    hs = sorted(hot_durs)
    n = len(hs)
    print(f"\n=== HOT 负载下 last_duration_ms：min={hs[0]} p50={hs[n//2]} max={hs[-1]} avg={sum(hs)/n:.1f}（n={n}）")
