"""
app_lifecycle.py — 应用生命周期（Phase 10）

- AppLifecycle：RUNNING -> STOPPING -> STOPPED 状态机。
  Exit 只被接受一次（重复请求/双击 Exit 直接忽略），避免多个 shutdown 同时执行；
  uptime 用 time.monotonic()（用户改系统时间不影响）。
- UiCommandDispatcher：UI 命令的单线程串行执行器（show / hide / exit）。
  Win32 Event 监听线程、pystray 菜单回调、FastAPI 的 exit 请求都只"入队"，
  由同一个线程按序执行——不同线程绝不直接并发操作 window.show/hide/destroy。
- AppIntegrationState：注入 FastAPI 的应用状态（/api/status 的 application 段、
  /api/app/integration、/api/app/autostart、/api/app/open-folder、/api/app/exit）。
  桌面入口负责填充 provider 回调；测试/浏览器模式注入 None 时 API 返回降级值。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger("llamamonitor.lifecycle")


class AppLifecycle:
    """优雅关闭状态机：running -> stopping -> stopped（不可逆）。"""

    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"

    def __init__(self) -> None:
        self._state = self.RUNNING
        self._stop_reason: str | None = None
        self._lock = threading.Lock()
        self._start_monotonic = time.monotonic()

    @property
    def state(self) -> str:
        return self._state

    @property
    def stop_reason(self) -> str | None:
        return self._stop_reason

    @property
    def start_monotonic(self) -> float:
        return self._start_monotonic

    def uptime_seconds(self) -> int:
        return int(time.monotonic() - self._start_monotonic)

    def request_stop(self, reason: str) -> bool:
        """
        请求关闭。只有从 running -> stopping 的那一次返回 True（执行者），
        之后重复请求（STOPPING/STOPPED）返回 False——幂等，不并发 shutdown。
        """
        with self._lock:
            if self._state != self.RUNNING:
                logger.debug("重复的退出请求已忽略（state=%s, reason=%s）", self._state, reason)
                return False
            self._state = self.STOPPING
            self._stop_reason = reason
            return True

    def mark_stopped(self) -> None:
        with self._lock:
            if self._state == self.RUNNING:
                self._state = self.STOPPING
            self._state = self.STOPPED

    def __repr__(self) -> str:  # 测试/日志可读
        return f"AppLifecycle(state={self._state!r})"


class UiCommandDispatcher:
    """
    UI 命令队列 + 单执行线程：
    - Win32 show-event 线程 / tray 菜单 / API exit 都通过 request() 入队；
    - 一个线程按 FIFO 串行执行 handlers[cmd]，窗口操作不会并发；
    - stop() 发送哨兵并 join（超时 WARNING），之后 request() 静默丢弃。
    """

    def __init__(self, handlers: dict[str, Callable[[], None]]) -> None:
        self._handlers = handlers
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stopped = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._thread = threading.Thread(target=self._loop, name="llamamonitor-ui", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while True:
            cmd = self._queue.get()
            if cmd is None:  # 哨兵
                break
            handler = self._handlers.get(cmd)
            if handler is None:
                logger.warning("未知的 UI 命令: %r", cmd)
                continue
            try:
                handler()
            except Exception:
                logger.exception("UI 命令 %r 执行失败", cmd)

    def request(self, cmd: str) -> None:
        """线程安全入队（调用方不等待执行完成）。"""
        if self._stopped:
            return
        self._queue.put(cmd)

    def stop(self, timeout: float = 3.0) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._thread is not None:
            self._queue.put(None)  # 哨兵
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("UI 命令线程未能在 %.1fs 内退出", timeout)
            self._thread = None


@dataclass
class AppIntegrationState:
    """
    注入 FastAPI 的应用集成状态（Phase 10）。

    桌面模式由 desktop.py 填充 provider 回调；build_app 收到 None（浏览器模式 /
    测试）时：/api/app/integration 返回 platform+frozen+降级 autostart，
    /api/app/exit 返回 503，open-folder 仍可用（纯后端映射）。
    """

    background: bool = False
    single_instance: bool = True
    tray_available: bool = False
    start_monotonic: float = field(default_factory=time.monotonic)
    # runtime 状态：server 在事件循环线程每轮采集后写入（_refresh_app_state），
    # 托盘 / /api/status 只读——不额外起线程查库。
    # {llama_online: bool|None, gpu_available: bool, gpu_count: int,
    #  today_logical_tokens: int}
    runtime: dict = field(default_factory=dict)
    # provider 回调（由 desktop.py 注入）：
    integration_provider: Callable[[], dict] | None = None       # GET /api/app/integration
    set_autostart: Callable[[bool], dict] | None = None          # PUT /api/app/autostart
    request_exit: Callable[[], bool] | None = None               # POST /api/app/exit
    status_application: Callable[[], dict] | None = None         # /api/status.application
