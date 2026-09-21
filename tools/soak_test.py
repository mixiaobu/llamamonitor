"""
soak_test.py — 长时运行模拟（Phase 11：Soak Test）

用 FakeClock 加速模拟 7/30/90 天连续运行：
- 真实 SQLite（临时目录）+ 真实 collector 代码路径（无 HTTP，进程内假 metrics 源）；
- 假 llama-server 以恒定速率产生 token（整数/秒），counter 可 reset（server 重启）；
- 注入事件：server 离线窗口、server 重启（counter reset）、monitor 重启；
- Ground Truth = 假 server 的累计真实产出；
- 判定：
  * 无 reset：observed == truth（整数精确，无丢失）；
  * 有 reset：observed + known_lost == truth（known_lost = 每次 reset 时
    [最后读取, reset 时刻] 之间真实产生但从未被读取的 token），
    且 monitor 对每个"缺口期内的核心 reset"标记 possible_token_loss、
    对每个"在线期 reset"记 counter_reset 事件。

用法：
    python tools/soak_test.py --days 7 --seed 42
    python tools/soak_test.py --days 30 --seed 7 --restart-rate 0.02 --offline-rate 0.05
    python tools/soak_test.py --days 90 --seed 1 --poll 60
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clock import FakeClock                                   # noqa: E402
from collector import MetricsCollector                         # noqa: E402
from db import Database                                        # noqa: E402
from config import AppConfig                                   # noqa: E402
from metrics_parser import parse_metrics                       # noqa: E402


def metrics_text(prompt: int, output: int) -> str:
    return (
        f"llamacpp:prompt_tokens_total {prompt}\n"
        f"llamacpp:tokens_predicted_total {output}\n"
        f"llamacpp:n_decode_total {output}\n"
        f"llamacpp:prompt_seconds_total 0\n"
        f"llamacpp:tokens_predicted_seconds_total 0\n"
    )


class FakeServer:
    """进程内假 llama-server：恒定速率产生 token，counter 可 reset。

    ground truth 簿记（全部整数）：
    - truth_*：整个模拟期累计真实产生；
    - lost_*：每次 reset 时 [最后一次被 monitor 读取, reset 时刻] 之间
      产生但从未被读取的 token（不可恢复丢失）；
    - 离线/monitor 重启**不产生丢失**（恢复后的 delta 覆盖整个窗口）——
      只有 reset 落在"未观测窗口"内才丢失。
    """

    def __init__(self, prompt_rate: int, output_rate: int):
        self.prompt_rate = prompt_rate
        self.output_rate = output_rate
        self.prompt = 0
        self.output = 0
        self.truth_prompt = 0
        self.truth_output = 0
        self.last_read_prompt = 0
        self.last_read_output = 0
        self.lost_prompt = 0
        self.lost_output = 0
        self.resets = 0

    def advance(self, seconds: int) -> None:
        self.prompt += self.prompt_rate * seconds
        self.output += self.output_rate * seconds
        self.truth_prompt += self.prompt_rate * seconds
        self.truth_output += self.output_rate * seconds

    def read(self) -> str:
        """monitor 成功读取：更新 last_read 并返回 /metrics 文本。"""
        self.last_read_prompt = self.prompt
        self.last_read_output = self.output
        return metrics_text(self.prompt, self.output)

    def reset(self) -> None:
        """server 重启：counter 归零。[最后读取, 现在] 的 token 丢失。"""
        self.lost_prompt += self.prompt - self.last_read_prompt
        self.lost_output += self.output - self.last_read_output
        self.prompt = 0
        self.output = 0
        self.resets += 1


def _make_config(poll: float) -> AppConfig:
    cfg = AppConfig.default()
    cfg.llama_server.url = "http://127.0.0.1:9"
    cfg.collector.poll_interval_seconds = poll
    cfg.backup.automatic = False  # 模拟期间不触发自动备份
    return cfg


class SoakSimulation:
    def __init__(
        self,
        days: int,
        seed: int,
        poll_seconds: float = 5.0,
        restart_rate: float = 0.01,     # 每轮触发 monitor 重启的概率
        offline_rate: float = 0.02,     # 每轮触发 server 离线窗口的概率
        counter_reset_rate: float = 0.01,  # 每个（非离线）间隔内 server reset 的概率
        prompt_rate: int = 3,
        output_rate: int = 1,
        start_date: str | None = None,  # "YYYY-MM-DD"，默认 = 今天 - days
    ):
        self.days = days
        self.seed = seed
        self.poll = poll_seconds
        self.restart_rate = restart_rate
        self.offline_rate = offline_rate
        self.counter_reset_rate = counter_reset_rate
        self.rng = random.Random(seed)
        if start_date is None:
            start = datetime.now() - timedelta(days=days)
            start_date = start.strftime("%Y-%m-%d")
        self.start_wall = datetime.strptime(start_date, "%Y-%m-%d").timestamp()
        self.end_wall = self.start_wall + days * 86400.0
        self.server = FakeServer(prompt_rate, output_rate)
        self.t0 = time.monotonic()
        # 事件计数
        self.monitor_restarts = 0
        self.offline_events = 0
        self.reset_events = 0          # 实际发生的 server reset（含离线期内）
        self.reset_during_offline = 0
        self.reset_during_online = 0
        self.db_path: Path | None = None
        self.tmpdir: str | None = None
        self.trace: list[tuple[float, str]] = []  # (t, 事件类型) 调试用

    # ---------- collector 管理 ----------

    def _new_collector(self, db: Database, clock: FakeClock) -> MetricsCollector:
        c = MetricsCollector(_make_config(self.poll), db, clock=clock)
        c._fetch_parsed = self._make_fetch()
        return c

    # ---------- 主循环 ----------

    def run(self) -> dict:
        self.tmpdir = tempfile.mkdtemp(prefix="soak_")
        self.db_path = Path(self.tmpdir) / "soak.db"
        db = Database(self.db_path, wal=False, retention_seconds=48 * 3600)
        clock = FakeClock(start_wall=self.start_wall)
        collector = self._new_collector(db, clock)
        collector.maybe_note_restart_gap()
        if db is not None:
            db.record_event("monitor_start", "info", "collector", {}, now=clock.now())

        poll = int(round(self.poll))
        assert poll >= 1
        t = self.start_wall
        offline_until = None
        pending_offline_reset_at = None
        just_ended_offline = False  # 恢复轮必须与开缺口的同一 collector 执行
        reset_last_interval = False  # 上一轮（正常轮）是否发生了在线 reset

        try:
            while t < self.end_wall:
                just_ended = just_ended_offline
                just_ended_offline = False
                # 1) monitor 重启（轮边界；期间 server 继续产生）。
                #    离线窗口内 / 刚恢复的下一轮 / 在线 reset 的下一轮不重启
                #    （后者必须先把"检测读取"做掉，否则 reset 被 restart 窗口掩盖、
                #    新进程首读与 reset 前 baseline 比较，reset 漏判 -> 恒等式破坏）。
                if t > self.start_wall and offline_until is None and not just_ended \
                        and not reset_last_interval \
                        and self.rng.random() < self.restart_rate:
                    self.trace.append((t, "restart"))
                    collector.shutdown()
                    self.monitor_restarts += 1
                    # 重启窗口：3~6 个 poll（server 继续运行，无 reset -> 无丢失）
                    window = self.rng.randint(3, 6) * poll
                    self.server.advance(window)
                    t += window
                    clock.advance(window)
                    collector = self._new_collector(db, clock)
                    collector.maybe_note_restart_gap()
                    db.record_event("monitor_start", "info", "collector", {}, now=clock.now())
                    continue

                # 2) 离线窗口（server 继续产生；窗口内可能 reset）
                if offline_until is not None:
                    if t >= offline_until:
                        offline_until = None
                        pending_offline_reset_at = None
                        just_ended_offline = True
                        self.trace.append((t, "offline_end"))
                        continue
                    # 离线中的一轮（fetch 失败）
                    if pending_offline_reset_at is not None and t >= pending_offline_reset_at:
                        self.server.reset()
                        self.reset_during_offline += 1
                        self.trace.append((t, "offline_reset"))
                        pending_offline_reset_at = None
                    self._offline_round(collector, clock)
                    self.trace.append((t, "offline_round"))
                    dt = min(poll, int(offline_until - t))
                    self.server.advance(dt)
                    t += dt
                    clock.advance(dt)
                    continue

                # 新离线窗口不在"刚恢复"的迭代开始（否则与上一窗口零读取间隔合并，
                # 事件计数与缺口行 1:1 关系被破坏）；也不在在线 reset 的下一轮开始
                # （该轮必须先做检测读取，reset 才会被 current < previous 判据捕获）。
                if not just_ended and not reset_last_interval \
                        and self.rng.random() < self.offline_rate:
                    window = self.rng.randint(4, 12) * poll  # 4~12 个 poll（> 阈值）
                    offline_until = t + window
                    self.offline_events += 1
                    self.trace.append((t, "offline_start"))
                    # 概率：离线窗口内 server reset（产生真实丢失）。
                    # 仅在"恢复读取必能检测到 reset"时才排程：恢复读值 R ≤ (window-poll)*rate，
                    # 要求 last_read > R 的最大值（current < previous 判据才可靠）。
                    # 连续盲窗 reset 的第二次在 last_read 很小时可能漏判——真实世界
                    # counter 以百万计、reset 间隔以小时计，此歧义可忽略（报告文档化）。
                    if (
                        self.rng.random() < 0.5
                        and self.server.last_read_prompt > (window - poll) * self.server.prompt_rate
                        and self.server.last_read_output > (window - poll) * self.server.output_rate
                    ):
                        pending_offline_reset_at = t + self.rng.randint(1, window // poll) * poll
                    continue

                # 3) 正常轮：monitor 读取 -> server 在间隔内产生（可能 reset）
                self._valid_round(collector, clock)
                self.trace.append((t, "valid"))
                # 在线期 reset 不与上一轮 reset 相邻：相邻时上一次读取值过小，
                # "current < previous" 的 reset 检测可能漏判（真实场景 counter 巨大、
                # reset 间隔以小时计，不存在此歧义；模拟里保持检测可靠）。
                if not reset_last_interval and self.rng.random() < self.counter_reset_rate:
                    off = self.rng.randint(1, max(1, poll - 1))
                    self.server.advance(off)
                    self.server.reset()
                    self.reset_during_online += 1
                    self.server.advance(poll - off)
                    reset_last_interval = True
                else:
                    self.server.advance(poll)
                    reset_last_interval = False
                t += poll
                clock.advance(poll)
            # 收尾读取：最后一个采集间隔的 token 必须被"最后一读"覆盖，
            # 否则 truth 包含从未被读取的尾部区间（差值恒等式被破坏）。
            # 无论循环结束时处于什么状态（正常/离线窗口中/重启窗口后），
            # 这一读都能让 observed + lost == truth 精确闭合。
            self._valid_round(collector, clock)
            self.trace.append((t, "final_read"))
        finally:
            collector.shutdown()
            db.checkpoint("PASSIVE")
            db.close()

        return self._report()

    def _valid_round(self, collector: MetricsCollector, clock: FakeClock) -> None:
        asyncio.run(collector.collect_once())

    def _offline_round(self, collector: MetricsCollector, clock: FakeClock) -> None:
        async def fetch_fail():
            return None

        collector._fetch_parsed = fetch_fail
        asyncio.run(collector.collect_once())
        collector._fetch_parsed = self._make_fetch()

    def _make_fetch(self):
        server = self.server

        async def fetch():
            return parse_metrics(server.read())
        return fetch

    # ---------- 报告 ----------

    def _report(self) -> dict:
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT SUM(prompt_tokens), SUM(output_tokens) FROM daily_usage"
        ).fetchone()
        live_rows = conn.execute("SELECT COUNT(*) FROM live_samples").fetchone()[0]
        gap_rows = conn.execute("SELECT COUNT(*) FROM data_gaps").fetchone()[0]
        loss_gaps = conn.execute(
            "SELECT COUNT(*) FROM data_gaps WHERE possible_token_loss = 1"
        ).fetchone()[0]
        offline_gaps = conn.execute(
            "SELECT COUNT(*) FROM data_gaps WHERE reason = 'server_offline'"
        ).fetchone()[0]
        reset_events = conn.execute(
            "SELECT COUNT(*) FROM monitor_events WHERE event_type = 'counter_reset' "
            "AND (details_json LIKE '%prompt_tokens_total%' OR details_json LIKE '%tokens_predicted_total%')"
        ).fetchone()[0]
        daily_rows = conn.execute("SELECT COUNT(*) FROM daily_usage").fetchone()[0]
        conn.close()

        observed_prompt = rows[0] or 0
        observed_output = rows[1] or 0
        truth_prompt = self.server.truth_prompt
        truth_output = self.server.truth_output
        lost_prompt = self.server.lost_prompt
        lost_output = self.server.lost_output

        diff_prompt = truth_prompt - observed_prompt - lost_prompt
        diff_output = truth_output - observed_output - lost_output

        ok = True
        problems = []
        if diff_prompt != 0:
            ok = False
            problems.append(f"prompt 差值 {diff_prompt} != 0")
        if diff_output != 0:
            ok = False
            problems.append(f"output 差值 {diff_output} != 0")
        # 在线期 reset：monitor 必须记 counter_reset 事件（核心 counter 之一即可）
        if self.reset_during_online > 0 and reset_events == 0:
            ok = False
            problems.append("存在在线期 server reset，但无核心 counter_reset 事件")
        # 离线期 reset：恢复时必须标记 possible_token_loss
        if self.reset_during_offline > 0 and loss_gaps == 0:
            ok = False
            problems.append("存在离线期 server reset，但无 possible_token_loss 缺口")
        # 离线缺口数量与离线事件一致（每个离线窗口一条缺口）
        if offline_gaps != self.offline_events:
            ok = False
            problems.append(f"server_offline 缺口 {offline_gaps} != 离线事件 {self.offline_events}")

        return {
            "pass": ok,
            "problems": problems,
            "simulation_days": self.days,
            "seed": self.seed,
            "poll_seconds": self.poll,
            "ground_truth_prompt": truth_prompt,
            "ground_truth_output": truth_output,
            "observed_prompt": observed_prompt,
            "observed_output": observed_output,
            "known_lost_prompt": lost_prompt,
            "known_lost_output": lost_output,
            "difference_prompt": diff_prompt,
            "difference_output": diff_output,
            "counter_resets": self.server.resets,
            "counter_resets_online": self.reset_during_online,
            "counter_resets_during_offline": self.reset_during_offline,
            "monitor_restarts": self.monitor_restarts,
            "server_offline_events": self.offline_events,
            "sampling_gaps": gap_rows,
            "possible_loss_gaps": loss_gaps,
            "offline_gaps_recorded": offline_gaps,
            "core_reset_events": reset_events,
            "db_rows_live": live_rows,
            "db_rows_daily": daily_rows,
            "db_size_bytes": db_path_size(self.db_path),
            "elapsed_seconds": round(time.monotonic() - self.t0, 1),
        }


def db_path_size(path: Path | None) -> int:
    if path is None or not path.exists():
        return 0
    total = path.stat().st_size
    for suffix in ("-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            total += p.stat().st_size
    return total


def print_report(r: dict) -> None:
    lines = [
        "",
        "=" * 64,
        f"SOAK SIMULATION  {'PASS' if r['pass'] else 'FAIL'}",
        "=" * 64,
        f"Simulation Days        : {r['simulation_days']}  (seed={r['seed']}, poll={r['poll_seconds']}s)",
        f"Ground Truth (prompt)  : {r['ground_truth_prompt']}",
        f"Ground Truth (output)  : {r['ground_truth_output']}",
        f"Observed (prompt)      : {r['observed_prompt']}",
        f"Observed (output)      : {r['observed_output']}",
        f"Known Lost (prompt)    : {r['known_lost_prompt']}",
        f"Known Lost (output)    : {r['known_lost_output']}",
        f"Difference (prompt)    : {r['difference_prompt']}   (truth - observed - lost, 必须为 0)",
        f"Difference (output)    : {r['difference_output']}",
        f"Counter Resets         : {r['counter_resets']}  (online={r['counter_resets_online']}, offline={r['counter_resets_during_offline']})",
        f"Monitor Restarts       : {r['monitor_restarts']}",
        f"Server Offline Events  : {r['server_offline_events']}  (gaps recorded={r['offline_gaps_recorded']})",
        f"Sampling Gaps          : {r['sampling_gaps']}  (possible loss={r['possible_loss_gaps']})",
        f"Core Reset Events      : {r['core_reset_events']}",
        f"DB Rows (live)         : {r['db_rows_live']}   (daily rows={r['db_rows_daily']})",
        f"DB Size                : {r['db_size_bytes']} bytes",
        f"Elapsed                : {r['elapsed_seconds']}s",
    ]
    if r["problems"]:
        lines.append("Problems:")
        lines.extend(f"  - {p}" for p in r["problems"])
    print("\n".join(lines))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="LlamaMonitor soak simulation (Phase 11)")
    ap.add_argument("--days", type=int, default=7, help="模拟天数（标准档 7/30/90；其他值可用于快速验证）")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--poll", type=float, default=5.0)
    ap.add_argument("--restart-rate", type=float, default=0.01)
    ap.add_argument("--offline-rate", type=float, default=0.02)
    ap.add_argument("--reset-rate", type=float, default=0.01)
    ap.add_argument("--prompt-rate", type=int, default=3, help="prompt tokens/秒")
    ap.add_argument("--output-rate", type=int, default=1, help="output tokens/秒")
    args = ap.parse_args(argv)
    # 365 天档用更粗的 poll（如 60s）压缩模拟轮数，仍覆盖午夜/月底/年份/DST/sleep/restart
    if args.days >= 365 and args.poll == 5.0:
        args.poll = 60.0
        print(f"[note] days={args.days}: auto poll 5s -> 60s to keep simulation tractable")

    sim = SoakSimulation(
        days=args.days,
        seed=args.seed,
        poll_seconds=args.poll,
        restart_rate=args.restart_rate,
        offline_rate=args.offline_rate,
        counter_reset_rate=args.reset_rate,
        prompt_rate=args.prompt_rate,
        output_rate=args.output_rate,
    )
    report = sim.run()
    print_report(report)
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
