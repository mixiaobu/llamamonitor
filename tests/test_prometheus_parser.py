"""prometheus_parser.py 单元测试（1.3.2）。

覆盖：HELP/TYPE/标签/le/+Inf/_bucket/_sum/_count、无效数字、NaN/Inf、
非单调 Bucket、Counter Reset、模型标签变化、多 Histogram 家族、分位数算法。

运行：python -m unittest discover -s tests
"""

import math
import unittest

from prometheus_parser import (
    HistogramSeries,
    histogram_bucket_delta,
    histogram_quantile,
    parse_histogram_series,
    parse_prometheus,
)

# Strata 官方 bucket 上界（秒）
OFFICIAL_LE = ["0.001", "0.005", "0.01", "0.02", "0.04", "0.06", "0.08", "0.1",
               "0.25", "0.5", "0.75", "1", "2.5", "5", "7.5", "10", "20", "40",
               "80", "160", "640", "2560"]


def _ttft_text(counts: list[int], model: str = "m1", sum_v: float = 12.5,
               count_v: int | None = None) -> str:
    """构造一个 TTFT histogram family（累计计数）。"""
    lines = [
        "# HELP vllm:time_to_first_token_seconds Time to first token in seconds",
        "# TYPE vllm:time_to_first_token_seconds histogram",
    ]
    total = 0
    for le, c in zip(OFFICIAL_LE, counts):
        total += c
        lines.append(
            f'vllm:time_to_first_token_seconds_bucket{{le="{le}",model_name="{model}"}} {total}'
        )
    lines.append(
        f'vllm:time_to_first_token_seconds_bucket{{le="+Inf",model_name="{model}"}} {total}'
    )
    lines.append(
        f'vllm:time_to_first_token_seconds_sum{{model_name="{model}"}} {sum_v}'
    )
    lines.append(
        f'vllm:time_to_first_token_seconds_count{{model_name="{model}"}} '
        f"{count_v if count_v is not None else total}"
    )
    return "\n".join(lines)


class ParseBasicsTests(unittest.TestCase):
    def test_help_type_labels_le(self):
        fams = parse_prometheus(_ttft_text([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12,
                                            13, 14, 15, 16, 17, 18, 19, 20, 21, 22]))
        fam = fams["vllm:time_to_first_token_seconds"]
        self.assertEqual(fam["type"], "histogram")
        self.assertIn("Time to first token", fam["help"])
        series = parse_histogram_series(fam)
        self.assertEqual(len(series), 1)
        s = series[0]
        self.assertEqual(s.labels["model_name"], "m1")
        self.assertEqual(len(s.buckets), len(OFFICIAL_LE) + 1)  # 含 +Inf
        self.assertEqual(s.sum, 12.5)
        self.assertEqual(s.count, sum([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12,
                                       13, 14, 15, 16, 17, 18, 19, 20, 21, 22]))

    def test_gauge_and_counter_families(self):
        text = (
            "# TYPE vllm:num_requests_running gauge\n"
            'vllm:num_requests_running{model_name="m1"} 3\n'
            "# TYPE strata:requests_total counter\n"
            "strata:requests_total 42\n"
            "# TYPE strata:live_state gauge\n"
            'strata:live_state{state="generating"} 1\n'
            'strata:live_state{state="idle"} 0\n'
        )
        fams = parse_prometheus(text)
        self.assertEqual(fams["vllm:num_requests_running"]["type"], "gauge")
        self.assertEqual(fams["vllm:num_requests_running"]["series"]["model_name=m1"]["value"], 3.0)
        self.assertEqual(fams["strata:requests_total"]["type"], "counter")
        self.assertEqual(fams["strata:requests_total"]["series"][""]["value"], 42.0)
        # 多标签同 family：两个序列
        self.assertEqual(len(fams["strata:live_state"]["series"]), 2)

    def test_no_type_line_defaults_untyped(self):
        fams = parse_prometheus("some_metric 7\n")
        self.assertEqual(fams["some_metric"]["type"], "untyped")
        self.assertEqual(fams["some_metric"]["series"][""]["value"], 7.0)


