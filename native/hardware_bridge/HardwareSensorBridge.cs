// HardwareSensorBridge.cs — LlamaMonitor 高级硬件传感器只读 Bridge（1.1.0 Stage C）
//
// 设计约束：
// - 目标框架：.NET Framework 4.8（Windows 10 1903+ / Windows 11 内置，Clean Machine
//   零额外 runtime 部署）；LibreHardwareMonitorLib 0.9.2（BSD-3-Clause，net472 构建，
//   在 .NET Framework 4.8 上完全兼容）；
// - 编译：C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe（无 dotnet SDK /
//   MSBuild 依赖，见 scripts\build_bridge.py）；
// - 只读：只调用 computer.Open() / Update()，绝不 Set 任何值（不调 SetFanSpeed /
//   SetClock / SetPowerLimit / SetVoltage）；GPU Provider 禁用（IsGpuEnabled=false）
//   —— GPU 遥测由 nvidia-smi 侧（gpu_collector）负责，避免同一 GPU 被两个 Provider
//   重复采集；
// - 通信：stdout 逐行 JSON Lines（每 --interval 秒一行）；无额外 HTTP 端口；
// - 生命周期：stdin 读到 EOF（LlamaMonitor 关闭 stdin）或 Ctrl+C -> 优雅退出
//   （computer.Close()）；LlamaMonitor 侧 Terminate 兜底，不留孤儿进程；
// - LHM 0.9.2 API：ISensor.Value 为 double?；无 Unit 枚举 —— 单位按 SensorType
//   归一化（LHM clock 传感器单位为 Hz，>1e6 时换算为 MHz 输出）；
// - CPU 温度/功耗位于 Cpu 的 SubHardware（"Package"）内 —— 必须递归遍历 SubHardware；
// - 输出格式（每行一个 JSON 对象）：
//   {"kind":"sensors","sensors":[
//      {"hardware_type":"cpu","hardware_name":"CPU > Package",
//       "sensor_type":"temperature","sensor_name":"Package",
//       "index":0,"value":64.0,"unit":"C"}, ...]}
//   单位归一化：C / % / RPM / W / V / A / MHz / J（LlamaMonitor 侧只认这些小写/大写单位）。

using System;
using System.Globalization;
using System.Text;
using System.Threading;
using LibreHardwareMonitor.Hardware;

namespace LlamaMonitor.HardwareBridge
{
    internal static class Program
    {
        private static Computer _computer;
        private static Timer _timer;
        private static double _intervalSeconds = 5.0;
        private static ManualResetEventSlim _exitSignal = new ManualResetEventSlim(false);
        private static readonly object ConsoleLock = new object();

        private static int Main(string[] args)
        {
            for (int i = 0; i < args.Length; i++)
            {
                if (args[i].StartsWith("--interval="))
                {
                    double v;
                    if (double.TryParse(args[i].Substring("--interval=".Length),
                        NumberStyles.Float, CultureInfo.InvariantCulture, out v) && v >= 1.0)
                    {
                        _intervalSeconds = v;
                    }
                }
            }

            // stdout UTF-8 无 BOM（Python 侧逐行 json.loads）
            try { Console.OutputEncoding = new UTF8Encoding(false); } catch (Exception) { }

            // stdin EOF -> 优雅退出（LlamaMonitor 关闭 stdin 时）
            var stdinThread = new Thread(StdinWatcher) { IsBackground = true, Name = "stdin-watch" };
            stdinThread.Start();
            Console.CancelKeyPress += (s, e) => { e.Cancel = true; _exitSignal.Set(); };

            // 只读硬件集合：CPU / Motherboard / Memory(控制器) / Storage / Cooler。
            // GPU 禁用（nvidia-smi 侧负责）；Network/PSU/Battery 对 1.1 遥测无用。
            _computer = new Computer
            {
                IsCpuEnabled = true,
                IsMotherboardEnabled = true,
                IsMemoryEnabled = true,
                IsStorageEnabled = true,
                IsGpuEnabled = false,
                IsNetworkEnabled = false,
                IsPsuEnabled = false,
                IsControllerEnabled = false,
                IsBatteryEnabled = false,
            };

            try
            {
                _computer.Open();
            }
            catch (Exception)
            {
                // Open 失败（权限/硬件异常）：保持进程存活并输出空传感器行，
                // Python 侧按 partial/unavailable 处理（状态翻转才记日志，不刷屏）。
                EmitEmpty();
                WaitAndClose();
                return 0;
            }

            // 首轮延迟 2s（LHM 需要一次完整初始化才能读到多数传感器）
            _timer = new Timer(_ => EmitCycle(), null, 2000, (long)(_intervalSeconds * 1000.0));
            WaitAndClose();
            return 0;
        }

        private static void StdinWatcher()
        {
            try
            {
                while (Console.In.Peek() >= 0) { }
            }
            catch (Exception) { }
            _exitSignal.Set();
        }

        private static void WaitAndClose()
        {
            try { _exitSignal.Wait(); } catch (Exception) { }
            try { if (_timer != null) _timer.Dispose(); } catch (Exception) { }
            try { _computer.Close(); } catch (Exception) { }
        }

