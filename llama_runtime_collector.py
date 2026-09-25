"""
llama_runtime_collector.py — llama.cpp Runtime 只读采集器（1.1.0 Stage A）

在现有 /metrics Token 采集（collector.py）之外，补全 llama.cpp 的
Health / Model / Slot 运行时遥测。严格只读，绝不 POST 任何控制接口
（不 POST /props、不 POST /models/load|unload、不调推理 API）。

只读取（GET）：
- /health       建立准确的 Server State（Ready/Loading/Unavailable）——
                不能仅凭 HTTP 端口可达就显示"就绪"；
- /slots        Active Slot 运行时（白名单字段：状态/计数/采样参数；
                绝不读取/保存 prompt 文本）；
- /props        模型与服务元数据（白名单字段；chat_template /
                generation_prompt / media_marker / 完整 model_path 一律
                不进数据库、不进日志、不进普通前端 API）；
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
from pathlib import Path
from typing import Any, Callable

import httpx

from clock import Clock, default_clock
from config import AppConfig, trust_env_for

logger = logging.getLogger("llamamonitor.runtime")

# ---- 采样周期（秒；config 可覆盖，见 config.SystemConfig 无关，此处为固定策略） ----
HEALTH_INTERVAL = 10.0          # /health：10 秒（metrics 失败时立即）
SLOTS_INTERVAL_ACTIVE = 2.0     # /slots：is_processing=true
SLOTS_INTERVAL_IDLE = 10.0      # /slots：空闲
PROPS_INTERVAL = 600.0          # /props：启动/重连/每 10 分钟最多一次
# /v1/models：启动/重连/模型变化时（无固定周期）

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
    # next_token 是 list（每位置一个 dict）；只取数字字段
    nt = raw.get("next_token")
    if isinstance(nt, list) and nt and isinstance(nt[0], dict):
        out["next_token"] = {
            "has_next_token": bool(nt[0].get("has_next_token")),
            "n_remain": nt[0].get("n_remain"),
            "n_decoded": nt[0].get("n_decoded"),
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
        if props.get("sleeping") is not None:
            info["is_sleeping"] = bool(props.get("sleeping"))
        dgs = props.get("default_generation_settings")
        if isinstance(dgs, dict) and dgs.get("n_ctx") is not None:
            info["context_size"] = dgs.get("n_ctx")

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
    - server_state: "ready" | "loading" | "unavailable"（/health 驱动）
    - model_info:   LlamaModelInfo dict 或 None
    - slots:        最近一次 /slots 白名单快照 list
    - capabilities: {"health": bool, "slots": bool, "props": bool, "models": bool}

    - 单轮任何异常都不影响 metrics Token 采集（独立任务、独立 HTTP 客户端）；
    - 状态转换才写日志/事件（不刷屏）：llama_health_changed / model_changed。
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
        }
        self.timeout = config.llama_server.timeout_seconds
        # ---- 状态 ----
        self.server_state: str = "unavailable"   # ready / loading / unavailable
        self.model_info: dict | None = None
        self.slots: list[dict] = []
        self.capabilities: dict[str, bool] = {
            "health": False, "slots": False, "props": False, "models": False,
        }
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

    # ---- HTTP ----

    def _get_client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=self.timeout,
                trust_env=trust_env_for(self._urls["health"]),
            )
        return self._http

    async def _get_json(self, key: str) -> tuple[dict | list | None, bool]:
        """
        GET 一个 runtime 端点并解析 JSON。
        返回 (payload, ok)：ok=False 时 payload=None（网络错误/非 200/超大/坏 JSON）。
        任何失败都不抛异常。
        """
        try:
            client = self._get_client()
            response = await client.get(self._urls[key])
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

    async def fetch_health(self) -> None:
        """一轮 /health 检查 + Server State 判定。"""
        now = self.clock.now()
        payload, ok = await self._get_json("health")
        if not ok:
            self._set_state("unavailable", now)
            self.capabilities["health"] = self.capabilities["health"] or False
            return
        self.capabilities["health"] = True
        self._any_request_succeeded = True
        status = ""
        if isinstance(payload, dict):
            status = str(payload.get("status", "")).lower()
        if status == "ok":
            self._set_state("ready", now)
        else:
            # "loading" / "error" / 未知 -> 模型加载中（端口可达但服务未就绪）
            self._set_state("loading", now)
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
        props, props_ok = await self._get_json("props")
        self.capabilities["props"] = bool(props_ok and isinstance(props, dict))
        models, models_ok = await self._get_json("models")
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

    def _set_state(self, state: str, now: float) -> None:
        """Server State 状态机；转换才写日志 + llama_health_changed 事件。"""
        if state == self.server_state and self._last_state is not None:
            return
        prev = self.server_state
        self.server_state = state
        if self._last_state is not None and state != prev:
            _label = {"ready": "就绪", "loading": "模型加载中", "unavailable": "不可用"}
            (logger.info if state == "ready" else logger.warning)(
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
        - 启动/重连：立即 health + props + models；
        - 稳态：health 10s；slots 2s(active)/10s(idle)；props/models 节流。
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
            except Exception as exc:
                logger.warning("llama runtime 采集轮错误: %r", exc)
            await asyncio.sleep(SLOTS_INTERVAL_ACTIVE if active else SLOTS_INTERVAL_IDLE)

    async def _reconnect_cycle(self) -> None:
        """启动/重连：立即 health + props + models（首次执行）。"""
        self._reconnect_pending = False
        await self.fetch_health()
        try:
            await self.fetch_props_and_models(force=True)
        except Exception as exc:
            logger.warning("llama runtime 启动采集错误: %r", exc)

    async def aclose(self) -> None:
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
