"""
strata_parser.py — Strata JSON 监控数据的解析与规范化（1.3.0，纯函数，无 I/O）

Strata 与 llama.cpp 的关键差异（语义核对自真实 Strata 0.1.43.1 响应）：

1. Token 口径（最重要的差异）：
   - llama.cpp  `llamacpp:prompt_tokens_total` = 实际处理（不含缓存复用）的 Prompt；
     `llamacpp:prompt_tokens_cached_total` = 缓存复用部分；
   - Strata     `totals.prompt_tokens` = **包含缓存复用**的 Prompt 总量；
     `totals.reused` = 缓存复用部分；`totals.output_tokens` = 生成。
   两者不能直接映射成同一个原始计数器。规范化公式（与 daily_usage 列语义对齐）：

       new_prompt   = prompt_tokens - reused     （实际计算 Prompt）
       cached       = reused
       generated    = output_tokens
       logical      = prompt_tokens + output_tokens
       compute      = new_prompt + generated

2. 隐私边界：
   - /status 的 `tail`（模型正在生成的实际文本）、`tool` 在生成期间才出现，
     一律不进入规范化结构、不落库、不打日志；
   - /props 的 `chat_template`（巨型 Jinja 模板）、`model_path`（完整本地路径）
     不进内存快照（model_path 例外：仅内存保留，API 层按只读策略脱敏，
     与 llama.cpp 的 _expose_path 同一策略）。

3. 有效性约束（violated -> 本轮样本无效，保持旧 baseline，绝不裁剪成 0）：
   prompt_tokens >= 0；reused >= 0；output_tokens >= 0；reused <= prompt_tokens。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

__all__ = [
    "STRATA_COUNTERS",
    "STRATA_EPOCH_KEY",
    "server_identity",
    "counter_state_key",
    "parse_metrics_json",
    "validate_totals",
    "totals_baseline_values",
    "convert_totals_snapshot",
    "normalize_totals_sample",
    "build_live_state",
    "build_engine_info",
    "build_conversation_cache",
    "build_recent_requests",
    "build_model_info",
    "build_health_info",
    "map_phase",
    "map_finish",
    "strata_capabilities",
    "STRATA_CAPABILITY_BASE",
]

# Strata 累计 Counter（totals.*）——每个独立做 reset detection（与 stats.py 同规则）
STRATA_COUNTERS = {
    "totals.prompt_tokens": "prompt_total",
    "totals.reused": "reused",
    "totals.output_tokens": "output",
    "totals.drafts_offered": "drafts_offered",
    "totals.drafts_accepted": "drafts_accepted",
    # 时间（浮点，秒；由 *_ms 换算）
    "totals.prompt_ms": "prompt_ms",
    "totals.decode_ms": "decode_ms",
}

# 累计周期识别字段：totals.since（累计窗口起点 epoch 秒）。
# since 变化（或 Counter 在同一 since 内下降）=> 新的统计周期（服务重启/重置）。
STRATA_EPOCH_KEY = "totals.since"

# 能力状态值（1.3.0 Capability 模型：不仅布尔，还能表达"暂时不可用"）
CAP_SUPPORTED = "supported"
CAP_UNSUPPORTED = "unsupported"
CAP_TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
CAP_UNKNOWN = "unknown"

# Strata 静态能力基线（运行时按端点可达性/字段存在性进一步降级为
# temporarily_unavailable；llama.cpp 对应能力见 collector.detect_capabilities）。
# 说明：
# - prompt_throughput：Strata 提供 prefill_tok_s_mean（**平均** Prefill 吞吐），
#   以及窗口口径（Δprompt/Δprompt_ms）；没有"当前 Prompt TPS"瞬时值；
# - draft_position_statistics：当前版本无 per-position 分母/分位置接受数 -> 不支持
#   （不伪造分布图）；
# - request_history / conversation_cache / backend_hardware：Strata 专属能力。
STRATA_CAPABILITY_BASE: dict[str, str] = {
    "usage_counters": CAP_SUPPORTED,
    "prompt_cache": CAP_SUPPORTED,
    "live_status": CAP_SUPPORTED,
    "live_throughput": CAP_SUPPORTED,          # live.tok_s（当前生成吞吐）
    "decode_throughput": CAP_SUPPORTED,        # live.tok_s / tok_s_mean
    "prompt_throughput": CAP_SUPPORTED,        # prefill_tok_s_mean（平均）+ 窗口口径
    "requests_processing": CAP_SUPPORTED,      # live.running / outside_slots
    "requests_waiting": CAP_SUPPORTED,         # live.waiting / queued
    "request_history": CAP_SUPPORTED,          # metrics.requests[]（内存态）
    "slots": CAP_SUPPORTED,
    "context": CAP_SUPPORTED,
    "model_metadata": CAP_SUPPORTED,
    "multimodal": CAP_SUPPORTED,               # /health.images + /v1/models modalities
    "speculative_decoding": CAP_SUPPORTED,     # drafts_offered/accepted
    "mtp": CAP_SUPPORTED,                      # Draft Token 接受率（来源可能非 MTP，UI 标注）
    "draft_position_statistics": CAP_UNSUPPORTED,  # 无 per-position 统计
    "conversation_cache": CAP_SUPPORTED,       # metrics.conversation_cache
    "engine_diagnostics": CAP_SUPPORTED,       # metrics.engine + /props
    "backend_hardware": CAP_SUPPORTED,         # metrics.hardware（Strata 主机）
}

# phase 显示映射（核对自真实 Strata 0.1.43.1：观察到的取值
# "reading the prompt" / "thinking" / "answering"；"generating" 为防御性映射）
_PHASE_LABELS = {
    "reading the prompt": "Prompt 处理",
    "prefill": "Prompt 处理",
    "thinking": "思考中",
    "answering": "生成回答",
    "generating": "生成中",
    "decode": "生成中",
    "idle": None,
    "": None,
}

# 请求结束原因映射（未知值安全回退为原值）
_FINISH_LABELS = {
    "stop": "正常结束",
    "cancel": "已取消",
    "cancelled": "已取消",
    "length": "达到长度上限",
    "timeout": "超时",
    "error": "错误",
}

# /metrics requests[] 每个请求保留的白名单字段（数字/状态/耗时；正文类字段绝不进）
_REQUEST_ALLOWED = {
    "time", "duration_s", "finish",
    "prompt_tokens", "prompt_total", "prompt_read", "reused",
    "output_tokens",
    "prompt_ms", "decode_ms", "decode_tok_s",
    "drafts_offered", "drafts_accepted",
    "reasoning_recoveries",
}

# /metrics engine 白名单（Strata 引擎/运行配置；全部为配置/状态，无请求正文）
_ENGINE_ALLOWED = {
    "model", "version", "engine", "max_context", "context", "images",
    "kv", "kv_resident", "logit_bias", "cvec", "arena_mib", "vram_free_mib",
    "vram_elastic", "expert_slots", "expert_slots_primary",
    "expert_cache_mib", "expert_cache_primary_mib",
    "spec", "spec_min_p", "mtp_max", "lookup",
    "pool_workers", "pcie_frac",
    "conversation_cache_mib", "conversation_cache_slots",
    "conversation_cache_min_free_mib", "conversation_cache_min_tokens",
    "batch_slots", "batch_groups", "slot_cache", "tail_role_token",
}

# conversation_cache 白名单（计数器/状态；last_event 等字符串短字段可保留）
_CONVERSATION_CACHE_ALLOWED = {
    "enabled", "budget_mib", "slots", "parked", "bytes",
    "evictions", "parks", "restores",
    "last_event", "last_tokens", "last_at",
    "requests", "requests_reused", "reused_tokens", "prompt_tokens",
    "last_reused", "last_prompt",
}

# /metrics hardware 白名单（Strata 主机实时硬件——远程监控摘要用）
_HARDWARE_ALLOWED = {
    "gpu_util", "gpu_mem_used", "gpu_mem_total", "gpu_temp",
    "gpu_power", "gpu_power_limit", "gpu_pcie_gen", "gpu_pcie_gen_max",
    "gpu_pcie_width", "gpu_pcie_rx_mb", "gpu_pcie_tx_mb",
    "cpu", "ram_used", "ram_total", "disk_read_mb", "disk_write_mb",
    "tok_s", "tok_s_mean", "prefill_tok_s_mean",
}
_HARDWARE_STATIC_ALLOWED = {
    "gpu_name", "gpu_count", "gpu_note", "cpu_name", "cores", "threads",
}


def _num(value: Any) -> float | None:
    """数值净化：None/NaN/Inf/负值/非数字 -> None；合法值 -> float。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _num_nonneg(value: Any) -> float | None:
    f = _num(value)
    if f is None or f < 0:
        return None
    return f


