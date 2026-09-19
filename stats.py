"""
stats.py — Counter delta、reset 检测、TPS 与 MTP 接受率计算（纯函数，无 I/O，Phase 2）。

核心规则（llama-server 重启会让 Prometheus Counter 从 0 重新开始）：
- previous 为 None（首次见到该 Counter）：仅建 baseline，delta = 0
- current 为 None（本轮该字段缺失）：delta = None（后续展示 N/A）
- current >= previous：delta = current - previous
- current < previous：判定 llama-server 重启，delta = current

每个 Counter 独立做 reset detection，互不干扰。
"""

from __future__ import annotations

__all__ = [
    "TRACKED_COUNTERS",
    "COUNTER_SHORT_NAMES",
    "counter_delta",
    "compute_deltas",
    "tps",
    "mtp_accept_rate",
]

# 跟踪的 Counter（llama.cpp 指标全名），每个独立做 reset detection
TRACKED_COUNTERS = (
    "llamacpp:prompt_tokens_total",
    "llamacpp:prompt_tokens_cached_total",
    "llamacpp:prompt_seconds_total",
    "llamacpp:tokens_predicted_total",
    "llamacpp:tokens_predicted_seconds_total",
    "llamacpp:n_decode_total",
    "llamacpp:spec_decode_num_draft_tokens_total",
    "llamacpp:spec_decode_num_accepted_tokens_total",
    "llamacpp:spec_decode_num_drafts_total",  # Phase 9：Draft Sequences（推测解码序列数）
)

# 全名 -> 短名（与 UI / 数据库字段保持一致）
COUNTER_SHORT_NAMES = {
    "llamacpp:prompt_tokens_total": "prompt_tokens",
    "llamacpp:prompt_tokens_cached_total": "cached_tokens",
    "llamacpp:prompt_seconds_total": "prompt_seconds",
    "llamacpp:tokens_predicted_total": "output_tokens",
    "llamacpp:tokens_predicted_seconds_total": "predicted_seconds",
    "llamacpp:n_decode_total": "n_decode",
    "llamacpp:spec_decode_num_draft_tokens_total": "draft_tokens",
    "llamacpp:spec_decode_num_accepted_tokens_total": "accepted_tokens",
    "llamacpp:spec_decode_num_drafts_total": "draft_sequences",
}


def counter_delta(current, previous):
    """
    计算单个 Counter 的 delta（独立 reset 检测）。

    返回 float；current 为 None 时返回 None。
    规则见模块 docstring。
    """
    if current is None:
        return None
    if previous is None:
        return 0.0  # 首次见到该 Counter：仅建 baseline，不把已有值当作历史数据
    if current >= previous:
        return current - previous
    return current  # Counter 变小：判定 llama-server 重启，delta = current


def compute_deltas(current_values, previous_state):
    """
    对所有跟踪的 Counter 一次性计算 delta。

    - current_values: {指标全名: float | None}（本轮解析结果）
    - previous_state: {指标全名: float}（state 表中的上一次值）
    返回 {短名: delta | None}
    """
    result = {}
    for name in TRACKED_COUNTERS:
        result[COUNTER_SHORT_NAMES[name]] = counter_delta(
            current_values.get(name), previous_state.get(name)
        )
    return result


def tps(token_delta, seconds_delta):
    """
    TPS = token delta / seconds delta。

    任一为 None，或 seconds_delta <= 0（含 baseline 的 0、字段缺失）时返回 None。
    """
    if token_delta is None or seconds_delta is None or seconds_delta <= 0:
        return None
    return token_delta / seconds_delta


def mtp_accept_rate(accepted_delta, draft_delta):
    """
    MTP/spec-decode 接受率(%) = accepted_delta / draft_delta * 100。

    draft_delta 为 0 或 None 时返回 None。
    """
    if accepted_delta is None or not draft_delta:
        return None
    return accepted_delta / draft_delta * 100.0
