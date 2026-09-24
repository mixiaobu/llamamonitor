# -*- coding: utf-8 -*-
"""1.0.0 Gate：首次启动首基线（fresh DB，真实 llama-server 9091 高累计计数）。

dev 模式（server.py，无 SingleInstance mutex）+ 隔离 LOCALAPPDATA + 独立端口 8766，
与正在跑的 4h 烧入（8765）互不干扰。

核心断言（首基线场景）：
  P1) 不把历史当增量：连到一个已累计数万的 server，fresh 监控的首扫设基线=current，
      之后 daily/summary 远小于 server 累计（≪），即不把 [0, 累计] 全记成今日。
  P2) 首基线后 token 精确：触发一次真实推理，监控增量 == server 增量（精确整数）。
  P3) fresh DB schema v4、10 表、quick_check ok、无负值。
"""
import json, os, sqlite3, time, urllib.request

APP = "http://127.0.0.1:8766"
SERVER = "http://127.0.0.1:9091"
ISOLATED = os.environ["LM_ISOLATED_LAD"]
DB = os.path.join(ISOLATED, "LlamaMonitor", "monitor.db")

# 与 app httpx 行为一致：loopback 不走系统代理（死代理会让 urllib 走 127.0.0.1:10808 超时）
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

def wait_online(timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        try:
            st = json.loads(get(APP + "/api/status"))
            if st.get("server_online") is True:
                return st
        except Exception:
            pass
        time.sleep(2)
    return None

def main():
    st = wait_online(120)
    if not st:
        print("FAIL: 120s 内 server_online != True"); return 1
    print(f"online v{st.get('version')}")
    # 等首扫设基线 + 一轮
    time.sleep(11)
    sb0 = server_counters()
    m0 = monitor_total()
    print("server 累计:", sb0)
    print("monitor total（首基线后）:", m0)
    fails = []
    # P1) 不把历史当增量：monitor total 远小于 server 累计
    for mk, sk in (("prompt_tokens", "prompt"), ("output_tokens", "output")):
        if sb0.get(sk, 0) > 1000 and m0[mk] > sb0[sk] * 0.5:
            fails.append(f"P1 {mk}: monitor {m0[mk]} 接近 server 累计 {sb0[sk]}（历史被当增量）")
    for k in ("prompt_tokens", "cached_tokens", "output_tokens"):
        if m0[k] < 0:
            fails.append(f"P3 total {k} 负值 {m0[k]}")
    # P2) 真实推理 → 精确 delta
    sa = server_counters()
    ma = monitor_total()
    try:
        req = urllib.request.Request(SERVER + "/completion",
            data=json.dumps({"prompt": "count to five", "max_tokens": 48,
                             "temperature": 0, "stream": False}).encode(),
            headers={"Content-Type": "application/json"})
        with _OPENER.open(req, timeout=60) as r:
            json.loads(r.read())
        print("inference done")
    except Exception as e:
        print("inference err:", e)
    time.sleep(13)  # ≥2 个 5s 采集周期
    sb = server_counters()
    mb = monitor_total()
    print("server delta:", {k: sb.get(k,0)-sa.get(k,0) for k in ("prompt","output","cached")})
    for mk, sk in (("prompt_tokens","prompt"),("output_tokens","output"),("cached_tokens","cached")):
        app_d = mb[mk]-ma[mk]; srv_d = sb.get(sk,0)-sa.get(sk,0)
        print(f"  delta {mk}: monitor={app_d} server={srv_d}")
        if app_d != srv_d:
            fails.append(f"P2 delta {mk}: monitor {app_d} != server {srv_d}")
    # P3) DB 完整性
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    ver = c.execute("PRAGMA user_version").fetchone()[0]
    if ver != 4: fails.append(f"P3 schema user_version={ver}")
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for need in ("daily_usage","live_samples","gpu_samples","gpu_daily","mtp_position_daily",
                 "data_gaps","monitor_events","state","backup_history","app_state"):
        if need not in tables: fails.append(f"P3 缺表 {need}")
    qc = c.execute("PRAGMA quick_check").fetchone()
    if not qc or qc[0] != "ok": fails.append(f"P3 quick_check={qc}")
    c.close()

    if fails:
        print("FIRST-BASELINE FAIL:"); [print("  -", f) for f in fails]; return 1
    print("FIRST-BASELINE PASS：首基线不导入历史、delta 精确、schema v4、quick_check OK")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
