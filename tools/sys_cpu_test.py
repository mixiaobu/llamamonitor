import psutil, time, sys
sys.path.insert(0, ".")

print("=== _downsample field bug (unit, deterministic) ===")
try:
    import server as S
    pts = [{"timestamp": float(i), "cpu_usage_percent": float(i % 10), "memory_usage_percent": 50.0,
            "disk_read_bps": 1000.0, "cpu_frequency_mhz": 3000.0, "utilization_percent": None}
           for i in range(3000)]
    out = S._downsample(pts, 2000)
    print("  in=%d out=%d"%(len(pts), len(out)))
    if out:
        o0 = out[0]
        print("  out[0] keys:", sorted(o0.keys()))
        print("  out[0].cpu_usage_percent = %s  -> %s"%(o0.get("cpu_usage_percent"),
              "BROKEN (None)" if o0.get("cpu_usage_percent") is None else "ok"))
        print("  out[0].utilization_percent(GPU-only field) = %s"%o0.get("utilization_percent"))
    # small input
    out2 = S._downsample(pts[:10], 2000)
    print("  <=max_points passthrough: out[0].cpu_usage_percent=%s"%out2[0].get("cpu_usage_percent"))
except Exception as e:
    print("  import/call failed:", repr(e))

print("\n=== AGGREGATE vs PER-CORE, back-to-back (tight), 8x over 8s ===")
psutil.cpu_percent(None); psutil.cpu_percent(None, percpu=True); time.sleep(0.3)
for i in range(8):
    a = psutil.cpu_percent(None)              # GetSystemTimes-based
    p = psutil.cpu_percent(None, percpu=True) # per_cpu_times-based
    if p:
        pm = sum(p)/len(p)
        n100 = sum(1 for x in p if x >= 99.9)
        # which half
        half = len(p)//2
        s1 = sum(p[:half])/half
        s2 = sum(p[half:])/len(p[half:])
        print("  #%d  AGG=%4.1f  permean=%4.1f (s1=%.1f s2=%.1f)  n@100=%d"%(i, a, pm, s1, s2, n100))
    time.sleep(1.0)

print("\n=== raw delta cross-check (3s window): GetSystemTimes vs sum(per-cpu) ===")
import psutil._pswindows as W
g0 = W.cpu_times()          # (user,system,idle,irq,softirq)
pc0 = W.per_cpu_times()     # list
time.sleep(3.0)
g1 = W.cpu_times()
pc1 = W.per_cpu_times()
def d(a,b): return b-a
# scputimes fields: user, system, idle, iowait(interrupt), irq(dpc), softirq
def busy_dt(t0, t1):
    dt = d(t0.user,t1.user)+d(t0.system,t1.system)+d(t0.idle,t1.idle)+d(t0.iowait,t1.iowait)+d(t0.irq,t1.irq)
    return dt, dt - d(t0.idle,t1.idle)
dt_g, busy_g = busy_dt(g0, g1)
print("  GetSystemTimes busy%% = %.2f"%(busy_g/dt_g*100 if dt_g else 0))
dt_p = 0; busy_p = 0
for a,b in zip(pc0,pc1):
    dt, bp = busy_dt(a, b)
    dt_p += dt; busy_p += bp
print("  sum(per-cpu) busy%%   = %.2f"%(busy_p/dt_p*100 if dt_p else 0))
print("  (these two SHOULD be ~equal; a big gap = Windows aggregation quirk on dual-socket)")
print("DONE")
