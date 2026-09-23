"""
Phase 7 测试：统一配置系统（config.py）+ /api/config + /api/status 配置状态。

覆盖（对应 Phase 7 规格要求的 12 项 + 附加项）：
 1. 配置不存在 -> 自动创建 config.json
 2. 默认配置正确（全部键与值）
 3. 用户配置覆盖默认值
 4. 缺失嵌套字段自动补默认
 5. 旧配置兼容新版本新增字段
 6. 非法数值字段回退默认
 7. 非法 web.port 回退默认
 8. 非法 ui.theme 回退默认
 9. 损坏的 JSON 不覆盖原文件
10. database.path 为空时正确解析到 %LOCALAPPDATA%
11. metrics URL 拼接正确（含 base 已带 path 的容错）
12. 环境变量 LLAMAMONITOR_CONFIG 覆盖默认路径
附加：未知字段告警不致命、WAL 开关、命令行覆盖、/api/config 字段选择、
      /api/status 的 config 状态（含配置错误态）、日志落盘与幂等。

测试统一把配置路径指向临时目录（load_config(path=...) / 临时 LOCALAPPDATA），
不污染真实的 %LOCALAPPDATA%\\LlamaMonitor。
项目使用标准库 unittest（非 pytest）：临时目录用 tempfile.TemporaryDirectory
（等价于 pytest 的 tmp_path fixture）。

在项目根目录运行：
    python -m unittest discover -s tests
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config as cfgmod
from config import AppConfig, LoadedConfig, build_metrics_url
from configutil import make_config, make_loaded
from db import Database


class ConfigFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg_file = self.tmp / "config.json"

    def tearDown(self):
        self._tmp.cleanup()

    # 1 + 2) 自动创建 + 默认配置正确
    def test_missing_config_is_auto_created_with_defaults(self):
        loaded = cfgmod.load_config(self.cfg_file)
        self.assertTrue(self.cfg_file.is_file())
        self.assertEqual(json.loads(self.cfg_file.read_text(encoding="utf-8")), cfgmod.DEFAULT_CONFIG)
        self.assertTrue(loaded.loaded)
        self.assertTrue(loaded.using_defaults)
        self.assertFalse(loaded.has_errors)
        cfg = loaded.config
        self.assertEqual(cfg.llama_server.url, "http://127.0.0.1:9091")
        self.assertEqual(cfg.llama_server.metrics_path, "/metrics")
        self.assertEqual(cfg.llama_server.timeout_seconds, 3.0)
        self.assertEqual(cfg.collector.poll_interval_seconds, 5.0)
        self.assertEqual(cfg.collector.live_retention_hours, 48.0)
        self.assertEqual(cfg.web.host, "127.0.0.1")
        self.assertEqual(cfg.web.port, 8765)
        self.assertEqual(cfg.database.path, "")
        self.assertTrue(cfg.database.wal)
        self.assertEqual(cfg.ui.refresh_interval_seconds, 5.0)
        self.assertEqual(cfg.ui.daily_default_days, 7)
        self.assertEqual(cfg.ui.theme, "dark")
        self.assertEqual(cfg.logging.level, "INFO")
        self.assertEqual(cfg.logging.max_size_mb, 10.0)
        self.assertEqual(cfg.logging.backup_count, 5)

    # 3) 用户配置覆盖默认值
    def test_user_values_override_defaults(self):
        self.cfg_file.write_text(json.dumps({
            "llama_server": {"url": "http://127.0.0.1:1234"},
            "collector": {"poll_interval_seconds": 10},
            "web": {"port": 9999},
            "ui": {"theme": "light"},
        }), encoding="utf-8")
        loaded = cfgmod.load_config(self.cfg_file)
        self.assertEqual(loaded.config.llama_server.url, "http://127.0.0.1:1234")
        self.assertEqual(loaded.config.collector.poll_interval_seconds, 10.0)
        self.assertEqual(loaded.config.web.port, 9999)
        self.assertEqual(loaded.config.ui.theme, "light")
        self.assertFalse(loaded.has_errors)
        self.assertFalse(loaded.using_defaults)

    # 4) 缺失嵌套字段自动补默认
    def test_missing_nested_fields_fill_defaults(self):
        self.cfg_file.write_text(json.dumps({"llama_server": {"url": "http://127.0.0.1:1234"}}), encoding="utf-8")
        loaded = cfgmod.load_config(self.cfg_file)
        cfg = loaded.config
        self.assertEqual(cfg.llama_server.url, "http://127.0.0.1:1234")
        # 同节缺失字段 -> 默认
        self.assertEqual(cfg.llama_server.metrics_path, "/metrics")
        self.assertEqual(cfg.llama_server.timeout_seconds, 3.0)
        # 整个缺失的节 -> 默认
        self.assertEqual(cfg.web.port, 8765)
        self.assertEqual(cfg.collector.poll_interval_seconds, 5.0)

    # 5) 旧配置兼容新版本新增字段
    def test_old_config_compatible_with_new_fields(self):
        # 旧配置只有 url；新版本新增 metrics_path / timeout_seconds -> 不报错、新字段用默认
        self.cfg_file.write_text(json.dumps({"llama_server": {"url": "http://127.0.0.1:9091"}}), encoding="utf-8")
        loaded = cfgmod.load_config(self.cfg_file)
        self.assertFalse(loaded.has_errors)
        self.assertTrue(loaded.loaded)
        self.assertEqual(loaded.config.llama_server.metrics_path, "/metrics")
        self.assertEqual(loaded.config.llama_server.timeout_seconds, 3.0)

    # 6) 非法数值字段回退默认
    def test_invalid_number_falls_back(self):
        self.cfg_file.write_text(json.dumps({"collector": {"poll_interval_seconds": -20}}), encoding="utf-8")
        loaded = cfgmod.load_config(self.cfg_file)
        self.assertEqual(loaded.config.collector.poll_interval_seconds, 5.0)
        self.assertTrue(loaded.has_errors)
        self.assertTrue(any("poll_interval_seconds" in e for e in loaded.errors))
        # 其他合法字段仍正常
        self.assertEqual(loaded.config.web.port, 8765)
        self.assertTrue(loaded.loaded)
        self.assertFalse(loaded.using_defaults)

    # 7) 非法 web.port 回退默认
    def test_invalid_port_falls_back(self):
        self.cfg_file.write_text(json.dumps({"web": {"port": "abc"}}), encoding="utf-8")
        loaded = cfgmod.load_config(self.cfg_file)
        self.assertEqual(loaded.config.web.port, 8765)
        self.assertTrue(loaded.has_errors)
        self.assertTrue(any("web.port" in e for e in loaded.errors))

    # 8) 非法 ui.theme 回退默认
    def test_invalid_theme_falls_back(self):
        self.cfg_file.write_text(json.dumps({"ui": {"theme": "blue"}}), encoding="utf-8")
        loaded = cfgmod.load_config(self.cfg_file)
        self.assertEqual(loaded.config.ui.theme, "dark")
        self.assertTrue(loaded.has_errors)

    # 9) 损坏的 JSON 不覆盖原文件
    def test_corrupt_json_not_overwritten(self):
        broken = '{\n  "abc":\n'
        self.cfg_file.write_text(broken, encoding="utf-8")
        loaded = cfgmod.load_config(self.cfg_file)
        self.assertFalse(loaded.loaded)
        self.assertTrue(loaded.using_defaults)
        self.assertTrue(loaded.has_errors)
        self.assertEqual(self.cfg_file.read_text(encoding="utf-8"), broken)  # 原样保留
        self.assertEqual(loaded.config, AppConfig.default())

    def test_bom_prefixed_config_is_tolerated(self):
        # Windows 工具可能写出带 BOM 的 UTF-8 文件，必须正常解析
        self.cfg_file.write_bytes(b'\xef\xbb\xbf' + json.dumps({"web": {"port": 9123}}).encode("utf-8"))
        loaded = cfgmod.load_config(self.cfg_file)
        self.assertTrue(loaded.loaded)
        self.assertFalse(loaded.has_errors)
        self.assertEqual(loaded.config.web.port, 9123)

    def test_json_root_not_object(self):
        self.cfg_file.write_text("[1, 2, 3]", encoding="utf-8")
        loaded = cfgmod.load_config(self.cfg_file)
        self.assertFalse(loaded.loaded)
        self.assertTrue(loaded.has_errors)
        self.assertEqual(self.cfg_file.read_text(encoding="utf-8"), "[1, 2, 3]")

    def test_unknown_keys_kept_and_warned(self):
        # 未知字段：保留、告警、不致命
        self.cfg_file.write_text(json.dumps({"future_feature": 1, "web": {"host": "127.0.0.1", "future": 2}}), encoding="utf-8")
        loaded = cfgmod.load_config(self.cfg_file)
        self.assertFalse(loaded.has_errors)  # 未知字段不算错误
        self.assertIn("Unknown config key: future_feature", loaded.warnings)
        self.assertIn("Unknown config key: web.future", loaded.warnings)
        # 原文件保持不变（未知键保留）
        data = json.loads(self.cfg_file.read_text(encoding="utf-8"))
        self.assertEqual(data["future_feature"], 1)
        self.assertEqual(data["web"]["future"], 2)


class PathResolutionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    # 10) database.path 为空 -> %LOCALAPPDATA%\LlamaMonitor\monitor.db
    def test_database_path_empty_uses_localappdata(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.tmp / "lad")}):
            p = AppConfig.default().database_path
        self.assertEqual(p, Path(self.tmp / "lad" / "LlamaMonitor" / "monitor.db"))

    def test_database_path_explicit_wins(self):
        cfg = AppConfig.default()
        cfg.database.path = str(self.tmp / "custom" / "m.db")
        self.assertEqual(cfg.database_path, self.tmp / "custom" / "m.db")

    # 12) 环境变量 LLAMAMONITOR_CONFIG 覆盖默认路径
    def test_env_var_overrides_config_path(self):
        target = self.tmp / "elsewhere" / "cfg.json"
        with mock.patch.dict(os.environ, {"LLAMAMONITOR_CONFIG": str(target)}):
            loaded = cfgmod.load_config()
        self.assertEqual(loaded.path, target)
        self.assertTrue(target.is_file())  # 在指定位置自动创建


class MetricsUrlTests(unittest.TestCase):
    # 11) metrics URL 拼接
    def test_url_concatenation(self):
        self.assertEqual(build_metrics_url("http://127.0.0.1:9091", "/metrics"), "http://127.0.0.1:9091/metrics")
        self.assertEqual(build_metrics_url("http://127.0.0.1:9091/", "/metrics"), "http://127.0.0.1:9091/metrics")
        self.assertEqual(build_metrics_url("http://127.0.0.1:9091/base", "/metrics"), "http://127.0.0.1:9091/base/metrics")
        # 容忍 base 已带 metrics path 的旧式写法
        self.assertEqual(build_metrics_url("http://127.0.0.1:9091/metrics", "/metrics"), "http://127.0.0.1:9091/metrics")
        self.assertEqual(AppConfig.default().metrics_url, "http://127.0.0.1:9091/metrics")
        # 不允许双斜杠
        self.assertNotIn("//metrics", build_metrics_url("http://127.0.0.1:9091", "/metrics"))


class WalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._dbs: list[Database] = []

    def tearDown(self):
        for db in self._dbs:
            db.close()
        self._tmp.cleanup()

    def _journal_mode(self, db: Database) -> str:
        conn = db._connect()
        return conn.execute("PRAGMA journal_mode").fetchone()[0]

    def test_wal_enabled_by_default(self):
        db = Database(self.tmp / "wal.db")  # wal 默认 True
        self._dbs.append(db)
        self.assertEqual(self._journal_mode(db), "wal")

    def test_wal_disabled_when_config_says_so(self):
        db = Database(self.tmp / "nowal.db", wal=False)
        self._dbs.append(db)
        self.assertEqual(self._journal_mode(db), "delete")


class OverrideTests(unittest.TestCase):
    def test_apply_overrides_valid(self):
        cfg = make_config()
        cfgmod.apply_overrides(cfg, url="http://127.0.0.1:9091", db_path="d.db", interval=10.0)
        self.assertEqual(cfg.llama_server.url, "http://127.0.0.1:9091")
        self.assertEqual(cfg.database.path, "d.db")
        self.assertEqual(cfg.collector.poll_interval_seconds, 10.0)

    def test_apply_overrides_invalid_keeps_values(self):
        cfg = make_config()  # poll_interval 已被设为 3600.0
        before = (cfg.llama_server.url, cfg.database.path, cfg.collector.poll_interval_seconds)
        cfgmod.apply_overrides(cfg, url="not-a-url", db_path=None, interval=0.5)  # 0.5 < 1，非法
        self.assertEqual(cfg.llama_server.url, before[0])
        self.assertEqual(cfg.database.path, before[1])
        self.assertEqual(cfg.collector.poll_interval_seconds, before[2])  # 保持原值


class SetupLoggingTests(unittest.TestCase):
    def setUp(self):
        cfgmod.reset_logging()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        cfgmod.reset_logging()
        self._tmp.cleanup()

    def test_logs_written_to_file_under_localappdata(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.tmp)}):
            log = cfgmod.setup_logging()
            log.info("hello-phase7")
            for h in cfgmod.logging.getLogger().handlers:
                h.flush()
            log_file = self.tmp / "LlamaMonitor" / "logs" / "monitor.log"
            self.assertTrue(log_file.is_file())
            self.assertIn("hello-phase7", log_file.read_text(encoding="utf-8"))

    def test_setup_is_idempotent(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.tmp)}):
            cfgmod.setup_logging()
            cfgmod.setup_logging()
        handlers = [h for h in cfgmod.logging.getLogger().handlers if getattr(h, "_llamamonitor", False)]
        self.assertEqual(len(handlers), 2)  # 仅一对（文件 + 控制台），无重复

    def test_rotating_handler_uses_config(self):
        from config import LoggingConfig
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.tmp)}):
            cfgmod.setup_logging(LoggingConfig(level="DEBUG", max_size_mb=2, backup_count=7))
        root = cfgmod.logging.getLogger()
        file_handlers = [h for h in root.handlers if getattr(h, "_llamamonitor", False) and h.__class__.__name__ == "RotatingFileHandler"]
        self.assertEqual(len(file_handlers), 1)
        self.assertEqual(file_handlers[0].maxBytes, 2 * 1024 * 1024)
        self.assertEqual(file_handlers[0].backupCount, 7)
        self.assertEqual(root.level, cfgmod.logging.DEBUG)


class ApiConfigTests(unittest.TestCase):
    """/api/config 与 /api/status 的 config 状态（TestClient，无真实网络）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._dbs: list[Database] = []

    def tearDown(self):
        for db in self._dbs:
            db.close()
        self._tmp.cleanup()

    def _start(self, loaded: LoadedConfig):
        from fastapi.testclient import TestClient

        from collector import MetricsCollector
        from configutil import loopback_app
        from server import build_app

        cfg = make_config()
        db = Database(self.tmp / "api_cfg.db")
        self._dbs.append(db)
        collector = MetricsCollector(cfg, db)
        app = build_app(db, collector, loaded)
        # AUDIT-SEC-001：GET /api/config 现在 loopback-only，模拟本机请求
        client = TestClient(loopback_app(app))
        client.__enter__()
        return client

    def test_api_config_requires_loopback(self):
        """AUDIT-SEC-001：非 loopback 客户端 GET /api/config -> 403
        （响应含本地路径/llama 地址，web.host=0.0.0.0 时不该暴露给局域网）。"""
        from fastapi.testclient import TestClient

        from collector import MetricsCollector
        from server import build_app

        cfg = make_config()
        loaded = make_loaded(cfg, self.tmp)
        db = Database(self.tmp / "api_cfg_noloopback.db")
        self._dbs.append(db)
        collector = MetricsCollector(cfg, db)
        app = build_app(db, collector, loaded)
        client = TestClient(app)  # 裸 testclient = 非 loopback 地址
        client.__enter__()
        try:
            self.assertEqual(client.get("/api/config").status_code, 403)
        finally:
            client.__exit__(None, None, None)

    def test_api_config_shape_and_status_config_block(self):
        cfg = make_config()
        loaded = make_loaded(cfg, self.tmp)
        client = self._start(loaded)
        try:
            r = client.get("/api/config")
            self.assertEqual(r.status_code, 200)
            data = r.json()
            # Phase 8：完整六段 + paths（Settings 页面初始化用，值全部来自 config.py 校验后的配置）
            self.assertEqual(data["llama_server"]["url"], "http://127.0.0.1:9")
            self.assertEqual(data["llama_server"]["metrics_path"], "/metrics")
            self.assertEqual(data["llama_server"]["metrics_url"], "http://127.0.0.1:9/metrics")
            self.assertEqual(data["llama_server"]["timeout_seconds"], 3.0)
            self.assertEqual(data["collector"]["poll_interval_seconds"], 3600.0)
            self.assertEqual(data["collector"]["live_retention_hours"], 48.0)
            self.assertEqual(data["web"], {"host": "127.0.0.1", "port": 8765})
            self.assertEqual(data["database"], {"path": "", "wal": True})
            self.assertEqual(data["ui"]["theme"], "dark")
            self.assertEqual(data["ui"]["daily_default_days"], 7)
            self.assertEqual(data["logging"]["level"], "INFO")
            self.assertEqual(data["paths"]["config"], str(self.tmp / "config.json"))
            # 不直接返回原始文件：未知键不出现
            self.assertNotIn("unknown_extra", data)

            s = client.get("/api/status").json()
            self.assertEqual(s["config"]["path"], str(self.tmp / "config.json"))
            self.assertTrue(s["config"]["loaded"])
            self.assertFalse(s["config"]["using_defaults"])
            self.assertFalse(s["config"]["has_errors"])
        finally:
            client.__exit__(None, None, None)

    def test_status_config_error_state(self):
        # 模拟"配置文件损坏"时的状态：loaded=false / using_defaults=true / has_errors=true
        loaded = LoadedConfig(
            config=make_config(),
            path=self.tmp / "config.json",
            loaded=False,
            using_defaults=True,
            has_errors=True,
            errors=["invalid JSON: {broken}"],
        )
        client = self._start(loaded)
        try:
            s = client.get("/api/status").json()
            self.assertFalse(s["config"]["loaded"])
            self.assertTrue(s["config"]["using_defaults"])
            self.assertTrue(s["config"]["has_errors"])
        finally:
            client.__exit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
