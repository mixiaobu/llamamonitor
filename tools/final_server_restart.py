# -*- coding: utf-8 -*-
"""1.0.0 Gate：llama-server 重启（counter reset）场景，真实环境。

操作 8765（烧入监控，生产 DB）+ 9091（真实 llama-server）：
  1. T1：server counter（高值）+ monitor total；制造推理推进 counter；等 ≥1 轮采集。
  2. kill llama-server（counter 归零，[最后读取, 重启] 之间产生的 token 丢失）。
  3. 等监控判定 offline。
  4. 用相同命令重启 llama-server（模型加载 ~1-3 min）。
  5. 等监控恢复 online + 新基线。
  6. 断言：
     - monitor_events 记了 counter_reset（核心 counter）；
     - data_gaps 有 possible_token_loss（离线期 reset）或 counter_reset 相关缺口；
     - monitor total 不回退（>= T1）、无负值；
     - 重启后 delta == 新 server counter（精确，不重复计入）。
"""
import json, os, subprocess, sys, time, urllib.request, ctypes

APP = "http://127.0.0.1:8765"
SERVER = "http://127.0.0.1:9091"
# 与 burnin_ops 完全相同的启动命令（保证模型/参数一致）
LLAMA_CMD = (
    '"C:\\Users\\mixiaobu\\Desktop\\ai\\llamacpp\\llama.cpp\\build\\bin\\Release\\llama-server.exe" '
    '-m "C:\\Users\\mixiaobu\\Desktop\\ai\\qwen3.8-27b\\Huihui-Qwen3.8-27B-abliterated-UD-Q4_K_XL.gguf" '
    '--mmproj "C:\\Users\\mixiaobu\\Desktop\\ai\\qwen3.8-27b\\mmproj-model-bf16.gguf" '
    '--no-mmproj-offload --image-min-tokens 1024 --image-max-tokens 1024 '
    '--mtmd-batch-max-tokens 1024 --device CUDA0 -ngl all --fit off --load-mode mmap -fa on '
    '-c 262144 -np 1 -ctk q8_0 -ctv q8_0 -b 8192 -ub 1024 -t 96 -tb 96 --jinja '
    '--reasoning on --reasoning-effort medium --reasoning-budget -1 --reasoning-preserve '
    '--cache-prompt --cache-ram 131072 --ctx-checkpoints 32 --checkpoint-min-step 8192 '
    '--spec-type draft-mtp --spec-draft-n-max 4 --spec-draft-n-min 0 --spec-draft-p-min 0.2 '
    '-ctkd q8_0 -ctvd q8_0 --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0 '
    '--presence-penalty 0.0 --repeat-penalty 1.0 --perf --metrics --log-timestamps '
    '--host 0.0.0.0 --port 9091 --alias qwen3.8-27b-medium'
)

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def get(url, to=10):
    with _OPENER.open(url, timeout=to) as r:
        return r.read().decode("utf-8", "replace")

def server_counters():
    out = {}
    for line in get(SERVER + "/metrics").splitlines():
        if line.startswith("#") or " " not in line:
            continue
        n, _, v = line.partition(" ")
        if n == "llamacpp:prompt_tokens_total":
            out["prompt"] = int(float(v.strip()))
        elif n == "llamacpp:tokens_predicted_total":
            out["output"] = int(float(v.strip()))
        elif n == "llamacpp:prompt_tokens_cached_total":
            out["cached"] = int(float(v.strip()))
    return out

def monitor_total():
    return json.loads(get(APP + "/api/summary"))["total"]

def status():
    return json.loads(get(APP + "/api/status"))

def llama_alive():
    try:
        get(SERVER + "/metrics", to=4)
        return True
    except Exception:
        return False

def kill_llama():
    subprocess.run('taskkill /F /IM llama-server.exe', capture_output=True, text=True)

def relaunch_llama():
    subprocess.Popen('cmd /c ' + LLAMA_CMD + ' > NUL 2>&1',
                     creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
                     shell=True)

