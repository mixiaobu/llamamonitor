import platform, psutil, re
print("platform.platform(terse) =", platform.platform(terse=True))
print("platform.platform()      =", platform.platform())
print("platform.system/version/release =", platform.system(), platform.version(), platform.release())
print("platform.machine()       =", platform.machine())
print("platform.node()          =", platform.node())
import sys; print("platform.processor() =", platform.processor())

# cpu model base freq parse
try:
    import subprocess
    out = subprocess.run(["powershell","-NoProfile","-Command",
        "(Get-CimInstance Win32_Processor | Select-Object -First 1).Name"],
        capture_output=True, text=True, timeout=6, creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
    print("WMI cpu name =", repr(out))
    m = re.search(r"@?\s*([\d.]+)\s*GHz", out, re.I)
    print("parsed base GHz =", m.group(1) if m else None, "-> MHz", (float(m.group(1))*1000) if m else None)
except Exception as e:
    print("wmi err", e)

freq = psutil.cpu_freq()
print("psutil.cpu_freq() = current=%s min=%s max=%s"%(freq.current, freq.min, freq.max))

# socket count via Win32_Processor instance count
try:
    out = subprocess.run(["powershell","-NoProfile","-Command",
        "@(Get-CimInstance Win32_Processor).Count"],
        capture_output=True, text=True, timeout=8, creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
    print("Win32_Processor instance count (sockets) =", out)
except Exception as e:
    print("socket count err", e)

# windows build via registry (reliable)
try:
    import winreg
    k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion")
    def rd(n):
        try: return winreg.QueryValueEx(k,n)[0]
        except Exception: return None
    print("ProductName =", rd("ProductName"))
    print("CurrentBuild =", rd("CurrentBuild"))
    print("UBR =", rd("UBR"))
    print("DisplayVersion =", rd("DisplayVersion"))
    print("ReleaseId =", rd("ReleaseId"))
except Exception as e:
    print("reg err", e)

print("\n=== net_if_addrs / interfaces ===")
try:
    for name, addrs in psutil.net_if_addrs().items():
        kinds = [a.family for a in addrs]
        ipv4 = [a.address for a in addrs if a.family==2 and a.address]
        isup = "up" if any(getattr(a,'is_virtual',False)==0 for a in addrs) else "?"
        print("  %-22s families=%s ipv4=%s"%(name, kinds, ipv4))
except Exception as e:
    print("net_if_addrs err", e)

print("\n=== net_if_stats (isup / speed / duplex / is_virtual) ===")
try:
    for name, st in psutil.net_if_stats().items():
        print("  %-22s isup=%s speed=%s duplex=%s is_virtual=%s"%(name, st.isup, st.speed, st.duplex, st.is_virtual))
except Exception as e:
    print("net_if_stats err", e)

print("\n=== net_io_counters(pernic=True) sample (names) ===")
try:
    d = psutil.net_io_counters(pernic=True)
    for name, c in d.items():
        print("  %-22s recv=%s sent=%s errin=%s errout=%s dropin=%s dropout=%s"%(name, c.bytes_recv, c.bytes_sent, c.errin, c.errout, c.dropin, c.dropout))
except Exception as e:
    print("pernic err", e)

# default route
try:
    out = subprocess.run(["powershell","-NoProfile","-Command",
        "(Get-NetRoute -DestinationPrefix 0.0.0.0/0 -ErrorAction SilentlyContinue | Select-Object -First 1).InterfaceAlias"],
        capture_output=True, text=True, timeout=8, creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
    print("\nDEFAULT ROUTE InterfaceAlias =", repr(out))
except Exception as e:
    print("route err", e)
print("DONE")
