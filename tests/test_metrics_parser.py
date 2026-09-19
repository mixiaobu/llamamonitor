"""metrics_parser.py 单元测试（Phase 1）。

在项目根目录运行：
    python -m unittest discover -s tests -v
"""

import math
import unittest
from pathlib import Path

from metrics_parser import get_metric_by_label, get_metric_value, parse_metrics

SAMPLE_FILE = Path(__file__).resolve().parent / "sample_metrics.txt"


def _parsed_sample() -> dict:
    return parse_metrics(SAMPLE_FILE.read_text(encoding="utf-8"))


class SampleFileTests(unittest.TestCase):
    """验证 tests/sample_metrics.txt（llama.cpp /metrics 真实格式样本）的解析结果。"""

    def test_counters(self):
        parsed = _parsed_sample()
        self.assertEqual(get_metric_value(parsed, "llamacpp:prompt_tokens_total"), 168507.0)
        self.assertEqual(get_metric_value(parsed, "llamacpp:prompt_tokens_cached_total"), 3314650.0)
        self.assertEqual(get_metric_value(parsed, "llamacpp:tokens_predicted_total"), 18722.0)
        self.assertEqual(get_metric_value(parsed, "llamacpp:spec_decode_num_draft_tokens_total"), 23912.0)
        self.assertEqual(get_metric_value(parsed, "llamacpp:spec_decode_num_accepted_tokens_total"), 14739.0)
        self.assertEqual(get_metric_value(parsed, "llamacpp:spec_decode_num_drafts_total"), 5420.0)
        self.assertEqual(get_metric_value(parsed, "llamacpp:n_decode_total"), 1501.0)

    def test_gauges_and_floats(self):
        parsed = _parsed_sample()
        self.assertEqual(get_metric_value(parsed, "llamacpp:n_tokens_max"), 2.0)
        self.assertEqual(get_metric_value(parsed, "llamacpp:requests_processing"), 1.0)
        self.assertEqual(get_metric_value(parsed, "llamacpp:requests_deferred"), 0.0)
        self.assertEqual(get_metric_value(parsed, "llamacpp:n_busy_slots_per_decode"), 1.0)
        self.assertAlmostEqual(get_metric_value(parsed, "llamacpp:prompt_seconds_total"), 12.345)
        self.assertAlmostEqual(get_metric_value(parsed, "llamacpp:tokens_predicted_seconds_total"), 88.12)

    def test_per_position_labels(self):
        parsed = _parsed_sample()
        self.assertEqual(
            get_metric_by_label(
                parsed,
                "llamacpp:spec_decode_num_accepted_tokens_per_pos_total",
                "position",
            ),
            {"0": 14039.0, "1": 652.0, "2": 44.0, "3": 4.0},
        )

    def test_unknown_metrics_are_ignored(self):
        # 样本包含 server_running 等采集清单之外的字段，解析器不应报错
        parsed = _parsed_sample()
        self.assertEqual(get_metric_value(parsed, "llamacpp:server_running"), 1.0)


class MissingFieldTests(unittest.TestCase):
    """缺失字段必须返回 None，而不是抛异常。"""

    def test_missing_metric_returns_none(self):
        parsed = _parsed_sample()
        self.assertIsNone(get_metric_value(parsed, "llamacpp:does_not_exist"))
        self.assertIsNone(get_metric_value(parsed, "llamacpp:does_not_exist", {"position": "0"}))
        self.assertIsNone(get_metric_by_label(parsed, "llamacpp:does_not_exist", "position"))

    def test_unlabeled_lookup_does_not_match_labeled_lines(self):
        # per_pos 指标只有带标签行；不带标签查询必须返回 None，不能误匹配
        parsed = _parsed_sample()
        self.assertIsNone(
            get_metric_value(parsed, "llamacpp:spec_decode_num_accepted_tokens_per_pos_total")
        )

    def test_empty_text(self):
        self.assertEqual(parse_metrics(""), {})
        self.assertIsNone(get_metric_value(parse_metrics(""), "any:metric"))


class InlineFormatTests(unittest.TestCase):
    """用内联小样本验证边界行为：注释、时间戳、特殊值、畸形行。"""

    def test_skip_comments_and_blank_lines(self):
        text = "\n# HELP foo_total help text\n# TYPE foo_total counter\nfoo_total 42\n"
        self.assertEqual(get_metric_value(parse_metrics(text), "foo_total"), 42.0)

    def test_trailing_timestamp_ignored(self):
        text = "foo_total 7 1700000000\n"
        self.assertEqual(get_metric_value(parse_metrics(text), "foo_total"), 7.0)

    def test_special_values(self):
        parsed = parse_metrics("a +Inf\nb -Inf\nc NaN\n")
        self.assertEqual(get_metric_value(parsed, "a"), math.inf)
        self.assertEqual(get_metric_value(parsed, "b"), -math.inf)
        self.assertTrue(math.isnan(get_metric_value(parsed, "c")))

    def test_multi_label_and_order_insensitive(self):
        parsed = parse_metrics('m{a="1", b="x"} 9\n')
        self.assertEqual(get_metric_value(parsed, "m", {"b": "x", "a": "1"}), 9.0)

    def test_later_line_overrides_earlier(self):
        parsed = parse_metrics("m 1\nm 2\n")
        self.assertEqual(get_metric_value(parsed, "m"), 2.0)

    def test_malformed_lines_skipped_without_raising(self):
        text = (
            "justoneword\n"              # 没有数值部分
            "metric{\n"                  # 花括号未闭合
            'metric{a=1} 5\n'            # 标签值未加引号
            'metric{a="unterminated} 5\n'  # 引号未闭合
            "metric 12abc\n"             # 非法数字
            "good 1\n"                   # 唯一合法行
        )
        parsed = parse_metrics(text)
        self.assertEqual(get_metric_value(parsed, "good"), 1.0)
        self.assertIsNone(get_metric_value(parsed, "metric"))

    def test_colon_metric_names(self):
        parsed = parse_metrics("llamacpp:n_busy_slots_per_decode 3\n")
        self.assertEqual(get_metric_value(parsed, "llamacpp:n_busy_slots_per_decode"), 3.0)


if __name__ == "__main__":
    unittest.main()