        private static void EmitCycle()
        {
            try
            {
                // LHM 0.9.2：Computer 无 Update() —— 对每个顶层硬件各自 Update()
                foreach (IHardware hw in _computer.Hardware)
                {
                    try { hw.Update(); } catch (Exception) { }
                }
                var sb = new StringBuilder(32768);
                sb.Append("{\"kind\":\"sensors\",\"sensors\":[");
                bool first = true;
                foreach (IHardware hw in _computer.Hardware)
                {
                    first = WalkHardware(sb, hw, hw.Name ?? "Unknown", first);
                }
                sb.Append("]}");
                lock (ConsoleLock)
                {
                    Console.Out.WriteLine(sb.ToString());
                }
            }
            catch (Exception)
            {
                EmitEmpty();
            }
        }

        // 递归遍历 SubHardware（CPU Package / 核心温度都在 Cpu 的子硬件内）
        private static bool WalkHardware(StringBuilder sb, IHardware hw, string path, bool first)
        {
            first = AppendSensors(sb, hw, path, first);
            foreach (IHardware sub in hw.SubHardware)
            {
                first = WalkHardware(sb, sub, path + " > " + (sub.Name ?? "Unknown"), first);
            }
            return first;
        }

        private static bool AppendSensors(StringBuilder sb, IHardware hw, string path, bool first)
        {
            string hwType = HardwareTypeString(hw.HardwareType);
            foreach (ISensor sensor in hw.Sensors)
            {
                if (!sensor.Value.HasValue) continue;
                double raw = sensor.Value.Value;
                if (double.IsNaN(raw) || double.IsInfinity(raw)) continue;
                string unit = UnitFor(sensor.SensorType, ref raw);
                if (unit == null) continue;  // 未知类型不输出（Python 侧按不可用处理）
                sb.Append(first ? "" : ",");
                first = false;
                sb.Append("{\"hardware_type\":\"").Append(hwType)
                  .Append("\",\"hardware_name\":\"").Append(JsonEscape(path))
                  .Append("\",\"sensor_type\":\"").Append(SensorTypeString(sensor.SensorType))
                  .Append("\",\"sensor_name\":\"").Append(JsonEscape(sensor.Name ?? ""))
                  .Append("\",\"index\":").Append(sensor.Index)
                  .Append(",\"value\":").Append(raw.ToString("R", CultureInfo.InvariantCulture))
                  .Append(",\"unit\":\"").Append(unit).Append("\"}");
            }
            return first;
        }

        private static void EmitEmpty()
        {
            lock (ConsoleLock)
            {
                Console.Out.WriteLine("{\"kind\":\"sensors\",\"sensors\":[]}");
            }
        }

        private static string HardwareTypeString(HardwareType t)
        {
            switch (t)
            {
                case HardwareType.Cpu: return "cpu";
                case HardwareType.Motherboard: return "motherboard";
                case HardwareType.SuperIO: return "motherboard";       // SuperIO 是主板芯片组
                case HardwareType.EmbeddedController: return "motherboard";
                case HardwareType.Memory: return "memorycontroller";   // LHM "Memory" = 内存控制器
                case HardwareType.Storage: return "storage";
                case HardwareType.Cooler: return "cooling";
                case HardwareType.Psu: return "powersupply";
                case HardwareType.Battery: return "battery";
                case HardwareType.GpuNvidia:
                case HardwareType.GpuAmd:
                case HardwareType.GpuIntel: return "gpu";
                case HardwareType.Network: return "network";
                default: return "unknown";
            }
        }

        private static string SensorTypeString(SensorType t)
        {
            switch (t)
            {
                case SensorType.Temperature: return "temperature";
                case SensorType.Power: return "power";
                case SensorType.Energy: return "energy";
                case SensorType.Fan: return "fan";
                case SensorType.Flow: return "flow";    // 水泵流量（常以 RPM 计）
                case SensorType.Control: return "control";
                case SensorType.Load: return "load";
                case SensorType.Clock: return "clock";
                case SensorType.Frequency: return "frequency";
                case SensorType.Voltage: return "voltage";
                case SensorType.Current: return "current";
                case SensorType.Level: return "level";
                default: return null;  // Data/SmallData/Factor/TimeSpan/Noise 不输出
            }
        }

        // LHM 无 Unit 枚举：单位按 SensorType 归一化。
        // clock/frequency 在 LHM 中以 Hz 报告（如 3.8e9）——>1e6 时换算为 MHz 输出。
        private static string UnitFor(SensorType t, ref double value)
        {
            switch (t)
            {
                case SensorType.Temperature: return "C";
                case SensorType.Power: return "W";
                case SensorType.Energy: return "J";
                case SensorType.Fan: return "RPM";
                case SensorType.Flow: return "RPM";
                case SensorType.Control: return "%";
                case SensorType.Load: return "%";
                case SensorType.Level: return "%";
                case SensorType.Voltage: return "V";
                case SensorType.Current: return "A";
                case SensorType.Clock:
                case SensorType.Frequency:
                    if (value > 1e6) value = value / 1e6;   // Hz -> MHz
                    return "MHz";
                default: return null;
            }
        }

        private static string JsonEscape(string s)
        {
            if (string.IsNullOrEmpty(s)) return "";
            var sb = new StringBuilder(s.Length);
            foreach (char c in s)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                        else sb.Append(c);
                        break;
                }
            }
            return sb.ToString();
        }
    }
}
