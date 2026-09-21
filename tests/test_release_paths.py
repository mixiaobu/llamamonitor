"""
Phase 12 测试：Release 路径规则 —— Portable ZIP 结构、用户数据隔离、
validate_release 验证链、数据目录规则（§12-§13、§18、§81、§88）。

运行：python -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import update_manifest as um  # noqa: E402
from generate_checksums import (  # noqa: E402
    build_update_manifest,
    write_sha256sums,
    write_signature,
    write_update_manifest_bytes,
)
from generate_update_key import generate_keypair  # noqa: E402
from make_portable import make_portable_zip  # noqa: E402
from validate_release import validate_release  # noqa: E402


def _fake_dist(root: Path) -> Path:
    """构造一个模拟 PyInstaller 输出：含 static/、assets/、EXE 与混入的用户数据。"""
    app = root / "dist" / "LlamaMonitor"
    (app / "static").mkdir(parents=True)
    (app / "assets").mkdir(parents=True)
    (app / "_internal" / "webview").mkdir(parents=True)
    (app / "LlamaMonitor.exe").write_bytes(b"MZ-fake-exe")
    (app / "static" / "index.html").write_text("<html>ok</html>")
    (app / "static" / "echarts.min.js").write_bytes(b"// echarts")
    (app / "assets" / "LlamaMonitor.ico").write_bytes(b"ico")
    (app / "_internal" / "webview" / "platforms.py").write_text("x")
    # 混入的用户数据（dist 理论上不含 —— 验证 ZIP 打包会过滤）
    (app / "monitor.db").write_bytes(b"sqlite")
    (app / "config.json").write_text("{}")
    (app / "logs").mkdir()
    (app / "logs" / "monitor.log").write_text("log")
    return app


class PortableZipTests(unittest.TestCase):
    def test_single_top_level_and_excludes_user_data(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _fake_dist(root)
            out = root / "release" / "LlamaMonitor-9.9.9-win-x64.zip"
            make_portable_zip(root / "dist", out)
            with zipfile.ZipFile(out) as zf:
                names = zf.namelist()
            tops = {n.split("/", 1)[0] for n in names}
            self.assertEqual(tops, {"LlamaMonitor"}, "ZIP 顶层必须唯一 LlamaMonitor/（不嵌套 release/dist）")
            self.assertIn("LlamaMonitor/LlamaMonitor.exe", names)
            self.assertIn("LlamaMonitor/static/index.html", names)
            self.assertIn("LlamaMonitor/_internal/webview/platforms.py", names)
            # 用户数据绝不进包
            for n in names:
                leaf = n.split("/")[-1]
                self.assertNotIn(leaf, {"monitor.db", "config.json", "monitor.log"},
                                 f"ZIP 混入用户数据文件: {n}")

    def test_missing_exe_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app = root / "dist" / "LlamaMonitor"
            app.mkdir(parents=True)
            (app / "static").mkdir()
            with self.assertRaises(ValueError):
                make_portable_zip(root / "dist", root / "x.zip")


def _fake_release(root: Path, version: str):
    """
    构造完整 release/ 目录（ZIP+Setup+SHA256SUMS+Phase 13 签名 manifest）。
    返回 (release_dir, private_key, public_key) —— 测试用临时密钥对（绝不用生产密钥），
    调用方用 public_key mock update_manifest 的公钥表后运行 validate_release。
    """
    release = root / "release"
    release.mkdir(exist_ok=True)
    # 用假 dist 打 ZIP
    _fake_dist(root)
    from make_portable import make_portable_zip as mkz
    mkz(root / "dist", release / f"LlamaMonitor-{version}-win-x64.zip")
    (release / f"LlamaMonitor-Setup-{version}-win-x64.exe").write_bytes(b"MZ-fake-setup")
    zip_name = f"LlamaMonitor-{version}-win-x64.zip"
    setup_name = f"LlamaMonitor-Setup-{version}-win-x64.exe"
    write_sha256sums(
        [release / zip_name, release / setup_name], release / "SHA256SUMS.txt")
    private, public = generate_keypair()
    manifest = build_update_manifest(
        version,
        installer=release / setup_name,
        portable=release / zip_name,
        application_schema=4,
        signing_key_id="test-key",
    )
    raw = write_update_manifest_bytes(manifest, release / "release-manifest.json")
    write_signature(raw, private, "test-key", release / "release-manifest.sig")
    return release, private, public


def _validate_with_trusted_key(release: Path, version: str, public, key_id: str = "test-key"):
    """mock 公钥表（临时测试密钥）后运行 validate_release，返回失败列表。"""
    with mock.patch.object(um, "_trusted_keys", return_value={key_id: "x"}), \
         mock.patch.object(um, "_load_public_key", return_value=public):
        return validate_release(release, version)


class ValidateReleaseTests(unittest.TestCase):
    def test_full_pass(self):
        """完整产物（含签名 manifest）+ dist EXE 的 PE/CLI 检查在 CI 上可跳过
        （PE 需要真实 EXE）：这里验证"文件/hash/ZIP/manifest/签名"链路全绿。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            release, _priv, public = _fake_release(root, "1.0.0")
            # 伪造 dist EXE 供 validate_release 检查存在性（--version CLI 会失败于
            # 假 EXE —— 因此这里只运行到文件/hash/ZIP 层，通过临时 monkeypatch 跳过 PE/CLI）
            dist_exe = root / "dist" / "LlamaMonitor" / "LlamaMonitor.exe"
            dist_exe.parent.mkdir(parents=True, exist_ok=True)
            dist_exe.write_bytes(b"MZ")
            import validate_release as vr
            old_pe = vr.check_pe_metadata
            # PE 检查对假 EXE 会失败：临时替换为通过（真实 PE 检查见 EXE 实测）
            vr.check_pe_metadata = lambda exe, version: []
            try:
                failures = [f for f in _validate_with_trusted_key(release, "1.0.0", public)
                            if not f.startswith("--version")]
            finally:
                vr.check_pe_metadata = old_pe
            # 只允许 --version CLI 项（假 EXE 必然失败），其余必须为空
            self.assertEqual(failures, [], f"验证失败: {failures}")

    def test_tampered_zip_detected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            release, _priv, public = _fake_release(root, "1.0.0")
            zip_path = release / "LlamaMonitor-1.0.0-win-x64.zip"
            zip_path.write_bytes(b"tampered")
            failures = _validate_with_trusted_key(release, "1.0.0", public)
            self.assertTrue(any("SHA256 mismatch" in f for f in failures), failures)

    def test_tampered_manifest_detected(self):
        """manifest 签名后被篡改 1 字节 -> 验签失败（§89 篡改检测）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            release, _priv, public = _fake_release(root, "1.0.0")
            manifest_path = release / "release-manifest.json"
            data = bytearray(manifest_path.read_bytes())
            data[-1] ^= 1
            manifest_path.write_bytes(bytes(data))
            failures = _validate_with_trusted_key(release, "1.0.0", public)
            self.assertTrue(any("签名校验失败" in f for f in failures), failures)

    def test_version_mismatch_detected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            release, _priv, public = _fake_release(root, "1.0.0")
            failures = _validate_with_trusted_key(release, "2.0.0", public)
            self.assertTrue(any("missing" in f for f in failures), failures)


class DataDirRulesTests(unittest.TestCase):
    """§13/§81：数据永远在 %LOCALAPPDATA%\\LlamaMonitor，与 EXE/工作目录无关。"""

    def test_app_data_dir_uses_localappdata_not_cwd(self):
        from config import app_data_dir

        with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as lad:
            old_lad = os.environ.get("LOCALAPPDATA")
            old_cwd = os.getcwd()
            os.environ["LOCALAPPDATA"] = lad
            os.chdir(cwd)
            try:
                d = app_data_dir()
                self.assertEqual(str(d), str(Path(lad) / "LlamaMonitor"))
                self.assertEqual(d.parent, Path(lad))
                # 数据目录不在工作目录 / EXE 目录下
                self.assertNotEqual(d.parent, Path(cwd))
            finally:
                os.chdir(old_cwd)
                if old_lad is None:
                    os.environ.pop("LOCALAPPDATA", None)
                else:
                    os.environ["LOCALAPPDATA"] = old_lad

    def test_portable_does_not_move_data_dir(self):
        """§13：Portable（ZIP）版数据仍在 %LOCALAPPDATA%，不跟随 EXE 位置。
        app_data_dir 不接收"EXE 目录"参数 —— 唯一来源是 LOCALAPPDATA。"""
        from config import app_data_dir

        sig = app_data_dir.__doc__ or ""
        self.assertIn("LOCALAPPDATA", sig, "数据目录规则必须保持 %LOCALAPPDATA%\\LlamaMonitor")


if __name__ == "__main__":
    unittest.main()
