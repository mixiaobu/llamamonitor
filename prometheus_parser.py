"""
prometheus_parser.py — Prometheus 文本格式解析（1.3.2，纯函数，无 I/O）

用途：解析 Strata 的两套 Prometheus 端点
- GET /metrics?format=prometheus（vLLM 兼容 + Strata 专属 + 延迟 Histogram）
- GET /metrics/prometheus（llamacpp: 命名兼容，交叉验证用）

支持：
- HELP / TYPE / 标签 / le；
- Counter / Gauge / Histogram（_bucket / _sum / _count）；
- 无效数字、NaN/Inf、Bucket 非单调、Counter Reset（delta 计算层处理）。

**不要把所有 Prometheus 指标统一当 Counter**——类型由 TYPE 行决定；
无 TYPE 行时按系列后缀推断（_bucket -> histogram，否则 gauge）。

解析结果是"家族 -> 序列"的纯数据映射，时间维度（delta/窗口/分位数）
由 histogram_quantile 模块与调用方处理。
"""

from __future__ import annotations

import math
import re
from typing import Any

__all__ = [
    "parse_prometheus",
    "parse_histogram_series",
    "histogram_bucket_delta",
    "bucket_interval_deltas",
    "histogram_quantile",
    "HistogramSeries",
    "le_sort_key",
]


def le_sort_key(le_raw: str) -> tuple:
    """
    le 标签排序键：有限 le 按数值升序，"+Inf"（或无法解析的）排最后。
    与 sorted_le / histogram_quantile 的桶序一致。
    """
    if le_raw == "+Inf":
        return (1, 0.0)
    try:
        return (0, float(le_raw))
    except (ValueError, TypeError):
        return (1, 0.0)

# 数值行：metric_name{label="v",...} value [timestamp]
_LINE_RE = re.compile(
    r'^(\S+?)(?:\{([^}]*)\})?\s+(.+?)(?:\s+(-?\d+))?\s*$'
)
_LABEL_RE = re.compile(r'(\w+)="([^"\\]*(?:\\.[^"\\]*)*)"')
_TYPE_RE = re.compile(r'^TYPE\s+(\S+)\s+(\w+)\s*$')
_HELP_RE = re.compile(r'^HELP\s+(\S+)\s*(.*?)\s*$')


