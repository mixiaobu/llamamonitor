"""
system_collector.py — Windows 系统基础监控采集器（1.1.0 Stage B）

基于 psutil 的只读系统遥测：CPU / 内存 / 磁盘 / 网络 / 系统 Uptime。
**不依赖** LibreHardwareMonitor（高级传感器不可用时本模块仍完整工作）。

设计约束（与 collector.py / gpu_collector.py 同族）：
- 纯旁路只读：psutil 只查询，绝不修改系统任何状态；
- 单轮任何异常不影响 llama Token 采集 / GPU 采集（独立任务、故障隔离）；
- 速率（disk/network）用**累计 counter delta / monotonic 间隔**计算，
  不用 wall clock；counter reset（接口消失/睡眠唤醒）安全处理（不产生负值/巨值）；
- 第一条 CPU 采样是 warmup（psutil.cpu_percent(interval=None) 首次无有效 delta）：
  不写数据库、不作为速率基线；
- CPU 能耗：CPU Package Power（来自高级传感器 provider）梯形积分，
  Δt 用 monotonic；> max_energy_integration_gap（2~3 个采样周期）不积分
  （睡眠/断档不能当持续满功耗）；
- 实时数据在内存（self.latest + 短 ring buffer 供 2s UI）；
  每 history_interval_seconds 一条写 system_samples（DB），每 48h 保留（config）；
- 静态 SystemInventory（OS/CPU/主板/BIOS/GPU/磁盘/BootTime）启动时读一次 +
  手动刷新；CIM/PowerShell 只在启动/刷新时跑，**绝不**每 2 秒。
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import psutil

from clock import Clock, default_clock
from config import AppConfig
from db import Database

logger = logging.getLogger("llamamonitor.system")

# 能耗积分安全上限：相邻采样 monotonic 间隔 > 该倍数 × history_interval 不积分
ENERGY_GAP_FACTOR = 3.0
# 实时 ring buffer 最大点数（供 2s UI 最近 5 分钟：150 × 2s）
LIVE_RING_MAX = 150
# 速率计算的安全上限（counter delta 异常兜底：> 该值视为 counter reset/接口变化）
_MAX_RATE_BPS = 25 * 1024**3  # 25 GB/s（超过即不可能，按 reset 处理）


def _counter_rate(prev: float | None, curr: float, dt: float) -> float | None:
    """
    累计 counter -> 速率（bytes/second）。

    - prev 为 None（首次/未 warmup）-> None（warmup）；
    - dt <= 0 -> None（单调时钟异常）；
    - curr < prev（counter reset：接口消失/睡眠唤醒/计数器溢出）-> None（该段不计算）；
    - rate > _MAX_RATE_BPS（异常巨值）-> None。
    """
    if prev is None or dt <= 0:
        return None
    delta = curr - prev
    if delta < 0:
        return None
    rate = delta / dt
    return rate if rate <= _MAX_RATE_BPS else None


def _safe(callable_, *args, **kwargs):
    """psutil 调用兜底：任何异常 -> None（单个指标失败不影响其他指标）。"""
    try:
        return callable_(*args, **kwargs)
    except Exception:
        return None


@dataclass
class SystemSample:
    """一条系统采样点（system_samples 行 + UI 实时展示）。None = 不可用（绝不存 0）。"""

    timestamp: float
    cpu_usage_percent: float | None = None
    cpu_frequency_mhz: float | None = None
    cpu_temperature_c: float | None = None       # 来自高级传感器（可空）
    cpu_package_power_w: float | None = None     # 来自高级传感器（可空）
    memory_used_bytes: int | None = None
    memory_total_bytes: int | None = None
    memory_usage_percent: float | None = None
    disk_read_bps: float | None = None
    disk_write_bps: float | None = None
    network_rx_bps: float | None = None
    network_tx_bps: float | None = None
    monitored_component_power_w: float | None = None  # CPU+GPU 已监测组件合计（非墙插）

    def to_row(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "cpu_usage_percent": self.cpu_usage_percent,
            "cpu_frequency_mhz": self.cpu_frequency_mhz,
            "cpu_temperature_c": self.cpu_temperature_c,
            "cpu_package_power_w": self.cpu_package_power_w,
            "memory_used_bytes": self.memory_used_bytes,
            "memory_total_bytes": self.memory_total_bytes,
            "memory_usage_percent": self.memory_usage_percent,
            "disk_read_bps": self.disk_read_bps,
            "disk_write_bps": self.disk_write_bps,
            "network_rx_bps": self.network_rx_bps,
            "network_tx_bps": self.network_tx_bps,
            "monitored_component_power_w": self.monitored_component_power_w,
        }


class SystemCollector:
    """
    常驻系统采集器（独立于 llama / GPU 采集；故障隔离）。

    - poll_once()：一次 psutil 采样 + 速率计算 + 能耗积分 + 按间隔落库；
      任何失败只记日志（状态翻转才写），不抛异常；
    - 高级传感器（CPU 温度/功耗、风扇、主板、存储温度）由 HardwareSensorProvider
      注入（set_advanced_sensor_values）：provider 不可用时这些字段保持 None；
    - GPU 功耗（组件功耗合计）由 GpuCollector 的最近采样注入（set_gpu_power_w）；
      两者都没有时 monitored_component_power_w = None（绝不显示假数值）；
    - wall_power_w 恒为 None（1.1 无外部功率计；数据模型预留，见 /api/system/status）。
    """

    def __init__(
        self,
        config: AppConfig,
        db: Database | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.clock = clock or default_clock
        self.enabled = config.system.enabled
        # ---- 速率基线（monotonic 配对）----
        self._disk_prev: tuple[float, float] | None = None   # (mono, read_bytes, write_bytes) 用三元
        self._net_prev: tuple[float, float, float] | None = None  # (mono, rx, tx)
        self._cpu_warmed = False          # psutil.cpu_percent 首次 warmup 标志
        # ---- 能耗基线 ----
        self._cpu_power_prev: tuple[float, float] | None = None  # (mono, power_w)
        self._cpu_energy_today: dict[str, float] = {}            # {date: Wh}（内存态，跨午夜用 local_date）
        self._last_energy_date: str | None = None
        # ---- 落库节流（每 history_interval_seconds 一条；UI 轮次更密）----
        self._last_db_write: float | None = None   # 上次写 system_samples 的 wall
        self._pending_cpu_energy_wh: float = 0.0   # 未落库轮的能耗累计（落库时一并写入 daily）
        # ---- 高级传感器（provider 注入；None = 不可用）----
        self.advanced_cpu_temperature_c: float | None = None
        self.advanced_cpu_power_w: float | None = None
        self.advanced_fans: list[dict] = []        # [{name, rpm, control_percent, source}]
        self.advanced_sensors: list[dict] = []     # 全部高级传感器（Settings 列表用）
        self.advanced_available: bool = False      # provider 状态（状态转换才记日志）
        # ---- GPU 功耗（组件合计用；GpuCollector 注入）----
        self.gpu_power_total_w: float | None = None
        # ---- 实时数据（内存）----
        self.latest: SystemSample | None = None
        # 普通类（非 @dataclass）：不能 field(default_factory=...)，否则 live_ring
        # 是 Field 描述符对象而非 deque（append 会 AttributeError）
        self.live_ring: deque[SystemSample] = deque(maxlen=LIVE_RING_MAX)
        self.last_update: float | None = None
        self.available: bool = False               # psutil 采样是否成功（状态翻转才记日志）
        self._last_available: bool | None = None
        # ---- 静态库存（启动读一次）----
        self.inventory: dict = {}

    # ---------- 静态库存（启动/手动刷新；绝不高频 CIM） ----------

    def read_inventory(self) -> dict:
        """
        读取静态 SystemInventory（OS/Computer/CPU/RAM/主板/BIOS/磁盘/BootTime）。
        只在启动与手动刷新时调用（CIM/PowerShell 低频）。
        任何字段失败保持 None（不猜）。
        """
        inv: dict[str, Any] = {
            "os": None, "computer_name": None, "cpu_model": None,
            "physical_cores": None, "logical_cpus": None,
            "installed_ram_bytes": None,
            "motherboard_manufacturer": None, "motherboard_model": None,
            "bios_version": None,
            "gpu_list": [], "disk_list": [], "boot_time": None,
        }
        inv["os"] = _os_description()
        inv["computer_name"] = _safe(lambda: __import__("platform").node())
        inv["cpu_model"] = _safe(lambda: (psutil.cpu_freq() and "") or _cpu_model())
        inv["logical_cpus"] = _safe(psutil.cpu_count, logical=True)
        inv["physical_cores"] = _safe(psutil.cpu_count, logical=False)
        mem = _safe(psutil.virtual_memory)
        if mem is not None:
            inv["installed_ram_bytes"] = mem.total
        inv["boot_time"] = _safe(psutil.boot_time)
        # 磁盘设备列表（容量 + 类型）
        try:
            for part in psutil.disk_partitions(all=False):
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                except Exception:
                    continue
                inv["disk_list"].append({
                    "device": part.device, "mountpoint": part.mountpoint,
                    "fstype": part.fstype, "total_bytes": usage.total,
                    "used_bytes": usage.used, "free_bytes": usage.free,
                })
        except Exception:
            pass
        # 主板 / BIOS（Windows：通过 CIM；低频）
        if _IS_WINDOWS:
            try:
                c = _cim_query()
                if c:
                    inv["motherboard_manufacturer"] = c.get("motherboard_manufacturer")
                    inv["motherboard_model"] = c.get("motherboard_model")
                    inv["bios_version"] = c.get("bios_version")
            except Exception:
                pass
        return inv

    def refresh_inventory(self) -> dict:
        """手动刷新库存（Settings 页面）；重新读取后返回。"""
        self.inventory = self.read_inventory()
        return self.inventory

    # ---------- 高级传感器注入（provider -> collector） ----------

    def set_advanced_sensor_values(self, values: dict, fans: list[dict],
                                   all_sensors: list[dict], available: bool) -> None:
        """
        由 HardwareSensorProvider 注入最新高级传感器值（每 advanced_sensor_interval 一次）。

        values: {"cpu_temperature_c": float|None, "cpu_package_power_w": float|None}
        fans:   [{"name","rpm","control_percent","source"}]（control_percent 缺失时 None，
                绝不从 RPM 推算）
        available: provider 状态（状态转换才记日志 + 事件）
        """
        self.advanced_cpu_temperature_c = values.get("cpu_temperature_c")
        self.advanced_cpu_power_w = values.get("cpu_package_power_w")
        self.advanced_fans = fans
        self.advanced_sensors = all_sensors
        if available != self.advanced_available:
            if self._last_advanced is not None and available != self._last_advanced:
                if available:
                    logger.info("高级硬件传感器 provider 恢复可用")
                    self._record_event("hardware_sensor_provider_recovered", "info")
                else:
                    logger.warning("高级硬件传感器 provider 不可用（基础系统监控继续）")
                    self._record_event("hardware_sensor_provider_unavailable", "warning")
            self._last_advanced = available
        self.advanced_available = available

    # 避免首次 set 就记日志：用一个哨兵
    _last_advanced: bool | None = None

    def set_gpu_power_total(self, total_w: float | None) -> None:
        """GpuCollector 注入全部 GPU 的功耗合计（None = 无 GPU 数据）。"""
        self.gpu_power_total_w = total_w

    def _record_event(self, event_type: str, severity: str) -> None:
        if self.db is None:
            return
        try:
            self.db.record_event(event_type, severity, "system", {}, now=self.clock.now())
        except Exception as exc:
            logger.warning("写入系统事件 %s 失败: %r", event_type, exc)

    # ---------- 能耗（CPU Package Power 梯形积分；monotonic） ----------

    def _cpu_energy_delta(self, now_mono: float) -> float:
        """本轮 CPU 能耗增量（Wh）；power 缺失或 gap 超限时不积分（返回 0.0）。"""
        power = self.advanced_cpu_power_w
        if power is None:
            self._cpu_power_prev = None
            return 0.0
        prev = self._cpu_power_prev
        delta = 0.0
        if prev is not None:
            dt = now_mono - prev[0]
            max_gap = self.config.system.history_interval_seconds * ENERGY_GAP_FACTOR
            if 0 < dt <= max_gap:
                delta = (prev[1] + power) / 2.0 * dt / 3600.0
        self._cpu_power_prev = (now_mono, power)
        return delta

    # ---------- 采样 ----------

    def poll_once(self) -> SystemSample | None:
        """
        一次系统采样。成功返回本轮 SystemSample（并加入 live ring）；失败返回 None。
        任何 psutil 异常都被兜底（单指标 None，不抛异常）。
        """
        if not self.enabled:
            return None
        now_wall = self.clock.now()
        now_mono = self.clock.monotonic()
        sample = SystemSample(timestamp=now_wall)

        # CPU 利用率（interval=None 非阻塞；首次 warmup：本条不写 DB、不作为有效利用率）
        was_warmed = self._cpu_warmed
        usage = _safe(psutil.cpu_percent, interval=None)
        if usage is not None:
            if not self._cpu_warmed:
                # 首次：只建立基线
                self._cpu_warmed = True
            else:
                sample.cpu_usage_percent = usage
        freq = _safe(psutil.cpu_freq)
        if freq is not None and freq.current is not None:
            sample.cpu_frequency_mhz = freq.current

        # 内存
        vm = _safe(psutil.virtual_memory)
        if vm is not None:
            sample.memory_used_bytes = vm.total - vm.available
            sample.memory_total_bytes = vm.total
            sample.memory_usage_percent = vm.percent

        # 磁盘 IO（累计 counter -> 速率；monotonic）
        dio = _safe(psutil.disk_io_counters)
        if dio is not None and dio.read_bytes is not None and dio.write_bytes is not None:
            if self._disk_prev is not None:
                prev_mono, prev_read, prev_write = self._disk_prev
                dt = now_mono - prev_mono
                sample.disk_read_bps = _counter_rate(prev_read, dio.read_bytes, dt)
                sample.disk_write_bps = _counter_rate(prev_write, dio.write_bytes, dt)
            self._disk_prev = (now_mono, dio.read_bytes, dio.write_bytes)

        # 网络 IO（累计 counter -> 速率；monotonic）
        nio = _safe(psutil.net_io_counters)
        if nio is not None and nio.bytes_recv is not None and nio.bytes_sent is not None:
            if self._net_prev is not None:
                prev_mono, prev_rx, prev_tx = self._net_prev
                dt = now_mono - prev_mono
                sample.network_rx_bps = _counter_rate(prev_rx, nio.bytes_recv, dt)
                sample.network_tx_bps = _counter_rate(prev_tx, nio.bytes_sent, dt)
            self._net_prev = (now_mono, nio.bytes_recv, nio.bytes_sent)

        # 高级传感器（provider 注入；None = 不可用）
        sample.cpu_temperature_c = self.advanced_cpu_temperature_c
        sample.cpu_package_power_w = self.advanced_cpu_power_w

        # 已监测组件功耗 = CPU Package Power + 全部 GPU Power（任一缺失 -> None，不显示假值）
        if sample.cpu_package_power_w is not None and self.gpu_power_total_w is not None:
            sample.monitored_component_power_w = (
                sample.cpu_package_power_w + self.gpu_power_total_w
            )

        # CPU 能耗积分（power 可靠时；gap 超限不积分）
        cpu_energy_wh = self._cpu_energy_delta(now_mono)
        if cpu_energy_wh > 0:
            date = _local_date(now_wall)
            self._cpu_energy_today[date] = self._cpu_energy_today.get(date, 0.0) + cpu_energy_wh
            self._last_energy_date = date
        # 未落库轮的能耗先累计（落库节流时不丢段）
        self._pending_cpu_energy_wh += cpu_energy_wh

        # 落库：每 history_interval_seconds 一条（UI 轮次更密）；warmup 轮不写 DB。
        # 组件能耗 daily 只累加 CPU 部分（GPU 能量由 gpu_daily 维护；组件合计在
        # API 层由 gpu_daily+system_daily 计算）。写失败保持旧写点与 pending，下轮重试。
        interval = self.config.system.history_interval_seconds
        due = (self._last_db_write is None) or (now_wall - self._last_db_write >= interval)
        if was_warmed and self.db is not None and due:
            try:
                self.db.save_system_sample(
                    sample.to_row(),
                    daily_increments={
                        "cpu_energy_wh": self._pending_cpu_energy_wh,
                        # "已监测组件能耗"的 GPU 部分**不**在这里累加——
                        # system_daily.monitored_component_energy_wh 列只存 CPU 部分
                        # （与 cpu_energy_wh 相同），GPU 能量由 gpu_daily 维护；
                        # API 层（server.api_system_daily）按日把两边相加，
                        # 使"今日能耗"= CPU + 被监控 GPU，与实时"组件功耗合计"同口径。
                        "monitored_component_energy_wh": self._pending_cpu_energy_wh,
                    },
                    now=now_wall,
                    retention_seconds=self.config.system.history_retention_hours * 3600,
                )
                self._last_db_write = now_wall
                self._pending_cpu_energy_wh = 0.0
            except Exception as exc:
                logger.warning("系统数据库写入失败，本轮跳过落盘: %r", exc)

        # 实时数据
        self.latest = sample
        self.live_ring.append(sample)
        self.last_update = now_wall
        self._set_available(True)
        return sample

    def _set_available(self, ok: bool) -> None:
        if self._last_available is not None and ok != self._last_available:
            (logger.info if ok else logger.warning)(
                "系统监控%s", "恢复" if ok else "不可用"
            )
        self._last_available = ok
        self.available = ok

    def cpu_energy_today_wh(self) -> float:
        """今日 CPU 能耗（Wh，内存态累计）。"""
        date = _local_date(self.clock.now())
        return self._cpu_energy_today.get(date, 0.0)

    async def run(self) -> None:
        """常驻采集循环（每 poll_interval_seconds 一轮）；与 llama/GPU 独立。"""
        # 启动：warmup CPU + 读库存
        try:
            _safe(psutil.cpu_percent, interval=None)  # warmup
            self._cpu_warmed = True
            self.inventory = self.read_inventory()
        except Exception as exc:
            logger.warning("系统监控启动初始化失败: %r", exc)
        import asyncio
        self._record_event("system_monitor_start", "info")
        while True:
            try:
                self.poll_once()
            except Exception as exc:
                logger.warning("系统采集轮错误: %r", exc)
                self._set_available(False)
            await asyncio.sleep(self.config.system.poll_interval_seconds)

    def shutdown(self) -> None:
        """优雅停止：记 system_monitor_stop 事件。"""
        if getattr(self, "_shutdown_done", False):
            return
        self._shutdown_done = True
        self._record_event("system_monitor_stop", "info")


# ---------- 平台辅助 ----------

import sys as _sys
_IS_WINDOWS = _sys.platform == "win32"


def _local_date(now: float) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(now).strftime("%Y-%m-%d")


def _os_description() -> str | None:
    try:
        import platform
        return platform.platform(terse=True)
    except Exception:
        return None


def _cpu_model() -> str | None:
    try:
        import platform
        # Windows：通过 WMI/注册表拿 CPU 名称（psutil 不直接提供型号名）
        if _IS_WINDOWS:
            try:
                import subprocess
                out = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "(Get-CimInstance Win32_Processor | Select-Object -First 1).Name"],
                    capture_output=True, text=True, timeout=5,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                name = (out.stdout or "").strip()
                if name:
                    return name
            except Exception:
                pass
        return platform.processor() or None
    except Exception:
        return None


def _cim_query() -> dict:
    """
    一次性 CIM 查询（主板/BIOS）——只在启动/手动刷新调用（绝不每 2 秒）。
    Windows：Get-CimInstance（现代替代 WMIC）；失败返回 {}。
    """
    out: dict = {}
    if not _IS_WINDOWS:
        return out
    try:
        import subprocess
        ps_cmd = (
            "$mb=Get-CimInstance Win32_BaseBoard; "
            "$bios=Get-CimInstance Win32_BIOS; "
            "$cpu=Get-CimInstance Win32_Processor; "
            "[pscustomobject]@{manufacturer=$mb.Manufacturer; model=$mb.Product; "
            "bios=$bios.SMBIOSBIOSVersion; cpu=$cpu.Name | Select-Object -First 1} | "
            "ConvertTo-Json -Compress"
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        import json as _json
        data = _json.loads((r.stdout or "").strip() or "{}")
        out["motherboard_manufacturer"] = data.get("manufacturer") or None
        out["motherboard_model"] = data.get("model") or None
        out["bios_version"] = data.get("bios") or None
        if data.get("cpu"):
            out["cpu_model"] = data["cpu"]
    except Exception:
        pass
    return out
