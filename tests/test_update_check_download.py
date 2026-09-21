"""
Phase 13 测试：update 检查（GitHub Release 发现 + 验签 + 版本比较 + ETag）
与流式下载（size/SHA-256 校验、取消、重定向、网络错误、磁盘保护）。

全部用 MockTransport（临时密钥 + FakeGithub），不访问真实网络；
db 用真实临时 Database（events / app_state runtime metadata）。

运行：python -m unittest discover -s tests
"""

import asyncio
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
import shutil

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import Database  # noqa: E402
from update_service import (  # noqa: E402
    DOWNLOADING,
    ERROR,
    IDLE,
    READY_TO_INSTALL,
    UP_TO_DATE,
    UPDATE_AVAILABLE,
)
from update_util import FakeGithub, make_service  # noqa: E402
from version import __version__  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.updates = self.tmp / "updates"
        self.db = Database(self.tmp / "monitor.db", wal=False)

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def events(self, event_type: str) -> list[dict]:
        return [e for e in self.db.get_events(limit=200) if e["event_type"] == event_type]


class CheckFlowTests(_Base):
    def _fake(self, **kw) -> FakeGithub:
        return FakeGithub(repo="owner/repo", current_version=__version__, **kw)

    def _svc(self, fake, **kw):
        return make_service(fake, db=self.db, updates_dir=self.updates, **kw)

    def test_not_configured_no_network(self):
        """UPDATE_REPOSITORY 为空 -> '更新服务未配置。'，不访问网络。"""
        fake = self._fake()
        svc = make_service(fake, db=self.db, updates_dir=self.updates, repository="")
        status = run(svc.check(manual=True))
        self.assertEqual(status["state"], IDLE)
        self.assertIn("更新服务未配置", status["error"])

    def test_check_finds_available_update(self):
        fake = self._fake()
        svc = self._svc(fake)
        status = run(svc.check(manual=True))
        self.assertEqual(status["state"], UPDATE_AVAILABLE)
        self.assertEqual(status["available_version"], fake.version)
        rel = status["release"]
        self.assertEqual(rel["signing_key_id"], "test-key")
        self.assertEqual(rel["installer"]["size"], len(fake.installer_bytes))
        self.assertEqual(rel["portable"]["size"], len(fake.portable_bytes))
        self.assertIn("Release notes for the test release.", rel["release_notes"])
        # last check 持久化到 app_state（§70：runtime metadata，不污染 state 表）
        self.assertIsNotNone(self.db.get_app_state("app.update.last_check"))
        # events
        self.assertTrue(self.events("update_check"))
        self.assertTrue(self.events("update_available"))

    def test_downgrade_ignored(self):
        """remote == current（或更低）-> UP_TO_DATE，绝不自动降级（§18/§19）。"""
        fake = self._fake(version=__version__)
        svc = self._svc(fake)
        status = run(svc.check(manual=True))
        self.assertEqual(status["state"], UP_TO_DATE)
        self.assertIsNone(status["available_version"])

    def test_old_version_ignored(self):
        fake = self._fake(version="0.0.1")
        svc = self._svc(fake)
        status = run(svc.check(manual=True))
        self.assertEqual(status["state"], UP_TO_DATE)

    def test_missing_sig_asset(self):
        """release 缺 .sig -> '更新无效'（缺少资产）。"""
        fake = self._fake(omit_sig=True)
        svc = self._svc(fake)
        status = run(svc.check(manual=True))
        self.assertEqual(status["state"], ERROR)
        self.assertIn("缺少", status["error"])
        self.assertTrue(self.events("update_check_failed"))

    def test_missing_manifest_asset(self):
        fake = self._fake(omit_manifest=True)
        svc = self._svc(fake)
        status = run(svc.check(manual=True))
        self.assertEqual(status["state"], ERROR)
        self.assertIn("缺少", status["error"])

    def test_draft_release_rejected(self):
        fake = self._fake(draft=True)
        svc = self._svc(fake)
        status = run(svc.check(manual=True))
        self.assertEqual(status["state"], ERROR)
        self.assertIn("不是正式版", status["error"])

    def test_prerelease_release_rejected(self):
        fake = self._fake(prerelease=True)
        svc = self._svc(fake)
        status = run(svc.check(manual=True))
        self.assertEqual(status["state"], ERROR)
        self.assertIn("不是正式版", status["error"])

    def test_rate_limit(self):
        """429 -> 速率限制提示。"""
        fake = self._fake()

        def rate_limit_handler(request):
            if request.url.path.endswith("/releases/latest"):
                return httpx.Response(429, json={"message": "rate limited"})
            return fake.handler(request)

        def factory(timeout):
            return httpx.AsyncClient(transport=httpx.MockTransport(rate_limit_handler))

        svc = UpdateServiceForTest(factory, self.db, self.updates, "owner/repo")
        status = run(svc.check(manual=True))
        self.assertEqual(status["state"], ERROR)
        self.assertIn("速率限制", status["error"])

    def test_network_error_transient(self):
        """网络错误 -> 无法检查更新，监控主流程不受影响。"""
        def factory(timeout):
            def boom(request):
                raise httpx.ConnectError("connection refused")
            return httpx.AsyncClient(transport=httpx.MockTransport(boom))

        svc = UpdateServiceForTest(factory, self.db, self.updates, "owner/repo")
        status = run(svc.check(manual=True))
        self.assertEqual(status["state"], ERROR)
        self.assertIn("无法检查更新", status["error"])

    def test_etag_304_not_modified(self):
        """§71：带 ETag 的第二次检查 -> 304 -> 保留上次结果（不重复下载/解析）。"""
        fake = self._fake(etag='"abc123"')
        svc = self._svc(fake)
        first = run(svc.check(manual=True))
        self.assertEqual(first["state"], UPDATE_AVAILABLE)
        saved_etag = self.db.get_app_state("app.update.github_etag")
        self.assertEqual(saved_etag, '"abc123"')
        fake._not_modified = True
        second = run(svc.check(manual=True))
        self.assertEqual(second["state"], UPDATE_AVAILABLE)  # 保留已验证状态
        self.assertEqual(second["available_version"], fake.version)
        self.assertTrue(self.events("update_check"))


