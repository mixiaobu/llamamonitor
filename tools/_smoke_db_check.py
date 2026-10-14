"""1.3.0 smoke DB 检查：Strata 事件 / 缺口 / daily / 来源明细 / state 键。"""
import sys
sys.path.insert(0, r"C:\Users\mixiaobu\Desktop\ai\LlamaMonitor")
from db import Database

d = Database(r"C:\Users\mixiaobu\AppData\Local\Temp\lm_smoke_130.db", wal=True)
conn = d._connect()
print("=== strata events (newest 12) ===")
rows = conn.execute(
    "SELECT event_type, details_json FROM monitor_events "
    "WHERE source='strata' ORDER BY timestamp DESC LIMIT 12"
).fetchall()
for t, det in rows:
    print(t, "|", (det or "")[:110])
print("=== gaps ===")
print(tuple(conn.execute("SELECT COUNT(*) FROM data_gaps").fetchone()))
print("=== daily_usage (newest 2) ===")
for r in conn.execute("SELECT date, prompt_tokens, cached_tokens, output_tokens FROM daily_usage ORDER BY date DESC LIMIT 2"):
    print(tuple(r))
print("=== daily_usage_by_source ===")
for r in conn.execute("SELECT date, provider, server_id, prompt_tokens, cached_tokens, output_tokens FROM daily_usage_by_source LIMIT 5"):
    print(tuple(r))
print("=== state values (strata totals) ===")
for r in conn.execute("SELECT metric_name, value FROM state WHERE metric_name LIKE 'strata:%'"):
    print(tuple(r))
print("=== live_samples: nonzero deltas (last 30) ===")
for r in conn.execute(
    "SELECT timestamp, prompt_delta, cached_delta, output_delta, prompt_tps, decode_tps "
    "FROM live_samples ORDER BY timestamp DESC LIMIT 30"
):
    if any(v for v in r[1:4]):
        print(tuple(r))
d.close()