def server_identity(url: str) -> str:
    """
    来源身份：从服务 URL 归一化出 "host:port"（Counter Baseline 的隔离键之一）。

    缺省端口按 scheme 补齐（http 80 / https 443）；解析失败返回原字符串截断，
    绝不抛异常。同一台机器的两个不同端口、同一端口的两次不同 Provider 监控
    都得到不同/可区分的身份。
    """
    from urllib.parse import urlparse
    try:
        p = urlparse(url or "")
        host = p.hostname or ""
        if not host:
            return (url or "").strip()[:64]
        port = p.port
        if port is None:
            port = 443 if p.scheme == "https" else 80
        return f"{host}:{port}"
    except Exception:
        return (url or "").strip()[:64]


def counter_state_key(server_id: str, counter: str) -> str:
    """
    state 表的来源化 Counter 键：`strata:{server_id}:{counter}`。

    与 llama.cpp 的裸指标全名天然隔离——即使地址相同，两个 Provider 的
    Counter 也绝不混用 baseline。
    """
    return f"strata:{server_id}:{counter}"


def parse_metrics_json(text: str) -> dict | None:
    """
    解析 Strata /metrics 的 JSON 响应。

    - 非 dict（数组/标量/坏 JSON）-> None（按本轮不可用处理，不抛异常）；
    - 顶层缺少 totals/live 时仍返回 dict（调用方按字段缺失处理）。
    """
    try:
        data = json.loads(text)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def validate_totals(totals: dict | None) -> bool:
    """
    Token 口径有效性约束（§21）：

        prompt_tokens >= 0；reused >= 0；output_tokens >= 0；reused <= prompt_tokens

    三个核心字段任一缺失（None）按"本轮缺失"处理——返回 False（不更新 baseline），
    调用方保持最后一次有效基线。违反约束的数据绝不静默裁剪为 0 后继续写库。
    """
    if not isinstance(totals, dict):
        return False
    prompt = _num_nonneg(totals.get("prompt_tokens"))
    reused = _num_nonneg(totals.get("reused"))
    output = _num_nonneg(totals.get("output_tokens"))
    if prompt is None or reused is None or output is None:
        return False
    if reused > prompt:
        return False
    return True


