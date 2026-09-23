# -*- coding: utf-8 -*-
"""1.0.0 Gate：Monitor 重启不丢基线 / 不重复计入（真实环境）。

T1: 记录 server /metrics + monitor total。
重启 monitor（POST /api/app/exit 经 loopback，再 PEXE --background）。
等待 monitor 回到在线 + collector 开始轮询。
T2: 断言 monitor total 未回退（每个 counter T2 >= T1），且与 server 差值
在合理范围（重启期间最多丢 [最后读取, 重启] 的增量，绝不重复计入）。
"""
import json, os, subprocess, sys, time, urllib.request

APP = "http://127.0.0.1:8765"
SERVER = "http://127.0.0.1:9091"
EXE = os.path.join(os.environ["LOCALAPPDATA"], "Programs", "LlamaMonitor",
                   "LlamaMonitor", "LlamaMonitor.exe")

def get(url, to=10):
    with urllib.request.urlopen(url, timeout=to) as r:
        return r.read().decode("utf-8", "replace")

def jget(url, to=10):
    return json.loads(get(url, to))

def server_totals():
    txt = get(SERVER + "/metrics")
    out = {}
    for line in txt.splitlines():
        if line.startswith("#") or " " not in line: continue
        n, _, v = line.partition(" ")
        if n in ("llamacpp:prompt_tokens_total", "llamacpp:tokens_predicted_total"):
            try: out[n.split(":")[1]] = int(float(v.strip()))
            except ValueError: pass
    return out

def monitor_totals():
    return jget(APP + "/api/summary")["total"]

def wait_restart(deadline=300):
    # 等退出
    d = time.time() + 60
    while time.time() < d:
        try:
            jget(APP + "/api/status", 3)
            time.sleep(1)
        except Exception:
            break
    subprocess.Popen([EXE, "--background"],
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    # 等回来 + collector 轮询
    d = time.time() + deadline
    while time.time() < d:
        try:
            st = jget(APP + "/api/status", 5)
            lu = st.get("last_update")
            if st.get("server_online") is True and lu and (time.time() - lu) < 20:
                return st
        except Exception:
            pass
        time.sleep(3)
    return None

def main():
    c1 = server_totals()
    m1 = monitor_totals()
    print("T1 server:", c1)
    print("T1 monitor total:", m1)
    # 制造一点活动，让重启前后 delta 可观测（可选；推理一次）
    try:
        req = urllib.request.Request(SERVER + "/completion",
            data=json.dumps({"prompt": "say hi in one word", "max_tokens": 16,
                             "temperature": 0, "stream": False}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            json.loads(r.read())
        print("pre-restart inference done")
    except Exception as e:
        print("inference err (non-fatal):", e)
    time.sleep(6)  # 让 monitor 采集这轮
    m1b = monitor_totals()
    print("T1b monitor total (post-inference):", m1b)

    print("=== restarting monitor ===")
    req = urllib.request.Request(APP + "/api/app/exit", method="POST", data=b"")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print("exit resp:", r.read()[:100])
    except Exception as e:
        print("exit err:", e)
    st = wait_restart(300)
    if not st:
        print("RESTART FAIL: monitor 未回来"); return 1
    print(f"monitor back: v{st.get('version')} uptime={st['application']['uptime_seconds']} online={st.get('server_online')}")
    time.sleep(7)  # 等重启后 ≥1 轮采集
    c2 = server_totals()
    m2 = monitor_totals()
    print("T2 server:", c2)
    print("T2 monitor total:", m2)

    fails = []
    for k in ("prompt_tokens", "cached_tokens", "output_tokens"):
        if m2[k] < m1b[k]:
            fails.append(f"monitor {k} 回退: {m1b[k]} -> {m2[k]}")
    # 重启期间 server 仍在出 token；monitor 可能丢 [最后读取,重启] 的增量，
    # 但绝不能超过 server 增量（重复计入 = monitor delta > server delta）
    for mk, sk in (("prompt_tokens", "prompt_tokens_total"), ("output_tokens", "tokens_predicted_total")):
        app_delta = m2[mk] - m1b[mk]
        srv_delta = c2.get(sk, 0) - c1.get(sk, 0)
        if app_delta > srv_delta:
            fails.append(f"monitor {mk} 增量 {app_delta} > server 增量 {srv_delta}（重复计入！）")
    if fails:
        print("MONITOR-RESTART FAIL:")
        for f in fails: print("  -", f)
        return 1
    print("MONITOR-RESTART PASS：基线保留、无回退、无重复计入")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
