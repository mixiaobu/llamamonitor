"""
gpu_collector.py — Windows NVIDIA GPU 只读采样器（Phase 9）

设计约束（纯旁路）：
- 数据只来自系统 NVIDIA 驱动自带的 nvidia-smi.exe（不打包进程序、不装 pynvml /
  CUDA Python 包 / PyTorch）；
- nvidia-smi 参数固定（见 NVSMI_QUERY），任何 API 都不接受自定义参数，
  不存在 GPU 控制接口；
- asyncio.create_subprocess_exec 非阻塞调用，timeout 固定 3 秒，超时 kill 子进程，
  绝不阻塞 FastAPI 事件循环；
- 某轮失败（找不到 nvidia-smi / 调用失败 / 无数据）只把 available 置 false，
  状态变化时记一条日志（available 翻转才写 WARNING/INFO），不刷屏；
  llama.cpp metrics 采集完全不受影响。

GPU 身份：
- 数据库与内部状态一律用 nvidia-smi UUID（驱动升级 / 硬件变化 / 拔插后
  index 可能变化，UUID 稳定）；index 只用于 UI 显示。

能耗估算：
- 相邻两个成功采样之间用梯形积分：
  energy_wh += ((previous_power + current_power) / 2) * delta_t / 3600
- delta_t > poll_interval_seconds * 3 视为 unknown gap（睡眠/断线），
  该段不积分——不能假设 GPU 一直保持最后一次采样时的功耗。

运行数据全部落 SQLite（gpu_samples / gpu_daily，见 db.py）；
config.json 只保存设置（enabled / 周期 / 保留时长 / 指定 UUID）。
"""

from __future__ import annotations

import asyncio
import contextlib
import csv as _csv
import io
import logging
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from clock import Clock, default_clock
from config import AppConfig
from db import Database, local_date

logger = logging.getLogger("llamamonitor.gpu")

# Phase 11：monotonic 间隔 >= 该秒数时，GPU 缺口原因记 system_pause_or_sleep（睡眠/暂停），
# 否则 unknown（短断档无法区分原因）
SLEEP_HINT_SECONDS = 300.0

# Windows：启动 nvidia-smi 等 console 子进程时加 CREATE_NO_WINDOW，避免每轮（默认 5s）
# 弹一个控制台窗口闪一下（UI 层可见的"终端框闪烁"）。非 Windows 平台该 flag 不存在，
# 传 0 即无副作用（asyncio 在非 Windows 上忽略 creationflags）。
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# 固定的 nvidia-smi 查询参数（顺序与 _NVM_COLUMNS 一一对应）
# 1.1：fast query 扩展 10 列（utilization.memory / power.limit / pstate /
# pcie gen·width max / driver_version / throttle reasons）——全部字段独立可选，
# 旧 nvidia-smi 版本不认识的字段输出 N/A 而不会让整行失败；列数不符的行跳过。
NVSMI_QUERY = [
    "--query-gpu=index,uuid,name,memory.used,memory.total,utilization.gpu,utilization.memory,"
    "temperature.gpu,power.draw,power.limit,fan.speed,clocks.sm,clocks.mem,"
    "pstate,pcie.link.gen.current,pcie.link.width.current,"
    "pcie.link.gen.max,pcie.link.width.max,driver_version,"
    "clocks_event_reasons.active",
    "--format=csv,noheader,nounits",
]

# 1.1：slow health query（60s 周期）——ECC / Row Remapper / Retired Pages。
# ECC 错误计数用 CSV（可机器解析）；Remapped Rows / Retired Pages 部分驱动版本
# 只出现在 -q -d ECC 的文本里，用 parse 兜底（解析失败不影响 CSV 部分）。
NVSMI_ECC_QUERY = [
    "--query-gpu=uuid,ecc.mode.current,"
    "ecc.errors.corrected.volatile.sram,ecc.errors.corrected.volatile.dram,"
    "ecc.errors.corrected.aggregate.sram,ecc.errors.corrected.aggregate.dram,"
    "ecc.errors.uncorrected.volatile.sram,ecc.errors.uncorrected.volatile.dram,"
    "ecc.errors.uncorrected.aggregate.sram,ecc.errors.uncorrected.aggregate.dram",
    "--format=csv,noheader,nounits",
]
NVSMI_ECC_DETAIL = ["-q", "-d", "ECC"]   # 文本兜底（Retired Pages / Remapped Rows）
NVSMI_PROCS_QUERY = [
    "--query-compute-apps=pid,process_name,gpu_uuid,used_memory",
    "--format=csv,noheader,nounits",
]