def _get_totals_value(totals: dict, counter: str) -> float | None:
    """取 totals 下某个 Counter 的合法非负值（缺失/坏值 -> None）。"""
    if counter in ("totals.prompt_ms", "totals.decode_ms"):
        ms = _num(totals.get(counter.split(".", 1)[1]))
        return ms / 1000.0 if ms is not None and ms >= 0 else None
    return _num_nonneg(totals.get(counter.split(".", 1)[1]))


def totals_baseline_values(totals: dict, epoch: Any) -> dict[str, float]:
    """由一次合法 totals 构建 baseline dict（{counter: value}，含 since epoch）。"""
    values: dict[str, float] = {}
    for counter in STRATA_COUNTERS:
        v = _get_totals_value(totals, counter)
        if v is not None:
            values[counter] = v
    if _num(epoch) is not None:
        values[STRATA_EPOCH_KEY] = _num(epoch)  # type: ignore[arg-type]
    return values


def convert_totals_snapshot(totals: dict | None) -> dict | None:
    """
    累计 totals 快照 -> 统一 Token 口径换算（§18-20 展示术语的单一计算源）。

    spec 强制验证（必须用原始整数，先加后减，不允许浮点近似）：
        Prompt Total 2,111,583 / Reused 2,032,318 / Generated 5,759
        -> Prompt 79,265 / cached 2,032,318 / generated 5,759
        -> total 2,117,342 / compute 85,024 / rate ≈ 96.25%

    输入是**累计值**（非 delta）；无效（约束违反/缺失）-> None。
    """
    if not validate_totals(totals):
        return None
    prompt = int(totals["prompt_tokens"])
    reused = int(totals["reused"])
    output = int(totals["output_tokens"])
    new_prompt = prompt - reused
    return {
        "new_prompt": new_prompt,                    # 实际计算 Prompt
        "cached": reused,                            # 缓存复用 Prompt
        "generated": output,                         # 生成 Token
        "logical": prompt + output,                  # Token 总量
        "compute": new_prompt + output,              # 实际计算 Token
        "reuse_rate": (reused / prompt) if prompt > 0 else None,  # Prompt 缓存复用率
    }


