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

import asyncio
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


def _core_groups() -> list[list[int]]:
    """1.1.3：逻辑核 -> 物理核分组（每个物理核的一组逻辑核，含超线程 sibling）。

    数据源优先级（都失败才 []，前端回退逻辑核平铺视图，**不伪造**）：
    1. Windows：GLPIEx(RELATION_PROCESSOR_CORE) 直接给每个物理核的逻辑核掩码（最准）。
    2. Linux：psutil.cpu_affinity 按亲和集合分组。
    3. 兜底：physical/logical 为整除 N 倍时，按交织步长配对（常见 HT sibling 布局
       0↔N/2、1↔N/2+1…）——仅在能确定 sibling 数时启用，否则返回 []。
    返回形如 [[0,48],[1,49],...]（每个物理核的逻辑核 id）。"""
    # --- Windows：GetLogicalProcessorInformationEx(RELATION_PROCESSOR_CORE=5) ---
    if _IS_WINDOWS:
        try:
            import ctypes
            import struct as _struct
            buf = ctypes.create_string_buffer(65536)
            ret = ctypes.c_ulong(65536)
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            if k.GetLogicalProcessorInformationEx(5, buf, ctypes.byref(ret)) and ret.value:
                base = ctypes.addressof(buf)
                n = ret.value
                off = 0
                groups: list[list[int]] = []
                while off + 8 <= n:
                    rel, size = _struct.unpack_from("<II", buf.raw, off)
                    if not size:
                        break
                    if rel == 5:
                        mask = _struct.unpack_from("<Q", buf.raw, off + 8)[0]
                        ids = sorted(i for i in range(64) if (mask >> i) & 1)
                        if ids:
                            groups.append(ids)
                    off += size
                if len(groups) >= 2:  # 2 个以上物理核才可信（排除单核 VM 的伪条目）
                    return groups
        except Exception:
            pass
    # --- Linux：cpu_affinity ---
    try:
        groups = {}
        order = []
        for i in range(_safe(psutil.cpu_count, logical=True) or 0):
            aff = frozenset(_safe(psutil.cpu_affinity, i) or [i])
            if aff not in groups:
                groups[aff] = []
                order.append(aff)
            groups[aff].append(i)
        if len(order) >= 2:
            return [groups[a] for a in order]
    except Exception:
        pass
    # --- 兜底：N:N 整除时按交织步长配对 ---
    logical = _safe(psutil.cpu_count, logical=True) or 0
    physical = _safe(psutil.cpu_count, logical=False) or 0
    if physical >= 2 and logical > physical and logical % physical == 0:
        n_sib = logical // physical
        return [[p + n_sib * s for s in range(n_sib)] for p in range(physical)]
    return []


