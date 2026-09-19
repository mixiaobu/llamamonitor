"""stats.py 纯函数单元测试（Phase 2）。

在项目根目录运行：
    python -m unittest discover -s tests -v
"""

import unittest

from stats import compute_deltas, counter_delta, mtp_accept_rate, tps


class CounterDeltaTests(unittest.TestCase):
    def test_first_time_is_baseline_zero(self):
        # 首次读到该 Counter：只建 baseline，delta=0
        self.assertEqual(counter_delta(100.0, None), 0.0)

    def test_current_missing_is_none(self):
        self.assertIsNone(counter_delta(None, 100.0))
        self.assertIsNone(counter_delta(None, None))

    def test_normal_delta(self):
        self.assertEqual(counter_delta(150.0, 100.0), 50.0)
        self.assertEqual(counter_delta(100.0, 100.0), 0.0)  # 未变化

    def test_reset_uses_current_value(self):
        # Counter 变小：判定 llama-server 重启，delta = current
        self.assertEqual(counter_delta(30.0, 150.0), 30.0)
        self.assertEqual(counter_delta(0.0, 150.0), 0.0)
        self.assertEqual(counter_delta(0.5, 150.0), 0.5)


class TpsTests(unittest.TestCase):
    def test_normal(self):
        self.assertAlmostEqual(tps(50.0, 0.5), 100.0)

    def test_denominator_le_zero_is_none(self):
        self.assertIsNone(tps(50.0, 0.0))
        self.assertIsNone(tps(50.0, -1.0))

    def test_none_inputs(self):
        self.assertIsNone(tps(None, 0.5))
        self.assertIsNone(tps(50.0, None))


class MtpTests(unittest.TestCase):
    def test_normal(self):
        self.assertAlmostEqual(mtp_accept_rate(3.0, 5.0), 60.0)

    def test_draft_zero_is_none(self):
        self.assertIsNone(mtp_accept_rate(3.0, 0.0))

    def test_none_inputs(self):
        self.assertIsNone(mtp_accept_rate(None, 5.0))
        self.assertIsNone(mtp_accept_rate(3.0, None))


class ComputeDeltasTests(unittest.TestCase):
    def test_independent_reset_detection(self):
        # prompt 重启变小、output 正常增长：每个 Counter 独立检测，互不干扰
        current = {
            "llamacpp:prompt_tokens_total": 30.0,     # 150 -> 30 重启
            "llamacpp:tokens_predicted_total": 25.0,  # 20 -> 25 正常
        }
        previous = {
            "llamacpp:prompt_tokens_total": 150.0,
            "llamacpp:tokens_predicted_total": 20.0,
        }
        deltas = compute_deltas(current, previous)
        self.assertEqual(deltas["prompt_tokens"], 30.0)
        self.assertEqual(deltas["output_tokens"], 5.0)
        self.assertIsNone(deltas["draft_tokens"])  # 未提供的 Counter 为 None


if __name__ == "__main__":
    unittest.main()