def normalize_totals_sample(
    totals: dict | None,
    previous_state: dict[str, float],
) -> dict | None:
    """
    规范化一轮 Strata totals 样本（对应 llama.cpp 的 compute_deltas + 口径换算）。

    返回 None 表示本轮无效（约束违反 / 核心字段缺失）——调用方保持旧 baseline。
    成功返回：

    {
        "epoch_changed": bool,        # totals.since 变化 => 新累计周期（服务重启）
        "counters": {counter: value}, # 本轮要保存的 baseline（合法字段）
        "deltas": {
            "prompt_total": float|None,  # Δ prompt_tokens（含复用）
            "reused": float|None,        # Δ reused
            "new_prompt": float|None,    # Δ prompt_total - Δ reused（实际计算 Prompt）
            "output": float|None,        # Δ output_tokens
            "logical": float|None,       # Δ prompt_total + Δ output（Token 总量）
            "compute": float|None,       # Δ new_prompt + Δ output（实际计算 Token）
            "drafts_offered": float|None,
            "drafts_accepted": float|None,
            "prompt_seconds": float|None,  # Δ prompt_ms / 1000
            "decode_seconds": float|None,  # Δ decode_ms / 1000
        },
        "cache_reuse_rate": float|None,  # reused / prompt_tokens（窗口口径，0~1）
        "reset_metrics": [counter, ...], # 本轮判定 reset 的 Counter
    }

    规则（与 stats.counter_delta 一致，按来源隔离）：
    - previous 中无该 Counter（首次 / 换来源）：只建 baseline，delta = 0；
    - epoch 变化：所有 Counter 视为新周期——只建 baseline（delta = 0）+
      epoch_changed=True（调用方记 strata_instance_changed 事件）；
    - current < previous（同 epoch 内下降）：reset，delta = current。
    """
    if not validate_totals(totals):
        return None

    raw_epoch = totals.get("since")
    cur_epoch = _num(raw_epoch)
    prev_epoch = previous_state.get(STRATA_EPOCH_KEY)
    epoch_changed = (
        cur_epoch is not None and prev_epoch is not None and cur_epoch != prev_epoch
    )
    # since 首次出现（prev 为 None）不算"变化"——首次连接建基线即可

    new_state: dict[str, float] = {}
    deltas: dict[str, float | None] = {}
    reset_metrics: list[str] = []

    for counter, _short in STRATA_COUNTERS.items():
        current = _get_totals_value(totals, counter)
        if current is None:
            continue  # 缺失：保持旧 baseline
        previous = previous_state.get(counter)
        if previous is None or epoch_changed:
            delta = 0.0  # 首次 / 新周期：只建 baseline
        elif current < previous:
            delta = current  # Counter 下降：判定重启，delta = current
            reset_metrics.append(counter)
        else:
            delta = current - previous
        new_state[counter] = current
        if cur_epoch is not None:
            new_state[STRATA_EPOCH_KEY] = cur_epoch
        deltas[counter] = delta

    d = {k: deltas.get(k) for k in
         ("totals.prompt_tokens", "totals.reused", "totals.output_tokens",
          "totals.drafts_offered", "totals.drafts_accepted",
          "totals.prompt_ms", "totals.decode_ms")}
    prompt_total_d = d["totals.prompt_tokens"]
    reused_d = d["totals.reused"]
    output_d = d["totals.output_tokens"]

    new_prompt_d = None
    if prompt_total_d is not None and reused_d is not None:
        # 约束保证 reused <= prompt_tokens（累计），delta 层面 new_prompt 可能为
        # 极小负值（同一轮内 reused 增量略大于 prompt 增量——统计延迟），
        # 允许保留负值进 live 展示，但**每日累加**时按 max(0, ...) 由调用方处理
        new_prompt_d = prompt_total_d - reused_d

    logical_d = None
    if prompt_total_d is not None and output_d is not None:
        logical_d = prompt_total_d + output_d
    compute_d = None
    if new_prompt_d is not None and output_d is not None:
        compute_d = new_prompt_d + output_d

    # 窗口缓存复用率：本轮 Δ reused / Δ prompt_total（分母 <=0 -> None）
    cache_reuse_rate = None
    if prompt_total_d is not None and reused_d is not None and prompt_total_d > 0:
        cache_reuse_rate = max(0.0, min(1.0, reused_d / prompt_total_d))

    return {
        "epoch_changed": bool(epoch_changed),
        "counters": new_state,
        "deltas": {
            "prompt_total": prompt_total_d,
            "reused": reused_d,
            "new_prompt": new_prompt_d,
            "output": output_d,
            "logical": logical_d,
            "compute": compute_d,
            "drafts_offered": d["totals.drafts_offered"],
            "drafts_accepted": d["totals.drafts_accepted"],
            "prompt_seconds": d["totals.prompt_ms"],
            "decode_seconds": d["totals.decode_ms"],
        },
        "cache_reuse_rate": cache_reuse_rate,
        "reset_metrics": reset_metrics,
    }


