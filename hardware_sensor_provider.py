"""
hardware_sensor_provider.py — 高级硬件传感器 Provider（1.1.0 Stage C）

Windows 基础 API（psutil）无法统一提供 CPU 温度 / CPU Package 功耗 / 主板温度 /
风扇 RPM / 风扇控制百分比 / VRM / Chipset / NVMe 温度。本模块抽象
HardwareSensorProvider 接口，默认实现为 **LibreHardwareMonitorBridge**：

- 常驻 C# Helper（native/hardware_bridge/HardwareSensorBridge.exe +
  LibreHardwareMonitorLib.dll，.NET Framework 4.8 —— Win10 1903+/Win11 内置，
  Clean Machine 零额外 runtime 部署）；
- 通信：stdout JSON Lines（周期 5s）；无额外 HTTP 端口；
- 严格只读：Bridge 只调用 LibreHardwareMonitorLib 的 Read()，
  绝不 Set Fan / Set Clock / Set Voltage / Set Power（GPU Provider 禁用，
  GPU 由 nvidia-smi 侧负责，避免重复采集）；
- 生命周期：LlamaMonitor 启动时 CREATE_NO_WINDOW 拉起；退出时优雅终止
  （先关 stdin 再 Terminate，绝不留下孤儿 HardwareSensorBridge.exe）；
  崩溃自动有限重启（指数退避 2s/4s/8s/.../30s 上限）；
- 状态机：available（有传感器数据）/ partial（Bridge 活但暂无数据）/
  unavailable（Bridge 缺失/反复崩溃/非 Windows）；状态转换才记日志 + 事件，
  不刷屏；
- **故障隔离**：Provider 任何失败不影响 psutil 基础系统监控，也不影响
  llama Token 采集 / GPU 采集。

Provider 不可用时所有高级字段 = None（UI 显示 --，绝不显示 0 冒充真实值）。
Fan 的 control_percent 只有 LHM 真实提供 Control 传感器时才有值——
绝不从 RPM / 假定 MaxRPM 推算。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from clock import Clock, default_clock
from config import AppConfig
from db import Database

logger = logging.getLogger("llamamonitor.hwsensors")

_IS_WINDOWS = sys.platform == "win32"
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0

# 重启退避（秒）：2/4/8/16/30 上限；成功输出后重置
_BACKOFF_STEPS = (2.0, 4.0, 8.0, 16.0, 30.0)
# 连续失败多少次后进入"长期不可用"（仍按上限周期重试，但不再频繁重启）
_MAX_BACKOFF_INDEX = len(_BACKOFF_STEPS) - 1
# 单行 JSON 上限（防御：Bridge 异常输出巨行）
_MAX_LINE_BYTES = 1024 * 1024
# 传感器新鲜度：超过该秒数的传感器值视为过期（-> None）
_SENSOR_STALE_SECONDS = 30.0


def find_bridge_exe() -> tuple[Path, Path] | None:
    """
    定位 Bridge 可执行文件（HardwareSensorBridge.exe + 同目录 LibreHardwareMonitorLib.dll）。
    - PyInstaller onedir：_internal 目录（与 LlamaMonitor.exe 同级布局，见 scripts/）；
    - 开发模式：native/hardware_bridge/（相对项目根）；
    - 找不到或 DLL 缺失 -> None（高级传感器 unavailable，应用正常启动）。
    """
    candidates: list[Path] = []
    try:
        import config as _config
        if _config.is_frozen():
            # PyInstaller onedir：sys._MEIPASS 指向 _internal（onedir 下 = 应用目录）
            base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
            candidates.append(base / "hardware_bridge" / "HardwareSensorBridge.exe")
            candidates.append(base / "HardwareSensorBridge.exe")
    except Exception:
        pass
    # 开发模式：项目根 / native/hardware_bridge
    try:
        here = Path(__file__).resolve().parent
        candidates.append(here / "native" / "hardware_bridge" / "HardwareSensorBridge.exe")
    except Exception:
        pass
    for exe in candidates:
        if exe.is_file():
            dll = exe.parent / "LibreHardwareMonitorLib.dll"
            if dll.is_file():
                return exe, dll
            # DLL 缺失：记录一次 WARNING（仍返回 exe，启动会失败 -> unavailable）
            logger.warning("HardwareSensorBridge.exe 存在但 LibreHardwareMonitorLib.dll 缺失: %s", exe.parent)
            return exe, dll
    return None


class HardwareSensorProvider:
    """
    高级硬件传感器 Provider（LibreHardwareMonitor Bridge 管理）。

    状态（供 /api/system/sensors 与 Settings 页面）：
    - state: "available" | "partial" | "unavailable"
    - latest: {sensor_key: {"value","unit","hardware_name","sensor_name","type","timestamp"}}
    - fans:   [{"name","rpm","control_percent","source"}]
    - counts: {"cpu": n, "motherboard": n, "cooling": n, "storage": n}

    线程模型：Bridge 是子进程，stdout 由**独立 reader 线程**读取（不阻塞事件循环）；
    主线程（事件循环）通过 get_latest()/snapshot() 读内存态（GIL 保护的 dict 替换，
    读旧引用安全）。
    """

    def __init__(
        self,
        config: AppConfig,
        db: Database | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.clock = clock or default_clock
        self.enabled = config.system.advanced_sensors and _IS_WINDOWS
        self.state: str = "unavailable"
        self._last_state: str | None = None
        self.latest: dict[str, dict] = {}
        self.fans: list[dict] = []
        self.counts: dict[str, int] = {"cpu": 0, "motherboard": 0, "cooling": 0, "storage": 0}
        self.last_bridge_update: float | None = None
        # Bridge 进程
        self._proc: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._restart_index = 0
        self._bridge_missing_logged = False
        self._lock = threading.Lock()  # 保护 _proc / _stop（优雅终止与重启竞态）
        self._runner: Callable[[], None] | None = None  # 测试注入：替代真实子进程

    # ---------- 生命周期 ----------

    def start(self) -> None:
        """启动 reader 线程（实际拉起 Bridge 在线程内进行，可退避重试）。

        AUDIT-1.1.1 BUG-1111-007：启动前先清理孤儿 HardwareSensorBridge 进程
        （LlamaMonitor 崩溃/被任务管理器强杀时 stop() 没跑，子进程可能残留）。
        """
        if self._reader_thread is not None or not self.enabled:
            return
        self.sweep_orphans()
        self._stop.clear()
        self._reader_thread = threading.Thread(
            target=self._supervise_loop, name="hwsensors-bridge", daemon=True
        )
        self._reader_thread.start()

    def sweep_orphans(self) -> int:
        """
        启动时清理孤儿 HardwareSensorBridge 进程（AUDIT-1.1.1 BUG-1111-007）。

        LlamaMonitor 崩溃或被强杀时 stop() 没执行，Bridge 子进程可能残留
        （父进程没了但 Bridge 还活着，每 5s 空转）。这里按可执行文件名
        找同名的 HardwareSensorBridge.exe（排除本进程），逐个 Terminate。
        返回清理的进程数。任何失败都不抛（不影响启动）。
        """
        if not _IS_WINDOWS:
            return 0
        killed = 0
        try:
            import psutil
        except ImportError:
            return 0
        own_pid = os.getpid()
        for p in psutil.process_iter(["pid", "name", "exe"]):
            try:
                pid = p.info["pid"]
                name = p.info.get("name") or ""
                if pid == own_pid or name.lower() != "hardwaresensorbridge.exe":
                    continue
                # 保守：只 kill 我们认识的 bridge exe 路径（避免误杀同名进程）
                exe = p.info.get("exe") or ""
                found = find_bridge_exe()
                if found and exe and Path(exe).resolve() == found[0].resolve():
                    p.terminate()
                    try:
                        p.wait(timeout=3.0)
                    except Exception:
                        with _suppress():
                            p.kill()
                    killed += 1
                    logger.info("启动时清理孤儿 HardwareSensorBridge 进程 pid=%s", pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception as exc:
                logger.debug("sweep_orphans 单进程处理失败: %r", exc)
        return killed

    def stop(self) -> None:
        """
        优雅终止（幂等）：置 stop 标志 -> 关 Bridge stdin（Bridge 检测到 EOF 退出）
        -> 宽限 3s -> Terminate。绝不留下孤儿 HardwareSensorBridge.exe。
        """
        self._stop.set()
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is not None:
            try:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()  # Bridge 读 EOF 正常退出
            except Exception:
                pass
            try:
                proc.wait(timeout=3.0)
            except Exception:
                with _suppress():
                    proc.terminate()
                try:
                    proc.wait(timeout=3.0)
                except Exception:
                    with _suppress():
                        proc.kill()
        self._set_state("unavailable", force=True)

    # ---------- 监督循环（reader 线程内） ----------

    def _supervise_loop(self) -> None:
        """拉起 Bridge -> 读 stdout 逐行解析 -> 崩溃/挂死退避重启。"""
        while not self._stop.is_set():
            proc = self._start_bridge()
            if proc is None:
                # Bridge 文件缺失（或创建失败）：记一次 WARNING，按上限周期重试
                if not self._bridge_missing_logged:
                    logger.warning("HardwareSensorBridge 不可用（未找到可执行文件）：高级硬件传感器不可用，基础系统监控继续")
                    self._bridge_missing_logged = True
                    self._record_event("hardware_sensor_provider_unavailable", "warning")
                self._set_state("unavailable")
                self._stop.wait(_BACKOFF_STEPS[_MAX_BACKOFF_INDEX])
                continue
            self._bridge_missing_logged = False
            hung = self._pump_stdout(proc)
            if self._stop.is_set():
                break
            # AUDIT-1.1.1 REL-1111-009（挂死看门狗）：Bridge 活着但长时间无输出
            # -> 主动 kill 再重启。原实现 readline 无限阻塞，Bridge hang（不退出
            # 也不输出）时监督循环永不触发，孤儿进程一直占资源。两种结束（崩溃
            # 退出 / 挂死）都走退避。
            if hung:
                self._kill_proc(proc)
                logger.warning(
                    "HardwareSensorBridge 挂死（%.0fs 无输出），已 kill 并退避重启",
                    self._hang_timeout_seconds(),
                )
            else:
                logger.info("HardwareSensorBridge 退出（code=%s），退避重启", proc.poll())
            delay = _BACKOFF_STEPS[self._restart_index]
            self._restart_index = min(self._restart_index + 1, _MAX_BACKOFF_INDEX)
            self._stop.wait(delay)

    def _hang_timeout_seconds(self) -> float:
        """挂死看门狗阈值：3 × 传感器轮询周期（正常每周期至少一条输出）。"""
        try:
            interval = max(1.0, float(self.config.system.advanced_sensor_interval_seconds))
        except (TypeError, ValueError):
            interval = 5.0
        return max(15.0, interval * 3.0)

    def _kill_proc(self, proc: subprocess.Popen) -> None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3.0)
                return
            except Exception:
                pass
        except Exception:
            pass
        with _suppress():
            proc.kill()
        try:
            proc.wait(timeout=3.0)
        except Exception:
            pass

    def _pump_stdout(self, proc: subprocess.Popen) -> bool:
        """
        读取 Bridge stdout（JSON Lines）直至进程退出或挂死超时。

        AUDIT-1.1.1 REL-1111-009：独立 reader 线程阻塞 readline（不碰事件循环、
        不用 select——Windows 上 select 对 Popen 管道 TextIOWrapper 不可靠，
        WSAStartup 报错），行入 queue；本方法（监督线程）用 1s 超时的 queue
        .get 取数据：进程退出且队列空 -> EOF；超过看门狗阈值无行 -> 挂死
        （返回 True，调用方 kill）。健康 Bridge 每周期（5s）至少一行，远早于
        阈值（3×周期）。
        """
        import queue as _queue
        q: _queue.Queue = _queue.Queue()
        sentinel = object()

        def _reader() -> None:
            assert proc.stdout is not None
            try:
                while True:
                    line = proc.stdout.readline()
                    if not line:
                        break  # EOF
                    q.put(line)
            except Exception:
                pass
            finally:
                q.put(sentinel)

        rt = threading.Thread(target=_reader, name="hwsensors-stdout", daemon=True)
        rt.start()
        last_line_time = self.clock.monotonic()
        hung = False
        try:
            while not self._stop.is_set():
                try:
                    item = q.get(timeout=1.0)
                except _queue.Empty:
                    if self.clock.monotonic() - last_line_time > self._hang_timeout_seconds():
                        if proc.poll() is None:
                            hung = True
                        break
                    continue
                if item is sentinel:
                    break  # EOF（reader 读到末尾，进程已退出）
                last_line_time = self.clock.monotonic()
                line = item
                if len(line) > _MAX_LINE_BYTES:
                    continue
                try:
                    payload = json.loads(line.decode("utf-8", "replace"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(payload, dict) and payload.get("kind") == "sensors":
                    self._ingest(payload)
        except Exception as exc:
            logger.warning("HardwareSensorBridge stdout 读取异常: %r", exc)
        # 确保 reader 线程收尾（EOF 时已自然结束；hang 时 proc 即将被 kill ->
        # reader 读到 EOF）。join 限时 2s：hang 路径下 reader 仍阻塞在 readline，
        # 等调用方 kill 后下一轮自然结束；2s 足够 EOF 场景收尾，又不至拖慢重启。
        rt.join(timeout=2.0)
        return hung

    def _start_bridge(self) -> subprocess.Popen | None:
        if self._runner is not None:
            # 测试注入
            try:
                return self._runner()
            except Exception as exc:
                logger.warning("Bridge runner 失败: %r", exc)
                return None
        found = find_bridge_exe()
        if found is None:
            return None
        exe, _dll = found
        try:
            interval = max(1.0, float(self.config.system.advanced_sensor_interval_seconds))
            proc = subprocess.Popen(
                [str(exe), f"--interval={interval:.3f}"],
                stdout=subprocess.PIPE,
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=_NO_WINDOW,  # 不弹控制台窗口
                bufsize=1,  # 行缓冲（Python 侧）
            )
            with self._lock:
                self._proc = proc
            return proc
        except Exception as exc:
            logger.warning("HardwareSensorBridge 启动失败: %r", exc)
            return None

    # ---------- 数据注入 ----------

    def _ingest(self, payload: dict) -> None:
        """把 Bridge 一轮传感器数据装入内存态（整体替换 latest，读旧引用安全）。"""
        now = self.clock.now()
        sensors = payload.get("sensors")
        if not isinstance(sensors, list):
            return
        latest: dict[str, dict] = {}
        for s in sensors:
            if not isinstance(s, dict):
                continue
            value = s.get("value")
            if not isinstance(value, (int, float)):
                continue
            key = f"{s.get('hardware_type', '')}|{s.get('sensor_type', '')}|{s.get('sensor_name', '')}|{s.get('index', 0)}"
            latest[key] = {
                "value": float(value),
                "unit": s.get("unit"),
                "hardware_type": s.get("hardware_type"),
                "hardware_name": s.get("hardware_name"),
                "sensor_type": s.get("sensor_type"),
                "sensor_name": s.get("sensor_name"),
                "timestamp": now,
            }
        self.latest = latest
        self.last_bridge_update = now
        self._restart_index = 0  # 成功输出 -> 退避重置
        self._rebuild_fans_and_counts(latest)

    def _rebuild_fans_and_counts(self, latest: dict[str, dict]) -> None:
        """从传感器集合重建 fans 列表（RPM 与 Control 配对）与分类计数。"""
        fans_rpm: dict[str, dict] = {}
        for entry in latest.values():
            if entry["sensor_type"] != "fan":
                continue
            hw_type = entry["hardware_type"] or ""
            hw_name = entry["hardware_name"] or "Cooling"
            fans_rpm[hw_name] = {
                "name": hw_name,
                "rpm": entry["value"],
                "control_percent": None,  # 有 Control 传感器时才填（绝不从 RPM 推算）
                "source": "LibreHardwareMonitor",
            }
        for entry in latest.values():
            if entry["sensor_type"] != "control":
                continue
            hw_name = entry["hardware_name"] or "Cooling"
            if hw_name in fans_rpm:
                fans_rpm[hw_name]["control_percent"] = entry["value"]
        self.fans = list(fans_rpm.values())
        counts = {"cpu": 0, "motherboard": 0, "cooling": 0, "storage": 0}
        for entry in latest.values():
            hw_type = (entry["hardware_type"] or "").lower()
            if hw_type == "cpu":
                counts["cpu"] += 1
            elif hw_type in ("motherboard", "memorycontroller"):
                counts["motherboard"] += 1
            elif hw_type in ("cooling", "fan"):
                counts["cooling"] += 1
            elif hw_type in ("storage", "gpu"):
                counts["storage"] += 1
        self.counts = counts

    # ---------- 状态 ----------

    def _set_state(self, state: str, force: bool = False) -> None:
        # 有传感器数据 = available；Bridge 活但无数据 = partial；否则 unavailable
        if state != "unavailable" or force:
            pass
        if state == "available" and not self.latest:
            state = "partial"
        if state not in ("available", "partial", "unavailable"):
            state = "unavailable"
        if self._last_state is not None and state != self._last_state:
            if state == "unavailable" and self._last_state != "unavailable":
                logger.warning("高级硬件传感器 provider 不可用（基础系统监控继续）")
                self._record_event("hardware_sensor_provider_unavailable", "warning")
            elif state == "available" and self._last_state in ("partial", "unavailable"):
                logger.info("高级硬件传感器恢复（%d 个传感器）", len(self.latest))
                self._record_event("hardware_sensor_provider_recovered", "info")
        self._last_state = state
        self.state = state

    def _record_event(self, event_type: str, severity: str) -> None:
        if self.db is None:
            return
        try:
            self.db.record_event(event_type, severity, "system", {}, now=self.clock.now())
        except Exception as exc:
            logger.warning("写入传感器事件 %s 失败: %r", event_type, exc)

    # ---------- 读取（事件循环线程调用） ----------

    def snapshot(self) -> dict:
        """
        当前传感器快照（供 SystemCollector 注入与 /api/system/sensors）：
        - 过期的传感器值（> _SENSOR_STALE_SECONDS）视为不可用（-> None）；
        - cpu_temperature_c / cpu_package_power_w：CPU Package 温度/功耗
          （找不到时 None——绝不猜）；
        - state 随最新数据刷新。
        """
        now = self.clock.now()
        self._set_state(
            "available" if (self.last_bridge_update and now - self.last_bridge_update <= _SENSOR_STALE_SECONDS)
            else ("partial" if self._proc is not None else "unavailable")
        )
        if self.last_bridge_update is None or now - self.last_bridge_update > _SENSOR_STALE_SECONDS:
            return {
                "cpu_temperature_c": None,
                "cpu_package_power_w": None,
                "fans": [],
                "sensors": [],
                "counts": {"cpu": 0, "motherboard": 0, "cooling": 0, "storage": 0},
                "state": self.state,
            }
        cpu_temp = self._find_first("cpu", "temperature")
        cpu_power = self._find_first("cpu", "power")
        return {
            "cpu_temperature_c": cpu_temp,
            "cpu_package_power_w": cpu_power,
            "fans": list(self.fans),
            "sensors": [dict(v) for v in self.latest.values()],
            "counts": dict(self.counts),
            "state": self.state,
        }

    def _find_first(self, hardware_type: str, sensor_type: str) -> float | None:
        """按 (hardware_type, sensor_type) 取第一个新鲜值；缺失 None。"""
        for entry in self.latest.values():
            if entry["hardware_type"] == hardware_type and entry["sensor_type"] == sensor_type:
                return entry["value"]
        return None


class _suppress:
    """contextlib.suppress 的轻量等价（避免 import 开销在热路径）。"""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None