@dataclass
class SystemSample:
    """一条系统采样点（system_samples 行 + UI 实时展示）。None = 不可用（绝不存 0）。"""

    timestamp: float
    # Round-3 系统页：整机 CPU 利用率 = **逐逻辑核均值**（psutil.cpu_percent(None)
    # 在双路/多处理器组 Windows 上只读 processor group 0，会 ~2× 高估——见 sys_cpu_raw.py
    # 实测 AGG 恒为 per-core mean 的 2 倍）。per-core 缺失时回退到聚合值（单路机一致）。
    cpu_usage_percent: float | None = None
    # 1.1.3：逐逻辑核利用率（live-only，不进 DB）。
    # None = 尚未 warmup / 不可用；列表长度 = 逻辑核数。
    cpu_per_core_percent: list[float] | None = None
    cpu_frequency_mhz: float | None = None       # 当前频率（psutil.cpu_freq.current，实时）
    cpu_base_frequency_mhz: float | None = None  # 基准/标称频率（型号解析，静态）
    cpu_temperature_c: float | None = None       # 来自高级传感器（可空）
    cpu_package_power_w: float | None = None     # 来自高级传感器（可空）
    memory_used_bytes: int | None = None
    memory_total_bytes: int | None = None
    memory_usage_percent: float | None = None
    disk_read_bps: float | None = None
    disk_write_bps: float | None = None
    network_rx_bps: float | None = None
    network_tx_bps: float | None = None
    # Round-3（§94-§102）：当前网络速率对应的接口名；None = 全接口合计（未识别默认接口）。
    # live-only（不进 DB，与 cpu_per_core_percent 同模式——历史 schema 保持稳定，
    # 历史曲线由 rate 值本身反映默认接口口径）。
    network_interface: str | None = None
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
            # network_interface 是 live-only（不在 DB）——历史曲线由 rate 值本身反映默认接口口径
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
        self._net_prev: tuple[float, float, float] | None = None  # (mono, rx, tx) 全接口合计
        self._net_per_prev: dict[str, tuple[float, float, float]] = {}  # Round-3：每接口 (mono, rx, tx)
        self._net_adapters: dict[str, dict] = {}  # Round-3：每接口当前速率 {name:{rx_bps,tx_bps}}
        self._cpu_warmed = False          # psutil.cpu_percent 首次 warmup 标志
        # ---- 能耗基线 ----
        self._cpu_power_prev: tuple[float, float] | None = None  # (mono, wall, power_w)
        self._cpu_energy_today: dict[str, float] = {}            # {date: Wh}（内存态，跨午夜用 local_date）
        self._last_energy_date: str | None = None
        # ---- 落库节流（每 history_interval_seconds 一条；UI 轮次更密）----
        self._last_db_write: float | None = None   # 上次写 system_samples 的 wall
        # AUDIT-1.1.1 DATA-1111-006：未落库轮的能耗按自然日累计（跨午夜分割后两段
        # 分别入账），落库时整体写入 system_daily——替代原"整段归当天"的单值累计。
        self._pending_cpu_energy_by_date: dict[str, float] = {}
        # 兼容旧接口：仍暴露单值视图（内部从 per-date 求和）
        self._pending_cpu_energy_wh: float = 0.0
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
            "cpu_model_raw": None, "cpu_base_frequency_mhz": None, "cpu_sockets": None,
            "architecture": None, "os_build": None, "os_display": None,
            "physical_cores": None, "logical_cpus": None,
            "installed_ram_bytes": None,
            "motherboard_manufacturer": None, "motherboard_model": None,
            "bios_version": None,
            "gpu_list": [], "disk_list": [], "boot_time": None,
        }
        inv["os"] = _os_description()
        inv["computer_name"] = _safe(lambda: __import__("platform").node())
        cpu_model_raw = _safe(lambda: (psutil.cpu_freq() and "") or _cpu_model())
        inv["cpu_model_raw"] = cpu_model_raw
        inv["cpu_model"] = _clean_cpu_model(cpu_model_raw)
        # 基准频率：从型号 "@2.70GHz" 解析（静态标称值，非实时）。解析失败 -> None（不猜）。
        inv["cpu_base_frequency_mhz"] = _parse_base_freq_mhz(cpu_model_raw)
        # 架构（§158）：AMD64 / x64。
        inv["architecture"] = _arch_label()
        # OS 版本（§157/§159）：os_display = "Windows 11"；os_build = "26200.9457"。
        _osv = _os_version_parts()
        inv["os_display"] = _osv.get("display")
        inv["os_build"] = _osv.get("build")
        # CPU 插槽数（§161）：Windows 用 Win32_Processor 实例数（多路可靠）；非 Windows None。
        inv["cpu_sockets"] = _cpu_socket_count()
        inv["logical_cpus"] = _safe(psutil.cpu_count, logical=True)
        inv["physical_cores"] = _safe(psutil.cpu_count, logical=False)
        # 1.1.3：Heat Grid 视图元数据——逻辑核到物理核的分组（cpu_affinity），
        # 供前端"物理核 / 逻辑核"切换。失败时 core_groups 为空（前端回退逻辑核平铺）。
        inv["core_view"] = "physical"  # 默认物理核视图（规格 §62）
        inv["core_groups"] = _core_groups()
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

    def _cpu_energy_delta(self, now_mono: float, now_wall: float) -> dict[str, float]:
        """
        本轮 CPU 能耗增量，按自然日归属（Wh）。

        返回 {date: Wh}（跨午夜时同一段能量分到两天，与 GPU _split_energy_across_midnight
        同语义）。power 缺失或 gap 超限时返回空 dict（不积分）。

        AUDIT-1.1.1 DATA-1111-006：原实现只返回单值 Wh，调用方按"整段归当天"入账，
        跨午夜段会被全部记到新一天（旧一天少记）。现在基线多记 prev_wall，
        用 _split_cpu_energy_across_midnight 按精确午夜分割到自然日。
        """
        power = self.advanced_cpu_power_w
        if power is None:
            self._cpu_power_prev = None
            return {}
        prev = self._cpu_power_prev
        out: dict[str, float] = {}
        if prev is not None:
            prev_mono, prev_wall, prev_power = prev
            dt = now_mono - prev_mono
            max_gap = self.config.system.history_interval_seconds * ENERGY_GAP_FACTOR
            if 0 < dt <= max_gap:
                e = (prev_power + power) / 2.0 * dt / 3600.0
                out = self._split_cpu_energy_across_midnight(prev_wall, now_wall, e)
        self._cpu_power_prev = (now_mono, now_wall, power)
        return out

    @staticmethod
    def _split_cpu_energy_across_midnight(prev_wall: float, curr_wall: float,
                                          energy_wh: float) -> dict[str, float]:
        """
        把 [prev_wall, curr_wall] 区间的 CPU 能量按"本机午夜"精确分割到自然日。
        与 GpuCollector._split_energy_across_midnight 同语义（wall 只定位午夜边界，
        不计算时长）：
        - 两端同一自然日 / wall 回拨：全部归 curr 日期；
        - 跨日：以本机午夜 00:00:00 为界，按 wall 时间比例分到前一天 / 当天。
        """
        from datetime import datetime
        from db import local_date

        curr_date = local_date(curr_wall)
        if local_date(prev_wall) == curr_date or curr_wall <= prev_wall:
            return {curr_date: energy_wh}
        midnight = datetime.strptime(curr_date, "%Y-%m-%d").timestamp()
        span = curr_wall - prev_wall
        if not (prev_wall < midnight <= curr_wall) or span <= 0:
            return {curr_date: energy_wh}
        ratio_prev = (midnight - prev_wall) / span
        prev_date = local_date(midnight - 1.0)
        out: dict[str, float] = {curr_date: energy_wh * (1.0 - ratio_prev)}
        if energy_wh * ratio_prev > 1e-9:
            out[prev_date] = energy_wh * ratio_prev
        return out

    # ---------- 采样 ----------

    def _sample_psutil(self) -> tuple:
        """
        阻塞的 psutil 采样段（AUDIT-1.1.1 REL-1111-001：从 poll_once 抽出）。

        Windows 上 disk_io_counters 等常走 WMI/GetSystemPowerInformation，
        单次 10~50ms 偶发更高——直接在事件循环里跑会周期性阻塞所有 HTTP 端点。
        本方法只读取原始值，不做速率/能耗/落库（那些留在事件循环线程，
        保持 db 单连接单线程不变量）。
        """
        usage = _safe(psutil.cpu_percent, interval=None)
        # 1.1.3：逐逻辑核利用率（percpu=True，与聚合值同一次采样窗口对齐）。
        # 首次 warmup 返回全 0/None——由 _apply_sample 的 _cpu_warmed 统一丢弃。
        per_core = _safe(psutil.cpu_percent, percpu=True, interval=None)
        freq = _safe(psutil.cpu_freq)
        vm = _safe(psutil.virtual_memory)
        dio = _safe(psutil.disk_io_counters)
        nio = _safe(psutil.net_io_counters)
        # Round-3：per-adapter counters（供默认接口速率 + 接口选择器即时速率；
        # 与聚合 nio 同一采样窗口，counter 一致）。
        pernic = _safe(psutil.net_io_counters, pernic=True)
        return (usage, per_core, freq, vm, dio, nio, pernic)

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
        was_warmed = self._cpu_warmed
        raw = self._sample_psutil()
        self._apply_sample(sample, raw, now_mono)
        self._finish_sample(sample, now_wall, now_mono, was_warmed)
        return sample

    async def poll_once_async(self) -> SystemSample | None:
        """
        poll_once 的 async 版本（AUDIT-1.1.1 REL-1111-001）：阻塞的 psutil 采样
        放到 asyncio.to_thread（事件循环不被 10~50ms/轮 卡住），速率/能耗/落库
        仍在事件循环线程（db 单连接单线程不变量）。
        """
        if not self.enabled:
            return None
        now_wall = self.clock.now()
        now_mono = self.clock.monotonic()
        sample = SystemSample(timestamp=now_wall)
        was_warmed = self._cpu_warmed
        try:
            raw = await asyncio.to_thread(self._sample_psutil)
        except Exception:
            # _safe 已兜底每个指标；这里防 to_thread 本身异常（如取消/解释器退出）
            self._set_available(False)
            return None
        self._apply_sample(sample, raw, now_mono)
        self._finish_sample(sample, now_wall, now_mono, was_warmed)
        return sample

    def _apply_sample(self, sample: SystemSample, raw: tuple, now_mono: float) -> None:
        """把原始 psutil 值填入 sample（速率用 monotonic；counter reset 安全）。"""
        usage, per_core, freq, vm, dio, nio, pernic = raw

        # CPU 利用率（interval=None 非阻塞；首次 warmup：本条不写 DB、不作为有效利用率）。
        # Round-3 系统页（§6/§10/§57/§58）：整机 CPU 利用率 = **逐逻辑核均值**。
        # psutil.cpu_percent(None) 在多处理器组（双路 Xeon）Windows 上只读
        # processor group 0，实测恒为 per-core mean 的 ~2 倍（sys_cpu_raw.py）——
        # 这正是"当前 3% / 历史 100%"异常采集的根因。per-core 缺失（单路/无 percpu）
        # 时回退到聚合值（单路机两者一致）。
        if not self._cpu_warmed:
            # 首次：只建立基线（per_core 同窗口，一并丢弃）
            self._cpu_warmed = True
        else:
            if per_core is not None and len(per_core) > 0:
                pc = [float(x) for x in per_core]
                sample.cpu_per_core_percent = pc
                sample.cpu_usage_percent = sum(pc) / len(pc)
            elif usage is not None:
                sample.cpu_usage_percent = usage
        if freq is not None and freq.current is not None:
            sample.cpu_frequency_mhz = freq.current
        # 基准/标称频率（静态，从库存型号解析；供 CPU 区"当前频率 + 基准频率"次值）
        base = (self.inventory or {}).get("cpu_base_frequency_mhz")
        if base is not None:
            sample.cpu_base_frequency_mhz = float(base)

        # 内存
        if vm is not None:
            sample.memory_used_bytes = vm.total - vm.available
            sample.memory_total_bytes = vm.total
            sample.memory_usage_percent = vm.percent

        # 磁盘 IO（累计 counter -> 速率；monotonic）
        if dio is not None and dio.read_bytes is not None and dio.write_bytes is not None:
            if self._disk_prev is not None:
                prev_mono, prev_read, prev_write = self._disk_prev
                dt = now_mono - prev_mono
                sample.disk_read_bps = _counter_rate(prev_read, dio.read_bytes, dt)
                sample.disk_write_bps = _counter_rate(prev_write, dio.write_bytes, dt)
            self._disk_prev = (now_mono, dio.read_bytes, dio.write_bytes)

        # 网络 IO（累计 counter -> 速率；monotonic）。
        # Round-3 系统页（§94-§102）：默认速率 = 拥有默认路由的主接口（WLAN/以太网等）。
        # 盲目相加所有 NIC 会让 WireGuard/VPN 隧道流量在"隧道+物理网卡"两处各计一次
        # （双重统计）。无法可靠识别默认接口时回退全接口合计（network_interface=None，
        # UI 显示"接口合计"并提示可能含虚拟网卡）。每轮同时算出所有接口的当前速率
        # （self._net_adapters），供 /api/system/status network.adapters + 接口选择器。
        defnic = self._default_network_interface()
        if nio is not None and nio.bytes_recv is not None and nio.bytes_sent is not None:
            # 所有接口当前速率（per-adapter，同窗口 counter delta）
            adapters: dict[str, dict] = {}
            if pernic:
                for name, cc in pernic.items():
                    if cc is None or getattr(cc, "bytes_recv", None) is None:
                        continue
                    prev = self._net_per_prev.get(name)
                    rx = tx = None
                    if prev is not None:
                        dt = now_mono - prev[0]
                        rx = _counter_rate(prev[1], cc.bytes_recv, dt)
                        tx = _counter_rate(prev[2], cc.bytes_sent, dt)
                    adapters[name] = {"rx_bps": rx, "tx_bps": tx}
                self._net_per_prev = {
                    name: (now_mono, cc.bytes_recv, cc.bytes_sent)
                    for name, cc in pernic.items()
                    if cc is not None and getattr(cc, "bytes_recv", None) is not None
                }
            self._net_adapters = adapters
            # 默认接口的速率作为页面主值（None -> 全接口合计，回退）
            if defnic is not None and defnic in adapters:
                sample.network_rx_bps = adapters[defnic]["rx_bps"]
                sample.network_tx_bps = adapters[defnic]["tx_bps"]
                sample.network_interface = defnic
            else:
                if self._net_prev is not None:
                    prev_mono, prev_rx, prev_tx = self._net_prev
                    dt = now_mono - prev_mono
                    sample.network_rx_bps = _counter_rate(prev_rx, nio.bytes_recv, dt)
                    sample.network_tx_bps = _counter_rate(prev_tx, nio.bytes_sent, dt)
                sample.network_interface = None
            self._net_prev = (now_mono, nio.bytes_recv, nio.bytes_sent)
        else:
            sample.network_interface = defnic

        # 高级传感器（provider 注入；None = 不可用）
        sample.cpu_temperature_c = self.advanced_cpu_temperature_c
        sample.cpu_package_power_w = self.advanced_cpu_power_w

        # 已监测组件功耗 = 所有可读取（非 None）组件功耗之和。
        # 1.1.4 精修（§28/§29）：null != 0 —— None 表示"该组件不可读取"，
        # 不参与求和（CPU Package 不可用但 GPU 有值时应显示 GPU 的 W，而非 None）；
        # 真实 0 是有效读数（保留）。全部 None 时才为 None（UI 显示 --）。
        _parts = [sample.cpu_package_power_w, self.gpu_power_total_w]
        _parts = [p for p in _parts if p is not None]
        sample.monitored_component_power_w = float(sum(_parts)) if _parts else None

    # ---------- 网络接口（Round-3 §94-§102：避免盲目全接口相加双算虚拟网卡） ----------

    _DEFAULT_IF_CACHE: float = 0.0
    _DEFAULT_IF_NAME: str | None = None

    def _default_network_interface(self) -> str | None:
        """拥有默认 IPv4 路由的主接口名（缓存 60s，避免每轮起 PowerShell）。
        找不到 -> None（UI 显示"接口合计"并提示可能含虚拟网卡流量）。"""
        now = self.clock.monotonic()
        if self._DEFAULT_IF_NAME is not None and (now - self._DEFAULT_IF_CACHE) < 60.0:
            return self._DEFAULT_IF_NAME
        name = None
        try:
            # 默认路由：解析默认 IPv4 路由对应的接口名（Windows Get-NetRoute / 其它平台 route）
            name = _default_route_interface()
        except Exception:
            name = None
        self._DEFAULT_IF_NAME = name
        self._DEFAULT_IF_CACHE = now
        return name

    def adapter_rates(self) -> dict[str, dict]:
        """所有接口当前速率（每轮 poll 计算并缓存）：{name: {rx_bps, tx_bps}}。
        供 /api/system/status network.adapters（接口选择器即时显示所选接口的速率）。"""
        return self._net_adapters

    def network_interfaces(self) -> list[dict]:
        """接口列表（供 /api/system/network-interfaces 与选择器）：
        [{name, kind, is_default, speed_mbps, is_virtual, errin, errout, dropin, dropout}]。
        只列出"有流量意义"的接口（排除纯 Loopback 与全 0 的死接口由前端/调用方判断，
        这里给原始信息 + 默认标记）。"""
        pernic = _safe(psutil.net_io_counters, pernic=True) or {}
        stats = _safe(psutil.net_if_stats) or {}
        addrs = _safe(psutil.net_if_addrs) or {}
        default_name = self._default_network_interface()
        out: list[dict] = []
        for name in pernic.keys():
            c = pernic[name]
            st = stats.get(name)
            kind = _if_kind(name, st)
            out.append({
                "name": name,
                "kind": kind,
                "is_default": (name == default_name),
                "speed_mbps": getattr(st, "speed", None),
                "is_virtual": _is_virtual_if(name),
                "errin": getattr(c, "errin", None),
                "errout": getattr(c, "errout", None),
                "dropin": getattr(c, "dropin", None),
                "dropout": getattr(c, "dropout", None),
            })
        return out

    def _finish_sample(self, sample: SystemSample, now_wall: float, now_mono: float,
                       was_warmed: bool) -> None:
        """能耗积分 + 按间隔落库 + 实时 ring（poll_once / poll_once_async 共用）。

        was_warmed：本轮**之前**是否已 warmup（warmup 轮不落库，由调用方在
        _apply_sample 修改 _cpu_warmed 之前捕获）。
        """
        # CPU 能耗积分（power 可靠时；gap 超限不积分）——按自然日归属（跨午夜分割）
        energy_by_date = self._cpu_energy_delta(now_mono, now_wall)
        for date, wh in energy_by_date.items():
            if wh > 0:
                self._cpu_energy_today[date] = self._cpu_energy_today.get(date, 0.0) + wh
                self._last_energy_date = date
                # 未落库轮的能耗按日累计（落库节流时不丢段；跨午夜两段分别入账）
                self._pending_cpu_energy_by_date[date] = (
                    self._pending_cpu_energy_by_date.get(date, 0.0) + wh
                )
        self._pending_cpu_energy_wh = sum(self._pending_cpu_energy_by_date.values())

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
                        # "已监测组件能耗"的 GPU 部分**不**在这里累加——
                        # system_daily.monitored_component_energy_wh 列只存 CPU 部分
                        # （与 cpu_energy_wh 相同），GPU 能量由 gpu_daily 维护；
                        # API 层（server.api_system_daily）按日把两边相加，
                        # 使"今日能耗"= CPU + 被监控 GPU，与实时"组件功耗合计"同口径。
                        "monitored_component_energy_wh": self._pending_cpu_energy_wh,
                    },
                    # AUDIT-1.1.1 DATA-1111-006：CPU 能耗按自然日分别入账（跨午夜
                    # 段的前一天部分也计入旧一天），而不是整段归样本当天。
                    cpu_energy_wh_by_date=dict(self._pending_cpu_energy_by_date),
                    now=now_wall,
                    retention_seconds=self.config.system.history_retention_hours * 3600,
                )
                self._last_db_write = now_wall
                self._pending_cpu_energy_by_date = {}
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


