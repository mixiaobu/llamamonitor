"""
Phase 13 测试：安装流程（Pre-Update Backup + pending marker + Installer 启动 + 优雅退出）
+ 安装模式差异（installed / portable / development）+ 更新成功检测 + 清理。

规则（§47-§64）：
- DB 本身 corrupt -> 拒绝安装（"Database health issue detected..."）；
- Pre-Update Backup（DB Backup API + quick_check / config 复制）失败 -> 不启动 Installer；
- 启动的是**已验证的固定路径**（不接受外部路径、不用 shell=True）；
- 参数 /SILENT /NORESTART /APPUPDATE[_BG]（后台模式带 --background 的新实例）；
- pending_update.json {from,to,installer_filename,verified_sha256,created_at}；
- 新版启动：to_version == 当前版本 -> update_success 事件 + 删 marker；
  不匹配 -> update_failed_mismatch 警告。

运行：python -m unittest discover -s tests
"""

import asyncio
import json
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import Database  # noqa: E402
from update_service import (  # noqa: E402
    PENDING_MARKER_NAME,
    IDLE,
    INSTALLING,
    READY_TO_INSTALL,
    check_pending_update,
)
from update_util import FakeGithub, make_service  # noqa: E402
from version import __version__  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def _seed_history(db: Database) -> None:
    """写入一点历史数据（验证备份内容完整）。"""
    conn = db._connect()
    conn.execute(
        "INSERT INTO daily_usage(date, prompt_tokens, cached_tokens, output_tokens, "
        "draft_tokens, accepted_tokens, prompt_seconds, predicted_seconds) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("2026-01-01", 1000, 0, 500, 0, 0, 1.0, 0.5),
    )
    conn.commit()


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.updates = self.tmp / "updates"
        self.db = Database(self.tmp / "monitor.db", wal=False)
        self.config_file = self.tmp / "config.json"
        self.config_file.write_text(json.dumps({"llama_server": {"url": "x"}}), encoding="utf-8")
        self._fake = None

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def _svc(self, **kw):
        self._fake = kw.pop("fake", None) or FakeGithub(
            repo="owner/repo", current_version=__version__)
        return make_service(
            self._fake, db=self.db,
            config_path=lambda: self.config_file,
            updates_dir=self.updates,
            backups_dir=self.tmp / "backups", **kw)

    def _check_and_download(self, svc, mode="installed"):
        run(svc.check(manual=True))
        run(svc.download())
        self.assertEqual(svc.state, READY_TO_INSTALL)


