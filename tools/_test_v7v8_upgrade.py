"""临时验证：v7 旧库（带旧数据）-> Database 打开 -> 迁移到 v8，旧数据保留。"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db as dbmod  # noqa: E402

td = tempfile.mkdtemp()
p = os.path.join(td, "old.db")
conn = sqlite3.connect(p)
conn.executescript(dbmod._SCHEMA_V1)
for v in (1, 2, 3, 4, 5, 6, 7):
    dbmod._MIGRATIONS[v](conn)
    conn.execute(f"PRAGMA user_version = {v + 1}")
conn.execute("INSERT INTO daily_usage(date, prompt_tokens, output_tokens) VALUES('2026-10-15', 100, 50)")
conn.execute("INSERT INTO state(metric_name, value) VALUES('llamacpp:prompt_tokens_total', 1234.0)")
conn.commit()
conn.close()

d = dbmod.Database(p, wal=False)
assert d.get_schema_version() == 8, f"期望 8，实际 {d.get_schema_version()}"
daily = d.get_daily_usage()
assert daily and daily[0]["prompt_tokens"] == 100, f"旧 daily_usage 丢失: {daily}"
tables = {r[0] for r in d._connect().execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('latency_histogram','strata_hardware')")}
assert tables == {"latency_histogram", "strata_hardware"}, tables
# 新表可写
d.record_latency_histogram("h:1", "ttft", "m", {"0.1": 1.0}, now=1000.0)
assert d.get_latency_histogram("ttft", 0, 9999) == {"0.1": 1.0}
d.close()
print("V7->V8 UPGRADE: OK（旧数据保留 + 新表可用）")
