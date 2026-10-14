# 2s 全链路验证：各采样表最近 40s 的行密度 + 间隔分布
import sqlite3, time, statistics

DB = r"C:\Users\mixiaobu\AppData\Local\LlamaMonitor\monitor.db"
db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
print("tables:", ", ".join(tables))

now = time.time()
for tbl in tables:
    cols = [r[1] for r in db.execute(f"PRAGMA table_info({tbl})")]
    tscol = next((c for c in cols if c in ("ts", "timestamp", "t", "ts_s", "sample_ts", "created_at")), None)
    if tscol is None:
        continue
    try:
        rows = db.execute(f"SELECT {tscol} FROM {tbl} WHERE {tscol} > ? ORDER BY {tscol}", (now - 40,)).fetchall()
    except Exception as e:
        print(f"{tbl:22s} ERR {e}")
        continue
    ts = [r[0] for r in rows]
    if len(ts) >= 2:
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        print(f"{tbl:22s} rows={len(ts):3d}  min={min(gaps):.1f}s max={max(gaps):.1f}s median={statistics.median(gaps):.1f}s")
    else:
        print(f"{tbl:22s} rows={len(ts)} (too few)")
db.close()