def map_phase(phase: Any) -> str | None:
    """
    Strata phase 的稳定显示映射（§35）。未知 phase 不崩溃：
    返回安全格式化（截断 + 去首尾空白）的原始值。
    """
    if phase is None:
        return None
    text = str(phase).strip()
    if not text:
        return None
    known = _PHASE_LABELS.get(text)
    if known is not None:
        return known
    return text[:40]  # 未知阶段：原值安全展示（截断防超长）


def map_finish(finish: Any) -> str | None:
    """请求结束原因映射（未知值安全回退为原始字符串）。"""
    if finish is None:
        return None
    text = str(finish).strip()
    if not text:
        return None
    return _FINISH_LABELS.get(text, text[:40])


def build_live_state(
    metrics: dict | None, status: dict | None,
) -> dict | None:
    """
    由 /metrics.live + /status 构建规范化 Live 状态（§33/§36/§37）。

    运行状态权威来源（不单独依赖 /slots.is_processing）：
        busy = status.busy OR live.running > 0 OR live.outside_slots > 0

    隐私边界：status.tail / status.tool 绝不进入返回结构。
    metrics 与 status 都不可用时返回 None。
    """
    live = metrics.get("live") if isinstance(metrics, dict) else None
    if not isinstance(live, dict) and not isinstance(status, dict):
        return None

    def _lv(key: str) -> Any:
        return live.get(key) if isinstance(live, dict) else None

    running = _num_nonneg(_lv("running")) or 0.0
    outside = _num_nonneg(_lv("outside_slots")) or 0.0
    waiting = _num_nonneg(_lv("waiting")) or 0.0
    queued = _num_nonneg(status.get("queued")) if isinstance(status, dict) else 0.0
    queued = queued or 0.0
    status_busy = bool(status.get("busy")) if isinstance(status, dict) else False
    busy = status_busy or running > 0 or outside > 0

    # 当前请求进度（live 为权威；status 兜底；生成中才可能有值）
    prompt_tokens = _num_nonneg(_lv("prompt_tokens"))
    if prompt_tokens is None and isinstance(status, dict):
        prompt_tokens = _num_nonneg(status.get("prompt_tokens"))
    generated = _num_nonneg(_lv("generated"))
    if generated is None and isinstance(status, dict):
        generated = _num_nonneg(status.get("generated"))
    max_tokens = _num_nonneg(_lv("max_tokens"))
    if max_tokens is None and isinstance(status, dict):
        max_tokens = _num_nonneg(status.get("max_tokens"))
    elapsed_s = _num(_lv("elapsed_s"))
    if elapsed_s is None and isinstance(status, dict):
        elapsed_s = _num(status.get("elapsed_s"))

    # Slot 快照（/metrics.live.slots 权威；白名单字段）
    slots = []
    raw_slots = _lv("slots")
    if isinstance(raw_slots, list):
        for s in raw_slots:
            if not isinstance(s, dict):
                continue
            slots.append({
                "id": s.get("slot", s.get("id")),
                "state": s.get("state"),
                "held_tokens": _num_nonneg(s.get("held_tokens")),
            })
            if len(slots) >= 64:
                break

    return {
        "state": _lv("state"),           # idle / reading / generating（原始值）
        "phase": _lv("phase") or (status.get("phase") if isinstance(status, dict) else None),
        "phase_label": None,             # 由 UI 层用 map_phase 渲染（保持 parser 纯数据）
        "busy": busy,
        "running": int(running),
        "waiting": int(waiting),
        "queued": int(queued),
        "outside_slots": int(outside),
        "parallel": _num(_lv("parallel")),
        "admitting": int(_num_nonneg(_lv("admitting")) or 0),
        # 吞吐（§40 区分）：tok_s=当前生成吞吐；tok_s_mean=会话平均生成吞吐；
        # prefill_tok_s_mean=平均 Prefill 吞吐（不是"当前 Prompt TPS"）
        "tok_s": _num(_lv("tok_s")),
        "tok_s_mean": _num(_lv("tok_s_mean")),
        "prefill_tok_s_mean": _num(_lv("prefill_tok_s_mean")),
        "tok_s_window_s": _num(_lv("tok_s_window_s")),
        # 当前请求进度（§39；held 上下文比例需先确认字段语义，这里只给原始值）
        "prompt_tokens": prompt_tokens,
        "generated": generated,
        "max_tokens": max_tokens,
        "elapsed_s": elapsed_s,
        "reasoning_recoveries": _num_nonneg(status.get("reasoning_recoveries"))
        if isinstance(status, dict) else None,
        "slots": slots,
    }


