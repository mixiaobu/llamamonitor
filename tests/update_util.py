"""
Phase 13 测试公共工具：临时 Ed25519 密钥对 + 模拟 GitHub Release 环境（MockTransport）。

规则：
- 测试只用**临时密钥对**（generate_keypair），绝不使用生产私钥；
- FakeGithub 生成一个完整的"受信 GitHub Release"：release JSON + manifest 原始
  bytes + .sig sidecar + installer/portable 产物 bytes，全部通过一个 httpx handler
  按 URL path 分发（httpx.MockTransport 驱动）；
- 版本动态生成（当前版本 patch+1），保证 remote > current 与 version.py 实际值无关；
- 篡改场景通过参数注入（missing sig/manifest、filename 不匹配、draft 等）。
"""

from __future__ import annotations

import hashlib
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from generate_update_key import generate_keypair  # noqa: E402
from update_manifest import (  # noqa: E402
    build_manifest,
    canonical_manifest_bytes,
    expected_installer_filename,
    expected_portable_filename,
    parse_version,
    sign_manifest,
)
from update_service import UpdateService  # noqa: E402


def next_patch_version(current: str) -> str:
    """当前版本 patch+1（保证 > current）。"""
    major, minor, patch = parse_version(current)
    return f"{major}.{minor}.{patch + 1}"


class _UpdatesCfg:
    def __init__(self, enabled: bool = False, interval_hours: float = 24.0,
                 auto_download: bool = False) -> None:
        self.check_enabled = enabled
        self.check_interval_hours = interval_hours
        self.auto_download = auto_download


class _Cfg:
    def __init__(self, updates: _UpdatesCfg) -> None:
        self.updates = updates


