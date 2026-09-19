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


class AuditParserRegressionTests(unittest.TestCase):
    """Phase 14 审计回归（DATA：文档行为与实现对齐 + 科学计数法 / 标签引号）。"""

    def test_scientific_notation_values(self):
        """Prometheus 值允许科学计数法（float(token) 原生支持；此前无回归测试保护）。"""
        parsed = parse_metrics(
            "llamacpp:prompt_tokens_total 1.68507e5\n"
            "llamacpp:kv_cache_usage_ratio 1.25E-1\n"
            "llamacpp:n_decode_total 3.0e0\n"
        )
        self.assertEqual(get_metric_value(parsed, "llamacpp:prompt_tokens_total"), 168507.0)
        self.assertAlmostEqual(get_metric_value(parsed, "llamacpp:kv_cache_usage_ratio"), 0.125)
        self.assertEqual(get_metric_value(parsed, "llamacpp:n_decode_total"), 3.0)

    def test_escaped_quote_in_label_value(self):
        """标签值内 \\" 转义防止值提前终止；**反斜杠按字面保留在解析值里**
        （AUDIT-DATA：docstring 与实现对齐后的实际行为回归）。"""
        parsed = parse_metrics('metric{a="he said \\"hi\\""} 7\n')
        self.assertEqual(get_metric_value(parsed, "metric", {"a": 'he said \\"hi\\"'}), 7.0)
        # 未反转义的值不是 key（明确文档化当前行为）
        self.assertIsNone(get_metric_value(parsed, "metric", {"a": 'he said "hi"'}))

    def test_multi_label_with_escaped_quotes(self):
        parsed = parse_metrics('metric{a="x\\"y", b="z"} 9\n')
        self.assertEqual(
            get_metric_value(parsed, "metric", {"a": 'x\\"y', "b": "z"}), 9.0)


if __name__ == "__main__":
    unittest.main()
