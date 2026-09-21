"""
desktop.py — LlamaMonitor Windows 桌面入口（Phase 5~10）

启动流程（单进程，不留下后台 Python 进程）：
1. 解析参数（--background / --url / --db / --interval）；
2. 挂载日志（--windowed 无控制台，关键失败必须写 monitor.log）；
3. 加载统一配置（config.load_config）；
4. **单实例（Named Mutex，最先执行，先于 DB / Collector / FastAPI）**：
   已有实例 -> 发 ShowWindow Event 唤醒它显示 Dashboard，本实例退出；
5. 启动 ShowWindow 监听线程（第二实例唤醒）+ UI 命令分发器（窗口操作单线程）；
6. 打开 SQLite / Collector / GPU / FastAPI（后台线程，独立事件循环）；
7. 启动系统托盘（pystray；失败不拖垮主程序）；
8. 主线程创建 pywebview 窗口（--background 时 hidden 创建），加载 FastAPI 地址：
   - 点 X（closing）：不退出 -> 隐藏到托盘（监控继续），一次性托盘提示；
   - 只有 Tray->Exit / Settings->Exit / API /api/app/exit 才真正退出；
9. 优雅关闭顺序（AppLifecycle 状态机幂等）：
   托盘 -> FastAPI(lifespan 取消 Collector/GPU 并等待、关 SQLite)
   -> 窗口 -> UI 分发器 -> show-event 监听(CloseHandle) -> 释放 Mutex -> 退出。

数据路径永远 %LOCALAPPDATA%\\LlamaMonitor（与启动位置无关）；
EXE 资源（static/assets）来自 _internal（onedir 打包目录）。

运行方式（项目根目录 / 打包后 EXE 同参数）：
    python desktop.py                     # 前台（显示 Dashboard）
    python desktop.py --background        # 后台（托盘，不弹窗口）
    python desktop.py --version           # 打印版本并退出（不启动任何组件，Phase 12）
    python desktop.py --shutdown-existing # 请求运行中实例优雅退出并等待 ≤10s（Phase 12，安装器用）
    LlamaMonitor.exe --background         # 开机自启使用（注册表固定命令）

Phase 12（Release/Installer）：
- 版本唯一来源 version.py（/api/version、About、EXE 版本资源、Inno Setup 全部读它）；
- 第一实例额外监听 Local\\LlamaMonitor.Shutdown（Named Event）：收到信号走正常
  优雅关闭；`--shutdown-existing` 发信号并等待单实例 Mutex 释放（超时返回 1）。
"""

from __future__ import annotations

import argparse
import platform
import socket
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI

from app_lifecycle import AppIntegrationState, AppLifecycle, UiCommandDispatcher
from config import (
    LoadedConfig,
    app_data_dir,
    apply_overrides,
    is_frozen,
    load_config,
    setup_logging,
    trust_env_for,
)
from db import Database
from server import build_app, build_collector
from tray_manager import TrayManager
from version import APP_NAME, __version__
from windows_integration import (
    AutostartManager,
    ShowWindowListener,
    ShutdownListener,
    SingleInstance,
    notify_show_window,
    open_folder,
    request_shutdown,
)

# 等待 API ready 的预算：lifespan 首次采集最坏情况是一次抓取超时 + 余量。
# RC-002：30s 在冷启动/系统重启后不够——重启后系统负载高（开机自启任务、
# GPU 驱动重新初始化）会拖慢整个 uvicorn 事件循环，/api/status 可能 30s 内
# 不返回 200，导致 autostart 的实例误判"API 未就绪"而退出（用户需手动重启）。
# 增到 120s 容纳重启后负载；真失败（端口占用/DB 损坏）只是错误提示延迟，无副作用。
READY_TIMEOUT_SECONDS = 120.0
READY_POLL_SECONDS = 0.2
# 优雅关闭每阶段等待预算（总计约 8~12s；超时记 WARNING 后继续，不无限等待）
SHUTDOWN_TIMEOUT_SECONDS = 8.0

WINDOW_TITLE = "LlamaMonitor"
WINDOW_WIDTH = 1400
WINDOW_HEIGHT = 900
WINDOW_MIN_SIZE = (1000, 650)  # Phase 15 spec §53：最小合理尺寸（<1100px 触发 compact 导航）


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def _port_is_llamamonitor(host: str, port: int) -> bool:
    """端口上的服务是否响应 /api/status（判断为已运行的 LlamaMonitor）。"""
    try:
        # RC-004：同 wait_for_ready——本机端口探测不走系统代理
        r = httpx.get(f"http://{host}:{port}/api/status", timeout=2.0,
                      trust_env=trust_env_for(f"http://{host}:{port}"))
        return r.status_code == 200
    except Exception:
        return False


