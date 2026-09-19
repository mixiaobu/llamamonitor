"""
tray_manager.py — 系统托盘（pystray + Pillow，Phase 10）

- 图标资源：assets/LlamaMonitor.ico（与 PyInstaller EXE 同一视觉资源，
  由 tools/generate_icon.py 生成的多尺寸 ICO）；
- 菜单（右键）：
    Open Dashboard（默认动作：单击托盘图标即触发，pystray win32 标准行为）
    ----
    llama.cpp: Online / GPU: 2 Online / Today: 1.23M tokens（disabled 信息项）
    ----
    Open Data Folder / Open Log Folder
    ----
    Start with Windows（checkable，状态来自真实 Registry 缓存）
    ----
    Exit
- tooltip：状态变化时更新（LlamaMonitor / LlamaMonitor - Online /
  LlamaMonitor - llama.cpp Offline），不每 5 秒重建；
- 状态数据来自 desktop 注入的 status_provider（复用 collector/gpu/DB 的
  现有状态，托盘自身不额外高频查库）；内部 30s 慢刷新线程负责更新菜单
  （pystray MenuItem 不可变 -> 重建 menu 对象 + icon.menu setter 自动
  update_menu()）；
- 独立线程运行（icon.run() 阻塞）；stop() 受控停止；
- pystray 导入/初始化失败 -> available=False + reason，绝不让托盘问题
  拖垮监控主程序（desktop 决定回退行为）。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

logger = logging.getLogger("llamamonitor.tray")

# 状态刷新周期：菜单/tooltip 最多滞后 30s（托盘信息项不是实时数据）
STATUS_REFRESH_SECONDS = 30.0


class TrayManager:
    """
    托盘生命周期管理。

    commands: {"open": fn, "exit": fn, "open_data": fn, "open_logs": fn,
               "autostart_toggle": fn}
    status_provider: () -> {"llama": "Online", "gpu": "2 Online",
                            "today": "1.23M", "autostart_enabled": bool}
    icon_path: assets/LlamaMonitor.ico
    """

    def __init__(self, icon_path: str | Path, commands: dict, status_provider) -> None:
        self._icon_path = Path(icon_path)
        self._commands = commands
        self._status_provider = status_provider
        self._icon = None
        self._thread: threading.Thread | None = None
        self._updater: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._available = False
        self._reason: str | None = None
        # 当前状态缓存（status_provider 结果）：供 checked 回调与 tooltip 比较用
        self._status: dict = {}
        self._tooltip = "LlamaMonitor"

    # ------------------------------------------------------------------ 状态

    @property
    def available(self) -> bool:
        return self._available

    @property
    def reason(self) -> str | None:
        return self._reason

    # ------------------------------------------------------------------ 菜单

    def _build_menu(self, pystray) -> object:
        st = self._status
        c = self._commands
        items = [
            pystray.MenuItem("Open Dashboard", c["open"], default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"llama.cpp: {st.get('llama', 'Starting')}", None, enabled=False),
            pystray.MenuItem(f"GPU: {st.get('gpu', 'Unknown')}", None, enabled=False),
            pystray.MenuItem(f"Today: {st.get('today', '0')} tokens", None, enabled=False),
            pystray.Menu.SEPARATOR,
        ]
        # Phase 13：有可用更新时显示 "Update Available: X"（点击 -> Dashboard 更新页）
        update_version = st.get("update_available")
        if update_version:
            items.append(pystray.MenuItem(
                f"Update Available: {update_version}",
                c.get("open_updates") or c["open"],
            ))
            items.append(pystray.Menu.SEPARATOR)
        items.extend([
            pystray.MenuItem("Open Data Folder", c["open_data"]),
            pystray.MenuItem("Open Log Folder", c["open_logs"]),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Start with Windows",
                c["autostart_toggle"],
                checked=lambda _item: bool(st.get("autostart_enabled")),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", c["exit"]),
        ])
        return pystray.Menu(*items)

    def _apply_status(self) -> None:
        """刷新状态：tooltip 变化才更新；菜单在状态变化时重建（30s 周期调用）。"""
        try:
            st = self._status_provider()
        except Exception:
            logger.exception("托盘状态 provider 失败")
            return
        changed = st != self._status
        self._status = st
        if self._icon is None:
            return
        tooltip = _tooltip_for(st)
        if tooltip != self._tooltip:
            self._tooltip = tooltip
            try:
                self._icon.title = tooltip
            except Exception:
                logger.warning("更新托盘 tooltip 失败", exc_info=True)
        if changed:
            # MenuItem 不可变：状态变化才重建 menu（icon.menu setter 内部自动
            # update_menu()）；无变化时不做任何 Win32 调用
            try:
                self._icon.menu = self._build_menu(_pystray())
            except Exception:
                logger.warning("刷新托盘菜单失败", exc_info=True)

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> None:
        """初始化托盘图标并启动运行线程 + 状态刷新线程。失败只记录原因。"""
        try:
            pystray = _pystray()
            image = _load_icon_image(self._icon_path)
            self._status = self._status_provider()
            self._tooltip = _tooltip_for(self._status)
            self._icon = pystray.Icon(
                "LlamaMonitor", image, self._tooltip, self._build_menu(pystray)
            )
        except Exception as exc:
            self._available = False
            self._reason = f"{type(exc).__name__}: {exc}"
            logger.warning("托盘初始化失败（监控继续运行）: %r", exc)
            return

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="llamamonitor-tray", daemon=True)
        self._thread.start()
        self._updater = threading.Thread(target=self._update_loop, name="llamamonitor-tray-status", daemon=True)
        self._updater.start()
        self._available = True
        self._reason = None
        logger.info("托盘已启动")

    def _run(self) -> None:
        try:
            self._icon.run()
        except Exception:
            logger.exception("托盘运行线程异常退出")
        finally:
            self._available = False

    def _update_loop(self) -> None:
        # 先睡后刷：启动时已有初始菜单
        while not self._stop.wait(STATUS_REFRESH_SECONDS):
            self._apply_status()

    def notify(self, message: str) -> None:
        """一次性气泡提示（best-effort，失败静默——不依赖通知机制）。"""
        if self._icon is None:
            return
        try:
            self._icon.notify(message)
        except Exception:
            pass

    def stop(self, timeout: float = 3.0) -> None:
        """受控停止：stop 图标消息循环 -> 等线程结束 -> 停刷新线程。幂等。"""
        with self._lock:
            if self._stop.is_set() and self._thread is None and self._updater is None:
                return
        self._stop.set()
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                logger.warning("icon.stop() 失败", exc_info=True)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("托盘线程未能在 %.1fs 内退出", timeout)
            self._thread = None
        if self._updater is not None:
            self._updater.join(timeout=1.0)
            if self._updater.is_alive():
                # AUDIT-ASYNC-003：join 超时不能静默（刷新线程卡住 = 托盘状态停更）
                logger.warning("托盘状态刷新线程未能在 1.0s 内退出")
            self._updater = None
        self._available = False


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _pystray():
    import pystray  # 延迟导入：非 Windows / 未安装时不影响主程序

    return pystray


def _load_icon_image(icon_path: Path):
    """加载 ICO -> 64x64 RGBA（pystray win32 推荐尺寸；ICO 本身含多尺寸）。"""
    from PIL import Image

    image = Image.open(icon_path)
    image = image.convert("RGBA")
    if image.size != (64, 64):
        image = image.resize((64, 64), Image.LANCZOS)
    return image


def _tooltip_for(status: dict) -> str:
    llama = status.get("llama")
    if llama == "Online":
        return "LlamaMonitor - Online"
    if llama == "Offline":
        return "LlamaMonitor - llama.cpp Offline"
    return "LlamaMonitor"
