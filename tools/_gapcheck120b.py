# 细查：最近 120s 各表/各设备行数 + 间隔分布
import sqlite3, time, statistics

DB = r"C:\Users\mixiaobu\AppData\Local\LlamaMonitor\monitor.db"
db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
now = time.time()

# gpu_samples 按设备分（timestamp 列）
rows = db.execute(
    "SELECT gpu_uuid, COUNT(*), MIN(timestamp), MAX(timestamp) FROM gpu_samples WHERE timestamp > ? GROUP BY gpu_uuid",
    (now - 120,)).fetchall()
for r in rows:
    print(f"gpu {str(r[0])[:20]:20s} rows120s={r[1]:4d}  first={int(now - r[2])}s ago last={int(now - r[3])}s ago")

for tbl, col in (("system_samples", "timestamp"), ("live_samples", "timestamp")):
    n = db.execute(f"SELECT COUNT(*) FROM {tbl} WHERE {col} > ?", (now - 120,)).fetchone()[0]
    print(f"{tbl:16s} rows120s={n}")
db.close()