class FakeGithub:
    """
    模拟受信 GitHub Release（owner/name、latest stable release）。

    参数：
    - version: remote 版本（默认 = 当前版本 patch+1）
    - omit_manifest / omit_sig: 模拟"错误 release"（缺资产）
    - draft / prerelease: 模拟非 stable release
    - installer_bytes / portable_bytes: 产物内容（默认小占位字节）
    - manifest_size_lie: manifest 中 size 字段相对真实 bytes 的偏差（模拟 size 谎言）
    - etag: 若设置，latest 响应带 ETag 头（304 测试）
    - asset_size_lie: GitHub API asset.size 相对真实 bytes 的偏差
    - release_body: release notes 文本
    """

    def __init__(
        self,
        repo: str = "owner/repo",
        version: str | None = None,
        current_version: str = "0.0.0",
        omit_manifest: bool = False,
        omit_sig: bool = False,
        draft: bool = False,
        prerelease: bool = False,
        installer_bytes: bytes | None = None,
        portable_bytes: bytes | None = None,
        manifest_size_lie: int = 0,
        asset_size_lie: int = 0,
        etag: str | None = None,
        release_body: str = "Release notes for the test release.",
    ) -> None:
        self.repo = repo
        self.private, self.public = generate_keypair()
        self.key_id = "test-key"
        self.version = version or next_patch_version(current_version)

        inst_name = expected_installer_filename(self.version)
        port_name = expected_portable_filename(self.version)
        self.installer_bytes = installer_bytes if installer_bytes is not None else b"FAKE-INSTALLER-" + inst_name.encode()
        self.portable_bytes = portable_bytes if portable_bytes is not None else b"FAKE-PORTABLE-" + port_name.encode()

        def _entry(name: str, data: bytes) -> dict:
            return {
                "filename": name,
                "size": len(data) + manifest_size_lie,
                "sha256": hashlib.sha256(data).hexdigest(),
            }

        self.manifest = build_manifest(
            self.version,
            installer=_entry(inst_name, self.installer_bytes),
            portable=_entry(port_name, self.portable_bytes),
            published_at="2026-09-15T00:00:00Z",
            application_schema=4,
            signing_key_id=self.key_id,
        )
        self.manifest_bytes = canonical_manifest_bytes(self.manifest)
        self.sig_text = sign_manifest(self.manifest_bytes, self.private, self.key_id).decode("ascii")

        self.etag = etag
        self._omit_manifest = omit_manifest
        self._omit_sig = omit_sig
        self.release = {
            "tag_name": self.version,
            "draft": draft,
            "prerelease": prerelease,
            "html_url": f"https://github.com/{repo}/releases/tag/{self.version}",
            "published_at": "2026-09-15T00:00:00Z",
            "body": release_body,
            "assets": [],
        }
        if not omit_manifest:
            self.release["assets"].append({
                "name": "release-manifest.json",
                "size": len(self.manifest_bytes) + asset_size_lie,
                "browser_download_url": f"https://files.example/{self.version}/release-manifest.json",
            })
        if not omit_sig:
            self.release["assets"].append({
                "name": "release-manifest.sig",
                "size": len(self.sig_text.encode("ascii")),
                "browser_download_url": f"https://files.example/{self.version}/release-manifest.sig",
            })
        self.release["assets"].append({
            "name": inst_name,
            "size": len(self.installer_bytes),
            "browser_download_url": f"https://files.example/{self.version}/{inst_name}",
        })
        self.release["assets"].append({
            "name": port_name,
            "size": len(self.portable_bytes),
            "browser_download_url": f"https://files.example/{self.version}/{port_name}",
        })

        self._not_modified = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/repos/{self.repo}/releases/latest":
            if self._not_modified and self.etag and request.headers.get("If-None-Match") == self.etag:
                return httpx.Response(304)
            headers = {"ETag": self.etag} if self.etag else {}
            return httpx.Response(200, json=self.release, headers=headers)
        if path.endswith("/release-manifest.json") and not self.omit_flag("manifest"):
            return httpx.Response(200, content=self.manifest_bytes)
        if path.endswith("/release-manifest.sig") and not self.omit_flag("sig"):
            return httpx.Response(200, content=self.sig_text.encode("ascii"))
        if path.endswith("-win-x64.exe"):
            return httpx.Response(200, content=self.installer_bytes)
        if path.endswith("-win-x64.zip"):
            return httpx.Response(200, content=self.portable_bytes)
        return httpx.Response(404)

    def omit_flag(self, what: str) -> bool:
        # handler 里引用的是构造参数；用闭包等价实现
        return getattr(self, f"_omit_{what}", False)

    @contextmanager
    def trusted(self):
        """
        让 update_manifest.verify_manifest_signature 信任本 FakeGithub 的临时公钥。
        流程测试（check/download）必须在 with fake.trusted(): 内运行，
        否则临时 key_id 不在内置 TRUSTED_UPDATE_KEYS 里会被当作"未知签名密钥"。
        """
        import update_manifest as um

        with mock.patch.object(um, "_trusted_keys", return_value={self.key_id: "x"}), \
             mock.patch.object(um, "_load_public_key", return_value=self.public):
            yield

    def client_factory(self):
        def factory(timeout: float):
            return httpx.AsyncClient(
                transport=httpx.MockTransport(self.handler), follow_redirects=True
            )
        return factory


def make_service(
    fake: FakeGithub,
    *,
    db=None,
    mode: str = "installed",
    updates_dir: Path,
    backups_dir: Path | None = None,
    request_exit=None,
    is_background: bool = False,
    check_enabled: bool = False,
    auto_download: bool = False,
    repository: str | None = None,
    config_path=None,
    now=None,
) -> UpdateService:
    """
    构造注入 MockTransport 的 UpdateService（测试用）。
    自动把 check/download/install/cancel 包进 fake.trusted() —— 让临时公钥被信任。
    """
    import functools

    svc = UpdateService(
        db=db,
        get_config=(lambda: _Cfg(_UpdatesCfg(check_enabled, 24.0, auto_download))),
        config_path=config_path,
        updates_dir=updates_dir,
        backups_dir=backups_dir,
        client_factory=fake.client_factory(),
        installation_mode=lambda: mode,
        request_exit=request_exit,
        is_background=lambda: is_background,
        repository=repository if repository is not None else fake.repo,
        api_base="https://api.github.com",
        now=now,
    )
    for name in ("check", "download", "install", "cancel"):
        orig = getattr(svc, name)

        @functools.wraps(orig)
        async def _wrapped(*a, _orig=orig, _fake=fake, **k):
            with _fake.trusted():
                return await _orig(*a, **k)

        setattr(svc, name, _wrapped)
    return svc
