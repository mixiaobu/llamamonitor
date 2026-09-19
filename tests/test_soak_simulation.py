"""
Phase 11 测试：Soak 模拟 —— 用 tools/soak_test.py 的模拟引擎在测试内
验证"observed + known_lost == ground_truth"恒等式与缺口/丢失标记。

模拟为确定性（seed 固定）；规模保持小（1 天 @ poll 60s），控制运行时间。
7/30/90 天标准档见 `python tools/soak_test.py --days 7|30|90`。
运行：
    python -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

_TOOLS = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

from soak_test import SoakSimulation  # noqa: E402

# 1 天 @ poll 60s = 1440 轮（约 20s/次）：跨一个午夜，事件足够密集
DAYS = 1
POLL = 60.0


class SoakSimulationTests(unittest.TestCase):
    def test_clean_run_exact_integer_equality(self):
        """无事件：observed == ground truth（整数精确，无丢失）。"""
        sim = SoakSimulation(days=DAYS, seed=11, poll_seconds=POLL,
                             restart_rate=0.0, offline_rate=0.0,
                             counter_reset_rate=0.0)
        r = sim.run()
        self.assertTrue(r["pass"], r["problems"])
        self.assertEqual(r["difference_prompt"], 0)
        self.assertEqual(r["difference_output"], 0)
        self.assertEqual(r["known_lost_prompt"], 0)
        self.assertEqual(r["observed_prompt"], r["ground_truth_prompt"])
        self.assertEqual(r["monitor_restarts"], 0)
        self.assertEqual(r["server_offline_events"], 0)
        self.assertGreater(r["ground_truth_prompt"], 0)

    def test_mixed_events_identity_and_flags(self):
        """混合事件：observed + known_lost == truth（整数精确），
        且标记一致性：离线缺口行数 == 离线事件数、离线期 reset 有 loss 缺口、
        在线期 reset 有 counter_reset 事件。"""
        sim = SoakSimulation(days=DAYS, seed=42, poll_seconds=POLL,
                             restart_rate=0.02, offline_rate=0.05,
                             counter_reset_rate=0.03)
        r = sim.run()
        self.assertTrue(r["pass"], r["problems"])
        self.assertEqual(r["difference_prompt"], 0)
        self.assertEqual(r["difference_output"], 0)
        # 至少发生了各类事件（seed 固定，若未来改动模拟导致某类不再出现要调整断言）
        self.assertGreaterEqual(r["server_offline_events"], 1)
        self.assertGreaterEqual(r["monitor_restarts"], 1)
        self.assertGreaterEqual(r["counter_resets"], 1)
        # 有真实丢失（离线期 reset）时必须有 possible_token_loss 缺口
        if r["known_lost_prompt"] > 0:
            self.assertGreaterEqual(r["possible_loss_gaps"], 1)
        # 多日模拟：daily 行数 >= 2
        self.assertGreaterEqual(r["db_rows_daily"], 2)

    def test_midnight_rollover_across_days(self):
        """跨午夜：delta 按样本时间戳归集（模拟内跨了 >= 1 个午夜）。
        daily 行数 >= 模拟天数（收尾读取可能落在下一个自然日边界）。"""
        sim = SoakSimulation(days=DAYS, seed=7, poll_seconds=POLL,
                             restart_rate=0.0, offline_rate=0.0,
                             counter_reset_rate=0.0)
        r = sim.run()
        self.assertTrue(r["pass"], r["problems"])
        self.assertGreaterEqual(r["db_rows_daily"], DAYS)
        self.assertEqual(r["difference_prompt"], 0)

    def test_reproducible_with_seed(self):
        """同 seed 两次运行结果完全一致（可复现性）。"""
        kw = dict(days=DAYS, seed=99, poll_seconds=POLL,
                  restart_rate=0.02, offline_rate=0.05, counter_reset_rate=0.03)
        r1 = SoakSimulation(**kw).run()
        r2 = SoakSimulation(**kw).run()
        for key in ("ground_truth_prompt", "observed_prompt", "known_lost_prompt",
                    "counter_resets", "monitor_restarts", "server_offline_events",
                    "sampling_gaps", "possible_loss_gaps"):
            self.assertEqual(r1[key], r2[key], key)


if __name__ == "__main__":
    unittest.main()
