# 测量 1.3.3 TestClient lifespan 启动耗时（临时诊断）
import sys
import os
import tempfile
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

from fastapi.testclient import TestClient
from collector import MetricsCollector
from configutil import make_config
from db import Database
from server import build_app
from config import LoadedConfig


def t(label, fn):
    t0 = time.time()
    fn()
    print("%.2fs  %s" % (time.time() - t0, label))


tmp = Path(tempfile.mkdtemp())
db = Database(tmp / "t.db", wal=False)
cfg = make_config()
loaded = LoadedConfig(config=cfg, path=tmp / "config.json", loaded=True,
                      using_defaults=False, has_errors=False)
collector = MetricsCollector(cfg, db)

app = build_app(db, collector, loaded, None)


def open_client():
    with TestClient(app) as c:
        r = c.get("/api/status")
        assert r.status_code == 200


t("TestClient lifespan 启动 + /api/status", open_client)
db.close()