class InstallFlowTests(_Base):
    def test_install_full_flow(self):
        """installed 模式：backup + marker + Popen(固定路径, /SILENT /NORESTART /APPUPDATE) + request_exit。"""
        _seed_history(self.db)
        exit_calls = []
        svc = self._svc(mode="installed", request_exit=lambda: exit_calls.append(1) or True)
        self._check_and_download(svc)

        popen_calls = []
        real_popen = None

        def fake_popen(args, **kwargs):
            popen_calls.append((list(args), kwargs))
            return mock.Mock()

        with mock.patch("update_service.subprocess.Popen", side_effect=fake_popen):
            status = run(svc.install())

        self.assertEqual(status["state"], INSTALLING)
        self.assertEqual(exit_calls, [1], "request_exit 必须被调用（graceful shutdown）")
        self.assertEqual(len(popen_calls), 1)
        args, kwargs = popen_calls[0]
        # 启动的是已验证的固定路径（downloads 目录里的 exe）
        self.assertEqual(Path(args[0]).name,
                         f"LlamaMonitor-Setup-{self._fake.version}-win-x64.exe")
        self.assertIn(str(self.updates), str(Path(args[0])))
        self.assertEqual(args[1:], ["/SILENT", "/NORESTART", "/APPUPDATE"])
        self.assertNotIn("shell", kwargs)
        self.assertFalse(kwargs.get("shell", False))
        # pending marker
        marker = json.loads((self.updates / PENDING_MARKER_NAME).read_text(encoding="utf-8"))
        self.assertEqual(marker["from_version"], __version__)
        self.assertEqual(marker["to_version"], self._fake.version)
        self.assertEqual(marker["installer_filename"], args[0].split("\\")[-1])
        self.assertEqual(len(marker["verified_sha256"]), 64)
        self.assertIn("created_at", marker)
        # Pre-Update 备份存在（DB + config）
        backups = list((self.tmp / "backups").glob("pre_update_*"))
        db_backups = [b for b in backups if b.suffix == ".db"]
        cfg_backups = [b for b in backups if b.name.endswith("config.json")]
        self.assertEqual(len(db_backups), 1)
        self.assertEqual(len(cfg_backups), 1)
        # DB 备份内容完整（quick_check + 历史数据）
        conn = sqlite3.connect(str(db_backups[0]))
        self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
        row = conn.execute(
            "SELECT prompt_tokens, output_tokens FROM daily_usage WHERE date='2026-01-01'"
        ).fetchone()
        self.assertEqual(row, (1000, 500))
        conn.close()
        # 事件
        self.assertTrue(self.db.get_events(limit=50,
                                           since_ts=0) and any(
            e["event_type"] == "update_install_started"
            for e in self.db.get_events(limit=50)))

    def test_install_background_mode(self):
        """后台模式：/APPUPDATE_BG（新实例带 --background 启动）。"""
        svc = self._svc(mode="installed", is_background=True,
                        request_exit=lambda: True)
        self._check_and_download(svc)

        popen_calls = []

        def fake_popen(args, **kwargs):
            popen_calls.append(list(args))
            return mock.Mock()

        with mock.patch("update_service.subprocess.Popen", side_effect=fake_popen):
            run(svc.install())
        self.assertEqual(popen_calls[0][1:], ["/SILENT", "/NORESTART", "/APPUPDATE_BG"])

    def test_install_rejected_when_not_ready(self):
        """state != READY_TO_INSTALL（未下载）-> NOT_READY。"""
        svc = self._svc(mode="installed")
        run(svc.check(manual=True))
        try:
            run(svc.install())
            self.fail("应抛出 NOT_READY")
        except Exception as exc:
            self.assertIn("NOT_READY", getattr(exc, "code", "") or str(exc))

    def test_install_corrupt_db_rejected_before_launch(self):
        """§49：DB corrupt -> 拒绝安装，不启动 Installer、不做后续。"""
        _seed_history(self.db)
        self.db.set_health("corrupt", "test corruption")
        svc = self._svc(mode="installed", request_exit=lambda: True)
        self._check_and_download(svc)

        with mock.patch("update_service.subprocess.Popen") as popen:
            try:
                run(svc.install())
                self.fail("应抛出 DB_UNHEALTHY")
            except Exception as exc:
                self.assertIn("DB_UNHEALTHY", getattr(exc, "code", "") or str(exc))
                self.assertIn("数据库健康问题", str(exc))
            self.assertEqual(popen.call_count, 0, "corrupt DB 时绝不能启动 Installer")

    def test_install_backup_failure_no_installer_launch(self):
        """Pre-Update 备份失败 -> 不启动 Installer（§47/§48）。"""
        _seed_history(self.db)
        svc = self._svc(mode="installed", request_exit=lambda: True)
        self._check_and_download(svc)

        with mock.patch.object(svc, "_pre_update_backup", return_value=(False, "simulated backup failure")), \
             mock.patch("update_service.subprocess.Popen") as popen:
            try:
                run(svc.install())
                self.fail("应抛出 BACKUP_FAILED")
            except Exception as exc:
                self.assertIn("BACKUP_FAILED", getattr(exc, "code", "") or str(exc))
            self.assertEqual(popen.call_count, 0, "备份失败绝不能启动 Installer")
        self.assertFalse((self.updates / PENDING_MARKER_NAME).exists(),
                         "备份失败时不写 pending marker")


class ToctouRegressionTests(_Base):
    """Phase 14 审计回归（AUDIT-DATA-001）。"""

    def test_install_rehash_rejects_tampered_installer(self):
        """Popen 前重算 SHA-256：READY_TO_INSTALL -> Popen 窗口内 installer 被替换
        -> 不匹配绝不启动（fail-closed）。"""
        _seed_history(self.db)
        svc = self._svc(mode="installed", request_exit=lambda: True)
        self._check_and_download(svc)
        verified = svc.verified_path
        self.assertIsNotNone(verified)
        verified.write_bytes(verified.read_bytes() + b"TAMPERED")
        with mock.patch("update_service.subprocess.Popen") as popen:
            try:
                run(svc.install())
                self.fail("应抛出 HASH_MISMATCH")
            except Exception as exc:
                self.assertIn("HASH_MISMATCH", getattr(exc, "code", "") or str(exc))
                self.assertIn("下载后被修改", str(exc))
            self.assertEqual(popen.call_count, 0, "复验失败绝不能启动 Installer")
        self.assertFalse((self.updates / PENDING_MARKER_NAME).exists(),
                         "复验失败时不写 pending marker（在 Popen 前拦截）")


