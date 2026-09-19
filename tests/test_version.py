"""
Phase 12 测试：统一版本号（version.py 单一来源）与 /api/version。

- version 格式 ^\d+\.\d+\.\d+$（无 v 前缀）；
- /api/version：name/version/app_version/schema_version（schema 来自数据库，不硬编码）；
- /api/status 携带 version；
- `LlamaMonitor.exe --version` CLI：不启动 Collector/FastAPI/Tray/DB；
- PyInstaller 版本资源生成（PE FileVersion = x.y.z.0，唯一转换点）。

运行：python -m unittest discover -s tests
"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import AppConfig, LoadedConfig  # noqa: E402
from db import CURRENT_SCHEMA_VERSION, Database  # noqa: E402
from server import build_app  # noqa: E402


def _load_version_module():
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("lm_version_under_test", root / "version.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class VersionFormatTests(unittest.TestCase):
    def test_version_matches_semver_digits(self):
        mod = _load_version_module()
        self.assertRegex(mod.__version__, r"^\d+\.\d+\.\d+$")
        self.assertFalse(mod.__version__.startswith("v"), "内部 version 值不带 v 前缀")
        self.assertEqual(mod.APP_NAME, "LlamaMonitor")

    def test_pe_file_version_conversion(self):
        """§9：SemVer x.y.z -> Windows FileVersion x.y.z.0（build_release 统一转换）。"""
        mod = _load_version_module()
        root = Path(__file__).resolve().parent.parent
        sys.path.insert(0, str(root / "scripts"))
        import build_release

        self.assertTrue(build_release.VERSION_RE.match(mod.__version__))
        # 实际生成版本资源（build/ 是临时构建目录，测试值 9.8.7 便于断言）
        path = build_release.write_version_info("9.8.7")
        try:
            text = path.read_text(encoding="utf-8")
            self.assertIn("filevers=(9, 8, 7, 0)", text)
            self.assertIn("prodvers=(9, 8, 7, 0)", text)
            self.assertIn("StringStruct('FileVersion', '9.8.7.0')", text)
            self.assertIn("StringStruct('ProductVersion', '9.8.7')", text)
            self.assertIn("StringStruct('ProductName', 'LlamaMonitor')", text)
            self.assertIn("StringStruct('FileDescription', 'LlamaMonitor')", text)
            self.assertIn("StringStruct('CompanyName', 'LlamaMonitor Project')", text)
        finally:
            # 恢复真实版本对应的资源文件（避免残留 9.8.7）
            build_release.write_version_info(mod.__version__)


class ApiVersionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.db = Database(tmp / "v.db", wal=False, retention_seconds=3600)
        cfg = AppConfig.default()
        cfg.llama_server.url = "http://127.0.0.1:9"
        loaded = LoadedConfig(cfg, tmp / "config.json", True, False, False)
        from collector import MetricsCollector

        self.app = build_app(self.db, MetricsCollector(cfg, self.db), loaded, None)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _mod(self):
        return _load_version_module()

    def test_api_version_endpoint(self):
        mod = self._mod()
        with TestClient(self.app) as client:
            r = client.get("/api/version")
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertEqual(data["name"], "LlamaMonitor")
            self.assertEqual(data["version"], mod.__version__)
            self.assertEqual(data["app_version"], mod.__version__)
            self.assertEqual(data["schema_version"], CURRENT_SCHEMA_VERSION)

    def test_api_status_includes_version(self):
        mod = self._mod()
        with TestClient(self.app) as client:
            r = client.get("/api/status")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json().get("version"), mod.__version__)

    def test_schema_version_not_hardcoded_in_frontend(self):
        """§6：前端必须从 API 取 schema 版本 —— 前端不得出现 schema 数字硬编码。

        Phase 15 起前端模块化：About 区在 settings.js 的 loadAbout() 里从
        /api/version 取 schema，不再写死 "3"。
        """
        root = Path(__file__).resolve().parent.parent
        js_dir = root / "static" / "js"
        frontend = {
            p.name: p.read_text(encoding="utf-8")
            for p in js_dir.glob("*.js")
        }
        html = (root / "static" / "index.html").read_text(encoding="utf-8")
        # /api/version 必须在前端被调用（About 从 API 取 schema，见 settings.js loadAbout）
        self.assertIn("/api/version", frontend.get("settings.js", ""))
        # schema 版本不得在任何前端文件里被写死
        blob = html + "\n" + "\n".join(frontend.values())
        self.assertNotIn('aboutSchema").textContent = "3"', blob)
        self.assertNotIn('aboutSchema").textContent = "4"', blob)
        self.assertNotIn("schema_version = 3", blob)


class VersionCliTests(unittest.TestCase):
    def test_main_version_flag_prints_and_exits_zero(self):
        """--version：打印 '<APP> <version>'、退出码 0；不启动任何组件。"""
        import desktop

        mod = _load_version_module()
        import io

        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            rc = desktop.main(["--version"])
            out = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), f"{mod.APP_NAME} {mod.__version__}")


if __name__ == "__main__":
    unittest.main()