def wait_for_ready(
    base_url: str,
    timeout: float = READY_TIMEOUT_SECONDS,
    poll: float = READY_POLL_SECONDS,
) -> bool:
    """轮询 /api/status 直到 200（lifespan 首次采集完成即代表数据已可用）。"""
    # AUDIT-ASYNC-005：截止时间用 monotonic——wall clock 在等待期间被系统调整
    # （NTP 校时 / DST）会让超时判断失真
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            # RC-004：trust_env 跟随目标地址——本机 API 不走系统代理（死代理
            # 会让本函数 120s 全部超时，自启动实例误判未就绪而退出）。
            if httpx.get(base_url + "/api/status", timeout=1.0,
                         trust_env=trust_env_for(base_url)).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(poll)
    return False


def run_uvicorn_in_thread(app: FastAPI, host: str, port: int) -> tuple[uvicorn.Server, threading.Thread]:
    """在守护线程中启动 uvicorn（独立事件循环）。log_config=None：日志走 root（文件 + 控制台）。"""
    config = uvicorn.Config(app, host=host, port=port, log_config=None)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="llamamonitor-uvicorn", daemon=True)
    thread.start()
    return server, thread


def stop_uvicorn(server: uvicorn.Server, thread: threading.Thread, timeout: float = SHUTDOWN_TIMEOUT_SECONDS) -> None:
    """优雅停止：should_exit 触发 lifespan 关闭（取消 Collector/GPU、关 SQLite），再等线程结束。"""
    server.should_exit = True
    thread.join(timeout=timeout)
    if thread.is_alive():
        # AUDIT-ASYNC-003：join 超时不能静默——uvicorn 线程还活着意味着 lifespan
        # 关闭（取消采集任务/关 DB）可能未完成，用户/测试需要知道
        logger.warning(
            "uvicorn 线程未在 %.0fs 内结束（lifespan 关闭可能未完成；daemon 线程随进程退出）",
            timeout,
        )


def _tray_icon_path() -> Path:
    """托盘 / EXE 图标：与 static 相同的资源解析（dev: 项目根 assets/；frozen: _internal/assets/）。"""
    return Path(__file__).resolve().parent / "assets" / "LlamaMonitor.ico"


def _format_tokens(n: int | float) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _message_box(message: str, icon: int = 0x10) -> None:
    """
    --windowed 模式下无控制台：用系统对话框提示用户。
    icon: 0x10=MB_ICONERROR（致命失败）, 0x40=MB_ICONINFORMATION（信息）。
    阻塞直到用户点 OK——这也是"已有实例在运行"分支的退出路径。
    """
    try:
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, message, "LlamaMonitor", icon)
    except Exception:
        pass


def _fatal_dialog(message: str) -> None:
    """致命启动失败：错误图标对话框（dev 模式同样可用）。"""
    _message_box(message, 0x10)


# ---------- UI 可见性桥接（Phase 15 UI-024；RC-005 线程安全化） ----------
#
# RC-005 根因：_on_closing 在 **WinForms UI 线程** 上执行，它调用
# _notify_ui_visible -> window.evaluate_js()。pywebview(edgechromium) 的
# evaluate_js 先 Invoke(把释放回调投到 UI 线程自己的 SyncContext) 再
# semaphore.acquire() **无超时**——即"在 UI 线程上等待 UI 线程自己来释放"，
# 自死锁：UI 消息泵卡死 -> set_on_top 等跨线程窗口操作、Collector 落盘、
# /api/status 全部停摆，直到进程重启。真实 X 按钮/WM_CLOSE(tray hide)路径
# 才会走到 _on_closing；加速 UI 压测走的是 ShowWindow/JS 路径，故此前未暴露。
#
# 修复：evaluate_js 一律在**后台 worker 线程**执行（pywebview 本就允许任意
# 线程调用，内部 Invoke 会正确跨线程投递）。UI 线程 _notify_ui_visible 只把
# 最新状态入队即返回，绝不阻塞消息泵。可见性信号是 best-effort（前端另有
# document.visibilitychange 轮询兜底），延迟/合并可接受；worker 取"最新值"，
# 快速 hide/show 循环不堆积线程、不丢最终态。
_ui_vis_state: dict = {"pending": None}
_ui_vis_lock = threading.Lock()
_ui_vis_wake = threading.Event()
_ui_vis_worker_started = False