# slow query 周期（秒）：ECC/Remapped Rows 是低频指标，绝不随 fast query 每 5s 查询
SLOW_QUERY_INTERVAL = 60.0

# nvidia-smi Throttle Reasons 位掩码 -> 名称（文档化位表；0x0 = "Not Active"）
_THROTTLE_REASON_BITS = {
    0x01: "GPU Idle",
    0x02: "Applications Clocks Setting",
    0x04: "Sync Boost",
    0x08: "SW Power Cap",
    0x10: "HW Slowdown",
    0x20: "HW Thermal Slowdown",
    0x40: "HW Power Brake Slowdown",
    0x80: "SW Thermal Slowdown",
    0x100: "SW Power Capping Throttle",
    0x200: "SW Thermal Capping Throttle",
}

# PATH 中找不到时的备用位置（NVIDIA 驱动标准安装路径）
NVSMI_FALLBACK_PATH = Path(r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe")

# nvidia-smi 调用超时（秒）：超时 kill 子进程，不让 GPU 循环卡住
NVSMI_TIMEOUT_SECONDS = 3.0

# 这些字段值（大小写不敏感、去空格后）一律解析为 None
_NA_TOKENS = {"", "n/a", "na", "not supported", "[not supported]", "[n/a]", "null", "none"}

# 每行固定 20 列（与 NVSMI_QUERY 一一对应，1.1 扩展）：
# index, uuid, name, memory.used, memory.total, utilization.gpu, utilization.memory,
# temperature.gpu, power.draw, power.limit, fan.speed, clocks.sm, clocks.mem,
# pstate, pcie.link.gen.current, pcie.link.width.current,
# pcie.link.gen.max, pcie.link.width.max, driver_version, clocks_event_reasons.active
_NVM_COLUMNS = 20


def find_nvidia_smi() -> Path | None:
    """
    查找 nvidia-smi：先 PATH（shutil.which），再驱动标准安装路径。
    找不到返回 None（GPU Monitor unavailable，LlamaMonitor 本身继续正常运行）。
    """
    found = shutil.which("nvidia-smi")
    if found:
        return Path(found)
    if NVSMI_FALLBACK_PATH.is_file():
        return NVSMI_FALLBACK_PATH
    return None


def _parse_number(token: str) -> float | None:
    """单个字段值 -> float | None；N/A / Not Supported / [Not Supported] / 空 / 非法 -> None。"""
    token = token.strip()
    if token.lower() in _NA_TOKENS:
        return None
    try:
        value = float(token)
    except ValueError:
        return None
    if value != value or value in (float("inf"), float("-inf")):  # NaN/Inf 视为不可用
        return None
    return value


def _parse_int(token: str) -> int | None:
    value = _parse_number(token)
    if value is None:
        return None
    return int(value)


def _parse_driver_version(token: str) -> str | None:
    """driver_version 文本字段 -> str | None（N/A / Not Supported / 空 -> None）。"""
    token = token.strip()
    if not token or token.lower() in _NA_TOKENS:
        return None
    return token


@dataclass
class GpuSnapshot:
    """一张 GPU 的一个采样点。所有数值字段：int / float / None（不把 'N/A' 传到前端）。"""

    timestamp: float
    index: int | None
    uuid: str
    name: str
    utilization_percent: float | None
    memory_used_mb: float | None
    memory_total_mb: float | None
    temperature_c: float | None
    power_draw_w: float | None
    fan_percent: float | None
    sm_clock_mhz: float | None
    memory_clock_mhz: float | None
    pcie_generation: int | None
    pcie_width: int | None
    # ---- 1.1 高级遥测（fast query 扩展列；旧数据/旧驱动 -> None；给默认值，
    #      测试/旧调用方可以只传旧字段构造快照）----
    memory_controller_percent: float | None = None   # 显存**控制器**利用率（!= 显存占用率！）
    power_limit_w: float | None = None               # 功耗上限（power.draw/limit 都有才算 power%）
    pcie_gen_max: int | None = None                  # PCIe 最大能力（当前 vs 最大）
    pcie_width_max: int | None = None
    performance_state: int | None = None             # P-state（P0/P2/...；P0 不等于 100% 性能）
    driver_version: str | None = None
    throttle_reasons: list[str] = field(default_factory=list)  # 性能限制原因（位掩码解码；非故障）
    # ---- 1.1 slow health（60s 周期；不支持的 GPU 保持 None -> UI 整个 ECC 区隐藏）----
    ecc_enabled: bool | None = None                  # None = 不支持（不显示），True/False = 支持
    ecc_corrected_volatile: int | None = None
    ecc_corrected_aggregate: int | None = None
    ecc_uncorrected_volatile: int | None = None
    ecc_uncorrected_aggregate: int | None = None
    retired_pages: int | None = None                 # 退役页（-q 文本；不可解析 -> None）
    remapped_rows: int | None = None                 # 重映射行（不可解析 -> None）

    def to_row(self) -> dict:
        """gpu_samples 行（与 db.save_gpu_samples 的 INSERT 列一致）。"""
        return {
            "timestamp": self.timestamp,
            "gpu_uuid": self.uuid,
            "gpu_index": self.index,
            "gpu_name": self.name,
            "utilization_percent": self.utilization_percent,
            "memory_used_mb": self.memory_used_mb,
            "memory_total_mb": self.memory_total_mb,
            "temperature_c": self.temperature_c,
            "power_draw_w": self.power_draw_w,
            "fan_percent": self.fan_percent,
            "sm_clock_mhz": self.sm_clock_mhz,
            "memory_clock_mhz": self.memory_clock_mhz,
            "pcie_generation": self.pcie_generation,
            "pcie_width": self.pcie_width,
            "memory_controller_percent": self.memory_controller_percent,
            "power_limit_w": self.power_limit_w,
            "pcie_gen_max": self.pcie_gen_max,
            "pcie_width_max": self.pcie_width_max,
            "performance_state": self.performance_state,
            "ecc_enabled": self.ecc_enabled,
            "ecc_corrected_volatile": self.ecc_corrected_volatile,
            "ecc_corrected_aggregate": self.ecc_corrected_aggregate,
            "ecc_uncorrected_volatile": self.ecc_uncorrected_volatile,
            "ecc_uncorrected_aggregate": self.ecc_uncorrected_aggregate,
        }


def _parse_pstate(token: str) -> int | None:
    """'P8' -> 8；N/A / 非法 -> None。"""
    token = token.strip()
    if token.lower() in _NA_TOKENS:
        return None
    if len(token) >= 2 and token[0].upper() == "P" and token[1:].isdigit():
        return int(token[1:])
    return None


def _parse_throttle_reasons(token: str) -> list[str]:
    """
    解码 nvidia-smi throttle reasons 位掩码（0x... 或 0）为原因名列表。
    0x0（Not Active）-> []；未知位保留十六进制标记（不丢弃数据）。
    解析失败 -> []（该字段可选，不影响整行）。
    """
    token = token.strip().lower()
    if token in ("", "n/a", "na", "not supported", "[not supported]", "0x0", "0"):
        return []
    try:
        value = int(token, 16) if token.startswith("0x") else int(token, 0)
    except ValueError:
        return []
    if value <= 0:
        return []
    out: list[str] = []
    for bit, name in sorted(_THROTTLE_REASON_BITS.items()):
        if value & bit:
            out.append(name)
    consumed = sum(b for b in _THROTTLE_REASON_BITS if value & b)
    if value & ~consumed:  # 文档外的未知位
        out.append(f"Unknown(0x{value & ~consumed:x})")
    return out


def parse_nvidia_smi_csv(text: str, now: float | None = None) -> list[GpuSnapshot]:
    """
    解析 `nvidia-smi --query-gpu=... --format=csv,noheader,nounits` 的输出（1.1：20 列）。

    - 每行 20 列（NVSMI_QUERY 顺序）；GPU 名称含逗号时 nvidia-smi 会加引号
      （csv 模块正确处理）；
    - uuid 为空或列数不符的行跳过（不影响其他 GPU）；
    - 1.1 扩展列（utilization.memory / power.limit / pstate / pcie max /
      driver_version / throttle）全部可选：N/A -> None / []（旧驱动不会让整行失败）；
    - 空输出 / 全为垃圾 -> []；本函数不抛异常。
    """
    now = time.time() if now is None else now
    out: list[GpuSnapshot] = []
    # skipinitialspace=True：nvidia-smi 输出形如 "0, GPU-x, NAME, ..."，
    # 逗号后恒有一个空格；含逗号的名称会被 nvidia-smi 加引号（" "Name, X""），
    # 只有跳过前导空格 csv 才能正确识别引号字段
    for row in _csv.reader(io.StringIO(text), skipinitialspace=True):
        if len(row) != _NVM_COLUMNS:
            continue
        uuid = row[1].strip()
        if not uuid:
            continue
        out.append(
            GpuSnapshot(
                timestamp=now,
                index=_parse_int(row[0]),
                uuid=uuid,
                name=row[2].strip(),
                memory_used_mb=_parse_number(row[3]),
                memory_total_mb=_parse_number(row[4]),
                utilization_percent=_parse_number(row[5]),
                memory_controller_percent=_parse_number(row[6]),
                temperature_c=_parse_number(row[7]),
                power_draw_w=_parse_number(row[8]),
                power_limit_w=_parse_number(row[9]),
                fan_percent=_parse_number(row[10]),
                sm_clock_mhz=_parse_number(row[11]),
                memory_clock_mhz=_parse_number(row[12]),
                performance_state=_parse_pstate(row[13]),
                pcie_generation=_parse_int(row[14]),
                pcie_width=_parse_int(row[15]),
                pcie_gen_max=_parse_int(row[16]),
                pcie_width_max=_parse_int(row[17]),
                driver_version=_parse_driver_version(row[18]),
                throttle_reasons=_parse_throttle_reasons(row[19]),
            )
        )
    return out


def parse_nvidia_smi_ecc_csv(text: str) -> dict[str, dict]:
    """
    解析 slow ECC query（10 列）-> {uuid: {ecc_enabled, corrected/uncorrected
    volatile/aggregate}}。

    - `ecc.mode.current` = N/A -> 该 GPU **不支持** ECC（ecc_enabled=None，
      UI 整个 ECC 区隐藏——不显示一排 --）；
    - 单个错误计数 N/A（如消费卡无 SRAM 计数）-> 该计数 None（UI 折叠 Advanced 可展开细项）；
    - corrected = SRAM + DRAM 之和（两者都可用时才加；只可用一个则取那个）；
    - 列数不符 / 解析失败 -> 该 GPU 跳过（不影响其他 GPU，更不影响 fast query）。
    """
    out: dict[str, dict] = {}
    for row in _csv.reader(io.StringIO(text), skipinitialspace=True):
        if len(row) != 10:
            continue
        uuid = row[0].strip()
        if not uuid or uuid.lower() in _NA_TOKENS:
            continue
        enabled = row[1].strip().lower()
        entry: dict = {"ecc_enabled": enabled not in _NA_TOKENS}
        for src, dst in (
            (2, "cv_sram"), (3, "cv_dram"), (4, "ca_sram"), (5, "ca_dram"),
            (6, "uv_sram"), (7, "uv_dram"), (8, "ua_sram"), (9, "ua_dram"),
        ):
            v = _parse_int(row[src])
            entry[dst] = int(v) if v is not None else None
        out[uuid] = entry
    return out


def _sum_ecc_pair(a: int | None, b: int | None) -> int | None:
    """两个 ECC 细分类计数求和：都不可用 -> None；只一个可用 -> 取那个；都有 -> 和。"""
    if a is None and b is None:
        return None
    return (a or 0) + (b or 0)


def parse_nvidia_smi_processes(text: str) -> list[dict]:
    """
    解析 `--query-compute-apps=pid,process_name,gpu_uuid,used_memory`（只读）。
    WDDM 下 used_memory 常为 [N/A] -> None（UI 显示 --，不影响其他字段）；
    进程名含路径时保留完整值（前端可自行 basename）。任何行解析失败跳过。
    """
    out: list[dict] = []
    for row in _csv.reader(io.StringIO(text), skipinitialspace=True):
        if len(row) < 4:
            continue
        pid = _parse_int(row[0])
        if pid is None:
            continue
        out.append({
            "pid": pid,
            "process_name": row[1].strip() or None,
            "gpu_uuid": row[2].strip() or None,
            "used_memory_mb": _parse_number(row[3]),
        })
    return out


class GpuCollector:
    """
    常驻 GPU 采样循环（独立于 llama.cpp metrics 采集，周期可分别配置）。

    - poll_once()：一次 发现 nvidia-smi -> 调用 -> 解析 -> 能量积分 -> 落库；
      任何失败都把 available 置 false，不抛异常；
    - 状态转换日志：available 翻转时才写（WARNING 不可用 / INFO 恢复），
      没有 nvidia-smi 的机器只会在状态翻转瞬间各记一条，不会每 5 秒刷屏；
    - runner 可注入（测试用）：async callable(args: list[str]) -> (exit_code, stdout)。
    """

    def __init__(
        self,
        config: AppConfig,
        db: Database | None = None,
        runner=None,
        timeout_seconds: float = NVSMI_TIMEOUT_SECONDS,
        clock: Clock | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.runner = runner or _default_runner
        self.timeout = timeout_seconds
        self.clock = clock or default_clock   # Phase 11：时间抽象（测试注入 FakeClock）
        self.available: bool = False
        self.unavailable_reason: str | None = None   # 供 /api/gpu/status 展示
        self.last_update: float | None = None        # 最近一次成功采样时刻
        # 已检测到的 GPU（nvidia-smi 报告的全集，含被 device_uuids 过滤掉的；
        # 供 Settings 页面展示；失败轮保留最后已知值）
        self.detected: list[dict] = []
        self._last_available: bool | None = None     # 状态转换日志用
        # uuid -> (上次采样 wall, 上次采样 mono, 上次 power_w)：能量梯形积分用
        # （Phase 11：能量 Δt 用 monotonic；进程重启后首个采样不积分，误差 <= 一个采样周期）
        self._prev: dict[str, tuple[float, float, float | None]] = {}
        # ---- 1.1 slow health 查询状态（ECC/Retired/Remapped/进程，60s 周期）----
        self._last_slow_mono: float | None = None
        self._last_slow_wall: float | None = None
        # uuid -> slow health 结果（ECC 等；fast 查询每轮把它合并进快照）
        self.slow_health: dict[str, dict] = {}
        self.processes: list[dict] = []              # 全部 GPU 的进程（只读）
        self.driver_version: str | None = None
        self.latest: list[GpuSnapshot] = []          # 最近一轮快照（组件功耗合计用）
        self.latest_throttle: dict[str, list[str]] = {}  # uuid -> throttle reasons

    # ---------- 能耗（Phase 11：monotonic 间隔 + 精确午夜分割） ----------

    def _split_energy_across_midnight(
        self, prev_wall: float, curr_wall: float, energy_wh: float
    ) -> dict[str, float]:
        """
        把 [prev_wall, curr_wall] 区间的能量按"本机午夜"精确分割到自然日：
        - 两端同一自然日：全部归 curr 的日期；
        - 跨日（如 23:59:58 -> 00:00:03）：以本机午夜 00:00:00 为界，
          按 wall 时间比例把能量分到前一天 / 当天。能量总量仍用 monotonic Δt 算好，
          这里只是**归属**（wall 只用来定位午夜边界，不计算时长）；
        - wall 出现回拨/异常（curr_wall <= prev_wall）：全部归 curr 日期（不产生负值）。
        """
        curr_date = local_date(curr_wall)
        if local_date(prev_wall) == curr_date or curr_wall <= prev_wall:
            return {curr_date: energy_wh}
        # 定位 curr_date 当天的本机午夜 00:00:00
        midnight = datetime.strptime(curr_date, "%Y-%m-%d").timestamp()
        span = curr_wall - prev_wall
        if not (prev_wall < midnight <= curr_wall) or span <= 0:
            return {curr_date: energy_wh}
        ratio_prev = (midnight - prev_wall) / span   # 落在前一天的比例
        prev_date = local_date(midnight - 1.0)
        out: dict[str, float] = {curr_date: energy_wh * (1.0 - ratio_prev)}
        if energy_wh * ratio_prev > 1e-9:
            out[prev_date] = energy_wh * ratio_prev
        return out

    def _note_gpu_gap(self, prev_wall: float, curr_wall: float, mono_dt: float) -> None:
        """GPU 采样断档（monotonic 间隔超阈值）：记 data_gaps（source=gpu）。"""
        if self.db is None:
            return
        duration = max(0.0, curr_wall - prev_wall)
        reason = "system_pause_or_sleep" if mono_dt >= SLEEP_HINT_SECONDS else "unknown"
        try:
            self.db.record_gap(
                start_ts=prev_wall, end_ts=curr_wall, source="gpu",
                reason=reason, token_recoverable=True, possible_token_loss=False,
                now=curr_wall,
            )
            logger.info("GPU 采样缺口 %.0fs（%s）已记录", duration, reason)
        except Exception as exc:
            logger.warning("写入 GPU 缺口记录失败: %r", exc)

    def energy_deltas(self, snapshots: list[GpuSnapshot], mono: float) -> dict[str, dict[str, float]]:
        """
        对每个 GPU 计算本轮相对上一轮采样的能量增量（Wh，梯形积分）。

        Phase 11：
        - **Δt 用 monotonic**（`mono` 为本轮 monotonic 秒）：系统时间前跳/后跳不会
          制造巨大或负的能量；wall 只用于把能量**归属**到自然日（精确午夜分割）；
        - 返回 {gpu_uuid: {date: Wh}}（跨午夜时同一段能量分到两天）；
        - 该 GPU 的第一个采样（无 previous）：空 dict；
        - dt <= 0 或 > poll_interval_seconds * 3：unknown gap（睡眠/断线），
          该段不积分 + 记 data_gaps（source=gpu）；
        - 上一轮或本轮 power 缺失（N/A）：该段不积分。
        """
        out: dict[str, dict[str, float]] = {s.uuid: {} for s in snapshots}
        max_gap = self.config.gpu.poll_interval_seconds * 3
        for snap in snapshots:
            prev = self._prev.get(snap.uuid)
            if prev is not None:
                prev_wall, prev_mono, prev_power = prev
                # AUDIT-WIN-002：docstring 承诺"上一轮或本轮 power 缺失（N/A）：该段不积分"
                # ——原代码把 prev_power=None 当 0W 参与梯形积分，系统性低估能耗且无测试。
                # 现在两端 power 都存在才积分（与 token 侧"缺失保持旧 baseline"同语义）。
                if snap.power_draw_w is not None and prev_power is not None:
                    dt = mono - prev_mono   # monotonic 间隔（不受系统时间调整影响）
                    if 0 < dt <= max_gap:
                        e = (prev_power + snap.power_draw_w) / 2.0 * dt / 3600.0
                        out[snap.uuid].update(
                            self._split_energy_across_midnight(prev_wall, snap.timestamp, e)
                        )
                    elif dt > max_gap:
                        self._note_gpu_gap(prev_wall, snap.timestamp, dt)
            self._prev[snap.uuid] = (snap.timestamp, mono, snap.power_draw_w)
        return out

    # ---------- 采样 ----------

    def _filter(self, snapshots: list[GpuSnapshot]) -> list[GpuSnapshot]:
        """device_uuids 非空时只保留指定 GPU；空列表 = 全部。"""
        allowed = self.config.gpu.device_uuids
        if not allowed:
            return snapshots
        return [s for s in snapshots if s.uuid in allowed]

    async def poll_once(self) -> list[GpuSnapshot]:
        """
        执行一轮 GPU 采样。成功返回本轮（过滤后）的快照列表；失败返回 []。
        绝不抛异常。
        """
        if not self.config.gpu.enabled:
            return []
        smi = find_nvidia_smi()
        if smi is None:
            self._set_available(False, "nvidia-smi not found")
            return []
        try:
            code, text = await self.runner([str(smi), *NVSMI_QUERY], self.timeout)
        except Exception as exc:
            self._set_available(False, f"nvidia-smi failed: {exc!r}")
            return []
        if code != 0:
            self._set_available(False, f"nvidia-smi exited with code {code}")
            return []
        all_snaps = parse_nvidia_smi_csv(text, now=self.clock.now())
        if not all_snaps:
            self._set_available(False, "nvidia-smi returned no GPU data")
            return []
        # detected：nvidia-smi 报告的全集（过滤前；Settings 页面用）
        self.detected = [
            {
                "index": s.index,
                "uuid": s.uuid,
                "name": s.name,
                "memory_total_mb": s.memory_total_mb,
            }
            for s in all_snaps
        ]
        snaps = self._filter(all_snaps)
        self.latest = snaps
        self.latest_throttle = {s.uuid: list(s.throttle_reasons) for s in all_snaps}
        # driver_version 是系统级（同一驱动管所有 GPU）：取任一轮解析出的非空值，
        # 保留最后已知值（N/A 的旧驱动不会把已有值抹成 None）。
        dv = next((s.driver_version for s in all_snaps if s.driver_version), None)
        if dv is not None:
            self.driver_version = dv
        # 1.1：slow health 查询（60s 周期）——ECC / 进程 / driver 版本。
        # 独立于 fast query：慢查询失败不影响本轮 fast 快照（能力探测见下）。
        if self._slow_query_due():
            await self._run_slow_health()
        # 把 slow health 合并进本轮快照（ECC 等低频字段）
        for s in snaps:
            self._merge_slow_health(s)
        if snaps:
            # AUDIT-DB-004：写入前先快照能量基线——写失败时回滚（见下方 except）。
            # energy_deltas 会推进 self._prev；若不回滚，失败轮次那一段的能量
            # 下一轮会从新基线起算而永久丢失（与 token 采集器"写失败保持旧 baseline、
            # 下轮重算完整 delta"的语义不一致）。
            prev_energy_state = {s.uuid: self._prev.get(s.uuid) for s in snaps}
            energies = self.energy_deltas(snaps, mono=self.clock.monotonic())
            if self.db is not None:
                try:
                    self.db.save_gpu_samples(
                        snaps,
                        energy_wh=energies,
                        now=snaps[0].timestamp,
                        retention_seconds=self.config.gpu.history_retention_hours * 3600,
                    )
                except Exception as exc:
                    logger.warning("GPU 数据库写入失败，本轮跳过落盘: %r", exc)
                    for s in snaps:
                        old = prev_energy_state.get(s.uuid)
                        if old is None:
                            self._prev.pop(s.uuid, None)
                        else:
                            self._prev[s.uuid] = old
            self.last_update = snaps[0].timestamp
        self._set_available(True, None)
        return snaps

    # ---------- 1.1 slow health 查询（ECC / 进程 / driver，60s 周期） ----------

    def _slow_query_due(self) -> bool:
        mono = self.clock.monotonic()
        if self._last_slow_mono is None:
            return True
        return (mono - self._last_slow_mono) >= SLOW_QUERY_INTERVAL

    def _merge_slow_health(self, snap: GpuSnapshot) -> None:
        """把 slow health 缓存（ECC 等）合并进本轮快照。

        fast query 每轮产生新快照；ECC 等低频字段从 slow 缓存取（缺失保持
        None = 不支持/未查询，UI 整个 ECC 区隐藏——不显示一排 --）。
        """
        health = self.slow_health.get(snap.uuid)
        if health is None:
            return
        snap.ecc_enabled = health.get("ecc_enabled")
        snap.ecc_corrected_volatile = health.get("ecc_corrected_volatile")
        snap.ecc_corrected_aggregate = health.get("ecc_corrected_aggregate")
        snap.ecc_uncorrected_volatile = health.get("ecc_uncorrected_volatile")
        snap.ecc_uncorrected_aggregate = health.get("ecc_uncorrected_aggregate")
        snap.retired_pages = health.get("retired_pages")
        snap.remapped_rows = health.get("remapped_rows")

    async def _run_slow_health(self) -> None:
        """
        一轮 slow health 查询（ECC CSV + 进程）。任何一步失败都保持上一轮缓存
        （ECC 是低频指标：单次查询失败不丢状态，也不影响 fast telemetry）。
        绝不抛异常。
        """
        mono = self.clock.monotonic()
        wall = self.clock.now()
        self._last_slow_mono = mono
        self._last_slow_wall = wall
        smi = find_nvidia_smi()
        if smi is None:
            return
        # 1) ECC CSV 查询（可机器解析的计数）
        try:
            code, text = await self.runner([str(smi), *NVSMI_ECC_QUERY], self.timeout)
            if code == 0:
                for uuid, entry in parse_nvidia_smi_ecc_csv(text).items():
                    enabled = entry.pop("ecc_enabled", None)
                    if not enabled:
                        # 不支持 ECC 的 GPU：整组置 None（UI 隐藏 ECC 区）
                        self.slow_health[uuid] = {"ecc_enabled": None}
                        continue
                    corrected_v = _sum_ecc_pair(entry.get("cv_sram"), entry.get("cv_dram"))
                    corrected_a = _sum_ecc_pair(entry.get("ca_sram"), entry.get("ca_dram"))
                    uncorrected_v = _sum_ecc_pair(entry.get("uv_sram"), entry.get("uv_dram"))
                    uncorrected_a = _sum_ecc_pair(entry.get("ua_sram"), entry.get("ua_dram"))
                    prev = self.slow_health.get(uuid, {})
                    self.slow_health[uuid] = {
                        "ecc_enabled": True,
                        "ecc_corrected_volatile": corrected_v,
                        "ecc_corrected_aggregate": corrected_a,
                        "ecc_uncorrected_volatile": uncorrected_v,
                        "ecc_uncorrected_aggregate": uncorrected_a,
                        "retired_pages": prev.get("retired_pages"),
                        "remapped_rows": prev.get("remapped_rows"),
                    }
        except Exception as exc:
            logger.warning("GPU slow ECC 查询失败（fast telemetry 不受影响）: %r", exc)
        # 2) 进程查询（只读；WDDM 下 used_memory 常 N/A -> None）
        try:
            code, text = await self.runner([str(smi), *NVSMI_PROCS_QUERY], self.timeout)
            if code == 0:
                self.processes = parse_nvidia_smi_processes(text)
        except Exception as exc:
            logger.warning("GPU 进程查询失败（保持上一轮）: %r", exc)

    def _set_available(self, ok: bool, reason: str | None) -> None:
        """更新可用状态；状态翻转才写日志（不刷屏）。"""
        if ok:
            new_state, log_fn, msg = True, logger.info, "GPU monitoring restored"
        else:
            new_state, log_fn, msg = False, logger.warning, f"GPU monitoring became unavailable ({reason})"
        if self._last_available is not None and new_state != self._last_available:
            log_fn(msg)
        self._last_available = new_state
        self.available = new_state
        self.unavailable_reason = None if ok else reason

    async def run(self) -> None:
        """常驻循环：每 poll_interval_seconds 一轮，自身永不退出。"""
        while True:
            try:
                await self.poll_once()
            except Exception as exc:  # 双保险（poll_once 已捕获一切）
                logger.warning("GPU 采集轮错误: %r", exc)
            await asyncio.sleep(self.config.gpu.poll_interval_seconds)


async def _default_runner(args: list[str], timeout: float) -> tuple[int, str]:
    """默认 runner：asyncio.create_subprocess_exec + 超时 kill（不用 shell）。

    creationflags=_NO_WINDOW：Windows 下不弹控制台窗口（nvidia-smi 是 console 程序，
    GPU 轮询每 5s 一次，不隐藏就会闪一下）。"""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=_NO_WINDOW,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        proc.kill()
        try:
            await proc.wait()
        except ProcessLookupError:
            pass
        return -1, ""
    except asyncio.CancelledError:
        # AUDIT-WIN-001：任务被 cancel 时（shutdown 取消 gpu_task、而 nvidia-smi 恰好
        # 挂死）必须 kill 子进程再传播 cancel——否则留一个孤儿 nvidia-smi 进程，
        # 它自己 3s 超时已失效（communicate 的 wait_for 随任务一起被取消）。
        proc.kill()
        try:
            # 完成子进程清理；若清理等待本身又被 cancel，吞掉后照样恢复 cancel 语义
            with contextlib.suppress(asyncio.CancelledError):
                await proc.wait()
        finally:
            raise
    return (proc.returncode if proc.returncode is not None else -1), stdout.decode("utf-8", "replace")
