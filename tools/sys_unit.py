import sys, time
sys.path.insert(0, ".")
import importlib, server
importlib.reload(server)
import system_collector as SC

# 1) downsample now preserves system fields
pts = [{"timestamp": float(i), "cpu_usage_percent": float(i % 10), "memory_usage_percent": 50.0,
        "disk_read_bps": 1000.0, "network_rx_bps": 5.0} for i in range(3000)]
out = server._downsample(pts, 2000, server._SYS_LIVE_FIELDS)
print("downsample out n=%d  out[0].cpu_usage_percent=%s (expect NOT None)"%(len(out), out[0].get("cpu_usage_percent")))
print("  out[0] keys:", sorted(out[0].keys()))

# 2) summary stats
s = server._system_live_summary(pts)
print("summary cpu:", s["cpu"])

# 3) CPU mean semantics: simulate _apply_sample with a per-core list where socket2 idle
class FakeClock(SC.default_clock.__class__ if hasattr(SC,'default_clock') else object):
    def now(self): return 1000.0
    def monotonic(self): return 500.0
col = SC.SystemCollector.__new__(SC.SystemCollector)
col._cpu_warmed = True
col._disk_prev = None
col._net_prev = None
col._net_per_prev = {}
col._DEFAULT_IF_NAME = None; col._DEFAULT_IF_CACHE = 0.0
col.advanced_cpu_temperature_c = None; col.advanced_cpu_power_w = None
col.gpu_power_total_w = 40.0
col.inventory = {"cpu_base_frequency_mhz": 2700.0}
col.config = type("C",(),{"system":type("S",(),{"history_interval_seconds":5.0})()})()
col.clock = FakeClock()

# per-core: 48 cores busy ~50%, 48 idle -> mean should be ~25, NOT 50
per_core = [50.0]*48 + [0.0]*48
import psutil
raw = (99.0, per_core, None, None, None, None, {})  # usage high (simulates group-0 bias)
sample = SC.SystemSample(timestamp=1000.0)
col._apply_sample(sample, raw, 500.0)
print("\nCPU semantics: per-core mean of [50x48, 0x48] =>")
print("  cpu_usage_percent = %s (expect ~25.0, NOT 99)"%(sample.cpu_usage_percent))
print("  base_frequency_mhz = %s"%sample.cpu_base_frequency_mhz)
print("  to_row has network_interface? %s (expect False, live-only)"%("network_interface" in sample.to_row()))

# 4) power aggregation partial (GPU=40, CPU=None) => 40
print("  monitored_component_power_w = %s (expect 40.0: GPU present, CPU None)"%(sample.monitored_component_power_w))

# power case 2: cpu=80 gpu=200 => 280
col2 = SC.SystemCollector.__new__(SC.SystemCollector)
col2.__dict__.update(col.__dict__)
col2.gpu_power_total_w = 200.0
s2 = SC.SystemSample(timestamp=1000.0)
s2.cpu_package_power_w = 80.0
col2._apply_sample(s2, (1.0, [1.0], None, None, None, None, {}), 500.0)
print("  power case2 cpu=80 gpu=200 => %s (expect 280.0)"%s2.monitored_component_power_w)
# case 3: all None
col3 = SC.SystemCollector.__new__(SC.SystemCollector); col3.__dict__.update(col2.__dict__)
col3.gpu_power_total_w = None
s3 = SC.SystemSample(timestamp=1000.0)
col3._apply_sample(s3, (1.0, [1.0], None, None, None, None, {}), 500.0)
print("  power case3 cpu=None gpu=None => %s (expect None)"%s3.monitored_component_power_w)
# case 4: cpu=0 gpu=None => 0
col4 = SC.SystemCollector.__new__(SC.SystemCollector); col4.__dict__.update(col2.__dict__)
col4.gpu_power_total_w = None
s4 = SC.SystemSample(timestamp=1000.0)
col4._apply_sample(s4, (1.0, [1.0], None, None, None, None, {}), 500.0)
s4.cpu_package_power_w = 0.0  # set after apply to test 0 preserved
parts=[s4.cpu_package_power_w, col4.gpu_power_total_w]; parts=[p for p in parts if p is not None]
print("  power case4 cpu=0 gpu=None => %s (expect 0.0)"%(sum(parts)))
print("DONE")