# ---------- Round-3 系统页：硬件信息辅助（低频，启动/手动刷新时调用） ----------

import re as _re

def _clean_cpu_model(raw: str | None) -> str | None:
    """把 'Intel(R) Xeon(R) Platinum 8168 CPU @ 2.70GHz' 清理为展示用型号（视觉，不改 raw 后台值）。
    规则：去掉 (R) 商标括号、结尾 'CPU @ x.xGHz'（频率单列展示）。无法解析时原样返回。"""
    if not raw:
        return raw
    s = raw.strip()
    # 去 "@ 2.70GHz" 尾巴（含大小写）
    s = _re.sub(r"\s*@\s*[\d.]+\s*GHz\s*$", "", s, flags=_re.I)
    # 去 "CPU" 尾巴（若 @ 已被去掉后还剩 "CPU"）
    s = _re.sub(r"\s+CPU\s*$", "", s, flags=_re.I)
    # 去 (R) 商标
    s = s.replace("(R)", "").replace("(r)", "")
    # 折叠多余空格
    s = _re.sub(r"\s+", " ", s).strip(" -–")
    return s or raw


def _parse_base_freq_mhz(raw: str | None) -> float | None:
    """从型号字符串解析基准/标称频率（'@ 2.70GHz' -> 2700.0）。解析失败 -> None（不猜）。"""
    if not raw:
        return None
    m = _re.search(r"@?\s*([\d.]+)\s*GHz", raw, _re.I)
    if m:
        try:
            return float(m.group(1)) * 1000.0
        except (ValueError, TypeError):
            return None
    m = _re.search(r"([\d]{3,5})\s*MHz", raw, _re.I)
    if m:
        try:
            return float(m.group(1))
        except (ValueError, TypeError):
            return None
    return None