def build_engine_info(metrics: dict | None, props: dict | None) -> dict | None:
    """
    引擎/运行配置信息（§60/§61）。白名单合并 /metrics.engine + /props；
    chat_template / model_path 绝不进入（model_path 走 model_info 单独脱敏路径）。
    两个源都不可用时返回 None。
    """
    out: dict[str, Any] = {}
    eng = metrics.get("engine") if isinstance(metrics, dict) else None
    if isinstance(eng, dict):
        for k, v in eng.items():
            if k in _ENGINE_ALLOWED:
                out[k] = v
    if isinstance(props, dict):
        # props 补充 /metrics.engine 没有的字段（不覆盖已有值：engine 是运行时权威）
        if out.get("model") is None and isinstance(props.get("model_alias"), str):
            out["model"] = props.get("model_alias")
        bi = props.get("build_info")
        if out.get("version") is None and isinstance(bi, str) and bi:
            out["version"] = bi
        total_slots = props.get("total_slots")
        if out.get("batch_slots") is None and _num_nonneg(total_slots) is not None:
            out["batch_slots"] = _num_nonneg(total_slots)
        modal = props.get("modalities")
        if isinstance(modal, dict):
            out["modalities"] = {k: v for k, v in modal.items() if isinstance(v, bool)}
        caps = props.get("chat_template_caps")
        if isinstance(caps, dict):
            out["template_caps"] = {
                k: v for k, v in caps.items() if isinstance(v, bool)
            }
    return out or None


def build_conversation_cache(metrics: dict | None) -> dict | None:
    """
    会话缓存（§55-§59）。白名单字段 + enabled 标志。

    注意（§58）：conversation_cache 与 Prompt 缓存复用不是同一概念——
    enabled=False 时 reused_tokens 仍可能有值（历史累计），调用方必须显示
    "会话缓存未启用"，不能因为 reused_tokens > 0 就判为已启用。
    """
    cc = metrics.get("conversation_cache") if isinstance(metrics, dict) else None
    if not isinstance(cc, dict):
        return None
    out: dict[str, Any] = {
        "enabled": bool(cc.get("enabled")),
    }
    for k, v in cc.items():
        if k in _CONVERSATION_CACHE_ALLOWED:
            out[k] = v
    return out


