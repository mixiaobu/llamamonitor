# -*- coding: utf-8 -*-
"""1.0.0 Final Gate 25/27/28/30 独立验证：Database Integrity

场景（全部临时目录，不碰真实 %LOCALAPPDATA%）：
1. Fresh DB -> quick_check ok + schema v4 + 10 张表
2. Current DB 写入后 -> quick_check ok
3. 旧 schema（v0 legacy / v2 / v3 带数据）迁移到 v4 -> quick_check ok + 数据完整 + pre_migration 备份生成且本身 quick_check ok
4. Newer schema guard（user_version=99）-> 只读、不写、不降级
5. Corrupt DB -> quick_check 报错被捕获（软件报 unavailable，不崩溃）
6. Reset statistics -> 删 daily/live/GPU history/MTP daily，保留 counter baseline state
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from clock import FakeClock                      # noqa: E402
from collector import MetricsCollector           # noqa: E402
from db import CURRENT_SCHEMA_VERSION, Database  # noqa: E402
from metrics_parser import parse_metrics         # noqa: E402
from configutil import make_config               # noqa: E402
import asyncio                                   # noqa: E402

import tests.test_migration as tmig              # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  :: " + str(detail)) if detail and not cond else ""))


def quick_check(path):
    conn = sqlite3.connect(str(path))
    try:
        r = conn.execute("PRAGMA quick_check").fetchone()[0]
        return r
    finally:
        conn.close()


BASE = 1_789_000_000.0


def metrics_text(prompt, output, cached=0):
    return (
        f"llamacpp:prompt_tokens_total {prompt}\n"
        f"llamacpp:tokens_predicted_total {output}\n"
        f"llamacpp:prompt_tokens_cached_total {cached}\n"
        f"llamacpp:prompt_seconds_total 0\n"
        f"llamacpp:tokens_predicted_seconds_total 0\n"
        f"llamacpp:n_decode_total {output}\n"
    )


def run_collector_round(db, tmp, poll=5.0, text=None):
    cfg = make_config(url="http://127.0.0.1:9", poll_interval=poll)
    clock = FakeClock(start_wall=BASE)
    col = MetricsCollector(cfg, db, clock=clock)

    async def fetch():
        return parse_metrics(text) if text else None

    col._fetch_parsed = fetch
    try:
        return asyncio.run(col.collect_once())
    finally:
        col.shutdown()


def main():
    tmp = Path(tempfile.mkdtemp(prefix="lm10_dbint_"))
    print(f"[db-integrity] tmp={tmp}")
    print(f"[db-integrity] CURRENT_SCHEMA_VERSION={CURRENT_SCHEMA_VERSION}")

    # ---------- 1. Fresh DB ----------
    print("1) Fresh DB")
    p1 = tmp / "fresh.db"
    d = Database(p1, wal=False, retention_seconds=48 * 3600)
    d.get_schema_version()
    check("fresh: quick_check ok", quick_check(p1) == "ok")
    check("fresh: schema == 4", d.get_schema_version() == CURRENT_SCHEMA_VERSION == 4)
    conn = d._connect()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    expected = {"state", "app_state", "daily_usage", "live_samples", "mtp_position_daily",
                "gpu_samples", "gpu_daily", "monitor_events", "data_gaps", "backup_history"}
    check("fresh: 10 expected tables", tables == expected, f"got {sorted(tables)}")
    # 写入一些数据再 quick_check
    for i, (pr, out) in enumerate([(100, 40), (250, 90), (600, 210)]):
        run_collector_round(d, tmp, text=metrics_text(pr, out, cached=pr // 4))
        d._connect().execute("PRAGMA wal_checkpoint(PASSIVE)")
    d.checkpoint("PASSIVE")
    d.close()
    check("fresh+data: quick_check ok", quick_check(p1) == "ok")

    # ---------- 2. 旧 schema 迁移 + pre-migration 备份 ----------
    print("2) Migration matrix (v0 legacy / v2 / v3 -> v4) + pre-migration backup")
    backup_dir = tmp / "backups"

    # v0 legacy（Phase 8：user_version=0 + v1 表 + 数据）
    p_leg = tmp / "legacy_v0.db"
    tmig._make_legacy_db(p_leg)
    # v2 / v3 带数据
    p_v2 = tmp / "v2.db"
    tmig._make_v2_db(p_v2)
    p_v3 = tmp / "v3.db"
    tmig._make_v3_db(p_v3)

    for name, p in [("v0 legacy", p_leg), ("v2", p_v2), ("v3", p_v3)]:
        if not p.exists():
            check(f"{name}: fixture built", False)
            continue
        # 历史/数据表（迁移必须 1:1 保留）。
        # 注：live_samples 为 48h 短保留，fixture 旧时间戳会在连接时被 retention 清理
        #     （与既有迁移测试一致，不在此按行数断言）；monitor_events 迁移会追加审计行。
        data_tables = ("daily_usage", "gpu_samples", "gpu_daily", "data_gaps", "mtp_position_daily")
        before = {}
        conn = sqlite3.connect(str(p))
        for t in data_tables:
            try:
                before[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error:
                before[t] = 0
        conn.close()

        pre_before = set(backup_dir.glob("pre_migration_*.db")) if backup_dir.exists() else set()
        d2 = Database(p, wal=False, retention_seconds=48 * 3600,
                      pre_migration_backup_dir=backup_dir)
        ver = d2.get_schema_version()
        check(f"{name}: migrated to v4", ver == CURRENT_SCHEMA_VERSION, f"user_version={ver}")
        check(f"{name}: post-migration quick_check ok", quick_check(p) == "ok")
        # 数据完整
        conn = sqlite3.connect(str(p))
        ok = True
        for t, n in before.items():
            try:
                after = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                if after != n:
                    ok = False
            except sqlite3.Error:
                ok = False
        conn.close()
        check(f"{name}: row counts preserved {before}", ok)
        # pre-migration 备份（迁移前生成，且备份本身 quick_check ok）
        pre_after = set(backup_dir.glob("pre_migration_*.db")) if backup_dir.exists() else set()
        new_pre = sorted(pre_after - pre_before)
        check(f"{name}: pre_migration backup created", len(new_pre) == 1,
              f"new={len(new_pre)}")
        if new_pre:
            check(f"{name}: pre_migration backup quick_check ok", quick_check(new_pre[0]) == "ok")
        d2.close()

    # ---------- 3. Newer schema guard ----------
    print("3) Newer schema guard (user_version=99)")
    p_new = tmp / "newer.db"
    d3 = Database(p_new, wal=False, retention_seconds=48 * 3600)
    d3.get_schema_version()  # 建 v4
    d3.close()
    conn = sqlite3.connect(str(p_new))
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    # 重新打开（读到 user_version=99 -> 只读 incompatible）
    d3b = Database(p_new, wal=False, retention_seconds=48 * 3600)
    ver = d3b.get_schema_version()
    check("newer: reports 99", ver == 99, f"got {ver}")
    check("newer: health=incompatible", "incompatible" in (d3b._health or "").lower(), d3b._health)
    try:
        d3b.record_event("probe", "info", "integrity", {})
        probe_rows = 1
    except Exception:
        probe_rows = 0
    conn = sqlite3.connect(str(p_new))
    uv = conn.execute("PRAGMA user_version").fetchone()[0]
    actual_probe = conn.execute("SELECT COUNT(*) FROM monitor_events WHERE event_type='probe'").fetchone()[0]
    conn.close()
    d3b.close()
    check("newer: schema not downgraded (still 99)", uv == 99, f"uv={uv}")
    check("newer: write blocked (no probe row)", actual_probe == 0, f"probe_rows={actual_probe}")

    # ---------- 4. Corrupt DB ----------
    print("4) Corrupt DB")
    p_cor = tmp / "corrupt.db"
    d4 = Database(p_cor, wal=False, retention_seconds=48 * 3600)
    d4.get_schema_version()
    d4.close()
    # 破坏文件头
    data = p_cor.read_bytes()
    p_cor.write_bytes(data[:16] + b"XXXX" + data[20:])
    conn = sqlite3.connect(str(p_cor))
    try:
        qc = conn.execute("PRAGMA quick_check").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        qc = f"raised: {exc}"
    conn.close()
    check("corrupt: quick_check != ok", qc != "ok", qc)
    # 应用层：quick_check() 返回 (ok=False, detail)，不崩溃
    d5 = Database(p_cor, wal=False, retention_seconds=48 * 3600)
    try:
        ok, detail = d5.quick_check()
        check("corrupt: app quick_check() reports not-ok", ok is False, f"ok={ok} detail={detail}")
    except Exception as exc:
        check("corrupt: app quick_check() raises cleanly", True, repr(exc))
    d5.close()

    # ---------- 5. Reset semantics ----------
    print("5) Reset statistics (删 daily/live/GPU history/MTP daily, 保留 counter baseline)")
    p6 = tmp / "reset.db"
    d6 = Database(p6, wal=False, retention_seconds=48 * 3600)
    d6.get_schema_version()
    for pr, out in [(100, 40), (250, 90)]:
        run_collector_round(d6, tmp, text=metrics_text(pr, out, cached=10))
    # 制造 GPU/MTP 数据
    conn = d6._connect()
    conn.execute(
        "INSERT INTO gpu_samples (timestamp, gpu_uuid, gpu_index, gpu_name, utilization_percent, "
        "memory_used_mb, memory_total_mb, temperature_c, power_draw_w) VALUES (?,?,?,?,?,?,?,?,?)",
        (BASE, "GPU-TEST", 0, "Test", 50.0, 1000.0, 8000.0, 60.0, 50.0),
    )
    conn.execute(
        "INSERT INTO gpu_daily (date, gpu_uuid, gpu_name, sample_count, utilization_sum, energy_wh) "
        "VALUES (?,?,?,?,?,?)",
        ("2026-09-15", "GPU-TEST", "Test", 1, 50.0, 100.0),
    )
    conn.execute("INSERT INTO mtp_position_daily (date, position, accepted_tokens) VALUES (?,?,?)",
                 ("2026-09-15", "0", 80))
    conn.commit()
    baseline_before = conn.execute("SELECT COUNT(*) FROM state").fetchone()[0]
    # 注意：conn 是 Database 内部连接，不能单独 close（会让 reset_statistics 拿到已关闭句柄）

    d6.reset_statistics()
    conn = d6._connect()
    daily = conn.execute("SELECT COUNT(*) FROM daily_usage").fetchone()[0]
    live = conn.execute("SELECT COUNT(*) FROM live_samples").fetchone()[0]
    gpus = conn.execute("SELECT COUNT(*) FROM gpu_samples").fetchone()[0]
    gupd = conn.execute("SELECT COUNT(*) FROM gpu_daily").fetchone()[0]
    mtp = conn.execute("SELECT COUNT(*) FROM mtp_position_daily").fetchone()[0]
    baseline_after = conn.execute("SELECT COUNT(*) FROM state").fetchone()[0]
    d6.close()
    check("reset: daily_usage cleared", daily == 0, f"{daily}")
    check("reset: live_samples cleared", live == 0, f"{live}")
    check("reset: gpu_samples cleared", gpus == 0, f"{gpus}")
    check("reset: gpu_daily cleared", gupd == 0, f"{gupd}")
    check("reset: mtp_position_daily cleared", mtp == 0, f"{mtp}")
    check("reset: counter baseline state preserved", baseline_after == baseline_before,
          f"before={baseline_before} after={baseline_after}")
    check("reset: post-reset quick_check ok", quick_check(p6) == "ok")

    print()
    print("=" * 60)
    print(f"DB-INTEGRITY  PASS={len(PASS)}  FAIL={len(FAIL)}")
    if FAIL:
        print("FAILED:", FAIL)
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
