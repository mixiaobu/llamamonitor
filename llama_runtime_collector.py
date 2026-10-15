"""
llama_runtime_collector.py — llama.cpp Runtime 只读采集器（1.1.0 Stage A）

在现有 /metrics Token 采集（collector.py）之外，补全 llama.cpp 的
Health / Model / Slot 运行时遥测。严格只读，绝不 POST 任何控制接口
（不 POST /props、不 POST /models/load|unload、不调推理 API）。

只读取（GET）：
- /health       建立准确的 Server State（Ready / Loading / Sleeping /
                Auth Error / Degraded / Unreachable）——不能仅凭 HTTP 端口
                可达就显示"就绪"；503 + "Loading model" 是加载态而非不可达；
- /slots        Active Slot 运行时（白名单字段：状态/计数/采样参数；
                next_token 兼容对象与数组两种形态；
                绝不读取/保存 prompt 文本）；
- /props        模型与服务元数据（白名单字段；is_sleeping 官方字段；
                chat_template / generation_prompt / media_marker /
                完整 model_path 一律不进数据库、不进日志、不进普通前端 API）；
- /v1/models    模型 metadata（n_params / size / n_ctx_train / n_embd /
                n_vocab / ftype 等）。

采样周期（分频调度，不是所有端点每 2 秒）：
- /health   10 秒；metrics 失败时允许立即检查；
- /slots    is_processing=true 时 2 秒，空闲 10 秒；
- /props    启动 / 重连 / 每 10 分钟最多一次；
- /v1/models 启动 / 重连 / 模型变化时。

能力探测（Capability Detection）：不同 llama.cpp 版本可能没有 /slots
（501）或 /props 的某些字段——缺失按"该功能不可用"处理，绝不让整个
推理性能页报错。

隐私边界（Privacy Gate）：
- /slots 只读数字/状态/采样参数；prompt / generation_prompt / chat_template /
  system/user/assistant 消息 / tool 内容一律不落库、不打日志、不进 API；
- 模型完整路径只在内存中保留（供本机 Diagnostics），普通 API 远程只读
  只暴露文件名（与 server._expose_path 同一策略）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx

from clock import Clock, default_clock
from config import AppConfig, trust_env_for
from gap_tracker import OFFLINE_TOLERANCE

logger = logging.getLogger("llamamonitor.runtime")

# ---- 采样周期（秒；config 可覆盖，见 config.SystemConfig 无关，此处为固定策略） ----
HEALTH_INTERVAL = 10.0          # /health：10 秒（metrics 失败时立即）
SLOTS_INTERVAL_ACTIVE = 2.0     # /slots：is_processing=true
SLOTS_INTERVAL_IDLE = 10.0      # /slots：空闲
PROPS_INTERVAL = 600.0          # /props：启动/重连/每 10 分钟最多一次
# /v1/models：启动/重连/模型变化时（无固定周期）
# 1.3.2：Router /models 列表 + /lora-adapters 低频刷新
ROUTER_MODELS_INTERVAL = 30.0   # /models（Router 多模型列表；30 秒）
LORA_INTERVAL = 300.0           # /lora-adapters（只读 LoRA 状态；5 分钟）
# 1.3.2：Router SSE（/models/sse）——单条受管连接
SSE_RECONNECT_BASE = 1.0        # 重连退避起步（秒）
SSE_RECONNECT_MAX = 30.0        # 重连退避上限（秒）
SSE_MAX_EVENTS = 100            # 最近事件环形缓冲长度（内存态）

# /props 白名单：只有这些键进入 LlamaModelInfo（chat_template 等不进内存快照）
_PROPS_ALLOWED_KEYS = {
    "model_alias", "model_ftype", "model_path", "total_slots", "modalities",
    "endpoint_slots", "endpoint_props", "endpoint_metrics", "ui",
}
# default_generation_settings 只取 n_ctx（上下文窗口上限——运行时配置值）
_PROPS_ALLOWED_NESTED = {"default_generation_settings": {"n_ctx"}}

# /v1/models 的 data[].meta 白名单（OpenAI 兼容列表；models[].details 同样字段）
_MODEL_META_ALLOWED = {
    "n_vocab", "n_ctx", "n_ctx_train", "n_embd", "n_params", "size", "ftype",
}

# /slots 每个 slot 的白名单（数字/状态/采样参数；prompt 文本一律丢弃）
_SLOT_ALLOWED_TOP = {
    "id", "n_ctx", "speculative", "is_processing", "id_task",
    "n_prompt_tokens", "n_prompt_tokens_processed", "n_prompt_tokens_cache",
    "n_decoded", "n_remaining", "is_stream", "has_next_token",
}
# 采样参数白名单（"常用"集合——不默认展示几十个内部参数）
_SLOT_PARAMS_ALLOWED = {
    "temperature", "top_p", "top_k", "min_p", "repeat_penalty",
    "max_tokens", "reasoning_format", "speculative.types", "seed",
}

# 响应大小上限（防御性；/props 含 chat_template 可能较大，1 MB 足够）
_MAX_RUNTIME_BYTES = 1 * 1024 * 1024
# /props 完整响应上限（chat_template 可能几 KB~几十 KB；1 MB 防御性上限）
_MAX_PROPS_BYTES = 1 * 1024 * 1024


def _whitelist(obj: Any, allowed: set) -> dict:
    """从 dict 中只保留白名单键（非 dict 返回 {}）。"""
    if not isinstance(obj, dict):
        return {}
    return {k: v for k, v in obj.items() if k in allowed}


def _num_or_none(value: Any) -> int | float | None:
    """数字净化：int/float 原样返回；数字字符串尝试转换；其它 -> None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            f = float(value)
            return int(f) if f.is_integer() else f
        except ValueError:
            return None
    return None