class PreUpdateBackupTests(_Base):
    def test_backup_creates_verified_db_and_config(self):
        _seed_history(self.db)
        (self.tmp / "backups").mkdir(parents=True, exist_ok=True)
        svc = self._svc()
        ok, detail = svc._pre_update_backup(__version__, "9.9.9", "20260915_120000",
                                            self.tmp / "backups")
        self.assertTrue(ok, detail)
        dbs = list((self.tmp / "backups").glob("pre_update_*.db"))
        cfgs = list((self.tmp / "backups").glob("pre_update_*_config.json"))
        self.assertEqual(len(dbs), 1)
        self.assertEqual(len(cfgs), 1)
        conn = sqlite3.connect(str(dbs[0]))
        self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
        self.assertEqual(conn.execute(
            "SELECT prompt_tokens FROM daily_usage WHERE date='2026-01-01'").fetchone()[0], 1000)
        conn.close()
        self.assertEqual(json.loads(cfgs[0].read_text(encoding="utf-8")),
                         json.loads(self.config_file.read_text(encoding="utf-8")))

    def test_backup_failure_on_invalid_db(self):
        """源 DB 不是合法 SQLite -> 备份失败 ok=False（ Installer 不启动由上层保证）。"""
        bad = self.tmp / "bad.db"
        bad.write_bytes(b"not a sqlite database at all")
        db2 = Database(bad, wal=False)
        svc = make_service(
            FakeGithub(repo="o/r", current_version=__version__),
            db=db2, updates_dir=self.updates, backups_dir=self.tmp / "backups",
            config_path=lambda: self.config_file)
        (self.tmp / "backups").mkdir(parents=True, exist_ok=True)
        ok, detail = svc._pre_update_backup(__version__, "9.9.9", "20260915_120000",
                                            self.tmp / "backups")
        self.assertFalse(ok)
        self.assertTrue(detail)


class InstallModeTests(_Base):
    def test_portable_download_selects_zip(self):
        """portable 模式：下载 portable ZIP（不是 installer）。"""
        svc = self._svc(mode="portable")
        run(svc.check(manual=True))
        status = run(svc.download())
        self.assertEqual(status["state"], READY_TO_INSTALL)
        zips = list(self.updates.glob("*/LlamaMonitor-*.zip"))
        exes = list(self.updates.glob("*/LlamaMonitor-Setup-*.exe"))
        self.assertEqual(len(zips), 1)
        self.assertEqual(len(exes), 0)

    def test_portable_install_rejected_no_self_overwrite(self):
        """§53：portable 从不自我覆盖 -> 安装被拒绝（用户手动更新）。"""
        svc = self._svc(mode="portable")
        self._check_and_download(svc, mode="portable")
        try:
            run(svc.install())
            self.fail("应抛出 PORTABLE_MODE")
        except Exception as exc:
            self.assertIn("PORTABLE_MODE", getattr(exc, "code", "") or str(exc))
            self.assertIn("不覆盖自身", str(exc))

    def test_development_check_allowed_download_install_rejected(self):
        """§52：development 模式 Check 可用，Download/Install 被禁用。"""
        svc = self._svc(mode="development")
        run(svc.check(manual=True))
        try:
            run(svc.download())
            self.fail("应抛出 DEVELOPMENT_MODE")
        except Exception as exc:
            self.assertIn("DEVELOPMENT_MODE", getattr(exc, "code", "") or str(exc))

    def test_missing_artifact_for_mode(self):
        """release 只有 portable（无 installer）+ installed 模式 -> 下载时 MISSING_ASSETS。"""
        # 构造一个无 installer 资产的 fake：用 manifest_size_lie 无关，直接裁剪 release
        fake = FakeGithub(repo="o/r", current_version=__version__)
        fake.release["assets"] = [
            a for a in fake.release["assets"] if not a["name"].endswith(".exe")
            and a["name"] in ("release-manifest.json", "release-manifest.sig")
        ]
        # manifest 仍声明 installer（签名包含它）；但 GitHub asset 缺失 -> check 阶段发现
        svc = make_service(fake, db=self.db, updates_dir=self.updates, mode="installed")
        status = run(svc.check(manual=True))
        self.assertEqual(status["state"], "ERROR")
        self.assertIn("不包含", status["error"])


class PendingUpdateDetectionTests(_Base):
    def _marker(self, to_version):
        (self.updates).mkdir(parents=True, exist_ok=True)
        (self.updates / PENDING_MARKER_NAME).write_text(json.dumps({
            "from_version": "0.0.1",
            "to_version": to_version,
            "installer_filename": "x.exe",
            "verified_sha256": "a" * 64,
            "created_at": "2026-09-15T12:00:00",
        }), encoding="utf-8")

    def test_success_recorded_and_marker_deleted(self):
        """§63：新版启动 + to_version 匹配 -> update_success 事件 + marker 删除。"""
        self._marker(__version__)
        result = check_pending_update(self.db, __version__, self.tmp, now=1_700_000_000)
        self.assertEqual(result, "success")
        self.assertFalse((self.updates / PENDING_MARKER_NAME).exists())
        events = [e for e in self.db.get_events(limit=50) if e["event_type"] == "update_success"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["details"]["to_version"], __version__)

    def test_mismatch_warning_and_marker_deleted(self):
        """to_version 不匹配 -> update_failed_mismatch 警告 + marker 删除。"""
        self._marker("9.9.9")
        result = check_pending_update(self.db, __version__, self.tmp, now=1_700_000_000)
        self.assertEqual(result, "mismatch")
        self.assertFalse((self.updates / PENDING_MARKER_NAME).exists())
        events = [e for e in self.db.get_events(limit=50)
                  if e["event_type"] == "update_failed_mismatch"]
        self.assertEqual(len(events), 1)

    def test_no_marker_none(self):
        self.assertEqual(check_pending_update(self.db, __version__, self.tmp), None)

    def test_corrupt_marker_none_no_crash(self):
        self.updates.mkdir(parents=True, exist_ok=True)
        (self.updates / PENDING_MARKER_NAME).write_text("{not json", encoding="utf-8")
        self.assertEqual(check_pending_update(self.db, __version__, self.tmp), None)


