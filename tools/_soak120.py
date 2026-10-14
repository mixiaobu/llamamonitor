# 1.2.0 长稳内存监控：每 5 分钟采样主进程/Bridge 资源 + DB 体积 + 表行数
# 用法：python _soak120.py <duration_minutes> [out_csv]
import csv, os, sys, time

import psutil

DB = r"C:\Users\mixiaobu\AppData\Local\LlamaMonitor\monitor.db"
DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
OUT = sys.argv[2] if len(sys.argv) > 2 else r"artifacts\soak120.csv"

t_end = time.time() + DURATION * 60.0
t0 = time.time()

def db_stats():
    import sqlite3
    try:
        db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        counts = {}
        for t in ("live_samples", "gpu_samples", "system_samples"):
            counts[t] = db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        db.close()
        return counts
    except Exception as e:
        return {"err": str(e)}

def proc_stats(pid):
    try:
        p = psutil.Process(pid)
        return {
            "rss_mb": round(p.memory_info().rss / 1048576, 1),
            "handles": p.num_handles(),
            "threads": p.num_threads(),
            "cpu_pct": round(p.cpu_percent(None), 1),
        }
    except Exception:
        return None

def find_dev_server():
    """找项目目录下的 python server.py（排除 unittest；取 RSS 最大者 = 真实 server）"""
    best = (0, None)
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cl = p.info["cmdline"] or []
        except Exception:
            continue
        if len(cl) < 2:
            continue
        if "server.py" in cl[-1] and "LlamaMonitor" in cl[0] and "unittest" not in " ".join(cl):
            try:
                rss = p.memory_info().rss
            except Exception:
                rss = 0
            if rss > best[0]:
                best = (rss, p.pid)
    return best[1]

main_pid = find_dev_server()
print("dev server pid:", main_pid)
if not main_pid:
    sys.exit(1)

def find_bridges(ppid):
    out = []
    for p in psutil.process_iter(["pid", "ppid", "name"]):
        try:
            if p.info["ppid"] == ppid and "HardwareSensorBridge" in (p.info["name"] or ""):
                out.append(p.pid)
        except Exception:
            continue
    return out

rows = []
with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["t_min", "main_rss_mb", "main_handles", "main_threads", "main_cpu",
                "bridge_rss_mb", "bridge_cpu", "db_mb", "wal_mb",
                "live_rows", "gpu_rows", "sys_rows"])
    while time.time() < t_end:
        ms = proc_stats(main_pid)
        if ms is None:
            print("main process gone at t=%.1f" % ((time.time() - t0) / 60), flush=True)
            break
        bpids = find_bridges(main_pid)
        b_rss, b_cpu = 0.0, 0.0
        for b in bpids:
            bs = proc_stats(b)
            if bs:
                b_rss += bs["rss_mb"]
                b_cpu += bs["cpu_pct"]
        try:
            db_mb = round(os.path.getsize(DB) / 1048576, 1)
        except Exception:
            db_mb = -1
        wal = DB + "-wal"
        try:
            wal_mb = round(os.path.getsize(wal) / 1048576, 1)
        except Exception:
            wal_mb = 0
        counts = db_stats()
        tmin = round((time.time() - t0) / 60, 2)
        row = [tmin, ms["rss_mb"], ms["handles"], ms["threads"], ms["cpu_pct"],
               round(b_rss, 1), round(b_cpu, 1), db_mb, wal_mb,
               counts.get("live_samples"), counts.get("gpu_samples"), counts.get("system_samples")]
        w.writerow(row)
        f.flush()
        print("t=%6.2f min  main RSS=%s MB handles=%s threads=%s cpu=%s%%  bridge RSS=%s MB  db=%s MB  live=%s gpu=%s sys=%s" % (
            tmin, ms["rss_mb"], ms["handles"], ms["threads"], ms["cpu_pct"],
            round(b_rss, 1), db_mb, counts.get("live_samples"), counts.get("gpu_samples"), counts.get("system_samples")), flush=True)
        # cpu_percent 首次调用返回 0——先预热
        if time.time() >= t_end:
            break
        time.sleep(300)
print("soak done")