def sanitize_slot(raw: dict) -> dict:
    """
    由 /slots 的单个 slot 原始 JSON 构建白名单快照。

    丢弃：prompt / generation_prompt / chat_template / messages / tools 等
    一切文本字段（隐私边界）；只保留数字、状态与常用采样参数。
    任何畸形输入返回 {}（不抛异常）。
    """
    if not isinstance(raw, dict):
        return {}
    out = _whitelist(raw, _SLOT_ALLOWED_TOP)
    # 1.3.2 修正：next_token 兼容两种形态——
    #   对象 {has_next_token, has_new_line, n_remain, n_decoded}（当前官方形态）；
    #   数组（每位置一个 dict，旧版本形态）-> 取第一个位置的数字字段。
    nt = raw.get("next_token")
    if isinstance(nt, dict):
        out["next_token"] = {
            "has_next_token": bool(nt.get("has_next_token")),
            "has_new_line": bool(nt.get("has_new_line")),
            "n_remain": _num_or_none(nt.get("n_remain")),
            "n_decoded": _num_or_none(nt.get("n_decoded")),
        }
    elif isinstance(nt, list) and nt and isinstance(nt[0], dict):
        out["next_token"] = {
            "has_next_token": bool(nt[0].get("has_next_token")),
            "has_new_line": bool(nt[0].get("has_new_line")),
            "n_remain": _num_or_none(nt[0].get("n_remain")),
            "n_decoded": _num_or_none(nt[0].get("n_decoded")),
        }
    params = raw.get("params")
    if isinstance(params, dict):
        out["params"] = _whitelist(params, _SLOT_PARAMS_ALLOWED)
    return out


