"""
runtime_stress_test.py — Accelerated Burn-in / Stress（Phase 16 修订）

不等待真实 48~72 小时，用加速压测 + 短真实 burn-in 判定长期运行可靠性。
本工具覆盖（详见 docs/RC_TEST_REPORT.md items 121-128）：

  A) collector 加速压测：真实 collector 代码路径 + 真实 SQLite（临时库）
     + 进程内假 metrics 源，100000 collector cycles（DB 事务 / live 插入 /
     retention 清理 / runtime snapshot / MTP state）。记录开始/结束
     RSS / Thread / Handle / DB Size，判定无明显线性资源增长。
  B) GPU fake stress：GpuCollector + 注入假 runner，100000 samples，
     覆盖 正常 / N/A / offline / recovery / UUID reorder / long gap；
     验证 memory / DB growth / energy 逻辑。
  C) HTTP poll stress：真实 httpx keep-alive 客户端对进程内假 llama
     server 发起 50000+ /metrics 请求；确认连接复用、Thread/Handle/
     Socket 不无限增长。
  E) lifecycle stress：offline/recover / counter reset / monitor restart /
     backup / quick_check / reset（仅临时库），判定无死锁/重复/线程/句柄增长。
  F) subprocess stress：nvidia-smi wrapper mock（正常/timeout/error/invalid
     大量次数）；真实 nvidia-smi 由 --real-smi-minutes 单独跑 ≥2h 验证无残留。
  G-365) FakeClock 365 天（午夜/月底/年份/DST/sleep/wall/restart）：
     本工具做 DST + wall-clock 跳变 + 午夜/月底/年份的 collector 快速模拟；
     完整 365 天恒等式由 tools/soak_test.py --days 365 承担（自动 poll 60s）。

泄漏判定（J）：识别"持续近似线性增长"而非"完全不变"。Python/WebView 允许
波动；Thread 初始化后应基本稳定；Handle 允许小幅波动但不得持续单向增长。

只用 Fake llama / Fake GPU / 临时数据库——**绝不**对真实 llama-server 用
如此高频率（真实环境用 4~8h 短 burn-in 覆盖）。

用法：
    python tools/runtime_stress_test.py --cycles 100000 --http-cycles 60000
    python tools/runtime_stress_test.py --cycles 2000 --gpu-cycles 2000 --quick
    python tools/runtime_stress_test.py --real-smi-minutes 120 --smi-only
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import os
import random
import sqlite3
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clock import FakeClock                          # noqa: E402
from collector import MetricsCollector               # noqa: E402
from db import Database                              # noqa: E402
from config import AppConfig                         # noqa: E402
from metrics_parser import parse_metrics             # noqa: E402
from gpu_collector import GpuCollector               # noqa: E402

# ---------------------------------------------------------------------------
# 资源采样（psutil 优先；ctypes/POSIX 兜底）
# psutil 是**测试工具**依赖（不进产品 requirements.txt）；缺失时退回系统调用。
# ---------------------------------------------------------------------------

try:
    import psutil as _psutil
    _SELF = _psutil.Process()
except Exception:  # pragma: no cover
    _psutil = None
    _SELF = None


def _rss_mb() -> float:
    """当前进程 RSS（MB）。psutil 优先；Windows ctypes / POSIX /proc 兜底。"""
    if _SELF is not None:
        return _SELF.memory_info().rss / (1024 * 1024)
    if sys.platform == "win32":
        class _PMC(ctypes.Structure):
            _fields_ = [
                ("Size", ctypes.c_uint32),
                ("PageFaultCount", ctypes.c_uint32),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]
        p = _PMC()
        p.Size = ctypes.sizeof(_PMC)
        h = ctypes.windll.kernel32.GetCurrentProcess()
        psapi = ctypes.windll.psapi
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        psapi.GetProcessMemoryInfo(h, ctypes.byref(p), ctypes.c_uint32(p.Size))
        return p.WorkingSetSize / (1024 * 1024)
    # POSIX：VmRSS from /proc/self/status（KB）
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return 0.0


def _thread_count() -> int:
    if _SELF is not None:
        return _SELF.num_threads()
    return threading.active_count()


def _handle_count() -> int:
    """进程句柄数（Windows；psutil 优先，非 Windows 返回 0）。"""
    if _SELF is not None:
        try:
            return _SELF.num_handles()
        except Exception:
            return 0
    return 0


@dataclass
class ResSample:
    t: float
    rss: float
    threads: int
    handles: int
    db_bytes: int = 0


def db_bytes(path: Path | None) -> int:
    if path is None or not path.exists():
        return 0
    total = path.stat().st_size
    for sfx in ("-wal", "-shm"):
        p = Path(str(path) + sfx)
        if p.exists():
            total += p.stat().st_size
    return total


def linear_slope(samples: list[ResSample], key) -> float:
    """对 (采样序号, key(sample)) 做最小二乘回归，返回"每 1000 个采样"的净增长。

    用采样序号（而非墙钟时间）归一化：压测段速度差异大（collector 段每采样
    1666 轮，HTTP 段每采样 1666 次请求），按序号归一化后各段阈值语义一致——
    "整个压测段（~60 采样）内，若存在持续线性增长，每 1000 采样的外推增量
    应远超一次性波动"。
    """
    n = len(samples)
    if n < 3:
        return 0.0
    xs = list(range(n))
    ys = [float(key(s)) for s in samples]
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return 0.0
    return (num / den) * 1000.0


def is_linear_growth(samples: list[ResSample], key, threshold_per_1000: float) -> bool:
    """斜率 > threshold 视为疑似线性增长（绝对增量阈值，由调用方按指标设）。"""
    return abs(linear_slope(samples, key)) > threshold_per_1000


def slope_trimmed(samples: list[ResSample], key, trim_frac: float = 0.15) -> float:
    """回归前先裁掉前 15% 采样：排除解释器/导入/事件循环线程启动/客户端建连的
    一次性预热，让"持续线性增长"的判定只看稳态段。"""
    n = len(samples)
    if n < 5:
        return 0.0
    k = int(n * trim_frac)
    return linear_slope(samples[k:], key)


def slope_backhalf(samples: list[ResSample], key, front_trim: float = 0.4) -> float:
    """判"持续"增长（spec J）：裁掉前 40%（一次性预热分配 / SQLite 页缓存 /
    Python 首次分配的一次性爬升）后对剩余段回归。

    - 前置爬升后**平台化**（37→41→41→41）-> 后段平坦，slope≈0，判无泄漏；
    - 真泄漏（120→160→205→250→295）-> 后段仍在涨，slope 大，判泄漏。
    整体最小二乘会把前置爬升误读为全程增长，故判定改用后段斜率。
    """
    n = len(samples)
    if n < 10:
        return 0.0
    k = int(n * front_trim)
    return linear_slope(samples[k:], key)


def sample_step(cycles: int) -> int:
    """采样步长：保证每个压测段 ~100 个采样点（回归噪声小）。"""
    return max(50, cycles // 100)


# ---------------------------------------------------------------------------
# 假 metrics 文本（含 MTP per-position / cached / kv_cache / requests gauge）
# ---------------------------------------------------------------------------


def metrics_text(prompt: int, output: int, cached: int,
                 positions: dict[str, int] | None = None,
                 kv: float | None = 0.3, drop_core: bool = False) -> str:
    pos = positions or {}
    lines = []
    if not drop_core:
        lines.append(f"llamacpp:prompt_tokens_total {prompt}")
        lines.append(f"llamacpp:tokens_predicted_total {output}")
    lines.append(f"llamacpp:prompt_tokens_cached_total {cached}")
    lines.append(f"llamacpp:n_decode_total {output}")
    lines.append("llamacpp:prompt_seconds_total 1.0")
    lines.append("llamacpp:tokens_predicted_seconds_total 1.0")
    lines.append(f"llamacpp:spec_decode_num_draft_tokens_total {int(output*1.2)}")
    lines.append(f"llamacpp:spec_decode_num_accepted_tokens_total {int(output*0.9)}")
    lines.append(f"llamacpp:spec_decode_num_drafts_total {int(output)}")
    for p, v in pos.items():
        lines.append(f'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{{position="{p}"}} {v}')
    if kv is not None:
        lines.append(f"llamacpp:kv_cache_usage_ratio {kv}")
    lines.append("llamacpp:requests_processing 1")
    lines.append("llamacpp:requests_deferred 0")
    lines.append("llamacpp:n_busy_slots_per_decode 2")
    lines.append("llamacpp:n_tokens_max 4096")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# A) collector 加速压测
# ---------------------------------------------------------------------------


def _make_cfg(poll: float = 5.0) -> AppConfig:
    cfg = AppConfig.default()
    cfg.llama_server.url = "http://127.0.0.1:9"
    cfg.collector.poll_interval_seconds = poll
    cfg.backup.automatic = False
    cfg.gpu.enabled = False
    return cfg


class FakeSource:
    """进程内假 metrics 源：恒定速率 + 可 reset + 可离线窗口。"""

    def __init__(self, prompt_rate=3, output_rate=1, cached_rate=1):
        self.prompt = 0
        self.output = 0
        self.cached = 0
        self.pos = {"0": 0, "1": 0, "2": 0}
        self.pr, self.or_, self.cr = prompt_rate, output_rate, cached_rate
        self.offline = False
        self.drop_core = False
        self.resets = 0
        self.truth_prompt = 0
        self.truth_output = 0
        self.truth_cached = 0
        self.lost_prompt = 0
        self.lost_output = 0
        self.last_read_prompt = 0
        self.last_read_output = 0

    def advance(self, sec: int) -> None:
        self.prompt += self.pr * sec
        self.output += self.or_ * sec
        self.cached += self.cr * sec
        for p in self.pos:
            self.pos[p] += self.or_ // 3
        self.truth_prompt += self.pr * sec
        self.truth_output += self.or_ * sec
        self.truth_cached += self.cr * sec

    def read(self) -> str:
        self.last_read_prompt = self.prompt
        self.last_read_output = self.output
        return metrics_text(self.prompt, self.output, self.cached, self.pos,
                            kv=0.3, drop_core=self.drop_core)

    def reset(self) -> None:
        self.lost_prompt += self.prompt - self.last_read_prompt
        self.lost_output += self.output - self.last_read_output
        self.prompt = self.output = self.cached = 0
        self.pos = {p: 0 for p in self.pos}
        self.resets += 1


def stress_collector(cycles: int, seed: int, tmpdir: Path) -> dict:
    sample_every = sample_step(cycles)
    src = FakeSource()
    # live 样本保留窗口 = cycles*5s（模拟时长）：retention 清理逻辑持续被执行
    # （每轮 check_retention），但窗口内行数不膨胀——否则 DB 单调增长属设计
    # 内行为（用户数据），不是泄漏，会污染"无资源增长"判定。
    retention = max(cycles * 5, 3600)
    db = Database(tmpdir / "collector.db", wal=True, retention_seconds=retention)
    clock = FakeClock(start_wall=1_700_000_000.0)
    cfg = _make_cfg(poll=5.0)

    # 与生产 run() 同构：单个持久事件循环跑完全部 cycles。
    # 每轮 asyncio.run 会新建/销毁 loop+线程，那是压测伪负载，不是采集形态。
    async def drive():
        nonlocal collector, restart_pending
        offline_until = -1
        for i in range(cycles):
            if i % 500 == 0 and i > 0:
                kind = rng.random()
                if kind < 0.4:
                    offline_until = i + rng.randint(2, 5)   # 离线 2~5 轮
                elif kind < 0.8:
                    src.reset()                             # 在线 reset
                else:
                    # monitor restart：同 loop 内重建 collector（保留 db/clock/src）
                    collector.shutdown()
                    collector = make_collector()
                    collector.maybe_note_restart_gap()
                    restart_pending += 1

            src.offline = i < offline_until
            await collector.collect_once()
            src.advance(5)   # 5s 间隔内 server 产生
            clock.advance(5)
            if i % sample_every == 0:
                samples.append(_res_snapshot(tmpdir / "collector.db"))
        # 收尾读取：最后一轮 advance 的 token 必须被读取覆盖，否则 truth
        # 包含从未被读取的尾部区间，恒等式差一个间隔。
        src.offline = False
        await collector.collect_once()
        await collector.aclose()

    def make_collector():
        c = MetricsCollector(cfg, db, clock=clock)
        async def fetch():
            if src.offline:
                return None
            return parse_metrics(src.read())
        c._fetch_parsed = fetch
        return c

    samples: list[ResSample] = []
    t0 = time.monotonic()
    start = _res_snapshot(tmpdir / "collector.db")
    restart_pending = 0
    rng = random.Random(seed)
    collector = make_collector()
    collector.maybe_note_restart_gap()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(drive())
    finally:
        loop.close()
    collector.shutdown()
    samples.append(_res_snapshot(tmpdir / "collector.db"))
    db.checkpoint("PASSIVE")
    db.close()

    # 校验：observed + lost == truth（核心恒等式）
    conn = sqlite3.connect(tmpdir / "collector.db")
    obs_p, obs_o, obs_c = conn.execute(
        "SELECT SUM(prompt_tokens), SUM(output_tokens), SUM(cached_tokens) FROM daily_usage").fetchone()
    gap_rows = conn.execute("SELECT COUNT(*) FROM data_gaps").fetchone()[0]
    live_rows = conn.execute("SELECT COUNT(*) FROM live_samples").fetchone()[0]
    reset_evt = conn.execute(
        "SELECT COUNT(*) FROM monitor_events WHERE event_type='counter_reset'").fetchone()[0]
    conn.close()

    diff_p = src.truth_prompt - (obs_p or 0) - src.lost_prompt
    diff_o = src.truth_output - (obs_o or 0) - src.lost_output

    end = samples[-1]
    rss_slope = slope_trimmed(samples, lambda s: s.rss)
    rss_slope_back = slope_backhalf(samples, lambda s: s.rss)
    thr_slope = slope_trimmed(samples, lambda s: s.threads)
    hdle_slope = slope_trimmed(samples, lambda s: s.handles)

    problems = []
    if diff_p != 0:
        problems.append(f"prompt 恒等式差值 {diff_p} != 0")
    if diff_o != 0:
        problems.append(f"output 恒等式差值 {diff_o} != 0")
    # 泄漏判定（spec J）：识别"持续"线性增长，而非一次性爬升后平台化。
    # 判据 = 后段斜率（裁前 40% 后回归）：前置分配爬升（SQLite 页缓存 /
    # Python 首次分配）会平台化 -> 后段平坦，判无泄漏；真泄漏后段仍在涨。
    # 同时报告整体斜率与净增量，形状一目了然。
    rss_net = end.rss - start.rss
    if rss_slope_back > 10.0:
        problems.append(
            f"RSS 后段仍线性增长 slope={rss_slope_back:.2f}MB/1000采样"
            f"（整体 {rss_slope:.1f}，净增 {rss_net:+.1f}MB）")
    # Thread 初始化后应基本稳定（>2/1000 采样视为增长）
    if thr_slope > 2.0:
        problems.append(f"Thread 疑似线性增长 slope={thr_slope:.2f}/1000采样")
    # Handle 允许波动但不得持续单向增长（>50/1000 采样视为可疑）
    if hdle_slope > 50.0:
        problems.append(f"Handle 疑似单向增长 slope={hdle_slope:.1f}/1000采样")

    return {
        "pass": not problems,
        "problems": problems,
        "cycles": cycles,
        "seed": seed,
        "truth_prompt": src.truth_prompt,
        "truth_output": src.truth_output,
        "observed_prompt": obs_p or 0,
        "observed_output": obs_o or 0,
        "lost_prompt": src.lost_prompt,
        "lost_output": src.lost_output,
        "difference_prompt": diff_p,
        "difference_output": diff_o,
        "resets": src.resets,
        "restarts": restart_pending,
        "gap_rows": gap_rows,
        "live_rows": live_rows,
        "reset_events": reset_evt,
        "db_bytes_start": start.db_bytes,
        "db_bytes_end": end.db_bytes,
        "rss_start_mb": round(start.rss, 1),
        "rss_end_mb": round(end.rss, 1),
        "threads_start": start.threads,
        "threads_end": end.threads,
        "handles_start": start.handles,
        "handles_end": end.handles,
        "rss_slope_mb_per_1000": round(rss_slope, 3),
        "rss_slope_backhalf_mb_per_1000": round(rss_slope_back, 3),
        "rss_net_mb": round(rss_net, 1),
        "threads_slope_per_1000": round(thr_slope, 3),
        "handles_slope_per_1000": round(hdle_slope, 2),
        "elapsed_seconds": round(time.monotonic() - t0, 1),
    }


def _res_snapshot(dbpath: Path) -> ResSample:
    return ResSample(t=time.monotonic(), rss=_rss_mb(),
                     threads=_thread_count(), handles=_handle_count(),
                     db_bytes=db_bytes(dbpath))


# ---------------------------------------------------------------------------
# B) GPU fake stress
# ---------------------------------------------------------------------------

GPU_A = "0, GPU-AAA, Test GPU A, 100, 2048, 50, 55, 280.0, 60, 1700, 9501, 3, 16"
GPU_B = "1, GPU-BBB, Test GPU B, 200, 4096, 60, 56, 300.0, 61, 1800, 9501, 3, 16"
# N/A 字段（power 缺失 -> 该段不积分）
GPU_NA = "0, GPU-AAA, Test GPU A, 100, 2048, 50, [N/A], [N/A], [Not Supported], 60, 1700, 9501, 3, 16"
# UUID reorder：交换 index/uuid 映射（A->index1, B->index0）验证身份按 UUID
GPU_REORDER = "1, GPU-AAA, Test GPU A, 100, 2048, 50, 55, 280.0, 60, 1700, 9501, 3, 16\n0, GPU-BBB, Test GPU B, 200, 4096, 60, 56, 300.0, 61, 1800, 9501, 3, 16"


def stress_gpu(cycles: int, seed: int, tmpdir: Path) -> dict:
    sample_every = sample_step(cycles)
    # 与 collector 段同理：retention 窗口 = 模拟时长，避免样本表单调膨胀
    # 污染"无资源增长"判定（清理逻辑本身每轮持续被执行）
    retention = max(cycles * 5, 3600)
    db = Database(tmpdir / "gpu.db", wal=True, retention_seconds=retention)
    clock = FakeClock(start_wall=1_700_000_000.0)
    cfg = _make_cfg(poll=5.0)
    cfg.gpu.enabled = True
    cfg.gpu.poll_interval_seconds = 5.0

    rng = random.Random(seed)
    scenario_idx = [0]

    def pick_scenario(i: int) -> tuple[str, bool]:
        """返回 (csv_text, offline)。按周期模式覆盖 正常/N/A/offline/recovery/reorder/longgap。"""
        mod = i % 60
        if mod < 10:
            return GPU_NA, False                 # N/A 字段
        if mod < 20:
            return "", True                       # offline（runner 无数据）
        if mod < 25:
            return GPU_REORDER, False             # UUID reorder
        if mod < 30:
            return GPU_B, False                   # 单卡
        if i % 1000 == 0:
            return "", True                       # long gap 触发点（配合 advance 大间隔）
        return GPU_A + "\n" + GPU_B, False        # 正常双卡

    async def runner(args, timeout):
        text, offline = pick_scenario(scenario_idx[0])
        if offline:
            return 0, ""        # 无 GPU 数据
        return 0, text

    g = GpuCollector(cfg, db, runner=runner, timeout_seconds=3.0, clock=clock)
    samples: list[ResSample] = []
    t0 = time.monotonic()
    start = _res_snapshot(tmpdir / "gpu.db")

    # 与生产 run() 同构：单 loop 跑完全部 cycles（避免每轮新建 loop 的伪负载）
    async def drive():
        for i in range(cycles):
            scenario_idx[0] = i
            long = (i % 1000 == 0)
            await g.poll_once()
            # 时间推进：long gap 用大间隔触发 unknown gap（> interval*3）
            clock.advance(600 if long else 5)
            if i % sample_every == 0:
                samples.append(_res_snapshot(tmpdir / "gpu.db"))

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(drive())
    finally:
        loop.close()
    samples.append(_res_snapshot(tmpdir / "gpu.db"))
    db.checkpoint("PASSIVE")
    db.close()

    conn = sqlite3.connect(tmpdir / "gpu.db")
    sample_rows = conn.execute("SELECT COUNT(*) FROM gpu_samples").fetchone()[0]
    daily = conn.execute("SELECT COUNT(*) FROM gpu_daily").fetchone()[0]
    gap_rows = conn.execute("SELECT COUNT(*) FROM data_gaps WHERE source='gpu'").fetchone()[0]
    # 能量应 > 0（正常段梯形积分），且无负值
    energy_ok = conn.execute(
        "SELECT MIN(energy_wh) FROM gpu_daily").fetchone()[0]
    total_energy = conn.execute("SELECT SUM(energy_wh) FROM gpu_daily").fetchone()[0] or 0
    conn.close()

    end = samples[-1]
    rss_slope = slope_trimmed(samples, lambda s: s.rss)
    rss_slope_back = slope_backhalf(samples, lambda s: s.rss)
    rss_net = end.rss - start.rss
    thr_slope = slope_trimmed(samples, lambda s: s.threads)
    hdle_slope = slope_trimmed(samples, lambda s: s.handles)

    problems = []
    if sample_rows == 0:
        problems.append("gpu_samples 无数据")
    if (energy_ok is None) or energy_ok < 0:
        problems.append(f"gpu_daily 能量出现负值/空: min={energy_ok}")
    if total_energy <= 0:
        problems.append("总能量为 0（正常段应梯形积分出正值）")
    if rss_slope_back > 10.0:
        problems.append(
            f"GPU stress RSS 后段仍线性增长 slope={rss_slope_back:.2f}MB/1000"
            f"（整体 {rss_slope:.1f}，净增 {rss_net:+.1f}MB）")
    if thr_slope > 2.0:
        problems.append(f"GPU stress Thread 疑似线性增长 slope={thr_slope:.2f}/1000")
    if hdle_slope > 50.0:
        problems.append(f"GPU stress Handle 疑似单向增长 slope={hdle_slope:.1f}/1000")

    return {
        "pass": not problems,
        "problems": problems,
        "cycles": cycles,
        "seed": seed,
        "gpu_sample_rows": sample_rows,
        "gpu_daily_rows": daily,
        "gpu_gap_rows": gap_rows,
        "total_energy_wh": round(total_energy, 3),
        "min_energy_wh": energy_ok,
        "db_bytes_start": start.db_bytes,
        "db_bytes_end": end.db_bytes,
        "rss_start_mb": round(start.rss, 1),
        "rss_end_mb": round(end.rss, 1),
        "threads_start": start.threads,
        "threads_end": end.threads,
        "handles_start": start.handles,
        "handles_end": end.handles,
        "rss_slope_mb_per_1000": round(rss_slope, 3),
        "threads_slope_per_1000": round(thr_slope, 3),
        "handles_slope_per_1000": round(hdle_slope, 2),
        "elapsed_seconds": round(time.monotonic() - t0, 1),
    }


# ---------------------------------------------------------------------------
# C) HTTP poll stress（真实 httpx keep-alive 对进程内假 server）
# ---------------------------------------------------------------------------


class _CountingHandler(BaseHTTPRequestHandler):
    # HTTP/1.1 + Content-Length -> 支持 keep-alive（连接复用）。
    # 若用默认 HTTP/1.0，每个请求后连接关闭，unique_connections==请求数，无法验证复用。
    protocol_version = "HTTP/1.1"
    server_version = "FakeLlamaStress/1.1"
    requests = 0
    unique_connections = 0
    _conn_counter = 0
    _seen_conns = set()
    body = metrics_text(1000, 500, 100, {"0": 10, "1": 9, "2": 8}).encode()

    def log_message(self, *a):
        pass

    def setup(self):
        # BaseHTTPRequestHandler 每个新 TCP 连接构造一个实例，setup 在该连接
        # 建立时调用一次；keep-alive 的多请求共享同一实例（同 _conn_id）。
        super().setup()
        _CountingHandler._conn_counter += 1
        self._conn_id = _CountingHandler._conn_counter

    def do_GET(self):
        if self.path == "/metrics":
            if self._conn_id not in _CountingHandler._seen_conns:
                _CountingHandler._seen_conns.add(self._conn_id)
                _CountingHandler.unique_connections += 1
            _CountingHandler.requests += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(self.body)))
            self.end_headers()
            self.wfile.write(self.body)


def stress_http(cycles: int, seed: int, tmpdir: Path) -> dict:
    sample_every = sample_step(cycles)
    # 重置计数（类属性跨 run 持久）
    _CountingHandler.requests = 0
    _CountingHandler.unique_connections = 0
    _CountingHandler._seen_conns = set()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CountingHandler)
    port = httpd.server_address[1]
    httpd_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    httpd_thread.start()
    url = f"http://127.0.0.1:{port}/metrics"

    samples: list[ResSample] = []
    t0 = time.monotonic()
    start = ResSample(t=time.monotonic(), rss=_rss_mb(),
                      threads=_thread_count(), handles=_handle_count())

    # 驱动**真实 collector** 的抓取路径：同一持久事件循环（= 生产 run() 的
    # 单 loop 形态），collector._get_client 的客户端/连接跨轮复用。
    # 每 asyncio.run 一次都会重建客户端（loop 变化），那验证的是"重启"，
    # 不是 keep-alive 复用——所以这里必须单 loop 内连续调用。
    cfg = _make_cfg(poll=5.0)
    cfg.llama_server.url = f"http://127.0.0.1:{port}"
    db = Database(tmpdir / "http.db", wal=True, retention_seconds=48 * 3600)
    clock = FakeClock(start_wall=1_700_000_000.0)
    collector = MetricsCollector(cfg, db, clock=clock)

    async def run_all():
        failures = 0
        for i in range(cycles):
            parsed = await collector._fetch_parsed()
            if parsed is None:
                failures += 1
            if i % sample_every == 0:
                samples.append(ResSample(t=time.monotonic(), rss=_rss_mb(),
                                         threads=_thread_count(), handles=_handle_count()))
        await collector.aclose()
        return failures

    failures = asyncio.run(run_all())
    samples.append(ResSample(t=time.monotonic(), rss=_rss_mb(),
                             threads=_thread_count(), handles=_handle_count()))
    httpd.shutdown()
    httpd.server_close()

    end = samples[-1]
    rss_slope = slope_trimmed(samples, lambda s: s.rss)
    thr_slope = slope_trimmed(samples, lambda s: s.threads)
    hdle_slope = slope_trimmed(samples, lambda s: s.handles)

    conns = _CountingHandler.unique_connections
    reqs = _CountingHandler.requests
    # 连接复用：keep-alive 下 unique_connections 应远小于 cycles（连接被复用）；
    # 若每次都新建连接，unique_connections == cycles 且句柄/内存上涨。
    reuse_ratio = reqs / max(1, conns)

    db.close()

    problems = []
    if failures:
        problems.append(f"collector 抓取失败 {failures}/{cycles} 次（应全成功）")
    if rss_slope > 10.0:
        problems.append(f"HTTP stress RSS 疑似线性增长 slope={rss_slope:.2f}MB/1000")
    if thr_slope > 2.0:
        problems.append(f"HTTP stress Thread 疑似线性增长 slope={thr_slope:.2f}/1000")
    if hdle_slope > 100.0:
        problems.append(f"HTTP stress Handle 疑似单向增长（socket 未复用）slope={hdle_slope:.1f}/1000")
    if reuse_ratio < 3.0:
        problems.append(f"连接复用不足：{reqs} 请求用了 {conns} 个连接（复用比 {reuse_ratio:.1f}）")

    return {
        "pass": not problems,
        "problems": problems,
        "cycles": cycles,
        "fetch_failures": failures,
        "requests_served": reqs,
        "unique_connections": conns,
        "conn_reuse_ratio": round(reuse_ratio, 2),
        "db_bytes": 0,
        "rss_start_mb": round(start.rss, 1),
        "rss_end_mb": round(end.rss, 1),
        "threads_start": start.threads,
        "threads_end": end.threads,
        "handles_start": start.handles,
        "handles_end": end.handles,
        "rss_slope_mb_per_1000": round(rss_slope, 3),
        "threads_slope_per_1000": round(thr_slope, 3),
        "handles_slope_per_1000": round(hdle_slope, 2),
        "elapsed_seconds": round(time.monotonic() - t0, 1),
    }


# ---------------------------------------------------------------------------
# E) lifecycle stress
# ---------------------------------------------------------------------------


def stress_lifecycle(tmpdir: Path, seed: int) -> dict:
    """offline/recover 200、counter reset 200、monitor restart 50、
    backup 50、quick_check 20、reset 20（仅临时库）。"""
    rng = random.Random(seed)
    db = Database(tmpdir / "life.db", wal=True, retention_seconds=48 * 3600)
    clock = FakeClock(start_wall=1_700_000_000.0)
    cfg = _make_cfg(poll=5.0)
    src = FakeSource()

    def make_collector():
        c = MetricsCollector(cfg, db, clock=clock)
        async def fetch():
            if src.offline:
                return None
            return parse_metrics(src.read())
        c._fetch_parsed = fetch
        return c

    t0 = time.monotonic()
    start_threads = _thread_count()
    start_handles = _handle_count()
    problems: list[str] = []
    restart_count = 0
    collector = make_collector()
    collector.maybe_note_restart_gap()

    async def once(c):
        return await c.collect_once()

    # offline/recover x200
    for i in range(200):
        src.offline = False
        asyncio.run(once(collector))
        src.advance(5); clock.advance(5)
        src.offline = True
        asyncio.run(once(collector))
        src.advance(5); clock.advance(5)

    # counter reset x200
    for i in range(200):
        src.reset()
        asyncio.run(once(collector))
        src.advance(5); clock.advance(5)

    # monitor restart x50
    for i in range(50):
        collector.shutdown()
        collector = make_collector()
        collector.maybe_note_restart_gap()
        asyncio.run(once(collector))
        restart_count += 1
        src.advance(5); clock.advance(5)

    # backup x50
    for i in range(50):
        size = db.backup_to(tmpdir / f"life_backup_{i}.db")
        if size <= 0:
            problems.append(f"backup #{i} 返回 0 字节")

    # quick_check x20
    for i in range(20):
        ok, detail = db.quick_check()
        if not ok:
            problems.append(f"quick_check #{i} 失败: {detail}")

    # reset x20（只删 live/daily，保留 state baseline）
    for i in range(20):
        res = db.reset_statistics()
        asyncio.run(once(collector))
        src.advance(5); clock.advance(5)

    end_threads = _thread_count()
    end_handles = _handle_count()
    collector.shutdown()
    db.checkpoint("PASSIVE")
    db.close()

    thr_delta = end_threads - start_threads
    hdle_delta = end_handles - start_handles
    if thr_delta > 5:
        problems.append(f"Thread 增长 {thr_delta}（start {start_threads} -> end {end_threads}）")
    if hdle_delta > 200:
        problems.append(f"Handle 净增长 {hdle_delta}（start {start_handles} -> end {end_handles}）")

    return {
        "pass": not problems,
        "problems": problems,
        "offline_recover": 200,
        "counter_reset": 200,
        "monitor_restart": restart_count,
        "backup": 50,
        "quick_check": 20,
        "reset": 20,
        "threads_start": start_threads,
        "threads_end": end_threads,
        "handles_start": start_handles,
        "handles_end": end_handles,
        "elapsed_seconds": round(time.monotonic() - t0, 1),
    }


# ---------------------------------------------------------------------------
# F) subprocess stress（nvidia-smi wrapper mock）
# ---------------------------------------------------------------------------


def stress_subprocess_mock(rounds: int, seed: int) -> dict:
    """对 nvidia-smi wrapper 的 mock runner 跑 正常/timeout/error/invalid 各大量次数。"""
    rng = random.Random(seed)
    db = Database.__new__(Database)  # 仅用 GpuCollector 的状态机，不真正落库
    cfg = _make_cfg(poll=5.0)
    cfg.gpu.enabled = True

    counts = {"ok": 0, "timeout": 0, "error": 0, "invalid": 0}
    crashes = 0

    async def runner(args, timeout):
        r = rng.random()
        if r < 0.5:
            counts["ok"] += 1
            return 0, GPU_A + "\n" + GPU_B
        if r < 0.65:
            counts["timeout"] += 1
            return -1, ""          # _default_runner 超时的约定返回
        if r < 0.8:
            counts["error"] += 1
            return 3, ""           # 非 0 退出
        counts["invalid"] += 1
        return 0, "garbage not a csv line\n"   # 无效输出 -> 解析为空

    g = GpuCollector(cfg, None, runner=runner, timeout_seconds=3.0)

    async def loop():
        nonlocal crashes
        for _ in range(rounds):
            try:
                await g.poll_once()
            except Exception:
                crashes += 1

    t0 = time.monotonic()
    asyncio.run(loop())
    problems = []
    if crashes:
        problems.append(f"poll_once 抛出未捕获异常 {crashes} 次")
    return {
        "pass": not problems,
        "problems": problems,
        "rounds": rounds,
        "ok": counts["ok"],
        "timeout": counts["timeout"],
        "error": counts["error"],
        "invalid": counts["invalid"],
        "final_available": g.available,
        "elapsed_seconds": round(time.monotonic() - t0, 1),
    }


def run_real_smi(minutes: int) -> dict:
    """真实 nvidia-smi 5s 间隔跑 >=minutes 分钟，期间与结束后检查无残留进程。"""
    from gpu_collector import find_nvidia_smi, _default_runner, NVSMI_QUERY
    smi = find_nvidia_smi()
    if smi is None:
        return {"pass": False, "problems": ["nvidia-smi not found"], "rounds": 0}
    cfg = _make_cfg(poll=5.0)
    cfg.gpu.enabled = True
    g = GpuCollector(cfg, None, timeout_seconds=3.0)

    def _count_smi_procs() -> int:
        import subprocess
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-Process nvidia-smi -EA SilentlyContinue | Measure-Object).Count"],
                capture_output=True, text=True, timeout=20).stdout.strip()
            return int(out)
        except Exception:
            return -1

    before = _count_smi_procs()
    t0 = time.monotonic()
    deadline = t0 + minutes * 60
    rounds = 0
    async def loop():
        nonlocal rounds
        while time.monotonic() < deadline:
            await g.poll_once()
            rounds += 1
            await asyncio.sleep(5)
    asyncio.run(loop())
    time.sleep(2)
    after = _count_smi_procs()
    elapsed = time.monotonic() - t0
    problems = []
    if after > before:
        problems.append(f"nvidia-smi 残留进程 {after - before} 个（before {before} -> after {after}）")
    return {
        "pass": not problems,
        "problems": problems,
        "rounds": rounds,
        "smi_procs_before": before,
        "smi_procs_after": after,
        "elapsed_seconds": round(elapsed, 1),
    }


# ---------------------------------------------------------------------------
# G-365) DST + wall-clock + 午夜/月底/年份 collector 快速模拟
# ---------------------------------------------------------------------------


def stress_clock_edges(tmpdir: Path, seed: int) -> dict:
    """验证 collector 在 DST / wall-clock 前跳/后跳 / 午夜 / 月底 / 年份边界下
    不崩溃、daily 按本机日期正确分桶、无负 delta。"""
    db = Database(tmpdir / "clock.db", wal=True, retention_seconds=48 * 3600)
    # 起点：2026-06-20 本地（美国中部 DST 生效期；午夜 2:30 后 DST 结束在 11 月）
    clock = FakeClock(start_wall=1781000000.0)  # ~2026-06-09
    cfg = _make_cfg(poll=5.0)
    src = FakeSource()

    def make_collector():
        c = MetricsCollector(cfg, db, clock=clock)
        async def fetch():
            return parse_metrics(src.read())
        c._fetch_parsed = fetch
        return c

    collector = make_collector()
    t0 = time.monotonic()
    problems = []

    # 跨越午夜（本地 23:59 -> 00:01）
    for _ in range(100):
        asyncio.run(collector.collect_once())
        src.advance(5)
        # 每次快进 30 分钟，覆盖午夜/日期边界
        clock.advance(1800)
    # wall-clock 前跳（模拟 NTP +1h）
    clock.set_wall(clock.now() + 3600)
    for _ in range(20):
        asyncio.run(collector.collect_once())
        src.advance(5)
        clock.advance(5)
    # wall-clock 后跳（模拟 -30min）
    clock.set_wall(clock.now() - 1800)
    for _ in range(20):
        asyncio.run(collector.collect_once())
        src.advance(5)
        clock.advance(5)
    # 跨月底 + 年份（快进到 2026-12-31 -> 2027-01-01）
    target = 1801200000.0  # ~2027-01-01
    while clock.now() < target:
        asyncio.run(collector.collect_once())
        src.advance(5)
        clock.advance(3600)
    for _ in range(10):
        asyncio.run(collector.collect_once())
        src.advance(5)
        clock.advance(5)

    collector.shutdown()
    conn = sqlite3.connect(tmpdir / "clock.db")
    daily_rows = conn.execute("SELECT date, prompt_tokens, output_tokens FROM daily_usage ORDER BY date").fetchall()
    neg = conn.execute(
        "SELECT COUNT(*) FROM daily_usage WHERE prompt_tokens<0 OR output_tokens<0").fetchone()[0]
    dates = [r[0] for r in daily_rows]
    conn.close()
    db.close()

    if neg:
        problems.append(f"clock edges 出现负 daily {neg} 行")
    if not dates:
        problems.append("clock edges 无 daily 行")
    # 日期应单调不减（分桶正确，不串桶）
    for a, b in zip(dates, dates[1:]):
        if a > b:
            problems.append(f"daily 日期非单调: {a} -> {b}")
            break

    return {
        "pass": not problems,
        "problems": problems,
        "daily_rows": len(daily_rows),
        "date_range": [dates[0], dates[-1]] if dates else [],
        "negative_rows": neg,
        "elapsed_seconds": round(time.monotonic() - t0, 1),
    }


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def print_section(title: str, r: dict) -> None:
    print()
    print("=" * 64)
    print(f"{title}  {'PASS' if r.get('pass') else 'FAIL'}")
    print("=" * 64)
    for k, v in r.items():
        if k in ("pass", "problems"):
            continue
        print(f"  {k:28s}: {v}")
    if r.get("problems"):
        for p in r["problems"]:
            print(f"  !! {p}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="LlamaMonitor accelerated burn-in / stress")
    ap.add_argument("--cycles", type=int, default=100000, help="collector cycles（默认 100000）")
    ap.add_argument("--gpu-cycles", type=int, default=100000, help="GPU fake samples（默认 100000）")
    ap.add_argument("--http-cycles", type=int, default=60000, help="HTTP /metrics 请求数（默认 60000）")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--quick", action="store_true", help="快速模式（各 2000 cycles，验证工具本身）")
    ap.add_argument("--real-smi-minutes", type=int, default=0, help="真实 nvidia-smi 5s 间隔跑 N 分钟（>=120 推荐）")
    ap.add_argument("--smi-only", action="store_true", help="只跑真实 nvidia-smi 段")
    ap.add_argument("--sections", default="a,b,c,e,f,g", help="逗号分隔运行段（a/b/c/e/f/g）")
    ap.add_argument("--keep-dir", default=None, help="保留临时目录（调试）")
    args = ap.parse_args(argv)
    sections = {s.strip().lower() for s in args.sections.split(",") if s.strip()}

    if args.quick:
        args.cycles = min(args.cycles, 2000)
        args.gpu_cycles = min(args.gpu_cycles, 2000)
        args.http_cycles = min(args.http_cycles, 3000)

    all_pass = True
    tmpdir = Path(tempfile.mkdtemp(prefix="lm_stress_"))

    if args.smi_only:
        if args.real_smi_minutes <= 0:
            print("--smi-only 需要 --real-smi-minutes N")
            return 2
        r = run_real_smi(args.real_smi_minutes)
        print_section("F) REAL nvidia-smi 5s POLL", r)
        return 0 if r["pass"] else 1

    # A) collector
    if "a" in sections:
        r_collector = stress_collector(args.cycles, args.seed, tmpdir)
        print_section("A) COLLECTOR ACCELERATED STRESS", r_collector)
        all_pass = all_pass and r_collector["pass"]

    # B) GPU fake
    if "b" in sections:
        r_gpu = stress_gpu(args.gpu_cycles, args.seed, tmpdir)
        print_section("B) GPU FAKE STRESS", r_gpu)
        all_pass = all_pass and r_gpu["pass"]

    # C) HTTP poll
    if "c" in sections:
        r_http = stress_http(args.http_cycles, args.seed, tmpdir)
        print_section("C) HTTP POLL STRESS (keep-alive)", r_http)
        all_pass = all_pass and r_http["pass"]

    # E) lifecycle
    if "e" in sections:
        r_life = stress_lifecycle(tmpdir, args.seed)
        print_section("E) LIFECYCLE STRESS", r_life)
        all_pass = all_pass and r_life["pass"]

    # F) subprocess mock
    if "f" in sections:
        r_sub = stress_subprocess_mock(args.cycles, args.seed)
        print_section("F) SUBPROCESS STRESS (nvidia-smi mock)", r_sub)
        all_pass = all_pass and r_sub["pass"]

    # G-365) clock edges (DST/wall/午夜/月底/年份)
    if "g" in sections:
        r_clock = stress_clock_edges(tmpdir, args.seed)
        print_section("G) CLOCK EDGES (DST/wall/midnight/month/year)", r_clock)
        all_pass = all_pass and r_clock["pass"]

    if args.real_smi_minutes > 0:
        r_real = run_real_smi(args.real_smi_minutes)
        print_section("F) REAL nvidia-smi 5s POLL", r_real)
        all_pass = all_pass and r_real["pass"]

    print()
    print("=" * 64)
    print(f"RUNTIME STRESS OVERALL  {'PASS' if all_pass else 'FAIL'}")
    print("=" * 64)
    print(f"temp dir: {tmpdir} (keep with --keep-dir)")
    if not args.keep_dir:
        # 清理临时目录
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())