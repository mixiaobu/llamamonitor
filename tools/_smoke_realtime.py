# 1.3.3 /api/realtime 冒烟验证（临时脚本；跑完可删）
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from config import LoadedConfig, atomic_write_json, get_default_config
from configutil import loopback_app, make_config
from db import Database
from fastapi.testclient import TestClient
from server import build_app
from strata_collector import StrataCollector
from collector import MetricsCollector

STRATA_FIX = ROOT / "tests" / "fixtures" / "strata"


def _strata_doc(name):
    return (STRATA_FIX / name).read_text(encoding="utf-8")


def smoke_strata():
    tmp = Path(tempfile.mkdtemp())
    cfg_file = tmp / "config.json"
    atomic_write_json(cfg_file, get_default_config())
    cfg = make_config()
    cfg.llama_server.provider = "strata"
    db = Database(tmp / "smoke.db")
    collector = StrataCollector(cfg, db)
    eps = {
        "/metrics": (lambda: (_strata_doc("metrics_idle.json"), True)),
        "/status": (lambda: (_strata_doc("status_idle.json"), True)),
        "/slots": (lambda: (_strata_doc("slots.json"), True)),
        "/health": (lambda: (_strata_doc("health_ok.json"), True)),
        "/props": (lambda: (_strata_doc("props.json"), True)),
        "/v1/models": (lambda: (_strata_doc("models.json"), True)),
    }

    def _rec(name, ok, err):
        if name:
            collector._record_endpoint(
                name, collector.clock.now(), collector.clock.monotonic(),
                None if ok else err)

    async def ft(p, l, headers=None, timeout=None, endpoint=None):
        f = eps.get(p)
        if f is None:
            _rec(endpoint, False, "unreachable")
            return None, False, "unreachable"
        t, ok = f()
        _rec(endpoint, ok, "temporarily_unavailable" if not ok else None)
        return t, ok, None

    async def fj(p, l, timeout=None, endpoint=None):
        t, ok, err = await ft(p, l, timeout=timeout, endpoint=endpoint)
        if not ok or t is None:
            return None, False, err
        try:
            return json.loads(t), True, None
        except Exception:
            _rec(endpoint, False, "invalid_response")
            return None, False, "invalid_response"

    collector._fetch_text = ft
    collector._fetch_json = fj
    loaded = LoadedConfig(config=cfg, path=cfg_file, loaded=True,
                          using_defaults=False, has_errors=False)
    app = build_app(db, collector, loaded, runtime=None)
    client = TestClient(loopback_app(app))
    with client:
        # 1) /api/realtime 形态
        r = client.get("/api/realtime")
        print("realtime status:", r.status_code, "cache:", r.headers.get("cache-control"))
        data = r.json()
        required = ["provider", "server_online", "activity", "throughput",
                    "requests", "slots", "tokens", "freshness", "errors",
                    "diagnostics", "sequence"]
        for k in required:
            assert k in data, f"missing key {k}"
        act = data["activity"]
        for k in ["active", "state", "phase", "source", "confidence"]:
            assert k in act, f"activity missing {k}"
        th = data["throughput"]
        for k in ["current_decode_tps", "current_prompt_tps",
                  "average_decode_tps", "average_prompt_tps"]:
            assert k in th, f"throughput missing {k}"
        fresh = data["freshness"]
        for k in ["sample_timestamp", "age_ms", "source"]:
            assert k in fresh, f"freshness missing {k}"
        diag = data["diagnostics"]
        for k in ["hot_loop_interval_ms", "last_hot_duration_ms",
                  "hot_overrun", "source"]:
            assert k in diag, f"diagnostics missing {k}"
        assert data["provider"] == "strata"
        assert data["activity"]["active"] in (True, False, None)
        # idle fixture：running=0 -> active False
        print("activity:", json.dumps(act, ensure_ascii=False))
        print("throughput:", json.dumps(th, ensure_ascii=False))
        print("freshness:", json.dumps(fresh, ensure_ascii=False))
        # 2) sequence 递增
        seq1 = data["sequence"]
        import asyncio
        asyncio.run(collector.collect_once())
        r2 = client.get("/api/realtime")
        seq2 = r2.json()["sequence"]
        assert seq2 > seq1, f"sequence not increasing: {seq1} -> {seq2}"
        print(f"sequence: {seq1} -> {seq2} (increasing OK)")
        # 3) 其它实时端点 no-store
        for path in ["/api/status", "/api/runtime"]:
            rr = client.get(path)
            print(f"{path} cache-control:", rr.headers.get("cache-control"))
            assert rr.headers.get("cache-control") == "no-store"
        print("SMOKE STRATA OK")
    db.close()
    shutil_rmtree(tmp)


import shutil
def shutil_rmtree(p):
    try:
        shutil.rmtree(p)
    except Exception:
        pass


def smoke_llama():
    tmp = Path(tempfile.mkdtemp())
    cfg_file = tmp / "config.json"
    atomic_write_json(cfg_file, get_default_config())
    cfg = make_config()
    cfg.llama_server.provider = "llamacpp"
    db = Database(tmp / "smoke_llama.db")
    collector = MetricsCollector(cfg, db)

    METRICS = (
        "# HELP llamacpp:requests_processing x\n"
        "# TYPE llamacpp:requests_processing gauge\n"
        "llamacpp:requests_processing 1\n"
        "llamacpp:requests_deferred 0\n"
        "llamacpp:n_tokens_max 4096\n"
        "llamacpp:kv_cache_usage_ratio 0.12\n"
    )
    SLOTS = json.dumps([{
        "id": 0, "id_task": 7, "state": "processing", "is_processing": True,
        "n_prompt_tokens": 1000, "n_prompt_tokens_processed": 1000,
        "n_decoded": 42,
        "params": {"max_tokens": 2048, "temp": 0.7},
    }])

    from metrics_parser import parse_metrics
    PARSED = parse_metrics(METRICS)

    async def fp():
        return PARSED

    async def fslots():
        from llama_runtime_collector import sanitize_slot
        return [sanitize_slot(s) for s in json.loads(SLOTS)]

    collector._fetch_parsed = fp
    collector._fetch_slots = fslots
    loaded = LoadedConfig(config=cfg, path=cfg_file, loaded=True,
                          using_defaults=False, has_errors=False)
    app = build_app(db, collector, loaded, runtime=None)
    client = TestClient(loopback_app(app))
    with client:
        r = client.get("/api/realtime")
        data = r.json()
        print("llama realtime status:", r.status_code, "cache:",
              r.headers.get("cache-control"))
        print("llama activity:", json.dumps(data["activity"], ensure_ascii=False))
        print("llama throughput:", json.dumps(data["throughput"], ensure_ascii=False))
        print("llama requests:", json.dumps(data["requests"], ensure_ascii=False))
        print("llama slots:", json.dumps(data["slots"], ensure_ascii=False))
        print("llama tokens:", json.dumps(data["tokens"], ensure_ascii=False))
        assert data["provider"] == "llamacpp"
        assert data["activity"]["active"] is True
        assert data["requests"]["processing"] == 1
        assert data["slots"]["active"] == 1
        assert data["tokens"]["generated"] == 42
        assert data["tokens"]["prompt"] == 1000
        assert data["tokens"]["max_tokens"] == 2048
        print("SMOKE LLAMA OK")
    db.close()
    shutil_rmtree(tmp)


if __name__ == "__main__":
    smoke_strata()
    smoke_llama()
    print("ALL SMOKE OK")