class UpdateServiceForTest:
    """make_service 的简化别名（无 db 配置时用于纯网络场景）。"""

    def __new__(cls, client_factory, db, updates_dir, repo):
        from update_service import UpdateService

        return UpdateService(
            db=db,
            updates_dir=updates_dir,
            client_factory=client_factory,
            installation_mode=lambda: "installed",
            repository=repo,
            api_base="https://api.github.com",
        )


class DownloadFlowTests(_Base):
    def _fake(self, **kw) -> FakeGithub:
        return FakeGithub(repo="owner/repo", current_version=__version__, **kw)

    def _check(self, svc):
        return run(svc.check(manual=True))

    def _downloaded_file(self):
        f = list(self.updates.glob("*/LlamaMonitor-Setup-*.exe"))
        return f[0] if f else None

    def test_download_success_installer(self):
        """installed 模式：下载 installer -> VERIFYING -> READY_TO_INSTALL（os.replace 转正）。"""
        fake = self._fake(installer_bytes=b"X" * 5_000_000)
        svc = make_service(fake, db=self.db, mode="installed", updates_dir=self.updates)
        self._check(svc)
        status = run(svc.download())
        self.assertEqual(status["state"], READY_TO_INSTALL)
        self.assertEqual(status["downloaded_bytes"], 5_000_000)
        f = self._downloaded_file()
        self.assertIsNotNone(f)
        self.assertFalse(f.name.endswith(".part"))
        # 无残留 .part
        self.assertEqual(list(self.updates.glob("*.part")), [])
        self.assertEqual(list(self.updates.glob("*/*.part")), [])
        # 事件
        self.assertTrue(self.events("update_download_started"))
        self.assertTrue(self.events("update_download_complete"))
        # 内容一致（流式 SHA-256 已保证，这里再核一次）
        self.assertEqual(hashlib.sha256(f.read_bytes()).hexdigest(),
                         hashlib.sha256(fake.installer_bytes).hexdigest())

    def test_download_truncated_size_mismatch(self):
        """服务端提前结束 -> size mismatch -> ERROR + 删除 .part（§36）。"""
        full = b"Y" * 100_000
        fake = FakeGithub(repo="owner/repo", current_version=__version__,
                          installer_bytes=full)
        svc = make_service(fake, db=self.db, mode="installed", updates_dir=self.updates)
        self._check(svc)
        # 下载端返回截断内容（manifest 声明完整 size）-> written != expected
        def trunc_factory(timeout):
            def handler(request):
                if request.url.path.endswith("-win-x64.exe"):
                    return httpx.Response(200, content=full[:50_000])
                return fake.handler(request)
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        svc._client_factory = trunc_factory
        status = run(svc.download())
        self.assertEqual(status["state"], ERROR)
        self.assertIn("大小不匹配", status["error"])
        self.assertIsNone(self._downloaded_file())
        self.assertEqual(list(self.updates.glob("*/*.part")), [], ".part 必须被删除")
        self.assertTrue(self.events("update_verification_failed"))

    def test_download_hash_mismatch(self):
        """内容 hash 不符 -> HASH_MISMATCH -> ERROR + 删除 .part（§37）。"""
        full = b"Z" * 80_000
        fake = self._fake(installer_bytes=full)
        svc = make_service(fake, db=self.db, mode="installed", updates_dir=self.updates)
        self._check(svc)
        evil = b"z" * 80_000  # 同长度不同内容

        def evil_factory(timeout):
            def handler(request):
                if request.url.path.endswith("-win-x64.exe"):
                    return httpx.Response(200, content=evil)
                return fake.handler(request)
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        svc._client_factory = evil_factory
        status = run(svc.download())
        self.assertEqual(status["state"], ERROR)
        self.assertIn("SHA-256 不匹配", status["error"])
        self.assertIsNone(self._downloaded_file())
        self.assertTrue(self.events("update_verification_failed"))

    def test_download_disk_space_insufficient(self):
        """§34：磁盘空间不足（< size + 500MB）-> INSUFFICIENT_DISK_SPACE，不开始下载。"""
        fake = self._fake(installer_bytes=b"Q" * 10_000)
        svc = make_service(fake, db=self.db, mode="installed", updates_dir=self.updates)
        self._check(svc)
        with mock.patch.object(shutil, "disk_usage",
                               return_value=type("DU", (), {"free": 1024})()):
            status = run(svc.download())
        self.assertEqual(status["state"], ERROR)
        self.assertIn("磁盘空间不足", status["error"])
        self.assertIsNone(self._downloaded_file())

    def test_download_cancel(self):
        """§40：下载中 cancel -> 删除 .part，回到 UPDATE_AVAILABLE（不强杀线程）。"""
        chunks = [b"C" * (1024 * 1024)] * 10  # 10 MB，分 10 个 1MB chunk
        full = b"".join(chunks)
        fake = self._fake(installer_bytes=full)
        svc = make_service(fake, db=self.db, mode="installed", updates_dir=self.updates)
        self._check(svc)

        started = asyncio.Event()
        hold = asyncio.Event()

        class _SlowStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                for i in range(10):
                    yield b"C" * (1024 * 1024)
                    if i == 3:
                        started.set()    # 下载已进行 4 个 chunk
                        await hold.wait()  # 暂停：等 cancel 后再放行（保证取消发生在下载中）

        def slow_factory(timeout):
            def handler(request):
                if request.url.path.endswith("-win-x64.exe"):
                    return httpx.Response(200, stream=_SlowStream())
                return fake.handler(request)
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        svc._client_factory = slow_factory

        async def scenario():
            task = asyncio.create_task(svc.download())
            await started.wait()          # 等下载真正进行到第 4 个 chunk
            self.assertEqual(svc.state, DOWNLOADING)
            await svc.cancel()            # 置 cancel event
            hold.set()                    # 放行下一个 chunk -> 下载循环检测到 cancel
            return await task             # 下载循环检测到 event 后返回

        status = asyncio.run(scenario())
        self.assertEqual(status["state"], UPDATE_AVAILABLE)
        self.assertIn("已取消", status["error"] or "")
        self.assertEqual(list(self.updates.glob("*/*.part")), [], "取消后 .part 必须删除")

    def test_download_requires_check_first(self):
        """未 Check（state=IDLE）就 Download -> 400 NOT_READY。"""
        fake = self._fake()
        svc = make_service(fake, db=self.db, mode="installed", updates_dir=self.updates)
        try:
            run(svc.download())
            self.fail("应抛出 NOT_READY")
        except Exception as exc:
            self.assertIn("NOT_READY", getattr(exc, "code", "") or str(exc))

    def test_download_development_mode_rejected(self):
        """development 模式：下载被禁用。"""
        fake = self._fake()
        svc = make_service(fake, db=self.db, mode="development", updates_dir=self.updates)
        self._check(svc)
        try:
            run(svc.download())
            self.fail("应抛出 DEVELOPMENT_MODE")
        except Exception as exc:
            self.assertIn("DEVELOPMENT_MODE", getattr(exc, "code", "") or str(exc))