def build_model_info(
    props: dict | None, models: dict | None, build_info: str | None = None
) -> dict | None:
    """
    由 /props 与 /v1/models 的响应构建 LlamaModelInfo（白名单字段合并）。

    字段（缺失一律 None，绝不猜）：
    model_alias / model_file_name / model_ftype / parameter_count /
    model_size_bytes / context_size / training_context_size / embedding_size /
    vocab_size / total_slots / vision_supported / video_supported /
    audio_supported / build_info / is_sleeping / model_path（内存用，完整路径）。

    chat_template / generation_prompt / media_marker 绝不进入本结构。
    两个源都不可用时返回 None。
    """
    info: dict[str, Any] = {
        "model_alias": None, "model_file_name": None, "model_ftype": None,
        "parameter_count": None, "model_size_bytes": None,
        "context_size": None, "training_context_size": None,
        "embedding_size": None, "vocab_size": None, "total_slots": None,
        "vision_supported": None, "video_supported": None, "audio_supported": None,
        "build_info": build_info, "is_sleeping": None, "model_path": None,
    }
    have = False

    if isinstance(props, dict):
        have = True
        info["model_alias"] = props.get("model_alias")
        info["model_ftype"] = props.get("model_ftype")
        info["total_slots"] = props.get("total_slots")
        mp = props.get("model_path")
        if isinstance(mp, str) and mp:
            info["model_path"] = mp
            info["model_file_name"] = Path(mp.replace("\\", "/")).name
        modal = props.get("modalities")
        if isinstance(modal, dict):
            info["vision_supported"] = modal.get("vision")
            info["video_supported"] = modal.get("video")
            info["audio_supported"] = modal.get("audio")
        # 1.3.2 修正：官方字段是 is_sleeping（此前误读 sleeping，恒为 None）。
        # true / false / 缺失三者必须能区分——缺失保持 None（不猜）。
        if props.get("is_sleeping") is not None:
            info["is_sleeping"] = bool(props.get("is_sleeping"))
        dgs = props.get("default_generation_settings")
        if isinstance(dgs, dict) and dgs.get("n_ctx") is not None:
            info["context_size"] = dgs.get("n_ctx")
        # Round 5：build_info 权威来源 = /props（llama-server 运行时直接报告，
        # 如 "b10976-987498f45"）。此前只从 `llama-server --version` 子进程取，
        # PATH 无该 exe 时恒为 None -> UI 长期显示 "--"（§159-§161 bug）。
        # props 提供了就用它，覆盖上面的 --version 回退值。
        bi = props.get("build_info")
        if isinstance(bi, str) and bi:
            info["build_info"] = bi

    if isinstance(models, dict):
        # /v1/models：{"models": [...], "data": [{"id","aliases","meta":{...}}]}
        meta: dict = {}
        data = models.get("data")
        if isinstance(data, list):
            for entry in data:
                if isinstance(entry, dict) and isinstance(entry.get("meta"), dict):
                    m = _whitelist(entry["meta"], _MODEL_META_ALLOWED)
                    if m:
                        meta = m
                        break
        if not meta:
            # 部分构建 meta 在 models[].details（OpenAI 兼容列表）
            for entry in models.get("models") or []:
                if isinstance(entry, dict) and isinstance(entry.get("details"), dict):
                    m = _whitelist(entry["details"], _MODEL_META_ALLOWED)
                    if m:
                        meta = m
                        break
        if meta:
            have = True
            # 已有 props 值时不覆盖（props 是运行时权威值）
            for src, dst in (
                ("n_params", "parameter_count"), ("size", "model_size_bytes"),
                ("n_ctx", "context_size"), ("n_ctx_train", "training_context_size"),
                ("n_embd", "embedding_size"), ("n_vocab", "vocab_size"),
                ("ftype", "model_ftype"),
            ):
                if info[dst] is None and meta.get(src) is not None:
                    info[dst] = meta[src]
            if info["model_alias"] is None:
                for entry in models.get("models") or []:
                    if isinstance(entry, dict) and entry.get("name"):
                        info["model_alias"] = entry["name"]
                        break
    return info if have else None


def model_change_signature(info: dict | None) -> str:
    """模型变更判据（alias/file/quant/context 任一变化 -> model_changed 事件）。"""
    if info is None:
        return ""
    return json.dumps(
        [info.get("model_alias"), info.get("model_file_name"),
         info.get("model_ftype"), info.get("context_size")],
        ensure_ascii=False,
    )


