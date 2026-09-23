"""
config.py — LlamaMonitor 统一配置系统（Phase 7）

项目中唯一负责配置的模块：定位配置文件、创建默认配置、读取 JSON、
校验取值、递归合并缺失默认值、提供最终配置对象，以及
配置文件 / 数据库 / 日志目录 的实际路径。
其他模块禁止自己读取 config.json —— 一律通过 load_config() / AppConfig 获取。

配置文件位置（优先级从高到低）：
1. 环境变量 LLAMAMONITOR_CONFIG 指定的路径；
2. 开发模式（非 PyInstaller）：当前工作目录（项目根目录）存在 config.json 时使用它；
3. 默认：%LOCALAPPDATA%\\LlamaMonitor\\config.json（首次启动自动创建目录和默认文件）。

加载语义：
- 文件不存在：自动创建默认 config.json，全默认值启动（using_defaults=True）；
- JSON 损坏 / 根节点不是对象：不覆盖原文件，记录错误，全默认值启动
  （loaded=False, using_defaults=True, has_errors=True）；
- 个别字段非法：warning + 该字段回退默认值，其余合法字段正常生效（has_errors=True）；
- 未知字段：不删除、不导致启动失败，只 warning（Unknown config key: xxx）；
- 缺失字段（含新版本新增字段）：递归用默认值补齐 —— 旧配置升级不会失败。

无热重载：修改 config.json 后需要重启 LlamaMonitor 才生效。
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# HTTP 代理策略（RC-004）
# ---------------------------------------------------------------------------

def trust_env_for(url: str) -> bool:
    """
    返回该 URL 的 httpx 客户端是否应该 trust_env（读取环境变量 / Windows
    注册表代理）。

    背景（RC-004，burn-in 0.16.2 实测）：Windows 系统代理指向一个**未运行**的
    本地代理客户端（如 VPN 工具崩溃退出后注册表残留 ProxyEnable=1）时，httpx
    默认 trust_env=True 会把**指向环回地址**的请求也发给死代理（httpx 不像
    WinINET 那样自动应用 ProxyOverride 的 <local> 绕过），导致：
    - collector 抓 127.0.0.1:9091/metrics 每轮超时 → 数据流中断 + 误报离线；
    - 桌面端 wait_for_ready 轮询本机 8765 120s 拿不到 200 → 自启动实例
      误判 "API 未就绪" 退出（用户开机后看不到应用）。

    策略：http(s) 指向**本地/内网地址**（127.*/localhost/10.*/172.16-31.*/
    192.168.* 及回环 IPv6）时不走代理——这些端点（llama-server 默认绑定、
    本应用自身 API）在环回/局域网内，代理对它们只可能有害；其余地址保留
    trust_env，代理仍然有效（如 llama-server 部署在远端且需要代理出网）。
    """
    try:
        parsed = urllib.parse.urlparse(url)
        # 非 http(s) 地址（或无 scheme 的畸形串）：保守起见仍信任环境，
        # 由 httpx 按自身规则处理（本判定只用于"本地/内网直连"优化）
        if parsed.scheme not in ("http", "https"):
            return True
        host = parsed.hostname or ""
    except ValueError:
        return True
    h = host.lower().rstrip(".")
    if h in ("localhost", ""):
        return False
    if h.startswith("127."):
        return False
    if h == "::1":
        return False
    # 私有网段（RFC1918）：10.0.0.0/8、172.16.0.0/12、192.168.0.0/16
    parts = h.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        if parts[0] == "10":
            return False
        if parts[0] == "172" and 16 <= int(parts[1]) <= 31:
            return False
        if parts[0] == "192" and parts[1] == "168":
            return False
    return True


# ---------------------------------------------------------------------------
# 配置结构（dataclass）
# ---------------------------------------------------------------------------


@dataclass
class LlamaServerConfig:
    url: str = "http://127.0.0.1:9091"        # 基础地址（不含 metrics path）
    metrics_path: str = "/metrics"
    timeout_seconds: float = 3.0              # 0.5 ~ 60


@dataclass
class CollectorConfig:
    poll_interval_seconds: float = 5.0        # 1 ~ 3600
    live_retention_hours: float = 48.0        # 1 ~ 8760


@dataclass
class GpuConfig:
    enabled: bool = True
    poll_interval_seconds: float = 5.0        # 1 ~ 3600
    history_retention_hours: float = 48.0     # 1 ~ 8760（gpu_samples 保留时长）
    device_uuids: list = field(default_factory=list)  # 空 = 监控所有检测到的 NVIDIA GPU


@dataclass
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8765                          # 1 ~ 65535


@dataclass
class DatabaseConfig:
    path: str = ""                            # 空字符串 -> %LOCALAPPDATA%\LlamaMonitor\monitor.db
    wal: bool = True


@dataclass
class BackupConfig:
    """自动备份（Phase 11）：距离上次成功自动备份 >= interval_hours 时触发。"""

    automatic: bool = True                    # 是否启用自动备份
    interval_hours: float = 24.0              # 1 ~ 24*365
    keep_count: int = 14                      # 1 ~ 365，自动备份保留份数（手动备份永不轮转）


@dataclass
class UpdatesConfig:
    """更新检查（Phase 13）：默认**关闭** —— 本地监控工具不应默认联网。"""

    check_enabled: bool = False               # 是否启用定期更新检查
    check_interval_hours: float = 24.0        # 1 ~ 24*365
    auto_download: bool = False               # 自动检查发现新版本时自动下载（仍不自动安装）


@dataclass
class UIConfig:
    refresh_interval_seconds: float = 5.0     # 1 ~ 3600
    daily_default_days: int = 7               # 1 ~ 3650（16D：默认 7 天）
    theme: str = "system"                     # dark / light / system（1.0 起默认跟随系统）


@dataclass
class LoggingConfig:
    level: str = "INFO"                       # DEBUG/INFO/WARNING/ERROR/CRITICAL
    max_size_mb: float = 10.0                 # 1 ~ 1024
    backup_count: int = 5                     # 1 ~ 100


@dataclass
class AppConfig:
    """最终配置对象（全部字段已校验，非法值已回退默认）。"""

    llama_server: LlamaServerConfig = field(default_factory=LlamaServerConfig)
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    gpu: GpuConfig = field(default_factory=GpuConfig)
    web: WebConfig = field(default_factory=WebConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)  # Phase 11（放末尾：位置构造兼容）
    updates: UpdatesConfig = field(default_factory=UpdatesConfig)  # Phase 13（追加在 backup 之后）

    @classmethod
    def default(cls) -> "AppConfig":
        """全默认配置（无文件 I/O、无副作用）。"""
        config, _ = _build_app_config(_deep_merge(DEFAULT_CONFIG, {}))
        return config

    @property
    def metrics_url(self) -> str:
        """最终 metrics 地址（url + metrics_path 拼接，见 build_metrics_url）。"""
        return build_metrics_url(self.llama_server.url, self.llama_server.metrics_path)

    @property
    def database_path(self) -> Path:
        """数据库实际路径：database.path 非空用用户值，否则 %LOCALAPPDATA%\\LlamaMonitor\\monitor.db。"""
        path = (self.database.path or "").strip()
        if path:
            return Path(path).expanduser()
        return app_data_dir() / "monitor.db"

    @property
    def log_directory(self) -> Path:
        """日志目录实际路径：%LOCALAPPDATA%\\LlamaMonitor\\logs"""
        return app_data_dir() / "logs"


# ---------------------------------------------------------------------------
# 默认值（首次启动写入 config.json 的内容；也是递归合并的基底）
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict = {
    "llama_server": {
        "url": "http://127.0.0.1:9091",
        "metrics_path": "/metrics",
        "timeout_seconds": 3,
    },
    "collector": {
        "poll_interval_seconds": 5,
        "live_retention_hours": 48,
    },
    "gpu": {
        "enabled": True,
        "poll_interval_seconds": 5,
        "history_retention_hours": 48,
        "device_uuids": [],
    },
    "web": {
        "host": "127.0.0.1",
        "port": 8765,
    },
    "database": {
        "path": "",
        "wal": True,
    },
    "ui": {
        "refresh_interval_seconds": 5,
        "daily_default_days": 7,
        "theme": "system",
    },
    "logging": {
        "level": "INFO",
        "max_size_mb": 10,
        "backup_count": 5,
    },
    "backup": {
        "automatic": True,
        "interval_hours": 24,
        "keep_count": 14,
    },
    "updates": {
        "check_enabled": False,
        "check_interval_hours": 24,
        "auto_download": False,
    },
}

_KNOWN_KEYS: dict[str, list[str]] = {
    section: list(keys) for section, keys in DEFAULT_CONFIG.items()
}


def default_config_text() -> str:
    """写入 config.json 的默认文件内容。"""
    return json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包内。"""
    return bool(getattr(sys, "frozen", False))