class CleanupTests(_Base):
    def test_stale_part_deleted_old_version_dirs_pruned(self):
        """§83-§85：>24h 的 .part 删除；版本目录保留最新 2 个。

        布局（避免目录 mtime 被 .part 删除改变）：
        - v1：25h 前 mtime、无 .part -> 应被 rmtree（最旧）；
        - v2：新目录、含 25h 前 .part -> .part 删除、目录保留；
        - v3：新目录、含新 .part -> 全保留。
        """
        import os as _os
        import time

        root = self.updates
        v1 = root / "0.0.1"
        v2 = root / "0.0.2"
        v3 = root / "0.0.3"
        for d in (v1, v2, v3):
            d.mkdir(parents=True)
        old_part = v2 / "x.part"
        new_part = v3 / "y.part"
        old_part.write_bytes(b"old")
        new_part.write_bytes(b"new")
        old_ts = time.time() - 25 * 3600
        _os.utime(old_part, (old_ts, old_ts))
        # v1 目录 mtime 设为最旧（25h 前），v2/v3 保持新建时间
        _os.utime(v1, (old_ts, old_ts))

        svc = self._svc()
        svc.cleanup_stale()

        self.assertFalse(old_part.exists(), ">24h 的 .part 必须删除")
        self.assertTrue(new_part.exists(), "新的 .part 保留")
        self.assertFalse(v1.exists(), "最旧版本目录必须清理")
        self.assertTrue(v2.exists())
        self.assertTrue(v3.exists())


class InnoScriptRunSectionTests(unittest.TestCase):
    """RC-003 回归：installer [Run] 段更新后自动启动 background 实例。

    Inno [Run] 的 Filename 字段只放可执行文件路径，命令行参数必须放
    Parameters 字段。旧实现把 `--background` 写进 Filename（
    `Filename: "{app}\\LlamaMonitor.exe --background"`），Inno 把整串当
    文件路径 -> CreateProcess error 2（文件找不到）-> 更新完成后新版
    不自动启动，用户需手动启动。本测试静态校验 [Run] 段 background
    启动项的参数位置正确。
    """

    @classmethod
    def setUpClass(cls):
        iss = Path(__file__).resolve().parent.parent / "installer" / "LlamaMonitor.iss"
        cls.text = iss.read_text(encoding="utf-8")

    def _run_section_lines(self):
        """提取 [Run] 段的所有 Filename 行。"""
        lines = self.text.splitlines()
        in_run = False
        run_lines = []
        for ln in lines:
            s = ln.strip()
            if s.startswith("[") and s.endswith("]"):
                in_run = (s == "[Run]")
                continue
            if in_run and s.startswith("Filename:"):
                run_lines.append(ln)
        return run_lines

    def test_background_run_uses_parameters_not_filename(self):
        bg = [l for l in self._run_section_lines() if "IsAppUpdateBg" in l]
        self.assertEqual(len(bg), 1, "应恰好有一条 IsAppUpdateBg 自动启动项")
        line = bg[0]
        # --background 不得出现在 Filename 值里
        m = re.search(r'Filename:\s*"([^"]+)"', line)
        self.assertTrue(m, "应有 Filename 字段")
        self.assertNotIn("--background", m.group(1),
                         "--background 不得写进 Filename（Inno 会把整串当文件路径）")
        # --background 必须在 Parameters 里
        self.assertIn("Parameters:", line)
        self.assertIn("--background", line.split("Parameters:", 1)[1])

    def test_run_filenames_have_no_trailing_args(self):
        """所有 [Run] 项的 Filename 值都应是纯路径（不含空格分隔的额外参数）。"""
        for ln in self._run_section_lines():
            m = re.search(r'Filename:\s*"([^"]+)"', ln)
            self.assertTrue(m, f"无法解析 Filename: {ln!r}")
            path = m.group(1)
            self.assertNotIn(" --", path,
                             f"Filename 含命令行参数（应为 Parameters）: {ln!r}")


if __name__ == "__main__":
    unittest.main()
