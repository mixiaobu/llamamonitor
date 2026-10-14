"""
runtime_provider.py — Runtime Provider 识别层（1.3.0）

一个 LlamaMonitor 实例监控一个推理服务；用户可在 llama.cpp 与 Strata 间
切换（设置 → 连接 → 推理后端：自动检测 / llama.cpp / Strata）。

识别规则（§8/§9）——只用 GET，绝不调用任何控制接口：

1. GET /health（超时 + 64KB 响应上限）：
   - JSON 且 service == "strata"  ->  Strata（/health 同时给出
     model/loaded/max_context/images/api_key，一并返回给"测试连接"展示）；
   - 注意：不能仅凭 status=="ok" 判断后端（llama.cpp 的 /health 也是
     status:ok，但它是文本/JSON 无 service 字段）。
2. GET <metrics_path>：
   - Prometheus 文本且出现 llamacpp: 指标行 -> llama.cpp；
3. 都不匹配 -> unknown（友好报错，不交给错误 Parser）。

手动选择与实际不匹配时：detect 返回实际识别结果，调用方（测试连接 UI）
明确提示"当前服务识别为 X，与所选 Y 不一致"。
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from config import trust_env_for
from strata_parser import build_health_info, parse_metrics_json, server_identity

logger = logging.getLogger("llamamonitor.provider")

# Provider 标识（数据库来源身份 / 配置 / API 统一使用）
PROVIDER_AUTO = "auto"
PROVIDER_LLMACPP = "llamacpp"
PROVIDER_STRATA = "strata"
KNOWN_PROVIDERS = (PROVIDER_AUTO, PROVIDER_LLMACPP, PROVIDER_STRATA)

# 识别响应大小上限（防御性；/health 正常 <1KB）
_MAX_DETECT_BYTES = 64 * 1024
_MAX_METRICS_DETECT_BYTES = 512 * 1024

llamacpp = "llama.cpp"
strata = "Strata"

_DISPLAY = {PROVIDER_LLMACPP: llamacpp, PROVIDER_STRATA: strata}


class DetectionError(Exception):
    """识别失败（带面向用户的友好信息）。"""

    def __init__(self, message: str, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


async def detect_backend(
    base_url: str,
    metrics_path: str = "/metrics",
    timeout_seconds: float = 3.0,
    api_key: str | None = None,
) -> dict[str, Any]:
    """
    识别 base_url 背后的推理后端（只读 GET；§9 安全要求：超时 + 有限响应体 +
    不调用控制接口）。

    返回：
        {
          "provider": "llamacpp" | "strata" | "unknown",
          "provider_display": "llama.cpp" | "Strata" | None,
          "server_id": "host:port",
          "latency_ms": float,          # /health 探测耗时（不可达时 None）
          "strata": {...health 白名单...} | None,   # strata 时
          "llamacpp": {"metrics_detected": bool} | None,  # llamacpp 时
          "error": str | None,          # unknown 时友好信息
        }
    """
    result: dict[str, Any] = {
        "provider": "unknown", "provider_display": None,
        "server_id": server_identity(base_url),
        "latency_ms": None, "strata": None, "llamacpp": None, "error": None,
    }
    base = (base_url or "").rstrip("/")
    if not base.startswith(("http://", "https://")):
        result["error"] = "无效的推理服务地址（需要 http:// 或 https:// 开头）"
        return result

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    import time
    t0 = time.perf_counter()
    seen_status: list[int] = []

    async def _get_text(client, url: str, limit: int):
        """GET -> (text, status)。超限/失败 -> (None, status|None)。用 .text
        （真实 httpx 与测试替身都提供）。超时/连接拒绝向上抛（友好错误分类）。"""
        try:
            r = await client.get(url)
        except (httpx.TimeoutException, httpx.ConnectError):
            raise
        except Exception:
            return None, None
        try:
            seen_status.append(int(r.status_code))
        except Exception:
            pass
        if int(r.status_code) != 200:
            return None, int(r.status_code)
        text = getattr(r, "text", None)
        if text is None:
            raw = r.content
            if len(raw) > limit:
                return None, 200
            text = raw.decode("utf-8", "replace")
        if len(text) > limit:
            return None, 200
        return text, 200

    try:
        async with httpx.AsyncClient(
            timeout=float(timeout_seconds), follow_redirects=True,
            headers=headers, trust_env=trust_env_for(base),
        ) as client:
            # 1) /health：Strata 识别（service == "strata"）
            health_payload: dict | None = None
            health_text, _h_status = await _get_text(client, base + "/health", _MAX_DETECT_BYTES)
            if health_text is not None:
                try:
                    payload = json.loads(health_text)
                except Exception:
                    payload = None
                if isinstance(payload, dict):
                    health_payload = payload

            if (
                isinstance(health_payload, dict)
                and str(health_payload.get("service", "")).strip().lower() == "strata"
            ):
                result["provider"] = PROVIDER_STRATA
                result["strata"] = build_health_info(health_payload)
            else:
                # 2) /metrics：llama.cpp 识别（Prometheus 文本 + llamacpp: 前缀）
                detected = False
                path = (metrics_path or "/metrics").lstrip("/")
                metrics_text, _m_status = await _get_text(client, base + "/" + path, _MAX_METRICS_DETECT_BYTES)
                if metrics_text is not None:
                    for line in metrics_text.splitlines():
                        if line.strip().startswith("llamacpp:"):
                            detected = True
                            break
                if detected:
                    result["provider"] = PROVIDER_LLMACPP
                    result["llamacpp"] = {"metrics_detected": True}
                elif health_payload is not None:
                    # /health 可达但两边都不匹配：llama.cpp 的 /health 也返回
                    # status:ok——此时看 /metrics 是否是 Prometheus（无 llamacpp:
                    # 前缀的罕见构建）
                    if metrics_text is not None and any(
                        line.startswith("# TYPE") for line in metrics_text.splitlines()
                    ):
                        result["provider"] = PROVIDER_LLMACPP
                        result["llamacpp"] = {"metrics_detected": True}
                    else:
                        result["error"] = (
                            "服务可达，但未识别出 llama.cpp 或 Strata 特征"
                            "（/health 无 service 字段，/metrics 无 llamacpp: 指标）"
                        )
                elif metrics_text is None and health_text is None and not seen_status:
                    result["error"] = "服务不可达（/health 与 /metrics 均无响应）"
                else:
                    # 有 HTTP 响应但都非 200：报告实际状态码
                    result["error"] = f"服务响应异常（HTTP {seen_status[-1] if seen_status else '?'}）"

            result["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            result["provider_display"] = _DISPLAY.get(result["provider"])
            if seen_status:
                result["http_status"] = seen_status[-1]
            return result
    except httpx.TimeoutException:
        result["error"] = f"连接超时（{timeout_seconds:g}s）"
    except httpx.ConnectError:
        result["error"] = "连接被拒绝（服务未启动或端口不正确）"
    except Exception as exc:
        result["error"] = str(exc) or exc.__class__.__name__
        logger.debug("detect_backend 异常: %r", exc)
    return result


async def verify_manual_provider(
    base_url: str,
    provider: str,
    metrics_path: str = "/metrics",
    timeout_seconds: float = 3.0,
    api_key: str | None = None,
) -> dict[str, Any]:
    """
    手动选择后端时的校验（§9）：识别实际后端，与用户选择对比。

    返回 detect_backend 结果 + {"mismatch": bool, "mismatch_detail": str|None}。
    """
    result = await detect_backend(
        base_url, metrics_path=metrics_path,
        timeout_seconds=timeout_seconds, api_key=api_key,
    )
    if provider not in (PROVIDER_LLMACPP, PROVIDER_STRATA):
        result["mismatch"] = False
        result["mismatch_detail"] = None
        return result
    actual = result["provider"]
    if actual == "unknown":
        result["mismatch"] = True
        result["mismatch_detail"] = (
            f"选择了 {_DISPLAY.get(provider, provider)}，但未能识别出该后端的特征"
            f"（{result.get('error')}）"
        )
    elif actual != provider:
        result["mismatch"] = True
        result["mismatch_detail"] = (
            f"当前服务识别为 {_DISPLAY.get(actual, actual)}，"
            f"与所选 {_DISPLAY.get(provider, provider)} 不一致——"
            f"请确认推理服务地址或改为自动检测"
        )
    else:
        result["mismatch"] = False
        result["mismatch_detail"] = None
    return result


def resolve_provider(configured: str | None, detected: dict | None) -> str:
    """
    启动时解析实际 Provider：
    - 配置为具体后端 -> 用配置值（即使识别不一致也按用户选择采集，
      识别结果用于 UI 提示——手动选择是用户意图）；
    - 配置 auto/缺失 -> 用识别结果（识别失败 -> llama.cpp 默认兼容路径，
      与升级前行为一致：llama.cpp 是原始后端）。
    """
    configured = (configured or PROVIDER_AUTO).strip().lower()
    if configured in (PROVIDER_LLMACPP, PROVIDER_STRATA):
        return configured
    if isinstance(detected, dict) and detected.get("provider") in (
        PROVIDER_LLMACPP, PROVIDER_STRATA
    ):
        return detected["provider"]
    return PROVIDER_LLMACPP
