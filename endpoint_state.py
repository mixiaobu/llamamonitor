"""
endpoint_state.py — 单端点健康状态 / 错误分类 / 能力缓存（1.3.3 故障隔离）

背景（1.3.3 §16-§18）：一个推理服务有多个端点（/metrics /status /slots
/health /props /v1/models /models /v1/status prometheus …），任何一个的
timeout / 5xx / 4xx / 坏 JSON 都**只能局部化**——不能让整个监控停摆，
更不能把"该端点暂时不可用"误报成"没有推理活动"。

每个端点独立维护：
    {available, last_success, last_attempt, age_ms,
     consecutive_failures, last_error}

错误分类（§17）：
    404 / 501            -> unsupported（端点不存在/版本不支持）
    401                  -> authentication_error
    429                  -> rate_limited
    500 / 502 / 503      -> temporarily_unavailable
    timeout              -> timeout
    connection refused   -> unreachable
    JSON parse 失败      -> invalid_response

能力缓存（§18）：unsupported / authentication_error / rate_limited 是
**明确结果**——HOT 路径不能每秒重复请求（1s 狂刷），恢复检测按
CAPABILITY_RETRY_SECONDS（15s）节流；temporarily_unavailable / timeout /
unreachable 是瞬时故障，保持正常节奏快速恢复（1s 1s 1s）。

本模块是纯内存状态（无 I/O），采集器在每次端点尝试后调用
record_success / record_failure，API 层读取 to_dict() 组装 diagnostics。
"""

from __future__ import annotations

# ---- 错误分类常量（§17） ----
ERR_UNSUPPORTED = "unsupported"
ERR_AUTH = "authentication_error"
ERR_RATE_LIMITED = "rate_limited"
ERR_TEMP = "temporarily_unavailable"
ERR_TIMEOUT = "timeout"
ERR_UNREACHABLE = "unreachable"
ERR_INVALID = "invalid_response"

# 能力型错误：明确结果，重试按恢复间隔节流（不每秒狂刷）
CAPABILITY_ERRORS = (ERR_UNSUPPORTED, ERR_AUTH, ERR_RATE_LIMITED)
# 能力错误的恢复检测间隔（§18：10~30s）
CAPABILITY_RETRY_SECONDS = 15.0

# HTTP 状态码 -> 分类
_STATUS_MAP = {
    401: ERR_AUTH,
    404: ERR_UNSUPPORTED,
    429: ERR_RATE_LIMITED,
    501: ERR_UNSUPPORTED,
}
# 5xx（408 请求超时按 timeout）
_TEMP_CODES = frozenset({500, 502, 503, 504})


def classify_http_status(status_code: int | None) -> str:
    """HTTP 状态码 -> 错误分类（2xx 不应调用；200 也容错返回 ''）。"""
    if status_code is None:
        return ERR_UNREACHABLE
    if 200 <= status_code < 300:
        return ""
    if status_code in _STATUS_MAP:
        return _STATUS_MAP[status_code]
    if status_code == 408:
        return ERR_TIMEOUT
    if status_code in _TEMP_CODES:
        return ERR_TEMP
    # 其它 4xx/5xx：服务可达但异常（保守按暂时不可用）
    return ERR_TEMP


def classify_exception(exc: BaseException) -> str:
    """异常 -> 错误分类（httpx 优先；未知异常按暂时不可用）。"""
    name = type(exc).__name__
    if name in ("ReadTimeout", "ConnectTimeout", "PoolTimeout", "WriteTimeout",
                "TimeoutException", "HTTPTimeout"):
        return ERR_TIMEOUT
    if name in ("ConnectError", "NetworkError", "RemoteProtocolError"):
        return ERR_UNREACHABLE
    try:
        import httpx
        if isinstance(exc, httpx.TimeoutException):
            return ERR_TIMEOUT
        if isinstance(exc, httpx.ConnectError):
            return ERR_UNREACHABLE
    except Exception:
        pass
    return ERR_TEMP


class EndpointState:
    """
    单个端点的健康状态（内存态；线程/协程安全由单事件循环保证）。

    - record_success(now_wall, now_mono)：成功一次（清零失败计数）；
    - record_failure(now_wall, now_mono, error)：失败一次（计数+1，分类）；
    - should_poll(now_mono)：能力型错误期间按恢复间隔节流（True=本轮应请求）；
    - to_dict(now_wall)：API diagnostics 形态（age_ms 毫秒，null=从未成功）。
    """

    __slots__ = (
        "name", "available", "last_success", "last_attempt",
        "last_attempt_mono", "consecutive_failures", "last_error",
    )

    def __init__(self, name: str) -> None:
        self.name = name
        self.available = False            # 最近一次尝试是否成功
        self.last_success: float | None = None   # wall（Unix 秒）
        self.last_attempt: float | None = None   # wall
        self.last_attempt_mono: float | None = None
        self.consecutive_failures = 0
        self.last_error: str | None = None

    def record_success(self, now_wall: float, now_mono: float) -> None:
        self.available = True
        self.last_success = now_wall
        self.last_attempt = now_wall
        self.last_attempt_mono = now_mono
        self.consecutive_failures = 0
        self.last_error = None

    def record_failure(self, now_wall: float, now_mono: float,
                       error: str) -> None:
        was_available = self.available
        self.available = False
        self.last_attempt = now_wall
        self.last_attempt_mono = now_mono
        self.consecutive_failures += 1
        self.last_error = error
        if was_available:
            # OK -> ERROR 状态转换：调用方（采集器）负责记日志（本模块不持有 logger）
            pass

    def error_changed(self, prev_error: str | None) -> bool:
        """错误分类是否发生变化（日志去重用）。"""
        return prev_error != self.last_error

    def should_poll(self, now_mono: float) -> bool:
        """
        能力缓存（§18）：明确能力错误（unsupported/auth/rate_limited）期间
        按 CAPABILITY_RETRY_SECONDS 节流——避免 HOT 路径每秒重复请求
        一个已知 404 的端点；其它状态（含从未探测）保持正常节奏。
        """
        if self.last_error not in CAPABILITY_ERRORS:
            return True
        if self.last_attempt_mono is None:
            return True
        return (now_mono - self.last_attempt_mono) >= CAPABILITY_RETRY_SECONDS

    def age_ms(self, now_wall: float) -> int | None:
        """距最近一次成功的毫秒数（从未成功 -> None）。"""
        if self.last_success is None:
            return None
        return int(max(0.0, (now_wall - self.last_success) * 1000))

    def to_dict(self, now_wall: float) -> dict:
        """API diagnostics 形态（1.3.3 §16/§31）。"""
        return {
            "available": self.available,
            "last_success": self.last_success,
            "last_attempt": self.last_attempt,
            "age_ms": self.age_ms(now_wall),
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
        }


class EndpointRegistry:
    """
    采集器的端点状态集合（dict 语义 + 统一 diagnostics 导出）。

    get(name) 自动创建；diagnostics(now_wall) 导出全部端点。
    """

    def __init__(self) -> None:
        self._states: dict[str, EndpointState] = {}

    def get(self, name: str) -> EndpointState:
        st = self._states.get(name)
        if st is None:
            st = EndpointState(name)
            self._states[name] = st
        return st

    def __contains__(self, name: str) -> bool:
        return name in self._states

    def names(self) -> list[str]:
        return list(self._states.keys())

    def diagnostics(self, now_wall: float) -> dict[str, dict]:
        return {name: st.to_dict(now_wall)
                for name, st in self._states.items()}