def _arch_label() -> str | None:
    """架构展示（§158）：AMD64/x86_64 -> x64；aarch64 -> arm64；i686/x86 -> x86。"""
    try:
        import platform
        m = platform.machine() or ""
    except Exception:
        return None
    m = m.lower()
    if m in ("amd64", "x86_64"):
        return "x64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    if m in ("i386", "i686", "x86"):
        return "x86"
    return m or None


def _os_version_parts() -> dict:
    """OS 版本（§157/§159）：
    - display = 'Windows 11'（由 platform 主版本映射，不用 'Windows-11' 带连字符的 raw）；
    - build   = '26200.9457'（registry CurrentBuild + UBR；无则仅 CurrentBuild）。
    非 Windows 返回 {'display': platform.platform(terse), 'build': None}。
    任何失败字段 -> None（不猜）。"""
    out: dict[str, str | None] = {"display": None, "build": None}
    try:
        import platform
        system = platform.system() or ""
        if system != "Windows":
            out["display"] = platform.platform(terse=True) or None
            return out
        # Windows N -> "Windows {name}"
        major = platform.release()  # '10'
        name = {
            "10": "10", "11": "11",
            "9": "9", "8.1": "8.1", "8": "8",
            "7": "7", "6.3": "8.1", "6.2": "8", "6.1": "7", "6.0": "Vista",
        }.get(str(major), str(major) or None)
        out["display"] = ("Windows " + name) if name else None
        # build：读注册表（低频，仅启动/刷新时）
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                               r"SOFTWARE\Microsoft\Windows NT\CurrentVersion")
            def rd(n):
                try:
                    return winreg.QueryValueEx(k, n)[0]
                except Exception:
                    return None
            cb = rd("CurrentBuild")
            ubr = rd("UBR")
            if cb is not None:
                out["build"] = (str(cb) + "." + str(ubr)) if ubr not in (None, "") else str(cb)
        except Exception:
            pass
    except Exception:
        pass
    return out


