"""
metrics_parser.py — llama.cpp Prometheus Metrics 文本解析器（Phase 1）

llama-server 的 /metrics 端点输出 Prometheus 文本暴露格式，例如：

    # HELP llamacpp:prompt_tokens_total Total prompt tokens processed
    # TYPE llamacpp:prompt_tokens_total counter
    llamacpp:prompt_tokens_total 168507
    llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="0"} 14039

设计要点：
- 逐行解析、纯字符串处理，不用正则硬写整个 Prometheus 文本格式；
- 跳过空行与 # HELP / # TYPE 注释行；
- 支持带冒号的指标名（llama.cpp 的 llamacpp:xxx）；
- 支持标签序列 {key="value",...}，值内支持 \" 转义；
- 支持行尾可选的 Unix 时间戳（只取名称后的第一个 token 作为数值）；
- 支持特殊值 +Inf / -Inf / NaN；
- 畸形行（无数值、花括号未闭合、非法数字）一律静默跳过，绝不抛异常。
"""

from __future__ import annotations

__all__ = ["parse_metrics", "get_metric_value", "get_metric_by_label"]


def _parse_value(token: str) -> float | None:
    """把数值 token 转为 float；非法值返回 None（调用方跳过该行）。"""
    lowered = token.strip().lower()
    if lowered == "+inf":
        return float("inf")
    if lowered == "-inf":
        return float("-inf")
    if lowered == "nan":
        return float("nan")
    try:
        return float(token)
    except ValueError:
        return None


def _parse_labels(text: str) -> dict[str, str] | None:
    """
    解析形如 'position="0",foo="bar"' 的标签文本。

    - 成功 -> dict（空文本返回 {}）
    - 失败 -> None（调用方跳过该行）
    支持值内的 \\" 转义。
    """
    labels: dict[str, str] = {}
    i, n = 0, len(text)
    while i < n:
        # 跳过空白与逗号
        while i < n and (text[i] == "," or text[i].isspace()):
            i += 1
        if i >= n:
            break
        # 读取标签 key（到 '=' 为止，key 必须非空）
        start = i
        while i < n and text[i] not in '="':
            i += 1
        key = text[start:i].strip()
        if not key or i >= n or text[i] != "=":
            return None
        i += 1  # 跳过 '='
        # 读取带引号的 value
        if i >= n or text[i] != '"':
            return None
        i += 1
        start = i
        while i < n and text[i] != '"':
            if text[i] == "\\" and i + 1 < n:
                i += 1  # 转义字符按字面保留
            i += 1
        if i >= n:
            return None  # 引号未闭合
        labels[key] = text[start:i]
        i += 1  # 跳过结尾引号
    return labels


def _split_metric_line(line: str):
    """
    切分单条指标行，返回 (name, labels, value_token)；畸形行返回 None。

    - 'llamacpp:x 123'                -> ('llamacpp:x', {}, '123')
    - 'llamacpp:y{a="1"} 123 1690000' -> ('llamacpp:y', {'a': '1'}, '123')
      （行尾时间戳 token 被忽略，只取第一个数值 token）
    """
    brace = line.find("{")
    if brace == -1:
        # 无标签序列：名称到第一个空白结束
        sep = line.find(" ")
        if sep == -1:
            sep = line.find("\t")
        if sep == -1:
            return None  # 没有数值部分
        name, rest = line[:sep], line[sep:]
        labels = {}
    else:
        name = line[:brace]
        close = line.find("}", brace)
        if close == -1:
            return None  # 花括号未闭合
        labels = _parse_labels(line[brace + 1:close])
        if labels is None:
            return None
        rest = line[close + 1:]
    name = name.strip()
    tokens = rest.split()
    if not name or not tokens:
        return None
    return name, labels, tokens[0]


def parse_metrics(text: str) -> dict[str, dict[tuple[tuple[str, str], ...], float]]:
    """
    解析完整 Prometheus 文本，返回 {metric_name: {labels_key: value}}。

    - labels_key 为按标签名排序的 (label_key, label_value) 元组；无标签指标为空元组；
    - 同名同标签出现多行时，最后一行生效；
    - 注释行 / 空行 / 畸形行一律跳过，本函数不抛异常。
    """
    result: dict[str, dict[tuple[tuple[str, str], ...], float]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue  # 空行或 HELP/TYPE 注释
        split = _split_metric_line(line)
        if split is None:
            continue
        name, labels, value_token = split
        value = _parse_value(value_token)
        if value is None:
            continue
        result.setdefault(name, {})[tuple(sorted(labels.items()))] = value
    return result


def get_metric_value(
    parsed: dict, name: str, labels: dict[str, str] | None = None
) -> float | None:
    """
    从 parse_metrics 结果中取单个指标行的值；缺失返回 None。

    - labels=None：只匹配无标签行（不会误匹配带标签的行）；
    - labels={"position": "0"}：匹配该标签集合的行（标签顺序无关）。
    """
    labels_key = () if labels is None else tuple(sorted(labels.items()))
    series = parsed.get(name)
    if not series:
        return None
    return series.get(labels_key)


def get_metric_by_label(
    parsed: dict, name: str, label_name: str
) -> dict[str, float] | None:
    """
    按指定标签名对带标签指标分组，返回 {label_value: value}。

    - 指标不存在 -> None
    - 指标存在但没有该标签的行 -> {}
    """
    series = parsed.get(name)
    if series is None:
        return None
    out: dict[str, float] = {}
    for labels_key, value in series.items():
        for key, val in labels_key:
            if key == label_name:
                out[val] = value
                break
    return out