def _ui_visible_worker(state: dict, lock: threading.Lock, wake: threading.Event) -> None:
    """单例守护线程：把最新窗口可见性发给前端，跨线程调用 evaluate_js（不卡 UI）。

    state/lock/wake 以参数捕获（启动时固定），避免运行期被替换导致多 worker 串扰。
    """
    while True:
        wake.wait()
        wake.clear()
        # 排空当前突发：每次取"最新值"发送；发送期间若又有新值则继续。
        for _ in range(16):
            with lock:
                pending = state["pending"]
                if pending is None:
                    break
                state["pending"] = None
            window, visible = pending
            try:
                window.evaluate_js(
                    "if (window.__lmSetVisible) { window.__lmSetVisible(%s); }"
                    % ("true" if visible else "false")
                )
            except Exception:
                pass  # 纯 UI 增强，静默
            with lock:
                if state["pending"] is None:
                    break


def _window_op_guarded(op: Callable[[], None], timeout: float = 3.0) -> bool:
    """在守护线程上执行窗口操作，限时等待完成（超时返回 False）。

    RC-005 第二成因（防御层）：经 `Invoke` 投递的 restore/show/hide 与
    evaluate_js（跨线程 Invoke + **无超时** semaphore.acquire）在 WinForms
    UI 线程消息泵被卡时都会把调用方（llamamonitor-ui dispatcher）无限期阻塞。
    dispatcher 卡死后续连锁：托盘命令停摆 +（UI 线程 GIL 星型饿死时）
    API/Collector 停摆直到重启。set_on_top 已由 `_patch_pywebview_set_on_top`
    改为 BeginInvoke 非阻塞，不经过本守卫；本守卫覆盖其余阻塞式窗口操作。
    修复：操作一律在 daemon 线程执行 + join(timeout)——卡住的操作留在后台
    daemon 线程（parked；其 GIL 占用无法靠超时消除，但至少 dispatcher/UI
    线程绝不被无限阻塞，托盘命令保持响应——严格优于无超时版）。
    返回 True=限时内完成，False=超时（调用方自行决定回退/静默）。
    """
    done = threading.Event()
    err: dict = {}

    def _run() -> None:
        try:
            op()
        except Exception as exc:
            err["e"] = exc
        finally:
            done.set()

    t = threading.Thread(target=_run, name="llamamonitor-winop", daemon=True)
    t.start()
    if not done.wait(timeout):
        return False
    if "e" in err:
        raise err["e"]
    return True


def _notify_ui_visible(window, visible: bool) -> None:
    """把窗口真实可见性桥给前端（best-effort）。

    必须在 WinForms UI 线程上也安全（closing 处理器跑在该线程）：本函数只入队
    最新值 + 唤醒 worker 即返回，evaluate_js 在后台 worker 执行，不会自死锁
    UI 消息泵（RC-005）。window 为 None 时静默返回。
    """
    global _ui_vis_worker_started
    if window is None:
        return
    with _ui_vis_lock:
        _ui_vis_state["pending"] = (window, bool(visible))
        if not _ui_vis_worker_started:
            _ui_vis_worker_started = True
            threading.Thread(
                target=_ui_visible_worker,
                args=(_ui_vis_state, _ui_vis_lock, _ui_vis_wake),
                name="llamamonitor-ui-visible",
                daemon=True,
            ).start()
    _ui_vis_wake.set()


