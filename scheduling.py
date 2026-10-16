"""
scheduling.py — 基于 monotonic deadline 的周期调度（1.3.3 实时链路重构）

背景（1.3.3 §11）：旧采集循环是 `await sleep(interval); await collect()`，
实际周期 = interval + 单轮耗时——interval=1s 且单轮 0.8s 时实际变成 1.8s，
时间漂移随轮次累积。

本模块提供 DeadlineScheduler：
- deadline 锚定：下一轮执行时刻 = 上一 deadline + interval（不叠加单轮耗时）；
  单轮 0.1s（interval 1s）-> 下一轮约 0.9s 后执行；
- 不重叠：work 在循环内 await，绝不并发启动第二轮（single-flight）；
- 不追赶：单轮超期（work 耗时 > interval）后下一轮立即进入（背靠背），
  落后超过一个完整周期时 deadline 重新同步——禁止无限追赶；
- 超期检测：记录每轮耗时（last_duration_s）与是否超期（last_overrun），
  超期仅在首次/周期性记日志（不逐轮刷屏）；
- CancelledError 正确传播（shutdown 取消任务时干净退出）；
- 单轮异常只记日志，循环不退出。

所有时间来自 Clock（monotonic）——测试注入 FakeClock 可确定性验证。
"""

from __future__ import annotations

import asyncio
import logging

from clock import Clock, default_clock

logger = logging.getLogger("llamamonitor.scheduling")

# 超期日志节流：首次必记，之后每 N 次超期记一条（防长超期刷屏）
_OVERRUN_LOG_EVERY = 30


class DeadlineScheduler:
    """
    monotonic deadline 周期调度器。

    用法：
        sched = DeadlineScheduler(interval=1.0, clock=clock, name="hot")
        await sched.run(work)      # 常驻循环（CancelledError 传播退出）

    work：async callable（无参）。首轮在一个 interval 后执行（与旧
    "先睡后采" 循环的启动语义一致——lifespan 已在启动时做过首轮采集）。
    """

    def __init__(
        self,
        interval: float,
        clock: Clock | None = None,
        name: str = "cycle",
    ) -> None:
        if interval <= 0:
            raise ValueError("interval 必须 > 0")
        self.interval = float(interval)
        self.clock = clock or default_clock
        self.name = name
        # 诊断字段（1.3.3 §31：/api/realtime diagnostics 直接读取）
        self.last_duration_s: float = 0.0
        self.last_overrun: bool = False
        self.overrun_count: int = 0
        self.round_count: int = 0

    @property
    def interval_ms(self) -> int:
        return int(round(self.interval * 1000))

    def _mono(self) -> float:
        return self.clock.monotonic()

    async def run(self, work) -> None:
        """
        常驻循环：按 deadline 执行 work。

        - work 抛非 Cancel 异常：记日志继续（单轮故障不杀循环）；
        - work 耗时 > interval：下一轮立即进入（sleep 0），不并发；
        - 落后 > 1 个完整周期：deadline 重新同步到 now（不无限追赶）；
        - task 被 cancel：CancelledError 从 sleep 或 work 传播，循环退出。
        """
        deadline = self._mono() + self.interval  # 首轮一个周期后
        while True:
            sleep_s = deadline - self._mono()
            if sleep_s > 0:
                await asyncio.sleep(sleep_s)
            # （sleep_s <= 0：已到/已过 deadline——立即执行本轮）
            t0 = self._mono()
            try:
                await work()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("周期 %s 单轮异常（已恢复，继续下一轮）: %r",
                                  self.name, exc)
            elapsed = self._mono() - t0
            self.last_duration_s = elapsed
            self.round_count += 1
            self.last_overrun = elapsed > self.interval
            if self.last_overrun:
                self.overrun_count += 1
                if self.overrun_count == 1 or self.overrun_count % _OVERRUN_LOG_EVERY == 0:
                    logger.warning(
                        "周期 %s 超期：单轮耗时 %.3fs > 目标 %.1fs（第 %d 次超期）",
                        self.name, elapsed, self.interval, self.overrun_count,
                    )
            # deadline 前移一个周期（锚定旧 deadline，不叠加本轮耗时）
            deadline += self.interval
            # 落后超过一个完整周期 -> 重新同步（禁止无限追赶）
            if self._mono() > deadline + self.interval:
                deadline = self._mono()