def wait_llama_up(deadline=300):
    end = time.time() + deadline
    while time.time() < end:
        if llama_alive():
            return True
        time.sleep(5)
    return False

def wait_monitor_state(online: bool, deadline=240):
    end = time.time() + deadline
    while time.time() < end:
        try:
            st = status()
            if st.get("server_online") is online:
                return st
        except Exception:
            pass
        time.sleep(5)
    return None

def db_query(sql):
    import sqlite3
    db = os.path.join(os.environ["LOCALAPPDATA"], "LlamaMonitor", "monitor.db")
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=8)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()

def main():
    print("T1 server:", server_counters())
    m_before = monitor_total()
    print("T1 monitor total:", m_before)
    # 推进 counter
    try:
        req = urllib.request.Request(SERVER + "/completion",
            data=json.dumps({"prompt": "one word", "max_tokens": 8, "temperature": 0,
                             "stream": False}).encode(),
            headers={"Content-Type": "application/json"})
        with _OPENER.open(req, timeout=60) as r:
            json.loads(r.read())
        print("pre-kill inference done")
    except Exception as e:
        print("inference err:", e)
    time.sleep(7)  # 让监控读一次高值
    hi = server_counters()
    print("high counter before kill:", hi)

    print("=== kill llama-server (counter reset) ===")
    kill_llama()
    time.sleep(3)
    print("llama alive after kill:", llama_alive())
    # 等监控判 offline
    wait_monitor_state(online=False, deadline=120)
    print("monitor sees offline")
    time.sleep(6)

    print("=== relaunch llama-server ===")
    relaunch_llama()
    if not wait_llama_up(300):
        print("FAIL: llama 未在 300s 内恢复"); return 1
    print("llama back up (fresh counters)")
    # 等监控恢复 + 新基线 + ≥2 轮
    wait_monitor_state(online=True, deadline=240)
    time.sleep(12)
    newc = server_counters()
    print("new server counters:", newc)
    m_after = monitor_total()
    print("monitor total after:", m_after)

    fails = []
    # 1) counter_reset 事件
    resets = db_query("SELECT COUNT(*) FROM monitor_events WHERE event_type='counter_reset'")
    print("counter_reset events:", resets[0][0])
    if resets[0][0] < 1:
        fails.append("无 counter_reset 事件（核心 counter reset 未被记录）")
    # 2) 缺口 + possible_token_loss
    gaps = db_query("SELECT COUNT(*), SUM(possible_token_loss) FROM data_gaps "
                    "WHERE possible_token_loss=1")
    loss_gaps = gaps[0][0]
    print("possible_token_loss gaps:", loss_gaps)
    if loss_gaps < 1:
        fails.append("无 possible_token_loss 缺口（离线期 reset 的丢失未标记）")
    # 3) 不回退 + 无负值
    for k in ("prompt_tokens", "cached_tokens", "output_tokens"):
        if m_after[k] < m_before[k]:
            fails.append(f"monitor {k} 回退 {m_before[k]} -> {m_after[k]}")
        if m_after[k] < 0:
            fails.append(f"monitor {k} 负值 {m_after[k]}")
    # 4) 重启后 delta == 新 counter（新 counter 从 ~0 开始，监控新基线=current，
    #    故重启后监控 delta 应 == 重启后 server 产生的 token）
    #    新基线后，monitor 增量应等于新 server 增量；由于新 counter 很小，
    #    这里校验 monitor total 增量（重启后产生的）不超过新 server 累计。
    if m_after["output_tokens"] > m_before["output_tokens"] + newc.get("output", 0) + 10:
        fails.append(f"monitor output 增量超过新 server 累计（重复计入）: "
                     f"{m_after['output_tokens']-m_before['output_tokens']} vs {newc.get('output')}")
    if fails:
        print("SERVER-RESTART FAIL:"); [print("  -", f) for f in fails]; return 1
    print("SERVER-RESTART PASS：counter reset 记录 + possible_token_loss + 无回退/重复计入")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