def _parse_labels(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    out: dict[str, str] = {}
    for m in _LABEL_RE.finditer(raw):
        k = m.group(1)
        v = m.group(2).replace('\\"', '"').replace("\\\\", "\\").replace("\\n", "\n")
        out[k] = v
    return out


def _to_float(text: str) -> tuple[float | None, str]:
    """
    解析数值字段。返回 (value, kind)：
    kind = "ok" | "nan" | "inf" | "ninf" | "bad"
    """
    t = text.strip()
    if t == "+Inf" or t == "Inf":
        return math.inf, "inf"
    if t == "-Inf":
        return -math.inf, "ninf"
    if t == "NaN":
        return math.nan, "nan"
    try:
        f = float(t)
    except ValueError:
        return None, "bad"
    if math.isnan(f):
        return math.nan, "nan"
    if math.isinf(f):
        return (math.inf if f > 0 else -math.inf), "inf" if f > 0 else "ninf"
    return f, "ok"


def parse_prometheus(text: str) -> dict[str, dict[str, Any]]:
    """
    解析 Prometheus 文本，返回：

    {
      "family_name": {
          "type": "counter" | "gauge" | "histogram" | "untyped",
          "help": str | None,
          "series": {
              "<labels_json_sorted>": {
                  "labels": {k: v},
                  "value": float | None,      # gauge/counter 的当前值
                  "buckets": {le_str: count} # histogram：_bucket 行
                  "sum": float | None,       # histogram：_sum
                  "count": float | None,     # histogram：_count
              },
          },
      },
    }

    规则：
    - 同一 family 下 gauge/counter 序列按 (labels) 归并；
    - histogram 的 _bucket/_sum/_count 按 (labels 去掉 le) 归并为同一序列；
    - le="+Inf" 保留为键 "+Inf"；
    - 非单调 bucket（某个 le 的累计值 < 更小的 le）：按前值钳制（取 max），
      不丢弃整个序列；
    - 坏数字行的 value=None（保留标签，便于诊断），不抛异常；
    - 空行 / # 注释按 Prometheus 规范处理。
    """
    families: dict[str, dict[str, Any]] = {}
    type_hints: dict[str, str] = {}
    help_texts: dict[str, str] = {}

    def _fam(name: str) -> dict[str, Any]:
        fam = families.get(name)
        if fam is None:
            fam = {
                "type": type_hints.get(name, "untyped"),
                "help": help_texts.get(name),
                "series": {},
            }
            families[name] = fam
        return fam

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            m = _TYPE_RE.match(line.lstrip("#").strip())
            if m:
                type_hints[m.group(1)] = m.group(2)
                if m.group(1) in families:
                    families[m.group(1)]["type"] = m.group(2)
                continue
            m = _HELP_RE.match(line.lstrip("#").strip())
            if m:
                help_texts[m.group(1)] = m.group(2) or None
                if m.group(1) in families:
                    families[m.group(1)]["help"] = m.group(2) or None
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        name, raw_labels, value_text = m.group(1), m.group(2), m.group(3)
        base = name
        is_bucket = name.endswith("_bucket")
        is_sum = name.endswith("_sum") and not is_bucket
        is_count = name.endswith("_count") and not is_bucket
        if is_bucket:
            base = name[: -len("_bucket")]
        elif is_sum:
            base = name[: -len("_sum")]
        elif is_count:
            base = name[: -len("_count")]

        value, kind = _to_float(value_text)
        labels = _parse_labels(raw_labels)

        fam = _fam(base)
        if fam["type"] == "untyped" and base in type_hints:
            fam["type"] = type_hints[base]

        if is_bucket or is_sum or is_count:
            # histogram 系列：labels 去掉 le 后作为序列键
            le = labels.pop("le", None)
            key = _series_key(labels)
            ser = fam["series"].get(key)
            if ser is None:
                ser = {"labels": labels, "value": None,
                       "buckets": {}, "sum": None, "count": None}
                fam["series"][key] = ser
            if is_bucket:
                if le is not None and value is not None:
                    ser["buckets"][le] = value
            elif is_sum:
                if value is not None and kind != "nan":
                    ser["sum"] = value
            else:
                if value is not None and kind != "nan":
                    ser["count"] = value
            fam["type"] = fam["type"] if fam["type"] != "untyped" else "histogram"
        else:
            key = _series_key(labels)
            fam["series"][key] = {
                "labels": labels,
                "value": value if kind != "nan" else None,
                "buckets": {},
                "sum": None,
                "count": None,
            }
    return families


def _series_key(labels: dict[str, str]) -> str:
    return ",".join(f"{k}={labels[k]}" for k in sorted(labels))


def parse_histogram_series(fam: dict[str, Any]) -> list["HistogramSeries"]:
    """从家族解析结果提取全部 histogram 序列（非单调 bucket 已钳制）。"""
    out: list[HistogramSeries] = []
    for _key, ser in fam.get("series", {}).items():
        buckets = ser.get("buckets") or {}
        if not buckets:
            continue
        out.append(HistogramSeries(
            labels=dict(ser.get("labels") or {}),
            buckets=buckets,
            sum=ser.get("sum"),
            count=ser.get("count"),
        ))
    return out


class HistogramSeries:
    """一个 histogram 序列（单 model/来源）的累计 bucket 视图。"""

    __slots__ = ("labels", "buckets", "sum", "count")

    def __init__(self, labels: dict[str, str], buckets: dict[str, float],
                 sum: float | None, count: float | None) -> None:
        self.labels = labels
        self.buckets = buckets
        self.sum = sum
        self.count = count

    def sorted_le(self) -> list[tuple[float, str, float]]:
        """
        [(le_float, le_raw, cumulative_count)] 按 le 升序；
        非单调（某桶累计值小于前桶）按前值钳制；"+Inf" 排最后。
        """
        rows: list[tuple[float, str, float]] = []
        for le_raw, count in self.buckets.items():
            if count is None or (isinstance(count, float) and math.isnan(count)):
                continue
            if le_raw == "+Inf":
                le_f = math.inf
            else:
                try:
                    le_f = float(le_raw)
                except ValueError:
                    continue
            rows.append((le_f, le_raw, float(count)))
        rows.sort(key=lambda r: (r[0] == math.inf, r[0]))
        # 非单调钳制
        clamped: list[tuple[float, str, float]] = []
        prev = -math.inf
        for le_f, le_raw, count in rows:
            if count < prev:
                count = prev
            clamped.append((le_f, le_raw, count))
            prev = count
        return clamped


def histogram_bucket_delta(
    prev: dict[str, float] | None,
    curr: dict[str, float],
) -> dict[str, float]:
    """
    两个累计 bucket 快照之间按 le 求 delta（Counter Reset 感知）：

    - prev 为 None（首次采样）：只建立基线，全部 delta = 0；
    - curr[le] >= prev[le]：delta = curr - prev；
    - curr[le] <  prev[le]：该序列 Counter Reset（新运行周期），delta = curr[le]
      （保守：假设旧周期贡献不可恢复，只计新周期累计）；
    - curr 缺某 le（版本变化）：delta = 0（不猜）。
    返回 {le_raw: delta}（只含 curr 中存在的 le）。
    """
    if prev is None:
        return {le: 0.0 for le in curr}
    out: dict[str, float] = {}
    for le, count in curr.items():
        if count is None or (isinstance(count, float) and math.isnan(count)):
            continue
        base = prev.get(le)
        if base is None:
            out[le] = 0.0
        elif count < base:
            out[le] = float(count)  # reset：新周期从 0 开始累计
        else:
            out[le] = float(count - base)
    return out


def bucket_interval_deltas(le_deltas: dict[str, float]) -> dict[str, float]:
    """
    把 histogram_bucket_delta 的**每 le 累计差**转换为**每桶区间 delta**
    （histogram_quantile 的输入形态）：

        interval[le_i] = cum_delta[le_i] - cum_delta[le_{i-1}]

    - 输入必须是单调 le 的累计差（reset 已按 per-le 处理）；
    - +Inf 桶 = 超出最后一个有限桶的样本数（通常 0）；
    - 区间值可能因非单调数据出现微小负值——quantile 层按 max(0, ...) 处理。
    """
    rows: list[tuple[float, str, float]] = []
    for le, d in (le_deltas or {}).items():
        v = float(d) if d is not None else 0.0
        if le == "+Inf":
            rows.append((math.inf, le, v))
        else:
            try:
                rows.append((float(le), le, v))
            except ValueError:
                continue
    rows.sort(key=lambda r: r[0])
    out: dict[str, float] = {}
    prev = 0.0
    for _le_f, le_raw, d in rows:
        out[le_raw] = d - prev
        prev = d
    return out


def histogram_quantile(
    q: float,
    bucket_deltas: dict[str, float],
    total_count: float | None = None,
) -> float | None:
    """
    与 Prometheus histogram_quantile 等价的经典 Histogram 分位数估计
    （线性插值）。输入 bucket_deltas 是**窗口内每桶的 delta 样本数**
    （由相邻采样累计值相减得到，非累计值）：{le_raw: 落入该桶的样本数}。

    - 只使用有限 le 的桶（+Inf 桶无上限，只能证明"样本存在"）；
    - total_count 缺省时用有限桶累计和（+Inf 增量不计入分母——
      无法为其估计数值位置）；
    - total_count <= 0 -> None（没有请求的窗口**不是** 0ms）；
    - 目标落在第一个桶内：lower=0；
    - 目标落在最后一个有限桶之后（全部样本在 +Inf）：返回最后一个有限 le
      （保守下界）。
    """
    if not (0.0 < q < 1.0):
        return None
    rows: list[tuple[float, float]] = []  # (le, cumulative)
    prev = 0.0
    for le_raw, delta in sorted(
        bucket_deltas.items(),
        key=lambda kv: (kv[0] == "+Inf", float(kv[0]) if kv[0] != "+Inf" else math.inf),
    ):
        if le_raw == "+Inf":
            continue
        try:
            le_f = float(le_raw)
        except ValueError:
            continue
        # 区间 delta 按 max(0, ...) 钳制（非单调/重置的微小负值不产生负样本）
        if delta is None:
            continue
        prev += max(0.0, float(delta))
        rows.append((le_f, prev))
    if not rows or rows[-1][1] <= 0:
        return None
    denom = float(total_count) if total_count is not None else rows[-1][1]
    if denom <= 0:
        return None
    target = q * denom
    # 全部样本都在 +Inf 桶
    if target > rows[-1][1] + 1e-9:
        return rows[-1][0]
    lower = 0.0
    lower_cum = 0.0
    for le_f, cum in rows:
        if cum >= target - 1e-9:
            upper = le_f
            if cum == lower_cum:
                # 单桶内集中了目标样本：区间 [lower, upper] 内均匀假设
                return (lower + upper) / 2.0
            frac = (target - lower_cum) / (cum - lower_cum)
            return lower + (upper - lower) * frac
        lower = le_f
        lower_cum = cum
    return rows[-1][0]