# ---------- RC-005（2/2）：pywebview(winforms) set_on_top 跨线程直接写 TopMost ----------
#
# 第二根因（0.16.4 托盘 20 次 hide/show 实测复现，py-spy --native dump 定位）：
# pywebview winforms 的模块级 set_on_top 是所有窗口操作中**唯一**由调用线程
# （llamamonitor-ui dispatcher / threading.Timer）**直接跨线程写** Control 的
# ——`i.TopMost = on_top` 不经过 Invoke（show/hide/restore/minimize/load_url 全部
# 经 Invoke 投递）。跨线程直接写触发 NtUserSetWindowPos（阻塞式 Win32 调用），
# 调用方 Python 线程在该调用上停住并继续持有 GIL；同时 WinForms UI 线程执行
# Python 回调（如 500ms timer）需 PyGILState_Ensure 获取 GIL => 循环等待 =>
# GIL 星型饿死 => asyncio 事件循环（/api/status + 事件循环内 Collector）停摆
# 直到进程重启。dump 实证：Thread-5 park 在 NtUserSetWindowPos（持 GIL），
# UI 线程 park 在 PyGILState_Ensure（等 GIL）。
#
# 修复（产品级，随源码走、不依赖第三方 venv 补丁）：create_window 前 monkeypatch
# 模块级 set_on_top，改为 **BeginInvoke 非阻塞投递** + **纯 .NET 方法组委托**
# Action[bool](i.set_TopMost)——.NET 委托在 UI 线程执行时无 Python 帧、不取
# GIL（与 pywebview 自身 show/hide 的 self.Show/self.Hide 模式一致）；调用线程
# 微秒级返回，绝不停留在跨线程阻塞 Win32 调用上持有 GIL，循环等待从结构上
# 消失。句柄未建/同线程时回退同线程直接写（InvokeRequired=False 无跨线程问题）。
# 仅 win32 生效、幂等；winforms 导入失败（测试 fake / 无 pythonnet）时 no-op。
def _patch_pywebview_set_on_top() -> bool:
    """对 pywebview(winforms) 施加 RC-005(2/2) 补丁。返回 True=已生效。"""
    if sys.platform != "win32":
        return False
    try:
        from webview.platforms import winforms as _wf
    except Exception:
        return False
    if getattr(_wf, "_llamamonitor_set_on_top_patched", False):
        return True
    try:
        # pythonnet 的 System shim：winforms 导入成功即 clr 已加载（上面已保证）
        from System import Action as _Action
    except Exception:
        return False

    def _safe_set_on_top(uid, on_top: bool) -> None:
        try:
            i = _wf.BrowserView.instances.get(uid)
        except Exception:
            return
        if not i:
            return
        try:
            # 句柄已建：非阻塞投递到 UI 线程（纯 .NET 委托，UI 线程执行不取 GIL）
            i.BeginInvoke(_Action[bool](i.set_TopMost), [bool(on_top)])
        except Exception:
            try:
                # 句柄未建/当前即 UI 线程：同线程直接写是安全的
                if not i.InvokeRequired:
                    i.TopMost = bool(on_top)
            except Exception:
                pass

    _wf.set_on_top = _safe_set_on_top
    _wf._llamamonitor_set_on_top_patched = True
    return True