def build_recent_requests(metrics: dict | None, limit: int = 20) -> list[dict]:
    """
    最近请求（§49-§54）。来自 /metrics.requests[]（内存态，不落库）。

    每个请求输出（不可靠字段不显示 -> 键缺失/None）：
    - time / duration_s / finish（+ finish_label）
    - prompt_total / new_prompt / reused / output_tokens
    - prompt_ms / decode_ms / decode_tok_s
    - drafts_offered / drafts_accepted / draft_accept_rate
    - prompt_reuse_rate（reused / prompt_total，0~1；分母 0 -> None）

    隐私边界：projection / ram_blobs / file_blobs / file_mb / pcie_share /
    hit_rate（原始字段名易被误读为"缓存命中率"，改由 prompt_reuse_rate 明确表达）/
    reasoning_repeat_coverage / engine_generated（与 output_tokens 同源冗余）不进。
    """
    requests = metrics.get("requests") if isinstance(metrics, dict) else None
    if not isinstance(requests, list):
        return []
    out: list[dict] = []
    for raw in requests[: max(0, int(limit))]:
        if not isinstance(raw, dict):
            continue
        entry: dict[str, Any] = {
            k: raw.get(k) for k in _REQUEST_ALLOWED if k in raw
        }
        finish = raw.get("finish")
        entry["finish_label"] = map_finish(finish)
        # 数值净化
        for k in ("time", "duration_s", "prompt_total", "prompt_read", "reused",
                  "output_tokens", "prompt_ms", "decode_ms", "decode_tok_s",
                  "drafts_offered", "drafts_accepted", "reasoning_recoveries"):
            entry[k] = _num(entry.get(k)) if k in entry else None
        prompt_total = entry.get("prompt_total")
        if prompt_total is None:
            prompt_total = entry.get("prompt_tokens")
        reused = entry.get("reused")
        new_prompt = None
        if prompt_total is not None and reused is not None:
            new_prompt = max(0.0, prompt_total - reused)
        elif entry.get("prompt_read") is not None:
            new_prompt = entry.get("prompt_read")
        entry["new_prompt"] = new_prompt
        entry["prompt_reuse_rate"] = (
            max(0.0, min(1.0, reused / prompt_total))
            if prompt_total not in (None, 0) and reused is not None and prompt_total > 0
            else None
        )
        offered = entry.get("drafts_offered")
        accepted = entry.get("drafts_accepted")
        entry["draft_accept_rate"] = (
            max(0.0, min(1.0, accepted / offered))
            if offered not in (None, 0) and accepted is not None and offered > 0
            else None
        )
        out.append(entry)
    return out


def build_model_info(
    health: dict | None, models: dict | None, props: dict | None,
) -> dict | None:
    """
    模型元数据（§15）。合并 /health + /v1/models + /props（白名单）：

    model / model_loaded / context_window / vision_supported /
    input_modalities / output_modalities / total_slots / is_sleeping /
    build_info / model_file_name（完整路径只进内存，API 层脱敏）。

    chat_template 绝不进入。全部源不可用时返回 None。
    """
    info: dict[str, Any] = {
        "model": None, "model_loaded": None, "context_window": None,
        "vision_supported": None, "input_modalities": None,
        "output_modalities": None, "total_slots": None, "is_sleeping": None,
        "build_info": None, "model_file_name": None, "api_key_required": None,
    }
    have = False
    if isinstance(health, dict):
        have = True
        if isinstance(health.get("model"), str):
            info["model"] = health.get("model")
        if health.get("loaded") is not None:
            info["model_loaded"] = bool(health.get("loaded"))
        if health.get("max_context") is not None:
            info["context_window"] = _num_nonneg(health.get("max_context"))
        if health.get("images") is not None:
            info["vision_supported"] = bool(health.get("images"))
        if health.get("api_key") is not None:
            info["api_key_required"] = bool(health.get("api_key"))
    if isinstance(models, dict):
        data = models.get("data")
        if isinstance(data, list):
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                if info["model"] is None and isinstance(entry.get("id"), str):
                    info["model"] = entry["id"]
                status = entry.get("status")
                if isinstance(status, dict) and status.get("value") is not None:
                    info["model_loaded"] = str(status.get("value")) == "loaded"
                meta = entry.get("meta")
                if isinstance(meta, dict) and meta.get("n_ctx") is not None:
                    if info["context_window"] is None:
                        info["context_window"] = _num_nonneg(meta.get("n_ctx"))
                arch = entry.get("architecture")
                if isinstance(arch, dict):
                    if isinstance(arch.get("input_modalities"), list):
                        info["input_modalities"] = list(arch["input_modalities"])
                    if isinstance(arch.get("output_modalities"), list):
                        info["output_modalities"] = list(arch["output_modalities"])
                have = True
    if isinstance(props, dict):
        if info["model"] is None and isinstance(props.get("model_alias"), str):
            info["model"] = props.get("model_alias")
        if info["context_window"] is None:
            dgs = props.get("default_generation_settings")
            if isinstance(dgs, dict) and dgs.get("n_ctx") is not None:
                info["context_window"] = _num_nonneg(dgs.get("n_ctx"))
        if info["total_slots"] is None:
            info["total_slots"] = _num_nonneg(props.get("total_slots"))
        if info["is_sleeping"] is None and props.get("is_sleeping") is not None:
            info["is_sleeping"] = bool(props.get("is_sleeping"))
        if info["build_info"] is None and isinstance(props.get("build_info"), str):
            info["build_info"] = props.get("build_info")
        mp = props.get("model_path")
        if isinstance(mp, str) and mp:
            info["model_file_name"] = Path(mp.replace("\\", "/")).name
        have = True
    return info if have else None