class AuditRegressionTests(_Base):
    """Phase 14 审计回归（AUDIT-SEC-003 / ASYNC-001 / WIN-003）。"""

    def _fake(self, **kw) -> FakeGithub:
        return FakeGithub(repo="owner/repo", current_version=__version__, **kw)

    def _svc(self, fake, **kw):
        return make_service(fake, db=self.db, mode="installed", updates_dir=self.updates, **kw)

    def test_oversized_release_json_rejected(self):
        """AUDIT-SEC-003：/releases/latest > 5MB -> BAD_RELEASE（原 resp.json() 无限制）。"""
        from update_service import UpdateService

        big = b"x" * (6 * 1024 * 1024)

        def factory(timeout):
            def handler(request):
                return httpx.Response(200, content=big)
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        svc = UpdateService(db=self.db, updates_dir=self.updates, client_factory=factory,
                            installation_mode=lambda: "installed", repository="owner/repo",
                            api_base="https://api.github.com")
        status = run(svc.check(manual=True))
        self.assertEqual(status["state"], ERROR)
        self.assertIn("过大", status["error"])

    def test_oversized_manifest_rejected(self):
        """AUDIT-SEC-003：manifest > 1MB -> BAD_RELEASE。"""
        fake = self._fake()
        big = b"m" * (2 * 1024 * 1024)
        svc = self._svc(fake)

        def factory(timeout):
            def handler(request):
                if request.url.path.endswith("/release-manifest.json"):
                    return httpx.Response(200, content=big)
                return fake.handler(request)
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        svc._client_factory = factory
        status = run(svc.check(manual=True))
        self.assertEqual(status["state"], ERROR)
        self.assertIn("过大", status["error"])

    def test_oversized_sig_rejected(self):
        """AUDIT-SEC-003：.sig > 64KB -> BAD_RELEASE。"""
        fake = self._fake()
        big = b"s" * (65 * 1024)
        svc = self._svc(fake)

        def factory(timeout):
            def handler(request):
                if request.url.path.endswith("/release-manifest.sig"):
                    return httpx.Response(200, content=big)
                return fake.handler(request)
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        svc._client_factory = factory
        status = run(svc.check(manual=True))
        self.assertEqual(status["state"], ERROR)
        self.assertIn("过大", status["error"])

    def test_auto_download_task_reference(self):
        """AUDIT-ASYNC-001：auto-download 任务有强引用（防 GC 中途回收），完成后释放。"""
        fake = self._fake()
        svc = self._svc(fake, check_enabled=True, auto_download=True)

        async def scenario():
            await svc.check(manual=False)
            self.assertIsNotNone(svc._auto_download_task, "auto-download 任务引用必须保存")
            await asyncio.shield(svc._auto_download_task)
            self.assertIsNone(svc._auto_download_task, "完成后引用必须释放")

        run(scenario())
        self.assertEqual(svc.state, READY_TO_INSTALL)  # 自动下载完成

    def test_task_cancel_during_download_cleans_part(self):
        """AUDIT-WIN-003：下载中任务被 cancel（shutdown，非 cancel event）->
        .part 清理、状态回非 busy（原来 .part 留 24h、状态卡 DOWNLOADING）。"""
        fake = self._fake(installer_bytes=b"K" * (5 * 1024 * 1024))
        svc = self._svc(fake)
        run(svc.check(manual=True))

        started = asyncio.Event()
        hold = asyncio.Event()

        class _SlowStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                started.set()      # .part 已创建（下载已开始）
                await hold.wait()
                yield b"K" * (5 * 1024 * 1024)

        def slow_factory(timeout):
            def handler(request):
                if request.url.path.endswith("-win-x64.exe"):
                    return httpx.Response(200, stream=_SlowStream())
                return fake.handler(request)
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        svc._client_factory = slow_factory

        async def scenario():
            task = asyncio.create_task(svc.download())
            await started.wait()
            self.assertEqual(svc.state, DOWNLOADING)
            task.cancel()          # 模拟 shutdown 对任务的 cancel（CancelledError 路径）
            with self.assertRaises(asyncio.CancelledError):
                await task
            hold.set()

        run(scenario())
        self.assertNotEqual(svc.state, DOWNLOADING, "cancel 后不能卡在 DOWNLOADING")
        self.assertEqual(list(self.updates.glob("*/*.part")), [], "cancel 后 .part 必须删除")


if __name__ == "__main__":
    unittest.main()