def app_data_dir(base_dir: str | Path | None = None) -> Path:
    """
    用户数据目录（自动创建）。

    传入 base_dir 时直接使用它（测试用）；
    默认 %LOCALAPPDATA%\\LlamaMonitor（非 Windows 回退 ~/LlamaMonitor）。
    """
    if base_dir is not None:
        p = Path(base_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p
    localappdata = os.environ.get("LOCALAPPDATA")
    base = Path(localappdata) / "LlamaMonitor" if localappdata else Path.home() / "LlamaMonitor"
    base.mkdir(parents=True, exist_ok=True)
    return base


def resolve_config_path() -> Path:
    """
    配置文件路径，优先级：
    1. 环境变量 LLAMAMONITOR_CONFIG
    2. 开发模式：当前工作目录（项目根目录）的 config.json（存在时）
    3. %LOCALAPPDATA%\\LlamaMonitor\\config.json
    """
    env = os.environ.get("LLAMAMONITOR_CONFIG")
    if env:
        return Path(env).expanduser()
    if not is_frozen():
        local = Path.cwd() / "config.json"
        if local.is_file():
            return local
    return app_data_dir() / "config.json"


# ---------------------------------------------------------------------------
# URL 拼接
# ---------------------------------------------------------------------------


def build_metrics_url(base_url: str, metrics_path: str) -> str:
    """
    拼接基础地址与 metrics path，保证不出现双斜杠：

    http://127.0.0.1:9091        + /metrics -> http://127.0.0.1:9091/metrics
    http://127.0.0.1:9091/       + /metrics -> http://127.0.0.1:9091/metrics
    http://127.0.0.1:9091/base   + /metrics -> http://127.0.0.1:9091/base/metrics
    http://127.0.0.1:9091/metrics + /metrics -> http://127.0.0.1:9091/metrics
    （最后一条：容忍 base 里已经带过 metrics path 的旧式写法）
    """
    base = (base_url or "").rstrip("/")
    path = str(metrics_path)
    if not path.startswith("/"):
        path = "/" + path
    if base.endswith(path):
        return base
    return base + path


# ---------------------------------------------------------------------------
# 合并 / 未知字段 / 校验
# ---------------------------------------------------------------------------


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并：override 的值优先；嵌套 dict 逐层合并；未知键保留。"""
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _unknown_keys(merged: dict) -> list[str]:
    unknown = [key for key in merged if key not in DEFAULT_CONFIG]
    for section, keys in _KNOWN_KEYS.items():
        sub = merged.get(section)
        if isinstance(sub, dict):
            unknown.extend(f"{section}.{key}" for key in sub if key not in keys)
    return unknown


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value  # 排除 NaN


def _in_range(value, low: float, high: float) -> bool:
    return _is_number(value) and low <= value <= high


def _is_int_in_range(value, low: int, high: int) -> bool:
    return _is_number(value) and low <= value <= high and int(value) == value


def _build_app_config(merged: dict) -> tuple[AppConfig, list[str]]:
    """
    从合并后的原始 dict 构建 AppConfig。

    每个字段独立校验：缺失 -> 默认值（不告警）；非法 -> 默认值 + 错误记录。
    返回 (config, 错误信息列表)。
    """
    errors: list[str] = []

    def _section(name: str) -> dict:
        value = merged.get(name)
        return value if isinstance(value, dict) else {}

    def _check(section: str, key: str, value, ok, cast, default):
        if value is None:
            return default
        try:
            good = ok(value)
        except Exception:
            good = False
        if good:
            return cast(value)
        errors.append(f"Invalid config value {section}.{key}={value!r}, fallback to {default!r}")
        return default

    ls = _section("llama_server")
    col = _section("collector")
    gpu_sec = _section("gpu")
    web = _section("web")
    dat = _section("database")
    ui = _section("ui")
    lg = _section("logging")
    bk = _section("backup")
    up = _section("updates")

    llama_server = LlamaServerConfig(
        url=_check(
            "llama_server", "url", ls.get("url"),
            lambda v: isinstance(v, str) and (v.startswith("http://") or v.startswith("https://")),
            str, "http://127.0.0.1:9091",
        ),
        metrics_path=_check(
            "llama_server", "metrics_path", ls.get("metrics_path"),
            lambda v: isinstance(v, str) and v.startswith("/"),
            str, "/metrics",
        ),
        timeout_seconds=_check(
            "llama_server", "timeout_seconds", ls.get("timeout_seconds"),
            lambda v: _in_range(v, 0.5, 60), float, 3.0,
        ),
    )
    collector = CollectorConfig(
        poll_interval_seconds=_check(
            "collector", "poll_interval_seconds", col.get("poll_interval_seconds"),
            lambda v: _in_range(v, 1, 3600), float, 5.0,
        ),
        live_retention_hours=_check(
            "collector", "live_retention_hours", col.get("live_retention_hours"),
            lambda v: _in_range(v, 1, 8760), float, 48.0,
        ),
    )
    gpu = GpuConfig(
        enabled=_check("gpu", "enabled", gpu_sec.get("enabled"), lambda v: isinstance(v, bool), bool, True),
        poll_interval_seconds=_check(
            "gpu", "poll_interval_seconds", gpu_sec.get("poll_interval_seconds"),
            lambda v: _in_range(v, 1, 3600), float, 5.0,
        ),
        history_retention_hours=_check(
            "gpu", "history_retention_hours", gpu_sec.get("history_retention_hours"),
            lambda v: _in_range(v, 1, 8760), float, 48.0,
        ),
        device_uuids=_check(
            "gpu", "device_uuids", gpu_sec.get("device_uuids"),
            lambda v: isinstance(v, list) and all(isinstance(u, str) and u.strip() != "" for u in v),
            lambda v: [str(u) for u in v], [],
        ),
    )
    web_config = WebConfig(
        host=_check(
            "web", "host", web.get("host"),
            lambda v: isinstance(v, str) and v.strip() != "",
            str, "127.0.0.1",
        ),
        port=_check(
            "web", "port", web.get("port"),
            lambda v: _is_int_in_range(v, 1, 65535), int, 8765,
        ),
    )
    database = DatabaseConfig(
        path=_check("database", "path", dat.get("path"), lambda v: isinstance(v, str), str, ""),
        wal=_check("database", "wal", dat.get("wal"), lambda v: isinstance(v, bool), bool, True),
    )
    ui_config = UIConfig(
        refresh_interval_seconds=_check(
            "ui", "refresh_interval_seconds", ui.get("refresh_interval_seconds"),
            lambda v: _in_range(v, 1, 3600), float, 5.0,
        ),
        daily_default_days=_check(
            "ui", "daily_default_days", ui.get("daily_default_days"),
            lambda v: _is_int_in_range(v, 1, 3650), int, 7,
        ),
        theme=_check(
            "ui", "theme", ui.get("theme"),
            lambda v: isinstance(v, str) and v in ("dark", "light", "system"),
            str, "system",
        ),
    )
    logging_config = LoggingConfig(
        level=_check(
            "logging", "level", lg.get("level"),
            lambda v: isinstance(v, str) and v.upper() in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
            lambda v: v.upper(), "INFO",
        ),
        max_size_mb=_check(
            "logging", "max_size_mb", lg.get("max_size_mb"),
            lambda v: _in_range(v, 1, 1024), float, 10.0,
        ),
        backup_count=_check(
            "logging", "backup_count", lg.get("backup_count"),
            lambda v: _is_int_in_range(v, 1, 100), int, 5,
        ),
    )
    backup_config = BackupConfig(
        automatic=_check("backup", "automatic", bk.get("automatic"), lambda v: isinstance(v, bool), bool, True),
        interval_hours=_check(
            "backup", "interval_hours", bk.get("interval_hours"),
            lambda v: _in_range(v, 1, 24 * 365), float, 24.0,
        ),
        keep_count=_check(
            "backup", "keep_count", bk.get("keep_count"),
            lambda v: _is_int_in_range(v, 1, 365), int, 14,
        ),
    )
    updates_config = UpdatesConfig(
        check_enabled=_check(
            "updates", "check_enabled", up.get("check_enabled"),
            lambda v: isinstance(v, bool), bool, False,
        ),
        check_interval_hours=_check(
            "updates", "check_interval_hours", up.get("check_interval_hours"),
            lambda v: _in_range(v, 1, 24 * 365), float, 24.0,
        ),
        auto_download=_check(
            "updates", "auto_download", up.get("auto_download"),
            lambda v: isinstance(v, bool), bool, False,
        ),
    )
    return (
        AppConfig(
            llama_server, collector, gpu, web_config, database, ui_config,
            logging_config, backup_config, updates_config,
        ),
        errors,
    )


def get_default_config() -> dict:
    """完整默认配置 dict（深拷贝，供 GET /api/config/defaults 与 Reset to Defaults）。"""
    return json.loads(json.dumps(DEFAULT_CONFIG))


def _flatten_known() -> list[str]:
    """DEFAULT_CONFIG 的所有已知字段点路径，如 ['llama_server.url', 'web.port', ...]。"""
    paths: list[str] = []
    for section, keys in DEFAULT_CONFIG.items():
        for key in keys:
            paths.append(f"{section}.{key}")
    return paths


_KNOWN_PATHS: list[str] = _flatten_known()


def _get_path(d: dict, dotted: str):
    section, key = dotted.split(".", 1)
    sub = d.get(section)
    if not isinstance(sub, dict):
        return None
    return sub.get(key)


def filter_known_updates(updates: dict) -> dict:
    """
    只保留请求中的已知字段（未知字段被忽略，不写入文件）。
    输入 dict 不被修改；无已知字段时返回空 dict。
    """
    if not isinstance(updates, dict):
        return {}
    out: dict = {}
    for section, keys in _KNOWN_KEYS.items():
        sub = updates.get(section)
        if not isinstance(sub, dict):
            continue
        known = {k: v for k, v in sub.items() if k in keys}
        if known:
            out[section] = known
    return out


def read_raw_config(path: Path) -> dict:
    """
    读取配置文件原始 dict（含未知字段，用于保存时保留）。

    文件不存在 / 损坏 / 根节点非对象时返回 {}（保存时以默认值补齐重建）。
    读取用 utf-8-sig 兼容 BOM。
    """
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
        data = json.loads(text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def validate_updates(existing: dict, updates: dict) -> tuple[dict, list[str], list[str]]:
    """
    校验配置更新，返回 (candidate, changed, errors)：

    - candidate：existing 合并已知更新字段后的完整 dict（未知字段保留）；
    - changed：已知字段中值实际发生变化的点路径列表（如 'llama_server.url'）；
    - errors：逐字段校验错误信息（与 load_config 同一套校验器；空列表 = 全部合法）。
    """
    candidate = _deep_merge(existing, filter_known_updates(updates))
    _, errors = _build_app_config(candidate)
    changed = [p for p in _KNOWN_PATHS if _get_path(candidate, p) != _get_path(existing, p)]
    return candidate, changed, errors


def atomic_write_json(path: Path, data: dict) -> None:
    """
    原子写入 JSON 配置：临时文件 + fsync + os.replace。

    写入过程中崩溃只会留下一个 .tmp 文件，config.json 本身要么完整旧内容、
    要么完整新内容，绝不会是半个文件。
    """
    path = Path(path)
    text = json.dumps(data, indent=2, ensure_ascii=False)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------


@dataclass
class LoadedConfig:
    """load_config() 的结果：最终配置 + 加载状态（供 /api/status、/api/config 使用）。"""

    config: AppConfig
    path: Path
    loaded: bool          # 是否成功读到了有效配置文件
    using_defaults: bool  # 是否以全默认值启动（首次生成 / 文件损坏）
    has_errors: bool      # 是否存在字段级回退或读取/解析错误
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def load_config(path: str | Path | None = None) -> LoadedConfig:
    """
    定位并加载配置文件，返回 LoadedConfig。

    - path=None：按优先级解析（环境变量 > 开发模式 ./config.json > %LOCALAPPDATA%）；
    - 文件不存在：自动创建默认 config.json，全默认值启动；
    - JSON 损坏 / 根节点非对象：不覆盖原文件，记录错误，全默认值启动。
    """
    log = logging.getLogger("llamamonitor.config")
    target = Path(path).expanduser() if path is not None else resolve_config_path()

    try:
        # utf-8-sig：兼容带 BOM 的文件（Windows 记事本/脚本工具可能写出 BOM），
        # 无 BOM 文件读取结果完全相同
        text = target.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(default_config_text(), encoding="utf-8")
            log.info("首次启动：已生成默认配置文件 %s", target)
        except OSError as exc:
            log.warning("无法写入默认配置文件 %s: %r", target, exc)
        return LoadedConfig(AppConfig.default(), target, loaded=True, using_defaults=True, has_errors=False)
    except (OSError, UnicodeDecodeError) as exc:
        log.error("读取配置文件失败 %s: %r（以全部默认配置启动，原文件保留）", target, exc)
        return LoadedConfig(
            AppConfig.default(), target, loaded=False, using_defaults=True,
            has_errors=True, errors=[f"read config failed: {exc!r}"],
        )

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        log.error(
            "配置文件 %s 不是合法 JSON（第 %s 行第 %s 列）：以全部默认配置启动，原文件保留",
            target, exc.lineno, exc.colno,
        )
        return LoadedConfig(
            AppConfig.default(), target, loaded=False, using_defaults=True,
            has_errors=True, errors=[f"invalid JSON: {exc}"],
        )

    if not isinstance(data, dict):
        log.error("配置文件 %s 根节点不是 JSON 对象：以全部默认配置启动，原文件保留", target)
        return LoadedConfig(
            AppConfig.default(), target, loaded=False, using_defaults=True,
            has_errors=True, errors=["config root is not a JSON object"],
        )

    merged = _deep_merge(DEFAULT_CONFIG, data)
    warnings = [f"Unknown config key: {key}" for key in _unknown_keys(merged)]
    for warning in warnings:
        log.warning(warning)
    config, errors = _build_app_config(merged)
    for error in errors:
        log.warning(error)
    return LoadedConfig(
        config, target,
        loaded=True, using_defaults=False,
        has_errors=bool(errors), errors=errors, warnings=warnings,
    )


# ---------------------------------------------------------------------------
# 命令行覆盖
# ---------------------------------------------------------------------------


def apply_overrides(
    config: AppConfig,
    url: str | None = None,
    db_path: str | None = None,
    interval: float | None = None,
) -> None:
    """
    命令行覆盖已加载配置（优先级：CLI > config.json > 默认值）。

    非法值 warning 并保持原值，不让启动失败。
    """
    log = logging.getLogger("llamamonitor.config")
    if url is not None:
        if isinstance(url, str) and (url.startswith("http://") or url.startswith("https://")):
            config.llama_server.url = url
        else:
            log.warning("Invalid --url %r, keeping %s", url, config.llama_server.url)
    if db_path is not None:
        config.database.path = str(db_path)
    if interval is not None:
        try:
            value = float(interval)
        except (TypeError, ValueError):
            log.warning("Invalid --interval %r, keeping %s", interval, config.collector.poll_interval_seconds)
            return
        if 1 <= value <= 3600:
            config.collector.poll_interval_seconds = value
        else:
            log.warning("Invalid --interval %r (range 1~3600), keeping %s", interval, config.collector.poll_interval_seconds)


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------


def _level(name) -> int:
    value = getattr(logging, str(name).upper(), None)
    return value if isinstance(value, int) else logging.INFO


def setup_logging(logging_config: LoggingConfig | None = None) -> logging.Logger:
    """
    配置 root 日志：控制台 + 滚动文件（<用户数据目录>\\logs\\monitor.log）。幂等。

    PyInstaller --windowed 没有控制台时文件日志依然存在。
    降噪：每次抓取 / 每次 HTTP 请求不写日志（httpx、uvicorn.access 降到 WARNING）；
    只有启动、关闭、在线/离线变化、Counter reset、配置/数据库错误、异常才写日志。
    """
    if logging_config is None:
        logging_config = AppConfig.default().logging
    log_file = app_data_dir() / "logs" / "monitor.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    level = _level(logging_config.level)
    if any(getattr(h, "_llamamonitor", False) for h in root.handlers):
        root.setLevel(level)
        return logging.getLogger("llamamonitor")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=int(logging_config.max_size_mb * 1024 * 1024),
        backupCount=int(logging_config.backup_count),
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler._llamamonitor = True  # 幂等标记
    root.addHandler(file_handler)
    if sys.stderr is not None:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(fmt)
        console_handler._llamamonitor = True
        root.addHandler(console_handler)
    root.setLevel(level)
    for name in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return logging.getLogger("llamamonitor")


def reset_logging() -> None:
    """移除本模块挂到 root 的 handler（测试用）。"""
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not getattr(h, "_llamamonitor", False)]