class ParseEdgeTests(unittest.TestCase):
    def test_invalid_number(self):
        fams = parse_prometheus("bad_metric oops\n")
        self.assertIsNone(fams["bad_metric"]["series"][""]["value"])

    def test_nan_inf_values(self):
        text = (
            "# TYPE m gauge\n"
            "m_nan NaN\n"
            "m_inf +Inf\n"
            "m_ninf -Inf\n"
        )
        fams = parse_prometheus(text)
        # 独立 metric 名（不同 series）
        self.assertIsNone(fams["m_nan"]["series"][""]["value"])
        self.assertEqual(fams["m_inf"]["series"][""]["value"], math.inf)
        self.assertEqual(fams["m_ninf"]["series"][""]["value"], -math.inf)

    def test_non_monotonic_buckets_clamped(self):
        text = (
            "# TYPE h histogram\n"
            'h_bucket{le="0.1"} 5\n'
            'h_bucket{le="0.5"} 3\n'   # 非单调：小于前桶
            'h_bucket{le="+Inf"} 9\n'
            "h_count 9\n"
        )
        fams = parse_prometheus(text)
        s = parse_histogram_series(fams["h"])[0]
        rows = s.sorted_le()
        self.assertEqual([r[2] for r in rows], [5.0, 5.0, 9.0])  # 0.5 桶被钳制到 5

    def test_multiple_histogram_families(self):
        t1 = _ttft_text([1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1], model="mA")
        t2 = _ttft_text([2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2], model="mB")
        fams = parse_prometheus(t1 + "\n" + t2)
        series = parse_histogram_series(fams["vllm:time_to_first_token_seconds"])
        self.assertEqual(len(series), 2)
        models = sorted(s.labels["model_name"] for s in series)
        self.assertEqual(models, ["mA", "mB"])

    def test_model_label_change_creates_new_series(self):
        a = _ttft_text([1] * 22, model="old")
        b = _ttft_text([1] * 22, model="new")
        fams = parse_prometheus(a + "\n" + b)
        series = parse_histogram_series(fams["vllm:time_to_first_token_seconds"])
        self.assertEqual(len(series), 2)


class BucketDeltaTests(unittest.TestCase):
    def test_first_sample_baseline_only(self):
        curr = {"0.1": 5.0, "+Inf": 5.0}
        self.assertEqual(histogram_bucket_delta(None, curr),
                         {"0.1": 0.0, "+Inf": 0.0})

    def test_normal_delta(self):
        prev = {"0.1": 5.0, "0.5": 8.0, "+Inf": 8.0}
        curr = {"0.1": 7.0, "0.5": 12.0, "+Inf": 12.0}
        self.assertEqual(histogram_bucket_delta(prev, curr),
                         {"0.1": 2.0, "0.5": 4.0, "+Inf": 4.0})

    def test_counter_reset(self):
        prev = {"0.1": 100.0, "0.5": 150.0, "+Inf": 150.0}
        curr = {"0.1": 3.0, "0.5": 5.0, "+Inf": 5.0}
        # reset：delta = 新周期累计值
        self.assertEqual(histogram_bucket_delta(prev, curr),
                         {"0.1": 3.0, "0.5": 5.0, "+Inf": 5.0})

    def test_missing_le_in_curr(self):
        prev = {"0.1": 5.0, "0.5": 8.0, "+Inf": 8.0}
        curr = {"0.1": 6.0, "+Inf": 9.0}  # 0.5 桶消失（版本变化）
        out = histogram_bucket_delta(prev, curr)
        self.assertEqual(out, {"0.1": 1.0, "+Inf": 1.0})


class QuantileTests(unittest.TestCase):
    def test_p50_simple(self):
        # 10 个样本各落一个桶 (1..10)：P50 target=5 -> 第一桶 cum>=5 是桶 5，
        # 桶内插值 (5-4)/(5-4)=1 -> 4+(5-4)*1 = 5.0（histogram_quantile 语义）
        deltas = {f"{i}": 1.0 for i in range(1, 11)}
        v = histogram_quantile(0.5, deltas)
        self.assertIsNotNone(v)
        self.assertAlmostEqual(v, 5.0, places=6)

    def test_low_sample_no_zero_ms(self):
        # 没有请求的窗口 -> None（不是 0ms）
        self.assertIsNone(histogram_quantile(0.5, {}))
        self.assertIsNone(histogram_quantile(0.5, {"0.1": 0.0}))
        self.assertIsNone(histogram_quantile(0.5, {"+Inf": 3.0}))  # 只有 +Inf 且无有限桶

    def test_all_in_plus_inf_returns_last_finite_le(self):
        deltas = {"0.1": 2.0, "+Inf": 3.0}  # 3 个样本超出所有有限桶
        v = histogram_quantile(0.95, deltas, total_count=5.0)
        self.assertAlmostEqual(v, 0.1, places=6)

    def test_single_bucket_concentration(self):
        deltas = {"0.1": 10.0}
        v = histogram_quantile(0.5, deltas)
        self.assertAlmostEqual(v, 0.05, places=6)  # [0, 0.1] 中点

    def test_interpolation_between_buckets(self):
        # 输入是**每桶 delta**（窗口内落入各桶的样本数）：
        # 桶 (0,0.1]: 90 个, (0.1,0.5]: 10 个 -> P95：
        # 累计 0.1->90, 0.5->100；target=0.95*100=95 在第二桶：
        # frac=(95-90)/(100-90)=0.5 -> 0.1+0.4*0.5=0.3
        deltas = {"0.1": 90.0, "0.5": 10.0}
        v = histogram_quantile(0.95, deltas)
        self.assertAlmostEqual(v, 0.3, places=6)

    def test_itl_estimate_semantics(self):
        """ITL 分布（请求平均间隔构造）：分位数算法与 TTFT 相同（每桶 delta 输入）。"""
        deltas = {"0.02": 50.0, "0.04": 30.0, "0.1": 20.0}
        v50 = histogram_quantile(0.5, deltas)
        v95 = histogram_quantile(0.95, deltas)
        self.assertLess(v50, v95)


if __name__ == "__main__":
    unittest.main()
