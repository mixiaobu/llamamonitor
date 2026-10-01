import sys, time
sys.path.insert(0, ".")
import importlib, server, system_collector as SC
importlib.reload(server); importlib.reload(SC)

# fake collector with a pernic stream to test adapter rates
class C:
    pass
col = SC.SystemCollector.__new__(SC.SystemCollector)
col._cpu_warmed = True; col._disk_prev=None
col._net_prev=None; col._net_per_prev={}; col._net_adapters={}
col._DEFAULT_IF_NAME="WLAN"; col._DEFAULT_IF_CACHE=1e9
col.advanced_cpu_temperature_c=None; col.advanced_cpu_power_w=None
col.gpu_power_total_w=None; col.inventory={"cpu_base_frequency_mhz":2700.0}
col.config=type("C",(),{"system":type("S",(),{"history_interval_seconds":5.0})()})()
class Clk:
    def now(self): return 1000.0
    def monotonic(self): return 500.0
col.clock=Clk()
class Ctr:
    def __init__(s,rx,tx): s.bytes_recv=rx; s.bytes_sent=tx
# first poll: establish baseline (rates None)
pernic1={"WLAN":Ctr(1000,500),"WireGuard 000005":Ctr(500,200),"Loopback Pseudo-Interface 1":Ctr(10,5)}
nio1=Ctr(1510,705)
col._apply_sample(SC.SystemSample(timestamp=1000.0),(1.0,[5.0],None,None,None,nio1,pernic1),500.0)
print("after poll1: WLAN rate (expect None, first)=%s  interface=%s"%(col._net_adapters.get("WLAN"), col._DEFAULT_IF_NAME))
# second poll: counters advance
class Clk2:
    def now(self): return 1005.0
    def monotonic(self): return 505.0
col.clock=Clk2()
pernic2={"WLAN":Ctr(11000,5500),"WireGuard 000005":Ctr(5200,2100),"Loopback Pseudo-Interface 1":Ctr(15,8)}
nio2=Ctr(16215,7608)
col._apply_sample(SC.SystemSample(timestamp=1005.0),(1.0,[5.0],None,None,None,nio2,pernic2),505.0)
print("after poll2 (5s):")
print("  WLAN rx_bps=%s tx_bps=%s (expect ~1800, ~1000)"%(col._net_adapters.get("WLAN",{}).get("rx_bps"), col._net_adapters.get("WLAN",{}).get("tx_bps")))
print("  adapter_rates keys=%s"%sorted(col.adapter_rates().keys()))
# default interface rate chosen
s=col._net_adapters["WLAN"]
print("  sample default = WLAN (defnic) -> rx should == WLAN rx")
# server helper
pub = server._system_adapters_public(col)
print("  _system_adapters_public WLAN=%s"%({k:pub["WLAN"][k] for k in ("rx_bps","tx_bps","is_virtual")} if "WLAN" in pub else "MISSING"))
print("DONE")
