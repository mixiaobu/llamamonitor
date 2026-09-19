r"""
update_service.py — LlamaMonitor 安全更新子系统（Phase 13）。

信任链（详见 docs/UPDATE_SECURITY.md）：
    LlamaMonitor.exe 内置 Ed25519 公钥（update_keys.py）
      -> 验证 release-manifest.json 原始 bytes（release-manifest.sig：key_id + Base64 签名）
        -> manifest 内含 installer/portable 的 filename + size + sha256
          -> 从**受信任 GitHub 仓库**（代码级常量 UPDATE_REPOSITORY，不来自 manifest/config）
             下载 Installer/ZIP，流式计算 SHA-256 + 累计 size，逐项比对后 os.replace 转正。

状态机（§25，单 asyncio 工作流，asyncio.Lock 串行，禁止并发 update）：
    IDLE -> CHECKING -> UPDATE_AVAILABLE / UP_TO_DATE
    UPDATE_AVAILABLE -> DOWNLOADING -> VERIFYING -> READY_TO_INSTALL
    READY_TO_INSTALL -> INSTALLING（启动 Installer -> 本应用优雅退出 -> 新版接管）
    任一阶段失败 -> ERROR

安全边界：
- 只有 loopback 能触发 check/download/install/cancel（server 层 _require_loopback）；
- 验签失败 / 字段非法 / hash 不符 / size 不符 -> 绝不进入 READY_TO_INSTALL，绝不执行 Installer；
- 下载只到 %LOCALAPPDATA%\LlamaMonitor\updates\{version}\*.part（永不写安装目录）；
- 大小上限 2 GiB + 磁盘空间检查（installer_size + 500 MB 余量），防磁盘耗尽；
- 私钥绝不进入运行程序（运行程序只内置公钥）；
- 网络错误（DNS/超时/SSL/rate limit）只影响 update 子系统，不影响监控主流程。

本模块不做：ALTER TABLE / 修改 schema（那是新版应用启动后的职责，§67）；
taskkill / os._exit（正常更新走 graceful shutdown，§59）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path

import httpx

from config import app_data_dir, is_frozen
from update_manifest import (
    ALGORITHM,
    compare_versions,
    is_safe_basename,
    parse_version,
    verify_manifest_signature,
    validate_manifest_fields,
)
from version import APP_NAME, __version__

logger = logging.getLogger("llamamonitor.update")

# ---------------------------------------------------------------------------
# 常量（代码级：受信任仓库 / 上限；manifest 只能提供 metadata，不能改变这些）
# ---------------------------------------------------------------------------

# 唯一受信任 GitHub 仓库（owner/name）。空 = Update service not configured（不访问网络）。
UPDATE_REPOSITORY = "mixiaobu/llamamonitor"

GITHUB_API_BASE = "https://api.github.com"
MANIFEST_ASSET = "release-manifest.json"
SIGNATURE_ASSET = "release-manifest.sig"
PENDING_MARKER_NAME = "pending_update.json"

MAX_UPDATE_SIZE = 2 * 1024 * 1024 * 1024          # 2 GiB（§33：Installer 不可能需要这么大）
DISK_SAFETY_MARGIN_BYTES = 500 * 1024 * 1024      # §34：额外 500 MB 余量
RELEASE_NOTES_MAX_BYTES = 100 * 1024              # §46：release body 上限
GITHUB_TIMEOUT_SECONDS = 10.0                     # §13
USER_AGENT = f"{APP_NAME}/{__version__}"
DOWNLOAD_CHUNK_SIZE = 1024 * 1024                 # §35：1 MB / chunk
STALE_PART_SECONDS = 24 * 3600                    # §84：.part 超过 24h 清理
KEEP_VERSION_DIRS = 2                             # §85：updates/ 版本目录保留最新 1~2 个

# 状态（§25）
IDLE = "IDLE"
CHECKING = "CHECKING"
UPDATE_AVAILABLE = "UPDATE_AVAILABLE"
UP_TO_DATE = "UP_TO_DATE"
DOWNLOADING = "DOWNLOADING"
VERIFYING = "VERIFYING"
READY_TO_INSTALL = "READY_TO_INSTALL"
INSTALLING = "INSTALLING"
ERROR = "ERROR"

BUSY_STATES = (CHECKING, DOWNLOADING, VERIFYING, INSTALLING)

# app_state 表（db.py schema v4）的 runtime metadata key（§70：不污染 metric counter 命名空间）
STATE_KEY_LAST_CHECK = "app.update.last_check"
STATE_KEY_ETAG = "app.update.github_etag"


class UpdateError(Exception):
    """update 子系统错误。code 用于 API 映射；transient=True 表示临时性（网络/限流）。"""

    def __init__(self, message: str, code: str = "ERROR", transient: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.transient = transient


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S")


def _is_rate_limited(resp: httpx.Response) -> bool:
    if resp.status_code in (403, 429):
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining is not None and remaining.strip() == "0":
            return True
    return resp.status_code == 429


def get_installation_mode() -> str:
    """
    installed  = Inno Setup 安装版（Phase 12 固定 AppId 的卸载注册表项存在）；
    portable   = 已打包（frozen）但无卸载注册表项（Portable ZIP）；
    development= 未打包（python desktop.py）。
    优先查注册表，不靠 EXE 路径猜（§51）。
    """
    if not is_frozen():
        return "development"
    if not _IS_WINDOWS:
        return "portable"
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{7E811DED-4947-495D-8F9C-1725CF459D43}}_is1",
            0,
            winreg.KEY_READ,
        )
        winreg.CloseKey(key)
        return "installed"
    except OSError:
        return "portable"


_IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------

class UpdateService:
    """
    安全更新状态机 + 下载/安装工作流（§25）。

    依赖注入（便于测试）：
    - db: Database | None（events / app_state runtime metadata / 健康状态；None = 无持久化）
    - get_config: () -> AppConfig | None（check 时读最新 updates 配置）
    - config_path: () -> Path | None（Pre-Update 备份 config.json 用）
    - updates_dir: Path | None（默认 %LOCALAPPDATA%\\LlamaMonitor\\updates）
    - client_factory: (timeout: float) -> httpx.AsyncClient（默认 httpx.AsyncClient；
      测试注入 MockTransport）
    - installation_mode: () -> str（默认 get_installation_mode）
    - request_exit: () -> None | bool（install 时触发应用优雅退出）
    - is_background: () -> bool（install 时决定 /APPUPDATE_BG）
    - repository / api_base：受信仓库 / API 基址（测试覆盖）
    - now: () -> float（时钟注入）
    """

    def __init__(
        self,
        db=None,
        get_config=None,
        *,
        config_path=None,
        updates_dir=None,
        backups_dir=None,
        client_factory=None,
        installation_mode=None,
        request_exit=None,
        is_background=None,
        repository: str | None = None,
        api_base: str = GITHUB_API_BASE,
        now=None,
    ) -> None:
        self._db = db
        self._get_config = get_config
        self._config_path = config_path
        self._updates_dir = Path(updates_dir) if updates_dir is not None else None
        self._backups_dir_path = Path(backups_dir) if backups_dir is not None else None
        self._client_factory = client_factory
        self._installation_mode = installation_mode or get_installation_mode
        self._request_exit = request_exit
        self._is_background = is_background or (lambda: False)
        self._repository = UPDATE_REPOSITORY if repository is None else repository
        self._api_base = api_base.rstrip("/")
        self._now = now or (lambda: time.time())

        self._lock = asyncio.Lock()
        self._cancel_event = asyncio.Event()
        self._state: str = IDLE
        self._error: str | None = None
        self._available: dict | None = None      # 已验证的 release 信息
        self._downloaded_bytes: int = 0
        self._total_bytes: int = 0
        self._verified_path: Path | None = None  # 已验证转正的 installer/zip
        self._last_check: float | None = None

    # ------------------------------------------------------------- 属性 / 状态

    @property
    def state(self) -> str:
        return self._state

    @property
    def verified_path(self) -> Path | None:
        return self._verified_path

    def _updates_root(self) -> Path:
        if self._updates_dir is not None:
            self._updates_dir.mkdir(parents=True, exist_ok=True)
            return self._updates_dir
        return (app_data_dir() / "updates")

    def _version_dir(self, version: str) -> Path:
        parse_version(version)  # 目录名必须是合法版本
        d = self._updates_root() / version
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _set_state(self, state: str, error: str | None = None) -> None:
        self._state = state
        self._error = error

    def _record_event(self, event_type: str, severity: str, details: dict) -> None:
        if self._db is None:
            return
        try:
            self._db.record_event(event_type, severity, "update", details, now=self._now())
        except Exception:
            logger.warning("写入 update 事件 %s 失败（不影响更新流程）", event_type, exc_info=True)

    def _read_state_key(self, key: str) -> str | None:
        if self._db is None:
            return None
        try:
            return self._db.get_app_state(key)
        except Exception:
            return None

    def _write_state_key(self, key: str, value: str) -> None:
        if self._db is None:
            return
        try:
            self._db.set_app_state(key, value)
        except Exception:
            logger.warning("写入 runtime metadata %s 失败（不影响更新流程）", key, exc_info=True)

    # ------------------------------------------------------------- 状态快照

    def status(self) -> dict:
        """GET /api/update/status 的响应体（§29 超集）。"""
        cfg = self._get_config() if self._get_config else None
        out: dict = {
            "state": self._state,
            "current_version": __version__,
            "installation_mode": self._installation_mode(),
            "available_version": self._available.get("version") if self._available else None,
            "downloaded_bytes": self._downloaded_bytes,
            "total_bytes": self._total_bytes,
            "progress_percent": (
                round(100.0 * self._downloaded_bytes / self._total_bytes, 1)
                if self._total_bytes else 0.0
            ),
            "last_check": _iso(self._last_check),
            "error": self._error,
            "ready_to_install": self._state == READY_TO_INSTALL,
        }
        if self._available:
            out["release"] = {
                "version": self._available["version"],
                "release_url": self._available.get("release_url"),
                "published_at": self._available.get("published_at"),
                "release_notes": self._available.get("release_notes"),
                "signing_key_id": self._available.get("signing_key_id"),
                "installer": self._available.get("installer"),
                "portable": self._available.get("portable"),
            }
        if cfg is not None and getattr(cfg, "updates", None) is not None:
            out["settings"] = {
                "check_enabled": cfg.updates.check_enabled,
                "check_interval_hours": cfg.updates.check_interval_hours,
                "auto_download": cfg.updates.auto_download,
            }
        return out

    # ------------------------------------------------------------- 检查

    def should_auto_check(self) -> bool:
        """§69：check_enabled 且距上次检查 >= interval（last check 来自 app_state）。"""
        cfg = self._get_config() if self._get_config else None
        if cfg is None or not cfg.updates.check_enabled:
            return False
        last = self._read_state_key(STATE_KEY_LAST_CHECK)
        if last is None:
            return True
        try:
            last_ts = float(last)
        except ValueError:
            return True
        return (self._now() - last_ts) >= cfg.updates.check_interval_hours * 3600.0

    async def check(self, manual: bool = False) -> dict:
        """POST /api/update/check：检查 GitHub Release（manual=用户主动点击）。"""
        async with self._lock:
            if self._state in BUSY_STATES:
                raise UpdateError("Update busy (a check/download/install is already running).",
                                  code="UPDATE_BUSY")
            prior_state = self._state
            self._set_state(CHECKING, None)
            try:
                await self._check_impl(manual=manual, prior_state=prior_state)
            except UpdateError as exc:
                # 临时性错误且此前已有已验证更新时，保留 UPDATE_AVAILABLE / READY_TO_INSTALL
                keep = (exc.transient and self._available is not None
                        and prior_state in (UPDATE_AVAILABLE, READY_TO_INSTALL))
                if keep:
                    self._set_state(prior_state, exc.message)
                    logger.warning("update check failed (transient, keeping last verified state): %s",
                                   exc.message)
                else:
                    self._set_state(ERROR, exc.message)
                self._record_event("update_check_failed", "warning",
                                   {"message": exc.message, "manual": manual})
                return self.status()
            return self.status()

    async def _check_impl(self, manual: bool, prior_state: str = IDLE) -> None:
        repo = self._repository.strip()
        if not repo or "/" not in repo:
            self._set_state(IDLE, "Update service not configured.")
            return

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        }
        etag = self._read_state_key(STATE_KEY_ETAG)
        if etag:
            headers["If-None-Match"] = etag
        url = f"{self._api_base}/repos/{repo}/releases/latest"

        async with self._client() as client:
            try:
                resp = await client.get(url, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)
            except httpx.HTTPError as exc:
                raise UpdateError(f"Unable to check for updates ({type(exc).__name__}).",
                                  code="NETWORK_ERROR", transient=True) from exc

            if resp.status_code == 304:
                # 无变化（§71 ETag）：Release 没变 -> 上次的验证结果依然有效，
                # 保留 UPDATE_AVAILABLE / READY_TO_INSTALL（已验证状态不因复查丢失）
                self._record_event("update_check", "info",
                                   {"manual": manual, "result": "not_modified"})
                if prior_state in (UPDATE_AVAILABLE, READY_TO_INSTALL) and self._available is not None:
                    self._set_state(prior_state, None)
                else:
                    self._set_state(UP_TO_DATE, None)
                return

            if _is_rate_limited(resp):
                raise UpdateError("GitHub API rate limit reached. Try again later.",
                                  code="RATE_LIMITED", transient=True)
            if resp.status_code == 404:
                raise UpdateError("No release found (repository or release does not exist).",
                                  code="NOT_FOUND")
            if resp.status_code != 200:
                raise UpdateError(f"Unable to check for updates (GitHub API HTTP {resp.status_code}).",
                                  code="NETWORK_ERROR", transient=True)

            try:
                release = resp.json()
            except ValueError as exc:
                raise UpdateError("Update invalid: release response is not JSON.",
                                  code="BAD_RELEASE") from exc

            if not isinstance(release, dict):
                raise UpdateError("Update invalid: release payload malformed.", code="BAD_RELEASE")
            # §20：draft / prerelease 都不用于 stable channel
            if release.get("draft") or release.get("prerelease"):
                raise UpdateError("Latest release is not a stable release (draft/prerelease).",
                                  code="UNSTABLE_RELEASE")

            assets = {
                a["name"]: a
                for a in release.get("assets", [])
                if isinstance(a, dict) and isinstance(a.get("name"), str)
            }
            manifest_asset = assets.get(MANIFEST_ASSET)
            sig_asset = assets.get(SIGNATURE_ASSET)
            if not manifest_asset or not sig_asset:
                raise UpdateError(
                    "Update invalid: release is missing release-manifest.json or release-manifest.sig.",
                    code="MISSING_ASSETS",
                )

            # §21：下载 manifest + 签名（小文件，直接 GET）
            manifest_bytes = await self._download_bytes(client, manifest_asset["browser_download_url"])
            sig_bytes = await self._download_bytes(client, sig_asset["browser_download_url"])

            # §22 步骤 3：先用内置公钥验签，**成功后才解析 JSON**
            ok, key_id, err = verify_manifest_signature(manifest_bytes, sig_bytes)
            if not ok:
                self._record_event("update_check_failed", "warning",
                                   {"stage": "signature", "key_id": key_id, "error": err})
                raise UpdateError(err or "Update signature verification failed.", code="BAD_SIGNATURE")
            try:
                manifest = json.loads(manifest_bytes.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise UpdateError("Update invalid: signed manifest is not valid JSON/UTF-8.",
                                  code="BAD_MANIFEST") from exc

            # §22 步骤 5-11：字段验证（product/schema/platform/arch/version/filename/entry）
            selected, errors = validate_manifest_fields(manifest)
            if errors:
                self._record_event("update_check_failed", "warning",
                                   {"stage": "manifest_fields", "errors": errors[:8]})
                raise UpdateError("Update invalid: " + "; ".join(errors[:4]), code="BAD_MANIFEST")

            version = selected["version"]
            # 交叉核对 GitHub asset 元数据（API 报告的 size 必须与 manifest 一致）
            for section in ("installer", "portable"):
                entry = selected.get(section)
                if not entry:
                    continue
                asset = assets.get(entry["filename"])
                if asset is None:
                    self._record_event("update_check_failed", "warning",
                                       {"stage": "asset_lookup", "filename": entry["filename"]})
                    raise UpdateError(
                        f"Update invalid: release does not contain {entry['filename']}.",
                        code="MISSING_ASSETS",
                    )
                if asset.get("size") is not None and int(asset["size"]) != entry["size"]:
                    raise UpdateError(
                        f"Update invalid: asset size mismatch for {entry['filename']}.",
                        code="BAD_MANIFEST",
                    )

            # 保存 runtime metadata（ETag / last check）
            new_etag = resp.headers.get("ETag")
            if new_etag:
                self._write_state_key(STATE_KEY_ETAG, new_etag)
            self._last_check = self._now()
            self._write_state_key(STATE_KEY_LAST_CHECK, str(int(self._last_check)))

            # §18/§19：版本比较；remote <= current 一律不更新（绝不自动降级）
            cmp = compare_versions(version, __version__)
            if cmp < 0:
                self._available = None
                self._verified_path = None
                self._set_state(UP_TO_DATE, None)
                self._record_event("update_check", "info",
                                   {"manual": manual, "result": "downgrade_ignored",
                                    "remote_version": version})
                return
            if cmp == 0:
                self._available = None
                self._verified_path = None
                self._set_state(UP_TO_DATE, None)
                self._record_event("update_check", "info",
                                   {"manual": manual, "result": "up_to_date"})
                return

            def _asset_entry(section: str, entry: dict) -> dict:
                asset = assets[entry["filename"]]
                out = dict(entry)
                out["url"] = asset["browser_download_url"]
                return out

            self._available = {
                "version": version,
                "release_url": release.get("html_url"),
                "published_at": release.get("published_at"),
                "release_notes": (release.get("body") or "")[:RELEASE_NOTES_MAX_BYTES],
                "signing_key_id": key_id,
                "installer": _asset_entry("installer", selected["installer"]) if selected.get("installer") else None,
                "portable": _asset_entry("portable", selected["portable"]) if selected.get("portable") else None,
            }
            self._set_state(UPDATE_AVAILABLE, None)
            self._record_event("update_check", "info",
                               {"manual": manual, "result": "available", "remote_version": version})
            self._record_event("update_available", "info",
                               {"manual": manual, "version": version, "key_id": key_id})

            # §41：auto_download 且**自动**检查（非 manual）-> 后台下载（仍不自动安装）
            cfg = self._get_config() if self._get_config else None
            if (
                not manual
                and cfg is not None
                and cfg.updates.check_enabled
                and cfg.updates.auto_download
            ):
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._auto_download_guarded())
                except RuntimeError:
                    pass

    async def _auto_download_guarded(self) -> None:
        try:
            await self.download()
        except UpdateError as exc:
            logger.warning("auto-download failed: %s", exc.message)
        except Exception:
            logger.exception("auto-download unexpected error")

    def _client(self):
        """返回 async context manager（httpx.AsyncClient 或测试注入的工厂产物）。"""
        if self._client_factory is not None:
            client = self._client_factory(GITHUB_TIMEOUT_SECONDS)
            if hasattr(client, "__aenter__"):
                return client
            return _BareAsyncContext(client)
        # trust_env=True：读取系统/环境变量代理（Windows 下读注册表 ProxyServer，
        # 国内网络常见需要代理访问 GitHub；无代理时行为不变）。
        return httpx.AsyncClient(follow_redirects=True, trust_env=True)

    async def _download_bytes(self, client, url: str) -> bytes:
        try:
            resp = await client.get(url, timeout=60.0, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise UpdateError(f"Unable to check for updates ({type(exc).__name__}).",
                              code="NETWORK_ERROR", transient=True) from exc
        if resp.status_code != 200:
            raise UpdateError(f"Unable to check for updates (HTTP {resp.status_code}).",
                              code="NETWORK_ERROR", transient=True)
        return resp.content

    # ------------------------------------------------------------- 下载

    async def download(self) -> dict:
        """POST /api/update/download：流式下载 + 边下载边算 SHA-256 + size 校验。"""
        async with self._lock:
            if self._state in BUSY_STATES:
                raise UpdateError("Update busy (a check/download/install is already running).",
                                  code="UPDATE_BUSY")
            if self._state != UPDATE_AVAILABLE or not self._available:
                raise UpdateError(
                    "Download requires a verified update (state=UPDATE_AVAILABLE); run Check first.",
                    code="NOT_READY",
                )
            mode = self._installation_mode()
            if mode == "development":
                raise UpdateError("Update download is unavailable in development mode.",
                                  code="DEVELOPMENT_MODE")

            section = "installer" if mode == "installed" else "portable"
            entry = self._available.get(section)
            if not entry:
                raise UpdateError(
                    f"Release does not provide a {section} artifact for this installation mode.",
                    code="MISSING_ASSETS",
                )
            filename = entry["filename"]
            expected_size = int(entry["size"])
            expected_sha = entry["sha256"].lower()
            url = entry["url"]

            vdir = self._version_dir(self._available["version"])
            self._set_state(DOWNLOADING, None)
            self._cancel_event.clear()
            self._downloaded_bytes = 0
            self._total_bytes = expected_size
            part = vdir / (filename + ".part")
            self._record_event("update_download_started", "info",
                               {"version": self._available["version"], "filename": filename,
                                "size": expected_size})
            try:
                # §33：大小上限（不信远程 Content-Length，只作预警；实际写入量同样受限）
                if expected_size > MAX_UPDATE_SIZE:
                    raise UpdateError(
                        f"Update too large: {expected_size} bytes exceeds the 2 GiB limit.",
                        code="TOO_LARGE",
                    )
                # §34：磁盘空间（下载前检查，不下载到一半才失败）
                try:
                    free = shutil.disk_usage(vdir).free
                except OSError as exc:
                    raise UpdateError(f"Unable to check disk space ({type(exc).__name__}).",
                                      code="DISK_SPACE") from exc
                if free < expected_size + DISK_SAFETY_MARGIN_BYTES:
                    raise UpdateError(
                        "Insufficient disk space for the update "
                        f"(need {expected_size + DISK_SAFETY_MARGIN_BYTES} bytes, free {free}).",
                        code="INSUFFICIENT_DISK_SPACE",
                    )
                async with self._client() as client:
                    async with client.stream("GET", url, timeout=60.0,
                                             follow_redirects=True) as resp:
                        if resp.status_code != 200:
                            raise UpdateError(f"Download failed (HTTP {resp.status_code}).",
                                              code="DOWNLOAD_ERROR", transient=True)
                        declared = resp.headers.get("Content-Length")
                        if declared is not None:
                            try:
                                if int(declared) > MAX_UPDATE_SIZE:
                                    raise UpdateError(
                                        "Update too large: server declares more than 2 GiB.",
                                        code="TOO_LARGE",
                                    )
                            except ValueError:
                                pass
                        hasher = hashlib.sha256()
                        written = 0
                        with open(part, "wb") as f:
                            async for chunk in resp.aiter_bytes(DOWNLOAD_CHUNK_SIZE):
                                if self._cancel_event.is_set():
                                    raise UpdateError("Download cancelled.", code="CANCELLED")
                                if not chunk:
                                    continue
                                f.write(chunk)
                                hasher.update(chunk)
                                written += len(chunk)
                                if written > MAX_UPDATE_SIZE:
                                    raise UpdateError(
                                        "Update too large: downloaded more than 2 GiB.",
                                        code="TOO_LARGE",
                                    )
                                self._downloaded_bytes = written

                # §36/§37/§38：VERIFYING —— size + SHA-256 双校验，失败立即删 .part
                self._set_state(VERIFYING, None)
                if written != expected_size:
                    raise UpdateError(
                        "Update verification failed: size mismatch "
                        f"(got {written}, expected {expected_size}).",
                        code="SIZE_MISMATCH",
                    )
                if hasher.hexdigest().lower() != expected_sha:
                    raise UpdateError(
                        "Update verification failed: SHA-256 mismatch "
                        f"(got {hasher.hexdigest()}, expected {expected_sha}).",
                        code="HASH_MISMATCH",
                    )
                final = vdir / filename
                os.replace(part, final)  # 原子转正
                self._verified_path = final
                self._set_state(READY_TO_INSTALL, None)
                self._record_event("update_download_complete", "info",
                                   {"version": self._available["version"],
                                    "filename": filename, "size": written})
            except UpdateError as exc:
                self._cleanup_part(part)
                if exc.code == "CANCELLED":
                    self._set_state(UPDATE_AVAILABLE, "Download cancelled.")
                    self._record_event("update_download_cancelled", "info",
                                       {"filename": filename})
                else:
                    self._set_state(ERROR, exc.message)
                    self._record_event("update_verification_failed", "warning",
                                       {"filename": filename, "code": exc.code,
                                        "message": exc.message})
                return self.status()
            except (httpx.HTTPError, OSError) as exc:
                self._cleanup_part(part)
                self._set_state(ERROR, f"Download failed ({type(exc).__name__}).")
                self._record_event("update_verification_failed", "warning",
                                   {"filename": filename, "code": "NETWORK",
                                    "message": str(exc)})
                return self.status()
            return self.status()

    @staticmethod
    def _cleanup_part(part: Path) -> None:
        try:
            if part.exists():
                part.unlink()
        except OSError:
            logger.warning("删除临时下载文件失败: %s", part, exc_info=True)

    # ------------------------------------------------------------- 取消

    async def cancel(self) -> dict:
        """POST /api/update/cancel：只允许 DOWNLOADING（§40：cancel event，不强杀线程）。"""
        if self._state != DOWNLOADING:
            raise UpdateError("No active download to cancel.", code="NOT_DOWNLOADING")
        self._cancel_event.set()
        return self.status()

    # ------------------------------------------------------------- 安装

    async def install(self) -> dict:
        """
        POST /api/update/install（§55-§61）：
        1) 检查 DB 健康（corrupt -> 拒绝）；
        2) Pre-Update Backup（DB 用 SQLite Backup API + quick_check；config 安全复制）；
        3) 写 pending_update.json；
        4) 启动**已验证的固定路径** Installer（/SILENT /NORESTART /APPUPDATE[_BG]，
           不接受外部传入路径、不用 shell=True）；
        5) 状态 INSTALLING + 触发应用 graceful shutdown（Installer 等 Mutex 释放后
           替换文件，完成后经 /APPUPDATE 自动启动新版）。
        """
        async with self._lock:
            if self._state in BUSY_STATES:
                raise UpdateError("Update busy (a check/download/install is already running).",
                                  code="UPDATE_BUSY")
            if self._state != READY_TO_INSTALL or self._verified_path is None:
                raise UpdateError(
                    "Install requires a verified download (state=READY_TO_INSTALL).",
                    code="NOT_READY",
                )
            mode = self._installation_mode()
            if mode == "development":
                raise UpdateError("Update installation is unavailable in development mode.",
                                  code="DEVELOPMENT_MODE")
            if mode == "portable":
                raise UpdateError(
                    "Portable builds are not self-overwritten. Use 'Open Download Folder' "
                    "and update manually.",
                    code="PORTABLE_MODE",
                )
            if self._request_exit is None:
                raise UpdateError(
                    "Update installation is unavailable (desktop integration missing).",
                    code="NOT_READY",
                )
            if not self._verified_path.is_file():
                raise UpdateError("Verified installer is missing from disk; download again.",
                                  code="NOT_READY")

            from_version = __version__
            to_version = self._available["version"] if self._available else ""
            if not to_version:
                raise UpdateError("Verified download has no version metadata.", code="NOT_READY")

            # §49：数据库本身 corrupt -> 不自动继续（提示用户先处理）
            if self._db is not None and getattr(self._db, "health", None) == "corrupt":
                raise UpdateError(
                    "Database health issue detected. Resolve or manually back up data "
                    "before updating.",
                    code="DB_UNHEALTHY",
                )

            # §47/§48：Pre-Update Backup（独立连接 / 安全复制；失败不启动 Installer）
            backups_dir = self._backups_dir()
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            ok, detail = await asyncio.to_thread(
                self._pre_update_backup, from_version, to_version, stamp, backups_dir
            )
            if not ok:
                self._record_event("update_backup_failed", "warning", {"detail": detail})
                raise UpdateError(f"Pre-update backup failed. ({detail})", code="BACKUP_FAILED")

            # §62：pending update marker（只用于恢复状态；不含命令/任意路径/URL）
            marker = {
                "from_version": from_version,
                "to_version": to_version,
                "installer_filename": self._verified_path.name,
                "verified_sha256": self._available["installer"]["sha256"] if self._available else None,
                "created_at": _iso(self._now()),
            }
            try:
                (self._updates_root() / PENDING_MARKER_NAME).write_text(
                    json.dumps(marker, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
                )
            except OSError as exc:
                raise UpdateError(f"Pre-update backup failed. (cannot write pending marker: {exc})",
                                  code="BACKUP_FAILED") from exc

            # §55/§56：启动已验证 Installer（列表参数、无 shell；/SILENT 有安装反馈、
            # 不用 /VERYSILENT；/NORESTART 不允许自动重启）
            args = [str(self._verified_path), "/SILENT", "/NORESTART"]
            args.append("/APPUPDATE_BG" if self._is_background() else "/APPUPDATE")
            try:
                subprocess.Popen(
                    args,
                    cwd=str(self._verified_path.parent),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if _IS_WINDOWS else 0,
                )
            except OSError as exc:
                raise UpdateError(f"Failed to launch installer: {exc}", code="INSTALL_LAUNCH") from exc

            self._set_state(INSTALLING, None)
            self._record_event("update_install_started", "info",
                               {"from_version": from_version, "to_version": to_version,
                                "installer": self._verified_path.name})
            logger.info("update install started: %s -> %s (installer launched, app shutting down)",
                        from_version, to_version)

            # §57：Installer 已启动 -> 本应用发起自身 graceful shutdown；
            # Installer 的 InitializeSetup 会 --shutdown-existing 等 Mutex 释放后替换文件，
            # 并经 /APPUPDATE 自动启动新版。
            self._request_exit()
            return self.status()

    def _backups_dir(self) -> Path:
        d = self._backups_dir_path if self._backups_dir_path is not None else (app_data_dir() / "backups")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _pre_update_backup(self, from_v: str, to_v: str, stamp: str, backups_dir: Path) -> tuple[bool, str]:
        """
        §47/§48：Pre-Update Backup（同步，经 asyncio.to_thread 调用）。
        - 数据库：SQLite Online Backup API（独立连接）-> PRAGMA quick_check 必须 ok；
        - config.json：安全文件复制（临时文件 + os.replace），不修改原文件。
        返回 (ok, detail)。
        """
        ts = int(self._now())
        try:
            # 1) 数据库备份
            if self._db is not None:
                db_dest = backups_dir / f"pre_update_v{from_v}_to_v{to_v}_{stamp}.db"
                src = sqlite3.connect(self._db.path, timeout=15.0)
                try:
                    dst = sqlite3.connect(str(db_dest))
                    try:
                        src.backup(dst)
                    finally:
                        dst.close()
                    # quick_check 必须 ok，否则删除新备份、视为失败（独立连接，用完即关）
                    qc = sqlite3.connect(str(db_dest))
                    try:
                        ok_row = qc.execute("PRAGMA quick_check").fetchone()
                    finally:
                        qc.close()
                finally:
                    src.close()
                if not ok_row or str(ok_row[0]).lower() != "ok":
                    try:
                        db_dest.unlink(missing_ok=True)
                    except OSError:
                        pass
                    return False, "pre-update database backup failed quick_check"
            # 2) config 备份（存在才备份）
            cfg_provider = self._config_path
            cfg_src = cfg_provider() if cfg_provider else None
            if cfg_src is not None:
                cfg_src = Path(cfg_src)
                if cfg_src.is_file():
                    cfg_dest = backups_dir / f"pre_update_v{from_v}_to_v{to_v}_config.json"
                    tmp = cfg_dest.with_suffix(".json.tmp")
                    shutil.copy2(cfg_src, tmp)
                    os.replace(tmp, cfg_dest)
            return True, "ok"
        except Exception as exc:
            logger.warning("pre-update backup failed: %r", exc)
            return False, f"{type(exc).__name__}: {exc}"

    # ------------------------------------------------------------- 清理

    def cleanup_stale(self) -> None:
        """§83/§84/§85：启动时清理 —— 删除 >24h 的 .part；版本目录只保留最新 2 个。"""
        root = self._updates_root()
        if not root.is_dir():
            return
        now = self._now()
        try:
            for part in root.glob("*/*.part"):
                try:
                    if now - part.stat().st_mtime > STALE_PART_SECONDS:
                        part.unlink()
                        logger.info("cleaned stale update part: %s", part.name)
                except OSError:
                    pass
            dirs = [d for d in root.iterdir() if d.is_dir()]
            if len(dirs) > KEEP_VERSION_DIRS:
                dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
                for d in dirs[KEEP_VERSION_DIRS:]:
                    shutil.rmtree(d, ignore_errors=True)
                    logger.info("cleaned old update version dir: %s", d.name)
        except OSError:
            logger.warning("update dir cleanup failed", exc_info=True)


class _BareAsyncContext:
    """测试工厂返回裸 AsyncClient 时的适配（无上下文管理器包装）。"""

    def __init__(self, client) -> None:
        self._client = client

    async def __aenter__(self):
        return self._client

    async def __aexit__(self, *exc):
        await self._client.aclose()
        return False


# ---------------------------------------------------------------------------
# 新版启动时的 update 成功检测（desktop.py 调用；§63）
# ---------------------------------------------------------------------------

def _record_update_event(db, event_type: str, severity: str, details: dict, ts: int) -> bool:
    """
    用**短命新连接**写一条 monitor_events（WAL 模式支持并发连接）。

    为什么不用 db.record_event：本函数在 desktop 主线程执行，而 Database 的
    长连接由 uvicorn 线程的 lifespan 首次 _connect() 创建（Python sqlite3 默认
    check_same_thread=True）——跨线程使用会抛 ProgrammingError，事件丢失
    （2026-09-19 实测：update_success 事件因此未落库）。新连接 + busy_timeout
    在 WAL 下与主连接并发安全；事件保留清理由后续常规 record_event 顺带执行。
    """
    try:
        # Database 惰性建表：先触发 schema 初始化（已初始化时为廉价 no-op；
        # 生产环境中此时 uvicorn 线程已连接，这里直接返回既有连接，不改变
        # 线程归属）。短命连接只负责 INSERT。
        db._connect()
        conn = sqlite3.connect(db.path, timeout=5.0)
    except (sqlite3.Error, AttributeError, TypeError):
        return False
    try:
        conn.execute("PRAGMA busy_timeout = 5000;")
        try:
            details_json = json.dumps(details, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            details_json = "{}"
        with conn:
            conn.execute(
                "INSERT INTO monitor_events(timestamp, event_type, severity, source, details_json) "
                "VALUES(?, ?, ?, ?, ?)",
                (ts, event_type, severity, "update", details_json),
            )
        return True
    except sqlite3.Error:
        logger.warning("记录 %s 事件失败", event_type, exc_info=True)
        return False
    finally:
        conn.close()


def check_pending_update(db, current_version: str, data_dir: str | Path, now: float | None = None) -> str | None:
    """
    新版启动：读 updates/pending_update.json。
    - to_version == 当前版本 -> 记 update_success 事件，删 marker，返回 "success"；
    - to_version != 当前版本 -> 记 update_failed_mismatch 警告，删 marker，返回 "mismatch"；
    - 无 marker / 损坏 -> None。
    绝不抛异常（更新状态检测失败不影响启动）。
    """
    try:
        marker_path = Path(data_dir) / "updates" / PENDING_MARKER_NAME
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(marker, dict):
            return None
        ts = int(now if now is not None else time.time())
        if str(marker.get("to_version")) == current_version:
            _record_update_event(db, "update_success", "info", marker, ts)
            result = "success"
        else:
            _record_update_event(
                db, "update_failed_mismatch", "warning",
                {**marker, "current_version": current_version}, ts,
            )
            result = "mismatch"
        try:
            marker_path.unlink(missing_ok=True)
        except OSError:
            pass
        return result
    except Exception:
        logger.warning("pending update 检测失败（不影响启动）", exc_info=True)
        return None