def _no_window() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class LlamaRuntimeCollector:
    """
    llama.cpp Runtime 常驻采集器（独立于 metrics Token 采集；故障隔离）。

    状态（供 FastAPI /api/llama/* 与 /api/status 读取，全部内存态）：
    - server_state: "ready" | "loading" | "sleeping" | "auth_error" |
                    "degraded" | "unavailable"（/health 驱动）
    - model_info:   LlamaModelInfo dict 或 None
    - slots:        最近一次 /slots 白名单快照 list
    - capabilities: {"health": bool, "slots": bool, "props": bool, "models": bool}
    - 1.3.2: router: Router 多模型块（非 Router 为 None）
             router_events: Router SSE 最近事件（有界环形缓冲）
             lora_adapters: /lora-adapters 只读快照（basename，不含路径）

    - 单轮任何异常都不影响 metrics Token 采集（独立任务、独立 HTTP 客户端）；
    - 状态转换才写日志/事件（不刷屏）：llama_health_changed / model_changed；
    - 1.3.2：Router 模式下通过 on_router_model_change 回调通知 server 层
      （server 再调用 MetricsCollector.set_router_model 追加
      model=<id>&autoload=false 到 /metrics 查询——只读，绝不自动加载）。
    """

    def __init__(
        self,
        config: AppConfig,
        db=None,
        clock: Clock | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.clock = clock or default_clock
        self._http = http_client  # 外部注入（测试）；否则懒创建
        self._owns_http = http_client is None
        base = (config.llama_server.url or "").rstrip("/")
        self._urls = {
            "health": f"{base}/health",
            "slots": f"{base}/slots",
            "props": f"{base}/props",
            "models": f"{base}/v1/models",
            "router_models": f"{base}/models",       # 1.3.2：Router 模型列表
            "router_sse": f"{base}/models/sse",      # 1.3.2：Router 加载事件
            "lora": f"{base}/lora-adapters",         # 1.3.2：只读 LoRA 状态
        }
        self.timeout = config.llama_server.timeout_seconds
        # ---- 状态 ----
        self.server_state: str = "unavailable"   # ready / loading / unavailable
        self.model_info: dict | None = None
        self.slots: list[dict] = []
        self.capabilities: dict[str, bool] = {
            "health": False, "slots": False, "props": False, "models": False,
        }
        # ---- 1.3.2：Router 多模型 + LoRA ----
        self.router: dict | None = None          # None = 未检测到 Router
        self.router_events: deque[dict] = deque(maxlen=SSE_MAX_EVENTS)
        self.lora_adapters: list[dict] = []
        self.on_router_model_change: Callable[[str | None], None] | None = None
        self._last_selected_model: str | None = None
        self._last_router_mono: float = 0.0
        self._last_lora_mono: float = 0.0
        self._router_refresh_pending = False     # SSE 报告状态变化 -> 立即刷新列表
        # ---- 1.3.2：SSE 受管连接 ----
        self._sse_task: asyncio.Task | None = None
        self._sse_stop = False
        self._sse_connected = False
        self.last_health_update: float | None = None
        self.last_slots_update: float | None = None
        self._any_request_succeeded = False      # 端口可达过（HTTP 层）
        self._last_state: str | None = None
        self._last_model_sig: str | None = None
        self._last_props_mono: float = 0.0       # /props 节流
        self._last_models_mono: float = 0.0
        self._reconnect_pending = True           # 启动即视为"重连"
        self._metrics_online: bool | None = None  # 供 metrics 失败 -> 立即 health 检查
        self._task: asyncio.Task | None = None
        # AUDIT-1.1.1 REL-1111-005 + 1.3.1：/health 连续失败计数（去抖）。
        # 单次偶发网络抖动不应立即把 Server State 翻转成"不可用"（+ 记一条
        # warning 事件）——统一按 1.2.1 容忍语义：连续 5 次失败才判定，
        # 恢复一次即清零（与 Token 采集器 OFFLINE_TOLERANCE 一致）。
        self._health_fail_count: int = 0

    # ---- HTTP ----

    def _get_client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=self.timeout,
                trust_env=trust_env_for(self._urls["health"]),
            )
        return self._http

    async def _get_json(self, key: str, url: str | None = None) -> tuple[dict | list | None, bool]:
        """
        GET 一个 runtime 端点并解析 JSON。
        返回 (payload, ok)：ok=False 时 payload=None（网络错误/非 200/超大/坏 JSON）。
        url 可显式覆盖（1.3.2：Router 模式按模型查询）。
        任何失败都不抛异常。
        """
        try:
            client = self._get_client()
            response = await client.get(url or self._urls[key])
            if response.status_code != 200:
                # 501/404 = 端点不存在（能力探测）；4xx/5xx 都按不可用
                return None, False
            body = response.content
            limit = _MAX_PROPS_BYTES if key == "props" else _MAX_RUNTIME_BYTES
            if len(body) > limit:
                return None, False
            text = body.decode("utf-8", "replace")
            return json.loads(text), True
        except Exception:
            return None, False

    # ---- 采集轮 ----

    async def _get_health_raw(self) -> tuple[int | None, dict | None]:
        """
        GET /health（专门保留状态码）。返回 (status_code, payload)：
        网络层失败（连接拒绝/超时）-> (None, None)；
        非 200 也可能有 JSON body（如 503 的 error.message）。
        任何失败都不抛异常。
        """
        try:
            client = self._get_client()
            response = await client.get(self._urls["health"])
            body = response.content
            if len(body) > _MAX_RUNTIME_BYTES:
                return response.status_code, None
            try:
                payload = json.loads(body.decode("utf-8", "replace"))
            except Exception:
                payload = None
            return response.status_code, payload if isinstance(payload, dict) else None
        except Exception:
            return None, None

    async def fetch_health(self) -> None:
        """
        一轮 /health 检查 + Server State 判定（1.3.2 细化状态）：

        - 200 + status ok            -> ready
        - 200 + 其它 status          -> loading
        - 401                        -> auth_error（端口可达、鉴权失败）
        - 503 + "Loading model"      -> loading（服务启动/加载模型中，
                                        **不是**网络不可达——端口是可达的）
        - 503 其它 / 4xx / 5xx       -> degraded（端口可达但服务异常）
        - 网络层失败                 -> 连续 OFFLINE_TOLERANCE(5) 次才 unavailable
        """
        now = self.clock.now()
        code, payload = await self._get_health_raw()
        if code is None:
            # 1.3.1：连续 OFFLINE_TOLERANCE(5) 次失败才判定不可用（去抖，防
            # 单次偶发网络抖动把 Server State 翻成"不可用"+ 记 warning 事件；
            # 与 Token 采集器 / 前端 streak 阈值统一）。
            self._health_fail_count += 1
            if self._health_fail_count >= OFFLINE_TOLERANCE:
                self._set_state("unavailable", now)
            return
        # HTTP 层可达一次即清零网络失败计数
        self._health_fail_count = 0
        self.capabilities["health"] = True
        self._any_request_succeeded = True
        if code == 200:
            status = str(payload.get("status", "")).lower() if payload else ""
            self._set_state("ready" if status == "ok" else "loading", now)
        elif code == 401:
            self._set_state("auth_error", now)
        elif code == 503:
            msg = ""
            if isinstance(payload, dict):
                err = payload.get("error")
                if isinstance(err, dict):
                    msg = str(err.get("message", ""))
                elif err is not None:
                    msg = str(err)
            # "Loading model"（或等价文案）-> 模型加载中的明确信号
            self._set_state("loading" if "loading" in msg.lower() else "degraded", now)
        else:
            # 其它 4xx/5xx：端口可达但服务异常（能力降级，不是不可达）
            self._set_state("degraded", now)
        self.last_health_update = now

    async def fetch_slots(self) -> bool:
        """
        一轮 /slots 检查。返回本轮是否有 active（is_processing）slot
        （供调度选择下轮间隔 2s/10s）。
        """
        payload, ok = await self._get_json("slots")
        if not ok:
            # 501/404：该版本无 /slots -> 能力位保持 False；Runtime Slot 监控不可用
            return False
        self.capabilities["slots"] = True
        self._any_request_succeeded = True
        if isinstance(payload, list):
            self.slots = [sanitize_slot(s) for s in payload]
            self.last_slots_update = self.clock.now()
            return any(s.get("is_processing") for s in self.slots)
        self.slots = []
        return False

    async def fetch_props_and_models(self, force: bool = False) -> None:
        """
        一轮 /props + /v1/models（模型元数据）。构建 LlamaModelInfo；
        模型变更（alias/file/quant/context）记 model_changed 事件。
        force=False 时按 PROPS_INTERVAL 节流。
        """
        mono = self.clock.monotonic()
        if not force and (mono - self._last_props_mono) < PROPS_INTERVAL:
            return
        self._last_props_mono = mono
        # 1.3.2：Router 模式——/props 与 /v1/models 同样需要
        # model=<id>&autoload=false（只读，绝不自动加载）。
        props_url = None
        models_url = None
        if self.router and self.router.get("selected"):
            sel = self.router["selected"]
            props_url = self._router_model_query_url("props", sel)
            models_url = self._router_model_query_url("v1/models", sel)
        props, props_ok = await self._get_json("props", url=props_url)
        self.capabilities["props"] = bool(props_ok and isinstance(props, dict))
        models, models_ok = await self._get_json("models", url=models_url)
        self.capabilities["models"] = bool(models_ok and isinstance(models, dict))
        build_info = None
        if not self.model_info or self.model_info.get("build_info") is None:
            build_info = await asyncio.to_thread(fetch_build_info)
        info = build_model_info(
            props if props_ok else None,
            models if models_ok else None,
            build_info=build_info,
        )
        if info is not None:
            prev_sig = self._last_model_sig
            self.model_info = info
            sig = model_change_signature(info)
            self._last_model_sig = sig
            if prev_sig is not None and prev_sig != sig and self.db is not None:
                try:
                    self.db.record_event(
                        "model_changed", "info", "collector",
                        {
                            "model_alias": info.get("model_alias"),
                            "model_ftype": info.get("model_ftype"),
                            "context_size": info.get("context_size"),
                        },
                        now=self.clock.now(),
                    )
                except Exception as exc:
                    logger.warning("写入 model_changed 事件失败: %r", exc)
            # 1.3.2：Sleep 状态（props.is_sleeping 权威；只在显式值间最小转移，
            # 不覆盖 loading/unavailable/auth_error/degraded——它们有更明确的含义）
            sleeping = info.get("is_sleeping")
            if sleeping is True and self.server_state == "ready":
                self._set_state("sleeping", self.clock.now())
            elif sleeping is False and self.server_state == "sleeping":
                self._set_state("ready", self.clock.now())

    # ---- 1.3.2：Router 多模型 + LoRA + SSE（只读；故障隔离） ----

    def _router_model_query_url(self, path: str, model_id: str) -> str:
        """Router 的 /props 等按模型查询：?model=<urlencoded>&autoload=false。

        autoload=false 是只读保证——绝不触发模型自动加载（spec 硬约束）。
        """
        base = self._urls["props"]
        root = base.rsplit("/", 1)[0]
        sep = "&" if "?" in path else "?"
        return f"{root}/{path.lstrip('/')}?model={quote(model_id, safe='')}&autoload=false"

    async def fetch_router_models(self, force: bool = False) -> None:
        """
        一轮 Router /models 列表检测（30s 节流；SSE 报告变化时立即刷新）。

        判定：GET /models 返回**列表**且条目带 status 对象 -> Router 多模型；
        普通 llama-server 无此端点（404）或条目无 status -> 保持 router=None。
        被监控模型选择：第一个 status=loaded；没有 loaded 时保持上一次选择
        （若该模型仍在列表）；都没有 -> None（metrics 采集回退无参数 /metrics）。
        """
        mono = self.clock.monotonic()
        if not force and (mono - self._last_router_mono) < ROUTER_MODELS_INTERVAL:
            return
        self._last_router_mono = mono
        try:
            client = self._get_client()
            response = await client.get(self._urls["router_models"])
        except Exception:
            return
        if response.status_code != 200:
            return
        try:
            payload = json.loads(response.content.decode("utf-8", "replace"))
        except Exception:
            return
        if not isinstance(payload, list):
            return
        entries: list[dict] = []
        is_router = False
        for e in payload:
            if not isinstance(e, dict):
                continue
            status = e.get("status")
            if not isinstance(status, dict):
                continue  # 普通 server 的 /models 条目无 status -> 不是 Router
            is_router = True
            progress = status.get("progress")
            entries.append({
                "id": e.get("id"),
                "name": e.get("name") or e.get("id"),
                "status": status.get("value"),
                "loaded": status.get("value") == "loaded",
                # progress.value 是**阶段内** 0~1（不是整模型进度），只保留原值
                "progress_stage_index": progress.get("current")
                if isinstance(progress, dict) else None,
                "progress_stage_value": progress.get("value")
                if isinstance(progress, dict) else None,
                # 隐私：完整路径只保留 basename（与 model_path 同一策略）
                "path_name": (Path(str(e["path"]).replace("\\", "/")).name
                              if e.get("path") else None),
            })
        if not is_router or not entries:
            self._set_router(None)
            return
        # 选择被监控模型：第一个 loaded；否则保持旧选择（仍在列表）
        selected: str | None = None
        for en in entries:
            if en["loaded"] and en["id"]:
                selected = str(en["id"])
                break
        if selected is None and self._last_selected_model is not None:
            if any(en["id"] == self._last_selected_model for en in entries):
                selected = self._last_selected_model
        self._set_router({
            "is_router": True,
            "models": entries,
            "selected": selected,
        })

    def _set_router(self, router: dict | None) -> None:
        """更新 router 块；被监控模型变化时通知 server 层（驱动 /metrics 查询参数）。"""
        self.router = router
        selected = (router or {}).get("selected")
        if selected != self._last_selected_model:
            self._last_selected_model = selected
            # Router 模式切换/模型变化 -> 下一轮立即重取 /props、/v1/models
            # （它们需要 model=<id>&autoload=false；不等 10 分钟 PROPS_INTERVAL）
            self._last_props_mono = 0.0
            if self.on_router_model_change is not None:
                try:
                    self.on_router_model_change(selected)
                except Exception as exc:
                    logger.warning("on_router_model_change 回调错误: %r", exc)

    async def fetch_lora(self, force: bool = False) -> None:
        """
        一轮 /lora-adapters 只读快照（5 分钟节流）。
        输出只保留 id / basename / scale（完整路径不离开采集器内存）。
        端点不存在（404）-> lora_adapters=[]（能力不可用，不报错）。
        """
        mono = self.clock.monotonic()
        if not force and (mono - self._last_lora_mono) < LORA_INTERVAL:
            return
        self._last_lora_mono = mono
        try:
            client = self._get_client()
            response = await client.get(self._urls["lora"])
            if response.status_code != 200:
                self.lora_adapters = []
                return
            payload = json.loads(response.content.decode("utf-8", "replace"))
        except Exception:
            return
        out: list[dict] = []
        if isinstance(payload, list):
            for a in payload:
                if not isinstance(a, dict):
                    continue
                out.append({
                    "id": a.get("id"),
                    "name": (Path(str(a["path"]).replace("\\", "/")).name
                             if a.get("path") else a.get("id")),
                    "scale": a.get("scale"),
                })
        self.lora_adapters = out

    # ---- 1.3.2：Router SSE（/models/sse）受管单连接 ----

    def _start_sse(self) -> None:
        if self._sse_task is None and not self._sse_stop:
            try:
                self._sse_task = asyncio.create_task(
                    self._sse_loop(), name="llama-router-sse")
            except RuntimeError:
                self._sse_task = None

    def _stop_sse(self) -> None:
        self._sse_stop = True
        task, self._sse_task = self._sse_task, None
        if task is not None:
            task.cancel()

    def _handle_sse_frame(self, frame: str) -> None:
        """解析一个 SSE 帧（event: + data:）；去重 + 有界缓冲。"""
        event = None
        data_text = None
        for line in frame.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_text = line[5:].strip()
        if data_text is None:
            return
        try:
            data = json.loads(data_text)
        except Exception:
            return
        if not isinstance(data, dict):
            data = {"value": data}
        status = data.get("status")
        progress = data.get("progress")
        ev = {
            "t": self.clock.now(),
            "event": event or "message",
            "model": data.get("id") or data.get("model"),
            "status": status if isinstance(status, str) else (
                (status or {}).get("value") if isinstance(status, dict) else None),
            # progress.value 是阶段内 0~1（不是整模型进度）
            "stage_index": progress.get("current") if isinstance(progress, dict) else None,
            "stage_value": progress.get("value") if isinstance(progress, dict) else None,
        }
        # 去重：与上一条相同 (event,model,status,stage) 的重复推送不入缓冲
        if self.router_events:
            last = self.router_events[-1]
            if (last["event"], last["model"], last["status"],
                    last["stage_index"], last["stage_value"]) == (
                    ev["event"], ev["model"], ev["status"],
                    ev["stage_index"], ev["stage_value"]):
                return
        self.router_events.append(ev)
        # 模型状态变化 -> 立即刷新 /models 列表（不等 30s 周期）
        if ev["status"] in ("loaded", "unloaded", "loading", "error") and ev["event"] in (
                "model_status", "download_progress", "model_remove"):
            self._router_refresh_pending = True

    async def _sse_loop(self) -> None:
        """SSE 主循环：连接 -> 读帧 -> 异常断开 -> 指数退避重连（上限 30s）。"""
        backoff = SSE_RECONNECT_BASE
        while not self._sse_stop:
            connected = False
            try:
                client = self._get_client()
                async with client.stream("GET", self._urls["router_sse"]) as resp:
                    if resp.status_code != 200:
                        raise httpx.HTTPStatusError(
                            f"SSE {resp.status_code}", request=resp.request,
                            response=resp)
                    connected = True
                    self._sse_connected = True
                    backoff = SSE_RECONNECT_BASE
                    buf = ""
                    async for chunk in resp.aiter_text():
                        if self._sse_stop:
                            break
                        buf += chunk
                        while "\n\n" in buf:
                            frame, buf = buf.split("\n\n", 1)
                            self._handle_sse_frame(frame)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                self._sse_connected = False
            if self._sse_stop:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, SSE_RECONNECT_MAX)

    def _set_state(self, state: str, now: float) -> None:
        """Server State 状态机；转换才写日志 + llama_health_changed 事件。"""
        if state == self.server_state and self._last_state is not None:
            return
        prev = self.server_state
        self.server_state = state
        if self._last_state is not None and state != prev:
            _label = {
                "ready": "就绪", "loading": "模型加载中", "unavailable": "不可用",
                "sleeping": "休眠", "auth_error": "鉴权失败", "degraded": "服务异常",
            }
            (logger.info if state in ("ready", "sleeping") else logger.warning)(
                "llama.cpp 状态变化: %s -> %s（%s）", _label.get(prev, prev), _label.get(state, state), state
            )
            if self.db is not None:
                try:
                    self.db.record_event(
                        "llama_health_changed",
                        "info" if state == "ready" else "warning",
                        "collector",
                        {"previous": prev, "current": state},
                        now=now,
                    )
                except Exception as exc:
                    logger.warning("写入 llama_health_changed 事件失败: %r", exc)
        self._last_state = state
        self.last_health_update = now

    # ---- 常驻循环 ----

    def set_metrics_online(self, online: bool) -> None:
        """metrics 采集器状态注入：metrics 失败时允许立即做 health 检查。"""
        self._metrics_online = online

    async def run(self) -> None:
        """
        常驻分频调度循环：
        - 启动/重连：立即 health + props + models + router 检测；
        - 稳态：health 10s；slots 2s(active)/10s(idle)；props/models 节流；
          router 列表 30s；LoRA 5min；Router 模式下维持 SSE 单连接。
        任何异常只记日志，循环不退出；与 metrics 采集完全独立。
        """
        await self._reconnect_cycle()
        while True:
            try:
                # health（10s 周期；metrics 刚失败时不等周期立即检查）
                await self.fetch_health()
                active = await self.fetch_slots()
                # 稳态周期性刷新模型元数据（节流：PROPS_INTERVAL）
                if self._reconnect_pending is False:
                    await self.fetch_props_and_models(force=False)
                # 1.3.2：Router 列表（30s）+ SSE 报告变化时立即刷新
                if self._reconnect_pending is False or self._router_refresh_pending:
                    force = self._router_refresh_pending
                    self._router_refresh_pending = False
                    await self.fetch_router_models(force=force)
                # 1.3.2：LoRA 只读快照（5min 节流）
                if self._reconnect_pending is False:
                    await self.fetch_lora(force=False)
                # 1.3.2：Router 模式 -> 维持 SSE；非 Router -> 关闭
                if self.router and self.router.get("is_router"):
                    self._sse_stop = False
                    self._start_sse()
                elif self._sse_task is not None:
                    self._stop_sse()
            except Exception as exc:
                logger.warning("llama runtime 采集轮错误: %r", exc)
            await asyncio.sleep(SLOTS_INTERVAL_ACTIVE if active else SLOTS_INTERVAL_IDLE)

    async def _reconnect_cycle(self) -> None:
        """启动/重连：立即 health + props + models + router 检测（首次执行）。"""
        self._reconnect_pending = False
        await self.fetch_health()
        try:
            # 先检测 Router（决定 /props、/v1/models 是否按模型查询）
            await self.fetch_router_models(force=True)
            await self.fetch_props_and_models(force=True)
            await self.fetch_lora(force=True)
        except Exception as exc:
            logger.warning("llama runtime 启动采集错误: %r", exc)

    async def aclose(self) -> None:
        # 1.3.2：先关 SSE（受管单连接），再关 HTTP 客户端
        self._stop_sse()
        if self._sse_task is not None:
            try:
                await self._sse_task
            except (asyncio.CancelledError, Exception):
                pass
            self._sse_task = None
        if self._owns_http and self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None


# ---- build_info（llama-server --version；低频 subprocess，只读） ----

def fetch_build_info(timeout: float = 5.0) -> str | None:
    """
    获取 llama.cpp 构建信息（`llama-server --version`，只在 PATH 中找到时调用；
    找不到返回 None——build_info 为可选遥测，缺失不影响任何功能）。
    低频调用（启动 / build_info 首次缺失时），绝不每轮执行。
    """
    exe = shutil.which("llama-server")
    if exe is None:
        return None
    try:
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True, text=True, timeout=timeout,
            creationflags=_no_window(),
        )
        out = (proc.stdout or "").strip().splitlines()
        # 典型输出首行：llama-server version X (build N)
        for line in out:
            line = line.strip()
            if line.startswith("llama-server") and ("version" in line or "build" in line):
                return line
        return out[0] if out else None
    except Exception:
        return None
