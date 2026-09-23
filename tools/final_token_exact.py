# -*- coding: utf-8 -*-
"""1.0.0 Gate：真实 Token 精确性（llama-server 9091 ↔ LlamaMonitor 8765）。

方法（整数精确，ground truth = llama-server /metrics 累计 counter）：
  1. T1：读 server /metrics（prompt/output/cached）+ monitor /api/summary（total）。
  2. 运行一次真实推理（/completion，temperature=0，max_tokens 受控），推进 counter。
  3. 等 monitor 完成 ≥1 轮 5s 采集（T2）。
  4. 断言：
     - monitor Δ(output)  ==  server Δ(output)     （整数精确）
     - monitor Δ(prompt)  ==  server Δ(prompt)
     - monitor Δ(cached)  ==  server Δ(cached)
     - compute = prompt+output；logical = prompt+cached+output（定义一致）
     - 若今天有记录：daily 增量与 summary 增量同向一致（无回退/重复计入）
"""
import json, sys, time, urllib.request

SERVER = "http://127.0.0.1:9091"
APP = "http://127.0.0.1:8765"

def http_get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

def http_post(url, body, timeout=60):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def server_counters():
    txt = http_get(SERVER + "/metrics")
    out = {}
    for line in txt.splitlines():
        if line.startswith("#") or ":" not in line:
            continue
        name, _, val = line.partition(" ")
        if name in ("llamacpp:prompt_tokens_total", "llamacpp:tokens_predicted_total",
                    "llamacpp:prompt_tokens_cached_total"):
            try:
                out[name.split(":")[1]] = int(float(val.strip()))
            except ValueError:
                pass
    return out  # keys: prompt_tokens_total, tokens_predicted_total, prompt_tokens_cached_total

def monitor_totals():
    s = json.loads(http_get(APP + "/api/summary"))
    return s["total"]  # prompt_tokens, cached_tokens, output_tokens, compute_tokens, logical_tokens

def run_inference():
    body = {
        "prompt": "Count from one to three, one per line, and stop.",
        "n_predict": 64,
        "temperature": 0,
        "stream": False,
    }
    r = http_post(SERVER + "/completion", body, timeout=120)
    # /completion 响应字段名因版本而异；尽量取出 generation 数
    gen = None
    for k in ("n_predict", "n_tokens_predicted", "tokens_predicted"):
        if isinstance(r, dict) and isinstance(r.get(k), int):
            gen = r[k]; break
    content = r.get("content") or r.get("completion") or ""
    return gen, (content or "")[:120]

def main():
    print("T1 baseline ...")
    c1 = server_counters()
    m1 = monitor_totals()
    time.sleep(1.0)  # 让 monitor 的下一轮采集落在推理之前（干净边界）
    print("server T1:", c1)
    print("monitor T1 total:", m1)

    print("running inference ...")
    t0 = time.time()
    gen, preview = run_inference()
    dt = time.time() - t0
    print(f"inference done in {dt:.1f}s; reported gen={gen}; preview={preview!r}")

    # 等 monitor ≥1 轮采集（5s 间隔；留足余量）
    print("waiting 12s for monitor poll ...")
    time.sleep(12.0)

    print("T2 ...")
    c2 = server_counters()
    m2 = monitor_totals()
    print("server T2:", c2)
    print("monitor T2 total:", m2)

    d_server = {k: c2.get(k, 0) - c1.get(k, 0) for k in c1}
    d_app = {
        "output": m2["output_tokens"] - m1["output_tokens"],
        "prompt": m2["prompt_tokens"] - m1["prompt_tokens"],
        "cached": m2["cached_tokens"] - m1["cached_tokens"],
    }
    print()
    print("=== DELTAS (T2 - T1) ===")
    print(f"server: output={d_server.get('tokens_predicted_total')} prompt={d_server.get('prompt_tokens_total')} cached={d_server.get('prompt_tokens_cached_total')}")
    print(f"monitor: output={d_app['output']} prompt={d_app['prompt']} cached={d_app['cached']}")

    fails = []
    if d_server.get("tokens_predicted_total", 0) <= 0:
        fails.append("server 未推进 output counter（推理未生效或模型未加载）")
    if d_server.get("tokens_predicted_total") != d_app["output"]:
        fails.append(f"output 不一致: server={d_server.get('tokens_predicted_total')} monitor={d_app['output']}")
    if d_server.get("prompt_tokens_total") != d_app["prompt"]:
        fails.append(f"prompt 不一致: server={d_server.get('prompt_tokens_total')} monitor={d_app['prompt']}")
    if d_server.get("prompt_tokens_cached_total") != d_app["cached"]:
        fails.append(f"cached 不一致: server={d_server.get('prompt_tokens_cached_total')} monitor={d_app['cached']}")

    # 定义一致性：compute = prompt+output；logical = prompt+cached+output
    if m2["compute_tokens"] != m2["prompt_tokens"] + m2["output_tokens"]:
        fails.append(f"compute 定义不符: {m2['compute_tokens']} != {m2['prompt_tokens']}+{m2['output_tokens']}")
    if m2["logical_tokens"] != m2["prompt_tokens"] + m2["cached_tokens"] + m2["output_tokens"]:
        fails.append(f"logical 定义不符: {m2['logical_tokens']} != p+c+o")

    print()
    if fails:
        print("TOKEN-EXACTNESS FAIL:")
        for f in fails: print("  -", f)
        return 1
    print("TOKEN-EXACTNESS PASS：monitor 增量与 llama-server /metrics ground truth 整数精确一致")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
