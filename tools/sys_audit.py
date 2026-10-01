import json, urllib.request, time, sys
BASE="http://127.0.0.1:8765"
def get(p):
    try:
        with urllib.request.urlopen(BASE+p, timeout=6) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"_err":str(e)}

print("=== /api/system/status ===")
st=get("/api/system/status")
print(json.dumps(st, ensure_ascii=False, indent=1)[:1200])

print("\n=== psutil direct (same lib as collector) ===")
try:
    import psutil
    psutil.cpu_percent(None)   # warmup
    psutil.cpu_percent(None)   # 2nd
    time.sleep(0.5)
    agg=psutil.cpu_percent(None)
    per=psutil.cpu_percent(None, percpu=True)
    freq=psutil.cpu_freq()
    print("agg cpu_percent =%.1f%%"%agg if agg is not None else "agg None")
    if per is not None:
        print("per-core: n=%d max=%.1f min=%.1f mean=%.1f"%(len(per), max(per), min(per), sum(per)/len(per)))
    print("cpu_freq current=%s min=%s max=%s"%(freq.current if freq else None, freq.min if freq else None, freq.max if freq else None))
    vm=psutil.virtual_memory()
    print("vm percent=%.1f avail=%d total=%d"%(vm.percent, vm.available, vm.total))
    dio=psutil.disk_io_counters(); nio=psutil.net_io_counters()
    print("disk read_bytes=%s write_bytes=%s read_count=%s write_count=%s"%(dio.read_bytes, dio.write_bytes, dio.read_count, dio.write_count))
    print("net recv=%s sent=%s errin=%s errout=%s dropin=%s dropout=%s"%(nio.bytes_recv,nio.bytes_sent,nio.errin,nio.errout,nio.dropin,nio.dropout))
except Exception as e:
    print("psutil err", e)

print("\n=== /api/system/live?minutes=1440 (last 5 pts + stats) ===")
lv=get("/api/system/live?minutes=1440")
pts=lv.get("points",[])
print("n_points=%d  _err=%s"%(len(pts), lv.get("_err")))
if pts:
    cpu=[p.get("cpu_usage_percent") for p in pts if p.get("cpu_usage_percent") is not None]
    mem=[p.get("memory_usage_percent") for p in pts if p.get("memory_usage_percent") is not None]
    print("last point cpu=%s mem=%s disk_r=%s disk_w=%s net_rx=%s net_tx=%s mon_power=%s"%(
        pts[-1].get("cpu_usage_percent"),pts[-1].get("memory_usage_percent"),
        pts[-1].get("disk_read_bps"),pts[-1].get("disk_write_bps"),
        pts[-1].get("network_rx_bps"),pts[-1].get("network_tx_bps"),pts[-1].get("monitored_component_power_w")))
    if cpu: print("cpu over 24h: min=%.1f max=%.1f mean=%.1f n=%d  last5=%s"%(min(cpu),max(cpu),sum(cpu)/len(cpu),len(cpu),[round(c,1) for c in cpu[-5:]]))
    if mem: print("mem over 24h: min=%.1f max=%.1f mean=%.1f"%(min(mem),max(mem),sum(mem)/len(mem)))
    # check downsample bug: are fields all None (GPU-field bug)?
    allnone=all(all(p.get(f) is None for f in ("cpu_usage_percent","memory_usage_percent","disk_read_bps")) for p in pts)
    print("DOWNsample-field-bug (all sys fields None):", allnone)

print("\n=== /api/system/live?minutes=60 (1h, last point vs status) ===")
lv60=get("/api/system/live?minutes=60")
p60=lv60.get("points",[])
if p60:
    print("1h last point cpu=%s  (status agg cpu=%s)"%(p60[-1].get("cpu_usage_percent"), st.get("cpu",{}).get("usage_percent") if isinstance(st,dict) else None))
    c60=[p.get("cpu_usage_percent") for p in p60 if p.get("cpu_usage_percent") is not None]
    if c60: print("1h cpu: min=%.1f max=%.1f mean=%.1f n=%d"%(min(c60),max(c60),sum(c60)/len(c60),len(c60)))

print("\n=== /api/system/sensors ===")
sn=get("/api/system/sensors")
print("state=%s n_sensors=%d n_fans=%d counts=%s"%(sn.get("state"),len(sn.get("sensors",[])),len(sn.get("fans",[])),sn.get("counts")))
print("cpu_temp=%s cpu_power=%s"%(sn.get("cpu_temperature_c"),sn.get("cpu_package_power_w")))
for f in sn.get("fans",[])[:12]:
    print("  FAN name=%r rpm=%s ctrl=%s"%(f.get("name"),f.get("rpm"),f.get("control_percent")))
types={}
for s in sn.get("sensors",[]):
    k=(s.get("hardware_type"),s.get("sensor_type"))
    types[k]=types.get(k,0)+1
print("sensor (hw,type) groups:")
for k,v in sorted(types.items(), key=lambda x:(x[0][0] or '',x[0][1] or '')):
    print("   %s | %s : %d"%(k[0],k[1],v))

print("\n=== /api/system/inventory ===")
inv=get("/api/system/inventory")
i=inv.get("inventory",{})
print("os=%r cpu_model=%r phys=%s logical=%s ram=%s core_groups_n=%s"%(i.get("os"),i.get("cpu_model"),i.get("physical_cores"),i.get("logical_cpus"),i.get("installed_ram_bytes"), len(i.get("core_groups") or [])))
print("mb=%r/%r bios=%r"%(i.get("motherboard_manufacturer"),i.get("motherboard_model"),i.get("bios_version")))
for d in i.get("disk_list",[]):
    print("  VOL %s %s total=%.1fGiB used=%.1fGiB free=%.1fGiB"%(d.get("device"),d.get("fstype"),(d.get("total_bytes") or 0)/1073741824,(d.get("used_bytes") or 0)/1073741824,(d.get("free_bytes") or 0)/1073741824))

print("\n=== /api/system/daily?days=1 ===")
dl=get("/api/system/daily?days=1")
print(json.dumps(dl, ensure_ascii=False)[:500])
print("DONE")
