"""E2E：真实 HTTP 路径验证 1.3.1 五次容忍（本地假 Strata 服务，不动真实 9091）。

阶段：
  1) 假 Strata 起服 -> 采集 2 轮 -> ready + 记录 live 值
  2) 杀假服务（全部端点不可达）-> 4 轮：数据/状态必须保留（不翻转）
  3) 第 5 轮：切离线 + unreachable
  4) 恢复假服务 -> 1 轮：恢复 ready + 数据回来

运行：python tools/_e2e_blip.py
"""

import asyncio
import http.server
import json
import socketserver
import threading
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from clock import default_clock
from configutil import make_config
from db import Database
from strata_collector import StrataCollector, STATE_READY

FIX = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "strata"
PORT = 9099
URL = f"http://127.0.0.1:{PORT}"

ENDPOINTS = {
    "/metrics": "metrics_idle.json",
    "/status": "status_idle.json",
    "/slots": "slots.json",
    "/health": "health_ok.json",
    "/props": "props.json",
    "/v1/models": "models.json",
}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        name = ENDPOINTS.get(self.path)
        if name is None:
            self.send_response(404)
            self.end_headers()
            return
        body = (FIX / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json" if self.path != "/metrics" else "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def start_server():
    server = socketserver.TCPServer(("127.0.0.1", PORT), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


def main():
    import tempfile
    tmp = tempfile.TemporaryDirectory()
    cfg = make_config(url=URL, poll_interval=1.0)
    cfg.llama_server.provider = "strata"
    db = Database(str(Path(tmp.name) / "blip.db"), wal=False)
    col = StrataCollector(cfg, db, clock=default_clock)

    failures = []

    def check(label, cond, detail=""):
        mark = "PASS" if cond else "FAIL"
        print(f"  [{mark}] {label} {detail}")
        if not cond:
            failures.append(label)

    async def run(n):
        for _ in range(n):
            await col.collect_once()
            await asyncio.sleep(1.05)

    server = start_server()
    try:
        print("阶段 1：服务在线，采集 2 轮")
        asyncio.run(run(2))
        snap0 = col.last_snapshot
        live0 = json.loads(json.dumps(col.live)) if col.live else None
        check("state=ready", col.server_state == STATE_READY, f"(实际 {col.server_state})")
        check("snapshot online", bool(snap0 and snap0.get("online")))
        check("live 有值", live0 is not None, f"(running={live0.get('running') if live0 else None})")
        rp0 = snap0.get("requests_processing")
        st0 = snap0["strata"]["state"]

        print("阶段 2：杀服务，4 轮容忍期")
        server.shutdown()
        server.server_close()
        for i in range(4):
            asyncio.run(run(1))
            s = col.last_snapshot
            if i < 3:
                check(f"第 {i+1} 轮保持在线表现", bool(s.get("online")), f"(online={s.get('online')})")
                check(f"第 {i+1} 轮状态不翻转", col.server_state == STATE_READY, f"(实际 {col.server_state})")
                check(f"第 {i+1} 轮快照值保留", s.get("requests_processing") == rp0
                      and s["strata"]["state"] == st0,
                      f"(rp={s.get('requests_processing')} state={s['strata']['state']})")
            else:
                # 第 4 次失败仍在容忍期（<5）
                check("第 4 轮仍在容忍期", bool(s.get("online")) and col.server_state == STATE_READY,
                      f"(online={s.get('online')} state={col.server_state})")

        print("阶段 3：第 5 轮 -> 离线 + unreachable")
        asyncio.run(run(1))
        s5 = col.last_snapshot
        check("online=False", s5.get("online") is False)
        check("state=unreachable", col.server_state == "unreachable", f"(实际 {col.server_state})")

        print("阶段 4：恢复服务，1 轮 -> ready")
        server = start_server()
        asyncio.run(run(1))
        s6 = col.last_snapshot
        check("online=True", bool(s6.get("online")))
        check("state=ready", col.server_state == STATE_READY, f"(实际 {col.server_state})")
        check("数据恢复", s6.get("requests_processing") == rp0,
              f"(rp={s6.get('requests_processing')})")
        events = db.get_events(limit=50)
        offs = [e for e in events if e["event_type"] == "strata_offline"]
        ons = [e for e in events if e["event_type"] == "strata_online"]
        check("strata_offline 事件恰 1 条", len(offs) == 1, f"(实际 {len(offs)})")
        check("strata_online 事件恰 1 条", len(ons) == 1, f"(实际 {len(ons)})")
    finally:
        try:
            server.shutdown()
        except Exception:
            pass
        asyncio.run(col.aclose())
        db.close()
        tmp.cleanup()

    print()
    if failures:
        print(f"E2E BLIP: FAIL（{len(failures)} 项未通过: {failures}）")
        raise SystemExit(1)
    print("E2E BLIP: 全部通过")


if __name__ == "__main__":
    main()
