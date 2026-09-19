"""
windows_integration.py — Windows 系统集成（Phase 10，纯 stdlib：ctypes + winreg）

职责（只做 Windows 系统层，不含 UI 与业务）：
- 单实例：Named Mutex `Local\\LlamaMonitor.SingleInstance`
  （Mutex 由操作系统在内核中持有：进程崩溃/断电自动释放，不会留下 stale 锁——
  这是不用 lock 文件的原因）；
- 第二实例唤醒：Named Event `Local\\LlamaMonitor.ShowWindow`
  （第一实例持有 Event 并有监听线程；第二实例 SetEvent 后退出）；
- 优雅退出请求（Phase 12）：Named Event `Local\\LlamaMonitor.Shutdown`
  + request_shutdown()（--shutdown-existing / 安装器升级前使用；
  等待单实例 Mutex 释放，超时返回非 0，绝不强杀）；
- 开机自启：HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run 的
  `LlamaMonitor` 值（当前用户登录时启动，无需管理员；只读写这一个值）；
- 打开文件夹：data / logs / backups 固定映射到 %LOCALAPPDATA%\\LlamaMonitor 下，
  os.startfile 打开（目录不存在先创建）；
- is_frozen()：统一 EXE/开发模式检测（定义在 config.py，这里 re-export 供
  Windows 集成模块使用，避免散落多处实现）。

本地前缀用 Local\\（当前会话）而不是 Global\\，避免跨会话/权限问题。
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import sys
import threading
import time
import winreg
from pathlib import Path

from config import app_data_dir, is_frozen  # noqa: F401  (re-export is_frozen)

logger = logging.getLogger("llamamonitor.windows")

IS_WINDOWS = sys.platform == "win32"

# ---------------------------------------------------------------------------
# Named Mutex / Named Event（单实例 + 唤醒信号）
# ---------------------------------------------------------------------------

SINGLE_INSTANCE_MUTEX_NAME = "Local\\LlamaMonitor.SingleInstance"
SHOW_WINDOW_EVENT_NAME = "Local\\LlamaMonitor.ShowWindow"
# Phase 12：升级/安装器请求已运行实例优雅退出（Named Event，与 ShowWindow 同机制；
# 不使用 HTTP /api/app/exit 作为核心升级机制——端口可能改变、HTTP 可能未 ready）
SHUTDOWN_EVENT_NAME = "Local\\LlamaMonitor.Shutdown"

# Win32 常量
ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102
INFINITE = 0xFFFFFFFF


def _kernel32():
    if not IS_WINDOWS:
        raise RuntimeError("Named Mutex/Event 只在 Windows 上可用")
    return ctypes.windll.kernel32


CreateMutexW = ctypes.WINFUNCTYPE(
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p, use_last_error=True
)
CreateEventW = ctypes.WINFUNCTYPE(
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p, use_last_error=True
)
WaitForSingleObject = ctypes.WINFUNCTYPE(
    ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong
)
SetEvent = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p)
ResetEvent = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p)
CloseHandle = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p)

if IS_WINDOWS:
    _CreateMutexW = CreateMutexW(_kernel32().CreateMutexW)
    _CreateEventW = CreateEventW(_kernel32().CreateEventW)
    _WaitForSingleObject = WaitForSingleObject(_kernel32().WaitForSingleObject)
    _SetEvent = SetEvent(_kernel32().SetEvent)
    _ResetEvent = ResetEvent(_kernel32().ResetEvent)
    _CloseHandle = CloseHandle(_kernel32().CloseHandle)


class SingleInstance:
    """
    Windows 单实例锁（Named Mutex）。

    - acquire()：创建 `Local\\LlamaMonitor.SingleInstance`；若已存在
      （GetLastError == ERROR_ALREADY_EXISTS）返回 False —— 已有实例在运行。
      调用方必须在打开数据库/启动 Collector/启动 FastAPI 之前 acquire。
    - release()：CloseHandle 释放（进程结束时 OS 也会自动释放——崩溃不留 stale 锁）。
    - 整个运行期间必须持有：handle 保存在实例上，防止被 GC 后误关。
    """

    # 非 Windows 测试替身：进程内"已被占用"标志
    _non_win_taken = False

    def __init__(self, name: str = SINGLE_INSTANCE_MUTEX_NAME) -> None:
        self.name = name
        self._handle = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self) -> bool:
        """
        True = 本次调用成为唯一实例；False = 已有 LlamaMonitor 在运行。

        判定不依赖 GetLastError（实测 CreateMutexW 返回已存在 mutex 时
        lasterror 不可靠），而是用**所有权检测**：
        - 新创建（bInitialOwner=1）或已存在但无人持有（旧进程已退出，OS 自动
          释放）-> WaitForSingleObject(0) 立即获得所有权 -> 唯一实例；
        - 已存在且被另一个存活实例持有 -> 0 毫秒内拿不到 -> 第二实例。
        崩溃/断电时 OS 自动释放 mutex，不会留下 stale 锁（这是不用 lock 文件的原因）。
        """
        if self._handle is not None:
            return True
        if not IS_WINDOWS:
            # 非 Windows（测试环境）：用进程内标志模拟"只能 acquire 一次"的语义
            if SingleInstance._non_win_taken:
                return False
            SingleInstance._non_win_taken = True
            return True
        handle = _CreateMutexW(None, 1, self.name)
        if not handle:
            logger.error("CreateMutexW(%s) 失败: last_error=%d", self.name, ctypes.get_last_error())
            return False
        result = _WaitForSingleObject(handle, 0)
        if result in (WAIT_OBJECT_0, WAIT_ABANDONED):
            # 获得所有权（WAIT_ABANDONED：原持有者线程已死，锁可安全接管）
            self._handle = handle
            return True
        # WAIT_TIMEOUT（0 毫秒拿不到）：另一个存活实例持有
        _CloseHandle(handle)
        return False

    def release(self) -> None:
        if self._handle is not None:
            _CloseHandle(self._handle)
            self._handle = None
        if not IS_WINDOWS:
            SingleInstance._non_win_taken = False


def notify_show_window(name: str = SHOW_WINDOW_EVENT_NAME) -> bool:
    """
    第二实例调用：打开（或创建）第一实例的 ShowWindow Event 并 SetEvent，
    通知已运行实例显示 Dashboard。True = 信号已发出。
    （name 参数供测试用独立事件名，避免与运行中的实例串扰。）
    """
    if not IS_WINDOWS:
        return _NON_WIN_SIGNAL.notify()
    handle = _CreateEventW(None, 1, 0, name)
    if not handle:
        logger.warning("CreateEventW(%s) 失败: last_error=%d", name, ctypes.get_last_error())
        return False
    try:
        ok = bool(_SetEvent(handle))
        if not ok:
            logger.warning("SetEvent(%s) 失败: last_error=%d", name, ctypes.get_last_error())
        return ok
    finally:
        _CloseHandle(handle)


class _NonWinSignal:
    """非 Windows 测试替身：内存信号，行为与 Named Event 等价。"""

    def __init__(self) -> None:
        self._event = threading.Event()
        self.listeners: list = []

    def notify(self) -> bool:
        self._event.set()
        return True

    def take(self) -> bool:
        return self._event.wait(timeout=0.0)

    def clear(self) -> None:
        self._event.clear()


_NON_WIN_SIGNAL = _NonWinSignal()


class ShowWindowListener:
    """
    第一实例的唤醒监听线程：等待 ShowWindow Event，触发后回调 on_signal。

    - 手动重置 Event：触发后先 ResetEvent 再回调，避免丢信号/重复触发；
    - stop()：先置 stop 标志并 join 线程（2s 超时），最后 CloseHandle——
      绝不在线程仍运行时关闭句柄（避免 use-after-close）；
    - on_signal 里只应"入队"（如 ui_queue.put('show')），不要直接操作 WebView
      （Win32 线程不能直接不受控地操作 pywebview UI，见 app_lifecycle.py）。
    """

    WAIT_SLICE_MS = 250

    def __init__(self, on_signal, name: str = SHOW_WINDOW_EVENT_NAME) -> None:
        self._on_signal = on_signal
        self._name = name
        self._handle = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._signaled = threading.Event()  # 非 Windows 测试路径
        # 非 Windows 测试路径的信号源（ShutdownListener 覆盖为自己的信号）
        self._non_win_signal = _NON_WIN_SIGNAL

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if IS_WINDOWS:
            self._handle = _CreateEventW(None, 1, 0, self._name)
            if not self._handle:
                logger.error("CreateEventW(%s) 失败: last_error=%d", self._name, ctypes.get_last_error())
                return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="llamamonitor-show-event", daemon=True)
        self._thread.start()
        return True

    def _loop(self) -> None:
        if IS_WINDOWS:
            while not self._stop.is_set():
                result = _WaitForSingleObject(self._handle, self.WAIT_SLICE_MS)
                if result == WAIT_OBJECT_0:
                    _ResetEvent(self._handle)
                    if not self._stop.is_set():
                        self._emit()
                elif result == WAIT_TIMEOUT:
                    continue
                else:
                    logger.warning("WaitForSingleObject 异常返回: %s", result)
                    break
        else:
            # 非 Windows：轮询内存信号（测试）
            while not self._stop.is_set():
                if self._non_win_signal.take():
                    self._emit()
                else:
                    self._stop.wait(self.WAIT_SLICE_MS / 1000.0)

    def _emit(self) -> None:
        try:
            self._on_signal()
        except Exception:
            logger.exception("show-window 回调失败")

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                # AUDIT-ASYNC-003：join 超时不能静默（监听线程卡住 = 后续升级/
                # 第二实例唤醒请求可能无人处理）
                logger.warning("事件监听线程未能在 %.1fs 内退出", timeout)
            self._thread = None
        if IS_WINDOWS and self._handle is not None:
            _CloseHandle(self._handle)
            self._handle = None


# ---------------------------------------------------------------------------
# Shutdown（Phase 12：升级/安装器请求优雅退出）
# ---------------------------------------------------------------------------


def notify_shutdown(name: str = SHUTDOWN_EVENT_NAME) -> bool:
    """
    向已运行实例发送 Shutdown 信号（打开或创建该 Event 并 SetEvent）。
    没有实例在监听时：Event 被创建后随本句柄关闭而消失，无副作用。
    True = 信号已发出。
    """
    if not IS_WINDOWS:
        return _NON_WIN_SHUTDOWN_SIGNAL.notify()
    handle = _CreateEventW(None, 1, 0, name)
    if not handle:
        logger.warning("CreateEventW(%s) 失败: last_error=%d", name, ctypes.get_last_error())
        return False
    try:
        ok = bool(_SetEvent(handle))
        if not ok:
            logger.warning("SetEvent(%s) 失败: last_error=%d", name, ctypes.get_last_error())
        return ok
    finally:
        _CloseHandle(handle)


class _NonWinShutdownSignal(_NonWinSignal):
    """非 Windows 测试替身（与 Shutdown Event 行为等价）。"""


_NON_WIN_SHUTDOWN_SIGNAL = _NonWinShutdownSignal()


class ShutdownListener(ShowWindowListener):
    """
    第一实例的 Shutdown 监听线程（Phase 12）。

    监听 `Local\\LlamaMonitor.Shutdown`；收到信号后回调 on_signal——
    desktop.py 里回调的是 dispatcher.request("exit")，走**正常优雅关闭**
    （Tray -> FastAPI/SQLite -> 窗口 -> 释放 Mutex），绝不 TerminateProcess。
    """

    def __init__(self, on_signal, name: str = SHUTDOWN_EVENT_NAME) -> None:
        super().__init__(on_signal, name)
        # 非 Windows 测试路径：用独立的内存信号（与 ShowWindow 信号分开）
        self._non_win_signal = _NON_WIN_SHUTDOWN_SIGNAL


def request_shutdown(
    timeout: float = 10.0,
    mutex_name: str = SINGLE_INSTANCE_MUTEX_NAME,
    event_name: str = SHUTDOWN_EVENT_NAME,
) -> int:
    """
    `--shutdown-existing` 的核心（Phase 12，供安装器升级/卸载前调用）：

    - 没有运行实例：返回 0（不需要做任何事）；
    - 有运行实例：发送 Shutdown Event（优雅退出），然后等待单实例 Mutex
      被释放（最多 timeout 秒）：
        成功释放 -> 0；超时仍被持有 -> 1。

    判定方式与 SingleInstance.acquire 相同的所有权检测（不依赖 GetLastError）：
    先创建 Mutex 并尝试 0ms 等待——立即拿到 = 无存活实例。
    等待期间持有自己的 Mutex 句柄：运行实例释放（CloseHandle/进程退出）时
    OS 解除 Mutex 信号，WaitForSingleObject 返回。
    mutex_name / event_name 供测试用独立名称，避免与运行中的实例串扰。
    非 Windows（测试环境）：返回 0（无真实实例可请求）。
    """
    if not IS_WINDOWS:
        return 0
    handle = _CreateMutexW(None, 1, mutex_name)
    if not handle:
        # 无法创建 Mutex -> 无法判定；返回 0 不阻塞安装流程（安装器可再检查）
        logger.warning("CreateMutexW(%s) 失败: last_error=%d",
                       mutex_name, ctypes.get_last_error())
        return 0
    try:
        result = _WaitForSingleObject(handle, 0)
        if result in (WAIT_OBJECT_0, WAIT_ABANDONED):
            # 我们持有 Mutex：没有运行实例（或只有已死实例留下的 stale 锁）
            return 0
        # 有存活实例持有：请求优雅退出，然后等 Mutex 释放
        notify_shutdown(name=event_name)
        logger.info("已请求运行中的 LlamaMonitor 优雅退出，等待最多 %.0fs ...", timeout)
        result = _WaitForSingleObject(handle, int(timeout * 1000))
        if result in (WAIT_OBJECT_0, WAIT_ABANDONED):
            logger.info("LlamaMonitor 已退出（Mutex 已释放）")
            return 0
        logger.warning("等待 %.0fs 后 LlamaMonitor 仍在运行", timeout)
        return 1
    finally:
        _CloseHandle(handle)


# ---------------------------------------------------------------------------
# Autostart：HKCU\...\Run（当前用户登录时启动，无需管理员）
# ---------------------------------------------------------------------------

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "LlamaMonitor"


def autostart_command(exe_path: str | Path) -> str:
    """
    开机启动命令：`"<绝对路径>" --background`（总是 quote 路径——可能含空格；
    绝对路径——不依赖启动时的工作目录；--background：登录时进托盘，不弹窗口）。
    """
    return f'"{Path(exe_path).resolve()}" --background'


class AutostartManager:
    """
    HKCU Run 注册表管理。程序只读写 RUN_KEY_PATH 下的 RUN_VALUE_NAME 这一个值，
    不接受外部传入任意 registry 路径（防注入）。

    - supported：Windows 且 EXE 模式（开发模式不写注册表，避免把临时 Python
      环境写进开机启动）；
    - enabled/stale 永远读真实注册表（config.json 里不保存"自启状态"）；
      stale = 注册表值存在但不等于当前 exe 的期望命令（EXE 被移动过）；
    - enable/disable 只在用户明确点击时调用（不自动偷改）。

    registry 参数供测试注入 mock（默认 winreg）。
    """

    def __init__(self, exe_path: str | Path | None, registry=None) -> None:
        self._registry = registry if registry is not None else winreg
        self._exe_path = Path(exe_path) if exe_path else None

    @property
    def exe_path(self) -> Path | None:
        return self._exe_path

    def expected_command(self) -> str:
        if self._exe_path is None:
            return ""
        return autostart_command(self._exe_path)

    def supported(self) -> bool:
        return IS_WINDOWS and is_frozen() and self._exe_path is not None

    def read_value(self) -> str | None:
        """当前注册表值；键或值不存在 -> None。"""
        if not IS_WINDOWS:
            return None
        try:
            with self._registry.OpenKey(self._registry.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, self._registry.KEY_READ) as key:
                value, _ = self._registry.QueryValueEx(key, RUN_VALUE_NAME)
            return value
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("读取 autostart 注册表失败: %r", exc)
            return None

    def get_state(self) -> dict:
        """
        {supported, enabled, stale, command, expected_command}
        command = 注册表现有值（无则 ""）；stale = enabled 且 command != 期望。
        """
        command = self.read_value() or ""
        enabled = bool(command)
        expected = self.expected_command()
        return {
            "supported": self.supported(),
            "enabled": enabled,
            "stale": enabled and bool(expected) and command != expected,
            "command": command,
            "expected_command": expected,
        }

    def _open_run_key_for_write(self, attempts: int = 3, delay: float = 0.15):
        """
        以 KEY_SET_VALUE 打开 HKCU Run 键（上下文管理器）。

        Run 键会被系统组件（Shell/计划任务等）短暂占用：偶发 WinError 5
        ACCESS_DENIED。短间隔重试 3 次；FileNotFoundError（键不存在）不重试。
        """

        @contextlib.contextmanager
        def _open():
            last = None
            for i in range(attempts):
                try:
                    key = self._registry.OpenKey(
                        self._registry.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, self._registry.KEY_SET_VALUE
                    )
                    break
                except FileNotFoundError:
                    raise
                except OSError as exc:
                    last = exc
                    if i < attempts - 1:
                        logger.warning(
                            "打开 autostart 注册表键瞬时失败（%r），%.0fms 后重试", exc, delay * 1000
                        )
                        time.sleep(delay)
            else:
                raise last
            try:
                yield key
            finally:
                self._registry.CloseKey(key)

        return _open()

    def enable(self) -> str:
        """写入期望命令，返回写入值。不支持时 raise RuntimeError。"""
        if not self.supported():
            raise RuntimeError("autostart 不可用（需要 Windows + EXE 模式）")
        expected = self.expected_command()
        with self._open_run_key_for_write() as key:
            self._registry.SetValueEx(key, RUN_VALUE_NAME, 0, self._registry.REG_SZ, expected)
        logger.info("Autostart enabled: %s", expected)
        return expected

    def disable(self) -> bool:
        """删除值（不存在也算成功，幂等）。返回 True。"""
        if not IS_WINDOWS:
            return True
        try:
            with self._open_run_key_for_write() as key:
                self._registry.DeleteValue(key, RUN_VALUE_NAME)
            logger.info("Autostart disabled")
            return True
        except FileNotFoundError:
            return True  # 本来就没注册：幂等
        except OSError as exc:
            logger.warning("删除 autostart 注册表失败（重试后仍失败）: %r", exc)
            return False

    def set_enabled(self, enabled: bool) -> dict:
        """enable/disable 统一入口，返回 get_state()（供 API 直接使用）。"""
        if enabled:
            self.enable()
        else:
            self.disable()
        return self.get_state()


# ---------------------------------------------------------------------------
# 打开文件夹（固定映射，不接受任意路径）
# ---------------------------------------------------------------------------

FOLDER_TARGETS = {
    "data": (),
    "logs": ("logs",),
    "backups": ("backups",),
    "updates": ("updates",),  # Phase 13：Portable 模式"打开下载目录"
}


def folder_path(target: str) -> Path:
    """data/logs/backups -> %LOCALAPPDATA%\\LlamaMonitor[\\子目录]（不存在则创建）。"""
    if target not in FOLDER_TARGETS:
        raise ValueError(f"未知 folder target: {target!r}（允许: {', '.join(sorted(FOLDER_TARGETS))}）")
    base = app_data_dir()
    path = base.joinpath(*FOLDER_TARGETS[target])
    path.mkdir(parents=True, exist_ok=True)
    return path


def open_folder(target: str) -> str:
    """打开固定映射目录（os.startfile，不经过 shell 字符串拼接）。"""
    path = folder_path(target)
    if IS_WINDOWS:
        os.startfile(str(path))  # noqa: S606（路径来自固定映射 + LOCALAPPDATA，非用户输入）
    return str(path)
