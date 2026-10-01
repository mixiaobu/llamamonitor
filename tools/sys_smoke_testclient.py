"""Round-3 系统后端 in-process 冒烟（不依赖端口 / 不依赖机器清醒）。
验证：_downsample 系统字段 / _system_live_summary / _system_adapters_public /
模块级 helper（CPU 型号清理 / base freq / arch / os version / virtual if / if kind）。"""
import sys
sys.path.insert(0, ".")
import server, system_collector as SC

def main():
    # 1) _downsample 保留系统字段（含 memory bytes）+ _system_live_summary
    pts = [
        {"timestamp":100.0,"cpu_usage_percent":5.0,"memory_usage_percent":40.0,
         "memory_used_bytes":1*1024**3,"memory_total_bytes":16*1024**3,
         "disk_read_bps":10.0,"disk_write_bps":20.0,"network_rx_bps":30.0,
         "network_tx_bps":40.0,"monitored_component_power_w":50.0},
        {"timestamp":200.0,"cpu_usage_percent":9.0,"memory_usage_percent":44.0,
         "memory_used_bytes":2*1024**3,"memory_total_bytes":16*1024**3,
         "disk_read_bps":None,"disk_write_bps":None,"network_rx_bps":None,
         "network_tx_bps":None,"monitored_component_power_w":None},
    ]
    out = server._downsample(pts, 1500, server._SYS_LIVE_FIELDS)
    assert "cpu_usage_percent" in out[0] and out[0]["cpu_usage_percent"] == 5.0, out[0]
    assert "memory_used_bytes" in out[0] and out[0]["memory_used_bytes"] == 1*1024**3, "memory bytes dropped"
    summ = server._system_live_summary(pts)
    assert summ["cpu"] == {"current":9.0,"avg":7.0,"max":9.0} or \
           (summ["cpu"]["current"]==9.0 and summ["cpu"]["avg"]==7.0 and summ["cpu"]["max"]==9.0), summ["cpu"]
    assert summ["memory"]["current"]==44.0 and summ["memory"]["avg"]==42.0, summ["memory"]
    assert summ["power"]["current"]==50.0, summ["power"]  # last non-null = 50
    print("downsample+summary OK")

    # 2) _system_adapters_public（合并 rates + meta）
    class FakeSys:
        def adapter_rates(self): return {"WLAN":{"rx_bps":100.0,"tx_bps":10.0}, "WG":{"rx_bps":None,"tx_bps":None}}
        def network_interfaces(self):
            return [{"name":"WLAN","kind":"Wi-Fi","is_default":True,"is_virtual":False,"speed_mbps":1200,"errin":0,"errout":0,"dropin":0,"dropout":0},
                    {"name":"WG","kind":"虚拟/VPN","is_default":False,"is_virtual":True,"speed_mbps":None,"errin":0,"errout":0,"dropin":1,"dropout":0}]
    pub = server._system_adapters_public(FakeSys())
    assert pub["WLAN"]["rx_bps"]==100.0 and pub["WLAN"]["speed_mbps"]==1200 and pub["WLAN"]["is_default"] is True, pub["WLAN"]
    assert pub["WG"]["is_virtual"] is True and pub["WG"]["dropin"]==1, pub["WG"]
    print("adapters_public OK")

    # 3) 模块级 helper
    m = SC._clean_cpu_model("Intel(R) Xeon(R) Platinum 8168 CPU @ 2.70GHz")
    assert m == "Intel Xeon Platinum 8168", repr(m)
    assert SC._parse_base_freq_mhz("Intel(R) Xeon(R) Platinum 8168 CPU @ 2.70GHz") == 2700.0
    arch = SC._arch_label()
    assert arch == "x64", arch  # 本机 AMD64
    osv = SC._os_version_parts()
    assert osv.get("display") and "Windows" in osv["display"], osv
    print("os display=", osv.get("display"), "build=", osv.get("build"))
    assert SC._is_virtual_if("WireGuard 000005") is True
    assert SC._is_virtual_if("000005") is True          # 纯 6 位数字 = 隧道
    assert SC._is_virtual_if("vEthernet (Default Switch)") is True
    assert SC._is_virtual_if("WLAN") is False
    assert SC._if_kind("WLAN", None) == "Wi-Fi", SC._if_kind("WLAN", None)
    assert SC._if_kind("以太网", None) == "以太网", SC._if_kind("以太网", None)
    assert SC._if_kind("000005", None) == "虚拟/VPN", SC._if_kind("000005", None)
    print("module helpers OK")
    print("ALL SMOKE OK")

if __name__ == "__main__":
    main()
