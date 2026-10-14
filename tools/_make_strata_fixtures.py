"""tools/_make_strata_fixtures.py — 从真实 Strata 0.1.43.1 抓取数据构造测试 fixtures。

生成 tests/fixtures/strata/*.json（基于 artifacts/_strata_metrics_live.json 的
真实结构；history 截短到 3 点以减小体积；chat_template 不单独存文件，
props fixture 内只留白名单相关字段）。
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "tests" / "fixtures" / "strata"
OUT.mkdir(parents=True, exist_ok=True)


def dump(name: str, data) -> None:
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print("wrote", name)


def load_real():
    return json.loads((ROOT / "artifacts" / "_strata_metrics_live.json").read_text(encoding="utf-8-sig"))


real = load_real()

# ---- 基础：idle（真实数据截短 history）----
base = copy.deepcopy(real)
for k, v in base.get("history", {}).items():
    if isinstance(v, list):
        base["history"][k] = v[:3]
base.pop("requests_kept", None)
dump("metrics_idle.json", base)

# ---- generating：live.state=generating, phase=answering, tok_s 有值, outside_slots=1 ----
gen = copy.deepcopy(base)
gen["live"].update({
    "state": "generating", "phase": "answering", "queued": 0,
    "prompt_tokens": 57, "prompt_read": None, "prompt_total": None,
    "generated": 49, "max_tokens": 60, "elapsed_s": 2.1,
    "tok_s": 26.3, "tok_s_mean": 23.9, "prefill_tok_s_mean": 51.5,
    "tok_s_window_s": 2.0, "parallel": 2, "running": 1,
    "slots": [{"slot": 0, "state": "idle", "held_tokens": 200903},
              {"slot": 1, "state": "idle", "held_tokens": 0}],
    "outside_slots": 1, "waiting": 0, "admitting": 0,
})
dump("metrics_generating.json", gen)

# ---- reading（pre-filling）----
rd = copy.deepcopy(base)
rd["live"].update({
    "state": "reading", "phase": "reading the prompt", "queued": 0,
    "prompt_tokens": 233026, "prompt_read": 15, "prompt_total": 233026,
    "generated": 0, "max_tokens": 60, "elapsed_s": 1.1,
    "tok_s": None, "tok_s_mean": 40.9, "prefill_tok_s_mean": 41.0,
    "tok_s_window_s": 2.0, "parallel": 2, "running": 1,
    "outside_slots": 1, "waiting": 0, "admitting": 0,
})
dump("metrics_reading.json", rd)

# ---- queued / waiting ----
q = copy.deepcopy(base)
q["live"].update({
    "state": "idle", "phase": None, "queued": 2,
    "prompt_tokens": None, "prompt_read": None, "prompt_total": None,
    "generated": None, "max_tokens": None, "elapsed_s": None,
    "tok_s": None, "tok_s_mean": None, "prefill_tok_s_mean": None,
    "tok_s_window_s": 2.0, "parallel": 2, "running": 0,
    "outside_slots": 0, "waiting": 2, "admitting": 0,
})
dump("metrics_queued.json", q)

# ---- conversation_cache 启用（§55-58：enabled=true 的真实分支）----
cc_on = copy.deepcopy(base)
cc_on["conversation_cache"] = {
    "enabled": True, "budget_mib": 2048, "slots": 8, "parked": 2,
    "bytes": 123456789, "evictions": 3, "parks": 12, "restores": 9,
    "last_event": "park", "last_tokens": 2048, "last_at": 1791969500.0,
    "requests": 42, "requests_reused": 41, "reused_tokens": 8998497,
    "prompt_tokens": 9080209, "last_reused": 233011, "last_prompt": 233026,
}
dump("metrics_cc_enabled.json", cc_on)

# ---- epoch 变化（totals.since 前进）----
epoch = copy.deepcopy(base)
epoch["totals"]["since"] = 1791999999.0
epoch["totals"]["requests"] = 54
epoch["totals"]["prompt_tokens"] = 11714703
epoch["totals"]["reused"] = 11629379
epoch["totals"]["output_tokens"] = 32843
dump("metrics_epoch_change.json", epoch)

# ---- Counter reset（同 epoch 内 counter 下降）----
rst = copy.deepcopy(base)
rst["totals"]["prompt_tokens"] = 5000
rst["totals"]["reused"] = 4000
rst["totals"]["output_tokens"] = 300
dump("metrics_counter_reset.json", rst)

# ---- 约束违反：reused > prompt_tokens ----
inv = copy.deepcopy(base)
inv["totals"]["reused"] = inv["totals"]["prompt_tokens"] + 100
dump("metrics_invalid_reused_gt_prompt.json", inv)

# ---- totals 缺失 ----
nt = copy.deepcopy(base)
nt.pop("totals")
dump("metrics_totals_missing.json", nt)

# ---- 核心字段缺失（output_tokens 缺失）----
fm = copy.deepcopy(base)
fm["totals"].pop("output_tokens")
dump("metrics_output_missing.json", fm)

# ---- 非 dict 根（数组）----
dump("metrics_bad_root.json", [1, 2, 3])

# ---- 坏 JSON（文本 fixture）----
(OUT / "metrics_bad_json.json").write_text("{not valid json", encoding="utf-8")
print("wrote metrics_bad_json.json")

# ---- /status（idle：无 tail/tool 键；真实行为）----
dump("status_idle.json", {
    "busy": False, "queued": 0, "phase": "thinking",
    "prompt_tokens": 233026, "generated": 0, "max_tokens": 60,
    "reasoning_recoveries": 0, "thinking_budget": 8192,
})

# ---- /status（busy：tail/tool/elapsed_s 出现；tail 为实际生成文本——隐私边界）----
dump("status_busy.json", {
    "busy": True, "queued": 1, "phase": "answering",
    "prompt_tokens": 57, "generated": 49, "max_tokens": 60,
    "reasoning_recoveries": 0, "thinking_budget": 8192,
    "tool": None, "tail": "这是模型正在生成的实际文本，绝不能落库或展示",
    "elapsed_s": 2.1,
})

# ---- /health ----
dump("health_ok.json", {
    "status": "ok", "max_context": 262144,
    "model": "qwen3.8-flash-next-unsloth-ud-q4_k_xl",
    "images": True, "api_key": False, "loaded": True, "service": "strata",
})
dump("health_loading.json", {
    "status": "loading", "max_context": 262144,
    "model": None, "images": None, "api_key": False, "loaded": False,
    "service": "strata",
})
# llama.cpp 风格 /health（无 service 字段——识别必须区分）
dump("health_llama_style.json", {"status": "ok"})

# ---- /slots（真实形状）----
dump("slots.json", [
    {"id": 0, "n_ctx": 262144, "is_processing": False, "n_prompt_tokens": 200903},
    {"id": 1, "n_ctx": 262144, "is_processing": False, "n_prompt_tokens": 0},
])
dump("slots_busy.json", [
    {"id": 0, "n_ctx": 262144, "is_processing": True, "n_prompt_tokens": 57},
    {"id": 1, "n_ctx": 262144, "is_processing": False, "n_prompt_tokens": 0},
])

# ---- /props（白名单字段；chat_template 保留一个小占位以验证不进内存快照）----
dump("props.json", {
    "default_generation_settings": {"n_ctx": 262144, "temperature": 0.7},
    "total_slots": 2,
    "model_alias": "qwen3.8-flash-next-unsloth-ud-q4_k_xl",
    "chat_template": "{{ bos }}\n{%- for m in messages %}...（真实数据中约 4KB 的 Jinja 模板）...",
    "chat_template_caps": {"tools": True, "thinking": True, "cache": True},
    "modalities": {"vision": True},
    "models_autoload": True,
    "is_sleeping": False,
    "model_path": "C:\\Users\\mixiaobu\\Models\\qwen\\qwen3.8-flash-next-unsloth-ud-q4_k_xl\\qwen3.8.gguf",
    "build_info": "Strata 0.1.43.1",
})
dump("props_sleeping.json", {
    "total_slots": 2, "model_alias": "qwen3.8-flash-next-unsloth-ud-q4_k_xl",
    "modalities": {"vision": True}, "is_sleeping": True,
    "model_path": "C:\\Users\\mixiaobu\\Models\\qwen\\q4.gguf",
    "build_info": "Strata 0.1.43.1",
})

# ---- /v1/models（OpenAI 风格列表）----
dump("models.json", {
    "object": "list",
    "data": [{
        "id": "qwen3.8-flash-next-unsloth-ud-q4_k_xl",
        "object": "model",
        "status": {"value": "loaded"},
        "meta": {"n_ctx": 262144},
        "architecture": {
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
        },
    }],
})

# ---- §20 强制数学验证数据（spec 原始整数：累计快照）----
dump("totals_spec_window.json", {
    "since": 1791967755.668746,
    "requests": 42,
    "prompt_tokens": 2111583,
    "reused": 2032318,
    "output_tokens": 5759,
    "prompt_ms": 160561.89999999997,
    "decode_ms": 712919.00000000003,
    "drafts_offered": 27307,
    "drafts_accepted": 17204,
})

print("done:", sorted(p.name for p in OUT.iterdir()))