def main(argv: list[str] | None = None, loaded: LoadedConfig | None = None) -> int:
    parser = argparse.ArgumentParser(description="LlamaMonitor 桌面入口（pywebview + 内嵌 FastAPI + 系统托盘）")
    parser.add_argument("--background", action="store_true", help="后台启动：完整运行但主窗口保持隐藏（托盘）")
    parser.add_argument("--url", default=None, help="llama-server 基础地址（覆盖 config.json）")
    parser.add_argument("--db", default=None, help="SQLite 路径（覆盖 config.json）")
    parser.add_argument("--interval", type=float, default=None, help="采集间隔秒数（覆盖 config.json）")
    # Phase 12：轻量 CLI（都不启动 Collector / FastAPI / Tray / 数据库）
    parser.add_argument("--version", action="store_true",
                        help="打印版本并退出（输出 '<APP_NAME> <version>'，退出码 0）")
    parser.add_argument("--shutdown-existing", action="store_true",
                        help="请求运行中的实例优雅退出并等待最多 10s（供安装器升级/卸载前调用）；"
                             "0=无实例或已成功退出，1=超时仍在运行")
    args = parser.parse_args(argv)
    background = args.background

    # ---------- Phase 12：轻量 CLI（先于日志/配置/单实例/DB/FastAPI/Tray） ----------
    if args.version:
        print(f"{APP_NAME} {__version__}")
        return 0
    if args.shutdown_existing:
        code = request_shutdown(timeout=10.0)
        if not is_frozen():  # 开发模式有控制台：打印结果（--windowed 无控制台保持安静）
            if code == 0:
                print(f"{APP_NAME}: running instance stopped (or no instance).")
            else:
                print(f"{APP_NAME}: running instance still active after 10s.")
        return code

    # 先挂载日志 handler（默认级别），保证 load_config / 启动失败的警告能写入 monitor.log
    setup_logging()
    if loaded is None:
        loaded = load_config()
    cfg = loaded.config
    apply_overrides(cfg, url=args.url, db_path=args.db, interval=args.interval)
    log = setup_logging(cfg.logging)

    host, port = cfg.web.host, cfg.web.port
    base_url = f"http://{host}:{port}"
    log.info("[LlamaMonitor] 启动: background=%s frozen=%s metrics=%s interval=%.1fs",
             background, is_frozen(), cfg.metrics_url, cfg.collector.poll_interval_seconds)

    # ---------- 1. 单实例（最先：先于 DB / Collector / FastAPI） ----------
    single = SingleInstance()
    if not single.acquire():
        log.info("[LlamaMonitor] 已有实例在运行：发送 ShowWindow 信号唤醒已有实例，本实例退出。")
        notify_show_window()
        return 0

    lifecycle = AppLifecycle()
    _shutdown_steps_done = threading.Event()
    shutdown_event = threading.Event()
    ui: dict = {"window": None, "webview_ok": False}
    tray_ref: dict = {"tray": None}
    tray_hint_shown = {"v": False}

    def _schedule_shutdown(reason: str) -> bool:
        # 幂等：第一次置为 stopping；销毁窗口解除 webview.start() 阻塞
        lifecycle.request_stop(reason)
        shutdown_event.set()
        w = ui["window"]
        if w is not None:
            try:
                w.destroy()
            except Exception:
                pass
        return True

    def _cmd_show() -> None:
        w = ui["window"]
        if w is not None and ui["webview_ok"]:
            try:
                # hidden -> show；minimized -> restore；然后短暂置顶带到最前
                w.on_top = True  # RC-005：monkeypatch 后非阻塞（BeginInvoke 投递 UI 线程）
                # RC-005：restore/show 走阻塞式 Invoke——守卫限 3s，dispatcher 绝不被卡死
                _window_op_guarded(lambda: (w.restore(), w.show()))
                _notify_ui_visible(w, True)

                def _drop_topmost() -> None:
                    try:
                        w.on_top = False
                    except Exception:
                        pass

                threading.Timer(0.4, _drop_topmost).start()
                return
            except Exception:
                log.warning("显示窗口失败，回退到浏览器", exc_info=True)
        webbrowser.open(base_url + "/")

    def _cmd_hide() -> None:
        w = ui["window"]
        if w is not None:
            try:
                _window_op_guarded(w.hide)  # RC-005：hide 阻塞式 Invoke——守卫限 3s
                _notify_ui_visible(w, False)
            except Exception:
                log.warning("隐藏窗口失败", exc_info=True)

    def _cmd_exit() -> None:
        _schedule_shutdown("tray/API exit")

    def _cmd_show_updates() -> None:
        """托盘 "Update Available: X"：显示窗口并切换到 Settings -> Updates（Phase 13）。"""
        w = ui["window"]
        if w is not None and ui["webview_ok"]:
            try:
                w.on_top = True  # RC-005：monkeypatch 后非阻塞（BeginInvoke 投递 UI 线程）
                # RC-005：restore/show 走阻塞式 Invoke——守卫限 3s，dispatcher 绝不被卡死
                _window_op_guarded(lambda: (w.restore(), w.show()))

                def _drop_topmost() -> None:
                    try:
                        w.on_top = False  # monkeypatch 后非阻塞，不卡 Timer 线程
                    except Exception:
                        pass

                threading.Timer(0.4, _drop_topmost).start()
                _notify_ui_visible(w, True)
                # 前端初始化时注册 window.__showUpdatesSection；页面未就绪时静默跳过。
                # RC-005：evaluate_js 跨线程 Invoke + 无超时 acquire——守卫限 3s
                try:
                    _window_op_guarded(
                        lambda: w.evaluate_js("if (window.__showUpdatesSection) { window.__showUpdatesSection(); }")
                    )
                except Exception:
                    pass
                return
            except Exception:
                log.warning("显示窗口（更新入口）失败，回退到浏览器", exc_info=True)
        webbrowser.open(base_url + "/#updates")

    def _safe_open(target: str) -> None:
        try:
            open_folder(target)
        except Exception:
            log.warning("打开文件夹失败: %s", target, exc_info=True)

    def _toggle_autostart() -> None:
        try:
            state = autostart.get_state()
            if not state["supported"]:
                return
            autostart.set_enabled(not state["enabled"])
        except Exception:
            log.warning("切换 autostart 失败", exc_info=True)

    # AUDIT-ASYNC-006：托盘状态用**持久** httpx.Client（keep-alive）——原实现每 30s
    # httpx.get 新建 TCP 连接（TIME_WAIT 累积 + 每轮握手开销）。httpx.Client 线程安全，
    # 托盘刷新线程复用同一个 client；shutdown 时关闭。
    # RC-004：本机 API 不走系统代理（死代理会让托盘 30s 刷新永远失败）
    tray_http = httpx.Client(timeout=2.0, trust_env=trust_env_for(base_url))

    def _tray_status() -> dict:
        # 复用 server 每轮采集刷新的 app_state.runtime（托盘不额外高频查库）
        rt = app_state.runtime
        online = rt.get("llama_online")
        llama = "Online" if online else ("Offline" if online is False else "Starting")
        if gpu is None or not cfg.gpu.enabled:
            gpu_text = "Disabled"
        elif rt.get("gpu_available"):
            gpu_text = f"{rt.get('gpu_count', 0)} Online"
        else:
            gpu_text = "Offline"
        try:
            auto_enabled = autostart.get_state()["enabled"]
        except Exception:
            auto_enabled = False
        # Phase 13：有可用更新时托盘显示 "Update Available: X"（本地 loopback API，开销可忽略）
        update_version = None
        try:
            upd = tray_http.get(base_url + "/api/update/status").json()
            if upd.get("state") == "UPDATE_AVAILABLE":
                update_version = upd.get("available_version")
        except Exception:
            pass
        return {
            "llama": llama,
            "gpu": gpu_text,
            "today": _format_tokens(rt.get("today_logical_tokens", 0)),
            "autostart_enabled": auto_enabled,
            "update_available": update_version,
        }

    dispatcher = UiCommandDispatcher(
        {"show": _cmd_show, "hide": _cmd_hide, "exit": _cmd_exit,
         "open_updates": _cmd_show_updates}
    )
    dispatcher.start()

    listener = ShowWindowListener(on_signal=lambda: dispatcher.request("show"))
    if not listener.start():
        log.warning("ShowWindow 监听线程启动失败（单实例仍有效，仅失去第二实例唤醒）")

    # Phase 12：Shutdown 监听（安装器/`--shutdown-existing` 请求优雅退出；
    # 收到信号 -> 正常 _schedule_shutdown 路径，绝不 TerminateProcess）
    shutdown_listener = ShutdownListener(on_signal=lambda: dispatcher.request("exit"))
    if not shutdown_listener.start():
        log.warning("Shutdown 监听线程启动失败（--shutdown-existing / 安装器将无法请求优雅退出）")

    # ---------- 2. 端口防御（正常第二实例已被 mutex 拦截并唤醒第一实例） ----------
    if _port_in_use(host, port):
        if _port_is_llamamonitor(host, port):
            log.warning("[LlamaMonitor] %s 已有 LlamaMonitor 在运行（mutex 未拦截），用默认浏览器打开。", base_url)
            webbrowser.open(base_url + "/")
            # 阻塞对话框：给用户明确的退出路径（点 OK 本实例退出，已有实例不受影响）。
            # --windowed 无控制台，这是唯一能"等到用户确认"的同步点。
            _message_box(
                "LlamaMonitor 已在运行。\n\n"
                "已用默认浏览器打开其 Dashboard。\n"
                "点击 OK 退出本实例（已运行的实例不受影响）。",
                0x40,  # MB_ICONINFORMATION
            )
            # 该分支尚未创建 tray/uvicorn/window：手工清理已创建的资源
            _shutdown_steps_done.set()
            dispatcher.stop()
            listener.stop()
            shutdown_listener.stop()
            tray_http.close()
            single.release()
            lifecycle.mark_stopped()
            return 0
        log.error("[LlamaMonitor] 端口 %d 已被其他程序占用，请关闭后重试。", port)
        _fatal_dialog(f"端口 {port} 已被其他程序占用，请关闭后重试。")
        _shutdown_steps_done.set()
        dispatcher.stop()
        listener.stop()
        shutdown_listener.stop()
        tray_http.close()
        single.release()
        lifecycle.mark_stopped()
        return 1

    # ---------- 3. 数据库 / 采集器 / FastAPI ----------
    # Phase 12：schema 迁移前自动快照（pre-migration backup）-> %LOCALAPPDATA%\LlamaMonitor\backups
    db = Database(
        cfg.database_path,
        wal=cfg.database.wal,
        retention_seconds=cfg.collector.live_retention_hours * 3600,
        pre_migration_backup_dir=app_data_dir() / "backups",
    )
    collector, gpu = build_collector(cfg, db)

    # 开机自启管理（只读写 HKCU Run 的 LlamaMonitor 值；dev 模式不支持）
    exe_path = Path(sys.executable) if is_frozen() else None
    autostart = AutostartManager(exe_path)

    # 应用集成状态（注入 FastAPI）
    app_state = AppIntegrationState(
        background=background,
        single_instance=True,
        tray_available=False,
        start_monotonic=lifecycle.start_monotonic,
        integration_provider=lambda: {
            "platform": "windows" if sys.platform == "win32" else sys.platform,
            "python": platform.python_implementation() + " " + platform.python_version(),
            "frozen": is_frozen(),
            "tray_supported": tray_ref["tray"].available if tray_ref["tray"] else False,
            "single_instance": True,
            "background": background,
            "executable": str(exe_path) if exe_path else None,
            "app_data": str(app_data_dir()),
            "uptime_seconds": int(time.monotonic() - app_state.start_monotonic),
            "autostart": autostart.get_state(),
        },
        set_autostart=autostart.set_enabled,
        request_exit=lambda: _schedule_shutdown("api exit"),
    )

    app = build_app(db, collector, loaded, gpu, app_state=app_state)
    server, uv_thread = run_uvicorn_in_thread(app, host, port)

    log.info("[LlamaMonitor] 正在启动 FastAPI（首次采集中）... metrics=%s interval=%.1fs",
             cfg.metrics_url, cfg.collector.poll_interval_seconds)

    def _perform_shutdown() -> None:
        """优雅关闭（_shutdown_steps_done 保证步骤只执行一次；各阶段超时后继续）。"""
        lifecycle.request_stop("graceful shutdown")
        if _shutdown_steps_done.is_set():
            return
        _shutdown_steps_done.set()
        t0 = time.monotonic()
        log.info("[LlamaMonitor] 开始优雅关闭（%s）", lifecycle.stop_reason)

        def _step(name: str, fn) -> None:
            try:
                fn()
            except Exception:
                log.warning("关闭阶段失败（继续后续清理）: %s", name, exc_info=True)

        _step("停止托盘", lambda: tray_ref["tray"].stop() if tray_ref["tray"] else None)
        _step("关闭托盘 HTTP client", lambda: tray_http.close())
        # lifespan 关闭：取消 Collector/GPU 任务并等待在途采样结束，最后关 SQLite
        _step("停止 FastAPI/Collector/GPU/SQLite", lambda: stop_uvicorn(server, uv_thread))
        _step("关闭窗口", lambda: ui["window"].destroy() if ui["window"] else None)
        _step("停止 UI 分发器", dispatcher.stop)
        _step("停止 ShowWindow 监听", listener.stop)
        _step("停止 Shutdown 监听", shutdown_listener.stop)
        _step("释放单实例 Mutex", single.release)
        lifecycle.mark_stopped()
        log.info("[LlamaMonitor] 已停止：Collector 已取消、SQLite 已关闭、托盘已停止、单实例已释放（%.1fs）。",
                 time.monotonic() - t0)

    try:
        if not wait_for_ready(base_url):
            log.error("[LlamaMonitor] API 未能在限时内就绪，退出。")
            _fatal_dialog("LlamaMonitor 未能启动：API 未就绪，详见 monitor.log。")
            return 1
        log.info("[LlamaMonitor] API 就绪。")

        # ---------- Phase 13：更新成功检测 + 旧更新文件清理 ----------
        # 上一版本安装前写了 updates/pending_update.json；新版启动时若 to_version ==
        # 当前版本 => 记 update_success 事件并删除 marker（不匹配 => 警告事件）。
        # 顺带清理 >24h 的 .part 与旧版本目录（不动 backups / pending marker）。
        try:
            from update_service import UpdateService, check_pending_update

            result = check_pending_update(db, __version__, app_data_dir())
            if result == "success":
                log.info("[LlamaMonitor] 更新成功（pending_update 与当前版本匹配）：已记录 update_success 事件。")
            elif result == "mismatch":
                log.warning("[LlamaMonitor] pending_update 版本不匹配（已记录 update_failed_mismatch 事件）。")
            UpdateService(updates_dir=app_data_dir() / "updates").cleanup_stale()
        except Exception:
            log.warning("更新状态检测失败（不影响启动）", exc_info=True)

        # ---------- Phase 11：启动时数据库健康检查（lifespan 已 quick_check） ----------
        # 损坏 -> 提示用户"请使用 Backup 导出/恢复"（不自动删除、不自动重建、不自动修复）；
        # UI 的 Data Quality 区域与 Settings->Data 同时展示健康状态。
        try:
            import httpx as _httpx
            health = _httpx.get(base_url + "/api/health", timeout=5.0,
                                trust_env=trust_env_for(base_url)).json()
            if health.get("database") == "corrupt":
                log.error("[LlamaMonitor] 数据库完整性检查失败（protective mode，只读）。")
                _message_box(
                    "检测到数据库完整性问题（Database integrity issue）。\n\n"
                    "监控继续运行，但暂停写入数据库（保护现有数据）。\n"
                    "建议：在 Settings -> Data 中运行 Database Check，\n"
                    "或使用 Backup 导出当前数据。\n\n"
                    "LlamaMonitor 不会自动删除或重建数据库。",
                    icon=0x30,
                )
            elif health.get("database") == "incompatible":
                # Phase 12：数据库由更新版本创建（降级保护）/ pre-migration backup 失败
                log.error("[LlamaMonitor] 数据库处于 incompatible 状态（只读保护模式）。")
                _message_box(
                    "This database was created by a newer version of LlamaMonitor.\n"
                    "Please upgrade the application.\n\n"
                    "检测到数据库 schema 比当前程序更新（或迁移前备份失败）：\n"
                    "监控继续以只读方式运行，暂停写入数据库。\n"
                    "LlamaMonitor 不会自动降低 schema 或修改数据。\n\n"
                    f"详情: {health.get('database_detail') or ''}",
                    icon=0x30,
                )
        except Exception:
            log.warning("启动健康检查读取失败（不影响启动）", exc_info=True)

        # ---------- 4. 系统托盘（失败不拖垮主程序） ----------
        tray = TrayManager(
            icon_path=_tray_icon_path(),
            # 注意：pystray 以 (icon, menu_item) 两个参数调用 action（左键默认项
            # Menu.__call__ 与右键 _handler 两条路径都是 action(icon, item)）——
            # 零参 lambda 会 TypeError 且被 pystray 内部吞掉（菜单"点了没反应"）。
            commands={
                "open": lambda *a: dispatcher.request("show"),
                "open_updates": lambda *a: dispatcher.request("open_updates"),
                "exit": lambda *a: _cmd_exit(),
                "open_data": lambda *a: _safe_open("data"),
                "open_logs": lambda *a: _safe_open("logs"),
                "autostart_toggle": lambda *a: _toggle_autostart(),
            },
            status_provider=_tray_status,
        )
        tray.start()
        tray_ref["tray"] = tray
        app_state.tray_available = tray.available
        if not tray.available:
            log.warning("[LlamaMonitor] 托盘不可用: %s", tray.reason)
            if background:
                # background + tray 失败：不能让用户"完全找不回"，自动显示 Dashboard
                log.warning("[LlamaMonitor] Tray failed in background mode; showing dashboard instead.")
                background = False

        # ---------- 5. pywebview 主窗口（主线程，阻塞直到真正退出） ----------
        start_hidden = background
        try:
            import webview as webview_module

            # RC-005(2/2)：create_window 前打 set_on_top 补丁（须在窗口创建前，
            # 保证 on_top setter 绑定的 gui.set_on_top 已是安全实现）
            _patch_pywebview_set_on_top()

            window = webview_module.create_window(
                WINDOW_TITLE,
                base_url + "/",
                width=WINDOW_WIDTH,
                height=WINDOW_HEIGHT,
                min_size=WINDOW_MIN_SIZE,
                hidden=start_hidden,
            )

            def _on_closing(window) -> bool | None:
                # 参数名必须是 window（pywebview 按参数名注入窗口对象）
                # 真正退出（stopping/stopped）：允许销毁；否则隐藏到托盘（监控继续）
                if lifecycle.state != AppLifecycle.RUNNING:
                    return None
                try:
                    window.hide()
                    _notify_ui_visible(window, False)
                except Exception:
                    log.warning("隐藏窗口失败，按退出处理", exc_info=True)
                    return None
                if not tray_hint_shown["v"] and tray_ref["tray"]:
                    tray_hint_shown["v"] = True
                    tray_ref["tray"].notify("LlamaMonitor is still running in the system tray.")
                log.info("[LlamaMonitor] 窗口已关闭 -> 隐藏到托盘（监控继续；Tray->Exit 才真正退出）")
                return False  # 取消关闭

            window.events.closing += _on_closing
            ui["window"] = window
            ui["webview_ok"] = True  # 窗口已创建：show/hide 命令可直接操作
            webview_module.start()  # 阻塞：点 X 被拦截隐藏，只有 destroy 才返回
        except Exception as exc:
            log.warning("[LlamaMonitor] pywebview 不可用: %r", exc)
            # 回退：前台 -> 浏览器；后台(托盘正常) -> 仅托盘继续
            if not start_hidden:
                webbrowser.open(base_url + "/")
                log.info("[LlamaMonitor] 回退：系统默认浏览器已打开，Tray->Exit 退出。")
            # 等待退出信号（Tray->Exit / API exit / Ctrl+C）
            shutdown_event.wait()

        # ---------- 6. 优雅关闭（主线程） ----------
        _perform_shutdown()
        return 0
    except KeyboardInterrupt:
        log.info("[LlamaMonitor] 收到 Ctrl+C，退出。")
        _perform_shutdown()
        return 0
    finally:
        # 兜底：任何路径（含 wait_for_ready 失败提前 return）都确保释放（幂等）
        _perform_shutdown()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        # --windowed 无控制台：未捕获异常必须写日志 + 弹窗
        import logging

        logging.getLogger("llamamonitor").exception("LlamaMonitor 启动崩溃")
        _fatal_dialog("LlamaMonitor 启动时发生错误，详见 monitor.log。")
        sys.exit(1)
