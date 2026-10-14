"""strata_parser.py 单元测试（1.3.0）。

在项目根目录运行：
    python -m unittest discover -s tests
"""

import json
import unittest
from pathlib import Path

from strata_parser import (
    CAP_SUPPORTED,
    CAP_TEMPORARILY_UNAVAILABLE,
    CAP_UNSUPPORTED,
    build_conversation_cache,
    build_engine_info,
    build_health_info,
    build_live_state,
    build_model_info,
    build_recent_requests,
    convert_totals_snapshot,
    counter_state_key,
    map_finish,
    map_phase,
    normalize_totals_sample,
    parse_metrics_json,
    server_identity,
    strata_capabilities,
    validate_totals,
)

FIX = Path(__file__).resolve().parent / "fixtures" / "strata"


def load(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def loadj(name: str):
    return json.loads(load(name))


class SpecMathMandatoryTests(unittest.TestCase):
    """§20 强制验证：spec 原始整数必须精确换算（先加后减，不允许浮点近似）。"""

    def test_spec_window_exact_integers(self):
        totals = loadj("totals_spec_window.json")
        out = convert_totals_snapshot(totals)
        self.assertIsNotNone(out)
        self.assertEqual(out["new_prompt"], 79265)
        self.assertEqual(out["cached"], 2032318)
        self.assertEqual(out["generated"], 5759)
        self.assertEqual(out["logical"], 2117342)   # 2,111,583 + 5,759
        self.assertEqual(out["compute"], 85024)     # 79,265 + 5,759
        self.assertAlmostEqual(out["reuse_rate"], 2032318 / 2111583, places=9)
        # ≈96.25%
        self.assertAlmostEqual(out["reuse_rate"] * 100, 96.25, delta=0.01)

    def test_spec_window_raw_input(self):
        # 直接构造 spec 的原始整数（不经 fixture），确认路径一致
        out = convert_totals_snapshot({
            "prompt_tokens": 2111583, "reused": 2032318, "output_tokens": 5759,
        })
        self.assertEqual(out["new_prompt"], 79265)
        self.assertEqual(out["logical"], 2117342)
        self.assertEqual(out["compute"], 85024)

    def test_zero_prompt_reuse_rate_none(self):
        out = convert_totals_snapshot({"prompt_tokens": 0, "reused": 0, "output_tokens": 5})
        self.assertIsNone(out["reuse_rate"])
        self.assertEqual(out["compute"], 5)

    def test_invalid_totals_return_none(self):
        self.assertIsNone(convert_totals_snapshot({"prompt_tokens": 10, "reused": 11, "output_tokens": 1}))
        self.assertIsNone(convert_totals_snapshot({"prompt_tokens": 10, "reused": 1, "output_tokens": None}))
        self.assertIsNone(convert_totals_snapshot(None))


class ParseMetricsJsonTests(unittest.TestCase):
    def test_valid_dict(self):
        d = parse_metrics_json(load("metrics_idle.json"))
        self.assertIsInstance(d, dict)
        self.assertIn("totals", d)

    def test_bad_json_returns_none(self):
        self.assertIsNone(parse_metrics_json(load("metrics_bad_json.json")))

    def test_non_dict_root_returns_none(self):
        self.assertIsNone(parse_metrics_json(load("metrics_bad_root.json")))

    def test_scalar_root(self):
        self.assertIsNone(parse_metrics_json("42"))


class ValidateTotalsTests(unittest.TestCase):
    def test_real_idle_valid(self):
        self.assertTrue(validate_totals(loadj("metrics_idle.json")["totals"]))

    def test_reused_gt_prompt_invalid(self):
        self.assertFalse(validate_totals(loadj("metrics_invalid_reused_gt_prompt.json")["totals"]))

    def test_missing_core_field_invalid(self):
        self.assertFalse(validate_totals(loadj("metrics_output_missing.json")["totals"]))

    def test_none_invalid(self):
        self.assertFalse(validate_totals(None))


class NormalizeTotalsSampleTests(unittest.TestCase):
    def _base_state(self):
        totals = loadj("metrics_idle.json")["totals"]
        return {
            "totals.prompt_tokens": float(totals["prompt_tokens"]),
            "totals.reused": float(totals["reused"]),
            "totals.output_tokens": float(totals["output_tokens"]),
            "totals.drafts_offered": float(totals["drafts_offered"]),
            "totals.drafts_accepted": float(totals["drafts_accepted"]),
            "totals.prompt_ms": totals["prompt_ms"] / 1000.0,
            "totals.decode_ms": totals["decode_ms"] / 1000.0,
            "totals.since": float(totals["since"]),
        }

    def test_first_sample_baselines_zero_delta(self):
        # 无 previous（首次连接）：只建 baseline，delta=0（不重复计历史累计）
        totals = loadj("metrics_idle.json")["totals"]
        out = normalize_totals_sample(totals, {})
        self.assertIsNotNone(out)
        self.assertFalse(out["epoch_changed"])
        self.assertEqual(out["deltas"]["prompt_total"], 0.0)
        self.assertEqual(out["deltas"]["output"], 0.0)
        self.assertEqual(out["counters"]["totals.prompt_tokens"], float(totals["prompt_tokens"]))
        self.assertEqual(out["counters"]["totals.since"], float(totals["since"]))

    def test_delta_math(self):
        prev = self._base_state()
        totals = dict(loadj("metrics_epoch_change.json")["totals"])
        # epoch 未变（since 相同？epoch_change fixture 改了 since）-> 用同 since
        totals["since"] = prev["totals.since"]
        out = normalize_totals_sample(totals, prev)
        self.assertIsNotNone(out)
        self.assertFalse(out["epoch_changed"])
        self.assertEqual(out["deltas"]["prompt_total"], 11714703 - 9080209)
        self.assertEqual(out["deltas"]["reused"], 11629379 - 8998497)
        self.assertEqual(out["deltas"]["output"], 32843 - 25936)
        # new_prompt = Δprompt_total - Δreused
        self.assertEqual(
            out["deltas"]["new_prompt"],
            (11714703 - 9080209) - (11629379 - 8998497),
        )
        self.assertEqual(
            out["deltas"]["logical"],
            (11714703 - 9080209) + (32843 - 25936),
        )
        # 秒口径
        self.assertAlmostEqual(
            out["deltas"]["prompt_seconds"],
            (160561.9 - 160561.9), places=6,
        )

    def test_epoch_change_resets_baselines(self):
        prev = self._base_state()
        totals = loadj("metrics_epoch_change.json")["totals"]
        self.assertNotEqual(totals["since"], prev["totals.since"])
        out = normalize_totals_sample(totals, prev)
        self.assertIsNotNone(out)
        self.assertTrue(out["epoch_changed"])
        # 新周期：delta 全 0（周期前不计入），baseline 更新为当前值
        self.assertEqual(out["deltas"]["prompt_total"], 0.0)
        self.assertEqual(out["deltas"]["output"], 0.0)
        self.assertEqual(out["counters"]["totals.prompt_tokens"], float(totals["prompt_tokens"]))

    def test_counter_reset_within_epoch(self):
        prev = self._base_state()
        totals = loadj("metrics_counter_reset.json")["totals"]
        self.assertEqual(totals["since"], prev["totals.since"])  # 同 epoch
        out = normalize_totals_sample(totals, prev)
        self.assertIsNotNone(out)
        self.assertFalse(out["epoch_changed"])
        self.assertIn("totals.prompt_tokens", out["reset_metrics"])
        # reset 规则：delta = current
        self.assertEqual(out["deltas"]["prompt_total"], 5000.0)
        self.assertEqual(out["deltas"]["reused"], 4000.0)
        self.assertEqual(out["deltas"]["output"], 300.0)

    def test_invalid_sample_returns_none(self):
        prev = self._base_state()
        self.assertIsNone(normalize_totals_sample(loadj("metrics_invalid_reused_gt_prompt.json")["totals"], prev))
        self.assertIsNone(normalize_totals_sample(None, prev))
        self.assertIsNone(normalize_totals_sample({"prompt_tokens": 1}, prev))

    def test_cache_reuse_rate_window(self):
        prev = self._base_state()
        totals = dict(loadj("metrics_epoch_change.json")["totals"])
        totals["since"] = prev["totals.since"]
        out = normalize_totals_sample(totals, prev)
        expected = (11629379 - 8998497) / (11714703 - 9080209)
        self.assertAlmostEqual(out["cache_reuse_rate"], expected, places=9)


class LiveStateTests(unittest.TestCase):
    def test_idle(self):
        metrics = loadj("metrics_idle.json")
        status = loadj("status_idle.json")
        live = build_live_state(metrics, status)
        self.assertIsNotNone(live)
        self.assertFalse(live["busy"])
        self.assertEqual(live["running"], 0)
        # idle 时 live.tok_s 为 None
        self.assertIsNone(live["tok_s"])
        # 隐私边界：tail 不进入（status_busy 才有）
        self.assertNotIn("tail", live)

    def test_generating_busy_and_progress(self):
        metrics = loadj("metrics_generating.json")
        status = loadj("status_busy.json")
        live = build_live_state(metrics, status)
        self.assertTrue(live["busy"])
        self.assertEqual(live["running"], 1)
        self.assertEqual(live["outside_slots"], 1)
        self.assertEqual(live["queued"], 1)
        self.assertEqual(live["generated"], 49)
        self.assertEqual(live["max_tokens"], 60)
        self.assertAlmostEqual(live["tok_s"], 26.3)
        self.assertAlmostEqual(live["prefill_tok_s_mean"], 51.5)
        self.assertEqual(live["state"], "generating")
        self.assertEqual(live["phase"], "answering")
        # tail（实际生成文本）绝不进入
        self.assertNotIn("tail", live)
        self.assertNotIn("tool", live)
        # 隐私边界：tail 文本内容不出现在任何字段值里
        self.assertNotIn("这是模型正在生成的实际文本", json.dumps(live, ensure_ascii=False))

    def test_outside_slots_authoritative_busy(self):
        # §36：slots.is_processing=false 但 outside_slots>0 => busy
        metrics = loadj("metrics_queued.json")
        status = loadj("status_idle.json")
        m = json.loads(json.dumps(metrics))
        m["live"]["outside_slots"] = 1
        m["live"]["state"] = "idle"
        live = build_live_state(m, status)
        self.assertTrue(live["busy"])

    def test_status_busy_authoritative(self):
        # status.busy=true 但 live 全 null（metrics 未轮询到）=> busy
        live = build_live_state(None, loadj("status_busy.json"))
        self.assertTrue(live["busy"])
        self.assertEqual(live["prompt_tokens"], 57)

    def test_all_missing_returns_none(self):
        self.assertIsNone(build_live_state(None, None))

    def test_phase_mapping(self):
        self.assertEqual(map_phase("reading the prompt"), "Prompt 处理")
        self.assertEqual(map_phase("thinking"), "思考中")
        self.assertEqual(map_phase("answering"), "生成回答")
        self.assertEqual(map_phase("generating"), "生成中")
        self.assertIsNone(map_phase(None))
        self.assertIsNone(map_phase("  "))
        # 未知 phase 安全回退（截断原值）
        self.assertEqual(map_phase("brand_new_phase"), "brand_new_phase")
        self.assertEqual(map_phase("x" * 100), "x" * 40)

    def test_finish_mapping(self):
        self.assertEqual(map_finish("stop"), "正常结束")
        self.assertEqual(map_finish("cancel"), "已取消")
        self.assertIsNone(map_finish(None))
        self.assertEqual(map_finish("weird_reason"), "weird_reason")


class EngineInfoTests(unittest.TestCase):
    def test_whitelist_no_chat_template(self):
        metrics = loadj("metrics_idle.json")
        props = loadj("props.json")
        info = build_engine_info(metrics, props)
        self.assertIsNotNone(info)
        self.assertEqual(info["version"], "0.1.43.1")  # engine.version 权威
        self.assertIn("kv", info)
        self.assertIn("model", info)
        # chat_template 绝不进入
        blob = json.dumps(info, ensure_ascii=False)
        self.assertNotIn("Jinja", blob)
        self.assertNotIn("chat_template", blob)
        # pcie_frac 是字符串 "0.20"（防御性原样保留，UI 层解析）
        self.assertEqual(info.get("pcie_frac"), metrics["engine"]["pcie_frac"])

    def test_both_missing_returns_none(self):
        self.assertIsNone(build_engine_info(None, None))

    def test_only_metrics(self):
        info = build_engine_info(loadj("metrics_idle.json"), None)
        self.assertIsNotNone(info)
        self.assertIn("version", info)


class ConversationCacheTests(unittest.TestCase):
    def test_disabled_with_reused_tokens(self):
        # §58：enabled=false 但 reused_tokens 有历史累计值
        cc = build_conversation_cache(loadj("metrics_idle.json"))
        self.assertIsNotNone(cc)
        self.assertFalse(cc["enabled"])
        self.assertGreater(cc["reused_tokens"], 0)

    def test_enabled_branch(self):
        cc = build_conversation_cache(loadj("metrics_cc_enabled.json"))
        self.assertTrue(cc["enabled"])
        self.assertEqual(cc["budget_mib"], 2048)

    def test_missing_returns_none(self):
        self.assertIsNone(build_conversation_cache({"live": {}}))


class RecentRequestsTests(unittest.TestCase):
    def test_fields_and_rates(self):
        reqs = build_recent_requests(loadj("metrics_idle.json"), limit=20)
        self.assertEqual(len(reqs), len(loadj("metrics_idle.json")["requests"]))
        first = reqs[0]
        # 真实第一条：prompt_total 233026, reused 233011
        self.assertEqual(first["prompt_total"], 233026)
        self.assertEqual(first["new_prompt"], 15)
        self.assertAlmostEqual(first["prompt_reuse_rate"], 233011 / 233026, places=9)
        self.assertAlmostEqual(first["draft_accept_rate"], 725 / 1242, places=9)
        self.assertEqual(first["finish_label"], "正常结束")
        # 不可靠/隐私字段不进
        for k in ("projection", "ram_blobs", "file_blobs", "file_mb", "pcie_share",
                  "hit_rate", "reasoning_repeat_coverage", "engine_generated"):
            self.assertNotIn(k, first)

    def test_zero_prompt_total_rate_none(self):
        m = {"requests": [{"prompt_total": 0, "reused": 0, "output_tokens": 10}]}
        reqs = build_recent_requests(m)
        self.assertIsNone(reqs[0]["prompt_reuse_rate"])
        self.assertIsNone(reqs[0]["draft_accept_rate"])

    def test_limit(self):
        m = loadj("metrics_idle.json")
        self.assertEqual(len(build_recent_requests(m, limit=3)), 3)

    def test_empty(self):
        self.assertEqual(build_recent_requests(None), [])
        self.assertEqual(build_recent_requests({"requests": []}), [])


class ModelInfoTests(unittest.TestCase):
    def test_merged(self):
        info = build_model_info(loadj("health_ok.json"), loadj("models.json"), loadj("props.json"))
        self.assertIsNotNone(info)
        self.assertEqual(info["model"], "qwen3.8-flash-next-unsloth-ud-q4_k_xl")
        self.assertTrue(info["model_loaded"])
        self.assertEqual(info["context_window"], 262144.0)
        self.assertTrue(info["vision_supported"])
        self.assertEqual(info["total_slots"], 2.0)
        self.assertEqual(info["build_info"], "Strata 0.1.43.1")
        # model_path 只留文件名（完整本地路径不进）
        self.assertEqual(info["model_file_name"], "qwen3.8.gguf")
        self.assertNotIn("C:\\Users", json.dumps(info, ensure_ascii=False))
        self.assertNotIn("C:/Users", json.dumps(info, ensure_ascii=False))

    def test_chat_template_never_included(self):
        info = build_model_info(None, None, loadj("props.json"))
        self.assertNotIn("Jinja", json.dumps(info, ensure_ascii=False))

    def test_all_missing_none(self):
        self.assertIsNone(build_model_info(None, None, None))

    def test_sleeping(self):
        info = build_model_info(None, None, loadj("props_sleeping.json"))
        self.assertTrue(info["is_sleeping"])


class HealthInfoTests(unittest.TestCase):
    def test_whitelist(self):
        h = build_health_info(loadj("health_ok.json"))
        self.assertEqual(h["service"], "strata")
        self.assertTrue(h["loaded"])
        self.assertEqual(h["max_context"], 262144.0)

    def test_none(self):
        self.assertIsNone(build_health_info("nope"))


class ServerIdentityTests(unittest.TestCase):
    def test_host_port(self):
        self.assertEqual(server_identity("http://127.0.0.1:9091"), "127.0.0.1:9091")

    def test_default_port(self):
        self.assertEqual(server_identity("http://example.com/metrics"), "example.com:80")
        self.assertEqual(server_identity("https://example.com"), "example.com:443")

    def test_counter_key_isolation(self):
        k1 = counter_state_key("127.0.0.1:9091", "totals.prompt_tokens")
        k2 = counter_state_key("192.168.1.5:9091", "totals.prompt_tokens")
        self.assertNotEqual(k1, k2)
        self.assertTrue(k1.startswith("strata:"))


class CapabilitiesTests(unittest.TestCase):
    def test_all_endpoints_ok(self):
        caps = strata_capabilities(
            {"health": True, "metrics": True, "status": True,
             "slots": True, "props": True, "models": True},
            loadj("metrics_idle.json"),
        )
        self.assertEqual(caps["request_history"], CAP_SUPPORTED)
        self.assertEqual(caps["conversation_cache"], CAP_SUPPORTED)
        self.assertEqual(caps["draft_position_statistics"], CAP_UNSUPPORTED)

    def test_metrics_down_degrades(self):
        caps = strata_capabilities({"metrics": False, "slots": True, "health": True}, None)
        self.assertEqual(caps["request_history"], CAP_TEMPORARILY_UNAVAILABLE)
        self.assertEqual(caps["conversation_cache"], CAP_TEMPORARILY_UNAVAILABLE)
        self.assertEqual(caps["usage_counters"], CAP_TEMPORARILY_UNAVAILABLE)

    def test_all_unknown(self):
        caps = strata_capabilities(None, None)
        self.assertEqual(caps["live_status"], "unknown")

    def test_missing_section_degrades(self):
        caps = strata_capabilities(
            {"metrics": True, "slots": True, "health": True,
             "props": True, "models": True, "status": True},
            {"live": {}, "requests": []},  # 无 conversation_cache/engine/hardware/totals
        )
        self.assertEqual(caps["conversation_cache"], CAP_TEMPORARILY_UNAVAILABLE)
        self.assertEqual(caps["engine_diagnostics"], CAP_TEMPORARILY_UNAVAILABLE)
        self.assertEqual(caps["backend_hardware"], CAP_TEMPORARILY_UNAVAILABLE)
        self.assertEqual(caps["usage_counters"], CAP_TEMPORARILY_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
