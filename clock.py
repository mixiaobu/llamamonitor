"""
clock.py — 时间抽象（Phase 11：可靠性 / 数据正确性）

Wall clock 与 Monotonic clock 严格区分：

- Wall clock（now()，Unix 秒）：只用于
  日期（daily bucket / 本地日期）、日志时间、UI 时间戳、
  数据库时间戳列。Windows 时间可能被 NTP / 用户 / 时区修改，
  绝不能用它计算"经过的时长"。
- Monotonic clock（monotonic()）：用于
  采样间隔判定、gap/sleep 检测、uptime、GPU energy Δt、stale detection。
  单调不减，不受系统时间调整影响。

本阶段是"逐步集中时间依赖"的第一步（不过度工程化）：
- 生产代码通过 SystemClock 取时间；
- 测试 / soak 模拟器注入 FakeClock（可 advance / 可模拟系统时间前跳/后跳）；
- 重点集中：collector / database aggregation / daily rollover / GPU energy /
  data quality / soak simulator。

约定：所有需要"当前时刻"的函数都接受 now / mono 参数（调用方从 Clock 取），
而不是在函数内部直接调 time.time() / time.monotonic()。
"""

from __future__ import annotations

import time

__all__ = ["Clock", "SystemClock", "FakeClock", "default_clock"]


class Clock:
    """时间抽象基类：now()（wall，Unix 秒）+ monotonic()。"""

    def now(self) -> float:
        """Wall clock：Unix 秒（float）。用于日期 / 时间戳 / 日志。"""
        raise NotImplementedError

    def monotonic(self) -> float:
        """单调时钟（秒）。用于间隔 / 时长 / gap 检测。"""
        raise NotImplementedError


class SystemClock(Clock):
    """生产时钟：time.time() + time.monotonic()。"""

    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()


class FakeClock(Clock):
    """
    测试时钟：wall 与 monotonic 独立可控。

    - advance(seconds)：wall 与 monotonic 同时前进（模拟真实流逝的时间，
      含跨午夜）；
    - set_wall(unix)：把 wall clock 直接拨到指定值（模拟 NTP / 用户改时间 /
      时区变化）——monotonic 不受影响（这正是 monotonic 的意义）；
    - set_wall_delta / 后跳：set_wall(更早的值) 模拟时间回拨。
    """

    def __init__(self, start_wall: float | None = None, start_mono: float | None = None) -> None:
        self._wall = time.time() if start_wall is None else float(start_wall)
        self._mono = 1000.0 if start_mono is None else float(start_mono)  # 非 0 起点：能看出误用

    # -- 控制 ---------------------------------------------------------------
    def advance(self, seconds: float) -> None:
        """时间流逝：wall 与 monotonic 同时前进 seconds（可为负？否，>=0）。"""
        if seconds < 0:
            raise ValueError("advance(seconds) 必须 >= 0；回拨请用 set_wall")
        self._wall += seconds
        self._mono += seconds

    def set_wall(self, unix: float) -> None:
        """直接设置 wall clock（模拟系统时间跳变；monotonic 不动）。"""
        self._wall = float(unix)

    def set_monotonic(self, value: float) -> None:
        self._mono = float(value)

    # -- Clock 接口 ----------------------------------------------------------
    def now(self) -> float:
        return self._wall

    def monotonic(self) -> float:
        return self._mono


# 进程默认时钟（单例）：未显式注入 Clock 的组件用它。
default_clock = SystemClock()