def build_health_info(health: dict | None) -> dict | None:
    """/health 白名单快照（§12；识别 + 模型加载状态 + 模态能力）。"""
    if not isinstance(health, dict):
        return None
    out: dict[str, Any] = {}
    for k in ("status", "service", "model", "loaded", "images", "api_key"):
        if health.get(k) is not None:
            out[k] = health[k]
    if health.get("max_context") is not None:
        out["max_context"] = _num_nonneg(health.get("max_context"))
    return out or None


def strata_capabilities(
    endpoint_ok: dict[str, bool] | None,
    metrics: dict | None,
) -> dict[str, str]:
    """
    运行时能力状态（§5）：静态基线 + 端点可达性/字段存在性降级。

    endpoint_ok: {"health": bool, "metrics": bool, "status": bool, "slots": bool,
                  "props": bool, "models": bool}——None 时全部按 unknown 处理。
    状态值：supported / unsupported / temporarily_unavailable / unknown。
    """
    caps = dict(STRATA_CAPABILITY_BASE)
    ep = endpoint_ok or {}

    def _degrade(*keys: str, ok: bool | None, unknown: bool = False) -> None:
        for k in keys:
            if caps.get(k) == CAP_SUPPORTED:
                caps[k] = (
                    CAP_UNKNOWN if (unknown and ok is None)
                    else CAP_TEMPORARILY_UNAVAILABLE if ok is False
                    else caps[k]
                )

    _degrade("live_status", "live_throughput", "decode_throughput",
             "prompt_throughput", "requests_processing", "requests_waiting",
             ok=ep.get("metrics"), unknown=True)
    _degrade("usage_counters", ok=ep.get("metrics"), unknown=True)
    if ep.get("metrics") is False:
        # /metrics 不可用时 request_history/conversation_cache/engine/hardware
        # 数据源都在该端点 -> 暂时不可用
        for k in ("request_history", "conversation_cache", "engine_diagnostics",
                  "backend_hardware", "speculative_decoding", "mtp"):
            if caps.get(k) == CAP_SUPPORTED:
                caps[k] = CAP_TEMPORARILY_UNAVAILABLE
    _degrade("slots", ok=ep.get("slots"), unknown=True)
    _degrade("model_metadata", ok=ep.get("models") or ep.get("props"), unknown=True)
    _degrade("context", ok=ep.get("health"), unknown=True)
    if ep.get("health") is False:
        caps["context"] = CAP_TEMPORARILY_UNAVAILABLE
    # 字段级降级：metrics 可读但缺某段
    if ep.get("metrics") and isinstance(metrics, dict):
        if not isinstance(metrics.get("requests"), list):
            caps["request_history"] = CAP_TEMPORARILY_UNAVAILABLE
        if not isinstance(metrics.get("conversation_cache"), dict):
            caps["conversation_cache"] = CAP_TEMPORARILY_UNAVAILABLE
        if not isinstance(metrics.get("engine"), dict):
            caps["engine_diagnostics"] = CAP_TEMPORARILY_UNAVAILABLE
        if not isinstance(metrics.get("hardware"), dict):
            caps["backend_hardware"] = CAP_TEMPORARILY_UNAVAILABLE
        if not isinstance(metrics.get("totals"), dict):
            caps["usage_counters"] = CAP_TEMPORARILY_UNAVAILABLE
    return caps