def _cpu_socket_count() -> int | None:
    """CPU 插槽/Package 数（§161，多路）：Windows 用 Win32_Processor 实例数；
    其它平台 / 失败 -> None（不猜，UI 隐藏该项）。"""
    if not _IS_WINDOWS:
        return None
    try:
        import subprocess
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "@(Get-CimInstance Win32_Processor).Count"],
            capture_output=True, text=True, timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout.strip()
        n = int(out)
        return n if n >= 1 else None
    except Exception:
        return None


def _default_route_interface() -> str | None:
    """拥有默认 IPv4 路由的接口名（Windows：Get-NetRoute）。找不到 -> None。"""
    if _IS_WINDOWS:
        try:
            import subprocess
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-NetRoute -DestinationPrefix 0.0.0.0/0 -ErrorAction SilentlyContinue "
                 "| Select-Object -First 1).InterfaceAlias"],
                capture_output=True, text=True, timeout=8,
                creationflags=subprocess.CREATE_NO_WINDOW,
            ).stdout.strip()
            return out or None
        except Exception:
            return None
    # 其它平台：route get default / ip route
    try:
        import subprocess
        out = subprocess.run(["sh", "-c", "ip route get default 2>/dev/null || route get default 2>/dev/null"],
                             capture_output=True, text=True, timeout=4).stdout
        m = _re.search(r"dev\s+(\S+)", out) or _re.search(r"interface:\s+(\S+)", out)
        return m.group(1) if m else None
    except Exception:
        return None


def _is_virtual_if(name: str) -> bool:
    """接口是否虚拟（WireGuard/VPN/TUN/虚拟交换机/Loopback）。名称启发式（不依赖 is_virtual 属性）。"""
    n = (name or "").lower()
    if "loopback" in n or n == "lo":
        return True
    for tag in ("wireguard", "tunnel", "vpn", "vethernet", "virtual", "virtualbox",
                "hamachi", "tap", "tun", "docker", "hyper-v", "hyper_v", "wsl",
                "bluetooth", "host network", "default switch"):
        if tag in n:
            return True
    # WireGuard 适配器常用数字命名（000005 之类）——保守：纯 6 位数字视为隧道
    if _re.fullmatch(r"\d{6}", (name or "").strip()):
        return True
    return False


def _if_kind(name: str, st) -> str:
    """接口类型展示标签：WLAN / Ethernet / VPN·虚拟 / 其它。仅用于选择器标注。"""
    n = (name or "").lower()
    if "wlan" in n or "wi-fi" in n or "wifi" in n or "wi fi" in n:
        return "Wi-Fi"
    if "以太网" in name or "ethernet" in n or "ethernet" in n:
        return "以太网"
    if _is_virtual_if(name):
        return "虚拟/VPN"
    return "其它"
