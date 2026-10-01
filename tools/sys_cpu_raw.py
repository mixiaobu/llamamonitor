import psutil, time, collections
# Discover actual scputimes fields
g0 = psutil.cpu_times(percpu=False)
print("scputimes fields:", g0._fields)
def busyfrac(t0, t1):
    tot0 = sum(getattr(t0, f, 0) for f in t0._fields)
    tot1 = sum(getattr(t1, f, 0) for f in t1._fields)
    idlef = "idle"
    idle0 = getattr(t0, idlef); idle1 = getattr(t1, idlef)
    dtot = tot1 - tot0
    if dtot <= 0: return None
    busy = dtot - (idle1 - idle0)
    return busy / dtot * 100.0

print("\n=== 4s raw cross-check: AGG(cpu_percent source) vs per-core mean ===")
g0 = psutil.cpu_times(percpu=False)
p0 = psutil.cpu_times(percpu=True)
time.sleep(4.0)
g1 = psutil.cpu_times(percpu=False)
p1 = psutil.cpu_times(percpu=True)
agg = busyfrac(g0, g1)
# per-core mean = sum busy deltas / sum total deltas over ALL cores (true system-wide)
bd = td = 0.0
for a, b in zip(p0, p1):
    tot1 = sum(getattr(b, f, 0) for f in b._fields); tot0 = sum(getattr(a, f, 0) for f in a._fields)
    dt = tot1 - tot0
    if dt > 0:
        td += dt
        bd += dt - ((b.idle) - (a.idle))
permean = bd / td * 100.0 if td > 0 else 0.0
print("  AGG (cpu_times(percpu=False), GetSystemTimes) = %.2f%%"%(agg or 0))
print("  per-core MEAN (all %d logical)                = %.2f%%"% (len(p0), permean))
h = len(p0)//2
def hm(lo,hi):
    b=t=0.0
    for i in range(lo,hi):
        a=p0[i];bb=p1[i]; t1=sum(getattr(bb,f,0) for f in bb._fields); t0=sum(getattr(a,f,0) for f in a._fields); dt=t1-t0
        if dt>0:
            t+=dt; b+=dt-(bb.idle-a.idle)
    return b/t*100.0 if t>0 else 0.0
print("  socket1 (0..%d) = %.2f%%   socket2 (%d..%d) = %.2f%%"%(h, hm(0,h), h, len(p0), hm(h,len(p0))))
print("\nInterpretation:")
print("  If AGG ~= socket1 and ~= 2x permean  => cpu_percent(None) reads processor-group 0 (socket 1) only.")
print("  True system-wide = per-core MEAN. FIX: cpu_usage_percent = mean(per_core_percent).")
print("DONE")
