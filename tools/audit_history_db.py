import sys, os, sqlite3
sys.path.insert(0, '.')
p = os.path.join(os.environ['LOCALAPPDATA'], 'LlamaMonitor', 'monitor.db')
conn = sqlite3.connect(p)
conn.row_factory = sqlite3.Row

print('=== monitor_events: event_type x severity x source ===')
for r in conn.execute('SELECT event_type,severity,source,COUNT(*) c FROM monitor_events GROUP BY event_type,severity,source ORDER BY c DESC'):
    print('  %-36s %-9s %-14s %d' % (r['event_type'], r['severity'], r['source'], r['c']))

print()
print('=== data_gaps: source x reason x token_recoverable x possible_token_loss ===')
for r in conn.execute('SELECT source,reason,token_recoverable,possible_token_loss,COUNT(*) c FROM data_gaps GROUP BY source,reason,token_recoverable,possible_token_loss ORDER BY c DESC'):
    print('  src=%-10s reason=%-28s recov=%d loss=%d  n=%d' % (r['source'], r['reason'], r['token_recoverable'], r['possible_token_loss'], r['c']))

print()
row = conn.execute('SELECT MIN(datetime(start_timestamp,"unixepoch")) a, MAX(datetime(start_timestamp,"unixepoch")) b FROM data_gaps').fetchone()
print('data_gaps span:', row['a'], '->', row['b'])
row = conn.execute('SELECT MIN(datetime(timestamp,"unixepoch")) a, MAX(datetime(timestamp,"unixepoch")) b FROM monitor_events').fetchone()
print('monitor_events span:', row['a'], '->', row['b'])
row = conn.execute('SELECT MIN(datetime(timestamp,"unixepoch")) a, MAX(datetime(timestamp,"unixepoch")) b FROM live_samples').fetchone()
print('live_samples span:', row['a'], '->', row['b'])
row = conn.execute('SELECT MIN(datetime(timestamp,"unixepoch")) a, MAX(datetime(timestamp,"unixepoch")) b FROM gpu_samples').fetchone()
print('gpu_samples span:', row['a'], '->', row['b'])

# sample a few details_json to see shape
print()
print('=== sample monitor_events details (first 8 distinct event_type) ===')
seen = set()
for r in conn.execute('SELECT event_type, details_json, severity FROM monitor_events ORDER BY id DESC'):
    if r['event_type'] in seen:
        continue
    seen.add(r['event_type'])
    print('  %s [%s]: %s' % (r['event_type'], r['severity'], (r['details_json'] or '{}')[:200]))
    if len(seen) >= 12:
        break
